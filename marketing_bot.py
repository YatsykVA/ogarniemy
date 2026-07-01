#!/usr/bin/env python3
"""
Ogarniemy marketing bot.

First version:
- Telegram opt-in subscribers.
- Telegram groups watched by keywords.
- Admin broadcasts to subscribers or saved groups.
- Lightweight Facebook Messenger webhook skeleton for opted-in users.

The bot intentionally does not send private messages to people who did not
start the bot first. Group keyword replies are public and rate-limited.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


DB_PATH = os.environ.get("MARKETING_BOT_DB", "marketing_bot.db")
CONFIG_PATH = os.environ.get("MARKETING_BOT_CONFIG", "marketing_bot_config.json")
FACEBOOK_PAGE_ACCESS_TOKEN = os.environ.get("FACEBOOK_PAGE_ACCESS_TOKEN", "")
FACEBOOK_VERIFY_TOKEN = os.environ.get("FACEBOOK_VERIFY_TOKEN", "ogarniemy-verify")
PRESENTATION_URL = os.environ.get("PRESENTATION_URL", "https://ogarniemy.pro")
TELEGRAM_SEND_INTERVAL_SECONDS = float(os.environ.get("TELEGRAM_SEND_INTERVAL_SECONDS", "2"))
TELEGRAM_USERBOT_CLIENT: Any | None = None
TELEGRAM_USERBOT_LOOP: asyncio.AbstractEventLoop | None = None
TELEGRAM_NEXT_SEND_AT = 0.0
PENDING_TELEGRAM_LOGINS: dict[str, dict[str, Any]] = {}


CLIENT_TEXT = (
    "Нужен мастер? Создайте заявку бесплатно, и Ogarniemy быстро поможет найти "
    f"исполнителя в вашем городе.\n\nРегистрация: {PRESENTATION_URL}"
)
WORKER_TEXT = (
    "Вы мастер? Получайте новые заказы рядом с вами. Первый месяц бесплатно.\n\n"
    f"Регистрация: {PRESENTATION_URL}"
)
GROUP_TEXT = (
    "Ogarniemy помогает быстро найти мастера для любой задачи. Для клиентов "
    "бесплатно, для мастеров первый месяц бесплатно.\n\n"
    f"{PRESENTATION_URL}"
)


def admin_ids() -> set[int]:
    raw = os.environ.get("ADMIN_TELEGRAM_IDS", "")
    result = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            result.add(int(part))
    return result


def config_path() -> str:
    if os.path.isabs(CONFIG_PATH):
        return CONFIG_PATH
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), CONFIG_PATH)


def read_config() -> dict[str, str]:
    path = config_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_config(values: dict[str, str]) -> None:
    current = read_config()
    current.update({key: str(value) for key, value in values.items() if value is not None})
    with open(config_path(), "w", encoding="utf-8") as file:
        json.dump(current, file, ensure_ascii=False, indent=2)
    write_db_config(values)


def read_db_config() -> dict[str, str]:
    try:
        with db() as conn:
            rows = conn.execute("select key, value from app_config").fetchall()
    except sqlite3.Error:
        return {}
    return {str(row["key"]): str(row["value"]) for row in rows}


def write_db_config(values: dict[str, str]) -> None:
    try:
        with db() as conn:
            conn.execute("create table if not exists app_config (key text primary key, value text not null)")
            for key, value in values.items():
                if value is not None:
                    conn.execute(
                        "insert into app_config(key, value) values(?, ?) on conflict(key) do update set value = excluded.value",
                        (str(key), str(value)),
                    )
    except sqlite3.Error:
        return


def telegram_config() -> dict[str, str]:
    config = {**read_db_config(), **read_config()}
    return {
        "api_id": os.environ.get("TELEGRAM_API_ID") or config.get("telegram_api_id", ""),
        "api_hash": os.environ.get("TELEGRAM_API_HASH") or config.get("telegram_api_hash", ""),
        "session": os.environ.get("TELEGRAM_SESSION") or config.get("telegram_session", "ogarniemy_userbot"),
        "session_string": os.environ.get("TELEGRAM_SESSION_STRING") or config.get("telegram_session_string", ""),
        "phone": config.get("telegram_phone", ""),
    }


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            create table if not exists telegram_subscribers (
              chat_id integer primary key,
              username text,
              display_name text,
              role text default 'unknown',
              city text default '',
              created_at integer not null,
              stopped_at integer
            );

            create table if not exists telegram_groups (
              chat_id integer primary key,
              title text,
              city text default '',
              keywords text default '',
              exclude_keywords text default '',
              enabled integer default 1,
              created_at integer not null
            );

            create table if not exists keyword_hits (
              id integer primary key autoincrement,
              group_chat_id integer not null,
              keyword text not null,
              user_id integer,
              username text,
              message text,
              created_at integer not null
            );

            create table if not exists keyword_reply_locks (
              group_chat_id integer not null,
              keyword text not null,
              last_sent_at integer not null,
              primary key(group_chat_id, keyword)
            );

            create table if not exists keyword_processed_messages (
              group_chat_id integer not null,
              message_id integer not null,
              keyword text not null,
              created_at integer not null,
              primary key(group_chat_id, message_id, keyword)
            );

            create table if not exists facebook_subscribers (
              psid text primary key,
              role text default 'unknown',
              city text default '',
              created_at integer not null,
              stopped_at integer
            );

            create table if not exists marketing_cities (
              id integer primary key autoincrement,
              platform text not null,
              name text not null,
              enabled integer default 1,
              created_at integer not null,
              unique(platform, name)
            );

            create table if not exists marketing_messages (
              id integer primary key autoincrement,
              platform text not null,
              title text not null,
              audience text default 'all',
              body text not null,
              image_url text default '',
              enabled integer default 1,
              created_at integer not null,
              updated_at integer not null
            );

            create table if not exists marketing_schedules (
              id integer primary key autoincrement,
              platform text not null,
              city text default '',
              send_time text not null,
              message_id integer,
              enabled integer default 1,
              last_sent_date text default '',
              created_at integer not null
            );

            create table if not exists marketing_logs (
              id integer primary key autoincrement,
              platform text not null,
              target_type text not null,
              target_id text default '',
              city text default '',
              action text not null,
              status text not null,
              detail text default '',
              created_at integer not null
            );

            create table if not exists facebook_targets (
              id integer primary key autoincrement,
              name text not null,
              city text default '',
              target_id text default '',
              notes text default '',
              enabled integer default 1,
              created_at integer not null
            );

            create table if not exists app_config (
              key text primary key,
              value text not null
            );

            create table if not exists telegram_outbox (
              id integer primary key autoincrement,
              action text not null default 'send',
              chat_id text not null,
              body text not null,
              image_url text default '',
              reply_to_message_id integer,
              source_chat_id text default '',
              source_message_id text default '',
              attempts integer default 0,
              not_before integer not null,
              status text default 'pending',
              detail text default '',
              created_at integer not null,
              sent_at integer
            );
            """
        )
        ensure_column(conn, "telegram_groups", "watch_enabled", "integer default 0")
        ensure_column(conn, "telegram_groups", "exclude_keywords", "text default ''")
        ensure_column(conn, "telegram_groups", "target_chat_id", "text default ''")
        ensure_column(conn, "telegram_groups", "response_message_id", "integer")
        ensure_column(conn, "telegram_groups", "notes", "text default ''")
        ensure_column(conn, "keyword_hits", "message_id", "integer")
        ensure_column(conn, "marketing_schedules", "target_id", "text default ''")
        ensure_column(conn, "facebook_targets", "keywords", "text default ''")
        ensure_column(conn, "facebook_targets", "action", "text default 'same_group'")
        ensure_column(conn, "facebook_targets", "response_message_id", "integer")
        ensure_column(conn, "telegram_outbox", "action", "text not null default 'send'")
        ensure_column(conn, "telegram_outbox", "source_chat_id", "text default ''")
        ensure_column(conn, "telegram_outbox", "source_message_id", "text default ''")


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"pragma table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"alter table {table} add column {column} {definition}")


def http_json(url: str, payload: dict[str, Any] | None = None) -> Any:
    data = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
      data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as res:
        body = res.read().decode("utf-8")
        return json.loads(body) if body else {}


def require_userbot_config() -> tuple[int, str]:
    config = telegram_config()
    if not config["api_id"] or not config["api_hash"]:
        raise RuntimeError("Set TELEGRAM_API_ID and TELEGRAM_API_HASH first.")
    return int(config["api_id"]), config["api_hash"]


def telegram_session_value(session: str = "", session_string: str = "") -> Any:
    if session_string:
        from telethon.sessions import StringSession
        return StringSession(session_string)
    return session or telegram_config()["session"]


def load_telethon() -> Any:
    try:
        from telethon import TelegramClient, events, utils
        from telethon.errors import FloodWaitError
        from telethon.tl.types import MessageMediaPhoto
    except ImportError as exc:
        raise RuntimeError("Install Telethon first: python -m pip install telethon") from exc
    return TelegramClient, events, utils, MessageMediaPhoto, FloodWaitError


async def userbot_client() -> Any:
    global TELEGRAM_USERBOT_CLIENT
    TelegramClient, _events, _utils, _MessageMediaPhoto, _FloodWaitError = load_telethon()
    api_id, api_hash = require_userbot_config()
    session = telegram_config()["session"]
    if TELEGRAM_USERBOT_CLIENT is None:
        TELEGRAM_USERBOT_CLIENT = TelegramClient(telegram_session_value(session, telegram_config()["session_string"]), api_id, api_hash)
    if not TELEGRAM_USERBOT_CLIENT.is_connected():
        await TELEGRAM_USERBOT_CLIENT.connect()
    if not await TELEGRAM_USERBOT_CLIENT.is_user_authorized():
        raise RuntimeError(
            "Telegram userbot session is not authorized. Run: python marketing_bot.py --telegram-login"
        )
    return TELEGRAM_USERBOT_CLIENT


def queue_telegram_ad(
    chat_id: int | str,
    text: str,
    image_url: str = "",
    reply_to_message_id: int | None = None,
    not_before: int | None = None,
    detail: str = "",
) -> None:
    now = int(time.time())
    with db() as conn:
        conn.execute(
            """
            insert into telegram_outbox(action, chat_id, body, image_url, reply_to_message_id, not_before, detail, created_at)
            values('send', ?, ?, ?, ?, ?, ?, ?)
            """,
            (str(chat_id), text, image_url or "", reply_to_message_id, int(not_before or now), detail[:1000], now),
        )


def queue_telegram_forward(
    source_chat_id: int | str,
    message_id: int | str,
    target_chat_id: int | str,
    not_before: int | None = None,
    detail: str = "",
) -> None:
    now = int(time.time())
    with db() as conn:
        conn.execute(
            """
            insert into telegram_outbox(action, chat_id, body, source_chat_id, source_message_id, not_before, detail, created_at)
            values('forward', ?, '', ?, ?, ?, ?, ?)
            """,
            (str(target_chat_id), str(source_chat_id), str(message_id), int(not_before or now), detail[:1000], now),
        )

async def wait_for_telegram_slot(max_wait: float = 15) -> bool:
    global TELEGRAM_NEXT_SEND_AT
    now = time.time()
    delay = max(0.0, TELEGRAM_NEXT_SEND_AT - now)
    if delay > max_wait:
        return False
    if delay:
        await asyncio.sleep(delay)
    TELEGRAM_NEXT_SEND_AT = time.time() + TELEGRAM_SEND_INTERVAL_SECONDS
    return True


def set_telegram_flood_wait(seconds: int | float) -> int:
    global TELEGRAM_NEXT_SEND_AT
    wait_seconds = max(1, int(seconds))
    TELEGRAM_NEXT_SEND_AT = max(TELEGRAM_NEXT_SEND_AT, time.time() + wait_seconds + 3)
    return wait_seconds


async def async_send_telegram(
    chat_id: int | str,
    text: str,
    reply_to_message_id: int | None = None,
    *,
    queue_on_limit: bool = True,
    max_wait: float = 15,
) -> bool:
    text = repair_cyrillic_mojibake(str(text or ""))
    try:
        if not await wait_for_telegram_slot(max_wait=max_wait):
            not_before = int(TELEGRAM_NEXT_SEND_AT) + 1
            if queue_on_limit:
                queue_telegram_ad(chat_id, text, "", reply_to_message_id, not_before, "telegram_rate_limit")
            print(f"telegram userbot send delayed for {chat_id}: rate limit until {not_before}")
            return False
        client = await userbot_client()
        await client.send_message(chat_id, text, reply_to=reply_to_message_id, link_preview=True)
        return True
    except Exception as exc:
        _TelegramClient, _events, _utils, _MessageMediaPhoto, FloodWaitError = load_telethon()
        if isinstance(exc, FloodWaitError):
            wait_seconds = set_telegram_flood_wait(getattr(exc, "seconds", 60))
            if queue_on_limit:
                queue_telegram_ad(chat_id, text, "", reply_to_message_id, int(time.time()) + wait_seconds + 3, f"flood_wait_{wait_seconds}")
            print(f"telegram userbot send delayed for {chat_id}: flood wait {wait_seconds} seconds")
            return False
        print(f"telegram userbot send failed for {chat_id}: {exc}")
        return False


async def async_send_telegram_ad(
    chat_id: int | str,
    text: str,
    image_url: str = "",
    reply_to_message_id: int | None = None,
    *,
    queue_on_limit: bool = True,
    max_wait: float = 15,
) -> bool:
    text = repair_cyrillic_mojibake(str(text or ""))
    if image_url:
        try:
            if not await wait_for_telegram_slot(max_wait=max_wait):
                not_before = int(TELEGRAM_NEXT_SEND_AT) + 1
                if queue_on_limit:
                    queue_telegram_ad(chat_id, text, image_url, reply_to_message_id, not_before, "telegram_rate_limit")
                print(f"telegram userbot photo delayed for {chat_id}: rate limit until {not_before}")
                return False
            client = await userbot_client()
            await client.send_file(chat_id, image_url, caption=text[:1024], reply_to=reply_to_message_id)
            return True
        except Exception as exc:
            _TelegramClient, _events, _utils, _MessageMediaPhoto, FloodWaitError = load_telethon()
            if isinstance(exc, FloodWaitError):
                wait_seconds = set_telegram_flood_wait(getattr(exc, "seconds", 60))
                if queue_on_limit:
                    queue_telegram_ad(chat_id, text, image_url, reply_to_message_id, int(time.time()) + wait_seconds + 3, f"flood_wait_{wait_seconds}")
                print(f"telegram userbot photo delayed for {chat_id}: flood wait {wait_seconds} seconds")
                return False
            print(f"telegram userbot photo failed for {chat_id}: {exc}")
    return await async_send_telegram(chat_id, text, reply_to_message_id, queue_on_limit=queue_on_limit, max_wait=max_wait)


async def async_forward_telegram_message(
    source_chat_id: int | str,
    message_id: int | str | None,
    target_chat_id: int | str,
    *,
    queue_on_limit: bool = True,
    max_wait: float = 15,
) -> bool:
    if not message_id:
        print(f"telegram userbot forward failed from {source_chat_id} to {target_chat_id}: missing message id")
        return False
    try:
        if not await wait_for_telegram_slot(max_wait=max_wait):
            not_before = int(TELEGRAM_NEXT_SEND_AT) + 1
            if queue_on_limit:
                queue_telegram_forward(source_chat_id, message_id, target_chat_id, not_before, "telegram_rate_limit_forward")
            print(f"telegram userbot forward delayed from {source_chat_id} to {target_chat_id}: rate limit until {not_before}")
            return False
        client = await userbot_client()
        await client.forward_messages(int(target_chat_id), int(message_id), from_peer=int(source_chat_id))
        return True
    except Exception as exc:
        _TelegramClient, _events, _utils, _MessageMediaPhoto, FloodWaitError = load_telethon()
        if isinstance(exc, FloodWaitError):
            wait_seconds = set_telegram_flood_wait(getattr(exc, "seconds", 60))
            if queue_on_limit:
                queue_telegram_forward(
                    source_chat_id,
                    message_id,
                    target_chat_id,
                    int(time.time()) + wait_seconds + 3,
                    f"flood_wait_forward_{wait_seconds}",
                )
            print(f"telegram userbot forward delayed from {source_chat_id} to {target_chat_id}: flood wait {wait_seconds} seconds")
            return False
        print(f"telegram userbot forward failed from {source_chat_id} to {target_chat_id}: {exc}")
        return False

def run_userbot_coroutine(coro: Any) -> Any:
    if TELEGRAM_USERBOT_LOOP and TELEGRAM_USERBOT_LOOP.is_running():
        future = asyncio.run_coroutine_threadsafe(coro, TELEGRAM_USERBOT_LOOP)
        return future.result(timeout=90)
    return asyncio.run(coro)


def send_telegram(chat_id: int, text: str, reply_to_message_id: int | None = None) -> bool:
    return bool(run_userbot_coroutine(async_send_telegram(chat_id, text, reply_to_message_id)))


def send_telegram_ad(chat_id: int, text: str, image_url: str = "", reply_to_message_id: int | None = None) -> bool:
    return bool(run_userbot_coroutine(async_send_telegram_ad(chat_id, text, image_url, reply_to_message_id)))


def log_marketing(platform: str, target_type: str, target_id: str, city: str, action: str, status: str, detail: str = "") -> None:
    with db() as conn:
        conn.execute(
            """
            insert into marketing_logs(platform, target_type, target_id, city, action, status, detail, created_at)
            values(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (platform, target_type, str(target_id), city, action, status, detail[:1000], int(time.time())),
        )


def repair_cyrillic_mojibake(text: str) -> str:
    """Repair common UTF-8/CP1251 mojibake in Russian/Cyrillic text."""
    if not isinstance(text, str):
        return text
    markers = ("Р", "С", "Ð", "Ñ", "вЂ", "В°", "Вµ")
    if not any(marker in text for marker in markers):
        return text
    result = []
    i = 0
    while i < len(text):
        if i + 1 < len(text):
            try:
                first = text[i].encode("cp1251")
                second = text[i + 1].encode("cp1251")
            except UnicodeEncodeError:
                first = second = b""
            if first in {b"\xd0", b"\xd1"} and second and 0x80 <= second[0] <= 0xBF:
                try:
                    result.append((first + second).decode("utf-8"))
                    i += 2
                    continue
                except UnicodeDecodeError:
                    pass
        result.append(text[i])
        i += 1
    return "".join(result)


def is_admin(user_id: int | None) -> bool:
    return bool(user_id and user_id in admin_ids())


def person_name(user: dict[str, Any]) -> str:
    return " ".join(
        part for part in [user.get("first_name", ""), user.get("last_name", "")] if part
    ).strip() or user.get("username", "") or str(user.get("id", ""))


def upsert_subscriber(message: dict[str, Any], role: str = "unknown") -> None:
    chat = message.get("chat", {})
    user = message.get("from", {})
    now = int(time.time())
    with db() as conn:
        conn.execute(
            """
            insert into telegram_subscribers(chat_id, username, display_name, role, created_at, stopped_at)
            values(?, ?, ?, ?, ?, null)
            on conflict(chat_id) do update set
              username = excluded.username,
              display_name = excluded.display_name,
              role = case when excluded.role = 'unknown' then telegram_subscribers.role else excluded.role end,
              stopped_at = null
            """,
            (
                int(chat["id"]),
                user.get("username", ""),
                person_name(user),
                role,
                now,
            ),
        )


def set_subscriber_city(chat_id: int, city: str) -> None:
    with db() as conn:
        conn.execute(
            "update telegram_subscribers set city = ? where chat_id = ?",
            (city.strip(), chat_id),
        )


def stop_subscriber(chat_id: int) -> None:
    with db() as conn:
        conn.execute(
            "update telegram_subscribers set stopped_at = ? where chat_id = ?",
            (int(time.time()), chat_id),
        )


def save_group(message: dict[str, Any], city: str = "", keywords: str = "", exclude_keywords: str = "") -> None:
    chat = message.get("chat", {})
    with db() as conn:
        conn.execute(
            """
            insert into telegram_groups(chat_id, title, city, keywords, exclude_keywords, watch_enabled, created_at)
            values(?, ?, ?, ?, ?, 1, ?)
            on conflict(chat_id) do update set
              title = excluded.title,
              city = coalesce(nullif(excluded.city, ''), telegram_groups.city),
              keywords = coalesce(nullif(excluded.keywords, ''), telegram_groups.keywords),
              exclude_keywords = coalesce(nullif(excluded.exclude_keywords, ''), telegram_groups.exclude_keywords),
              watch_enabled = 1,
              enabled = 1
            """,
            (
                int(chat["id"]),
                chat.get("title", ""),
                city.strip(),
                keywords.strip(),
                exclude_keywords.strip(),
                int(time.time()),
            ),
        )


def group_watch_config(chat_id: int) -> sqlite3.Row | None:
    with db() as conn:
        row = conn.execute(
            """
            select chat_id, title, keywords, exclude_keywords, target_chat_id, response_message_id
            from telegram_groups
            where chat_id = ? and watch_enabled = 1
            """,
            (chat_id,),
        ).fetchone()
    return row


def group_keywords(chat_id: int) -> list[str]:
    row = group_watch_config(chat_id)
    if not row or not row["keywords"]:
        return []
    return [item.strip().lower() for item in row["keywords"].split(",") if item.strip()]


def matching_exclude_keyword(text: str, exclude_keywords: str | None) -> str:
    if not exclude_keywords:
        return ""
    lower = (text or "").lower()
    for item in exclude_keywords.split(","):
        word = item.strip().lower()
        if word and word in lower:
            return item.strip()
    return ""


def marketing_message(message_id: int | None, platform: str = "telegram") -> sqlite3.Row | None:
    if not message_id:
        return None
    with db() as conn:
        return conn.execute(
            "select body, image_url, title from marketing_messages where id = ? and platform = ? and enabled = 1",
            (message_id, platform),
        ).fetchone()


def can_process_keyword_message(chat_id: int, message_id: int | str | None, keyword: str) -> bool:
    if message_id is None:
        return True
    now = int(time.time())
    with db() as conn:
        try:
            conn.execute(
                """
                insert into keyword_processed_messages(group_chat_id, message_id, keyword, created_at)
                values(?, ?, ?, ?)
                """,
                (chat_id, int(message_id), keyword, now),
            )
        except sqlite3.IntegrityError:
            return False
    return True


def record_keyword_hit(message: dict[str, Any], keyword: str) -> None:
    chat = message.get("chat", {})
    user = message.get("from", {})
    with db() as conn:
        conn.execute(
            """
            insert into keyword_hits(group_chat_id, keyword, user_id, username, message, created_at, message_id)
            values(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(chat["id"]),
                keyword,
                user.get("id"),
                user.get("username", ""),
                message.get("text", "")[:1000],
                int(time.time()),
                message.get("message_id"),
            ),
        )


def found_ad_notification(message: dict[str, Any], keyword: str, response: sqlite3.Row | None = None) -> str:
    chat = message.get("chat", {})
    user = message.get("from", {})
    chat_title = str(chat.get("title") or chat.get("username") or chat.get("id") or "").strip()
    username = str(user.get("username") or "").strip()
    display_name = " ".join(
        part
        for part in (str(user.get("first_name") or "").strip(), str(user.get("last_name") or "").strip())
        if part
    )
    author = f"@{username}" if username else display_name or str(user.get("id") or "не указан")
    text = str(message.get("text") or "").strip() or "(без текста)"
    lines = [
        f"Найдено объявление по ключевому слову: {keyword}",
        "",
        f"Группа: {chat_title}",
        f"Автор: {author}",
        "",
        "Текст объявления:",
        text,
    ]
    if response:
        lines.extend(["", "Материал для комментария:", str(response["body"] or "").strip()])
        if response["image_url"]:
            lines.append(f"Картинка: {response['image_url']}")
    return "\n".join(lines)[:4096]


def broadcast_subscribers(role: str, text: str) -> tuple[int, int]:
    query = "select chat_id from telegram_subscribers where stopped_at is null"
    params: tuple[Any, ...] = ()
    if role in {"clients", "client"}:
        query += " and role = ?"
        params = ("client",)
    elif role in {"workers", "worker", "masters"}:
        query += " and role = ?"
        params = ("worker",)
    with db() as conn:
        rows = conn.execute(query, params).fetchall()
    sent = sum(1 for row in rows if send_telegram(int(row["chat_id"]), text))
    return sent, len(rows)


async def async_broadcast_subscribers(role: str, text: str) -> tuple[int, int]:
    query = "select chat_id from telegram_subscribers where stopped_at is null"
    params: tuple[Any, ...] = ()
    if role in {"clients", "client"}:
        query += " and role = ?"
        params = ("client",)
    elif role in {"workers", "worker", "masters"}:
        query += " and role = ?"
        params = ("worker",)
    with db() as conn:
        rows = conn.execute(query, params).fetchall()
    sent = 0
    for row in rows:
        if await async_send_telegram(int(row["chat_id"]), text):
            sent += 1
    return sent, len(rows)


def post_to_groups(text: str, image_url: str = "", city: str = "", target_id: str = "") -> tuple[int, int]:
    with db() as conn:
        if target_id:
            rows = conn.execute(
                "select chat_id, title, city from telegram_groups where enabled = 1 and chat_id = ?",
                (int(target_id),),
            ).fetchall()
        elif city:
            rows = conn.execute(
                "select chat_id, title, city from telegram_groups where enabled = 1 and lower(city) = lower(?)",
                (city,),
            ).fetchall()
        else:
            rows = conn.execute(
                "select chat_id, title, city from telegram_groups where enabled = 1"
            ).fetchall()
    sent = 0
    for row in rows:
        ok = send_telegram_ad(int(row["chat_id"]), text, image_url)
        if ok:
            sent += 1
        log_marketing("telegram", "group", row["chat_id"], row["city"] or city, "post", "sent" if ok else "failed", row["title"] or "")
    return sent, len(rows)


async def async_post_to_groups(text: str, image_url: str = "", city: str = "", target_id: str = "") -> tuple[int, int]:
    with db() as conn:
        if target_id:
            rows = conn.execute(
                "select chat_id, title, city from telegram_groups where enabled = 1 and chat_id = ?",
                (int(target_id),),
            ).fetchall()
        elif city:
            rows = conn.execute(
                "select chat_id, title, city from telegram_groups where enabled = 1 and lower(city) = lower(?)",
                (city,),
            ).fetchall()
        else:
            rows = conn.execute(
                "select chat_id, title, city from telegram_groups where enabled = 1"
            ).fetchall()
    sent = 0
    for row in rows:
        ok = await async_send_telegram_ad(int(row["chat_id"]), text, image_url)
        if ok:
            sent += 1
        log_marketing("telegram", "group", row["chat_id"], row["city"] or city, "post", "sent" if ok else "failed", row["title"] or "")
    return sent, len(rows)


async def async_process_telegram_outbox(limit: int = 5) -> None:
    if TELEGRAM_NEXT_SEND_AT > time.time() + 1:
        return
    now = int(time.time())
    with db() as conn:
        rows = conn.execute(
            """
            select * from telegram_outbox
            where status = 'pending' and not_before <= ?
            order by id
            limit ?
            """,
            (now, limit),
        ).fetchall()
    for row in rows:
        if TELEGRAM_NEXT_SEND_AT > time.time() + 1:
            return
        action = str(row["action"] or "send") if "action" in row.keys() else "send"
        if action == "forward":
            ok = await async_forward_telegram_message(
                row["source_chat_id"],
                row["source_message_id"],
                row["chat_id"],
                queue_on_limit=False,
                max_wait=15,
            )
            log_action = "queued_forward"
        else:
            ok = await async_send_telegram_ad(
                row["chat_id"],
                row["body"],
                row["image_url"] or "",
                row["reply_to_message_id"],
                queue_on_limit=False,
                max_wait=15,
            )
            log_action = "queued_post"
        with db() as conn:
            if ok:
                conn.execute(
                    "update telegram_outbox set status = 'sent', sent_at = ? where id = ?",
                    (int(time.time()), row["id"]),
                )
                log_marketing("telegram", "outbox", row["chat_id"], "", log_action, "sent", f"queued #{row['id']}")
            else:
                attempts = int(row["attempts"] or 0) + 1
                next_try = int(max(time.time() + 300, TELEGRAM_NEXT_SEND_AT + 1))
                status = "failed" if attempts >= 10 else "pending"
                conn.execute(
                    "update telegram_outbox set attempts = ?, not_before = ?, status = ?, detail = ? where id = ?",
                    (attempts, next_try, status, "retry_after_rate_limit", row["id"]),
                )
                log_marketing("telegram", "outbox", row["chat_id"], "", log_action, status, f"queued #{row['id']} attempt {attempts}")

def due_schedules(platform: str = "telegram") -> list[sqlite3.Row]:
    now = time.localtime()
    current_time = time.strftime("%H:%M", now)
    today = time.strftime("%Y-%m-%d", now)
    with db() as conn:
        return conn.execute(
            """
            select schedules.*, messages.body, messages.image_url, messages.title
            from marketing_schedules schedules
            join marketing_messages messages on messages.id = schedules.message_id
            where schedules.platform = ?
              and schedules.enabled = 1
              and messages.enabled = 1
              and schedules.send_time <= ?
              and coalesce(schedules.last_sent_date, '') != ?
            order by schedules.send_time, schedules.id
            """,
            (platform, current_time, today),
        ).fetchall()


def run_due_schedules() -> None:
    today = time.strftime("%Y-%m-%d", time.localtime())
    for schedule in due_schedules("telegram"):
        sent, total = post_to_groups(schedule["body"], schedule["image_url"] or "", schedule["city"] or "", schedule["target_id"] or "")
        with db() as conn:
            conn.execute(
                "update marketing_schedules set last_sent_date = ? where id = ?",
                (today, schedule["id"]),
            )
        log_marketing(
            "telegram",
            "schedule",
            schedule["id"],
            schedule["city"] or "",
            "scheduled_post",
            "sent" if sent else "failed",
            f"{schedule['title']}: материал готов к публикации/рекламе",
        )
    for schedule in due_schedules("facebook"):
        with db() as conn:
            conn.execute(
                "update marketing_schedules set last_sent_date = ? where id = ?",
                (today, schedule["id"]),
            )
        log_marketing(
            "facebook",
            "schedule",
            schedule["id"],
            schedule["city"] or "",
            "scheduled_prepare",
            "prepared",
            f"{schedule['title']}: материал готов к публикации/рекламе",
        )


async def async_run_due_schedules() -> None:
    await async_process_telegram_outbox()
    today = time.strftime("%Y-%m-%d", time.localtime())
    for schedule in due_schedules("telegram"):
        sent, total = await async_post_to_groups(
            schedule["body"],
            schedule["image_url"] or "",
            schedule["city"] or "",
            schedule["target_id"] or "",
        )
        with db() as conn:
            conn.execute(
                "update marketing_schedules set last_sent_date = ? where id = ?",
                (today, schedule["id"]),
            )
        log_marketing(
            "telegram",
            "schedule",
            schedule["id"],
            schedule["city"] or "",
            "scheduled_post",
            "sent" if sent else "failed",
            f"{schedule['title']}: материал готов к публикации/рекламе",
        )
    for schedule in due_schedules("facebook"):
        with db() as conn:
            conn.execute(
                "update marketing_schedules set last_sent_date = ? where id = ?",
                (today, schedule["id"]),
            )
        log_marketing(
            "facebook",
            "schedule",
            schedule["id"],
            schedule["city"] or "",
            "scheduled_prepare",
            "prepared",
            f"{schedule['title']}: материал готов к публикации/рекламе",
        )
    await async_process_telegram_outbox()


def stats_text() -> str:
    with db() as conn:
        subs = conn.execute(
            "select role, count(*) count from telegram_subscribers where stopped_at is null group by role"
        ).fetchall()
        groups = conn.execute(
            "select count(*) count from telegram_groups where enabled = 1"
        ).fetchone()["count"]
        hits = conn.execute("select count(*) count from keyword_hits").fetchone()["count"]
    lines = ["Статистика Ogarniemy bot:"]
    for row in subs:
        lines.append(f"- {row['role']}: {row['count']}")
    lines.append(f"- группы: {groups}")
    lines.append(f"- найдено по ключевым словам: {hits}")
    return "\n".join(lines)


async def handle_private_message(message: dict[str, Any]) -> None:
    chat_id = int(message["chat"]["id"])
    user_id = message.get("from", {}).get("id")
    text = (message.get("text") or "").strip()
    lower = text.lower()

    if lower.startswith("/start"):
        upsert_subscriber(message)
        await async_send_telegram(
            chat_id,
            "Здравствуйте! Выберите, кто вы:\n/client - клиент\n/worker - мастер\n\n"
            "Можно указать город: /city Warszawa",
        )
        return
    if lower.startswith("/client"):
        upsert_subscriber(message, "client")
        await async_send_telegram(chat_id, CLIENT_TEXT)
        return
    if lower.startswith("/worker") or lower.startswith("/master"):
        upsert_subscriber(message, "worker")
        await async_send_telegram(chat_id, WORKER_TEXT)
        return
    if lower.startswith("/city"):
        city = text[5:].strip()
        if not city:
            await async_send_telegram(chat_id, "Напишите так: /city Warszawa")
            return
        upsert_subscriber(message)
        set_subscriber_city(chat_id, city)
        await async_send_telegram(chat_id, f"Город сохранен: {city}")
        return
    if lower.startswith("/stop"):
        stop_subscriber(chat_id)
        await async_send_telegram(chat_id, "Рассылка отключена.")
        return

    if not is_admin(user_id):
        await async_send_telegram(chat_id, "Команды: /client, /worker, /city, /stop")
        return

    if lower.startswith("/stats"):
        await async_send_telegram(chat_id, stats_text())
        return
    if lower.startswith("/broadcast "):
        parts = text.split(" ", 2)
        if len(parts) < 3:
            await async_send_telegram(chat_id, "Формат: /broadcast all|clients|workers текст")
            return
        sent, total = await async_broadcast_subscribers(parts[1].lower(), parts[2])
        await async_send_telegram(chat_id, f"Отправлено подписчикам: {sent}/{total}")
        return
    if lower.startswith("/postgroups "):
        sent, total = await async_post_to_groups(text.split(" ", 1)[1])
        await async_send_telegram(chat_id, f"Опубликовано в группах: {sent}/{total}")
        return
    if lower.startswith("/groups"):
        with db() as conn:
            rows = conn.execute(
                "select chat_id, title, city, keywords from telegram_groups where enabled = 1 order by title"
            ).fetchall()
        if not rows:
            await async_send_telegram(chat_id, "Группы пока не добавлены.")
        else:
            await async_send_telegram(
                chat_id,
                "\n".join(
                    f"{row['title']} ({row['chat_id']}), {row['city']}: {row['keywords']}"
                    for row in rows
                ),
            )
        return

    await async_send_telegram(
        chat_id,
        "Админ-команды:\n"
        "/stats\n"
        "/broadcast all|clients|workers текст\n"
        "/postgroups текст\n"
        "В группе: /watch город | слово1, слово2",
    )


async def handle_group_message(message: dict[str, Any]) -> None:
    chat_id = int(message["chat"]["id"])
    user_id = message.get("from", {}).get("id")
    text = (message.get("text") or "").strip()
    lower = text.lower()

    if lower.startswith("/watch") and is_admin(user_id):
        payload = text[6:].strip()
        exclude_keywords = ""
        if "|" in payload:
            parts = [part.strip() for part in payload.split("|")]
            city = parts[0] if len(parts) > 0 else ""
            keywords = parts[1] if len(parts) > 1 else ""
            exclude_keywords = parts[2] if len(parts) > 2 else ""
        else:
            city, keywords = "", payload
        save_group(message, city=city, keywords=keywords, exclude_keywords=exclude_keywords)
        await async_send_telegram(
            chat_id,
            "Группа добавлена. Я буду смотреть ключевые слова и отвечать публично, "
            "с ограничением по частоте.",
        )
        return

    config = group_watch_config(chat_id)
    keywords = group_keywords(chat_id)
    if not keywords:
        return
    for keyword in keywords:
        if keyword in lower:
            if can_process_keyword_message(chat_id, message.get("message_id"), keyword):
                record_keyword_hit(message, keyword)
                excluded_word = matching_exclude_keyword(text, config["exclude_keywords"] if config else "")
                if excluded_word:
                    log_marketing(
                        "telegram",
                        "group",
                        chat_id,
                        "",
                        "keyword_excluded",
                        "skipped",
                        f"keyword: {keyword}, excluded by: {excluded_word}",
                    )
                    return
                response = marketing_message(config["response_message_id"] if config else None)
                target_chat_id = str(config["target_chat_id"] or "").strip() if config else ""
                if target_chat_id == "__same_group__":
                    target_chat_id = ""
                try:
                    reply_chat_id = int(target_chat_id) if target_chat_id else chat_id
                except ValueError:
                    log_marketing("telegram", "group", chat_id, "", "keyword_reply", "failed", f"bad target chat id: {target_chat_id}")
                    return
                reply_to = message.get("message_id") if reply_chat_id == chat_id else None
                if target_chat_id:
                    ok = await async_forward_telegram_message(chat_id, message.get("message_id"), reply_chat_id)
                    log_marketing(
                        "telegram",
                        "group",
                        chat_id,
                        "",
                        "keyword_forward",
                        "sent" if ok else "failed",
                        f"to {reply_chat_id}, keyword: {keyword}",
                    )
                elif response:
                    await async_send_telegram_ad(reply_chat_id, response["body"], response["image_url"] or "", reply_to)
                else:
                    await async_send_telegram(reply_chat_id, GROUP_TEXT, reply_to)
            return


async def telethon_message_to_bot_dict(event: Any) -> dict[str, Any]:
    _TelegramClient, _events, utils, _MessageMediaPhoto, _FloodWaitError = load_telethon()
    chat = await event.get_chat()
    sender = await event.get_sender()
    chat_id = utils.get_peer_id(chat)
    sender_id = getattr(sender, "id", None)
    chat_type = "private"
    if event.is_group:
        chat_type = "supergroup"
    elif event.is_channel:
        chat_type = "channel"
    return {
        "message_id": event.message.id,
        "text": event.raw_text or "",
        "chat": {
            "id": chat_id,
            "type": chat_type,
            "title": getattr(chat, "title", "") or "",
        },
        "from": {
            "id": sender_id,
            "username": getattr(sender, "username", "") or "",
            "first_name": getattr(sender, "first_name", "") or "",
            "last_name": getattr(sender, "last_name", "") or "",
        },
    }


async def handle_telegram_update(update: dict[str, Any]) -> None:
    message = update.get("message") or update.get("edited_message")
    if not message or "chat" not in message:
        return
    chat_type = message["chat"].get("type")
    if chat_type == "private":
        await handle_private_message(message)
    elif chat_type in {"group", "supergroup", "channel"}:
        await handle_group_message(message)


async def run_telegram_userbot() -> None:
    global TELEGRAM_USERBOT_LOOP
    init_db()
    TELEGRAM_USERBOT_LOOP = asyncio.get_running_loop()
    TelegramClient, events, _utils, _MessageMediaPhoto, _FloodWaitError = load_telethon()
    api_id, api_hash = require_userbot_config()
    config = telegram_config()
    client = TelegramClient(telegram_session_value(config["session"], config["session_string"]), api_id, api_hash)

    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError(
                "Telegram userbot session is not authorized. Run: python marketing_bot.py --telegram-login"
            )
        globals()["TELEGRAM_USERBOT_CLIENT"] = client

        @client.on(events.NewMessage())
        async def on_new_message(event: Any) -> None:
            try:
                if event.out and not (event.raw_text or "").strip().startswith("/"):
                    return
                message = await telethon_message_to_bot_dict(event)
                await handle_telegram_update({"message": message})
            except Exception as exc:
                print(f"telegram userbot event error: {exc}")

        async def schedule_loop() -> None:
            while True:
                try:
                    await async_run_due_schedules()
                except Exception as exc:
                    print(f"schedule error: {exc}")
                await asyncio.sleep(30)

        asyncio.create_task(schedule_loop())
        me = await client.get_me()
        print(f"Telegram userbot started as @{getattr(me, 'username', '') or getattr(me, 'id', '')}.")
        await client.run_until_disconnected()
    finally:
        await client.disconnect()


def run_telegram_login() -> None:
    TelegramClient, _events, _utils, _MessageMediaPhoto, _FloodWaitError = load_telethon()
    api_id, api_hash = require_userbot_config()

    async def login() -> None:
        config = telegram_config()
        client = TelegramClient(telegram_session_value(config["session"], config["session_string"]), api_id, api_hash)
        await client.start()
        try:
            me = await client.get_me()
            print(f"Telegram session saved for @{getattr(me, 'username', '') or getattr(me, 'id', '')}.")
        finally:
            await client.disconnect()

    asyncio.run(login())


async def async_start_telegram_login(api_id: str, api_hash: str, phone: str, session: str = "") -> dict[str, Any]:
    TelegramClient, _events, _utils, _MessageMediaPhoto, _FloodWaitError = load_telethon()
    clean_api_id = str(api_id).strip()
    clean_api_hash = str(api_hash).strip()
    clean_phone = str(phone).strip()
    clean_session = str(session or "ogarniemy_userbot").strip()
    if not clean_api_id or not clean_api_hash or not clean_phone:
        raise RuntimeError("api_id, api_hash and phone are required.")
    login_id = os.urandom(12).hex()
    client = TelegramClient(telegram_session_value("", ""), int(clean_api_id), clean_api_hash)
    await client.connect()
    try:
        sent = await client.send_code_request(clean_phone)
        pending_session_string = client.session.save()
    finally:
        await client.disconnect()
    PENDING_TELEGRAM_LOGINS[login_id] = {
        "api_id": clean_api_id,
        "api_hash": clean_api_hash,
        "phone": clean_phone,
        "session": clean_session,
        "session_string": pending_session_string,
        "phone_code_hash": sent.phone_code_hash,
        "created_at": int(time.time()),
    }
    return {"loginId": login_id, "phone": clean_phone}


def start_telegram_login(api_id: str, api_hash: str, phone: str, session: str = "") -> dict[str, Any]:
    return asyncio.run(async_start_telegram_login(api_id, api_hash, phone, session))


async def async_complete_telegram_login(login_id: str, code: str, password: str = "") -> dict[str, Any]:
    TelegramClient, _events, _utils, _MessageMediaPhoto, _FloodWaitError = load_telethon()
    try:
        from telethon.errors import SessionPasswordNeededError
    except ImportError as exc:
        raise RuntimeError("Install Telethon first: python -m pip install telethon") from exc
    pending = PENDING_TELEGRAM_LOGINS.get(str(login_id))
    if not pending:
        raise RuntimeError("Login request expired. Send the code again.")
    client = TelegramClient(telegram_session_value("", pending.get("session_string", "")), int(pending["api_id"]), pending["api_hash"])
    await client.connect()
    try:
        try:
            await client.sign_in(
                phone=pending["phone"],
                code=str(code).strip(),
                phone_code_hash=pending["phone_code_hash"],
            )
        except SessionPasswordNeededError:
            if not password:
                return {"ok": False, "passwordRequired": True}
            await client.sign_in(password=str(password))
        me = await client.get_me()
        session_string = client.session.save()
    finally:
        await client.disconnect()
    write_config(
        {
            "telegram_api_id": pending["api_id"],
            "telegram_api_hash": pending["api_hash"],
            "telegram_session": pending["session"],
            "telegram_session_string": session_string,
            "telegram_phone": pending["phone"],
        }
    )
    PENDING_TELEGRAM_LOGINS.pop(str(login_id), None)
    return {
        "ok": True,
        "user": {
            "id": getattr(me, "id", ""),
            "username": getattr(me, "username", "") or "",
            "firstName": getattr(me, "first_name", "") or "",
            "lastName": getattr(me, "last_name", "") or "",
        },
    }


def complete_telegram_login(login_id: str, code: str, password: str = "") -> dict[str, Any]:
    return asyncio.run(async_complete_telegram_login(login_id, code, password))


def telegram_login_status() -> dict[str, Any]:
    config = telegram_config()
    session = config["session"]
    session_path = session if os.path.isabs(session) else os.path.join(os.path.dirname(os.path.abspath(__file__)), session)
    if not session_path.endswith(".session"):
        session_path += ".session"
    return {
        "configured": bool(config["api_id"] and config["api_hash"]),
        "session": session,
        "sessionExists": bool(config.get("session_string")) or os.path.exists(session_path),
        "phone": config.get("phone", ""),
    }


def run_telegram_polling() -> None:
    asyncio.run(run_telegram_userbot())


def facebook_send(psid: str, text: str) -> bool:
    if not FACEBOOK_PAGE_ACCESS_TOKEN:
        return False
    url = "https://graph.facebook.com/v20.0/me/messages?" + urllib.parse.urlencode(
        {"access_token": FACEBOOK_PAGE_ACCESS_TOKEN}
    )
    try:
        http_json(url, {"recipient": {"id": psid}, "message": {"text": text}})
        return True
    except Exception as exc:
        print(f"facebook send failed for {psid}: {exc}")
        return False


def handle_facebook_event(event: dict[str, Any]) -> None:
    sender = event.get("sender", {}).get("id")
    message_text = (event.get("message", {}).get("text") or "").strip().lower()
    if not sender:
        return
    with db() as conn:
        conn.execute(
            """
            insert into facebook_subscribers(psid, created_at, stopped_at)
            values(?, ?, null)
            on conflict(psid) do update set stopped_at = null
            """,
            (sender, int(time.time())),
        )
    if "мастер" in message_text or "master" in message_text:
        facebook_send(sender, WORKER_TEXT)
    else:
        facebook_send(sender, CLIENT_TEXT)


class MarketingWebhook(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        url = urllib.parse.urlparse(self.path)
        if url.path != "/facebook/webhook":
            self.send_response(404)
            self.end_headers()
            return
        params = urllib.parse.parse_qs(url.query)
        token = params.get("hub.verify_token", [""])[0]
        challenge = params.get("hub.challenge", [""])[0]
        if token == FACEBOOK_VERIFY_TOKEN:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(challenge.encode("utf-8"))
        else:
            self.send_response(403)
            self.end_headers()

    def do_POST(self) -> None:
        if self.path != "/facebook/webhook":
            self.send_response(404)
            self.end_headers()
            return
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        payload = json.loads(raw.decode("utf-8") or "{}")
        for entry in payload.get("entry", []):
            for event in entry.get("messaging", []):
                handle_facebook_event(event)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")


def run_facebook_webhook(host: str, port: int) -> None:
    init_db()
    server = ThreadingHTTPServer((host, port), MarketingWebhook)
    print(f"Facebook webhook listening on http://{host}:{port}/facebook/webhook")
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--telegram", action="store_true", help="run Telegram userbot listener")
    parser.add_argument("--telegram-login", action="store_true", help="authorize and save Telegram userbot session")
    parser.add_argument("--facebook-webhook", action="store_true", help="run Facebook webhook server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    args = parser.parse_args()

    if args.telegram_login:
        run_telegram_login()
    elif args.facebook_webhook:
        run_facebook_webhook(args.host, args.port)
    else:
        run_telegram_polling()


if __name__ == "__main__":
    main()


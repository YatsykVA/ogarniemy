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
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
FACEBOOK_PAGE_ACCESS_TOKEN = os.environ.get("FACEBOOK_PAGE_ACCESS_TOKEN", "")
FACEBOOK_VERIFY_TOKEN = os.environ.get("FACEBOOK_VERIFY_TOKEN", "ogarniemy-verify")
PRESENTATION_URL = os.environ.get("PRESENTATION_URL", "https://ogarniemy.pro")
KEYWORD_REPLY_COOLDOWN = int(os.environ.get("KEYWORD_REPLY_COOLDOWN", "21600"))


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

            create table if not exists facebook_subscribers (
              psid text primary key,
              role text default 'unknown',
              city text default '',
              created_at integer not null,
              stopped_at integer
            );
            """
        )


def http_json(url: str, payload: dict[str, Any] | None = None) -> Any:
    data = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
      data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as res:
        body = res.read().decode("utf-8")
        return json.loads(body) if body else {}


def telegram_api(method: str, payload: dict[str, Any] | None = None) -> Any:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN first.")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    return http_json(url, payload)


def send_telegram(chat_id: int, text: str) -> bool:
    try:
        telegram_api(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": False,
            },
        )
        return True
    except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError) as exc:
        print(f"telegram send failed for {chat_id}: {exc}")
        return False


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


def save_group(message: dict[str, Any], city: str = "", keywords: str = "") -> None:
    chat = message.get("chat", {})
    with db() as conn:
        conn.execute(
            """
            insert into telegram_groups(chat_id, title, city, keywords, created_at)
            values(?, ?, ?, ?, ?)
            on conflict(chat_id) do update set
              title = excluded.title,
              city = coalesce(nullif(excluded.city, ''), telegram_groups.city),
              keywords = coalesce(nullif(excluded.keywords, ''), telegram_groups.keywords),
              enabled = 1
            """,
            (
                int(chat["id"]),
                chat.get("title", ""),
                city.strip(),
                keywords.strip(),
                int(time.time()),
            ),
        )


def group_keywords(chat_id: int) -> list[str]:
    with db() as conn:
        row = conn.execute(
            "select keywords from telegram_groups where chat_id = ? and enabled = 1",
            (chat_id,),
        ).fetchone()
    if not row or not row["keywords"]:
        return []
    return [item.strip().lower() for item in row["keywords"].split(",") if item.strip()]


def can_reply_to_keyword(chat_id: int, keyword: str) -> bool:
    now = int(time.time())
    with db() as conn:
        row = conn.execute(
            "select last_sent_at from keyword_reply_locks where group_chat_id = ? and keyword = ?",
            (chat_id, keyword),
        ).fetchone()
        if row and now - int(row["last_sent_at"]) < KEYWORD_REPLY_COOLDOWN:
            return False
        conn.execute(
            """
            insert into keyword_reply_locks(group_chat_id, keyword, last_sent_at)
            values(?, ?, ?)
            on conflict(group_chat_id, keyword) do update set last_sent_at = excluded.last_sent_at
            """,
            (chat_id, keyword, now),
        )
    return True


def record_keyword_hit(message: dict[str, Any], keyword: str) -> None:
    chat = message.get("chat", {})
    user = message.get("from", {})
    with db() as conn:
        conn.execute(
            """
            insert into keyword_hits(group_chat_id, keyword, user_id, username, message, created_at)
            values(?, ?, ?, ?, ?, ?)
            """,
            (
                int(chat["id"]),
                keyword,
                user.get("id"),
                user.get("username", ""),
                message.get("text", "")[:1000],
                int(time.time()),
            ),
        )


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


def post_to_groups(text: str) -> tuple[int, int]:
    with db() as conn:
        rows = conn.execute(
            "select chat_id from telegram_groups where enabled = 1"
        ).fetchall()
    sent = sum(1 for row in rows if send_telegram(int(row["chat_id"]), text))
    return sent, len(rows)


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


def handle_private_message(message: dict[str, Any]) -> None:
    chat_id = int(message["chat"]["id"])
    user_id = message.get("from", {}).get("id")
    text = (message.get("text") or "").strip()
    lower = text.lower()

    if lower.startswith("/start"):
        upsert_subscriber(message)
        send_telegram(
            chat_id,
            "Здравствуйте! Выберите, кто вы:\n/client - клиент\n/worker - мастер\n\n"
            "Можно указать город: /city Warszawa",
        )
        return
    if lower.startswith("/client"):
        upsert_subscriber(message, "client")
        send_telegram(chat_id, CLIENT_TEXT)
        return
    if lower.startswith("/worker") or lower.startswith("/master"):
        upsert_subscriber(message, "worker")
        send_telegram(chat_id, WORKER_TEXT)
        return
    if lower.startswith("/city"):
        city = text[5:].strip()
        if not city:
            send_telegram(chat_id, "Напишите так: /city Warszawa")
            return
        upsert_subscriber(message)
        set_subscriber_city(chat_id, city)
        send_telegram(chat_id, f"Город сохранен: {city}")
        return
    if lower.startswith("/stop"):
        stop_subscriber(chat_id)
        send_telegram(chat_id, "Рассылка отключена.")
        return

    if not is_admin(user_id):
        send_telegram(chat_id, "Команды: /client, /worker, /city, /stop")
        return

    if lower.startswith("/stats"):
        send_telegram(chat_id, stats_text())
        return
    if lower.startswith("/broadcast "):
        parts = text.split(" ", 2)
        if len(parts) < 3:
            send_telegram(chat_id, "Формат: /broadcast all|clients|workers текст")
            return
        sent, total = broadcast_subscribers(parts[1].lower(), parts[2])
        send_telegram(chat_id, f"Отправлено подписчикам: {sent}/{total}")
        return
    if lower.startswith("/postgroups "):
        sent, total = post_to_groups(text.split(" ", 1)[1])
        send_telegram(chat_id, f"Опубликовано в группах: {sent}/{total}")
        return
    if lower.startswith("/groups"):
        with db() as conn:
            rows = conn.execute(
                "select chat_id, title, city, keywords from telegram_groups where enabled = 1 order by title"
            ).fetchall()
        if not rows:
            send_telegram(chat_id, "Группы пока не добавлены.")
        else:
            send_telegram(
                chat_id,
                "\n".join(
                    f"{row['title']} ({row['chat_id']}), {row['city']}: {row['keywords']}"
                    for row in rows
                ),
            )
        return

    send_telegram(
        chat_id,
        "Админ-команды:\n"
        "/stats\n"
        "/broadcast all|clients|workers текст\n"
        "/postgroups текст\n"
        "В группе: /watch город | слово1, слово2",
    )


def handle_group_message(message: dict[str, Any]) -> None:
    chat_id = int(message["chat"]["id"])
    user_id = message.get("from", {}).get("id")
    text = (message.get("text") or "").strip()
    lower = text.lower()

    if lower.startswith("/watch") and is_admin(user_id):
        payload = text[6:].strip()
        if "|" in payload:
            city, keywords = [part.strip() for part in payload.split("|", 1)]
        else:
            city, keywords = "", payload
        save_group(message, city=city, keywords=keywords)
        send_telegram(
            chat_id,
            "Группа добавлена. Я буду смотреть ключевые слова и отвечать публично, "
            "с ограничением по частоте.",
        )
        return

    keywords = group_keywords(chat_id)
    if not keywords:
        return
    for keyword in keywords:
        if keyword in lower:
            record_keyword_hit(message, keyword)
            if can_reply_to_keyword(chat_id, keyword):
                send_telegram(chat_id, GROUP_TEXT)
            return


def handle_telegram_update(update: dict[str, Any]) -> None:
    message = update.get("message") or update.get("edited_message")
    if not message or "chat" not in message:
        return
    chat_type = message["chat"].get("type")
    if chat_type == "private":
        handle_private_message(message)
    elif chat_type in {"group", "supergroup"}:
        handle_group_message(message)


def run_telegram_polling() -> None:
    init_db()
    offset = 0
    print("Telegram marketing bot started.")
    while True:
        try:
            data = telegram_api(
                "getUpdates",
                {"timeout": 30, "offset": offset, "allowed_updates": ["message", "edited_message"]},
            )
            for update in data.get("result", []):
                offset = max(offset, int(update["update_id"]) + 1)
                handle_telegram_update(update)
        except Exception as exc:
            print(f"polling error: {exc}")
            time.sleep(5)


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
    parser.add_argument("--telegram", action="store_true", help="run Telegram bot polling")
    parser.add_argument("--facebook-webhook", action="store_true", help="run Facebook webhook server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    args = parser.parse_args()

    if args.facebook_webhook:
        run_facebook_webhook(args.host, args.port)
    else:
        run_telegram_polling()


if __name__ == "__main__":
    main()

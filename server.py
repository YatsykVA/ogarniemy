from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import urlopen
import base64
import hashlib
import json
import mimetypes
import os
import secrets
import sqlite3
import threading
import time
import uuid


ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("TASK_DB_PATH", os.path.join(ROOT, "server.db"))
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", os.environ.get("TASK_SERVER_PORT", "8080")))
ADMIN_PASSWORD = os.environ.get("TASK_ADMIN_PASSWORD", "admin123")
RESET_ADMIN_PASSWORD = "ZarazaZ"
TRANSLATION_CACHE = {}
SUPPORTED_LANGUAGES = {"en", "uk", "ru", "pl"}
RETENTION_DEFAULT_DAYS = 365
RETENTION_MIN_DAYS = 1
RETENTION_MAX_DAYS = 365
CLEANUP_INTERVAL_SECONDS = 60 * 60
MARKETING_BOT_ENABLED = os.environ.get("MARKETING_BOT_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
MARKETING_BOT_STARTED = False


def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma busy_timeout = 30000")
    return conn


def password_hash(password, salt):
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def get_setting(conn, key):
    row = conn.execute("select value from settings where key = ?", (key,)).fetchone()
    return row["value"] if row else ""


def retention_days(conn, key):
    try:
        value = int(float(get_setting(conn, key) or RETENTION_DEFAULT_DAYS))
    except (TypeError, ValueError):
        value = RETENTION_DEFAULT_DAYS
    return max(RETENTION_MIN_DAYS, min(RETENTION_MAX_DAYS, value))


def parse_retention_days(value):
    try:
        days = int(float(value))
    except (TypeError, ValueError):
        return None
    if days < RETENTION_MIN_DAYS or days > RETENTION_MAX_DAYS:
        return None
    return days


def repair_cyrillic_mojibake(text):
    if not any(marker in text for marker in ("Р ", "Р°", "Рµ", "Рё", "Рѕ", "Рґ", "СЃ", "С‚", "СЊ", "Рџ", "Рќ", "РЎ", "Р’", "Р“", "Р", "Ð", "Ñ")):
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


def repair_json_text(value):
    if isinstance(value, str):
        return repair_cyrillic_mojibake(value)
    if isinstance(value, list):
        return [repair_json_text(item) for item in value]
    if isinstance(value, dict):
        return {key: repair_json_text(item) for key, item in value.items()}
    return value


def ensure_admin_password(conn):
    existing = conn.execute("select value from settings where key = 'admin_salt'").fetchone()
    if existing:
        return
    salt = secrets.token_hex(16)
    conn.execute("insert into settings(key, value) values('admin_salt', ?)", (salt,))
    conn.execute(
        "insert into settings(key, value) values('admin_password_hash', ?)",
        (password_hash(ADMIN_PASSWORD, salt),),
    )


def ensure_pln_and_credit_defaults(conn):
    if get_setting(conn, "pln_credit_defaults_applied") == "1":
        return
    conn.execute(
        "insert into settings(key, value) values('currency', 'PLN') "
        "on conflict(key) do update set value = excluded.value"
    )
    conn.execute(
        "insert into settings(key, value) values('reserve_unit', 'credits') "
        "on conflict(key) do update set value = excluded.value"
    )
    conn.execute(
        "insert into settings(key, value) values('pln_credit_defaults_applied', '1') "
        "on conflict(key) do update set value = excluded.value"
    )


def create_user(conn, login, password, display_name):
    salt = secrets.token_hex(16)
    conn.execute(
        "insert or ignore into users(login, password_hash, salt, display_name) values(?, ?, ?, ?)",
        (login, password_hash(password, salt), salt, display_name),
    )


def create_client(conn, login, password, display_name):
    salt = secrets.token_hex(16)
    conn.execute(
        "insert or ignore into clients(login, password_hash, salt, display_name) values(?, ?, ?, ?)",
        (login, password_hash(password, salt), salt, display_name),
    )


def normalize_phone(value):
    text = str(value or "").strip()
    return "".join(ch for ch in text if ch.isdigit())


def compose_address(city="", postal_code="", street="", house="", apartment="", fallback=""):
    city = str(city or "").strip()
    postal_code = str(postal_code or "").strip()
    street = str(street or "").strip()
    house = str(house or "").strip()
    apartment = str(apartment or "").strip()
    parts = [part for part in (postal_code, city, street) if part]
    if house:
        parts.append(f"{house}/{apartment}" if apartment else house)
    elif apartment:
        parts.append(apartment)
    return ", ".join(parts) if parts else str(fallback or "").strip()


def is_placeholder_text(value):
    text = str(value or "").strip()
    return bool(text) and all(ch == "?" or ch.isspace() or ch.isdigit() for ch in text)


def repair_placeholder_names(conn):
    default_users = {
        "worker1": "РЎРѕС‚СЂСѓРґРЅРёРє 1",
        "worker2": "РЎРѕС‚СЂСѓРґРЅРёРє 2",
    }
    default_clients = {
        "client1": "РљР»РёРµРЅС‚ 1",
        "555": "Boss",
    }
    for login, display_name in default_users.items():
        row = conn.execute("select display_name from users where login = ?", (login,)).fetchone()
        if row and is_placeholder_text(row["display_name"]):
            conn.execute("update users set display_name = ? where login = ?", (display_name, login))
    for login, display_name in default_clients.items():
        row = conn.execute("select display_name from clients where login = ?", (login,)).fetchone()
        if row and is_placeholder_text(row["display_name"]):
            conn.execute("update clients set display_name = ? where login = ?", (display_name, login))


def delete_user_data(conn, user_id):
    conn.execute("delete from tokens where user_id = ?", (user_id,))
    conn.execute("delete from reserve_events where user_id = ?", (user_id,))
    conn.execute("delete from task_events where user_id = ?", (user_id,))
    conn.execute("delete from settlements where user_id = ?", (user_id,))
    conn.execute(
        """
        update tasks
        set status = 'new', assigned_to = null, decided_at = null, settlement_id = null
        where assigned_to = ? and status = 'accepted'
        """,
        (user_id,),
    )
    conn.execute("update tasks set assigned_to = null where assigned_to = ?", (user_id,))
    conn.execute("delete from users where id = ?", (user_id,))


def purge_deleted_users(conn):
    rows = conn.execute("select id from users where deleted_at is not null").fetchall()
    for row in rows:
        delete_user_data(conn, row["id"])


def delete_tasks_by_ids(conn, task_ids):
    if not task_ids:
        return
    placeholders = ",".join("?" for _ in task_ids)
    conn.execute(f"delete from task_events where task_id in ({placeholders})", task_ids)
    conn.execute(f"delete from tasks where id in ({placeholders})", task_ids)


def cleanup_expired_data():
    conn = db()
    try:
        now = int(time.time())
        completed_cutoff = now - retention_days(conn, "completed_tasks_retention_days") * 86400
        unaccepted_cutoff = now - retention_days(conn, "unaccepted_tasks_retention_days") * 86400
        employee_settlement_cutoff = now - retention_days(conn, "employee_settlements_retention_days") * 86400
        client_settlement_cutoff = now - retention_days(conn, "client_settlements_retention_days") * 86400

        task_rows = conn.execute(
            """
            select id
            from tasks
            where status = 'completed'
              and coalesce(completed_at, decided_at, created_at) < ?
            """,
            (completed_cutoff,),
        ).fetchall()
        delete_tasks_by_ids(conn, [row["id"] for row in task_rows])
        unaccepted_task_rows = conn.execute(
            """
            select id
            from tasks
            where status = 'new'
              and created_at < ?
            """,
            (unaccepted_cutoff,),
        ).fetchall()
        delete_tasks_by_ids(conn, [row["id"] for row in unaccepted_task_rows])
        conn.execute("delete from settlements where created_at < ?", (employee_settlement_cutoff,))
        conn.execute("delete from client_settlements where created_at < ?", (client_settlement_cutoff,))
        conn.commit()
    finally:
        conn.close()


def start_cleanup_worker():
    def loop():
        while True:
            try:
                cleanup_expired_data()
            except Exception as exc:
                print(f"Cleanup error: {exc}")
            time.sleep(CLEANUP_INTERVAL_SECONDS)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()


def start_marketing_bot_worker():
    global MARKETING_BOT_STARTED
    if not MARKETING_BOT_ENABLED:
        return
    if MARKETING_BOT_STARTED:
        return
    try:
        import marketing_bot

        status = marketing_bot.telegram_login_status()
        if not status["configured"] or not status["sessionExists"]:
            print("Marketing userbot is enabled, but Telegram API config or session is missing.")
            return
    except Exception as exc:
        print(f"Marketing bot cannot start: {exc}")
        return

    def run():
        try:
            marketing_bot.run_telegram_polling()
        except Exception as exc:
            print(f"Marketing bot stopped: {exc}")

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    MARKETING_BOT_STARTED = True


def init_db():
    conn = db()
    conn.executescript(
        """
        create table if not exists users (
            id integer primary key autoincrement,
            login text unique not null,
            password_hash text not null,
            salt text not null,
            display_name text not null,
            deleted_at integer
        );

        create table if not exists tokens (
            token text primary key,
            user_id integer not null,
            created_at integer not null,
            foreign key(user_id) references users(id)
        );

        create table if not exists clients (
            id integer primary key autoincrement,
            login text unique not null,
            password_hash text not null,
            salt text not null,
            display_name text not null,
            deleted_at integer
        );

        create table if not exists client_tokens (
            token text primary key,
            client_id integer not null,
            created_at integer not null,
            foreign key(client_id) references clients(id)
        );

        create table if not exists tasks (
            id integer primary key autoincrement,
            title text not null,
            description text not null,
            phone text not null default '',
            address text not null default '',
            city text not null default '',
            postal_code text not null default '',
            street text not null default '',
            house text not null default '',
            apartment text not null default '',
            price real not null default 0,
            payment_method text not null default 'card',
            status text not null default 'new',
            assigned_to integer,
            client_id integer,
            decided_at integer,
            created_at integer not null,
            foreign key(assigned_to) references users(id)
        );

        create table if not exists task_events (
            id integer primary key autoincrement,
            task_id integer not null,
            user_id integer not null,
            event text not null,
            created_at integer not null,
            foreign key(task_id) references tasks(id),
            foreign key(user_id) references users(id)
        );

        create table if not exists settlements (
            id integer primary key autoincrement,
            user_id integer not null,
            created_at integer not null,
            snapshot_json text not null,
            foreign key(user_id) references users(id)
        );

        create table if not exists reserve_events (
            id integer primary key autoincrement,
            user_id integer not null,
            kind text not null,
            amount real not null,
            created_at integer not null,
            settlement_id integer,
            foreign key(user_id) references users(id)
        );

        create table if not exists client_settlements (
            id integer primary key autoincrement,
            client_id integer not null,
            created_at integer not null,
            snapshot_json text not null,
            foreign key(client_id) references clients(id)
        );

        create table if not exists client_reserve_events (
            id integer primary key autoincrement,
            client_id integer not null,
            kind text not null,
            amount real not null,
            created_at integer not null,
            settlement_id integer,
            foreign key(client_id) references clients(id)
        );

        create table if not exists settings (
            key text primary key,
            value text not null
        );
        """
    )
    ensure_task_columns(conn)
    ensure_account_columns(conn)
    conn.execute("update tasks set settlement_id = null where status != 'completed' and settlement_id is not null")
    conn.execute("update tasks set client_settlement_id = null where status != 'completed' and client_settlement_id is not null")
    restore_client_settlement_links(conn)
    purge_deleted_users(conn)
    ensure_admin_password(conn)
    ensure_pln_and_credit_defaults(conn)
    create_user(conn, "worker1", "123456", "РЎРѕС‚СЂСѓРґРЅРёРє 1")
    create_user(conn, "worker2", "123456", "РЎРѕС‚СЂСѓРґРЅРёРє 2")
    create_client(conn, "client1", "123456", "РљР»РёРµРЅС‚ 1")
    repair_placeholder_names(conn)

    count = conn.execute("select count(*) from tasks").fetchone()[0]
    if count == 0:
        now = int(time.time())
        conn.executemany(
            "insert into tasks(title, description, created_at) values(?, ?, ?)",
            [
                ("РџСЂРѕРІРµСЂРёС‚СЊ СЃРєР»Р°Рґ", "РџРѕСЃС‡РёС‚Р°С‚СЊ РєРѕСЂРѕР±РєРё РІ Р·РѕРЅРµ A Рё РѕС‚РјРµС‚РёС‚СЊ СЂР°СЃС…РѕР¶РґРµРЅРёСЏ.", now),
                ("Р”РѕСЃС‚Р°РІРєР° РґРѕРєСѓРјРµРЅС‚РѕРІ", "Р—Р°Р±СЂР°С‚СЊ РїР°РєРµС‚ Сѓ Р°РґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂР° Рё РѕС‚РІРµР·С‚Рё РєР»РёРµРЅС‚Сѓ.", now),
                ("Р¤РѕС‚РѕРѕС‚С‡РµС‚", "РЎРґРµР»Р°С‚СЊ С„РѕС‚РѕРіСЂР°С„РёРё РѕР±РѕСЂСѓРґРѕРІР°РЅРёСЏ РїРѕСЃР»Рµ СѓСЃС‚Р°РЅРѕРІРєРё.", now),
            ],
        )
    conn.commit()
    conn.close()


def ensure_task_columns(conn):
    user_columns = {row["name"] for row in conn.execute("pragma table_info(users)").fetchall()}
    if "deleted_at" not in user_columns:
        conn.execute("alter table users add column deleted_at integer")

    columns = {row["name"] for row in conn.execute("pragma table_info(tasks)").fetchall()}
    if "address" not in columns:
        conn.execute("alter table tasks add column address text not null default ''")
    if "phone" not in columns:
        conn.execute("alter table tasks add column phone text not null default ''")
    if "city" not in columns:
        conn.execute("alter table tasks add column city text not null default ''")
    if "postal_code" not in columns:
        conn.execute("alter table tasks add column postal_code text not null default ''")
    if "street" not in columns:
        conn.execute("alter table tasks add column street text not null default ''")
    if "house" not in columns:
        conn.execute("alter table tasks add column house text not null default ''")
    if "apartment" not in columns:
        conn.execute("alter table tasks add column apartment text not null default ''")
    if "price" not in columns:
        conn.execute("alter table tasks add column price real not null default 0")
    if "payment_method" not in columns:
        conn.execute("alter table tasks add column payment_method text not null default 'card'")
    if "settlement_id" not in columns:
        conn.execute("alter table tasks add column settlement_id integer")
    if "hidden_from_completed" not in columns:
        conn.execute("alter table tasks add column hidden_from_completed integer not null default 0")
    if "client_id" not in columns:
        conn.execute("alter table tasks add column client_id integer")
    if "client_settlement_id" not in columns:
        conn.execute("alter table tasks add column client_settlement_id integer")
    if "accepted_at" not in columns:
        conn.execute("alter table tasks add column accepted_at integer")
    if "completed_at" not in columns:
        conn.execute("alter table tasks add column completed_at integer")
    conn.execute("update tasks set accepted_at = decided_at where status = 'accepted' and accepted_at is null and decided_at is not null")
    conn.execute("update tasks set completed_at = decided_at where status = 'completed' and completed_at is null and decided_at is not null")

    event_columns = {row["name"] for row in conn.execute("pragma table_info(task_events)").fetchall()}
    if "settlement_id" not in event_columns:
        conn.execute("alter table task_events add column settlement_id integer")


def ensure_account_columns(conn):
    user_columns = {row["name"] for row in conn.execute("pragma table_info(users)").fetchall()}
    if "phone" not in user_columns:
        conn.execute("alter table users add column phone text not null default ''")
    client_columns = {row["name"] for row in conn.execute("pragma table_info(clients)").fetchall()}
    if "phone" not in client_columns:
        conn.execute("alter table clients add column phone text not null default ''")
    for table in ("users", "clients"):
        rows = conn.execute(f"select id, login, phone from {table}").fetchall()
        for row in rows:
            if not row["phone"] and not str(row["login"] or "").startswith("+"):
                continue
            phone = normalize_phone(row["phone"] or row["login"])
            if phone:
                conn.execute(f"update {table} set login = ?, phone = ? where id = ?", (phone, phone, row["id"]))


def restore_client_settlement_links(conn):
    rows = conn.execute(
        "select id, snapshot_json from client_settlements order by id"
    ).fetchall()
    for row in rows:
        try:
            snapshot = json.loads(row["snapshot_json"])
        except Exception:
            continue
        task_ids = []
        for key in ("completed",):
            for task in snapshot.get(key, []) or []:
                task_id = task.get("id") if isinstance(task, dict) else None
                if task_id:
                    task_ids.append(task_id)
        if not task_ids:
            continue
        placeholders = ",".join("?" for _ in task_ids)
        conn.execute(
            f"""
            update tasks
            set client_settlement_id = ?
            where client_settlement_id is null
              and id in ({placeholders})
            """,
            (row["id"], *task_ids),
        )


class App(BaseHTTPRequestHandler):
    server_version = "TaskServer/1.0"

    def redirect_to_canonical_host(self):
        host = self.headers.get("Host", "").split(":", 1)[0].lower()
        if host != "ogarniemy.pro":
            return False
        self.send_response(308)
        self.send_header("Location", f"https://www.ogarniemy.pro{self.path}")
        self.end_headers()
        return True

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw)

    def send_json(self, data, status=200):
        data = repair_json_text(data)
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Admin-Password, X-Language")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html):
        html = inject_server_language_tools(html)
        html = repair_cyrillic_mojibake(html)
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_redirect(self, location, status=308):
        self.send_response(status)
        self.send_header("Location", location)
        self.end_headers()

    def send_static_file(self, relative_path, cache_seconds=0):
        file_path = os.path.abspath(os.path.join(ROOT, relative_path))
        if os.path.commonpath([ROOT, file_path]) != ROOT or not os.path.isfile(file_path):
            self.send_json({"error": "not_found"}, 404)
            return
        with open(file_path, "rb") as file:
            body = file.read()
        content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", f"public, max-age={cache_seconds}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def user_from_token(self):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return None
        token = header.replace("Bearer ", "", 1).strip()
        conn = db()
        row = conn.execute(
            """
            select users.id, users.login, users.display_name
            from tokens join users on users.id = tokens.user_id
            where tokens.token = ?
            """,
            (token,),
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def client_from_token(self):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return None
        token = header.replace("Bearer ", "", 1).strip()
        conn = db()
        row = conn.execute(
            """
            select clients.id, clients.login, clients.display_name
            from client_tokens join clients on clients.id = client_tokens.client_id
            where client_tokens.token = ? and clients.deleted_at is null
            """,
            (token,),
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def request_language(self):
        lang = self.headers.get("X-Language", "ru").strip().lower()
        return lang if lang in SUPPORTED_LANGUAGES else "ru"

    def is_admin(self):
        return self.check_admin_password(self.headers.get("X-Admin-Password", ""))

    def check_admin_password(self, password):
        conn = db()
        salt = get_setting(conn, "admin_salt")
        stored_hash = get_setting(conn, "admin_password_hash")
        conn.close()
        return bool(password) and bool(salt) and password_hash(password, salt) == stored_hash

    def has_confirmation_password(self, data):
        return self.check_admin_password(str(data.get("password", "")))

    def do_OPTIONS(self):
        self.send_json({})

    def do_HEAD(self):
        if self.redirect_to_canonical_host():
            return
        path = urlparse(self.path).path
        if path == "/":
            relative_path = "index.html"
        elif path == "/client":
            self.send_redirect("/#client-signup")
            return
        elif path == "/employee":
            self.send_redirect("/#employee-signup")
            return
        elif path in {"/styles.css", "/script.js", "/language.js", "/signup.css", "/signup.js"} or path.startswith("/assets/") or path.startswith("/downloads/"):
            relative_path = path.lstrip("/")
        elif path == "/admin":
            self.send_redirect("/server")
            return
        else:
            self.send_response(404)
            self.end_headers()
            return
        file_path = os.path.abspath(os.path.join(ROOT, relative_path))
        if os.path.commonpath([ROOT, file_path]) != ROOT or not os.path.isfile(file_path):
            self.send_response(404)
            self.end_headers()
            return
        content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        cache_seconds = 7 * 24 * 60 * 60 if path.startswith("/assets/") else 60 * 60 if path != "/" else 0
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", f"public, max-age={cache_seconds}")
        self.send_header("Content-Length", str(os.path.getsize(file_path)))
        self.end_headers()

    def do_GET(self):
        if self.redirect_to_canonical_host():
            return
        path = urlparse(self.path).path
        if path == "/facebook/webhook":
            self.handle_facebook_webhook_verify()
            return
        if path == "/":
            self.send_static_file("index.html")
            return
        if path in {"/styles.css", "/script.js", "/language.js", "/signup.css", "/signup.js"}:
            self.send_static_file(path.lstrip("/"), 60 * 60)
            return
        if path.startswith("/assets/"):
            self.send_static_file(path.lstrip("/"), 7 * 24 * 60 * 60)
            return
        if path.startswith("/downloads/"):
            self.send_static_file(path.lstrip("/"))
            return
        if path == "/client":
            self.send_redirect("/#client-signup")
            return
        if path == "/employee":
            self.send_redirect("/#employee-signup")
            return
        if path == "/admin":
            self.send_redirect("/server")
            return
        if path == "/server":
            self.send_html(INDEX_HTML)
            return
        if path == "/users":
            self.send_html(USERS_HTML)
            return
        if path == "/clients":
            self.send_html(CLIENTS_HTML)
            return
        if path == "/completed":
            self.send_html(COMPLETED_HTML)
            return
        if path == "/calculations":
            self.send_html(CALCULATIONS_HTML)
            return
        if path == "/client-calculations":
            self.send_html(CLIENT_CALCULATIONS_HTML)
            return
        if path == "/settings":
            self.send_html(SETTINGS_HTML)
            return
        if path == "/telegram-ads":
            self.send_html(TELEGRAM_ADS_HTML)
            return
        if path == "/telegram-login":
            self.send_html(TELEGRAM_LOGIN_HTML)
            return
        if path == "/facebook-ads":
            self.send_html(FACEBOOK_ADS_HTML)
            return
        if path == "/api/tasks":
            self.handle_tasks()
            return
        if path == "/api/me/report":
            self.handle_my_report()
            return
        if path == "/api/settings":
            self.handle_public_settings()
            return
        if path == "/api/client/tasks":
            self.handle_client_tasks()
            return
        if path == "/api/client/report":
            self.handle_client_self_report()
            return
        if path == "/api/client/calculations":
            self.handle_client_self_calculations()
            return
        if path == "/api/admin/tasks":
            self.handle_admin_tasks()
            return
        if path == "/api/admin/completed-tasks":
            self.handle_admin_completed_tasks()
            return
        if path == "/api/admin/settlements":
            self.handle_admin_settlements()
            return
        if path == "/api/admin/users":
            self.handle_admin_users()
            return
        if path == "/api/admin/clients":
            self.handle_admin_clients()
            return
        if path == "/api/admin/client-calculations":
            self.handle_admin_client_calculations()
            return
        if path == "/api/admin/check-password":
            self.handle_check_admin_password()
            return
        if path == "/api/admin/settings":
            self.handle_admin_settings()
            return
        if path == "/api/admin/marketing/telegram":
            self.handle_marketing_state("telegram")
            return
        if path == "/api/admin/telegram-login/status":
            self.handle_telegram_login_status()
            return
        if path == "/api/admin/marketing/facebook":
            self.handle_marketing_state("facebook")
            return
        if path.startswith("/api/admin/users/") and path.endswith("/report"):
            self.handle_user_report(path)
            return
        if path.startswith("/api/admin/clients/") and path.endswith("/report"):
            self.handle_client_report(path)
            return
        self.send_json({"error": "not_found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/facebook/webhook":
            self.handle_facebook_webhook_event()
            return
        if path == "/api/login":
            self.handle_login()
            return
        if path == "/api/client-login":
            self.handle_client_login()
            return
        if path == "/api/register/client":
            self.handle_public_client_registration()
            return
        if path == "/api/register/employee":
            self.handle_public_employee_registration()
            return
        if path == "/api/translate":
            self.handle_translate()
            return
        if path == "/api/admin/change-password":
            self.handle_change_admin_password()
            return
        if path == "/api/admin/reset-password":
            self.handle_reset_admin_password()
            return
        if path == "/api/admin/settings":
            self.handle_save_admin_settings()
            return
        if path == "/api/admin/telegram-login/start":
            self.handle_telegram_login_start()
            return
        if path == "/api/admin/telegram-login/complete":
            self.handle_telegram_login_complete()
            return
        if path.startswith("/api/admin/marketing/"):
            self.handle_marketing_post(path)
            return
        if path == "/api/me/reserve":
            self.handle_my_reserve()
            return
        if path == "/api/admin/tasks":
            self.handle_create_task()
            return
        if path.startswith("/api/admin/tasks/") and path.endswith("/reset"):
            self.handle_reset_task(path)
            return
        if path.startswith("/api/admin/tasks/") and path.endswith("/delete"):
            self.handle_delete_task(path)
            return
        if path.startswith("/api/admin/tasks/") and path.endswith("/complete"):
            self.handle_admin_complete_task(path)
            return
        if path.startswith("/api/admin/tasks/") and path.endswith("/edit"):
            self.handle_edit_task(path)
            return
        if path.startswith("/api/admin/completed-tasks/") and path.endswith("/hide"):
            self.handle_hide_completed_task(path)
            return
        if path.startswith("/api/admin/settlements/") and path.endswith("/delete"):
            self.handle_delete_settlement(path)
            return
        if path.startswith("/api/admin/settlements/") and path.endswith("/calculate"):
            self.handle_calculate_settlement(path)
            return
        if path.startswith("/api/admin/client-settlements/") and path.endswith("/calculate"):
            self.handle_calculate_client_settlement(path)
            return
        if path.startswith("/api/admin/client-settlements/") and path.endswith("/delete"):
            self.handle_delete_client_settlement(path)
            return
        if path == "/api/admin/users":
            self.handle_create_user()
            return
        if path == "/api/admin/clients":
            self.handle_create_client()
            return
        if path == "/api/client/tasks":
            self.handle_create_client_task()
            return
        if path.startswith("/api/client/tasks/") and path.endswith("/price"):
            self.handle_update_client_task_price(path)
            return
        if path.startswith("/api/client/tasks/") and path.endswith("/delete"):
            self.handle_delete_client_task(path)
            return
        if path == "/api/client/reserve":
            self.handle_client_self_reserve()
            return
        if path.startswith("/api/admin/users/"):
            if path.endswith("/settle"):
                self.handle_settle_user(path)
                return
            if path.endswith("/reserve"):
                self.handle_user_reserve(path)
                return
            if path.endswith("/delete"):
                self.handle_delete_user(path)
                return
            self.handle_update_user(path)
            return
        if path == "/api/admin/users-settle-all":
            self.handle_settle_all_users()
            return
        if path.startswith("/api/admin/clients/"):
            if path.endswith("/settle"):
                self.handle_settle_client(path)
                return
            if path.endswith("/reserve"):
                self.handle_client_reserve(path)
                return
            if path.endswith("/delete"):
                self.handle_delete_client(path)
                return
            self.handle_update_client(path)
            return
        if path == "/api/admin/clients-settle-all":
            self.handle_settle_all_clients()
            return
        if path.startswith("/api/tasks/") and path.endswith("/decision"):
            self.handle_decision(path)
            return
        self.send_json({"error": "not_found"}, 404)

    def handle_login(self):
        data = self.read_json()
        login = str(data.get("login", "")).strip()
        phone = normalize_phone(login)
        password = str(data.get("password", ""))
        conn = db()
        row = conn.execute(
            "select * from users where (login = ? or phone = ?) and deleted_at is null",
            (login, phone),
        ).fetchone()
        if not row or row["password_hash"] != password_hash(password, row["salt"]):
            conn.close()
            self.send_json({"error": "bad_credentials"}, 401)
            return

        token = secrets.token_urlsafe(32)
        conn.execute(
            "insert into tokens(token, user_id, created_at) values(?, ?, ?)",
            (token, row["id"], int(time.time())),
        )
        conn.commit()
        conn.close()
        self.send_json(
            {
                "token": token,
                "user": {
                    "id": row["id"],
                    "login": row["login"],
                    "displayName": row["display_name"],
                },
            }
        )

    def handle_client_login(self):
        data = self.read_json()
        login = str(data.get("login", "")).strip()
        phone = normalize_phone(login)
        password = str(data.get("password", ""))
        conn = db()
        row = conn.execute(
            "select * from clients where (login = ? or phone = ?) and deleted_at is null",
            (login, phone),
        ).fetchone()
        if not row or row["password_hash"] != password_hash(password, row["salt"]):
            conn.close()
            self.send_json({"error": "bad_credentials"}, 401)
            return

        token = secrets.token_urlsafe(32)
        conn.execute(
            "insert into client_tokens(token, client_id, created_at) values(?, ?, ?)",
            (token, row["id"], int(time.time())),
        )
        conn.commit()
        conn.close()
        self.send_json(
            {
                "token": token,
                "client": {
                    "id": row["id"],
                    "login": row["login"],
                    "displayName": row["display_name"],
                },
            }
        )

    def handle_public_client_registration(self):
        data = self.read_json()
        display_name = str(data.get("displayName", "")).strip()
        phone = normalize_phone(data.get("phone", ""))
        password = str(data.get("password", ""))
        if not display_name or len(phone) < 8 or len(password) < 4:
            self.send_json({"error": "name_phone_password_required"}, 400)
            return
        conn = db()
        exists = conn.execute("select id from clients where login = ?", (phone,)).fetchone()
        if exists:
            conn.close()
            self.send_json({"error": "phone_already_registered"}, 409)
            return
        create_client(conn, phone, password, display_name)
        conn.execute("update clients set phone = ? where login = ?", (phone, phone))
        conn.commit()
        conn.close()
        self.send_json({"ok": True, "login": phone}, 201)

    def handle_public_employee_registration(self):
        data = self.read_json()
        display_name = str(data.get("displayName", "")).strip()
        phone = normalize_phone(data.get("phone", ""))
        password = str(data.get("password", ""))
        if not display_name or len(phone) < 8 or len(password) < 4:
            self.send_json({"error": "name_phone_password_required"}, 400)
            return
        conn = db()
        exists = conn.execute(
            "select id from users where login = ?",
            (phone,),
        ).fetchone()
        if exists:
            conn.close()
            self.send_json({"error": "phone_already_registered"}, 409)
            return
        create_user(conn, phone, password, display_name)
        conn.execute("update users set phone = ? where login = ?", (phone, phone))
        conn.commit()
        conn.close()
        self.send_json({"ok": True, "login": phone}, 201)

    def handle_translate(self):
        data = self.read_json()
        lang = str(data.get("language", "ru")).strip().lower()
        if lang not in SUPPORTED_LANGUAGES:
            lang = "ru"
        texts = data.get("texts", [])
        if not isinstance(texts, list):
            self.send_json({"error": "texts_required"}, 400)
            return
        self.send_json(
            {
                "texts": [
                    translate_text(str(text), lang)
                    for text in texts
                ]
            }
        )

    def handle_check_admin_password(self):
        if not self.is_admin():
            self.send_json({"error": "admin_unauthorized"}, 401)
            return
        self.send_json({"ok": True})

    def handle_change_admin_password(self):
        data = self.read_json()
        old_password = str(data.get("oldPassword", ""))
        old_password_repeat = str(data.get("oldPasswordRepeat", ""))
        new_password = str(data.get("newPassword", ""))
        if old_password != old_password_repeat:
            self.send_json({"error": "old_passwords_do_not_match"}, 400)
            return
        if len(new_password) < 4:
            self.send_json({"error": "password_too_short"}, 400)
            return
        if not self.check_admin_password(old_password):
            self.send_json({"error": "bad_old_password"}, 401)
            return
        salt = secrets.token_hex(16)
        conn = db()
        conn.execute("update settings set value = ? where key = 'admin_salt'", (salt,))
        conn.execute(
            "update settings set value = ? where key = 'admin_password_hash'",
            (password_hash(new_password, salt),),
        )
        conn.commit()
        conn.close()
        self.send_json({"ok": True})

    def handle_reset_admin_password(self):
        if not self.is_admin():
            self.send_json({"error": "admin_unauthorized"}, 401)
            return
        salt = secrets.token_hex(16)
        conn = db()
        conn.execute("update settings set value = ? where key = 'admin_salt'", (salt,))
        conn.execute(
            "update settings set value = ? where key = 'admin_password_hash'",
            (password_hash(RESET_ADMIN_PASSWORD, salt),),
        )
        conn.commit()
        conn.close()
        self.send_json({"ok": True})

    def admin_settings(self):
        conn = db()
        reserve_unit = get_setting(conn, "reserve_unit") or "credits"
        if reserve_unit not in ("credits", "tokens", "coins", "points"):
            reserve_unit = "credits"
        settings = {
            "currency": get_setting(conn, "currency") or "PLN",
            "reserveUnit": reserve_unit,
            "showPrices": True,
            "completedFeePercent": float(get_setting(conn, "completed_fee_percent") or "1"),
            "refusedFeePercent": float(get_setting(conn, "refused_fee_percent") or "1"),
            "completedTasksRetentionDays": retention_days(conn, "completed_tasks_retention_days"),
            "unacceptedTasksRetentionDays": retention_days(conn, "unaccepted_tasks_retention_days"),
            "employeeSettlementsRetentionDays": retention_days(conn, "employee_settlements_retention_days"),
            "clientSettlementsRetentionDays": retention_days(conn, "client_settlements_retention_days"),
            "feedbackPhone": get_setting(conn, "feedback_phone"),
            "feedbackEmail": get_setting(conn, "feedback_email"),
            "feedbackAddress": get_setting(conn, "feedback_address"),
            "feedbackTelegram": get_setting(conn, "feedback_telegram"),
            "feedbackWhatsApp": get_setting(conn, "feedback_whatsapp"),
        }
        conn.close()
        return settings

    def handle_admin_settings(self):
        if not self.is_admin():
            self.send_json({"error": "admin_unauthorized"}, 401)
            return
        self.send_json({"settings": self.admin_settings()})

    def handle_public_settings(self):
        self.send_json({"settings": self.admin_settings()})

    def handle_save_admin_settings(self):
        if not self.is_admin():
            self.send_json({"error": "admin_unauthorized"}, 401)
            return
        data = self.read_json()
        currency = str(data.get("currency", "PLN")).strip().upper()
        if currency not in ("RUB", "USD", "EUR", "PLN", "UAH"):
            self.send_json({"error": "bad_currency"}, 400)
            return
        reserve_unit = str(data.get("reserveUnit", "credits")).strip().lower()
        if reserve_unit not in ("credits", "tokens", "coins", "points"):
            self.send_json({"error": "bad_reserve_unit"}, 400)
            return
        completed_fee_percent = parse_price(data.get("completedFeePercent", 1))
        refused_fee_percent = parse_price(data.get("refusedFeePercent", 1))
        completed_tasks_retention_days = parse_retention_days(data.get("completedTasksRetentionDays", RETENTION_DEFAULT_DAYS))
        unaccepted_tasks_retention_days = parse_retention_days(data.get("unacceptedTasksRetentionDays", RETENTION_DEFAULT_DAYS))
        employee_settlements_retention_days = parse_retention_days(data.get("employeeSettlementsRetentionDays", RETENTION_DEFAULT_DAYS))
        client_settlements_retention_days = parse_retention_days(data.get("clientSettlementsRetentionDays", RETENTION_DEFAULT_DAYS))
        if completed_fee_percent is None or completed_fee_percent < 0 or completed_fee_percent > 100:
            self.send_json({"error": "bad_completed_fee_percent"}, 400)
            return
        if refused_fee_percent is None or refused_fee_percent < 0 or refused_fee_percent > 100:
            self.send_json({"error": "bad_refused_fee_percent"}, 400)
            return
        if refused_fee_percent > completed_fee_percent:
            self.send_json({"error": "refused_fee_percent_exceeds_completed_fee_percent"}, 400)
            return
        if completed_tasks_retention_days is None:
            self.send_json({"error": "bad_completed_tasks_retention_days"}, 400)
            return
        if unaccepted_tasks_retention_days is None:
            self.send_json({"error": "bad_unaccepted_tasks_retention_days"}, 400)
            return
        if employee_settlements_retention_days is None:
            self.send_json({"error": "bad_employee_settlements_retention_days"}, 400)
            return
        if client_settlements_retention_days is None:
            self.send_json({"error": "bad_client_settlements_retention_days"}, 400)
            return
        conn = db()
        conn.execute(
            "insert into settings(key, value) values('currency', ?) on conflict(key) do update set value = excluded.value",
            (currency,),
        )
        conn.execute(
            "insert into settings(key, value) values('reserve_unit', ?) on conflict(key) do update set value = excluded.value",
            (reserve_unit,),
        )
        conn.execute(
            "insert into settings(key, value) values('completed_fee_percent', ?) on conflict(key) do update set value = excluded.value",
            (str(completed_fee_percent),),
        )
        conn.execute(
            "insert into settings(key, value) values('refused_fee_percent', ?) on conflict(key) do update set value = excluded.value",
            (str(refused_fee_percent),),
        )
        conn.execute(
            "insert into settings(key, value) values('completed_tasks_retention_days', ?) on conflict(key) do update set value = excluded.value",
            (str(completed_tasks_retention_days),),
        )
        conn.execute(
            "insert into settings(key, value) values('unaccepted_tasks_retention_days', ?) on conflict(key) do update set value = excluded.value",
            (str(unaccepted_tasks_retention_days),),
        )
        conn.execute(
            "insert into settings(key, value) values('employee_settlements_retention_days', ?) on conflict(key) do update set value = excluded.value",
            (str(employee_settlements_retention_days),),
        )
        conn.execute(
            "insert into settings(key, value) values('client_settlements_retention_days', ?) on conflict(key) do update set value = excluded.value",
            (str(client_settlements_retention_days),),
        )
        for key, field in (
            ("feedback_phone", "feedbackPhone"),
            ("feedback_email", "feedbackEmail"),
            ("feedback_address", "feedbackAddress"),
            ("feedback_telegram", "feedbackTelegram"),
            ("feedback_whatsapp", "feedbackWhatsApp"),
        ):
            conn.execute(
                "insert into settings(key, value) values(?, ?) on conflict(key) do update set value = excluded.value",
                (key, str(data.get(field, "")).strip()),
            )
        conn.commit()
        conn.close()
        cleanup_expired_data()
        self.send_json({"ok": True, "settings": self.admin_settings()})

    def handle_tasks(self):
        user = self.user_from_token()
        if not user:
            self.send_json({"error": "unauthorized"}, 401)
            return
        conn = db()
        rows = conn.execute(
            """
            select tasks.id, tasks.title, tasks.description, tasks.phone, tasks.address, tasks.city, tasks.postal_code, tasks.street, tasks.house, tasks.apartment, tasks.price, tasks.payment_method,
                   tasks.status, tasks.created_at, tasks.accepted_at, tasks.completed_at, tasks.client_id, tasks.settlement_id,
                   tasks.client_settlement_id,
                   users.display_name as assigned_to_name,
                   users.login as assigned_to_login,
                   clients.login as client_login,
                   clients.display_name as client_name
            from tasks
            left join users on users.id = tasks.assigned_to
            left join clients on clients.id = tasks.client_id
            where tasks.status = 'new'
               or (tasks.assigned_to = ? and tasks.status = 'accepted')
            order by
                case when tasks.status = 'accepted' and tasks.assigned_to = ? then 0 else 1 end,
                coalesce(tasks.decided_at, tasks.created_at) desc,
                tasks.id desc
            """,
            (user["id"], user["id"]),
        ).fetchall()
        conn.close()
        lang = self.request_language()
        self.send_json({"tasks": [task_json(row, lang, hide_private=row["status"] != "accepted") for row in rows]})

    def handle_decision(self, path):
        user = self.user_from_token()
        if not user:
            self.send_json({"error": "unauthorized"}, 401)
            return
        parts = path.strip("/").split("/")
        task_id = int(parts[2])
        decision = str(self.read_json().get("decision", "")).strip().lower()
        if decision not in ("accept", "decline", "complete", "refuse"):
            self.send_json({"error": "bad_decision"}, 400)
            return

        conn = db()
        row = conn.execute("select * from tasks where id = ?", (task_id,)).fetchone()
        if not row:
            conn.close()
            self.send_json({"error": "task_not_found"}, 404)
            return

        if decision in ("accept", "decline"):
            if row["status"] != "new":
                conn.close()
                self.send_json({"error": "task_already_taken"}, 409)
                return
            if decision == "accept":
                capacity = self.acceptance_capacity(user["id"], row["price"])
                if not capacity["allowed"]:
                    conn.close()
                    self.send_json({"error": "insufficient_reserve", "capacity": capacity}, 409)
                    return
            new_status = "accepted" if decision == "accept" else "declined"
        else:
            if row["status"] != "accepted" or row["assigned_to"] != user["id"]:
                conn.close()
                self.send_json({"error": "task_not_accepted_by_you"}, 409)
                return
            if decision == "complete" and not self.client_has_enough_reserve_for_card_task(row):
                conn.close()
                self.send_json({"error": "client_reserve_too_low"}, 409)
                return
            new_status = "completed" if decision == "complete" else "new"

        now = int(time.time())
        if decision in ("refuse", "decline"):
            conn.execute(
                "insert into task_events(task_id, user_id, event, created_at) values(?, ?, ?, ?)",
                (task_id, user["id"], "refused", now),
            )
        if decision == "refuse":
            reserve_deduction = self.refused_fee_reserve_deduction(user["id"], row["price"], conn)
            if reserve_deduction > 0:
                conn.execute(
                    """
                    insert into reserve_events(user_id, kind, amount, created_at)
                    values(?, ?, ?, ?)
                    """,
                    (user["id"], "refused_fee_from_reserve", -reserve_deduction, now),
                )
        if decision == "complete" and row["payment_method"] == "cash":
            reserve_deduction = self.cash_completed_fee_reserve_deduction(user["id"], row["price"], conn)
            if reserve_deduction > 0:
                conn.execute(
                    """
                    insert into reserve_events(user_id, kind, amount, created_at)
                    values(?, ?, ?, ?)
                    """,
                    (user["id"], "cash_completed_fee_from_reserve", -reserve_deduction, now),
                )

        if decision == "refuse":
            conn.execute(
                "update tasks set status = 'new', assigned_to = null, decided_at = null, accepted_at = null, completed_at = null where id = ?",
                (task_id,),
            )
        elif decision == "accept":
            conn.execute(
                "update tasks set status = ?, assigned_to = ?, decided_at = ?, accepted_at = ?, completed_at = null where id = ?",
                (new_status, user["id"], now, now, task_id),
            )
        elif decision == "complete":
            accepted_at = row["accepted_at"] if "accepted_at" in row.keys() and row["accepted_at"] else row["decided_at"]
            if not accepted_at:
                accepted_at = now
            conn.execute(
                "update tasks set status = ?, assigned_to = ?, decided_at = ?, accepted_at = ?, completed_at = ? where id = ?",
                (new_status, user["id"], now, accepted_at, now, task_id),
            )
        else:
            conn.execute(
                "update tasks set status = ?, assigned_to = ?, decided_at = ? where id = ?",
                (new_status, user["id"], now, task_id),
            )
        conn.commit()
        conn.close()
        self.send_json({"ok": True, "status": new_status})

    def refused_fee_reserve_deduction(self, user_id, task_price, conn):
        refused_fee_percent = float(get_setting(conn, "refused_fee_percent") or "0")
        refused_fee = round(float(task_price or 0) * refused_fee_percent / 100, 2)
        if refused_fee <= 0:
            return 0.0

        report = self.build_user_report(user_id)
        if report is None:
            return 0.0
        totals = report["totals"]
        completed_available = float(totals.get("payoutPrice", 0) or 0)
        reserve_available = float(totals.get("reservePrice", 0) or 0)
        reserve_part = max(0.0, refused_fee - completed_available)
        return round(min(reserve_available, reserve_part), 2)

    def cash_completed_fee_reserve_deduction(self, user_id, task_price, conn):
        completed_fee_percent = float(get_setting(conn, "completed_fee_percent") or "0")
        completed_fee = round(float(task_price or 0) * completed_fee_percent / 100, 2)
        if completed_fee <= 0:
            return 0.0

        report = self.build_user_report(user_id)
        if report is None:
            return 0.0
        totals = report["totals"]
        payout_available = float(totals.get("payoutPrice", 0) or 0)
        reserve_available = float(totals.get("reservePrice", 0) or 0)
        reserve_part = max(0.0, completed_fee - payout_available)
        return round(min(reserve_available, reserve_part), 2)

    def client_has_enough_reserve_for_card_task(self, task_row):
        if normalize_payment_method(task_row["payment_method"]) != "card":
            return True
        client_id = task_row["client_id"] if "client_id" in task_row.keys() else None
        if not client_id:
            return True
        report = self.build_client_report(client_id)
        if report is None:
            return False
        reserve_available = float(report.get("totals", {}).get("reservePrice", 0) or 0)
        return reserve_available >= float(task_row["price"] or 0)

    def client_available_reserve_for_new_card_task(self, client_id, exclude_task_id=None):
        report = self.build_client_report(client_id)
        reserve_available = float((report or {}).get("totals", {}).get("reservePrice", 0) or 0)
        conn = db()
        params = [client_id]
        exclude_sql = ""
        if exclude_task_id is not None:
            exclude_sql = " and id != ?"
            params.append(exclude_task_id)
        row = conn.execute(
            f"""
            select coalesce(sum(price), 0) as reserved_price
            from tasks
            where client_id = ?
              and client_settlement_id is null
              and status in ('new', 'accepted')
              and payment_method != 'cash'
              {exclude_sql}
            """,
            params,
        ).fetchone()
        conn.close()
        reserved_price = float(row["reserved_price"] or 0) if row else 0.0
        return round(max(0.0, reserve_available - reserved_price), 2)

    def handle_admin_tasks(self):
        if not self.is_admin():
            self.send_json({"error": "admin_unauthorized"}, 401)
            return
        conn = db()
        rows = conn.execute(
            """
            select tasks.id, tasks.title, tasks.description, tasks.phone, tasks.address, tasks.city, tasks.postal_code, tasks.street, tasks.house, tasks.apartment, tasks.price, tasks.payment_method,
                   tasks.status, tasks.created_at, tasks.accepted_at, tasks.completed_at,
                   users.display_name as assigned_to_name,
                   users.login as assigned_to_login,
                   clients.login as client_login,
                   clients.display_name as client_name,
                   tasks.client_id,
                   tasks.settlement_id,
                   tasks.client_settlement_id
            from tasks
            left join users on users.id = tasks.assigned_to
            left join clients on clients.id = tasks.client_id
            where tasks.status != 'completed'
            order by tasks.created_at desc, tasks.id desc
            """
        ).fetchall()
        conn.close()
        lang = self.request_language()
        self.send_json({"tasks": [task_json(row, lang) for row in rows]})

    def handle_admin_completed_tasks(self):
        if not self.is_admin():
            self.send_json({"error": "admin_unauthorized"}, 401)
            return
        conn = db()
        rows = conn.execute(
            """
            select tasks.id, tasks.title, tasks.description, tasks.phone, tasks.address, tasks.city, tasks.postal_code, tasks.street, tasks.house, tasks.apartment, tasks.price, tasks.payment_method,
                   tasks.status, tasks.created_at, tasks.accepted_at, tasks.completed_at,
                   users.display_name as assigned_to_name,
                   users.login as assigned_to_login,
                   clients.login as client_login,
                   clients.display_name as client_name,
                   tasks.client_id,
                   tasks.settlement_id,
                   tasks.client_settlement_id
            from tasks
            left join users on users.id = tasks.assigned_to
            left join clients on clients.id = tasks.client_id
            where tasks.status = 'completed'
              and tasks.hidden_from_completed = 0
            order by coalesce(tasks.decided_at, tasks.created_at) desc, tasks.id desc
            """
        ).fetchall()
        conn.close()
        lang = self.request_language()
        self.send_json({"tasks": [task_json(row, lang) for row in rows]})

    def handle_create_task(self):
        if not self.is_admin():
            self.send_json({"error": "admin_unauthorized"}, 401)
            return
        data = self.read_json()
        self.create_task_from_data(data, None)

    def create_task_from_data(self, data, client_id=None):
        title = str(data.get("title", "")).strip()
        description = str(data.get("description", "")).strip()
        phone = normalize_phone(data.get("phone", ""))
        city = str(data.get("city", "")).strip()
        postal_code = str(data.get("postalCode", data.get("postal_code", ""))).strip()
        street = str(data.get("street", "")).strip()
        house = str(data.get("house", "")).strip()
        apartment = str(data.get("apartment", "")).strip()
        address = compose_address(city, postal_code, street, house, apartment, data.get("address", ""))
        price = parse_price(data.get("price", 0))
        raw_payment_method = data.get("paymentMethod", data.get("payment_method", None))
        payment_method = normalize_payment_method(raw_payment_method)
        raw_client_id = data.get("clientId", data.get("client_id", ""))
        if client_id is not None:
            if not title or not description or not phone or not city or not street:
                self.send_json({"error": "all_fields_required"}, 400)
                return
            if data.get("price") in (None, "") or price is None or price <= 0:
                self.send_json({"error": "bad_price"}, 400)
                return
            if str(raw_payment_method or "").strip().lower() not in ("cash", "card"):
                self.send_json({"error": "payment_method_required"}, 400)
                return
        if client_id is None and raw_client_id not in (None, "", "dispatcher", "null"):
            try:
                client_id = int(raw_client_id)
            except (TypeError, ValueError):
                self.send_json({"error": "bad_client_id"}, 400)
                return
        if not title:
            self.send_json({"error": "title_required"}, 400)
            return
        if not city or not street:
            self.send_json({"error": "city_and_street_required"}, 400)
            return
        if price is None:
            self.send_json({"error": "bad_price"}, 400)
            return
        conn = db()
        if client_id is not None:
            client = conn.execute(
                "select id from clients where id = ? and deleted_at is null",
                (client_id,),
            ).fetchone()
            if not client:
                conn.close()
                self.send_json({"error": "client_not_found"}, 404)
                return
            if payment_method == "card":
                reserve_available = self.client_available_reserve_for_new_card_task(client_id)
                if reserve_available < float(price or 0):
                    conn.close()
                    self.send_json({"error": "client_reserve_too_low"}, 409)
                    return
        cur = conn.execute(
            "insert into tasks(title, description, phone, address, city, postal_code, street, house, apartment, price, payment_method, client_id, created_at) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (title, description, phone, address, city, postal_code, street, house, apartment, price, payment_method, client_id, int(time.time())),
        )
        conn.commit()
        task_id = cur.lastrowid
        conn.close()
        self.send_json({"ok": True, "id": task_id}, 201)

    def handle_client_tasks(self):
        client = self.client_from_token()
        if not client:
            self.send_json({"error": "unauthorized"}, 401)
            return
        conn = db()
        rows = conn.execute(
            """
            select tasks.id, tasks.title, tasks.description, tasks.phone, tasks.address, tasks.city, tasks.postal_code, tasks.street, tasks.house, tasks.apartment, tasks.price, tasks.payment_method,
                   tasks.status, tasks.created_at, tasks.accepted_at, tasks.completed_at, tasks.client_id, tasks.settlement_id,
                   tasks.client_settlement_id,
                   users.display_name as assigned_to_name,
                   users.login as assigned_to_login,
                   clients.login as client_login,
                   clients.display_name as client_name
            from tasks
            left join users on users.id = tasks.assigned_to
            left join clients on clients.id = tasks.client_id
            where tasks.client_id = ?
              and tasks.status != 'completed'
            order by tasks.created_at desc, tasks.id desc
            """,
            (client["id"],),
        ).fetchall()
        conn.close()
        lang = self.request_language()
        self.send_json({"tasks": [task_json(row, lang) for row in rows]})

    def handle_create_client_task(self):
        client = self.client_from_token()
        if not client:
            self.send_json({"error": "unauthorized"}, 401)
            return
        self.create_task_from_data(self.read_json(), client["id"])

    def handle_delete_client_task(self, path):
        client = self.client_from_token()
        if not client:
            self.send_json({"error": "unauthorized"}, 401)
            return
        parts = path.strip("/").split("/")
        if len(parts) != 5:
            self.send_json({"error": "not_found"}, 404)
            return
        try:
            task_id = int(parts[3])
        except ValueError:
            self.send_json({"error": "bad_task_id"}, 400)
            return
        conn = db()
        row = conn.execute(
            "select id, status, payment_method from tasks where id = ? and client_id = ?",
            (task_id, client["id"]),
        ).fetchone()
        if not row:
            conn.close()
            self.send_json({"error": "task_not_found"}, 404)
            return
        if row["status"] != "new":
            conn.close()
            self.send_json({"error": "task_not_new"}, 409)
            return
        conn.execute("delete from task_events where task_id = ?", (task_id,))
        conn.execute("delete from tasks where id = ?", (task_id,))
        conn.commit()
        conn.close()
        self.send_json({"ok": True, "deleted": task_id})

    def handle_update_client_task_price(self, path):
        client = self.client_from_token()
        if not client:
            self.send_json({"error": "unauthorized"}, 401)
            return
        parts = path.strip("/").split("/")
        if len(parts) != 5:
            self.send_json({"error": "not_found"}, 404)
            return
        try:
            task_id = int(parts[3])
        except ValueError:
            self.send_json({"error": "bad_task_id"}, 400)
            return
        data = self.read_json()
        price = parse_price(data.get("price", None))
        if price is None or price <= 0:
            self.send_json({"error": "bad_price"}, 400)
            return

        conn = db()
        row = conn.execute(
            "select id, status, payment_method from tasks where id = ? and client_id = ?",
            (task_id, client["id"]),
        ).fetchone()
        if not row:
            conn.close()
            self.send_json({"error": "task_not_found"}, 404)
            return
        if row["status"] != "new":
            conn.close()
            self.send_json({"error": "task_not_new"}, 409)
            return
        if normalize_payment_method(row["payment_method"]) == "card":
            reserve_available = self.client_available_reserve_for_new_card_task(client["id"], task_id)
            if reserve_available < float(price or 0):
                conn.close()
                self.send_json({"error": "client_reserve_too_low"}, 409)
                return
        conn.execute("update tasks set price = ? where id = ?", (price, task_id))
        conn.commit()
        conn.close()
        self.send_json({"ok": True, "id": task_id, "price": price})

    def handle_client_self_report(self):
        client = self.client_from_token()
        if not client:
            self.send_json({"error": "unauthorized"}, 401)
            return
        report = self.build_client_report(client["id"])
        if report is None:
            self.send_json({"error": "client_not_found"}, 404)
            return
        self.send_json(report)

    def handle_client_self_calculations(self):
        client = self.client_from_token()
        if not client:
            self.send_json({"error": "unauthorized"}, 401)
            return
        conn = db()
        rows = conn.execute(
            """
            select id, created_at, snapshot_json
            from client_settlements
            where client_id = ?
            order by created_at desc, id desc
            """,
            (client["id"],),
        ).fetchall()
        conn.close()
        settlements = []
        for row in rows:
            try:
                snapshot = json.loads(row["snapshot_json"])
            except Exception:
                snapshot = {}
            translate_snapshot_tasks(snapshot, self.request_language())
            settlements.append(
                {
                    "id": row["id"],
                    "createdAt": row["created_at"],
                    "client": snapshot.get("client", {}),
                    "counts": snapshot.get("counts", {}),
                    "totals": snapshot.get("totals", {}),
                    "completed": snapshot.get("completed", []),
                    "active": snapshot.get("active", []),
                    "new": snapshot.get("new", []),
                    "refused": snapshot.get("refused", []),
                    "reserveEvents": snapshot.get("reserveEvents", []),
                    "currentReserveEvents": snapshot.get("currentReserveEvents", snapshot.get("reserveEvents", [])),
                    "other": snapshot.get("other", []),
                    "calculated": snapshot.get("calculated", True),
                }
            )
        settlements.sort(
            key=lambda item: (
                1 if item.get("calculated") else 0,
                -int(item.get("createdAt") or 0),
                -int(item.get("id") or 0),
            )
        )
        self.send_json({"settlements": settlements})

    def handle_client_self_reserve(self):
        client = self.client_from_token()
        if not client:
            self.send_json({"error": "unauthorized"}, 401)
            return
        self.send_json({"error": "client_reserve_transfers_disabled"}, 403)

    def handle_reset_task(self, path):
        if not self.is_admin():
            self.send_json({"error": "admin_unauthorized"}, 401)
            return
        data = self.read_json()
        if not self.has_confirmation_password(data):
            self.send_json({"error": "bad_confirmation_password"}, 401)
            return
        parts = path.strip("/").split("/")
        if len(parts) != 5:
            self.send_json({"error": "not_found"}, 404)
            return
        try:
            task_id = int(parts[3])
        except ValueError:
            self.send_json({"error": "bad_task_id"}, 400)
            return

        conn = db()
        row = conn.execute("select id from tasks where id = ?", (task_id,)).fetchone()
        if not row:
            conn.close()
            self.send_json({"error": "task_not_found"}, 404)
            return
        conn.execute(
            "update tasks set status = 'new', assigned_to = null, decided_at = null, accepted_at = null, completed_at = null, settlement_id = null where id = ?",
            (task_id,),
        )
        conn.commit()
        conn.close()
        self.send_json({"ok": True, "status": "new"})

    def handle_delete_task(self, path):
        if not self.is_admin():
            self.send_json({"error": "admin_unauthorized"}, 401)
            return
        data = self.read_json()
        if not self.has_confirmation_password(data):
            self.send_json({"error": "bad_confirmation_password"}, 401)
            return
        parts = path.strip("/").split("/")
        if len(parts) != 5:
            self.send_json({"error": "not_found"}, 404)
            return
        try:
            task_id = int(parts[3])
        except ValueError:
            self.send_json({"error": "bad_task_id"}, 400)
            return

        conn = db()
        row = conn.execute("select id from tasks where id = ?", (task_id,)).fetchone()
        if not row:
            conn.close()
            self.send_json({"error": "task_not_found"}, 404)
            return
        conn.execute("delete from tasks where id = ?", (task_id,))
        conn.commit()
        conn.close()
        self.send_json({"ok": True, "deleted": task_id})

    def handle_admin_complete_task(self, path):
        if not self.is_admin():
            self.send_json({"error": "admin_unauthorized"}, 401)
            return
        data = self.read_json()
        if not self.has_confirmation_password(data):
            self.send_json({"error": "bad_confirmation_password"}, 401)
            return
        parts = path.strip("/").split("/")
        if len(parts) != 5:
            self.send_json({"error": "not_found"}, 404)
            return
        try:
            task_id = int(parts[3])
        except ValueError:
            self.send_json({"error": "bad_task_id"}, 400)
            return

        conn = db()
        row = conn.execute("select * from tasks where id = ?", (task_id,)).fetchone()
        if not row:
            conn.close()
            self.send_json({"error": "task_not_found"}, 404)
            return
        if row["status"] != "accepted" or not row["assigned_to"]:
            conn.close()
            self.send_json({"error": "task_not_accepted"}, 409)
            return
        if not self.client_has_enough_reserve_for_card_task(row):
            conn.close()
            self.send_json({"error": "client_reserve_too_low"}, 409)
            return

        now = int(time.time())
        if normalize_payment_method(row["payment_method"]) == "cash":
            reserve_deduction = self.cash_completed_fee_reserve_deduction(row["assigned_to"], row["price"], conn)
            if reserve_deduction > 0:
                conn.execute(
                    """
                    insert into reserve_events(user_id, kind, amount, created_at)
                    values(?, ?, ?, ?)
                    """,
                    (row["assigned_to"], "cash_completed_fee_from_reserve", -reserve_deduction, now),
                )
        accepted_at = row["accepted_at"] if "accepted_at" in row.keys() and row["accepted_at"] else row["decided_at"]
        if not accepted_at:
            accepted_at = now
        conn.execute(
            "update tasks set status = 'completed', decided_at = ?, accepted_at = ?, completed_at = ? where id = ?",
            (now, accepted_at, now, task_id),
        )
        conn.commit()
        conn.close()
        self.send_json({"ok": True, "status": "completed"})

    def handle_edit_task(self, path):
        if not self.is_admin():
            self.send_json({"error": "admin_unauthorized"}, 401)
            return
        parts = path.strip("/").split("/")
        if len(parts) != 5:
            self.send_json({"error": "not_found"}, 404)
            return
        try:
            task_id = int(parts[3])
        except ValueError:
            self.send_json({"error": "bad_task_id"}, 400)
            return

        data = self.read_json()
        title = str(data.get("title", "")).strip()
        description = str(data.get("description", "")).strip()
        phone = normalize_phone(data.get("phone", ""))
        city = str(data.get("city", "")).strip()
        postal_code = str(data.get("postalCode", data.get("postal_code", ""))).strip()
        street = str(data.get("street", "")).strip()
        house = str(data.get("house", "")).strip()
        apartment = str(data.get("apartment", "")).strip()
        address = compose_address(city, postal_code, street, house, apartment, data.get("address", ""))
        price = parse_price(data.get("price", 0))
        payment_method = normalize_payment_method(data.get("paymentMethod", data.get("payment_method", "card")))
        raw_client_id = data.get("clientId", data.get("client_id", ""))
        client_id = None
        if raw_client_id not in (None, "", "dispatcher", "null"):
            try:
                client_id = int(raw_client_id)
            except (TypeError, ValueError):
                self.send_json({"error": "bad_client_id"}, 400)
                return
        if not title:
            self.send_json({"error": "title_required"}, 400)
            return
        if not city or not street:
            self.send_json({"error": "city_and_street_required"}, 400)
            return
        if price is None:
            self.send_json({"error": "bad_price"}, 400)
            return

        conn = db()
        row = conn.execute(
            """
            select id, status, settlement_id, client_settlement_id
            from tasks
            where id = ?
            """,
            (task_id,),
        ).fetchone()
        if not row:
            conn.close()
            self.send_json({"error": "task_not_found"}, 404)
            return
        if row["status"] == "completed":
            conn.close()
            self.send_json({"error": "task_locked"}, 409)
            return
        if client_id is not None:
            client = conn.execute(
                "select id from clients where id = ? and deleted_at is null",
                (client_id,),
            ).fetchone()
            if not client:
                conn.close()
                self.send_json({"error": "client_not_found"}, 404)
                return
            if payment_method == "card":
                reserve_available = self.client_available_reserve_for_new_card_task(client_id, task_id)
                if reserve_available < float(price or 0):
                    conn.close()
                    self.send_json({"error": "client_reserve_too_low"}, 409)
                    return

        conn.execute(
            """
            update tasks
            set title = ?, description = ?, phone = ?, address = ?, city = ?, postal_code = ?, street = ?, house = ?, apartment = ?, price = ?, payment_method = ?, client_id = ?
            where id = ?
            """,
            (title, description, phone, address, city, postal_code, street, house, apartment, price, payment_method, client_id, task_id),
        )
        conn.commit()
        conn.close()
        self.send_json({"ok": True, "id": task_id})

    def handle_hide_completed_task(self, path):
        if not self.is_admin():
            self.send_json({"error": "admin_unauthorized"}, 401)
            return
        data = self.read_json()
        if not self.has_confirmation_password(data):
            self.send_json({"error": "bad_confirmation_password"}, 401)
            return
        parts = path.strip("/").split("/")
        if len(parts) != 5:
            self.send_json({"error": "not_found"}, 404)
            return
        try:
            task_id = int(parts[3])
        except ValueError:
            self.send_json({"error": "bad_task_id"}, 400)
            return

        conn = db()
        row = conn.execute("select id from tasks where id = ? and status = 'completed'", (task_id,)).fetchone()
        if not row:
            conn.close()
            self.send_json({"error": "task_not_found"}, 404)
            return
        conn.execute("update tasks set hidden_from_completed = 1 where id = ?", (task_id,))
        conn.commit()
        conn.close()
        self.send_json({"ok": True, "hidden": task_id})

    def handle_admin_users(self):
        if not self.is_admin():
            self.send_json({"error": "admin_unauthorized"}, 401)
            return
        conn = db()
        rows = conn.execute(
            """
            select users.id, users.login, users.display_name, users.phone,
                   count(tasks.id) as task_count
            from users
            left join tasks on tasks.assigned_to = users.id and tasks.settlement_id is null
            where users.deleted_at is null
            group by users.id
            order by users.id
            """
        ).fetchall()
        settings = self.admin_settings()
        user_totals = {
            row["id"]: calculate_current_user_totals(conn, row["id"], settings)
            for row in rows
        }
        conn.close()
        users = [
            {
                "id": row["id"],
                "login": row["login"],
                "displayName": row["display_name"],
                "phone": row["phone"],
                "taskCount": row["task_count"],
                "totals": user_totals.get(row["id"], {}),
                "payoutPrice": user_totals.get(row["id"], {}).get("payoutPrice", 0),
                "reservePrice": user_totals.get(row["id"], {}).get("reservePrice", 0),
            }
            for row in rows
        ]
        users.sort(
            key=lambda item: (
                0 if float(item.get("payoutPrice") or 0) > 0 else 1,
                -float(item.get("payoutPrice") or 0),
                item.get("id") or 0,
            )
        )
        self.send_json({"users": users})

    def handle_create_user(self):
        if not self.is_admin():
            self.send_json({"error": "admin_unauthorized"}, 401)
            return
        data = self.read_json()
        login = normalize_phone(data.get("login", ""))
        password = str(data.get("password", "")).strip()
        display_name = str(data.get("displayName", "")).strip()
        if not login or not password or not display_name:
            self.send_json({"error": "login_password_name_required"}, 400)
            return
        if len(password) < 4:
            self.send_json({"error": "password_too_short"}, 400)
            return
        conn = db()
        exists = conn.execute("select id from users where login = ?", (login,)).fetchone()
        if exists:
            conn.close()
            self.send_json({"error": "login_already_exists"}, 409)
            return
        create_user(conn, login, password, display_name)
        conn.execute("update users set phone = ? where login = ?", (login, login))
        conn.commit()
        user_id = conn.execute("select id from users where login = ?", (login,)).fetchone()["id"]
        conn.close()
        self.send_json({"ok": True, "id": user_id}, 201)

    def handle_admin_clients(self):
        if not self.is_admin():
            self.send_json({"error": "admin_unauthorized"}, 401)
            return
        conn = db()
        rows = conn.execute(
            """
            select clients.id, clients.login, clients.display_name, clients.phone,
                   count(tasks.id) as task_count,
                   coalesce(sum(tasks.price), 0) as total_price
            from clients
            left join tasks on tasks.client_id = clients.id
                           and tasks.client_settlement_id is null
            where clients.deleted_at is null
            group by clients.id
            """
        ).fetchall()
        reserve_rows = conn.execute(
            """
            select client_id, coalesce(sum(amount), 0) as reserve_price
            from client_reserve_events
            group by client_id
            """
        ).fetchall()
        reserve_totals = {
            row["client_id"]: round(max(0, float(row["reserve_price"] or 0)), 2)
            for row in reserve_rows
        }
        current_reserve_rows = conn.execute(
            """
            select client_id,
                   coalesce(sum(case when kind = 'to_reserve' and settlement_id is null then amount else 0 end), 0) as to_reserve,
                   coalesce(sum(case when kind = 'from_reserve' and settlement_id is null then -amount else 0 end), 0) as from_reserve
            from client_reserve_events
            group by client_id
            """
        ).fetchall()
        current_transfers = {
            row["client_id"]: {
                "toReserve": float(row["to_reserve"] or 0),
                "fromReserve": float(row["from_reserve"] or 0),
            }
            for row in current_reserve_rows
        }
        conn.close()
        clients = []
        for row in rows:
            report = self.build_client_report(row["id"]) or {}
            totals = report.get("totals", {})
            clients.append(
                {
                    "id": row["id"],
                    "login": row["login"],
                    "displayName": row["display_name"],
                    "phone": row["phone"],
                    "taskCount": row["task_count"],
                    "totalPrice": totals.get("activePaymentDue", 0),
                    "reservePrice": totals.get("reservePrice", reserve_totals.get(row["id"], 0)),
                }
            )
        clients.sort(
            key=lambda item: (
                0 if float(item.get("totalPrice") or 0) > 0 else 1,
                -float(item.get("totalPrice") or 0),
                item.get("id") or 0,
            )
        )
        self.send_json({"clients": clients})

    def handle_create_client(self):
        if not self.is_admin():
            self.send_json({"error": "admin_unauthorized"}, 401)
            return
        data = self.read_json()
        login = normalize_phone(data.get("login", ""))
        password = str(data.get("password", "")).strip()
        display_name = str(data.get("displayName", "")).strip()
        if not login or not password or not display_name:
            self.send_json({"error": "login_password_name_required"}, 400)
            return
        if len(password) < 4:
            self.send_json({"error": "password_too_short"}, 400)
            return
        conn = db()
        exists = conn.execute("select id from clients where login = ?", (login,)).fetchone()
        if exists:
            conn.close()
            self.send_json({"error": "login_already_exists"}, 409)
            return
        create_client(conn, login, password, display_name)
        conn.execute("update clients set phone = ? where login = ?", (login, login))
        conn.commit()
        client_id = conn.execute("select id from clients where login = ?", (login,)).fetchone()["id"]
        conn.close()
        self.send_json({"ok": True, "id": client_id}, 201)

    def handle_update_client(self, path):
        if not self.is_admin():
            self.send_json({"error": "admin_unauthorized"}, 401)
            return
        parts = path.strip("/").split("/")
        if len(parts) != 4:
            self.send_json({"error": "not_found"}, 404)
            return
        try:
            client_id = int(parts[3])
        except ValueError:
            self.send_json({"error": "bad_client_id"}, 400)
            return
        data = self.read_json()
        login = normalize_phone(data.get("login", ""))
        display_name = str(data.get("displayName", data.get("display_name", ""))).strip()
        password = str(data.get("password", "")).strip()
        if not login or not display_name:
            self.send_json({"error": "login_name_required"}, 400)
            return
        if password and len(password) < 4:
            self.send_json({"error": "password_too_short"}, 400)
            return
        conn = db()
        client = conn.execute("select id from clients where id = ?", (client_id,)).fetchone()
        if not client:
            conn.close()
            self.send_json({"error": "client_not_found"}, 404)
            return
        exists = conn.execute(
            "select id from clients where login = ? and id != ?",
            (login, client_id),
        ).fetchone()
        if exists:
            conn.close()
            self.send_json({"error": "login_already_exists"}, 409)
            return
        if password:
            salt = secrets.token_hex(16)
            conn.execute(
                "update clients set login = ?, phone = ?, display_name = ?, password_hash = ?, salt = ? where id = ?",
                (login, login, display_name, password_hash(password, salt), salt, client_id),
            )
            conn.execute("delete from client_tokens where client_id = ?", (client_id,))
        else:
            conn.execute(
                "update clients set login = ?, phone = ?, display_name = ? where id = ?",
                (login, login, display_name, client_id),
            )
        conn.commit()
        conn.close()
        self.send_json({"ok": True})

    def handle_delete_client(self, path):
        if not self.is_admin():
            self.send_json({"error": "admin_unauthorized"}, 401)
            return
        data = self.read_json()
        if not self.has_confirmation_password(data):
            self.send_json({"error": "bad_confirmation_password"}, 401)
            return
        parts = path.strip("/").split("/")
        if len(parts) != 5:
            self.send_json({"error": "not_found"}, 404)
            return
        try:
            client_id = int(parts[3])
        except ValueError:
            self.send_json({"error": "bad_client_id"}, 400)
            return
        conn = db()
        client = conn.execute("select id from clients where id = ?", (client_id,)).fetchone()
        if not client:
            conn.close()
            self.send_json({"error": "client_not_found"}, 404)
            return
        conn.execute("update clients set deleted_at = ? where id = ?", (int(time.time()), client_id))
        conn.execute("delete from client_tokens where client_id = ?", (client_id,))
        conn.commit()
        conn.close()
        self.send_json({"ok": True, "deleted": client_id})

    def handle_update_user(self, path):
        if not self.is_admin():
            self.send_json({"error": "admin_unauthorized"}, 401)
            return
        parts = path.strip("/").split("/")
        if len(parts) != 4:
            self.send_json({"error": "not_found"}, 404)
            return
        try:
            user_id = int(parts[3])
        except ValueError:
            self.send_json({"error": "bad_user_id"}, 400)
            return

        data = self.read_json()
        login = normalize_phone(data.get("login", ""))
        display_name = str(data.get("displayName", "")).strip()
        password = str(data.get("password", "")).strip()
        if not login or not display_name:
            self.send_json({"error": "login_name_required"}, 400)
            return
        if password and len(password) < 4:
            self.send_json({"error": "password_too_short"}, 400)
            return

        conn = db()
        user = conn.execute("select id from users where id = ? and deleted_at is null", (user_id,)).fetchone()
        if not user:
            conn.close()
            self.send_json({"error": "user_not_found"}, 404)
            return
        exists = conn.execute(
            "select id from users where login = ? and id != ?",
            (login, user_id),
        ).fetchone()
        if exists:
            conn.close()
            self.send_json({"error": "login_already_exists"}, 409)
            return

        if password:
            salt = secrets.token_hex(16)
            conn.execute(
                """
                update users
                set login = ?, phone = ?, display_name = ?, password_hash = ?, salt = ?
                where id = ?
                """,
                (login, login, display_name, password_hash(password, salt), salt, user_id),
            )
            conn.execute("delete from tokens where user_id = ?", (user_id,))
        else:
            conn.execute(
                "update users set login = ?, phone = ?, display_name = ? where id = ?",
                (login, login, display_name, user_id),
            )
        conn.commit()
        conn.close()
        self.send_json({"ok": True, "id": user_id})

    def handle_delete_user(self, path):
        if not self.is_admin():
            self.send_json({"error": "admin_unauthorized"}, 401)
            return
        parts = path.strip("/").split("/")
        if len(parts) != 5:
            self.send_json({"error": "not_found"}, 404)
            return
        try:
            user_id = int(parts[3])
        except ValueError:
            self.send_json({"error": "bad_user_id"}, 400)
            return
        data = self.read_json()
        if not self.has_confirmation_password(data):
            self.send_json({"error": "bad_confirmation_password"}, 401)
            return
        conn = db()
        user = conn.execute("select id from users where id = ? and deleted_at is null", (user_id,)).fetchone()
        if not user:
            conn.close()
            self.send_json({"error": "user_not_found"}, 404)
            return
        delete_user_data(conn, user_id)
        conn.commit()
        conn.close()
        self.send_json({"ok": True, "deleted": user_id})

    def handle_user_report(self, path):
        if not self.is_admin():
            self.send_json({"error": "admin_unauthorized"}, 401)
            return
        parts = path.strip("/").split("/")
        if len(parts) != 5:
            self.send_json({"error": "not_found"}, 404)
            return
        try:
            user_id = int(parts[3])
        except ValueError:
            self.send_json({"error": "bad_user_id"}, 400)
            return

        self.send_user_report(user_id)

    def handle_my_report(self):
        user = self.user_from_token()
        if not user:
            self.send_json({"error": "unauthorized"}, 401)
            return
        self.send_user_report(user["id"])

    def handle_my_reserve(self):
        user = self.user_from_token()
        if not user:
            self.send_json({"error": "unauthorized"}, 401)
            return
        data = self.read_json()
        self.apply_reserve_transfer(user["id"], data, allowed_actions=("to_reserve", "from_reserve"))

    def send_user_report(self, user_id):
        report = self.build_user_report(user_id)
        if report is None:
            self.send_json({"error": "user_not_found"}, 404)
            return
        self.send_json(report)

    def build_user_report(self, user_id):
        conn = db()
        user = conn.execute(
            "select id, login, display_name from users where id = ?",
            (user_id,),
        ).fetchone()
        if not user:
            conn.close()
            return None

        rows = conn.execute(
            """
            select tasks.id, tasks.title, tasks.description, tasks.phone, tasks.address, tasks.city, tasks.postal_code, tasks.street, tasks.house, tasks.apartment, tasks.price, tasks.payment_method, tasks.status, tasks.created_at, tasks.decided_at, tasks.accepted_at, tasks.completed_at,
                   tasks.client_id, clients.login as client_login, clients.display_name as client_name
            from tasks
            left join clients on clients.id = tasks.client_id
            where tasks.assigned_to = ? and tasks.settlement_id is null
            order by coalesce(tasks.decided_at, tasks.created_at) desc, tasks.id desc
            """,
            (user_id,),
        ).fetchall()
        refused_rows = conn.execute(
            """
            select tasks.id, tasks.title, tasks.description, tasks.phone, tasks.address, tasks.city, tasks.postal_code, tasks.street, tasks.house, tasks.apartment, tasks.price, tasks.payment_method,
                   'refused' as status, tasks.created_at, task_events.created_at as decided_at,
                   tasks.accepted_at, tasks.completed_at, tasks.client_id,
                   clients.login as client_login, clients.display_name as client_name
            from task_events
            join tasks on tasks.id = task_events.task_id
            left join clients on clients.id = tasks.client_id
            where task_events.user_id = ?
              and task_events.event = 'refused'
              and task_events.settlement_id is null
            order by task_events.created_at desc, task_events.id desc
            """,
            (user_id,),
        ).fetchall()
        settlements = conn.execute(
            """
            select id, created_at, snapshot_json
            from settlements
            where user_id = ?
            order by created_at desc, id desc
            """,
            (user_id,),
        ).fetchall()
        reserve_rows = conn.execute(
            """
            select id, kind, amount, created_at, settlement_id
            from reserve_events
            where user_id = ?
            order by created_at desc, id desc
            """,
            (user_id,),
        ).fetchall()
        conn.close()

        lang = self.request_language()
        tasks = [task_report_json(row, lang) for row in rows]
        refused_tasks = [task_report_json(row, lang) for row in refused_rows]
        reserve_events = [reserve_event_json(row) for row in reserve_rows]
        counts = {
            "all": len(tasks),
            "completed": sum(1 for task in tasks if task["status"] == "completed"),
            "refused": len(refused_tasks),
            "accepted": sum(1 for task in tasks if task["status"] == "accepted"),
            "declined": sum(1 for task in tasks if task["status"] == "declined"),
        }
        settings = self.admin_settings()
        totals = calculate_user_totals(tasks, refused_tasks, reserve_events, settings)
        history = []
        for settlement in settlements:
            try:
                snapshot = json.loads(settlement["snapshot_json"])
            except Exception:
                snapshot = {}
            enrich_snapshot_task_sources(snapshot)
            translate_snapshot_tasks(snapshot, lang)
            snapshot["id"] = settlement["id"]
            snapshot["createdAt"] = settlement["created_at"]
            history.append(snapshot)

        return {
            "user": {
                "id": user["id"],
                "login": user["login"],
                "displayName": user["display_name"],
            },
            "counts": counts,
            "totals": totals,
            "completed": [task for task in tasks if task["status"] == "completed"],
            "refused": refused_tasks,
            "reserveEvents": reserve_events,
            "currentReserveEvents": [
                event for event in reserve_events if event.get("settlementId") is None
            ],
            "other": [
                task
                for task in tasks
                if task["status"] not in ("completed", "refused")
            ],
            "history": history,
        }

    def handle_user_reserve(self, path):
        if not self.is_admin():
            self.send_json({"error": "admin_unauthorized"}, 401)
            return
        parts = path.strip("/").split("/")
        if len(parts) != 5:
            self.send_json({"error": "not_found"}, 404)
            return
        try:
            user_id = int(parts[3])
        except ValueError:
            self.send_json({"error": "bad_user_id"}, 400)
            return
        data = self.read_json()
        if not self.has_confirmation_password(data):
            self.send_json({"error": "bad_confirmation_password"}, 401)
            return
        self.apply_reserve_transfer(user_id, data, allowed_actions=("to_reserve", "from_reserve", "top_up"))

    def apply_reserve_transfer(self, user_id, data, allowed_actions):
        amount = parse_price(data.get("amount"))
        action = str(data.get("action", "")).strip()
        if amount is None or amount <= 0:
            self.send_json({"error": "bad_amount"}, 400)
            return
        if action not in allowed_actions:
            self.send_json({"error": "bad_reserve_action"}, 400)
            return

        report = self.build_user_report(user_id)
        if report is None:
            self.send_json({"error": "user_not_found"}, 404)
            return
        totals = report["totals"]
        if action == "to_reserve" and amount > float(totals.get("payoutPrice", 0) or 0):
            self.send_json({"error": "not_enough_completed_sum"}, 400)
            return
        if action == "from_reserve" and amount > float(totals.get("reservePrice", 0) or 0):
            self.send_json({"error": "not_enough_reserve"}, 400)
            return

        reserve_amount = amount if action in ("to_reserve", "top_up") else -amount
        conn = db()
        user = conn.execute("select id from users where id = ?", (user_id,)).fetchone()
        if not user:
            conn.close()
            self.send_json({"error": "user_not_found"}, 404)
            return
        conn.execute(
            """
            insert into reserve_events(user_id, kind, amount, created_at)
            values(?, ?, ?, ?)
            """,
            (user_id, action, reserve_amount, int(time.time())),
        )
        conn.commit()
        conn.close()
        updated = self.build_user_report(user_id)
        self.send_json({"ok": True, "report": updated})

    def acceptance_capacity(self, user_id, new_task_price):
        report = self.build_user_report(user_id)
        if report is None:
            return {"allowed": False, "reason": "user_not_found"}
        totals = report["totals"]
        completed_fee_percent = float(totals.get("completedFeePercent", 0) or 0)
        current_accepted_price = float(totals.get("acceptedPrice", 0) or 0)
        candidate_price = float(new_task_price or 0)
        required_reserve = round(
            (current_accepted_price + candidate_price) * completed_fee_percent / 100,
            2,
        )
        available_reserve = round(
            float(totals.get("payoutPrice", 0) or 0) + float(totals.get("reservePrice", 0) or 0),
            2,
        )
        return {
            "allowed": required_reserve <= available_reserve,
            "availableReserve": available_reserve,
            "requiredReserve": required_reserve,
            "currentAcceptedPrice": round(current_accepted_price, 2),
            "newTaskPrice": round(candidate_price, 2),
            "completedFeePercent": completed_fee_percent,
        }

    def handle_settle_user(self, path):
        if not self.is_admin():
            self.send_json({"error": "admin_unauthorized"}, 401)
            return
        parts = path.strip("/").split("/")
        if len(parts) != 5:
            self.send_json({"error": "not_found"}, 404)
            return
        try:
            user_id = int(parts[3])
        except ValueError:
            self.send_json({"error": "bad_user_id"}, 400)
            return

        data = self.read_json()
        if not self.has_confirmation_password(data):
            self.send_json({"error": "bad_confirmation_password"}, 401)
            return

        report = self.build_user_report(user_id)
        if report is None:
            self.send_json({"error": "user_not_found"}, 404)
            return
        if not self.has_user_settlement_items(report):
            self.send_json({"error": "nothing_to_settle"}, 400)
            return

        now = int(time.time())
        snapshot = self.user_settlement_snapshot(report, now)
        conn = db()
        cur = conn.execute(
            "insert into settlements(user_id, created_at, snapshot_json) values(?, ?, ?)",
            (user_id, now, json.dumps(snapshot, ensure_ascii=False)),
        )
        settlement_id = cur.lastrowid
        task_ids = [
            task["id"]
            for task in report["completed"]
            if task.get("id")
        ]
        if task_ids:
            placeholders = ",".join("?" for _ in task_ids)
            conn.execute(
                f"""
                update tasks
                set settlement_id = ?
                where assigned_to = ?
                  and settlement_id is null
                  and id in ({placeholders})
                """,
                (settlement_id, user_id, *task_ids),
            )
        conn.execute(
            """
            update task_events
            set settlement_id = ?
            where user_id = ? and event = 'refused' and settlement_id is null
            """,
            (settlement_id, user_id),
        )
        conn.execute(
            """
            update reserve_events
            set settlement_id = ?
            where user_id = ? and settlement_id is null
            """,
            (settlement_id, user_id),
        )
        conn.commit()
        conn.close()
        self.send_json({"ok": True, "settlementId": settlement_id})

    def handle_settle_all_users(self):
        if not self.is_admin():
            self.send_json({"error": "admin_unauthorized"}, 401)
            return
        data = self.read_json()
        if not self.has_confirmation_password(data):
            self.send_json({"error": "bad_confirmation_password"}, 401)
            return
        conn = db()
        rows = conn.execute("select id from users where deleted_at is null order by id").fetchall()
        conn.close()
        created = []
        skipped = []
        for row in rows:
            report = self.build_user_report(row["id"])
            if not report or not self.has_user_settlement_items(report):
                skipped.append(row["id"])
                continue
            result = self.create_user_settlement_from_report(row["id"], report)
            if result.get("ok"):
                created.append(result["settlementId"])
            else:
                skipped.append(row["id"])
        self.send_json({"ok": True, "created": created, "skipped": skipped})

    def create_user_settlement_from_report(self, user_id, report):
        if report is None:
            return {"ok": False, "error": "user_not_found"}
        if not self.has_user_settlement_items(report):
            return {"ok": False, "error": "nothing_to_settle"}
        now = int(time.time())
        snapshot = self.user_settlement_snapshot(report, now)
        conn = db()
        cur = conn.execute(
            "insert into settlements(user_id, created_at, snapshot_json) values(?, ?, ?)",
            (user_id, now, json.dumps(snapshot, ensure_ascii=False)),
        )
        settlement_id = cur.lastrowid
        task_ids = [
            task["id"]
            for task in report["completed"]
            if task.get("id")
        ]
        if task_ids:
            placeholders = ",".join("?" for _ in task_ids)
            conn.execute(
                f"""
                update tasks
                set settlement_id = ?
                where assigned_to = ?
                  and settlement_id is null
                  and id in ({placeholders})
                """,
                (settlement_id, user_id, *task_ids),
            )
        conn.execute(
            """
            update task_events
            set settlement_id = ?
            where user_id = ? and event = 'refused' and settlement_id is null
            """,
            (settlement_id, user_id),
        )
        conn.execute(
            """
            update reserve_events
            set settlement_id = ?
            where user_id = ? and settlement_id is null
            """,
            (settlement_id, user_id),
        )
        conn.commit()
        conn.close()
        return {"ok": True, "settlementId": settlement_id}

    def has_user_settlement_items(self, report):
        return bool(report["completed"] or report["refused"] or report["currentReserveEvents"])

    def user_settlement_snapshot(self, report, created_at):
        payout_price = round(float(report["totals"].get("payoutPrice", 0) or 0), 2)
        counts = dict(report["counts"])
        counts["all"] = len(report["completed"]) + len(report["refused"])
        counts["completed"] = len(report["completed"])
        counts["refused"] = len(report["refused"])
        counts["accepted"] = 0
        counts["declined"] = 0
        return {
            "createdAt": created_at,
            "calculated": payout_price <= 0,
            "user": report["user"],
            "counts": counts,
            "totals": report["totals"],
            "completed": report["completed"],
            "refused": report["refused"],
            "reserveEvents": report["currentReserveEvents"],
            "currentReserveEvents": report["currentReserveEvents"],
            "other": [],
        }

    def handle_admin_settlements(self):
        if not self.is_admin():
            self.send_json({"error": "admin_unauthorized"}, 401)
            return
        conn = db()
        rows = conn.execute(
            """
            select settlements.id, settlements.created_at, users.display_name, settlements.snapshot_json
            from settlements
            join users on users.id = settlements.user_id
            order by settlements.created_at desc, settlements.id desc
            """
        ).fetchall()
        conn.close()
        users = []
        conn = db()
        active_users = conn.execute(
            "select id from users where deleted_at is null order by id"
        ).fetchall()
        conn.close()
        for user_row in active_users:
            report = self.build_user_report(user_row["id"])
            if report and self.has_user_settlement_items(report):
                users.append(
                    {
                        "id": report["user"]["id"],
                        "displayName": report["user"]["displayName"],
                        "counts": self.user_settlement_snapshot(report, int(time.time()))["counts"],
                        "totals": report["totals"],
                        "completed": report["completed"],
                        "refused": report["refused"],
                        "reserveEvents": report["currentReserveEvents"],
                        "currentReserveEvents": report["currentReserveEvents"],
                        "other": [],
                        "calculated": False,
                    }
                )
        settlements = []
        for row in rows:
            try:
                snapshot = json.loads(row["snapshot_json"])
            except Exception:
                snapshot = {}
            enrich_snapshot_task_sources(snapshot)
            translate_snapshot_tasks(snapshot, self.request_language())
            settlements.append(
                {
                    "id": row["id"],
                    "createdAt": row["created_at"],
                    "displayName": row["display_name"],
                    "counts": snapshot.get("counts", {}),
                    "totals": snapshot.get("totals", {}),
                    "completed": snapshot.get("completed", []),
                    "refused": snapshot.get("refused", []),
                    "reserveEvents": snapshot.get("reserveEvents", []),
                    "currentReserveEvents": snapshot.get("currentReserveEvents", snapshot.get("reserveEvents", [])),
                    "other": snapshot.get("other", []),
                    "user": snapshot.get("user", {}),
                    "calculated": snapshot.get("calculated", True),
                }
            )
        settlements.sort(
            key=lambda item: (
                1 if item.get("calculated") else 0,
                -int(item.get("createdAt") or 0),
                -int(item.get("id") or 0),
            )
        )
        self.send_json({"users": users, "settlements": settlements})

    def handle_admin_client_calculations(self):
        if not self.is_admin():
            self.send_json({"error": "admin_unauthorized"}, 401)
            return
        conn = db()
        rows = conn.execute(
            """
            select clients.id, clients.display_name,
                   count(tasks.id) as task_count,
                   sum(case when tasks.status = 'new' then 1 else 0 end) as new_count,
                   sum(case when tasks.status = 'accepted' then 1 else 0 end) as accepted_count,
                   sum(case when tasks.status = 'completed' then 1 else 0 end) as completed_count,
                   coalesce(sum(tasks.price), 0) as total_price
            from clients
            left join tasks on tasks.client_id = clients.id and tasks.client_settlement_id is null
            where clients.deleted_at is null
            group by clients.id
            order by clients.id
            """
        ).fetchall()
        settlement_rows = conn.execute(
            """
            select client_settlements.id, client_settlements.created_at, clients.display_name, client_settlements.snapshot_json
            from client_settlements
            join clients on clients.id = client_settlements.client_id
            order by client_settlements.created_at desc, client_settlements.id desc
            """
        ).fetchall()
        conn.close()
        settlements = []
        for row in settlement_rows:
            try:
                snapshot = json.loads(row["snapshot_json"])
            except Exception:
                snapshot = {}
            translate_snapshot_tasks(snapshot, self.request_language())
            calculated = snapshot.get("calculated", True)
            settlements.append(
                {
                    "id": row["id"],
                    "createdAt": row["created_at"],
                    "displayName": row["display_name"],
                    "client": snapshot.get("client", {}),
                    "counts": snapshot.get("counts", {}),
                    "totals": snapshot.get("totals", {}),
                    "completed": snapshot.get("completed", []),
                    "active": snapshot.get("active", []),
                    "new": snapshot.get("new", []),
                    "refused": snapshot.get("refused", []),
                    "reserveEvents": snapshot.get("reserveEvents", []),
                    "currentReserveEvents": snapshot.get("currentReserveEvents", snapshot.get("reserveEvents", [])),
                    "other": snapshot.get("other", []),
                    "calculated": calculated,
                }
            )
        settlements.sort(
            key=lambda item: (
                1 if item.get("calculated") else 0,
                -int(item.get("createdAt") or 0),
                -int(item.get("id") or 0),
            )
        )
        clients = []
        for row in rows:
            report = self.build_client_report(row["id"]) or {}
            if report and report.get("counts", {}).get("all"):
                clients.append(
                    {
                        "id": row["id"],
                        "displayName": row["display_name"],
                        "taskCount": row["task_count"] or 0,
                        "newCount": row["new_count"] or 0,
                        "acceptedCount": row["accepted_count"] or 0,
                        "completedCount": row["completed_count"] or 0,
                        "totalPrice": report.get("totals", {}).get("activePaymentDue", 0),
                        "counts": report.get("counts", {}),
                        "totals": report.get("totals", {}),
                        "completed": report.get("completed", []),
                        "active": report.get("active", []),
                        "new": report.get("new", []),
                        "refused": report.get("refused", []),
                        "reserveEvents": report.get("currentReserveEvents", []),
                        "currentReserveEvents": report.get("currentReserveEvents", []),
                        "other": report.get("other", []),
                        "calculated": False,
                    }
                )
        self.send_json({"clients": clients, "settlements": settlements})

    def build_client_report(self, client_id):
        conn = db()
        client = conn.execute(
            "select id, login, display_name from clients where id = ?",
            (client_id,),
        ).fetchone()
        if not client:
            conn.close()
            return None
        rows = conn.execute(
            """
            select tasks.id, tasks.title, tasks.description, tasks.phone, tasks.address, tasks.city, tasks.postal_code, tasks.street, tasks.house, tasks.apartment, tasks.price,
                   tasks.payment_method, tasks.status, tasks.created_at, tasks.decided_at,
                   tasks.accepted_at, tasks.completed_at,
                   tasks.assigned_to, tasks.client_id,
                   users.display_name as assigned_to_name,
                   users.login as assigned_to_login,
                   clients.login as client_login,
                   clients.display_name as client_name
            from tasks
            left join users on users.id = tasks.assigned_to
            left join clients on clients.id = tasks.client_id
            where tasks.client_id = ? and tasks.client_settlement_id is null
            order by tasks.created_at desc, tasks.id desc
            """,
            (client_id,),
        ).fetchall()
        reserve_rows = conn.execute(
            """
            select id, kind, amount, created_at, settlement_id
            from client_reserve_events
            where client_id = ?
            order by created_at desc, id desc
            """,
            (client_id,),
        ).fetchall()
        conn.close()
        lang = self.request_language()
        tasks = [task_report_json(row, lang) for row in rows]
        reserve_events = [reserve_event_json(row) for row in reserve_rows]
        current_reserve_events = [
            event for event in reserve_events if event.get("settlementId") is None
        ]
        active_statuses = {"accepted"}
        refused_tasks = [task for task in tasks if task["status"] in ("refused", "declined")]
        new_tasks = [task for task in tasks if task["status"] == "new"]
        active_tasks = [task for task in tasks if task["status"] in active_statuses]
        counts = {
            "all": len(tasks),
            "completed": sum(1 for task in tasks if task["status"] == "completed"),
            "active": len(active_tasks),
            "new": len(new_tasks),
            "refused": len(refused_tasks),
            "other": sum(
                1
                for task in tasks
                if task["status"] not in active_statuses
                and task["status"] != "new"
                and task["status"] != "completed"
                and task not in refused_tasks
            ),
        }
        gross_total_price = round(sum(task["price"] for task in tasks), 2)
        completed_price = round(sum(task["price"] for task in tasks if task["status"] == "completed"), 2)
        completed_card_price = round(
            sum(
                task["price"]
                for task in tasks
                if task["status"] == "completed" and task.get("paymentMethod", "card") != "cash"
            ),
            2,
        )
        completed_cash_price = round(
            sum(
                task["price"]
                for task in tasks
                if task["status"] == "completed" and task.get("paymentMethod", "card") == "cash"
            ),
            2,
        )
        active_price = round(sum(task["price"] for task in active_tasks), 2)
        active_card_price = round(
            sum(task["price"] for task in active_tasks if task.get("paymentMethod", "card") != "cash"),
            2,
        )
        active_cash_price = round(
            sum(task["price"] for task in active_tasks if task.get("paymentMethod", "card") == "cash"),
            2,
        )
        refused_price = round(sum(task["price"] for task in refused_tasks), 2)
        to_reserve = round(
            sum(event["amount"] for event in current_reserve_events if event["kind"] == "to_reserve"),
            2,
        )
        from_reserve = round(
            sum(event["absoluteAmount"] for event in current_reserve_events if event["kind"] == "from_reserve"),
            2,
        )
        reserve_before_completed = round(max(0, sum(event["amount"] for event in reserve_events)), 2)
        reserve_used_for_completed = round(min(reserve_before_completed, completed_card_price), 2)
        reserve_price = round(max(0, reserve_before_completed - completed_card_price), 2)
        total_price = round(max(0, completed_card_price - reserve_before_completed), 2)
        active_payment_due = active_cash_price
        reserved_card_price = round(active_card_price + sum(
            task["price"] for task in new_tasks if task.get("paymentMethod", "card") != "cash"
        ), 2)
        available_reserve = round(max(0, reserve_price - reserved_card_price), 2)
        totals = {
            "grossTotalPrice": gross_total_price,
            "totalPrice": total_price,
            "activePrice": active_price,
            "activeCardPrice": active_card_price,
            "activeCashPrice": active_cash_price,
            "activePaymentDue": active_payment_due,
            "reservedCardPrice": reserved_card_price,
            "availableReserve": available_reserve,
            "completedPrice": completed_price,
            "completedCardPrice": completed_card_price,
            "completedCashPrice": completed_cash_price,
            "refusedPrice": refused_price,
            "reserveBeforeCompleted": reserve_before_completed,
            "reserveUsedForCompleted": reserve_used_for_completed,
            "reservePrice": reserve_price,
            "toReserve": to_reserve,
            "fromReserve": from_reserve,
        }
        return {
            "client": {
                "id": client["id"],
                "login": client["login"],
                "displayName": client["display_name"],
            },
            "counts": counts,
            "totals": totals,
            "completed": [task for task in tasks if task["status"] == "completed"],
            "active": active_tasks,
            "new": new_tasks,
            "refused": refused_tasks,
            "reserveEvents": reserve_events,
            "currentReserveEvents": current_reserve_events,
            "other": [
                task
                for task in tasks
                if task["status"] not in active_statuses
                and task["status"] != "new"
                and task["status"] != "completed"
                and task not in refused_tasks
            ],
        }

    def handle_client_report(self, path):
        if not self.is_admin():
            self.send_json({"error": "admin_unauthorized"}, 401)
            return
        parts = path.strip("/").split("/")
        if len(parts) != 5:
            self.send_json({"error": "not_found"}, 404)
            return
        try:
            client_id = int(parts[3])
        except ValueError:
            self.send_json({"error": "bad_client_id"}, 400)
            return
        report = self.build_client_report(client_id)
        if report is None:
            self.send_json({"error": "client_not_found"}, 404)
            return
        self.send_json(report)

    def handle_client_reserve(self, path):
        if not self.is_admin():
            self.send_json({"error": "admin_unauthorized"}, 401)
            return
        parts = path.strip("/").split("/")
        if len(parts) != 5:
            self.send_json({"error": "not_found"}, 404)
            return
        try:
            client_id = int(parts[3])
        except ValueError:
            self.send_json({"error": "bad_client_id"}, 400)
            return
        data = self.read_json()
        if not self.has_confirmation_password(data):
            self.send_json({"error": "bad_confirmation_password"}, 401)
            return
        action = str(data.get("action", "")).strip()
        if action in ("to_reserve", "from_reserve"):
            self.send_json({"error": "client_reserve_transfers_disabled"}, 403)
            return
        self.apply_client_reserve_transfer(
            client_id,
            data,
            allowed_actions=("to_reserve", "from_reserve", "top_up"),
        )

    def apply_client_reserve_transfer(self, client_id, data, allowed_actions):
        amount = parse_price(data.get("amount"))
        action = str(data.get("action", "")).strip()
        if amount is None or amount <= 0:
            self.send_json({"error": "bad_amount"}, 400)
            return
        if action not in allowed_actions:
            self.send_json({"error": "bad_reserve_action"}, 400)
            return

        report = self.build_client_report(client_id)
        if report is None:
            self.send_json({"error": "client_not_found"}, 404)
            return
        totals = report["totals"]
        if action == "to_reserve" and amount > float(totals.get("totalPrice", 0) or 0):
            self.send_json({"error": "not_enough_payment_sum"}, 400)
            return
        if action == "from_reserve" and amount > float(totals.get("reservePrice", 0) or 0):
            self.send_json({"error": "not_enough_reserve"}, 400)
            return

        reserve_amount = amount if action in ("to_reserve", "top_up") else -amount
        conn = db()
        client = conn.execute("select id from clients where id = ? and deleted_at is null", (client_id,)).fetchone()
        if not client:
            conn.close()
            self.send_json({"error": "client_not_found"}, 404)
            return
        conn.execute(
            """
            insert into client_reserve_events(client_id, kind, amount, created_at)
            values(?, ?, ?, ?)
            """,
            (client_id, action, reserve_amount, int(time.time())),
        )
        conn.commit()
        conn.close()
        updated = self.build_client_report(client_id)
        self.send_json({"ok": True, "report": updated})

    def handle_settle_client(self, path):
        if not self.is_admin():
            self.send_json({"error": "admin_unauthorized"}, 401)
            return
        parts = path.strip("/").split("/")
        if len(parts) != 5:
            self.send_json({"error": "not_found"}, 404)
            return
        try:
            client_id = int(parts[3])
        except ValueError:
            self.send_json({"error": "bad_client_id"}, 400)
            return
        data = self.read_json()
        if not self.has_confirmation_password(data):
            self.send_json({"error": "bad_confirmation_password"}, 401)
            return
        report = self.build_client_report(client_id)
        if report is None:
            self.send_json({"error": "client_not_found"}, 404)
            return
        if not report["completed"] and not report["currentReserveEvents"]:
            self.send_json({"error": "nothing_to_settle"}, 400)
            return
        now = int(time.time())
        total_price = round(float(report["totals"].get("totalPrice", 0) or 0), 2)
        reserve_used_for_completed = float(report["totals"].get("reserveUsedForCompleted", 0) or 0)
        current_reserve_events = list(report["currentReserveEvents"])
        if reserve_used_for_completed > 0:
            current_reserve_events.append(
                {
                    "id": None,
                    "kind": "completed_from_reserve",
                    "amount": -round(reserve_used_for_completed, 2),
                    "absoluteAmount": round(reserve_used_for_completed, 2),
                    "createdAt": now,
                    "settlementId": None,
                }
            )
        snapshot = {
            "createdAt": now,
            "calculated": total_price <= 0,
            "client": report["client"],
            "counts": report["counts"],
            "totals": report["totals"],
            "completed": report["completed"],
            "active": report["active"],
            "new": report["new"],
            "refused": report["refused"],
            "reserveEvents": current_reserve_events,
            "currentReserveEvents": current_reserve_events,
            "other": report["other"],
        }
        conn = db()
        cur = conn.execute(
            "insert into client_settlements(client_id, created_at, snapshot_json) values(?, ?, ?)",
            (client_id, now, json.dumps(snapshot, ensure_ascii=False)),
        )
        settlement_id = cur.lastrowid
        task_ids = [
            task["id"]
            for task in report["completed"]
            if task.get("id")
        ]
        if task_ids:
            placeholders = ",".join("?" for _ in task_ids)
            conn.execute(
                f"""
                update tasks
                set client_settlement_id = ?
                where client_id = ?
                  and client_settlement_id is null
                  and id in ({placeholders})
                """,
                (settlement_id, client_id, *task_ids),
            )
        conn.execute(
            """
            update client_reserve_events
            set settlement_id = ?
            where client_id = ? and settlement_id is null
            """,
            (settlement_id, client_id),
        )
        if reserve_used_for_completed > 0:
            conn.execute(
                """
                insert into client_reserve_events(client_id, kind, amount, created_at, settlement_id)
                values(?, ?, ?, ?, ?)
                """,
                (
                    client_id,
                    "completed_from_reserve",
                    -round(reserve_used_for_completed, 2),
                    now,
                    settlement_id,
                ),
            )
        conn.commit()
        conn.close()
        self.send_json({"ok": True, "settlementId": settlement_id})

    def handle_settle_all_clients(self):
        if not self.is_admin():
            self.send_json({"error": "admin_unauthorized"}, 401)
            return
        data = self.read_json()
        if not self.has_confirmation_password(data):
            self.send_json({"error": "bad_confirmation_password"}, 401)
            return
        conn = db()
        rows = conn.execute("select id from clients where deleted_at is null order by id").fetchall()
        conn.close()
        created = []
        skipped = []
        for row in rows:
            report = self.build_client_report(row["id"])
            if not report or (not report["completed"] and not report["currentReserveEvents"]):
                skipped.append(row["id"])
                continue
            result = self.create_client_settlement_from_report(row["id"], report)
            if result.get("ok"):
                created.append(result["settlementId"])
            else:
                skipped.append(row["id"])
        self.send_json({"ok": True, "created": created, "skipped": skipped})

    def create_client_settlement_from_report(self, client_id, report):
        if report is None:
            return {"ok": False, "error": "client_not_found"}
        if not report["completed"] and not report["currentReserveEvents"]:
            return {"ok": False, "error": "nothing_to_settle"}
        now = int(time.time())
        total_price = round(float(report["totals"].get("totalPrice", 0) or 0), 2)
        reserve_used_for_completed = float(report["totals"].get("reserveUsedForCompleted", 0) or 0)
        current_reserve_events = list(report["currentReserveEvents"])
        if reserve_used_for_completed > 0:
            current_reserve_events.append(
                {
                    "id": None,
                    "kind": "completed_from_reserve",
                    "amount": -round(reserve_used_for_completed, 2),
                    "absoluteAmount": round(reserve_used_for_completed, 2),
                    "createdAt": now,
                    "settlementId": None,
                }
            )
        snapshot = {
            "createdAt": now,
            "calculated": total_price <= 0,
            "client": report["client"],
            "counts": report["counts"],
            "totals": report["totals"],
            "completed": report["completed"],
            "active": report["active"],
            "new": report["new"],
            "refused": report["refused"],
            "reserveEvents": current_reserve_events,
            "currentReserveEvents": current_reserve_events,
            "other": report["other"],
        }
        conn = db()
        cur = conn.execute(
            "insert into client_settlements(client_id, created_at, snapshot_json) values(?, ?, ?)",
            (client_id, now, json.dumps(snapshot, ensure_ascii=False)),
        )
        settlement_id = cur.lastrowid
        task_ids = [
            task["id"]
            for task in report["completed"]
            if task.get("id")
        ]
        if task_ids:
            placeholders = ",".join("?" for _ in task_ids)
            conn.execute(
                f"""
                update tasks
                set client_settlement_id = ?
                where client_id = ?
                  and client_settlement_id is null
                  and id in ({placeholders})
                """,
                (settlement_id, client_id, *task_ids),
            )
        conn.execute(
            """
            update client_reserve_events
            set settlement_id = ?
            where client_id = ? and settlement_id is null
            """,
            (settlement_id, client_id),
        )
        if reserve_used_for_completed > 0:
            conn.execute(
                """
                insert into client_reserve_events(client_id, kind, amount, created_at, settlement_id)
                values(?, ?, ?, ?, ?)
                """,
                (
                    client_id,
                    "completed_from_reserve",
                    -round(reserve_used_for_completed, 2),
                    now,
                    settlement_id,
                ),
            )
        conn.commit()
        conn.close()
        return {"ok": True, "settlementId": settlement_id}

    def handle_delete_client_settlement(self, path):
        if not self.is_admin():
            self.send_json({"error": "admin_unauthorized"}, 401)
            return
        data = self.read_json()
        if not self.has_confirmation_password(data):
            self.send_json({"error": "bad_confirmation_password"}, 401)
            return
        parts = path.strip("/").split("/")
        if len(parts) != 5:
            self.send_json({"error": "not_found"}, 404)
            return
        try:
            settlement_id = int(parts[3])
        except ValueError:
            self.send_json({"error": "bad_settlement_id"}, 400)
            return
        conn = db()
        row = conn.execute("select id from client_settlements where id = ?", (settlement_id,)).fetchone()
        if not row:
            conn.close()
            self.send_json({"error": "settlement_not_found"}, 404)
            return
        conn.execute("delete from client_settlements where id = ?", (settlement_id,))
        conn.commit()
        conn.close()
        self.send_json({"ok": True, "deleted": settlement_id})

    def handle_calculate_client_settlement(self, path):
        if not self.is_admin():
            self.send_json({"error": "admin_unauthorized"}, 401)
            return
        data = self.read_json()
        if not self.has_confirmation_password(data):
            self.send_json({"error": "bad_confirmation_password"}, 401)
            return
        parts = path.strip("/").split("/")
        if len(parts) != 5:
            self.send_json({"error": "not_found"}, 404)
            return
        try:
            settlement_id = int(parts[3])
        except ValueError:
            self.send_json({"error": "bad_settlement_id"}, 400)
            return
        conn = db()
        row = conn.execute(
            "select snapshot_json from client_settlements where id = ?",
            (settlement_id,),
        ).fetchone()
        if not row:
            conn.close()
            self.send_json({"error": "settlement_not_found"}, 404)
            return
        try:
            snapshot = json.loads(row["snapshot_json"])
        except Exception:
            snapshot = {}
        snapshot["calculated"] = True
        conn.execute(
            "update client_settlements set snapshot_json = ? where id = ?",
            (json.dumps(snapshot, ensure_ascii=False), settlement_id),
        )
        conn.commit()
        conn.close()
        self.send_json({"ok": True, "settlementId": settlement_id, "calculated": True})

    def handle_calculate_settlement(self, path):
        if not self.is_admin():
            self.send_json({"error": "admin_unauthorized"}, 401)
            return
        data = self.read_json()
        if not self.has_confirmation_password(data):
            self.send_json({"error": "bad_confirmation_password"}, 401)
            return
        parts = path.strip("/").split("/")
        if len(parts) != 5:
            self.send_json({"error": "not_found"}, 404)
            return
        try:
            settlement_id = int(parts[3])
        except ValueError:
            self.send_json({"error": "bad_settlement_id"}, 400)
            return
        conn = db()
        row = conn.execute(
            "select snapshot_json from settlements where id = ?",
            (settlement_id,),
        ).fetchone()
        if not row:
            conn.close()
            self.send_json({"error": "settlement_not_found"}, 404)
            return
        try:
            snapshot = json.loads(row["snapshot_json"])
        except Exception:
            snapshot = {}
        snapshot["calculated"] = True
        conn.execute(
            "update settlements set snapshot_json = ? where id = ?",
            (json.dumps(snapshot, ensure_ascii=False), settlement_id),
        )
        conn.commit()
        conn.close()
        self.send_json({"ok": True, "settlementId": settlement_id, "calculated": True})

    def handle_delete_settlement(self, path):
        if not self.is_admin():
            self.send_json({"error": "admin_unauthorized"}, 401)
            return
        data = self.read_json()
        if not self.has_confirmation_password(data):
            self.send_json({"error": "bad_confirmation_password"}, 401)
            return
        parts = path.strip("/").split("/")
        if len(parts) != 5:
            self.send_json({"error": "not_found"}, 404)
            return
        try:
            settlement_id = int(parts[3])
        except ValueError:
            self.send_json({"error": "bad_settlement_id"}, 400)
            return
        conn = db()
        row = conn.execute("select id from settlements where id = ?", (settlement_id,)).fetchone()
        if not row:
            conn.close()
            self.send_json({"error": "settlement_not_found"}, 404)
            return
        conn.execute("delete from settlements where id = ?", (settlement_id,))
        conn.commit()
        conn.close()
        self.send_json({"ok": True, "deleted": settlement_id})

    def marketing(self):
        import marketing_bot

        marketing_bot.init_db()
        return marketing_bot

    def send_plain(self, text, status=200):
        body = str(text or "").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_facebook_webhook_verify(self):
        params = parse_qs(urlparse(self.path).query)
        verify_token = os.environ.get("FACEBOOK_VERIFY_TOKEN", "ogarniemy-verify")
        token = params.get("hub.verify_token", [""])[0]
        challenge = params.get("hub.challenge", [""])[0]
        if token != verify_token:
            self.send_plain("bad verify token", 403)
            return
        self.send_plain(challenge)

    def handle_facebook_webhook_event(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            payload = json.loads(raw or "{}")
        except json.JSONDecodeError:
            self.send_json({"error": "bad_json"}, 400)
            return
        marketing = self.marketing()
        for entry in payload.get("entry", []):
            for event in entry.get("messaging", []):
                marketing.handle_facebook_event(event)
        self.send_plain("ok")

    def handle_telegram_login_status(self):
        marketing = self.marketing()
        self.send_json(marketing.telegram_login_status())

    def handle_telegram_login_start(self):
        data = self.read_json()
        try:
            result = self.marketing().start_telegram_login(
                str(data.get("apiId", "")).strip(),
                str(data.get("apiHash", "")).strip(),
                str(data.get("phone", "")).strip(),
                str(data.get("session", "")).strip(),
            )
        except Exception as exc:
            self.send_json({"error": "telegram_login_failed", "detail": str(exc)}, 400)
            return
        self.send_json({"ok": True, **result})

    def handle_telegram_login_complete(self):
        data = self.read_json()
        try:
            result = self.marketing().complete_telegram_login(
                str(data.get("loginId", "")).strip(),
                str(data.get("code", "")).strip(),
                str(data.get("password", "")),
            )
        except Exception as exc:
            self.send_json({"error": "telegram_code_failed", "detail": str(exc)}, 400)
            return
        if result.get("ok"):
            start_marketing_bot_worker()
        self.send_json(result)

    def handle_marketing_state(self, platform):
        if not self.is_admin():
            self.send_json({"error": "admin_unauthorized"}, 401)
            return
        marketing = self.marketing()
        conn = marketing.db()
        cities = [dict(row) for row in conn.execute(
            "select id, name, enabled from marketing_cities where platform = ? order by name",
            (platform,),
        ).fetchall()]
        messages = [dict(row) for row in conn.execute(
            "select id, title, audience, body, image_url, enabled, updated_at from marketing_messages where platform = ? order by id desc",
            (platform,),
        ).fetchall()]
        schedules = [dict(row) for row in conn.execute(
            "select id, city, target_id, send_time, message_id, enabled, last_sent_date from marketing_schedules where platform = ? order by send_time, target_id",
            (platform,),
        ).fetchall()]
        logs = [dict(row) for row in conn.execute(
            "select platform, target_type, target_id, city, action, status, detail, created_at from marketing_logs where platform = ? order by id desc limit 60",
            (platform,),
        ).fetchall()]
        if platform == "telegram":
            groups = [dict(row) for row in conn.execute(
                "select chat_id, title, city, keywords, enabled, watch_enabled, target_chat_id, response_message_id, notes, created_at from telegram_groups order by title"
            ).fetchall()]
            subscribers = [dict(row) for row in conn.execute(
                "select role, city, count(*) count from telegram_subscribers where stopped_at is null group by role, city order by role, city"
            ).fetchall()]
            hits = [dict(row) for row in conn.execute(
                "select keyword, group_chat_id, username, message, created_at from keyword_hits order by id desc limit 40"
            ).fetchall()]
            conn.close()
            self.send_json({"cities": cities, "groups": groups, "subscribers": subscribers, "messages": messages, "schedules": schedules, "logs": logs, "hits": hits})
            return
        targets = [dict(row) for row in conn.execute(
            "select id, name, city, target_id, notes, keywords, action, response_message_id, enabled, created_at from facebook_targets order by name"
        ).fetchall()]
        subscribers = [dict(row) for row in conn.execute(
            "select role, city, count(*) count from facebook_subscribers where stopped_at is null group by role, city order by role, city"
        ).fetchall()]
        conn.close()
        self.send_json({"cities": cities, "targets": targets, "subscribers": subscribers, "messages": messages, "schedules": schedules, "logs": logs})

    def handle_marketing_post(self, path):
        if not self.is_admin():
            self.send_json({"error": "admin_unauthorized"}, 401)
            return
        parts = path.strip("/").split("/")
        if len(parts) < 5:
            self.send_json({"error": "not_found"}, 404)
            return
        platform = parts[3]
        action = parts[4]
        if platform not in {"telegram", "facebook"}:
            self.send_json({"error": "bad_platform"}, 400)
            return
        data = self.read_json()
        marketing = self.marketing()
        conn = marketing.db()
        now = int(time.time())
        try:
            if action == "city":
                name = str(data.get("name", "")).strip()
                if not name:
                    self.send_json({"error": "city_required"}, 400)
                    return
                conn.execute(
                    "insert into marketing_cities(platform, name, enabled, created_at) values(?, ?, ?, ?) on conflict(platform, name) do update set enabled = excluded.enabled",
                    (platform, name, 1 if data.get("enabled", True) else 0, now),
                )
            elif action == "telegram-group" and platform == "telegram":
                chat_id = int(str(data.get("chatId", "")).strip())
                target_chat_id = str(data.get("targetChatId", "")).strip()
                if target_chat_id == "__same_group__":
                    target_chat_id = ""
                conn.execute(
                    """
                    insert into telegram_groups(chat_id, title, city, keywords, enabled, watch_enabled, target_chat_id, response_message_id, notes, created_at)
                    values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    on conflict(chat_id) do update set
                      title = excluded.title,
                      city = excluded.city,
                      keywords = excluded.keywords,
                      enabled = excluded.enabled,
                      watch_enabled = excluded.watch_enabled,
                      target_chat_id = excluded.target_chat_id,
                      response_message_id = excluded.response_message_id,
                      notes = excluded.notes
                    """,
                    (
                        chat_id,
                        str(data.get("title", "")).strip(),
                        "",
                        str(data.get("keywords", "")).strip(),
                        1 if data.get("enabled", True) else 0,
                        1 if data.get("watchEnabled", False) else 0,
                        target_chat_id,
                        int(data.get("responseMessageId") or 0) or None,
                        str(data.get("notes", "")).strip(),
                        now,
                    ),
                )
            elif action == "facebook-target" and platform == "facebook":
                target_id = data.get("id")
                if target_id:
                    conn.execute(
                        "update facebook_targets set name = ?, city = ?, target_id = ?, notes = ?, keywords = ?, action = ?, response_message_id = ?, enabled = ? where id = ?",
                        (str(data.get("name", "")).strip(), "", str(data.get("targetId", "")).strip(), str(data.get("notes", "")).strip(), str(data.get("keywords", "")).strip(), str(data.get("targetAction", "same_group")).strip(), int(data.get("responseMessageId") or 0) or None, 1 if data.get("enabled", True) else 0, int(target_id)),
                    )
                else:
                    conn.execute(
                        "insert into facebook_targets(name, city, target_id, notes, keywords, action, response_message_id, enabled, created_at) values(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (str(data.get("name", "")).strip(), "", str(data.get("targetId", "")).strip(), str(data.get("notes", "")).strip(), str(data.get("keywords", "")).strip(), str(data.get("targetAction", "same_group")).strip(), int(data.get("responseMessageId") or 0) or None, 1 if data.get("enabled", True) else 0, now),
                    )
            elif action == "delete":
                kind = str(data.get("kind", "")).strip()
                item_id = str(data.get("id", "")).strip()
                if kind == "telegram-group" and platform == "telegram":
                    conn.execute("delete from telegram_groups where chat_id = ?", (int(item_id),))
                elif kind == "facebook-target" and platform == "facebook":
                    conn.execute("delete from facebook_targets where id = ?", (int(item_id),))
                elif kind == "message":
                    conn.execute("delete from marketing_messages where id = ? and platform = ?", (int(item_id), platform))
                elif kind == "schedule":
                    conn.execute("delete from marketing_schedules where id = ? and platform = ?", (int(item_id), platform))
                else:
                    self.send_json({"error": "bad_delete_kind"}, 400)
                    return
            elif action == "clear-logs":
                conn.execute("delete from marketing_logs where platform = ?", (platform,))
            elif action == "upload-image":
                content_type = str(data.get("contentType", "")).split(";", 1)[0].strip().lower()
                allowed = {
                    "image/jpeg": ".jpg",
                    "image/png": ".png",
                    "image/webp": ".webp",
                    "image/gif": ".gif",
                }
                if content_type not in allowed:
                    self.send_json({"error": "bad_image_type"}, 400)
                    return
                encoded = str(data.get("content", ""))
                if "," in encoded:
                    encoded = encoded.split(",", 1)[1]
                try:
                    body = base64.b64decode(encoded, validate=True)
                except (ValueError, TypeError):
                    self.send_json({"error": "bad_image"}, 400)
                    return
                if not body or len(body) > 5 * 1024 * 1024:
                    self.send_json({"error": "image_too_large"}, 400)
                    return
                folder = os.path.join(ROOT, "assets", "marketing")
                os.makedirs(folder, exist_ok=True)
                filename = f"{platform}-{int(time.time())}-{uuid.uuid4().hex}{allowed[content_type]}"
                path = os.path.abspath(os.path.join(folder, filename))
                if os.path.commonpath([folder, path]) != folder:
                    self.send_json({"error": "bad_path"}, 400)
                    return
                with open(path, "wb") as file:
                    file.write(body)
                host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "www.ogarniemy.pro"
                proto = self.headers.get("X-Forwarded-Proto") or ("http" if host.startswith(("127.0.0.1", "localhost")) else "https")
                conn.close()
                self.send_json({"ok": True, "url": f"{proto}://{host}/assets/marketing/{filename}"})
                return
            elif action == "message":
                message_id = data.get("id")
                values = (platform, str(data.get("title", "")).strip() or "Р РµРєР»Р°РјР°", str(data.get("audience", "all")).strip() or "all", str(data.get("body", "")).strip(), str(data.get("imageUrl", "")).strip(), 1 if data.get("enabled", True) else 0, now)
                if not values[3]:
                    self.send_json({"error": "body_required"}, 400)
                    return
                if message_id:
                    conn.execute(
                        "update marketing_messages set title = ?, audience = ?, body = ?, image_url = ?, enabled = ?, updated_at = ? where id = ? and platform = ?",
                        (values[1], values[2], values[3], values[4], values[5], now, int(message_id), platform),
                    )
                else:
                    conn.execute(
                        "insert into marketing_messages(platform, title, audience, body, image_url, enabled, created_at, updated_at) values(?, ?, ?, ?, ?, ?, ?, ?)",
                        values + (now,),
                    )
            elif action == "schedule":
                schedule_id = data.get("id")
                city = str(data.get("city", "")).strip()
                target_id = str(data.get("targetId", "")).strip()
                send_time = str(data.get("sendTime", "")).strip()
                message_id = int(data.get("messageId") or 0)
                if len(send_time) != 5 or ":" not in send_time or not message_id:
                    self.send_json({"error": "bad_schedule"}, 400)
                    return
                if schedule_id:
                    conn.execute(
                        "update marketing_schedules set city = ?, target_id = ?, send_time = ?, message_id = ?, enabled = ? where id = ? and platform = ?",
                        (city, target_id, send_time, message_id, 1 if data.get("enabled", True) else 0, int(schedule_id), platform),
                    )
                else:
                    conn.execute(
                        "insert into marketing_schedules(platform, city, target_id, send_time, message_id, enabled, created_at) values(?, ?, ?, ?, ?, ?, ?)",
                        (platform, city, target_id, send_time, message_id, 1 if data.get("enabled", True) else 0, now),
                    )
            elif action == "send-now":
                message_id = int(data.get("messageId") or 0)
                city = str(data.get("city", "")).strip()
                target_id = str(data.get("targetId", "")).strip()
                message = conn.execute("select body, image_url from marketing_messages where id = ? and platform = ?", (message_id, platform)).fetchone()
                if not message:
                    self.send_json({"error": "message_not_found"}, 404)
                    return
                conn.commit()
                conn.close()
                if platform == "telegram":
                    sent, total = marketing.post_to_groups(message["body"], message["image_url"] or "", city, target_id)
                    self.send_json({"ok": True, "sent": sent, "total": total})
                    return
                marketing.log_marketing("facebook", "manual", "", city, "send_now", "prepared", "Facebook РѕС‚РїСЂР°РІРєР° С‚СЂРµР±СѓРµС‚ РїРѕРґРєР»СЋС‡РµРЅРЅРѕРіРѕ Page Access Token Рё РІС…РѕРґСЏС‰РёС… РїРѕРґРїРёСЃС‡РёРєРѕРІ.")
                self.send_json({"ok": True, "prepared": True})
                return
            else:
                self.send_json({"error": "bad_action"}, 400)
                return
            conn.commit()
        except (ValueError, TypeError):
            conn.close()
            self.send_json({"error": "bad_value"}, 400)
            return
        conn.close()
        self.send_json({"ok": True})


def row_value(row, key, default=""):
    try:
        return row[key] if row[key] is not None else default
    except (KeyError, IndexError):
        return default


def enrich_snapshot_task_sources(snapshot):
    tasks = []
    for key in ("completed", "active", "new", "refused", "other"):
        tasks.extend(task for task in snapshot.get(key, []) or [] if isinstance(task, dict))
    task_ids = sorted({task.get("id") for task in tasks if task.get("id")})
    if not task_ids:
        return
    placeholders = ",".join("?" for _ in task_ids)
    conn = db()
    rows = conn.execute(
        f"""
        select tasks.id, tasks.address, tasks.city, tasks.postal_code, tasks.street, tasks.house, tasks.apartment,
               tasks.client_id, clients.login as client_login, clients.display_name as client_name
        from tasks
        left join clients on clients.id = tasks.client_id
        where tasks.id in ({placeholders})
        """,
        task_ids,
    ).fetchall()
    conn.close()
    by_id = {row["id"]: row for row in rows}
    for task in tasks:
        row = by_id.get(task.get("id"))
        if not row:
            continue
        client_id = row["client_id"]
        client_name = row["client_name"] or ""
        task["clientId"] = client_id
        task["clientName"] = "" if is_placeholder_text(client_name) else client_name
        task["clientLogin"] = row["client_login"] or ""
        task["source"] = "client" if client_id else "dispatcher"
        task["sourceName"] = task["clientName"] if client_id else ""
        task["address"] = compose_address(
            row["city"],
            row["postal_code"],
            row["street"],
            row["house"],
            row["apartment"],
            row["address"],
        )


def translate_snapshot_tasks(snapshot, lang):
    for key in ("completed", "active", "new", "refused", "other"):
        for task in snapshot.get(key, []) or []:
            if not isinstance(task, dict):
                continue
            original_title = task.get("originalTitle") or task.get("title") or ""
            original_description = task.get("originalDescription") or task.get("description") or ""
            task["originalTitle"] = original_title
            task["originalDescription"] = original_description
            task["title"] = translate_text(original_title, lang)
            task["description"] = translate_text(original_description, lang)


def task_json(row, lang="ru", hide_private=False):
    client_name = ""
    assigned_to_name = ""
    assigned_to_login = ""
    client_login = ""
    payment_method = "card"
    try:
        client_name = row["client_name"] or ""
    except (KeyError, IndexError):
        client_name = ""
    try:
        client_login = row["client_login"] or ""
    except (KeyError, IndexError):
        client_login = ""
    try:
        assigned_to_name = row["assigned_to_name"] or ""
    except (KeyError, IndexError):
        assigned_to_name = ""
    try:
        assigned_to_login = row["assigned_to_login"] or ""
    except (KeyError, IndexError):
        assigned_to_login = ""
    try:
        payment_method = normalize_payment_method(row["payment_method"])
    except (KeyError, IndexError):
        payment_method = "card"
    client_id = None
    settlement_id = None
    client_settlement_id = None
    try:
        client_id = row["client_id"]
    except (KeyError, IndexError):
        client_id = None
    try:
        settlement_id = row["settlement_id"]
    except (KeyError, IndexError):
        settlement_id = None
    try:
        client_settlement_id = row["client_settlement_id"]
    except (KeyError, IndexError):
        client_settlement_id = None
    accepted_at = None
    completed_at = None
    decided_at = None
    try:
        accepted_at = row["accepted_at"]
    except (KeyError, IndexError):
        accepted_at = None
    try:
        completed_at = row["completed_at"]
    except (KeyError, IndexError):
        completed_at = None
    try:
        decided_at = row["decided_at"]
    except (KeyError, IndexError):
        decided_at = None
    editable = row["status"] != "completed"
    city = row_value(row, "city")
    postal_code = row_value(row, "postal_code")
    street = row_value(row, "street")
    house = "" if hide_private else row_value(row, "house")
    apartment = "" if hide_private else row_value(row, "apartment")
    address = compose_address(city, postal_code, street, house, apartment, row["address"])
    return {
        "id": row["id"],
        "title": translate_text(row["title"], lang),
        "description": translate_text(row["description"], lang),
        "phone": "" if hide_private else row["phone"],
        "address": address,
        "city": city,
        "postalCode": postal_code,
        "street": street,
        "house": house,
        "apartment": apartment,
        "originalTitle": row["title"],
        "originalDescription": row["description"],
        "originalPhone": row["phone"],
        "originalAddress": row["address"],
        "price": row["price"],
        "paymentMethod": payment_method,
        "paymentMethodName": payment_method_name(payment_method),
        "status": row["status"],
        "assignedToName": assigned_to_name,
        "assignedToLogin": assigned_to_login,
        "clientId": client_id,
        "clientName": "" if is_placeholder_text(client_name) else client_name,
        "clientLogin": client_login,
        "source": "client" if client_id else "dispatcher",
        "sourceName": ("" if is_placeholder_text(client_name) else client_name) if client_id else "",
        "editable": editable,
        "createdAt": row["created_at"],
        "acceptedAt": accepted_at,
        "completedAt": completed_at,
        "decidedAt": decided_at,
    }


def task_report_json(row, lang="ru"):
    payment_method = "card"
    assigned_to_name = ""
    assigned_to_login = ""
    client_id = row_value(row, "client_id", None)
    client_name = row_value(row, "client_name")
    client_login = row_value(row, "client_login")
    try:
        payment_method = normalize_payment_method(row["payment_method"])
    except (KeyError, IndexError):
        payment_method = "card"
    try:
        assigned_to_name = row["assigned_to_name"] or ""
    except (KeyError, IndexError):
        assigned_to_name = ""
    try:
        assigned_to_login = row["assigned_to_login"] or ""
    except (KeyError, IndexError):
        assigned_to_login = ""
    accepted_at = None
    completed_at = None
    try:
        accepted_at = row["accepted_at"]
    except (KeyError, IndexError):
        accepted_at = None
    try:
        completed_at = row["completed_at"]
    except (KeyError, IndexError):
        completed_at = None
    return {
        "id": row["id"],
        "title": translate_text(row["title"], lang),
        "description": translate_text(row["description"], lang),
        "phone": row["phone"],
        "address": compose_address(
            row_value(row, "city"),
            row_value(row, "postal_code"),
            row_value(row, "street"),
            row_value(row, "house"),
            row_value(row, "apartment"),
            row["address"],
        ),
        "city": row_value(row, "city"),
        "postalCode": row_value(row, "postal_code"),
        "street": row_value(row, "street"),
        "house": row_value(row, "house"),
        "apartment": row_value(row, "apartment"),
        "originalTitle": row["title"],
        "originalDescription": row["description"],
        "originalPhone": row["phone"],
        "originalAddress": row["address"],
        "price": row["price"],
        "paymentMethod": payment_method,
        "paymentMethodName": payment_method_name(payment_method),
        "status": row["status"],
        "assignedToName": assigned_to_name,
        "assignedToLogin": assigned_to_login,
        "clientId": client_id,
        "clientName": "" if is_placeholder_text(client_name) else client_name,
        "clientLogin": client_login,
        "source": "client" if client_id else "dispatcher",
        "sourceName": ("" if is_placeholder_text(client_name) else client_name) if client_id else "",
        "createdAt": row["created_at"],
        "decidedAt": row["decided_at"],
        "acceptedAt": accepted_at,
        "completedAt": completed_at,
    }


def calculate_current_user_totals(conn, user_id, settings):
    rows = conn.execute(
        """
        select price, payment_method, status
        from tasks
        where assigned_to = ? and settlement_id is null
        """,
        (user_id,),
    ).fetchall()
    refused_rows = conn.execute(
        """
        select tasks.price, tasks.payment_method, 'refused' as status
        from task_events
        join tasks on tasks.id = task_events.task_id
        where task_events.user_id = ?
          and task_events.event = 'refused'
          and task_events.settlement_id is null
        """,
        (user_id,),
    ).fetchall()
    reserve_rows = conn.execute(
        """
        select id, kind, amount, created_at, settlement_id
        from reserve_events
        where user_id = ?
        order by created_at desc, id desc
        """,
        (user_id,),
    ).fetchall()
    tasks = [
        {
            "price": row["price"] or 0,
            "status": row["status"],
            "paymentMethod": normalize_payment_method(row["payment_method"]),
        }
        for row in rows
    ]
    refused_tasks = [
        {
            "price": row["price"] or 0,
            "status": "refused",
            "paymentMethod": normalize_payment_method(row["payment_method"]),
        }
        for row in refused_rows
    ]
    reserve_events = [reserve_event_json(row) for row in reserve_rows]
    return calculate_user_totals(tasks, refused_tasks, reserve_events, settings)


def calculate_user_totals(tasks, refused_tasks, reserve_events, settings):
    completed_gross_price = round(
        sum(task["price"] for task in tasks if task["status"] == "completed"),
        2,
    )
    completed_card_price = round(
        sum(
            task["price"]
            for task in tasks
            if task["status"] == "completed" and task.get("paymentMethod", "card") != "cash"
        ),
        2,
    )
    completed_cash_price = round(
        sum(
            task["price"]
            for task in tasks
            if task["status"] == "completed" and task.get("paymentMethod", "card") == "cash"
        ),
        2,
    )
    accepted_price = round(
        sum(task["price"] for task in tasks if task["status"] == "accepted"),
        2,
    )
    refused_price = round(sum(task["price"] for task in refused_tasks), 2)
    completed_fee_percent = float(settings.get("completedFeePercent", 1) or 0)
    refused_fee_percent = float(settings.get("refusedFeePercent", 1) or 0)
    completed_card_fee = round(completed_card_price * completed_fee_percent / 100, 2)
    completed_cash_fee = round(completed_cash_price * completed_fee_percent / 100, 2)
    completed_fee = round(completed_card_fee + completed_cash_fee, 2)
    refused_fee = round(refused_price * refused_fee_percent / 100, 2)

    current_reserve_events = [
        event for event in reserve_events if event.get("settlementId") is None
    ]
    completed_to_reserve = round(
        sum(event["absoluteAmount"] for event in current_reserve_events if event["kind"] == "to_reserve"),
        2,
    )
    reserve_to_completed = round(
        sum(event["absoluteAmount"] for event in current_reserve_events if event["kind"] == "from_reserve"),
        2,
    )
    refused_from_reserve = round(
        sum(event["absoluteAmount"] for event in current_reserve_events if event["kind"] == "refused_fee_from_reserve"),
        2,
    )
    cash_completed_from_reserve_raw = round(
        sum(event["absoluteAmount"] for event in current_reserve_events if event["kind"] == "cash_completed_fee_from_reserve"),
        2,
    )
    cash_completed_from_reserve = round(min(cash_completed_from_reserve_raw, completed_cash_fee), 2)
    cash_completed_fee_remaining = round(max(0, completed_cash_fee - cash_completed_from_reserve), 2)
    payout_fee_base = round(max(0, completed_card_price - completed_to_reserve + reserve_to_completed), 2)
    completed_fee_from_payout = round(
        min(
            payout_fee_base,
            completed_card_fee + cash_completed_fee_remaining,
        ),
        2,
    )
    completed_fee_uncovered = round(max(0, completed_fee - completed_fee_from_payout - cash_completed_from_reserve), 2)
    payout_before_refused_fee = round(
        max(0, completed_card_price - completed_fee_from_payout - completed_to_reserve + reserve_to_completed),
        2,
    )
    refused_from_completed = round(
        min(
            payout_before_refused_fee,
            max(0, refused_fee - refused_from_reserve),
        ),
        2,
    )
    refused_uncovered = round(max(0, refused_fee - refused_from_completed - refused_from_reserve), 2)
    payout_price = round(
        max(0, payout_before_refused_fee - refused_from_completed),
        2,
    )
    reserve_price = round(max(0, sum(event["amount"] for event in reserve_events)), 2)

    required_accepted_reserve = round(accepted_price * completed_fee_percent / 100, 2)
    available_for_accepted = round(payout_price + reserve_price, 2)

    return {
        "completedGrossPrice": completed_gross_price,
        "completedPrice": completed_gross_price,
        "completedCardPrice": completed_card_price,
        "completedCashPrice": completed_cash_price,
        "acceptedPrice": accepted_price,
        "refusedPrice": refused_price,
        "completedFeePercent": completed_fee_percent,
        "refusedFeePercent": refused_fee_percent,
        "completedFee": completed_fee,
        "completedFeeFromPayout": completed_fee_from_payout,
        "completedFeeFromReserve": cash_completed_from_reserve,
        "completedFeeUncovered": completed_fee_uncovered,
        "completedToReserve": completed_to_reserve,
        "reserveToCompleted": reserve_to_completed,
        "refusedFee": refused_fee,
        "refusedFeeFromCompleted": refused_from_completed,
        "refusedFeeFromReserve": refused_from_reserve,
        "refusedFeeUncovered": refused_uncovered,
        "reservePrice": reserve_price,
        "requiredAcceptedReserve": required_accepted_reserve,
        "availableForAccepted": available_for_accepted,
        "payoutPrice": payout_price,
    }


def reserve_event_json(row):
    amount = round(float(row["amount"] or 0), 2)
    return {
        "id": row["id"],
        "kind": row["kind"],
        "amount": amount,
        "absoluteAmount": round(abs(amount), 2),
        "createdAt": row["created_at"],
        "settlementId": row["settlement_id"],
    }


def normalize_payment_method(value):
    method = str(value or "card").strip().lower()
    return "cash" if method == "cash" else "card"


def payment_method_name(value):
    return "????????" if normalize_payment_method(value) == "cash" else "?? ???????"


def parse_price(value):
    if value is None or value == "":
        return 0
    try:
        return round(float(str(value).replace(",", ".")), 2)
    except ValueError:
        return None


def translate_text(text, lang):
    text = "" if text is None else str(text)
    if not text:
        return text
    key = (lang, text)
    if key in TRANSLATION_CACHE:
        return TRANSLATION_CACHE[key]
    try:
        query = urlencode(
            {
                "client": "gtx",
                "sl": "auto",
                "tl": lang,
                "dt": "t",
                "q": text,
            }
        )
        with urlopen(
            "https://translate.googleapis.com/translate_a/single?" + query,
            timeout=5,
        ) as response:
            data = json.loads(response.read().decode("utf-8"))
        translated = "".join(part[0] for part in data[0] if part and part[0])
        TRANSLATION_CACHE[key] = translated or text
        return TRANSLATION_CACHE[key]
    except Exception:
        return text


SERVER_LANGUAGE_TOOLS = r"""
<style id="server-language-style">
  header { position: relative; }
  header nav {
    display: grid !important;
    grid-template-columns: repeat(10, minmax(96px, 1fr)) !important;
    gap: 8px !important;
    align-items: stretch !important;
  }
  header nav a {
    min-height: 42px !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
    padding: 8px 10px !important;
    border-radius: 8px !important;
    text-align: center !important;
    line-height: 1.15 !important;
    white-space: normal !important;
    box-sizing: border-box !important;
  }
  .language-corner { position: absolute; top: 18px; right: 20px; z-index: 5; }
  .language-corner select {
    width: auto !important;
    min-width: 126px !important;
    height: 36px !important;
    font: inherit !important;
    font-size: 14px !important;
    line-height: 1.1 !important;
    padding: 6px 10px !important;
    border-radius: 8px !important;
    border: 0 !important;
    background: white !important;
    color: #172026 !important;
    font-weight: 700 !important;
    box-shadow: 0 10px 22px rgba(23, 32, 38, 0.14) !important;
    text-align: left !important;
    text-align-last: left !important;
  }
  @media (max-width: 760px) {
    .language-corner { position: static; margin: 0 0 12px auto; width: max-content; }
    header nav { grid-template-columns: repeat(2, minmax(0, 1fr)) !important; }
  }
</style>
<script id="server-language-script">
(function () {
  const languages = [
    ["en", "English"],
    ["uk", "\u0423\u043a\u0440\u0430\u0457\u043d\u0441\u044c\u043a\u0430"],
    ["ru", "\u0420\u0443\u0441\u0441\u043a\u0438\u0439"],
    ["pl", "Polski"]
  ];
  const dict = {
    en: {
      "Р—Р°РґР°РЅРёСЏ": "Tasks", "РЎРѕС‚СЂСѓРґРЅРёРєРё": "Employees", "РљР»РёРµРЅС‚С‹": "Clients", "Р’С‹РїРѕР»РЅРµРЅРЅС‹Рµ Р·Р°РґР°РЅРёСЏ": "Completed tasks",
      "Р Р°СЃС‡РµС‚С‹ СЃРѕС‚СЂСѓРґРЅРёРєРѕРІ": "Employee payments", "Р Р°СЃС‡РµС‚С‹ РєР»РёРµРЅС‚РѕРІ": "Client payments", "РќР°СЃС‚СЂРѕР№РєРё": "Settings",
      "РќР°Р·РІР°РЅРёРµ Р·Р°РґР°РЅРёСЏ": "Task name", "РќР°Р·РІР°РЅРёРµ": "Name", "Р—Р°РґР°РЅРёРµ": "Task", "РћРїРёСЃР°РЅРёРµ": "Description",
      "РќРѕРјРµСЂ С‚РµР»РµС„РѕРЅР°": "Phone number", "РќРѕРјРµСЂ": "Phone", "РўРµР»РµС„РѕРЅР°": "Number", "РўРµР»РµС„РѕРЅ": "Phone",
      "РђРґСЂРµСЃ": "Address", "Р“РѕСЂРѕРґ": "City", "РљРѕРґ": "Postal code", "РЈР»РёС†Р°": "Street", "Р”РѕРј": "House", "РљРІР°СЂС‚РёСЂР°": "Apartment", "Р¦РµРЅР°": "Price", "РљР°СЂС‚Р°": "Card", "РќР°Р»РёС‡РЅС‹Рµ": "Cash", "РћРїР»Р°С‚Р°": "Payment",
      "Р”РѕР±Р°РІРёС‚СЊ": "Add", "Р РµРґР°РєС‚РёСЂРѕРІР°С‚СЊ": "Edit", "РЎРѕС…СЂР°РЅРёС‚СЊ": "Save", "РћС‚РјРµРЅР°": "Cancel", "РЈРґР°Р»РёС‚СЊ": "Delete",
      "РќР°С‡Р°С‚СЊ Р·Р°РЅРѕРІРѕ": "Start again", "РћР±РЅРѕРІРёС‚СЊ СЃРїРёСЃРѕРє": "Refresh list", "РРјСЏ СЃРѕС‚СЂСѓРґРЅРёРєР°": "Employee name",
      "РРјСЏ РєР»РёРµРЅС‚Р°": "Client name", "Р›РѕРіРёРЅ": "Login", "РџР°СЂРѕР»СЊ": "Password", "РќРѕРІС‹Р№ РїР°СЂРѕР»СЊ, РµСЃР»Рё РЅСѓР¶РЅРѕ": "New password if needed",
      "РЈРґР°Р»РёС‚СЊ СЃРѕС‚СЂСѓРґРЅРёРєР°": "Delete employee", "РЈРґР°Р»РёС‚СЊ РєР»РёРµРЅС‚Р°": "Delete client", "РћС‚С‡РµС‚": "Report", "Р Р°СЃСЃС‡РёС‚Р°С‚СЊ": "Calculate",
      "РћРїР»Р°С‚РёС‚СЊ": "Pay", "РћРїР»Р°С‡РµРЅРѕ": "Paid", "РќРµ РѕРїР»Р°С‡РµРЅРѕ": "Not paid", "Р Р°СЃСЃС‡РёС‚Р°РЅРѕ": "Paid", "РќРµ СЂР°СЃСЃС‡РёС‚Р°РЅРѕ": "Not paid", "РЎС‚Р°С‚СѓСЃ СЂР°СЃС‡РµС‚Р°": "Payment status",
      "РЎС‚Р°С‚СѓСЃ": "Status", "РќРѕРІРѕРµ": "New", "РџСЂРёРЅСЏС‚Рѕ": "Accepted", "РћС‚РєР»РѕРЅРµРЅРѕ": "Declined", "Р’С‹РїРѕР»РЅРµРЅРѕ": "Completed",
      "РћС‚РєР°Р·Р°Р»СЃСЏ": "Refused", "Р’ СЂР°Р±РѕС‚Рµ": "In progress", "Р’СЃРµРіРѕ РЅР°Р·РЅР°С‡РµРЅРѕ": "Total assigned", "РђРєС‚РёРІРЅС‹Рµ": "Active",
      "РќРѕРІС‹Рµ": "New", "Р’СЃРµРіРѕ Р·Р°РґР°РЅРёР№": "Total tasks", "Р—Р°РґР°РЅРёР№": "Tasks", "РўРµРєСѓС‰РёР№ СЂР°СЃС‡РµС‚": "Current payment",
      "РСЃС‚РѕСЂРёСЏ СЂР°СЃС‡РµС‚РѕРІ": "Payment history", "Р Р°СЃС‡РµС‚": "Payment", "РЎСѓРјРјР° СЂР°СЃС‡РµС‚Р°": "Payment total",
      "РЎСѓРјРјР° Рє РѕРїР»Р°С‚Рµ": "Amount to pay", "РЎСѓРјРјР° Рє РІС‹РїР»Р°С‚Рµ": "Amount to pay", "РЎСѓРјРјР° РІС‹РїРѕР»РЅРµРЅРЅС‹С…": "Completed total", "РЎСѓРјРјР° РѕС‚РєР°Р·Р°РЅРЅС‹С…": "Refused total",
      "РћР±С‰Р°СЏ СЃСѓРјРјР° Р·Р°РґР°РЅРёР№": "Total task amount", "Р’С‹РїРѕР»РЅРµРЅРЅС‹Рµ СЂР°Р±РѕС‚С‹": "Completed jobs", "РћС‚РєР°Р·Р°РЅРЅС‹Рµ СЂР°Р±РѕС‚С‹": "Refused jobs",
      "Р Р°Р±РѕС‚С‹ СЃ РѕС‚РєР°Р·РѕРј": "Refused jobs", "РћСЃС‚Р°Р»СЊРЅС‹Рµ СЂР°Р±РѕС‚С‹": "Other jobs", "РђРєС‚РёРІРЅС‹Рµ СЂР°Р±РѕС‚С‹": "Active jobs",
      "РџРѕР»РЅС‹Р№ РѕС‚С‡РµС‚ РїРѕ СЂР°СЃС‡РµС‚Сѓ": "Full payment report", "РџРѕРґСЂРѕР±РЅС‹Р№ РѕС‚С‡РµС‚": "Detailed report", "РќРµС‚ Р·Р°РїРёСЃРµР№": "No records",
      "РЎРѕР·РґР°РЅРЅС‹С… СЂР°СЃС‡РµС‚РѕРІ РїРѕРєР° РЅРµС‚": "No payments have been created yet", "РќРѕРІС‹Р№ СЂР°СЃС‡РµС‚ СЃРѕР·РґР°РµС‚СЃСЏ РІ СЂР°Р·РґРµР»Рµ": "A new payment is created in",
      "РєРЅРѕРїРєРѕР№": "with the button", "РџР°СЂРѕР»СЊ РїРѕРґС‚РІРµСЂР¶РґРµРЅРёСЏ": "Confirmation password", "РЎСѓРјРјР°": "Amount",
      "Р РµР·РµСЂРІ": "Reserve", "РџРѕРїРѕР»РЅРёС‚СЊ СЂРµР·РµСЂРІ": "Top up reserve", "РџРѕРїРѕР»РЅРёС‚СЊ": "Top up", "РћРїРµСЂР°С†РёРё СЂРµР·РµСЂРІР°": "Reserve operations",
      "РР· СЃСѓРјРјС‹ Рє РІС‹РїР»Р°С‚Рµ РІ СЂРµР·РµСЂРІ": "From payout to reserve", "РР· СЂРµР·РµСЂРІР° РІ РІС‹РїР»Р°С‚Сѓ": "From reserve to payout",
      "РџРѕРїРѕР»РЅРµРЅРёРµ СЂРµР·РµСЂРІР°": "Reserve top-up", "РЈРґРµСЂР¶Р°РЅРёРµ Р·Р° РѕС‚РєР°Р· РёР· СЂРµР·РµСЂРІР°": "Refusal fee from reserve",
      "РЈРґРµСЂР¶Р°РЅРёРµ Р·Р° РЅР°Р»РёС‡РЅС‹Рµ РёР· СЂРµР·РµСЂРІР°": "Cash job fee from reserve", "РЈРґРµСЂР¶Р°РЅРёРµ": "Fee",
      "РџСЂРѕС†РµРЅС‚ СЃ РІС‹РїРѕР»РЅРµРЅРЅС‹С… СЂР°Р±РѕС‚, РєРѕС‚РѕСЂС‹Р№ РјС‹ СѓРґРµСЂР¶РёРІР°РµРј СЃРµР±Рµ": "Percent withheld from completed jobs",
      "РџСЂРѕС†РµРЅС‚ СЃ РѕС‚РєР°Р·Р°РЅРЅС‹С… РёР»Рё РѕС‚РјРµРЅРµРЅРЅС‹С… СЂР°Р±РѕС‚, РєРѕС‚РѕСЂС‹Р№ РјС‹ СѓРґРµСЂР¶РёРІР°РµРј СЃРµР±Рµ": "Percent withheld from refused or cancelled jobs",
      "Р’Р°Р»СЋС‚Р°": "Currency", "Р•РґРёРЅРёС†Р° СЂРµР·РµСЂРІР°": "Reserve unit", "РџРѕРєР°Р·С‹РІР°С‚СЊ С†РµРЅС‹ Рё СЃСѓРјРјС‹": "Show prices and amounts", "РЎРєРѕР»СЊРєРѕ РґРЅРµР№ С…СЂР°РЅРёС‚СЊ РІС‹РїРѕР»РЅРµРЅРЅРѕРµ Р·Р°РґР°РЅРёРµ": "How many days to keep completed tasks", "РЎРєРѕР»СЊРєРѕ РґРЅРµР№ С…СЂР°РЅРёС‚СЊ РЅРµРїСЂРёРЅСЏС‚С‹Рµ Р·Р°РґР°РЅРёСЏ": "How many days to keep unaccepted tasks", "РЎРєРѕР»СЊРєРѕ РґРЅРµР№ С…СЂР°РЅРёС‚СЊ СЂР°СЃС‡РµС‚С‹ СЃРѕС‚СЂСѓРґРЅРёРєРѕРІ": "How many days to keep employee payments", "РЎРєРѕР»СЊРєРѕ РґРЅРµР№ С…СЂР°РЅРёС‚СЊ СЂР°СЃС‡РµС‚С‹ РєР»РёРµРЅС‚РѕРІ": "How many days to keep client payments", "РўРµР»РµС„РѕРЅ РѕР±СЂР°С‚РЅРѕР№ СЃРІСЏР·Рё": "Feedback phone",
      "E-mail РѕР±СЂР°С‚РЅРѕР№ СЃРІСЏР·Рё": "Feedback e-mail", "РћР±С‹С‡РЅС‹Р№ Р°РґСЂРµСЃ": "Regular address", "Telegram": "Telegram", "WhatsApp": "WhatsApp",
      "РР·РјРµРЅРёС‚СЊ РїР°СЂРѕР»СЊ": "Change password", "РЎС‚Р°СЂС‹Р№ РїР°СЂРѕР»СЊ": "Old password", "РџРѕРІС‚РѕСЂРёС‚Рµ СЃС‚Р°СЂС‹Р№ РїР°СЂРѕР»СЊ": "Repeat old password",
      "Р’РІРµРґРёС‚Рµ РЅРѕРІС‹Р№ РїР°СЂРѕР»СЊ": "Enter new password", "РЎР±СЂРѕСЃРёС‚СЊ РїР°СЂРѕР»СЊ": "Reset password", "РќР°СЃС‚СЂРѕР№РєРё СЃРѕС…СЂР°РЅРµРЅС‹": "Settings saved",
      "РќРµ СѓРґР°Р»РѕСЃСЊ Р·Р°РіСЂСѓР·РёС‚СЊ РЅР°СЃС‚СЂРѕР№РєРё": "Could not load settings", "РќРµ СѓРґР°Р»РѕСЃСЊ СЃРѕС…СЂР°РЅРёС‚СЊ РЅР°СЃС‚СЂРѕР№РєРё": "Could not save settings",
      "РџР°СЂРѕР»СЊ РёР·РјРµРЅРµРЅ": "Password changed", "РџР°СЂРѕР»СЊ СЃР±СЂРѕС€РµРЅ": "Password reset", "РЎРѕР·РґР°РЅРѕ": "Created", "РР·РјРµРЅРµРЅРѕ": "Changed",
      "РСЃС‚РѕС‡РЅРёРє": "Source", "Р”РёСЃРїРµС‚С‡РµСЂ": "Dispatcher", "РљР»РёРµРЅС‚": "Client", "СЃРѕС‚СЂСѓРґРЅРёРє": "employee",
      "РЈРґР°Р»РёС‚СЊ СЌС‚Рѕ Р·Р°РґР°РЅРёРµ": "Delete this task", "РќРµ СѓРґР°Р»РѕСЃСЊ РІРµСЂРЅСѓС‚СЊ Р·Р°РґР°РЅРёРµ": "Could not return task", "РќРµ СѓРґР°Р»РѕСЃСЊ СѓРґР°Р»РёС‚СЊ Р·Р°РґР°РЅРёРµ": "Could not delete task",
      "РќРµ СѓРґР°Р»РѕСЃСЊ СЃРѕС…СЂР°РЅРёС‚СЊ Р·Р°РґР°РЅРёРµ": "Could not save task", "РќРµ СѓРґР°Р»РѕСЃСЊ Р·Р°РіСЂСѓР·РёС‚СЊ РєР»РёРµРЅС‚РѕРІ": "Could not load clients",
      "РќРµ СѓРґР°Р»РѕСЃСЊ СЃРѕС…СЂР°РЅРёС‚СЊ РєР»РёРµРЅС‚Р°": "Could not save client", "РќРµ СѓРґР°Р»РѕСЃСЊ СѓРґР°Р»РёС‚СЊ РєР»РёРµРЅС‚Р°": "Could not delete client",
      "РќРµ СѓРґР°Р»РѕСЃСЊ РґРѕР±Р°РІРёС‚СЊ РєР»РёРµРЅС‚Р°": "Could not add client", "РќСѓР¶РЅРѕ Р·Р°РЅРѕРІРѕ РІРІРµСЃС‚Рё РїР°СЂРѕР»СЊ Р°РґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂР°": "Enter the admin password again",
      "Р—Р°РїРѕР»РЅРёС‚Рµ РёРјСЏ РєР»РёРµРЅС‚Р° Рё Р»РѕРіРёРЅ": "Fill in the client name and login", "РџР°СЂРѕР»СЊ РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ РЅРµ РєРѕСЂРѕС‡Рµ 4 СЃРёРјРІРѕР»РѕРІ": "Password must be at least 4 characters",
      "РўР°РєРѕР№ Р»РѕРіРёРЅ СѓР¶Рµ РёСЃРїРѕР»СЊР·СѓРµС‚СЃСЏ": "This login is already used", "РљР»РёРµРЅС‚ РЅРµ РЅР°Р№РґРµРЅ": "Client not found",
      "РЈРґР°Р»РёС‚СЊ СЂР°СЃС‡РµС‚": "Delete payment", "РќРµ СѓРґР°Р»РѕСЃСЊ Р·Р°РіСЂСѓР·РёС‚СЊ СЂР°СЃС‡РµС‚С‹": "Could not load payments",
      "РќРµ СѓРґР°Р»РѕСЃСЊ Р·Р°РіСЂСѓР·РёС‚СЊ СЂР°СЃС‡РµС‚С‹ РєР»РёРµРЅС‚РѕРІ": "Could not load client payments", "РќРµ СѓРґР°Р»РѕСЃСЊ СѓРґР°Р»РёС‚СЊ СЂР°СЃС‡РµС‚": "Could not delete payment",
      "РќРµ СѓРґР°Р»РѕСЃСЊ РѕРїР»Р°С‚РёС‚СЊ": "Could not pay", "РќРµ СѓРґР°Р»РѕСЃСЊ РѕС‚РјРµС‚РёС‚СЊ СЂР°СЃС‡РµС‚ РєР°Рє СЂР°СЃСЃС‡РёС‚Р°РЅРЅС‹Р№": "Could not mark payment as paid"
    },
    uk: {
      "Р—Р°РґР°РЅРёСЏ": "Р—Р°РІРґР°РЅРЅСЏ", "РЎРѕС‚СЂСѓРґРЅРёРєРё": "РЎРїС–РІСЂРѕР±С–С‚РЅРёРєРё", "РљР»РёРµРЅС‚С‹": "РљР»С–С”РЅС‚Рё", "Р’С‹РїРѕР»РЅРµРЅРЅС‹Рµ Р·Р°РґР°РЅРёСЏ": "Р’РёРєРѕРЅР°РЅС– Р·Р°РІРґР°РЅРЅСЏ",
      "Р Р°СЃС‡РµС‚С‹ СЃРѕС‚СЂСѓРґРЅРёРєРѕРІ": "Р РѕР·СЂР°С…СѓРЅРєРё СЃРїС–РІСЂРѕР±С–С‚РЅРёРєС–РІ", "Р Р°СЃС‡РµС‚С‹ РєР»РёРµРЅС‚РѕРІ": "Р РѕР·СЂР°С…СѓРЅРєРё РєР»С–С”РЅС‚С–РІ", "РќР°СЃС‚СЂРѕР№РєРё": "РќР°Р»Р°С€С‚СѓРІР°РЅРЅСЏ",
      "РќР°Р·РІР°РЅРёРµ Р·Р°РґР°РЅРёСЏ": "РќР°Р·РІР° Р·Р°РІРґР°РЅРЅСЏ", "РќР°Р·РІР°РЅРёРµ": "РќР°Р·РІР°", "Р—Р°РґР°РЅРёРµ": "Р—Р°РІРґР°РЅРЅСЏ", "РћРїРёСЃР°РЅРёРµ": "РћРїРёСЃ",
      "РќРѕРјРµСЂ С‚РµР»РµС„РѕРЅР°": "РќРѕРјРµСЂ С‚РµР»РµС„РѕРЅСѓ", "РќРѕРјРµСЂ": "РќРѕРјРµСЂ", "РўРµР»РµС„РѕРЅР°": "РўРµР»РµС„РѕРЅСѓ", "РўРµР»РµС„РѕРЅ": "РўРµР»РµС„РѕРЅ",
      "РђРґСЂРµСЃ": "РђРґСЂРµСЃР°", "Р“РѕСЂРѕРґ": "РњС–СЃС‚Рѕ", "РљРѕРґ": "РљРѕРґ", "РЈР»РёС†Р°": "Р’СѓР»РёС†СЏ", "Р”РѕРј": "Р‘СѓРґРёРЅРѕРє", "РљРІР°СЂС‚РёСЂР°": "РљРІР°СЂС‚РёСЂР°", "Р¦РµРЅР°": "Р¦С–РЅР°", "РљР°СЂС‚Р°": "РљР°СЂС‚РєР°", "РќР°Р»РёС‡РЅС‹Рµ": "Р“РѕС‚С–РІРєР°", "РћРїР»Р°С‚Р°": "РћРїР»Р°С‚Р°",
      "Р”РѕР±Р°РІРёС‚СЊ": "Р”РѕРґР°С‚Рё", "Р РµРґР°РєС‚РёСЂРѕРІР°С‚СЊ": "Р РµРґР°РіСѓРІР°С‚Рё", "РЎРѕС…СЂР°РЅРёС‚СЊ": "Р—Р±РµСЂРµРіС‚Рё", "РћС‚РјРµРЅР°": "РЎРєР°СЃСѓРІР°С‚Рё", "РЈРґР°Р»РёС‚СЊ": "Р’РёРґР°Р»РёС‚Рё",
      "РќР°С‡Р°С‚СЊ Р·Р°РЅРѕРІРѕ": "РџРѕС‡Р°С‚Рё Р·Р°РЅРѕРІРѕ", "РћР±РЅРѕРІРёС‚СЊ СЃРїРёСЃРѕРє": "РћРЅРѕРІРёС‚Рё СЃРїРёСЃРѕРє", "РРјСЏ СЃРѕС‚СЂСѓРґРЅРёРєР°": "Р†Рј'СЏ СЃРїС–РІСЂРѕР±С–С‚РЅРёРєР°",
      "РРјСЏ РєР»РёРµРЅС‚Р°": "Р†Рј'СЏ РєР»С–С”РЅС‚Р°", "Р›РѕРіРёРЅ": "Р›РѕРіС–РЅ", "РџР°СЂРѕР»СЊ": "РџР°СЂРѕР»СЊ", "РќРѕРІС‹Р№ РїР°СЂРѕР»СЊ, РµСЃР»Рё РЅСѓР¶РЅРѕ": "РќРѕРІРёР№ РїР°СЂРѕР»СЊ, СЏРєС‰Рѕ РїРѕС‚СЂС–Р±РЅРѕ",
      "РЈРґР°Р»РёС‚СЊ СЃРѕС‚СЂСѓРґРЅРёРєР°": "Р’РёРґР°Р»РёС‚Рё СЃРїС–РІСЂРѕР±С–С‚РЅРёРєР°", "РЈРґР°Р»РёС‚СЊ РєР»РёРµРЅС‚Р°": "Р’РёРґР°Р»РёС‚Рё РєР»С–С”РЅС‚Р°", "РћС‚С‡РµС‚": "Р—РІС–С‚", "Р Р°СЃСЃС‡РёС‚Р°С‚СЊ": "Р РѕР·СЂР°С…СѓРІР°С‚Рё",
      "РћРїР»Р°С‚РёС‚СЊ": "РћРїР»Р°С‚РёС‚Рё", "РћРїР»Р°С‡РµРЅРѕ": "РћРїР»Р°С‡РµРЅРѕ", "РќРµ РѕРїР»Р°С‡РµРЅРѕ": "РќРµ РѕРїР»Р°С‡РµРЅРѕ", "Р Р°СЃСЃС‡РёС‚Р°РЅРѕ": "РћРїР»Р°С‡РµРЅРѕ", "РќРµ СЂР°СЃСЃС‡РёС‚Р°РЅРѕ": "РќРµ РѕРїР»Р°С‡РµРЅРѕ", "РЎС‚Р°С‚СѓСЃ СЂР°СЃС‡РµС‚Р°": "РЎС‚Р°С‚СѓСЃ СЂРѕР·СЂР°С…СѓРЅРєСѓ",
      "РЎС‚Р°С‚СѓСЃ": "РЎС‚Р°С‚СѓСЃ", "РќРѕРІРѕРµ": "РќРѕРІРµ", "РџСЂРёРЅСЏС‚Рѕ": "РџСЂРёР№РЅСЏС‚Рѕ", "РћС‚РєР»РѕРЅРµРЅРѕ": "Р’С–РґС…РёР»РµРЅРѕ", "Р’С‹РїРѕР»РЅРµРЅРѕ": "Р’РёРєРѕРЅР°РЅРѕ",
      "РћС‚РєР°Р·Р°Р»СЃСЏ": "Р’С–РґРјРѕРІРёРІСЃСЏ", "Р’ СЂР°Р±РѕС‚Рµ": "РЈ СЂРѕР±РѕС‚С–", "Р’СЃРµРіРѕ РЅР°Р·РЅР°С‡РµРЅРѕ": "РЈСЃСЊРѕРіРѕ РїСЂРёР·РЅР°С‡РµРЅРѕ", "РђРєС‚РёРІРЅС‹Рµ": "РђРєС‚РёРІРЅС–",
      "РќРѕРІС‹Рµ": "РќРѕРІС–", "Р’СЃРµРіРѕ Р·Р°РґР°РЅРёР№": "РЈСЃСЊРѕРіРѕ Р·Р°РІРґР°РЅСЊ", "Р—Р°РґР°РЅРёР№": "Р—Р°РІРґР°РЅСЊ", "РўРµРєСѓС‰РёР№ СЂР°СЃС‡РµС‚": "РџРѕС‚РѕС‡РЅРёР№ СЂРѕР·СЂР°С…СѓРЅРѕРє",
      "РСЃС‚РѕСЂРёСЏ СЂР°СЃС‡РµС‚РѕРІ": "Р†СЃС‚РѕСЂС–СЏ СЂРѕР·СЂР°С…СѓРЅРєС–РІ", "Р Р°СЃС‡РµС‚": "Р РѕР·СЂР°С…СѓРЅРѕРє", "РЎСѓРјРјР° СЂР°СЃС‡РµС‚Р°": "РЎСѓРјР° СЂРѕР·СЂР°С…СѓРЅРєСѓ",
      "РЎСѓРјРјР° Рє РѕРїР»Р°С‚Рµ": "РЎСѓРјР° РґРѕ РѕРїР»Р°С‚Рё", "РЎСѓРјРјР° Рє РІС‹РїР»Р°С‚Рµ": "РЎСѓРјР° РґРѕ РІРёРїР»Р°С‚Рё", "РЎСѓРјРјР° РІС‹РїРѕР»РЅРµРЅРЅС‹С…": "РЎСѓРјР° РІРёРєРѕРЅР°РЅРёС…", "РЎСѓРјРјР° РѕС‚РєР°Р·Р°РЅРЅС‹С…": "РЎСѓРјР° РІС–РґРјРѕРІ",
      "РћР±С‰Р°СЏ СЃСѓРјРјР° Р·Р°РґР°РЅРёР№": "Р—Р°РіР°Р»СЊРЅР° СЃСѓРјР° Р·Р°РІРґР°РЅСЊ", "Р’С‹РїРѕР»РЅРµРЅРЅС‹Рµ СЂР°Р±РѕС‚С‹": "Р’РёРєРѕРЅР°РЅС– СЂРѕР±РѕС‚Рё", "РћС‚РєР°Р·Р°РЅРЅС‹Рµ СЂР°Р±РѕС‚С‹": "Р’С–РґРјРѕРІР»РµРЅС– СЂРѕР±РѕС‚Рё",
      "Р Р°Р±РѕС‚С‹ СЃ РѕС‚РєР°Р·РѕРј": "Р РѕР±РѕС‚Рё Р· РІС–РґРјРѕРІРѕСЋ", "РћСЃС‚Р°Р»СЊРЅС‹Рµ СЂР°Р±РѕС‚С‹": "Р†РЅС€С– СЂРѕР±РѕС‚Рё", "РђРєС‚РёРІРЅС‹Рµ СЂР°Р±РѕС‚С‹": "РђРєС‚РёРІРЅС– СЂРѕР±РѕС‚Рё",
      "РџРѕР»РЅС‹Р№ РѕС‚С‡РµС‚ РїРѕ СЂР°СЃС‡РµС‚Сѓ": "РџРѕРІРЅРёР№ Р·РІС–С‚ Р·Р° СЂРѕР·СЂР°С…СѓРЅРєРѕРј", "РџРѕРґСЂРѕР±РЅС‹Р№ РѕС‚С‡РµС‚": "Р”РѕРєР»Р°РґРЅРёР№ Р·РІС–С‚", "РќРµС‚ Р·Р°РїРёСЃРµР№": "РќРµРјР°С” Р·Р°РїРёСЃС–РІ",
      "РЎРѕР·РґР°РЅРЅС‹С… СЂР°СЃС‡РµС‚РѕРІ РїРѕРєР° РЅРµС‚": "РЎС‚РІРѕСЂРµРЅРёС… СЂРѕР·СЂР°С…СѓРЅРєС–РІ РїРѕРєРё РЅРµРјР°С”", "РќРѕРІС‹Р№ СЂР°СЃС‡РµС‚ СЃРѕР·РґР°РµС‚СЃСЏ РІ СЂР°Р·РґРµР»Рµ": "РќРѕРІРёР№ СЂРѕР·СЂР°С…СѓРЅРѕРє СЃС‚РІРѕСЂСЋС”С‚СЊСЃСЏ РІ СЂРѕР·РґС–Р»С–",
      "РєРЅРѕРїРєРѕР№": "РєРЅРѕРїРєРѕСЋ", "РџР°СЂРѕР»СЊ РїРѕРґС‚РІРµСЂР¶РґРµРЅРёСЏ": "РџР°СЂРѕР»СЊ РїС–РґС‚РІРµСЂРґР¶РµРЅРЅСЏ", "РЎСѓРјРјР°": "РЎСѓРјР°",
      "Р РµР·РµСЂРІ": "Р РµР·РµСЂРІ", "РџРѕРїРѕР»РЅРёС‚СЊ СЂРµР·РµСЂРІ": "РџРѕРїРѕРІРЅРёС‚Рё СЂРµР·РµСЂРІ", "РџРѕРїРѕР»РЅРёС‚СЊ": "РџРѕРїРѕРІРЅРёС‚Рё", "РћРїРµСЂР°С†РёРё СЂРµР·РµСЂРІР°": "РћРїРµСЂР°С†С–С— СЂРµР·РµСЂРІСѓ",
      "РР· СЃСѓРјРјС‹ Рє РІС‹РїР»Р°С‚Рµ РІ СЂРµР·РµСЂРІ": "Р†Р· СЃСѓРјРё РґРѕ РІРёРїР»Р°С‚Рё РІ СЂРµР·РµСЂРІ", "РР· СЂРµР·РµСЂРІР° РІ РІС‹РїР»Р°С‚Сѓ": "Р— СЂРµР·РµСЂРІСѓ Сѓ РІРёРїР»Р°С‚Сѓ",
      "РџРѕРїРѕР»РЅРµРЅРёРµ СЂРµР·РµСЂРІР°": "РџРѕРїРѕРІРЅРµРЅРЅСЏ СЂРµР·РµСЂРІСѓ", "РЈРґРµСЂР¶Р°РЅРёРµ Р·Р° РѕС‚РєР°Р· РёР· СЂРµР·РµСЂРІР°": "РЈС‚СЂРёРјР°РЅРЅСЏ Р·Р° РІС–РґРјРѕРІСѓ Р· СЂРµР·РµСЂРІСѓ",
      "РЈРґРµСЂР¶Р°РЅРёРµ Р·Р° РЅР°Р»РёС‡РЅС‹Рµ РёР· СЂРµР·РµСЂРІР°": "РЈС‚СЂРёРјР°РЅРЅСЏ Р·Р° РіРѕС‚С–РІРєСѓ Р· СЂРµР·РµСЂРІСѓ", "РЈРґРµСЂР¶Р°РЅРёРµ": "РЈС‚СЂРёРјР°РЅРЅСЏ",
      "РџСЂРѕС†РµРЅС‚ СЃ РІС‹РїРѕР»РЅРµРЅРЅС‹С… СЂР°Р±РѕС‚, РєРѕС‚РѕСЂС‹Р№ РјС‹ СѓРґРµСЂР¶РёРІР°РµРј СЃРµР±Рµ": "Р’С–РґСЃРѕС‚РѕРє Р· РІРёРєРѕРЅР°РЅРёС… СЂРѕР±С–С‚, СЏРєРёР№ РјРё СѓС‚СЂРёРјСѓС”РјРѕ СЃРѕР±С–",
      "РџСЂРѕС†РµРЅС‚ СЃ РѕС‚РєР°Р·Р°РЅРЅС‹С… РёР»Рё РѕС‚РјРµРЅРµРЅРЅС‹С… СЂР°Р±РѕС‚, РєРѕС‚РѕСЂС‹Р№ РјС‹ СѓРґРµСЂР¶РёРІР°РµРј СЃРµР±Рµ": "Р’С–РґСЃРѕС‚РѕРє Р· РІС–РґРјРѕРІР»РµРЅРёС… Р°Р±Рѕ СЃРєР°СЃРѕРІР°РЅРёС… СЂРѕР±С–С‚, СЏРєРёР№ РјРё СѓС‚СЂРёРјСѓС”РјРѕ СЃРѕР±С–",
      "Р’Р°Р»СЋС‚Р°": "Р’Р°Р»СЋС‚Р°", "Р•РґРёРЅРёС†Р° СЂРµР·РµСЂРІР°": "РћРґРёРЅРёС†СЏ СЂРµР·РµСЂРІСѓ", "РџРѕРєР°Р·С‹РІР°С‚СЊ С†РµРЅС‹ Рё СЃСѓРјРјС‹": "РџРѕРєР°Р·СѓРІР°С‚Рё С†С–РЅРё С‚Р° СЃСѓРјРё", "РЎРєРѕР»СЊРєРѕ РґРЅРµР№ С…СЂР°РЅРёС‚СЊ РІС‹РїРѕР»РЅРµРЅРЅРѕРµ Р·Р°РґР°РЅРёРµ": "РЎРєС–Р»СЊРєРё РґРЅС–РІ Р·Р±РµСЂС–РіР°С‚Рё РІРёРєРѕРЅР°РЅРµ Р·Р°РІРґР°РЅРЅСЏ", "РЎРєРѕР»СЊРєРѕ РґРЅРµР№ С…СЂР°РЅРёС‚СЊ РЅРµРїСЂРёРЅСЏС‚С‹Рµ Р·Р°РґР°РЅРёСЏ": "РЎРєС–Р»СЊРєРё РґРЅС–РІ Р·Р±РµСЂС–РіР°С‚Рё РЅРµРїСЂРёР№РЅСЏС‚С– Р·Р°РІРґР°РЅРЅСЏ", "РЎРєРѕР»СЊРєРѕ РґРЅРµР№ С…СЂР°РЅРёС‚СЊ СЂР°СЃС‡РµС‚С‹ СЃРѕС‚СЂСѓРґРЅРёРєРѕРІ": "РЎРєС–Р»СЊРєРё РґРЅС–РІ Р·Р±РµСЂС–РіР°С‚Рё СЂРѕР·СЂР°С…СѓРЅРєРё СЃРїС–РІСЂРѕР±С–С‚РЅРёРєС–РІ", "РЎРєРѕР»СЊРєРѕ РґРЅРµР№ С…СЂР°РЅРёС‚СЊ СЂР°СЃС‡РµС‚С‹ РєР»РёРµРЅС‚РѕРІ": "РЎРєС–Р»СЊРєРё РґРЅС–РІ Р·Р±РµСЂС–РіР°С‚Рё СЂРѕР·СЂР°С…СѓРЅРєРё РєР»С–С”РЅС‚С–РІ", "РўРµР»РµС„РѕРЅ РѕР±СЂР°С‚РЅРѕР№ СЃРІСЏР·Рё": "РўРµР»РµС„РѕРЅ Р·РІРѕСЂРѕС‚РЅРѕРіРѕ Р·РІ'СЏР·РєСѓ",
      "E-mail РѕР±СЂР°С‚РЅРѕР№ СЃРІСЏР·Рё": "E-mail Р·РІРѕСЂРѕС‚РЅРѕРіРѕ Р·РІ'СЏР·РєСѓ", "РћР±С‹С‡РЅС‹Р№ Р°РґСЂРµСЃ": "Р—РІРёС‡Р°Р№РЅР° Р°РґСЂРµСЃР°", "Telegram": "Telegram", "WhatsApp": "WhatsApp",
      "РР·РјРµРЅРёС‚СЊ РїР°СЂРѕР»СЊ": "Р—РјС–РЅРёС‚Рё РїР°СЂРѕР»СЊ", "РЎС‚Р°СЂС‹Р№ РїР°СЂРѕР»СЊ": "РЎС‚Р°СЂРёР№ РїР°СЂРѕР»СЊ", "РџРѕРІС‚РѕСЂРёС‚Рµ СЃС‚Р°СЂС‹Р№ РїР°СЂРѕР»СЊ": "РџРѕРІС‚РѕСЂС–С‚СЊ СЃС‚Р°СЂРёР№ РїР°СЂРѕР»СЊ",
      "Р’РІРµРґРёС‚Рµ РЅРѕРІС‹Р№ РїР°СЂРѕР»СЊ": "Р’РІРµРґС–С‚СЊ РЅРѕРІРёР№ РїР°СЂРѕР»СЊ", "РЎР±СЂРѕСЃРёС‚СЊ РїР°СЂРѕР»СЊ": "РЎРєРёРЅСѓС‚Рё РїР°СЂРѕР»СЊ", "РќР°СЃС‚СЂРѕР№РєРё СЃРѕС…СЂР°РЅРµРЅС‹": "РќР°Р»Р°С€С‚СѓРІР°РЅРЅСЏ Р·Р±РµСЂРµР¶РµРЅРѕ",
      "РќРµ СѓРґР°Р»РѕСЃСЊ Р·Р°РіСЂСѓР·РёС‚СЊ РЅР°СЃС‚СЂРѕР№РєРё": "РќРµ РІРґР°Р»РѕСЃСЏ Р·Р°РІР°РЅС‚Р°Р¶РёС‚Рё РЅР°Р»Р°С€С‚СѓРІР°РЅРЅСЏ", "РќРµ СѓРґР°Р»РѕСЃСЊ СЃРѕС…СЂР°РЅРёС‚СЊ РЅР°СЃС‚СЂРѕР№РєРё": "РќРµ РІРґР°Р»РѕСЃСЏ Р·Р±РµСЂРµРіС‚Рё РЅР°Р»Р°С€С‚СѓРІР°РЅРЅСЏ",
      "РџР°СЂРѕР»СЊ РёР·РјРµРЅРµРЅ": "РџР°СЂРѕР»СЊ Р·РјС–РЅРµРЅРѕ", "РџР°СЂРѕР»СЊ СЃР±СЂРѕС€РµРЅ": "РџР°СЂРѕР»СЊ СЃРєРёРЅСѓС‚Рѕ", "РЎРѕР·РґР°РЅРѕ": "РЎС‚РІРѕСЂРµРЅРѕ", "РР·РјРµРЅРµРЅРѕ": "Р—РјС–РЅРµРЅРѕ",
      "РСЃС‚РѕС‡РЅРёРє": "Р”Р¶РµСЂРµР»Рѕ", "Р”РёСЃРїРµС‚С‡РµСЂ": "Р”РёСЃРїРµС‚С‡РµСЂ", "РљР»РёРµРЅС‚": "РљР»С–С”РЅС‚", "СЃРѕС‚СЂСѓРґРЅРёРє": "СЃРїС–РІСЂРѕР±С–С‚РЅРёРє",
      "РЈРґР°Р»РёС‚СЊ СЌС‚Рѕ Р·Р°РґР°РЅРёРµ": "Р’РёРґР°Р»РёС‚Рё С†Рµ Р·Р°РІРґР°РЅРЅСЏ", "РќРµ СѓРґР°Р»РѕСЃСЊ РІРµСЂРЅСѓС‚СЊ Р·Р°РґР°РЅРёРµ": "РќРµ РІРґР°Р»РѕСЃСЏ РїРѕРІРµСЂРЅСѓС‚Рё Р·Р°РІРґР°РЅРЅСЏ", "РќРµ СѓРґР°Р»РѕСЃСЊ СѓРґР°Р»РёС‚СЊ Р·Р°РґР°РЅРёРµ": "РќРµ РІРґР°Р»РѕСЃСЏ РІРёРґР°Р»РёС‚Рё Р·Р°РІРґР°РЅРЅСЏ",
      "РќРµ СѓРґР°Р»РѕСЃСЊ СЃРѕС…СЂР°РЅРёС‚СЊ Р·Р°РґР°РЅРёРµ": "РќРµ РІРґР°Р»РѕСЃСЏ Р·Р±РµСЂРµРіС‚Рё Р·Р°РІРґР°РЅРЅСЏ", "РќРµ СѓРґР°Р»РѕСЃСЊ Р·Р°РіСЂСѓР·РёС‚СЊ РєР»РёРµРЅС‚РѕРІ": "РќРµ РІРґР°Р»РѕСЃСЏ Р·Р°РІР°РЅС‚Р°Р¶РёС‚Рё РєР»С–С”РЅС‚С–РІ",
      "РќРµ СѓРґР°Р»РѕСЃСЊ СЃРѕС…СЂР°РЅРёС‚СЊ РєР»РёРµРЅС‚Р°": "РќРµ РІРґР°Р»РѕСЃСЏ Р·Р±РµСЂРµРіС‚Рё РєР»С–С”РЅС‚Р°", "РќРµ СѓРґР°Р»РѕСЃСЊ СѓРґР°Р»РёС‚СЊ РєР»РёРµРЅС‚Р°": "РќРµ РІРґР°Р»РѕСЃСЏ РІРёРґР°Р»РёС‚Рё РєР»С–С”РЅС‚Р°",
      "РќРµ СѓРґР°Р»РѕСЃСЊ РґРѕР±Р°РІРёС‚СЊ РєР»РёРµРЅС‚Р°": "РќРµ РІРґР°Р»РѕСЃСЏ РґРѕРґР°С‚Рё РєР»С–С”РЅС‚Р°", "РќСѓР¶РЅРѕ Р·Р°РЅРѕРІРѕ РІРІРµСЃС‚Рё РїР°СЂРѕР»СЊ Р°РґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂР°": "РџРѕС‚СЂС–Р±РЅРѕ Р·РЅРѕРІСѓ РІРІРµСЃС‚Рё РїР°СЂРѕР»СЊ Р°РґРјС–РЅС–СЃС‚СЂР°С‚РѕСЂР°",
      "Р—Р°РїРѕР»РЅРёС‚Рµ РёРјСЏ РєР»РёРµРЅС‚Р° Рё Р»РѕРіРёРЅ": "Р—Р°РїРѕРІРЅС–С‚СЊ С–Рј'СЏ РєР»С–С”РЅС‚Р° С– Р»РѕРіС–РЅ", "РџР°СЂРѕР»СЊ РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ РЅРµ РєРѕСЂРѕС‡Рµ 4 СЃРёРјРІРѕР»РѕРІ": "РџР°СЂРѕР»СЊ РјР°С” Р±СѓС‚Рё РЅРµ РєРѕСЂРѕС‚С€РёРј Р·Р° 4 СЃРёРјРІРѕР»Рё",
      "РўР°РєРѕР№ Р»РѕРіРёРЅ СѓР¶Рµ РёСЃРїРѕР»СЊР·СѓРµС‚СЃСЏ": "РўР°РєРёР№ Р»РѕРіС–РЅ РІР¶Рµ РІРёРєРѕСЂРёСЃС‚РѕРІСѓС”С‚СЊСЃСЏ", "РљР»РёРµРЅС‚ РЅРµ РЅР°Р№РґРµРЅ": "РљР»С–С”РЅС‚Р° РЅРµ Р·РЅР°Р№РґРµРЅРѕ",
      "РЈРґР°Р»РёС‚СЊ СЂР°СЃС‡РµС‚": "Р’РёРґР°Р»РёС‚Рё СЂРѕР·СЂР°С…СѓРЅРѕРє", "РќРµ СѓРґР°Р»РѕСЃСЊ Р·Р°РіСЂСѓР·РёС‚СЊ СЂР°СЃС‡РµС‚С‹": "РќРµ РІРґР°Р»РѕСЃСЏ Р·Р°РІР°РЅС‚Р°Р¶РёС‚Рё СЂРѕР·СЂР°С…СѓРЅРєРё",
      "РќРµ СѓРґР°Р»РѕСЃСЊ Р·Р°РіСЂСѓР·РёС‚СЊ СЂР°СЃС‡РµС‚С‹ РєР»РёРµРЅС‚РѕРІ": "РќРµ РІРґР°Р»РѕСЃСЏ Р·Р°РІР°РЅС‚Р°Р¶РёС‚Рё СЂРѕР·СЂР°С…СѓРЅРєРё РєР»С–С”РЅС‚С–РІ", "РќРµ СѓРґР°Р»РѕСЃСЊ СѓРґР°Р»РёС‚СЊ СЂР°СЃС‡РµС‚": "РќРµ РІРґР°Р»РѕСЃСЏ РІРёРґР°Р»РёС‚Рё СЂРѕР·СЂР°С…СѓРЅРѕРє",
      "РќРµ СѓРґР°Р»РѕСЃСЊ РѕРїР»Р°С‚РёС‚СЊ": "РќРµ РІРґР°Р»РѕСЃСЏ РѕРїР»Р°С‚РёС‚Рё", "РќРµ СѓРґР°Р»РѕСЃСЊ РѕС‚РјРµС‚РёС‚СЊ СЂР°СЃС‡РµС‚ РєР°Рє СЂР°СЃСЃС‡РёС‚Р°РЅРЅС‹Р№": "РќРµ РІРґР°Р»РѕСЃСЏ РїРѕР·РЅР°С‡РёС‚Рё СЂРѕР·СЂР°С…СѓРЅРѕРє СЏРє РѕРїР»Р°С‡РµРЅРёР№"
    },
    pl: {
      "Р—Р°РґР°РЅРёСЏ": "Zadania", "РЎРѕС‚СЂСѓРґРЅРёРєРё": "Pracownicy", "РљР»РёРµРЅС‚С‹": "Klienci", "Р’С‹РїРѕР»РЅРµРЅРЅС‹Рµ Р·Р°РґР°РЅРёСЏ": "Wykonane zadania",
      "Р Р°СЃС‡РµС‚С‹ СЃРѕС‚СЂСѓРґРЅРёРєРѕРІ": "Rozliczenia pracownikГіw", "Р Р°СЃС‡РµС‚С‹ РєР»РёРµРЅС‚РѕРІ": "Rozliczenia klientГіw", "РќР°СЃС‚СЂРѕР№РєРё": "Ustawienia",
      "РќР°Р·РІР°РЅРёРµ Р·Р°РґР°РЅРёСЏ": "Nazwa zadania", "РќР°Р·РІР°РЅРёРµ": "Nazwa", "Р—Р°РґР°РЅРёРµ": "Zadanie", "РћРїРёСЃР°РЅРёРµ": "Opis",
      "РќРѕРјРµСЂ С‚РµР»РµС„РѕРЅР°": "Numer telefonu", "РќРѕРјРµСЂ": "Numer", "РўРµР»РµС„РѕРЅР°": "Telefonu", "РўРµР»РµС„РѕРЅ": "Telefon",
      "РђРґСЂРµСЃ": "Adres", "Р“РѕСЂРѕРґ": "Miasto", "РљРѕРґ": "Kod", "РЈР»РёС†Р°": "Ulica", "Р”РѕРј": "Dom", "РљРІР°СЂС‚РёСЂР°": "Mieszkanie", "Р¦РµРЅР°": "Cena", "РљР°СЂС‚Р°": "Karta", "РќР°Р»РёС‡РЅС‹Рµ": "GotГіwka", "РћРїР»Р°С‚Р°": "PЕ‚atnoЕ›Д‡",
      "Р”РѕР±Р°РІРёС‚СЊ": "Dodaj", "Р РµРґР°РєС‚РёСЂРѕРІР°С‚СЊ": "Edytuj", "РЎРѕС…СЂР°РЅРёС‚СЊ": "Zapisz", "РћС‚РјРµРЅР°": "Anuluj", "РЈРґР°Р»РёС‚СЊ": "UsuЕ„",
      "РќР°С‡Р°С‚СЊ Р·Р°РЅРѕРІРѕ": "Zacznij od nowa", "РћР±РЅРѕРІРёС‚СЊ СЃРїРёСЃРѕРє": "OdЕ›wieЕј listД™", "РРјСЏ СЃРѕС‚СЂСѓРґРЅРёРєР°": "ImiД™ pracownika",
      "РРјСЏ РєР»РёРµРЅС‚Р°": "ImiД™ klienta", "Р›РѕРіРёРЅ": "Login", "РџР°СЂРѕР»СЊ": "HasЕ‚o", "РќРѕРІС‹Р№ РїР°СЂРѕР»СЊ, РµСЃР»Рё РЅСѓР¶РЅРѕ": "Nowe hasЕ‚o, jeЕ›li potrzebne",
      "РЈРґР°Р»РёС‚СЊ СЃРѕС‚СЂСѓРґРЅРёРєР°": "UsuЕ„ pracownika", "РЈРґР°Р»РёС‚СЊ РєР»РёРµРЅС‚Р°": "UsuЕ„ klienta", "РћС‚С‡РµС‚": "Raport", "Р Р°СЃСЃС‡РёС‚Р°С‚СЊ": "Rozlicz",
      "РћРїР»Р°С‚РёС‚СЊ": "OpЕ‚aciД‡", "РћРїР»Р°С‡РµРЅРѕ": "OpЕ‚acono", "РќРµ РѕРїР»Р°С‡РµРЅРѕ": "Nie opЕ‚acono", "Р Р°СЃСЃС‡РёС‚Р°РЅРѕ": "OpЕ‚acono", "РќРµ СЂР°СЃСЃС‡РёС‚Р°РЅРѕ": "Nie opЕ‚acono", "РЎС‚Р°С‚СѓСЃ СЂР°СЃС‡РµС‚Р°": "Status rozliczenia",
      "РЎС‚Р°С‚СѓСЃ": "Status", "РќРѕРІРѕРµ": "Nowe", "РџСЂРёРЅСЏС‚Рѕ": "PrzyjД™te", "РћС‚РєР»РѕРЅРµРЅРѕ": "Odrzucone", "Р’С‹РїРѕР»РЅРµРЅРѕ": "Wykonane",
      "РћС‚РєР°Р·Р°Р»СЃСЏ": "OdmГіwiЕ‚", "Р’ СЂР°Р±РѕС‚Рµ": "W trakcie", "Р’СЃРµРіРѕ РЅР°Р·РЅР°С‡РµРЅРѕ": "ЕЃД…cznie przypisane", "РђРєС‚РёРІРЅС‹Рµ": "Aktywne",
      "РќРѕРІС‹Рµ": "Nowe", "Р’СЃРµРіРѕ Р·Р°РґР°РЅРёР№": "ЕЃД…cznie zadaЕ„", "Р—Р°РґР°РЅРёР№": "ZadaЕ„", "РўРµРєСѓС‰РёР№ СЂР°СЃС‡РµС‚": "BieЕјД…ce rozliczenie",
      "РСЃС‚РѕСЂРёСЏ СЂР°СЃС‡РµС‚РѕРІ": "Historia rozliczeЕ„", "Р Р°СЃС‡РµС‚": "Rozliczenie", "РЎСѓРјРјР° СЂР°СЃС‡РµС‚Р°": "Suma rozliczenia",
      "РЎСѓРјРјР° Рє РѕРїР»Р°С‚Рµ": "Kwota do zapЕ‚aty", "РЎСѓРјРјР° Рє РІС‹РїР»Р°С‚Рµ": "Kwota do wypЕ‚aty", "РЎСѓРјРјР° РІС‹РїРѕР»РЅРµРЅРЅС‹С…": "Suma wykonanych", "РЎСѓРјРјР° РѕС‚РєР°Р·Р°РЅРЅС‹С…": "Suma odmГіw",
      "РћР±С‰Р°СЏ СЃСѓРјРјР° Р·Р°РґР°РЅРёР№": "ЕЃД…czna kwota zadaЕ„", "Р’С‹РїРѕР»РЅРµРЅРЅС‹Рµ СЂР°Р±РѕС‚С‹": "Wykonane prace", "РћС‚РєР°Р·Р°РЅРЅС‹Рµ СЂР°Р±РѕС‚С‹": "OdmГіwione prace",
      "Р Р°Р±РѕС‚С‹ СЃ РѕС‚РєР°Р·РѕРј": "Prace z odmowД…", "РћСЃС‚Р°Р»СЊРЅС‹Рµ СЂР°Р±РѕС‚С‹": "PozostaЕ‚e prace", "РђРєС‚РёРІРЅС‹Рµ СЂР°Р±РѕС‚С‹": "Aktywne prace",
      "РџРѕР»РЅС‹Р№ РѕС‚С‡РµС‚ РїРѕ СЂР°СЃС‡РµС‚Сѓ": "PeЕ‚ny raport rozliczenia", "РџРѕРґСЂРѕР±РЅС‹Р№ РѕС‚С‡РµС‚": "SzczegГіЕ‚owy raport", "РќРµС‚ Р·Р°РїРёСЃРµР№": "Brak wpisГіw",
      "РЎРѕР·РґР°РЅРЅС‹С… СЂР°СЃС‡РµС‚РѕРІ РїРѕРєР° РЅРµС‚": "Nie ma jeszcze utworzonych rozliczeЕ„", "РќРѕРІС‹Р№ СЂР°СЃС‡РµС‚ СЃРѕР·РґР°РµС‚СЃСЏ РІ СЂР°Р·РґРµР»Рµ": "Nowe rozliczenie tworzy siД™ w sekcji",
      "РєРЅРѕРїРєРѕР№": "przyciskiem", "РџР°СЂРѕР»СЊ РїРѕРґС‚РІРµСЂР¶РґРµРЅРёСЏ": "HasЕ‚o potwierdzenia", "РЎСѓРјРјР°": "Kwota",
      "Р РµР·РµСЂРІ": "Rezerwa", "РџРѕРїРѕР»РЅРёС‚СЊ СЂРµР·РµСЂРІ": "DoЕ‚aduj rezerwД™", "РџРѕРїРѕР»РЅРёС‚СЊ": "DoЕ‚aduj", "РћРїРµСЂР°С†РёРё СЂРµР·РµСЂРІР°": "Operacje rezerwy",
      "РР· СЃСѓРјРјС‹ Рє РІС‹РїР»Р°С‚Рµ РІ СЂРµР·РµСЂРІ": "Z kwoty wypЕ‚aty do rezerwy", "РР· СЂРµР·РµСЂРІР° РІ РІС‹РїР»Р°С‚Сѓ": "Z rezerwy do wypЕ‚aty",
      "РџРѕРїРѕР»РЅРµРЅРёРµ СЂРµР·РµСЂРІР°": "DoЕ‚adowanie rezerwy", "РЈРґРµСЂР¶Р°РЅРёРµ Р·Р° РѕС‚РєР°Р· РёР· СЂРµР·РµСЂРІР°": "PotrД…cenie za odmowД™ z rezerwy",
      "РЈРґРµСЂР¶Р°РЅРёРµ Р·Р° РЅР°Р»РёС‡РЅС‹Рµ РёР· СЂРµР·РµСЂРІР°": "PotrД…cenie za gotГіwkД™ z rezerwy", "РЈРґРµСЂР¶Р°РЅРёРµ": "PotrД…cenie",
      "РџСЂРѕС†РµРЅС‚ СЃ РІС‹РїРѕР»РЅРµРЅРЅС‹С… СЂР°Р±РѕС‚, РєРѕС‚РѕСЂС‹Р№ РјС‹ СѓРґРµСЂР¶РёРІР°РµРј СЃРµР±Рµ": "Procent z wykonanych prac, ktГіry zatrzymujemy",
      "РџСЂРѕС†РµРЅС‚ СЃ РѕС‚РєР°Р·Р°РЅРЅС‹С… РёР»Рё РѕС‚РјРµРЅРµРЅРЅС‹С… СЂР°Р±РѕС‚, РєРѕС‚РѕСЂС‹Р№ РјС‹ СѓРґРµСЂР¶РёРІР°РµРј СЃРµР±Рµ": "Procent z odmГіwionych lub anulowanych prac, ktГіry zatrzymujemy",
      "Р’Р°Р»СЋС‚Р°": "Waluta", "Р•РґРёРЅРёС†Р° СЂРµР·РµСЂРІР°": "Jednostka rezerwy", "РџРѕРєР°Р·С‹РІР°С‚СЊ С†РµРЅС‹ Рё СЃСѓРјРјС‹": "PokazywaД‡ ceny i kwoty", "РЎРєРѕР»СЊРєРѕ РґРЅРµР№ С…СЂР°РЅРёС‚СЊ РІС‹РїРѕР»РЅРµРЅРЅРѕРµ Р·Р°РґР°РЅРёРµ": "Ile dni przechowywaД‡ wykonane zadanie", "РЎРєРѕР»СЊРєРѕ РґРЅРµР№ С…СЂР°РЅРёС‚СЊ РЅРµРїСЂРёРЅСЏС‚С‹Рµ Р·Р°РґР°РЅРёСЏ": "Ile dni przechowywaД‡ nieprzyjД™te zadania", "РЎРєРѕР»СЊРєРѕ РґРЅРµР№ С…СЂР°РЅРёС‚СЊ СЂР°СЃС‡РµС‚С‹ СЃРѕС‚СЂСѓРґРЅРёРєРѕРІ": "Ile dni przechowywaД‡ rozliczenia pracownikГіw", "РЎРєРѕР»СЊРєРѕ РґРЅРµР№ С…СЂР°РЅРёС‚СЊ СЂР°СЃС‡РµС‚С‹ РєР»РёРµРЅС‚РѕРІ": "Ile dni przechowywaД‡ rozliczenia klientГіw", "РўРµР»РµС„РѕРЅ РѕР±СЂР°С‚РЅРѕР№ СЃРІСЏР·Рё": "Telefon kontaktowy",
      "E-mail РѕР±СЂР°С‚РЅРѕР№ СЃРІСЏР·Рё": "E-mail kontaktowy", "РћР±С‹С‡РЅС‹Р№ Р°РґСЂРµСЃ": "ZwykЕ‚y adres", "Telegram": "Telegram", "WhatsApp": "WhatsApp",
      "РР·РјРµРЅРёС‚СЊ РїР°СЂРѕР»СЊ": "ZmieЕ„ hasЕ‚o", "РЎС‚Р°СЂС‹Р№ РїР°СЂРѕР»СЊ": "Stare hasЕ‚o", "РџРѕРІС‚РѕСЂРёС‚Рµ СЃС‚Р°СЂС‹Р№ РїР°СЂРѕР»СЊ": "PowtГіrz stare hasЕ‚o",
      "Р’РІРµРґРёС‚Рµ РЅРѕРІС‹Р№ РїР°СЂРѕР»СЊ": "Wpisz nowe hasЕ‚o", "РЎР±СЂРѕСЃРёС‚СЊ РїР°СЂРѕР»СЊ": "Resetuj hasЕ‚o", "РќР°СЃС‚СЂРѕР№РєРё СЃРѕС…СЂР°РЅРµРЅС‹": "Ustawienia zapisane",
      "РќРµ СѓРґР°Р»РѕСЃСЊ Р·Р°РіСЂСѓР·РёС‚СЊ РЅР°СЃС‚СЂРѕР№РєРё": "Nie udaЕ‚o siД™ zaЕ‚adowaД‡ ustawieЕ„", "РќРµ СѓРґР°Р»РѕСЃСЊ СЃРѕС…СЂР°РЅРёС‚СЊ РЅР°СЃС‚СЂРѕР№РєРё": "Nie udaЕ‚o siД™ zapisaД‡ ustawieЕ„",
      "РџР°СЂРѕР»СЊ РёР·РјРµРЅРµРЅ": "HasЕ‚o zmienione", "РџР°СЂРѕР»СЊ СЃР±СЂРѕС€РµРЅ": "HasЕ‚o zresetowane", "РЎРѕР·РґР°РЅРѕ": "Utworzono", "РР·РјРµРЅРµРЅРѕ": "Zmieniono",
      "РСЃС‚РѕС‡РЅРёРє": "Е№rГіdЕ‚o", "Р”РёСЃРїРµС‚С‡РµСЂ": "Dyspozytor", "РљР»РёРµРЅС‚": "Klient", "СЃРѕС‚СЂСѓРґРЅРёРє": "pracownik",
      "РЈРґР°Р»РёС‚СЊ СЌС‚Рѕ Р·Р°РґР°РЅРёРµ": "UsunД…Д‡ to zadanie", "РќРµ СѓРґР°Р»РѕСЃСЊ РІРµСЂРЅСѓС‚СЊ Р·Р°РґР°РЅРёРµ": "Nie udaЕ‚o siД™ przywrГіciД‡ zadania", "РќРµ СѓРґР°Р»РѕСЃСЊ СѓРґР°Р»РёС‚СЊ Р·Р°РґР°РЅРёРµ": "Nie udaЕ‚o siД™ usunД…Д‡ zadania",
      "РќРµ СѓРґР°Р»РѕСЃСЊ СЃРѕС…СЂР°РЅРёС‚СЊ Р·Р°РґР°РЅРёРµ": "Nie udaЕ‚o siД™ zapisaД‡ zadania", "РќРµ СѓРґР°Р»РѕСЃСЊ Р·Р°РіСЂСѓР·РёС‚СЊ РєР»РёРµРЅС‚РѕРІ": "Nie udaЕ‚o siД™ zaЕ‚adowaД‡ klientГіw",
      "РќРµ СѓРґР°Р»РѕСЃСЊ СЃРѕС…СЂР°РЅРёС‚СЊ РєР»РёРµРЅС‚Р°": "Nie udaЕ‚o siД™ zapisaД‡ klienta", "РќРµ СѓРґР°Р»РѕСЃСЊ СѓРґР°Р»РёС‚СЊ РєР»РёРµРЅС‚Р°": "Nie udaЕ‚o siД™ usunД…Д‡ klienta",
      "РќРµ СѓРґР°Р»РѕСЃСЊ РґРѕР±Р°РІРёС‚СЊ РєР»РёРµРЅС‚Р°": "Nie udaЕ‚o siД™ dodaД‡ klienta", "РќСѓР¶РЅРѕ Р·Р°РЅРѕРІРѕ РІРІРµСЃС‚Рё РїР°СЂРѕР»СЊ Р°РґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂР°": "Wpisz ponownie hasЕ‚o administratora",
      "Р—Р°РїРѕР»РЅРёС‚Рµ РёРјСЏ РєР»РёРµРЅС‚Р° Рё Р»РѕРіРёРЅ": "WypeЕ‚nij imiД™ klienta i login", "РџР°СЂРѕР»СЊ РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ РЅРµ РєРѕСЂРѕС‡Рµ 4 СЃРёРјРІРѕР»РѕРІ": "HasЕ‚o musi mieД‡ co najmniej 4 znaki",
      "РўР°РєРѕР№ Р»РѕРіРёРЅ СѓР¶Рµ РёСЃРїРѕР»СЊР·СѓРµС‚СЃСЏ": "Ten login jest juЕј uЕјywany", "РљР»РёРµРЅС‚ РЅРµ РЅР°Р№РґРµРЅ": "Klient nie znaleziony",
      "РЈРґР°Р»РёС‚СЊ СЂР°СЃС‡РµС‚": "UsuЕ„ rozliczenie", "РќРµ СѓРґР°Р»РѕСЃСЊ Р·Р°РіСЂСѓР·РёС‚СЊ СЂР°СЃС‡РµС‚С‹": "Nie udaЕ‚o siД™ zaЕ‚adowaД‡ rozliczeЕ„",
      "РќРµ СѓРґР°Р»РѕСЃСЊ Р·Р°РіСЂСѓР·РёС‚СЊ СЂР°СЃС‡РµС‚С‹ РєР»РёРµРЅС‚РѕРІ": "Nie udaЕ‚o siД™ zaЕ‚adowaД‡ rozliczeЕ„ klientГіw", "РќРµ СѓРґР°Р»РѕСЃСЊ СѓРґР°Р»РёС‚СЊ СЂР°СЃС‡РµС‚": "Nie udaЕ‚o siД™ usunД…Д‡ rozliczenia",
      "РќРµ СѓРґР°Р»РѕСЃСЊ РѕРїР»Р°С‚РёС‚СЊ": "Nie udaЕ‚o siД™ opЕ‚aciД‡", "РќРµ СѓРґР°Р»РѕСЃСЊ РѕС‚РјРµС‚РёС‚СЊ СЂР°СЃС‡РµС‚ РєР°Рє СЂР°СЃСЃС‡РёС‚Р°РЅРЅС‹Р№": "Nie udaЕ‚o siД™ oznaczyД‡ rozliczenia jako opЕ‚acone"
    },
    ru: {}
  };
  dict.en["Р Р°СЃСЃС‡РёС‚Р°С‚СЊ РІСЃРµС…"] = "Calculate all";
  dict.uk["Р Р°СЃСЃС‡РёС‚Р°С‚СЊ РІСЃРµС…"] = "Р РѕР·СЂР°С…СѓРІР°С‚Рё РІСЃС–С…";
  dict.pl["Р Р°СЃСЃС‡РёС‚Р°С‚СЊ РІСЃРµС…"] = "Rozlicz wszystkich";
  Object.assign(dict.en, {
    "Реклама Telegram": "Telegram Ads", "Реклама Facebook": "Facebook Ads", "Вход Telegram userbot": "Telegram userbot login",
    "Группы для рекламы, группы для поиска объявлений, материалы, расписание и журнал.": "Ad groups, search groups, materials, schedule and log.",
    "Группы, материалы, расписание и журнал. Проверку Page Access Token сделаем через Meta.": "Groups, materials, schedule and log. Page Access Token is checked through Meta.",
    "Telegram-группы для рекламы": "Telegram groups for ads", "Ваши группы, куда бот отправляет рекламные материалы по расписанию.": "Your groups where the bot sends ad materials on schedule.",
    "Название группы": "Group name", "Комментарий": "Comment", "Сохранить группу": "Save group", "Пока нет групп для рекламы": "No ad groups yet",
    "Telegram-группы для поиска объявлений": "Telegram groups for finding requests", "Бот ищет ключевые слова и отвечает в этой же группе или пересылает в выбранную вашу группу.": "The bot searches keywords and replies in the same group or forwards to one of your groups.",
    "Группа поиска": "Search group", "Действие при совпадении": "Action on match", "Материал для комментария": "Comment material", "Ключевые слова": "Keywords",
    "Дополнительная заметка": "Additional note", "Использовать эту группу также для рекламы": "Use this group for ads too", "Сохранить поиск": "Save search", "Пока нет групп для поиска": "No search groups yet",
    "аренда, купить, срочно": "rent, buy, urgent", "Не пересылать, ответить в этой же группе": "Do not forward, reply in this group", "Переслать в": "Forward to",
    "Рекламный материал": "Ad material", "Отдельный рекламный текст и картинка с компьютера.": "Separate ad text and an image from computer.",
    "Название материала": "Material name", "Аудитория": "Audience", "Все": "All", "Мастера": "Workers", "Текст рекламы": "Ad text",
    "Ссылка на картинку": "Image link", "Картинка с компьютера": "Image from computer", "Материал включен": "Material enabled",
    "Сохранить рекламный материал": "Save ad material", "Картинка не выбрана": "No image selected", "Пока нет рекламных материалов": "No ad materials yet",
    "Расписание рекламы": "Ad schedule", "Выберите группу, материал и время публикации.": "Choose a group, material and publishing time.", "Выберите Facebook-группу, материал и время публикации.": "Choose a Facebook group, material and publishing time.",
    "Группа": "Group", "Материал": "Material", "Время": "Time", "Расписание включено": "Schedule enabled", "Сохранить расписание": "Save schedule", "Пока нет расписаний": "No schedules yet",
    "Найденные объявления": "Found requests", "Последние совпадения по ключевым словам.": "Latest keyword matches.", "Совпадений пока нет": "No matches yet",
    "Журнал Telegram": "Telegram log", "Журнал Facebook": "Facebook log", "Последние действия.": "Latest actions.", "Очистка журнала": "Clear log", "Журнал очищен": "Log is empty",
    "Отправить сейчас": "Send now", "Ручная отправка выбранного рекламного материала.": "Manual sending of the selected ad material.", "Отправлено": "Sent",
    "Facebook-группы": "Facebook groups", "Группы или страницы для планирования рекламы и поиска по ключевым словам.": "Groups or pages for ad planning and keyword search.",
    "ID или ссылка": "ID or link", "Только записать в журнал": "Only write to log", "Группа включена": "Group enabled", "Пока нет Facebook-групп": "No Facebook groups yet",
    "Материал не выбран": "Material not selected", "последняя отправка": "last sent", "нет": "none", "цель": "target"
  });
  Object.assign(dict.uk, {
    "Реклама Telegram": "Реклама Telegram", "Реклама Facebook": "Реклама Facebook", "Вход Telegram userbot": "Вхід Telegram userbot",
    "Группы для рекламы, группы для поиска объявлений, материалы, расписание и журнал.": "Групи для реклами, групи для пошуку оголошень, матеріали, розклад і журнал.",
    "Группы, материалы, расписание и журнал. Проверку Page Access Token сделаем через Meta.": "Групи, матеріали, розклад і журнал. Page Access Token перевіряється через Meta.",
    "Telegram-группы для рекламы": "Telegram-групи для реклами", "Ваши группы, куда бот отправляет рекламные материалы по расписанию.": "Ваші групи, куди бот надсилає рекламні матеріали за розкладом.",
    "Название группы": "Назва групи", "Комментарий": "Коментар", "Сохранить группу": "Зберегти групу", "Пока нет групп для рекламы": "Поки немає груп для реклами",
    "Telegram-группы для поиска объявлений": "Telegram-групи для пошуку оголошень", "Группа поиска": "Група пошуку", "Действие при совпадении": "Дія при збігу", "Материал для комментария": "Матеріал для коментаря",
    "Ключевые слова": "Ключові слова", "Дополнительная заметка": "Додаткова нотатка", "Использовать эту группу также для рекламы": "Використовувати цю групу також для реклами",
    "Сохранить поиск": "Зберегти пошук", "Пока нет групп для поиска": "Поки немає груп для пошуку", "аренда, купить, срочно": "оренда, купити, терміново",
    "Не пересылать, ответить в этой же группе": "Не пересилати, відповісти в цій самій групі", "Переслать в": "Переслати в",
    "Рекламный материал": "Рекламний матеріал", "Отдельный рекламный текст и картинка с компьютера.": "Окремий рекламний текст і картинка з комп'ютера.",
    "Название материала": "Назва матеріалу", "Аудитория": "Аудиторія", "Все": "Усі", "Мастера": "Майстри", "Текст рекламы": "Текст реклами",
    "Ссылка на картинку": "Посилання на картинку", "Картинка с компьютера": "Картинка з комп'ютера", "Материал включен": "Матеріал увімкнено",
    "Сохранить рекламный материал": "Зберегти рекламний матеріал", "Картинка не выбрана": "Картинку не вибрано", "Пока нет рекламных материалов": "Поки немає рекламних матеріалів",
    "Расписание рекламы": "Розклад реклами", "Группа": "Група", "Материал": "Матеріал", "Время": "Час", "Расписание включено": "Розклад увімкнено", "Сохранить расписание": "Зберегти розклад", "Пока нет расписаний": "Поки немає розкладів",
    "Найденные объявления": "Знайдені оголошення", "Последние совпадения по ключевым словам.": "Останні збіги за ключовими словами.", "Совпадений пока нет": "Збігів поки немає",
    "Журнал Telegram": "Журнал Telegram", "Журнал Facebook": "Журнал Facebook", "Последние действия.": "Останні дії.", "Очистка журнала": "Очистити журнал", "Журнал очищен": "Журнал порожній",
    "Отправить сейчас": "Надіслати зараз", "Ручная отправка выбранного рекламного материала.": "Ручне надсилання вибраного рекламного матеріалу.", "Отправлено": "Надіслано",
    "Facebook-группы": "Facebook-групи", "ID или ссылка": "ID або посилання", "Только записать в журнал": "Тільки записати в журнал", "Группа включена": "Групу увімкнено", "Пока нет Facebook-групп": "Поки немає Facebook-груп",
    "Материал не выбран": "Матеріал не вибрано", "последняя отправка": "остання відправка", "нет": "немає", "цель": "ціль"
  });
  Object.assign(dict.pl, {
    "Реклама Telegram": "Reklama Telegram", "Реклама Facebook": "Reklama Facebook", "Вход Telegram userbot": "Logowanie Telegram userbot",
    "Группы для рекламы, группы для поиска объявлений, материалы, расписание и журнал.": "Grupy reklamowe, grupy do szukania ogłoszeń, materiały, harmonogram i dziennik.",
    "Группы, материалы, расписание и журнал. Проверку Page Access Token сделаем через Meta.": "Grupy, materiały, harmonogram i dziennik. Page Access Token sprawdzamy przez Meta.",
    "Telegram-группы для рекламы": "Grupy Telegram do reklamy", "Ваши группы, куда бот отправляет рекламные материалы по расписанию.": "Twoje grupy, do których bot wysyła materiały reklamowe według harmonogramu.",
    "Название группы": "Nazwa grupy", "Комментарий": "Komentarz", "Сохранить группу": "Zapisz grupę", "Пока нет групп для рекламы": "Nie ma jeszcze grup do reklamy",
    "Telegram-группы для поиска объявлений": "Grupy Telegram do szukania ogłoszeń", "Группа поиска": "Grupa wyszukiwania", "Действие при совпадении": "Działanie przy dopasowaniu", "Материал для комментария": "Materiał do komentarza",
    "Ключевые слова": "Słowa kluczowe", "Дополнительная заметка": "Dodatkowa notatka", "Использовать эту группу также для рекламы": "Używaj tej grupy także do reklamy",
    "Сохранить поиск": "Zapisz wyszukiwanie", "Пока нет групп для поиска": "Nie ma jeszcze grup do wyszukiwania", "аренда, купить, срочно": "wynajem, kupić, pilne",
    "Не пересылать, ответить в этой же группе": "Nie przekazywać, odpowiedzieć w tej samej grupie", "Переслать в": "Przekaż do",
    "Рекламный материал": "Materiał reklamowy", "Отдельный рекламный текст и картинка с компьютера.": "Osobny tekst reklamowy i obraz z komputera.",
    "Название материала": "Nazwa materiału", "Аудитория": "Odbiorcy", "Все": "Wszyscy", "Мастера": "Wykonawcy", "Текст рекламы": "Tekst reklamy",
    "Ссылка на картинку": "Link do obrazu", "Картинка с компьютера": "Obraz z komputera", "Материал включен": "Materiał włączony",
    "Сохранить рекламный материал": "Zapisz materiał reklamowy", "Картинка не выбрана": "Nie wybrano obrazu", "Пока нет рекламных материалов": "Nie ma jeszcze materiałów reklamowych",
    "Расписание рекламы": "Harmonogram reklamy", "Группа": "Grupa", "Материал": "Materiał", "Время": "Czas", "Расписание включено": "Harmonogram włączony", "Сохранить расписание": "Zapisz harmonogram", "Пока нет расписаний": "Nie ma jeszcze harmonogramów",
    "Найденные объявления": "Znalezione ogłoszenia", "Последние совпадения по ключевым словам.": "Ostatnie dopasowania słów kluczowych.", "Совпадений пока нет": "Nie ma jeszcze dopasowań",
    "Журнал Telegram": "Dziennik Telegram", "Журнал Facebook": "Dziennik Facebook", "Последние действия.": "Ostatnie działania.", "Очистка журнала": "Wyczyść dziennik", "Журнал очищен": "Dziennik jest pusty",
    "Отправить сейчас": "Wyślij teraz", "Ручная отправка выбранного рекламного материала.": "Ręczna wysyłka wybranego materiału reklamowego.", "Отправлено": "Wysłano",
    "Facebook-группы": "Grupy Facebook", "ID или ссылка": "ID lub link", "Только записать в журнал": "Tylko zapisz w dzienniku", "Группа включена": "Grupa włączona", "Пока нет Facebook-групп": "Nie ma jeszcze grup Facebook",
    "Материал не выбран": "Nie wybrano materiału", "последняя отправка": "ostatnia wysyłka", "нет": "brak", "цель": "cel"
  });
  const keysByLength = {};
  Object.keys(dict).forEach(lang => {
    keysByLength[lang] = Object.keys(dict[lang]).sort((a, b) => b.length - a.length);
  });
  function currentLanguage() {
    const stored = localStorage.getItem("language") || "ru";
    return dict[stored] ? stored : "ru";
  }
  function translate(value, lang) {
    if (!value || lang === "ru") return value;
    let result = value;
    keysByLength[lang].forEach(key => {
      result = result.split(key).join(dict[lang][key]);
    });
    return result;
  }
  function translateElementValue(el, attr, lang) {
    const value = el.getAttribute(attr);
    if (!value) return;
    const store = "serverRu" + attr.charAt(0).toUpperCase() + attr.slice(1);
    if (!el.dataset[store]) el.dataset[store] = value;
    const translated = translate(el.dataset[store], lang);
    if (el.getAttribute(attr) !== translated) el.setAttribute(attr, translated);
  }
  function translateNode(node, lang) {
    if (!node || node.nodeType !== Node.TEXT_NODE) return;
    const parent = node.parentElement;
    if (!parent || ["SCRIPT", "STYLE", "TEXTAREA"].includes(parent.tagName)) return;
    if (parent.closest(".language-corner")) return;
    if (parent.closest("[data-no-translate], .no-translate")) return;
    if (!node.serverRuText) node.serverRuText = node.nodeValue;
    const translated = translate(node.serverRuText, lang);
    if (node.nodeValue !== translated) node.nodeValue = translated;
  }
  function walk(root, lang) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    nodes.forEach(node => translateNode(node, lang));
    root.querySelectorAll("input[placeholder], textarea[placeholder], button[value], input[value]").forEach(el => {
      if (el.hasAttribute("placeholder")) translateElementValue(el, "placeholder", lang);
      if ((el.tagName === "BUTTON" || ["button", "submit"].includes((el.type || "").toLowerCase())) && el.hasAttribute("value")) {
        translateElementValue(el, "value", lang);
      }
    });
  }
  function ensureLanguageSelect() {
    const header = document.querySelector("header");
    if (!header) return null;
    let select = document.querySelector("#languageSelect");
    if (!select) {
      const box = document.createElement("div");
      box.className = "language-corner";
      select = document.createElement("select");
      select.id = "languageSelect";
      box.appendChild(select);
      header.insertBefore(box, header.firstChild);
    } else if (!select.closest(".language-corner")) {
      const box = document.createElement("div");
      box.className = "language-corner";
      select.parentNode.insertBefore(box, select);
      box.appendChild(select);
    } else {
      select.closest(".language-corner").classList.add("language-corner");
    }
    select.innerHTML = languages.map(([value, label]) => `<option value="${value}">${label}</option>`).join("");
    select.value = currentLanguage();
    return select;
  }
  function applyServerLanguage() {
    const lang = currentLanguage();
    document.documentElement.lang = lang;
    if (!document.documentElement.dataset.serverRuTitle) {
      document.documentElement.dataset.serverRuTitle = document.title || "";
    }
    document.title = translate(document.documentElement.dataset.serverRuTitle, lang);
    const select = ensureLanguageSelect();
    if (select && select.value !== lang) select.value = lang;
    walk(document.body, lang);
  }
  const originalFetch = window.fetch.bind(window);
  window.fetch = function(resource, options) {
    const lang = currentLanguage();
    const next = options ? { ...options } : {};
    const headers = new Headers(next.headers || {});
    if (!headers.has("X-Language")) headers.set("X-Language", lang);
    next.headers = headers;
    return originalFetch(resource, next);
  };
  window.setServerLanguage = function(value) {
    if (!dict[value]) value = "ru";
    localStorage.setItem("language", value);
    applyServerLanguage();
    if (typeof window.loadTasks === "function") window.loadTasks();
    if (typeof window.loadUsers === "function") window.loadUsers();
    if (typeof window.loadClients === "function") window.loadClients();
    if (typeof window.loadSettlements === "function") window.loadSettlements();
    if (typeof window.loadCalculations === "function") window.loadCalculations();
    if (typeof window.loadSettings === "function") window.loadSettings();
  };
  document.addEventListener("change", event => {
    if (event.target && event.target.id === "languageSelect") {
      window.setServerLanguage(event.target.value);
    }
  }, true);
  document.addEventListener("DOMContentLoaded", () => {
    ensureLanguageSelect();
    applyServerLanguage();
    new MutationObserver(mutations => {
      const lang = currentLanguage();
      mutations.forEach(mutation => {
        mutation.addedNodes.forEach(node => {
          if (node.nodeType === Node.TEXT_NODE) translateNode(node, lang);
          if (node.nodeType === Node.ELEMENT_NODE) walk(node, lang);
        });
        if (mutation.type === "attributes" && mutation.target instanceof Element) {
          if (mutation.attributeName === "placeholder") translateElementValue(mutation.target, "placeholder", lang);
          if (mutation.attributeName === "value") translateElementValue(mutation.target, "value", lang);
        }
      });
    }).observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["placeholder", "value"] });
  });
})();
</script>
"""


def inject_server_language_tools(html):
    if "server-language-script" in html:
        return html
    if "</head>" in html:
        html = html.replace("</head>", SERVER_LANGUAGE_TOOLS + "\n</head>", 1)
    return html


INDEX_HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Р—Р°РґР°РЅРёСЏ</title>
  <style>
    :root { --ink: #172026; --teal: #0f766e; --coral: #f9735b; --gold: #f6c85f; --sky: #d9f3ff; --paper: #fffaf0; }
    body { font-family: Arial, sans-serif; margin: 0; color: var(--ink); background: linear-gradient(135deg, #fff7df 0%, #d9f3ff 46%, #f3e8ff 100%); min-height: 100vh; }
    body.locked header, body.locked main { display: none; }
    header { position: relative; background: linear-gradient(135deg, #0f766e 0%, #2563eb 48%, #7c3aed 100%); color: white; padding: 24px 28px; box-shadow: 0 18px 42px rgba(37, 99, 235, 0.22); }
    main { max-width: 980px; margin: 0 auto; padding: 24px; }
    form { display: grid; gap: 10px; grid-template-columns: repeat(6, minmax(110px, 1fr)); align-items: start; margin-bottom: 24px; }
    input, textarea, select, button { font: inherit; padding: 11px 13px; border-radius: 8px; border: 1px solid rgba(23, 32, 38, 0.18); }
    input, textarea, select { background: rgba(255, 255, 255, 0.92); color: #60717d; min-width: 0; height: 88px; box-sizing: border-box; font-weight: 400; text-align: center; }
    textarea { resize: none; overflow: hidden; line-height: 1.25; padding-top: 27px; }
    input::placeholder, textarea::placeholder { color: #60717d; opacity: 1; font-weight: 400; }
    select { appearance: none; font-weight: 400; text-align-last: center; }
    select, select option { color: #60717d; font-weight: 400; }
    #price, #paymentMethod { width: 100%; }
    #price { text-align: center; }
    .mainTaskRow { display: contents; }
    .addressRow { display: grid; gap: 10px; grid-template-columns: repeat(5, minmax(110px, 1fr)); grid-column: 1 / -1; }
    button { background: linear-gradient(135deg, var(--teal), #2563eb); color: white; border: 0; cursor: pointer; font-weight: 700; box-shadow: 0 10px 24px rgba(15, 118, 110, 0.22); }
    #form .mainTaskRow > button { height: 88px; display: flex; align-items: center; justify-content: center; text-align: center; }
    .secondary { background: #fff0bf; color: #4a3200; box-shadow: none; margin-top: 8px; }
    .restart { background: #16a34a; color: white; box-shadow: 0 10px 24px rgba(22, 163, 74, 0.22); margin-top: 8px; }
    .danger { background: #ef4444; color: white; box-shadow: none; margin-top: 8px; margin-left: 8px; }
    .editTask { display: grid; gap: 10px; grid-template-columns: repeat(6, minmax(110px, 1fr)); margin-top: 12px; }
    .editTask input, .editTask textarea, .editTask select, .editTask button { min-width: 0; width: 100%; box-sizing: border-box; }
    .editTask input, .editTask select { height: 88px; text-align: center; color: var(--ink); }
    .editTask select { text-align-last: left; appearance: auto; }
    .editTask button { height: 88px; margin: 0; }
    .editTaskActions { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; grid-column: 5 / span 2; }
    .editTask .clientSourceSelect { height: 88px; }
    nav { display: grid; grid-template-columns: repeat(10, minmax(104px, 1fr)); gap: 10px; margin-top: 12px; max-width: 1320px; }
    nav a { display: flex; align-items: center; justify-content: center; min-height: 44px; box-sizing: border-box; color: var(--ink); background: var(--gold); font-weight: bold; padding: 8px 10px; border-radius: 8px; text-align: center; line-height: 1.15; text-decoration: none; }
    .language-corner { position: absolute; top: 18px; right: 20px; }
    .language-corner select { font: inherit; padding: 8px 10px; border-radius: 8px; border: 0; background: white; color: var(--ink); font-weight: 700; }
    .task { position: relative; background: rgba(255, 255, 255, 0.94); border: 1px solid rgba(255, 255, 255, 0.72); border-left: 6px solid var(--coral); border-radius: 8px; padding: 16px; margin: 12px 0; box-shadow: 0 14px 34px rgba(23, 32, 38, 0.1); }
    .task h3 { padding-right: 210px; }
    .taskDates { position: absolute; top: 12px; right: 12px; display: grid; gap: 3px; min-width: 178px; max-width: 210px; padding: 7px 9px; border-radius: 8px; background: #eef7ff; color: #33515f; font-size: 12px; line-height: 1.2; text-align: right; box-shadow: inset 0 0 0 1px rgba(37, 99, 235, 0.1); }
    .taskDates span { display: block; white-space: nowrap; }
    .meta { color: #60717d; font-size: 14px; }
    .status { display: inline-block; padding: 5px 10px; border-radius: 999px; background: #fff0bf; color: #6b4300; font-weight: 700; }
    .status.accepted { background: #dcfce7; color: #166534; }
    @media (max-width: 980px) { nav { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
    @media (max-width: 760px) { form, .editTask, .addressRow { grid-template-columns: 1fr; } .task h3 { padding-right: 0; } .taskDates { position: static; margin: 0 0 10px auto; } }
  </style>
</head>
<body class="locked">
  <header>
    <div class="language-corner">
      <select id="languageSelect" onchange="setLanguage(this.value)">
        <option value="en">English</option>
        <option value="uk">Українська</option>
        <option value="ru">Русский</option>
        <option value="pl">Polski</option>
      </select>
    </div>
    <h1 data-i18n="serverTitle">Р—Р°РґР°РЅРёСЏ</h1>
    <nav><a href="/server">Р—Р°РґР°РЅРёСЏ</a> <a href="/users">РЎРѕС‚СЂСѓРґРЅРёРєРё</a> <a href="/clients">РљР»РёРµРЅС‚С‹</a> <a href="/completed">Р’С‹РїРѕР»РЅРµРЅРЅС‹Рµ Р·Р°РґР°РЅРёСЏ</a> <a href="/calculations">Р Р°СЃС‡РµС‚С‹ СЃРѕС‚СЂСѓРґРЅРёРєРѕРІ</a> <a href="/client-calculations">Р Р°СЃС‡РµС‚С‹ РєР»РёРµРЅС‚РѕРІ</a> <a href="/telegram-ads">Р РµРєР»Р°РјР° Telegram</a> <a href="/facebook-ads">Р РµРєР»Р°РјР° Facebook</a> <a href="/telegram-login">Telegram userbot</a> <a href="/settings">РќР°СЃС‚СЂРѕР№РєРё</a></nav>
  </header>
  <main>
    <form id="form">
      <div class="addressRow">
        <input id="city" placeholder="Р“РѕСЂРѕРґ" required>
        <input id="postalCode" placeholder="РљРѕРґ">
        <input id="street" placeholder="РЈР»РёС†Р°" required>
        <input id="house" placeholder="Р”РѕРј">
        <input id="apartment" placeholder="РљРІР°СЂС‚РёСЂР°">
      </div>
      <div class="mainTaskRow">
        <textarea id="title" data-placeholder="taskTitle" placeholder="РќР°Р·РІР°РЅРёРµ&#10;Р—Р°РґР°РЅРёРµ" required></textarea>
        <input id="description" data-placeholder="description" placeholder="РћРїРёСЃР°РЅРёРµ">
        <textarea id="phone" placeholder="РќРѕРјРµСЂ&#10;РўРµР»РµС„РѕРЅР°" inputmode="tel"></textarea>
        <input id="price" data-placeholder="price" placeholder="Р¦РµРЅР°" inputmode="decimal">
        <select id="paymentMethod">
          <option value="cash" data-payment-label="cash">РќР°Р»РёС‡РЅС‹Рµ</option>
          <option value="card" data-payment-label="fromReserve">РР· СЂРµР·РµСЂРІР°</option>
        </select>
        <button data-i18n="add">Р”РѕР±Р°РІРёС‚СЊ</button>
      </div>
    </form>
    <section id="tasks"></section>
  </main>
  <script>
    const tasks = document.querySelector("#tasks");
    let language = localStorage.getItem("language") || "ru";
    let appSettings = { currency: "PLN", reserveUnit: "credits", showPrices: true };
    let clientOptions = [];
    const texts = {
      ru: { serverTitle: "Р—Р°РґР°РЅРёСЏ", openUsers: "РЎРѕС‚СЂСѓРґРЅРёРєРё", completedTasks: "Р’С‹РїРѕР»РЅРµРЅРЅС‹Рµ Р·Р°РґР°РЅРёСЏ", calculations: "Р Р°СЃС‡РµС‚С‹", changePassword: "РР·РјРµРЅРёС‚СЊ РїР°СЂРѕР»СЊ", oldPassword: "РЎС‚Р°СЂС‹Р№ РїР°СЂРѕР»СЊ", oldPasswordRepeat: "РџРѕРІС‚РѕСЂРёС‚Рµ СЃС‚Р°СЂС‹Р№ РїР°СЂРѕР»СЊ", newPassword: "Р’РІРµРґРёС‚Рµ РЅРѕРІС‹Р№ РїР°СЂРѕР»СЊ", passwordChanged: "РџР°СЂРѕР»СЊ РёР·РјРµРЅРµРЅ", changePasswordError: "РќРµ СѓРґР°Р»РѕСЃСЊ РёР·РјРµРЅРёС‚СЊ РїР°СЂРѕР»СЊ: ", confirmPassword: "РџР°СЂРѕР»СЊ РїРѕРґС‚РІРµСЂР¶РґРµРЅРёСЏ", taskTitle: "РќР°Р·РІР°РЅРёРµ Р·Р°РґР°РЅРёСЏ", description: "РћРїРёСЃР°РЅРёРµ", city: "Р“РѕСЂРѕРґ", postalCode: "РљРѕРґ", street: "РЈР»РёС†Р°", house: "Р”РѕРј", apartment: "РљРІР°СЂС‚РёСЂР°", address: "РђРґСЂРµСЃ", price: "Р¦РµРЅР°", add: "Р”РѕР±Р°РІРёС‚СЊ", employee: "СЃРѕС‚СЂСѓРґРЅРёРє", delete: "РЈРґР°Р»РёС‚СЊ", restart: "РќР°С‡Р°С‚СЊ Р·Р°РЅРѕРІРѕ", confirmDelete: "РЈРґР°Р»РёС‚СЊ СЌС‚Рѕ Р·Р°РґР°РЅРёРµ?", resetError: "РќРµ СѓРґР°Р»РѕСЃСЊ РІРµСЂРЅСѓС‚СЊ Р·Р°РґР°РЅРёРµ: ", deleteError: "РќРµ СѓРґР°Р»РѕСЃСЊ СѓРґР°Р»РёС‚СЊ Р·Р°РґР°РЅРёРµ: ", createdAt: "РЎРѕР·РґР°РЅРѕ", acceptedAt: "РџСЂРёРЅСЏС‚Рѕ", completedAt: "Р’С‹РїРѕР»РЅРµРЅРѕ", new: "РќРѕРІРѕРµ", accepted: "РџСЂРёРЅСЏС‚Рѕ", declined: "РћС‚РєР»РѕРЅРµРЅРѕ", completed: "Р’С‹РїРѕР»РЅРµРЅРѕ", refused: "РћС‚РєР°Р·Р°Р»СЃСЏ", payment: "РћРїР»Р°С‚Р°", source: "РСЃС‚РѕС‡РЅРёРє", dispatcher: "Р”РёСЃРїРµС‚С‡РµСЂ", client: "РљР»РёРµРЅС‚", cash: "РќР°Р»РёС‡РЅС‹Рµ", fromReserve: "РР· СЂРµР·РµСЂРІР°", save: "РЎРѕС…СЂР°РЅРёС‚СЊ", cancel: "РћС‚РјРµРЅР°", phone: "РќРѕРјРµСЂ С‚РµР»РµС„РѕРЅР°" },
      en: { serverTitle: "Tasks", openUsers: "Employees", completedTasks: "Completed tasks", calculations: "Calculations", changePassword: "Change password", oldPassword: "Old password", oldPasswordRepeat: "Repeat old password", newPassword: "Enter new password", passwordChanged: "Password changed", changePasswordError: "Could not change password: ", confirmPassword: "Confirmation password", taskTitle: "Task name", description: "Description", city: "City", postalCode: "Postal code", street: "Street", house: "House", apartment: "Apartment", address: "Address", price: "Price", add: "Add", employee: "employee", delete: "Delete", restart: "Start again", confirmDelete: "Delete this task?", resetError: "Could not return task: ", deleteError: "Could not delete task: ", createdAt: "Created", acceptedAt: "Accepted", completedAt: "Completed", new: "New", accepted: "Accepted", declined: "Declined", completed: "Completed", refused: "Refused", payment: "Payment", source: "Source", acceptedBy: "Accepted by", dispatcher: "Dispatcher", client: "Client", cash: "Cash", fromReserve: "From reserve", save: "Save", cancel: "Cancel", phone: "Phone number" },
      uk: { serverTitle: "Р—Р°РІРґР°РЅРЅСЏ", openUsers: "РЎРїС–РІСЂРѕР±С–С‚РЅРёРєРё", completedTasks: "Р’РёРєРѕРЅР°РЅС– Р·Р°РІРґР°РЅРЅСЏ", calculations: "Р РѕР·СЂР°С…СѓРЅРєРё", changePassword: "Р—РјС–РЅРёС‚Рё РїР°СЂРѕР»СЊ", oldPassword: "РЎС‚Р°СЂРёР№ РїР°СЂРѕР»СЊ", oldPasswordRepeat: "РџРѕРІС‚РѕСЂС–С‚СЊ СЃС‚Р°СЂРёР№ РїР°СЂРѕР»СЊ", newPassword: "Р’РІРµРґС–С‚СЊ РЅРѕРІРёР№ РїР°СЂРѕР»СЊ", passwordChanged: "РџР°СЂРѕР»СЊ Р·РјС–РЅРµРЅРѕ", changePasswordError: "РќРµ РІРґР°Р»РѕСЃСЏ Р·РјС–РЅРёС‚Рё РїР°СЂРѕР»СЊ: ", confirmPassword: "РџР°СЂРѕР»СЊ РїС–РґС‚РІРµСЂРґР¶РµРЅРЅСЏ", taskTitle: "РќР°Р·РІР° Р·Р°РІРґР°РЅРЅСЏ", description: "РћРїРёСЃ", city: "РњС–СЃС‚Рѕ", postalCode: "РљРѕРґ", street: "Р’СѓР»РёС†СЏ", house: "Р‘СѓРґРёРЅРѕРє", apartment: "РљРІР°СЂС‚РёСЂР°", address: "РђРґСЂРµСЃР°", price: "Р¦С–РЅР°", add: "Р”РѕРґР°С‚Рё", employee: "СЃРїС–РІСЂРѕР±С–С‚РЅРёРє", delete: "Р’РёРґР°Р»РёС‚Рё", restart: "РџРѕС‡Р°С‚Рё Р·Р°РЅРѕРІРѕ", confirmDelete: "Р’РёРґР°Р»РёС‚Рё С†Рµ Р·Р°РІРґР°РЅРЅСЏ?", resetError: "РќРµ РІРґР°Р»РѕСЃСЏ РїРѕРІРµСЂРЅСѓС‚Рё Р·Р°РІРґР°РЅРЅСЏ: ", deleteError: "РќРµ РІРґР°Р»РѕСЃСЏ РІРёРґР°Р»РёС‚Рё Р·Р°РІРґР°РЅРЅСЏ: ", createdAt: "РЎС‚РІРѕСЂРµРЅРѕ", acceptedAt: "РџСЂРёР№РЅСЏС‚Рѕ", completedAt: "Р’РёРєРѕРЅР°РЅРѕ", new: "РќРѕРІРµ", accepted: "РџСЂРёР№РЅСЏС‚Рѕ", declined: "Р’С–РґС…РёР»РµРЅРѕ", completed: "Р’РёРєРѕРЅР°РЅРѕ", refused: "Р’С–РґРјРѕРІРёРІСЃСЏ", payment: "РћРїР»Р°С‚Р°", source: "Р”Р¶РµСЂРµР»Рѕ", dispatcher: "Р”РёСЃРїРµС‚С‡РµСЂ", client: "РљР»С–С”РЅС‚", cash: "Р“РѕС‚С–РІРєР°", fromReserve: "Р— СЂРµР·РµСЂРІСѓ", save: "Р—Р±РµСЂРµРіС‚Рё", cancel: "РЎРєР°СЃСѓРІР°С‚Рё", phone: "РќРѕРјРµСЂ С‚РµР»РµС„РѕРЅСѓ" },
      pl: { serverTitle: "Zadania", openUsers: "Pracownicy", completedTasks: "Wykonane zadania", calculations: "Rozliczenia", changePassword: "ZmieЕ„ hasЕ‚o", oldPassword: "Stare hasЕ‚o", oldPasswordRepeat: "PowtГіrz stare hasЕ‚o", newPassword: "Wpisz nowe hasЕ‚o", passwordChanged: "HasЕ‚o zmienione", changePasswordError: "Nie udaЕ‚o siД™ zmieniД‡ hasЕ‚a: ", confirmPassword: "HasЕ‚o potwierdzenia", taskTitle: "Nazwa zadania", description: "Opis", city: "Miasto", postalCode: "Kod", street: "Ulica", house: "Dom", apartment: "Mieszkanie", address: "Adres", price: "Cena", add: "Dodaj", employee: "pracownik", delete: "UsuЕ„", restart: "Zacznij od nowa", confirmDelete: "UsunД…Д‡ to zadanie?", resetError: "Nie udaЕ‚o siД™ przywrГіciД‡ zadania: ", deleteError: "Nie udaЕ‚o siД™ usunД…Д‡ zadania: ", createdAt: "Utworzono", acceptedAt: "PrzyjД™to", completedAt: "Wykonano", new: "Nowe", accepted: "PrzyjД™te", declined: "Odrzucone", completed: "Wykonane", refused: "OdmГіwiЕ‚", payment: "PЕ‚atnoЕ›Д‡", source: "Е№rГіdЕ‚o", dispatcher: "Dyspozytor", client: "Klient", cash: "GotГіwka", fromReserve: "Z rezerwy", save: "Zapisz", cancel: "Anuluj", phone: "Numer telefonu" }
    };
    function setLanguage(value) {
      language = value;
      localStorage.setItem("language", language);
      applyLanguage();
      loadTasks();
    }
    function applyLanguage() {
      document.querySelector("#languageSelect").value = language;
      document.querySelectorAll("[data-i18n]").forEach(el => el.textContent = texts[language][el.dataset.i18n]);
      document.querySelectorAll("[data-placeholder]").forEach(el => el.placeholder = texts[language][el.dataset.placeholder]);
      title.placeholder = language === "ru" ? "РќР°Р·РІР°РЅРёРµ\nР—Р°РґР°РЅРёРµ" : texts[language].taskTitle;
      phone.placeholder = language === "ru" ? "РќРѕРјРµСЂ\nРўРµР»РµС„РѕРЅР°" : "Phone";
      city.placeholder = texts[language].city;
      postalCode.placeholder = texts[language].postalCode;
      street.placeholder = texts[language].street;
      house.placeholder = texts[language].house;
      apartment.placeholder = texts[language].apartment;
      document.querySelectorAll("[data-payment-label]").forEach(option => {
        option.textContent = texts[language][option.dataset.paymentLabel] || option.textContent;
      });
    }
    function statusName(status) {
      return texts[language][status] || status;
    }
    function statusClass(status) {
      return "status " + String(status || "").replace(/[^a-z0-9_-]/gi, "");
    }
    let adminPassword = sessionStorage.getItem("adminPassword") || "";
    function adminHeaders(extra = {}) {
      if (!adminPassword) {
        adminPassword = prompt("Admin password") || "";
        sessionStorage.setItem("adminPassword", adminPassword);
      }
      return { "X-Admin-Password": adminPassword, "X-Language": language, ...extra };
    }
    async function requireAdminAccess(start) {
      while (true) {
        if (!adminPassword) {
          adminPassword = prompt("Admin password") || "";
        }
        if (!adminPassword) {
          document.body.innerHTML = "";
          return;
        }
        sessionStorage.setItem("adminPassword", adminPassword);
        const res = await fetch("/api/admin/check-password", {
          headers: { "X-Admin-Password": adminPassword, "X-Language": language }
        });
        if (res.ok) {
          document.body.classList.remove("locked");
          start();
          return;
        }
        sessionStorage.removeItem("adminPassword");
        adminPassword = "";
      }
    }
    async function changeAdminPassword() {
      const oldPassword = prompt(texts[language].oldPassword || "РЎС‚Р°СЂС‹Р№ РїР°СЂРѕР»СЊ");
      if (!oldPassword) return;
      const oldPasswordRepeat = prompt(texts[language].oldPasswordRepeat || "РџРѕРІС‚РѕСЂРёС‚Рµ СЃС‚Р°СЂС‹Р№ РїР°СЂРѕР»СЊ");
      if (!oldPasswordRepeat) return;
      const newPassword = prompt(texts[language].newAdminPassword || "Р’РІРµРґРёС‚Рµ РЅРѕРІС‹Р№ РїР°СЂРѕР»СЊ");
      if (!newPassword) return;
      const res = await fetch("/api/admin/change-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ oldPassword, oldPasswordRepeat, newPassword })
      });
      if (!res.ok) {
        const data = await res.json();
        alert((texts[language].changePasswordError || "РќРµ СѓРґР°Р»РѕСЃСЊ РёР·РјРµРЅРёС‚СЊ РїР°СЂРѕР»СЊ: ") + data.error);
        return;
      }
      adminPassword = newPassword;
      sessionStorage.setItem("adminPassword", adminPassword);
      alert(texts[language].passwordChanged || "РџР°СЂРѕР»СЊ РёР·РјРµРЅРµРЅ");
    }
    async function loadTasks() {
      const res = await fetch("/api/admin/tasks", { headers: adminHeaders() });
      if (res.status === 401) {
        sessionStorage.removeItem("adminPassword");
        adminPassword = "";
        alert("Wrong admin password");
        return;
      }
      const data = await res.json();
      tasks.innerHTML = data.tasks.map(t => `
        <article class="task">
          ${taskDatePanel(t)}
          <h3>${escapeHtml(t.title)}</h3>
          <p>${escapeHtml(t.description || "")}</p>
          <p>${t.phone ? "<strong>РўРµР»РµС„РѕРЅ:</strong> <a href=\"tel:" + phoneHref(t.phone) + "\">" + escapeHtml(t.phone) + "</a>" : ""}</p>
          <p>${t.address ? "<strong>" + texts[language].address + ":</strong> " + escapeHtml(t.address) : ""}</p>
          <p>${appSettings.showPrices && Number(t.price) ? "<strong>" + texts[language].price + ":</strong> " + formatMoney(t.price) : ""}</p>
          <p><strong>${texts[language].payment || "РћРїР»Р°С‚Р°"}:</strong> ${paymentMethodName(t.paymentMethod)}</p>
          <p><strong>${texts[language].source || "РСЃС‚РѕС‡РЅРёРє"}:</strong> ${taskSource(t)}</p>
          ${acceptedEmployeeLine(t)}
          <p class="meta"><span class="${statusClass(t.status)}">${statusName(t.status)}</span></p>
          ${resetButton(t)}
          ${editButton(t)}
          ${completeButton(t)}
          <button class="danger" type="button" onclick="deleteTask(${t.id})">${texts[language].delete}</button>
          ${editTaskForm(t)}
        </article>
      `).join("");
    }
    async function loadClientOptions() {
      const res = await fetch("/api/admin/clients", { headers: adminHeaders() });
      if (!res.ok) {
        clientOptions = [];
        return;
      }
      const data = await res.json();
      clientOptions = data.clients || [];
    }
    function taskSource(task) {
      if (task.source === "client") {
        const name = task.clientName || task.sourceName || texts[language].client || "РљР»РёРµРЅС‚";
        const login = loginWithoutPlus(task.clientLogin || "");
        return escapeHtml(login ? name + ", " + login : name);
      }
      return texts[language].dispatcher || "Р”РёСЃРїРµС‚С‡РµСЂ";
    }
    function taskDatePanel(task) {
      const rows = [];
      if (task.createdAt) rows.push([texts[language].createdAt || "РЎРѕР·РґР°РЅРѕ", task.createdAt]);
      if (task.acceptedAt) rows.push([texts[language].acceptedAt || "РџСЂРёРЅСЏС‚Рѕ", task.acceptedAt]);
      if (task.completedAt) rows.push([texts[language].completedAt || "Р’С‹РїРѕР»РЅРµРЅРѕ", task.completedAt]);
      if (!rows.length) return "";
      return `<div class="taskDates">${rows.map(row => `<span><strong>${row[0]}:</strong> ${formatCompactDate(row[1])}</span>`).join("")}</div>`;
    }
    function paymentMethodName(method) {
      return method === "cash" ? (texts[language].cash || "РќР°Р»РёС‡РЅС‹Рµ") : (texts[language].fromReserve || "РР· СЂРµР·РµСЂРІР°");
    }
    function assignedEmployeeName(task) {
      const parts = [task.assignedToName, task.assignedToLogin].filter(value => value && String(value).trim());
      return parts.length ? escapeHtml(parts.join(" ")) : "";
    }
    function acceptedEmployeeLine(task) {
      const name = assignedEmployeeName(task);
      const labels = { ru: "РљРµРј РїСЂРёРЅСЏС‚Рѕ", en: "Accepted by", uk: "РљРёРј РїСЂРёР№РЅСЏС‚Рѕ", pl: "Przyjete przez" };
      return name ? `<p><strong>${texts[language].acceptedBy || labels[language] || labels.ru}:</strong> ${name}</p>` : "";
    }
    function clientSourceOptions(task) {
      const currentId = task.clientId || "";
      return `
        <option value="" ${currentId ? "" : "selected"}>${texts[language].dispatcher || "Р”РёСЃРїРµС‚С‡РµСЂ"}</option>
        ${clientOptions.map(client => `
          <option value="${client.id}" ${String(currentId) === String(client.id) ? "selected" : ""}>
            ${escapeHtml(client.displayName || client.login || ("РљР»РёРµРЅС‚ #" + client.id))}
          </option>
        `).join("")}
      `;
    }
    function editButton(task) {
      if (!task.editable) {
        return "";
      }
      return `<button class="secondary" type="button" onclick="toggleTaskEdit(${task.id})">Р РµРґР°РєС‚РёСЂРѕРІР°С‚СЊ</button>`;
    }
    function completeButton(task) {
      if (!task.editable) {
        return "";
      }
      return `<button class="restart" type="button" onclick="completeTask(${task.id})">Р’С‹РїРѕР»РЅРµРЅРѕ</button>`;
    }
    function editTaskForm(task) {
      if (!task.editable) {
        return "";
      }
      return `
        <form class="editTask" id="edit-task-${task.id}" style="display:none" onsubmit="saveTaskEdit(event, ${task.id})">
          <div class="addressRow">
            <input name="city" value="${escapeAttr(task.city || "")}" placeholder="${texts[language].city}" required>
            <input name="postalCode" value="${escapeAttr(task.postalCode || "")}" placeholder="${texts[language].postalCode}">
            <input name="street" value="${escapeAttr(task.street || "")}" placeholder="${texts[language].street}" required>
            <input name="house" value="${escapeAttr(task.house || "")}" placeholder="${texts[language].house}">
            <input name="apartment" value="${escapeAttr(task.apartment || "")}" placeholder="${texts[language].apartment}">
          </div>
          <input name="title" value="${escapeAttr(task.originalTitle || task.title || "")}" placeholder="${texts[language].taskTitle}" required>
          <input name="description" value="${escapeAttr(task.originalDescription || task.description || "")}" placeholder="${texts[language].description}">
          <input name="phone" value="${escapeAttr(task.originalPhone || task.phone || "")}" placeholder="${texts[language].phone || "РќРѕРјРµСЂ С‚РµР»РµС„РѕРЅР°"}" inputmode="tel">
          <input name="price" value="${escapeAttr(task.price || "")}" placeholder="${texts[language].price}" inputmode="decimal">
          <select name="paymentMethod">
            <option value="card" ${(task.paymentMethod || "card") === "card" ? "selected" : ""}>${texts[language].fromReserve || "РР· СЂРµР·РµСЂРІР°"}</option>
            <option value="cash" ${task.paymentMethod === "cash" ? "selected" : ""}>${texts[language].cash || "РќР°Р»РёС‡РЅС‹Рµ"}</option>
          </select>
          <select class="clientSourceSelect" name="clientId" aria-label="${texts[language].client || "РљР»РёРµРЅС‚"}">
            ${clientSourceOptions(task)}
          </select>
          <div class="editTaskActions">
            <button type="submit">${texts[language].save || "РЎРѕС…СЂР°РЅРёС‚СЊ"}</button>
            <button class="secondary" type="button" onclick="toggleTaskEdit(${task.id})">${texts[language].cancel || "РћС‚РјРµРЅР°"}</button>
          </div>
        </form>
      `;
    }
    function toggleTaskEdit(id) {
      const form = document.querySelector("#edit-task-" + id);
      if (!form) return;
      form.style.display = form.style.display === "none" ? "grid" : "none";
    }
    function isTaskEditing() {
      return Array.from(document.querySelectorAll(".editTask")).some(form => form.style.display !== "none");
    }
    async function saveTaskEdit(event, id) {
      event.preventDefault();
      const form = event.target;
      const res = await fetch("/api/admin/tasks/" + id + "/edit", {
        method: "POST",
        headers: adminHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({
          title: form.title.value,
          description: form.description.value,
          phone: form.phone.value,
          city: form.city.value,
          postalCode: form.postalCode.value,
          street: form.street.value,
          house: form.house.value,
          apartment: form.apartment.value,
          price: form.price.value,
          paymentMethod: form.paymentMethod.value,
          clientId: form.clientId.value
        })
      });
      if (!res.ok) {
        const data = await res.json();
        alert("РќРµ СѓРґР°Р»РѕСЃСЊ СЃРѕС…СЂР°РЅРёС‚СЊ Р·Р°РґР°РЅРёРµ: " + data.error);
        return;
      }
      loadTasks();
    }
    function resetButton(task) {
      if (task.status === "completed") {
        return `<button class="restart" type="button" onclick="resetTask(${task.id})">${texts[language].restart}</button>`;
      }
      return "";
    }
    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    }
    function escapeAttr(value) {
      return escapeHtml(value).replace(/`/g, "&#96;");
    }
    function phoneHref(value) {
      return String(value).replace(/[^\d+]/g, "");
    }
    function loginWithoutPlus(value) {
      return String(value || "").replace(/^\+/, "");
    }
    document.querySelector("#form").addEventListener("submit", async event => {
      event.preventDefault();
      await fetch("/api/admin/tasks", {
        method: "POST",
        headers: adminHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ title: title.value, description: description.value, phone: phone.value, city: city.value, postalCode: postalCode.value, street: street.value, house: house.value, apartment: apartment.value, price: price.value, paymentMethod: paymentMethod.value })
      });
      title.value = "";
      description.value = "";
      phone.value = "";
      city.value = "";
      postalCode.value = "";
      street.value = "";
      house.value = "";
      apartment.value = "";
      price.value = "";
      paymentMethod.value = "cash";
      loadTasks();
    });
    async function resetTask(id) {
      const password = prompt(texts[language].confirmPassword);
      if (!password) {
        return;
      }
      const res = await fetch("/api/admin/tasks/" + id + "/reset", {
        method: "POST",
        headers: adminHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ password })
      });
      if (!res.ok) {
        const data = await res.json();
        alert(texts[language].resetError + data.error);
        return;
      }
      loadTasks();
    }
    async function deleteTask(id) {
      if (!confirm(texts[language].confirmDelete)) {
        return;
      }
      const password = prompt(texts[language].confirmPassword);
      if (!password) {
        return;
      }
      const res = await fetch("/api/admin/tasks/" + id + "/delete", {
        method: "POST",
        headers: adminHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ password })
      });
      if (!res.ok) {
        const data = await res.json();
        alert(texts[language].deleteError + data.error);
        return;
      }
      loadTasks();
    }
    async function completeTask(id) {
      const password = prompt(texts[language].confirmPassword);
      if (!password) {
        return;
      }
      const res = await fetch("/api/admin/tasks/" + id + "/complete", {
        method: "POST",
        headers: adminHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ password })
      });
      if (!res.ok) {
        const data = await res.json();
        const message = data.error === "task_not_accepted"
          ? "Р—Р°РґР°РЅРёРµ РЅРµР»СЊР·СЏ РІС‹РїРѕР»РЅРёС‚СЊ: РµРіРѕ РµС‰Рµ РЅРµ РїСЂРёРЅСЏР» СЃРѕС‚СЂСѓРґРЅРёРє."
          : "РќРµ СѓРґР°Р»РѕСЃСЊ РѕС‚РјРµС‚РёС‚СЊ Р·Р°РґР°РЅРёРµ РІС‹РїРѕР»РЅРµРЅРЅС‹Рј: " + data.error;
        alert(message);
        return;
      }
      loadTasks();
    }
    function formatMoney(value) {
      return new Intl.NumberFormat("ru-RU", { style: "currency", currency: appSettings.currency || "PLN" }).format(Number(value || 0));
    }
    function formatReserve(value) {
      const labels = { credits: "CRDT", tokens: "TKN", coins: "KOIN", points: "BAL" };
      return `${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 }).format(Number(value || 0))} ${labels[appSettings.reserveUnit] || labels.credits}`;
    }
    function formatDate(value) {
      return new Date(value * 1000).toLocaleString("ru-RU");
    }
    function formatCompactDate(value) {
      if (!value) return "";
      return new Date(value * 1000).toLocaleString("ru-RU", {
        day: "2-digit",
        month: "2-digit",
        year: "2-digit",
        hour: "2-digit",
        minute: "2-digit"
      });
    }
    function calculationStatus(item) {
      return item && item.calculated ? "РћРїР»Р°С‡РµРЅРѕ" : "РќРµ РѕРїР»Р°С‡РµРЅРѕ";
    }
    function calculationClass(item) {
      return item && item.calculated ? " calculated" : "";
    }
    function calculationStatus(item) {
      return item && item.calculated ? "Р Р°СЃСЃС‡РёС‚Р°РЅРѕ" : "РќРµ СЂР°СЃСЃС‡РёС‚Р°РЅРѕ";
    }
    function calculationClass(item) {
      return item && item.calculated ? " calculated" : "";
    }
    async function loadAppSettings() {
      const res = await fetch("/api/admin/settings", { headers: adminHeaders() });
      if (res.ok) {
        const data = await res.json();
        appSettings = data.settings || appSettings;
      }
    }
    applyLanguage();
    requireAdminAccess(async () => {
      await loadAppSettings();
      await loadClientOptions();
      loadTasks();
      setInterval(() => {
        if (!isTaskEditing()) {
          loadTasks();
        }
      }, 30000);
    });
  </script>
</body>
</html>"""


COMPLETED_HTML = INDEX_HTML.replace(
    "<title>Р—Р°РґР°РЅРёСЏ</title>",
    "<title>Р’С‹РїРѕР»РЅРµРЅРЅС‹Рµ Р·Р°РґР°РЅРёСЏ</title>",
).replace(
    '<h1 data-i18n="serverTitle">Р—Р°РґР°РЅРёСЏ</h1>',
    '<h1 data-i18n="serverTitle">Р’С‹РїРѕР»РЅРµРЅРЅС‹Рµ Р·Р°РґР°РЅРёСЏ</h1>',
).replace(
    '<form id="form">',
    '<form id="form" style="display:none">',
).replace(
    'ru: { serverTitle: "Р—Р°РґР°РЅРёСЏ",',
    'ru: { serverTitle: "Р’С‹РїРѕР»РЅРµРЅРЅС‹Рµ Р·Р°РґР°РЅРёСЏ",',
).replace(
    'en: { serverTitle: "Tasks",',
    'en: { serverTitle: "Completed tasks",',
).replace(
    'uk: { serverTitle: "Р—Р°РІРґР°РЅРЅСЏ",',
    'uk: { serverTitle: "Р’РёРєРѕРЅР°РЅС– Р·Р°РІРґР°РЅРЅСЏ",',
).replace(
    'pl: { serverTitle: "Zadania",',
    'pl: { serverTitle: "Wykonane zadania",',
).replace(
    'fetch("/api/admin/tasks"',
    'fetch("/api/admin/completed-tasks"',
).replace(
    'fetch("/api/admin/tasks/" + id + "/delete"',
    'fetch("/api/admin/completed-tasks/" + id + "/hide"',
).replace(
    'fetch("/api/admin/completed-tasks/" + id + "/delete"',
    'fetch("/api/admin/completed-tasks/" + id + "/hide"',
).replace(
    'setInterval(loadTasks, 5000);',
    'setInterval(loadTasks, 30000);',
)


CALCULATIONS_HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Р Р°СЃС‡РµС‚С‹ СЃРѕС‚СЂСѓРґРЅРёРєРѕРІ</title>
  <style>
    :root { --ink: #172026; --teal: #0f766e; --gold: #f6c85f; --paper: #fffaf0; }
    body { font-family: Arial, sans-serif; margin: 0; color: var(--ink); background: linear-gradient(135deg, #fff7df 0%, #d8f8eb 48%, #e9d5ff 100%); min-height: 100vh; }
    body.locked header, body.locked main { display: none; }
    header { background: linear-gradient(135deg, #0f766e 0%, #2563eb 48%, #7c3aed 100%); color: white; padding: 24px 28px; }
    main { max-width: 980px; margin: 0 auto; padding: 24px; }
    nav { display: grid; grid-template-columns: repeat(10, minmax(104px, 1fr)); gap: 10px; margin-top: 12px; max-width: 1320px; }
    nav a { display: flex; align-items: center; justify-content: center; min-height: 44px; box-sizing: border-box; color: var(--ink); background: var(--gold); font-weight: bold; padding: 8px 10px; border-radius: 8px; text-align: center; line-height: 1.15; text-decoration: none; }
    button { font: inherit; padding: 11px 13px; border-radius: 8px; border: 0; background: linear-gradient(135deg, var(--teal), #2563eb); color: white; cursor: pointer; font-weight: 700; }
    .item { background: #fee2e2; border-left: 6px solid #ef4444; border-radius: 8px; padding: 16px; margin: 12px 0; box-shadow: 0 14px 34px rgba(23,32,38,.1); }
    .item.calculated { background: #dcfce7; border-left-color: #16a34a; }
    .reportTask { padding: 12px; margin: 8px 0; background: white; border-radius: 8px; border-left: 5px solid var(--teal); }
    .reportTask.active { border-left-color: #f6c85f; }
    .reportTask.other { border-left-color: #94a3b8; }
    details { margin-top: 12px; padding: 12px; background: rgba(255,255,255,.72); border: 1px solid rgba(23,32,38,.1); border-radius: 8px; }
    summary { cursor: pointer; font-weight: 700; }
    .actions { display: flex; justify-content: flex-end; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
    .danger { background: #ef4444; box-shadow: none; }
    .success { background: #16a34a; box-shadow: none; }
    .reportTask { padding: 12px; margin: 8px 0; background: white; border-radius: 8px; border-left: 5px solid var(--teal); }
    .reportTask.refused { border-left-color: #f9735b; }
    .reportTask.other { border-left-color: #94a3b8; }
    details { margin-top: 12px; padding: 12px; background: rgba(255,255,255,.72); border: 1px solid rgba(23,32,38,.1); border-radius: 8px; }
    summary { cursor: pointer; font-weight: 700; }
    .actions { display: flex; justify-content: flex-end; margin-top: 12px; }
    .danger { background: #ef4444; box-shadow: none; }
    .success { background: #16a34a; box-shadow: none; }
    .meta { color: #60717d; font-size: 14px; }
    .status { display: inline-block; padding: 5px 10px; border-radius: 999px; background: #ef4444; color: white; font-weight: 700; }
    .item.calculated .status { background: #16a34a; }
    @media (max-width: 980px) { nav { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
  </style>
</head>
<body class="locked">
  <header>
    <h1>Р Р°СЃС‡РµС‚С‹ СЃРѕС‚СЂСѓРґРЅРёРєРѕРІ</h1>
    <nav><a href="/server">Р—Р°РґР°РЅРёСЏ</a> <a href="/users">РЎРѕС‚СЂСѓРґРЅРёРєРё</a> <a href="/clients">РљР»РёРµРЅС‚С‹</a> <a href="/completed">Р’С‹РїРѕР»РЅРµРЅРЅС‹Рµ Р·Р°РґР°РЅРёСЏ</a> <a href="/calculations">Р Р°СЃС‡РµС‚С‹ СЃРѕС‚СЂСѓРґРЅРёРєРѕРІ</a> <a href="/client-calculations">Р Р°СЃС‡РµС‚С‹ РєР»РёРµРЅС‚РѕРІ</a> <a href="/telegram-ads">Р РµРєР»Р°РјР° Telegram</a> <a href="/facebook-ads">Р РµРєР»Р°РјР° Facebook</a> <a href="/telegram-login">Telegram userbot</a> <a href="/settings">РќР°СЃС‚СЂРѕР№РєРё</a></nav>
  </header>
  <main>
    <section id="settlements"></section>
  </main>
  <script>
    const settlements = document.querySelector("#settlements");
    let appSettings = { currency: "PLN", reserveUnit: "credits", showPrices: true };
    let adminPassword = sessionStorage.getItem("adminPassword") || "";
    function adminHeaders(extra = {}) {
      if (!adminPassword) {
        adminPassword = prompt("Admin password") || "";
        sessionStorage.setItem("adminPassword", adminPassword);
      }
      return { "X-Admin-Password": adminPassword, ...extra };
    }
    async function requireAdminAccess(start) {
      while (true) {
        if (!adminPassword) {
          adminPassword = prompt("Admin password") || "";
        }
        if (!adminPassword) {
          document.body.innerHTML = "";
          return;
        }
        sessionStorage.setItem("adminPassword", adminPassword);
        const res = await fetch("/api/admin/check-password", {
          headers: { "X-Admin-Password": adminPassword }
        });
        if (res.ok) {
          document.body.classList.remove("locked");
          start();
          return;
        }
        sessionStorage.removeItem("adminPassword");
        adminPassword = "";
      }
    }
    function formatMoney(value) {
      return new Intl.NumberFormat("ru-RU", { style: "currency", currency: appSettings.currency || "PLN" }).format(Number(value || 0));
    }
    function formatReserve(value) {
      const labels = { credits: "CRDT", tokens: "TKN", coins: "KOIN", points: "BAL" };
      return `${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 }).format(Number(value || 0))} ${labels[appSettings.reserveUnit] || labels.credits}`;
    }
    function formatDate(value) {
      return new Date(value * 1000).toLocaleString("ru-RU");
    }
    function calculationStatus(item) {
      return item && item.calculated ? "РћРїР»Р°С‡РµРЅРѕ" : "РќРµ РѕРїР»Р°С‡РµРЅРѕ";
    }
    function calculationClass(item) {
      return item && item.calculated ? " calculated" : "";
    }
    function taskStatusName(status) {
      const names = {
        completed: "Р’С‹РїРѕР»РЅРµРЅРѕ",
        refused: "РћС‚РєР°Р·Р°Р»СЃСЏ",
        accepted: "РџСЂРёРЅСЏС‚Рѕ",
        declined: "РћС‚РєР»РѕРЅРµРЅРѕ",
        new: "РќРѕРІРѕРµ"
      };
      return names[status] || status || "";
    }
    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    }
    async function loadSettlements() {
      const res = await fetch("/api/admin/settlements", { headers: adminHeaders() });
      if (!res.ok) {
        settlements.innerHTML = "<p>РќРµ СѓРґР°Р»РѕСЃСЊ Р·Р°РіСЂСѓР·РёС‚СЊ СЂР°СЃС‡РµС‚С‹.</p>";
        return;
      }
      const data = await res.json();
      const history = (data.settlements || []).map(item => `
        <article class="item${calculationClass(item)}">
          <h3>#${item.id} ${escapeHtml(item.displayName)} В· ${formatDate(item.createdAt)}</h3>
          <p class="status">РЎС‚Р°С‚СѓСЃ СЂР°СЃС‡РµС‚Р°: ${calculationStatus(item)}</p>
          <p class="meta">Р’С‹РїРѕР»РЅРµРЅРѕ: ${item.counts.completed || 0} В· РћС‚РєР°Р·Р°Р»СЃСЏ: ${item.counts.refused || 0} В· Р’СЃРµРіРѕ РЅР°Р·РЅР°С‡РµРЅРѕ: ${item.counts.all || 0}</p>
          <p>${appSettings.showPrices ? "<strong>РЎСѓРјРјР° Рє РІС‹РїР»Р°С‚Рµ:</strong> " + formatMoney(item.totals.payoutPrice ?? item.totals.completedPrice ?? 0) : ""}</p>
          ${renderDeductions(item)}
          ${renderSettlementReport(item)}
          <div class="actions">${calculateSettlementButton(item)}<button class="danger" type="button" onclick="deleteSettlement(${item.id})">РЈРґР°Р»РёС‚СЊ СЂР°СЃС‡РµС‚</button></div>
        </article>
      `).join("");
      settlements.innerHTML = history || "<p class=\"meta\">РЎРѕР·РґР°РЅРЅС‹С… СЂР°СЃС‡РµС‚РѕРІ РїРѕРєР° РЅРµС‚. РќРѕРІС‹Р№ СЂР°СЃС‡РµС‚ СЃРѕР·РґР°РµС‚СЃСЏ РІ СЂР°Р·РґРµР»Рµ В«РЎРѕС‚СЂСѓРґРЅРёРєРёВ» РєРЅРѕРїРєРѕР№ В«Р Р°СЃСЃС‡РёС‚Р°С‚СЊВ».</p>";
    }
    function calculateSettlementButton(item) {
      if (item.calculated) {
        return "";
      }
      return `<button class="success" type="button" onclick="calculateSettlement(${item.id})">РћРїР»Р°С‚РёС‚СЊ</button>`;
    }
    function renderDeductions(item) {
      if (!appSettings.showPrices) {
        return "";
      }
      return `
        <p class="meta">Р’С‹РїРѕР»РЅРµРЅРЅС‹Рµ СЂР°Р±РѕС‚С‹: ${formatMoney(item.totals.completedPrice || 0)} В· РЈРґРµСЂР¶Р°РЅРёРµ ${item.totals.completedFeePercent || 0}%: ${formatMoney(item.totals.completedFee || 0)}</p>
        <p class="meta">РћС‚РєР°Р·Р°РЅРЅС‹Рµ СЂР°Р±РѕС‚С‹: ${formatMoney(item.totals.refusedPrice || 0)} В· РЈРґРµСЂР¶Р°РЅРёРµ ${item.totals.refusedFeePercent || 0}%: ${formatMoney(item.totals.refusedFee || 0)}</p>
      `;
    }
    function renderSettlementReport(item) {
      return `
        <details>
          <summary>РџРѕР»РЅС‹Р№ РѕС‚С‡РµС‚ РїРѕ СЂР°СЃС‡РµС‚Сѓ</summary>
          ${renderTaskSection("Р’С‹РїРѕР»РЅРµРЅРЅС‹Рµ СЂР°Р±РѕС‚С‹", item.completed || [], "completed")}
          ${renderTaskSection("Р Р°Р±РѕС‚С‹ СЃ РѕС‚РєР°Р·РѕРј", item.refused || [], "refused")}
          ${renderTaskSection("РћСЃС‚Р°Р»СЊРЅС‹Рµ СЂР°Р±РѕС‚С‹", item.other || [], "other")}
          ${renderReserveSummary(item)}
          ${renderReserveEvents(item.currentReserveEvents || item.reserveEvents || [])}
        </details>
      `;
    }
    function renderReserveSummary(item) {
      if (!appSettings.showPrices) {
        return "";
      }
      const totals = item.totals || {};
      const reserveDeductions = Number(totals.completedFeeFromReserve || 0) + Number(totals.refusedFeeFromReserve || 0);
      return `
        <h4>Р РµР·РµСЂРІ РІ СЂР°СЃС‡РµС‚Рµ</h4>
        <p class="meta">Р РµР·РµСЂРІ: ${formatReserve(totals.reservePrice || 0)} В· Р’ СЂРµР·РµСЂРІ: ${formatReserve(totals.completedToReserve || 0)} В· РР· СЂРµР·РµСЂРІР° РІ РІС‹РїР»Р°С‚Сѓ: ${formatReserve(totals.reserveToCompleted || 0)} В· РЈРґРµСЂР¶Р°РЅРѕ РёР· СЂРµР·РµСЂРІР°: ${formatReserve(reserveDeductions)}</p>
      `;
    }
    function reserveEventName(kind) {
      if (kind === "to_reserve") return "РР· СЃСѓРјРјС‹ Рє РІС‹РїР»Р°С‚Рµ РІ СЂРµР·РµСЂРІ";
      if (kind === "from_reserve") return "РР· СЂРµР·РµСЂРІР° РІ РІС‹РїР»Р°С‚Сѓ";
      if (kind === "top_up") return "РџРѕРїРѕР»РЅРµРЅРёРµ СЂРµР·РµСЂРІР°";
      if (kind === "refused_fee_from_reserve") return "РЈРґРµСЂР¶Р°РЅРёРµ Р·Р° РѕС‚РєР°Р· РёР· СЂРµР·РµСЂРІР°";
      if (kind === "cash_completed_fee_from_reserve") return "РЈРґРµСЂР¶Р°РЅРёРµ Р·Р° РЅР°Р»РёС‡РЅС‹Рµ РёР· СЂРµР·РµСЂРІР°";
      return kind;
    }
    function renderReserveEvents(events) {
      if (!events || !events.length) {
        return `
          <h4>РћРїРµСЂР°С†РёРё СЂРµР·РµСЂРІР°</h4>
          <p class="meta">РќРµС‚ РѕРїРµСЂР°С†РёР№ СЂРµР·РµСЂРІР°.</p>
        `;
      }
      return `
        <h4>РћРїРµСЂР°С†РёРё СЂРµР·РµСЂРІР°</h4>
        ${events.map(event => `<p class="meta">${formatDate(event.createdAt)} В· ${reserveEventName(event.kind)} В· ${formatReserve(event.absoluteAmount ?? Math.abs(event.amount || 0))}</p>`).join("")}
      `;
    }
    function renderTaskSection(title, tasks, type) {
      if (!tasks.length) {
        return `<h4>${title}</h4><p class="meta">РќРµС‚ Р·Р°РїРёСЃРµР№.</p>`;
      }
      return `
        <h4>${title}</h4>
        ${tasks.map(task => `
          <article class="reportTask ${type}">
            <strong>#${task.id}</strong>
            <p><strong>${escapeHtml(task.title)}</strong></p>
            <p>${escapeHtml(task.description || "")}</p>
            <p>${task.phone ? "<strong>РўРµР»РµС„РѕРЅ:</strong> <a href=\"tel:" + phoneHref(task.phone) + "\">" + escapeHtml(task.phone) + "</a>" : ""}</p>
            <p>${task.address ? "<strong>РђРґСЂРµСЃ:</strong> " + escapeHtml(task.address) : ""}</p>
            <p>${appSettings.showPrices ? "<strong>Р¦РµРЅР°:</strong> " + formatMoney(task.price || 0) : ""}</p>
            <p><strong>РћРїР»Р°С‚Р°:</strong> ${paymentMethodName(task.paymentMethod)}</p>
            <div class="meta">РЎС‚Р°С‚СѓСЃ: ${taskStatusName(task.status)} В· ${taskDatesMeta(task)}</div>
          </article>
        `).join("")}
      `;
    }
    function phoneHref(value) {
      return String(value).replace(/[^\d+]/g, "");
    }
    function paymentMethodName(method) {
      return method === "cash" ? "РќР°Р»РёС‡РЅС‹Рµ" : "РР· СЂРµР·РµСЂРІР°";
    }
    function taskDatesMeta(task) {
      const rows = [];
      if (task.createdAt) rows.push("РЎРѕР·РґР°РЅРѕ: " + formatDate(task.createdAt));
      if (task.acceptedAt) rows.push("РџСЂРёРЅСЏС‚Рѕ: " + formatDate(task.acceptedAt));
      if (task.completedAt) rows.push("Р’С‹РїРѕР»РЅРµРЅРѕ: " + formatDate(task.completedAt));
      return rows.length ? rows.join(" В· ") : "РЅРµС‚ РґР°С‚С‹";
    }
    async function deleteSettlement(id) {
      const password = prompt("РџР°СЂРѕР»СЊ РїРѕРґС‚РІРµСЂР¶РґРµРЅРёСЏ");
      if (!password) {
        return;
      }
      const res = await fetch("/api/admin/settlements/" + id + "/delete", {
        method: "POST",
        headers: adminHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ password })
      });
      if (!res.ok) {
        alert("РќРµ СѓРґР°Р»РѕСЃСЊ СѓРґР°Р»РёС‚СЊ СЂР°СЃС‡РµС‚.");
        return;
      }
      loadSettlements();
    }
    async function calculateSettlement(id) {
      const password = prompt("РџР°СЂРѕР»СЊ РїРѕРґС‚РІРµСЂР¶РґРµРЅРёСЏ");
      if (!password) {
        return;
      }
      const res = await fetch("/api/admin/settlements/" + id + "/calculate", {
        method: "POST",
        headers: adminHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ password })
      });
      if (!res.ok) {
        alert("РќРµ СѓРґР°Р»РѕСЃСЊ РѕРїР»Р°С‚РёС‚СЊ.");
        return;
      }
      loadSettlements();
    }
    async function loadAppSettings() {
      const res = await fetch("/api/admin/settings", { headers: adminHeaders() });
      if (res.ok) {
        const data = await res.json();
        appSettings = data.settings || appSettings;
      }
    }
    requireAdminAccess(async () => {
      await loadAppSettings();
      loadSettlements();
    });
  </script>
</body>
</html>"""


USERS_HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>РЎРѕС‚СЂСѓРґРЅРёРєРё</title>
  <style>
    :root { --ink: #172026; --teal: #0f766e; --coral: #f9735b; --gold: #f6c85f; --mint: #d8f8eb; --paper: #fffaf0; }
    body { font-family: Arial, sans-serif; margin: 0; color: var(--ink); background: linear-gradient(135deg, #fff7df 0%, #d8f8eb 48%, #e9d5ff 100%); min-height: 100vh; }
    body.locked header, body.locked main { display: none; }
    header { position: relative; background: linear-gradient(135deg, #7c3aed 0%, #2563eb 48%, #0f766e 100%); color: white; padding: 24px 28px; box-shadow: 0 18px 42px rgba(124, 58, 237, 0.22); }
    main { max-width: 980px; margin: 0 auto; padding: 24px; }
    form { display: grid; gap: 10px; grid-template-columns: 1fr 1fr 1fr auto; margin-bottom: 24px; }
    input, button { font: inherit; padding: 11px 13px; border-radius: 8px; border: 1px solid rgba(23, 32, 38, 0.18); }
    input { background: rgba(255, 255, 255, 0.94); }
    button { background: linear-gradient(135deg, var(--teal), #2563eb); color: white; border: 0; cursor: pointer; font-weight: 700; box-shadow: 0 10px 24px rgba(15, 118, 110, 0.2); }
    .secondary { background: #fff0bf; color: #4a3200; box-shadow: none; }
    .user { background: rgba(255, 255, 255, 0.94); border: 1px solid rgba(255, 255, 255, 0.72); border-left: 6px solid var(--coral); border-radius: 8px; padding: 14px 16px; margin: 12px 0; box-shadow: 0 14px 34px rgba(23, 32, 38, 0.1); }
    .userHeader { display: flex; justify-content: space-between; gap: 16px; align-items: center; }
    .userActions { display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; align-items: center; }
    .userMoneyMini { min-width: 148px; min-height: 44px; box-sizing: border-box; padding: 6px 12px; border-radius: 8px; background: white; border: 1px solid #eef2f4; display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; box-shadow: inset 0 0 0 1px rgba(15, 118, 110, 0.05); }
    .userMoneyMini strong { font-size: 16px; line-height: 1.1; overflow-wrap: anywhere; }
    .userMoneyMini span { margin-top: 2px; color: #60717d; font-size: 12px; line-height: 1.1; }
    .editForm { grid-template-columns: 1fr 1fr 1fr auto auto; margin: 14px 0 0; }
    .danger, .deleteEmployee { background: #ef4444; color: white; box-shadow: none; }
    .report { margin-top: 14px; padding: 16px; background: linear-gradient(135deg, #f8fbff, #fff7df); border: 1px solid rgba(23, 32, 38, 0.1); border-radius: 8px; }
    .reportStats { display: grid; grid-template-columns: repeat(4, minmax(110px, 1fr)); gap: 10px; margin-bottom: 16px; }
    .moneyStats { grid-template-columns: repeat(4, minmax(120px, 1fr)); gap: 8px; }
    .statBox { min-height: 76px; padding: 12px; background: white; border-radius: 8px; border: 1px solid #eef2f4; display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; }
    .moneyStats .statBox { min-height: 68px; padding: 10px; }
    .statBox strong { display: block; font-size: 24px; line-height: 1.15; overflow-wrap: anywhere; }
    .moneyStats .statBox strong { font-size: 20px; }
    .statBox span { display: block; margin-top: 5px; color: #60717d; font-size: 14px; line-height: 1.25; }
    .reserveBox { justify-content: space-between; gap: 6px; }
    .reserveControls { display: grid; grid-template-columns: 38px 1fr 38px; align-items: center; gap: 8px; width: 100%; margin-top: 4px; }
    .reserveControls button { min-width: 38px; padding: 7px 0; }
    .reserveControls span { margin: 0; font-weight: 700; color: #172026; }
    .reportSection { margin-top: 14px; }
    .reportTask { padding: 12px; margin: 8px 0; background: white; border-radius: 8px; border-left: 5px solid var(--teal); }
    .reportTask.refused { border-left-color: var(--coral); }
    .reportTask.other { border-left-color: #94a3b8; }
    details.settlement { margin-top: 12px; padding: 12px; background: white; border-radius: 8px; border: 1px solid #eef2f4; }
    details.settlement summary { cursor: pointer; font-weight: 700; }
    .meta { color: #60717d; font-size: 14px; }
    nav { display: grid; grid-template-columns: repeat(10, minmax(104px, 1fr)); gap: 10px; margin-top: 12px; max-width: 1320px; }
    nav a { display: flex; align-items: center; justify-content: center; min-height: 44px; box-sizing: border-box; color: var(--ink); background: var(--gold); font-weight: bold; padding: 8px 10px; border-radius: 8px; text-align: center; line-height: 1.15; text-decoration: none; }
    .language-corner { position: absolute; top: 18px; right: 20px; }
    .language-corner select { font: inherit; padding: 8px 10px; border-radius: 8px; border: 0; background: white; color: var(--ink); font-weight: 700; }
    @media (max-width: 980px) { nav { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
    @media (max-width: 760px) { form, .editForm, .reportStats { grid-template-columns: 1fr; } .userHeader { display: block; } .userActions { justify-content: flex-start; margin-top: 10px; } }
  </style>
</head>
<body class="locked">
  <header>
    <div class="language-corner">
      <select id="languageSelect" onchange="setLanguage(this.value)">
        <option value="en">English</option>
        <option value="uk">Українська</option>
        <option value="ru">Русский</option>
        <option value="pl">Polski</option>
      </select>
    </div>
    <h1 data-i18n="employees">РЎРѕС‚СЂСѓРґРЅРёРєРё</h1>
    <nav><a href="/server">Р—Р°РґР°РЅРёСЏ</a> <a href="/users">РЎРѕС‚СЂСѓРґРЅРёРєРё</a> <a href="/clients">РљР»РёРµРЅС‚С‹</a> <a href="/completed">Р’С‹РїРѕР»РЅРµРЅРЅС‹Рµ Р·Р°РґР°РЅРёСЏ</a> <a href="/calculations">Р Р°СЃС‡РµС‚С‹ СЃРѕС‚СЂСѓРґРЅРёРєРѕРІ</a> <a href="/client-calculations">Р Р°СЃС‡РµС‚С‹ РєР»РёРµРЅС‚РѕРІ</a> <a href="/telegram-ads">Р РµРєР»Р°РјР° Telegram</a> <a href="/facebook-ads">Р РµРєР»Р°РјР° Facebook</a> <a href="/telegram-login">Telegram userbot</a> <a href="/settings">РќР°СЃС‚СЂРѕР№РєРё</a></nav>
  </header>
  <main>
    <button class="secondary" type="button" onclick="refreshUsersList()" data-i18n="refreshList">РћР±РЅРѕРІРёС‚СЊ СЃРїРёСЃРѕРє</button>
    <form id="userForm">
      <input id="userDisplayName" data-placeholder="employeeName" placeholder="РРјСЏ СЃРѕС‚СЂСѓРґРЅРёРєР°" required>
      <input id="userLogin" data-placeholder="login" placeholder="Р›РѕРіРёРЅ" required>
      <input id="userPassword" data-placeholder="password" placeholder="РџР°СЂРѕР»СЊ" required>
      <button data-i18n="add">Р”РѕР±Р°РІРёС‚СЊ</button>
    </form>
    <button class="secondary" type="button" onclick="settleAllUsers()" data-i18n="calculateAll">Р Р°СЃСЃС‡РёС‚Р°С‚СЊ РІСЃРµС…</button>
    <section id="users"></section>
  </main>
  <script>
    const users = document.querySelector("#users");
    let editingUserId = null;
    let openReportId = null;
    let language = localStorage.getItem("language") || "ru";
    let appSettings = { currency: "PLN", reserveUnit: "credits", showPrices: true };
    const texts = {
      ru: { employees: "РЎРѕС‚СЂСѓРґРЅРёРєРё", changePassword: "РР·РјРµРЅРёС‚СЊ РїР°СЂРѕР»СЊ", oldPassword: "РЎС‚Р°СЂС‹Р№ РїР°СЂРѕР»СЊ", oldPasswordRepeat: "РџРѕРІС‚РѕСЂРёС‚Рµ СЃС‚Р°СЂС‹Р№ РїР°СЂРѕР»СЊ", newAdminPassword: "Р’РІРµРґРёС‚Рµ РЅРѕРІС‹Р№ РїР°СЂРѕР»СЊ", passwordChanged: "РџР°СЂРѕР»СЊ РёР·РјРµРЅРµРЅ", changePasswordError: "РќРµ СѓРґР°Р»РѕСЃСЊ РёР·РјРµРЅРёС‚СЊ РїР°СЂРѕР»СЊ: ", backHome: "Р’РµСЂРЅСѓС‚СЊСЃСЏ РЅР° РіР»Р°РІРЅС‹Р№ СЌРєСЂР°РЅ", refreshList: "РћР±РЅРѕРІРёС‚СЊ СЃРїРёСЃРѕРє", employeeName: "РРјСЏ СЃРѕС‚СЂСѓРґРЅРёРєР°", login: "Р›РѕРіРёРЅ", password: "РџР°СЂРѕР»СЊ", newPassword: "РќРѕРІС‹Р№ РїР°СЂРѕР»СЊ, РµСЃР»Рё РЅСѓР¶РЅРѕ", add: "Р”РѕР±Р°РІРёС‚СЊ", tasks: "Р—Р°РґР°РЅРёР№", report: "РћС‚С‡РµС‚", calculate: "Р Р°СЃСЃС‡РёС‚Р°С‚СЊ", edit: "Р РµРґР°РєС‚РёСЂРѕРІР°С‚СЊ", save: "РЎРѕС…СЂР°РЅРёС‚СЊ", deleteEmployee: "РЈРґР°Р»РёС‚СЊ СЃРѕС‚СЂСѓРґРЅРёРєР°", loadingReport: "Р—Р°РіСЂСѓР¶Р°СЋ РѕС‚С‡РµС‚...", reportError: "РќРµ СѓРґР°Р»РѕСЃСЊ Р·Р°РіСЂСѓР·РёС‚СЊ РѕС‚С‡РµС‚: ", saveError: "РќРµ СѓРґР°Р»РѕСЃСЊ СЃРѕС…СЂР°РЅРёС‚СЊ СЃРѕС‚СЂСѓРґРЅРёРєР°: ", addError: "РќРµ СѓРґР°Р»РѕСЃСЊ РґРѕР±Р°РІРёС‚СЊ СЃРѕС‚СЂСѓРґРЅРёРєР°: ", settleError: "РќРµ СѓРґР°Р»РѕСЃСЊ РІС‹РїРѕР»РЅРёС‚СЊ СЂР°СЃС‡РµС‚: ", deleteUserError: "РќРµ СѓРґР°Р»РѕСЃСЊ СѓРґР°Р»РёС‚СЊ СЃРѕС‚СЂСѓРґРЅРёРєР°: ", confirmPassword: "РџР°СЂРѕР»СЊ РїРѕРґС‚РІРµСЂР¶РґРµРЅРёСЏ", detailedReport: "РџРѕРґСЂРѕР±РЅС‹Р№ РѕС‚С‡РµС‚", history: "РСЃС‚РѕСЂРёСЏ СЂР°СЃС‡РµС‚РѕРІ", settlement: "Р Р°СЃС‡РµС‚", completed: "Р’С‹РїРѕР»РЅРµРЅРѕ", refused: "РћС‚РєР°Р·Р°Р»СЃСЏ", accepted: "Р’ СЂР°Р±РѕС‚Рµ", allAssigned: "Р’СЃРµРіРѕ РЅР°Р·РЅР°С‡РµРЅРѕ", completedSum: "РЎСѓРјРјР° РІС‹РїРѕР»РЅРµРЅРЅС‹С…", refusedSum: "РЎСѓРјРјР° РѕС‚РєР°Р·Р°РЅРЅС‹С…", reserve: "Р РµР·РµСЂРІ", payout: "РЎСѓРјРјР° Рє РІС‹РїР»Р°С‚Рµ", completedWorks: "Р’С‹РїРѕР»РЅРµРЅРЅС‹Рµ СЂР°Р±РѕС‚С‹", refusedWorks: "РћС‚РєР°Р·Р°РЅРЅС‹Рµ СЂР°Р±РѕС‚С‹", otherWorks: "Р’ СЂР°Р±РѕС‚Рµ", noRecords: "РќРµС‚ Р·Р°РїРёСЃРµР№.", address: "РђРґСЂРµСЃ", price: "Р¦РµРЅР°", status: "РЎС‚Р°С‚СѓСЃ", created: "РЎРѕР·РґР°РЅРѕ", changed: "РР·РјРµРЅРµРЅРѕ", noDate: "РЅРµС‚ РґР°С‚С‹", statusCompleted: "Р’С‹РїРѕР»РЅРµРЅРѕ", statusRefused: "РћС‚РєР°Р·Р°Р»СЃСЏ", statusAccepted: "РџСЂРёРЅСЏС‚Рѕ", statusDeclined: "РћС‚РєР»РѕРЅРµРЅРѕ", statusNew: "РќРѕРІРѕРµ" },
      en: { employees: "Employees", backHome: "Back to the main screen", refreshList: "Refresh list", employeeName: "Employee name", login: "Login", password: "Password", newPassword: "New password, if needed", add: "Add", tasks: "Tasks", report: "Report", calculate: "Calculate", edit: "Edit", save: "Save", deleteEmployee: "Delete employee", loadingReport: "Loading report...", reportError: "Could not load report: ", saveError: "Could not save employee: ", addError: "Could not add employee: ", settleError: "Could not calculate: ", deleteUserError: "Could not delete employee: ", confirmPassword: "Confirmation password", detailedReport: "Detailed report", history: "Calculation history", settlement: "Calculation", completed: "Completed", refused: "Refused", accepted: "In progress", allAssigned: "Total assigned", completedSum: "Completed total", refusedSum: "Refused total", reserve: "Reserve", payout: "Amount to pay", completedWorks: "Completed jobs", refusedWorks: "Refused jobs", otherWorks: "Other assigned jobs", noRecords: "No records.", address: "Address", price: "Price", status: "Status", created: "Created", changed: "Changed", noDate: "no date", statusCompleted: "Completed", statusRefused: "Refused", statusAccepted: "Accepted", statusDeclined: "Declined", statusNew: "New" },
      uk: { employees: "РЎРїС–РІСЂРѕР±С–С‚РЅРёРєРё", backHome: "РџРѕРІРµСЂРЅСѓС‚РёСЃСЏ РЅР° РіРѕР»РѕРІРЅРёР№ РµРєСЂР°РЅ", refreshList: "РћРЅРѕРІРёС‚Рё СЃРїРёСЃРѕРє", employeeName: "Р†Рј'СЏ СЃРїС–РІСЂРѕР±С–С‚РЅРёРєР°", login: "Р›РѕРіС–РЅ", password: "РџР°СЂРѕР»СЊ", newPassword: "РќРѕРІРёР№ РїР°СЂРѕР»СЊ, СЏРєС‰Рѕ РїРѕС‚СЂС–Р±РЅРѕ", add: "Р”РѕРґР°С‚Рё", tasks: "Р—Р°РІРґР°РЅСЊ", report: "Р—РІС–С‚", calculate: "Р РѕР·СЂР°С…СѓРІР°С‚Рё", edit: "Р РµРґР°РіСѓРІР°С‚Рё", save: "Р—Р±РµСЂРµРіС‚Рё", deleteEmployee: "Р’РёРґР°Р»РёС‚Рё СЃРїС–РІСЂРѕР±С–С‚РЅРёРєР°", loadingReport: "Р—Р°РІР°РЅС‚Р°Р¶СѓСЋ Р·РІС–С‚...", reportError: "РќРµ РІРґР°Р»РѕСЃСЏ Р·Р°РІР°РЅС‚Р°Р¶РёС‚Рё Р·РІС–С‚: ", saveError: "РќРµ РІРґР°Р»РѕСЃСЏ Р·Р±РµСЂРµРіС‚Рё СЃРїС–РІСЂРѕР±С–С‚РЅРёРєР°: ", addError: "РќРµ РІРґР°Р»РѕСЃСЏ РґРѕРґР°С‚Рё СЃРїС–РІСЂРѕР±С–С‚РЅРёРєР°: ", settleError: "РќРµ РІРґР°Р»РѕСЃСЏ РІРёРєРѕРЅР°С‚Рё СЂРѕР·СЂР°С…СѓРЅРѕРє: ", deleteUserError: "РќРµ РІРґР°Р»РѕСЃСЏ РІРёРґР°Р»РёС‚Рё СЃРїС–РІСЂРѕР±С–С‚РЅРёРєР°: ", confirmPassword: "РџР°СЂРѕР»СЊ РїС–РґС‚РІРµСЂРґР¶РµРЅРЅСЏ", detailedReport: "Р”РѕРєР»Р°РґРЅРёР№ Р·РІС–С‚", history: "Р†СЃС‚РѕСЂС–СЏ СЂРѕР·СЂР°С…СѓРЅРєС–РІ", settlement: "Р РѕР·СЂР°С…СѓРЅРѕРє", completed: "Р’РёРєРѕРЅР°РЅРѕ", refused: "Р’С–РґРјРѕРІРёРІСЃСЏ", accepted: "РЈ СЂРѕР±РѕС‚С–", allAssigned: "РЈСЃСЊРѕРіРѕ РїСЂРёР·РЅР°С‡РµРЅРѕ", completedSum: "РЎСѓРјР° РІРёРєРѕРЅР°РЅРёС…", refusedSum: "РЎСѓРјР° РІС–РґРјРѕРІ", reserve: "Р РµР·РµСЂРІ", payout: "РЎСѓРјР° РґРѕ РІРёРїР»Р°С‚Рё", completedWorks: "Р’РёРєРѕРЅР°РЅС– СЂРѕР±РѕС‚Рё", refusedWorks: "Р’С–РґРјРѕРІР»РµРЅС– СЂРѕР±РѕС‚Рё", otherWorks: "Р†РЅС€С– РїСЂРёР·РЅР°С‡РµРЅС– СЂРѕР±РѕС‚Рё", noRecords: "Р—Р°РїРёСЃС–РІ РЅРµРјР°С”.", address: "РђРґСЂРµСЃР°", price: "Р¦С–РЅР°", status: "РЎС‚Р°С‚СѓСЃ", created: "РЎС‚РІРѕСЂРµРЅРѕ", changed: "Р—РјС–РЅРµРЅРѕ", noDate: "РЅРµРјР°С” РґР°С‚Рё", statusCompleted: "Р’РёРєРѕРЅР°РЅРѕ", statusRefused: "Р’С–РґРјРѕРІРёРІСЃСЏ", statusAccepted: "РџСЂРёР№РЅСЏС‚Рѕ", statusDeclined: "Р’С–РґС…РёР»РµРЅРѕ", statusNew: "РќРѕРІРµ" },
      pl: { employees: "Pracownicy", backHome: "WrГіД‡ do ekranu gЕ‚Гіwnego", refreshList: "OdЕ›wieЕј listД™", employeeName: "ImiД™ pracownika", login: "Login", password: "HasЕ‚o", newPassword: "Nowe hasЕ‚o, jeЕ›li potrzebne", add: "Dodaj", tasks: "ZadaЕ„", report: "Raport", calculate: "Rozlicz", edit: "Edytuj", save: "Zapisz", deleteEmployee: "UsuЕ„ pracownika", loadingReport: "ЕЃadujД™ raport...", reportError: "Nie udaЕ‚o siД™ zaЕ‚adowaД‡ raportu: ", saveError: "Nie udaЕ‚o siД™ zapisaД‡ pracownika: ", addError: "Nie udaЕ‚o siД™ dodaД‡ pracownika: ", settleError: "Nie udaЕ‚o siД™ rozliczyД‡: ", deleteUserError: "Nie udaЕ‚o siД™ usunД…Д‡ pracownika: ", confirmPassword: "HasЕ‚o potwierdzenia", detailedReport: "SzczegГіЕ‚owy raport", history: "Historia rozliczeЕ„", settlement: "Rozliczenie", completed: "Wykonane", refused: "OdmГіwione", accepted: "W trakcie", allAssigned: "ЕЃД…cznie przypisane", completedSum: "Suma wykonanych", refusedSum: "Suma odmГіwionych", reserve: "Rezerwa", payout: "Kwota do wypЕ‚aty", completedWorks: "Wykonane prace", refusedWorks: "OdmГіwione prace", otherWorks: "Inne przypisane prace", noRecords: "Brak wpisГіw.", address: "Adres", price: "Cena", status: "Status", created: "Utworzono", changed: "Zmieniono", noDate: "brak daty", statusCompleted: "Wykonane", statusRefused: "OdmГіwione", statusAccepted: "PrzyjД™te", statusDeclined: "Odrzucone", statusNew: "Nowe" }
    };
    function setLanguage(value) {
      language = value;
      localStorage.setItem("language", language);
      editingUserId = null;
      openReportId = null;
      applyLanguage();
      loadUsers();
    }
    function applyLanguage() {
      document.querySelector("#languageSelect").value = language;
      const fallbackTexts = {
        calculateAll: { ru: "Р Р°СЃСЃС‡РёС‚Р°С‚СЊ РІСЃРµС…", en: "Calculate all", uk: "Р РѕР·СЂР°С…СѓРІР°С‚Рё РІСЃС–С…", pl: "Rozlicz wszystkich" }
      };
      document.querySelectorAll("[data-i18n]").forEach(el => {
        const key = el.dataset.i18n;
        const value = texts[language][key] || (fallbackTexts[key] && fallbackTexts[key][language]);
        if (value) el.textContent = value;
      });
      document.querySelectorAll("[data-placeholder]").forEach(el => el.placeholder = texts[language][el.dataset.placeholder]);
    }
    let adminPassword = sessionStorage.getItem("adminPassword") || "";
    function adminHeaders(extra = {}) {
      if (!adminPassword) {
        adminPassword = prompt("Admin password") || "";
        sessionStorage.setItem("adminPassword", adminPassword);
      }
      return { "X-Admin-Password": adminPassword, "X-Language": language, ...extra };
    }
    async function requireAdminAccess(start) {
      while (true) {
        if (!adminPassword) {
          adminPassword = prompt("Admin password") || "";
        }
        if (!adminPassword) {
          document.body.innerHTML = "";
          return;
        }
        sessionStorage.setItem("adminPassword", adminPassword);
        const res = await fetch("/api/admin/check-password", {
          headers: { "X-Admin-Password": adminPassword, "X-Language": language }
        });
        if (res.ok) {
          document.body.classList.remove("locked");
          start();
          return;
        }
        sessionStorage.removeItem("adminPassword");
        adminPassword = "";
      }
    }
    async function changeAdminPassword() {
      const oldPassword = prompt(texts[language].oldPassword || "РЎС‚Р°СЂС‹Р№ РїР°СЂРѕР»СЊ");
      if (!oldPassword) return;
      const oldPasswordRepeat = prompt(texts[language].oldPasswordRepeat || "РџРѕРІС‚РѕСЂРёС‚Рµ СЃС‚Р°СЂС‹Р№ РїР°СЂРѕР»СЊ");
      if (!oldPasswordRepeat) return;
      const newPassword = prompt(texts[language].newAdminPassword || "Р’РІРµРґРёС‚Рµ РЅРѕРІС‹Р№ РїР°СЂРѕР»СЊ");
      if (!newPassword) return;
      const res = await fetch("/api/admin/change-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ oldPassword, oldPasswordRepeat, newPassword })
      });
      if (!res.ok) {
        const data = await res.json();
        alert((texts[language].changePasswordError || "РќРµ СѓРґР°Р»РѕСЃСЊ РёР·РјРµРЅРёС‚СЊ РїР°СЂРѕР»СЊ: ") + data.error);
        return;
      }
      adminPassword = newPassword;
      sessionStorage.setItem("adminPassword", adminPassword);
      alert(texts[language].passwordChanged || "РџР°СЂРѕР»СЊ РёР·РјРµРЅРµРЅ");
    }
    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    }
    async function loadUsers() {
      if (editingUserId !== null || openReportId !== null) {
        return;
      }
      const res = await fetch("/api/admin/users", { headers: adminHeaders() });
      if (res.status === 401) {
        sessionStorage.removeItem("adminPassword");
        adminPassword = "";
        alert("Wrong admin password");
        return;
      }
      const data = await res.json();
      users.innerHTML = data.users.map(u => `
        <article class="user">
          <div class="userHeader">
            <div>
              <strong>${escapeHtml(u.displayName)}</strong>
              <div class="meta">${texts[language].login}: ${escapeHtml(u.login)} В· ${texts[language].tasks}: ${u.taskCount}</div>
            </div>
            <div class="userActions">
              ${appSettings.showPrices ? `<div class="userMoneyMini"><strong>${formatMoney(u.payoutPrice ?? u.totals?.payoutPrice ?? 0)}</strong><span>${texts[language].payout}</span></div>` : ""}
              ${appSettings.showPrices ? `<div class="userMoneyMini"><strong>${formatReserve(u.reservePrice ?? u.totals?.reservePrice ?? 0)}</strong><span>${texts[language].reserve}</span></div>` : ""}
              <button class="secondary" type="button" onclick="toggleReport(${u.id})">${texts[language].report}</button>
              <button class="secondary" type="button" onclick="settleUser(${u.id})">${texts[language].calculate}</button>
              <button class="secondary" type="button" onclick="toggleEdit(${u.id})">${texts[language].edit}</button>
            </div>
          </div>
          <form class="editForm" id="edit-${u.id}" style="display:none" onsubmit="saveUser(event, ${u.id})">
            <input name="displayName" value="${escapeAttr(u.displayName)}" placeholder="${texts[language].employeeName}" required>
            <input name="login" value="${escapeAttr(u.login)}" placeholder="${texts[language].login}" required>
            <input name="password" value="" placeholder="${texts[language].newPassword}">
            <button>${texts[language].save}</button>
            <button class="danger" type="button" onclick="deleteUser(${u.id})">${texts[language].deleteEmployee}</button>
          </form>
          <div class="report" id="report-${u.id}" style="display:none"></div>
        </article>
      `).join("");
    }
    function refreshUsersList() {
      editingUserId = null;
      openReportId = null;
      loadUsers();
    }
    function escapeAttr(value) {
      return escapeHtml(value).replace(/"/g, "&quot;");
    }
    function toggleEdit(id) {
      const form = document.querySelector("#edit-" + id);
      const willOpen = form.style.display === "none";
      document.querySelectorAll(".editForm").forEach(item => item.style.display = "none");
      form.style.display = willOpen ? "grid" : "none";
      editingUserId = willOpen ? id : null;
    }
    async function toggleReport(id) {
      const panel = document.querySelector("#report-" + id);
      const isOpen = panel.style.display !== "none";
      document.querySelectorAll(".report").forEach(item => item.style.display = "none");
      if (isOpen) {
        panel.style.display = "none";
        openReportId = null;
        return;
      }
      panel.style.display = "block";
      openReportId = id;
      panel.innerHTML = "<p class=\"meta\">" + texts[language].loadingReport + "</p>";
      const res = await fetch("/api/admin/users/" + id + "/report", { headers: adminHeaders() });
      if (!res.ok) {
        const data = await res.json();
        panel.innerHTML = "<p>" + texts[language].reportError + escapeHtml(data.error) + "</p>";
        return;
      }
      const report = await res.json();
      panel.innerHTML = renderReport(report);
    }
    function renderReport(report) {
      return `
        <h3>${texts[language].detailedReport}: ${escapeHtml(report.user.displayName)}</h3>
        <div class="reportStats">
          <div class="statBox"><strong>${report.counts.completed}</strong><span>${texts[language].completed}</span></div>
          <div class="statBox"><strong>${report.counts.refused}</strong><span>${texts[language].refused}</span></div>
          <div class="statBox"><strong>${report.counts.accepted}</strong><span>${texts[language].accepted}</span></div>
          <div class="statBox"><strong>${report.counts.all}</strong><span>${texts[language].allAssigned}</span></div>
        </div>
        <div class="reportStats moneyStats">
          ${appSettings.showPrices ? `<div class="statBox"><strong>${formatMoney(report.totals.completedPrice)}</strong><span>${texts[language].completedSum}</span></div>` : ""}
          ${appSettings.showPrices ? `<div class="statBox"><strong>${formatMoney(report.totals.refusedPrice)}</strong><span>${texts[language].refusedSum}</span></div>` : ""}
          ${appSettings.showPrices ? renderReserveBox(report) : ""}
          ${appSettings.showPrices ? `<div class="statBox"><strong>${formatMoney(report.totals.payoutPrice ?? report.totals.completedPrice ?? 0)}</strong><span>${texts[language].payout}</span></div>` : ""}
        </div>
        ${appSettings.showPrices ? renderReserveTopUp(report.user.id) : ""}
        ${renderReserveEvents(report.currentReserveEvents || [])}
        ${renderTaskSection(texts[language].otherWorks, report.other, "other")}
        ${renderTaskSection(texts[language].completedWorks, report.completed, "completed")}
        ${renderTaskSection(texts[language].refusedWorks, report.refused, "refused")}
        ${renderHistory(report.history || [])}
      `;
    }
    function renderReserveBox(report) {
      const reserve = report.totals.reservePrice ?? 0;
      return `
        <div class="statBox reserveBox">
          <strong>${formatReserve(reserve)}</strong>
          <div class="reserveControls">
            <button class="secondary" type="button" onclick="reserveTransfer(${report.user.id}, 'from_reserve')">-</button>
            <span>${texts[language].reserve}</span>
            <button class="secondary" type="button" onclick="reserveTransfer(${report.user.id}, 'to_reserve')">+</button>
          </div>
        </div>
      `;
    }
    function renderReserveTopUp(userId) {
      return `
        <section class="reportSection">
          <h4>РџРѕРїРѕР»РЅРёС‚СЊ СЂРµР·РµСЂРІ</h4>
          <form onsubmit="reserveTopUp(event, ${userId})">
            <input name="amount" type="number" min="0.01" step="0.01" placeholder="РЎСѓРјРјР°" required>
            <button type="submit">РџРѕРїРѕР»РЅРёС‚СЊ</button>
          </form>
        </section>
      `;
    }
    function reserveEventName(kind) {
      if (kind === "to_reserve") return "РР· СЃСѓРјРјС‹ Рє РІС‹РїР»Р°С‚Рµ РІ СЂРµР·РµСЂРІ";
      if (kind === "from_reserve") return "РР· СЂРµР·РµСЂРІР° РІ РІС‹РїР»Р°С‚Сѓ";
      if (kind === "top_up") return "РџРѕРїРѕР»РЅРµРЅРёРµ СЂРµР·РµСЂРІР°";
      if (kind === "refused_fee_from_reserve") return "РЈРґРµСЂР¶Р°РЅРёРµ Р·Р° РѕС‚РєР°Р· РёР· СЂРµР·РµСЂРІР°";
      if (kind === "cash_completed_fee_from_reserve") return "РЈРґРµСЂР¶Р°РЅРёРµ Р·Р° РЅР°Р»РёС‡РЅС‹Рµ РёР· СЂРµР·РµСЂРІР°";
      return kind;
    }
    function renderReserveEvents(events) {
      if (!events.length) {
        return "";
      }
      return `
        <section class="reportSection">
          <h4>РћРїРµСЂР°С†РёРё СЂРµР·РµСЂРІР°</h4>
          ${events.map(event => `
            <p class="meta">${formatDate(event.createdAt)} В· ${reserveEventName(event.kind)} В· ${formatReserve(event.absoluteAmount ?? Math.abs(event.amount || 0))}</p>
          `).join("")}
        </section>
      `;
    }
    async function reserveTransfer(userId, action) {
      const amount = prompt("РЎСѓРјРјР°");
      if (!amount) return;
      const password = prompt(texts[language].confirmPassword);
      if (!password) return;
      const res = await fetch("/api/admin/users/" + userId + "/reserve", {
        method: "POST",
        headers: adminHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ action, amount, password })
      });
      if (!res.ok) {
        const data = await res.json();
        alert("РќРµ СѓРґР°Р»РѕСЃСЊ РёР·РјРµРЅРёС‚СЊ СЂРµР·РµСЂРІ: " + (data.error || res.status));
        return;
      }
      const panel = document.querySelector("#report-" + userId);
      const data = await res.json();
      panel.innerHTML = renderReport(data.report);
    }
    async function reserveTopUp(event, userId) {
      event.preventDefault();
      const amount = event.target.amount.value;
      const password = prompt(texts[language].confirmPassword);
      if (!password) return;
      const res = await fetch("/api/admin/users/" + userId + "/reserve", {
        method: "POST",
        headers: adminHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ action: "top_up", amount, password })
      });
      if (!res.ok) {
        const data = await res.json();
        alert("РќРµ СѓРґР°Р»РѕСЃСЊ РїРѕРїРѕР»РЅРёС‚СЊ СЂРµР·РµСЂРІ: " + (data.error || res.status));
        return;
      }
      const panel = document.querySelector("#report-" + userId);
      const data = await res.json();
      panel.innerHTML = renderReport(data.report);
    }
    function renderHistory(history) {
      if (!history.length) {
        return "";
      }
      return `
        <section class="reportSection">
          <h4>${texts[language].history}</h4>
          ${history.map(item => `
            <details class="settlement">
              <summary>${texts[language].settlement} #${item.id} В· ${formatDate(item.createdAt)} В· ${formatMoney(item.totals?.payoutPrice ?? item.totals?.completedPrice ?? 0)}</summary>
              ${renderTaskSection(texts[language].otherWorks, item.other || [], "other")}
              ${renderTaskSection(texts[language].completedWorks, item.completed || [], "completed")}
              ${renderTaskSection(texts[language].refusedWorks, item.refused || [], "refused")}
              ${renderReserveEvents(item.currentReserveEvents || item.reserveEvents || [])}
            </details>
          `).join("")}
        </section>
      `;
    }
    function renderTaskSection(title, tasks, type) {
      if (!tasks.length) {
        return `<section class="reportSection"><h4>${title}</h4><p class="meta">${texts[language].noRecords}</p></section>`;
      }
      return `
        <section class="reportSection">
          <h4>${title}</h4>
          ${tasks.map(task => `
            <article class="reportTask ${type}">
              <strong>#${task.id}</strong>
              <p><strong>${escapeHtml(task.title)}</strong></p>
              <p>${escapeHtml(task.description || "")}</p>
              <p>${task.phone ? "<strong>РўРµР»РµС„РѕРЅ:</strong> <a href=\"tel:" + phoneHref(task.phone) + "\">" + escapeHtml(task.phone) + "</a>" : ""}</p>
              <p>${task.address ? "<strong>" + texts[language].address + ":</strong> " + escapeHtml(task.address) : ""}</p>
              <p>${appSettings.showPrices ? "<strong>" + texts[language].price + ":</strong> " + formatMoney(task.price) : ""}</p>
              <p><strong>РћРїР»Р°С‚Р°:</strong> ${paymentMethodName(task.paymentMethod)}</p>
              <div class="meta">${texts[language].status}: ${statusName(task.status)} В· ${taskDatesMeta(task)}</div>
            </article>
          `).join("")}
        </section>
      `;
    }
    function statusName(status) {
      const names = { completed: texts[language].statusCompleted, refused: texts[language].statusRefused, accepted: texts[language].statusAccepted, declined: texts[language].statusDeclined, new: texts[language].statusNew };
      return names[status] || status;
    }
    function taskDatesMeta(task) {
      const rows = [];
      if (task.createdAt) rows.push(texts[language].created + ": " + formatDate(task.createdAt));
      if (task.acceptedAt) rows.push(texts[language].statusAccepted + ": " + formatDate(task.acceptedAt));
      if (task.completedAt) rows.push(texts[language].statusCompleted + ": " + formatDate(task.completedAt));
      return rows.length ? rows.join(" В· ") : texts[language].noDate;
    }
    function formatDate(value) {
      if (!value) {
        return texts[language].noDate;
      }
      return new Date(value * 1000).toLocaleString("ru-RU");
    }
    function formatMoney(value) {
      return new Intl.NumberFormat("ru-RU", { style: "currency", currency: appSettings.currency || "PLN" }).format(Number(value || 0));
    }
    function formatReserve(value) {
      const labels = { credits: "CRDT", tokens: "TKN", coins: "KOIN", points: "BAL" };
      return `${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 }).format(Number(value || 0))} ${labels[appSettings.reserveUnit] || labels.credits}`;
    }
    function calculationStatus(item) {
      return item && item.calculated ? "РћРїР»Р°С‡РµРЅРѕ" : "РќРµ РѕРїР»Р°С‡РµРЅРѕ";
    }
    function calculationClass(item) {
      return item && item.calculated ? " calculated" : "";
    }
    function calculateClientSettlementButton(item) {
      if (item.calculated) {
        return "";
      }
      return `<button class="success" type="button" onclick="calculateClientSettlement(${item.id})">РћРїР»Р°С‡РµРЅРѕ</button>`;
    }
    async function loadAppSettings() {
      const res = await fetch("/api/admin/settings", { headers: adminHeaders() });
      if (res.ok) {
        const data = await res.json();
        appSettings = data.settings || appSettings;
      }
    }
    function phoneHref(value) {
      return String(value).replace(/[^\d+]/g, "");
    }
    function paymentMethodName(method) {
      return method === "cash" ? "РќР°Р»РёС‡РЅС‹Рµ" : "РР· СЂРµР·РµСЂРІР°";
    }
    async function saveUser(event, id) {
      event.preventDefault();
      const form = event.target;
      const res = await fetch("/api/admin/users/" + id, {
        method: "POST",
        headers: adminHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({
          displayName: form.displayName.value,
          login: form.login.value,
          password: form.password.value
        })
      });
      if (!res.ok) {
        const data = await res.json();
        alert(texts[language].saveError + data.error);
        return;
      }
      editingUserId = null;
      loadUsers();
    }
    async function settleUser(id) {
      const password = prompt(texts[language].confirmPassword);
      if (!password) {
        return;
      }
      const res = await fetch("/api/admin/users/" + id + "/settle", {
        method: "POST",
        headers: adminHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ password })
      });
      if (!res.ok) {
        const data = await res.json();
        alert(texts[language].settleError + data.error);
        return;
      }
      openReportId = null;
      loadUsers();
    }
    async function settleAllUsers() {
      const password = prompt(texts[language].confirmPassword);
      if (!password) {
        return;
      }
      const res = await fetch("/api/admin/users-settle-all", {
        method: "POST",
        headers: adminHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ password })
      });
      if (!res.ok) {
        const data = await res.json();
        alert(texts[language].settleError + (data.error || res.status));
        return;
      }
      openReportId = null;
      editingUserId = null;
      loadUsers();
    }
    async function deleteUser(id) {
      const password = prompt(texts[language].confirmPassword);
      if (!password) {
        return;
      }
      const res = await fetch("/api/admin/users/" + id + "/delete", {
        method: "POST",
        headers: adminHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ password })
      });
      if (!res.ok) {
        const data = await res.json();
        alert(texts[language].deleteUserError + data.error);
        return;
      }
      editingUserId = null;
      openReportId = null;
      loadUsers();
    }
    document.querySelector("#userForm").addEventListener("submit", async event => {
      event.preventDefault();
      const res = await fetch("/api/admin/users", {
        method: "POST",
        headers: adminHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({
          displayName: userDisplayName.value,
          login: userLogin.value,
          password: userPassword.value
        })
      });
      if (!res.ok) {
        const data = await res.json();
        alert(texts[language].addError + data.error);
        return;
      }
      userDisplayName.value = "";
      userLogin.value = "";
      userPassword.value = "";
      loadUsers();
    });
    applyLanguage();
    requireAdminAccess(async () => {
      await loadAppSettings();
      loadUsers();
      setInterval(() => {
        if (editingUserId === null && openReportId === null) {
          loadUsers();
        }
      }, 30000);
    });
  </script>
</body>
</html>"""


CLIENTS_HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>РљР»РёРµРЅС‚С‹</title>
  <style>
    :root { --ink: #172026; --teal: #0f766e; --coral: #f9735b; --gold: #f6c85f; --mint: #d8f8eb; --paper: #fffaf0; }
    body { font-family: Arial, sans-serif; margin: 0; color: var(--ink); background: linear-gradient(135deg, #fff7df 0%, #d8f8eb 48%, #e9d5ff 100%); min-height: 100vh; }
    body.locked header, body.locked main { display: none; }
    header { background: linear-gradient(135deg, #7c3aed 0%, #2563eb 48%, #0f766e 100%); color: white; padding: 24px 28px; box-shadow: 0 18px 42px rgba(124, 58, 237, 0.22); }
    main { max-width: 980px; margin: 0 auto; padding: 24px; }
    form { display: grid; gap: 10px; grid-template-columns: 1fr 1fr 1fr auto; margin-bottom: 24px; }
    input, button { font: inherit; padding: 11px 13px; border-radius: 8px; border: 1px solid rgba(23, 32, 38, 0.18); }
    input { background: rgba(255, 255, 255, 0.94); }
    button { background: linear-gradient(135deg, var(--teal), #2563eb); color: white; border: 0; cursor: pointer; font-weight: 700; box-shadow: 0 10px 24px rgba(15, 118, 110, 0.2); }
    .secondary { background: #fff0bf; color: #4a3200; box-shadow: none; }
    .danger { background: #ef4444; color: white; box-shadow: none; }
    .client { background: rgba(255, 255, 255, 0.94); border-left: 6px solid var(--coral); border-radius: 8px; padding: 14px 16px; margin: 12px 0; box-shadow: 0 14px 34px rgba(23, 32, 38, 0.1); }
    .clientHeader { display: flex; justify-content: space-between; gap: 16px; align-items: center; }
    .actions { display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; align-items: center; }
    .clientMoneyMini { min-width: 148px; min-height: 44px; box-sizing: border-box; padding: 6px 12px; border-radius: 8px; background: white; border: 1px solid #eef2f4; display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; box-shadow: inset 0 0 0 1px rgba(15, 118, 110, 0.05); }
    .clientMoneyMini strong { font-size: 16px; line-height: 1.1; overflow-wrap: anywhere; }
    .clientMoneyMini span { margin-top: 2px; color: #60717d; font-size: 12px; line-height: 1.1; }
    .editForm { grid-template-columns: 1fr 1fr 1fr auto auto; margin: 14px 0 0; }
    .report { margin-top: 14px; padding: 16px; background: linear-gradient(135deg, #f8fbff, #fff7df); border: 1px solid rgba(23, 32, 38, 0.1); border-radius: 8px; }
    .reportStats { display: grid; grid-template-columns: repeat(4, minmax(110px, 1fr)); gap: 10px; margin-bottom: 16px; }
    .statBox { min-height: 76px; padding: 12px; background: white; border-radius: 8px; border: 1px solid #eef2f4; display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; }
    .statBox strong { display: block; font-size: 22px; line-height: 1.15; overflow-wrap: anywhere; }
    .statBox span { display: block; margin-top: 5px; color: #60717d; font-size: 14px; line-height: 1.25; }
    .reserveControls { display: grid; grid-template-columns: 34px 1fr 34px; gap: 8px; align-items: center; width: 100%; margin-top: 8px; }
    .reserveControls button { min-height: 32px; padding: 4px 0; }
    .reserveControls span { margin-top: 0; }
    .reportSection { margin-top: 14px; }
    .reportTask { padding: 12px; margin: 8px 0; background: white; border-radius: 8px; border-left: 5px solid var(--teal); }
    .reportTask.active { border-left-color: #f6c85f; }
    .reportTask.new { border-left-color: #60a5fa; }
    .reportTask.other { border-left-color: #94a3b8; }
    .meta { color: #60717d; font-size: 14px; }
    nav { display: grid; grid-template-columns: repeat(10, minmax(104px, 1fr)); gap: 10px; margin-top: 12px; max-width: 1320px; }
    nav a { display: flex; align-items: center; justify-content: center; min-height: 44px; box-sizing: border-box; color: var(--ink); background: var(--gold); font-weight: bold; padding: 8px 10px; border-radius: 8px; text-align: center; line-height: 1.15; text-decoration: none; }
    @media (max-width: 980px) { nav { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
    @media (max-width: 760px) { form, .editForm, .reportStats { grid-template-columns: 1fr; } .clientHeader { display: block; } .actions { justify-content: flex-start; margin-top: 10px; } }
  </style>
</head>
<body class="locked">
  <header>
    <h1>РљР»РёРµРЅС‚С‹</h1>
    <nav><a href="/server">Р—Р°РґР°РЅРёСЏ</a> <a href="/users">РЎРѕС‚СЂСѓРґРЅРёРєРё</a> <a href="/clients">РљР»РёРµРЅС‚С‹</a> <a href="/completed">Р’С‹РїРѕР»РЅРµРЅРЅС‹Рµ Р·Р°РґР°РЅРёСЏ</a> <a href="/calculations">Р Р°СЃС‡РµС‚С‹ СЃРѕС‚СЂСѓРґРЅРёРєРѕРІ</a> <a href="/client-calculations">Р Р°СЃС‡РµС‚С‹ РєР»РёРµРЅС‚РѕРІ</a> <a href="/telegram-ads">Р РµРєР»Р°РјР° Telegram</a> <a href="/facebook-ads">Р РµРєР»Р°РјР° Facebook</a> <a href="/telegram-login">Telegram userbot</a> <a href="/settings">РќР°СЃС‚СЂРѕР№РєРё</a></nav>
  </header>
  <main>
    <button class="secondary" type="button" onclick="refreshClientsList()">РћР±РЅРѕРІРёС‚СЊ СЃРїРёСЃРѕРє</button>
    <form id="clientForm">
      <input id="clientDisplayName" placeholder="РРјСЏ РєР»РёРµРЅС‚Р°" required>
      <input id="clientLogin" placeholder="РќРѕРјРµСЂ С‚РµР»РµС„РѕРЅР°" required>
      <input id="clientPassword" placeholder="РџР°СЂРѕР»СЊ" required>
      <button>Р”РѕР±Р°РІРёС‚СЊ</button>
    </form>
    <button class="secondary" type="button" onclick="settleAllClients()" data-i18n="calculateAll">Р Р°СЃСЃС‡РёС‚Р°С‚СЊ РІСЃРµС…</button>
    <section id="clients"></section>
  </main>
  <script>
    const clients = document.querySelector("#clients");
    let editingClientId = null;
    let openReportId = null;
    let appSettings = { currency: "PLN", reserveUnit: "credits", showPrices: true };
    let adminPassword = sessionStorage.getItem("adminPassword") || "";
    function adminHeaders(extra = {}) {
      if (!adminPassword) {
        adminPassword = prompt("Admin password") || "";
        sessionStorage.setItem("adminPassword", adminPassword);
      }
      return { "X-Admin-Password": adminPassword, "X-Language": currentLanguage(), ...extra };
    }
    async function requireAdminAccess(start) {
      while (true) {
        if (!adminPassword) adminPassword = prompt("Admin password") || "";
        if (!adminPassword) { document.body.innerHTML = ""; return; }
        sessionStorage.setItem("adminPassword", adminPassword);
        const res = await fetch("/api/admin/check-password", { headers: { "X-Admin-Password": adminPassword } });
        if (res.ok) { document.body.classList.remove("locked"); start(); return; }
        sessionStorage.removeItem("adminPassword");
        adminPassword = "";
      }
    }
    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    }
    function escapeAttr(value) {
      return escapeHtml(value).replace(/"/g, "&quot;");
    }
    const clientCalcTexts = {
      ru: {
        completed: "Р’С‹РїРѕР»РЅРµРЅРѕ", refused: "РћС‚РєР°Р·Р°Р»СЃСЏ", accepted: "РџСЂРёРЅСЏС‚Рѕ", declined: "РћС‚РєР»РѕРЅРµРЅРѕ", new: "РќРѕРІРѕРµ",
        paid: "РћРїР»Р°С‡РµРЅРѕ", unpaid: "РќРµ РѕРїР»Р°С‡РµРЅРѕ", settlementStatus: "РЎС‚Р°С‚СѓСЃ СЂР°СЃС‡РµС‚Р°", completedCount: "Р’С‹РїРѕР»РЅРµРЅРѕ",
        refusedCount: "РћС‚РєР°Р·Р°Р»СЃСЏ", activeCount: "Р’ СЂР°Р±РѕС‚Рµ", totalTasks: "Р’СЃРµРіРѕ Р·Р°РґР°РЅРёР№", settlementTotal: "РЎСѓРјРјР° СЂР°СЃС‡РµС‚Р°",
        fullReport: "РџРѕР»РЅС‹Р№ РѕС‚С‡РµС‚ РїРѕ СЂР°СЃС‡РµС‚Сѓ", completedWorks: "Р’С‹РїРѕР»РЅРµРЅРЅС‹Рµ СЂР°Р±РѕС‚С‹", activeWorks: "РђРєС‚РёРІРЅС‹Рµ СЂР°Р±РѕС‚С‹",
        newWorks: "РќРѕРІС‹Рµ СЂР°Р±РѕС‚С‹", refusedWorks: "РћС‚РєР°Р·Р°РЅРЅС‹Рµ СЂР°Р±РѕС‚С‹", otherWorks: "РћСЃС‚Р°Р»СЊРЅС‹Рµ СЂР°Р±РѕС‚С‹",
        reserveInSettlement: "Р РµР·РµСЂРІ РІ СЂР°СЃС‡РµС‚Рµ", reserveBefore: "Р РµР·РµСЂРІ РґРѕ СЂР°СЃС‡РµС‚Р°", reserveUsed: "РЎРїРёСЃР°РЅРѕ РёР· СЂРµР·РµСЂРІР°",
        reserveLeft: "РћСЃС‚Р°С‚РѕРє СЂРµР·РµСЂРІР°", amountDue: "РЎСѓРјРјР° Рє РѕРїР»Р°С‚Рµ", reserveOperations: "РћРїРµСЂР°С†РёРё СЂРµР·РµСЂРІР°",
        noReserveOperations: "РќРµС‚ РѕРїРµСЂР°С†РёР№ СЂРµР·РµСЂРІР°.", noRecords: "РќРµС‚ Р·Р°РїРёСЃРµР№.", phone: "РўРµР»РµС„РѕРЅ", address: "РђРґСЂРµСЃ",
        acceptedBy: "РљС‚Рѕ РїСЂРёРЅСЏР»", price: "Р¦РµРЅР°", payment: "РћРїР»Р°С‚Р°", status: "РЎС‚Р°С‚СѓСЃ", cash: "РќР°Р»РёС‡РЅС‹Рµ",
        fromReserve: "РР· СЂРµР·РµСЂРІР°", created: "РЎРѕР·РґР°РЅРѕ", acceptedAt: "РџСЂРёРЅСЏС‚Рѕ", completedAt: "Р’С‹РїРѕР»РЅРµРЅРѕ",
        noDate: "РЅРµС‚ РґР°С‚С‹", deleteSettlement: "РЈРґР°Р»РёС‚СЊ СЂР°СЃС‡РµС‚", noSettlements: "РЎРѕР·РґР°РЅРЅС‹С… СЂР°СЃС‡РµС‚РѕРІ РїРѕРєР° РЅРµС‚. РќРѕРІС‹Р№ СЂР°СЃС‡РµС‚ СЃРѕР·РґР°РµС‚СЃСЏ РІ СЂР°Р·РґРµР»Рµ В«РљР»РёРµРЅС‚С‹В» РєРЅРѕРїРєРѕР№ В«Р Р°СЃСЃС‡РёС‚Р°С‚СЊВ».",
        toReserve: "РР· СЃСѓРјРјС‹ Рє РѕРїР»Р°С‚Рµ РІ СЂРµР·РµСЂРІ", fromReserveToPay: "РР· СЂРµР·РµСЂРІР° РІ СЃСѓРјРјСѓ Рє РѕРїР»Р°С‚Рµ",
        topUp: "РџРѕРїРѕР»РЅРµРЅРёРµ СЂРµР·РµСЂРІР°", completedFromReserve: "Р’С‹РїРѕР»РЅРµРЅРЅС‹Рµ СЂР°Р±РѕС‚С‹ РёР· СЂРµР·РµСЂРІР°"
      },
      en: {
        completed: "Completed", refused: "Refused", accepted: "Accepted", declined: "Declined", new: "New",
        paid: "Paid", unpaid: "Not paid", settlementStatus: "Payment status", completedCount: "Completed",
        refusedCount: "Refused", activeCount: "In progress", totalTasks: "Total tasks", settlementTotal: "Payment total",
        fullReport: "Full payment report", completedWorks: "Completed jobs", activeWorks: "Active jobs",
        newWorks: "New jobs", refusedWorks: "Refused jobs", otherWorks: "Other jobs",
        reserveInSettlement: "Reserve in payment", reserveBefore: "Reserve before payment", reserveUsed: "Written off from reserve",
        reserveLeft: "Reserve left", amountDue: "Amount to pay", reserveOperations: "Reserve operations",
        noReserveOperations: "No reserve operations.", noRecords: "No records.", phone: "Phone", address: "Address",
        acceptedBy: "Accepted by", price: "Price", payment: "Payment", status: "Status", cash: "Cash",
        fromReserve: "From reserve", created: "Created", acceptedAt: "Accepted", completedAt: "Completed",
        noDate: "no date", deleteSettlement: "Delete payment", noSettlements: "No client payments have been created yet. Create a new payment in Clients with the Calculate button.",
        toReserve: "From amount to pay to reserve", fromReserveToPay: "From reserve to amount to pay",
        topUp: "Reserve top-up", completedFromReserve: "Completed jobs from reserve"
      },
      uk: {
        completed: "Р’РёРєРѕРЅР°РЅРѕ", refused: "Р’С–РґРјРѕРІРёРІСЃСЏ", accepted: "РџСЂРёР№РЅСЏС‚Рѕ", declined: "Р’С–РґС…РёР»РµРЅРѕ", new: "РќРѕРІРµ",
        paid: "РћРїР»Р°С‡РµРЅРѕ", unpaid: "РќРµ РѕРїР»Р°С‡РµРЅРѕ", settlementStatus: "РЎС‚Р°С‚СѓСЃ СЂРѕР·СЂР°С…СѓРЅРєСѓ", completedCount: "Р’РёРєРѕРЅР°РЅРѕ",
        refusedCount: "Р’С–РґРјРѕРІРёРІСЃСЏ", activeCount: "РЈ СЂРѕР±РѕС‚С–", totalTasks: "РЈСЃСЊРѕРіРѕ Р·Р°РІРґР°РЅСЊ", settlementTotal: "РЎСѓРјР° СЂРѕР·СЂР°С…СѓРЅРєСѓ",
        fullReport: "РџРѕРІРЅРёР№ Р·РІС–С‚ Р·Р° СЂРѕР·СЂР°С…СѓРЅРєРѕРј", completedWorks: "Р’РёРєРѕРЅР°РЅС– СЂРѕР±РѕС‚Рё", activeWorks: "РђРєС‚РёРІРЅС– СЂРѕР±РѕС‚Рё",
        newWorks: "РќРѕРІС– СЂРѕР±РѕС‚Рё", refusedWorks: "Р’С–РґРјРѕРІР»РµРЅС– СЂРѕР±РѕС‚Рё", otherWorks: "Р†РЅС€С– СЂРѕР±РѕС‚Рё",
        reserveInSettlement: "Р РµР·РµСЂРІ Сѓ СЂРѕР·СЂР°С…СѓРЅРєСѓ", reserveBefore: "Р РµР·РµСЂРІ РґРѕ СЂРѕР·СЂР°С…СѓРЅРєСѓ", reserveUsed: "РЎРїРёСЃР°РЅРѕ Р· СЂРµР·РµСЂРІСѓ",
        reserveLeft: "Р—Р°Р»РёС€РѕРє СЂРµР·РµСЂРІСѓ", amountDue: "РЎСѓРјР° РґРѕ РѕРїР»Р°С‚Рё", reserveOperations: "РћРїРµСЂР°С†С–С— СЂРµР·РµСЂРІСѓ",
        noReserveOperations: "РћРїРµСЂР°С†С–Р№ СЂРµР·РµСЂРІСѓ РЅРµРјР°С”.", noRecords: "Р—Р°РїРёСЃС–РІ РЅРµРјР°С”.", phone: "РўРµР»РµС„РѕРЅ", address: "РђРґСЂРµСЃР°",
        acceptedBy: "РљРёРј РїСЂРёР№РЅСЏС‚Рѕ", price: "Р¦С–РЅР°", payment: "РћРїР»Р°С‚Р°", status: "РЎС‚Р°С‚СѓСЃ", cash: "Р“РѕС‚С–РІРєР°",
        fromReserve: "Р— СЂРµР·РµСЂРІСѓ", created: "РЎС‚РІРѕСЂРµРЅРѕ", acceptedAt: "РџСЂРёР№РЅСЏС‚Рѕ", completedAt: "Р’РёРєРѕРЅР°РЅРѕ",
        noDate: "РЅРµРјР°С” РґР°С‚Рё", deleteSettlement: "Р’РёРґР°Р»РёС‚Рё СЂРѕР·СЂР°С…СѓРЅРѕРє", noSettlements: "РЎС‚РІРѕСЂРµРЅРёС… СЂРѕР·СЂР°С…СѓРЅРєС–РІ РїРѕРєРё РЅРµРјР°С”. РќРѕРІРёР№ СЂРѕР·СЂР°С…СѓРЅРѕРє СЃС‚РІРѕСЂСЋС”С‚СЊСЃСЏ РІ СЂРѕР·РґС–Р»С– В«РљР»С–С”РЅС‚РёВ» РєРЅРѕРїРєРѕСЋ В«Р РѕР·СЂР°С…СѓРІР°С‚РёВ».",
        toReserve: "Р†Р· СЃСѓРјРё РґРѕ РѕРїР»Р°С‚Рё РІ СЂРµР·РµСЂРІ", fromReserveToPay: "Р— СЂРµР·РµСЂРІСѓ РІ СЃСѓРјСѓ РґРѕ РѕРїР»Р°С‚Рё",
        topUp: "РџРѕРїРѕРІРЅРµРЅРЅСЏ СЂРµР·РµСЂРІСѓ", completedFromReserve: "Р’РёРєРѕРЅР°РЅС– СЂРѕР±РѕС‚Рё Р· СЂРµР·РµСЂРІСѓ"
      },
      pl: {
        completed: "Wykonane", refused: "OdmГіwione", accepted: "PrzyjД™te", declined: "Odrzucone", new: "Nowe",
        paid: "OpЕ‚acono", unpaid: "Nie opЕ‚acono", settlementStatus: "Status rozliczenia", completedCount: "Wykonane",
        refusedCount: "OdmГіwione", activeCount: "W trakcie", totalTasks: "ЕЃД…cznie zadaЕ„", settlementTotal: "Suma rozliczenia",
        fullReport: "PeЕ‚ny raport rozliczenia", completedWorks: "Wykonane prace", activeWorks: "Aktywne prace",
        newWorks: "Nowe prace", refusedWorks: "OdmГіwione prace", otherWorks: "PozostaЕ‚e prace",
        reserveInSettlement: "Rezerwa w rozliczeniu", reserveBefore: "Rezerwa przed rozliczeniem", reserveUsed: "Pobrano z rezerwy",
        reserveLeft: "PozostaЕ‚a rezerwa", amountDue: "Kwota do zapЕ‚aty", reserveOperations: "Operacje rezerwy",
        noReserveOperations: "Brak operacji rezerwy.", noRecords: "Brak wpisГіw.", phone: "Telefon", address: "Adres",
        acceptedBy: "Kto przyjД…Е‚", price: "Cena", payment: "PЕ‚atnoЕ›Д‡", status: "Status", cash: "GotГіwka",
        fromReserve: "Z rezerwy", created: "Utworzono", acceptedAt: "PrzyjД™to", completedAt: "Wykonano",
        noDate: "brak daty", deleteSettlement: "UsuЕ„ rozliczenie", noSettlements: "Nie ma jeszcze utworzonych rozliczeЕ„ klientГіw. Nowe rozliczenie utworzysz w sekcji вЂћKlienciвЂќ przyciskiem вЂћRozliczвЂќ.",
        toReserve: "Z kwoty do zapЕ‚aty do rezerwy", fromReserveToPay: "Z rezerwy do kwoty do zapЕ‚aty",
        topUp: "DoЕ‚adowanie rezerwy", completedFromReserve: "Wykonane prace z rezerwy"
      }
    };
    function currentLanguage() {
      return localStorage.getItem("language") || "ru";
    }
    function text(key) {
      const language = currentLanguage();
      return (clientCalcTexts[language] && clientCalcTexts[language][key]) || clientCalcTexts.ru[key] || key;
    }
    function localeName() {
      const locales = { ru: "ru-RU", en: "en-US", uk: "uk-UA", pl: "pl-PL" };
      return locales[currentLanguage()] || "ru-RU";
    }
    function formatMoney(value) {
      return new Intl.NumberFormat(localeName(), { style: "currency", currency: appSettings.currency || "PLN" }).format(Number(value || 0));
    }
    function formatReserve(value) {
      const labels = { credits: "CRDT", tokens: "TKN", coins: "KOIN", points: "BAL" };
      return `${new Intl.NumberFormat(localeName(), { maximumFractionDigits: 2 }).format(Number(value || 0))} ${labels[appSettings.reserveUnit] || labels.credits}`;
    }
    function formatDate(value) {
      if (!value) return text("noDate");
      return new Date(value * 1000).toLocaleString(localeName());
    }
    function phoneHref(value) {
      return String(value).replace(/[^\d+]/g, "");
    }
    function paymentMethodName(method) {
      return method === "cash" ? text("cash") : text("fromReserve");
    }
    function taskStatusName(status) {
      return text(status) || status || "";
    }
    function taskDatesMeta(task) {
      const rows = [];
      if (task.createdAt) rows.push(text("created") + ": " + formatDate(task.createdAt));
      if (task.acceptedAt) rows.push(text("acceptedAt") + ": " + formatDate(task.acceptedAt));
      if (task.completedAt) rows.push(text("completedAt") + ": " + formatDate(task.completedAt));
      return rows.length ? rows.join(" В· ") : text("noDate");
    }
    async function loadAppSettings() {
      const res = await fetch("/api/admin/settings", { headers: adminHeaders() });
      if (res.ok) {
        const data = await res.json();
        appSettings = data.settings || appSettings;
      }
    }
    async function apiErrorMessage(res) {
      try {
        const data = await res.json();
        if (data.error === "admin_unauthorized") return "РќСѓР¶РЅРѕ Р·Р°РЅРѕРІРѕ РІРІРµСЃС‚Рё РїР°СЂРѕР»СЊ Р°РґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂР°.";
        if (data.error === "login_name_required") return "Р—Р°РїРѕР»РЅРёС‚Рµ РёРјСЏ РєР»РёРµРЅС‚Р° Рё Р»РѕРіРёРЅ.";
        if (data.error === "password_too_short") return "РџР°СЂРѕР»СЊ РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ РЅРµ РєРѕСЂРѕС‡Рµ 4 СЃРёРјРІРѕР»РѕРІ.";
        if (data.error === "login_already_exists") return "РўР°РєРѕР№ Р»РѕРіРёРЅ СѓР¶Рµ РёСЃРїРѕР»СЊР·СѓРµС‚СЃСЏ.";
        if (data.error === "client_not_found") return "РљР»РёРµРЅС‚ РЅРµ РЅР°Р№РґРµРЅ.";
        return data.error || res.statusText;
      } catch (error) {
        return res.statusText || "РќРµРёР·РІРµСЃС‚РЅР°СЏ РѕС€РёР±РєР°";
      }
    }
    async function loadClients() {
      if (editingClientId !== null || openReportId !== null) return;
      const res = await fetch("/api/admin/clients", { headers: adminHeaders() });
      if (!res.ok) { clients.innerHTML = "<p>РќРµ СѓРґР°Р»РѕСЃСЊ Р·Р°РіСЂСѓР·РёС‚СЊ РєР»РёРµРЅС‚РѕРІ.</p>"; return; }
      const data = await res.json();
      clients.innerHTML = data.clients.map(client => `
        <article class="client">
          <div class="clientHeader">
            <div>
              <strong>${escapeHtml(client.displayName)}</strong>
              <div class="meta">РўРµР»РµС„РѕРЅ: ${escapeHtml(client.phone || client.login)} В· Р—Р°РґР°РЅРёР№: ${client.taskCount}</div>
            </div>
            <div class="actions">
              <div class="clientMoneyMini"><strong>${formatMoney(client.totalPrice || 0)}</strong><span>РЎСѓРјРјР° Рє РѕРїР»Р°С‚Рµ</span></div>
              <div class="clientMoneyMini"><strong>${formatReserve(client.reservePrice || 0)}</strong><span>Р РµР·РµСЂРІ</span></div>
              <button class="secondary" type="button" onclick="toggleClientReport(${client.id})">РћС‚С‡РµС‚</button>
              <button class="secondary" type="button" onclick="settleClientFromList(${client.id})">Р Р°СЃСЃС‡РёС‚Р°С‚СЊ</button>
              <button class="secondary" type="button" onclick="toggleEdit(${client.id})">Р РµРґР°РєС‚РёСЂРѕРІР°С‚СЊ</button>
            </div>
          </div>
          <form class="editForm" id="edit-${client.id}" style="display:none" onsubmit="saveClient(event, ${client.id})">
            <input name="displayName" value="${escapeAttr(client.displayName)}" placeholder="РРјСЏ РєР»РёРµРЅС‚Р°" required>
            <input name="login" value="${escapeAttr(client.phone || client.login)}" placeholder="РќРѕРјРµСЂ С‚РµР»РµС„РѕРЅР°" required>
            <input name="password" value="" placeholder="РќРѕРІС‹Р№ РїР°СЂРѕР»СЊ, РµСЃР»Рё РЅСѓР¶РЅРѕ">
            <button>РЎРѕС…СЂР°РЅРёС‚СЊ</button>
            <button class="danger" type="button" onclick="deleteClient(${client.id})">РЈРґР°Р»РёС‚СЊ РєР»РёРµРЅС‚Р°</button>
          </form>
          <div class="report" id="report-${client.id}" style="display:none"></div>
        </article>
      `).join("");
    }
    function refreshClientsList() {
      editingClientId = null;
      openReportId = null;
      loadClients();
    }
    function toggleEdit(id) {
      const form = document.querySelector("#edit-" + id);
      const willOpen = form.style.display === "none";
      document.querySelectorAll(".editForm").forEach(item => item.style.display = "none");
      form.style.display = willOpen ? "grid" : "none";
      editingClientId = willOpen ? id : null;
    }
    async function toggleClientReport(id) {
      const panel = document.querySelector("#report-" + id);
      const isOpen = panel.style.display !== "none";
      document.querySelectorAll(".report").forEach(item => item.style.display = "none");
      if (isOpen) {
        panel.style.display = "none";
        openReportId = null;
        return;
      }
      panel.style.display = "block";
      openReportId = id;
      panel.innerHTML = "<p class=\"meta\">Р—Р°РіСЂСѓР¶Р°СЋ РѕС‚С‡РµС‚...</p>";
      const res = await fetch("/api/admin/clients/" + id + "/report", { headers: adminHeaders() });
      if (!res.ok) {
        const data = await res.json();
        panel.innerHTML = "<p>РќРµ СѓРґР°Р»РѕСЃСЊ Р·Р°РіСЂСѓР·РёС‚СЊ РѕС‚С‡РµС‚: " + escapeHtml(data.error || res.status) + "</p>";
        return;
      }
      const report = await res.json();
      panel.innerHTML = renderClientListReport(report);
    }
    function renderClientListReport(report) {
      return `
        <h3>РџРѕРґСЂРѕР±РЅС‹Р№ РѕС‚С‡РµС‚: ${escapeHtml(report.client.displayName)}</h3>
        <div class="reportStats">
          <div class="statBox"><strong>${report.counts.completed || 0}</strong><span>Р’С‹РїРѕР»РЅРµРЅРѕ</span></div>
          <div class="statBox"><strong>${report.counts.refused || 0}</strong><span>РћС‚РєР°Р·Р°Р»СЃСЏ</span></div>
          <div class="statBox"><strong>${report.counts.active || 0}</strong><span>Р’ СЂР°Р±РѕС‚Рµ</span></div>
          <div class="statBox"><strong>${report.counts.all || 0}</strong><span>Р’СЃРµРіРѕ Р·Р°РґР°РЅРёР№</span></div>
        </div>
        <div class="reportStats">
          ${appSettings.showPrices ? `<div class="statBox"><strong>${formatMoney(report.totals.completedPrice || 0)}</strong><span>Р’С‹РїРѕР»РЅРµРЅРЅС‹Рµ СЂР°Р±РѕС‚С‹</span></div>` : ""}
          ${appSettings.showPrices ? `<div class="statBox"><strong>${formatMoney(report.totals.refusedPrice || 0)}</strong><span>РЎСѓРјРјР° РѕС‚РєР°Р·Р°РЅРЅС‹С…</span></div>` : ""}
          ${appSettings.showPrices ? renderClientReserveBox(report) : ""}
          ${appSettings.showPrices ? `<div class="statBox"><strong>${formatMoney(report.totals.activePaymentDue || 0)}</strong><span>РЎСѓРјРјР° Рє РѕРїР»Р°С‚Рµ</span></div>` : ""}
        </div>
        ${appSettings.showPrices ? renderClientReserveTopUp(report.client.id) : ""}
        ${renderClientReserveEvents(report.currentReserveEvents || [])}
        ${renderClientTaskSection(text("completedWorks"), report.completed || [], "completed")}
        ${renderClientTaskSection(text("activeCount"), report.active || [], "active")}
        ${renderClientTaskSection(text("newWorks"), report.new || [], "new")}
        ${renderClientTaskSection(text("refusedWorks"), report.refused || [], "refused")}
        ${renderClientTaskSection(text("otherWorks"), report.other || [], "other")}
      `;
    }
    function renderClientReserveBox(report) {
      const reserve = report.totals.reservePrice ?? 0;
      return `
        <div class="statBox reserveBox">
          <strong>${formatReserve(reserve)}</strong>
          <div class="reserveControls">
            <button class="secondary" type="button" disabled title="Р’СЂРµРјРµРЅРЅРѕ РѕС‚РєР»СЋС‡РµРЅРѕ">-</button>
            <span>${text("reserveInSettlement")}</span>
            <button class="secondary" type="button" disabled title="Р’СЂРµРјРµРЅРЅРѕ РѕС‚РєР»СЋС‡РµРЅРѕ">+</button>
          </div>
        </div>
      `;
    }
    function renderClientReserveTopUp(clientId) {
      return `
        <section class="reportSection">
          <h4>${text("topUp")}</h4>
          <form onsubmit="clientReserveTopUp(event, ${clientId})">
            <input name="amount" type="number" min="0.01" step="0.01" placeholder="${text("amountDue")}" required>
            <button type="submit">${text("topUp")}</button>
          </form>
        </section>
      `;
    }
    function clientReserveEventName(kind) {
      if (kind === "to_reserve") return text("toReserve");
      if (kind === "from_reserve") return text("fromReserveToPay");
      if (kind === "top_up") return text("topUp");
      if (kind === "completed_from_reserve") return text("completedFromReserve");
      return kind;
    }
    function renderClientReserveEvents(events) {
      if (!events.length) {
        return "";
      }
      return `
        <section class="reportSection">
          <h4>${text("reserveOperations")}</h4>
          ${events.map(event => `
            <p class="meta">${formatDate(event.createdAt)} В· ${clientReserveEventName(event.kind)} В· ${formatReserve(event.absoluteAmount ?? Math.abs(event.amount || 0))}</p>
          `).join("")}
        </section>
      `;
    }
    async function clientReserveTransfer(clientId, action) {
      const amount = prompt("РЎСѓРјРјР°");
      if (!amount) return;
      const password = prompt("РџР°СЂРѕР»СЊ РїРѕРґС‚РІРµСЂР¶РґРµРЅРёСЏ");
      if (!password) return;
      const res = await fetch("/api/admin/clients/" + clientId + "/reserve", {
        method: "POST",
        headers: adminHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ action, amount, password })
      });
      if (!res.ok) {
        const data = await res.json();
        alert("РќРµ СѓРґР°Р»РѕСЃСЊ РёР·РјРµРЅРёС‚СЊ СЂРµР·РµСЂРІ: " + (data.error || res.status));
        return;
      }
      const data = await res.json();
      const panel = document.querySelector("#report-" + clientId);
      panel.innerHTML = renderClientListReport(data.report);
    }
    async function clientReserveTopUp(event, clientId) {
      event.preventDefault();
      const amount = event.target.amount.value;
      const password = prompt("РџР°СЂРѕР»СЊ РїРѕРґС‚РІРµСЂР¶РґРµРЅРёСЏ");
      if (!password) return;
      const res = await fetch("/api/admin/clients/" + clientId + "/reserve", {
        method: "POST",
        headers: adminHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ action: "top_up", amount, password })
      });
      if (!res.ok) {
        const data = await res.json();
        alert("РќРµ СѓРґР°Р»РѕСЃСЊ РїРѕРїРѕР»РЅРёС‚СЊ СЂРµР·РµСЂРІ: " + (data.error || res.status));
        return;
      }
      const data = await res.json();
      const panel = document.querySelector("#report-" + clientId);
      panel.innerHTML = renderClientListReport(data.report);
    }
    function renderClientTaskSection(title, tasks, type) {
      if (!tasks.length) {
        return `<section class="reportSection"><h4>${title}</h4><p class="meta">${text("noRecords")}</p></section>`;
      }
      return `
        <section class="reportSection">
          <h4>${title}</h4>
          ${tasks.map(task => `
            <article class="reportTask ${type}">
              <strong>#${task.id}</strong>
              <p><strong>${escapeHtml(task.title)}</strong></p>
              <p>${escapeHtml(task.description || "")}</p>
              <p>${task.phone ? "<strong>" + text("phone") + ":</strong> <a href=\"tel:" + phoneHref(task.phone) + "\">" + escapeHtml(task.phone) + "</a>" : ""}</p>
              <p>${task.address ? "<strong>" + text("address") + ":</strong> " + escapeHtml(task.address) : ""}</p>
              <p>${task.assignedToName ? "<strong>" + text("acceptedBy") + ":</strong> " + escapeHtml(task.assignedToName) + (task.assignedToLogin ? " В· " + escapeHtml(task.assignedToLogin) : "") : ""}</p>
              <p>${appSettings.showPrices ? "<strong>" + text("price") + ":</strong> " + formatMoney(task.price || 0) : ""}</p>
              <p><strong>${text("payment")}:</strong> ${paymentMethodName(task.paymentMethod)}</p>
              <div class="meta">${text("status")}: ${taskStatusName(task.status)} В· ${taskDatesMeta(task)}</div>
            </article>
          `).join("")}
        </section>
      `;
    }
    async function settleClientFromList(id) {
      const password = prompt("РџР°СЂРѕР»СЊ РїРѕРґС‚РІРµСЂР¶РґРµРЅРёСЏ");
      if (!password) return;
      const res = await fetch("/api/admin/clients/" + id + "/settle", {
        method: "POST",
        headers: adminHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ password })
      });
      if (!res.ok) {
        const data = await res.json();
        alert("РќРµ СѓРґР°Р»РѕСЃСЊ РІС‹РїРѕР»РЅРёС‚СЊ СЂР°СЃС‡РµС‚: " + (data.error || res.status));
        return;
      }
      editingClientId = null;
      openReportId = null;
      loadClients();
    }
    async function settleAllClients() {
      const password = prompt("РџР°СЂРѕР»СЊ РїРѕРґС‚РІРµСЂР¶РґРµРЅРёСЏ");
      if (!password) return;
      const res = await fetch("/api/admin/clients-settle-all", {
        method: "POST",
        headers: adminHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ password })
      });
      if (!res.ok) {
        const data = await res.json();
        alert("РќРµ СѓРґР°Р»РѕСЃСЊ РІС‹РїРѕР»РЅРёС‚СЊ СЂР°СЃС‡РµС‚: " + (data.error || res.status));
        return;
      }
      editingClientId = null;
      openReportId = null;
      loadClients();
    }
    async function saveClient(event, id) {
      event.preventDefault();
      const form = event.target;
      const fields = form.elements;
      const res = await fetch("/api/admin/clients/" + id, {
        method: "POST",
        headers: adminHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({
          displayName: fields.displayName.value.trim(),
          login: fields.login.value.trim(),
          password: fields.password.value.trim()
        })
      });
      if (!res.ok) {
        const message = await apiErrorMessage(res);
        if (res.status === 401) {
          sessionStorage.removeItem("adminPassword");
          adminPassword = "";
        }
        alert("РќРµ СѓРґР°Р»РѕСЃСЊ СЃРѕС…СЂР°РЅРёС‚СЊ РєР»РёРµРЅС‚Р°: " + message);
        return;
      }
      editingClientId = null;
      openReportId = null;
      loadClients();
    }
    async function deleteClient(id) {
      const password = prompt("РџР°СЂРѕР»СЊ РїРѕРґС‚РІРµСЂР¶РґРµРЅРёСЏ");
      if (!password) return;
      const res = await fetch("/api/admin/clients/" + id + "/delete", {
        method: "POST",
        headers: adminHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ password })
      });
      if (!res.ok) { alert("РќРµ СѓРґР°Р»РѕСЃСЊ СѓРґР°Р»РёС‚СЊ РєР»РёРµРЅС‚Р°."); return; }
      editingClientId = null;
      openReportId = null;
      loadClients();
    }
    document.querySelector("#clientForm").addEventListener("submit", async event => {
      event.preventDefault();
      const res = await fetch("/api/admin/clients", {
        method: "POST",
        headers: adminHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ displayName: clientDisplayName.value, login: clientLogin.value, password: clientPassword.value })
      });
      if (!res.ok) { alert("РќРµ СѓРґР°Р»РѕСЃСЊ РґРѕР±Р°РІРёС‚СЊ РєР»РёРµРЅС‚Р°."); return; }
      clientDisplayName.value = "";
      clientLogin.value = "";
      clientPassword.value = "";
      loadClients();
    });
    requireAdminAccess(async () => {
      await loadAppSettings();
      loadClients();
    });
    document.addEventListener("change", event => {
      if (event.target && event.target.id === "languageSelect") {
        loadClients();
      }
    });
  </script>
</body>
</html>"""


CLIENT_CALCULATIONS_HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Р Р°СЃС‡РµС‚С‹ РєР»РёРµРЅС‚РѕРІ</title>
  <style>
    :root { --ink: #172026; --teal: #0f766e; --gold: #f6c85f; --paper: #fffaf0; }
    body { font-family: Arial, sans-serif; margin: 0; color: var(--ink); background: linear-gradient(135deg, #fff7df 0%, #d8f8eb 48%, #e9d5ff 100%); min-height: 100vh; }
    body.locked header, body.locked main { display: none; }
    header { background: linear-gradient(135deg, #0f766e 0%, #2563eb 48%, #7c3aed 100%); color: white; padding: 24px 28px; }
    main { max-width: 980px; margin: 0 auto; padding: 24px; }
    nav { display: grid; grid-template-columns: repeat(10, minmax(104px, 1fr)); gap: 10px; margin-top: 12px; max-width: 1320px; }
    nav a { display: flex; align-items: center; justify-content: center; min-height: 44px; box-sizing: border-box; color: var(--ink); background: var(--gold); font-weight: bold; padding: 8px 10px; border-radius: 8px; text-align: center; line-height: 1.15; text-decoration: none; }
    button { font: inherit; padding: 11px 13px; border-radius: 8px; border: 0; background: linear-gradient(135deg, var(--teal), #2563eb); color: white; cursor: pointer; font-weight: 700; }
    .item { background: #fee2e2; border-left: 6px solid #ef4444; border-radius: 8px; padding: 16px; margin: 12px 0; box-shadow: 0 14px 34px rgba(23,32,38,.1); }
    .item.calculated { background: #dcfce7; border-left-color: #16a34a; }
    .reportTask { padding: 12px; margin: 8px 0; background: white; border-radius: 8px; border-left: 5px solid var(--teal); }
    .reportTask.active { border-left-color: #f6c85f; }
    .reportTask.refused { border-left-color: #f9735b; }
    .reportTask.other { border-left-color: #94a3b8; }
    details { margin-top: 12px; padding: 12px; background: rgba(255,255,255,.72); border: 1px solid rgba(23,32,38,.1); border-radius: 8px; }
    summary { cursor: pointer; font-weight: 700; }
    .actions { display: flex; justify-content: flex-end; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
    .danger { background: #ef4444; box-shadow: none; }
    .success { background: #16a34a; box-shadow: none; }
    .status { display: inline-block; padding: 5px 10px; border-radius: 999px; background: #ef4444; color: white; font-weight: 700; }
    .item.calculated .status { background: #16a34a; }
    .meta { color: #60717d; font-size: 14px; }
    @media (max-width: 980px) { nav { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
  </style>
</head>
<body class="locked">
  <header>
    <h1>Р Р°СЃС‡РµС‚С‹ РєР»РёРµРЅС‚РѕРІ</h1>
    <nav><a href="/server">Р—Р°РґР°РЅРёСЏ</a> <a href="/users">РЎРѕС‚СЂСѓРґРЅРёРєРё</a> <a href="/clients">РљР»РёРµРЅС‚С‹</a> <a href="/completed">Р’С‹РїРѕР»РЅРµРЅРЅС‹Рµ Р·Р°РґР°РЅРёСЏ</a> <a href="/calculations">Р Р°СЃС‡РµС‚С‹ СЃРѕС‚СЂСѓРґРЅРёРєРѕРІ</a> <a href="/client-calculations">Р Р°СЃС‡РµС‚С‹ РєР»РёРµРЅС‚РѕРІ</a> <a href="/telegram-ads">Р РµРєР»Р°РјР° Telegram</a> <a href="/facebook-ads">Р РµРєР»Р°РјР° Facebook</a> <a href="/telegram-login">Telegram userbot</a> <a href="/settings">РќР°СЃС‚СЂРѕР№РєРё</a></nav>
  </header>
  <main>
    <section id="items"></section>
  </main>
  <script>
    const items = document.querySelector("#items");
    let appSettings = { currency: "PLN", reserveUnit: "credits", showPrices: true };
    let adminPassword = sessionStorage.getItem("adminPassword") || "";
    function adminHeaders(extra = {}) {
      if (!adminPassword) {
        adminPassword = prompt("Admin password") || "";
        sessionStorage.setItem("adminPassword", adminPassword);
      }
      return { "X-Admin-Password": adminPassword, "X-Language": currentLanguage(), ...extra };
    }
    async function requireAdminAccess(start) {
      while (true) {
        if (!adminPassword) adminPassword = prompt("Admin password") || "";
        if (!adminPassword) { document.body.innerHTML = ""; return; }
        sessionStorage.setItem("adminPassword", adminPassword);
        const res = await fetch("/api/admin/check-password", { headers: { "X-Admin-Password": adminPassword } });
        if (res.ok) { document.body.classList.remove("locked"); start(); return; }
        sessionStorage.removeItem("adminPassword");
        adminPassword = "";
      }
    }
    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    }
    const calcTexts = {
      ru: { completed: "Р’С‹РїРѕР»РЅРµРЅРѕ", refused: "РћС‚РєР°Р·Р°Р»СЃСЏ", accepted: "РџСЂРёРЅСЏС‚Рѕ", declined: "РћС‚РєР»РѕРЅРµРЅРѕ", new: "РќРѕРІРѕРµ", paid: "РћРїР»Р°С‡РµРЅРѕ", unpaid: "РќРµ РѕРїР»Р°С‡РµРЅРѕ", settlementStatus: "РЎС‚Р°С‚СѓСЃ СЂР°СЃС‡РµС‚Р°", completedCount: "Р’С‹РїРѕР»РЅРµРЅРѕ", refusedCount: "РћС‚РєР°Р·Р°Р»СЃСЏ", activeCount: "Р’ СЂР°Р±РѕС‚Рµ", totalTasks: "Р’СЃРµРіРѕ Р·Р°РґР°РЅРёР№", settlementTotal: "РЎСѓРјРјР° СЂР°СЃС‡РµС‚Р°", fullReport: "РџРѕР»РЅС‹Р№ РѕС‚С‡РµС‚ РїРѕ СЂР°СЃС‡РµС‚Сѓ", completedWorks: "Р’С‹РїРѕР»РЅРµРЅРЅС‹Рµ СЂР°Р±РѕС‚С‹", activeWorks: "РђРєС‚РёРІРЅС‹Рµ СЂР°Р±РѕС‚С‹", newWorks: "РќРѕРІС‹Рµ СЂР°Р±РѕС‚С‹", refusedWorks: "РћС‚РєР°Р·Р°РЅРЅС‹Рµ СЂР°Р±РѕС‚С‹", otherWorks: "РћСЃС‚Р°Р»СЊРЅС‹Рµ СЂР°Р±РѕС‚С‹", reserveInSettlement: "Р РµР·РµСЂРІ РІ СЂР°СЃС‡РµС‚Рµ", reserveBefore: "Р РµР·РµСЂРІ РґРѕ СЂР°СЃС‡РµС‚Р°", reserveUsed: "РЎРїРёСЃР°РЅРѕ РёР· СЂРµР·РµСЂРІР°", reserveLeft: "РћСЃС‚Р°С‚РѕРє СЂРµР·РµСЂРІР°", amountDue: "РЎСѓРјРјР° Рє РѕРїР»Р°С‚Рµ", reserveOperations: "РћРїРµСЂР°С†РёРё СЂРµР·РµСЂРІР°", noReserveOperations: "РќРµС‚ РѕРїРµСЂР°С†РёР№ СЂРµР·РµСЂРІР°.", noRecords: "РќРµС‚ Р·Р°РїРёСЃРµР№.", phone: "РўРµР»РµС„РѕРЅ", address: "РђРґСЂРµСЃ", acceptedBy: "РљС‚Рѕ РїСЂРёРЅСЏР»", price: "Р¦РµРЅР°", payment: "РћРїР»Р°С‚Р°", status: "РЎС‚Р°С‚СѓСЃ", cash: "РќР°Р»РёС‡РЅС‹Рµ", fromReserve: "РР· СЂРµР·РµСЂРІР°", created: "РЎРѕР·РґР°РЅРѕ", acceptedAt: "РџСЂРёРЅСЏС‚Рѕ", completedAt: "Р’С‹РїРѕР»РЅРµРЅРѕ", noDate: "РЅРµС‚ РґР°С‚С‹", deleteSettlement: "РЈРґР°Р»РёС‚СЊ СЂР°СЃС‡РµС‚", noSettlements: "РЎРѕР·РґР°РЅРЅС‹С… СЂР°СЃС‡РµС‚РѕРІ РїРѕРєР° РЅРµС‚. РќРѕРІС‹Р№ СЂР°СЃС‡РµС‚ СЃРѕР·РґР°РµС‚СЃСЏ РІ СЂР°Р·РґРµР»Рµ В«РљР»РёРµРЅС‚С‹В» РєРЅРѕРїРєРѕР№ В«Р Р°СЃСЃС‡РёС‚Р°С‚СЊВ».", toReserve: "РР· СЃСѓРјРјС‹ Рє РѕРїР»Р°С‚Рµ РІ СЂРµР·РµСЂРІ", fromReserveToPay: "РР· СЂРµР·РµСЂРІР° РІ СЃСѓРјРјСѓ Рє РѕРїР»Р°С‚Рµ", topUp: "РџРѕРїРѕР»РЅРµРЅРёРµ СЂРµР·РµСЂРІР°", completedFromReserve: "Р’С‹РїРѕР»РЅРµРЅРЅС‹Рµ СЂР°Р±РѕС‚С‹ РёР· СЂРµР·РµСЂРІР°" },
      en: { completed: "Completed", refused: "Refused", accepted: "Accepted", declined: "Declined", new: "New", paid: "Paid", unpaid: "Not paid", settlementStatus: "Payment status", completedCount: "Completed", refusedCount: "Refused", activeCount: "In progress", totalTasks: "Total tasks", settlementTotal: "Payment total", fullReport: "Full payment report", completedWorks: "Completed jobs", activeWorks: "Active jobs", newWorks: "New jobs", refusedWorks: "Refused jobs", otherWorks: "Other jobs", reserveInSettlement: "Reserve in payment", reserveBefore: "Reserve before payment", reserveUsed: "Written off from reserve", reserveLeft: "Reserve left", amountDue: "Amount to pay", reserveOperations: "Reserve operations", noReserveOperations: "No reserve operations.", noRecords: "No records.", phone: "Phone", address: "Address", acceptedBy: "Accepted by", price: "Price", payment: "Payment", status: "Status", cash: "Cash", fromReserve: "From reserve", created: "Created", acceptedAt: "Accepted", completedAt: "Completed", noDate: "no date", deleteSettlement: "Delete payment", noSettlements: "No client payments have been created yet. Create a new payment in Clients with the Calculate button.", toReserve: "From amount to pay to reserve", fromReserveToPay: "From reserve to amount to pay", topUp: "Reserve top-up", completedFromReserve: "Completed jobs from reserve" },
      uk: { completed: "Р’РёРєРѕРЅР°РЅРѕ", refused: "Р’С–РґРјРѕРІРёРІСЃСЏ", accepted: "РџСЂРёР№РЅСЏС‚Рѕ", declined: "Р’С–РґС…РёР»РµРЅРѕ", new: "РќРѕРІРµ", paid: "РћРїР»Р°С‡РµРЅРѕ", unpaid: "РќРµ РѕРїР»Р°С‡РµРЅРѕ", settlementStatus: "РЎС‚Р°С‚СѓСЃ СЂРѕР·СЂР°С…СѓРЅРєСѓ", completedCount: "Р’РёРєРѕРЅР°РЅРѕ", refusedCount: "Р’С–РґРјРѕРІРёРІСЃСЏ", activeCount: "РЈ СЂРѕР±РѕС‚С–", totalTasks: "РЈСЃСЊРѕРіРѕ Р·Р°РІРґР°РЅСЊ", settlementTotal: "РЎСѓРјР° СЂРѕР·СЂР°С…СѓРЅРєСѓ", fullReport: "РџРѕРІРЅРёР№ Р·РІС–С‚ Р·Р° СЂРѕР·СЂР°С…СѓРЅРєРѕРј", completedWorks: "Р’РёРєРѕРЅР°РЅС– СЂРѕР±РѕС‚Рё", activeWorks: "РђРєС‚РёРІРЅС– СЂРѕР±РѕС‚Рё", newWorks: "РќРѕРІС– СЂРѕР±РѕС‚Рё", refusedWorks: "Р’С–РґРјРѕРІР»РµРЅС– СЂРѕР±РѕС‚Рё", otherWorks: "Р†РЅС€С– СЂРѕР±РѕС‚Рё", reserveInSettlement: "Р РµР·РµСЂРІ Сѓ СЂРѕР·СЂР°С…СѓРЅРєСѓ", reserveBefore: "Р РµР·РµСЂРІ РґРѕ СЂРѕР·СЂР°С…СѓРЅРєСѓ", reserveUsed: "РЎРїРёСЃР°РЅРѕ Р· СЂРµР·РµСЂРІСѓ", reserveLeft: "Р—Р°Р»РёС€РѕРє СЂРµР·РµСЂРІСѓ", amountDue: "РЎСѓРјР° РґРѕ РѕРїР»Р°С‚Рё", reserveOperations: "РћРїРµСЂР°С†С–С— СЂРµР·РµСЂРІСѓ", noReserveOperations: "РћРїРµСЂР°С†С–Р№ СЂРµР·РµСЂРІСѓ РЅРµРјР°С”.", noRecords: "Р—Р°РїРёСЃС–РІ РЅРµРјР°С”.", phone: "РўРµР»РµС„РѕРЅ", address: "РђРґСЂРµСЃР°", acceptedBy: "РљРёРј РїСЂРёР№РЅСЏС‚Рѕ", price: "Р¦С–РЅР°", payment: "РћРїР»Р°С‚Р°", status: "РЎС‚Р°С‚СѓСЃ", cash: "Р“РѕС‚С–РІРєР°", fromReserve: "Р— СЂРµР·РµСЂРІСѓ", created: "РЎС‚РІРѕСЂРµРЅРѕ", acceptedAt: "РџСЂРёР№РЅСЏС‚Рѕ", completedAt: "Р’РёРєРѕРЅР°РЅРѕ", noDate: "РЅРµРјР°С” РґР°С‚Рё", deleteSettlement: "Р’РёРґР°Р»РёС‚Рё СЂРѕР·СЂР°С…СѓРЅРѕРє", noSettlements: "РЎС‚РІРѕСЂРµРЅРёС… СЂРѕР·СЂР°С…СѓРЅРєС–РІ РїРѕРєРё РЅРµРјР°С”. РќРѕРІРёР№ СЂРѕР·СЂР°С…СѓРЅРѕРє СЃС‚РІРѕСЂСЋС”С‚СЊСЃСЏ РІ СЂРѕР·РґС–Р»С– В«РљР»С–С”РЅС‚РёВ» РєРЅРѕРїРєРѕСЋ В«Р РѕР·СЂР°С…СѓРІР°С‚РёВ».", toReserve: "Р†Р· СЃСѓРјРё РґРѕ РѕРїР»Р°С‚Рё РІ СЂРµР·РµСЂРІ", fromReserveToPay: "Р— СЂРµР·РµСЂРІСѓ РІ СЃСѓРјСѓ РґРѕ РѕРїР»Р°С‚Рё", topUp: "РџРѕРїРѕРІРЅРµРЅРЅСЏ СЂРµР·РµСЂРІСѓ", completedFromReserve: "Р’РёРєРѕРЅР°РЅС– СЂРѕР±РѕС‚Рё Р· СЂРµР·РµСЂРІСѓ" },
      pl: { completed: "Wykonane", refused: "OdmГіwione", accepted: "PrzyjД™te", declined: "Odrzucone", new: "Nowe", paid: "OpЕ‚acono", unpaid: "Nie opЕ‚acono", settlementStatus: "Status rozliczenia", completedCount: "Wykonane", refusedCount: "OdmГіwione", activeCount: "W trakcie", totalTasks: "ЕЃД…cznie zadaЕ„", settlementTotal: "Suma rozliczenia", fullReport: "PeЕ‚ny raport rozliczenia", completedWorks: "Wykonane prace", activeWorks: "Aktywne prace", newWorks: "Nowe prace", refusedWorks: "OdmГіwione prace", otherWorks: "PozostaЕ‚e prace", reserveInSettlement: "Rezerwa w rozliczeniu", reserveBefore: "Rezerwa przed rozliczeniem", reserveUsed: "Pobrano z rezerwy", reserveLeft: "PozostaЕ‚a rezerwa", amountDue: "Kwota do zapЕ‚aty", reserveOperations: "Operacje rezerwy", noReserveOperations: "Brak operacji rezerwy.", noRecords: "Brak wpisГіw.", phone: "Telefon", address: "Adres", acceptedBy: "Kto przyjД…Е‚", price: "Cena", payment: "PЕ‚atnoЕ›Д‡", status: "Status", cash: "GotГіwka", fromReserve: "Z rezerwy", created: "Utworzono", acceptedAt: "PrzyjД™to", completedAt: "Wykonano", noDate: "brak daty", deleteSettlement: "UsuЕ„ rozliczenie", noSettlements: "Nie ma jeszcze utworzonych rozliczeЕ„ klientГіw. Nowe rozliczenie utworzysz w sekcji вЂћKlienciвЂќ przyciskiem вЂћRozliczвЂќ.", toReserve: "Z kwoty do zapЕ‚aty do rezerwy", fromReserveToPay: "Z rezerwy do kwoty do zapЕ‚aty", topUp: "DoЕ‚adowanie rezerwy", completedFromReserve: "Wykonane prace z rezerwy" }
    };
    function currentLanguage() {
      return localStorage.getItem("language") || "ru";
    }
    function text(key) {
      const language = currentLanguage();
      return (calcTexts[language] && calcTexts[language][key]) || calcTexts.ru[key] || key;
    }
    function localeName() {
      const locales = { ru: "ru-RU", en: "en-US", uk: "uk-UA", pl: "pl-PL" };
      return locales[currentLanguage()] || "ru-RU";
    }
    function taskStatusName(status) {
      return text(status) || status || "";
    }
    function formatMoney(value) {
      return new Intl.NumberFormat(localeName(), { style: "currency", currency: appSettings.currency || "PLN" }).format(Number(value || 0));
    }
    function formatReserve(value) {
      const labels = { credits: "CRDT", tokens: "TKN", coins: "KOIN", points: "BAL" };
      return `${new Intl.NumberFormat(localeName(), { maximumFractionDigits: 2 }).format(Number(value || 0))} ${labels[appSettings.reserveUnit] || labels.credits}`;
    }
    function calculationStatus(item) {
      return item && item.calculated ? text("paid") : text("unpaid");
    }
    function calculationClass(item) {
      return item && item.calculated ? " calculated" : "";
    }
    function calculateClientSettlementButton(item) {
      if (item.calculated) {
        return "";
      }
      return `<button class="success" type="button" onclick="calculateClientSettlement(${item.id})">${text("paid")}</button>`;
    }
    async function loadAppSettings() {
      const res = await fetch("/api/admin/settings", { headers: adminHeaders() });
      if (res.ok) {
        const data = await res.json();
        appSettings = data.settings || appSettings;
      }
    }
    async function loadCalculations() {
      const res = await fetch("/api/admin/client-calculations", { headers: adminHeaders() });
      if (!res.ok) { items.innerHTML = "<p>РќРµ СѓРґР°Р»РѕСЃСЊ Р·Р°РіСЂСѓР·РёС‚СЊ СЂР°СЃС‡РµС‚С‹ РєР»РёРµРЅС‚РѕРІ.</p>"; return; }
      const data = await res.json();
      const history = (data.settlements || []).map(item => `
        <article class="item${calculationClass(item)}">
          <h3>#${item.id} ${escapeHtml(item.displayName)} В· ${formatDate(item.createdAt)}</h3>
          <p class="status">${text("settlementStatus")}: ${calculationStatus(item)}</p>
          <p class="meta">${text("completedCount")}: ${item.counts.completed || 0} В· ${text("refusedCount")}: ${item.counts.refused || 0} В· ${text("activeCount")}: ${item.counts.active || 0} В· ${text("totalTasks")}: ${item.counts.all || 0}</p>
          <p>${appSettings.showPrices ? "<strong>" + text("settlementTotal") + ":</strong> " + formatMoney(item.totals.totalPrice || 0) : ""}</p>
          ${renderClientReport(item)}
          <div class="actions">${calculateClientSettlementButton(item)}<button class="danger" type="button" onclick="deleteClientSettlement(${item.id})">${text("deleteSettlement")}</button></div>
        </article>
      `).join("");
      items.innerHTML = history || `<p class="meta">${text("noSettlements")}</p>`;
    }
    function renderClientReport(item) {
      return `
        <details>
          <summary>${text("fullReport")}</summary>
          ${renderTaskSection(text("completedWorks"), item.completed || [], "completed")}
          ${renderTaskSection(text("activeWorks"), item.active || [], "active")}
          ${renderTaskSection(text("newWorks"), item.new || [], "new")}
          ${renderTaskSection(text("refusedWorks"), item.refused || [], "refused")}
          ${renderTaskSection(text("otherWorks"), item.other || [], "other")}
          ${renderReserveSummary(item)}
          ${renderReserveEvents(item.currentReserveEvents || item.reserveEvents || [])}
        </details>
      `;
    }
    function renderReserveSummary(item) {
      if (!appSettings.showPrices) {
        return "";
      }
      const totals = item.totals || {};
      return `
        <h4>${text("reserveInSettlement")}</h4>
        <p class="meta">${text("reserveBefore")}: ${formatReserve(totals.reserveBeforeCompleted || 0)} В· ${text("reserveUsed")}: ${formatReserve(totals.reserveUsedForCompleted || 0)} В· ${text("reserveLeft")}: ${formatReserve(totals.reservePrice || 0)} В· ${text("amountDue")}: ${formatMoney(totals.totalPrice || 0)}</p>
      `;
    }
    function reserveEventName(kind) {
      if (kind === "to_reserve") return text("toReserve");
      if (kind === "from_reserve") return text("fromReserveToPay");
      if (kind === "top_up") return text("topUp");
      if (kind === "completed_from_reserve") return text("completedFromReserve");
      return kind;
    }
    function renderReserveEvents(events) {
      if (!events.length) {
        return `
          <h4>${text("reserveOperations")}</h4>
          <p class="meta">${text("noReserveOperations")}</p>
        `;
      }
      return `
        <h4>${text("reserveOperations")}</h4>
        ${events.map(event => `
          <p class="meta">${formatDate(event.createdAt)} В· ${reserveEventName(event.kind)} В· ${formatReserve(event.absoluteAmount ?? Math.abs(event.amount || 0))}</p>
        `).join("")}
      `;
    }
    function renderTaskSection(title, tasks, type) {
      if (!tasks.length) {
        return `<h4>${title}</h4><p class="meta">${text("noRecords")}</p>`;
      }
      return `
        <h4>${title}</h4>
        ${tasks.map(task => `
          <article class="reportTask ${type}">
            <strong>#${task.id}</strong>
            <p><strong>${escapeHtml(task.title)}</strong></p>
            <p>${escapeHtml(task.description || "")}</p>
            <p>${task.phone ? "<strong>" + text("phone") + ":</strong> <a href=\"tel:" + phoneHref(task.phone) + "\">" + escapeHtml(task.phone) + "</a>" : ""}</p>
            <p>${task.address ? "<strong>" + text("address") + ":</strong> " + escapeHtml(task.address) : ""}</p>
            <p>${task.assignedToName ? "<strong>" + text("acceptedBy") + ":</strong> " + escapeHtml(task.assignedToName) + (task.assignedToLogin ? " В· " + escapeHtml(task.assignedToLogin) : "") : ""}</p>
            <p>${appSettings.showPrices ? "<strong>" + text("price") + ":</strong> " + formatMoney(task.price || 0) : ""}</p>
            <p><strong>${text("payment")}:</strong> ${paymentMethodName(task.paymentMethod)}</p>
            <div class="meta">${text("status")}: ${taskStatusName(task.status)} В· ${taskDatesMeta(task)}</div>
          </article>
        `).join("")}
      `;
    }
    function phoneHref(value) {
      return String(value).replace(/[^\d+]/g, "");
    }
    function paymentMethodName(method) {
      return method === "cash" ? text("cash") : text("fromReserve");
    }
    function taskDatesMeta(task) {
      const rows = [];
      if (task.createdAt) rows.push(text("created") + ": " + formatDate(task.createdAt));
      if (task.acceptedAt) rows.push(text("acceptedAt") + ": " + formatDate(task.acceptedAt));
      if (task.completedAt) rows.push(text("completedAt") + ": " + formatDate(task.completedAt));
      return rows.length ? rows.join(" В· ") : text("noDate");
    }
    function formatDate(value) {
      return new Date(value * 1000).toLocaleString(localeName());
    }
    async function settleClient(id) {
      const password = prompt("РџР°СЂРѕР»СЊ РїРѕРґС‚РІРµСЂР¶РґРµРЅРёСЏ");
      if (!password) return;
      const res = await fetch("/api/admin/clients/" + id + "/settle", {
        method: "POST",
        headers: adminHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ password })
      });
      if (!res.ok) {
        alert("РќРµ СѓРґР°Р»РѕСЃСЊ РѕС‚РјРµС‚РёС‚СЊ СЂР°СЃС‡РµС‚ РєР°Рє СЂР°СЃСЃС‡РёС‚Р°РЅРЅС‹Р№.");
        return;
      }
      loadCalculations();
    }
    async function calculateClientSettlement(id) {
      const password = prompt("РџР°СЂРѕР»СЊ РїРѕРґС‚РІРµСЂР¶РґРµРЅРёСЏ");
      if (!password) return;
      const res = await fetch("/api/admin/client-settlements/" + id + "/calculate", {
        method: "POST",
        headers: adminHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ password })
      });
      if (!res.ok) {
        alert("РќРµ СѓРґР°Р»РѕСЃСЊ РѕРїР»Р°С‚РёС‚СЊ.");
        return;
      }
      loadCalculations();
    }
    async function deleteClientSettlement(id) {
      const password = prompt("РџР°СЂРѕР»СЊ РїРѕРґС‚РІРµСЂР¶РґРµРЅРёСЏ");
      if (!password) return;
      const res = await fetch("/api/admin/client-settlements/" + id + "/delete", {
        method: "POST",
        headers: adminHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ password })
      });
      if (!res.ok) {
        alert("РќРµ СѓРґР°Р»РѕСЃСЊ СѓРґР°Р»РёС‚СЊ СЂР°СЃС‡РµС‚.");
        return;
      }
      loadCalculations();
    }
    requireAdminAccess(async () => {
      await loadAppSettings();
      loadCalculations();
    });
    document.addEventListener("change", event => {
      if (event.target && event.target.id === "languageSelect") {
        loadCalculations();
      }
    });
  </script>
</body>
</html>"""


MARKETING_PAGE_STYLE = r"""
    :root { --ink: #172026; --teal: #0f766e; --gold: #f6c85f; --paper: #fffaf0; --muted: #60717d; }
    body { font-family: Arial, sans-serif; margin: 0; color: var(--ink); background: linear-gradient(135deg, #fff7df 0%, #d8f8eb 48%, #e9d5ff 100%); min-height: 100vh; }
    body.locked header, body.locked main { display: none; }
    header { background: linear-gradient(135deg, #0f766e 0%, #2563eb 48%, #7c3aed 100%); color: white; padding: 24px 28px; }
    main { max-width: 1220px; margin: 0 auto; padding: 24px; }
    nav { display: grid; grid-template-columns: repeat(10, minmax(104px, 1fr)); gap: 10px; margin-top: 12px; max-width: 1320px; }
    nav a { display: flex; align-items: center; justify-content: center; min-height: 44px; box-sizing: border-box; color: var(--ink); background: var(--gold); font-weight: bold; padding: 8px 10px; border-radius: 8px; text-align: center; line-height: 1.15; text-decoration: none; }
    section { margin: 18px 0; }
    .grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
    .panel { background: rgba(255,255,255,.82); border: 1px solid rgba(23,32,38,.12); border-radius: 8px; padding: 16px; box-shadow: 0 14px 34px rgba(23,32,38,.08); }
    .full { grid-column: 1 / -1; }
    h2, h3 { margin-top: 0; }
    form { display: grid; gap: 10px; }
    .row { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
    input, textarea, select { width: 100%; box-sizing: border-box; font: inherit; padding: 10px 12px; border-radius: 8px; border: 1px solid rgba(23,32,38,.18); background: white; }
    textarea { min-height: 120px; resize: vertical; }
    label.field { display: grid; gap: 6px; color: var(--muted); font-size: 13px; font-weight: 700; }
    label.field input, label.field textarea, label.field select { color: var(--ink); font-size: 16px; font-weight: 400; }
    .upload-row { display: grid; grid-template-columns: minmax(0, 1fr) minmax(220px, .55fr); gap: 10px; align-items: end; }
    .upload-status { min-height: 18px; margin: -4px 0 0; }
    button { font: inherit; padding: 11px 13px; border-radius: 8px; border: 0; background: linear-gradient(135deg, var(--teal), #2563eb); color: white; cursor: pointer; font-weight: 700; }
    button.secondary { background: #475569; }
    button.success { background: #16a34a; }
    .cards { display: grid; gap: 10px; }
    .item { background: white; border-left: 5px solid var(--teal); border-radius: 8px; padding: 12px; }
    .item.off { opacity: .62; border-left-color: #94a3b8; }
    .meta { color: var(--muted); font-size: 14px; }
    .pill { display: inline-flex; align-items: center; border-radius: 999px; padding: 4px 9px; background: #e0f2fe; margin: 3px 4px 3px 0; font-size: 13px; font-weight: 700; }
    .preview { max-width: 220px; border-radius: 8px; border: 1px solid rgba(23,32,38,.12); margin-top: 8px; display: block; }
    .note { background: rgba(15,118,110,.1); border-left: 5px solid var(--teal); padding: 12px; border-radius: 8px; }
    @media (max-width: 980px) { nav, .grid, .row, .upload-row { grid-template-columns: 1fr; } .full { grid-column: auto; } }
"""


MARKETING_PAGE_SCRIPT = r"""
    let adminPassword = sessionStorage.getItem("adminPassword") || "";
    let state = {};
    const marketingTranslations = {
      en: {
        "Р РµРєР»Р°РјР° Telegram": "Telegram Ads",
        "Р РµРєР»Р°РјР° Facebook": "Facebook Ads",
        "Р“РѕСЂРѕРґР°": "Cities",
        "Р“РѕСЂРѕРґ": "City",
        "Р”РѕР±Р°РІРёС‚СЊ РіРѕСЂРѕРґ": "Add city",
        "Р”РѕР±Р°РІРёС‚СЊ Telegram-С‡Р°С‚": "Add Telegram chat",
        "РќР°Р·РІР°РЅРёРµ РіСЂСѓРїРїС‹": "Group name",
        "Chat ID, РЅР°РїСЂРёРјРµСЂ -100...": "Chat ID, for example -100...",
        "РјР°СЃС‚РµСЂ, СЃР°РЅС‚РµС…РЅРёРє, СЌР»РµРєС‚СЂРёРє, СЂРµРјРѕРЅС‚": "handyman, plumber, electrician, repair",
        "РљР»СЋС‡РµРІС‹Рµ СЃР»РѕРІР°": "Keywords",
        "Р§Р°С‚ РІРєР»СЋС‡РµРЅ: Р±РѕС‚ РјРѕР¶РµС‚ РѕС‚РїСЂР°РІР»СЏС‚СЊ СЂРµРєР»Р°РјСѓ РІ СЌС‚РѕС‚ С‡Р°С‚": "Chat enabled: the bot can send ads to this chat",
        "РЎРѕС…СЂР°РЅРёС‚СЊ С‡Р°С‚": "Save chat",
        "Р РµРєР»Р°РјРЅС‹Р№ С‚РµРєСЃС‚ Рё РєР°СЂС‚РёРЅРєР°": "Ad text and image",
        "РќР°Р·РІР°РЅРёРµ С‚РµРєСЃС‚Р°": "Text title",
        "Р’СЃРµ": "All",
        "РљР»РёРµРЅС‚С‹": "Clients",
        "РњР°СЃС‚РµСЂР°": "Workers",
        "РњР°С‚РµСЂРёР°Р» РІРєР»СЋС‡РµРЅ: РјРѕР¶РЅРѕ РѕС‚РїСЂР°РІР»СЏС‚СЊ Рё СЃС‚Р°РІРёС‚СЊ РІ СЂР°СЃРїРёСЃР°РЅРёРµ": "Material enabled: it can be sent and scheduled",
        "РўРµРєСЃС‚ СЂРµРєР»Р°РјС‹": "Ad text",
        "РўРµРєСЃС‚ СЂРµРєР»Р°РјС‹ / Messenger-РѕС‚РІРµС‚": "Ad text / Messenger reply",
        "РЎСЃС‹Р»РєР° РЅР° РєР°СЂС‚РёРЅРєСѓ, РЅР°РїСЂРёРјРµСЂ https://ogarniemy.pro/assets/banner.jpg": "Image link, for example https://ogarniemy.pro/assets/banner.jpg",
        "РЎСЃС‹Р»РєР° РЅР° РєР°СЂС‚РёРЅРєСѓ": "Image link",
        "РљР°СЂС‚РёРЅРєР° СЃ РєРѕРјРїСЊСЋС‚РµСЂР°": "Image from computer",
        "Р—Р°РіСЂСѓР·РёС‚СЊ РєР°СЂС‚РёРЅРєСѓ СЃ РєРѕРјРїСЊСЋС‚РµСЂР°": "Upload image from computer",
        "РљР°СЂС‚РёРЅРєР° Р·Р°РіСЂСѓР¶РµРЅР°.": "Image uploaded.",
        "РќРµ СѓРґР°Р»РѕСЃСЊ Р·Р°РіСЂСѓР·РёС‚СЊ РєР°СЂС‚РёРЅРєСѓ.": "Could not upload image.",
        "РЎРѕС…СЂР°РЅРёС‚СЊ СЂРµРєР»Р°РјРЅС‹Р№ РјР°С‚РµСЂРёР°Р»": "Save ad material",
        "Р Р°СЃРїРёСЃР°РЅРёРµ": "Schedule",
        "Р Р°СЃРїРёСЃР°РЅРёРµ РїРѕРґРіРѕС‚РѕРІРєРё": "Preparation schedule",
        "Р Р°СЃРїРёСЃР°РЅРёРµ РѕС‚РїСЂР°РІРєРё": "Sending schedule",
        "Р’СЂРµРјСЏ РѕС‚РїСЂР°РІРєРё": "Sending time",
        "Р РµРєР»Р°РјРЅС‹Р№ РјР°С‚РµСЂРёР°Р»": "Ad material",
        "Р Р°СЃРїРёСЃР°РЅРёРµ РІРєР»СЋС‡РµРЅРѕ: РѕС‚РїСЂР°РІР»СЏС‚СЊ РєР°Р¶РґС‹Р№ РґРµРЅСЊ РІ СЌС‚Рѕ РІСЂРµРјСЏ": "Schedule enabled: send every day at this time",
        "Р Р°СЃРїРёСЃР°РЅРёРµ РІРєР»СЋС‡РµРЅРѕ: РіРѕС‚РѕРІРёС‚СЊ РјР°С‚РµСЂРёР°Р» РєР°Р¶РґС‹Р№ РґРµРЅСЊ РІ СЌС‚Рѕ РІСЂРµРјСЏ": "Schedule enabled: prepare material every day at this time",
        "РЎРѕС…СЂР°РЅРёС‚СЊ РІСЂРµРјСЏ": "Save time",
        "РћС‚РїСЂР°РІРёС‚СЊ СЃРµР№С‡Р°СЃ": "Send now",
        "РћС‚РїСЂР°РІРёС‚СЊ СЃРµР№С‡Р°СЃ": "Send now",
        "РџРѕРґРіРѕС‚РѕРІРёС‚СЊ СЃРµР№С‡Р°СЃ": "Prepare now",
        "Р§С‚Рѕ СѓР¶Рµ РґРѕР±Р°РІР»РµРЅРѕ": "Already added",
        "Р–СѓСЂРЅР°Р»": "Log",
        "Р’СЃРµ РіРѕСЂРѕРґР°": "All cities",
        "Р“РѕСЂРѕРґР° РїРѕРєР° РЅРµ РґРѕР±Р°РІР»РµРЅС‹.": "No cities added yet.",
        "Р§Р°С‚С‹": "Chats",
        "Р§Р°С‚С‹ РїРѕРєР° РЅРµ РґРѕР±Р°РІР»РµРЅС‹.": "No chats added yet.",
        "Р РµРєР»Р°РјРЅС‹Рµ РјР°С‚РµСЂРёР°Р»С‹": "Ad materials",
        "РњР°С‚РµСЂРёР°Р»С‹ РїРѕРєР° РЅРµ РґРѕР±Р°РІР»РµРЅС‹.": "No materials added yet.",
        "С‚РµРєСЃС‚": "text",
        "РїРѕСЃР»РµРґРЅСЏСЏ РѕС‚РїСЂР°РІРєР°": "last sent",
        "РїРѕСЃР»РµРґРЅСЏСЏ РїРѕРґРіРѕС‚РѕРІРєР°": "last prepared",
        "РЅРµС‚": "none",
        "Р Р°СЃРїРёСЃР°РЅРёРµ РїРѕРєР° РЅРµ РґРѕР±Р°РІР»РµРЅРѕ.": "No schedule added yet.",
        "РќР°Р№РґРµРЅРЅС‹Рµ Р·Р°СЏРІРєРё": "Found requests",
        "Р—Р°СЏРІРѕРє РїРѕРєР° РЅРµС‚.": "No requests yet.",
        "Р–СѓСЂРЅР°Р» РїРѕРєР° РїСѓСЃС‚.": "The log is empty.",
        "Р РµРґР°РєС‚РёСЂРѕРІР°С‚СЊ": "Edit",
        "Р’С‹Р±РµСЂРёС‚Рµ СЂРµРєР»Р°РјРЅС‹Р№ С‚РµРєСЃС‚.": "Choose an ad text.",
        "РћС‚РїСЂР°РІР»РµРЅРѕ": "Sent",
        "Facebook РѕС‚РїСЂР°РІРєР° РїРѕРґРіРѕС‚РѕРІР»РµРЅР°.": "Facebook delivery prepared.",
        "Facebook Р»СѓС‡С€Рµ РёСЃРїРѕР»СЊР·РѕРІР°С‚СЊ РєР°Рє РІС…РѕРґСЏС‰РёР№ РєР°РЅР°Р»: СЂРµРєР»Р°РјР° РІРµРґРµС‚ РІ Messenger РёР»Рё РЅР° СЃР°Р№С‚, Р±РѕС‚ РѕС‚РІРµС‡Р°РµС‚ С‚РµРј, РєС‚Рѕ СЃР°Рј РЅР°РїРёСЃР°Р». РњР°СЃСЃРѕРІС‹Рµ РµР¶РµРґРЅРµРІРЅС‹Рµ Р»РёС‡РЅС‹Рµ СЃРѕРѕР±С‰РµРЅРёСЏ РЅРµР·РЅР°РєРѕРјС‹Рј Р»СЋРґСЏРј Facebook РѕРіСЂР°РЅРёС‡РёРІР°РµС‚.": "Facebook is best used as an inbound channel: ads lead to Messenger or the site, and the bot replies to people who contacted you first. Facebook restricts daily mass private messages to strangers.",
        "Facebook-С†РµР»Рё": "Facebook targets",
        "Facebook РіСЂСѓРїРїС‹": "Facebook groups",
        "Р”РѕР±Р°РІРёС‚СЊ Facebook РІ РіСЂСѓРїРїСѓ": "Add Facebook group",
        "РЎС‚СЂР°РЅРёС†Р° / РєР°РјРїР°РЅРёСЏ / РіСЂСѓРїРїР°": "Page / campaign / group",
        "РЎС‚СЂР°РЅРёС†Р° / РіСЂСѓРїРїР° / СЃСЃС‹Р»РєР°": "Page / group / link",
        "ID РёР»Рё СЃСЃС‹Р»РєР°": "ID or link",
        "Р—Р°РјРµС‚РєРё: Р°СѓРґРёС‚РѕСЂРёСЏ, Р±СЋРґР¶РµС‚, С‡С‚Рѕ РїСЂРѕРІРµСЂРёС‚СЊ": "Notes: audience, budget, what to check",
        "Р“СЂСѓРїРїР° РІРєР»СЋС‡РµРЅР°: РјРѕР¶РЅРѕ РёСЃРїРѕР»СЊР·РѕРІР°С‚СЊ РІ РїР»Р°РЅРёСЂРѕРІР°РЅРёРё Facebook": "Group enabled: it can be used in Facebook planning",
        "РЎРѕС…СЂР°РЅРёС‚СЊ РіСЂСѓРїРїСѓ": "Save group",
        "РџРѕРґРіРѕС‚РѕРІРёС‚СЊ СЃРµР№С‡Р°СЃ": "Prepare now",
        "Р“СЂСѓРїРїС‹ РїРѕРєР° РЅРµ РґРѕР±Р°РІР»РµРЅС‹.": "No groups added yet."
      },
      uk: {
        "Р РµРєР»Р°РјР° Telegram": "Р РµРєР»Р°РјР° Telegram",
        "Р РµРєР»Р°РјР° Facebook": "Р РµРєР»Р°РјР° Facebook",
        "Р“РѕСЂРѕРґР°": "РњС–СЃС‚Р°",
        "Р“РѕСЂРѕРґ": "РњС–СЃС‚Рѕ",
        "Р”РѕР±Р°РІРёС‚СЊ РіРѕСЂРѕРґ": "Р”РѕРґР°С‚Рё РјС–СЃС‚Рѕ",
        "Р”РѕР±Р°РІРёС‚СЊ Telegram-С‡Р°С‚": "Р”РѕРґР°С‚Рё Telegram-С‡Р°С‚",
        "РќР°Р·РІР°РЅРёРµ РіСЂСѓРїРїС‹": "РќР°Р·РІР° РіСЂСѓРїРё",
        "Chat ID, РЅР°РїСЂРёРјРµСЂ -100...": "Chat ID, РЅР°РїСЂРёРєР»Р°Рґ -100...",
        "РјР°СЃС‚РµСЂ, СЃР°РЅС‚РµС…РЅРёРє, СЌР»РµРєС‚СЂРёРє, СЂРµРјРѕРЅС‚": "РјР°Р№СЃС‚РµСЂ, СЃР°РЅС‚РµС…РЅС–Рє, РµР»РµРєС‚СЂРёРє, СЂРµРјРѕРЅС‚",
        "РљР»СЋС‡РµРІС‹Рµ СЃР»РѕРІР°": "РљР»СЋС‡РѕРІС– СЃР»РѕРІР°",
        "Р§Р°С‚ РІРєР»СЋС‡РµРЅ: Р±РѕС‚ РјРѕР¶РµС‚ РѕС‚РїСЂР°РІР»СЏС‚СЊ СЂРµРєР»Р°РјСѓ РІ СЌС‚РѕС‚ С‡Р°С‚": "Р§Р°С‚ СѓРІС–РјРєРЅРµРЅРѕ: Р±РѕС‚ РјРѕР¶Рµ РЅР°РґСЃРёР»Р°С‚Рё СЂРµРєР»Р°РјСѓ РІ С†РµР№ С‡Р°С‚",
        "РЎРѕС…СЂР°РЅРёС‚СЊ С‡Р°С‚": "Р—Р±РµСЂРµРіС‚Рё С‡Р°С‚",
        "Р РµРєР»Р°РјРЅС‹Р№ С‚РµРєСЃС‚ Рё РєР°СЂС‚РёРЅРєР°": "Р РµРєР»Р°РјРЅРёР№ С‚РµРєСЃС‚ С– РєР°СЂС‚РёРЅРєР°",
        "РќР°Р·РІР°РЅРёРµ С‚РµРєСЃС‚Р°": "РќР°Р·РІР° С‚РµРєСЃС‚Сѓ",
        "Р’СЃРµ": "РЈСЃС–",
        "РљР»РёРµРЅС‚С‹": "РљР»С–С”РЅС‚Рё",
        "РњР°СЃС‚РµСЂР°": "РњР°Р№СЃС‚СЂРё",
        "РњР°С‚РµСЂРёР°Р» РІРєР»СЋС‡РµРЅ: РјРѕР¶РЅРѕ РѕС‚РїСЂР°РІР»СЏС‚СЊ Рё СЃС‚Р°РІРёС‚СЊ РІ СЂР°СЃРїРёСЃР°РЅРёРµ": "РњР°С‚РµСЂС–Р°Р» СѓРІС–РјРєРЅРµРЅРѕ: Р№РѕРіРѕ РјРѕР¶РЅР° РЅР°РґСЃРёР»Р°С‚Рё Р№ СЃС‚Р°РІРёС‚Рё РІ СЂРѕР·РєР»Р°Рґ",
        "РўРµРєСЃС‚ СЂРµРєР»Р°РјС‹": "РўРµРєСЃС‚ СЂРµРєР»Р°РјРё",
        "РўРµРєСЃС‚ СЂРµРєР»Р°РјС‹ / Messenger-РѕС‚РІРµС‚": "РўРµРєСЃС‚ СЂРµРєР»Р°РјРё / РІС–РґРїРѕРІС–РґСЊ Messenger",
        "РЎСЃС‹Р»РєР° РЅР° РєР°СЂС‚РёРЅРєСѓ, РЅР°РїСЂРёРјРµСЂ https://ogarniemy.pro/assets/banner.jpg": "РџРѕСЃРёР»Р°РЅРЅСЏ РЅР° РєР°СЂС‚РёРЅРєСѓ, РЅР°РїСЂРёРєР»Р°Рґ https://ogarniemy.pro/assets/banner.jpg",
        "РЎСЃС‹Р»РєР° РЅР° РєР°СЂС‚РёРЅРєСѓ": "РџРѕСЃРёР»Р°РЅРЅСЏ РЅР° РєР°СЂС‚РёРЅРєСѓ",
        "РљР°СЂС‚РёРЅРєР° СЃ РєРѕРјРїСЊСЋС‚РµСЂР°": "РљР°СЂС‚РёРЅРєР° Р· РєРѕРјРї'СЋС‚РµСЂР°",
        "Р—Р°РіСЂСѓР·РёС‚СЊ РєР°СЂС‚РёРЅРєСѓ СЃ РєРѕРјРїСЊСЋС‚РµСЂР°": "Р—Р°РІР°РЅС‚Р°Р¶РёС‚Рё РєР°СЂС‚РёРЅРєСѓ Р· РєРѕРјРї'СЋС‚РµСЂР°",
        "РљР°СЂС‚РёРЅРєР° Р·Р°РіСЂСѓР¶РµРЅР°.": "РљР°СЂС‚РёРЅРєСѓ Р·Р°РІР°РЅС‚Р°Р¶РµРЅРѕ.",
        "РќРµ СѓРґР°Р»РѕСЃСЊ Р·Р°РіСЂСѓР·РёС‚СЊ РєР°СЂС‚РёРЅРєСѓ.": "РќРµ РІРґР°Р»РѕСЃСЏ Р·Р°РІР°РЅС‚Р°Р¶РёС‚Рё РєР°СЂС‚РёРЅРєСѓ.",
        "РЎРѕС…СЂР°РЅРёС‚СЊ СЂРµРєР»Р°РјРЅС‹Р№ РјР°С‚РµСЂРёР°Р»": "Р—Р±РµСЂРµРіС‚Рё СЂРµРєР»Р°РјРЅРёР№ РјР°С‚РµСЂС–Р°Р»",
        "Р Р°СЃРїРёСЃР°РЅРёРµ": "Р РѕР·РєР»Р°Рґ",
        "Р Р°СЃРїРёСЃР°РЅРёРµ РїРѕРґРіРѕС‚РѕРІРєРё": "Р РѕР·РєР»Р°Рґ РїС–РґРіРѕС‚РѕРІРєРё",
        "Р Р°СЃРїРёСЃР°РЅРёРµ РѕС‚РїСЂР°РІРєРё": "Р РѕР·РєР»Р°Рґ РІС–РґРїСЂР°РІРєРё",
        "Р’СЂРµРјСЏ РѕС‚РїСЂР°РІРєРё": "Р§Р°СЃ РІС–РґРїСЂР°РІРєРё",
        "Р РµРєР»Р°РјРЅС‹Р№ РјР°С‚РµСЂРёР°Р»": "Р РµРєР»Р°РјРЅРёР№ РјР°С‚РµСЂС–Р°Р»",
        "Р Р°СЃРїРёСЃР°РЅРёРµ РІРєР»СЋС‡РµРЅРѕ: РѕС‚РїСЂР°РІР»СЏС‚СЊ РєР°Р¶РґС‹Р№ РґРµРЅСЊ РІ СЌС‚Рѕ РІСЂРµРјСЏ": "Р РѕР·РєР»Р°Рґ СѓРІС–РјРєРЅРµРЅРѕ: РЅР°РґСЃРёР»Р°С‚Рё С‰РѕРґРЅСЏ РІ С†РµР№ С‡Р°СЃ",
        "Р Р°СЃРїРёСЃР°РЅРёРµ РІРєР»СЋС‡РµРЅРѕ: РіРѕС‚РѕРІРёС‚СЊ РјР°С‚РµСЂРёР°Р» РєР°Р¶РґС‹Р№ РґРµРЅСЊ РІ СЌС‚Рѕ РІСЂРµРјСЏ": "Р РѕР·РєР»Р°Рґ СѓРІС–РјРєРЅРµРЅРѕ: РіРѕС‚СѓРІР°С‚Рё РјР°С‚РµСЂС–Р°Р» С‰РѕРґРЅСЏ РІ С†РµР№ С‡Р°СЃ",
        "РЎРѕС…СЂР°РЅРёС‚СЊ РІСЂРµРјСЏ": "Р—Р±РµСЂРµРіС‚Рё С‡Р°СЃ",
        "РћС‚РїСЂР°РІРёС‚СЊ СЃРµР№С‡Р°СЃ": "РќР°РґС–СЃР»Р°С‚Рё Р·Р°СЂР°Р·",
        "РџРѕРґРіРѕС‚РѕРІРёС‚СЊ СЃРµР№С‡Р°СЃ": "РџС–РґРіРѕС‚СѓРІР°С‚Рё Р·Р°СЂР°Р·",
        "Р§С‚Рѕ СѓР¶Рµ РґРѕР±Р°РІР»РµРЅРѕ": "Р©Рѕ РІР¶Рµ РґРѕРґР°РЅРѕ",
        "Р–СѓСЂРЅР°Р»": "Р–СѓСЂРЅР°Р»",
        "Р’СЃРµ РіРѕСЂРѕРґР°": "РЈСЃС– РјС–СЃС‚Р°",
        "Р“РѕСЂРѕРґР° РїРѕРєР° РЅРµ РґРѕР±Р°РІР»РµРЅС‹.": "РњС–СЃС‚Р° С‰Рµ РЅРµ РґРѕРґР°РЅС–.",
        "Р§Р°С‚С‹": "Р§Р°С‚Рё",
        "Р§Р°С‚С‹ РїРѕРєР° РЅРµ РґРѕР±Р°РІР»РµРЅС‹.": "Р§Р°С‚Рё С‰Рµ РЅРµ РґРѕРґР°РЅС–.",
        "Р РµРєР»Р°РјРЅС‹Рµ РјР°С‚РµСЂРёР°Р»С‹": "Р РµРєР»Р°РјРЅС– РјР°С‚РµСЂС–Р°Р»Рё",
        "РњР°С‚РµСЂРёР°Р»С‹ РїРѕРєР° РЅРµ РґРѕР±Р°РІР»РµРЅС‹.": "РњР°С‚РµСЂС–Р°Р»Рё С‰Рµ РЅРµ РґРѕРґР°РЅС–.",
        "С‚РµРєСЃС‚": "С‚РµРєСЃС‚",
        "РїРѕСЃР»РµРґРЅСЏСЏ РѕС‚РїСЂР°РІРєР°": "РѕСЃС‚Р°РЅРЅСЏ РІС–РґРїСЂР°РІРєР°",
        "РїРѕСЃР»РµРґРЅСЏСЏ РїРѕРґРіРѕС‚РѕРІРєР°": "РѕСЃС‚Р°РЅРЅСЏ РїС–РґРіРѕС‚РѕРІРєР°",
        "РЅРµС‚": "РЅРµРјР°С”",
        "Р Р°СЃРїРёСЃР°РЅРёРµ РїРѕРєР° РЅРµ РґРѕР±Р°РІР»РµРЅРѕ.": "Р РѕР·РєР»Р°Рґ С‰Рµ РЅРµ РґРѕРґР°РЅРёР№.",
        "РќР°Р№РґРµРЅРЅС‹Рµ Р·Р°СЏРІРєРё": "Р—РЅР°Р№РґРµРЅС– Р·Р°СЏРІРєРё",
        "Р—Р°СЏРІРѕРє РїРѕРєР° РЅРµС‚.": "Р—Р°СЏРІРѕРє РїРѕРєРё РЅРµРјР°С”.",
        "Р–СѓСЂРЅР°Р» РїРѕРєР° РїСѓСЃС‚.": "Р–СѓСЂРЅР°Р» РїРѕРєРё РїРѕСЂРѕР¶РЅС–Р№.",
        "Р РµРґР°РєС‚РёСЂРѕРІР°С‚СЊ": "Р РµРґР°РіСѓРІР°С‚Рё",
        "Р’С‹Р±РµСЂРёС‚Рµ СЂРµРєР»Р°РјРЅС‹Р№ С‚РµРєСЃС‚.": "РћР±РµСЂС–С‚СЊ СЂРµРєР»Р°РјРЅРёР№ С‚РµРєСЃС‚.",
        "РћС‚РїСЂР°РІР»РµРЅРѕ": "РќР°РґС–СЃР»Р°РЅРѕ",
        "Facebook РѕС‚РїСЂР°РІРєР° РїРѕРґРіРѕС‚РѕРІР»РµРЅР°.": "Facebook-РІС–РґРїСЂР°РІРєСѓ РїС–РґРіРѕС‚РѕРІР»РµРЅРѕ.",
        "Facebook Р»СѓС‡С€Рµ РёСЃРїРѕР»СЊР·РѕРІР°С‚СЊ РєР°Рє РІС…РѕРґСЏС‰РёР№ РєР°РЅР°Р»: СЂРµРєР»Р°РјР° РІРµРґРµС‚ РІ Messenger РёР»Рё РЅР° СЃР°Р№С‚, Р±РѕС‚ РѕС‚РІРµС‡Р°РµС‚ С‚РµРј, РєС‚Рѕ СЃР°Рј РЅР°РїРёСЃР°Р». РњР°СЃСЃРѕРІС‹Рµ РµР¶РµРґРЅРµРІРЅС‹Рµ Р»РёС‡РЅС‹Рµ СЃРѕРѕР±С‰РµРЅРёСЏ РЅРµР·РЅР°РєРѕРјС‹Рј Р»СЋРґСЏРј Facebook РѕРіСЂР°РЅРёС‡РёРІР°РµС‚.": "Facebook РєСЂР°С‰Рµ РІРёРєРѕСЂРёСЃС‚РѕРІСѓРІР°С‚Рё СЏРє РІС…С–РґРЅРёР№ РєР°РЅР°Р»: СЂРµРєР»Р°РјР° РІРµРґРµ РІ Messenger Р°Р±Рѕ РЅР° СЃР°Р№С‚, Р° Р±РѕС‚ РІС–РґРїРѕРІС–РґР°С” С‚РёРј, С…С‚Рѕ СЃР°Рј РЅР°РїРёСЃР°РІ. Facebook РѕР±РјРµР¶СѓС” РјР°СЃРѕРІС– С‰РѕРґРµРЅРЅС– РѕСЃРѕР±РёСЃС‚С– РїРѕРІС–РґРѕРјР»РµРЅРЅСЏ РЅРµР·РЅР°Р№РѕРјРёРј Р»СЋРґСЏРј.",
        "Facebook-С†РµР»Рё": "Facebook-С†С–Р»С–",
        "Facebook РіСЂСѓРїРїС‹": "Facebook-РіСЂСѓРїРё",
        "Р”РѕР±Р°РІРёС‚СЊ Facebook РІ РіСЂСѓРїРїСѓ": "Р”РѕРґР°С‚Рё Facebook Сѓ РіСЂСѓРїСѓ",
        "РЎС‚СЂР°РЅРёС†Р° / РєР°РјРїР°РЅРёСЏ / РіСЂСѓРїРїР°": "РЎС‚РѕСЂС–РЅРєР° / РєР°РјРїР°РЅС–СЏ / РіСЂСѓРїР°",
        "РЎС‚СЂР°РЅРёС†Р° / РіСЂСѓРїРїР° / СЃСЃС‹Р»РєР°": "РЎС‚РѕСЂС–РЅРєР° / РіСЂСѓРїР° / РїРѕСЃРёР»Р°РЅРЅСЏ",
        "ID РёР»Рё СЃСЃС‹Р»РєР°": "ID Р°Р±Рѕ РїРѕСЃРёР»Р°РЅРЅСЏ",
        "Р—Р°РјРµС‚РєРё: Р°СѓРґРёС‚РѕСЂРёСЏ, Р±СЋРґР¶РµС‚, С‡С‚Рѕ РїСЂРѕРІРµСЂРёС‚СЊ": "РќРѕС‚Р°С‚РєРё: Р°СѓРґРёС‚РѕСЂС–СЏ, Р±СЋРґР¶РµС‚, С‰Рѕ РїРµСЂРµРІС–СЂРёС‚Рё",
        "Р“СЂСѓРїРїР° РІРєР»СЋС‡РµРЅР°: РјРѕР¶РЅРѕ РёСЃРїРѕР»СЊР·РѕРІР°С‚СЊ РІ РїР»Р°РЅРёСЂРѕРІР°РЅРёРё Facebook": "Р“СЂСѓРїСѓ СѓРІС–РјРєРЅРµРЅРѕ: РјРѕР¶РЅР° РІРёРєРѕСЂРёСЃС‚РѕРІСѓРІР°С‚Рё РІ РїР»Р°РЅСѓРІР°РЅРЅС– Facebook",
        "РЎРѕС…СЂР°РЅРёС‚СЊ РіСЂСѓРїРїСѓ": "Р—Р±РµСЂРµРіС‚Рё РіСЂСѓРїСѓ",
        "Р“СЂСѓРїРїС‹ РїРѕРєР° РЅРµ РґРѕР±Р°РІР»РµРЅС‹.": "Р“СЂСѓРїРё С‰Рµ РЅРµ РґРѕРґР°РЅС–."
      },
      pl: {
        "Р РµРєР»Р°РјР° Telegram": "Reklama Telegram",
        "Р РµРєР»Р°РјР° Facebook": "Reklama Facebook",
        "Р“РѕСЂРѕРґР°": "Miasta",
        "Р“РѕСЂРѕРґ": "Miasto",
        "Р”РѕР±Р°РІРёС‚СЊ РіРѕСЂРѕРґ": "Dodaj miasto",
        "Р”РѕР±Р°РІРёС‚СЊ Telegram-С‡Р°С‚": "Dodaj czat Telegram",
        "РќР°Р·РІР°РЅРёРµ РіСЂСѓРїРїС‹": "Nazwa grupy",
        "Chat ID, РЅР°РїСЂРёРјРµСЂ -100...": "Chat ID, na przykЕ‚ad -100...",
        "РјР°СЃС‚РµСЂ, СЃР°РЅС‚РµС…РЅРёРє, СЌР»РµРєС‚СЂРёРє, СЂРµРјРѕРЅС‚": "fachowiec, hydraulik, elektryk, naprawa",
        "РљР»СЋС‡РµРІС‹Рµ СЃР»РѕРІР°": "SЕ‚owa kluczowe",
        "Р§Р°С‚ РІРєР»СЋС‡РµРЅ: Р±РѕС‚ РјРѕР¶РµС‚ РѕС‚РїСЂР°РІР»СЏС‚СЊ СЂРµРєР»Р°РјСѓ РІ СЌС‚РѕС‚ С‡Р°С‚": "Czat wЕ‚Д…czony: bot moЕјe wysyЕ‚aД‡ reklamy na ten czat",
        "РЎРѕС…СЂР°РЅРёС‚СЊ С‡Р°С‚": "Zapisz czat",
        "Р РµРєР»Р°РјРЅС‹Р№ С‚РµРєСЃС‚ Рё РєР°СЂС‚РёРЅРєР°": "Tekst reklamowy i obraz",
        "РќР°Р·РІР°РЅРёРµ С‚РµРєСЃС‚Р°": "Nazwa tekstu",
        "Р’СЃРµ": "Wszyscy",
        "РљР»РёРµРЅС‚С‹": "Klienci",
        "РњР°СЃС‚РµСЂР°": "Fachowcy",
        "РњР°С‚РµСЂРёР°Р» РІРєР»СЋС‡РµРЅ: РјРѕР¶РЅРѕ РѕС‚РїСЂР°РІР»СЏС‚СЊ Рё СЃС‚Р°РІРёС‚СЊ РІ СЂР°СЃРїРёСЃР°РЅРёРµ": "MateriaЕ‚ wЕ‚Д…czony: moЕјna go wysyЕ‚aД‡ i dodaД‡ do harmonogramu",
        "РўРµРєСЃС‚ СЂРµРєР»Р°РјС‹": "Tekst reklamy",
        "РўРµРєСЃС‚ СЂРµРєР»Р°РјС‹ / Messenger-РѕС‚РІРµС‚": "Tekst reklamy / odpowiedЕє Messenger",
        "РЎСЃС‹Р»РєР° РЅР° РєР°СЂС‚РёРЅРєСѓ, РЅР°РїСЂРёРјРµСЂ https://ogarniemy.pro/assets/banner.jpg": "Link do obrazu, np. https://ogarniemy.pro/assets/banner.jpg",
        "РЎСЃС‹Р»РєР° РЅР° РєР°СЂС‚РёРЅРєСѓ": "Link do obrazu",
        "РљР°СЂС‚РёРЅРєР° СЃ РєРѕРјРїСЊСЋС‚РµСЂР°": "Obraz z komputera",
        "Р—Р°РіСЂСѓР·РёС‚СЊ РєР°СЂС‚РёРЅРєСѓ СЃ РєРѕРјРїСЊСЋС‚РµСЂР°": "PrzeЕ›lij obraz z komputera",
        "РљР°СЂС‚РёРЅРєР° Р·Р°РіСЂСѓР¶РµРЅР°.": "Obraz zostaЕ‚ przesЕ‚any.",
        "РќРµ СѓРґР°Р»РѕСЃСЊ Р·Р°РіСЂСѓР·РёС‚СЊ РєР°СЂС‚РёРЅРєСѓ.": "Nie udaЕ‚o siД™ przesЕ‚aД‡ obrazu.",
        "РЎРѕС…СЂР°РЅРёС‚СЊ СЂРµРєР»Р°РјРЅС‹Р№ РјР°С‚РµСЂРёР°Р»": "Zapisz materiaЕ‚ reklamowy",
        "Р Р°СЃРїРёСЃР°РЅРёРµ": "Harmonogram",
        "Р Р°СЃРїРёСЃР°РЅРёРµ РїРѕРґРіРѕС‚РѕРІРєРё": "Harmonogram przygotowania",
        "Р Р°СЃРїРёСЃР°РЅРёРµ РѕС‚РїСЂР°РІРєРё": "Harmonogram wysyЕ‚ki",
        "Р’СЂРµРјСЏ РѕС‚РїСЂР°РІРєРё": "Godzina wysyЕ‚ki",
        "Р РµРєР»Р°РјРЅС‹Р№ РјР°С‚РµСЂРёР°Р»": "MateriaЕ‚ reklamowy",
        "Р Р°СЃРїРёСЃР°РЅРёРµ РІРєР»СЋС‡РµРЅРѕ: РѕС‚РїСЂР°РІР»СЏС‚СЊ РєР°Р¶РґС‹Р№ РґРµРЅСЊ РІ СЌС‚Рѕ РІСЂРµРјСЏ": "Harmonogram wЕ‚Д…czony: wysyЕ‚aj codziennie o tej godzinie",
        "Р Р°СЃРїРёСЃР°РЅРёРµ РІРєР»СЋС‡РµРЅРѕ: РіРѕС‚РѕРІРёС‚СЊ РјР°С‚РµСЂРёР°Р» РєР°Р¶РґС‹Р№ РґРµРЅСЊ РІ СЌС‚Рѕ РІСЂРµРјСЏ": "Harmonogram wЕ‚Д…czony: przygotuj materiaЕ‚ codziennie o tej godzinie",
        "РЎРѕС…СЂР°РЅРёС‚СЊ РІСЂРµРјСЏ": "Zapisz czas",
        "РћС‚РїСЂР°РІРёС‚СЊ СЃРµР№С‡Р°СЃ": "WyЕ›lij teraz",
        "РџРѕРґРіРѕС‚РѕРІРёС‚СЊ СЃРµР№С‡Р°СЃ": "Przygotuj teraz",
        "Р§С‚Рѕ СѓР¶Рµ РґРѕР±Р°РІР»РµРЅРѕ": "Co juЕј dodano",
        "Р–СѓСЂРЅР°Р»": "Dziennik",
        "Р’СЃРµ РіРѕСЂРѕРґР°": "Wszystkie miasta",
        "Р“РѕСЂРѕРґР° РїРѕРєР° РЅРµ РґРѕР±Р°РІР»РµРЅС‹.": "Nie dodano jeszcze miast.",
        "Р§Р°С‚С‹": "Czaty",
        "Р§Р°С‚С‹ РїРѕРєР° РЅРµ РґРѕР±Р°РІР»РµРЅС‹.": "Nie dodano jeszcze czatГіw.",
        "Р РµРєР»Р°РјРЅС‹Рµ РјР°С‚РµСЂРёР°Р»С‹": "MateriaЕ‚y reklamowe",
        "РњР°С‚РµСЂРёР°Р»С‹ РїРѕРєР° РЅРµ РґРѕР±Р°РІР»РµРЅС‹.": "Nie dodano jeszcze materiaЕ‚Гіw.",
        "С‚РµРєСЃС‚": "tekst",
        "РїРѕСЃР»РµРґРЅСЏСЏ РѕС‚РїСЂР°РІРєР°": "ostatnia wysyЕ‚ka",
        "РїРѕСЃР»РµРґРЅСЏСЏ РїРѕРґРіРѕС‚РѕРІРєР°": "ostatnie przygotowanie",
        "РЅРµС‚": "brak",
        "Р Р°СЃРїРёСЃР°РЅРёРµ РїРѕРєР° РЅРµ РґРѕР±Р°РІР»РµРЅРѕ.": "Nie dodano jeszcze harmonogramu.",
        "РќР°Р№РґРµРЅРЅС‹Рµ Р·Р°СЏРІРєРё": "Znalezione zlecenia",
        "Р—Р°СЏРІРѕРє РїРѕРєР° РЅРµС‚.": "Na razie brak zleceЕ„.",
        "Р–СѓСЂРЅР°Р» РїРѕРєР° РїСѓСЃС‚.": "Dziennik jest pusty.",
        "Р РµРґР°РєС‚РёСЂРѕРІР°С‚СЊ": "Edytuj",
        "Р’С‹Р±РµСЂРёС‚Рµ СЂРµРєР»Р°РјРЅС‹Р№ С‚РµРєСЃС‚.": "Wybierz tekst reklamy.",
        "РћС‚РїСЂР°РІР»РµРЅРѕ": "WysЕ‚ano",
        "Facebook РѕС‚РїСЂР°РІРєР° РїРѕРґРіРѕС‚РѕРІР»РµРЅР°.": "WysyЕ‚ka Facebook zostaЕ‚a przygotowana.",
        "Facebook Р»СѓС‡С€Рµ РёСЃРїРѕР»СЊР·РѕРІР°С‚СЊ РєР°Рє РІС…РѕРґСЏС‰РёР№ РєР°РЅР°Р»: СЂРµРєР»Р°РјР° РІРµРґРµС‚ РІ Messenger РёР»Рё РЅР° СЃР°Р№С‚, Р±РѕС‚ РѕС‚РІРµС‡Р°РµС‚ С‚РµРј, РєС‚Рѕ СЃР°Рј РЅР°РїРёСЃР°Р». РњР°СЃСЃРѕРІС‹Рµ РµР¶РµРґРЅРµРІРЅС‹Рµ Р»РёС‡РЅС‹Рµ СЃРѕРѕР±С‰РµРЅРёСЏ РЅРµР·РЅР°РєРѕРјС‹Рј Р»СЋРґСЏРј Facebook РѕРіСЂР°РЅРёС‡РёРІР°РµС‚.": "Facebook najlepiej dziaЕ‚a jako kanaЕ‚ przychodzД…cy: reklama prowadzi do Messengera albo na stronД™, a bot odpowiada osobom, ktГіre same napisaЕ‚y. Facebook ogranicza masowe codzienne wiadomoЕ›ci prywatne do nieznajomych.",
        "Facebook-С†РµР»Рё": "Cele Facebook",
        "Facebook РіСЂСѓРїРїС‹": "Grupy Facebook",
        "Р”РѕР±Р°РІРёС‚СЊ Facebook РІ РіСЂСѓРїРїСѓ": "Dodaj grupД™ Facebook",
        "РЎС‚СЂР°РЅРёС†Р° / РєР°РјРїР°РЅРёСЏ / РіСЂСѓРїРїР°": "Strona / kampania / grupa",
        "РЎС‚СЂР°РЅРёС†Р° / РіСЂСѓРїРїР° / СЃСЃС‹Р»РєР°": "Strona / grupa / link",
        "ID РёР»Рё СЃСЃС‹Р»РєР°": "ID lub link",
        "Р—Р°РјРµС‚РєРё: Р°СѓРґРёС‚РѕСЂРёСЏ, Р±СЋРґР¶РµС‚, С‡С‚Рѕ РїСЂРѕРІРµСЂРёС‚СЊ": "Notatki: grupa odbiorcГіw, budЕјet, co sprawdziД‡",
        "Р“СЂСѓРїРїР° РІРєР»СЋС‡РµРЅР°: РјРѕР¶РЅРѕ РёСЃРїРѕР»СЊР·РѕРІР°С‚СЊ РІ РїР»Р°РЅРёСЂРѕРІР°РЅРёРё Facebook": "Grupa wЕ‚Д…czona: moЕјna jej uЕјywaД‡ w planowaniu Facebook",
        "РЎРѕС…СЂР°РЅРёС‚СЊ РіСЂСѓРїРїСѓ": "Zapisz grupД™",
        "Р“СЂСѓРїРїС‹ РїРѕРєР° РЅРµ РґРѕР±Р°РІР»РµРЅС‹.": "Nie dodano jeszcze grup."
      }
    };
    const marketingLocales = { en: "en-US", uk: "uk-UA", ru: "ru-RU", pl: "pl-PL" };
    function marketingLanguage() {
      const stored = localStorage.getItem("language") || "ru";
      return marketingTranslations[stored] ? stored : "ru";
    }
    function mt(value) {
      const lang = marketingLanguage();
      if (!value || lang === "ru") return value;
      return (marketingTranslations[lang] && marketingTranslations[lang][value]) || value;
    }
    function translateMarketingNode(node) {
      if (!node || node.nodeType !== Node.TEXT_NODE) return;
      const parent = node.parentElement;
      if (!parent || ["SCRIPT", "STYLE", "TEXTAREA"].includes(parent.tagName)) return;
      if (parent.closest(".language-corner")) return;
      if (!node.marketingRuText) node.marketingRuText = node.serverRuText || node.nodeValue;
      const translated = mt(node.marketingRuText.trim());
      if (translated !== node.marketingRuText.trim()) {
        node.nodeValue = node.marketingRuText.replace(node.marketingRuText.trim(), translated);
      } else if (marketingLanguage() === "ru") {
        node.nodeValue = node.marketingRuText;
      }
    }
    function translateMarketingElementValue(el, attr) {
      if (!el || !el.hasAttribute(attr)) return;
      const store = "marketingRu" + attr.charAt(0).toUpperCase() + attr.slice(1);
      if (!el.dataset[store]) el.dataset[store] = el.dataset["serverRu" + attr.charAt(0).toUpperCase() + attr.slice(1)] || el.getAttribute(attr);
      el.setAttribute(attr, mt(el.dataset[store]));
    }
    function applyMarketingLanguage(root = document.body) {
      if (!root) return;
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      const nodes = [];
      while (walker.nextNode()) nodes.push(walker.currentNode);
      nodes.forEach(translateMarketingNode);
      root.querySelectorAll("input[placeholder], textarea[placeholder], button[value], input[value]").forEach(el => {
        if (el.hasAttribute("placeholder")) translateMarketingElementValue(el, "placeholder");
        if (el.hasAttribute("value")) translateMarketingElementValue(el, "value");
      });
      document.title = mt(platform === "telegram" ? "Р РµРєР»Р°РјР° Telegram" : "Р РµРєР»Р°РјР° Facebook");
    }
    document.addEventListener("change", event => {
      if (event.target && event.target.id === "languageSelect") setTimeout(() => applyMarketingLanguage(), 0);
    }, false);
    setTimeout(() => applyMarketingLanguage(), 0);
    function adminHeaders(extra = {}) {
      if (!adminPassword) {
        adminPassword = prompt("Admin password") || "";
        sessionStorage.setItem("adminPassword", adminPassword);
      }
      return { "X-Admin-Password": adminPassword, ...extra };
    }
    async function requireAdminAccess(start) {
      while (true) {
        if (!adminPassword) adminPassword = prompt("Admin password") || "";
        if (!adminPassword) { document.body.innerHTML = ""; return; }
        sessionStorage.setItem("adminPassword", adminPassword);
        const res = await fetch("/api/admin/check-password", { headers: { "X-Admin-Password": adminPassword } });
        if (res.ok) { document.body.classList.remove("locked"); start(); return; }
        sessionStorage.removeItem("adminPassword");
        adminPassword = "";
      }
    }
    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    }
    function formatDate(value) {
      return value ? new Date(Number(value) * 1000).toLocaleString(marketingLocales[marketingLanguage()] || "ru-RU") : "";
    }
    async function api(action, data = {}) {
      const res = await fetch(`/api/admin/marketing/${platform}/${action}`, {
        method: "POST",
        headers: adminHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(data)
      });
      const payload = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(payload.error || res.status);
      return payload;
    }
    function readFileAsDataUrl(file) {
      return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = () => reject(reader.error);
        reader.readAsDataURL(file);
      });
    }
    async function uploadSelectedImage() {
      const input = document.getElementById("messageImageFile");
      const status = document.getElementById("messageImageStatus");
      if (!input || !input.files || !input.files[0]) return messageImageUrl.value;
      const file = input.files[0];
      if (status) status.textContent = mt("Р—Р°РіСЂСѓР·РёС‚СЊ РєР°СЂС‚РёРЅРєСѓ СЃ РєРѕРјРїСЊСЋС‚РµСЂР°") + "...";
      const content = await readFileAsDataUrl(file);
      const result = await api("upload-image", {
        name: file.name,
        contentType: file.type,
        content
      });
      messageImageUrl.value = result.url || "";
      input.value = "";
      if (status) status.textContent = mt("РљР°СЂС‚РёРЅРєР° Р·Р°РіСЂСѓР¶РµРЅР°.");
      return messageImageUrl.value;
    }
    async function loadState() {
      const res = await fetch(`/api/admin/marketing/${platform}`, { headers: adminHeaders() });
      state = await res.json();
      render();
      applyMarketingLanguage();
    }
    function cityOptions(selected = "") {
      return `<option value="">${escapeHtml(mt("Р’СЃРµ РіРѕСЂРѕРґР°"))}</option>` + (state.cities || []).map(city => `<option value="${escapeHtml(city.name)}" ${city.name === selected ? "selected" : ""}>${escapeHtml(city.name)}</option>`).join("");
    }
    function messageOptions(selected = "") {
      return (state.messages || []).map(message => `<option value="${message.id}" ${String(message.id) === String(selected) ? "selected" : ""}>#${message.id} ${escapeHtml(message.title)}</option>`).join("");
    }
    async function saveCity(event) {
      event.preventDefault();
      await api("city", { name: cityName.value, enabled: true });
      cityName.value = "";
      loadState();
    }
    async function saveMessage(event) {
      event.preventDefault();
      try {
        await uploadSelectedImage();
      } catch (error) {
        alert(mt("РќРµ СѓРґР°Р»РѕСЃСЊ Р·Р°РіСЂСѓР·РёС‚СЊ РєР°СЂС‚РёРЅРєСѓ."));
        return;
      }
      await api("message", {
        id: messageId.value || null,
        title: messageTitle.value,
        audience: messageAudience.value,
        body: messageBody.value,
        imageUrl: messageImageUrl.value,
        enabled: messageEnabled.checked
      });
      messageId.value = "";
      messageTitle.value = "";
      messageBody.value = "";
      messageImageUrl.value = "";
      if (document.getElementById("messageImageFile")) document.getElementById("messageImageFile").value = "";
      if (document.getElementById("messageImageStatus")) document.getElementById("messageImageStatus").textContent = "";
      messageEnabled.checked = true;
      loadState();
    }
    async function saveSchedule(event) {
      event.preventDefault();
      await api("schedule", {
        id: scheduleId.value || null,
        city: scheduleCity.value,
        sendTime: scheduleTime.value,
        messageId: scheduleMessage.value,
        enabled: scheduleEnabled.checked
      });
      scheduleId.value = "";
      scheduleTime.value = "09:30";
      scheduleEnabled.checked = true;
      loadState();
    }
    async function sendNow() {
      if (!sendMessage.value) return alert(mt("Р’С‹Р±РµСЂРёС‚Рµ СЂРµРєР»Р°РјРЅС‹Р№ С‚РµРєСЃС‚."));
      const result = await api("send-now", { city: sendCity.value, messageId: sendMessage.value });
      alert(platform === "telegram" ? `${mt("РћС‚РїСЂР°РІР»РµРЅРѕ")}: ${result.sent}/${result.total}` : mt("Facebook РѕС‚РїСЂР°РІРєР° РїРѕРґРіРѕС‚РѕРІР»РµРЅР°."));
      loadState();
    }
    function editMessage(id) {
      const message = (state.messages || []).find(item => item.id === id);
      if (!message) return;
      messageId.value = message.id;
      messageTitle.value = message.title || "";
      messageAudience.value = message.audience || "all";
      messageBody.value = message.body || "";
      messageImageUrl.value = message.image_url || "";
      messageEnabled.checked = !!message.enabled;
      window.scrollTo({ top: 0, behavior: "smooth" });
    }
    function editSchedule(id) {
      const item = (state.schedules || []).find(row => row.id === id);
      if (!item) return;
      scheduleId.value = item.id;
      scheduleCity.value = item.city || "";
      scheduleTime.value = item.send_time || "09:30";
      scheduleMessage.value = item.message_id || "";
      scheduleEnabled.checked = !!item.enabled;
    }
"""


TELEGRAM_ADS_HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Р РµРєР»Р°РјР° Telegram</title>
  <style>""" + MARKETING_PAGE_STYLE + r"""</style>
</head>
<body class="locked">
  <header>
    <h1>Р РµРєР»Р°РјР° Telegram</h1>
    <nav><a href="/server">Р—Р°РґР°РЅРёСЏ</a> <a href="/users">РЎРѕС‚СЂСѓРґРЅРёРєРё</a> <a href="/clients">РљР»РёРµРЅС‚С‹</a> <a href="/completed">Р’С‹РїРѕР»РЅРµРЅРЅС‹Рµ Р·Р°РґР°РЅРёСЏ</a> <a href="/calculations">Р Р°СЃС‡РµС‚С‹ СЃРѕС‚СЂСѓРґРЅРёРєРѕРІ</a> <a href="/client-calculations">Р Р°СЃС‡РµС‚С‹ РєР»РёРµРЅС‚РѕРІ</a> <a href="/telegram-ads">Р РµРєР»Р°РјР° Telegram</a> <a href="/facebook-ads">Р РµРєР»Р°РјР° Facebook</a> <a href="/telegram-login">Telegram userbot</a> <a href="/settings">РќР°СЃС‚СЂРѕР№РєРё</a></nav>
  </header>
  <main>
    <section class="grid">
      <div class="panel"><h2>Р“РѕСЂРѕРґР°</h2><form onsubmit="saveCity(event)"><input id="cityName" placeholder="Warszawa" required><button>Р”РѕР±Р°РІРёС‚СЊ РіРѕСЂРѕРґ</button></form><div id="cities" class="cards"></div></div>
      <div class="panel"><h2>Р”РѕР±Р°РІРёС‚СЊ Telegram-С‡Р°С‚</h2><form id="groupForm"><input id="groupTitle" placeholder="РќР°Р·РІР°РЅРёРµ РіСЂСѓРїРїС‹" required><input id="groupChatId" placeholder="Chat ID, РЅР°РїСЂРёРјРµСЂ -100..." required><select id="groupCity"></select><label class="field">РљР»СЋС‡РµРІС‹Рµ СЃР»РѕРІР°<textarea id="groupKeywords" placeholder="РјР°СЃС‚РµСЂ, СЃР°РЅС‚РµС…РЅРёРє, СЌР»РµРєС‚СЂРёРє, СЂРµРјРѕРЅС‚"></textarea></label><label><input id="groupEnabled" type="checkbox" checked> Р§Р°С‚ РІРєР»СЋС‡РµРЅ: Р±РѕС‚ РјРѕР¶РµС‚ РѕС‚РїСЂР°РІР»СЏС‚СЊ СЂРµРєР»Р°РјСѓ РІ СЌС‚РѕС‚ С‡Р°С‚</label><button>РЎРѕС…СЂР°РЅРёС‚СЊ С‡Р°С‚</button></form></div>
      <div class="panel full"><h2>Р РµРєР»Р°РјРЅС‹Р№ С‚РµРєСЃС‚ Рё РєР°СЂС‚РёРЅРєР°</h2><form onsubmit="saveMessage(event)"><input id="messageId" type="hidden"><div class="row"><input id="messageTitle" placeholder="РќР°Р·РІР°РЅРёРµ С‚РµРєСЃС‚Р°" required><select id="messageAudience"><option value="all">Р’СЃРµ</option><option value="clients">РљР»РёРµРЅС‚С‹</option><option value="workers">РњР°СЃС‚РµСЂР°</option></select><label><input id="messageEnabled" type="checkbox" checked> РњР°С‚РµСЂРёР°Р» РІРєР»СЋС‡РµРЅ: РјРѕР¶РЅРѕ РѕС‚РїСЂР°РІР»СЏС‚СЊ Рё СЃС‚Р°РІРёС‚СЊ РІ СЂР°СЃРїРёСЃР°РЅРёРµ</label></div><label class="field">РўРµРєСЃС‚ СЂРµРєР»Р°РјС‹<textarea id="messageBody" placeholder="РўРµРєСЃС‚ СЂРµРєР»Р°РјС‹" required></textarea></label><div class="upload-row"><label class="field">РЎСЃС‹Р»РєР° РЅР° РєР°СЂС‚РёРЅРєСѓ<input id="messageImageUrl" placeholder="РЎСЃС‹Р»РєР° РЅР° РєР°СЂС‚РёРЅРєСѓ, РЅР°РїСЂРёРјРµСЂ https://ogarniemy.pro/assets/banner.jpg"></label><label class="field">РљР°СЂС‚РёРЅРєР° СЃ РєРѕРјРїСЊСЋС‚РµСЂР°<input id="messageImageFile" type="file" accept="image/png,image/jpeg,image/webp,image/gif"></label></div><p id="messageImageStatus" class="meta upload-status"></p><button>РЎРѕС…СЂР°РЅРёС‚СЊ СЂРµРєР»Р°РјРЅС‹Р№ РјР°С‚РµСЂРёР°Р»</button></form></div>
      <div class="panel"><h2>Р Р°СЃРїРёСЃР°РЅРёРµ</h2><form onsubmit="saveSchedule(event)"><input id="scheduleId" type="hidden"><label class="field">Р“РѕСЂРѕРґ<select id="scheduleCity"></select></label><label class="field">Р’СЂРµРјСЏ РѕС‚РїСЂР°РІРєРё<input id="scheduleTime" type="time" value="09:30" required></label><label class="field">Р РµРєР»Р°РјРЅС‹Р№ РјР°С‚РµСЂРёР°Р»<select id="scheduleMessage"></select></label><label><input id="scheduleEnabled" type="checkbox" checked> Р Р°СЃРїРёСЃР°РЅРёРµ РІРєР»СЋС‡РµРЅРѕ: РѕС‚РїСЂР°РІР»СЏС‚СЊ РєР°Р¶РґС‹Р№ РґРµРЅСЊ РІ СЌС‚Рѕ РІСЂРµРјСЏ</label><button>РЎРѕС…СЂР°РЅРёС‚СЊ РІСЂРµРјСЏ</button></form></div>
      <div class="panel"><h2>РћС‚РїСЂР°РІРёС‚СЊ СЃРµР№С‡Р°СЃ</h2><label class="field">Р“РѕСЂРѕРґ<select id="sendCity"></select></label><label class="field">Р РµРєР»Р°РјРЅС‹Р№ РјР°С‚РµСЂРёР°Р»<select id="sendMessage"></select></label><button class="success" onclick="sendNow()">РћС‚РїСЂР°РІРёС‚СЊ СЃРµР№С‡Р°СЃ</button></div>
      <div class="panel full"><h2>Р§С‚Рѕ СѓР¶Рµ РґРѕР±Р°РІР»РµРЅРѕ</h2><div id="summary" class="cards"></div></div>
      <div class="panel full"><h2>Р–СѓСЂРЅР°Р»</h2><div id="logs" class="cards"></div></div>
    </section>
  </main>
  <script>
    const platform = "telegram";
""" + MARKETING_PAGE_SCRIPT + r"""
    groupForm.addEventListener("submit", async event => {
      event.preventDefault();
      await api("telegram-group", { title: groupTitle.value, chatId: groupChatId.value, city: groupCity.value, keywords: groupKeywords.value, enabled: groupEnabled.checked });
      groupTitle.value = ""; groupChatId.value = ""; groupKeywords.value = ""; groupEnabled.checked = true;
      loadState();
    });
    function render() {
      const citySelects = [groupCity, scheduleCity, sendCity];
      citySelects.forEach(select => select.innerHTML = cityOptions(select.value));
      [scheduleMessage, sendMessage].forEach(select => select.innerHTML = messageOptions(select.value));
      cities.innerHTML = (state.cities || []).map(city => `<span class="pill">${escapeHtml(city.name)}</span>`).join("") || "<p class='meta'>Р“РѕСЂРѕРґР° РїРѕРєР° РЅРµ РґРѕР±Р°РІР»РµРЅС‹.</p>";
      summary.innerHTML = `
        <h3>Р§Р°С‚С‹</h3>${(state.groups || []).map(group => `<article class="item ${group.enabled ? "" : "off"}"><strong>${escapeHtml(group.title || group.chat_id)}</strong><p class="meta">${escapeHtml(group.city)} В· ${escapeHtml(group.chat_id)}</p><p>${escapeHtml(group.keywords || "")}</p></article>`).join("") || "<p class='meta'>Р§Р°С‚С‹ РїРѕРєР° РЅРµ РґРѕР±Р°РІР»РµРЅС‹.</p>"}
        <h3>Р РµРєР»Р°РјРЅС‹Рµ РјР°С‚РµСЂРёР°Р»С‹</h3>${(state.messages || []).map(message => `<article class="item ${message.enabled ? "" : "off"}"><strong>#${message.id} ${escapeHtml(message.title)}</strong><p>${escapeHtml(message.body)}</p>${message.image_url ? `<img class="preview" src="${escapeHtml(message.image_url)}">` : ""}<button class="secondary" onclick="editMessage(${message.id})">Р РµРґР°РєС‚РёСЂРѕРІР°С‚СЊ</button></article>`).join("") || "<p class='meta'>РњР°С‚РµСЂРёР°Р»С‹ РїРѕРєР° РЅРµ РґРѕР±Р°РІР»РµРЅС‹.</p>"}
        <h3>Р Р°СЃРїРёСЃР°РЅРёРµ</h3>${(state.schedules || []).map(item => `<article class="item ${item.enabled ? "" : "off"}"><strong>${escapeHtml(item.send_time)}</strong><p class="meta">${escapeHtml(item.city || "Р’СЃРµ РіРѕСЂРѕРґР°")} В· С‚РµРєСЃС‚ #${item.message_id} В· РїРѕСЃР»РµРґРЅСЏСЏ РѕС‚РїСЂР°РІРєР°: ${escapeHtml(item.last_sent_date || "РЅРµС‚")}</p><button class="secondary" onclick="editSchedule(${item.id})">Р РµРґР°РєС‚РёСЂРѕРІР°С‚СЊ</button></article>`).join("") || "<p class='meta'>Р Р°СЃРїРёСЃР°РЅРёРµ РїРѕРєР° РЅРµ РґРѕР±Р°РІР»РµРЅРѕ.</p>"}
        <h3>РќР°Р№РґРµРЅРЅС‹Рµ Р·Р°СЏРІРєРё</h3>${(state.hits || []).map(hit => `<article class="item"><strong>${escapeHtml(hit.keyword)}</strong><p class="meta">${formatDate(hit.created_at)} В· ${escapeHtml(hit.username || "")}</p><p>${escapeHtml(hit.message || "")}</p></article>`).join("") || "<p class='meta'>Р—Р°СЏРІРѕРє РїРѕРєР° РЅРµС‚.</p>"}
      `;
      logs.innerHTML = (state.logs || []).map(log => `<article class="item"><strong>${escapeHtml(log.action)} В· ${escapeHtml(log.status)}</strong><p class="meta">${formatDate(log.created_at)} В· ${escapeHtml(log.city || "")} В· ${escapeHtml(log.target_type)}</p><p>${escapeHtml(log.detail || "")}</p></article>`).join("") || "<p class='meta'>Р–СѓСЂРЅР°Р» РїРѕРєР° РїСѓСЃС‚.</p>";
    }
    requireAdminAccess(loadState);
  </script>
</body>
</html>"""


FACEBOOK_ADS_HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Р РµРєР»Р°РјР° Facebook</title>
  <style>""" + MARKETING_PAGE_STYLE + r"""</style>
</head>
<body class="locked">
  <header>
    <h1>Р РµРєР»Р°РјР° Facebook</h1>
    <nav><a href="/server">Р—Р°РґР°РЅРёСЏ</a> <a href="/users">РЎРѕС‚СЂСѓРґРЅРёРєРё</a> <a href="/clients">РљР»РёРµРЅС‚С‹</a> <a href="/completed">Р’С‹РїРѕР»РЅРµРЅРЅС‹Рµ Р·Р°РґР°РЅРёСЏ</a> <a href="/calculations">Р Р°СЃС‡РµС‚С‹ СЃРѕС‚СЂСѓРґРЅРёРєРѕРІ</a> <a href="/client-calculations">Р Р°СЃС‡РµС‚С‹ РєР»РёРµРЅС‚РѕРІ</a> <a href="/telegram-ads">Р РµРєР»Р°РјР° Telegram</a> <a href="/facebook-ads">Р РµРєР»Р°РјР° Facebook</a> <a href="/telegram-login">Telegram userbot</a> <a href="/settings">РќР°СЃС‚СЂРѕР№РєРё</a></nav>
  </header>
  <main>
    <div class="note">Facebook Р»СѓС‡С€Рµ РёСЃРїРѕР»СЊР·РѕРІР°С‚СЊ РєР°Рє РІС…РѕРґСЏС‰РёР№ РєР°РЅР°Р»: СЂРµРєР»Р°РјР° РІРµРґРµС‚ РІ Messenger РёР»Рё РЅР° СЃР°Р№С‚, Р±РѕС‚ РѕС‚РІРµС‡Р°РµС‚ С‚РµРј, РєС‚Рѕ СЃР°Рј РЅР°РїРёСЃР°Р». РњР°СЃСЃРѕРІС‹Рµ РµР¶РµРґРЅРµРІРЅС‹Рµ Р»РёС‡РЅС‹Рµ СЃРѕРѕР±С‰РµРЅРёСЏ РЅРµР·РЅР°РєРѕРјС‹Рј Р»СЋРґСЏРј Facebook РѕРіСЂР°РЅРёС‡РёРІР°РµС‚.</div>
    <section class="grid">
      <div class="panel"><h2>Р“РѕСЂРѕРґР°</h2><form onsubmit="saveCity(event)"><input id="cityName" placeholder="Warszawa" required><button>Р”РѕР±Р°РІРёС‚СЊ РіРѕСЂРѕРґ</button></form><div id="cities" class="cards"></div></div>
      <div class="panel"><h2>Р”РѕР±Р°РІРёС‚СЊ Facebook РІ РіСЂСѓРїРїСѓ</h2><form id="targetForm"><input id="targetName" placeholder="РЎС‚СЂР°РЅРёС†Р° / РіСЂСѓРїРїР° / СЃСЃС‹Р»РєР°" required><input id="targetId" placeholder="ID РёР»Рё СЃСЃС‹Р»РєР°"><select id="targetCity"></select><textarea id="targetNotes" placeholder="Р—Р°РјРµС‚РєРё: Р°СѓРґРёС‚РѕСЂРёСЏ, Р±СЋРґР¶РµС‚, С‡С‚Рѕ РїСЂРѕРІРµСЂРёС‚СЊ"></textarea><label><input id="targetEnabled" type="checkbox" checked> Р“СЂСѓРїРїР° РІРєР»СЋС‡РµРЅР°: РјРѕР¶РЅРѕ РёСЃРїРѕР»СЊР·РѕРІР°С‚СЊ РІ РїР»Р°РЅРёСЂРѕРІР°РЅРёРё Facebook</label><button>РЎРѕС…СЂР°РЅРёС‚СЊ РіСЂСѓРїРїСѓ</button></form></div>
      <div class="panel full"><h2>Р РµРєР»Р°РјРЅС‹Р№ С‚РµРєСЃС‚ Рё РєР°СЂС‚РёРЅРєР°</h2><form onsubmit="saveMessage(event)"><input id="messageId" type="hidden"><div class="row"><input id="messageTitle" placeholder="РќР°Р·РІР°РЅРёРµ С‚РµРєСЃС‚Р°" required><select id="messageAudience"><option value="all">Р’СЃРµ</option><option value="clients">РљР»РёРµРЅС‚С‹</option><option value="workers">РњР°СЃС‚РµСЂР°</option></select><label><input id="messageEnabled" type="checkbox" checked> РњР°С‚РµСЂРёР°Р» РІРєР»СЋС‡РµРЅ: РјРѕР¶РЅРѕ РѕС‚РїСЂР°РІР»СЏС‚СЊ Рё СЃС‚Р°РІРёС‚СЊ РІ СЂР°СЃРїРёСЃР°РЅРёРµ</label></div><label class="field">РўРµРєСЃС‚ СЂРµРєР»Р°РјС‹<textarea id="messageBody" placeholder="РўРµРєСЃС‚ СЂРµРєР»Р°РјС‹ / Messenger-РѕС‚РІРµС‚" required></textarea></label><div class="upload-row"><label class="field">РЎСЃС‹Р»РєР° РЅР° РєР°СЂС‚РёРЅРєСѓ<input id="messageImageUrl" placeholder="РЎСЃС‹Р»РєР° РЅР° РєР°СЂС‚РёРЅРєСѓ"></label><label class="field">РљР°СЂС‚РёРЅРєР° СЃ РєРѕРјРїСЊСЋС‚РµСЂР°<input id="messageImageFile" type="file" accept="image/png,image/jpeg,image/webp,image/gif"></label></div><p id="messageImageStatus" class="meta upload-status"></p><button>РЎРѕС…СЂР°РЅРёС‚СЊ СЂРµРєР»Р°РјРЅС‹Р№ РјР°С‚РµСЂРёР°Р»</button></form></div>
      <div class="panel"><h2>Р Р°СЃРїРёСЃР°РЅРёРµ РѕС‚РїСЂР°РІРєРё</h2><form onsubmit="saveSchedule(event)"><input id="scheduleId" type="hidden"><label class="field">Р“РѕСЂРѕРґ<select id="scheduleCity"></select></label><label class="field">Р’СЂРµРјСЏ РѕС‚РїСЂР°РІРєРё<input id="scheduleTime" type="time" value="10:00" required></label><label class="field">Р РµРєР»Р°РјРЅС‹Р№ РјР°С‚РµСЂРёР°Р»<select id="scheduleMessage"></select></label><label><input id="scheduleEnabled" type="checkbox" checked> Р Р°СЃРїРёСЃР°РЅРёРµ РІРєР»СЋС‡РµРЅРѕ: РѕС‚РїСЂР°РІР»СЏС‚СЊ РєР°Р¶РґС‹Р№ РґРµРЅСЊ РІ СЌС‚Рѕ РІСЂРµРјСЏ</label><button>РЎРѕС…СЂР°РЅРёС‚СЊ РІСЂРµРјСЏ</button></form></div>
      <div class="panel"><h2>РћС‚РїСЂР°РІРёС‚СЊ СЃРµР№С‡Р°СЃ</h2><label class="field">Р“РѕСЂРѕРґ<select id="sendCity"></select></label><label class="field">Р РµРєР»Р°РјРЅС‹Р№ РјР°С‚РµСЂРёР°Р»<select id="sendMessage"></select></label><button class="success" onclick="sendNow()">РћС‚РїСЂР°РІРёС‚СЊ СЃРµР№С‡Р°СЃ</button></div>
      <div class="panel full"><h2>Р§С‚Рѕ СѓР¶Рµ РґРѕР±Р°РІР»РµРЅРѕ</h2><div id="summary" class="cards"></div></div>
      <div class="panel full"><h2>Р–СѓСЂРЅР°Р»</h2><div id="logs" class="cards"></div></div>
    </section>
  </main>
  <script>
    const platform = "facebook";
""" + MARKETING_PAGE_SCRIPT + r"""
    targetForm.addEventListener("submit", async event => {
      event.preventDefault();
      await api("facebook-target", { name: targetName.value, targetId: targetId.value, city: targetCity.value, notes: targetNotes.value, enabled: targetEnabled.checked });
      targetName.value = ""; targetId.value = ""; targetNotes.value = ""; targetEnabled.checked = true;
      loadState();
    });
    function render() {
      [targetCity, scheduleCity, sendCity].forEach(select => select.innerHTML = cityOptions(select.value));
      [scheduleMessage, sendMessage].forEach(select => select.innerHTML = messageOptions(select.value));
      cities.innerHTML = (state.cities || []).map(city => `<span class="pill">${escapeHtml(city.name)}</span>`).join("") || "<p class='meta'>Р“РѕСЂРѕРґР° РїРѕРєР° РЅРµ РґРѕР±Р°РІР»РµРЅС‹.</p>";
      summary.innerHTML = `
        <h3>Facebook РіСЂСѓРїРїС‹</h3>${(state.targets || []).map(target => `<article class="item ${target.enabled ? "" : "off"}"><strong>${escapeHtml(target.name)}</strong><p class="meta">${escapeHtml(target.city)} В· ${escapeHtml(target.target_id || "")}</p><p>${escapeHtml(target.notes || "")}</p></article>`).join("") || "<p class='meta'>Р“СЂСѓРїРїС‹ РїРѕРєР° РЅРµ РґРѕР±Р°РІР»РµРЅС‹.</p>"}
        <h3>Р РµРєР»Р°РјРЅС‹Рµ РјР°С‚РµСЂРёР°Р»С‹</h3>${(state.messages || []).map(message => `<article class="item ${message.enabled ? "" : "off"}"><strong>#${message.id} ${escapeHtml(message.title)}</strong><p>${escapeHtml(message.body)}</p>${message.image_url ? `<img class="preview" src="${escapeHtml(message.image_url)}">` : ""}<button class="secondary" onclick="editMessage(${message.id})">Р РµРґР°РєС‚РёСЂРѕРІР°С‚СЊ</button></article>`).join("") || "<p class='meta'>РњР°С‚РµСЂРёР°Р»С‹ РїРѕРєР° РЅРµ РґРѕР±Р°РІР»РµРЅС‹.</p>"}
        <h3>Р Р°СЃРїРёСЃР°РЅРёРµ</h3>${(state.schedules || []).map(item => `<article class="item ${item.enabled ? "" : "off"}"><strong>${escapeHtml(item.send_time)}</strong><p class="meta">${escapeHtml(item.city || "Р’СЃРµ РіРѕСЂРѕРґР°")} В· С‚РµРєСЃС‚ #${item.message_id} В· РїРѕСЃР»РµРґРЅСЏСЏ РїРѕРґРіРѕС‚РѕРІРєР°: ${escapeHtml(item.last_sent_date || "РЅРµС‚")}</p><button class="secondary" onclick="editSchedule(${item.id})">Р РµРґР°РєС‚РёСЂРѕРІР°С‚СЊ</button></article>`).join("") || "<p class='meta'>Р Р°СЃРїРёСЃР°РЅРёРµ РїРѕРєР° РЅРµ РґРѕР±Р°РІР»РµРЅРѕ.</p>"}
      `;
      logs.innerHTML = (state.logs || []).map(log => `<article class="item"><strong>${escapeHtml(log.action)} В· ${escapeHtml(log.status)}</strong><p class="meta">${formatDate(log.created_at)} В· ${escapeHtml(log.city || "")} В· ${escapeHtml(log.target_type)}</p><p>${escapeHtml(log.detail || "")}</p></article>`).join("") || "<p class='meta'>Р–СѓСЂРЅР°Р» РїРѕРєР° РїСѓСЃС‚.</p>";
    }
    requireAdminAccess(loadState);
  </script>
</body>
</html>"""


TELEGRAM_LOGIN_HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Вход Telegram userbot</title>
  <style>
    :root { --ink:#172026; --muted:#5f6b7a; --line:#d7dde6; --gold:#f6c85f; --gold-dark:#d9a928; --blue:#163b66; --green:#1f8a70; --red:#b42318; --panel:#ffffff; }
    * { box-sizing: border-box; }
    body { margin: 0; font-family: Arial, sans-serif; color: var(--ink); background: linear-gradient(180deg, #eef3f7 0%, #f8fafc 45%, #ffffff 100%); }
    header { position: relative; background: linear-gradient(135deg, #163b66 0%, #1f8a70 100%); color: white; padding: 24px 28px 26px; box-shadow: 0 14px 34px rgba(22, 59, 102, 0.18); }
    h1 { margin: 0 0 14px; font-size: 28px; line-height: 1.15; letter-spacing: 0; }
    nav { display: grid; grid-template-columns: repeat(10, minmax(104px, 1fr)); gap: 8px; align-items: stretch; }
    nav a { display: flex; align-items: center; justify-content: center; min-height: 40px; padding: 8px 10px; border-radius: 8px; background: var(--gold); color: var(--ink); text-decoration: none; font-size: 13px; font-weight: 700; line-height: 1.15; text-align: center; box-shadow: 0 6px 14px rgba(23, 32, 38, 0.12); }
    nav a:hover { background: #ffd86f; }
    main { max-width: 920px; margin: 0 auto; padding: 24px; }
    section { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 20px; margin-bottom: 16px; box-shadow: 0 10px 24px rgba(23, 32, 38, 0.06); }
    h2 { margin: 0 0 12px; font-size: 18px; line-height: 1.25; }
    label { display: grid; gap: 6px; font-size: 14px; font-weight: 700; margin-bottom: 12px; }
    input { width: 100%; border: 1px solid #c8d1dd; border-radius: 7px; padding: 11px 12px; font: inherit; background: #fff; color: var(--ink); }
    button { border: 0; border-radius: 7px; min-height: 42px; padding: 10px 16px; font: inherit; font-weight: 700; cursor: pointer; background: var(--gold); color: var(--ink); box-shadow: 0 6px 14px rgba(23, 32, 38, 0.12); }
    button:hover { background: #ffd86f; }
    button.secondary { background: #e8eef5; color: var(--blue); }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    .status { border-left: 4px solid var(--blue); background: #eef5fb; padding: 12px; border-radius: 7px; margin-bottom: 14px; line-height: 1.45; }
    .error { border-left-color: var(--red); background: #fff1f0; color: #8a1f16; }
    .ok { border-left-color: var(--green); background: #edf8f4; color: #14634f; }
    .muted { color: var(--muted); font-size: 13px; line-height: 1.45; }
    .hidden { display: none; }
    @media (max-width: 1180px) { nav { grid-template-columns: repeat(5, minmax(120px, 1fr)); } }
    @media (max-width: 680px) { header { padding: 20px 16px; } h1 { font-size: 24px; } nav { grid-template-columns: repeat(2, minmax(0, 1fr)); } nav a { min-height: 42px; font-size: 12px; } .row { grid-template-columns: 1fr; } main { padding: 16px; } }
  </style>
</head>
<body>
  <header>
    <h1>Вход Telegram userbot</h1>
    <nav><a href="/server">Задания</a><a href="/users">Сотрудники</a><a href="/clients">Клиенты</a><a href="/completed">Выполненные задания</a><a href="/calculations">Расчеты сотрудников</a><a href="/client-calculations">Расчеты клиентов</a><a href="/telegram-ads">Реклама Telegram</a><a href="/facebook-ads">Реклама Facebook</a><a href="/telegram-login">Telegram userbot</a><a href="/settings">Настройки</a></nav>
  </header>
  <main>
    <section>
      <h2>Состояние</h2>
      <div id="status" class="status">Проверяю подключение Telegram.</div>
      <button class="secondary" onclick="loadStatus()">Обновить состояние</button>
    </section>
    <section>
      <h2>1. Отправить код</h2>
      <div class="row">
        <label>API ID
          <input id="apiId" inputmode="numeric" placeholder="123456">
        </label>
        <label>API Hash
          <input id="apiHash" placeholder="abcdef123456...">
        </label>
      </div>
      <div class="row">
        <label>Номер телефона Telegram
          <input id="phone" autocomplete="tel" placeholder="+48123456789">
        </label>
        <label>Имя сессии
          <input id="sessionName" value="ogarniemy_userbot">
        </label>
      </div>
      <p class="muted">API ID и API Hash берутся на my.telegram.org в разделе API development tools. Код придет в Telegram.</p>
      <button onclick="startLogin()">Отправить код</button>
    </section>
    <section id="codeSection" class="hidden">
      <h2>2. Подтвердить вход</h2>
      <label>Код из Telegram
        <input id="code" inputmode="numeric" autocomplete="one-time-code">
      </label>
      <label>Облачный пароль Telegram, если включен
        <input id="tgPassword" type="password" autocomplete="current-password">
      </label>
      <button onclick="completeLogin()">Сохранить личный аккаунт</button>
    </section>
  </main>
  <script>
    let loginId = "";
    function adminHeaders(extra = {}) {
      return { "Content-Type": "application/json", ...extra };
    }
    function setStatus(text, kind = "") {
      status.className = "status " + kind;
      status.textContent = text;
    }
    async function loadStatus() {
      try {
        const res = await fetch("/api/admin/telegram-login/status", { headers: adminHeaders() });
        const data = await res.json();
        if (!res.ok) return setStatus(data.detail || "Не удалось проверить Telegram userbot.", "error");
        const sessionText = data.sessionExists ? "сессия сохранена" : "сессии пока нет";
        const configText = data.configured ? "API настроен" : "API еще не настроен";
        setStatus(`${configText}, ${sessionText}. Сессия: ${data.session || "ogarniemy_userbot"}${data.phone ? ", телефон: " + data.phone : ""}.`, data.sessionExists ? "ok" : "");
        if (data.session) sessionName.value = data.session;
      } catch (err) {
        setStatus("Не удалось связаться с сервером Telegram userbot.", "error");
      }
    }
    async function startLogin() {
      if (!apiId.value.trim() || !apiHash.value.trim() || !phone.value.trim()) {
        return setStatus("Введите API ID, API Hash и номер телефона.", "error");
      }
      setStatus("Отправляю код в Telegram...");
      const res = await fetch("/api/admin/telegram-login/start", {
        method: "POST",
        headers: adminHeaders(),
        body: JSON.stringify({ apiId: apiId.value, apiHash: apiHash.value, phone: phone.value, session: sessionName.value || "ogarniemy_userbot" })
      });
      const data = await res.json();
      if (!res.ok) return setStatus(data.detail || "Не удалось отправить код.", "error");
      loginId = data.loginId;
      codeSection.classList.remove("hidden");
      code.focus();
      setStatus(`Код отправлен на ${data.phone}. Введите его ниже.`, "ok");
    }
    async function completeLogin() {
      if (!loginId) return setStatus("Сначала отправьте код.", "error");
      setStatus("Проверяю код...");
      const res = await fetch("/api/admin/telegram-login/complete", {
        method: "POST",
        headers: adminHeaders(),
        body: JSON.stringify({ loginId, code: code.value, password: tgPassword.value })
      });
      const data = await res.json();
      if (data.passwordRequired) {
        tgPassword.focus();
        return setStatus("Telegram просит облачный пароль. Введите его и нажмите кнопку еще раз.", "error");
      }
      if (!res.ok || !data.ok) return setStatus(data.detail || "Не удалось подтвердить код.", "error");
      const user = data.user || {};
      setStatus(`Готово. Сессия сохранена для ${user.username ? "@" + user.username : user.id}.`, "ok");
      codeSection.classList.add("hidden");
      loadStatus();
    }
    window.addEventListener("DOMContentLoaded", loadStatus);
  </script>
</body>
</html>
"""
SETTINGS_HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>РќР°СЃС‚СЂРѕР№РєРё</title>
  <style>
    :root { --ink: #172026; --teal: #0f766e; --gold: #f6c85f; --paper: #fffaf0; }
    body { font-family: Arial, sans-serif; margin: 0; color: var(--ink); background: linear-gradient(135deg, #fff7df 0%, #d8f8eb 48%, #e9d5ff 100%); min-height: 100vh; }
    body.locked header, body.locked main { display: none; }
    header { background: linear-gradient(135deg, #0f766e 0%, #2563eb 48%, #7c3aed 100%); color: white; padding: 24px 28px; }
    main { max-width: 760px; margin: 0 auto; padding: 24px; }
    nav { display: grid; grid-template-columns: repeat(10, minmax(104px, 1fr)); gap: 10px; margin-top: 12px; max-width: 1320px; }
    nav a { display: flex; align-items: center; justify-content: center; min-height: 44px; box-sizing: border-box; color: var(--ink); background: var(--gold); font-weight: bold; padding: 8px 10px; border-radius: 8px; text-align: center; line-height: 1.15; text-decoration: none; }
    form { background: rgba(255,255,255,.94); border-left: 6px solid var(--teal); border-radius: 8px; padding: 18px; box-shadow: 0 14px 34px rgba(23,32,38,.1); }
    label { display: block; font-weight: 700; margin-top: 14px; }
    select, input, textarea, button { font: inherit; padding: 11px 13px; border-radius: 8px; border: 1px solid rgba(23,32,38,.18); box-sizing: border-box; }
    select, input[type="text"], input[type="password"], input[type="number"] { width: 100%; margin-top: 6px; background: white; }
    button { margin-top: 18px; background: linear-gradient(135deg, var(--teal), #2563eb); color: white; border: 0; cursor: pointer; font-weight: 700; }
    .meta { color: #60717d; font-size: 14px; }
    @media (max-width: 980px) { nav { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
  </style>
</head>
<body class="locked">
  <header>
    <h1>РќР°СЃС‚СЂРѕР№РєРё</h1>
    <nav><a href="/server">Р—Р°РґР°РЅРёСЏ</a> <a href="/users">РЎРѕС‚СЂСѓРґРЅРёРєРё</a> <a href="/clients">РљР»РёРµРЅС‚С‹</a> <a href="/completed">Р’С‹РїРѕР»РЅРµРЅРЅС‹Рµ Р·Р°РґР°РЅРёСЏ</a> <a href="/calculations">Р Р°СЃС‡РµС‚С‹ СЃРѕС‚СЂСѓРґРЅРёРєРѕРІ</a> <a href="/client-calculations">Р Р°СЃС‡РµС‚С‹ РєР»РёРµРЅС‚РѕРІ</a> <a href="/telegram-ads">Р РµРєР»Р°РјР° Telegram</a> <a href="/facebook-ads">Р РµРєР»Р°РјР° Facebook</a> <a href="/telegram-login">Telegram userbot</a> <a href="/settings">РќР°СЃС‚СЂРѕР№РєРё</a></nav>
  </header>
  <main>
    <form id="settingsForm">
      <label>Р’Р°Р»СЋС‚Р°
        <select id="currency">
          <option value="RUB">RUB</option>
          <option value="USD">USD</option>
          <option value="EUR">EUR</option>
          <option value="PLN">PLN</option>
          <option value="UAH">UAH</option>
        </select>
      </label>
      <label>Р•РґРёРЅРёС†Р° СЂРµР·РµСЂРІР°
        <select id="reserveUnit">
          <option value="credits">CRDT</option>
          <option value="tokens">TKN</option>
          <option value="coins">KOIN</option>
          <option value="points">BAL</option>
        </select>
      </label>
      <label>РџСЂРѕС†РµРЅС‚ СЃ РІС‹РїРѕР»РЅРµРЅРЅС‹С… СЂР°Р±РѕС‚, РєРѕС‚РѕСЂС‹Р№ РјС‹ СѓРґРµСЂР¶РёРІР°РµРј СЃРµР±Рµ
        <input id="completedFeePercent" type="number" min="0" max="100" step="0.1">
      </label>
      <label>РџСЂРѕС†РµРЅС‚ СЃ РѕС‚РєР°Р·Р°РЅРЅС‹С… РёР»Рё РѕС‚РјРµРЅРµРЅРЅС‹С… СЂР°Р±РѕС‚, РєРѕС‚РѕСЂС‹Р№ РјС‹ СѓРґРµСЂР¶РёРІР°РµРј СЃРµР±Рµ
        <input id="refusedFeePercent" type="number" min="0" max="100" step="0.1">
      </label>
      <label>РЎРєРѕР»СЊРєРѕ РґРЅРµР№ С…СЂР°РЅРёС‚СЊ РІС‹РїРѕР»РЅРµРЅРЅРѕРµ Р·Р°РґР°РЅРёРµ
        <input id="completedTasksRetentionDays" type="number" min="1" max="365" step="1">
      </label>
      <label>РЎРєРѕР»СЊРєРѕ РґРЅРµР№ С…СЂР°РЅРёС‚СЊ РЅРµРїСЂРёРЅСЏС‚С‹Рµ Р·Р°РґР°РЅРёСЏ
        <input id="unacceptedTasksRetentionDays" type="number" min="1" max="365" step="1">
      </label>
      <label>РЎРєРѕР»СЊРєРѕ РґРЅРµР№ С…СЂР°РЅРёС‚СЊ СЂР°СЃС‡РµС‚С‹ СЃРѕС‚СЂСѓРґРЅРёРєРѕРІ
        <input id="employeeSettlementsRetentionDays" type="number" min="1" max="365" step="1">
      </label>
      <label>РЎРєРѕР»СЊРєРѕ РґРЅРµР№ С…СЂР°РЅРёС‚СЊ СЂР°СЃС‡РµС‚С‹ РєР»РёРµРЅС‚РѕРІ
        <input id="clientSettlementsRetentionDays" type="number" min="1" max="365" step="1">
      </label>
      <label>РўРµР»РµС„РѕРЅ РѕР±СЂР°С‚РЅРѕР№ СЃРІСЏР·Рё
        <input id="feedbackPhone" type="text">
      </label>
      <label>E-mail РѕР±СЂР°С‚РЅРѕР№ СЃРІСЏР·Рё
        <input id="feedbackEmail" type="text">
      </label>
      <label>РћР±С‹С‡РЅС‹Р№ Р°РґСЂРµСЃ
        <input id="feedbackAddress" type="text">
      </label>
      <label>Telegram
        <input id="feedbackTelegram" type="text">
      </label>
      <label>WhatsApp
        <input id="feedbackWhatsApp" type="text">
      </label>
      <button>РЎРѕС…СЂР°РЅРёС‚СЊ</button>
      <p id="message" class="meta"></p>
    </form>
    <form id="passwordForm">
      <h2>РР·РјРµРЅРёС‚СЊ РїР°СЂРѕР»СЊ</h2>
      <label>РЎС‚Р°СЂС‹Р№ РїР°СЂРѕР»СЊ
        <input id="oldPassword" type="password" autocomplete="current-password">
      </label>
      <label>РџРѕРІС‚РѕСЂРёС‚Рµ СЃС‚Р°СЂС‹Р№ РїР°СЂРѕР»СЊ
        <input id="oldPasswordRepeat" type="password" autocomplete="current-password">
      </label>
      <label>Р’РІРµРґРёС‚Рµ РЅРѕРІС‹Р№ РїР°СЂРѕР»СЊ
        <input id="newPassword" type="password" autocomplete="new-password">
      </label>
      <button>РР·РјРµРЅРёС‚СЊ РїР°СЂРѕР»СЊ</button>
      <button id="resetPassword" type="button">РЎР±СЂРѕСЃРёС‚СЊ РїР°СЂРѕР»СЊ</button>
      <p id="passwordMessage" class="meta"></p>
    </form>
  </main>
  <script>
    let adminPassword = sessionStorage.getItem("adminPassword") || "";
    function adminHeaders(extra = {}) {
      if (!adminPassword) {
        adminPassword = prompt("Admin password") || "";
        sessionStorage.setItem("adminPassword", adminPassword);
      }
      return { "X-Admin-Password": adminPassword, ...extra };
    }
    async function requireAdminAccess(start) {
      while (true) {
        if (!adminPassword) {
          adminPassword = prompt("Admin password") || "";
        }
        if (!adminPassword) {
          document.body.innerHTML = "";
          return;
        }
        sessionStorage.setItem("adminPassword", adminPassword);
        const res = await fetch("/api/admin/check-password", { headers: { "X-Admin-Password": adminPassword } });
        if (res.ok) {
          document.body.classList.remove("locked");
          start();
          return;
        }
        sessionStorage.removeItem("adminPassword");
        adminPassword = "";
      }
    }
    async function loadSettings() {
      const res = await fetch("/api/admin/settings", { headers: adminHeaders() });
      if (!res.ok) {
        message.textContent = "РќРµ СѓРґР°Р»РѕСЃСЊ Р·Р°РіСЂСѓР·РёС‚СЊ РЅР°СЃС‚СЂРѕР№РєРё.";
        return;
      }
      const data = await res.json();
      const settings = data.settings || {};
      currency.value = settings.currency || "PLN";
      reserveUnit.value = settings.reserveUnit || "credits";
      completedFeePercent.value = settings.completedFeePercent ?? 1;
      refusedFeePercent.value = settings.refusedFeePercent ?? 1;
      refusedFeePercent.max = completedFeePercent.value;
      completedTasksRetentionDays.value = settings.completedTasksRetentionDays ?? 365;
      unacceptedTasksRetentionDays.value = settings.unacceptedTasksRetentionDays ?? 365;
      employeeSettlementsRetentionDays.value = settings.employeeSettlementsRetentionDays ?? 365;
      clientSettlementsRetentionDays.value = settings.clientSettlementsRetentionDays ?? 365;
      feedbackPhone.value = settings.feedbackPhone || "";
      feedbackEmail.value = settings.feedbackEmail || "";
      feedbackAddress.value = settings.feedbackAddress || "";
      feedbackTelegram.value = settings.feedbackTelegram || "";
      feedbackWhatsApp.value = settings.feedbackWhatsApp || "";
    }
    completedFeePercent.addEventListener("input", () => {
      refusedFeePercent.max = completedFeePercent.value || "0";
      if (Number(refusedFeePercent.value || 0) > Number(completedFeePercent.value || 0)) {
        refusedFeePercent.value = completedFeePercent.value || "0";
      }
    });
    document.querySelector("#settingsForm").addEventListener("submit", async event => {
      event.preventDefault();
      const res = await fetch("/api/admin/settings", {
        method: "POST",
        headers: adminHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({
          currency: currency.value,
          reserveUnit: reserveUnit.value,
          completedFeePercent: completedFeePercent.value,
          refusedFeePercent: refusedFeePercent.value,
          completedTasksRetentionDays: completedTasksRetentionDays.value,
          unacceptedTasksRetentionDays: unacceptedTasksRetentionDays.value,
          employeeSettlementsRetentionDays: employeeSettlementsRetentionDays.value,
          clientSettlementsRetentionDays: clientSettlementsRetentionDays.value,
          feedbackPhone: feedbackPhone.value,
          feedbackEmail: feedbackEmail.value,
          feedbackAddress: feedbackAddress.value,
          feedbackTelegram: feedbackTelegram.value,
          feedbackWhatsApp: feedbackWhatsApp.value
        })
      });
      message.textContent = res.ok ? "РќР°СЃС‚СЂРѕР№РєРё СЃРѕС…СЂР°РЅРµРЅС‹." : "РќРµ СѓРґР°Р»РѕСЃСЊ СЃРѕС…СЂР°РЅРёС‚СЊ РЅР°СЃС‚СЂРѕР№РєРё.";
    });
    document.querySelector("#passwordForm").addEventListener("submit", async event => {
      event.preventDefault();
      const res = await fetch("/api/admin/change-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          oldPassword: oldPassword.value,
          oldPasswordRepeat: oldPasswordRepeat.value,
          newPassword: newPassword.value
        })
      });
      if (!res.ok) {
        passwordMessage.textContent = "РќРµ СѓРґР°Р»РѕСЃСЊ РёР·РјРµРЅРёС‚СЊ РїР°СЂРѕР»СЊ.";
        return;
      }
      adminPassword = newPassword.value;
      sessionStorage.setItem("adminPassword", adminPassword);
      oldPassword.value = "";
      oldPasswordRepeat.value = "";
      newPassword.value = "";
      passwordMessage.textContent = "РџР°СЂРѕР»СЊ РёР·РјРµРЅРµРЅ.";
    });
    document.querySelector("#resetPassword").addEventListener("click", async () => {
      if (!confirm("РЎР±СЂРѕСЃРёС‚СЊ РїР°СЂРѕР»СЊ РЅР° Р·Р°РїР°СЃРЅРѕР№ РїРѕСЃС‚РѕСЏРЅРЅС‹Р№ РїР°СЂРѕР»СЊ?")) {
        return;
      }
      const res = await fetch("/api/admin/reset-password", {
        method: "POST",
        headers: adminHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({})
      });
      if (!res.ok) {
        passwordMessage.textContent = "РќРµ СѓРґР°Р»РѕСЃСЊ СЃР±СЂРѕСЃРёС‚СЊ РїР°СЂРѕР»СЊ.";
        return;
      }
      adminPassword = "ZarazaZ";
      sessionStorage.setItem("adminPassword", adminPassword);
      oldPassword.value = "";
      oldPasswordRepeat.value = "";
      newPassword.value = "";
      passwordMessage.textContent = "РџР°СЂРѕР»СЊ СЃР±СЂРѕС€РµРЅ.";
    });
    requireAdminAccess(loadSettings);
  </script>
</body>
</html>"""


APPROVED_MARKETING_STYLE = r"""
    :root { --ink:#172026; --teal:#0f766e; --gold:#f6c85f; --bg:#fffaf0; --surface:#fff; --line:rgba(23,32,38,.14); --text:#172026; --muted:#60717d; --blue:#0f766e; --green:#0f766e; --red:#b42318; --amber:#8a5a00; --shadow:0 14px 34px rgba(23,32,38,.10); }
    * { box-sizing: border-box; }
    body { margin:0; min-height:100vh; font-family:Arial, sans-serif; color:var(--text); background:linear-gradient(135deg, #fff7df 0%, #d8f8eb 48%, #e9d5ff 100%); }
    body.locked header, body.locked main { display:none; }
    header { background:linear-gradient(135deg, #0f766e 0%, #2563eb 48%, #7c3aed 100%); color:white; padding:24px 28px; }
    header h1 { margin:0 0 12px; font-size:28px; }
    nav { display:grid; grid-template-columns:repeat(10,minmax(104px,1fr)); gap:8px; }
    nav a { min-height:40px; display:flex; align-items:center; justify-content:center; padding:8px; border-radius:8px; background:#f6c85f; color:#17202a; text-decoration:none; font-weight:700; text-align:center; line-height:1.15; }
    main { max-width:1280px; margin:0 auto; padding:24px; }
    .topbar { display:flex; justify-content:space-between; gap:16px; align-items:flex-start; margin-bottom:18px; }
    .topbar p { margin:6px 0 0; color:var(--muted); }
    .status-row { display:flex; gap:8px; flex-wrap:wrap; justify-content:flex-end; }
    .pill { min-height:30px; border-radius:999px; padding:6px 10px; display:inline-flex; align-items:center; font-size:13px; font-weight:700; white-space:nowrap; }
    .pill.blue, .pill.green, .pill.amber { color:var(--ink); background:rgba(246,200,95,.9); }
    .content { display:grid; grid-template-columns:minmax(0,1.15fr) minmax(320px,.85fr); gap:18px; align-items:start; }
    .section { background:var(--surface); border:1px solid var(--line); border-radius:8px; box-shadow:var(--shadow); overflow:hidden; margin-bottom:18px; }
    .section-header { padding:16px; border-bottom:1px solid var(--line); display:flex; justify-content:space-between; gap:12px; align-items:flex-start; }
    .section-header h2 { margin:0; font-size:17px; } .section-header p { margin:5px 0 0; color:var(--muted); font-size:13px; }
    .section-body { padding:16px; }
    .form-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; margin-bottom:14px; }
    .full { grid-column:1 / -1; }
    label { display:grid; gap:6px; color:#344054; font-size:13px; font-weight:700; }
    input, textarea, select, button { font:inherit; }
    input, textarea, select { width:100%; min-height:38px; border-radius:7px; border:1px solid #cfd7e3; padding:8px 10px; color:var(--text); background:white; } input[type="file"] { color:transparent; } input[type="file"]::file-selector-button { min-height:32px; border:0; border-radius:7px; padding:0 12px; margin-right:10px; background:#eef2f7; color:#344054; font-weight:700; cursor:pointer; }
    textarea { min-height:82px; resize:vertical; }
    button { border:0; cursor:pointer; min-height:38px; border-radius:7px; padding:0 13px; font-weight:700; color:#344054; background:#eef2f7; }
    button.primary { color:white; background:linear-gradient(135deg, var(--teal), #2563eb); } button.danger { color:var(--red); background:#fff0ee; } button.ghost { background:white; border:1px solid var(--line); }
    .form-actions { display:flex; justify-content:flex-end; gap:10px; flex-wrap:wrap; }
    .items { display:grid; gap:10px; }
    .item { border:1px solid var(--line); border-radius:8px; padding:12px; background:#fbfcfe; display:grid; gap:8px; }
    .item-head { display:flex; justify-content:space-between; gap:12px; align-items:flex-start; }
    .item-title { display:grid; gap:4px; min-width:0; } .item-title strong { overflow-wrap:anywhere; }
    .meta, .item-title span { color:var(--muted); font-size:13px; line-height:1.35; }
    .item-actions { display:flex; gap:6px; flex-shrink:0; }
    .tag { min-height:26px; border-radius:999px; padding:5px 9px; background:#eef2f7; color:#344054; font-size:12px; font-weight:700; display:inline-flex; margin:2px; }
    .preview-thumb { width:68px; height:68px; border-radius:7px; background:#e9edf3; display:grid; place-items:center; color:#667085; font-size:12px; overflow:hidden; }
    .preview-thumb img, img.preview { width:100%; height:100%; object-fit:cover; }
    img.preview { max-width:180px; height:auto; border-radius:8px; }
    .send-now { margin-top:18px; padding:16px; background:rgba(255,255,255,.94); color:var(--ink); border:1px solid var(--line); border-left:6px solid var(--teal); box-shadow:var(--shadow); border-radius:8px; display:flex; align-items:center; justify-content:space-between; gap:16px; }
    .send-now span { display:block; margin-top:4px; color:var(--muted); font-size:13px; }
    .empty { color:var(--muted); border:1px dashed var(--line); border-radius:8px; padding:14px; text-align:center; font-size:14px; }
    @media (max-width:1100px) { .content { grid-template-columns:1fr; } nav { grid-template-columns:repeat(2,minmax(0,1fr)); } }
    @media (max-width:640px) { main { padding:16px; } .topbar, .send-now { flex-direction:column; } .form-grid { grid-template-columns:1fr; } .item-head { flex-direction:column; } }
"""


APPROVED_MARKETING_SCRIPT = r"""
    let state = {};
    let adminPassword = sessionStorage.getItem("adminPassword") || "";
    const SAME_GROUP = "__same_group__";
    function adminHeaders(extra = {}) {
      if (!adminPassword) {
        adminPassword = prompt("Admin password") || "";
        sessionStorage.setItem("adminPassword", adminPassword);
      }
      return { "X-Admin-Password": adminPassword, ...extra };
    }
    async function requireAdminAccess(start) {
      while (true) {
        if (!adminPassword) adminPassword = prompt("Admin password") || "";
        if (!adminPassword) { document.body.innerHTML = ""; return; }
        sessionStorage.setItem("adminPassword", adminPassword);
        const res = await fetch("/api/admin/check-password", { headers: { "X-Admin-Password": adminPassword } });
        if (res.ok) { document.body.classList.remove("locked"); start(); return; }
        sessionStorage.removeItem("adminPassword"); adminPassword = "";
      }
    }
    const esc = value => String(value ?? "").replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    const fmt = value => value ? new Date(Number(value) * 1000).toLocaleString("ru-RU") : "";
    async function api(action, data = {}) {
      const res = await fetch(`/api/admin/marketing/${platform}/${action}`, { method:"POST", headers:adminHeaders({ "Content-Type":"application/json" }), body:JSON.stringify(data) });
      const payload = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(payload.error || res.status);
      return payload;
    }
    async function loadState() {
      const res = await fetch(`/api/admin/marketing/${platform}`, { headers:adminHeaders() });
      state = await res.json();
      render();
    }
    function messageOptions(selected = "") {
      return (state.messages || []).map(item => `<option value="${item.id}" ${String(item.id) === String(selected) ? "selected" : ""}>#${item.id} ${esc(item.title)}</option>`).join("");
    }
    function targetOptions(selected = "") {
      if (platform === "telegram") {
        const groups = (state.groups || []).filter(item => item.enabled);
        return `<option value="">Р’СЃРµ СЂРµРєР»Р°РјРЅС‹Рµ РіСЂСѓРїРїС‹</option>` + groups.map(item => `<option value="${item.chat_id}" ${String(item.chat_id) === String(selected) ? "selected" : ""}>${esc(item.title || item.chat_id)}</option>`).join("");
      }
      const targets = state.targets || [];
      return `<option value="">Р’СЃРµ Facebook-РіСЂСѓРїРїС‹</option>` + targets.map(item => `<option value="${item.id}" ${String(item.id) === String(selected) ? "selected" : ""}>${esc(item.name)}</option>`).join("");
    }
    function watchTargetOptions(selected = "") {
      const groups = (state.groups || []).filter(item => item.enabled);
      return `<option value="${SAME_GROUP}" ${!selected ? "selected" : ""}>РќРµ РїРµСЂРµСЃС‹Р»Р°С‚СЊ, РѕС‚РІРµС‚РёС‚СЊ РІ СЌС‚РѕР№ Р¶Рµ РіСЂСѓРїРїРµ</option>` + groups.map(item => `<option value="${item.chat_id}" ${String(item.chat_id) === String(selected) ? "selected" : ""}>РџРµСЂРµСЃР»Р°С‚СЊ РІ: ${esc(item.title || item.chat_id)}</option>`).join("");
    }
    function imageFromFile(input, target) {
      const file = input.files && input.files[0];
      if (!file) return Promise.resolve(target.value);
      return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = async () => {
          try {
            const result = await api("upload-image", { name:file.name, contentType:file.type, content:reader.result });
            target.value = result.url || "";
            input.value = "";
            resolve(target.value);
          } catch (error) { reject(error); }
        };
        reader.onerror = () => reject(reader.error);
        reader.readAsDataURL(file);
      });
    }
    async function saveOwned(event) {
      event.preventDefault();
      await api("telegram-group", { chatId:ownedChatId.value, title:ownedTitle.value, notes:ownedNotes.value, enabled:true, watchEnabled:false });
      event.target.reset(); loadState();
    }
    async function saveWatch(event) {
      event.preventDefault();
      await api("telegram-group", { chatId:watchChatId.value, title:watchTitle.value, keywords:watchKeywords.value, targetChatId:watchTarget.value, responseMessageId:watchMaterial.value, notes:watchNotes.value, enabled:watchAdEnabled.checked, watchEnabled:true });
      event.target.reset(); watchAdEnabled.checked = false; loadState();
    }
    async function saveFacebookTarget(event) {
      event.preventDefault();
      await api("facebook-target", { id:fbTargetId.value || null, name:fbTargetName.value, targetId:fbTargetLink.value, keywords:fbKeywords.value, targetAction:fbAction.value, responseMessageId:fbMaterial.value, notes:fbNotes.value, enabled:fbEnabled.checked });
      event.target.reset(); fbEnabled.checked = true; fbAction.value = "same_group"; loadState();
    }
    async function saveMessage(event) {
      event.preventDefault();
      try { await imageFromFile(messageImageFile, messageImageUrl); } catch (error) { alert("РќРµ СѓРґР°Р»РѕСЃСЊ Р·Р°РіСЂСѓР·РёС‚СЊ РєР°СЂС‚РёРЅРєСѓ."); return; }
      await api("message", { id:messageId.value || null, title:messageTitle.value, audience:messageAudience.value, body:messageBody.value, imageUrl:messageImageUrl.value, enabled:messageEnabled.checked });
      event.target.reset(); messageEnabled.checked = true; messageId.value = ""; loadState();
    }
    async function saveSchedule(event) {
      event.preventDefault();
      await api("schedule", { id:scheduleId.value || null, targetId:scheduleTarget.value, sendTime:scheduleTime.value, messageId:scheduleMessage.value, enabled:scheduleEnabled.checked });
      event.target.reset(); scheduleTime.value = platform === "telegram" ? "09:30" : "10:00"; scheduleEnabled.checked = true; loadState();
    }
    async function sendNow() {
      if (!sendMessage.value) return alert("Р’С‹Р±РµСЂРёС‚Рµ СЂРµРєР»Р°РјРЅС‹Р№ РјР°С‚РµСЂРёР°Р».");
      const result = await api("send-now", { targetId:sendTarget.value, messageId:sendMessage.value });
      alert(platform === "telegram" ? `РћС‚РїСЂР°РІР»РµРЅРѕ: ${result.sent}/${result.total}` : "Facebook РѕС‚РїСЂР°РІРєР° РїРѕРґРіРѕС‚РѕРІР»РµРЅР°.");
      loadState();
    }
    async function deleteItem(kind, id) {
      if (!confirm("РЈРґР°Р»РёС‚СЊ?")) return;
      await api("delete", { kind, id });
      loadState();
    }
    async function clearLogs() {
      if (!confirm("РћС‡РёСЃС‚РёС‚СЊ Р¶СѓСЂРЅР°Р»?")) return;
      await api("clear-logs", {});
      loadState();
    }
    function editMessage(id) {
      const item = (state.messages || []).find(row => row.id === id);
      if (!item) return;
      messageId.value = item.id; messageTitle.value = item.title || ""; messageAudience.value = item.audience || "all"; messageBody.value = item.body || ""; messageImageUrl.value = item.image_url || ""; messageEnabled.checked = !!item.enabled;
      window.scrollTo({ top: document.querySelector("#messagePanel").offsetTop - 20, behavior:"smooth" });
    }
    function editSchedule(id) {
      const item = (state.schedules || []).find(row => row.id === id);
      if (!item) return;
      scheduleId.value = item.id; scheduleTarget.value = item.target_id || ""; scheduleTime.value = item.send_time || "09:30"; scheduleMessage.value = item.message_id || ""; scheduleEnabled.checked = !!item.enabled;
    }
    function editTelegramGroup(chatId, mode) {
      const item = (state.groups || []).find(row => String(row.chat_id) === String(chatId));
      if (!item) return;
      if (mode === "watch") {
        watchTitle.value = item.title || ""; watchChatId.value = item.chat_id || ""; watchKeywords.value = item.keywords || ""; watchTarget.value = item.target_chat_id || SAME_GROUP; watchMaterial.value = item.response_message_id || ""; watchNotes.value = item.notes || ""; watchAdEnabled.checked = !!item.enabled;
      } else {
        ownedTitle.value = item.title || ""; ownedChatId.value = item.chat_id || ""; ownedNotes.value = item.notes || "";
      }
    }
    function editFacebookTarget(id) {
      const item = (state.targets || []).find(row => String(row.id) === String(id));
      if (!item) return;
      fbTargetId.value = item.id; fbTargetName.value = item.name || ""; fbTargetLink.value = item.target_id || ""; fbKeywords.value = item.keywords || ""; fbAction.value = item.action || "same_group"; fbMaterial.value = item.response_message_id || ""; fbNotes.value = item.notes || ""; fbEnabled.checked = !!item.enabled;
    }
    function renderCommonSelects() {
      [scheduleTarget, sendTarget].forEach(select => select.innerHTML = targetOptions(select.value));
      [scheduleMessage, sendMessage].forEach(select => select.innerHTML = messageOptions(select.value));
      if (platform === "telegram") {
        watchTarget.innerHTML = watchTargetOptions(watchTarget.value);
        watchMaterial.innerHTML = messageOptions(watchMaterial.value);
      } else {
        fbMaterial.innerHTML = messageOptions(fbMaterial.value);
      }
    }
    function renderMessages() {
      messagesList.innerHTML = (state.messages || []).map(item => `<article class="item ${item.enabled ? "" : "off"}"><div class="item-head"><div class="item-title"><strong>#${item.id} <span class="no-translate">${esc(item.title)}</span></strong><span class="no-translate">${esc((item.body || "").slice(0, 140))}</span></div><div class="item-actions"><button onclick="editMessage(${item.id})">Р РµРґР°РєС‚РёСЂРѕРІР°С‚СЊ</button><button class="danger" onclick="deleteItem('message', ${item.id})">РЈРґР°Р»РёС‚СЊ</button></div></div>${item.image_url ? `<img class="preview" src="${esc(item.image_url)}">` : `<div class="meta">РљР°СЂС‚РёРЅРєР° РЅРµ РІС‹Р±СЂР°РЅР°</div>`}</article>`).join("") || `<div class="empty">РџРѕРєР° РЅРµС‚ СЂРµРєР»Р°РјРЅС‹С… РјР°С‚РµСЂРёР°Р»РѕРІ</div>`;
    }
    function renderSchedules() {
      schedulesList.innerHTML = (state.schedules || []).map(item => `<article class="item ${item.enabled ? "" : "off"}"><div class="item-head"><div class="item-title"><strong>${esc(item.send_time)}</strong><span>РњР°С‚РµСЂРёР°Р» #${esc(item.message_id)} В· С†РµР»СЊ: ${esc(item.target_id || "РІСЃРµ")}</span></div><div class="item-actions"><button onclick="editSchedule(${item.id})">Р РµРґР°РєС‚РёСЂРѕРІР°С‚СЊ</button><button class="danger" onclick="deleteItem('schedule', ${item.id})">РЈРґР°Р»РёС‚СЊ</button></div></div><div class="meta">РџРѕСЃР»РµРґРЅСЏСЏ РѕС‚РїСЂР°РІРєР°: ${esc(item.last_sent_date || "РЅРµС‚")}</div></article>`).join("") || `<div class="empty">РџРѕРєР° РЅРµС‚ СЂР°СЃРїРёСЃР°РЅРёР№</div>`;
    }
    function renderLogs() {
      logs.innerHTML = (state.logs || []).map(item => `<article class="item"><strong>${esc(item.action)} В· ${esc(item.status)}</strong><div class="meta">${fmt(item.created_at)} В· ${esc(item.target_type || "")}</div><p class="no-translate">${esc(item.detail || "")}</p></article>`).join("") || `<div class="empty">Р–СѓСЂРЅР°Р» РѕС‡РёС‰РµРЅ</div>`;
    }
"""


TELEGRAM_ADS_HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Р РµРєР»Р°РјР° Telegram</title>
  <style>""" + APPROVED_MARKETING_STYLE + r"""</style>
</head>
<body class="locked">
  <header><h1>Р РµРєР»Р°РјР° Telegram</h1><nav><a href="/server">Р—Р°РґР°РЅРёСЏ</a><a href="/users">РЎРѕС‚СЂСѓРґРЅРёРєРё</a><a href="/clients">РљР»РёРµРЅС‚С‹</a><a href="/completed">Р’С‹РїРѕР»РЅРµРЅРЅС‹Рµ Р·Р°РґР°РЅРёСЏ</a><a href="/calculations">Р Р°СЃС‡РµС‚С‹ СЃРѕС‚СЂСѓРґРЅРёРєРѕРІ</a><a href="/client-calculations">Р Р°СЃС‡РµС‚С‹ РєР»РёРµРЅС‚РѕРІ</a><a href="/telegram-ads">Р РµРєР»Р°РјР° Telegram</a><a href="/facebook-ads">Р РµРєР»Р°РјР° Facebook</a><a href="/telegram-login">Telegram userbot</a><a href="/settings">РќР°СЃС‚СЂРѕР№РєРё</a></nav></header>
  <main>
    <div class="topbar"><div><h1>Р РµРєР»Р°РјР° Telegram</h1><p>Р“СЂСѓРїРїС‹ РґР»СЏ СЂРµРєР»Р°РјС‹, РіСЂСѓРїРїС‹ РґР»СЏ РїРѕРёСЃРєР° РѕР±СЉСЏРІР»РµРЅРёР№, РјР°С‚РµСЂРёР°Р»С‹, СЂР°СЃРїРёСЃР°РЅРёРµ Рё Р¶СѓСЂРЅР°Р».</p></div><div class="status-row"><span class="pill blue" id="ownedCount">0 СЂРµРєР»Р°РјРЅС‹С… РіСЂСѓРїРї</span><span class="pill green" id="watchCount">0 РіСЂСѓРїРї РїРѕРёСЃРєР°</span><span class="pill amber" id="scheduleCount">0 СЂР°СЃРїРёСЃР°РЅРёР№</span></div></div>
    <div class="content"><div>
      <section class="section"><div class="section-header"><div><h2>Telegram-РіСЂСѓРїРїС‹ РґР»СЏ СЂРµРєР»Р°РјС‹</h2><p>Р’Р°С€Рё РіСЂСѓРїРїС‹, РєСѓРґР° Р±РѕС‚ РѕС‚РїСЂР°РІР»СЏРµС‚ СЂРµРєР»Р°РјРЅС‹Рµ РјР°С‚РµСЂРёР°Р»С‹ РїРѕ СЂР°СЃРїРёСЃР°РЅРёСЋ.</p></div></div><div class="section-body"><form class="form-grid" onsubmit="saveOwned(event)"><label>РќР°Р·РІР°РЅРёРµ РіСЂСѓРїРїС‹<input id="ownedTitle" required></label><label>Chat ID<input id="ownedChatId" required placeholder="-100..."></label><label class="full">РљРѕРјРјРµРЅС‚Р°СЂРёР№<input id="ownedNotes"></label><div class="form-actions full"><button class="primary">РЎРѕС…СЂР°РЅРёС‚СЊ РіСЂСѓРїРїСѓ</button></div></form><div class="items" id="ownedList"></div></div></section>
      <section class="section"><div class="section-header"><div><h2>Telegram-РіСЂСѓРїРїС‹ РґР»СЏ РїРѕРёСЃРєР° РѕР±СЉСЏРІР»РµРЅРёР№</h2><p>Р‘РѕС‚ РёС‰РµС‚ РєР»СЋС‡РµРІС‹Рµ СЃР»РѕРІР° Рё РѕС‚РІРµС‡Р°РµС‚ РІ СЌС‚РѕР№ Р¶Рµ РіСЂСѓРїРїРµ РёР»Рё РїРµСЂРµСЃС‹Р»Р°РµС‚ РІ РІС‹Р±СЂР°РЅРЅСѓСЋ РІР°С€Сѓ РіСЂСѓРїРїСѓ.</p></div></div><div class="section-body"><form class="form-grid" onsubmit="saveWatch(event)"><label>Р“СЂСѓРїРїР° РїРѕРёСЃРєР°<input id="watchTitle" required></label><label>Chat ID<input id="watchChatId" required placeholder="-100..."></label><label>Р”РµР№СЃС‚РІРёРµ РїСЂРё СЃРѕРІРїР°РґРµРЅРёРё<select id="watchTarget"></select></label><label>РњР°С‚РµСЂРёР°Р» РґР»СЏ РєРѕРјРјРµРЅС‚Р°СЂРёСЏ<select id="watchMaterial"></select></label><label class="full">РљР»СЋС‡РµРІС‹Рµ СЃР»РѕРІР°<input id="watchKeywords" placeholder="Р°СЂРµРЅРґР°, РєСѓРїРёС‚СЊ, СЃСЂРѕС‡РЅРѕ"></label><label class="full">Р”РѕРїРѕР»РЅРёС‚РµР»СЊРЅР°СЏ Р·Р°РјРµС‚РєР°<textarea id="watchNotes"></textarea></label><label class="full"><input id="watchAdEnabled" type="checkbox"> РСЃРїРѕР»СЊР·РѕРІР°С‚СЊ СЌС‚Сѓ РіСЂСѓРїРїСѓ С‚Р°РєР¶Рµ РґР»СЏ СЂРµРєР»Р°РјС‹</label><div class="form-actions full"><button class="primary">РЎРѕС…СЂР°РЅРёС‚СЊ РїРѕРёСЃРє</button></div></form><div class="items" id="watchList"></div></div></section>
      <section class="section" id="messagePanel"><div class="section-header"><div><h2>Р РµРєР»Р°РјРЅС‹Р№ РјР°С‚РµСЂРёР°Р»</h2><p>РћС‚РґРµР»СЊРЅС‹Р№ СЂРµРєР»Р°РјРЅС‹Р№ С‚РµРєСЃС‚ Рё РєР°СЂС‚РёРЅРєР° СЃ РєРѕРјРїСЊСЋС‚РµСЂР°.</p></div></div><div class="section-body"><form class="form-grid" onsubmit="saveMessage(event)"><input id="messageId" type="hidden"><label>РќР°Р·РІР°РЅРёРµ РјР°С‚РµСЂРёР°Р»Р°<input id="messageTitle" required></label><label>РђСѓРґРёС‚РѕСЂРёСЏ<select id="messageAudience"><option value="all">Р’СЃРµ</option><option value="clients">РљР»РёРµРЅС‚С‹</option><option value="workers">РњР°СЃС‚РµСЂР°</option></select></label><label class="full">РўРµРєСЃС‚ СЂРµРєР»Р°РјС‹<textarea id="messageBody" required></textarea></label><label>РЎСЃС‹Р»РєР° РЅР° РєР°СЂС‚РёРЅРєСѓ<input id="messageImageUrl"></label><label>РљР°СЂС‚РёРЅРєР° СЃ РєРѕРјРїСЊСЋС‚РµСЂР°<input id="messageImageFile" type="file" accept="image/png,image/jpeg,image/webp,image/gif"></label><label class="full"><input id="messageEnabled" type="checkbox" checked> РњР°С‚РµСЂРёР°Р» РІРєР»СЋС‡РµРЅ</label><div class="form-actions full"><button class="primary">РЎРѕС…СЂР°РЅРёС‚СЊ СЂРµРєР»Р°РјРЅС‹Р№ РјР°С‚РµСЂРёР°Р»</button></div></form><div class="items" id="messagesList"></div></div></section>
      <section class="section"><div class="section-header"><div><h2>Р Р°СЃРїРёСЃР°РЅРёРµ СЂРµРєР»Р°РјС‹</h2><p>Р’С‹Р±РµСЂРёС‚Рµ РіСЂСѓРїРїСѓ, РјР°С‚РµСЂРёР°Р» Рё РІСЂРµРјСЏ РїСѓР±Р»РёРєР°С†РёРё.</p></div></div><div class="section-body"><form class="form-grid" onsubmit="saveSchedule(event)"><input id="scheduleId" type="hidden"><label>Р“СЂСѓРїРїР°<select id="scheduleTarget"></select></label><label>РњР°С‚РµСЂРёР°Р»<select id="scheduleMessage"></select></label><label>Р’СЂРµРјСЏ<input id="scheduleTime" type="time" value="09:30" required></label><label><input id="scheduleEnabled" type="checkbox" checked> Р Р°СЃРїРёСЃР°РЅРёРµ РІРєР»СЋС‡РµРЅРѕ</label><div class="form-actions full"><button class="primary">РЎРѕС…СЂР°РЅРёС‚СЊ СЂР°СЃРїРёСЃР°РЅРёРµ</button></div></form><div class="items" id="schedulesList"></div></div></section>
    </div><aside><section class="section"><div class="section-header"><div><h2>РќР°Р№РґРµРЅРЅС‹Рµ РѕР±СЉСЏРІР»РµРЅРёСЏ</h2><p>РџРѕСЃР»РµРґРЅРёРµ СЃРѕРІРїР°РґРµРЅРёСЏ РїРѕ РєР»СЋС‡РµРІС‹Рј СЃР»РѕРІР°Рј.</p></div></div><div class="section-body"><div class="items" id="hitsList"></div></div></section><section class="section"><div class="section-header"><div><h2>Р–СѓСЂРЅР°Р» Telegram</h2><p>РџРѕСЃР»РµРґРЅРёРµ РґРµР№СЃС‚РІРёСЏ.</p></div><button class="danger" onclick="clearLogs()">РћС‡РёСЃС‚РєР° Р¶СѓСЂРЅР°Р»Р°</button></div><div class="section-body"><div class="items" id="logs"></div></div></section></aside></div>
    <div class="send-now"><div><strong>РћС‚РїСЂР°РІРёС‚СЊ СЃРµР№С‡Р°СЃ</strong><span>Р СѓС‡РЅР°СЏ РѕС‚РїСЂР°РІРєР° РІС‹Р±СЂР°РЅРЅРѕРіРѕ СЂРµРєР»Р°РјРЅРѕРіРѕ РјР°С‚РµСЂРёР°Р»Р°.</span></div><div><select id="sendTarget"></select><select id="sendMessage"></select><button onclick="sendNow()">РћС‚РїСЂР°РІРёС‚СЊ СЃРµР№С‡Р°СЃ</button></div></div>
  </main>
  <script>const platform = "telegram";""" + APPROVED_MARKETING_SCRIPT + r"""
    function render() {
      renderCommonSelects(); renderMessages(); renderSchedules(); renderLogs();
      const groups = state.groups || [];
      const owned = groups.filter(item => item.enabled);
      const watched = groups.filter(item => item.watch_enabled);
      ownedCount.textContent = `${owned.length} СЂРµРєР»Р°РјРЅС‹С… РіСЂСѓРїРї`; watchCount.textContent = `${watched.length} РіСЂСѓРїРї РїРѕРёСЃРєР°`; scheduleCount.textContent = `${(state.schedules || []).length} СЂР°СЃРїРёСЃР°РЅРёР№`;
      ownedList.innerHTML = owned.map(item => `<article class="item"><div class="item-head"><div class="item-title"><strong class="no-translate">${esc(item.title || item.chat_id)}</strong><span class="no-translate">${esc(item.chat_id)}</span></div><div class="item-actions"><button onclick="editTelegramGroup('${item.chat_id}', 'owned')">Р РµРґР°РєС‚РёСЂРѕРІР°С‚СЊ</button><button class="danger" onclick="deleteItem('telegram-group', '${item.chat_id}')">РЈРґР°Р»РёС‚СЊ</button></div></div><div class="meta no-translate">${esc(item.notes || "")}</div></article>`).join("") || `<div class="empty">РџРѕРєР° РЅРµС‚ РіСЂСѓРїРї РґР»СЏ СЂРµРєР»Р°РјС‹</div>`;
      watchList.innerHTML = watched.map(item => `<article class="item"><div class="item-head"><div class="item-title"><strong>${esc(item.title || item.chat_id)}</strong><span>${item.target_chat_id ? "РџРµСЂРµСЃР»Р°С‚СЊ РІ: " + esc(item.target_chat_id) : "РќРµ РїРµСЂРµСЃС‹Р»Р°С‚СЊ, РѕС‚РІРµС‚РёС‚СЊ РІ СЌС‚РѕР№ Р¶Рµ РіСЂСѓРїРїРµ"}</span></div><div class="item-actions"><button onclick="editTelegramGroup('${item.chat_id}', 'watch')">Р РµРґР°РєС‚РёСЂРѕРІР°С‚СЊ</button><button class="danger" onclick="deleteItem('telegram-group', '${item.chat_id}')">РЈРґР°Р»РёС‚СЊ</button></div></div><div>${String(item.keywords || "").split(",").filter(Boolean).map(word => `<span class="tag no-translate">${esc(word.trim())}</span>`).join("")}</div><div class="meta">РњР°С‚РµСЂРёР°Р»: ${esc(item.response_message_id || "РЅРµ РІС‹Р±СЂР°РЅ")}</div><div class="meta no-translate">${esc(item.notes || "")}</div></article>`).join("") || `<div class="empty">РџРѕРєР° РЅРµС‚ РіСЂСѓРїРї РґР»СЏ РїРѕРёСЃРєР°</div>`;
      hitsList.innerHTML = (state.hits || []).map(item => `<article class="item"><strong>${esc(item.keyword)}</strong><div class="meta">${fmt(item.created_at)} В· ${esc(item.username || "")}</div><p class="no-translate">${esc(item.message || "")}</p></article>`).join("") || `<div class="empty">РЎРѕРІРїР°РґРµРЅРёР№ РїРѕРєР° РЅРµС‚</div>`;
    }
    requireAdminAccess(loadState);
  </script>
</body>
</html>"""


FACEBOOK_ADS_HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Р РµРєР»Р°РјР° Facebook</title>
  <style>""" + APPROVED_MARKETING_STYLE + r"""</style>
</head>
<body class="locked">
  <header><h1>Р РµРєР»Р°РјР° Facebook</h1><nav><a href="/server">Р—Р°РґР°РЅРёСЏ</a><a href="/users">РЎРѕС‚СЂСѓРґРЅРёРєРё</a><a href="/clients">РљР»РёРµРЅС‚С‹</a><a href="/completed">Р’С‹РїРѕР»РЅРµРЅРЅС‹Рµ Р·Р°РґР°РЅРёСЏ</a><a href="/calculations">Р Р°СЃС‡РµС‚С‹ СЃРѕС‚СЂСѓРґРЅРёРєРѕРІ</a><a href="/client-calculations">Р Р°СЃС‡РµС‚С‹ РєР»РёРµРЅС‚РѕРІ</a><a href="/telegram-ads">Р РµРєР»Р°РјР° Telegram</a><a href="/facebook-ads">Р РµРєР»Р°РјР° Facebook</a><a href="/telegram-login">Telegram userbot</a><a href="/settings">РќР°СЃС‚СЂРѕР№РєРё</a></nav></header>
  <main>
    <div class="topbar"><div><h1>Р РµРєР»Р°РјР° Facebook</h1><p>Р“СЂСѓРїРїС‹, РјР°С‚РµСЂРёР°Р»С‹, СЂР°СЃРїРёСЃР°РЅРёРµ Рё Р¶СѓСЂРЅР°Р». РџСЂРѕРІРµСЂРєСѓ Page Access Token СЃРґРµР»Р°РµРј С‡РµСЂРµР· Meta.</p></div><div class="status-row"><span class="pill blue" id="targetCount">0 РіСЂСѓРїРї</span><span class="pill green" id="messageCount">0 РјР°С‚РµСЂРёР°Р»РѕРІ</span><span class="pill amber" id="scheduleCount">0 СЂР°СЃРїРёСЃР°РЅРёР№</span></div></div>
    <div class="content"><div>
      <section class="section"><div class="section-header"><div><h2>Facebook-РіСЂСѓРїРїС‹</h2><p>Р“СЂСѓРїРїС‹ РёР»Рё СЃС‚СЂР°РЅРёС†С‹ РґР»СЏ РїР»Р°РЅРёСЂРѕРІР°РЅРёСЏ СЂРµРєР»Р°РјС‹ Рё РїРѕРёСЃРєР° РїРѕ РєР»СЋС‡РµРІС‹Рј СЃР»РѕРІР°Рј.</p></div></div><div class="section-body"><form class="form-grid" onsubmit="saveFacebookTarget(event)"><input id="fbTargetId" type="hidden"><label>РќР°Р·РІР°РЅРёРµ<input id="fbTargetName" required></label><label>ID РёР»Рё СЃСЃС‹Р»РєР°<input id="fbTargetLink"></label><label>Р”РµР№СЃС‚РІРёРµ РїСЂРё СЃРѕРІРїР°РґРµРЅРёРё<select id="fbAction"><option value="same_group">РќРµ РїРµСЂРµСЃС‹Р»Р°С‚СЊ, РѕС‚РІРµС‚РёС‚СЊ РІ СЌС‚РѕР№ Р¶Рµ РіСЂСѓРїРїРµ</option><option value="manual">РўРѕР»СЊРєРѕ Р·Р°РїРёСЃР°С‚СЊ РІ Р¶СѓСЂРЅР°Р»</option></select></label><label>РњР°С‚РµСЂРёР°Р» РґР»СЏ РєРѕРјРјРµРЅС‚Р°СЂРёСЏ<select id="fbMaterial"></select></label><label class="full">РљР»СЋС‡РµРІС‹Рµ СЃР»РѕРІР°<input id="fbKeywords" placeholder="СЂР°Р±РѕС‚Р°, РєРІР°СЂС‚РёСЂР°, РїСЂРѕРґР°Р¶Р°"></label><label class="full">Р—Р°РјРµС‚РєРё<textarea id="fbNotes"></textarea></label><label class="full"><input id="fbEnabled" type="checkbox" checked> Р“СЂСѓРїРїР° РІРєР»СЋС‡РµРЅР°</label><div class="form-actions full"><button class="primary">РЎРѕС…СЂР°РЅРёС‚СЊ РіСЂСѓРїРїСѓ</button></div></form><div class="items" id="targetsList"></div></div></section>
      <section class="section" id="messagePanel"><div class="section-header"><div><h2>Р РµРєР»Р°РјРЅС‹Р№ РјР°С‚РµСЂРёР°Р»</h2><p>РћС‚РґРµР»СЊРЅС‹Р№ СЂРµРєР»Р°РјРЅС‹Р№ С‚РµРєСЃС‚ Рё РєР°СЂС‚РёРЅРєР° СЃ РєРѕРјРїСЊСЋС‚РµСЂР°.</p></div></div><div class="section-body"><form class="form-grid" onsubmit="saveMessage(event)"><input id="messageId" type="hidden"><label>РќР°Р·РІР°РЅРёРµ РјР°С‚РµСЂРёР°Р»Р°<input id="messageTitle" required></label><label>РђСѓРґРёС‚РѕСЂРёСЏ<select id="messageAudience"><option value="all">Р’СЃРµ</option><option value="clients">РљР»РёРµРЅС‚С‹</option><option value="workers">РњР°СЃС‚РµСЂР°</option></select></label><label class="full">РўРµРєСЃС‚ СЂРµРєР»Р°РјС‹<textarea id="messageBody" required></textarea></label><label>РЎСЃС‹Р»РєР° РЅР° РєР°СЂС‚РёРЅРєСѓ<input id="messageImageUrl"></label><label>РљР°СЂС‚РёРЅРєР° СЃ РєРѕРјРїСЊСЋС‚РµСЂР°<input id="messageImageFile" type="file" accept="image/png,image/jpeg,image/webp,image/gif"></label><label class="full"><input id="messageEnabled" type="checkbox" checked> РњР°С‚РµСЂРёР°Р» РІРєР»СЋС‡РµРЅ</label><div class="form-actions full"><button class="primary">РЎРѕС…СЂР°РЅРёС‚СЊ СЂРµРєР»Р°РјРЅС‹Р№ РјР°С‚РµСЂРёР°Р»</button></div></form><div class="items" id="messagesList"></div></div></section>
      <section class="section"><div class="section-header"><div><h2>Р Р°СЃРїРёСЃР°РЅРёРµ СЂРµРєР»Р°РјС‹</h2><p>Р’С‹Р±РµСЂРёС‚Рµ Facebook-РіСЂСѓРїРїСѓ, РјР°С‚РµСЂРёР°Р» Рё РІСЂРµРјСЏ РїСѓР±Р»РёРєР°С†РёРё.</p></div></div><div class="section-body"><form class="form-grid" onsubmit="saveSchedule(event)"><input id="scheduleId" type="hidden"><label>Р“СЂСѓРїРїР°<select id="scheduleTarget"></select></label><label>РњР°С‚РµСЂРёР°Р»<select id="scheduleMessage"></select></label><label>Р’СЂРµРјСЏ<input id="scheduleTime" type="time" value="10:00" required></label><label><input id="scheduleEnabled" type="checkbox" checked> Р Р°СЃРїРёСЃР°РЅРёРµ РІРєР»СЋС‡РµРЅРѕ</label><div class="form-actions full"><button class="primary">РЎРѕС…СЂР°РЅРёС‚СЊ СЂР°СЃРїРёСЃР°РЅРёРµ</button></div></form><div class="items" id="schedulesList"></div></div></section>
    </div><aside><section class="section"><div class="section-header"><div><h2>Р–СѓСЂРЅР°Р» Facebook</h2><p>РџРѕСЃР»РµРґРЅРёРµ РґРµР№СЃС‚РІРёСЏ.</p></div><button class="danger" onclick="clearLogs()">РћС‡РёСЃС‚РєР° Р¶СѓСЂРЅР°Р»Р°</button></div><div class="section-body"><div class="items" id="logs"></div></div></section></aside></div>
    <div class="send-now"><div><strong>РћС‚РїСЂР°РІРёС‚СЊ СЃРµР№С‡Р°СЃ</strong><span>Р СѓС‡РЅР°СЏ РїРѕРґРіРѕС‚РѕРІРєР° РІС‹Р±СЂР°РЅРЅРѕРіРѕ СЂРµРєР»Р°РјРЅРѕРіРѕ РјР°С‚РµСЂРёР°Р»Р°.</span></div><div><select id="sendTarget"></select><select id="sendMessage"></select><button onclick="sendNow()">РћС‚РїСЂР°РІРёС‚СЊ СЃРµР№С‡Р°СЃ</button></div></div>
  </main>
  <script>const platform = "facebook";""" + APPROVED_MARKETING_SCRIPT + r"""
    function render() {
      renderCommonSelects(); renderMessages(); renderSchedules(); renderLogs();
      const targets = state.targets || [];
      targetCount.textContent = `${targets.length} РіСЂСѓРїРї`; messageCount.textContent = `${(state.messages || []).length} РјР°С‚РµСЂРёР°Р»РѕРІ`; scheduleCount.textContent = `${(state.schedules || []).length} СЂР°СЃРїРёСЃР°РЅРёР№`;
      targetsList.innerHTML = targets.map(item => `<article class="item ${item.enabled ? "" : "off"}"><div class="item-head"><div class="item-title"><strong class="no-translate">${esc(item.name)}</strong><span class="no-translate">${esc(item.target_id || "")}</span></div><div class="item-actions"><button onclick="editFacebookTarget(${item.id})">Р РµРґР°РєС‚РёСЂРѕРІР°С‚СЊ</button><button class="danger" onclick="deleteItem('facebook-target', ${item.id})">РЈРґР°Р»РёС‚СЊ</button></div></div><div>${String(item.keywords || "").split(",").filter(Boolean).map(word => `<span class="tag no-translate">${esc(word.trim())}</span>`).join("")}</div><div class="meta">РњР°С‚РµСЂРёР°Р»: ${esc(item.response_message_id || "РЅРµ РІС‹Р±СЂР°РЅ")} В· РґРµР№СЃС‚РІРёРµ: ${esc(item.action || "same_group")}</div><div class="meta no-translate">${esc(item.notes || "")}</div></article>`).join("") || `<div class="empty">РџРѕРєР° РЅРµС‚ Facebook-РіСЂСѓРїРї</div>`;
    }
    requireAdminAccess(loadState);
  </script>
</body>
</html>"""


if __name__ == "__main__":
    init_db()
    cleanup_expired_data()
    start_cleanup_worker()
    start_marketing_bot_worker()
    print(f"Task server started: http://localhost:{PORT}")
    print(f"Admin password: {ADMIN_PASSWORD}")
    ThreadingHTTPServer((HOST, PORT), App).serve_forever()


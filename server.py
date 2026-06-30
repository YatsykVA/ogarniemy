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
import subprocess
import sys
import signal
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
COLLECTOR_ROOT = os.path.join(ROOT, "collectorhub_project")
COLLECTOR_PROCESS = None
COLLECTOR_SEARCH_PROCESS = None


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
    def legacy_byte(char):
        if ord(char) == 0x0098:
            return b"\x98"
        return char.encode("cp1251")

    if not text:
        return text
    result = []
    i = 0
    while i < len(text):
        if i + 1 < len(text):
            try:
                first = legacy_byte(text[i])
                second = legacy_byte(text[i + 1])
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
        "worker1": "Сотрудник 1",
        "worker2": "Сотрудник 2",
    }
    default_clients = {
        "client1": "Клиент 1",
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
    create_user(conn, "worker1", "123456", "Сотрудник 1")
    create_user(conn, "worker2", "123456", "Сотрудник 2")
    create_client(conn, "client1", "123456", "Клиент 1")
    repair_placeholder_names(conn)

    count = conn.execute("select count(*) from tasks").fetchone()[0]
    if count == 0:
        now = int(time.time())
        conn.executemany(
            "insert into tasks(title, description, created_at) values(?, ?, ?)",
            [
                ("Проверить склад", "Посчитать коробки в зоне A и отметить расхождения.", now),
                ("Доставка документов", "Забрать пакет у администратора и отвезти клиенту.", now),
                ("Фотоотчет", "Сделать фотографии оборудования после установки.", now),
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
        if path == "/api/admin/collectorhub/state":
            self.handle_collectorhub_state()
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
        if path.startswith("/api/admin/collectorhub/"):
            self.handle_collectorhub_post(path)
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


    def handle_collectorhub_state(self):
        if not self.is_admin():
            self.send_json({"error": "unauthorized"}, 401)
            return
        data_dir = os.path.join(COLLECTOR_ROOT, "data")
        os.makedirs(data_dir, exist_ok=True)
        def read_file(name):
            path = os.path.join(data_dir, name)
            if not os.path.exists(path):
                return ""
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        def read_json(name, default):
            try:
                raw = read_file(name)
                return json.loads(raw) if raw.strip() else default
            except Exception:
                return default
        legacy_settings = read_json("collector_settings.json", {"send_mode":"telegram", "facebook_target_group_name":"", "facebook_target_group_url":""})
        app_settings = read_json("settings.json", {})
        settings = {
            "send_mode": app_settings.get("send_destination") or legacy_settings.get("send_mode", "telegram"),
            "facebook_target_group_name": app_settings.get("facebook_target_group_name") or legacy_settings.get("facebook_target_group_name", ""),
            "facebook_target_group_url": app_settings.get("facebook_target_group_url") or legacy_settings.get("facebook_target_group_url", ""),
            "max_posts_per_group": app_settings.get("max_posts_per_group", 100),
            "headless_browser": app_settings.get("headless_browser", False),
        }
        groups_raw = read_file("facebook_groups.txt")
        groups = []
        for line in groups_raw.splitlines():
            line=line.strip()
            if not line or line.startswith("#"):
                continue
            if "|" in line:
                name,url = [x.strip() for x in line.split("|",1)]
            else:
                name,url = line,line
            groups.append({"name": name, "url": url})
        log_path = os.path.join(ROOT, "data", "logs", "collectorhub.log")
        log_text = ""
        if os.path.exists(log_path):
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-160:]
                log_text = "".join(lines)
        proc_running = False
        search_running = False
        global COLLECTOR_PROCESS, COLLECTOR_SEARCH_PROCESS
        if COLLECTOR_PROCESS is not None and COLLECTOR_PROCESS.poll() is None:
            proc_running = True
        if COLLECTOR_SEARCH_PROCESS is not None and COLLECTOR_SEARCH_PROCESS.poll() is None:
            search_running = True
        self.send_json({
            "running": proc_running,
            "searchRunning": search_running,
            "keywords": read_file("keywords.txt"),
            "exclusions": read_file("exclusions.txt"),
            "groupsText": groups_raw,
            "groups": groups,
            "settings": settings,
            "log": log_text,
            "collectorRoot": COLLECTOR_ROOT,
        })

    def handle_collectorhub_post(self, path):
        if not self.is_admin():
            self.send_json({"error": "unauthorized"}, 401)
            return
        data = self.read_json()
        data_dir = os.path.join(COLLECTOR_ROOT, "data")
        os.makedirs(data_dir, exist_ok=True)
        def write_file(name, content):
            with open(os.path.join(data_dir, name), "w", encoding="utf-8") as f:
                f.write(str(content or ""))
        global COLLECTOR_PROCESS
        try:
            if path == "/api/admin/collectorhub/save-words":
                write_file("keywords.txt", data.get("keywords", ""))
                write_file("exclusions.txt", data.get("exclusions", ""))
                self.send_json({"ok": True})
                return
            if path == "/api/admin/collectorhub/save-groups":
                write_file("facebook_groups.txt", data.get("groupsText", ""))
                self.send_json({"ok": True})
                return
            if path == "/api/admin/collectorhub/save-settings":
                mode = str(data.get("sendMode", "telegram") or "telegram").strip().lower()
                if mode not in {"telegram", "facebook", "both"}:
                    mode = "telegram"
                try:
                    max_posts = int(data.get("postsLimit", 100) or 100)
                except Exception:
                    max_posts = 100
                max_posts = max(1, min(max_posts, 500))
                settings = {
                    "send_mode": mode,
                    "facebook_target_group_name": str(data.get("facebookTargetName", "") or ""),
                    "facebook_target_group_url": str(data.get("facebookTargetUrl", "") or ""),
                    "max_posts_per_group": max_posts,
                    "headless_browser": bool(data.get("headlessBrowser", False)),
                }
                # Совместимость со старым collector_settings.py
                with open(os.path.join(data_dir, "collector_settings.json"), "w", encoding="utf-8") as f:
                    json.dump({
                        "send_mode": mode,
                        "facebook_target_group_name": settings["facebook_target_group_name"],
                        "facebook_target_group_url": settings["facebook_target_group_url"],
                    }, f, ensure_ascii=False, indent=4)
                # Реальный config.py читает именно data/settings.json
                config_path = os.path.join(data_dir, "settings.json")
                try:
                    with open(config_path, "r", encoding="utf-8") as f:
                        config_data = json.load(f)
                        if not isinstance(config_data, dict):
                            config_data = {}
                except Exception:
                    config_data = {}
                config_data.update({
                    "send_destination": mode,
                    "facebook_target_group_name": settings["facebook_target_group_name"],
                    "facebook_target_group_url": settings["facebook_target_group_url"],
                    "max_posts_per_group": max_posts,
                    "headless_browser": settings["headless_browser"],
                })
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(config_data, f, ensure_ascii=False, indent=4)
                self.send_json({"ok": True, "settings": settings})
                return
            if path == "/api/admin/collectorhub/start":
                if COLLECTOR_PROCESS is not None and COLLECTOR_PROCESS.poll() is None:
                    self.send_json({"ok": True, "alreadyRunning": True})
                    return
                script = os.path.join(COLLECTOR_ROOT, "collector.py")
                if not os.path.exists(script):
                    self.send_json({"error": "collector.py not found"}, 500)
                    return
                env = os.environ.copy()
                env["PYTHONUNBUFFERED"] = "1"
                COLLECTOR_PROCESS = subprocess.Popen(
                    [sys.executable, script],
                    cwd=COLLECTOR_ROOT,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=env,
                )
                self.send_json({"ok": True, "pid": COLLECTOR_PROCESS.pid})
                return
            if path == "/api/admin/collectorhub/stop":
                if COLLECTOR_PROCESS is not None and COLLECTOR_PROCESS.poll() is None:
                    COLLECTOR_PROCESS.terminate()
                    self.send_json({"ok": True})
                else:
                    self.send_json({"ok": True, "alreadyStopped": True})
                return
            if path == "/api/admin/collectorhub/search-groups":
                global COLLECTOR_SEARCH_PROCESS
                if COLLECTOR_SEARCH_PROCESS is not None and COLLECTOR_SEARCH_PROCESS.poll() is None:
                    self.send_json({"ok": True, "alreadyRunning": True})
                    return
                query = str(data.get("query", "") or data.get("queries", "") or "").strip()
                if not query:
                    self.send_json({"error": "Введите слова для автопоиска групп"}, 400)
                    return
                script = os.path.join(COLLECTOR_ROOT, "facebook_group_search.py")
                if not os.path.exists(script):
                    self.send_json({"error": "facebook_group_search.py not found"}, 500)
                    return
                env = os.environ.copy()
                env["PYTHONUNBUFFERED"] = "1"
                env["FB_GROUP_SEARCH_QUERIES"] = query
                COLLECTOR_SEARCH_PROCESS = subprocess.Popen(
                    [sys.executable, script],
                    cwd=COLLECTOR_ROOT,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=env,
                )
                self.send_json({"ok": True, "pid": COLLECTOR_SEARCH_PROCESS.pid})
                return
            if path == "/api/admin/collectorhub/test-telegram":
                script = os.path.join(COLLECTOR_ROOT, "telegram_test.py")
                if not os.path.exists(script):
                    self.send_json({"error": "telegram_test.py not found"}, 500)
                    return
                result = subprocess.run([sys.executable, script], cwd=COLLECTOR_ROOT, capture_output=True, text=True, timeout=90, env={**os.environ.copy(), "PYTHONUNBUFFERED":"1"})
                self.send_json({"ok": result.returncode == 0, "code": result.returncode, "stdout": result.stdout[-4000:], "stderr": result.stderr[-4000:]}, 200 if result.returncode == 0 else 500)
                return
            if path == "/api/admin/collectorhub/resend-last":
                script = os.path.join(COLLECTOR_ROOT, "telegram_resend_last.py")
                if not os.path.exists(script):
                    self.send_json({"error": "telegram_resend_last.py not found"}, 500)
                    return
                result = subprocess.run([sys.executable, script], cwd=COLLECTOR_ROOT, capture_output=True, text=True, timeout=90, env={**os.environ.copy(), "PYTHONUNBUFFERED":"1"})
                self.send_json({"ok": result.returncode == 0, "code": result.returncode, "stdout": result.stdout[-4000:], "stderr": result.stderr[-4000:]}, 200 if result.returncode == 0 else 500)
                return
            if path == "/api/admin/collectorhub/reset-posts":
                script = os.path.join(COLLECTOR_ROOT, "reset_posts_db.py")
                if os.path.exists(script):
                    result = subprocess.run([sys.executable, script], cwd=COLLECTOR_ROOT, capture_output=True, text=True, timeout=60, env={**os.environ.copy(), "PYTHONUNBUFFERED":"1"})
                    self.send_json({"ok": result.returncode == 0, "code": result.returncode, "stdout": result.stdout[-4000:], "stderr": result.stderr[-4000:]}, 200 if result.returncode == 0 else 500)
                    return
            if path == "/api/admin/collectorhub/clear-posts":
                db_path = os.path.join(data_dir, "collector.db")
                if os.path.exists(db_path):
                    try:
                        conn = sqlite3.connect(db_path)
                        for table in ["posts", "sent_posts", "facebook_posts"]:
                            try:
                                conn.execute(f"delete from {table}")
                            except Exception:
                                pass
                        conn.commit(); conn.close()
                    except Exception as exc:
                        self.send_json({"error": str(exc)}, 500)
                        return
                self.send_json({"ok": True})
                return
        except Exception as exc:
            self.send_json({"error": str(exc)}, 500)
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
                "select chat_id, title, city, keywords, exclude_keywords, enabled, watch_enabled, target_chat_id, response_message_id, notes, created_at from telegram_groups order by title"
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
            "select id, name, city, target_id, notes, keywords, exclude_keywords, action, response_message_id, enabled, created_at from facebook_targets order by name"
        ).fetchall()]
        telegram_groups = [dict(row) for row in conn.execute(
            "select chat_id, title, enabled from telegram_groups where enabled = 1 order by title, chat_id"
        ).fetchall()]
        subscribers = [dict(row) for row in conn.execute(
            "select role, city, count(*) count from facebook_subscribers where stopped_at is null group by role, city order by role, city"
        ).fetchall()]
        hits = [dict(row) for row in conn.execute(
            "select target_id, target_name, keyword, username, message, action, created_at from facebook_keyword_hits order by id desc limit 40"
        ).fetchall()]
        settings = {row["key"]: row["value"] for row in conn.execute(
            "select key, value from app_config where key in ('facebook_forward_target_id', 'facebook_forward_telegram_chat_id', 'facebook_page_access_token')"
        ).fetchall()}
        if settings.get("facebook_page_access_token"):
            token = settings["facebook_page_access_token"]
            settings["facebook_page_access_token_saved"] = "1"
            settings["facebook_page_access_token_mask"] = token[:6] + "..." + token[-4:] if len(token) > 12 else "saved"
            settings.pop("facebook_page_access_token", None)
        conn.close()
        self.send_json({"cities": cities, "targets": targets, "telegramGroups": telegram_groups, "subscribers": subscribers, "messages": messages, "schedules": schedules, "logs": logs, "hits": hits, "settings": settings})

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
                watch_enabled = 1 if data.get("watchEnabled", False) else 0
                target_chat_id = str(data.get("targetChatId", "")).strip()
                if target_chat_id == "__same_group__":
                    target_chat_id = ""
                if watch_enabled:
                    conn.execute(
                        """
                        insert into telegram_groups(chat_id, title, city, keywords, exclude_keywords, enabled, watch_enabled, target_chat_id, response_message_id, notes, created_at)
                        values(?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                        on conflict(chat_id) do update set
                          title = excluded.title,
                          city = excluded.city,
                          keywords = excluded.keywords,
                          exclude_keywords = excluded.exclude_keywords,
                          enabled = excluded.enabled,
                          watch_enabled = 1,
                          target_chat_id = excluded.target_chat_id,
                          response_message_id = excluded.response_message_id,
                          notes = excluded.notes
                        """,
                        (
                            chat_id,
                            str(data.get("title", "")).strip(),
                            "",
                            str(data.get("keywords", "")).strip(),
                            str(data.get("excludeKeywords", "")).strip(),
                            1 if data.get("enabled", True) else 0,
                            target_chat_id,
                            int(data.get("responseMessageId") or 0) or None,
                            str(data.get("notes", "")).strip(),
                            now,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        insert into telegram_groups(chat_id, title, city, keywords, enabled, watch_enabled, notes, created_at)
                        values(?, ?, '', '', ?, 0, ?, ?)
                        on conflict(chat_id) do update set
                          title = excluded.title,
                          enabled = excluded.enabled,
                          notes = excluded.notes
                        """,
                        (
                            chat_id,
                            str(data.get("title", "")).strip(),
                            1 if data.get("enabled", True) else 0,
                            str(data.get("notes", "")).strip(),
                            now,
                        ),
                    )
            elif action == "facebook-target" and platform == "facebook":
                target_id = data.get("id")
                if target_id:
                    conn.execute(
                        "update facebook_targets set name = ?, city = ?, target_id = ?, notes = ?, keywords = ?, exclude_keywords = ?, action = ?, response_message_id = ?, enabled = ? where id = ?",
                        (str(data.get("name", "")).strip(), "", str(data.get("targetId", "")).strip(), str(data.get("notes", "")).strip(), str(data.get("keywords", "")).strip(), str(data.get("excludeKeywords", "")).strip(), str(data.get("targetAction", "same_group")).strip(), int(data.get("responseMessageId") or 0) or None, 1 if data.get("enabled", True) else 0, int(target_id)),
                    )
                else:
                    conn.execute(
                        "insert into facebook_targets(name, city, target_id, notes, keywords, exclude_keywords, action, response_message_id, enabled, created_at) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (str(data.get("name", "")).strip(), "", str(data.get("targetId", "")).strip(), str(data.get("notes", "")).strip(), str(data.get("keywords", "")).strip(), str(data.get("excludeKeywords", "")).strip(), str(data.get("targetAction", "same_group")).strip(), int(data.get("responseMessageId") or 0) or None, 1 if data.get("enabled", True) else 0, now),
                    )
            elif action == "facebook-settings" and platform == "facebook":
                conn.execute(
                    "insert into app_config(key, value) values('facebook_forward_target_id', ?) on conflict(key) do update set value = excluded.value",
                    (str(data.get("facebookForwardTargetId", "")).strip(),),
                )
                conn.execute(
                    "insert into app_config(key, value) values('facebook_forward_telegram_chat_id', ?) on conflict(key) do update set value = excluded.value",
                    (str(data.get("facebookForwardTelegramChatId", "")).strip(),),
                )
                token = str(data.get("facebookPageAccessToken", "")).strip()
                if token:
                    conn.execute(
                        "insert into app_config(key, value) values('facebook_page_access_token', ?) on conflict(key) do update set value = excluded.value",
                        (token,),
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
            elif action == "clear-hits" and platform == "telegram":
                conn.execute("delete from keyword_hits")
            elif action == "clear-hits" and platform == "facebook":
                conn.execute("delete from facebook_keyword_hits")
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
                values = (platform, str(data.get("title", "")).strip() or "Реклама", str(data.get("audience", "all")).strip() or "all", str(data.get("body", "")).strip(), str(data.get("imageUrl", "")).strip(), 1 if data.get("enabled", True) else 0, now)
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
                sent, total = marketing.post_to_facebook_targets(message["body"], target_id)
                self.send_json({"ok": True, "sent": sent, "total": total})
                return
                marketing.log_marketing("facebook", "manual", "", city, "send_now", "prepared", "Facebook отправка требует подключенного Page Access Token и входящих подписчиков.")
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
  body {
    background: linear-gradient(135deg, #fff7df 0%, #d8f8eb 48%, #e9d5ff 100%) !important;
    min-height: 100vh !important;
  }
  header {
    position: relative !important;
    background: linear-gradient(135deg, #0f766e 0%, #2563eb 48%, #7c3aed 100%) !important;
    color: white !important;
    padding: 24px 28px !important;
    box-shadow: none !important;
  }
  header h1 {
    margin: 0 0 12px !important;
    font-family: Arial, sans-serif !important;
    font-size: 28px !important;
    line-height: 1.15 !important;
    letter-spacing: 0 !important;
  }
  header nav {
    display: grid !important;
    grid-template-columns: repeat(10, minmax(104px, 1fr)) !important;
    gap: 10px !important;
    align-items: stretch !important;
    margin-top: 12px !important;
    max-width: 1320px !important;
  }
  header nav a {
    min-width: 0 !important;
    min-height: 44px !important;
    height: 44px !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
    padding: 8px 10px !important;
    border-radius: 8px !important;
    background: #f6c85f !important;
    color: #172026 !important;
    font-family: Arial, sans-serif !important;
    font-size: 13px !important;
    font-weight: 700 !important;
    text-align: center !important;
    line-height: 1.15 !important;
    text-decoration: none !important;
    white-space: normal !important;
    box-sizing: border-box !important;
    overflow-wrap: anywhere !important;
    box-shadow: none !important;
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
    header nav a { height: 44px !important; }
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
      "Задания": "Tasks", "Сотрудники": "Employees", "Клиенты": "Clients", "Выполненные задания": "Completed tasks",
      "Расчеты сотрудников": "Employee payments", "Расчеты клиентов": "Client payments", "Настройки": "Settings",
      "Название задания": "Task name", "Название": "Name", "Задание": "Task", "Описание": "Description",
      "Номер телефона": "Phone number", "Номер": "Phone", "Телефона": "Number", "Телефон": "Phone",
      "Адрес": "Address", "Город": "City", "Код": "Postal code", "Улица": "Street", "Дом": "House", "Квартира": "Apartment", "Цена": "Price", "Карта": "Card", "Наличные": "Cash", "Оплата": "Payment",
      "Добавить": "Add", "Редактировать": "Edit", "Сохранить": "Save", "Отмена": "Cancel", "Удалить": "Delete",
      "Начать заново": "Start again", "Обновить список": "Refresh list", "Имя сотрудника": "Employee name",
      "Имя клиента": "Client name", "Логин": "Login", "Пароль": "Password", "Новый пароль, если нужно": "New password if needed",
      "Удалить сотрудника": "Delete employee", "Удалить клиента": "Delete client", "Отчет": "Report", "Рассчитать": "Calculate",
      "Оплатить": "Pay", "Оплачено": "Paid", "Не оплачено": "Not paid", "Рассчитано": "Paid", "Не рассчитано": "Not paid", "Статус расчета": "Payment status",
      "Статус": "Status", "Новое": "New", "Принято": "Accepted", "Отклонено": "Declined", "Выполнено": "Completed",
      "Отказался": "Refused", "В работе": "In progress", "Всего назначено": "Total assigned", "Активные": "Active",
      "Новые": "New", "Всего заданий": "Total tasks", "Заданий": "Tasks", "Текущий расчет": "Current payment",
      "История расчетов": "Payment history", "Расчет": "Payment", "Сумма расчета": "Payment total",
      "Сумма к оплате": "Amount to pay", "Сумма к выплате": "Amount to pay", "Сумма выполненных": "Completed total", "Сумма отказанных": "Refused total",
      "Общая сумма заданий": "Total task amount", "Выполненные работы": "Completed jobs", "Отказанные работы": "Refused jobs",
      "Работы с отказом": "Refused jobs", "Остальные работы": "Other jobs", "Активные работы": "Active jobs",
      "Полный отчет по расчету": "Full payment report", "Подробный отчет": "Detailed report", "Нет записей": "No records",
      "Созданных расчетов пока нет": "No payments have been created yet", "Новый расчет создается в разделе": "A new payment is created in",
      "кнопкой": "with the button", "Пароль подтверждения": "Confirmation password", "Сумма": "Amount",
      "Резерв": "Reserve", "Пополнить резерв": "Top up reserve", "Пополнить": "Top up", "Операции резерва": "Reserve operations",
      "Из суммы к выплате в резерв": "From payout to reserve", "Из резерва в выплату": "From reserve to payout",
      "Пополнение резерва": "Reserve top-up", "Удержание за отказ из резерва": "Refusal fee from reserve",
      "Удержание за наличные из резерва": "Cash job fee from reserve", "Удержание": "Fee",
      "Процент с выполненных работ, который мы удерживаем себе": "Percent withheld from completed jobs",
      "Процент с отказанных или отмененных работ, который мы удерживаем себе": "Percent withheld from refused or cancelled jobs",
      "Валюта": "Currency", "Единица резерва": "Reserve unit", "Показывать цены и суммы": "Show prices and amounts", "Сколько дней хранить выполненное задание": "How many days to keep completed tasks", "Сколько дней хранить непринятые задания": "How many days to keep unaccepted tasks", "Сколько дней хранить расчеты сотрудников": "How many days to keep employee payments", "Сколько дней хранить расчеты клиентов": "How many days to keep client payments", "Телефон обратной связи": "Feedback phone",
      "E-mail обратной связи": "Feedback e-mail", "Обычный адрес": "Regular address", "Telegram": "Telegram", "WhatsApp": "WhatsApp",
      "Изменить пароль": "Change password", "Старый пароль": "Old password", "Повторите старый пароль": "Repeat old password",
      "Введите новый пароль": "Enter new password", "Сбросить пароль": "Reset password", "Настройки сохранены": "Settings saved",
      "Не удалось загрузить настройки": "Could not load settings", "Не удалось сохранить настройки": "Could not save settings",
      "Пароль изменен": "Password changed", "Пароль сброшен": "Password reset", "Создано": "Created", "Изменено": "Changed",
      "Источник": "Source", "Диспетчер": "Dispatcher", "Клиент": "Client", "сотрудник": "employee",
      "Удалить это задание": "Delete this task", "Не удалось вернуть задание": "Could not return task", "Не удалось удалить задание": "Could not delete task",
      "Не удалось сохранить задание": "Could not save task", "Не удалось загрузить клиентов": "Could not load clients",
      "Не удалось сохранить клиента": "Could not save client", "Не удалось удалить клиента": "Could not delete client",
      "Не удалось добавить клиента": "Could not add client", "Нужно заново ввести пароль администратора": "Enter the admin password again",
      "Заполните имя клиента и логин": "Fill in the client name and login", "Пароль должен быть не короче 4 символов": "Password must be at least 4 characters",
      "Такой логин уже используется": "This login is already used", "Клиент не найден": "Client not found",
      "Удалить расчет": "Delete payment", "Не удалось загрузить расчеты": "Could not load payments",
      "Не удалось загрузить расчеты клиентов": "Could not load client payments", "Не удалось удалить расчет": "Could not delete payment",
      "Не удалось оплатить": "Could not pay", "Не удалось отметить расчет как рассчитанный": "Could not mark payment as paid"
    },
    uk: {
      "Задания": "Завдання", "Сотрудники": "Співробітники", "Клиенты": "Клієнти", "Выполненные задания": "Виконані завдання",
      "Расчеты сотрудников": "Розрахунки співробітників", "Расчеты клиентов": "Розрахунки клієнтів", "Настройки": "Налаштування",
      "Название задания": "Назва завдання", "Название": "Назва", "Задание": "Завдання", "Описание": "Опис",
      "Номер телефона": "Номер телефону", "Номер": "Номер", "Телефона": "Телефону", "Телефон": "Телефон",
      "Адрес": "Адреса", "Город": "Місто", "Код": "Код", "Улица": "Вулиця", "Дом": "Будинок", "Квартира": "Квартира", "Цена": "Ціна", "Карта": "Картка", "Наличные": "Готівка", "Оплата": "Оплата",
      "Добавить": "Додати", "Редактировать": "Редагувати", "Сохранить": "Зберегти", "Отмена": "Скасувати", "Удалить": "Видалити",
      "Начать заново": "Почати заново", "Обновить список": "Оновити список", "Имя сотрудника": "Ім'я співробітника",
      "Имя клиента": "Ім'я клієнта", "Логин": "Логін", "Пароль": "Пароль", "Новый пароль, если нужно": "Новий пароль, якщо потрібно",
      "Удалить сотрудника": "Видалити співробітника", "Удалить клиента": "Видалити клієнта", "Отчет": "Звіт", "Рассчитать": "Розрахувати",
      "Оплатить": "Оплатити", "Оплачено": "Оплачено", "Не оплачено": "Не оплачено", "Рассчитано": "Оплачено", "Не рассчитано": "Не оплачено", "Статус расчета": "Статус розрахунку",
      "Статус": "Статус", "Новое": "Нове", "Принято": "Прийнято", "Отклонено": "Відхилено", "Выполнено": "Виконано",
      "Отказался": "Відмовився", "В работе": "У роботі", "Всего назначено": "Усього призначено", "Активные": "Активні",
      "Новые": "Нові", "Всего заданий": "Усього завдань", "Заданий": "Завдань", "Текущий расчет": "Поточний розрахунок",
      "История расчетов": "Історія розрахунків", "Расчет": "Розрахунок", "Сумма расчета": "Сума розрахунку",
      "Сумма к оплате": "Сума до оплати", "Сумма к выплате": "Сума до виплати", "Сумма выполненных": "Сума виконаних", "Сумма отказанных": "Сума відмов",
      "Общая сумма заданий": "Загальна сума завдань", "Выполненные работы": "Виконані роботи", "Отказанные работы": "Відмовлені роботи",
      "Работы с отказом": "Роботи з відмовою", "Остальные работы": "Інші роботи", "Активные работы": "Активні роботи",
      "Полный отчет по расчету": "Повний звіт за розрахунком", "Подробный отчет": "Докладний звіт", "Нет записей": "Немає записів",
      "Созданных расчетов пока нет": "Створених розрахунків поки немає", "Новый расчет создается в разделе": "Новий розрахунок створюється в розділі",
      "кнопкой": "кнопкою", "Пароль подтверждения": "Пароль підтвердження", "Сумма": "Сума",
      "Резерв": "Резерв", "Пополнить резерв": "Поповнити резерв", "Пополнить": "Поповнити", "Операции резерва": "Операції резерву",
      "Из суммы к выплате в резерв": "Із суми до виплати в резерв", "Из резерва в выплату": "З резерву у виплату",
      "Пополнение резерва": "Поповнення резерву", "Удержание за отказ из резерва": "Утримання за відмову з резерву",
      "Удержание за наличные из резерва": "Утримання за готівку з резерву", "Удержание": "Утримання",
      "Процент с выполненных работ, который мы удерживаем себе": "Відсоток з виконаних робіт, який ми утримуємо собі",
      "Процент с отказанных или отмененных работ, который мы удерживаем себе": "Відсоток з відмовлених або скасованих робіт, який ми утримуємо собі",
      "Валюта": "Валюта", "Единица резерва": "Одиниця резерву", "Показывать цены и суммы": "Показувати ціни та суми", "Сколько дней хранить выполненное задание": "Скільки днів зберігати виконане завдання", "Сколько дней хранить непринятые задания": "Скільки днів зберігати неприйняті завдання", "Сколько дней хранить расчеты сотрудников": "Скільки днів зберігати розрахунки співробітників", "Сколько дней хранить расчеты клиентов": "Скільки днів зберігати розрахунки клієнтів", "Телефон обратной связи": "Телефон зворотного зв'язку",
      "E-mail обратной связи": "E-mail зворотного зв'язку", "Обычный адрес": "Звичайна адреса", "Telegram": "Telegram", "WhatsApp": "WhatsApp",
      "Изменить пароль": "Змінити пароль", "Старый пароль": "Старий пароль", "Повторите старый пароль": "Повторіть старий пароль",
      "Введите новый пароль": "Введіть новий пароль", "Сбросить пароль": "Скинути пароль", "Настройки сохранены": "Налаштування збережено",
      "Не удалось загрузить настройки": "Не вдалося завантажити налаштування", "Не удалось сохранить настройки": "Не вдалося зберегти налаштування",
      "Пароль изменен": "Пароль змінено", "Пароль сброшен": "Пароль скинуто", "Создано": "Створено", "Изменено": "Змінено",
      "Источник": "Джерело", "Диспетчер": "Диспетчер", "Клиент": "Клієнт", "сотрудник": "співробітник",
      "Удалить это задание": "Видалити це завдання", "Не удалось вернуть задание": "Не вдалося повернути завдання", "Не удалось удалить задание": "Не вдалося видалити завдання",
      "Не удалось сохранить задание": "Не вдалося зберегти завдання", "Не удалось загрузить клиентов": "Не вдалося завантажити клієнтів",
      "Не удалось сохранить клиента": "Не вдалося зберегти клієнта", "Не удалось удалить клиента": "Не вдалося видалити клієнта",
      "Не удалось добавить клиента": "Не вдалося додати клієнта", "Нужно заново ввести пароль администратора": "Потрібно знову ввести пароль адміністратора",
      "Заполните имя клиента и логин": "Заповніть ім'я клієнта і логін", "Пароль должен быть не короче 4 символов": "Пароль має бути не коротшим за 4 символи",
      "Такой логин уже используется": "Такий логін вже використовується", "Клиент не найден": "Клієнта не знайдено",
      "Удалить расчет": "Видалити розрахунок", "Не удалось загрузить расчеты": "Не вдалося завантажити розрахунки",
      "Не удалось загрузить расчеты клиентов": "Не вдалося завантажити розрахунки клієнтів", "Не удалось удалить расчет": "Не вдалося видалити розрахунок",
      "Не удалось оплатить": "Не вдалося оплатити", "Не удалось отметить расчет как рассчитанный": "Не вдалося позначити розрахунок як оплачений"
    },
    pl: {
      "Задания": "Zadania", "Сотрудники": "Pracownicy", "Клиенты": "Klienci", "Выполненные задания": "Wykonane zadania",
      "Расчеты сотрудников": "Rozliczenia pracowników", "Расчеты клиентов": "Rozliczenia klientów", "Настройки": "Ustawienia",
      "Название задания": "Nazwa zadania", "Название": "Nazwa", "Задание": "Zadanie", "Описание": "Opis",
      "Номер телефона": "Numer telefonu", "Номер": "Numer", "Телефона": "Telefonu", "Телефон": "Telefon",
      "Адрес": "Adres", "Город": "Miasto", "Код": "Kod", "Улица": "Ulica", "Дом": "Dom", "Квартира": "Mieszkanie", "Цена": "Cena", "Карта": "Karta", "Наличные": "Gotówka", "Оплата": "Płatność",
      "Добавить": "Dodaj", "Редактировать": "Edytuj", "Сохранить": "Zapisz", "Отмена": "Anuluj", "Удалить": "Usuń",
      "Начать заново": "Zacznij od nowa", "Обновить список": "Odśwież listę", "Имя сотрудника": "Imię pracownika",
      "Имя клиента": "Imię klienta", "Логин": "Login", "Пароль": "Hasło", "Новый пароль, если нужно": "Nowe hasło, jeśli potrzebne",
      "Удалить сотрудника": "Usuń pracownika", "Удалить клиента": "Usuń klienta", "Отчет": "Raport", "Рассчитать": "Rozlicz",
      "Оплатить": "Opłacić", "Оплачено": "Opłacono", "Не оплачено": "Nie opłacono", "Рассчитано": "Opłacono", "Не рассчитано": "Nie opłacono", "Статус расчета": "Status rozliczenia",
      "Статус": "Status", "Новое": "Nowe", "Принято": "Przyjęte", "Отклонено": "Odrzucone", "Выполнено": "Wykonane",
      "Отказался": "Odmówił", "В работе": "W trakcie", "Всего назначено": "Łącznie przypisane", "Активные": "Aktywne",
      "Новые": "Nowe", "Всего заданий": "Łącznie zadań", "Заданий": "Zadań", "Текущий расчет": "Bieżące rozliczenie",
      "История расчетов": "Historia rozliczeń", "Расчет": "Rozliczenie", "Сумма расчета": "Suma rozliczenia",
      "Сумма к оплате": "Kwota do zapłaty", "Сумма к выплате": "Kwota do wypłaty", "Сумма выполненных": "Suma wykonanych", "Сумма отказанных": "Suma odmów",
      "Общая сумма заданий": "Łączna kwota zadań", "Выполненные работы": "Wykonane prace", "Отказанные работы": "Odmówione prace",
      "Работы с отказом": "Prace z odmową", "Остальные работы": "Pozostałe prace", "Активные работы": "Aktywne prace",
      "Полный отчет по расчету": "Pełny raport rozliczenia", "Подробный отчет": "Szczegółowy raport", "Нет записей": "Brak wpisów",
      "Созданных расчетов пока нет": "Nie ma jeszcze utworzonych rozliczeń", "Новый расчет создается в разделе": "Nowe rozliczenie tworzy się w sekcji",
      "кнопкой": "przyciskiem", "Пароль подтверждения": "Hasło potwierdzenia", "Сумма": "Kwota",
      "Резерв": "Rezerwa", "Пополнить резерв": "Doładuj rezerwę", "Пополнить": "Doładuj", "Операции резерва": "Operacje rezerwy",
      "Из суммы к выплате в резерв": "Z kwoty wypłaty do rezerwy", "Из резерва в выплату": "Z rezerwy do wypłaty",
      "Пополнение резерва": "Doładowanie rezerwy", "Удержание за отказ из резерва": "Potrącenie za odmowę z rezerwy",
      "Удержание за наличные из резерва": "Potrącenie za gotówkę z rezerwy", "Удержание": "Potrącenie",
      "Процент с выполненных работ, который мы удерживаем себе": "Procent z wykonanych prac, który zatrzymujemy",
      "Процент с отказанных или отмененных работ, который мы удерживаем себе": "Procent z odmówionych lub anulowanych prac, który zatrzymujemy",
      "Валюта": "Waluta", "Единица резерва": "Jednostka rezerwy", "Показывать цены и суммы": "Pokazywać ceny i kwoty", "Сколько дней хранить выполненное задание": "Ile dni przechowywać wykonane zadanie", "Сколько дней хранить непринятые задания": "Ile dni przechowywać nieprzyjęte zadania", "Сколько дней хранить расчеты сотрудников": "Ile dni przechowywać rozliczenia pracowników", "Сколько дней хранить расчеты клиентов": "Ile dni przechowywać rozliczenia klientów", "Телефон обратной связи": "Telefon kontaktowy",
      "E-mail обратной связи": "E-mail kontaktowy", "Обычный адрес": "Zwykły adres", "Telegram": "Telegram", "WhatsApp": "WhatsApp",
      "Изменить пароль": "Zmień hasło", "Старый пароль": "Stare hasło", "Повторите старый пароль": "Powtórz stare hasło",
      "Введите новый пароль": "Wpisz nowe hasło", "Сбросить пароль": "Resetuj hasło", "Настройки сохранены": "Ustawienia zapisane",
      "Не удалось загрузить настройки": "Nie udało się załadować ustawień", "Не удалось сохранить настройки": "Nie udało się zapisać ustawień",
      "Пароль изменен": "Hasło zmienione", "Пароль сброшен": "Hasło zresetowane", "Создано": "Utworzono", "Изменено": "Zmieniono",
      "Источник": "Źródło", "Диспетчер": "Dyspozytor", "Клиент": "Klient", "сотрудник": "pracownik",
      "Удалить это задание": "Usunąć to zadanie", "Не удалось вернуть задание": "Nie udało się przywrócić zadania", "Не удалось удалить задание": "Nie udało się usunąć zadania",
      "Не удалось сохранить задание": "Nie udało się zapisać zadania", "Не удалось загрузить клиентов": "Nie udało się załadować klientów",
      "Не удалось сохранить клиента": "Nie udało się zapisać klienta", "Не удалось удалить клиента": "Nie udało się usunąć klienta",
      "Не удалось добавить клиента": "Nie udało się dodać klienta", "Нужно заново ввести пароль администратора": "Wpisz ponownie hasło administratora",
      "Заполните имя клиента и логин": "Wypełnij imię klienta i login", "Пароль должен быть не короче 4 символов": "Hasło musi mieć co najmniej 4 znaki",
      "Такой логин уже используется": "Ten login jest już używany", "Клиент не найден": "Klient nie znaleziony",
      "Удалить расчет": "Usuń rozliczenie", "Не удалось загрузить расчеты": "Nie udało się załadować rozliczeń",
      "Не удалось загрузить расчеты клиентов": "Nie udało się załadować rozliczeń klientów", "Не удалось удалить расчет": "Nie udało się usunąć rozliczenia",
      "Не удалось оплатить": "Nie udało się opłacić", "Не удалось отметить расчет как рассчитанный": "Nie udało się oznaczyć rozliczenia jako opłacone"
    },
    ru: {}
  };
  dict.en["Рассчитать всех"] = "Calculate all";
  dict.uk["Рассчитать всех"] = "Розрахувати всіх";
  dict.pl["Рассчитать всех"] = "Rozlicz wszystkich";
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
  Object.assign(dict.en, {
    "Задания": "Tasks", "Сотрудники": "Employees", "Клиенты": "Clients", "Выполненные задания": "Completed tasks", "Расчеты сотрудников": "Employee payments", "Расчеты клиентов": "Client payments", "Реклама Telegram": "Telegram Ads", "Реклама Facebook": "Facebook Ads", "Настройки": "Settings",
    "Вход Telegram userbot": "Telegram userbot login", "Состояние": "Status", "Проверяю подключение Telegram.": "Checking Telegram connection.", "Обновить состояние": "Refresh status",
    "1. Отправить код": "1. Send code", "Номер телефона Telegram": "Telegram phone number", "Имя сессии": "Session name", "API ID и API Hash берутся на my.telegram.org в разделе API development tools. Код придет в Telegram.": "API ID and API Hash are taken from my.telegram.org in API development tools. The code will arrive in Telegram.",
    "Отправить код": "Send code", "2. Подтвердить вход": "2. Confirm login", "Код из Telegram": "Telegram code", "Облачный пароль Telegram, если включен": "Telegram cloud password, if enabled", "Сохранить личный аккаунт": "Save personal account",
    "Не удалось проверить Telegram userbot.": "Could not check Telegram userbot.", "сессия сохранена": "session saved", "сессии пока нет": "no session yet", "API настроен": "API configured", "API еще не настроен": "API is not configured yet", "Сессия": "Session", "телефон": "phone",
    "Не удалось связаться с сервером Telegram userbot.": "Could not connect to the Telegram userbot server.", "Введите API ID, API Hash и номер телефона.": "Enter API ID, API Hash and phone number.", "Отправляю код в Telegram...": "Sending code to Telegram...", "Не удалось отправить код.": "Could not send the code.",
    "Код отправлен на": "Code sent to", "Введите его ниже.": "Enter it below.", "Сначала отправьте код.": "Send the code first.", "Проверяю код...": "Checking code...", "Telegram просит облачный пароль. Введите его и нажмите кнопку еще раз.": "Telegram asks for the cloud password. Enter it and press the button again.", "Не удалось подтвердить код.": "Could not confirm the code.", "Готово. Сессия сохранена для": "Done. Session saved for"
  });
  Object.assign(dict.uk, {
    "Задания": "Завдання", "Сотрудники": "Співробітники", "Клиенты": "Клієнти", "Выполненные задания": "Виконані завдання", "Расчеты сотрудников": "Розрахунки співробітників", "Расчеты клиентов": "Розрахунки клієнтів", "Реклама Telegram": "Реклама Telegram", "Реклама Facebook": "Реклама Facebook", "Настройки": "Налаштування",
    "Вход Telegram userbot": "Вхід Telegram userbot", "Состояние": "Стан", "Проверяю подключение Telegram.": "Перевіряю підключення Telegram.", "Обновить состояние": "Оновити стан",
    "1. Отправить код": "1. Надіслати код", "Номер телефона Telegram": "Номер телефону Telegram", "Имя сессии": "Назва сесії", "API ID и API Hash берутся на my.telegram.org в разделе API development tools. Код придет в Telegram.": "API ID та API Hash беруться на my.telegram.org у розділі API development tools. Код прийде в Telegram.",
    "Отправить код": "Надіслати код", "2. Подтвердить вход": "2. Підтвердити вхід", "Код из Telegram": "Код із Telegram", "Облачный пароль Telegram, если включен": "Хмарний пароль Telegram, якщо увімкнено", "Сохранить личный аккаунт": "Зберегти особистий акаунт",
    "Не удалось проверить Telegram userbot.": "Не вдалося перевірити Telegram userbot.", "сессия сохранена": "сесію збережено", "сессии пока нет": "сесії поки немає", "API настроен": "API налаштовано", "API еще не настроен": "API ще не налаштовано", "Сессия": "Сесія", "телефон": "телефон",
    "Не удалось связаться с сервером Telegram userbot.": "Не вдалося зв'язатися із сервером Telegram userbot.", "Введите API ID, API Hash и номер телефона.": "Введіть API ID, API Hash і номер телефону.", "Отправляю код в Telegram...": "Надсилаю код у Telegram...", "Не удалось отправить код.": "Не вдалося надіслати код.",
    "Код отправлен на": "Код надіслано на", "Введите его ниже.": "Введіть його нижче.", "Сначала отправьте код.": "Спочатку надішліть код.", "Проверяю код...": "Перевіряю код...", "Telegram просит облачный пароль. Введите его и нажмите кнопку еще раз.": "Telegram просить хмарний пароль. Введіть його і натисніть кнопку ще раз.", "Не удалось подтвердить код.": "Не вдалося підтвердити код.", "Готово. Сессия сохранена для": "Готово. Сесію збережено для"
  });
  Object.assign(dict.pl, {
    "Задания": "Zadania", "Сотрудники": "Pracownicy", "Клиенты": "Klienci", "Выполненные задания": "Wykonane zadania", "Расчеты сотрудников": "Rozliczenia pracowników", "Расчеты клиентов": "Rozliczenia klientów", "Реклама Telegram": "Reklama Telegram", "Реклама Facebook": "Reklama Facebook", "Настройки": "Ustawienia",
    "Вход Telegram userbot": "Logowanie Telegram userbot", "Состояние": "Status", "Проверяю подключение Telegram.": "Sprawdzam połączenie Telegram.", "Обновить состояние": "Odśwież status",
    "1. Отправить код": "1. Wyślij kod", "Номер телефона Telegram": "Numer telefonu Telegram", "Имя сессии": "Nazwa sesji", "API ID и API Hash берутся на my.telegram.org в разделе API development tools. Код придет в Telegram.": "API ID i API Hash bierze się z my.telegram.org w sekcji API development tools. Kod przyjdzie w Telegramie.",
    "Отправить код": "Wyślij kod", "2. Подтвердить вход": "2. Potwierdź logowanie", "Код из Telegram": "Kod z Telegrama", "Облачный пароль Telegram, если включен": "Hasło chmurowe Telegram, jeśli jest włączone", "Сохранить личный аккаунт": "Zapisz konto osobiste",
    "Не удалось проверить Telegram userbot.": "Nie udało się sprawdzić Telegram userbot.", "сессия сохранена": "sesja zapisana", "сессии пока нет": "brak sesji", "API настроен": "API skonfigurowane", "API еще не настроен": "API nie jest jeszcze skonfigurowane", "Сессия": "Sesja", "телефон": "telefon",
    "Не удалось связаться с сервером Telegram userbot.": "Nie udało się połączyć z serwerem Telegram userbot.", "Введите API ID, API Hash и номер телефона.": "Wpisz API ID, API Hash i numer telefonu.", "Отправляю код в Telegram...": "Wysyłam kod do Telegrama...", "Не удалось отправить код.": "Nie udało się wysłać kodu.",
    "Код отправлен на": "Kod wysłano na", "Введите его ниже.": "Wpisz go poniżej.", "Сначала отправьте код.": "Najpierw wyślij kod.", "Проверяю код...": "Sprawdzam kod...", "Telegram просит облачный пароль. Введите его и нажмите кнопку еще раз.": "Telegram prosi o hasło chmurowe. Wpisz je i naciśnij przycisk jeszcze raz.", "Не удалось подтвердить код.": "Nie udało się potwierdzić kodu.", "Готово. Сессия сохранена для": "Gotowe. Sesja zapisana dla"
  });
  function asLegacyMojibake(value) {
    try {
      return Array.from(new TextEncoder().encode(value), byte => String.fromCharCode(byte)).join("");
    } catch (error) {
      return value;
    }
  }
  Object.keys(dict).forEach(lang => {
    Object.entries({ ...dict[lang] }).forEach(([key, value]) => {
      const legacyKey = asLegacyMojibake(key);
      if (legacyKey && legacyKey !== key && !dict[lang][legacyKey]) dict[lang][legacyKey] = value;
    });
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
  <title>Задания</title>
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
    <h1 data-i18n="serverTitle">Задания</h1>
    <nav><a href="/server">Задания</a> <a href="/users">Сотрудники</a> <a href="/clients">Клиенты</a> <a href="/completed">Выполненные задания</a> <a href="/calculations">Расчеты сотрудников</a> <a href="/client-calculations">Расчеты клиентов</a> <a href="/telegram-ads">Реклама Telegram</a> <a href="/facebook-ads">Реклама Facebook</a> <a href="/telegram-login">Telegram userbot</a> <a href="/settings">Настройки</a></nav>
  </header>
  <main>
    <form id="form">
      <div class="addressRow">
        <input id="city" placeholder="Город" required>
        <input id="postalCode" placeholder="Код">
        <input id="street" placeholder="Улица" required>
        <input id="house" placeholder="Дом">
        <input id="apartment" placeholder="Квартира">
      </div>
      <div class="mainTaskRow">
        <textarea id="title" data-placeholder="taskTitle" placeholder="Название&#10;Задание" required></textarea>
        <input id="description" data-placeholder="description" placeholder="Описание">
        <textarea id="phone" placeholder="Номер&#10;Телефона" inputmode="tel"></textarea>
        <input id="price" data-placeholder="price" placeholder="Цена" inputmode="decimal">
        <select id="paymentMethod">
          <option value="cash" data-payment-label="cash">Наличные</option>
          <option value="card" data-payment-label="fromReserve">Из резерва</option>
        </select>
        <button data-i18n="add">Добавить</button>
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
      ru: { serverTitle: "Задания", openUsers: "Сотрудники", completedTasks: "Выполненные задания", calculations: "Расчеты", changePassword: "Изменить пароль", oldPassword: "Старый пароль", oldPasswordRepeat: "Повторите старый пароль", newPassword: "Введите новый пароль", passwordChanged: "Пароль изменен", changePasswordError: "Не удалось изменить пароль: ", confirmPassword: "Пароль подтверждения", taskTitle: "Название задания", description: "Описание", city: "Город", postalCode: "Код", street: "Улица", house: "Дом", apartment: "Квартира", address: "Адрес", price: "Цена", add: "Добавить", employee: "сотрудник", delete: "Удалить", restart: "Начать заново", confirmDelete: "Удалить это задание?", resetError: "Не удалось вернуть задание: ", deleteError: "Не удалось удалить задание: ", createdAt: "Создано", acceptedAt: "Принято", completedAt: "Выполнено", new: "Новое", accepted: "Принято", declined: "Отклонено", completed: "Выполнено", refused: "Отказался", payment: "Оплата", source: "Источник", dispatcher: "Диспетчер", client: "Клиент", cash: "Наличные", fromReserve: "Из резерва", save: "Сохранить", cancel: "Отмена", phone: "Номер телефона" },
      en: { serverTitle: "Tasks", openUsers: "Employees", completedTasks: "Completed tasks", calculations: "Calculations", changePassword: "Change password", oldPassword: "Old password", oldPasswordRepeat: "Repeat old password", newPassword: "Enter new password", passwordChanged: "Password changed", changePasswordError: "Could not change password: ", confirmPassword: "Confirmation password", taskTitle: "Task name", description: "Description", city: "City", postalCode: "Postal code", street: "Street", house: "House", apartment: "Apartment", address: "Address", price: "Price", add: "Add", employee: "employee", delete: "Delete", restart: "Start again", confirmDelete: "Delete this task?", resetError: "Could not return task: ", deleteError: "Could not delete task: ", createdAt: "Created", acceptedAt: "Accepted", completedAt: "Completed", new: "New", accepted: "Accepted", declined: "Declined", completed: "Completed", refused: "Refused", payment: "Payment", source: "Source", acceptedBy: "Accepted by", dispatcher: "Dispatcher", client: "Client", cash: "Cash", fromReserve: "From reserve", save: "Save", cancel: "Cancel", phone: "Phone number" },
      uk: { serverTitle: "Завдання", openUsers: "Співробітники", completedTasks: "Виконані завдання", calculations: "Розрахунки", changePassword: "Змінити пароль", oldPassword: "Старий пароль", oldPasswordRepeat: "Повторіть старий пароль", newPassword: "Введіть новий пароль", passwordChanged: "Пароль змінено", changePasswordError: "Не вдалося змінити пароль: ", confirmPassword: "Пароль підтвердження", taskTitle: "Назва завдання", description: "Опис", city: "Місто", postalCode: "Код", street: "Вулиця", house: "Будинок", apartment: "Квартира", address: "Адреса", price: "Ціна", add: "Додати", employee: "співробітник", delete: "Видалити", restart: "Почати заново", confirmDelete: "Видалити це завдання?", resetError: "Не вдалося повернути завдання: ", deleteError: "Не вдалося видалити завдання: ", createdAt: "Створено", acceptedAt: "Прийнято", completedAt: "Виконано", new: "Нове", accepted: "Прийнято", declined: "Відхилено", completed: "Виконано", refused: "Відмовився", payment: "Оплата", source: "Джерело", dispatcher: "Диспетчер", client: "Клієнт", cash: "Готівка", fromReserve: "З резерву", save: "Зберегти", cancel: "Скасувати", phone: "Номер телефону" },
      pl: { serverTitle: "Zadania", openUsers: "Pracownicy", completedTasks: "Wykonane zadania", calculations: "Rozliczenia", changePassword: "Zmień hasło", oldPassword: "Stare hasło", oldPasswordRepeat: "Powtórz stare hasło", newPassword: "Wpisz nowe hasło", passwordChanged: "Hasło zmienione", changePasswordError: "Nie udało się zmienić hasła: ", confirmPassword: "Hasło potwierdzenia", taskTitle: "Nazwa zadania", description: "Opis", city: "Miasto", postalCode: "Kod", street: "Ulica", house: "Dom", apartment: "Mieszkanie", address: "Adres", price: "Cena", add: "Dodaj", employee: "pracownik", delete: "Usuń", restart: "Zacznij od nowa", confirmDelete: "Usunąć to zadanie?", resetError: "Nie udało się przywrócić zadania: ", deleteError: "Nie udało się usunąć zadania: ", createdAt: "Utworzono", acceptedAt: "Przyjęto", completedAt: "Wykonano", new: "Nowe", accepted: "Przyjęte", declined: "Odrzucone", completed: "Wykonane", refused: "Odmówił", payment: "Płatność", source: "Źródło", dispatcher: "Dyspozytor", client: "Klient", cash: "Gotówka", fromReserve: "Z rezerwy", save: "Zapisz", cancel: "Anuluj", phone: "Numer telefonu" }
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
      title.placeholder = language === "ru" ? "Название\nЗадание" : texts[language].taskTitle;
      phone.placeholder = language === "ru" ? "Номер\nТелефона" : "Phone";
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
      const oldPassword = prompt(texts[language].oldPassword || "Старый пароль");
      if (!oldPassword) return;
      const oldPasswordRepeat = prompt(texts[language].oldPasswordRepeat || "Повторите старый пароль");
      if (!oldPasswordRepeat) return;
      const newPassword = prompt(texts[language].newAdminPassword || "Введите новый пароль");
      if (!newPassword) return;
      const res = await fetch("/api/admin/change-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ oldPassword, oldPasswordRepeat, newPassword })
      });
      if (!res.ok) {
        const data = await res.json();
        alert((texts[language].changePasswordError || "Не удалось изменить пароль: ") + data.error);
        return;
      }
      adminPassword = newPassword;
      sessionStorage.setItem("adminPassword", adminPassword);
      alert(texts[language].passwordChanged || "Пароль изменен");
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
          <p>${t.phone ? "<strong>Телефон:</strong> <a href=\"tel:" + phoneHref(t.phone) + "\">" + escapeHtml(t.phone) + "</a>" : ""}</p>
          <p>${t.address ? "<strong>" + texts[language].address + ":</strong> " + escapeHtml(t.address) : ""}</p>
          <p>${appSettings.showPrices && Number(t.price) ? "<strong>" + texts[language].price + ":</strong> " + formatMoney(t.price) : ""}</p>
          <p><strong>${texts[language].payment || "Оплата"}:</strong> ${paymentMethodName(t.paymentMethod)}</p>
          <p><strong>${texts[language].source || "Источник"}:</strong> ${taskSource(t)}</p>
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
        const name = task.clientName || task.sourceName || texts[language].client || "Клиент";
        const login = loginWithoutPlus(task.clientLogin || "");
        return escapeHtml(login ? name + ", " + login : name);
      }
      return texts[language].dispatcher || "Диспетчер";
    }
    function taskDatePanel(task) {
      const rows = [];
      if (task.createdAt) rows.push([texts[language].createdAt || "Создано", task.createdAt]);
      if (task.acceptedAt) rows.push([texts[language].acceptedAt || "Принято", task.acceptedAt]);
      if (task.completedAt) rows.push([texts[language].completedAt || "Выполнено", task.completedAt]);
      if (!rows.length) return "";
      return `<div class="taskDates">${rows.map(row => `<span><strong>${row[0]}:</strong> ${formatCompactDate(row[1])}</span>`).join("")}</div>`;
    }
    function paymentMethodName(method) {
      return method === "cash" ? (texts[language].cash || "Наличные") : (texts[language].fromReserve || "Из резерва");
    }
    function assignedEmployeeName(task) {
      const parts = [task.assignedToName, task.assignedToLogin].filter(value => value && String(value).trim());
      return parts.length ? escapeHtml(parts.join(" ")) : "";
    }
    function acceptedEmployeeLine(task) {
      const name = assignedEmployeeName(task);
      const labels = { ru: "Кем принято", en: "Accepted by", uk: "Ким прийнято", pl: "Przyjete przez" };
      return name ? `<p><strong>${texts[language].acceptedBy || labels[language] || labels.ru}:</strong> ${name}</p>` : "";
    }
    function clientSourceOptions(task) {
      const currentId = task.clientId || "";
      return `
        <option value="" ${currentId ? "" : "selected"}>${texts[language].dispatcher || "Диспетчер"}</option>
        ${clientOptions.map(client => `
          <option value="${client.id}" ${String(currentId) === String(client.id) ? "selected" : ""}>
            ${escapeHtml(client.displayName || client.login || ("Клиент #" + client.id))}
          </option>
        `).join("")}
      `;
    }
    function editButton(task) {
      if (!task.editable) {
        return "";
      }
      return `<button class="secondary" type="button" onclick="toggleTaskEdit(${task.id})">Редактировать</button>`;
    }
    function completeButton(task) {
      if (!task.editable) {
        return "";
      }
      return `<button class="restart" type="button" onclick="completeTask(${task.id})">Выполнено</button>`;
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
          <input name="phone" value="${escapeAttr(task.originalPhone || task.phone || "")}" placeholder="${texts[language].phone || "Номер телефона"}" inputmode="tel">
          <input name="price" value="${escapeAttr(task.price || "")}" placeholder="${texts[language].price}" inputmode="decimal">
          <select name="paymentMethod">
            <option value="card" ${(task.paymentMethod || "card") === "card" ? "selected" : ""}>${texts[language].fromReserve || "Из резерва"}</option>
            <option value="cash" ${task.paymentMethod === "cash" ? "selected" : ""}>${texts[language].cash || "Наличные"}</option>
          </select>
          <select class="clientSourceSelect" name="clientId" aria-label="${texts[language].client || "Клиент"}">
            ${clientSourceOptions(task)}
          </select>
          <div class="editTaskActions">
            <button type="submit">${texts[language].save || "Сохранить"}</button>
            <button class="secondary" type="button" onclick="toggleTaskEdit(${task.id})">${texts[language].cancel || "Отмена"}</button>
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
        alert("Не удалось сохранить задание: " + data.error);
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
          ? "Задание нельзя выполнить: его еще не принял сотрудник."
          : "Не удалось отметить задание выполненным: " + data.error;
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
      return item && item.calculated ? "Оплачено" : "Не оплачено";
    }
    function calculationClass(item) {
      return item && item.calculated ? " calculated" : "";
    }
    function calculationStatus(item) {
      return item && item.calculated ? "Рассчитано" : "Не рассчитано";
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
    "<title>Задания</title>",
    "<title>Выполненные задания</title>",
).replace(
    '<h1 data-i18n="serverTitle">Задания</h1>',
    '<h1 data-i18n="serverTitle">Выполненные задания</h1>',
).replace(
    '<form id="form">',
    '<form id="form" style="display:none">',
).replace(
    'ru: { serverTitle: "Задания",',
    'ru: { serverTitle: "Выполненные задания",',
).replace(
    'en: { serverTitle: "Tasks",',
    'en: { serverTitle: "Completed tasks",',
).replace(
    'uk: { serverTitle: "Завдання",',
    'uk: { serverTitle: "Виконані завдання",',
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
  <title>Расчеты сотрудников</title>
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
    <h1>Расчеты сотрудников</h1>
    <nav><a href="/server">Задания</a> <a href="/users">Сотрудники</a> <a href="/clients">Клиенты</a> <a href="/completed">Выполненные задания</a> <a href="/calculations">Расчеты сотрудников</a> <a href="/client-calculations">Расчеты клиентов</a> <a href="/telegram-ads">Реклама Telegram</a> <a href="/facebook-ads">Реклама Facebook</a> <a href="/telegram-login">Telegram userbot</a> <a href="/settings">Настройки</a></nav>
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
      return item && item.calculated ? "Оплачено" : "Не оплачено";
    }
    function calculationClass(item) {
      return item && item.calculated ? " calculated" : "";
    }
    function taskStatusName(status) {
      const names = {
        completed: "Выполнено",
        refused: "Отказался",
        accepted: "Принято",
        declined: "Отклонено",
        new: "Новое"
      };
      return names[status] || status || "";
    }
    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    }
    async function loadSettlements() {
      const res = await fetch("/api/admin/settlements", { headers: adminHeaders() });
      if (!res.ok) {
        settlements.innerHTML = "<p>Не удалось загрузить расчеты.</p>";
        return;
      }
      const data = await res.json();
      const history = (data.settlements || []).map(item => `
        <article class="item${calculationClass(item)}">
          <h3>#${item.id} ${escapeHtml(item.displayName)} В· ${formatDate(item.createdAt)}</h3>
          <p class="status">Статус расчета: ${calculationStatus(item)}</p>
          <p class="meta">Выполнено: ${item.counts.completed || 0} · Отказался: ${item.counts.refused || 0} · Всего назначено: ${item.counts.all || 0}</p>
          <p>${appSettings.showPrices ? "<strong>Сумма к выплате:</strong> " + formatMoney(item.totals.payoutPrice ?? item.totals.completedPrice ?? 0) : ""}</p>
          ${renderDeductions(item)}
          ${renderSettlementReport(item)}
          <div class="actions">${calculateSettlementButton(item)}<button class="danger" type="button" onclick="deleteSettlement(${item.id})">Удалить расчет</button></div>
        </article>
      `).join("");
      settlements.innerHTML = history || "<p class=\"meta\">Созданных расчетов пока нет. Новый расчет создается в разделе «Сотрудники» кнопкой «Рассчитать».</p>";
    }
    function calculateSettlementButton(item) {
      if (item.calculated) {
        return "";
      }
      return `<button class="success" type="button" onclick="calculateSettlement(${item.id})">Оплатить</button>`;
    }
    function renderDeductions(item) {
      if (!appSettings.showPrices) {
        return "";
      }
      return `
        <p class="meta">Выполненные работы: ${formatMoney(item.totals.completedPrice || 0)} · Удержание ${item.totals.completedFeePercent || 0}%: ${formatMoney(item.totals.completedFee || 0)}</p>
        <p class="meta">Отказанные работы: ${formatMoney(item.totals.refusedPrice || 0)} · Удержание ${item.totals.refusedFeePercent || 0}%: ${formatMoney(item.totals.refusedFee || 0)}</p>
      `;
    }
    function renderSettlementReport(item) {
      return `
        <details>
          <summary>Полный отчет по расчету</summary>
          ${renderTaskSection("Выполненные работы", item.completed || [], "completed")}
          ${renderTaskSection("Работы с отказом", item.refused || [], "refused")}
          ${renderTaskSection("Остальные работы", item.other || [], "other")}
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
        <h4>Резерв в расчете</h4>
        <p class="meta">Резерв: ${formatReserve(totals.reservePrice || 0)} · В резерв: ${formatReserve(totals.completedToReserve || 0)} · Из резерва в выплату: ${formatReserve(totals.reserveToCompleted || 0)} · Удержано из резерва: ${formatReserve(reserveDeductions)}</p>
      `;
    }
    function reserveEventName(kind) {
      if (kind === "to_reserve") return "Из суммы к выплате в резерв";
      if (kind === "from_reserve") return "Из резерва в выплату";
      if (kind === "top_up") return "Пополнение резерва";
      if (kind === "refused_fee_from_reserve") return "Удержание за отказ из резерва";
      if (kind === "cash_completed_fee_from_reserve") return "Удержание за наличные из резерва";
      return kind;
    }
    function renderReserveEvents(events) {
      if (!events || !events.length) {
        return `
          <h4>Операции резерва</h4>
          <p class="meta">Нет операций резерва.</p>
        `;
      }
      return `
        <h4>Операции резерва</h4>
        ${events.map(event => `<p class="meta">${formatDate(event.createdAt)} В· ${reserveEventName(event.kind)} В· ${formatReserve(event.absoluteAmount ?? Math.abs(event.amount || 0))}</p>`).join("")}
      `;
    }
    function renderTaskSection(title, tasks, type) {
      if (!tasks.length) {
        return `<h4>${title}</h4><p class="meta">Нет записей.</p>`;
      }
      return `
        <h4>${title}</h4>
        ${tasks.map(task => `
          <article class="reportTask ${type}">
            <strong>#${task.id}</strong>
            <p><strong>${escapeHtml(task.title)}</strong></p>
            <p>${escapeHtml(task.description || "")}</p>
            <p>${task.phone ? "<strong>Телефон:</strong> <a href=\"tel:" + phoneHref(task.phone) + "\">" + escapeHtml(task.phone) + "</a>" : ""}</p>
            <p>${task.address ? "<strong>Адрес:</strong> " + escapeHtml(task.address) : ""}</p>
            <p>${appSettings.showPrices ? "<strong>Цена:</strong> " + formatMoney(task.price || 0) : ""}</p>
            <p><strong>Оплата:</strong> ${paymentMethodName(task.paymentMethod)}</p>
            <div class="meta">Статус: ${taskStatusName(task.status)} · ${taskDatesMeta(task)}</div>
          </article>
        `).join("")}
      `;
    }
    function phoneHref(value) {
      return String(value).replace(/[^\d+]/g, "");
    }
    function paymentMethodName(method) {
      return method === "cash" ? "Наличные" : "Из резерва";
    }
    function taskDatesMeta(task) {
      const rows = [];
      if (task.createdAt) rows.push("Создано: " + formatDate(task.createdAt));
      if (task.acceptedAt) rows.push("Принято: " + formatDate(task.acceptedAt));
      if (task.completedAt) rows.push("Выполнено: " + formatDate(task.completedAt));
      return rows.length ? rows.join(" · ") : "нет даты";
    }
    async function deleteSettlement(id) {
      const password = prompt("Пароль подтверждения");
      if (!password) {
        return;
      }
      const res = await fetch("/api/admin/settlements/" + id + "/delete", {
        method: "POST",
        headers: adminHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ password })
      });
      if (!res.ok) {
        alert("Не удалось удалить расчет.");
        return;
      }
      loadSettlements();
    }
    async function calculateSettlement(id) {
      const password = prompt("Пароль подтверждения");
      if (!password) {
        return;
      }
      const res = await fetch("/api/admin/settlements/" + id + "/calculate", {
        method: "POST",
        headers: adminHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ password })
      });
      if (!res.ok) {
        alert("Не удалось оплатить.");
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
  <title>Сотрудники</title>
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
    <h1 data-i18n="employees">Сотрудники</h1>
    <nav><a href="/server">Задания</a> <a href="/users">Сотрудники</a> <a href="/clients">Клиенты</a> <a href="/completed">Выполненные задания</a> <a href="/calculations">Расчеты сотрудников</a> <a href="/client-calculations">Расчеты клиентов</a> <a href="/telegram-ads">Реклама Telegram</a> <a href="/facebook-ads">Реклама Facebook</a> <a href="/telegram-login">Telegram userbot</a> <a href="/settings">Настройки</a></nav>
  </header>
  <main>
    <button class="secondary" type="button" onclick="refreshUsersList()" data-i18n="refreshList">Обновить список</button>
    <form id="userForm">
      <input id="userDisplayName" data-placeholder="employeeName" placeholder="Имя сотрудника" required>
      <input id="userLogin" data-placeholder="login" placeholder="Логин" required>
      <input id="userPassword" data-placeholder="password" placeholder="Пароль" required>
      <button data-i18n="add">Добавить</button>
    </form>
    <button class="secondary" type="button" onclick="settleAllUsers()" data-i18n="calculateAll">Рассчитать всех</button>
    <section id="users"></section>
  </main>
  <script>
    const users = document.querySelector("#users");
    let editingUserId = null;
    let openReportId = null;
    let language = localStorage.getItem("language") || "ru";
    let appSettings = { currency: "PLN", reserveUnit: "credits", showPrices: true };
    const texts = {
      ru: { employees: "Сотрудники", changePassword: "Изменить пароль", oldPassword: "Старый пароль", oldPasswordRepeat: "Повторите старый пароль", newAdminPassword: "Введите новый пароль", passwordChanged: "Пароль изменен", changePasswordError: "Не удалось изменить пароль: ", backHome: "Вернуться на главный экран", refreshList: "Обновить список", employeeName: "Имя сотрудника", login: "Логин", password: "Пароль", newPassword: "Новый пароль, если нужно", add: "Добавить", tasks: "Заданий", report: "Отчет", calculate: "Рассчитать", edit: "Редактировать", save: "Сохранить", deleteEmployee: "Удалить сотрудника", loadingReport: "Загружаю отчет...", reportError: "Не удалось загрузить отчет: ", saveError: "Не удалось сохранить сотрудника: ", addError: "Не удалось добавить сотрудника: ", settleError: "Не удалось выполнить расчет: ", deleteUserError: "Не удалось удалить сотрудника: ", confirmPassword: "Пароль подтверждения", detailedReport: "Подробный отчет", history: "История расчетов", settlement: "Расчет", completed: "Выполнено", refused: "Отказался", accepted: "В работе", allAssigned: "Всего назначено", completedSum: "Сумма выполненных", refusedSum: "Сумма отказанных", reserve: "Резерв", payout: "Сумма к выплате", completedWorks: "Выполненные работы", refusedWorks: "Отказанные работы", otherWorks: "В работе", noRecords: "Нет записей.", address: "Адрес", price: "Цена", status: "Статус", created: "Создано", changed: "Изменено", noDate: "нет даты", statusCompleted: "Выполнено", statusRefused: "Отказался", statusAccepted: "Принято", statusDeclined: "Отклонено", statusNew: "Новое" },
      en: { employees: "Employees", backHome: "Back to the main screen", refreshList: "Refresh list", employeeName: "Employee name", login: "Login", password: "Password", newPassword: "New password, if needed", add: "Add", tasks: "Tasks", report: "Report", calculate: "Calculate", edit: "Edit", save: "Save", deleteEmployee: "Delete employee", loadingReport: "Loading report...", reportError: "Could not load report: ", saveError: "Could not save employee: ", addError: "Could not add employee: ", settleError: "Could not calculate: ", deleteUserError: "Could not delete employee: ", confirmPassword: "Confirmation password", detailedReport: "Detailed report", history: "Calculation history", settlement: "Calculation", completed: "Completed", refused: "Refused", accepted: "In progress", allAssigned: "Total assigned", completedSum: "Completed total", refusedSum: "Refused total", reserve: "Reserve", payout: "Amount to pay", completedWorks: "Completed jobs", refusedWorks: "Refused jobs", otherWorks: "Other assigned jobs", noRecords: "No records.", address: "Address", price: "Price", status: "Status", created: "Created", changed: "Changed", noDate: "no date", statusCompleted: "Completed", statusRefused: "Refused", statusAccepted: "Accepted", statusDeclined: "Declined", statusNew: "New" },
      uk: { employees: "Співробітники", backHome: "Повернутися на головний екран", refreshList: "Оновити список", employeeName: "Ім'я співробітника", login: "Логін", password: "Пароль", newPassword: "Новий пароль, якщо потрібно", add: "Додати", tasks: "Завдань", report: "Звіт", calculate: "Розрахувати", edit: "Редагувати", save: "Зберегти", deleteEmployee: "Видалити співробітника", loadingReport: "Завантажую звіт...", reportError: "Не вдалося завантажити звіт: ", saveError: "Не вдалося зберегти співробітника: ", addError: "Не вдалося додати співробітника: ", settleError: "Не вдалося виконати розрахунок: ", deleteUserError: "Не вдалося видалити співробітника: ", confirmPassword: "Пароль підтвердження", detailedReport: "Докладний звіт", history: "Історія розрахунків", settlement: "Розрахунок", completed: "Виконано", refused: "Відмовився", accepted: "У роботі", allAssigned: "Усього призначено", completedSum: "Сума виконаних", refusedSum: "Сума відмов", reserve: "Резерв", payout: "Сума до виплати", completedWorks: "Виконані роботи", refusedWorks: "Відмовлені роботи", otherWorks: "Інші призначені роботи", noRecords: "Записів немає.", address: "Адреса", price: "Ціна", status: "Статус", created: "Створено", changed: "Змінено", noDate: "немає дати", statusCompleted: "Виконано", statusRefused: "Відмовився", statusAccepted: "Прийнято", statusDeclined: "Відхилено", statusNew: "Нове" },
      pl: { employees: "Pracownicy", backHome: "Wróć do ekranu głównego", refreshList: "Odśwież listę", employeeName: "Imię pracownika", login: "Login", password: "Hasło", newPassword: "Nowe hasło, jeśli potrzebne", add: "Dodaj", tasks: "Zadań", report: "Raport", calculate: "Rozlicz", edit: "Edytuj", save: "Zapisz", deleteEmployee: "Usuń pracownika", loadingReport: "Ładuję raport...", reportError: "Nie udało się załadować raportu: ", saveError: "Nie udało się zapisać pracownika: ", addError: "Nie udało się dodać pracownika: ", settleError: "Nie udało się rozliczyć: ", deleteUserError: "Nie udało się usunąć pracownika: ", confirmPassword: "Hasło potwierdzenia", detailedReport: "Szczegółowy raport", history: "Historia rozliczeń", settlement: "Rozliczenie", completed: "Wykonane", refused: "Odmówione", accepted: "W trakcie", allAssigned: "Łącznie przypisane", completedSum: "Suma wykonanych", refusedSum: "Suma odmówionych", reserve: "Rezerwa", payout: "Kwota do wypłaty", completedWorks: "Wykonane prace", refusedWorks: "Odmówione prace", otherWorks: "Inne przypisane prace", noRecords: "Brak wpisów.", address: "Adres", price: "Cena", status: "Status", created: "Utworzono", changed: "Zmieniono", noDate: "brak daty", statusCompleted: "Wykonane", statusRefused: "Odmówione", statusAccepted: "Przyjęte", statusDeclined: "Odrzucone", statusNew: "Nowe" }
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
        calculateAll: { ru: "Рассчитать всех", en: "Calculate all", uk: "Розрахувати всіх", pl: "Rozlicz wszystkich" }
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
      const oldPassword = prompt(texts[language].oldPassword || "Старый пароль");
      if (!oldPassword) return;
      const oldPasswordRepeat = prompt(texts[language].oldPasswordRepeat || "Повторите старый пароль");
      if (!oldPasswordRepeat) return;
      const newPassword = prompt(texts[language].newAdminPassword || "Введите новый пароль");
      if (!newPassword) return;
      const res = await fetch("/api/admin/change-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ oldPassword, oldPasswordRepeat, newPassword })
      });
      if (!res.ok) {
        const data = await res.json();
        alert((texts[language].changePasswordError || "Не удалось изменить пароль: ") + data.error);
        return;
      }
      adminPassword = newPassword;
      sessionStorage.setItem("adminPassword", adminPassword);
      alert(texts[language].passwordChanged || "Пароль изменен");
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
          <h4>Пополнить резерв</h4>
          <form onsubmit="reserveTopUp(event, ${userId})">
            <input name="amount" type="number" min="0.01" step="0.01" placeholder="Сумма" required>
            <button type="submit">Пополнить</button>
          </form>
        </section>
      `;
    }
    function reserveEventName(kind) {
      if (kind === "to_reserve") return "Из суммы к выплате в резерв";
      if (kind === "from_reserve") return "Из резерва в выплату";
      if (kind === "top_up") return "Пополнение резерва";
      if (kind === "refused_fee_from_reserve") return "Удержание за отказ из резерва";
      if (kind === "cash_completed_fee_from_reserve") return "Удержание за наличные из резерва";
      return kind;
    }
    function renderReserveEvents(events) {
      if (!events.length) {
        return "";
      }
      return `
        <section class="reportSection">
          <h4>Операции резерва</h4>
          ${events.map(event => `
            <p class="meta">${formatDate(event.createdAt)} В· ${reserveEventName(event.kind)} В· ${formatReserve(event.absoluteAmount ?? Math.abs(event.amount || 0))}</p>
          `).join("")}
        </section>
      `;
    }
    async function reserveTransfer(userId, action) {
      const amount = prompt("Сумма");
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
        alert("Не удалось изменить резерв: " + (data.error || res.status));
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
        alert("Не удалось пополнить резерв: " + (data.error || res.status));
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
              <p>${task.phone ? "<strong>Телефон:</strong> <a href=\"tel:" + phoneHref(task.phone) + "\">" + escapeHtml(task.phone) + "</a>" : ""}</p>
              <p>${task.address ? "<strong>" + texts[language].address + ":</strong> " + escapeHtml(task.address) : ""}</p>
              <p>${appSettings.showPrices ? "<strong>" + texts[language].price + ":</strong> " + formatMoney(task.price) : ""}</p>
              <p><strong>Оплата:</strong> ${paymentMethodName(task.paymentMethod)}</p>
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
      return item && item.calculated ? "Оплачено" : "Не оплачено";
    }
    function calculationClass(item) {
      return item && item.calculated ? " calculated" : "";
    }
    function calculateClientSettlementButton(item) {
      if (item.calculated) {
        return "";
      }
      return `<button class="success" type="button" onclick="calculateClientSettlement(${item.id})">Оплачено</button>`;
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
      return method === "cash" ? "Наличные" : "Из резерва";
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
  <title>Клиенты</title>
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
    <h1>Клиенты</h1>
    <nav><a href="/server">Задания</a> <a href="/users">Сотрудники</a> <a href="/clients">Клиенты</a> <a href="/completed">Выполненные задания</a> <a href="/calculations">Расчеты сотрудников</a> <a href="/client-calculations">Расчеты клиентов</a> <a href="/telegram-ads">Реклама Telegram</a> <a href="/facebook-ads">Реклама Facebook</a> <a href="/telegram-login">Telegram userbot</a> <a href="/settings">Настройки</a></nav>
  </header>
  <main>
    <button class="secondary" type="button" onclick="refreshClientsList()">Обновить список</button>
    <form id="clientForm">
      <input id="clientDisplayName" placeholder="Имя клиента" required>
      <input id="clientLogin" placeholder="Номер телефона" required>
      <input id="clientPassword" placeholder="Пароль" required>
      <button>Добавить</button>
    </form>
    <button class="secondary" type="button" onclick="settleAllClients()" data-i18n="calculateAll">Рассчитать всех</button>
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
        completed: "Выполнено", refused: "Отказался", accepted: "Принято", declined: "Отклонено", new: "Новое",
        paid: "Оплачено", unpaid: "Не оплачено", settlementStatus: "Статус расчета", completedCount: "Выполнено",
        refusedCount: "Отказался", activeCount: "В работе", totalTasks: "Всего заданий", settlementTotal: "Сумма расчета",
        fullReport: "Полный отчет по расчету", completedWorks: "Выполненные работы", activeWorks: "Активные работы",
        newWorks: "Новые работы", refusedWorks: "Отказанные работы", otherWorks: "Остальные работы",
        reserveInSettlement: "Резерв в расчете", reserveBefore: "Резерв до расчета", reserveUsed: "Списано из резерва",
        reserveLeft: "Остаток резерва", amountDue: "Сумма к оплате", reserveOperations: "Операции резерва",
        noReserveOperations: "Нет операций резерва.", noRecords: "Нет записей.", phone: "Телефон", address: "Адрес",
        acceptedBy: "Кто принял", price: "Цена", payment: "Оплата", status: "Статус", cash: "Наличные",
        fromReserve: "Из резерва", created: "Создано", acceptedAt: "Принято", completedAt: "Выполнено",
        noDate: "нет даты", deleteSettlement: "Удалить расчет", noSettlements: "Созданных расчетов пока нет. Новый расчет создается в разделе «Клиенты» кнопкой «Рассчитать».",
        toReserve: "Из суммы к оплате в резерв", fromReserveToPay: "Из резерва в сумму к оплате",
        topUp: "Пополнение резерва", completedFromReserve: "Выполненные работы из резерва"
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
        completed: "Виконано", refused: "Відмовився", accepted: "Прийнято", declined: "Відхилено", new: "Нове",
        paid: "Оплачено", unpaid: "Не оплачено", settlementStatus: "Статус розрахунку", completedCount: "Виконано",
        refusedCount: "Відмовився", activeCount: "У роботі", totalTasks: "Усього завдань", settlementTotal: "Сума розрахунку",
        fullReport: "Повний звіт за розрахунком", completedWorks: "Виконані роботи", activeWorks: "Активні роботи",
        newWorks: "Нові роботи", refusedWorks: "Відмовлені роботи", otherWorks: "Інші роботи",
        reserveInSettlement: "Резерв у розрахунку", reserveBefore: "Резерв до розрахунку", reserveUsed: "Списано з резерву",
        reserveLeft: "Залишок резерву", amountDue: "Сума до оплати", reserveOperations: "Операції резерву",
        noReserveOperations: "Операцій резерву немає.", noRecords: "Записів немає.", phone: "Телефон", address: "Адреса",
        acceptedBy: "Ким прийнято", price: "Ціна", payment: "Оплата", status: "Статус", cash: "Готівка",
        fromReserve: "З резерву", created: "Створено", acceptedAt: "Прийнято", completedAt: "Виконано",
        noDate: "немає дати", deleteSettlement: "Видалити розрахунок", noSettlements: "Створених розрахунків поки немає. Новий розрахунок створюється в розділі «Клієнти» кнопкою «Розрахувати».",
        toReserve: "Із суми до оплати в резерв", fromReserveToPay: "З резерву в суму до оплати",
        topUp: "Поповнення резерву", completedFromReserve: "Виконані роботи з резерву"
      },
      pl: {
        completed: "Wykonane", refused: "Odmówione", accepted: "Przyjęte", declined: "Odrzucone", new: "Nowe",
        paid: "Opłacono", unpaid: "Nie opłacono", settlementStatus: "Status rozliczenia", completedCount: "Wykonane",
        refusedCount: "Odmówione", activeCount: "W trakcie", totalTasks: "Łącznie zadań", settlementTotal: "Suma rozliczenia",
        fullReport: "Pełny raport rozliczenia", completedWorks: "Wykonane prace", activeWorks: "Aktywne prace",
        newWorks: "Nowe prace", refusedWorks: "Odmówione prace", otherWorks: "Pozostałe prace",
        reserveInSettlement: "Rezerwa w rozliczeniu", reserveBefore: "Rezerwa przed rozliczeniem", reserveUsed: "Pobrano z rezerwy",
        reserveLeft: "Pozostała rezerwa", amountDue: "Kwota do zapłaty", reserveOperations: "Operacje rezerwy",
        noReserveOperations: "Brak operacji rezerwy.", noRecords: "Brak wpisów.", phone: "Telefon", address: "Adres",
        acceptedBy: "Kto przyjął", price: "Cena", payment: "Płatność", status: "Status", cash: "Gotówka",
        fromReserve: "Z rezerwy", created: "Utworzono", acceptedAt: "Przyjęto", completedAt: "Wykonano",
        noDate: "brak daty", deleteSettlement: "Usuń rozliczenie", noSettlements: "Nie ma jeszcze utworzonych rozliczeń klientów. Nowe rozliczenie utworzysz w sekcji „Klienci” przyciskiem „Rozlicz”.",
        toReserve: "Z kwoty do zapłaty do rezerwy", fromReserveToPay: "Z rezerwy do kwoty do zapłaty",
        topUp: "Doładowanie rezerwy", completedFromReserve: "Wykonane prace z rezerwy"
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
        if (data.error === "admin_unauthorized") return "Нужно заново ввести пароль администратора.";
        if (data.error === "login_name_required") return "Заполните имя клиента и логин.";
        if (data.error === "password_too_short") return "Пароль должен быть не короче 4 символов.";
        if (data.error === "login_already_exists") return "Такой логин уже используется.";
        if (data.error === "client_not_found") return "Клиент не найден.";
        return data.error || res.statusText;
      } catch (error) {
        return res.statusText || "Неизвестная ошибка";
      }
    }
    async function loadClients() {
      if (editingClientId !== null || openReportId !== null) return;
      const res = await fetch("/api/admin/clients", { headers: adminHeaders() });
      if (!res.ok) { clients.innerHTML = "<p>Не удалось загрузить клиентов.</p>"; return; }
      const data = await res.json();
      clients.innerHTML = data.clients.map(client => `
        <article class="client">
          <div class="clientHeader">
            <div>
              <strong>${escapeHtml(client.displayName)}</strong>
              <div class="meta">Телефон: ${escapeHtml(client.phone || client.login)} · Заданий: ${client.taskCount}</div>
            </div>
            <div class="actions">
              <div class="clientMoneyMini"><strong>${formatMoney(client.totalPrice || 0)}</strong><span>Сумма к оплате</span></div>
              <div class="clientMoneyMini"><strong>${formatReserve(client.reservePrice || 0)}</strong><span>Резерв</span></div>
              <button class="secondary" type="button" onclick="toggleClientReport(${client.id})">Отчет</button>
              <button class="secondary" type="button" onclick="settleClientFromList(${client.id})">Рассчитать</button>
              <button class="secondary" type="button" onclick="toggleEdit(${client.id})">Редактировать</button>
            </div>
          </div>
          <form class="editForm" id="edit-${client.id}" style="display:none" onsubmit="saveClient(event, ${client.id})">
            <input name="displayName" value="${escapeAttr(client.displayName)}" placeholder="Имя клиента" required>
            <input name="login" value="${escapeAttr(client.phone || client.login)}" placeholder="Номер телефона" required>
            <input name="password" value="" placeholder="Новый пароль, если нужно">
            <button>Сохранить</button>
            <button class="danger" type="button" onclick="deleteClient(${client.id})">Удалить клиента</button>
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
      panel.innerHTML = "<p class=\"meta\">Загружаю отчет...</p>";
      const res = await fetch("/api/admin/clients/" + id + "/report", { headers: adminHeaders() });
      if (!res.ok) {
        const data = await res.json();
        panel.innerHTML = "<p>Не удалось загрузить отчет: " + escapeHtml(data.error || res.status) + "</p>";
        return;
      }
      const report = await res.json();
      panel.innerHTML = renderClientListReport(report);
    }
    function renderClientListReport(report) {
      return `
        <h3>Подробный отчет: ${escapeHtml(report.client.displayName)}</h3>
        <div class="reportStats">
          <div class="statBox"><strong>${report.counts.completed || 0}</strong><span>Выполнено</span></div>
          <div class="statBox"><strong>${report.counts.refused || 0}</strong><span>Отказался</span></div>
          <div class="statBox"><strong>${report.counts.active || 0}</strong><span>В работе</span></div>
          <div class="statBox"><strong>${report.counts.all || 0}</strong><span>Всего заданий</span></div>
        </div>
        <div class="reportStats">
          ${appSettings.showPrices ? `<div class="statBox"><strong>${formatMoney(report.totals.completedPrice || 0)}</strong><span>Выполненные работы</span></div>` : ""}
          ${appSettings.showPrices ? `<div class="statBox"><strong>${formatMoney(report.totals.refusedPrice || 0)}</strong><span>Сумма отказанных</span></div>` : ""}
          ${appSettings.showPrices ? renderClientReserveBox(report) : ""}
          ${appSettings.showPrices ? `<div class="statBox"><strong>${formatMoney(report.totals.activePaymentDue || 0)}</strong><span>Сумма к оплате</span></div>` : ""}
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
            <button class="secondary" type="button" disabled title="Временно отключено">-</button>
            <span>${text("reserveInSettlement")}</span>
            <button class="secondary" type="button" disabled title="Временно отключено">+</button>
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
      const amount = prompt("Сумма");
      if (!amount) return;
      const password = prompt("Пароль подтверждения");
      if (!password) return;
      const res = await fetch("/api/admin/clients/" + clientId + "/reserve", {
        method: "POST",
        headers: adminHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ action, amount, password })
      });
      if (!res.ok) {
        const data = await res.json();
        alert("Не удалось изменить резерв: " + (data.error || res.status));
        return;
      }
      const data = await res.json();
      const panel = document.querySelector("#report-" + clientId);
      panel.innerHTML = renderClientListReport(data.report);
    }
    async function clientReserveTopUp(event, clientId) {
      event.preventDefault();
      const amount = event.target.amount.value;
      const password = prompt("Пароль подтверждения");
      if (!password) return;
      const res = await fetch("/api/admin/clients/" + clientId + "/reserve", {
        method: "POST",
        headers: adminHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ action: "top_up", amount, password })
      });
      if (!res.ok) {
        const data = await res.json();
        alert("Не удалось пополнить резерв: " + (data.error || res.status));
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
      const password = prompt("Пароль подтверждения");
      if (!password) return;
      const res = await fetch("/api/admin/clients/" + id + "/settle", {
        method: "POST",
        headers: adminHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ password })
      });
      if (!res.ok) {
        const data = await res.json();
        alert("Не удалось выполнить расчет: " + (data.error || res.status));
        return;
      }
      editingClientId = null;
      openReportId = null;
      loadClients();
    }
    async function settleAllClients() {
      const password = prompt("Пароль подтверждения");
      if (!password) return;
      const res = await fetch("/api/admin/clients-settle-all", {
        method: "POST",
        headers: adminHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ password })
      });
      if (!res.ok) {
        const data = await res.json();
        alert("Не удалось выполнить расчет: " + (data.error || res.status));
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
        alert("Не удалось сохранить клиента: " + message);
        return;
      }
      editingClientId = null;
      openReportId = null;
      loadClients();
    }
    async function deleteClient(id) {
      const password = prompt("Пароль подтверждения");
      if (!password) return;
      const res = await fetch("/api/admin/clients/" + id + "/delete", {
        method: "POST",
        headers: adminHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ password })
      });
      if (!res.ok) { alert("Не удалось удалить клиента."); return; }
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
      if (!res.ok) { alert("Не удалось добавить клиента."); return; }
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
  <title>Расчеты клиентов</title>
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
    <h1>Расчеты клиентов</h1>
    <nav><a href="/server">Задания</a> <a href="/users">Сотрудники</a> <a href="/clients">Клиенты</a> <a href="/completed">Выполненные задания</a> <a href="/calculations">Расчеты сотрудников</a> <a href="/client-calculations">Расчеты клиентов</a> <a href="/telegram-ads">Реклама Telegram</a> <a href="/facebook-ads">Реклама Facebook</a> <a href="/telegram-login">Telegram userbot</a> <a href="/settings">Настройки</a></nav>
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
      ru: { completed: "Выполнено", refused: "Отказался", accepted: "Принято", declined: "Отклонено", new: "Новое", paid: "Оплачено", unpaid: "Не оплачено", settlementStatus: "Статус расчета", completedCount: "Выполнено", refusedCount: "Отказался", activeCount: "В работе", totalTasks: "Всего заданий", settlementTotal: "Сумма расчета", fullReport: "Полный отчет по расчету", completedWorks: "Выполненные работы", activeWorks: "Активные работы", newWorks: "Новые работы", refusedWorks: "Отказанные работы", otherWorks: "Остальные работы", reserveInSettlement: "Резерв в расчете", reserveBefore: "Резерв до расчета", reserveUsed: "Списано из резерва", reserveLeft: "Остаток резерва", amountDue: "Сумма к оплате", reserveOperations: "Операции резерва", noReserveOperations: "Нет операций резерва.", noRecords: "Нет записей.", phone: "Телефон", address: "Адрес", acceptedBy: "Кто принял", price: "Цена", payment: "Оплата", status: "Статус", cash: "Наличные", fromReserve: "Из резерва", created: "Создано", acceptedAt: "Принято", completedAt: "Выполнено", noDate: "нет даты", deleteSettlement: "Удалить расчет", noSettlements: "Созданных расчетов пока нет. Новый расчет создается в разделе «Клиенты» кнопкой «Рассчитать».", toReserve: "Из суммы к оплате в резерв", fromReserveToPay: "Из резерва в сумму к оплате", topUp: "Пополнение резерва", completedFromReserve: "Выполненные работы из резерва" },
      en: { completed: "Completed", refused: "Refused", accepted: "Accepted", declined: "Declined", new: "New", paid: "Paid", unpaid: "Not paid", settlementStatus: "Payment status", completedCount: "Completed", refusedCount: "Refused", activeCount: "In progress", totalTasks: "Total tasks", settlementTotal: "Payment total", fullReport: "Full payment report", completedWorks: "Completed jobs", activeWorks: "Active jobs", newWorks: "New jobs", refusedWorks: "Refused jobs", otherWorks: "Other jobs", reserveInSettlement: "Reserve in payment", reserveBefore: "Reserve before payment", reserveUsed: "Written off from reserve", reserveLeft: "Reserve left", amountDue: "Amount to pay", reserveOperations: "Reserve operations", noReserveOperations: "No reserve operations.", noRecords: "No records.", phone: "Phone", address: "Address", acceptedBy: "Accepted by", price: "Price", payment: "Payment", status: "Status", cash: "Cash", fromReserve: "From reserve", created: "Created", acceptedAt: "Accepted", completedAt: "Completed", noDate: "no date", deleteSettlement: "Delete payment", noSettlements: "No client payments have been created yet. Create a new payment in Clients with the Calculate button.", toReserve: "From amount to pay to reserve", fromReserveToPay: "From reserve to amount to pay", topUp: "Reserve top-up", completedFromReserve: "Completed jobs from reserve" },
      uk: { completed: "Виконано", refused: "Відмовився", accepted: "Прийнято", declined: "Відхилено", new: "Нове", paid: "Оплачено", unpaid: "Не оплачено", settlementStatus: "Статус розрахунку", completedCount: "Виконано", refusedCount: "Відмовився", activeCount: "У роботі", totalTasks: "Усього завдань", settlementTotal: "Сума розрахунку", fullReport: "Повний звіт за розрахунком", completedWorks: "Виконані роботи", activeWorks: "Активні роботи", newWorks: "Нові роботи", refusedWorks: "Відмовлені роботи", otherWorks: "Інші роботи", reserveInSettlement: "Резерв у розрахунку", reserveBefore: "Резерв до розрахунку", reserveUsed: "Списано з резерву", reserveLeft: "Залишок резерву", amountDue: "Сума до оплати", reserveOperations: "Операції резерву", noReserveOperations: "Операцій резерву немає.", noRecords: "Записів немає.", phone: "Телефон", address: "Адреса", acceptedBy: "Ким прийнято", price: "Ціна", payment: "Оплата", status: "Статус", cash: "Готівка", fromReserve: "З резерву", created: "Створено", acceptedAt: "Прийнято", completedAt: "Виконано", noDate: "немає дати", deleteSettlement: "Видалити розрахунок", noSettlements: "Створених розрахунків поки немає. Новий розрахунок створюється в розділі «Клієнти» кнопкою «Розрахувати».", toReserve: "Із суми до оплати в резерв", fromReserveToPay: "З резерву в суму до оплати", topUp: "Поповнення резерву", completedFromReserve: "Виконані роботи з резерву" },
      pl: { completed: "Wykonane", refused: "Odmówione", accepted: "Przyjęte", declined: "Odrzucone", new: "Nowe", paid: "Opłacono", unpaid: "Nie opłacono", settlementStatus: "Status rozliczenia", completedCount: "Wykonane", refusedCount: "Odmówione", activeCount: "W trakcie", totalTasks: "Łącznie zadań", settlementTotal: "Suma rozliczenia", fullReport: "Pełny raport rozliczenia", completedWorks: "Wykonane prace", activeWorks: "Aktywne prace", newWorks: "Nowe prace", refusedWorks: "Odmówione prace", otherWorks: "Pozostałe prace", reserveInSettlement: "Rezerwa w rozliczeniu", reserveBefore: "Rezerwa przed rozliczeniem", reserveUsed: "Pobrano z rezerwy", reserveLeft: "Pozostała rezerwa", amountDue: "Kwota do zapłaty", reserveOperations: "Operacje rezerwy", noReserveOperations: "Brak operacji rezerwy.", noRecords: "Brak wpisów.", phone: "Telefon", address: "Adres", acceptedBy: "Kto przyjął", price: "Cena", payment: "Płatność", status: "Status", cash: "Gotówka", fromReserve: "Z rezerwy", created: "Utworzono", acceptedAt: "Przyjęto", completedAt: "Wykonano", noDate: "brak daty", deleteSettlement: "Usuń rozliczenie", noSettlements: "Nie ma jeszcze utworzonych rozliczeń klientów. Nowe rozliczenie utworzysz w sekcji „Klienci” przyciskiem „Rozlicz”.", toReserve: "Z kwoty do zapłaty do rezerwy", fromReserveToPay: "Z rezerwy do kwoty do zapłaty", topUp: "Doładowanie rezerwy", completedFromReserve: "Wykonane prace z rezerwy" }
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
      if (!res.ok) { items.innerHTML = "<p>Не удалось загрузить расчеты клиентов.</p>"; return; }
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
      const password = prompt("Пароль подтверждения");
      if (!password) return;
      const res = await fetch("/api/admin/clients/" + id + "/settle", {
        method: "POST",
        headers: adminHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ password })
      });
      if (!res.ok) {
        alert("Не удалось отметить расчет как рассчитанный.");
        return;
      }
      loadCalculations();
    }
    async function calculateClientSettlement(id) {
      const password = prompt("Пароль подтверждения");
      if (!password) return;
      const res = await fetch("/api/admin/client-settlements/" + id + "/calculate", {
        method: "POST",
        headers: adminHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ password })
      });
      if (!res.ok) {
        alert("Не удалось оплатить.");
        return;
      }
      loadCalculations();
    }
    async function deleteClientSettlement(id) {
      const password = prompt("Пароль подтверждения");
      if (!password) return;
      const res = await fetch("/api/admin/client-settlements/" + id + "/delete", {
        method: "POST",
        headers: adminHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ password })
      });
      if (!res.ok) {
        alert("Не удалось удалить расчет.");
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
        "Реклама Telegram": "Telegram Ads",
        "Реклама Facebook": "Facebook Ads",
        "Города": "Cities",
        "Город": "City",
        "Добавить город": "Add city",
        "Добавить Telegram-чат": "Add Telegram chat",
        "Название группы": "Group name",
        "Chat ID, например -100...": "Chat ID, for example -100...",
        "мастер, сантехник, электрик, ремонт": "handyman, plumber, electrician, repair",
        "Ключевые слова": "Keywords",
        "Чат включен: бот может отправлять рекламу в этот чат": "Chat enabled: the bot can send ads to this chat",
        "Сохранить чат": "Save chat",
        "Рекламный текст и картинка": "Ad text and image",
        "Название текста": "Text title",
        "Все": "All",
        "Клиенты": "Clients",
        "Мастера": "Workers",
        "Материал включен: можно отправлять и ставить в расписание": "Material enabled: it can be sent and scheduled",
        "Текст рекламы": "Ad text",
        "Текст рекламы / Messenger-ответ": "Ad text / Messenger reply",
        "Ссылка на картинку, например https://ogarniemy.pro/assets/banner.jpg": "Image link, for example https://ogarniemy.pro/assets/banner.jpg",
        "Ссылка на картинку": "Image link",
        "Картинка с компьютера": "Image from computer",
        "Загрузить картинку с компьютера": "Upload image from computer",
        "Картинка загружена.": "Image uploaded.",
        "Не удалось загрузить картинку.": "Could not upload image.",
        "Сохранить рекламный материал": "Save ad material",
        "Расписание": "Schedule",
        "Расписание подготовки": "Preparation schedule",
        "Расписание отправки": "Sending schedule",
        "Время отправки": "Sending time",
        "Рекламный материал": "Ad material",
        "Расписание включено: отправлять каждый день в это время": "Schedule enabled: send every day at this time",
        "Расписание включено: готовить материал каждый день в это время": "Schedule enabled: prepare material every day at this time",
        "Сохранить время": "Save time",
        "Отправить сейчас": "Send now",
        "Отправить сейчас": "Send now",
        "Подготовить сейчас": "Prepare now",
        "Что уже добавлено": "Already added",
        "Журнал": "Log",
        "Все города": "All cities",
        "Города пока не добавлены.": "No cities added yet.",
        "Чаты": "Chats",
        "Чаты пока не добавлены.": "No chats added yet.",
        "Рекламные материалы": "Ad materials",
        "Материалы пока не добавлены.": "No materials added yet.",
        "текст": "text",
        "последняя отправка": "last sent",
        "последняя подготовка": "last prepared",
        "нет": "none",
        "Расписание пока не добавлено.": "No schedule added yet.",
        "Найденные заявки": "Found requests",
        "Заявок пока нет.": "No requests yet.",
        "Журнал пока пуст.": "The log is empty.",
        "Редактировать": "Edit",
        "Выберите рекламный текст.": "Choose an ad text.",
        "Отправлено": "Sent",
        "Facebook отправка подготовлена.": "Facebook delivery prepared.",
        "Facebook лучше использовать как входящий канал: реклама ведет в Messenger или на сайт, бот отвечает тем, кто сам написал. Массовые ежедневные личные сообщения незнакомым людям Facebook ограничивает.": "Facebook is best used as an inbound channel: ads lead to Messenger or the site, and the bot replies to people who contacted you first. Facebook restricts daily mass private messages to strangers.",
        "Facebook-цели": "Facebook targets",
        "Facebook группы": "Facebook groups",
        "Добавить Facebook в группу": "Add Facebook group",
        "Страница / кампания / группа": "Page / campaign / group",
        "Страница / группа / ссылка": "Page / group / link",
        "ID или ссылка": "ID or link",
        "Заметки: аудитория, бюджет, что проверить": "Notes: audience, budget, what to check",
        "Группа включена: можно использовать в планировании Facebook": "Group enabled: it can be used in Facebook planning",
        "Сохранить группу": "Save group",
        "Подготовить сейчас": "Prepare now",
        "Группы пока не добавлены.": "No groups added yet."
      },
      uk: {
        "Реклама Telegram": "Реклама Telegram",
        "Реклама Facebook": "Реклама Facebook",
        "Города": "Міста",
        "Город": "Місто",
        "Добавить город": "Додати місто",
        "Добавить Telegram-чат": "Додати Telegram-чат",
        "Название группы": "Назва групи",
        "Chat ID, например -100...": "Chat ID, наприклад -100...",
        "мастер, сантехник, электрик, ремонт": "майстер, сантехнік, електрик, ремонт",
        "Ключевые слова": "Ключові слова",
        "Чат включен: бот может отправлять рекламу в этот чат": "Чат увімкнено: бот може надсилати рекламу в цей чат",
        "Сохранить чат": "Зберегти чат",
        "Рекламный текст и картинка": "Рекламний текст і картинка",
        "Название текста": "Назва тексту",
        "Все": "Усі",
        "Клиенты": "Клієнти",
        "Мастера": "Майстри",
        "Материал включен: можно отправлять и ставить в расписание": "Матеріал увімкнено: його можна надсилати й ставити в розклад",
        "Текст рекламы": "Текст реклами",
        "Текст рекламы / Messenger-ответ": "Текст реклами / відповідь Messenger",
        "Ссылка на картинку, например https://ogarniemy.pro/assets/banner.jpg": "Посилання на картинку, наприклад https://ogarniemy.pro/assets/banner.jpg",
        "Ссылка на картинку": "Посилання на картинку",
        "Картинка с компьютера": "Картинка з комп'ютера",
        "Загрузить картинку с компьютера": "Завантажити картинку з комп'ютера",
        "Картинка загружена.": "Картинку завантажено.",
        "Не удалось загрузить картинку.": "Не вдалося завантажити картинку.",
        "Сохранить рекламный материал": "Зберегти рекламний матеріал",
        "Расписание": "Розклад",
        "Расписание подготовки": "Розклад підготовки",
        "Расписание отправки": "Розклад відправки",
        "Время отправки": "Час відправки",
        "Рекламный материал": "Рекламний матеріал",
        "Расписание включено: отправлять каждый день в это время": "Розклад увімкнено: надсилати щодня в цей час",
        "Расписание включено: готовить материал каждый день в это время": "Розклад увімкнено: готувати матеріал щодня в цей час",
        "Сохранить время": "Зберегти час",
        "Отправить сейчас": "Надіслати зараз",
        "Подготовить сейчас": "Підготувати зараз",
        "Что уже добавлено": "Що вже додано",
        "Журнал": "Журнал",
        "Все города": "Усі міста",
        "Города пока не добавлены.": "Міста ще не додані.",
        "Чаты": "Чати",
        "Чаты пока не добавлены.": "Чати ще не додані.",
        "Рекламные материалы": "Рекламні матеріали",
        "Материалы пока не добавлены.": "Матеріали ще не додані.",
        "текст": "текст",
        "последняя отправка": "остання відправка",
        "последняя подготовка": "остання підготовка",
        "нет": "немає",
        "Расписание пока не добавлено.": "Розклад ще не доданий.",
        "Найденные заявки": "Знайдені заявки",
        "Заявок пока нет.": "Заявок поки немає.",
        "Журнал пока пуст.": "Журнал поки порожній.",
        "Редактировать": "Редагувати",
        "Выберите рекламный текст.": "Оберіть рекламний текст.",
        "Отправлено": "Надіслано",
        "Facebook отправка подготовлена.": "Facebook-відправку підготовлено.",
        "Facebook лучше использовать как входящий канал: реклама ведет в Messenger или на сайт, бот отвечает тем, кто сам написал. Массовые ежедневные личные сообщения незнакомым людям Facebook ограничивает.": "Facebook краще використовувати як вхідний канал: реклама веде в Messenger або на сайт, а бот відповідає тим, хто сам написав. Facebook обмежує масові щоденні особисті повідомлення незнайомим людям.",
        "Facebook-цели": "Facebook-цілі",
        "Facebook группы": "Facebook-групи",
        "Добавить Facebook в группу": "Додати Facebook у групу",
        "Страница / кампания / группа": "Сторінка / кампанія / група",
        "Страница / группа / ссылка": "Сторінка / група / посилання",
        "ID или ссылка": "ID або посилання",
        "Заметки: аудитория, бюджет, что проверить": "Нотатки: аудиторія, бюджет, що перевірити",
        "Группа включена: можно использовать в планировании Facebook": "Групу увімкнено: можна використовувати в плануванні Facebook",
        "Сохранить группу": "Зберегти групу",
        "Группы пока не добавлены.": "Групи ще не додані."
      },
      pl: {
        "Реклама Telegram": "Reklama Telegram",
        "Реклама Facebook": "Reklama Facebook",
        "Города": "Miasta",
        "Город": "Miasto",
        "Добавить город": "Dodaj miasto",
        "Добавить Telegram-чат": "Dodaj czat Telegram",
        "Название группы": "Nazwa grupy",
        "Chat ID, например -100...": "Chat ID, na przykład -100...",
        "мастер, сантехник, электрик, ремонт": "fachowiec, hydraulik, elektryk, naprawa",
        "Ключевые слова": "Słowa kluczowe",
        "Чат включен: бот может отправлять рекламу в этот чат": "Czat włączony: bot może wysyłać reklamy na ten czat",
        "Сохранить чат": "Zapisz czat",
        "Рекламный текст и картинка": "Tekst reklamowy i obraz",
        "Название текста": "Nazwa tekstu",
        "Все": "Wszyscy",
        "Клиенты": "Klienci",
        "Мастера": "Fachowcy",
        "Материал включен: можно отправлять и ставить в расписание": "Materiał włączony: można go wysyłać i dodać do harmonogramu",
        "Текст рекламы": "Tekst reklamy",
        "Текст рекламы / Messenger-ответ": "Tekst reklamy / odpowiedź Messenger",
        "Ссылка на картинку, например https://ogarniemy.pro/assets/banner.jpg": "Link do obrazu, np. https://ogarniemy.pro/assets/banner.jpg",
        "Ссылка на картинку": "Link do obrazu",
        "Картинка с компьютера": "Obraz z komputera",
        "Загрузить картинку с компьютера": "Prześlij obraz z komputera",
        "Картинка загружена.": "Obraz został przesłany.",
        "Не удалось загрузить картинку.": "Nie udało się przesłać obrazu.",
        "Сохранить рекламный материал": "Zapisz materiał reklamowy",
        "Расписание": "Harmonogram",
        "Расписание подготовки": "Harmonogram przygotowania",
        "Расписание отправки": "Harmonogram wysyłki",
        "Время отправки": "Godzina wysyłki",
        "Рекламный материал": "Materiał reklamowy",
        "Расписание включено: отправлять каждый день в это время": "Harmonogram włączony: wysyłaj codziennie o tej godzinie",
        "Расписание включено: готовить материал каждый день в это время": "Harmonogram włączony: przygotuj materiał codziennie o tej godzinie",
        "Сохранить время": "Zapisz czas",
        "Отправить сейчас": "Wyślij teraz",
        "Подготовить сейчас": "Przygotuj teraz",
        "Что уже добавлено": "Co już dodano",
        "Журнал": "Dziennik",
        "Все города": "Wszystkie miasta",
        "Города пока не добавлены.": "Nie dodano jeszcze miast.",
        "Чаты": "Czaty",
        "Чаты пока не добавлены.": "Nie dodano jeszcze czatów.",
        "Рекламные материалы": "Materiały reklamowe",
        "Материалы пока не добавлены.": "Nie dodano jeszcze materiałów.",
        "текст": "tekst",
        "последняя отправка": "ostatnia wysyłka",
        "последняя подготовка": "ostatnie przygotowanie",
        "нет": "brak",
        "Расписание пока не добавлено.": "Nie dodano jeszcze harmonogramu.",
        "Найденные заявки": "Znalezione zlecenia",
        "Заявок пока нет.": "Na razie brak zleceń.",
        "Журнал пока пуст.": "Dziennik jest pusty.",
        "Редактировать": "Edytuj",
        "Выберите рекламный текст.": "Wybierz tekst reklamy.",
        "Отправлено": "Wysłano",
        "Facebook отправка подготовлена.": "Wysyłka Facebook została przygotowana.",
        "Facebook лучше использовать как входящий канал: реклама ведет в Messenger или на сайт, бот отвечает тем, кто сам написал. Массовые ежедневные личные сообщения незнакомым людям Facebook ограничивает.": "Facebook najlepiej działa jako kanał przychodzący: reklama prowadzi do Messengera albo na stronę, a bot odpowiada osobom, które same napisały. Facebook ogranicza masowe codzienne wiadomości prywatne do nieznajomych.",
        "Facebook-цели": "Cele Facebook",
        "Facebook группы": "Grupy Facebook",
        "Добавить Facebook в группу": "Dodaj grupę Facebook",
        "Страница / кампания / группа": "Strona / kampania / grupa",
        "Страница / группа / ссылка": "Strona / grupa / link",
        "ID или ссылка": "ID lub link",
        "Заметки: аудитория, бюджет, что проверить": "Notatki: grupa odbiorców, budżet, co sprawdzić",
        "Группа включена: можно использовать в планировании Facebook": "Grupa włączona: można jej używać w planowaniu Facebook",
        "Сохранить группу": "Zapisz grupę",
        "Группы пока не добавлены.": "Nie dodano jeszcze grup."
      }
    };
    Object.assign(marketingTranslations.en, {
      "Слова-исключения": "Excluded words",
      "спам, казино, бесплатно": "spam, casino, free",
      "Выберите файл": "Choose file",
      "Без комментария": "No comment",
      "Все рекламные группы": "All ad groups",
      "Все Facebook-группы": "All Facebook groups",
      "Переслать в": "Forward to",
      "Не пересылать, ответить в этой же группе": "Do not forward, reply in this group",
      "Очистить найденные объявления": "Clear found ads",
      "Мои группы для пересылки": "My forwarding groups",
      "Куда перенаправлять найденные объявления из Facebook.": "Where to forward found Facebook ads.",
      "Моя группа Telegram": "My Telegram group",
      "Вставьте токен, чтобы сохранить или обновить.": "Paste the token to save or update.",
      "Сохранить настройки Facebook": "Save Facebook settings",
      "Не выбрана": "Not selected",
      "не выбрана": "not selected",
      "не сохранена": "not saved",
      "Пересылать в мою группу Telegram": "Forward to my Telegram group",
      "Пересылать в мою группу Facebook": "Forward to my Facebook group",
      "Группы или страницы для планирования рекламы и поиска по ключевым словам.": "Groups or pages for ad planning and keyword search.",
      "работа, квартира, продажа": "job, apartment, sale",
      "Выберите Facebook-группу, материал и время публикации.": "Choose a Facebook group, material and publishing time.",
      "Выберите группу, материал и время публикации.": "Choose a group, material and publishing time.",
      "Ручная подготовка выбранного рекламного материала.": "Manual preparation of the selected ad material.",
      "рекламных групп": "ad groups",
      "групп поиска": "search groups",
      "расписаний": "schedules",
      "групп": "groups",
      "материалов": "materials"
    });
    Object.assign(marketingTranslations.uk, {
      "Слова-исключения": "Слова-винятки",
      "спам, казино, бесплатно": "спам, казино, безкоштовно",
      "Выберите файл": "Оберіть файл",
      "Без комментария": "Без коментаря",
      "Все рекламные группы": "Усі рекламні групи",
      "Все Facebook-группы": "Усі Facebook-групи",
      "Переслать в": "Переслати в",
      "Не пересылать, ответить в этой же группе": "Не пересилати, відповісти в цій самій групі",
      "Очистить найденные объявления": "Очистити знайдені оголошення",
      "Мои группы для пересылки": "Мої групи для пересилання",
      "Куда перенаправлять найденные объявления из Facebook.": "Куди перенаправляти знайдені оголошення з Facebook.",
      "Моя группа Telegram": "Моя група Telegram",
      "Вставьте токен, чтобы сохранить или обновить.": "Вставте токен, щоб зберегти або оновити.",
      "Сохранить настройки Facebook": "Зберегти налаштування Facebook",
      "Не выбрана": "Не вибрано",
      "не выбрана": "не вибрано",
      "не сохранена": "не збережено",
      "Пересылать в мою группу Telegram": "Пересилати в мою групу Telegram",
      "Пересылать в мою группу Facebook": "Пересилати в мою групу Facebook",
      "Группы или страницы для планирования рекламы и поиска по ключевым словам.": "Групи або сторінки для планування реклами й пошуку за ключовими словами.",
      "работа, квартира, продажа": "робота, квартира, продаж",
      "Выберите Facebook-группу, материал и время публикации.": "Оберіть Facebook-групу, матеріал і час публікації.",
      "Выберите группу, материал и время публикации.": "Оберіть групу, матеріал і час публікації.",
      "Ручная подготовка выбранного рекламного материала.": "Ручна підготовка вибраного рекламного матеріалу.",
      "рекламных групп": "рекламних груп",
      "групп поиска": "груп пошуку",
      "расписаний": "розкладів",
      "групп": "груп",
      "материалов": "матеріалів"
    });
    Object.assign(marketingTranslations.pl, {
      "Слова-исключения": "Słowa wykluczające",
      "спам, казино, бесплатно": "spam, kasyno, za darmo",
      "Выберите файл": "Wybierz plik",
      "Без комментария": "Bez komentarza",
      "Все рекламные группы": "Wszystkie grupy reklamowe",
      "Все Facebook-группы": "Wszystkie grupy Facebook",
      "Переслать в": "Przekaż do",
      "Не пересылать, ответить в этой же группе": "Nie przekazywać, odpowiedzieć w tej samej grupie",
      "Очистить найденные объявления": "Wyczyść znalezione ogłoszenia",
      "Мои группы для пересылки": "Moje grupy do przekazywania",
      "Куда перенаправлять найденные объявления из Facebook.": "Dokąd przekazywać znalezione ogłoszenia z Facebooka.",
      "Моя группа Telegram": "Moja grupa Telegram",
      "Вставьте токен, чтобы сохранить или обновить.": "Wklej token, aby zapisać lub zaktualizować.",
      "Сохранить настройки Facebook": "Zapisz ustawienia Facebook",
      "Не выбрана": "Nie wybrano",
      "не выбрана": "nie wybrano",
      "не сохранена": "nie zapisano",
      "Пересылать в мою группу Telegram": "Przekazuj do mojej grupy Telegram",
      "Пересылать в мою группу Facebook": "Przekazuj do mojej grupy Facebook",
      "Группы или страницы для планирования рекламы и поиска по ключевым словам.": "Grupy lub strony do planowania reklamy i wyszukiwania po słowach kluczowych.",
      "работа, квартира, продажа": "praca, mieszkanie, sprzedaż",
      "Выберите Facebook-группу, материал и время публикации.": "Wybierz grupę Facebook, materiał i czas publikacji.",
      "Выберите группу, материал и время публикации.": "Wybierz grupę, materiał i czas publikacji.",
      "Ручная подготовка выбранного рекламного материала.": "Ręczne przygotowanie wybranego materiału reklamowego.",
      "рекламных групп": "grup reklamowych",
      "групп поиска": "grup wyszukiwania",
      "расписаний": "harmonogramów",
      "групп": "grup",
      "материалов": "materiałów"
    });
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
    function mtCount(count, label) {
      return `${count} ${mt(label)}`;
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
      document.title = mt(platform === "telegram" ? "Реклама Telegram" : "Реклама Facebook");
    }
    document.addEventListener("change", event => {
      if (event.target && event.target.id === "languageSelect") setTimeout(() => {
        if (state && Object.keys(state).length && typeof render === "function") render();
        applyMarketingLanguage();
      }, 0);
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
      if (status) status.textContent = mt("Загрузить картинку с компьютера") + "...";
      const content = await readFileAsDataUrl(file);
      const result = await api("upload-image", {
        name: file.name,
        contentType: file.type,
        content
      });
      messageImageUrl.value = result.url || "";
      input.value = "";
      if (status) status.textContent = mt("Картинка загружена.");
      return messageImageUrl.value;
    }
    async function loadState() {
      const res = await fetch(`/api/admin/marketing/${platform}`, { headers: adminHeaders() });
      state = await res.json();
      render();
      applyMarketingLanguage();
    }
    function cityOptions(selected = "") {
      return `<option value="">${escapeHtml(mt("Все города"))}</option>` + (state.cities || []).map(city => `<option value="${escapeHtml(city.name)}" ${city.name === selected ? "selected" : ""}>${escapeHtml(city.name)}</option>`).join("");
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
        alert(mt("Не удалось загрузить картинку."));
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
      if (!sendMessage.value) return alert(mt("Выберите рекламный текст."));
      const result = await api("send-now", { city: sendCity.value, messageId: sendMessage.value });
      alert(platform === "telegram" ? `${mt("Отправлено")}: ${result.sent}/${result.total}` : mt("Facebook отправка подготовлена."));
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
  <title>Реклама Telegram</title>
  <style>""" + MARKETING_PAGE_STYLE + r"""</style>
</head>
<body class="locked">
  <header>
    <h1>Реклама Telegram</h1>
    <nav><a href="/server">Задания</a> <a href="/users">Сотрудники</a> <a href="/clients">Клиенты</a> <a href="/completed">Выполненные задания</a> <a href="/calculations">Расчеты сотрудников</a> <a href="/client-calculations">Расчеты клиентов</a> <a href="/telegram-ads">Реклама Telegram</a> <a href="/facebook-ads">Реклама Facebook</a> <a href="/telegram-login">Telegram userbot</a> <a href="/settings">Настройки</a></nav>
  </header>
  <main>
    <section class="grid">
      <div class="panel"><h2>Города</h2><form onsubmit="saveCity(event)"><input id="cityName" placeholder="Warszawa" required><button>Добавить город</button></form><div id="cities" class="cards"></div></div>
      <div class="panel"><h2>Добавить Telegram-чат</h2><form id="groupForm"><input id="groupTitle" placeholder="Название группы" required><input id="groupChatId" placeholder="Chat ID, например -100..." required><select id="groupCity"></select><label class="field">Ключевые слова<textarea id="groupKeywords" placeholder="мастер, сантехник, электрик, ремонт"></textarea></label><label><input id="groupEnabled" type="checkbox" checked> Чат включен: бот может отправлять рекламу в этот чат</label><button>Сохранить чат</button></form></div>
      <div class="panel full"><h2>Рекламный текст и картинка</h2><form onsubmit="saveMessage(event)"><input id="messageId" type="hidden"><div class="row"><input id="messageTitle" placeholder="Название текста" required><select id="messageAudience"><option value="all">Все</option><option value="clients">Клиенты</option><option value="workers">Мастера</option></select><label><input id="messageEnabled" type="checkbox" checked> Материал включен: можно отправлять и ставить в расписание</label></div><label class="field">Текст рекламы<textarea id="messageBody" placeholder="Текст рекламы" required></textarea></label><div class="upload-row"><label class="field">Ссылка на картинку<input id="messageImageUrl" placeholder="Ссылка на картинку, например https://ogarniemy.pro/assets/banner.jpg"></label><label class="field">Картинка с компьютера<input id="messageImageFile" class="native-file" type="file" accept="image/png,image/jpeg,image/webp,image/gif"><button type="button" class="file-button" onclick="messageImageFile.click()">Выберите файл</button></label></div><p id="messageImageStatus" class="meta upload-status"></p><button>Сохранить рекламный материал</button></form></div>
      <div class="panel"><h2>Расписание</h2><form onsubmit="saveSchedule(event)"><input id="scheduleId" type="hidden"><label class="field">Город<select id="scheduleCity"></select></label><label class="field">Время отправки<input id="scheduleTime" type="time" value="09:30" required></label><label class="field">Рекламный материал<select id="scheduleMessage"></select></label><label><input id="scheduleEnabled" type="checkbox" checked> Расписание включено: отправлять каждый день в это время</label><button>Сохранить время</button></form></div>
      <div class="panel"><h2>Отправить сейчас</h2><label class="field">Город<select id="sendCity"></select></label><label class="field">Рекламный материал<select id="sendMessage"></select></label><button class="success" onclick="sendNow()">Отправить сейчас</button></div>
      <div class="panel full"><h2>Что уже добавлено</h2><div id="summary" class="cards"></div></div>
      <div class="panel full"><h2>Журнал</h2><div id="logs" class="cards"></div></div>
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
      cities.innerHTML = (state.cities || []).map(city => `<span class="pill">${escapeHtml(city.name)}</span>`).join("") || "<p class='meta'>Города пока не добавлены.</p>";
      summary.innerHTML = `
        <h3>Чаты</h3>${(state.groups || []).map(group => `<article class="item ${group.enabled ? "" : "off"}"><strong>${escapeHtml(group.title || group.chat_id)}</strong><p class="meta">${escapeHtml(group.city)} · ${escapeHtml(group.chat_id)}</p><p>${escapeHtml(group.keywords || "")}</p></article>`).join("") || "<p class='meta'>Чаты пока не добавлены.</p>"}
        <h3>Рекламные материалы</h3>${(state.messages || []).map(message => `<article class="item ${message.enabled ? "" : "off"}"><strong>#${message.id} ${escapeHtml(message.title)}</strong><p>${escapeHtml(message.body)}</p>${message.image_url ? `<img class="preview" src="${escapeHtml(message.image_url)}">` : ""}<button class="secondary" onclick="editMessage(${message.id})">Редактировать</button></article>`).join("") || "<p class='meta'>Материалы пока не добавлены.</p>"}
        <h3>Расписание</h3>${(state.schedules || []).map(item => `<article class="item ${item.enabled ? "" : "off"}"><strong>${escapeHtml(item.send_time)}</strong><p class="meta">${escapeHtml(item.city || "Все города")} · текст #${item.message_id} · последняя отправка: ${escapeHtml(item.last_sent_date || "нет")}</p><button class="secondary" onclick="editSchedule(${item.id})">Редактировать</button></article>`).join("") || "<p class='meta'>Расписание пока не добавлено.</p>"}
        <h3>Найденные заявки</h3>${(state.hits || []).map(hit => `<article class="item"><strong>${escapeHtml(hit.keyword)}</strong><p class="meta">${formatDate(hit.created_at)} · ${escapeHtml(hit.username || "")}</p><p>${escapeHtml(hit.message || "")}</p></article>`).join("") || "<p class='meta'>Заявок пока нет.</p>"}
      `;
      logs.innerHTML = (state.logs || []).map(log => `<article class="item"><strong>${escapeHtml(log.action)} · ${escapeHtml(log.status)}</strong><p class="meta">${formatDate(log.created_at)} · ${escapeHtml(log.city || "")} · ${escapeHtml(log.target_type)}</p><p>${escapeHtml(log.detail || "")}</p></article>`).join("") || "<p class='meta'>Журнал пока пуст.</p>";
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
  <title>Реклама Facebook</title>
  <style>""" + MARKETING_PAGE_STYLE + r"""</style>
</head>
<body class="locked">
  <header>
    <h1>Реклама Facebook</h1>
    <nav><a href="/server">Задания</a> <a href="/users">Сотрудники</a> <a href="/clients">Клиенты</a> <a href="/completed">Выполненные задания</a> <a href="/calculations">Расчеты сотрудников</a> <a href="/client-calculations">Расчеты клиентов</a> <a href="/telegram-ads">Реклама Telegram</a> <a href="/facebook-ads">Реклама Facebook</a> <a href="/telegram-login">Telegram userbot</a> <a href="/settings">Настройки</a></nav>
  </header>
  <main>
    <div class="note">Facebook лучше использовать как входящий канал: реклама ведет в Messenger или на сайт, бот отвечает тем, кто сам написал. Массовые ежедневные личные сообщения незнакомым людям Facebook ограничивает.</div>
    <section class="grid">
      <div class="panel"><h2>Города</h2><form onsubmit="saveCity(event)"><input id="cityName" placeholder="Warszawa" required><button>Добавить город</button></form><div id="cities" class="cards"></div></div>
      <div class="panel"><h2>Добавить Facebook в группу</h2><form id="targetForm"><input id="targetName" placeholder="Страница / группа / ссылка" required><input id="targetId" placeholder="ID или ссылка"><select id="targetCity"></select><textarea id="targetNotes" placeholder="Заметки: аудитория, бюджет, что проверить"></textarea><label><input id="targetEnabled" type="checkbox" checked> Группа включена: можно использовать в планировании Facebook</label><button>Сохранить группу</button></form></div>
      <div class="panel full"><h2>Рекламный текст и картинка</h2><form onsubmit="saveMessage(event)"><input id="messageId" type="hidden"><div class="row"><input id="messageTitle" placeholder="Название текста" required><select id="messageAudience"><option value="all">Все</option><option value="clients">Клиенты</option><option value="workers">Мастера</option></select><label><input id="messageEnabled" type="checkbox" checked> Материал включен: можно отправлять и ставить в расписание</label></div><label class="field">Текст рекламы<textarea id="messageBody" placeholder="Текст рекламы / Messenger-ответ" required></textarea></label><div class="upload-row"><label class="field">Ссылка на картинку<input id="messageImageUrl" placeholder="Ссылка на картинку"></label><label class="field">Картинка с компьютера<input id="messageImageFile" class="native-file" type="file" accept="image/png,image/jpeg,image/webp,image/gif"><button type="button" class="file-button" onclick="messageImageFile.click()">Выберите файл</button></label></div><p id="messageImageStatus" class="meta upload-status"></p><button>Сохранить рекламный материал</button></form></div>
      <div class="panel"><h2>Расписание отправки</h2><form onsubmit="saveSchedule(event)"><input id="scheduleId" type="hidden"><label class="field">Город<select id="scheduleCity"></select></label><label class="field">Время отправки<input id="scheduleTime" type="time" value="10:00" required></label><label class="field">Рекламный материал<select id="scheduleMessage"></select></label><label><input id="scheduleEnabled" type="checkbox" checked> Расписание включено: отправлять каждый день в это время</label><button>Сохранить время</button></form></div>
      <div class="panel"><h2>Отправить сейчас</h2><label class="field">Город<select id="sendCity"></select></label><label class="field">Рекламный материал<select id="sendMessage"></select></label><button class="success" onclick="sendNow()">Отправить сейчас</button></div>
      <div class="panel full"><h2>Что уже добавлено</h2><div id="summary" class="cards"></div></div>
      <div class="panel full"><h2>Журнал</h2><div id="logs" class="cards"></div></div>
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
      cities.innerHTML = (state.cities || []).map(city => `<span class="pill">${escapeHtml(city.name)}</span>`).join("") || "<p class='meta'>Города пока не добавлены.</p>";
      summary.innerHTML = `
        <h3>Facebook группы</h3>${(state.targets || []).map(target => `<article class="item ${target.enabled ? "" : "off"}"><strong>${escapeHtml(target.name)}</strong><p class="meta">${escapeHtml(target.city)} · ${escapeHtml(target.target_id || "")}</p><p>${escapeHtml(target.notes || "")}</p></article>`).join("") || "<p class='meta'>Группы пока не добавлены.</p>"}
        <h3>Рекламные материалы</h3>${(state.messages || []).map(message => `<article class="item ${message.enabled ? "" : "off"}"><strong>#${message.id} ${escapeHtml(message.title)}</strong><p>${escapeHtml(message.body)}</p>${message.image_url ? `<img class="preview" src="${escapeHtml(message.image_url)}">` : ""}<button class="secondary" onclick="editMessage(${message.id})">Редактировать</button></article>`).join("") || "<p class='meta'>Материалы пока не добавлены.</p>"}
        <h3>Расписание</h3>${(state.schedules || []).map(item => `<article class="item ${item.enabled ? "" : "off"}"><strong>${escapeHtml(item.send_time)}</strong><p class="meta">${escapeHtml(item.city || "Все города")} · текст #${item.message_id} · последняя подготовка: ${escapeHtml(item.last_sent_date || "нет")}</p><button class="secondary" onclick="editSchedule(${item.id})">Редактировать</button></article>`).join("") || "<p class='meta'>Расписание пока не добавлено.</p>"}
      `;
      logs.innerHTML = (state.logs || []).map(log => `<article class="item"><strong>${escapeHtml(log.action)} · ${escapeHtml(log.status)}</strong><p class="meta">${formatDate(log.created_at)} · ${escapeHtml(log.city || "")} · ${escapeHtml(log.target_type)}</p><p>${escapeHtml(log.detail || "")}</p></article>`).join("") || "<p class='meta'>Журнал пока пуст.</p>";
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
  <title>Настройки</title>
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
    <h1>Настройки</h1>
    <nav><a href="/server">Задания</a> <a href="/users">Сотрудники</a> <a href="/clients">Клиенты</a> <a href="/completed">Выполненные задания</a> <a href="/calculations">Расчеты сотрудников</a> <a href="/client-calculations">Расчеты клиентов</a> <a href="/telegram-ads">Реклама Telegram</a> <a href="/facebook-ads">Реклама Facebook</a> <a href="/telegram-login">Telegram userbot</a> <a href="/settings">Настройки</a></nav>
  </header>
  <main>
    <form id="settingsForm">
      <label>Валюта
        <select id="currency">
          <option value="RUB">RUB</option>
          <option value="USD">USD</option>
          <option value="EUR">EUR</option>
          <option value="PLN">PLN</option>
          <option value="UAH">UAH</option>
        </select>
      </label>
      <label>Единица резерва
        <select id="reserveUnit">
          <option value="credits">CRDT</option>
          <option value="tokens">TKN</option>
          <option value="coins">KOIN</option>
          <option value="points">BAL</option>
        </select>
      </label>
      <label>Процент с выполненных работ, который мы удерживаем себе
        <input id="completedFeePercent" type="number" min="0" max="100" step="0.1">
      </label>
      <label>Процент с отказанных или отмененных работ, который мы удерживаем себе
        <input id="refusedFeePercent" type="number" min="0" max="100" step="0.1">
      </label>
      <label>Сколько дней хранить выполненное задание
        <input id="completedTasksRetentionDays" type="number" min="1" max="365" step="1">
      </label>
      <label>Сколько дней хранить непринятые задания
        <input id="unacceptedTasksRetentionDays" type="number" min="1" max="365" step="1">
      </label>
      <label>Сколько дней хранить расчеты сотрудников
        <input id="employeeSettlementsRetentionDays" type="number" min="1" max="365" step="1">
      </label>
      <label>Сколько дней хранить расчеты клиентов
        <input id="clientSettlementsRetentionDays" type="number" min="1" max="365" step="1">
      </label>
      <label>Телефон обратной связи
        <input id="feedbackPhone" type="text">
      </label>
      <label>E-mail обратной связи
        <input id="feedbackEmail" type="text">
      </label>
      <label>Обычный адрес
        <input id="feedbackAddress" type="text">
      </label>
      <label>Telegram
        <input id="feedbackTelegram" type="text">
      </label>
      <label>WhatsApp
        <input id="feedbackWhatsApp" type="text">
      </label>
      <button>Сохранить</button>
      <p id="message" class="meta"></p>
    </form>
    <form id="passwordForm">
      <h2>Изменить пароль</h2>
      <label>Старый пароль
        <input id="oldPassword" type="password" autocomplete="current-password">
      </label>
      <label>Повторите старый пароль
        <input id="oldPasswordRepeat" type="password" autocomplete="current-password">
      </label>
      <label>Введите новый пароль
        <input id="newPassword" type="password" autocomplete="new-password">
      </label>
      <button>Изменить пароль</button>
      <button id="resetPassword" type="button">Сбросить пароль</button>
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
        message.textContent = "Не удалось загрузить настройки.";
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
      message.textContent = res.ok ? "Настройки сохранены." : "Не удалось сохранить настройки.";
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
        passwordMessage.textContent = "Не удалось изменить пароль.";
        return;
      }
      adminPassword = newPassword.value;
      sessionStorage.setItem("adminPassword", adminPassword);
      oldPassword.value = "";
      oldPasswordRepeat.value = "";
      newPassword.value = "";
      passwordMessage.textContent = "Пароль изменен.";
    });
    document.querySelector("#resetPassword").addEventListener("click", async () => {
      if (!confirm("Сбросить пароль на запасной постоянный пароль?")) {
        return;
      }
      const res = await fetch("/api/admin/reset-password", {
        method: "POST",
        headers: adminHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({})
      });
      if (!res.ok) {
        passwordMessage.textContent = "Не удалось сбросить пароль.";
        return;
      }
      adminPassword = "ZarazaZ";
      sessionStorage.setItem("adminPassword", adminPassword);
      oldPassword.value = "";
      oldPasswordRepeat.value = "";
      newPassword.value = "";
      passwordMessage.textContent = "Пароль сброшен.";
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
    input, textarea, select { width:100%; min-height:38px; border-radius:7px; border:1px solid #cfd7e3; padding:8px 10px; color:var(--text); background:white; } input[type="file"].native-file { display:none; } .file-button { width:max-content; }
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
    function messageOptions(selected = "", emptyLabel = "") {
      const emptyOption = emptyLabel ? `<option value="" ${!selected ? "selected" : ""}>${esc(emptyLabel)}</option>` : "";
      return emptyOption + (state.messages || []).map(item => `<option value="${item.id}" ${String(item.id) === String(selected) ? "selected" : ""}>#${item.id} ${esc(item.title)}</option>`).join("");
    }
    function targetOptions(selected = "") {
      if (platform === "telegram") {
        const groups = (state.groups || []).filter(item => item.enabled);
        return `<option value="">${esc(mt("Все рекламные группы"))}</option>` + groups.map(item => `<option value="${item.chat_id}" ${String(item.chat_id) === String(selected) ? "selected" : ""}>${esc(item.title || item.chat_id)}</option>`).join("");
      }
      const targets = state.targets || [];
      return `<option value="">${esc(mt("Все Facebook-группы"))}</option>` + targets.map(item => `<option value="${item.id}" ${String(item.id) === String(selected) ? "selected" : ""}>${esc(item.name)}</option>`).join("");
    }
    function watchTargetOptions(selected = "") {
      const groups = (state.groups || []).filter(item => item.enabled);
      return `<option value="${SAME_GROUP}" ${!selected ? "selected" : ""}>${esc(mt("Не пересылать, ответить в этой же группе"))}</option>` + groups.map(item => `<option value="${item.chat_id}" ${String(item.chat_id) === String(selected) ? "selected" : ""}>${esc(mt("Переслать в"))}: ${esc(item.title || item.chat_id)}</option>`).join("");
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
      await api("telegram-group", { chatId:watchChatId.value, title:watchTitle.value, keywords:watchKeywords.value, excludeKeywords:watchExcludeKeywords.value, targetChatId:watchTarget.value, responseMessageId:watchMaterial.value, notes:watchNotes.value, enabled:watchAdEnabled.checked, watchEnabled:true });
      event.target.reset(); watchAdEnabled.checked = false; loadState();
    }
    async function saveFacebookTarget(event) {
      event.preventDefault();
      await api("facebook-target", { id:fbTargetId.value || null, name:fbTargetName.value, targetId:fbTargetLink.value, keywords:fbKeywords.value, excludeKeywords:fbExcludeKeywords.value, targetAction:fbAction.value, responseMessageId:fbMaterial.value, notes:fbNotes.value, enabled:fbEnabled.checked });
      event.target.reset(); fbEnabled.checked = true; fbAction.value = "same_group"; loadState();
    }
    async function saveFacebookSettings(event) {
      event.preventDefault();
      await api("facebook-settings", { facebookForwardTelegramChatId:fbForwardTelegramChatId.value, facebookForwardTargetId:fbForwardTargetId.value, facebookPageAccessToken:fbPageAccessToken.value });
      fbPageAccessToken.value = "";
      loadState();
    }
    async function saveMessage(event) {
      event.preventDefault();
      try { await imageFromFile(messageImageFile, messageImageUrl); } catch (error) { alert("Не удалось загрузить картинку."); return; }
      await api("message", { id:messageId.value || null, title:messageTitle.value, audience:messageAudience.value, body:messageBody.value, imageUrl:messageImageUrl.value, enabled:messageEnabled.checked });
      event.target.reset(); messageEnabled.checked = true; messageId.value = ""; loadState();
    }
    async function saveSchedule(event) {
      event.preventDefault();
      await api("schedule", { id:scheduleId.value || null, targetId:scheduleTarget.value, sendTime:scheduleTime.value, messageId:scheduleMessage.value, enabled:scheduleEnabled.checked });
      event.target.reset(); scheduleTime.value = platform === "telegram" ? "09:30" : "10:00"; scheduleEnabled.checked = true; loadState();
    }
    async function sendNow() {
      if (!sendMessage.value) return alert("Выберите рекламный материал.");
      const result = await api("send-now", { targetId:sendTarget.value, messageId:sendMessage.value });
      alert(`Отправлено: ${result.sent || 0}/${result.total || 0}`);
      loadState();
      return;
      alert(platform === "telegram" ? `Отправлено: ${result.sent}/${result.total}` : "Facebook отправка подготовлена.");
      loadState();
    }
    async function deleteItem(kind, id) {
      if (!confirm("Удалить?")) return;
      await api("delete", { kind, id });
      loadState();
    }
    async function clearLogs() {
      if (!confirm("Очистить журнал?")) return;
      await api("clear-logs", {});
      loadState();
    }
    async function clearHits() {
      if (!confirm("\u041e\u0447\u0438\u0441\u0442\u0438\u0442\u044c \u043d\u0430\u0439\u0434\u0435\u043d\u043d\u044b\u0435 \u043e\u0431\u044a\u044f\u0432\u043b\u0435\u043d\u0438\u044f?")) return;
      await api("clear-hits", {});
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
        watchTitle.value = item.title || ""; watchChatId.value = item.chat_id || ""; watchKeywords.value = item.keywords || ""; watchExcludeKeywords.value = item.exclude_keywords || ""; watchTarget.value = item.target_chat_id || SAME_GROUP; watchMaterial.value = item.response_message_id || ""; watchNotes.value = item.notes || ""; watchAdEnabled.checked = !!item.enabled;
      } else {
        ownedTitle.value = item.title || ""; ownedChatId.value = item.chat_id || ""; ownedNotes.value = item.notes || "";
      }
    }
    function editFacebookTarget(id) {
      const item = (state.targets || []).find(row => String(row.id) === String(id));
      if (!item) return;
      fbTargetId.value = item.id; fbTargetName.value = item.name || ""; fbTargetLink.value = item.target_id || ""; fbKeywords.value = item.keywords || ""; fbExcludeKeywords.value = item.exclude_keywords || ""; fbAction.value = item.action || "same_group"; fbMaterial.value = item.response_message_id || ""; fbNotes.value = item.notes || ""; fbEnabled.checked = !!item.enabled;
    }
    function renderCommonSelects() {
      [scheduleTarget, sendTarget].forEach(select => select.innerHTML = targetOptions(select.value));
      [scheduleMessage, sendMessage].forEach(select => select.innerHTML = messageOptions(select.value));
      if (platform === "telegram") {
        watchTarget.innerHTML = watchTargetOptions(watchTarget.value);
        watchMaterial.innerHTML = messageOptions(watchMaterial.value, mt("Без комментария"));
      } else {
        fbMaterial.innerHTML = messageOptions(fbMaterial.value, mt("Без комментария"));
      }
    }
    function renderMessages() {
      messagesList.innerHTML = (state.messages || []).map(item => `<article class="item ${item.enabled ? "" : "off"}"><div class="item-head"><div class="item-title"><strong>#${item.id} <span class="no-translate">${esc(item.title)}</span></strong><span class="no-translate">${esc((item.body || "").slice(0, 140))}</span></div><div class="item-actions"><button onclick="editMessage(${item.id})">Редактировать</button><button class="danger" onclick="deleteItem('message', ${item.id})">Удалить</button></div></div>${item.image_url ? `<img class="preview" src="${esc(item.image_url)}">` : `<div class="meta">Картинка не выбрана</div>`}</article>`).join("") || `<div class="empty">Пока нет рекламных материалов</div>`;
    }
    function renderSchedules() {
      schedulesList.innerHTML = (state.schedules || []).map(item => `<article class="item ${item.enabled ? "" : "off"}"><div class="item-head"><div class="item-title"><strong>${esc(item.send_time)}</strong><span>Материал #${esc(item.message_id)} · цель: ${esc(item.target_id || "все")}</span></div><div class="item-actions"><button onclick="editSchedule(${item.id})">Редактировать</button><button class="danger" onclick="deleteItem('schedule', ${item.id})">Удалить</button></div></div><div class="meta">Последняя отправка: ${esc(item.last_sent_date || "нет")}</div></article>`).join("") || `<div class="empty">Пока нет расписаний</div>`;
    }
    function renderLogs() {
      logs.innerHTML = (state.logs || []).map(item => `<article class="item"><strong>${esc(item.action)} · ${esc(item.status)}</strong><div class="meta">${fmt(item.created_at)} · ${esc(item.target_type || "")}</div><p class="no-translate">${esc(item.detail || "")}</p></article>`).join("") || `<div class="empty">Журнал очищен</div>`;
    }
"""


TELEGRAM_ADS_HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Реклама Telegram</title>
  <style>""" + APPROVED_MARKETING_STYLE + r"""</style>
</head>
<body class="locked">
  <header><h1>Реклама Telegram</h1><nav><a href="/server">Задания</a><a href="/users">Сотрудники</a><a href="/clients">Клиенты</a><a href="/completed">Выполненные задания</a><a href="/calculations">Расчеты сотрудников</a><a href="/client-calculations">Расчеты клиентов</a><a href="/telegram-ads">Реклама Telegram</a><a href="/facebook-ads">Реклама Facebook</a><a href="/telegram-login">Telegram userbot</a><a href="/settings">Настройки</a></nav></header>
  <main>
    <div class="topbar"><div><h1>Реклама Telegram</h1><p>Группы для рекламы, группы для поиска объявлений, материалы, расписание и журнал.</p></div><div class="status-row"><span class="pill blue" id="ownedCount">0 рекламных групп</span><span class="pill green" id="watchCount">0 групп поиска</span><span class="pill amber" id="scheduleCount">0 расписаний</span></div></div>
    <div class="content"><div>
      <section class="section"><div class="section-header"><div><h2>Telegram-группы для рекламы</h2><p>Ваши группы, куда бот отправляет рекламные материалы по расписанию.</p></div></div><div class="section-body"><form class="form-grid" onsubmit="saveOwned(event)"><label>Название группы<input id="ownedTitle" required></label><label>Chat ID<input id="ownedChatId" required placeholder="-100..."></label><label class="full">Комментарий<input id="ownedNotes"></label><div class="form-actions full"><button class="primary">Сохранить группу</button></div></form><div class="items" id="ownedList"></div></div></section>
      <section class="section"><div class="section-header"><div><h2>Telegram-группы для поиска объявлений</h2><p>Бот ищет ключевые слова и отвечает в этой же группе или пересылает в выбранную вашу группу.</p></div></div><div class="section-body"><form class="form-grid" onsubmit="saveWatch(event)"><label>Группа поиска<input id="watchTitle" required></label><label>Chat ID<input id="watchChatId" required placeholder="-100..."></label><label>Действие при совпадении<select id="watchTarget"></select></label><label>Материал для комментария<select id="watchMaterial"></select></label><label class="full">Ключевые слова<input id="watchKeywords" placeholder="аренда, купить, срочно"></label><label class="full">Слова-исключения<input id="watchExcludeKeywords" placeholder="спам, казино, бесплатно"></label><label class="full">Дополнительная заметка<textarea id="watchNotes"></textarea></label><label class="full"><input id="watchAdEnabled" type="checkbox"> Использовать эту группу также для рекламы</label><div class="form-actions full"><button class="primary">Сохранить поиск</button></div></form><div class="items" id="watchList"></div></div></section>
      <section class="section" id="messagePanel"><div class="section-header"><div><h2>Рекламный материал</h2><p>Отдельный рекламный текст и картинка с компьютера.</p></div></div><div class="section-body"><form class="form-grid" onsubmit="saveMessage(event)"><input id="messageId" type="hidden"><label>Название материала<input id="messageTitle" required></label><label>Аудитория<select id="messageAudience"><option value="all">Все</option><option value="clients">Клиенты</option><option value="workers">Мастера</option></select></label><label class="full">Текст рекламы<textarea id="messageBody" required></textarea></label><label>Ссылка на картинку<input id="messageImageUrl"></label><label>Картинка с компьютера<input id="messageImageFile" class="native-file" type="file" accept="image/png,image/jpeg,image/webp,image/gif"><button type="button" class="file-button" onclick="messageImageFile.click()">Выберите файл</button></label><label class="full"><input id="messageEnabled" type="checkbox" checked> Материал включен</label><div class="form-actions full"><button class="primary">Сохранить рекламный материал</button></div></form><div class="items" id="messagesList"></div></div></section>
      <section class="section"><div class="section-header"><div><h2>Расписание рекламы</h2><p>Выберите группу, материал и время публикации.</p></div></div><div class="section-body"><form class="form-grid" onsubmit="saveSchedule(event)"><input id="scheduleId" type="hidden"><label>Группа<select id="scheduleTarget"></select></label><label>Материал<select id="scheduleMessage"></select></label><label>Время<input id="scheduleTime" type="time" value="09:30" required></label><label><input id="scheduleEnabled" type="checkbox" checked> Расписание включено</label><div class="form-actions full"><button class="primary">Сохранить расписание</button></div></form><div class="items" id="schedulesList"></div></div></section>
    </div><aside><section class="section"><div class="section-header"><div><h2>Найденные объявления</h2><p>Последние совпадения по ключевым словам.</p></div></div><div class="section-body"><div class="items" id="hitsList"></div></div></section><section class="section"><div class="section-header"><div><h2>Журнал Telegram</h2><p>Последние действия.</p></div><button class="danger" onclick="clearLogs()">Очистка журнала</button></div><div class="section-body"><div class="items" id="logs"></div></div></section></aside></div>
    <div class="send-now"><div><strong>Отправить сейчас</strong><span>Ручная отправка выбранного рекламного материала.</span></div><div><select id="sendTarget"></select><select id="sendMessage"></select><button onclick="sendNow()">Отправить сейчас</button></div></div>
  </main>
  <script>const platform = "telegram";""" + APPROVED_MARKETING_SCRIPT + r"""
    function render() {
      const hitsHeader = hitsList.closest(".section").querySelector(".section-header");
      if (hitsHeader && !document.getElementById("clearHitsButton")) hitsHeader.insertAdjacentHTML("beforeend", `<button id="clearHitsButton" class="danger" onclick="clearHits()">&#1054;&#1095;&#1080;&#1089;&#1090;&#1080;&#1090;&#1100; &#1085;&#1072;&#1081;&#1076;&#1077;&#1085;&#1085;&#1099;&#1077; &#1086;&#1073;&#1098;&#1103;&#1074;&#1083;&#1077;&#1085;&#1080;&#1103;</button>`);
      renderCommonSelects(); renderMessages(); renderSchedules(); renderLogs();
      const groups = state.groups || [];
      const owned = groups.filter(item => item.enabled);
      const watched = groups.filter(item => item.watch_enabled);
      const groupLabel = chatId => {
        const group = groups.find(row => String(row.chat_id) === String(chatId));
        return group ? `${group.title || group.chat_id} (${group.chat_id})` : chatId;
      };
      ownedCount.textContent = mtCount(owned.length, "рекламных групп"); watchCount.textContent = mtCount(watched.length, "групп поиска"); scheduleCount.textContent = mtCount((state.schedules || []).length, "расписаний");
      ownedList.innerHTML = owned.map(item => `<article class="item"><div class="item-head"><div class="item-title"><strong class="no-translate">${esc(item.title || item.chat_id)}</strong><span class="no-translate">${esc(item.chat_id)}</span></div><div class="item-actions"><button onclick="editTelegramGroup('${item.chat_id}', 'owned')">Редактировать</button><button class="danger" onclick="deleteItem('telegram-group', '${item.chat_id}')">Удалить</button></div></div><div class="meta no-translate">${esc(item.notes || "")}</div></article>`).join("") || `<div class="empty">Пока нет групп для рекламы</div>`;
      watchList.innerHTML = watched.map(item => `<article class="item"><div class="item-head"><div class="item-title"><strong>${esc(item.title || item.chat_id)}</strong><span>${item.target_chat_id ? "Переслать в: " + esc(groupLabel(item.target_chat_id)) : "Не пересылать, ответить в этой же группе"}</span></div><div class="item-actions"><button onclick="editTelegramGroup('${item.chat_id}', 'watch')">Редактировать</button><button class="danger" onclick="deleteItem('telegram-group', '${item.chat_id}')">Удалить</button></div></div><div>${String(item.keywords || "").split(",").filter(Boolean).map(word => `<span class="tag no-translate">${esc(word.trim())}</span>`).join("")}</div><div>${String(item.exclude_keywords || "").split(",").filter(Boolean).map(word => `<span class="tag no-translate">-${esc(word.trim())}</span>`).join("")}</div><div class="meta">Материал: ${esc(item.response_message_id || "не выбран")}</div><div class="meta no-translate">${esc(item.notes || "")}</div></article>`).join("") || `<div class="empty">Пока нет групп для поиска</div>`;
      hitsList.innerHTML = (state.hits || []).map(item => `<article class="item"><strong>${esc(item.keyword)}</strong><div class="meta">${fmt(item.created_at)} · ${esc(item.username || "")}</div><p class="no-translate">${esc(item.message || "")}</p></article>`).join("") || `<div class="empty">Совпадений пока нет</div>`;
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
  <title>CollectorHub — Реклама Facebook</title>
  <style>
    :root { --bg:#f4f7fb; --card:#ffffff; --ink:#172026; --muted:#64748b; --blue:#2563eb; --blue2:#1d4ed8; --green:#16a34a; --red:#dc2626; --amber:#f59e0b; --violet:#7c3aed; --line:#dbe4f0; --shadow:0 18px 45px rgba(15,23,42,.12); }
    * { box-sizing:border-box; }
    body { margin:0; font-family:Arial, sans-serif; color:var(--ink); background:linear-gradient(135deg,#eef6ff,#f8fafc 48%,#f3e8ff); min-height:100vh; }
    body.locked header, body.locked main { display:none; }
    header { background:linear-gradient(135deg,#0f172a,#1d4ed8 55%,#7c3aed); color:white; padding:22px 28px; box-shadow:var(--shadow); }
    header h1 { margin:0 0 14px; font-size:28px; }
    nav { display:grid; grid-template-columns:repeat(10,minmax(104px,1fr)); gap:10px; max-width:1380px; }
    nav a { color:#0f172a; background:#facc15; font-weight:800; padding:10px; border-radius:12px; text-align:center; text-decoration:none; min-height:44px; display:flex; align-items:center; justify-content:center; }
    nav a.active { background:white; color:#1d4ed8; }
    main { max-width:1380px; margin:0 auto; padding:24px; }
    .hero { display:flex; justify-content:space-between; gap:18px; align-items:stretch; background:rgba(255,255,255,.78); border:1px solid rgba(255,255,255,.8); border-radius:24px; padding:22px; box-shadow:var(--shadow); margin-bottom:20px; }
    .hero h2 { margin:0; font-size:34px; }
    .hero p { margin:8px 0 0; color:var(--muted); font-size:16px; }
    .status { display:flex; flex-wrap:wrap; gap:10px; align-items:center; justify-content:flex-end; }
    .pill { border-radius:999px; padding:9px 13px; font-weight:800; color:white; background:var(--blue); white-space:nowrap; }
    .pill.green { background:var(--green); } .pill.red { background:var(--red); } .pill.amber { background:var(--amber); } .pill.violet { background:var(--violet); }
    .grid { display:grid; grid-template-columns:1.2fr .8fr; gap:18px; align-items:start; }
    .card { background:var(--card); border:1px solid var(--line); border-radius:22px; box-shadow:var(--shadow); padding:18px; margin-bottom:18px; }
    .card h3 { margin:0 0 6px; font-size:22px; }
    .card p { color:var(--muted); margin:0 0 14px; }
    .buttons { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:14px; }
    button, .bigbtn { border:0; border-radius:16px; padding:14px 16px; font:inherit; font-weight:900; cursor:pointer; color:white; background:linear-gradient(135deg,var(--blue),var(--blue2)); box-shadow:0 12px 26px rgba(37,99,235,.22); text-align:center; }
    .bigbtn { min-height:82px; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:5px; font-size:16px; }
    .bigbtn .icon { font-size:25px; line-height:1; }
    button:hover, .bigbtn:hover { transform:translateY(-1px); filter:brightness(1.03); }
    .green { background:linear-gradient(135deg,#16a34a,#15803d); } .red { background:linear-gradient(135deg,#ef4444,#b91c1c); } .amber { background:linear-gradient(135deg,#f59e0b,#d97706); } .violet { background:linear-gradient(135deg,#8b5cf6,#6d28d9); } .dark { background:linear-gradient(135deg,#334155,#0f172a); }
    .formrow { display:grid; grid-template-columns:180px 1fr; gap:12px; align-items:center; margin:10px 0; }
    label { font-weight:800; color:#334155; }
    input, select, textarea { width:100%; border:1px solid var(--line); background:#f8fafc; border-radius:13px; padding:12px; font:inherit; color:var(--ink); }
    textarea { min-height:140px; resize:vertical; font-family:Consolas, monospace; }
    .two { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
    .actions { display:flex; flex-wrap:wrap; gap:10px; margin-top:12px; }
    .log { background:#0f172a; color:#dbeafe; border-radius:16px; padding:14px; min-height:260px; max-height:520px; overflow:auto; white-space:pre-wrap; font-family:Consolas, monospace; font-size:13px; }
    .list { display:grid; gap:10px; }
    .item { padding:12px; border-radius:16px; border:1px solid var(--line); background:#f8fafc; }
    .item strong { display:block; margin-bottom:4px; }
    .muted { color:var(--muted); font-size:13px; }
    .notice { background:#fff7ed; border:1px solid #fed7aa; color:#9a3412; border-radius:16px; padding:12px; margin-top:12px; }
    @media (max-width:1000px){ nav{grid-template-columns:repeat(2,1fr)} .grid{grid-template-columns:1fr} .buttons{grid-template-columns:repeat(2,1fr)} .formrow{grid-template-columns:1fr} .hero{flex-direction:column} }
  </style>
</head>
<body class="locked">
<header>
  <h1>CollectorHub</h1>
  <nav><a href="/server">Задания</a><a href="/users">Сотрудники</a><a href="/clients">Клиенты</a><a href="/completed">Выполненные задания</a><a href="/calculations">Расчеты сотрудников</a><a href="/client-calculations">Расчеты клиентов</a><a href="/telegram-ads">Реклама Telegram</a><a class="active" href="/facebook-ads">Реклама Facebook</a><a href="/telegram-login">Telegram userbot</a><a href="/settings">Настройки</a></nav>
</header>
<main>
  <section class="hero">
    <div><h2>Facebook → фильтры → Telegram</h2><p>Это серверная панель нашего CollectorHub внутри кнопки «Реклама Facebook».</p></div>
    <div class="status"><span id="runPill" class="pill red">■ Остановлен</span><span id="groupsPill" class="pill violet">0 групп</span><span id="modePill" class="pill amber">Telegram</span></div>
  </section>
  <div class="grid">
    <div>
      <section class="card">
        <h3>Главные кнопки CollectorHub</h3><p>Те же действия, что в программе на компьютере.</p>
        <div class="buttons">
          <button class="bigbtn green" onclick="startCollector()"><span class="icon">▶</span>Запустить Collector</button>
          <button class="bigbtn red" onclick="stopCollector()"><span class="icon">■</span>Остановить</button>
          <button class="bigbtn violet" onclick="saveAll()"><span class="icon">💾</span>Сохранить всё</button>
          <button class="bigbtn" onclick="showPanel('keywords')"><span class="icon">🟢</span>Ключевые слова</button>
          <button class="bigbtn red" onclick="showPanel('exclusions')"><span class="icon">🔴</span>Слова-исключения</button>
          <button class="bigbtn" onclick="showPanel('groups')"><span class="icon">👥</span>Группы Facebook</button>
          <button class="bigbtn amber" onclick="showPanel('search')"><span class="icon">🔍</span>Автопоиск Facebook-групп</button>
          <button class="bigbtn dark" onclick="showPanel('env')"><span class="icon">⚙️</span>Telegram .env</button>
          <button class="bigbtn" onclick="testTelegram()"><span class="icon">📨</span>Тест Telegram</button>
          <button class="bigbtn violet" onclick="resendLast()"><span class="icon">🧪</span>Повторить последнее</button>
          <button class="bigbtn amber" onclick="clearPosts()"><span class="icon">🧹</span>Очистить посты</button>
          <button class="bigbtn dark" onclick="refreshState()"><span class="icon">🔄</span>Обновить список групп</button>
        </div>
        <div class="notice">Все кнопки этой панели подключены к серверу: запуск/остановка, слова, исключения, группы, автопоиск, тест Telegram, повтор последнего и очистка постов.</div>
      </section>
      <section class="card">
        <h3>Настройки отправки</h3>
        <div class="two">
          <div class="formrow"><label>Постов на группу</label><input id="postsLimit" type="number" value="100" min="1" max="500"></div>
          <div class="formrow"><label>Куда отправлять</label><select id="sendMode"><option value="telegram">Telegram</option><option value="facebook">Facebook</option><option value="both">Telegram + Facebook</option></select></div>
        </div>
        <div class="formrow"><label>FB-группа куда публиковать</label><select id="fbTargetSelect"></select></div>
        <div class="formrow"><label>Название FB-группы</label><input id="fbTargetName"></div>
        <div class="formrow"><label>Ссылка FB-группы</label><input id="fbTargetUrl"></div>
        <div class="actions"><button onclick="saveSettings()">💾 Сохранить настройки</button></div>
      </section>
      <section class="card panel" id="panel-keywords">
        <h3>🟢 Ключевые слова</h3><p>Одна строка или через запятую — как в проекте.</p><textarea id="keywords"></textarea><div class="actions"><button onclick="saveWords()">Сохранить слова</button></div>
      </section>
      <section class="card panel" id="panel-exclusions" style="display:none">
        <h3>🔴 Слова-исключения</h3><p>Если найдено исключение — пересылку отменяем, запись остаётся в базе/журнале.</p><textarea id="exclusions"></textarea><div class="actions"><button onclick="saveWords()">Сохранить исключения</button></div>
      </section>
      <section class="card panel" id="panel-groups" style="display:none">
        <h3>👥 Группы Facebook</h3><p>Формат: название | ссылка. Можно вставлять список строками.</p><textarea id="groupsText"></textarea><div class="actions"><button onclick="saveGroups()">Сохранить группы</button></div>
      </section>
      <section class="card panel" id="panel-search" style="display:none">
        <h3>🔍 Автопоиск Facebook-групп</h3><p>Ищет группы по словам, открывает группы, вступает и сохраняет только подтверждённые группы.</p><input id="searchQuery" placeholder="ремонт, работа, Świdnica, Wałbrzych"><div class="actions"><button onclick="searchGroups()">Запустить автопоиск</button></div>
      </section>
      <section class="card panel" id="panel-env" style="display:none">
        <h3>⚙️ Telegram .env</h3><p>Секреты лучше держать в Railway Variables, не в интерфейсе. Здесь пока только напоминание.</p><div class="notice">TELEGRAM_BOT_TOKEN и TELEGRAM_GROUP_ID на сервере должны быть в переменных окружения Railway.</div>
      </section>
    </div>
    <aside>
      <section class="card"><h3>Добавленные Facebook-группы</h3><div id="groupsList" class="list"></div></section>
      <section class="card"><h3>Журнал CollectorHub</h3><div id="log" class="log">Загрузка...</div></section>
    </aside>
  </div>
</main>
<script>
let adminPassword = sessionStorage.getItem("adminPassword") || "";
let state = {};
function adminHeaders(extra={}){ if(!adminPassword){ adminPassword = prompt("Admin password") || ""; sessionStorage.setItem("adminPassword", adminPassword); } return {"X-Admin-Password":adminPassword, ...extra}; }
async function requireAdminAccess(start){ while(true){ if(!adminPassword) adminPassword = prompt("Admin password") || ""; if(!adminPassword){ document.body.innerHTML=""; return; } sessionStorage.setItem("adminPassword", adminPassword); const res=await fetch("/api/admin/check-password",{headers:{"X-Admin-Password":adminPassword}}); if(res.ok){ document.body.classList.remove("locked"); start(); return; } sessionStorage.removeItem("adminPassword"); adminPassword=""; alert("Неверный пароль администратора"); } }
function esc(v){ return String(v ?? "").replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch])); }
async function api(path, body){ const res=await fetch(path,{method:"POST",headers:adminHeaders({"Content-Type":"application/json"}),body:JSON.stringify(body||{})}); const data=await res.json().catch(()=>({})); if(!res.ok) throw new Error(data.error || "Ошибка сервера"); return data; }
async function refreshState(){ const res=await fetch("/api/admin/collectorhub/state",{headers:adminHeaders()}); if(!res.ok){ alert("Не удалось загрузить CollectorHub"); return; } state=await res.json(); render(); }
function render(){
  runPill.textContent = state.running ? "▶ Работает" : (state.searchRunning ? "🔍 Автопоиск" : "■ Остановлен"); runPill.className = "pill " + (state.running ? "green" : (state.searchRunning ? "amber" : "red"));
  groupsPill.textContent = (state.groups||[]).length + " групп";
  const settings = state.settings || {}; sendMode.value = settings.send_mode || "telegram"; modePill.textContent = sendMode.options[sendMode.selectedIndex]?.textContent || "Telegram";
  keywords.value = state.keywords || ""; exclusions.value = state.exclusions || ""; groupsText.value = state.groupsText || ""; postsLimit.value = settings.max_posts_per_group || 100;
  fbTargetName.value = settings.facebook_target_group_name || ""; fbTargetUrl.value = settings.facebook_target_group_url || "";
  fbTargetSelect.innerHTML = `<option value="">Не выбрана</option>` + (state.groups||[]).map(g=>`<option value="${esc(g.url)}" ${g.url===(settings.facebook_target_group_url||"")?"selected":""}>${esc(g.name)}</option>`).join("");
  groupsList.innerHTML = (state.groups||[]).map(g=>`<div class="item"><strong>${esc(g.name)}</strong><div class="muted">${esc(g.url)}</div></div>`).join("") || `<div class="item muted">Пока нет групп Facebook</div>`;
  log.textContent = state.log || "Журнал пока пуст.";
}
fbTargetSelect?.addEventListener("change",()=>{ const g=(state.groups||[]).find(x=>x.url===fbTargetSelect.value); if(g){ fbTargetName.value=g.name; fbTargetUrl.value=g.url; }});
function showPanel(name){ document.querySelectorAll('.panel').forEach(p=>p.style.display='none'); const el=document.getElementById('panel-'+name); if(el) el.style.display='block'; }
async function saveWords(){ await api('/api/admin/collectorhub/save-words',{keywords:keywords.value, exclusions:exclusions.value}); await refreshState(); alert('Слова сохранены'); }
async function saveGroups(){ await api('/api/admin/collectorhub/save-groups',{groupsText:groupsText.value}); await refreshState(); alert('Группы сохранены'); }
async function saveSettings(){ await api('/api/admin/collectorhub/save-settings',{sendMode:sendMode.value, facebookTargetName:fbTargetName.value, facebookTargetUrl:fbTargetUrl.value, postsLimit:postsLimit.value}); await refreshState(); alert('Настройки сохранены'); }
async function saveAll(){ await saveWords(); await saveGroups(); await saveSettings(); }
async function startCollector(){ try{ await saveAll(); await api('/api/admin/collectorhub/start',{}); await refreshState(); }catch(e){ alert(e.message); } }
async function stopCollector(){ try{ await api('/api/admin/collectorhub/stop',{}); await refreshState(); }catch(e){ alert(e.message); } }
async function clearPosts(){ if(!confirm('Очистить найденные посты CollectorHub?')) return; await api('/api/admin/collectorhub/reset-posts',{}); alert('Посты очищены'); }
async function showCommandResult(title, fn){ try{ const r=await fn(); const text=(r.stdout||'') + (r.stderr ? '\n' + r.stderr : ''); alert(title + ': ' + (r.ok ? 'OK' : 'ОШИБКА') + (text.trim() ? '\n\n' + text.trim() : '')); await refreshState(); }catch(e){ alert(title + ': ' + e.message); await refreshState(); } }
async function searchGroups(){ const q=(searchQuery.value||keywords.value||'').trim(); if(!q){ alert('Введи слова для автопоиска групп'); return; } await api('/api/admin/collectorhub/search-groups',{query:q}); await refreshState(); alert('Автопоиск запущен. Смотри журнал справа.'); }
async function testTelegram(){ await showCommandResult('Тест Telegram', ()=>api('/api/admin/collectorhub/test-telegram',{})); }
async function resendLast(){ await showCommandResult('Повтор последнего поста', ()=>api('/api/admin/collectorhub/resend-last',{})); }
requireAdminAccess(refreshState); setInterval(refreshState, 6000);
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

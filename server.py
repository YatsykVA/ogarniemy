from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlencode, urlparse
from urllib.request import urlopen
import hashlib
import json
import mimetypes
import os
import secrets
import sqlite3
import threading
import time


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
RETENTION_MAX_DAYS = 1000
CLEANUP_INTERVAL_SECONDS = 60 * 60


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
    digits = "".join(ch for ch in text if ch.isdigit())
    return f"+{digits}" if digits else ""


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
            relative_path = "client.html"
        elif path == "/employee":
            relative_path = "employee.html"
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
            self.send_static_file("client.html")
            return
        if path == "/employee":
            self.send_static_file("employee.html")
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
        if path.startswith("/api/admin/users/") and path.endswith("/report"):
            self.handle_user_report(path)
            return
        if path.startswith("/api/admin/clients/") and path.endswith("/report"):
            self.handle_client_report(path)
            return
        self.send_json({"error": "not_found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
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
        password = str(data.get("password", ""))
        conn = db()
        row = conn.execute(
            "select * from users where login = ? and deleted_at is null",
            (login,),
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
        password = str(data.get("password", ""))
        conn = db()
        row = conn.execute(
            "select * from clients where login = ? and deleted_at is null",
            (login,),
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
        exists = conn.execute(
            "select id from clients where phone = ? and deleted_at is null",
            (phone,),
        ).fetchone()
        if exists:
            conn.close()
            self.send_json({"error": "phone_already_registered"}, 409)
            return
        login = f"client-{secrets.token_hex(4)}"
        while conn.execute("select id from clients where login = ?", (login,)).fetchone():
            login = f"client-{secrets.token_hex(4)}"
        create_client(conn, login, password, display_name)
        conn.execute("update clients set phone = ? where login = ?", (phone, login))
        conn.commit()
        conn.close()
        self.send_json({"ok": True, "login": login}, 201)

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
        settings = {
            "currency": get_setting(conn, "currency") or "RUB",
            "showPrices": (get_setting(conn, "show_prices") or "1") != "0",
            "completedFeePercent": float(get_setting(conn, "completed_fee_percent") or "1"),
            "refusedFeePercent": float(get_setting(conn, "refused_fee_percent") or "1"),
            "completedTasksRetentionDays": retention_days(conn, "completed_tasks_retention_days"),
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
        currency = str(data.get("currency", "RUB")).strip().upper()
        if currency not in ("RUB", "USD", "EUR", "PLN", "UAH"):
            self.send_json({"error": "bad_currency"}, 400)
            return
        show_prices = "1" if data.get("showPrices", True) else "0"
        completed_fee_percent = parse_price(data.get("completedFeePercent", 1))
        refused_fee_percent = parse_price(data.get("refusedFeePercent", 1))
        completed_tasks_retention_days = parse_retention_days(data.get("completedTasksRetentionDays", RETENTION_DEFAULT_DAYS))
        employee_settlements_retention_days = parse_retention_days(data.get("employeeSettlementsRetentionDays", RETENTION_DEFAULT_DAYS))
        client_settlements_retention_days = parse_retention_days(data.get("clientSettlementsRetentionDays", RETENTION_DEFAULT_DAYS))
        if completed_fee_percent is None or completed_fee_percent < 0 or completed_fee_percent > 100:
            self.send_json({"error": "bad_completed_fee_percent"}, 400)
            return
        if refused_fee_percent is None or refused_fee_percent < 0 or refused_fee_percent > 100:
            self.send_json({"error": "bad_refused_fee_percent"}, 400)
            return
        if completed_tasks_retention_days is None:
            self.send_json({"error": "bad_completed_tasks_retention_days"}, 400)
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
            "insert into settings(key, value) values('show_prices', ?) on conflict(key) do update set value = excluded.value",
            (show_prices,),
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
            select tasks.id, tasks.title, tasks.description, tasks.phone, tasks.address, tasks.price, tasks.payment_method,
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
        self.send_json({"tasks": [task_json(row, lang) for row in rows]})

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

    def handle_admin_tasks(self):
        if not self.is_admin():
            self.send_json({"error": "admin_unauthorized"}, 401)
            return
        conn = db()
        rows = conn.execute(
            """
            select tasks.id, tasks.title, tasks.description, tasks.phone, tasks.address, tasks.price, tasks.payment_method,
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
            select tasks.id, tasks.title, tasks.description, tasks.phone, tasks.address, tasks.price, tasks.payment_method,
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
        phone = str(data.get("phone", "")).strip()
        address = str(data.get("address", "")).strip()
        price = parse_price(data.get("price", 0))
        raw_payment_method = data.get("paymentMethod", data.get("payment_method", None))
        payment_method = normalize_payment_method(raw_payment_method)
        raw_client_id = data.get("clientId", data.get("client_id", ""))
        if client_id is not None:
            if not title or not description or not phone or not address:
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
        cur = conn.execute(
            "insert into tasks(title, description, phone, address, price, payment_method, client_id, created_at) values(?, ?, ?, ?, ?, ?, ?, ?)",
            (title, description, phone, address, price, payment_method, client_id, int(time.time())),
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
            select tasks.id, tasks.title, tasks.description, tasks.phone, tasks.address, tasks.price, tasks.payment_method,
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
            "select id, status from tasks where id = ? and client_id = ?",
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
            "select id, status from tasks where id = ? and client_id = ?",
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
        phone = str(data.get("phone", "")).strip()
        address = str(data.get("address", "")).strip()
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

        conn.execute(
            """
            update tasks
            set title = ?, description = ?, phone = ?, address = ?, price = ?, payment_method = ?, client_id = ?
            where id = ?
            """,
            (title, description, phone, address, price, payment_method, client_id, task_id),
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
        login = str(data.get("login", "")).strip()
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
        login = str(data.get("login", "")).strip()
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
        login = str(data.get("login", "")).strip()
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
                "update clients set login = ?, display_name = ?, password_hash = ?, salt = ? where id = ?",
                (login, display_name, password_hash(password, salt), salt, client_id),
            )
            conn.execute("delete from client_tokens where client_id = ?", (client_id,))
        else:
            conn.execute(
                "update clients set login = ?, display_name = ? where id = ?",
                (login, display_name, client_id),
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
        login = str(data.get("login", "")).strip()
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
                set login = ?, display_name = ?, password_hash = ?, salt = ?
                where id = ?
                """,
                (login, display_name, password_hash(password, salt), salt, user_id),
            )
            conn.execute("delete from tokens where user_id = ?", (user_id,))
        else:
            conn.execute(
                "update users set login = ?, display_name = ? where id = ?",
                (login, display_name, user_id),
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
            select id, title, description, phone, address, price, payment_method, status, created_at, decided_at, accepted_at, completed_at
            from tasks
            where assigned_to = ? and settlement_id is null
            order by coalesce(decided_at, created_at) desc, id desc
            """,
            (user_id,),
        ).fetchall()
        refused_rows = conn.execute(
            """
            select tasks.id, tasks.title, tasks.description, tasks.phone, tasks.address, tasks.price, tasks.payment_method,
                   'refused' as status, tasks.created_at, task_events.created_at as decided_at,
                   tasks.accepted_at, tasks.completed_at
            from task_events
            join tasks on tasks.id = task_events.task_id
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
            select tasks.id, tasks.title, tasks.description, tasks.phone, tasks.address, tasks.price,
                   tasks.payment_method, tasks.status, tasks.created_at, tasks.decided_at,
                   tasks.accepted_at, tasks.completed_at,
                   tasks.assigned_to,
                   users.display_name as assigned_to_name,
                   users.login as assigned_to_login
            from tasks
            left join users on users.id = tasks.assigned_to
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
        active_payment_due = round(max(0, active_card_price - reserve_price), 2)
        totals = {
            "grossTotalPrice": gross_total_price,
            "totalPrice": total_price,
            "activePrice": active_price,
            "activeCardPrice": active_card_price,
            "activeCashPrice": active_cash_price,
            "activePaymentDue": active_payment_due,
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


def task_json(row, lang="ru"):
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
    return {
        "id": row["id"],
        "title": translate_text(row["title"], lang),
        "description": translate_text(row["description"], lang),
        "phone": row["phone"],
        "address": row["address"],
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
        "address": row["address"],
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
    return "Наличные" if normalize_payment_method(value) == "cash" else "Карта"


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
  }
</style>
<script id="server-language-script">
(function () {
  const languages = [
    ["en", "🇺🇸 English"],
    ["uk", "🇺🇦 Українська"],
    ["ru", "🇷🇺 Русский"],
    ["pl", "🇵🇱 Polski"]
  ];
  const dict = {
    en: {
      "Задания": "Tasks", "Сотрудники": "Employees", "Клиенты": "Clients", "Выполненные задания": "Completed tasks",
      "Расчеты сотрудников": "Employee payments", "Расчеты клиентов": "Client payments", "Настройки": "Settings",
      "Название задания": "Task name", "Название": "Name", "Задание": "Task", "Описание": "Description",
      "Номер телефона": "Phone number", "Номер": "Phone", "Телефона": "Number", "Телефон": "Phone",
      "Адрес": "Address", "Цена": "Price", "Карта": "Card", "Наличные": "Cash", "Оплата": "Payment",
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
      "Валюта": "Currency", "Показывать цены и суммы": "Show prices and amounts", "Телефон обратной связи": "Feedback phone",
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
      "Адрес": "Адреса", "Цена": "Ціна", "Карта": "Картка", "Наличные": "Готівка", "Оплата": "Оплата",
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
      "Валюта": "Валюта", "Показывать цены и суммы": "Показувати ціни та суми", "Телефон обратной связи": "Телефон зворотного зв'язку",
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
      "Адрес": "Adres", "Цена": "Cena", "Карта": "Karta", "Наличные": "Gotówka", "Оплата": "Płatność",
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
      "Валюта": "Waluta", "Показывать цены и суммы": "Pokazywać ceny i kwoty", "Телефон обратной связи": "Telefon kontaktowy",
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
    form { display: grid; gap: 10px; grid-template-columns: minmax(210px, 1.35fr) minmax(130px, .82fr) minmax(150px, 1fr) minmax(150px, 1fr) minmax(78px, 92px) minmax(120px, 140px) auto; align-items: start; margin-bottom: 24px; }
    input, textarea, select, button { font: inherit; padding: 11px 13px; border-radius: 8px; border: 1px solid rgba(23, 32, 38, 0.18); }
    input, textarea, select { background: rgba(255, 255, 255, 0.92); color: #60717d; min-width: 0; height: 88px; box-sizing: border-box; font-weight: 400; text-align: center; }
    textarea { resize: none; overflow: hidden; line-height: 1.25; padding-top: 27px; }
    input::placeholder, textarea::placeholder { color: #60717d; opacity: 1; font-weight: 400; }
    select { appearance: none; font-weight: 400; text-align-last: center; }
    select, select option { color: #60717d; font-weight: 400; }
    #price, #paymentMethod { width: 100%; }
    #price { text-align: right; }
    button { background: linear-gradient(135deg, var(--teal), #2563eb); color: white; border: 0; cursor: pointer; font-weight: 700; box-shadow: 0 10px 24px rgba(15, 118, 110, 0.22); }
    #form > button { height: 88px; display: flex; align-items: center; justify-content: center; text-align: center; }
    .secondary { background: #fff0bf; color: #4a3200; box-shadow: none; margin-top: 8px; }
    .restart { background: #16a34a; color: white; box-shadow: 0 10px 24px rgba(22, 163, 74, 0.22); margin-top: 8px; }
    .danger { background: #ef4444; color: white; box-shadow: none; margin-top: 8px; margin-left: 8px; }
    .editTask { display: grid; gap: 10px; grid-template-columns: repeat(4, minmax(120px, 1fr)) minmax(88px, .65fr) minmax(88px, .65fr) minmax(128px, .85fr); margin-top: 12px; }
    .editTask input, .editTask textarea, .editTask select, .editTask button { min-width: 0; width: 100%; box-sizing: border-box; }
    .editTask input, .editTask select { height: 88px; text-align: center; color: var(--ink); }
    .editTask select { text-align-last: left; appearance: auto; }
    .editTask button { height: 44px; margin: 0; }
    .editTaskActions { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; grid-column: 1 / -1; }
    nav { display: grid; grid-template-columns: repeat(7, minmax(112px, 1fr)); gap: 10px; margin-top: 12px; max-width: 1120px; }
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
    @media (max-width: 760px) { form, .editTask { grid-template-columns: 1fr; } .task h3 { padding-right: 0; } .taskDates { position: static; margin: 0 0 10px auto; } }
  </style>
</head>
<body class="locked">
  <header>
    <div class="language-corner">
      <select id="languageSelect" onchange="setLanguage(this.value)">
        <option value="en">🇺🇸 English</option>
        <option value="uk">🇺🇦 Українська</option>
        <option value="ru">🇷🇺 Русский</option>
        <option value="pl">🇵🇱 Polski</option>
      </select>
    </div>
    <h1 data-i18n="serverTitle">Задания</h1>
    <nav><a href="/server">Задания</a> <a href="/users">Сотрудники</a> <a href="/clients">Клиенты</a> <a href="/completed">Выполненные задания</a> <a href="/calculations">Расчеты сотрудников</a> <a href="/client-calculations">Расчеты клиентов</a> <a href="/settings">Настройки</a></nav>
  </header>
  <main>
    <form id="form">
      <textarea id="title" data-placeholder="taskTitle" placeholder="Название&#10;Задание" required></textarea>
      <input id="description" data-placeholder="description" placeholder="Описание">
      <textarea id="phone" placeholder="Номер&#10;Телефона" inputmode="tel"></textarea>
      <input id="address" data-placeholder="address" placeholder="Адрес">
      <input id="price" data-placeholder="price" placeholder="Цена" inputmode="decimal">
      <select id="paymentMethod">
        <option value="cash">Наличные</option>
        <option value="card">Карта</option>
      </select>
      <button data-i18n="add">Добавить</button>
    </form>
    <section id="tasks"></section>
  </main>
  <script>
    const tasks = document.querySelector("#tasks");
    let language = localStorage.getItem("language") || "ru";
    let appSettings = { currency: "RUB", showPrices: true };
    let clientOptions = [];
    const texts = {
      ru: { serverTitle: "Задания", openUsers: "Сотрудники", completedTasks: "Выполненные задания", calculations: "Расчеты", changePassword: "Изменить пароль", oldPassword: "Старый пароль", oldPasswordRepeat: "Повторите старый пароль", newPassword: "Введите новый пароль", passwordChanged: "Пароль изменен", changePasswordError: "Не удалось изменить пароль: ", confirmPassword: "Пароль подтверждения", taskTitle: "Название задания", description: "Описание", address: "Адрес", price: "Цена", add: "Добавить", employee: "сотрудник", delete: "Удалить", restart: "Начать заново", confirmDelete: "Удалить это задание?", resetError: "Не удалось вернуть задание: ", deleteError: "Не удалось удалить задание: ", createdAt: "Создано", acceptedAt: "Принято", completedAt: "Выполнено", new: "Новое", accepted: "Принято", declined: "Отклонено", completed: "Выполнено", refused: "Отказался" },
      en: { serverTitle: "Tasks", openUsers: "Employees", completedTasks: "Completed tasks", calculations: "Calculations", changePassword: "Change password", oldPassword: "Old password", oldPasswordRepeat: "Repeat old password", newPassword: "Enter new password", passwordChanged: "Password changed", changePasswordError: "Could not change password: ", confirmPassword: "Confirmation password", taskTitle: "Task name", description: "Description", address: "Address", price: "Price", add: "Add", employee: "employee", delete: "Delete", restart: "Start again", confirmDelete: "Delete this task?", resetError: "Could not return task: ", deleteError: "Could not delete task: ", createdAt: "Created", acceptedAt: "Accepted", completedAt: "Completed", new: "New", accepted: "Accepted", declined: "Declined", completed: "Completed", refused: "Refused" },
      uk: { serverTitle: "Завдання", openUsers: "Співробітники", completedTasks: "Виконані завдання", calculations: "Розрахунки", changePassword: "Змінити пароль", oldPassword: "Старий пароль", oldPasswordRepeat: "Повторіть старий пароль", newPassword: "Введіть новий пароль", passwordChanged: "Пароль змінено", changePasswordError: "Не вдалося змінити пароль: ", confirmPassword: "Пароль підтвердження", taskTitle: "Назва завдання", description: "Опис", address: "Адреса", price: "Ціна", add: "Додати", employee: "співробітник", delete: "Видалити", restart: "Почати заново", confirmDelete: "Видалити це завдання?", resetError: "Не вдалося повернути завдання: ", deleteError: "Не вдалося видалити завдання: ", createdAt: "Створено", acceptedAt: "Прийнято", completedAt: "Виконано", new: "Нове", accepted: "Прийнято", declined: "Відхилено", completed: "Виконано", refused: "Відмовився" },
      pl: { serverTitle: "Zadania", openUsers: "Pracownicy", completedTasks: "Wykonane zadania", calculations: "Rozliczenia", changePassword: "Zmień hasło", oldPassword: "Stare hasło", oldPasswordRepeat: "Powtórz stare hasło", newPassword: "Wpisz nowe hasło", passwordChanged: "Hasło zmienione", changePasswordError: "Nie udało się zmienić hasła: ", confirmPassword: "Hasło potwierdzenia", taskTitle: "Nazwa zadania", description: "Opis", address: "Adres", price: "Cena", add: "Dodaj", employee: "pracownik", delete: "Usuń", restart: "Zacznij od nowa", confirmDelete: "Usunąć to zadanie?", resetError: "Nie udało się przywrócić zadania: ", deleteError: "Nie udało się usunąć zadania: ", createdAt: "Utworzono", acceptedAt: "Przyjęto", completedAt: "Wykonano", new: "Nowe", accepted: "Przyjęte", declined: "Odrzucone", completed: "Wykonane", refused: "Odmówił" }
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
          <p><strong>Оплата:</strong> ${paymentMethodName(t.paymentMethod)}</p>
          <p class="meta"><strong>Источник:</strong> ${taskSource(t)}</p>
          <p class="meta"><span class="${statusClass(t.status)}">${statusName(t.status)}</span> ${assignedEmployeeName(t)}</p>
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
        return escapeHtml(task.clientName || task.sourceName || task.clientLogin || "Клиент");
      }
      return "Диспетчер";
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
      return method === "cash" ? "Наличные" : "Карта";
    }
    function assignedEmployeeName(task) {
      const parts = [task.assignedToName, task.assignedToLogin].filter(value => value && String(value).trim());
      return parts.length ? escapeHtml(parts.join(" ")) : "";
    }
    function clientSourceOptions(task) {
      const currentId = task.clientId || "";
      return `
        <option value="" ${currentId ? "" : "selected"}>Диспетчер</option>
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
          <input name="title" value="${escapeAttr(task.originalTitle || task.title || "")}" placeholder="${texts[language].taskTitle}" required>
          <input name="description" value="${escapeAttr(task.originalDescription || task.description || "")}" placeholder="${texts[language].description}">
          <input name="phone" value="${escapeAttr(task.originalPhone || task.phone || "")}" placeholder="Номер телефона" inputmode="tel">
          <input name="address" value="${escapeAttr(task.originalAddress || task.address || "")}" placeholder="${texts[language].address}">
          <input name="price" value="${escapeAttr(task.price || "")}" placeholder="${texts[language].price}" inputmode="decimal">
          <select name="paymentMethod">
            <option value="card" ${(task.paymentMethod || "card") === "card" ? "selected" : ""}>Карта</option>
            <option value="cash" ${task.paymentMethod === "cash" ? "selected" : ""}>Наличные</option>
          </select>
          <select name="clientId">
            ${clientSourceOptions(task)}
          </select>
          <div class="editTaskActions">
            <button type="submit">Сохранить</button>
            <button class="secondary" type="button" onclick="toggleTaskEdit(${task.id})">Отмена</button>
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
          address: form.address.value,
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
    document.querySelector("#form").addEventListener("submit", async event => {
      event.preventDefault();
      await fetch("/api/admin/tasks", {
        method: "POST",
        headers: adminHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ title: title.value, description: description.value, phone: phone.value, address: address.value, price: price.value, paymentMethod: paymentMethod.value })
      });
      title.value = "";
      description.value = "";
      phone.value = "";
      address.value = "";
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
      return new Intl.NumberFormat("ru-RU", { style: "currency", currency: appSettings.currency || "RUB" }).format(Number(value || 0));
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
    nav { display: grid; grid-template-columns: repeat(7, minmax(112px, 1fr)); gap: 10px; margin-top: 12px; max-width: 1120px; }
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
    <nav><a href="/server">Задания</a> <a href="/users">Сотрудники</a> <a href="/clients">Клиенты</a> <a href="/completed">Выполненные задания</a> <a href="/calculations">Расчеты сотрудников</a> <a href="/client-calculations">Расчеты клиентов</a> <a href="/settings">Настройки</a></nav>
  </header>
  <main>
    <section id="settlements"></section>
  </main>
  <script>
    const settlements = document.querySelector("#settlements");
    let appSettings = { currency: "RUB", showPrices: true };
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
      return new Intl.NumberFormat("ru-RU", { style: "currency", currency: appSettings.currency || "RUB" }).format(Number(value || 0));
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
          <h3>#${item.id} ${escapeHtml(item.displayName)} · ${formatDate(item.createdAt)}</h3>
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
        <p class="meta">Резерв: ${formatMoney(totals.reservePrice || 0)} · В резерв: ${formatMoney(totals.completedToReserve || 0)} · Из резерва в выплату: ${formatMoney(totals.reserveToCompleted || 0)} · Удержано из резерва: ${formatMoney(reserveDeductions)}</p>
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
        ${events.map(event => `<p class="meta">${formatDate(event.createdAt)} · ${reserveEventName(event.kind)} · ${formatMoney(event.absoluteAmount ?? Math.abs(event.amount || 0))}</p>`).join("")}
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
      return method === "cash" ? "Наличные" : "Карта";
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
    nav { display: grid; grid-template-columns: repeat(7, minmax(112px, 1fr)); gap: 10px; margin-top: 12px; max-width: 1120px; }
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
        <option value="en">🇺🇸 English</option>
        <option value="uk">🇺🇦 Українська</option>
        <option value="ru">🇷🇺 Русский</option>
        <option value="pl">🇵🇱 Polski</option>
      </select>
    </div>
    <h1 data-i18n="employees">Сотрудники</h1>
    <nav><a href="/server">Задания</a> <a href="/users">Сотрудники</a> <a href="/clients">Клиенты</a> <a href="/completed">Выполненные задания</a> <a href="/calculations">Расчеты сотрудников</a> <a href="/client-calculations">Расчеты клиентов</a> <a href="/settings">Настройки</a></nav>
  </header>
  <main>
    <button class="secondary" type="button" onclick="refreshUsersList()" data-i18n="refreshList">Обновить список</button>
    <form id="userForm">
      <input id="userDisplayName" data-placeholder="employeeName" placeholder="Имя сотрудника" required>
      <input id="userLogin" data-placeholder="login" placeholder="Логин" required>
      <input id="userPassword" data-placeholder="password" placeholder="Пароль" required>
      <button data-i18n="add">Добавить</button>
    </form>
    <button class="secondary" type="button" onclick="settleAllUsers()">Рассчитать всех</button>
    <section id="users"></section>
  </main>
  <script>
    const users = document.querySelector("#users");
    let editingUserId = null;
    let openReportId = null;
    let language = localStorage.getItem("language") || "ru";
    let appSettings = { currency: "RUB", showPrices: true };
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
      document.querySelectorAll("[data-i18n]").forEach(el => el.textContent = texts[language][el.dataset.i18n]);
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
              <div class="meta">${texts[language].login}: ${escapeHtml(u.login)}${u.phone ? " · Телефон: " + escapeHtml(u.phone) : ""} · ${texts[language].tasks}: ${u.taskCount}</div>
            </div>
            <div class="userActions">
              ${appSettings.showPrices ? `<div class="userMoneyMini"><strong>${formatMoney(u.payoutPrice ?? u.totals?.payoutPrice ?? 0)}</strong><span>${texts[language].payout}</span></div>` : ""}
              ${appSettings.showPrices ? `<div class="userMoneyMini"><strong>${formatMoney(u.reservePrice ?? u.totals?.reservePrice ?? 0)}</strong><span>${texts[language].reserve}</span></div>` : ""}
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
          <strong>${formatMoney(reserve)}</strong>
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
            <p class="meta">${formatDate(event.createdAt)} · ${reserveEventName(event.kind)} · ${formatMoney(event.absoluteAmount ?? Math.abs(event.amount || 0))}</p>
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
              <summary>${texts[language].settlement} #${item.id} · ${formatDate(item.createdAt)} · ${formatMoney(item.totals?.payoutPrice ?? item.totals?.completedPrice ?? 0)}</summary>
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
              <div class="meta">${texts[language].status}: ${statusName(task.status)} · ${taskDatesMeta(task)}</div>
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
      return rows.length ? rows.join(" · ") : texts[language].noDate;
    }
    function formatDate(value) {
      if (!value) {
        return texts[language].noDate;
      }
      return new Date(value * 1000).toLocaleString("ru-RU");
    }
    function formatMoney(value) {
      return new Intl.NumberFormat("ru-RU", { style: "currency", currency: appSettings.currency || "RUB" }).format(Number(value || 0));
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
      return method === "cash" ? "Наличные" : "Карта";
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
    nav { display: grid; grid-template-columns: repeat(7, minmax(112px, 1fr)); gap: 10px; margin-top: 12px; max-width: 1120px; }
    nav a { display: flex; align-items: center; justify-content: center; min-height: 44px; box-sizing: border-box; color: var(--ink); background: var(--gold); font-weight: bold; padding: 8px 10px; border-radius: 8px; text-align: center; line-height: 1.15; text-decoration: none; }
    @media (max-width: 980px) { nav { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
    @media (max-width: 760px) { form, .editForm, .reportStats { grid-template-columns: 1fr; } .clientHeader { display: block; } .actions { justify-content: flex-start; margin-top: 10px; } }
  </style>
</head>
<body class="locked">
  <header>
    <h1>Клиенты</h1>
    <nav><a href="/server">Задания</a> <a href="/users">Сотрудники</a> <a href="/clients">Клиенты</a> <a href="/completed">Выполненные задания</a> <a href="/calculations">Расчеты сотрудников</a> <a href="/client-calculations">Расчеты клиентов</a> <a href="/settings">Настройки</a></nav>
  </header>
  <main>
    <button class="secondary" type="button" onclick="refreshClientsList()">Обновить список</button>
    <form id="clientForm">
      <input id="clientDisplayName" placeholder="Имя клиента" required>
      <input id="clientLogin" placeholder="Логин" required>
      <input id="clientPassword" placeholder="Пароль" required>
      <button>Добавить</button>
    </form>
    <button class="secondary" type="button" onclick="settleAllClients()">Рассчитать всех</button>
    <section id="clients"></section>
  </main>
  <script>
    const clients = document.querySelector("#clients");
    let editingClientId = null;
    let openReportId = null;
    let appSettings = { currency: "RUB", showPrices: true };
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
    function formatMoney(value) {
      return new Intl.NumberFormat("ru-RU", { style: "currency", currency: appSettings.currency || "RUB" }).format(Number(value || 0));
    }
    function formatDate(value) {
      if (!value) return "нет даты";
      return new Date(value * 1000).toLocaleString("ru-RU");
    }
    function phoneHref(value) {
      return String(value).replace(/[^\d+]/g, "");
    }
    function paymentMethodName(method) {
      return method === "cash" ? "Наличные" : "Карта";
    }
    function taskStatusName(status) {
      const names = { completed: "Выполнено", refused: "Отказался", accepted: "Принято", declined: "Отклонено", new: "Новое" };
      return names[status] || status || "";
    }
    function taskDatesMeta(task) {
      const rows = [];
      if (task.createdAt) rows.push("Создано: " + formatDate(task.createdAt));
      if (task.acceptedAt) rows.push("Принято: " + formatDate(task.acceptedAt));
      if (task.completedAt) rows.push("Выполнено: " + formatDate(task.completedAt));
      return rows.length ? rows.join(" · ") : "нет даты";
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
              <div class="meta">Логин: ${escapeHtml(client.login)}${client.phone ? " · Телефон: " + escapeHtml(client.phone) : ""} · Заданий: ${client.taskCount}</div>
            </div>
            <div class="actions">
              <div class="clientMoneyMini"><strong>${formatMoney(client.totalPrice || 0)}</strong><span>Сумма к оплате</span></div>
              <div class="clientMoneyMini"><strong>${formatMoney(client.reservePrice || 0)}</strong><span>Резерв</span></div>
              <button class="secondary" type="button" onclick="toggleClientReport(${client.id})">Отчет</button>
              <button class="secondary" type="button" onclick="settleClientFromList(${client.id})">Рассчитать</button>
              <button class="secondary" type="button" onclick="toggleEdit(${client.id})">Редактировать</button>
            </div>
          </div>
          <form class="editForm" id="edit-${client.id}" style="display:none" onsubmit="saveClient(event, ${client.id})">
            <input name="displayName" value="${escapeAttr(client.displayName)}" placeholder="Имя клиента" required>
            <input name="login" value="${escapeAttr(client.login)}" placeholder="Логин" required>
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
        ${renderClientTaskSection("Выполненные работы", report.completed || [], "completed")}
        ${renderClientTaskSection("В работе", report.active || [], "active")}
        ${renderClientTaskSection("Новые работы", report.new || [], "new")}
        ${renderClientTaskSection("Отказанные работы", report.refused || [], "refused")}
        ${renderClientTaskSection("Остальные работы", report.other || [], "other")}
      `;
    }
    function renderClientReserveBox(report) {
      const reserve = report.totals.reservePrice ?? 0;
      return `
        <div class="statBox reserveBox">
          <strong>${formatMoney(reserve)}</strong>
          <div class="reserveControls">
            <button class="secondary" type="button" disabled title="Временно отключено">-</button>
            <span>Резерв</span>
            <button class="secondary" type="button" disabled title="Временно отключено">+</button>
          </div>
        </div>
      `;
    }
    function renderClientReserveTopUp(clientId) {
      return `
        <section class="reportSection">
          <h4>Пополнить резерв</h4>
          <form onsubmit="clientReserveTopUp(event, ${clientId})">
            <input name="amount" type="number" min="0.01" step="0.01" placeholder="Сумма" required>
            <button type="submit">Пополнить</button>
          </form>
        </section>
      `;
    }
    function clientReserveEventName(kind) {
      if (kind === "to_reserve") return "Из суммы к оплате в резерв";
      if (kind === "from_reserve") return "Из резерва в сумму к оплате";
      if (kind === "top_up") return "Пополнение резерва";
      if (kind === "completed_from_reserve") return "Выполненные работы из резерва";
      return kind;
    }
    function renderClientReserveEvents(events) {
      if (!events.length) {
        return "";
      }
      return `
        <section class="reportSection">
          <h4>Операции резерва</h4>
          ${events.map(event => `
            <p class="meta">${formatDate(event.createdAt)} · ${clientReserveEventName(event.kind)} · ${formatMoney(event.absoluteAmount ?? Math.abs(event.amount || 0))}</p>
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
        return `<section class="reportSection"><h4>${title}</h4><p class="meta">Нет записей.</p></section>`;
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
              <p>${task.address ? "<strong>Адрес:</strong> " + escapeHtml(task.address) : ""}</p>
              <p>${task.assignedToName ? "<strong>Кто принял:</strong> " + escapeHtml(task.assignedToName) + (task.assignedToLogin ? " · " + escapeHtml(task.assignedToLogin) : "") : ""}</p>
              <p>${appSettings.showPrices ? "<strong>Цена:</strong> " + formatMoney(task.price || 0) : ""}</p>
              <p><strong>Оплата:</strong> ${paymentMethodName(task.paymentMethod)}</p>
              <div class="meta">Статус: ${taskStatusName(task.status)} · ${taskDatesMeta(task)}</div>
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
    nav { display: grid; grid-template-columns: repeat(7, minmax(112px, 1fr)); gap: 10px; margin-top: 12px; max-width: 1120px; }
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
    <nav><a href="/server">Задания</a> <a href="/users">Сотрудники</a> <a href="/clients">Клиенты</a> <a href="/completed">Выполненные задания</a> <a href="/calculations">Расчеты сотрудников</a> <a href="/client-calculations">Расчеты клиентов</a> <a href="/settings">Настройки</a></nav>
  </header>
  <main>
    <section id="items"></section>
  </main>
  <script>
    const items = document.querySelector("#items");
    let appSettings = { currency: "RUB", showPrices: true };
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
    function formatMoney(value) {
      return new Intl.NumberFormat("ru-RU", { style: "currency", currency: appSettings.currency || "RUB" }).format(Number(value || 0));
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
    async function loadCalculations() {
      const res = await fetch("/api/admin/client-calculations", { headers: adminHeaders() });
      if (!res.ok) { items.innerHTML = "<p>Не удалось загрузить расчеты клиентов.</p>"; return; }
      const data = await res.json();
      const history = (data.settlements || []).map(item => `
        <article class="item${calculationClass(item)}">
          <h3>#${item.id} ${escapeHtml(item.displayName)} · ${formatDate(item.createdAt)}</h3>
          <p class="status">Статус расчета: ${calculationStatus(item)}</p>
          <p class="meta">Выполнено: ${item.counts.completed || 0} · Отказался: ${item.counts.refused || 0} · В работе: ${item.counts.active || 0} · Всего заданий: ${item.counts.all || 0}</p>
          <p>${appSettings.showPrices ? "<strong>Сумма расчета:</strong> " + formatMoney(item.totals.totalPrice || 0) : ""}</p>
          ${renderClientReport(item)}
          <div class="actions">${calculateClientSettlementButton(item)}<button class="danger" type="button" onclick="deleteClientSettlement(${item.id})">Удалить расчет</button></div>
        </article>
      `).join("");
      items.innerHTML = history || "<p class=\"meta\">Созданных расчетов пока нет. Новый расчет создается в разделе «Клиенты» кнопкой «Рассчитать».</p>";
    }
    function renderClientReport(item) {
      return `
        <details>
          <summary>Полный отчет по расчету</summary>
          ${renderTaskSection("Выполненные работы", item.completed || [], "completed")}
          ${renderTaskSection("Активные работы", item.active || [], "active")}
          ${renderTaskSection("Новые работы", item.new || [], "new")}
          ${renderTaskSection("Отказанные работы", item.refused || [], "refused")}
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
      return `
        <h4>Резерв в расчете</h4>
        <p class="meta">Резерв до расчета: ${formatMoney(totals.reserveBeforeCompleted || 0)} · Списано из резерва: ${formatMoney(totals.reserveUsedForCompleted || 0)} · Остаток резерва: ${formatMoney(totals.reservePrice || 0)} · Сумма к оплате: ${formatMoney(totals.totalPrice || 0)}</p>
      `;
    }
    function reserveEventName(kind) {
      if (kind === "to_reserve") return "Из суммы к оплате в резерв";
      if (kind === "from_reserve") return "Из резерва в сумму к оплате";
      if (kind === "top_up") return "Пополнение резерва";
      if (kind === "completed_from_reserve") return "Выполненные работы из резерва";
      return kind;
    }
    function renderReserveEvents(events) {
      if (!events.length) {
        return `
          <h4>Операции резерва</h4>
          <p class="meta">Нет операций резерва.</p>
        `;
      }
      return `
        <h4>Операции резерва</h4>
        ${events.map(event => `
          <p class="meta">${formatDate(event.createdAt)} · ${reserveEventName(event.kind)} · ${formatMoney(event.absoluteAmount ?? Math.abs(event.amount || 0))}</p>
        `).join("")}
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
            <p>${task.assignedToName ? "<strong>Кто принял:</strong> " + escapeHtml(task.assignedToName) + (task.assignedToLogin ? " · " + escapeHtml(task.assignedToLogin) : "") : ""}</p>
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
      return method === "cash" ? "Наличные" : "Карта";
    }
    function taskDatesMeta(task) {
      const rows = [];
      if (task.createdAt) rows.push("Создано: " + formatDate(task.createdAt));
      if (task.acceptedAt) rows.push("Принято: " + formatDate(task.acceptedAt));
      if (task.completedAt) rows.push("Выполнено: " + formatDate(task.completedAt));
      return rows.length ? rows.join(" · ") : "нет даты";
    }
    function formatDate(value) {
      return new Date(value * 1000).toLocaleString("ru-RU");
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
  </script>
</body>
</html>"""


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
    nav { display: grid; grid-template-columns: repeat(7, minmax(112px, 1fr)); gap: 10px; margin-top: 12px; max-width: 1120px; }
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
    <nav><a href="/server">Задания</a> <a href="/users">Сотрудники</a> <a href="/clients">Клиенты</a> <a href="/completed">Выполненные задания</a> <a href="/calculations">Расчеты сотрудников</a> <a href="/client-calculations">Расчеты клиентов</a> <a href="/settings">Настройки</a></nav>
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
      <label>
        <input id="showPrices" type="checkbox">
        Показывать цены и суммы
      </label>
      <label>Процент с выполненных работ, который мы удерживаем себе
        <input id="completedFeePercent" type="number" min="0" max="100" step="0.1">
      </label>
      <label>Процент с отказанных или отмененных работ, который мы удерживаем себе
        <input id="refusedFeePercent" type="number" min="0" max="100" step="0.1">
      </label>
      <label>Сколько дней хранить выполненное задание
        <input id="completedTasksRetentionDays" type="number" min="1" max="1000" step="1">
      </label>
      <label>Сколько дней хранить расчеты сотрудников
        <input id="employeeSettlementsRetentionDays" type="number" min="1" max="1000" step="1">
      </label>
      <label>Сколько дней хранить расчеты клиентов
        <input id="clientSettlementsRetentionDays" type="number" min="1" max="1000" step="1">
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
      currency.value = settings.currency || "RUB";
      showPrices.checked = settings.showPrices !== false;
      completedFeePercent.value = settings.completedFeePercent ?? 1;
      refusedFeePercent.value = settings.refusedFeePercent ?? 1;
      completedTasksRetentionDays.value = settings.completedTasksRetentionDays ?? 365;
      employeeSettlementsRetentionDays.value = settings.employeeSettlementsRetentionDays ?? 365;
      clientSettlementsRetentionDays.value = settings.clientSettlementsRetentionDays ?? 365;
      feedbackPhone.value = settings.feedbackPhone || "";
      feedbackEmail.value = settings.feedbackEmail || "";
      feedbackAddress.value = settings.feedbackAddress || "";
      feedbackTelegram.value = settings.feedbackTelegram || "";
      feedbackWhatsApp.value = settings.feedbackWhatsApp || "";
    }
    document.querySelector("#settingsForm").addEventListener("submit", async event => {
      event.preventDefault();
      const res = await fetch("/api/admin/settings", {
        method: "POST",
        headers: adminHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({
          currency: currency.value,
          showPrices: showPrices.checked,
          completedFeePercent: completedFeePercent.value,
          refusedFeePercent: refusedFeePercent.value,
          completedTasksRetentionDays: completedTasksRetentionDays.value,
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


if __name__ == "__main__":
    init_db()
    cleanup_expired_data()
    start_cleanup_worker()
    print(f"Task server started: http://localhost:{PORT}")
    print(f"Admin password: {ADMIN_PASSWORD}")
    ThreadingHTTPServer((HOST, PORT), App).serve_forever()

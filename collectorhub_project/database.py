"""
CollectorHub - database.py
SQLite база CollectorHub.

v9:
- ключевые слова и исключения берутся ТОЛЬКО из TXT-файлов;
- добавлен метод get_last_post_for_test() для кнопки тестовой повторной отправки.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from words_manager import sync_words_to_database

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "collector.db"


class Database:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row

    def initialize(self):
        cur = self.conn.cursor()

        cur.execute("""
        CREATE TABLE IF NOT EXISTS facebook_groups(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            group_id TEXT,
            url TEXT,
            enabled INTEGER DEFAULT 1
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS keywords(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            word TEXT UNIQUE NOT NULL
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS exclusions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            word TEXT UNIQUE NOT NULL
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS posts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            facebook_post_id TEXT UNIQUE,
            author TEXT,
            author_profile TEXT,
            phone TEXT,
            text TEXT,
            post_url TEXT,
            created_at TEXT,
            sent_to_telegram INTEGER DEFAULT 0,
            published_to_facebook INTEGER DEFAULT 0
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            level TEXT,
            message TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)

        self.conn.commit()
        self._migrate()

    def _migrate(self):
        columns = self._columns("posts")
        migrations = {
            "group_url": "ALTER TABLE posts ADD COLUMN group_url TEXT",
            "accepted": "ALTER TABLE posts ADD COLUMN accepted INTEGER DEFAULT 0",
            "matched_keywords": "ALTER TABLE posts ADD COLUMN matched_keywords TEXT",
            "matched_exclusions": "ALTER TABLE posts ADD COLUMN matched_exclusions TEXT",
            "updated_at": "ALTER TABLE posts ADD COLUMN updated_at DATETIME",
            "facebook_publish_status": "ALTER TABLE posts ADD COLUMN facebook_publish_status TEXT",
            "facebook_publish_detail": "ALTER TABLE posts ADD COLUMN facebook_publish_detail TEXT",
        }

        for column, sql in migrations.items():
            if column not in columns:
                try:
                    self.conn.execute(sql)
                    self.conn.commit()
                except sqlite3.OperationalError:
                    pass

    def _columns(self, table: str) -> set[str]:
        cur = self.conn.execute(f"PRAGMA table_info({table})")
        return {row["name"] for row in cur.fetchall()}

    def log(self, level: str, message: str):
        self.conn.execute(
            "INSERT INTO logs(level, message) VALUES(?, ?)",
            (level, message),
        )
        self.conn.commit()

    def seed_words(self):
        sync_words_to_database(self.conn)

    def save_post(self, post: dict) -> tuple[bool, int | None]:
        text = (post.get("text") or "").strip()
        author = (post.get("author") or "Unknown").strip()
        unique_key = (post.get("facebook_post_id") or f"{author}|{text[:180]}").strip()

        if not text:
            return False, None

        exists = self.conn.execute(
            "SELECT id FROM posts WHERE facebook_post_id=? LIMIT 1",
            (unique_key,),
        ).fetchone()

        if exists:
            return False, exists["id"]

        cur = self.conn.execute(
            """
            INSERT INTO posts(
                facebook_post_id,
                author,
                author_profile,
                phone,
                text,
                post_url,
                group_url,
                created_at,
                accepted,
                matched_keywords,
                matched_exclusions
            )
            VALUES(?,?,?,?,?,?,?,datetime('now'),?,?,?)
            """,
            (
                unique_key,
                author,
                post.get("author_profile"),
                post.get("phone") or ", ".join(post.get("phones", [])),
                text,
                post.get("post_url"),
                post.get("group_url"),
                1 if post.get("accepted") else 0,
                ", ".join(post.get("matched_keywords", [])),
                ", ".join(post.get("matched_exclusions", [])),
            ),
        )
        self.conn.commit()
        return True, cur.lastrowid

    def get_unsent_accepted_posts(self, limit: int = 20):
        cur = self.conn.execute(
            """
            SELECT *
            FROM posts
            WHERE accepted=1 AND sent_to_telegram=0
            ORDER BY id ASC
            LIMIT ?
            """,
            (limit,),
        )
        return cur.fetchall()

    def get_last_post_for_test(self):
        """
        Для временной кнопки теста оформления.
        Берём последнее подходящее объявление, а если такого нет — просто последний сохранённый пост.
        """
        row = self.conn.execute(
            """
            SELECT *
            FROM posts
            WHERE accepted=1
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

        if row:
            return row

        return self.conn.execute(
            """
            SELECT *
            FROM posts
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

    def mark_sent_to_telegram(self, post_id: int):
        self.conn.execute(
            "UPDATE posts SET sent_to_telegram=1, updated_at=datetime('now') WHERE id=?",
            (post_id,),
        )
        self.conn.commit()

    def get_unpublished_accepted_posts(self, limit: int = 20):
        cur = self.conn.execute(
            """
            SELECT *
            FROM posts
            WHERE accepted=1 AND published_to_facebook=0
            ORDER BY id ASC
            LIMIT ?
            """,
            (limit,),
        )
        return cur.fetchall()

    def mark_published_to_facebook(self, post_id: int, detail: str = ""):
        self.conn.execute(
            """
            UPDATE posts
            SET published_to_facebook=1,
                facebook_publish_status='sent',
                facebook_publish_detail=?,
                updated_at=datetime('now')
            WHERE id=?
            """,
            (detail, post_id),
        )
        self.conn.commit()

    def mark_facebook_publish_failed(self, post_id: int, detail: str = ""):
        self.conn.execute(
            """
            UPDATE posts
            SET facebook_publish_status='failed',
                facebook_publish_detail=?,
                updated_at=datetime('now')
            WHERE id=?
            """,
            (detail, post_id),
        )
        self.conn.commit()

    def close(self):
        self.conn.close()


if __name__ == "__main__":
    db = Database()
    db.initialize()
    db.seed_words()
    db.log("INFO", "Database initialized")
    print("OK")

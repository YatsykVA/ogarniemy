from __future__ import annotations

import argparse
import os
import sqlite3
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(
    os.environ.get(
        "OGARNIEMY_DATA_DIR",
        os.environ.get("TASK_DATA_DIR", ROOT.parent / "ogarniemy_data"),
    )
).resolve()
DB_PATH = Path(os.environ.get("TASK_DB_PATH", DATA_DIR / "server.db")).resolve()


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        create table if not exists facebook_targets (
          id integer primary key autoincrement,
          name text not null,
          city text default '',
          target_id text default '',
          notes text default '',
          enabled integer default 1,
          created_at integer not null,
          keywords text default '',
          action text default 'same_group',
          response_message_id integer
        );

        create table if not exists facebook_keyword_hits (
          id integer primary key autoincrement,
          target_id text default '',
          target_name text default '',
          keyword text not null,
          username text default '',
          message text default '',
          action text default 'collector',
          source_url text default '',
          created_at integer not null
        );

        create table if not exists facebook_collector_posts (
          id integer primary key autoincrement,
          facebook_post_id text unique,
          target_id text default '',
          target_name text default '',
          author text default '',
          author_profile text default '',
          phone text default '',
          message text not null,
          post_url text default '',
          matched_keywords text default '',
          matched_exclusions text default '',
          accepted integer default 1,
          sent_to_telegram integer default 0,
          published_to_facebook integer default 0,
          created_at integer not null,
          updated_at integer
        );
        """
    )


def split_group_line(line: str) -> tuple[str, str] | None:
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    if "|" in text:
        name, url = [part.strip() for part in text.rsplit("|", 1)]
    else:
        name, url = text, text
    if not url:
        return None
    return name or url, url


def import_groups(conn: sqlite3.Connection, groups_file: Path) -> int:
    if not groups_file.exists():
        return 0
    now = int(time.time())
    imported = 0
    for raw_line in groups_file.read_text(encoding="utf-8-sig").splitlines():
        parsed = split_group_line(raw_line)
        if not parsed:
            continue
        name, url = parsed
        existing = conn.execute(
            "select id from facebook_targets where target_id = ?",
            (url,),
        ).fetchone()
        if existing:
            conn.execute(
                "update facebook_targets set name = coalesce(nullif(name, ''), ?), enabled = 1 where id = ?",
                (name, existing["id"]),
            )
        else:
            conn.execute(
                """
                insert into facebook_targets(name, target_id, notes, keywords, action, enabled, created_at)
                values(?, ?, ?, '', 'same_group', 1, ?)
                """,
                (name, url, "Imported from CollectorHub", now),
            )
            imported += 1
    return imported


def import_posts(conn: sqlite3.Connection, collector_db: Path) -> int:
    if not collector_db.exists():
        return 0
    source = sqlite3.connect(collector_db)
    source.row_factory = sqlite3.Row
    try:
        rows = source.execute("select * from posts order by id").fetchall()
    except sqlite3.Error:
        source.close()
        return 0
    imported = 0
    now = int(time.time())
    for row in rows:
        message = (row["text"] if "text" in row.keys() else "") or ""
        if not message.strip():
            continue
        facebook_post_id = (row["facebook_post_id"] if "facebook_post_id" in row.keys() else "") or f"collector:{row['id']}"
        target_id = (row["group_url"] if "group_url" in row.keys() else "") or ""
        keywords = (row["matched_keywords"] if "matched_keywords" in row.keys() else "") or ""
        exclusions = (row["matched_exclusions"] if "matched_exclusions" in row.keys() else "") or ""
        created_at = now
        conn.execute(
            """
            insert into facebook_collector_posts(
              facebook_post_id, target_id, author, author_profile, phone, message, post_url,
              matched_keywords, matched_exclusions, accepted, sent_to_telegram, published_to_facebook, created_at
            )
            values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(facebook_post_id) do update set
              target_id = excluded.target_id,
              author = excluded.author,
              author_profile = excluded.author_profile,
              phone = excluded.phone,
              message = excluded.message,
              post_url = excluded.post_url,
              matched_keywords = excluded.matched_keywords,
              matched_exclusions = excluded.matched_exclusions,
              updated_at = ?
            """,
            (
                facebook_post_id,
                target_id,
                (row["author"] if "author" in row.keys() else "") or "",
                (row["author_profile"] if "author_profile" in row.keys() else "") or "",
                (row["phone"] if "phone" in row.keys() else "") or "",
                message,
                (row["post_url"] if "post_url" in row.keys() else "") or "",
                keywords,
                exclusions,
                int((row["accepted"] if "accepted" in row.keys() else 1) or 0),
                int((row["sent_to_telegram"] if "sent_to_telegram" in row.keys() else 0) or 0),
                int((row["published_to_facebook"] if "published_to_facebook" in row.keys() else 0) or 0),
                created_at,
                now,
            ),
        )
        if keywords:
            first_keyword = keywords.split(",", 1)[0].strip()
            conn.execute(
                """
                insert into facebook_keyword_hits(target_id, target_name, keyword, username, message, action, source_url, created_at)
                values(?, '', ?, ?, ?, 'collector_import', ?, ?)
                """,
                (
                    target_id,
                    first_keyword,
                    (row["author"] if "author" in row.keys() else "") or "",
                    message,
                    (row["post_url"] if "post_url" in row.keys() else "") or "",
                    created_at,
                ),
            )
        imported += 1
    source.close()
    return imported


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default=r"C:\Users\Viktor\Desktop\Проект")
    parser.add_argument("--db", default=str(DB_PATH))
    args = parser.parse_args()

    project = Path(args.project)
    target_db = Path(args.db)
    with connect(target_db) as conn:
        ensure_tables(conn)
        group_count = import_groups(conn, project / "data" / "facebook_groups.txt")
        post_count = import_posts(conn, project / "data" / "collector.db")
    print(f"Imported CollectorHub data into {target_db}: groups={group_count}, posts={post_count}")


if __name__ == "__main__":
    main()

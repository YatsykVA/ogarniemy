"""
CollectorHub - reset_posts_db.py
Очищает только сохранённые посты.

Группы, ключевые слова, исключения и настройки НЕ трогает.
"""

from database import Database
from logger import info


def main():
    db = Database()
    db.initialize()

    db.conn.execute("DELETE FROM posts")
    db.conn.commit()

    info("Posts database cleared")
    print("OK: posts database cleared")


if __name__ == "__main__":
    main()

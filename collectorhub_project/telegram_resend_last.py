"""
CollectorHub - telegram_resend_last.py
Временный тест оформления Telegram.

Отправляет последнее сохранённое объявление из базы ещё раз.
Позже эту кнопку и файл можно удалить.
"""

from database import Database
from telegram_sender import TelegramSender
from logger import info, warning, error


def main():
    db = Database()
    db.initialize()

    post = db.get_last_post_for_test()
    if not post:
        warning("В базе пока нет постов для тестовой отправки.")
        print("ERROR: no posts in database")
        raise SystemExit(1)

    sender = TelegramSender()
    ok = sender.send(dict(post))

    if ok:
        info(f"Last post resent to Telegram for format test. Post id: {post['id']}")
        print("OK: last post resent to Telegram")
    else:
        error("Failed to resend last post to Telegram")
        print("ERROR: failed to resend last post to Telegram")
        raise SystemExit(1)


if __name__ == "__main__":
    main()

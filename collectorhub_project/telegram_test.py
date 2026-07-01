"""
CollectorHub - telegram_test.py
Проверка отправки тестового сообщения в Telegram.
"""

from telegram_sender import TelegramSender
from logger import info, error


def main():
    sender = TelegramSender()

    test_post = {
        "author": "CollectorHub Test",
        "phone": "+48 000 000 000",
        "text": "✅ Тестовое сообщение CollectorHub. Если ты это видишь — Telegram настроен правильно.",
        "post_url": "https://facebook.com/",
        "author_profile": "https://facebook.com/",
    }

    ok = sender.send(test_post)

    if ok:
        info("Telegram test message sent successfully")
        print("OK: Telegram test message sent")
    else:
        error("Telegram test message failed")
        print("ERROR: Telegram test message failed")
        raise SystemExit(1)


if __name__ == "__main__":
    main()

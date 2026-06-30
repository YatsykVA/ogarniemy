"""
CollectorHub - facebook_login.py
Отдельный вход в Facebook.

Этот процесс просто открывает Chrome и держит его открытым.
Когда пользователь нажимает "Я вошёл в Facebook" в GUI, main.py мягко завершает процесс.
"""

import time
from facebook_session import FacebookSession
from logger import info, error


def main():
    fb = FacebookSession()

    try:
        info("=== Facebook login started ===")
        fb.open_facebook_login_page()
        info("Chrome открыт. Войдите в Facebook, затем нажмите кнопку 'Я вошёл в Facebook' в CollectorHub.")

        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        info("Facebook login stopped by user")

    except Exception as exc:
        error(f"Facebook login crashed: {exc}")
        raise

    finally:
        fb.stop()
        info("=== Facebook login finished ===")


if __name__ == "__main__":
    main()

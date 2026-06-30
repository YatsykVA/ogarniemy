"""
CollectorHub - app.py
Запуск коллектора.
"""

from database import Database
from groups_manager import GroupsManager
from collector import FacebookCollector
from filters import Filters
from telegram_sender import TelegramSender
from logger import info, error


class CollectorApp:
    def __init__(self):
        self.db = Database()
        self.db.initialize()
        self.db.seed_words()

        self.groups = GroupsManager()
        self.groups.seed()

        self.filters = Filters()
        self.telegram = TelegramSender()
        self.collector = FacebookCollector()

    def run_once(self):
        info("=== CollectorHub started ===")

        try:
            self.collector.start(headless=False)
            self.collector.collect()

        except KeyboardInterrupt:
            info("Collector stopped by user")

        except Exception as exc:
            error(f"Collector crashed: {exc}")
            raise

        finally:
            try:
                self.collector.stop()
            finally:
                info("=== CollectorHub finished ===")


if __name__ == "__main__":
    app = CollectorApp()
    app.run_once()

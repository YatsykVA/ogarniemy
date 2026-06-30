"""
CollectorHub - collector.py
Сборщик Facebook-групп.

v5:
- открывает группу;
- сразу читает посты;
- сразу ищет ключевые слова;
- не делает второй пустой проход по группам;
- отправляет Telegram после обработки группы.
"""

from database import Database
from groups_manager import GroupsManager
from facebook_session import FacebookSession
from parser import FacebookParser
from telegram_sender import TelegramSender
from facebook_publisher import FacebookPublisher
from config import load_config
from logger import info, warning


class FacebookCollector:
    def __init__(self):
        self.db = Database()
        self.db.initialize()
        self.db.seed_words()

        self.cfg = load_config()

        self.groups = GroupsManager()
        self.groups.seed()

        self.fb = FacebookSession()
        self.parser = FacebookParser(self.db)
        self.telegram = TelegramSender()
        self.facebook_publisher = FacebookPublisher(self.fb)

        self.running = False

    def start(self, headless=False):
        self.running = True
        info("Collector started")
        self.fb.start(headless=headless)

    def collect(self):
        enabled = self.groups.get_enabled()

        if not enabled:
            warning("Нет включенных групп.")
            return

        total = len(enabled)
        opened = 0
        skipped = 0
        found_total = 0
        saved_total = 0
        duplicate_total = 0
        accepted_total = 0
        sent_total = 0
        facebook_sent_total = 0

        max_posts = int(getattr(self.cfg, "max_posts_per_group", 100) or 100)

        info(f"Начинаю проверку групп: {total}. Постов на группу: до {max_posts} / режим: новые публикации")

        for index, row in enumerate(enabled, start=1):
            if not self.running:
                warning("Collector остановлен пользователем.")
                break

            try:
                url = row["url"]
                gid = row["group_id"]
                group_name = row["name"]
            except Exception:
                group_name = row[1] if len(row) > 1 else "Unknown"
                gid = row[2] if len(row) > 2 else ""
                url = row[3] if len(row) > 3 else ""

            if url:
                target = url
            elif gid:
                target = f"https://www.facebook.com/groups/{gid}"
            else:
                skipped += 1
                continue

            info(f"[{index}/{total}] Проверяю группу: {group_name}")

            ok = self.fb.open_group(target)
            if not ok:
                skipped += 1
                if not self.fb.is_alive():
                    warning("Браузер закрыт. Дальше идти по группам невозможно.")
                    break
                continue

            opened += 1

            stats = self.parser.parse_current_page(
                self.fb.page,
                group_url=target,
                max_posts=max_posts,
            )

            found_total += stats["found"]
            saved_total += stats["saved"]
            duplicate_total += stats.get("duplicates", 0)
            accepted_total += stats["accepted"]

            sent_total += self.send_unsent_to_telegram(limit=20)
            facebook_sent_total += self.send_unsent_to_facebook(limit=20)

        info(
            "Итог Collector: "
            f"групп открыто {opened}, пропущено {skipped}, "
            f"постов найдено {found_total}, новых {saved_total}, дублей {duplicate_total}, "
            f"подошло по ключам {accepted_total}, отправлено Telegram {sent_total}, отправлено Facebook {facebook_sent_total}"
        )

    def _send_destination(self) -> str:
        value = (getattr(self.cfg, "send_destination", "telegram") or "telegram").lower().strip()
        if value not in {"telegram", "facebook", "both"}:
            return "telegram"
        return value

    def send_unsent_to_telegram(self, limit: int = 20) -> int:
        if self._send_destination() not in {"telegram", "both"}:
            return 0

        sent = 0
        posts = self.db.get_unsent_accepted_posts(limit=limit)

        for post in posts:
            if self.telegram.send(dict(post)):
                self.db.mark_sent_to_telegram(post["id"])
                sent += 1

        if posts and sent == 0:
            warning("Есть подходящие посты, но они не отправлены в Telegram. Проверь TELEGRAM_BOT_TOKEN и TELEGRAM_GROUP_ID.")

        return sent

    def send_unsent_to_facebook(self, limit: int = 20) -> int:
        if self._send_destination() not in {"facebook", "both"}:
            return 0

        target = (getattr(self.cfg, "facebook_target_group_url", "") or "").strip()
        if not target:
            warning("Выбрана отправка в Facebook, но не задана Facebook-группа для публикации.")
            return 0

        sent = 0
        posts = self.db.get_unpublished_accepted_posts(limit=limit)

        for post in posts:
            ok, detail = self.facebook_publisher.publish_original_post_to_group(dict(post), target)
            if ok:
                self.db.mark_published_to_facebook(post["id"], detail)
                sent += 1
            else:
                self.db.mark_facebook_publish_failed(post["id"], detail)
                warning(f"Facebook публикация не выполнена: {detail}")

        return sent

    def stop(self):
        self.running = False
        self.fb.stop()
        info("Collector stopped")


if __name__ == "__main__":
    c = FacebookCollector()
    try:
        c.start(headless=False)
        c.collect()
    finally:
        c.stop()

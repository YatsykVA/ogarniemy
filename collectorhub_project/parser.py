"""
CollectorHub - parser.py
Связывает Extractor, Filters и Database.

v5:
- чёткий лог по ключевым словам;
- сохраняет только новые посты;
- показывает, почему пост принят или отклонён.
"""

from extractor import FacebookExtractor
from filters import Filters
from logger import info


class FacebookParser:
    def __init__(self, database):
        self.db = database
        self.filters = Filters()

    def parse_current_page(self, page, group_url: str = "", max_posts: int = 20) -> dict:
        extractor = FacebookExtractor(page)
        posts = extractor.extract_posts(max_posts=max_posts)

        saved = 0
        accepted = 0
        rejected = 0
        duplicates = 0

        for post in posts:
            post["group_url"] = group_url

            result = self.filters.match(post.get("text", ""))
            post["accepted"] = result["accepted"]
            post["matched_keywords"] = result["keywords"]
            post["matched_exclusions"] = result["exclusions"]

            is_new, post_id = self.db.save_post(post)

            if not is_new:
                duplicates += 1
                continue

            saved += 1

            short_text = " ".join((post.get("text") or "").split())[:140]
            author = post.get("author") or "Unknown"

            if post["accepted"]:
                accepted += 1
                info(
                    "✅ Подходит: "
                    f"автор={author} | ключи={', '.join(post['matched_keywords'])} | "
                    f"текст={short_text}"
                )
            else:
                rejected += 1
                if post["matched_exclusions"]:
                    info(
                        "⛔ Отклонено исключением: "
                        f"исключения={', '.join(post['matched_exclusions'])} | текст={short_text}"
                    )

        info(
            f"Посты по группе: найдено {len(posts)}, новых {saved}, "
            f"дублей {duplicates}, подходит {accepted}, отклонено {rejected}"
        )

        return {
            "found": len(posts),
            "saved": saved,
            "duplicates": duplicates,
            "accepted": accepted,
            "rejected": rejected,
        }

"""
CollectorHub - extractor.py

v31 STRICT EXTRACTOR REWRITE:
- НИКОГДА не берёт article.inner_text() как текст объявления;
- комментарии, даты, кнопки Facebook и поле ответа не читаются как пост;
- если чистый message-блок Facebook не найден — пост пропускается;
- сначала раскрывает "Показать больше / See more / Zobacz więcej" только внутри message-блока;
- чистка не переписывает объявление, а только убирает интерфейсный мусор;
- сначала чистит хвост комментариев/интерфейса, потом отправляет чистый текст;
- пост пропускается только если текст пустой или явно обрезан после "Показать больше".
"""

import re
from typing import List, Dict
from playwright.sync_api import Page

PHONE_RE = re.compile(r"(\+?\d[\d\s\-\(\)]{7,}\d)")

# Только блоки текста самого поста. Комментарии и весь article.inner_text() запрещены.
MESSAGE_SELECTORS = [
    '[data-ad-preview="message"]',
    '[data-ad-comet-preview="message"]',
    'div[data-ad-preview="message"]',
    'div[data-ad-comet-preview="message"]',
]

SEE_MORE_XPATH = (
    './/*[self::div or self::span or self::a]'
    '[normalize-space(.)="Ещё"'
    ' or normalize-space(.)="Еще"'
    ' or normalize-space(.)="Показать ещё"'
    ' or normalize-space(.)="Показать еще"'
    ' or normalize-space(.)="Показать больше"'
    ' or normalize-space(.)="See more"'
    ' or normalize-space(.)="Zobacz więcej"]'
)

# Если такая строка всё-таки появилась в тексте — это граница интерфейса/комментариев.
# Всё ниже выкидываем, чтобы не отправлять комментарии в Telegram.
STOP_AFTER_LINE_RE = re.compile(
    r"^("
    r"смотреть\s+(другие\s+)?комментарии|"
    r"посмотреть\s+(другие\s+)?комментарии|"
    r"показать\s+\d+\s+(ответ|ответа|ответов|комментар|комментария|комментариев)|"
    r"view\s+(more\s+)?comments?|"
    r"view\s+\d+\s+(repl|replies|comments?)|"
    r"see\s+(more\s+)?comments?|"
    r"show\s+\d+\s+(repl|replies|comments?)|"
    r"zobacz\s+(więcej\s+)?komentarz.*|"
    r"pokaż\s+\d+\s+(odpowied|odpowiedzi|komentar).*|"
    r"показать\s+\d+\s+ответ.*|"
    r"показать\s+\d+\s+коммент.*|"
    r"нравится|like|lubię\s+to|"
    r"ответить|reply|odpowiedz|"
    r"поделиться|share|udostępnij|"
    r"send|wysłać|wyślij|"
    r"priv|pw|pm|"
    r"комментировать\s+как\b.*|comment\s+as\b.*|skomentuj\s+jako\b.*|"
    r"ответить\s+как\b.*|reply\s+as\b.*|odpowiedz\s+jako\b.*"
    r")$",
    flags=re.IGNORECASE | re.UNICODE,
)

DROP_LINE_RE = re.compile(
    r"("
    r"^\s*[.·•●▪◦]+\s*$|"
    r"^\s*\d+\s*$|"
    r"^\s*(и|i|and|oraz)\s+\d+\s*$|"
    r"^\s*\d+\s*[·•]\s*\d+\s*$|"
    r"^\s*(ещё|еще|more|więcej)\s*$|"
    r"^\s*(показать\s+(ещё|еще|больше|меньше)|see\s+more|show\s+less|zobacz\s+więcej|zobacz\s+mniej)\s*$|"
    r"^\s*(показать\s+перевод|see\s+translation|zobacz\s+tłumaczenie)\s*$|"
    r"^\s*(комментировать\s+как|comment\s+as|skomentuj\s+jako|ответить\s+как|reply\s+as|odpowiedz\s+jako)\b.*|"
    r"^\s*(опубликовано\s+для\s+группы|posted\s+to\s+group|opublikowano\s+w\s+grupie)\b.*|"
    r"^\s*(подписаться|follow|obserwuj|позвонить|call|zadzwoń|рекрутер|recruiter)\s*$|"
    r"^\s*#.+$"
    r")",
    flags=re.IGNORECASE | re.UNICODE,
)

INLINE_REMOVE_RE = re.compile(
    r"("
    r"\b(ещё|еще|more|więcej)\b|"
    r"\bпоказать\s+(ещё|еще|больше|меньше)\b|"
    r"\bsee\s+more\b|\bshow\s+less\b|"
    r"\bzobacz\s+więcej\b|\bzobacz\s+mniej\b|"
    r"\bпоказать\s+перевод\b|\bsee\s+translation\b|\bzobacz\s+tłumaczenie\b"
    r")",
    flags=re.IGNORECASE | re.UNICODE,
)

# Относительные даты Facebook.
DATE_ONLY_RE = re.compile(
    r"^\s*(сегодня|вчера|today|yesterday|dzisiaj|wczoraj|"
    r"\d+\s*(сек\.?|мин\.?|ч\.?|дн\.?|нед\.?|мес\.?|год|лет|"
    r"sec|secs|min|mins|h|hr|hrs|d|day|days|w|week|weeks|"
    r"godz\.?|godziny|min\.?|dni|tydz\.?))\s*(·.*)?$",
    flags=re.IGNORECASE | re.UNICODE,
)

# Абсолютные даты Facebook: 12 июнь в 09:11 / 17 cze o 11:01 / June 12 at 9:11.
DATE_ABSOLUTE_RE = re.compile(
    r"^\s*\d{1,2}\s+("
    r"январ\w*|феврал\w*|март\w*|апрел\w*|ма[йя]|июн\w*|июл\w*|август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*|"
    r"січ\w*|лют\w*|бер\w*|квіт\w*|трав\w*|черв\w*|лип\w*|серп\w*|вер\w*|жовт\w*|лист\w*|груд\w*|"
    r"sty\w*|lut\w*|mar\w*|kwi\w*|maj\w*|cze\w*|lip\w*|sie\w*|wrz\w*|paź\w*|paz\w*|lis\w*|gru\w*|"
    r"jan\w*|feb\w*|apr\w*|jun\w*|jul\w*|aug\w*|sep\w*|oct\w*|nov\w*|dec\w*"
    r")\s+((в|о|at)\s+)?\d{1,2}:\d{2}\s*$",
    flags=re.IGNORECASE | re.UNICODE,
)

DATE_PREFIX_RE = re.compile(
    r"^\s*(сегодня|вчера|today|yesterday|dzisiaj|wczoraj|\d+\s*(сек\.?|мин\.?|ч\.?|дн\.?|нед\.?|мес\.?|год|лет|godz\.?|min\.?|d|h|w))\s*·\s*",
    flags=re.IGNORECASE | re.UNICODE,
)

DIRTY_MARKER_RE = re.compile(
    r"(смотреть\s+другие\s+комментарии|нравится|ответить|поделиться|"
    r"comment\s+as|reply\s+as|ответить\s+как|priv\b|"
    r"lubię\s+to|odpowiedz|udostępnij)",
    flags=re.IGNORECASE | re.UNICODE,
)

INCOMPLETE_END_RE = re.compile(r"[A-Za-zА-Яа-яЁёĄąĆćĘęŁłŃńÓóŚśŹźŻż]\.{3}$")


class FacebookExtractor:
    def __init__(self, page: Page):
        self.page = page

    def extract_posts(self, max_posts: int = 200) -> List[Dict]:
        self._deep_scroll_feed(max_posts=max_posts)

        posts = []
        articles = self.page.locator('div[role="article"]')

        try:
            count = min(articles.count(), max_posts)
        except Exception:
            count = 0

        seen_keys = set()

        for i in range(count):
            article = articles.nth(i)

            try:
                article.scroll_into_view_if_needed(timeout=2500)
                self.page.wait_for_timeout(250)
            except Exception:
                pass

            if self._looks_like_non_post(article):
                continue

            message_blocks = self._get_message_blocks(article)

            # Жёсткое правило v28:
            # нет чистого message-блока — не читаем весь article и не тащим комментарии.
            if not message_blocks:
                continue

            self._click_see_more_inside_message_blocks(message_blocks)
            author = self._extract_author(article)
            post_text = self._extract_clean_message_text(message_blocks, author=author)

            if not self._is_good_post_text(post_text):
                continue

            phones = PHONE_RE.findall(post_text)
            post_url = self._extract_post_url(article)

            post = {
                "facebook_post_id": None,
                "author": author,
                "author_profile": self._extract_author_profile(article),
                "text": post_text,
                "phones": phones,
                "phone": ", ".join(phones),
                "post_url": post_url,
                "photos": [],
            }

            post["facebook_post_id"] = self._make_unique_key(post)

            if post["facebook_post_id"] in seen_keys:
                continue

            seen_keys.add(post["facebook_post_id"])
            posts.append(post)

        return posts

    def _deep_scroll_feed(self, max_posts: int = 200):
        try:
            self.page.wait_for_timeout(3000)
            last_count = 0
            stable_cycles = 0
            max_cycles = max(80, max_posts)

            for _ in range(max_cycles):
                try:
                    current_count = self.page.locator('div[role="article"]').count()
                except Exception:
                    current_count = 0

                if current_count >= max_posts:
                    break

                if current_count <= last_count:
                    stable_cycles += 1
                else:
                    stable_cycles = 0
                    last_count = current_count

                if stable_cycles >= 10 and current_count >= min(max_posts, 60):
                    break

                self.page.mouse.wheel(0, 3600)
                self.page.wait_for_timeout(1500)

            self.page.wait_for_timeout(800)
        except Exception:
            pass

    def _get_message_blocks(self, article):
        blocks = []
        seen_texts = set()
        for selector in MESSAGE_SELECTORS:
            try:
                loc = article.locator(selector)
                count = min(loc.count(), 8)
                for i in range(count):
                    block = loc.nth(i)
                    text = self._safe_inner_text(block)
                    key = " ".join(text.split()).lower()
                    if text and len(text.strip()) >= 3 and key not in seen_texts:
                        seen_texts.add(key)
                        blocks.append(block)
            except Exception:
                continue
        return blocks

    def _click_see_more_inside_message_blocks(self, message_blocks):
        # Несколько проходов: Facebook иногда раскрывает текст не с первого клика.
        for _ in range(3):
            clicked_any = False
            for block in message_blocks:
                try:
                    buttons = block.locator(f"xpath={SEE_MORE_XPATH}")
                    count = min(buttons.count(), 8)
                    for i in range(count):
                        try:
                            btn = buttons.nth(i)
                            if btn.is_visible(timeout=700):
                                btn.click(timeout=1800)
                                clicked_any = True
                                self.page.wait_for_timeout(550)
                        except Exception:
                            continue
                except Exception:
                    continue
            if not clicked_any:
                break

    def _extract_clean_message_text(self, message_blocks, author: str = "") -> str:
        candidates = []
        for block in message_blocks:
            raw = self._safe_inner_text(block)
            cleaned = self._clean_post_text(raw, author=author)
            if cleaned and len(cleaned) >= 5:
                candidates.append(cleaned)
        if not candidates:
            return ""

        # Берём самый полный вариант message-блока, но только среди чистых message-блоков.
        unique = []
        for text in candidates:
            if text not in unique:
                unique.append(text)
        return max(unique, key=len)

    def _safe_inner_text(self, locator) -> str:
        try:
            return locator.inner_text(timeout=3500).strip()
        except Exception:
            return ""

    def _clean_post_text(self, text: str, author: str = "") -> str:
        author = (author or "").strip()
        lines = []
        seen = set()
        prev_empty = False

        for raw in (text or "").splitlines():
            line = raw.replace("\u00a0", " ").strip()

            if not line:
                if lines and not prev_empty:
                    lines.append("")
                    prev_empty = True
                continue

            low = line.lower().strip()

            # Если вдруг в message попала граница комментариев/интерфейса — дальше не читаем.
            # Перед этим выкидываем короткий хвост комментария, который часто идёт прямо перед
            # строками "Нравится / Ответить / Поделиться".
            if STOP_AFTER_LINE_RE.match(low):
                self._remove_comment_tail(lines)
                break

            if author and low == author.lower():
                continue

            if author and low.startswith(author.lower()):
                line = line[len(author):].strip()
                low = line.lower().strip()
                if not line:
                    continue

            if DATE_ONLY_RE.match(line) or DATE_ABSOLUTE_RE.match(line):
                continue

            line = DATE_PREFIX_RE.sub("", line).strip()
            if not line:
                continue

            if DROP_LINE_RE.match(line):
                continue

            line = INLINE_REMOVE_RE.sub("", line).strip()
            line = re.sub(r"\s{2,}", " ", line).strip()
            if not line:
                continue

            # Хэштеги удаляем, но остальной текст строки оставляем.
            if "#" in line:
                line = re.sub(r"#\S+", "", line).strip()
                if not line:
                    continue

            key = line.lower()
            if key in seen:
                continue

            seen.add(key)
            lines.append(line)
            prev_empty = False

        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()

        return "\n".join(lines).strip()


    def _is_good_post_text(self, text: str) -> bool:
        text = (text or "").strip()
        if len(text) < 5:
            return False
        # После чистки таких маркеров уже быть не должно. Если остались —
        # это неизвестный кусок интерфейса, безопаснее пропустить, чем отправить комментарии.
        if DIRTY_MARKER_RE.search(text):
            return False

        non_empty = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not non_empty:
            return False

        # Пример плохого хвоста: "Łączę b..." — значит текст не раскрыт полностью.
        if INCOMPLETE_END_RE.search(non_empty[-1]):
            return False

        return True


    def _remove_comment_tail(self, lines: list[str]) -> None:
        """
        Защита на случай, если Facebook всё-таки подсунул комментарий внутрь текста.
        Ищем в последних строках типичный шаблон комментария:
        "Имя Фамилия" + короткий ответ, прямо перед "Нравится/Ответить".
        Нормальный текст объявления выше не трогаем.
        """
        if not lines:
            return

        def looks_like_person_name(value: str) -> bool:
            value = (value or "").strip()
            if not value or len(value) > 45:
                return False
            if any(ch.isdigit() for ch in value):
                return False
            words = [w for w in re.split(r"\s+", value) if w]
            if not (2 <= len(words) <= 4):
                return False
            good = 0
            for word in words:
                first = word[:1]
                if first and first.upper() == first and any(c.isalpha() for c in word):
                    good += 1
            return good >= 2

        # Ищем имя автора комментария среди последних 4 непустых строк.
        start = max(0, len(lines) - 4)
        for idx in range(len(lines) - 1, start - 1, -1):
            if looks_like_person_name(lines[idx]):
                del lines[idx:]
                while lines and not lines[-1].strip():
                    lines.pop()
                return

        # Если имя автора комментария не нашли, ничего больше не режем.
        # Важно: нормальные объявления часто заканчиваются короткими вопросами
        # вроде "Barber?" или "Sklep z herbatami i kawami?".
        return

    def _looks_like_non_post(self, article) -> bool:
        # Это НЕ источник текста объявления. Только быстрая проверка, чтобы не брать composer/search blocks.
        text = self._safe_inner_text(article)
        low = text.lower()
        junk_markers = [
            "создать публикацию",
            "write something",
            "napisz coś",
            "invite",
            "запросы на вступление",
            "search facebook",
            "поиск на facebook",
            "places",
            "места",
        ]
        return any(marker in low for marker in junk_markers)

    def _extract_author(self, article) -> str:
        try:
            links = article.locator("a")
            total = min(links.count(), 14)
            for i in range(total):
                txt = links.nth(i).inner_text(timeout=1000).strip()
                low = txt.lower()

                if not txt or len(txt) < 2:
                    continue
                if DATE_ONLY_RE.match(txt) or DATE_ABSOLUTE_RE.match(txt):
                    continue
                if any(x in low for x in [
                    "facebook", "коммент", "comment", "reply", "ответ",
                    "поделиться", "share", "группа", "group", "подписаться",
                    "follow", "obserwuj", "позвонить", "call", "рекрутер",
                    "нравится", "like", "lubię"
                ]):
                    continue
                return txt
        except Exception:
            pass
        return "Unknown"

    def _extract_author_profile(self, article):
        try:
            links = article.locator("a")
            total = min(links.count(), 25)
            for i in range(total):
                href = links.nth(i).get_attribute("href", timeout=1000)
                if not href:
                    continue
                if href.startswith("/"):
                    href = "https://www.facebook.com" + href
                if "facebook.com" not in href:
                    continue
                bad_parts = [
                    "/groups/", "/posts/", "comment_id=", "reply_comment_id=",
                    "multi_permalinks", "permalink", "/photo/", "/watch/",
                    "/search/", "/places/",
                ]
                if any(x in href for x in bad_parts):
                    continue
                return href.split("?")[0]
        except Exception:
            pass
        return None

    def _extract_post_url(self, article):
        try:
            links = article.locator("a")
            total = min(links.count(), 60)
            for i in range(total):
                href = links.nth(i).get_attribute("href", timeout=1000)
                if not href:
                    continue
                if href.startswith("/"):
                    href = "https://www.facebook.com" + href
                if (
                    "/posts/" in href
                    or "permalink" in href
                    or "multi_permalinks" in href
                    or ("/groups/" in href and "/posts/" in href)
                ):
                    return href.split("?")[0]
        except Exception:
            pass
        return None

    def _make_unique_key(self, post: dict) -> str:
        if post.get("post_url"):
            return post["post_url"].split("?")[0]
        author = post.get("author") or "Unknown"
        text = post.get("text") or ""
        return f"{author}|{text[:260]}"


if __name__ == "__main__":
    print("Extractor module v31 loaded.")

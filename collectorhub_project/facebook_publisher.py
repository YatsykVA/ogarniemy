"""
CollectorHub - facebook_publisher.py

Update 37:
- Telegram и Facebook отправка разделены.
- Facebook пересылает только через нижнюю кнопку поста: Переслать/Поделиться/Share/Udostępnij.
- Кнопка ищется внизу оригинального поста, а не через Telegram-логику и не через создание нового поста.
- Целевая группа берётся из выбранной в интерфейсе группы-получателя.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from logger import info, warning


SHARE_BUTTON_LABELS = [
    # RU / UA
    "Переслать", "Поделиться", "Отправить",
    "Переслати", "Поділитися", "Поширити", "Надіслати",
    # EN / PL
    "Share", "Send", "Forward", "Udostępnij", "Wyślij", "Prześlij",
]

SHARE_TO_GROUP_LABELS = [
    "Поделиться в группе", "Поделиться в группе Facebook", "В группе",
    "Share to a group", "Share in a group", "Group",
    "Udostępnij w grupie", "W grupie",
    "Поширити в групі", "Поділитися в групі", "У групі",
]

DESTINATION_LABELS = [
    "Лента", "Ваша лента", "Новостная лента",
    "Feed", "News Feed", "Your Feed", "Share to Feed",
    "Aktualności", "Twój kanał", "Kanał aktualności",
    "Стрічка", "Ваша стрічка",
]

GROUP_SEARCH_LABELS = [
    "Поиск групп", "Найти группу", "Выберите группу", "Название группы",
    "Search groups", "Search for groups", "Find a group", "Choose a group", "Group name",
    "Szukaj grup", "Znajdź grupę", "Wybierz grupę", "Nazwa grupy",
    "Пошук груп", "Знайти групу", "Виберіть групу", "Назва групи",
]

SHARE_NOW_LABELS = [
    "Поделиться сейчас", "Поделиться", "Опубликовать",
    "Share now", "Share", "Post",
    "Udostępnij teraz", "Udostępnij", "Opublikuj",
    "Поширити зараз", "Поширити", "Опублікувати",
]


def _clean_line(line: str) -> str:
    return re.sub(r"\s+", " ", (line or "").replace("\u00a0", " ")).strip()


def _norm(text: str) -> str:
    return _clean_line(text).casefold()


class FacebookPublisher:
    def __init__(self, fb_session):
        self.fb = fb_session

    @property
    def page(self):
        return self.fb.page

    def _click_first(self, selectors: list[str], timeout: int = 1200, wait_after: int = 900) -> bool:
        if not self.page:
            return False
        for selector in selectors:
            try:
                loc = self.page.locator(selector).first
                if loc.is_visible(timeout=timeout):
                    loc.scroll_into_view_if_needed(timeout=2500)
                    loc.click(timeout=4500)
                    self.page.wait_for_timeout(wait_after)
                    return True
            except Exception:
                continue
        return False

    def _click_by_labels(self, labels: list[str], timeout: int = 1200, wait_after: int = 900) -> bool:
        selectors: list[str] = []
        for label in labels:
            safe = label.replace('"', '\\"')
            selectors.extend([
                f'button:has-text("{safe}")',
                f'[role="button"]:has-text("{safe}")',
                f'[aria-label*="{safe}"]',
                f'div[role="menuitem"]:has-text("{safe}")',
                f'span:has-text("{safe}")',
                f'text="{safe}"',
            ])
        return self._click_first(selectors, timeout=timeout, wait_after=wait_after)

    def _visible_text(self, locator) -> str:
        try:
            return _clean_line(locator.inner_text(timeout=900))
        except Exception:
            return ""

    def _click_visible_locator(self, locator, timeout: int = 2500, wait_after: int = 1000) -> bool:
        try:
            if not locator.is_visible(timeout=timeout):
                return False
            locator.scroll_into_view_if_needed(timeout=timeout)
            locator.click(timeout=4500)
            self.page.wait_for_timeout(wait_after)
            return True
        except Exception:
            return False

    def _target_base_url(self, target_group_url: str) -> str:
        url = (target_group_url or "").strip()
        if not url:
            return ""
        return url.split("?")[0].rstrip("/")

    def _target_group_name_from_saved_groups(self, target_group_url: str) -> str:
        """Берём название выбранной группы из списка CollectorHub, чтобы выбрать её в окне Share."""
        target = self._target_base_url(target_group_url)
        if not target:
            return ""
        try:
            from groups_manager import GroupsManager, best_group_name, normalize_url

            target_norm = normalize_url(target).rstrip("/")
            target_gid = self._group_id_from_url(target_norm)
            for row in GroupsManager().get_all():
                row_url = normalize_url((row["url"] or "").strip()).rstrip("/")
                row_gid = (row["group_id"] or "").strip()
                if (row_url and row_url == target_norm) or (target_gid and row_gid == target_gid):
                    return best_group_name(row["name"] or "", row_url or target_norm)
        except Exception:
            pass
        return ""

    def _group_id_from_url(self, url: str) -> str:
        try:
            path = urlparse(url).path.strip("/")
            parts = path.split("/")
            if "groups" in parts:
                idx = parts.index("groups")
                if idx + 1 < len(parts):
                    return parts[idx + 1]
        except Exception:
            pass
        return ""

    def _open_original_post(self, post: dict) -> tuple[bool, str]:
        post_url = self._target_base_url(post.get("post_url") or "")
        if not post_url or "facebook.com" not in post_url:
            return False, "У объявления нет ссылки на оригинальный Facebook-пост. Через стрелочку переслать нечего."
        info(f"Facebook share: открываю оригинальное объявление {post_url}")
        self.page.goto(post_url, wait_until="domcontentloaded", timeout=60000)
        self.page.wait_for_timeout(4500)
        return True, post_url

    def _click_share_arrow(self) -> bool:
        """
        Ищет именно нижнюю кнопку поста "Переслать/Поделиться".
        Важно: не Telegram, не создание нового поста, а нативный Facebook share у оригинального объявления.
        """
        if not self.page:
            return False

        label_norms = [_norm(x) for x in SHARE_BUTTON_LABELS]

        # 1) Обычный и самый правильный путь: кнопки внутри article.
        # Берём статьи на странице и идём снизу вверх, потому что нужная кнопка обычно в нижней панели поста.
        for _ in range(5):
            try:
                articles = self.page.locator('div[role="article"]')
                article_count = min(articles.count(), 8)
                for article_index in range(article_count - 1, -1, -1):
                    article = articles.nth(article_index)
                    buttons = article.locator('[role="button"], button, a[role="link"]')
                    btn_count = buttons.count()
                    for i in range(btn_count - 1, -1, -1):
                        btn = buttons.nth(i)
                        text = _norm(self._visible_text(btn))
                        aria = _norm(btn.get_attribute('aria-label') or '')
                        haystack = f'{text} {aria}'
                        if any(label and label in haystack for label in label_norms):
                            info(f"Facebook share: нажимаю нижнюю кнопку поста: {self._visible_text(btn) or aria}")
                            if self._click_visible_locator(btn, timeout=1200, wait_after=1600):
                                return True
            except Exception:
                pass

            # 2) Запасной путь: текстовые локаторы, но тоже с попыткой попасть в видимую нижнюю кнопку.
            for label in SHARE_BUTTON_LABELS:
                safe = label.replace('"', '\\"')
                selectors = [
                    f'div[role="article"] [role="button"]:has-text("{safe}")',
                    f'div[role="article"] button:has-text("{safe}")',
                    f'div[role="article"] [aria-label*="{safe}"]',
                    f'[role="button"]:has-text("{safe}")',
                    f'button:has-text("{safe}")',
                    f'[aria-label*="{safe}"]',
                ]
                for selector in selectors:
                    try:
                        loc = self.page.locator(selector).last
                        if self._click_visible_locator(loc, timeout=800, wait_after=1600):
                            info(f"Facebook share: нажата кнопка пересылки по селектору {selector}")
                            return True
                    except Exception:
                        continue

            # Кнопка действительно внизу поста: докручиваем оригинальный пост вниз и пробуем снова.
            try:
                self.page.mouse.wheel(0, 650)
            except Exception:
                pass
            self.page.wait_for_timeout(900)

        return False

    def _choose_share_to_group_mode(self) -> bool:
        # Иногда сразу в меню есть "Поделиться в группе".
        if self._click_by_labels(SHARE_TO_GROUP_LABELS, timeout=2200, wait_after=1500):
            return True

        # Иногда сначала открывается окно, где сверху стоит "Лента". Нажимаем его и выбираем группу.
        if self._click_by_labels(DESTINATION_LABELS, timeout=1800, wait_after=1200):
            if self._click_by_labels(SHARE_TO_GROUP_LABELS, timeout=2500, wait_after=1500):
                return True

        # Иногда селектор назначения выглядит как combobox.
        selectors = [
            'div[role="dialog"] [role="combobox"]',
            'div[role="dialog"] [aria-haspopup="listbox"]',
            'div[role="dialog"] [aria-haspopup="menu"]',
        ]
        if self._click_first(selectors, timeout=1200, wait_after=1000):
            if self._click_by_labels(SHARE_TO_GROUP_LABELS, timeout=2500, wait_after=1500):
                return True

        return False

    def _fill_group_search(self, group_name: str) -> bool:
        if not group_name:
            return False

        # Полное название иногда слишком длинное. Сначала полное, потом короткие куски.
        candidates = [group_name]
        short = re.split(r"[|(]", group_name)[0].strip()
        if short and short != group_name:
            candidates.append(short)
        words = [w for w in re.split(r"\s+", short or group_name) if len(w) >= 4]
        if words:
            candidates.append(" ".join(words[:3]))

        input_selectors = []
        for label in GROUP_SEARCH_LABELS:
            safe = label.replace('"', '\\"')
            input_selectors.extend([
                f'div[role="dialog"] input[placeholder*="{safe}"]',
                f'div[role="dialog"] [role="textbox"][aria-label*="{safe}"]',
                f'div[role="dialog"] [contenteditable="true"][aria-label*="{safe}"]',
            ])
        input_selectors.extend([
            'div[role="dialog"] input[type="search"]',
            'div[role="dialog"] input[type="text"]',
            'div[role="dialog"] [role="textbox"][contenteditable="true"]',
            'div[role="dialog"] [contenteditable="true"]',
        ])

        for candidate in candidates:
            for selector in input_selectors:
                try:
                    box = self.page.locator(selector).first
                    if not box.is_visible(timeout=1200):
                        continue
                    box.click(timeout=3000)
                    try:
                        self.page.keyboard.press("Control+A")
                        self.page.keyboard.press("Backspace")
                    except Exception:
                        pass
                    self.page.keyboard.insert_text(candidate)
                    self.page.wait_for_timeout(2200)
                    return True
                except Exception:
                    continue
        return False

    def _click_target_group(self, group_name: str, target_group_url: str) -> bool:
        names = []
        if group_name:
            names.append(group_name)
            short = re.split(r"[|(]", group_name)[0].strip()
            if short and short != group_name:
                names.append(short)
            words = [w for w in re.split(r"\s+", short or group_name) if len(w) >= 4]
            if words:
                names.append(" ".join(words[:3]))

        # Пробуем кликнуть по названию без поиска.
        for name in names:
            if self._click_by_labels([name], timeout=1400, wait_after=1400):
                return True

        # Пробуем через поле поиска групп.
        if self._fill_group_search(group_name):
            for name in names:
                if self._click_by_labels([name], timeout=2500, wait_after=1500):
                    return True

        # Последний запасной вариант: если в окне есть только один результат группы, кликаем первый menuitem/listitem.
        selectors = [
            'div[role="dialog"] div[role="menuitem"]',
            'div[role="dialog"] div[role="option"]',
            'div[role="dialog"] div[role="listitem"]',
        ]
        return self._click_first(selectors, timeout=1200, wait_after=1500)

    def _click_share_now(self) -> bool:
        for _ in range(10):
            if self._click_by_labels(SHARE_NOW_LABELS, timeout=1000, wait_after=1500):
                return True
            self.page.wait_for_timeout(1000)
        return False

    def publish_original_post_to_group(self, post: dict, target_group_url: str) -> tuple[bool, str]:
        """
        Пересылает именно оригинальный Facebook-пост через кнопку со стрелочкой:
        Share/Поделиться -> в группу -> выбранная группа -> Поделиться сейчас.
        """
        target_group_url = self._target_base_url(target_group_url)
        if not target_group_url:
            return False, "Не задана Facebook-группа-получатель"
        if not self.fb._ensure_page():
            return False, "Браузер Facebook закрыт"

        target_group_name = self._target_group_name_from_saved_groups(target_group_url)
        if not target_group_name:
            try:
                from config import load_config
                target_group_name = (getattr(load_config(), "facebook_target_group_name", "") or "").strip()
            except Exception:
                target_group_name = ""
        target_label = target_group_name or target_group_url

        try:
            ok, detail = self._open_original_post(post)
            if not ok:
                return False, detail

            if not self._click_share_arrow():
                return False, "Не нашёл кнопку со стрелочкой / Поделиться на оригинальном объявлении"

            if not self._choose_share_to_group_mode():
                return False, "Не смог выбрать режим Поделиться в группе"

            if not self._click_target_group(target_group_name, target_group_url):
                return False, f"Не смог выбрать группу-получатель: {target_label}"

            if not self._click_share_now():
                return False, "Не нашёл/не нажал кнопку Поделиться сейчас"

            self.page.wait_for_timeout(5000)
            info(f"Facebook share: объявление переслано в группу {target_label}")
            return True, f"Переслано через Поделиться сейчас в группу: {target_label}"

        except Exception as exc:
            warning(f"Facebook share failed: {exc}")
            return False, str(exc)

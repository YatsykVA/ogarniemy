"""
CollectorHub - facebook_group_search.py
Автопоиск Facebook-групп по запросам из одной строки через запятую.

Update 34 logic:
- никаких лимитов на количество найденных групп;
- собираем все группы, которые Facebook подгружает по запросу;
- фильтруем ТОЛЬКО по названию группы, без описаний/постов/комментариев;
- открываем каждую найденную группу, проверяем только её h1/title, вступаем, сохраняем только после подтверждённого вступления;
- в поле name/title сохраняем человеческое название группы, НЕ ID;
- если группа уже есть, не создаём дубль, а обновляем запись;
- ID хранится только технически в group_id/url.
"""

from __future__ import annotations

import os
import re
from urllib.parse import quote_plus

from facebook_session import FacebookSession
from groups_manager import GroupsManager, normalize_url
from logger import info, warning

JOIN_LABELS = [
    "Присоединиться к группе", "Вступить в группу", "Вступить", "Присоединиться",
    "Join group", "Join",
    "Dołącz do grupy", "Dołącz",
    "Приєднатися до групи", "Приєднатися",
]
JOINED_MARKERS = [
    "вы вступили в группу", "вы присоединились к группе", "вы участник", "вы состоите в группе",
    "вы уже состоите", "вы являетесь участником", "joined the group", "you joined",
    "you're a member", "you are a member", "you’re a member", "member of this group",
    "dołączono do grupy", "jesteś członkiem", "jestes czlonkiem", "należysz do grupy",
    "vi ste član", "ви вступили до групи", "ви приєдналися до групи", "ви учасник",
]
ALREADY_JOINED_LABELS = [
    "Вы участник", "Участник", "Состою", "Joined", "Following", "Member",
    "Obserwujesz", "Członek", "Czlonek", "Jesteś członkiem", "Jestes czlonkiem",
    "Учасник",
]
PENDING_MARKERS = [
    "заявка отправлена", "запрос отправлен", "запрос на вступление отправлен",
    "pending", "request sent", "membership pending", "oczekuje", "wysłano prośbę", "wyslano prosbe",
]
QUESTIONS_MARKERS = ["ответьте", "вопрос", "questions", "pytania", "membership questions", "regulamin", "odpowiedz", "odpowiedzieć"]


def parse_queries(raw: str) -> list[str]:
    return [q.strip() for q in (raw or "").split(",") if q.strip()]

def parse_keywords(raw: str) -> list[str]:
    """Ключевые слова пользователя: одна строка через запятую. Это НЕ лимит и НЕ фильтр по описанию."""
    items = []
    seen = set()
    for q in parse_queries(raw):
        key = _normalize_for_match(q)
        if key and key not in seen:
            seen.add(key)
            items.append(q)
    return items



def clean_url(href: str) -> str:
    href = (href or "").strip()
    if href.startswith("/"):
        href = "https://www.facebook.com" + href
    href = href.split("?")[0].rstrip("/")
    return normalize_url(href)


def is_bad_group_name(name: str) -> bool:
    value = (name or "").strip()
    low = value.lower()
    if not value:
        return True
    if value.isdigit():
        return True
    if re.fullmatch(r"facebook\s+group\s*\d*", low):
        return True
    if low in {"facebook", "groups", "группы", "group", "search", "поиск"}:
        return True
    if "facebook.com/groups" in low:
        return True
    return False


def clean_group_title(text: str) -> str:
    """Берём из карточки/страницы именно название, выкидывая технический мусор."""
    lines = []
    for line in (text or "").splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if not line:
            continue
        low = line.lower()
        if low in {"facebook", "groups", "группы", "join", "dołącz", "вступить"}:
            continue
        if any(x in low for x in ["members", "участник", "członk", "posts", "публикац", "пост"]):
            continue
        if is_bad_group_name(line):
            continue
        lines.append(line)

    # Обычно название группы — первая нормальная строка в карточке/заголовке.
    return lines[0] if lines else ""


def title_from_url(url: str) -> str:
    tail = url.rstrip('/').split('/')[-1]
    title = re.sub(r"[-_]+", " ", tail).strip()
    if not title or title.isdigit():
        return "Facebook Group"
    return title


def _normalize_for_match(value: str) -> str:
    value = (value or "").lower()
    table = str.maketrans({
        "ą": "a", "ć": "c", "ę": "e", "ł": "l", "ń": "n", "ó": "o", "ś": "s", "ż": "z", "ź": "z",
        "і": "и", "ї": "и", "є": "е", "ґ": "г",
    })
    value = value.translate(table)
    value = re.sub(r"[^a-zа-я0-9]+", " ", value, flags=re.IGNORECASE).strip()
    return re.sub(r"\s+", " ", value)


def title_matches_query(title: str, query: str) -> bool:
    """
    Главное правило пользователя: фильтрация ТОЛЬКО по названию группы.
    Не читаем описание, посты, комментарии и страницу "О группе".
    """
    title_norm = _normalize_for_match(title)
    query_norm = _normalize_for_match(query)
    if not title_norm or not query_norm:
        return False

    return query_norm in title_norm


def title_matches_any_keyword(title: str, keywords: list[str]) -> bool:
    """Название должно содержать хотя бы одно ключевое слово пользователя."""
    return any(title_matches_query(title, keyword) for keyword in keywords)


class FacebookGroupAutoSearch:
    def __init__(self):
        self.fb = FacebookSession()
        self.groups = GroupsManager()
        self.groups.seed()
        # Не лимит групп. Это только защита от бесконечной прокрутки, если Facebook зависнет.
        self.max_no_new_scroll_rounds = 7

    def _body_text_lower(self) -> str:
        try:
            return self.fb.page.locator("body").inner_text(timeout=3000).lower()
        except Exception:
            return ""

    def _is_already_joined(self) -> bool:
        page = self.fb.page
        body = self._body_text_lower()
        if any(marker in body for marker in JOINED_MARKERS):
            return True
        for label in ALREADY_JOINED_LABELS:
            try:
                if page.locator(f'button:has-text("{label}")').first.is_visible(timeout=700):
                    return True
            except Exception:
                pass
            try:
                if page.locator(f'[role="button"]:has-text("{label}")').first.is_visible(timeout=700):
                    return True
            except Exception:
                pass
        return False

    def _click_join_and_confirm(self) -> tuple[bool, str]:
        """
        Жёсткое правило:
        - нажимаем кнопку вступления, если она есть;
        - ждём подтверждение 3–5+ секунд;
        - сохраняем только если статус реально стал "участник/ joined / członek";
        - если заявка pending или вопросы — НЕ сохраняем, но продолжаем следующую группу.
        """
        page = self.fb.page

        # Сначала верх страницы: кнопка вступления обычно рядом с заголовком.
        try:
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(800)
        except Exception:
            pass

        if self._is_already_joined():
            return True, "Уже состоим в группе"

        # Иногда Facebook показывает cookie/confirm-окна поверх кнопки.
        overlay_labels = [
            "Разрешить все cookies", "Allow all cookies", "Akceptuj wszystkie", "Accept all",
            "ОК", "OK", "Gotowe", "Готово",
        ]
        for label in overlay_labels:
            try:
                loc = page.locator(f'button:has-text("{label}")').first
                if loc.is_visible(timeout=600):
                    loc.click(timeout=2000)
                    page.wait_for_timeout(700)
            except Exception:
                pass

        clicked = False
        last_error = ""

        # Не кликаем один случайный элемент и не сдаёмся. Пробуем все видимые варианты.
        for attempt in range(3):
            if self._is_already_joined():
                return True, "Уже/теперь состоим в группе"

            for label in JOIN_LABELS:
                selectors = [
                    f'button:has-text("{label}")',
                    f'[role="button"]:has-text("{label}")',
                    f'div[aria-label*="{label}"][role="button"]',
                    f'[aria-label*="{label}"]',
                    f'text="{label}"',
                ]
                for selector in selectors:
                    try:
                        locs = page.locator(selector)
                        count = min(locs.count(), 8)
                        for i in range(count):
                            loc = locs.nth(i)
                            try:
                                if not loc.is_visible(timeout=700):
                                    continue
                                loc.scroll_into_view_if_needed(timeout=2000)
                                page.wait_for_timeout(300)
                                loc.click(timeout=5000)
                                clicked = True
                                info(f"Нажал кнопку вступления: {label}")

                                # Если после первого клика появляется вторичная кнопка/подтверждение — нажимаем её.
                                page.wait_for_timeout(1200)
                                for confirm in ["Присоединиться к группе", "Вступить", "Join group", "Join", "Dołącz", "Dołącz do grupy", "Приєднатися"]:
                                    try:
                                        c = page.locator(f'button:has-text("{confirm}")').first
                                        if c.is_visible(timeout=600):
                                            c.click(timeout=2500)
                                            page.wait_for_timeout(1000)
                                            info(f"Подтвердил вступление второй кнопкой: {confirm}")
                                            break
                                    except Exception:
                                        pass

                                # Пользователь просил ждать 3–5 секунд. Ждём, но не блокируем весь цикл навечно.
                                for _ in range(6):
                                    page.wait_for_timeout(1000)
                                    body = self._body_text_lower()

                                    if any(m in body for m in QUESTIONS_MARKERS):
                                        return False, "Нужны ответы на вопросы — не сохраняю"

                                    if any(m in body for m in PENDING_MARKERS):
                                        return False, "Заявка отправлена, но вступление не подтверждено — не сохраняю"

                                    if any(m in body for m in JOINED_MARKERS) or self._is_already_joined():
                                        return True, "Вступление подтверждено"

                                # Проверяем ещё раз без toast: Facebook часто просто меняет кнопку.
                                if self._is_already_joined():
                                    return True, "Вступление подтверждено кнопкой/статусом"
                            except Exception as exc:
                                last_error = str(exc)
                                continue
                    except Exception as exc:
                        last_error = str(exc)
                        continue

            # Иногда кнопка появляется после небольшой прокрутки/возврата вверх.
            try:
                page.mouse.wheel(0, 700)
                page.wait_for_timeout(700)
                page.evaluate("window.scrollTo(0, 0)")
                page.wait_for_timeout(700)
            except Exception:
                pass

        if self._is_already_joined():
            return True, "Уже состоим в группе"

        if clicked:
            return False, "Кнопку нажал, но подтверждение вступления не появилось — не сохраняю"

        return False, f"Кнопка присоединения не найдена — не сохраняю{(': ' + last_error) if last_error else ''}"

    def _collect_all_group_cards_from_results(self) -> list[dict]:
        """Собирает ВСЕ группы из результатов поиска: без [:12], без max_per_query, без лимита."""
        page = self.fb.page
        groups: list[dict] = []
        seen: set[str] = set()
        last_count = -1
        no_new_rounds = 0

        while True:
            try:
                anchors = page.locator('a[href*="/groups/"]')
                count = anchors.count()

                for i in range(count):
                    try:
                        anchor = anchors.nth(i)
                        href = anchor.get_attribute("href", timeout=800)
                        url = clean_url(href or "")
                        if not url or "/groups/" not in url:
                            continue
                        if "/posts/" in url or "permalink" in url:
                            continue

                        key = url.rstrip("/").lower()
                        if key in seen:
                            continue

                        raw_title = ""
                        try:
                            raw_title = anchor.inner_text(timeout=800)
                        except Exception:
                            pass
                        title = clean_group_title(raw_title) or title_from_url(url)

                        seen.add(key)
                        groups.append({"name": title, "title": title, "url": url})
                    except Exception:
                        continue

                if len(groups) == last_count:
                    no_new_rounds += 1
                else:
                    no_new_rounds = 0
                    info(f"Собрано групп из результатов: {len(groups)}")

                last_count = len(groups)
                if no_new_rounds >= self.max_no_new_scroll_rounds:
                    break

                page.mouse.wheel(0, 3000)
                page.wait_for_timeout(1400)
            except Exception as exc:
                warning(f"Не удалось прочитать результаты поиска групп: {exc}")
                break

        return groups

    def _read_group_name_from_page(self, fallback: str, url: str) -> str:
        """После открытия группы стараемся взять h1/title. ID сюда не допускаем."""
        page = self.fb.page
        candidates = []

        for selector in ['h1', '[role="main"] h1', '[role="main"] span']:
            try:
                loc = page.locator(selector).first
                if loc.is_visible(timeout=1200):
                    candidates.append(loc.inner_text(timeout=1200))
            except Exception:
                pass

        try:
            candidates.append(page.title(timeout=1500).replace(" | Facebook", ""))
        except Exception:
            pass

        # ВАЖНО: тело страницы, описание, посты и комментарии НЕ читаем для названия.
        # Только h1/title + fallback из карточки результата.
        for candidate in candidates:
            title = clean_group_title(candidate)
            if not is_bad_group_name(title):
                return title

        fallback = clean_group_title(fallback) or title_from_url(url)
        if is_bad_group_name(fallback):
            return "Facebook Group"
        return fallback

    def run(self, raw_queries: str):
        keywords = parse_keywords(raw_queries)
        if not keywords:
            warning("Не заданы ключевые слова. Вводи через запятую в одну строку.")
            return

        self.fb.start(headless=False)
        added = 0
        updated = 0
        joined = 0
        skipped_by_title = 0
        not_joined = 0
        errors = 0
        processed_urls: set[str] = set()

        try:
            for query in keywords:
                info(f"Ищу ВСЕ Facebook-группы по ключевому слову: {query}")
                search_url = f"https://www.facebook.com/search/groups/?q={quote_plus(query)}"
                self.fb.page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
                self.fb.page.wait_for_timeout(4500)

                found_groups = self._collect_all_group_cards_from_results()
                info(f"Найдено групп по ключевому слову '{query}': {len(found_groups)}")

                for group in found_groups:
                    url = group["url"]
                    url_key = url.rstrip("/").lower()
                    if url_key in processed_urls:
                        continue
                    processed_urls.add(url_key)

                    fallback_name = group.get("name") or group.get("title") or ""

                    try:
                        # ВАЖНО: не отбрасываем группу только из-за криво прочитанной карточки.
                        # Открываем страницу, читаем только h1/title, НЕ описание/посты/комменты.
                        self.fb.page.goto(url, wait_until="domcontentloaded", timeout=45000)
                        self.fb.page.wait_for_timeout(3200)
                        name = self._read_group_name_from_page(fallback_name, url)

                        if not title_matches_any_keyword(name, keywords):
                            skipped_by_title += 1
                            info(f"Пропущено: в НАЗВАНИИ нет ключевых слов: {name} | {url}")
                            continue

                        ok, detail = self._click_join_and_confirm()
                        if not ok:
                            not_joined += 1
                            info(f"НЕ сохраняю группу: {name} | {detail} | {url}")
                            continue

                        joined += 1
                        info(f"Вступление подтверждено: {name} | {detail}")

                        result = self.groups.add_or_update_group(name=name, url_or_id=url, enabled=True)
                        if result == "added":
                            added += 1
                            info(f"Добавлено в список Facebook-групп: {name} | {url}")
                        elif result == "updated":
                            updated += 1
                            info(f"Уже была — обновил без дубля: {name} | {url}")
                    except Exception as exc:
                        errors += 1
                        warning(f"Ошибка обработки группы {url}: {exc}")
                        continue

            info(
                f"Автопоиск завершён: добавлено новых {added}, "
                f"обновлено существующих {updated}, подтверждённых вступлений/уже участник {joined}, "
                f"пропущено по названию {skipped_by_title}, не вступили {not_joined}, "
                f"ошибок {errors}, дублей создано 0. "
                f"Группы без подтверждённого вступления НЕ сохранялись."
            )

        finally:
            self.fb.stop()


if __name__ == "__main__":
    raw = os.getenv("FB_GROUP_SEARCH_QUERIES", "").strip()
    FacebookGroupAutoSearch().run(raw)

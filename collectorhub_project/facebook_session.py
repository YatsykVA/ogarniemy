"""
CollectorHub - facebook_session.py
Управление сессией Facebook через Playwright.

v30:
- группа открывается в хронологическом режиме через sorting_setting=CHRONOLOGICAL;
- дополнительно в интерфейсе Facebook пытается переключить "Самое актуальное" на "Новые публикации";
- после переключения обновляет страницу и только потом extractor читает посты.
"""

from pathlib import Path
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

from playwright.sync_api import (
    sync_playwright,
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
)

from logger import info, warning

DATA_DIR = Path(__file__).resolve().parent / "data"
SESSION_DIR = DATA_DIR / "playwright_profile"


class FacebookSession:
    def __init__(self):
        self.playwright = None
        self.context = None
        self.page = None
        self.started = False

    def start(self, headless: bool = False):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SESSION_DIR.mkdir(parents=True, exist_ok=True)

        self.playwright = sync_playwright().start()

        self.context = self.playwright.chromium.launch_persistent_context(
            user_data_dir=str(SESSION_DIR),
            headless=headless,
            viewport={"width": 1440, "height": 900},
            slow_mo=80,
        )

        pages = self.context.pages
        self.page = pages[0] if pages else self.context.new_page()
        self.page.set_default_timeout(15000)
        self.page.set_default_navigation_timeout(45000)

        self.started = True
        info("Facebook browser session started")

    def open_facebook_login_page(self):
        if not self.started:
            self.start(headless=False)

        if not self.page:
            self.page = self.context.new_page()

        self.page.goto("https://www.facebook.com/", wait_until="domcontentloaded")
        info("Facebook login page opened")

    def save_state(self):
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            self.context.storage_state(path=str(DATA_DIR / "facebook_session.json"))
            info("Facebook session saved")
        except Exception as exc:
            warning(f"Не удалось сохранить storage_state: {exc}")

    def is_alive(self) -> bool:
        try:
            return bool(self.context and self.page and not self.page.is_closed())
        except Exception:
            return False

    def _ensure_page(self):
        if not self.context:
            return False

        if self.page and not self.page.is_closed():
            return True

        try:
            pages = self.context.pages
            self.page = pages[0] if pages else self.context.new_page()
            self.page.set_default_timeout(15000)
            self.page.set_default_navigation_timeout(45000)
            return True
        except Exception as exc:
            warning(f"Не удалось восстановить страницу браузера: {exc}")
            return False

    def _chronological_group_url(self, url: str) -> str:
        """
        Facebook иногда показывает не самые свежие посты, а "самые актуальные".
        Этот параметр часто переключает группу на хронологическую ленту.
        """
        try:
            parsed = urlparse(url)
            if "facebook.com" not in parsed.netloc:
                return url

            query = dict(parse_qsl(parsed.query))
            query["sorting_setting"] = "CHRONOLOGICAL"

            return urlunparse((
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                parsed.params,
                urlencode(query),
                parsed.fragment,
            ))

        except Exception:
            return url


    def _click_first_visible_text(self, texts: list[str], timeout: int = 1200) -> bool:
        """Кликает первый видимый элемент с одним из текстов."""
        if not self.page:
            return False

        for text in texts:
            selectors = [
                f'button:has-text("{text}")',
                f'[role="button"]:has-text("{text}")',
                f'[aria-label*="{text}"]',
                f'text="{text}"',
            ]

            for selector in selectors:
                try:
                    loc = self.page.locator(selector).first
                    if loc.is_visible(timeout=timeout):
                        loc.click(timeout=2500)
                        self.page.wait_for_timeout(900)
                        return True
                except Exception:
                    continue

        return False

    def _ensure_new_posts_sort(self) -> bool:
        """
        Реально переключает ленту группы с "Самое актуальное" на "Новые публикации".

        ВАЖНО:
        - Это не источник текста объявления.
        - Это только подготовка страницы перед чтением постов.
        - Если Facebook поменял язык/разметку и кнопку не удалось нажать,
          остаётся fallback через sorting_setting=CHRONOLOGICAL в URL.
        """
        if not self.page:
            return False

        relevant_labels = [
            "Самое актуальное",
            "Most relevant",
            "Najtrafniejsze",
            "Najważniejsze",
            "Najbardziej trafne",
            "Top posts",
        ]

        newest_labels = [
            "Новые публикации",
            "Новые посты",
            "Recent activity",
            "New posts",
            "Newest posts",
            "Najnowsze posty",
            "Nowe posty",
            "Najnowsze publikacje",
            "Nowe publikacje",
        ]

        try:
            # Если режим уже виден на странице — ничего не трогаем.
            for label in newest_labels:
                try:
                    if self.page.locator(f'text="{label}"').first.is_visible(timeout=700):
                        info(f"Режим ленты уже выглядит как новые публикации: {label}")
                        return True
                except Exception:
                    pass

            opened_menu = self._click_first_visible_text(relevant_labels, timeout=900)
            if not opened_menu:
                warning("Не нашёл кнопку сортировки 'Самое актуальное'. Использую только sorting_setting=CHRONOLOGICAL.")
                return False

            selected = self._click_first_visible_text(newest_labels, timeout=1800)
            if not selected:
                warning("Меню сортировки открылось, но пункт 'Новые публикации' не найден.")
                return False

            info("Переключил группу на режим: Новые публикации")
            self.page.wait_for_timeout(1500)

            # Пользователь просил именно обновить страницу после переключения.
            try:
                self.page.reload(wait_until="domcontentloaded", timeout=45000)
                self.page.wait_for_timeout(3500)
                info("Страница группы обновлена после переключения на новые публикации")
            except Exception as exc:
                warning(f"Не удалось обновить страницу после переключения сортировки: {exc}")

            return True

        except Exception as exc:
            warning(f"Не удалось переключить сортировку на новые публикации: {exc}")
            return False

    def open_group(self, url: str) -> bool:
        if not self._ensure_page():
            warning("Браузер или вкладка закрыты. Группа пропущена.")
            return False

        # Открываем именно хронологическую ленту, а не "популярные" / "актуальные" посты.
        target_url = self._chronological_group_url(url)

        try:
            info(f"Opening group: {target_url}")
            self.page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
            self.page.wait_for_timeout(3500)

            # Сначала пытаемся включить "Новые публикации", потом extractor читает посты.
            self._ensure_new_posts_sort()
            return True

        except PlaywrightTimeoutError:
            warning(f"Таймаут открытия группы, пропускаю: {target_url}")
            return False

        except PlaywrightError as exc:
            msg = str(exc)
            if "Target page, context or browser has been closed" in msg:
                warning("Браузер/страница закрыты во время открытия группы. Останавливаю обход.")
                self.stop()
                return False

            warning(f"Ошибка Playwright при открытии группы {target_url}: {exc}")
            return False

        except Exception as exc:
            warning(f"Неожиданная ошибка при открытии группы {target_url}: {exc}")
            return False

    def stop(self):
        try:
            if self.context:
                self.save_state()
                self.context.close()
        except Exception as exc:
            warning(f"Ошибка закрытия browser context: {exc}")

        try:
            if self.playwright:
                self.playwright.stop()
        except Exception as exc:
            warning(f"Ошибка остановки Playwright: {exc}")

        self.context = None
        self.page = None
        self.playwright = None
        self.started = False
        info("Facebook browser session stopped")

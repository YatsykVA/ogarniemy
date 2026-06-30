"""
CollectorHub - facebook_forwarder.py
Update 31.

Полуавтоматическая публикация найденных объявлений в Facebook-группу.
Важно: Facebook часто меняет интерфейс. Этот модуль делает безопасную попытку:
- открыть оригинальный пост;
- найти кнопку Поделиться/Share/Udostępnij;
- выбрать публикацию в группе, если интерфейс это позволяет;
- не отвечает на проверки, капчи и вопросы.

Если Facebook покажет ручной экран, окно останется открытым, а в логе будет видно,
что нужно помочь руками.
"""

from __future__ import annotations

from typing import Iterable

from logger import info, warning, error
from collector_settings import load_settings

SHARE_BUTTON_TEXTS = [
    "Поделиться",
    "Share",
    "Udostępnij",
]

GROUP_DESTINATION_TEXTS = [
    "Поделиться в группе",
    "Share to a group",
    "Udostępnij w grupie",
]

POST_BUTTON_TEXTS = [
    "Опубликовать",
    "Post",
    "Publikuj",
    "Udostępnij",
]


class FacebookForwarder:
    def __init__(self, fb_session):
        self.fb = fb_session

    def _click_first_text(self, texts: Iterable[str], timeout: int = 5000) -> bool:
        page = self.fb.page
        for text in texts:
            try:
                locator = page.get_by_text(text, exact=False).first
                if locator.count() > 0:
                    locator.click(timeout=timeout)
                    page.wait_for_timeout(1200)
                    return True
            except Exception:
                continue
        return False

    def forward_post_to_group(self, post: dict) -> bool:
        settings = load_settings()
        target_group_name = settings.get("facebook_target_group_name") or ""
        target_group_url = settings.get("facebook_target_group_url") or ""

        post_url = (post.get("post_url") or "").strip()
        post_id = post.get("id")

        if not post_url or post_url == "—":
            warning(f"Facebook forward skipped: у поста нет ссылки. DB id: {post_id}")
            return False

        if not target_group_name and not target_group_url:
            warning("Facebook target group не настроена. Открой data/collector_settings.json и заполни facebook_target_group_name или facebook_target_group_url.")
            return False

        if not self.fb.is_alive():
            warning("Facebook browser session закрыта, публикация невозможна.")
            return False

        page = self.fb.page

        try:
            info(f"Facebook forward: открываю оригинальный пост DB id {post_id}: {post_url}")
            page.goto(post_url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3500)

            if not self._click_first_text(SHARE_BUTTON_TEXTS, timeout=7000):
                warning("Не нашёл кнопку Share/Поделиться. Оставляю пост открытым для ручной публикации.")
                return False

            if not self._click_first_text(GROUP_DESTINATION_TEXTS, timeout=7000):
                warning("Не нашёл вариант 'поделиться в группе'. Возможно Facebook открыл другой интерфейс — нужна ручная помощь.")
                return False

            page.wait_for_timeout(1500)

            if target_group_name:
                try:
                    textbox = page.locator("input[type='search'], input[role='combobox'], input").first
                    textbox.fill(target_group_name, timeout=5000)
                    page.wait_for_timeout(2500)
                    page.get_by_text(target_group_name, exact=False).first.click(timeout=7000)
                    page.wait_for_timeout(1500)
                except Exception as exc:
                    warning(f"Не смог выбрать группу по имени '{target_group_name}': {exc}. Нужна ручная помощь.")
                    return False

            if self._click_first_text(POST_BUTTON_TEXTS, timeout=7000):
                info(f"Facebook forward: пост DB id {post_id} отправлен/поставлен на публикацию.")
                page.wait_for_timeout(2500)
                return True

            warning("Не нашёл финальную кнопку публикации. Оставляю окно для ручного завершения.")
            return False

        except Exception as exc:
            error(f"Facebook forward failed for DB id {post_id}: {exc}")
            return False

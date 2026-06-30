"""Запоминает текущую открытую Facebook-группу как цель публикации."""
from config import update_config
from facebook_session import FacebookSession
from logger import info, warning

fb = FacebookSession()
try:
    fb.start(headless=False)
    url = fb.page.url if fb.page else ""
    if "facebook.com/groups/" not in url:
        warning("Открой нужную Facebook-группу в окне браузера, потом нажми кнопку ещё раз.")
        raise SystemExit(1)
    update_config(facebook_target_group_url=url.split('?')[0], facebook_publish_enabled=True)
    info(f"Facebook-группа для публикации сохранена: {url.split('?')[0]}")
finally:
    fb.stop()

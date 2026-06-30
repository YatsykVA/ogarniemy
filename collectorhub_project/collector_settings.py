"""
CollectorHub - collector_settings.py
Update 31.

Хранит режим отправки объявлений:
- telegram
- facebook
- both

Файл настроек: data/collector_settings.json
"""

from __future__ import annotations

import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SETTINGS_FILE = DATA_DIR / "collector_settings.json"

SEND_MODES = ("telegram", "facebook", "both")
MODE_LABELS = {
    "telegram": "Telegram",
    "facebook": "Facebook",
    "both": "Telegram + Facebook",
}

DEFAULT_SETTINGS = {
    "send_mode": "telegram",
    "facebook_target_group_name": "",
    "facebook_target_group_url": "",
}


def ensure_settings_file() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not SETTINGS_FILE.exists():
        save_settings(DEFAULT_SETTINGS.copy())


def load_settings() -> dict:
    ensure_settings_file()
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}

    settings = DEFAULT_SETTINGS.copy()
    settings.update(data)

    if settings.get("send_mode") not in SEND_MODES:
        settings["send_mode"] = "telegram"

    save_settings(settings)
    return settings


def save_settings(settings: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(
        json.dumps(settings, ensure_ascii=False, indent=4),
        encoding="utf-8",
    )


def get_send_mode() -> str:
    return load_settings().get("send_mode", "telegram")


def set_send_mode(mode: str) -> str:
    mode = (mode or "telegram").strip().lower()
    if mode not in SEND_MODES:
        mode = "telegram"

    settings = load_settings()
    settings["send_mode"] = mode
    save_settings(settings)
    return mode


def cycle_send_mode() -> str:
    current = get_send_mode()
    order = list(SEND_MODES)
    try:
        index = order.index(current)
    except ValueError:
        index = 0
    return set_send_mode(order[(index + 1) % len(order)])


def mode_label(mode: str | None = None) -> str:
    return MODE_LABELS.get(mode or get_send_mode(), "Telegram")


def set_facebook_target_group(name: str = "", url: str = "") -> None:
    settings = load_settings()
    settings["facebook_target_group_name"] = (name or "").strip()
    settings["facebook_target_group_url"] = (url or "").strip()
    save_settings(settings)

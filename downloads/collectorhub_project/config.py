"""
CollectorHub - config.py

Единая конфигурация проекта.

Что делает:
- создает нужные папки проекта;
- хранит все пути в одном месте;
- загружает настройки из data/settings.json;
- поддерживает переменные окружения из .env;
- не требует хардкодить Telegram Token в коде;
- дает единый объект Config для всех модулей.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
import json
import os


# ============================================================
# Пути проекта
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

DATA_DIR = Path(os.getenv("COLLECTOR_DATA_DIR", BASE_DIR / "data")).resolve()
LOGS_DIR = DATA_DIR / "logs"
SESSIONS_DIR = DATA_DIR / "sessions"
PLAYWRIGHT_PROFILE_DIR = DATA_DIR / "playwright_profile"

SETTINGS_FILE = DATA_DIR / "settings.json"
DATABASE_FILE = DATA_DIR / "collector.db"
FACEBOOK_SESSION_FILE = DATA_DIR / "facebook_session.json"
ENV_FILE = Path(os.getenv("COLLECTOR_ENV_FILE", DATA_DIR / ".env")).resolve()


def ensure_directories() -> None:
    """Создает системные папки, если их еще нет."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    PLAYWRIGHT_PROFILE_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# Простая загрузка .env без обязательной зависимости python-dotenv
# ============================================================

def load_env_file(path: Path = ENV_FILE) -> None:
    """
    Загружает .env в os.environ.

    Формат:
        KEY=value
        KEY="value"
        KEY='value'

    Уже существующие переменные окружения не перезаписываются.
    """
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")

        if key and key not in os.environ:
            os.environ[key] = value


# ============================================================
# Модель настроек
# ============================================================

@dataclass
class AppConfig:
    # Telegram
    telegram_bot_token: str = ""
    telegram_group_id: str = "-1004294928850"

    # Facebook
    facebook_publish_enabled: bool = False
    facebook_target_group_url: str = ""
    facebook_target_group_name: str = ""
    send_destination: str = "telegram"  # telegram / facebook / both

    # Collector
    collector_enabled: bool = True
    check_interval_seconds: int = 300
    max_posts_per_group: int = 100
    headless_browser: bool = False

    # AI
    ai_enabled: bool = False
    ai_provider: str = "local"
    ai_model: str = ""

    # Filters
    require_phone: bool = False
    save_photos: bool = True

    # UI
    theme: str = "dark"
    language: str = "ru"

    # Internal / advanced
    version: str = "0.2"
    extra: dict[str, Any] = field(default_factory=dict)


def _merge_env(config: AppConfig) -> AppConfig:
    """
    Перекрывает настройки значениями из .env / environment.

    Это важно для секретов:
    Telegram token лучше держать в .env, а не в settings.json.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    group_id = os.getenv("TELEGRAM_GROUP_ID", "").strip()
    fb_group = os.getenv("FACEBOOK_TARGET_GROUP_URL", "").strip()
    send_destination = os.getenv("SEND_DESTINATION", "").strip().lower()

    if token:
        config.telegram_bot_token = token

    if group_id:
        config.telegram_group_id = group_id

    if fb_group:
        config.facebook_target_group_url = fb_group

    if send_destination in {"telegram", "facebook", "both"}:
        config.send_destination = send_destination

    return config


def default_config() -> AppConfig:
    """Возвращает дефолтные настройки приложения."""
    return AppConfig()


def load_config() -> AppConfig:
    """
    Загружает конфиг.

    Порядок:
    1. создаем папки;
    2. читаем .env;
    3. читаем data/settings.json;
    4. значения из .env перекрывают settings.json.
    """
    ensure_directories()
    load_env_file()

    if not SETTINGS_FILE.exists():
        config = default_config()
        save_config(config)
        return _merge_env(config)

    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        allowed = set(AppConfig.__dataclass_fields__.keys())
        clean_data = {k: v for k, v in data.items() if k in allowed}
        config = AppConfig(**clean_data)
    except Exception:
        # Если settings.json сломан, не падаем — создаем новый дефолтный.
        backup = SETTINGS_FILE.with_suffix(".broken.json")
        try:
            SETTINGS_FILE.replace(backup)
        except Exception:
            pass

        config = default_config()
        save_config(config)

    return _merge_env(config)


def save_config(config: AppConfig) -> None:
    """
    Сохраняет настройки в settings.json.

    ВАЖНО:
    Если token пришел из .env, он все равно может попасть в settings.json,
    если пользователь явно сохранил настройки через GUI.
    Позже в GUI сделаем маскировку и отдельное предупреждение.
    """
    ensure_directories()

    data = asdict(config)
    SETTINGS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=4),
        encoding="utf-8",
    )


def update_config(**kwargs: Any) -> AppConfig:
    """Обновляет часть настроек и сохраняет результат."""
    config = load_config()

    for key, value in kwargs.items():
        if hasattr(config, key):
            setattr(config, key, value)

    save_config(config)
    return config


# ============================================================
# Helpers для остальных модулей
# ============================================================

def get_database_path() -> Path:
    ensure_directories()
    return DATABASE_FILE


def get_logs_dir() -> Path:
    ensure_directories()
    return LOGS_DIR


def get_playwright_profile_dir() -> Path:
    ensure_directories()
    return PLAYWRIGHT_PROFILE_DIR


def get_facebook_session_file() -> Path:
    ensure_directories()
    return FACEBOOK_SESSION_FILE


def is_telegram_configured(config: AppConfig | None = None) -> bool:
    cfg = config or load_config()
    return bool(cfg.telegram_bot_token.strip() and cfg.telegram_group_id.strip())


def is_ai_enabled(config: AppConfig | None = None) -> bool:
    cfg = config or load_config()
    return bool(cfg.ai_enabled)


def is_facebook_publish_enabled(config: AppConfig | None = None) -> bool:
    cfg = config or load_config()
    return bool((getattr(cfg, "send_destination", "telegram") in {"facebook", "both"} or cfg.facebook_publish_enabled) and cfg.facebook_target_group_url.strip())


if __name__ == "__main__":
    cfg = load_config()
    print("CollectorHub config loaded")
    print(f"Base dir: {BASE_DIR}")
    print(f"Data dir: {DATA_DIR}")
    print(f"Database: {DATABASE_FILE}")
    print(f"Telegram configured: {is_telegram_configured(cfg)}")
    print(f"AI enabled: {is_ai_enabled(cfg)}")
    print(f"Facebook publish enabled: {is_facebook_publish_enabled(cfg)}")

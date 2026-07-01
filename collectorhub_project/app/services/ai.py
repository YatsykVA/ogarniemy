from .filtering import FilterResult
from app.config import get_settings

settings = get_settings()


async def improve_text_if_enabled(text: str, result: FilterResult) -> str:
    """Заглушка под AI. Сейчас безопасно возвращает оригинал.

    AI выключается через AI_ENABLED=false. Когда понадобится, сюда можно добавить
    OpenAI API, не меняя остальной код проекта.
    """
    if not settings.ai_enabled:
        return text
    return text

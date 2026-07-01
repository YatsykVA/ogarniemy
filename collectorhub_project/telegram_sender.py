"""
CollectorHub - telegram_sender.py

v24:
Формат:
👤 Автор
📞 Телефон
    Текст объявления
🔗 Ссылка на объявление

Финальная защита: перед отправкой ещё раз убирает
"показать больше / показать меньше / ещё / see more / show less".
"""

import re
import requests

from config import load_config, is_telegram_configured
from logger import info, warning, error


TRASH_RE = re.compile(
    r"("
    r"\b(ещё|еще|more|więcej)\b|"
    r"\bпоказать\s+(ещё|еще|больше|меньше)\b|"
    r"\bsee\s+more\b|\bshow\s+less\b|"
    r"\bzobacz\s+więcej\b|\bzobacz\s+mniej\b|"
    r"\bпоказать\s+перевод\b|\bsee\s+translation\b|\bzobacz\s+tłumaczenie\b"
    r")",
    flags=re.IGNORECASE | re.UNICODE,
)


STOP_RE = re.compile(
    r"^("
    r"смотреть\s+(другие\s+)?комментарии|посмотреть\s+(другие\s+)?комментарии|"
    r"показать\s+\d+\s+(ответ|ответа|ответов|комментар|комментария|комментариев)|"
    r"view\s+(more\s+)?comments?|see\s+(more\s+)?comments?|show\s+\d+\s+(repl|replies|comments?)|"
    r"zobacz\s+(więcej\s+)?komentarz|pokaż\s+\d+\s+(odpowied|odpowiedzi|komentar)|"
    r"нравится|like|lubię\s+to|ответить|reply|odpowiedz|поделиться|share|udostępnij|"
    r"комментировать\s+как\b.*|comment\s+as\b.*|skomentuj\s+jako\b.*|"
    r"ответить\s+как\b.*|reply\s+as\b.*|odpowiedz\s+jako\b.*"
    r")$",
    flags=re.IGNORECASE | re.UNICODE,
)

DATE_ABSOLUTE_RE = re.compile(
    r"^\s*\d{1,2}\s+("
    r"январ\w*|феврал\w*|март\w*|апрел\w*|ма[йя]|июн\w*|июл\w*|август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*|"
    r"sty\w*|lut\w*|mar\w*|kwi\w*|maj\w*|cze\w*|lip\w*|sie\w*|wrz\w*|paź\w*|paz\w*|lis\w*|gru\w*|"
    r")\s+((в|о|at)\s+)?\d{1,2}:\d{2}\s*$",
    flags=re.IGNORECASE | re.UNICODE,
)


class TelegramSender:
    def __init__(self):
        self.cfg = load_config()

    @property
    def api_url(self):
        return f"https://api.telegram.org/bot{self.cfg.telegram_bot_token}/sendMessage"

    def configured(self) -> bool:
        return is_telegram_configured(self.cfg)

    def _clean_final(self, text: str) -> str:
        result = []
        seen = set()

        for raw in (text or "").splitlines():
            line = raw.replace("\u00a0", " ").strip()
            if not line:
                result.append("")
                continue

            low = line.lower().strip()
            if STOP_RE.match(low):
                break
            if DATE_ABSOLUTE_RE.match(line):
                continue
            if re.match(r"^\s*[.·•●▪◦]+\s*$", line):
                continue
            if re.match(r"^\s*(и|and|oraz)\s+\d+\s*$", low):
                continue

            line = TRASH_RE.sub("", line).strip()
            line = re.sub(r"\s{2,}", " ", line).strip()

            if not line:
                continue

            key = line.lower()
            if key in seen:
                continue
            seen.add(key)
            result.append(line)

        while result and not result[0].strip():
            result.pop(0)
        while result and not result[-1].strip():
            result.pop()

        return "\n".join(result).strip()

    def _indent(self, text: str) -> str:
        text = self._clean_final(text)
        result = []
        for line in text.splitlines():
            if line.strip():
                result.append("    " + line.strip())
            else:
                result.append("")
        return "\n".join(result).strip("\n")

    def build_message(self, post: dict) -> str:
        author = post.get("author") or "Неизвестно"
        phone = post.get("phone") or "—"
        text = self._indent(post.get("text") or "")
        post_url = post.get("post_url") or "—"

        parts = [
            f"👤 {author}",
            f"📞 {phone}",
        ]

        if text:
            parts.append(text)

        parts.append(f"🔗 {post_url}")

        return "\n".join(parts).strip()

    def send(self, post: dict) -> bool:
        if not self.configured():
            warning("Telegram не настроен: заполни TELEGRAM_BOT_TOKEN и TELEGRAM_GROUP_ID в файле .env")
            return False

        payload = {
            "chat_id": self.cfg.telegram_group_id,
            "text": self.build_message(dict(post)),
            "disable_web_page_preview": False,
        }

        try:
            response = requests.post(self.api_url, json=payload, timeout=20)
            response.raise_for_status()
            data = response.json()

            if data.get("ok"):
                info("Объявление отправлено в Telegram")
                return True

            error(f"Telegram API error: {data}")

        except Exception as exc:
            error(f"Ошибка отправки Telegram: {exc}")

        return False

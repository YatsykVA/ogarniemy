"""
CollectorHub - words_manager.py

Главное правило:
- ключевые слова берутся ТОЛЬКО из data/keywords.txt
- слова-исключения берутся ТОЛЬКО из data/exclusions.txt
- программа сама НЕ добавляет слова
- программа сама НЕ удаляет слова
"""

from config import DATA_DIR

KEYWORDS_FILE = DATA_DIR / "keywords.txt"
EXCLUSIONS_FILE = DATA_DIR / "exclusions.txt"


KEYWORDS_TEMPLATE = """# keywords.txt
# Сюда вписываешь ТОЛЬКО свои ключевые слова.
# Одно слово или фраза на строку.
# Пустые строки и строки с # игнорируются.

"""

EXCLUSIONS_TEMPLATE = """# exclusions.txt
# Сюда вписываешь ТОЛЬКО свои слова-исключения.
# Одно слово или фраза на строку.
# Пустые строки и строки с # игнорируются.

"""


def ensure_word_files() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if not KEYWORDS_FILE.exists():
        KEYWORDS_FILE.write_text(KEYWORDS_TEMPLATE, encoding="utf-8")

    if not EXCLUSIONS_FILE.exists():
        EXCLUSIONS_FILE.write_text(EXCLUSIONS_TEMPLATE, encoding="utf-8")


def load_word_file(path: Path) -> list[str]:
    """
    Читает keywords.txt / exclusions.txt.

    Поддерживает оба нормальных формата:
    - одно слово или фраза на строку;
    - через запятую в одной строке.

    Это важно: раньше переносы строк склеивались в одну большую фразу,
    из-за чего русские ключевые слова могли почти не срабатывать.
    """
    ensure_word_files()

    result = []

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()

        if not line or line.startswith("#"):
            continue

        # Разрешаем комментарий после значения: ремонт # комментарий
        if "#" in line:
            line = line.split("#", 1)[0].strip()

        for raw in line.split(","):
            word = raw.strip().lower()

            if not word:
                continue

            if word not in result:
                result.append(word)

    return result


def load_keywords() -> list[str]:
    return load_word_file(KEYWORDS_FILE)


def load_exclusions() -> list[str]:
    return load_word_file(EXCLUSIONS_FILE)


def sync_words_to_database(conn) -> None:
    """
    TXT-файлы — единственный источник правды.
    База каждый запуск просто копирует туда то, что написано в TXT.
    """
    ensure_word_files()

    keywords = load_keywords()
    exclusions = load_exclusions()

    conn.execute("DELETE FROM keywords")
    conn.execute("DELETE FROM exclusions")

    for word in keywords:
        conn.execute("INSERT OR IGNORE INTO keywords(word) VALUES(?)", (word,))

    for word in exclusions:
        conn.execute("INSERT OR IGNORE INTO exclusions(word) VALUES(?)", (word,))

    conn.commit()

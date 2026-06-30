"""
CollectorHub - filters.py
Фильтрация объявлений.

v6:
- читает ключевые слова из data/keywords.txt;
- читает исключения из data/exclusions.txt;
- больше не надо лезть в базу руками.
"""

import re

from words_manager import load_keywords, load_exclusions


class Filters:
    def load_keywords(self):
        return sorted(load_keywords(), key=len, reverse=True)

    def load_exclusions(self):
        return sorted(load_exclusions(), key=len, reverse=True)

    def _contains(self, text: str, phrase: str) -> bool:
        phrase = (phrase or "").lower().strip()
        if not phrase:
            return False

        if " " in phrase:
            return phrase in text

        pattern = r"(?<!\w)" + re.escape(phrase) + r"(?!\w)"
        return re.search(pattern, text, flags=re.IGNORECASE | re.UNICODE) is not None

    def match(self, text: str):
        text = (text or "").lower()

        keywords = self.load_keywords()
        exclusions = self.load_exclusions()

        found_keywords = [k for k in keywords if self._contains(text, k)]
        found_exclusions = [e for e in exclusions if self._contains(text, e)]

        accepted = bool(found_keywords) and not found_exclusions

        return {
            "accepted": accepted,
            "keywords": found_keywords,
            "exclusions": found_exclusions,
        }


if __name__ == "__main__":
    f = Filters()
    sample = "Нужен сантехник в Свиднице. Телефон +48 500 600 700"
    print(f.match(sample))

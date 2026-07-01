import re
from dataclasses import dataclass


@dataclass
class FilterResult:
    matched_keyword: str | None
    matched_exclusion: str | None
    should_forward: bool


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def contains_phrase(text: str, phrase: str) -> bool:
    return normalize(phrase) in normalize(text)


def check_message(text: str, keywords: list[str], exclusions: list[str]) -> FilterResult:
    matched_keyword = next((kw for kw in keywords if contains_phrase(text, kw)), None)
    matched_exclusion = next((ex for ex in exclusions if contains_phrase(text, ex)), None)
    return FilterResult(
        matched_keyword=matched_keyword,
        matched_exclusion=matched_exclusion,
        should_forward=bool(matched_keyword and not matched_exclusion),
    )

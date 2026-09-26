import re

from .config import META

JP_RE = re.compile(r"[\u3040-\u30ff]")
LINK_RE = re.compile(r"(https?://|ftp://|mqqapi://|[a-zA-Z0-9\-]+\.[a-zA-Z]{2,})")
LETTER_RE = re.compile(r"[a-zA-Z\u4e00-\u9fff\u3040-\u30ff]")
DIGIT_RE = re.compile(r"\d")


def is_japanese(text: str) -> bool:
    if not text:
        return False
    jp_count = len(JP_RE.findall(text))
    return jp_count / len(text) >= META["japanese_ratio"]


def is_number(text: str) -> bool:
    if not text.strip():
        return False
    if not DIGIT_RE.search(text):  # 至少含一个数字
        return False
    return not LETTER_RE.search(text)  # 不含字母、汉字、假名


def is_link(text: str) -> bool:
    return bool(LINK_RE.search(text))


def get_category(text: str) -> str:
    if is_link(text):
        return "links"
    if is_japanese(text):
        return "japanese"
    if is_number(text):
        return "numbers"
    return "other"

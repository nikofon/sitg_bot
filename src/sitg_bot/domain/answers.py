import re
import unicodedata


def normalize_answer(value: str) -> str:
    """Normalize a submitted or accepted answer for exact comparison."""
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"[^\w\s]", " ", value)
    return " ".join(value.split())

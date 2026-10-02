import re


def is_email(value: str) -> bool:
    return bool(value) and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value) is not None

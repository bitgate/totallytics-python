from __future__ import annotations

import math
import re
from typing import Any, Callable, TypeVar

T = TypeVar("T")

_ORIGIN = re.compile(r"^[a-z][a-z0-9+.-]*://[^/?#]*", re.IGNORECASE | re.ASCII)
_QUERY_OR_FRAGMENT = re.compile(r"[?#]")


def path_of(url: str) -> str:
    origin = _ORIGIN.match(url)
    path = url[origin.end() :] if origin else url
    end = _QUERY_OR_FRAGMENT.search(path)
    return (path[: end.start()] if end else path) or "/"


def clip(value: str, limit: int) -> str:
    # Limits count UTF-16 code units like the JS SDK and the server, and never split a character
    head = value[:limit]
    if head.isascii() or max(head) <= "\uffff":
        return head

    units = 0
    for index, char in enumerate(head):
        units += 2 if ord(char) > 0xFFFF else 1
        if units > limit:
            return head[:index]
    return head


def describe_error(error: object) -> str | None:
    if error is None:
        return None
    try:
        if isinstance(error, BaseException):
            name = type(error).__name__
            message = str(error)
            return f"{name}: {message}" if message else name
        return str(error)
    except Exception:
        return None


def round_ms(value: float) -> float:
    # Math.round semantics (half up), so sums match the JS SDK to the last digit
    scaled = value * 1000
    whole = math.floor(scaled)
    return (whole + 1 if scaled - whole >= 0.5 else whole) / 1000


def attempt(callback: Callable[..., T], *args: Any, log: Callable[[str], None]) -> T | None:
    try:
        return callback(*args)
    except Exception as error:
        log(f"callback raised {describe_error(error)}")
        return None

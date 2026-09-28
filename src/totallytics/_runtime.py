from __future__ import annotations

import logging
import os
import threading

logger = logging.getLogger("totallytics")

_warned: set[str] = set()
_warned_lock = threading.Lock()


def normalize_key(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip() or None


def default_key() -> str | None:
    return normalize_key(os.environ.get("TOTALLYTICS_API_KEY"))


def warn(message: str) -> None:
    logger.warning("[totallytics] %s", message)


def warn_once(warning_id: str, message: str) -> None:
    with _warned_lock:
        if warning_id in _warned:
            return
        _warned.add(warning_id)
    warn(message)


def warn_no_key(debug: bool) -> None:
    if debug:
        warn_once("no-key", "no API key (set TOTALLYTICS_API_KEY or pass api_key), requests are not recorded")


def reset_after_fork() -> None:
    global _warned_lock
    _warned_lock = threading.Lock()
    _warned.clear()

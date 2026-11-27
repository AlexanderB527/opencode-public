"""Разбор кодовой строки запуска от плагина.

Плагин после кнопки «Разобрать» передаёт агенту строку с кодовым словом:

    TRIAGE::RERUN launch=1367360
    TRIAGE::RERUN 1367360
    TRIAGE::RERUN launch_id=1367360 deep

Токен уникален для скилла testops-rerun-triage, поэтому строка однозначно определяет
и скилл, и режим. Здесь — только парсинг: никакой сети и побочных эффектов.
"""

import re

CODE_WORD = "TRIAGE::RERUN"
LEVEL3_MARKERS = {"deep", "level=3", "level3", "уровень3"}

_ID_PATTERNS = (
    re.compile(r"\blaunch_id\s*=\s*(\d+)", re.IGNORECASE),
    re.compile(r"\blaunch\s*=\s*(\d+)", re.IGNORECASE),
    re.compile(r"\bid\s*=\s*(\d+)", re.IGNORECASE),
    re.compile(r"(?<![\w.=])(\d{4,})(?![\w.])"),
)


class CodeError(ValueError):
    """Кодовая строка не распознана (нет токена или ID)."""


def parse_code(text: str) -> dict:
    """Разбирает кодовую строку.

    Возвращает {"code": True, "launch_id": int|None, "level": 2|3, "raw": str}.
    Бросает CodeError, если токена нет (значит, это не наш вызов) — вызывающий
    код должен в этом случае вести себя как при обычном --launch-id.
    """
    raw = str(text or "")
    if CODE_WORD not in raw:
        raise CodeError(f"нет кодового слова {CODE_WORD}")

    launch_id = None
    for pattern in _ID_PATTERNS:
        m = pattern.search(raw)
        if m:
            launch_id = int(m.group(1))
            break

    level = 3 if any(marker in raw.lower() for marker in LEVEL3_MARKERS) else 2
    return {"code": True, "launch_id": launch_id, "level": level, "raw": raw.strip()}


def describe(parsed: dict) -> str:
    """Человеко-читаемое описание распознанной кодовой строки (для лога)."""
    lid = parsed.get("launch_id")
    return (f"код {CODE_WORD} | launch_id={lid if lid else 'НЕ НАЙДЕН'} "
            f"| уровень={parsed.get('level')}")

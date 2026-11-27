#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""team_canon.py — ЕДИНЫЙ источник нормализации имён команд TestOps.

Проблема, которую решает модуль: в TestOps названия команд приходят с
кириллическими гомоглифами (`ТАМUL`, `ТЕ`, `ТММБ`, `ТСПб`, `ТX`), а канон
папок эталонов, реестра джоб и артефактов — латиница (`TAMUL`, `TE`, `TMMB`,
`TSPB`, `TX-T9`). Если транслитерировать только `Т`, остаются «мусорные»
имена (`TАMUL`) и создаются мусорные папки.

Здесь: полная таблица транслита, канонический список команд, срез префикса
«SFA Команда», разрешение имени в канон (с учётом алиасов реестра).

Модуль НИЧЕГО не читает с диска и ничего не печатает при импорте.
Только stdlib.
"""

# Кириллица → латиница. Замена посимвольная и однозначная (1 символ → 1 символ),
# поэтому длины строк при транслите сохраняются — это используется при срезе префикса
# (strip_team_prefix удаляет ровно len(prefix) символов из исходной строки).
# Таблица должна покрывать весь алфавит: неполнота давала «TAMYЛ» вместо «TAMUL»
# (не хватало Л, а У мапился в Y) и создавала мусорные папки эталонов.
# Совместимость: ранее работавшие пары не изменены (А,Б,В,Е,К,М,Н,О,П,Р,С,Т,Х);
# исправлена только У: Y → U (единственная реальная команда с У — ТАМУЛ → TAMUL).
TEAM_TRANSLIT = {
    # заглавные (1:1; Ж/Ц/Ш/Щ даны однобуквенно, чтобы не ломать срез префикса)
    "А": "A", "Б": "B", "В": "B", "Г": "G", "Д": "D", "Е": "E", "Ё": "E",
    "Ж": "Z", "З": "Z", "И": "I", "Й": "Y", "К": "K", "Л": "L", "М": "M",
    "Н": "H", "О": "O", "П": "P", "Р": "P", "С": "S", "Т": "T", "У": "U",
    "Ф": "F", "Х": "X", "Ц": "C", "Ч": "C", "Ш": "S", "Щ": "S", "Ы": "Y",
    "Э": "E", "Ю": "U", "Я": "Y",
    # строчные
    "а": "A", "б": "B", "в": "B", "г": "G", "д": "D", "е": "E", "ё": "E",
    "ж": "Z", "з": "Z", "и": "I", "й": "Y", "к": "K", "л": "L", "м": "M",
    "н": "H", "о": "O", "п": "P", "р": "P", "с": "S", "т": "T", "у": "U",
    "ф": "F", "х": "X", "ц": "C", "ч": "C", "ш": "S", "щ": "S", "ы": "Y",
    "э": "E", "ю": "U", "я": "Y",
}

# Канонические имена команд = имена папок эталонов = ключи реестра джоб.
CANON_TEAMS = (
    "BIT", "ECOM", "TACO", "TAMUL", "TCAT", "TE", "TFU",
    "TMMB", "TPR", "TREX", "TSPB", "TT", "TX-T9",
)

CANON_SET = frozenset(CANON_TEAMS)

# Префиксы служебного поля TestOps («SFA Команда ТАМUL» / «SFA Команды …»).
# Сравниваются уже в «сплющенном» и транслитерированном виде (пробелы удалены),
# поэтому эталоны префиксов тоже прогоняются через translit — часть букв
# («Д», «Ы») в таблицу транслита не входит и остаётся кириллицей с обеих сторон.
TEAM_PREFIXES = ("SFAКОМАНДА", "SFAКОМАНДЫ")

# Алиасы на случай, когда реестр не передан или в нём нет нужной записи.
# Ключ — НОРМАЛИЗОВАННОЕ имя (normalize), значение — канон.
# `T9` и `TX` — две отдельные команды TestOps, но эталон и джоба у них общие: TX-T9.
DEFAULT_ALIASES = {
    "T9": "TX-T9",
    "TX": "TX-T9",
    "T9-TX": "TX-T9",
    "TX-T9": "TX-T9",
    # «TSРB» с кириллической Р нормализуется в TSPB (Р→P) — ключ уже нормализованный
    "TSPB": "TSPB",
}


def translit(value):
    """Посимвольная замена кириллицы на латиницу по TEAM_TRANSLIT."""
    if not value:
        return ""
    return "".join(TEAM_TRANSLIT.get(ch, ch) for ch in str(value))


def strip_team_prefix(value):
    """Срезает префикс «SFA Команда»/«SFA Команды» (с пробелами и без, любой регистр).

    Префикс распознаётся на «сплющенной» и транслитерированной копии, а срез
    делается по той же длине в исходной строке (транслит 1:1, длины совпадают).
    Если префикса нет — исходная строка возвращается без изменений (кроме strip).
    """
    text = str(value or "").strip()
    if not text:
        return ""
    compact = "".join(text.split())
    probe = translit(compact).upper()
    for prefix in TEAM_PREFIXES:
        # Эталон префикса тоже транслитерируем: иначе буквы вне таблицы
        # («Д», «Ы») не совпадут с probe.
        if probe.startswith(translit(prefix).upper()):
            # Префикс найден: остаток берём из compact (пробелы всё равно уберём)
            return compact[len(prefix):]
    return text


def normalize(value):
    """Полная нормализация: strip → срез префикса → удаление пробелов → транслит → upper."""
    if value is None:
        return ""
    text = strip_team_prefix(str(value).strip())
    text = "".join(str(text).split())
    return translit(text).upper()


def _registry_alias_map(registry):
    """Карта «нормализованное имя → канон» по реестру (каноны + их aliases)."""
    mapping = {}
    for key, conf in (registry or {}).items():
        if str(key).startswith("_"):
            continue
        canon = str(key)
        mapping.setdefault(normalize(canon), canon)
        if isinstance(conf, dict):
            for alias in conf.get("aliases") or []:
                mapping.setdefault(normalize(alias), canon)
    return mapping


def resolve_canon(value, registry=None):
    """Возвращает канонический ключ команды или None.

    Порядок: точное совпадение канона → алиас из реестра → нормализованное
    значение как канон → DEFAULT_ALIASES. Реестр — обычный dict (файлов не читаем).
    """
    raw = str(value or "").strip()
    norm = normalize(raw)

    # 1. Точное совпадение канона (например, уже пришло «TAMUL»)
    upper_raw = raw.upper()
    if upper_raw in CANON_SET:
        return upper_raw

    # 2. Алиасы реестра (и сами каноны реестра — тоже через map)
    mapping = _registry_alias_map(registry)
    if norm in mapping:
        return mapping[norm]

    # 3. Нормализованное значение как канон
    if norm in CANON_SET:
        return norm

    # 4. Встроенные алиасы (T9/TX → TX-T9 и т.п.)
    if norm in DEFAULT_ALIASES:
        return DEFAULT_ALIASES[norm]

    return None

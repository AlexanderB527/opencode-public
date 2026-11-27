#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
classifier.py — классификация ошибок TestOps по config/classify_rules.json
(перенесено из publish_confluence.py).
"""

import json
import re
from pathlib import Path

from _paths import RULES_PATH


def natural_key(text):
    """Стабильный ключ естественной сортировки (без TypeError на int/str)."""
    parts = []
    for chunk in re.split(r"(\d+)", str(text)):
        if chunk.isdigit():
            parts.append((0, int(chunk)))
        elif chunk:
            parts.append((1, chunk.lower()))
    return parts


def with_at(number):
    """Гарантирует префикс '@' у номера теста."""
    s = str(number).strip()
    return s if s.startswith("@") else "@" + s


# ------------------------- классификатор ошибок §5 (для §7.2A) -------------------------

# 🔴 Правила — ДАННЫЕ, не код: грузятся из <QA_HOME>/config/classify_rules.json
# (файл данных внутри скилла). Команды дополняют паттерны там, БЕЗ правки кода.
# Значения ниже — fallback, если файла правил нет.
_DEFAULT_ERROR_CLASSES = [
    ("Таймаут ожидания элемента", [
        r"Unable to find visible xpath", r"Unable to find visible field",
        r"expected to find visible xpath", r"Unable to find css",
        r"Capybara::ElementNotFound", r"Selenium::WebDriver::Error::TimeoutError",
        r"не дождались элемента"]),
    ("Таймаут выполнения", [
        r"execution expired", r"Failed to open TCP connection",
        r"Connection timed out", r"getaddrinfo", r"Net::ReadTimeout"]),
    ("Несовпадение формата даты", [
        r"expected:.*\d{2}:\d{2}.*got:.*\d{1}:\d{2}", r"ведущим нулём", r"ведущий ноль"]),
    ("Ошибка утверждения", [
        r"expected:.*got:", r"AssertionError", r"expected not to find",
        r"ОР:.*ФР:", r"не выделена красным", r"не включает в себя"]),
    ("Ошибка тестовых данных", [
        r"Не удалось создать клиента", r"отсутствует в таблице",
        r"undefined method", r"nil:NilClass", r"NoMethodError"]),
    ("Элемент не описан в коде", [r"не описан в коде", r"не описан в page object"]),
    ("Ошибка переключения фрейма", [r"не переключились на фрейм", r"No node with given id found"]),
    ("Неоднозначное совпадение", [r"Ambiguous match", r"found \d+ elements matching"]),
    ("Таймаут ожидания статуса", [r"Не дождались статуса", r"в течение \d+ секунд", r"в течение \d+ минут"]),
    ("Проверка наличия в списке", [r"присутствует в списке", r"отсутствует в списке"]),
    ("Утверждение состояния элемента", [r"активен", r"отображается", r"выбран"]),
    ("Несовпадение данных", [r"не совпадает", r"не соответствует", r"отличается от ожидаемого"]),
    ("500 Internal Server Error", [r"500 Internal Server Error", r"500 ошибк"]),
    ("element click intercepted", [r"element click intercepted"]),
    ("Connection reset by peer", [r"Connection reset by peer"]),
    ("Не удалось создать клиента в EQ и MDM", [r"Не удалось создать клиента в EQ"]),
]

RULES = {"error_classes": _DEFAULT_ERROR_CLASSES}
_CLASS_COMPILED = []


def _rebuild_classifier(rules):
    """(Пере)собирает скомпилированные паттерны из правил."""
    global _CLASS_COMPILED, RULES
    RULES = rules
    _CLASS_COMPILED = [(name, [re.compile(p, re.DOTALL) for p in pats])
                       for name, pats in rules.get("error_classes", _DEFAULT_ERROR_CLASSES)]


def load_rules(path=None):
    """Загружает правила классификации из JSON (по умолчанию config/classify_rules.json).

    Отсутствие файла — не ошибка: используются встроенные (fallback) паттерны.
    """
    p = Path(path) if path else RULES_PATH
    rules = dict(RULES)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            data = {k: v for k, v in data.items() if not k.startswith("_")}
            rules.update(data)
        except (json.JSONDecodeError, OSError):
            pass
    _rebuild_classifier(rules)
    return rules


_rebuild_classifier(RULES)  # инициализация при импорте (fallback)


def classify_error(message):
    """Класс ошибки по правилам §5 (первое совпадение). Нет совпадения → 'Другое'."""
    m = message or ""
    for name, pats in _CLASS_COMPILED:
        for p in pats:
            if p.search(m):
                return name
    return "Другое"

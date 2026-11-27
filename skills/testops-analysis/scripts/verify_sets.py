#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_sets.py — инструментальная верификация @-списка по эталону.

Заменяет поштучный grep каждого номера «в голове»: скрипт читает два файла,
считает set(at_list) - set(etalon) и выдаёт жёсткий вердикт.
Это механика п.3.6 скилла testops-run-analysis — формат вывода зафиксирован.

Использование:
  py -X utf8 verify_sets.py --at-list ".qa-artifacts/launches/12345/at_list.txt" --etalon ".qa-artifacts/etalons/TX-T9/etalon_HF.txt"
  py -X utf8 verify_sets.py --at-list ".qa-artifacts/launches/12345/at_list.txt" --expected-set ".qa-artifacts/launches/12345/T9-TX/expected_set.json"

Режим --expected-set (tag-режим): референс = expected_numbers из expected_set.json
(надмножество: команда∩эталон ∪ тег). --etalon в этом режиме не обязателен.

Exit code:
  0 — ЧИСТО (лишних нет)
  1 — ЕСТЬ ЛИШНИЕ (публикация заблокирована)
  2 — ошибка входа (файлы не найдены / пусто)
"""
import argparse
import json
import re
import sys
from pathlib import Path

TEST_NUMBER_RE = re.compile(r"(?<!\d)(\d{2}(?:\.\d{2,3})+)(?!\d)")


def read_numbers(path: Path) -> tuple[list[str], list[str]]:
    """Возвращает (номера в порядке файла, уникальные). Номера без префикса @."""
    if not path.exists():
        raise FileNotFoundError(f"Файл не найден: {path}")
    raw = None
    for enc in ("utf-8", "cp1251"):
        try:
            raw = path.read_text(encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    if raw is None:
        raw = path.read_text(encoding="utf-8", errors="replace")
    numbers = []
    for line in raw.splitlines():
        line = line.strip().lstrip("@")
        if not line:
            continue
        m = TEST_NUMBER_RE.search(line)
        if m:
            numbers.append(m.group(1))
    unique = list(dict.fromkeys(numbers))  # сохраняем порядок первого вхождения
    return numbers, unique


def natural_key(num: str) -> list[int]:
    """Естественная сортировка номеров по числовым компонентам (02.x < 10.x < 39.x)."""
    return [int(c) for c in num.split(".")]


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(description="Верификация @-списка по эталону (set-разность)")
    ap.add_argument("--at-list", required=True, help="Файл @-списка (по одному номеру на строку)")
    ap.add_argument("--etalon", default=None,
                    help="Файл эталона (нужен, если не задан --expected-set)")
    ap.add_argument("--expected-set", default=None,
                    help="Путь к expected_set.json (tag-режим: референс = expected_numbers)")
    args = ap.parse_args()

    try:
        _, at_unique = read_numbers(Path(args.at_list))
    except FileNotFoundError as e:
        print(f"ОШИБКА: {e}")
        return 2

    # ---- Tag-режим: референс = expected_numbers из expected_set.json (--etalon не обязателен) ----
    if args.expected_set:
        exp_path = Path(args.expected_set)
        if not exp_path.exists():
            print(f"ОШИБКА: expected_set.json не найден: {exp_path}")
            return 2
        try:
            expected = json.loads(exp_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"ОШИБКА чтения {exp_path}: {e}")
            return 2
        ref_numbers = list(expected.get("expected_numbers") or [])
        if not ref_numbers:
            print(f"ОШИБКА: {exp_path} не содержит expected_numbers")
            return 2

        at_set = set(at_unique)
        ref_set = set(ref_numbers)
        k = len(at_set)
        m = len(at_set & ref_set)
        extra = sorted(at_set - ref_set, key=natural_key)
        print("ВЕРИФИКАЦИЯ @-СПИСКА (tag-режим, референс = expected_set):")
        print(f"- Номеров в @-списке: {k}")
        print(f"- Из них ∈ expected_set: {m}")
        print(f"- Из них ∉ expected_set: {len(extra)} → {' '.join('@' + n for n in extra) if extra else '(нет)'}")
        if extra:
            print("- Вывод: ЕСТЬ ЛИШНИЕ — публикация ЗАБЛОКИРОВАНА")
            return 1
        print("- Вывод: ЧИСТО")
        return 0

    # ---- Прежний режим: референс = --etalon ----
    if not args.etalon:
        print("ОШИБКА: не задан ни --etalon, ни --expected-set")
        return 2
    try:
        _, etalon_unique = read_numbers(Path(args.etalon))
    except FileNotFoundError as e:
        print(f"ОШИБКА: {e}")
        return 2
    if not etalon_unique:
        print(f"ОШИБКА: эталон пуст (N=0): {args.etalon}")
        return 2

    at_set = set(at_unique)
    etalon_set = set(etalon_unique)
    k = len(at_set)
    m = len(at_set & etalon_set)
    extra = sorted(at_set - etalon_set, key=natural_key)

    # Обязательный формат-блок из п.3.6 скилла
    print("ВЕРИФИКАЦИЯ @-СПИСКА:")
    print(f"- Номеров в @-списке: {k}")
    print(f"- Из них ∈ etalon: {m}")
    print(f"- Из них ∉ etalon: {len(extra)} → {' '.join('@' + n for n in extra) if extra else '(нет)'}")
    if extra:
        print("- Вывод: ЕСТЬ ЛИШНИЕ — публикация ЗАБЛОКИРОВАНА")
        return 1
    print("- Вывод: ЧИСТО")
    return 0


if __name__ == "__main__":
    sys.exit(main())

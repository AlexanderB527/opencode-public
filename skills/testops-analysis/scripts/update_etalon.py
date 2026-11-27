#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update_etalon.py — дополнение эталона тестов команды новыми номерами.

Назначение: дописать подтверждённые номера в существующий etalon_<тип>.txt
в формате "<номер> <Название>", с естественной сортировкой, дедупом и
идемпотентностью (повторный запуск не меняет файл, exit 0).

Использование:
  python update_etalon.py --etalon "%USERPROFILE%/.qa-artifacts/etalons/TX-T9/etalon_QG.txt" --add "16.01.05 16.01.06"
  python update_etalon.py --etalon "%USERPROFILE%/.qa-artifacts/etalons/TX-T9/etalon_QG.txt" --input "candidates.json"

Путь по умолчанию — локальный кэш `~/.qa-artifacts/etalons/<TEAM>/etalon_<тип>.txt`.
Источник истины — ветка qa-analysis-data (AUTOTEST/sfa): правка кэша не меняет
эталон у остальных, изменения в ветку переносятся отдельным PR.

--input принимает либо путь к JSON-файлу, либо инлайн-JSON списка [{number, name}].
--add принимает номера через пробел (без названий).

Перед записью делается .bak-копия исходного файла (etalon_<тип>.txt.bak).
В stdout: added, skipped (уже есть), total_after.
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ETALON_NUMBER_RE = re.compile(r"\d{2}(?:\.\d{2,3}){1,5}")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def refresh_pin(etalon_path: Path) -> str:
    """Пересчитывает пломбу <etalon>.sha256 после правки эталона.

    🔴 Обязательно: scan_testops.py сверяет эталон с пломбой и падает при расхождении.
    Без пересчёта любая правка через update_etalon.py блокировала бы следующий скан
    сообщением «эталон не совпадает с пломбой». Пломбы нет — не создаём (старый кэш).
    """
    side = Path(str(etalon_path) + ".sha256")
    if not side.is_file():
        return "пломбы нет — не создавали"
    side.write_text(_sha256_file(etalon_path), encoding="utf-8")
    return f"пломба обновлена: {side.name}"


def natural_sort_key(n: str):
    """Естественная сортировка числовых компонент '02.01' < '10.01'."""
    return [int(x) for x in n.split(".")]


def parse_etalon_entries(raw: str) -> dict:
    """Читает текст эталона в {number: строка 'номер Название'}.

    Номер извлекается тем же паттерном, что read_etalon в scan_testops.py:
    в начале строки, до 6 компонент. Строки без номера (пустые/комментарии) не сохраняются.
    """
    entries: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        m = ETALON_NUMBER_RE.search(line)
        if not m:
            continue
        number = m.group(0)
        entries[number] = line
    return entries


def load_new_items(value: str) -> dict:
    """Возвращает {number: name} из --input: инлайн-JSON списка или путь к JSON-файлу."""
    value = value.strip()
    if value.startswith("["):
        data = json.loads(value)
    else:
        data = json.loads(Path(value).read_text(encoding="utf-8-sig"))
    items: dict[str, str] = {}
    for obj in data:
        if not isinstance(obj, dict):
            continue
        number = str(obj.get("number") or "").strip()
        if not number:
            continue
        name = str(obj.get("name") or "").strip()
        items.setdefault(number, name)
    return items


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(description="Дополнение эталона тестов новыми номерами")
    ap.add_argument("--etalon", required=True, help="Путь к etalon_<тип>.txt")
    ap.add_argument("--add", default="", help="Номера через пробел, напр. '16.01.05 16.01.06'")
    ap.add_argument("--input", default="", help="JSON [{number, name}] — путь к файлу или инлайн-JSON")
    args = ap.parse_args()

    if bool(args.add) == bool(args.input):
        print("ОШИБКА: укажи ровно одно из --add или --input")
        return 1

    etalon_path = Path(args.etalon)
    if not etalon_path.exists():
        print(f"ОШИБКА: эталон не найден: {etalon_path}")
        return 1

    raw = etalon_path.read_text(encoding="utf-8-sig")
    entries = parse_etalon_entries(raw)

    if args.add:
        new_items = {}
        for token in args.add.split():
            token = token.strip()
            if token and ETALON_NUMBER_RE.fullmatch(token):
                new_items.setdefault(token, "")
    else:
        try:
            new_items = load_new_items(args.input)
        except (json.JSONDecodeError, OSError) as e:
            print(f"ОШИБКА чтения --input: {e}")
            return 1

    added = 0
    skipped = 0
    for number, name in new_items.items():
        if number in entries:
            skipped += 1
            continue
        entries[number] = f"{number} {name}".strip()
        added += 1

    if added == 0:
        print("нет новых номеров")
        print(f"added: {added}")
        print(f"skipped (уже есть): {skipped}")
        print(f"total_after: {len(entries)}")
        return 0

    sorted_numbers = sorted(entries.keys(), key=natural_sort_key)
    new_content = "\n".join(entries[n] for n in sorted_numbers) + "\n"

    bak_path = Path(str(etalon_path) + ".bak")
    bak_path.write_text(raw, encoding="utf-8")
    etalon_path.write_text(new_content, encoding="utf-8")
    pin_msg = refresh_pin(etalon_path)

    print(f"added: {added}")
    print(f"skipped (уже есть): {skipped}")
    print(f"total_after: {len(entries)}")
    print(pin_msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())

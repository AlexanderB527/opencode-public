#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
render_markdown.py — детерминированный рендер markdown-страниц разбора запуска
и перезапуска из артефактов (expected_set.json / rerun_diff.json).

Числа, таблицы и @-список считает Python: агент передаёт готовый текст в MCP
atlassian как есть, ничего не пересчитывая. Только стандартная библиотека.

Режимы:
  render_markdown.py --expected-set <expected_set.json> [--out <file>]
  render_markdown.py --rerun-diff <rerun_diff.json> \
      [--classification <rerun_classification.json>] [--out <file>]

Единственный рендерер: тот же markdown страницы перезапуска печатает
`classify_rerun.py --chat` (функция render_classification), без копии разметки.

Exit: 0 = ok, 1 = несогласованность данных (сообщение в stderr).
"""
import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from classifier import classify_error, natural_key, with_at  # noqa: E402

STATUS_UP = {
    "passed": "PASS", "failed": "FAIL", "broken": "BROKEN", "skipped": "SKIP",
}
# Статусы, которые НЕ считаются падением (для счёта результатов в группах).
PASSED_STATUSES = frozenset({"passed", "pass"})
MSG_LIMIT = 140


def _cell(text):
    """Безопасная ячейка markdown-таблицы (без переводов строк и '|')."""
    s = " ".join(str(text if text is not None else "").split())
    return s.replace("|", "\\|")


def _short(text, limit=MSG_LIMIT):
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _status_up(status):
    return STATUS_UP.get(str(status or "").lower(), str(status or "").upper() or "—")


def _num_key(number):
    return natural_key(str(number).lstrip("@"))


def _load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _nonpass_rows(expected):
    """[(номер_с_@, статус, сообщение)] только не-PASS, по natural_key."""
    rows = []
    for num, meta in (expected.get("statuses") or {}).items():
        if str(meta.get("status") or "").lower() == "passed":
            continue
        msgs = meta.get("messages")
        if msgs is None:
            msgs = [meta.get("reason", "")]
        msg = next((m for m in (msgs or []) if m), "")
        rows.append((with_at(num), _status_up(meta.get("status")), msg))
    rows.sort(key=lambda r: _num_key(r[0]))
    return rows


def _coverage_section(expected, diagnosis=None):
    """🔴 Полнота охвата: сколько эталона прогнано. Нет данных -> ''.

    Появляется только когда прогон неполный (etalon_missing_numbers непуст):
    иначе секция была бы шумом на каждом обычном скане.

    `diagnosis` — результат `diagnose_skips.py` (skip_diagnosis.json). Если он
    есть, в блок добавляется разбивка «причина -> что делать»: без причины
    список пропусков не отвечает на главный вопрос оператора «это чинить или
    перезапускать».
    """
    missing = expected.get("etalon_missing_numbers") or []
    if not missing:
        return ""
    total = expected.get("etalon_total")
    covered = expected.get("etalon_covered")
    pct = expected.get("etalon_coverage_etalon_pct")
    lines = [
        "## ⚠️ Прогон НЕПОЛНЫЙ — сначала это, потом статистика",
        "",
        f"**{len(missing)} из {total} тестов команды в прогоне не участвовали.** "
        f"Покрытие эталона — **{pct}%**. У этих тестов НЕТ статуса: они не проходили "
        "и не падали. Судить об их работоспособности по этому запуску нельзя, и "
        "процент прохождения ниже посчитан только по фактически прогнанным.",
        "",
        f"| Эталон | Прогнано | Не запускалось | Покрытие |",
        "|---:|---:|---:|---:|",
        f"| {total} | {covered} | {len(missing)} | {pct}% |",
        "",
    ]
    if diagnosis and diagnosis.get("verdicts"):
        advice = {
            "тест отключён в репозитории (features/excluded)":
                "вернуть файл в `features/Tests` или убрать тест из эталона",
            "нет файла с таким номером (номер может быть тегом внутри feature)":
                "проверить вручную: номер может быть тегом внутри другого feature",
            "теги не передавались в сборку": "добавить номера в `tags` сборки",
            "сборка прервана": "перезапустить и НЕ прерывать сборку",
            "сборка упала до этих тестов": "разобрать падение сборки, затем перезапустить",
            "тег передан, но сценарий не стартовал":
                "перезапустить (тег передан, сценарий не дошёл)",
            "не установлено: feature есть, статус сборки недоступен":
                "перезапустить их отдельно и смотреть результат",
            "неизвестно": "проверить вручную",
        }
        lines += ["**Почему не запускались** (`skip_diagnosis.json`):", "",
                  "| Причина | Тестов | Что делать |", "|---|---:|---|"]
        for cause, count in sorted(diagnosis["verdicts"].items(), key=lambda kv: -kv[1]):
            lines.append(f"| {cause} | {count} | {advice.get(cause, 'проверить вручную')} |")
        lines.append("")
    lines += [
        f"**Не запускались ({len(missing)}):** "
        + " ".join(with_at(str(n)) for n in missing),
        "",
        "> Для полного покрытия их нужно запустить отдельно "
        "(`dispatch_team_reruns.py --include-missing`). Причину пропуска даёт "
        "`diagnose_skips.py` — часть тестов может быть отключена в репозитории, "
        "и перезапуск их не подхватит.",
    ]
    return "\n".join(lines)


def _stats_section(counts):
    total = int(counts.get("total", 0) or 0)
    passed = int(counts.get("passed", 0) or 0)
    failed = int(counts.get("failed", 0) or 0)
    broken = int(counts.get("broken", 0) or 0)
    skipped = int(counts.get("skipped", 0) or 0)
    rate = counts.get("pass_rate", 0)
    return "\n".join([
        "## Статистика",
        "",
        "| Всего | Прошло | Упало | Сломано | Пропущено | Процент прохождения |",
        "|---:|---:|---:|---:|---:|---:|",
        f"| {total} | {passed} | {failed} | {broken} | {skipped} | {rate}% |",
    ])


def _at_list_section(numbers):
    return "\n".join([
        f"## @-список ({len(numbers)})",
        "",
        " ".join(numbers) if numbers else "—",
    ])


def _grouped_errors_section(rows, expected=None):
    """Сгруппированная таблица §7.2A: одна строка = один класс ошибки.

    rows: [(номер_с_@, статус, сообщение)] — по одной строке на НОМЕР.
    Параметризованные Examples дают несколько результатов на номер, поэтому
    показываем обе величины: «Тестов» (уникальные номера) и «Результатов»
    (с учётом параметров). Иначе 39 против 42 выглядят как ошибка расчёта.

    Возвращает (текст, множество номеров, сумма тестов, сумма результатов).
    """
    statuses = (expected or {}).get("statuses") or {}
    groups = {}
    for num, _status, msg in rows:
        raw = str(num).lstrip("@")
        cls = classify_error(msg)
        g = groups.setdefault(cls, {"numbers": set(), "results": 0})
        g["numbers"].add(raw)
        # Не-PASS результаты по номеру: параметризованные Examples дают несколько.
        # Берём statuses[] (все результаты номера) и считаем только непрошедшие —
        # meta["count"] включал бы и PASS, что завышало бы «Результатов».
        meta = statuses.get(raw) or {}
        per_statuses = meta.get("statuses") or []
        nonpass_n = sum(1 for s in per_statuses
                        if str(s or "").lower() not in PASSED_STATUSES)
        g["results"] += nonpass_n or 1

    lines = [
        "## Затронутые области (по классу ошибки)",
        "",
        "| Ошибка | Тестов | Результатов | Теги |",
        "|---|---:|---:|---|",
    ]
    all_numbers = set()
    total_tests = 0
    total_results = 0
    for cls, g in sorted(groups.items(), key=lambda x: (-len(x[1]["numbers"]), x[0])):
        nums = sorted(g["numbers"], key=natural_key)
        all_numbers |= g["numbers"]
        total_tests += len(g["numbers"])
        total_results += g["results"]
        tags = " ".join(with_at(n) for n in nums)
        lines.append(
            f"| {_cell(cls)} | {len(g['numbers'])} | {g['results']} | {tags} |"
        )
    lines.append(f"| **Итого** | **{total_tests}** | **{total_results}** | |")
    return "\n".join(lines), all_numbers, total_tests, total_results


def _new_tests_section(expected):
    """Новые тесты команды, которых нет в эталоне. Нет данных -> ''."""
    tests = expected.get("new_tests") or []
    if not tests:
        return ""
    lines = [
        "## Новые тесты (вне эталона)",
        "",
        "| Номер | Название | Команда | Статус |",
        "|---|---|---|---|",
    ]
    for item in tests:
        if not isinstance(item, dict):
            continue
        num = with_at(str(item.get("number") or "").lstrip("@"))
        name = str(item.get("name") or "")
        # Убираем дублирующий номер из начала названия.
        if name.startswith(num.lstrip("@")):
            name = name[len(num.lstrip("@")):].strip()
        statuses = item.get("statuses") or []
        status = ", ".join(str(s).upper() for s in statuses) if statuses else "—"
        lines.append(
            f"| {num} | {_cell(name)} | {_cell(item.get('team') or '')} | {_cell(status)} |"
        )
    lines += [
        "",
        "> Эти тесты ещё не входят в эталон команды. В @-список они не включаются "
        "автоматически: добавление в эталон — решение человека (`update_etalon.py`).",
    ]
    return "\n".join(lines)


def _detail_section(rows):
    lines = [
        "## Детальная таблица не-PASS",
        "",
        "| Номер | Статус | Класс ошибки | Сообщение (кратко) |",
        "|---|---|---|---|",
    ]
    for num, status, msg in rows:
        lines.append(f"| {num} | {status} | {_cell(classify_error(msg))} | {_cell(_short(msg))} |")
    return "\n".join(lines)


def _teams_section(expected):
    """Статистика по командам: teams_stats → иначе разбивка по origin. Нет данных → ''."""
    ts = expected.get("teams_stats") or {}
    if ts:
        lines = [
            "## Статистика по командам",
            "",
            "| Команда | Всего | PASS | FAIL | BROKEN | SKIP | Процент прохождения |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        order = [t for t in (expected.get("teams") or ts.keys()) if t in ts]
        order += [t for t in ts if t not in order]
        for team in order:
            v = ts[team] or {}
            total = int(v.get("total", 0) or 0)
            passed = int(v.get("passed", 0) or 0)
            rate = f"{passed / total * 100:.1f}" if total else "0.0"
            lines.append(
                f"| {_cell(team)} | {total} | {passed} | {int(v.get('failed', 0) or 0)} "
                f"| {int(v.get('broken', 0) or 0)} | {int(v.get('skipped', 0) or 0)} | {rate}% |"
            )
        c = expected.get("counts") or {}
        total = int(c.get("total", 0) or 0)
        passed = int(c.get("passed", 0) or 0)
        rate = f"{passed / total * 100:.1f}" if total else "0.0"
        lines.append(
            f"| **Итого** | {total} | {passed} | {int(c.get('failed', 0) or 0)} "
            f"| {int(c.get('broken', 0) or 0)} | {int(c.get('skipped', 0) or 0)} | {rate}% |"
        )
        return "\n".join(lines)

    origin = {}
    for meta in (expected.get("statuses") or {}).values():
        key = meta.get("origin")
        if not key:
            continue
        agg = origin.setdefault(key, {"total": 0, "passed": 0})
        agg["total"] += 1
        if str(meta.get("status") or "").lower() == "passed":
            agg["passed"] += 1
    if not origin:
        return ""

    lines = [
        "## Статистика по командам",
        "",
        "| Источник | Номеров | PASS | Не-PASS |",
        "|---|---:|---:|---:|",
    ]
    for key in sorted(origin):
        agg = origin[key]
        lines.append(
            f"| {_cell(key)} | {agg['total']} | {agg['passed']} | {agg['total'] - agg['passed']} |"
        )
    return "\n".join(lines)


def render_expected_set(expected, diagnosis=None):
    """Markdown страницы разбора запуска. Возвращает (text, problems).

    `diagnosis` — содержимое `skip_diagnosis.json` (см. diagnose_skips.py),
    опционально: без него блок неполноты печатается без разбивки по причинам.
    """
    problems = []
    counts = expected.get("counts") or {}
    rows = _nonpass_rows(expected)
    numbers = [r[0] for r in rows]
    nonpass = int(counts.get("nonpass", 0) or 0)
    if len(numbers) != nonpass:
        problems.append(
            f"длина @-списка ({len(numbers)}) не совпала с counts.nonpass ({nonpass})"
        )

    grouped, grouped_numbers, grouped_tests, grouped_results = \
        _grouped_errors_section(rows, expected)

    # Гейты §7.2A: таблица групп должна быть полной и согласованной.
    at_set = {str(n).lstrip("@") for n in numbers}
    if grouped_numbers != at_set:
        problems.append(
            f"объединение тегов групп ({len(grouped_numbers)}) "
            f"!= @-список ({len(at_set)})"
        )
    if grouped_tests != len(at_set):
        problems.append(
            f"сумма «Тестов» групп ({grouped_tests}) != числа не-PASS номеров ({len(at_set)})"
        )

    # 🔴 Блок неполноты идёт ПЕРВЫМ: если половины команды не было в прогоне,
    # это главный вывод страницы, а не сноска под статистикой.
    blocks = [f"# Анализ прогона {expected.get('launch_name', '')}".rstrip(), ""]
    coverage = _coverage_section(expected, diagnosis)
    if coverage:
        blocks += [coverage, ""]
    blocks += [
        _stats_section(counts),
        "",
    ]
    blocks += [
        _at_list_section(numbers),
        "",
        grouped,
        "",
    ]
    teams = _teams_section(expected)
    if teams:
        blocks += [teams, ""]
    new_tests = _new_tests_section(expected)
    if new_tests:
        blocks += [new_tests, ""]
    blocks.append(_detail_section(rows).replace(
        "## Детальная таблица не-PASS", "## Детально по тестам"))
    return "\n".join(blocks) + "\n", problems


SECTION_TITLES = {
    "product": "🔴 Дефекты продукта",
    "autotest": "🟡 Ошибки автотеста",
    "system": "⚪ Системные",
    "triage": "❓ Триаж (low confidence)",
}
OWNER_TO_SECTION = {"дефект": "product", "автотест": "autotest", "система": "system"}
CATEGORY_TO_SECTION = {
    "product": "product", "testdata": "product",
    "locator": "autotest", "testcode": "autotest",
    "infra": "system", "env": "system",
}


def _cell_md(text):
    """Ячейка таблицы с переносами строк как '<br>' и экранированным '|'."""
    s = "" if text is None else str(text)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = s.replace("|", "\\|").replace("\n", "<br>")
    return s or "—"


def _status_label(item):
    """Статус теста + пометка по prev_status: 'BROKEN ✦повтор'."""
    base = str(item.get("status") or "").upper() or "—"
    prev = str(item.get("prev_status") or "")
    if prev in ("failed", "broken"):
        return base + " ✦повтор"
    if prev == "passed":
        return base + " флейк"
    if prev == "absent":
        return base + " новый"
    return base


def _routing_text(routing, fallback_action=""):
    """owner_team · severity · action · where · критерий: criterion (пустые части опускаются)."""
    if not isinstance(routing, dict):
        return fallback_action or ""
    action = routing.get("action") or fallback_action
    parts = [str(x) for x in (
        routing.get("owner_team"), routing.get("severity"), action, routing.get("where"),
    ) if x]
    text = " · ".join(parts)
    criterion = routing.get("criterion")
    if criterion:
        text = f"{text} · критерий: {criterion}" if text else f"критерий: {criterion}"
    return text


def _majority(values):
    vals = [str(v) for v in values if v]
    if not vals:
        return ""
    return max(sorted(set(vals)), key=vals.count)


def _unit_label(unit):
    return str(unit.get("label") or unit.get("group_label") or unit.get("error_class") or "—")


def _unit_count(unit):
    return int(unit.get("count", len(unit.get("numbers") or [])) or 0)


def _unit_numbers(unit):
    return [str(n) for n in (unit.get("numbers") or []) if n]


def _unit_type(unit):
    return str(unit.get("category_label") or unit.get("owner") or unit.get("category") or "—")


def _items_by_number(classification):
    return {str(it.get("number")): it for it in (classification or {}).get("items") or []}


def _unit_confidence(unit, items_by_number):
    """Confidence единицы: своё поле → большинство по её тестам → '—'."""
    if unit.get("confidence"):
        return str(unit["confidence"])
    return _majority(
        items_by_number[n].get("confidence")
        for n in _unit_numbers(unit) if n in items_by_number
    ) or "—"


def _unit_action(unit, items_by_number):
    """Действие единицы: своё поле → большинство по её тестам → ''."""
    if unit.get("action"):
        return str(unit["action"])
    return _majority(
        items_by_number[n].get("action")
        for n in _unit_numbers(unit) if n in items_by_number
    )


def _unit_recommendation(unit, items_by_number):
    """Рекомендация: поле recommendation → routing → action единицы."""
    if unit.get("recommendation"):
        return str(unit["recommendation"])
    routing = unit.get("routing") if isinstance(unit.get("routing"), dict) else {}
    return _routing_text(routing, fallback_action=_unit_action(unit, items_by_number))


def _section_of(unit, items_by_number):
    """Секция страницы по confidence/владельцу/категории: product|autotest|system|triage."""
    if _unit_confidence(unit, items_by_number).lower() == "low":
        return "triage"
    owner = str(unit.get("owner") or "")
    if owner in OWNER_TO_SECTION:
        return OWNER_TO_SECTION[owner]
    return CATEGORY_TO_SECTION.get(str(unit.get("category") or ""), "triage")


def _aggregate_items(items):
    """Свернуть items в псевдо-единицы по error_class (если групп/кластеров нет)."""
    agg = {}
    for it in items:
        label = it.get("error_class") or "Другое"
        bucket = agg.setdefault(label, {
            "label": label, "count": 0, "category": it.get("category") or "",
            "owner": it.get("owner") or "", "numbers": [], "_conf": [],
        })
        bucket["count"] += 1
        bucket["numbers"].append(it.get("number"))
        bucket["_conf"].append(it.get("confidence"))
    for bucket in agg.values():
        bucket["confidence"] = _majority(bucket.pop("_conf"))
        bucket["numbers"] = sorted(set(map(str, bucket["numbers"])), key=_num_key)
    out = list(agg.values())
    out.sort(key=lambda u: (-u["count"], u["label"]))
    return out


def _rerun_units(classification):
    """Единицы рендера: clusters → groups → агрегат по items. Сортировка: count↓, label."""
    units = list(classification.get("clusters") or [])
    if not units:
        units = list(classification.get("groups") or [])
    if not units:
        return _aggregate_items(classification.get("items") or [])
    units.sort(key=lambda u: (-_unit_count(u), _unit_label(u)))
    return units


def _rerun_counts(diff, classification=None):
    """healed/failed_again/skipped/absent: stats → counts классификации → длины списков."""
    stats = diff.get("stats") or diff.get("stats_source") or {}
    counts = {}
    for key in ("healed", "failed_again", "skipped", "absent"):
        value = stats.get(key)
        if value is None and classification is not None:
            cls_counts = classification.get("counts") or {}
            if key == "failed_again":
                value = cls_counts.get("failed_again")
            else:
                value = len(classification.get(key) or [])
        if value is None:
            value = len(diff.get(key) or [])
        counts[key] = int(value or 0)
    return counts


def _stability_line(items, failed_again):
    """Строка стабильности по prev_status; нет данных — ''."""
    prev = [str(it.get("prev_status") or "") for it in items]
    if not any(prev):
        return ""
    stable = sum(1 for p in prev if p in ("failed", "broken"))
    flaky = sum(1 for p in prev if p == "passed")
    new = sum(1 for p in prev if p == "absent")
    return f"Стабильность: повторяются {stable}/{failed_again} · флейк {flaky} · новых {new}"


def _summary_section(units, items_by_number, classification, failed_again, items):
    """Сводка по типам + покрытие evidence + стабильность."""
    buckets = {key: 0 for key in ("product", "autotest", "system", "triage")}
    for unit in units:
        buckets[_section_of(unit, items_by_number)] += _unit_count(unit)

    def share(number):
        return f"{round(number / failed_again * 100, 1)}%" if failed_again else "0.0%"

    lines = ["## Сводка по типам", "", "| Тип | Кол-во | Доля |", "|---|---:|---:|"]
    for key in ("product", "autotest", "system", "triage"):
        lines.append(f"| {SECTION_TITLES[key]} | {buckets[key]} | {share(buckets[key])} |")

    by_conf = (classification.get("counts") or {}).get("by_confidence") or {}
    if by_conf:
        low = int(by_conf.get("low", 0) or 0)
        high_med = sum(int(v or 0) for k, v in by_conf.items() if str(k).lower() != "low")
        total = high_med + low
        hm_pct = round(high_med / total * 100, 1) if total else 0.0
        low_pct = round(low / total * 100, 1) if total else 0.0
        lines += ["", f"Покрытие evidence (по тестам): подтверждено (high/medium) {hm_pct}%"
                      f" · триаж (low) {low_pct}%"]
    stability = _stability_line(items, failed_again)
    if stability:
        lines.append(stability)
    return "\n".join(lines)


def _clusters_section(units, items_by_number, has_prev):
    """Обзорная таблица кластеров: тестов, тип, confidence, стабильность, рекомендация."""
    if not units:
        return ""
    header = "| Кластер | Тестов | Тип | Confidence | Рекомендация |"
    divider = "|---|---:|---|---|---|"
    if has_prev:
        header = "| Кластер | Тестов | Тип | Confidence | Стабильно | Рекомендация |"
        divider = "|---|---:|---|---:|---|---|"
    lines = ["## Снова упавшие по кластерам", "", header, divider]
    for unit in units:
        cells = [
            _cell_md(_unit_label(unit)),
            str(_unit_count(unit)),
            _cell_md(_unit_type(unit)),
            _cell_md(_unit_confidence(unit, items_by_number)),
        ]
        if has_prev:
            stable = unit.get("stable")
            cells.append(f"{int(stable or 0)}/{_unit_count(unit)}" if stable is not None else "—")
        cells.append(_cell_md(_unit_recommendation(unit, items_by_number)))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _cluster_detail(unit, items_by_number, has_prev):
    """Блок кластера: стабильность, диагноз/обоснование, confidence, рекомендация, подгруппы."""
    count = _unit_count(unit)
    numbers = _unit_numbers(unit)
    title = f"**{_unit_label(unit)}** — {count} тестов"
    if has_prev:
        bits = [f"стабильно {int(unit.get('stable') or 0)}/{count}"]
        if unit.get("flaky"):
            bits.append(f"флейк {unit['flaky']}")
        if unit.get("new"):
            bits.append(f"новых {unit['new']}")
        title += " · " + ", ".join(bits)
    lines = [title]
    for key, label in (("verdict", "Диагноз"), ("reason", "Обоснование"),
                       ("steps", "Шаги проверки")):
        if unit.get(key):
            lines.append(f"- **{label}:** {unit[key]}")
    if unit.get("files"):
        files = unit["files"]
        if isinstance(files, (list, tuple)):
            files = ", ".join(str(x) for x in files)
        lines.append(f"- **Файлы:** {files}")
    confidence = _unit_confidence(unit, items_by_number)
    score = unit.get("evidence_score")
    if score is None:
        lines.append(f"- **Confidence:** {confidence}")
    else:
        kinds = [str(e.get("kind")) for e in (unit.get("evidence_list") or [])
                 if isinstance(e, dict) and e.get("kind")]
        line = f"- **Confidence:** {confidence} (score {score}"
        if kinds:
            line += f": {', '.join(kinds)}"
        lines.append(line + ")")
    recommendation = _unit_recommendation(unit, items_by_number)
    if recommendation:
        lines.append(f"- **Рекомендация:** {recommendation}")
    if numbers:
        lines.append(f"- **Тесты ({len(numbers)}):** {' '.join(numbers)}")
    subgroups = unit.get("subgroups") or []
    if len(subgroups) > 1:
        for sub in subgroups:
            lines.append(f"  - **{sub.get('label', '')}** — {sub.get('count', 0)}: "
                         f"{' '.join(str(n) for n in sub.get('numbers') or [])}")
    return lines


def _type_sections(units, items_by_number, has_prev):
    """Секции по типу (дефект/автотест/система/триаж) с блоками кластеров."""
    buckets = {key: [] for key in ("product", "autotest", "system", "triage")}
    for unit in units:
        buckets[_section_of(unit, items_by_number)].append(unit)
    blocks = []
    for key in ("product", "autotest", "system", "triage"):
        if not buckets[key]:
            continue
        total = sum(_unit_count(u) for u in buckets[key])
        blocks += [f"### {SECTION_TITLES[key]} ({total})", ""]
        for unit in buckets[key]:
            blocks.extend(_cluster_detail(unit, items_by_number, has_prev))
            blocks.append("")
    return blocks


def _tests_section(diff, classification, units, items_by_number):
    """Пер-тестовая таблица: items (статусы не меняются) или failed_again из diff."""
    cluster_by_number = {}
    for unit in units:
        for number in _unit_numbers(unit):
            cluster_by_number.setdefault(number, unit)

    rows = []
    items = (classification or {}).get("items") or []
    if items:
        for item in sorted(items, key=lambda x: _num_key(x.get("number") or "")):
            unit = cluster_by_number.get(str(item.get("number")))
            recommendation = item.get("recommendation")
            if not recommendation and unit is not None:
                recommendation = _unit_recommendation(unit, items_by_number)
            verdict = (item.get("verdict")
                       or (unit.get("verdict") if unit else "")
                       or item.get("error_class") or "—")
            rows.append((
                _cell_md(item.get("number")),
                _cell_md(_status_label(item)),
                _cell_md(item.get("error_class") or "—"),
                _cell_md(item.get("category_label") or item.get("owner")
                         or item.get("category") or "—"),
                _cell_md(item.get("confidence") or "—"),
                _cell_md(_unit_label(unit) if unit else "—"),
                _cell_md(verdict),
                _cell_md(recommendation or "—"),
            ))
    else:
        for failed in diff.get("failed_again") or []:
            message = next((m for m in (failed.get("messages") or []) if m), "")
            rows.append((
                _cell_md(with_at(failed.get("number"))),
                _cell_md(_status_up(failed.get("status"))),
                _cell_md(classify_error(message)),
                "—", "—", "—", "—", "—",
            ))
    if not rows:
        return ""
    lines = [
        "## Таблица по тестам",
        "",
        "| Номер | Статус | Класс ошибки | Тип | Confidence | Кластер | Вердикт | Рекомендация |",
        "|---|---|---|---|---|---|---|---|",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def _healed_section(healed):
    """Вылечившиеся тесты — статус PASS (в пер-тестовой таблице их нет)."""
    rows = []
    for entry in healed:
        number = entry.get("number") if isinstance(entry, dict) else entry
        if number:
            rows.append(f"| {_cell_md(with_at(number))} | PASS |")
    if not rows:
        return ""
    return "\n".join(["## Вылечившиеся (PASS)", "", "| Номер | Статус |", "|---|---|", *rows])


def _numbers_section(title, numbers):
    """Секция со списком номеров в одну строку."""
    return "\n".join([f"## {title} ({len(numbers)})", "", " ".join(numbers) if numbers else "—"])


def render_rerun(diff, classification=None):
    """Markdown страницы перезапуска. Возвращает (text, problems)."""
    problems = []
    items = (classification or {}).get("items") or []
    if classification:
        cf = int((classification.get("counts") or {}).get("failed_again", -1))
        if cf >= 0 and len(items) != cf:
            problems.append(
                f"items в классификации ({len(items)}) не совпали с counts.failed_again ({cf})"
            )

    counts = _rerun_counts(diff, classification)
    head = (
        f"# Перезапуск {diff.get('rerun_launch_id', '')} "
        f"относительно {diff.get('expected_launch_id', '')}"
    ).rstrip()
    teams = [str(t) for t in (diff.get("teams") or []) if t]
    if teams:
        head += f" ({', '.join(teams)})"

    blocks = [
        head,
        "",
        "## Итог",
        "",
        "| Итог | Тестов |",
        "|---|---:|",
    ]
    for key in ("healed", "failed_again", "skipped", "absent"):
        blocks.append(f"| {key} | {counts[key]} |")

    if classification:
        items_by_number = _items_by_number(classification)
        units = _rerun_units(classification)
        has_prev = any(str(it.get("prev_status") or "") for it in items)
        blocks += ["", _summary_section(units, items_by_number, classification,
                                        counts["failed_again"], items)]
        clusters = _clusters_section(units, items_by_number, has_prev)
        if clusters:
            blocks += ["", clusters]
        details = _type_sections(units, items_by_number, has_prev)
        if details:
            blocks += [""] + details
        tests = _tests_section(diff, classification, units, items_by_number)
        if tests:
            blocks += ["", tests]
        healed = _healed_section(classification.get("healed") or diff.get("healed") or [])
        if healed:
            blocks += ["", healed]
        numbers = sorted({n for unit in units for n in _unit_numbers(unit)}, key=_num_key)
        if numbers:
            blocks += ["", _numbers_section("Новый @-список", numbers)]
    else:
        tests = _tests_section(diff, None, [], {})
        if tests:
            blocks += ["", tests]
        healed = _healed_section(diff.get("healed") or [])
        if healed:
            blocks += ["", healed]
    return "\n".join(blocks) + "\n", problems


def render_classification(result):
    """Markdown перезапуска по готовому артефакту rerun_classification.json.

    Артефакт содержит поля diff (rerun_launch_id/expected_launch_id/stats_source,
    healed/absent/skipped), поэтому передаётся и как diff, и как classification.
    Используется `classify_rerun.py --chat` — разметка не дублируется.
    """
    return render_rerun(result, result)


# ---------------------------------------------------------------------------
# Страница раздачи перезапусков («AI run {launch.name}») из dispatch.json
# ---------------------------------------------------------------------------

# Статусы dispatch.json — контракт dispatch_team_reruns.py.
# Запуск состоялся (или состоялся бы в dry-run).
_TRIGGERED = {"triggered", "would_trigger", "merged_scan"}
# Не запускались, и это норма.
_INFO = {"skipped_empty", "already_dispatched", "disabled"}
# Требуют решения человека. Всё, чего нет в трёх множествах, тоже сюда —
# неизвестный статус нельзя выдавать за «норму».
_PROBLEMS = {"error", "unmapped", "duplicate_scan", "no_scan"}

_LABELS = {
    "triggered": "запущено",
    "would_trigger": "план (dry-run)",
    "merged_scan": "запущено (папки объединены)",
    "already_dispatched": "уже запущено ранее",
    "skipped_empty": "пропущено (нет упавших)",
    "disabled": "отключено в реестре",
    "no_scan": "нет скана",
    "unmapped": "нет команды в реестре",
    "duplicate_scan": "дубль папок команды",
    "error": "ошибка запуска",
}


def _dispatch_status_label(status):
    """Человекочитаемая метка статуса раздачи."""
    key = str(status or "")
    return _LABELS.get(key, f"неизвестный статус: {key or '—'}")


def render_dispatch(dispatch, launch_url=""):
    """Markdown страницы «AI run {launch_name}» из launches/{id}/dispatch.json.

    Заменяет удалённый publish_confluence.py ai-run: числа и ссылки берутся
    из артефакта, агент вставляет готовый текст через MCP atlassian.

    Гейты (проверяются здесь, exit 1 в main() при problems):
      1. каждая запись teams попала ровно в одну секцию;
      2. счётчики summary не противоречат фактическим записям
         (teams_total / teams_triggered / teams_skipped_empty);
      3. неизвестный статус — это проблема, а не «норма».
    """
    teams = dispatch.get("teams") or []
    launch_id = dispatch.get("launch_id")
    summary = dispatch.get("summary") or {}
    problems = []

    def status_of(e):
        return str(e.get("status") or "")

    triggered = [e for e in teams if status_of(e) in _TRIGGERED]
    info = [e for e in teams if status_of(e) in _INFO]
    bad = [e for e in teams if status_of(e) not in _TRIGGERED
           and status_of(e) not in _INFO]

    # --- Гейт 1-3: полнота и согласованность ---
    covered = len(triggered) + len(info) + len(bad)
    if teams and covered != len(teams):
        problems.append(f"секций страницы ({covered}) != записей teams в dispatch.json ({len(teams)})")
    if not teams:
        problems.append("dispatch.json не содержит записей teams")

    checks = [
        ("teams_total", len(teams)),
        ("teams_triggered", len(triggered)),
        ("teams_skipped_empty", sum(1 for e in teams if status_of(e) == "skipped_empty")),
    ]
    for key, actual in checks:
        if key in summary and int(summary.get(key) or 0) != actual:
            problems.append(f"summary.{key}={summary.get(key)} != фактических {actual}")

    blocks = [f"# AI run {dispatch.get('launch_name') or launch_id}", ""]
    if launch_url:
        blocks += [f"Запуск: {launch_url}", ""]
    blocks += [
        f"Раздача перезапусков: env={dispatch.get('env') or 'qa'}, "
        f"parallel={dispatch.get('parallel') or '15'}, rerun={dispatch.get('rerun') or '1'}",
        "",
    ]

    if summary:
        problems_count = len(bad)
        blocks += [
            "## Итог раздачи",
            "",
            "| Показатель | Значение |",
            "|---|---:|",
            f"| Команд в раздаче | {int(summary.get('teams_total', len(teams)) or 0)} |",
            f"| Запущено | {int(summary.get('teams_triggered', len(triggered)) or 0)} |",
            f"| Пропущено (нет упавших) | {int(summary.get('teams_skipped_empty', 0) or 0)} |",
            f"| Требуют решения | {problems_count} |",
            f"| Тестов в @-списках | {int(summary.get('tests_dispatched', 0) or 0)} |",
            "",
        ]

    if triggered:
        blocks += ["## Перезапуски по командам", ""]
        for e in triggered:
            team = e.get("team") or e.get("folder") or "—"
            job = e.get("job") or "—"
            link = e.get("build_url") or e.get("queue_url") or ""
            count = int(e.get("count", 0) or 0)
            lines_out = [
                f"### {team} — {_dispatch_status_label(e.get('status'))}",
                "",
                f"- Джоба: `{job}`",
                f"- Тестов в @-списке: {count}",
            ]
            if link:
                lines_out.append(f"- Сборка: {link}")
            folders = e.get("folders") or []
            if len(folders) > 1:
                lines_out.append(f"- Объединённые папки: {', '.join(_cell(f) for f in folders)}")
            tags = e.get("tags") or ""
            if tags:
                lines_out += ["", "```", tags, "```"]
            blocks += ["\n".join(lines_out), ""]

    if bad:
        blocks += [
            "## Требуют решения",
            "",
            "| Команда | Статус | Причина |",
            "|---|---|---|",
        ]
        for e in bad:
            team = e.get("team") or e.get("folder") or "—"
            reason = e.get("error") or e.get("status") or ""
            blocks.append(
                f"| {_cell(team)} | {_cell(_dispatch_status_label(e.get('status')))} "
                f"| {_cell(_short(reason, 200))} |"
            )
        blocks.append("")

    if info:
        blocks += [
            "## Не запускались",
            "",
            "| Команда | Статус | Тестов |",
            "|---|---|---:|",
        ]
        for e in info:
            team = e.get("team") or e.get("folder") or "—"
            blocks.append(
                f"| {_cell(team)} | {_cell(_dispatch_status_label(e.get('status')))} "
                f"| {int(e.get('count', 0) or 0)} |"
            )
        blocks.append("")

    return "\n".join(blocks).rstrip() + "\n", problems


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(description="Детерминированный markdown страниц TestOps")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--expected-set", help="Путь к expected_set.json (страница разбора запуска)")
    ap.add_argument("--no-diagnosis", action="store_true",
                    help="Не подхватывать skip_diagnosis.json из папки expected_set.json")
    src.add_argument("--rerun-diff", help="Путь к rerun_diff.json (страница перезапуска)")
    src.add_argument("--dispatch", help="Путь к dispatch.json (страница «AI run» раздачи перезапусков)")
    ap.add_argument("--classification", help="Путь к rerun_classification.json (только с --rerun-diff)")
    ap.add_argument("--launch-url", default="", help="Ссылка на запуск TestOps (для страницы --dispatch)")
    ap.add_argument("--out", help="Записать markdown в файл UTF-8 (по умолчанию stdout)")
    args = ap.parse_args()

    if args.classification and not args.rerun_diff:
        print("ОШИБКА: --classification допустим только вместе с --rerun-diff", file=sys.stderr)
        return 1

    sources = [args.expected_set, args.rerun_diff, args.dispatch, args.classification]
    for path in sources:
        if not path:
            continue
        if not Path(path).exists():
            print(f"ОШИБКА: файл не найден: {path}", file=sys.stderr)
            return 1

    try:
        if args.expected_set:
            diagnosis = None
            if not args.no_diagnosis:
                # Тот же каталог, что и expected_set.json: launches/{id}/{team}/skip_diagnosis.json
                cand = Path(args.expected_set).parent / "skip_diagnosis.json"
                if cand.exists():
                    diagnosis = _load_json(cand)
            text, problems = render_expected_set(_load_json(args.expected_set), diagnosis)
        elif args.dispatch:
            text, problems = render_dispatch(_load_json(args.dispatch), args.launch_url)
        else:
            classification = _load_json(args.classification) if args.classification else None
            text, problems = render_rerun(_load_json(args.rerun_diff), classification)
    except (json.JSONDecodeError, OSError) as e:
        print(f"ОШИБКА чтения JSON: {e}", file=sys.stderr)
        return 1

    if problems:
        for p in problems:
            print(f"ПРЕДУПРЕЖДЕНИЕ: {p}", file=sys.stderr)
        return 1

    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"  markdown: {args.out}")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())

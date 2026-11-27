#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""classify_rerun.py — УРОВЕНЬ 2 триажа: классификация результатов рерана.

Вход:  ID запуска-рерана (плагин уже перезапустил упавшие и создал запуск в TestOps).
Выход: <root>/triage/{rerun_id}/triage_classification.json
       + markdown в stdout в фиксированном формате вердикта (гейты -> типы -> группы ->
         триаж-хвост -> кандидат на deep -> @-список) — печатается ВСЕГДА.
       Построчная таблица в чат НЕ печатается (доступна только по флагу --with-rows),
       детализация по каждому тесту остаётся в items внутри JSON.

Что делает:
  1. Полный постраничный скан запуска (size=100) с гейтом полноты (complete).
  2. По каждому не-PASS результату берёт message И trace (backtrace) из /api/testresult/{id}.
  3. Классифицирует по trace -> message (config/triage_rules.json): owner = система/автотест/дефект/триаж.
  4. Группирует по кластерам (правило) + подгруппам (gem-фрейм / локатор).
  5. Считает confidence по evidence-score; low -> триаж, не выдаётся за вердикт.

Чего НЕ делает: не ходит в Jenkins, не раздаёт рераны, не требует эталон,
не публикует в Confluence, не пишет в артефакты других скиллов.

Использование:
  py -X utf8 classify_rerun.py --launch-id 1367360
  py -X utf8 classify_rerun.py --launch-id 1367360 --json-only
  py -X utf8 classify_rerun.py --launch-id 1367360 --no-trace   # быстрый режим (только message)

Exit: 0 = ok, 1 = ошибка/неполный скан, 2 = ошибка входа.
"""

import argparse
import datetime
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import classifier as cl  # noqa: E402
import code_parser as cp  # noqa: E402
from _paths import artifacts_root, triage_dir, RULES_PATH  # noqa: E402
from testops_client import (  # noqa: E402
    get_launch, get_result, init_state, load_config, resolve_token, scan_flat,
)

NON_PASS = {"failed", "broken", "skipped"}
STATUS_DISPLAY = {"failed": "FAIL", "broken": "BROKEN", "skipped": "SKIP", "passed": "PASS"}
OWNER_ORDER = ["дефект", "автотест", "система", "триаж"]
# Порядок типов в вердикте — фиксированный (как в утверждённом формате), не по количеству
CHAT_OWNER_ORDER = ["система", "автотест", "дефект", "триаж"]
OWNER_ICON = {"дефект": "🔴", "автотест": "🟡", "система": "⚪", "триаж": "❓"}


def collect(launch_id: int, with_trace: bool = True, page_size: int = 100) -> dict:
    """Собирает и классифицирует все не-PASS результаты запуска."""
    launch = get_launch(launch_id)
    launch_name = str(launch.get("name") or launch_id)

    rows, info = scan_flat(launch_id, page_size)
    if not info["complete"]:
        raise RuntimeError(
            f"неполный скан: обработано {info['rows_scanned']} из {info['total']} строк")

    by_number = {}
    unmatched_rows = []
    for row in rows:
        number = cl.extract_number(str(row.get("name") or ""))
        status = str(row.get("status") or "").lower()
        if not number:
            if status in NON_PASS:
                unmatched_rows.append({"result_id": row.get("id"), "status": status})
            continue
        entry = by_number.setdefault(number, {
            "number": number, "name": str(row.get("name") or ""),
            "statuses": [], "results": [], "testCaseId": row.get("testCaseId"),
        })
        entry["statuses"].append(status)
        entry["results"].append({
            "result_id": row.get("id"),
            "status": status,
            "duration": row.get("duration"),
            "flaky": row.get("flaky"),
        })

    items, errored = [], []
    for number, entry in by_number.items():
        nonpass = [r for r in entry["results"] if r["status"] in NON_PASS]
        if not nonpass:
            continue  # тест прошёл — в таблицу падений не попадает
        # агрегат: если хоть одна строка не-PASS — номер считается упавшим
        first = next((r for r in nonpass if r["status"] != "skipped"), nonpass[0])
        agg_status = "skipped" if all(r["status"] == "skipped" for r in nonpass) else first["status"]

        primary_msg, primary_trace, evidence_result_id = "", "", None
        for r in nonpass[:3]:  # параметризованные тесты: не больше 3 строк на номер
            try:
                det = get_result(r["result_id"]) or {}
            except Exception as e:  # noqa: BLE001 — детали не должны ронять разбор
                errored.append({"number": number, "result_id": r["result_id"], "error": str(e)[:200]})
                continue
            msg = str(det.get("message") or "").strip()
            tr = str(det.get("trace") or "").strip() if with_trace else ""
            if (tr and not primary_trace) or (msg and not primary_trace and not primary_msg):
                primary_msg, primary_trace = msg, tr
                evidence_result_id = r["result_id"]
            if r.get("flaky") is None:
                r["flaky"] = det.get("flaky")

        rule = cl.classify(primary_trace, primary_msg)
        frames = cl.parse_frames(primary_trace)
        own = cl.own_frame(frames)
        gem = cl.gem_frame(frames)
        locator = cl.extract_locator(primary_msg)

        evidence = {
            "message": bool(primary_msg),
            "trace": bool(primary_trace),
            "own_frame": bool(own),
        }
        score = cl.evidence_score(evidence)
        confidence = cl.confidence_from_score(score)
        owner = rule["owner"]
        # Низкая уверенность => не выдаём тип за вердикт, отправляем в триаж
        if confidence == "low":
            owner = "триаж"

        items.append({
            "number": cl.with_at(number),
            "name": entry["name"],
            "status": STATUS_DISPLAY.get(agg_status, "BROKEN"),
            "results": len(entry["results"]),
            "nonpass": len(nonpass),
            "testCaseId": entry["testCaseId"],
            "rule_key": rule["key"],
            "cluster_label": rule["label"],
            "category": rule["category"],
            "owner": owner,
            "matched_by": rule["matched_by"],
            "confidence": confidence,
            "evidence_score": score,
            "evidence": evidence,
            "evidence_result_id": evidence_result_id,
            "message": primary_msg[:400],
            "trace": primary_trace[:1500] if with_trace else "",
            "own_frame": f"{own['short']}:{own['line']} {own['method']}" if own else "",
            "gem_frame": gem["short"] if gem else "",
            "subgroup": cl.subgroup_key(rule, frames, locator),
            "locator": locator,
        })

    items.sort(key=lambda x: cl.natural_key(x["number"]))

    # Группы: кластер (правило) -> подгруппы (gem-фрейм / локатор)
    groups = {}
    for it in items:
        key = (it["rule_key"], it["cluster_label"], it["category"], it["owner"])
        g = groups.setdefault(key, {
            "key": it["rule_key"], "label": it["cluster_label"],
            "category": it["category"], "owner": it["owner"],
            "confidence": it["confidence"],
            "numbers": [], "subgroups": {},
        })
        g["numbers"].append(it["number"])
        sub_key = it["subgroup"] or ("локатор" if it["locator"] else "прочее")
        sub = g["subgroups"].setdefault(sub_key, {"label": sub_key, "numbers": [], "sample": ""})
        sub["numbers"].append(it["number"])
        if not sub["sample"]:
            sub["sample"] = it["locator"] or it["message"][:120]

    group_list = []
    for g in groups.values():
        subs = [{"label": s["label"], "count": len(s["numbers"]),
                 "numbers": s["numbers"], "sample": s["sample"]}
                for s in g["subgroups"].values()]
        subs.sort(key=lambda s: (-s["count"], s["label"]))
        group_list.append({
            "key": g["key"], "label": g["label"], "category": g["category"],
            "owner": g["owner"], "confidence": g["confidence"],
            "count": len(g["numbers"]), "numbers": g["numbers"],
            "subgroups": subs,
        })
    group_list.sort(key=lambda g: (OWNER_ORDER.index(g["owner"])
                                   if g["owner"] in OWNER_ORDER else 9, -g["count"], g["label"]))

    by_owner, by_category, by_confidence = {}, {}, {}
    for it in items:
        by_owner[it["owner"]] = by_owner.get(it["owner"], 0) + 1
        by_category[it["category"]] = by_category.get(it["category"], 0) + 1
        by_confidence[it["confidence"]] = by_confidence.get(it["confidence"], 0) + 1

    total = len(items)
    failed_rows = sum(it["nonpass"] for it in items) + len(unmatched_rows)
    confirmed = total - by_owner.get("триаж", 0)
    # ГЕЙТ: каждая группа покрывает ровно свои номера, суммы сходятся
    seen = [n for g in group_list for n in g["numbers"]]
    gate_ok = not unmatched_rows and sum(g["count"] for g in group_list) == total and len(seen) == len(set(seen))

    return {
        "schema": "testops-rerun-triage/classification@1",
        "mode": "rerun_classification",
        "rerun_launch_id": launch_id,
        "rerun_name": launch_name,
        "launch_closed": launch.get("closed"),
        "teams_note": "классификация без эталона: берутся все не-PASS результаты запуска",
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "rules_path": str(RULES_PATH),
        "with_trace": with_trace,
        "scan": info,
        "stats": {
            "failed_numbers": total,
            "failed_rows": failed_rows,
            "by_owner": by_owner,
            "by_category": by_category,
            "by_confidence": by_confidence,
            "confirmed_pct": round(confirmed / total * 100, 1) if total else 0.0,
            "triage_pct": round(by_owner.get("триаж", 0) / total * 100, 1) if total else 0.0,
            "gate_ok": gate_ok,
        },
        "groups": group_list,
        "items": items,
        "unmatched_rows": unmatched_rows,
        "detail_errors": errored,
        "rerun_tags": cl_at(items),
        "triage_tail": [it for it in items if it["owner"] == "триаж" or it["confidence"] == "low"],
        "deep_candidate": pick_deep_candidate(group_list, total),
    }


def pick_deep_candidate(groups: list, total: int) -> dict | None:
    """Крупнейший кластер с подтверждённым типом — кандидат на уровень 3 (deep)."""
    candidates = [g for g in groups if g["owner"] != "триаж" and g["count"] > 0]
    if not candidates:
        return None
    g = max(candidates, key=lambda x: (x["count"], x["label"]))
    owner_total = sum(x["count"] for x in groups if x["owner"] == g["owner"])
    return {
        "key": g["key"],
        "label": g["label"],
        "owner": g["owner"],
        "count": g["count"],
        "share_of_failed_pct": round(g["count"] / total * 100, 1) if total else 0.0,
        "share_of_owner_pct": round(g["count"] / owner_total * 100, 1) if owner_total else 0.0,
        "subgroups": len(g["subgroups"]),
        "representative": g["numbers"][0] if g["numbers"] else "",
    }


def cl_at(items):
    """@-список всех упавших номеров — готовый вход для следующего перезапуска."""
    return " ".join(it["number"] for it in items)


def render_markdown(result: dict, with_rows: bool = False) -> str:
    """Детерминированный markdown в ФИКСИРОВАННОМ формате вердикта.

    Порядок блоков (не менять): гейты -> типы с долями -> группы с подгруппами ->
    триаж-хвост -> кандидат на deep -> @-список. Построчная таблица — только по флагу.
    """
    st = result["stats"]
    scan = result["scan"]
    total = st["failed_numbers"] or 1
    out = []
    out.append(f"# Триаж рерана {result['rerun_launch_id']} — {result['rerun_name']}")
    out.append("")
    closed = result.get("launch_closed")
    closed_note = ("запуск закрыт" if closed else
                   "запуск не закрыт (`closed=false`) — снимок на момент скана")
    out.append(
        f"**Гейты:** скан {scan['rows_scanned']}/{scan['total']} "
        f"`complete={'true' if scan['complete'] else 'false'}` · "
        f"гейт сходимости групп **{'OK' if st['gate_ok'] else 'FAIL'}** "
        f"({st['failed_numbers']} = {sum(g['count'] for g in result['groups'])}) · "
        f"`detail_errors` {len(result.get('detail_errors') or [])} · "
        f"confirmed {st['confirmed_pct']}% · триаж {st['triage_pct']}% · "
        f"low confidence {st['by_confidence'].get('low', 0)}. "
        f"{closed_note.capitalize()}.")
    out.append("")
    out.append(f"**Строк результатов:** {st['failed_rows']} (параметризованные тесты дают "
               f"несколько строк на номер).")
    out.append("")

    # Типы — фиксированный порядок и доли от числа упавших номеров
    parts = []
    for owner in CHAT_OWNER_ORDER:
        cnt = st["by_owner"].get(owner, 0)
        if cnt:
            parts.append(f"{owner} **{cnt} ({round(cnt / total * 100, 1)}%)**")
    out.append("**Типы:** " + " · ".join(parts) + ".")
    out.append("")

    out.append("**Группы**")
    out.append("")
    for owner in CHAT_OWNER_ORDER:
        own_groups = [g for g in result["groups"] if g["owner"] == owner]
        if not own_groups:
            continue
        icon = OWNER_ICON.get(owner, "")
        for g in own_groups:
            out.append(f"- {icon} **{g['label']}** — {g['count']}, {g['confidence']}")
            if len(g["subgroups"]) == 1 and str(g["subgroups"][0]["label"]).strip() in ("прочее", "локатор", ""):
                # единственная безымянная подгруппа — номера прямо в строке группы
                out.append(f"  - номера: {' '.join(g['numbers'])}")
            else:
                for sub in g["subgroups"]:
                    label = " ".join(str(sub["label"]).split())[:110]
                    out.append(f"  - `{label}` — {sub['count']}: {' '.join(sub['numbers'])}")
    out.append("")

    tail = result.get("triage_tail") or []
    if tail:
        out.append("**Требует точечного разбора** (низкая уверенность или нет владельца)")
        out.append("")
        for it in tail:
            msg = " ".join((it["message"] or "").split())[:130]
            frame = f", `{it['own_frame']}`" if it.get("own_frame") else ""
            out.append(f"- {it['number']} — {msg}{frame} · группа: {it['cluster_label']}")
        out.append("")

    deep = result.get("deep_candidate")
    if deep:
        out.append("**Кандидат на deep (уровень 3)**")
        out.append("")
        out.append(f"- **{deep['label']}** — {deep['count']} тестов: "
                   f"{deep['share_of_failed_pct']}% упавших, "
                   f"{deep['share_of_owner_pct']}% типа «{deep['owner']}», "
                   f"подгрупп {deep['subgroups']}, представитель `{deep['representative']}`.")
        out.append("")

    out.append("**@-список**")
    out.append("")
    out.append(f"```text\n{result['rerun_tags']}\n```")
    out.append("")

    if with_rows:
        out.append("## Построчно (служебная детализация, --with-rows)")
        out.append("")
        out.append("| № | Статус | Группа | Тип | Conf | Ошибка | Свой фрейм | Gem-фрейм |")
        out.append("|---|---|---|---|---|---|---|---|")
        for it in result["items"]:
            msg = (it["message"] or "").replace("|", "\\|").replace("\n", " ")[:90]
            own = it["own_frame"].replace("|", "\\|")[:40]
            gem = it["gem_frame"].replace("|", "\\|")[:40]
            out.append(f"| {it['number']} | {it['status']} | {it['cluster_label']} | "
                       f"{it['owner']} | {it['confidence']} | {msg} | {own} | {gem} |")
        out.append("")

    if result.get("detail_errors"):
        out.append("## ⚠ Ошибки получения деталей")
        out.append("")
        for e in result["detail_errors"][:10]:
            out.append(f"- {e['number']} (result {e['result_id']}): {e['error']}")
        out.append("")
    return "\n".join(out)


def print_matrix(result: dict) -> None:
    """Компактный вывод для терминала (когда markdown не нужен)."""
    st = result["stats"]
    print(f"TRIAGE rerun {result['rerun_launch_id']} — {result['rerun_name']}")
    print(f"  упавших номеров: {st['failed_numbers']} | строк: {st['failed_rows']} "
          f"| скан: {result['scan']['rows_scanned']}/{result['scan']['total']} "
          f"| complete: {result['scan']['complete']}")
    print("  по типам: " + ", ".join(f"{k}={v}" for k, v in st["by_owner"].items()))
    print(f"  подтверждено: {st['confirmed_pct']}% | триаж: {st['triage_pct']}% "
          f"| гейт: {'OK' if st['gate_ok'] else 'FAIL'}")
    for g in result["groups"]:
        print(f"  {g['label']} [{g['owner']}, {g['confidence']}] — {g['count']}")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(
        description="Уровень 2: классификация упавших тестов рерана (без эталона)")
    ap.add_argument("--launch-id", type=int, default=None, help="ID запуска-рерана в TestOps")
    ap.add_argument("--code", default=None,
                    help='Кодовая строка от плагина: "TRIAGE::RERUN launch=1367360" '
                         "(ID и уровень берутся из неё)")
    ap.add_argument("--root", default=None, help="Корень артефактов (по умолчанию ~/.qa-artifacts)")
    ap.add_argument("--rules", default=None, help="Путь к triage_rules.json")
    ap.add_argument("--no-trace", action="store_true",
                    help="Быстрый режим: не тянуть trace (только message; ниже точность типов)")
    ap.add_argument("--json-only", action="store_true", help="Не печатать markdown, только матрицу")
    ap.add_argument("--with-rows", action="store_true",
                    help="Добавить служебную построчную таблицу (по умолчанию вердикт без таблицы)")
    ap.add_argument("--out", default=None, help="Куда сохранить markdown (опционально)")
    args = ap.parse_args()

    # Кодовая строка от плагина: приоритетнее --launch-id, если в ней есть ID
    if args.code:
        try:
            parsed = cp.parse_code(args.code)
        except cp.CodeError as e:
            print(f"ОШИБКА кодовой строки: {e}")
            return 2
        print(f"  код: {cp.describe(parsed)}")
        if parsed["launch_id"]:
            args.launch_id = parsed["launch_id"]
        if parsed["level"] == 3:
            print("ВНИМАНИЕ: передан уровень 3 (deep) — режим глубокого разбора ещё не реализован; "
                  "выполняется уровень 2.")

    if not args.launch_id:
        print("ОШИБКА: не указан ID рерана. Передай --launch-id <ID> "
              f'или кодовую строку --code "{cp.CODE_WORD} launch=<ID>".')
        return 2

    root = artifacts_root(args.root)
    cfg = load_config(root)
    token = resolve_token(cfg)
    if not token:
        print("ОШИБКА: токен TestOps не задан (env TESTOPS_TOKEN или config.json).")
        return 2

    cl.load_rules(args.rules or RULES_PATH)
    init_state(cfg, token)

    try:
        result = collect(args.launch_id, with_trace=not args.no_trace,
                         page_size=cfg.get("page_size", 100))
    except Exception as e:  # noqa: BLE001 — сеть/пагинация/скан
        print(f"ОШИБКА разбора рерана: {e}")
        return 1

    out_dir = triage_dir(args.launch_id, root)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "triage_classification.json"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json_only:
        print_matrix(result)
    else:
        text = render_markdown(result, with_rows=args.with_rows)
        print(text, end="")
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")

    print(f"\n  json: {json_path}")
    if not result["stats"]["gate_ok"]:
        print("  ⚠ ГЕЙТ СХОДИМОСТИ НАРУШЕН — проверить группы и строки без номера")
        return 1
    print("СТАТУС: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
diff_rerun.py — детерминированный дифф rerun-запуска против expected_set (ШАБЛОН С ЗАГЛУШКАМИ).

Заменяет ручное сравнение «в уме»: скрипт сам сканирует rerun_launch_id,
сопоставляет с .qa-artifacts/launches/{expected_launch_id}/expected_set.json
и вычисляет множества:
  healed       — все Examples PASS в rerun (вылечился);
  failed_again — есть не-PASS (FAIL/BROKEN, либо SKIP-как-не-PASS) — снова упал;
  skipped      — только SKIP-результаты (намеренность решает LLM по hint_intentional);
  absent       — номер не участвовал в rerun (игнорировать, НЕ считать вылечившимся).
Результат — .qa-artifacts/reruns/{rerun_id}/rerun_diff.json; LLM анализирует только его.

Заглушки (TODO) — те же, что в scan_testops.py: формат авторизации, extract_team,
extract_test_number, extract_message. Контракты файлов не менять.

Использование:
  python diff_rerun.py --launch-id 67890 --expected ".qa-artifacts/launches/12345/expected_set.json"
  python diff_rerun.py --launch-id 67890 --expected-launch-id 12345 --root ".qa-artifacts"

Exit code: 0 = ok, 1 = ошибка/неполный скан, 2 = ошибка входа.
"""
import argparse
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

# Переиспользуем механику скана и заглушки из scan_testops.py (тот же каталог)
from scan_testops import (  # noqa: E402
    DEFAULT_ARTIFACTS_ROOT,
    NON_PASS_STATUSES,
    extract_message_stub,
    extract_test_number_stub,
    http_get_json,
    init_scan_state,
    load_config,
    resolve_token,
)

INTENTIONAL_SKIP_RE = re.compile(r"skip|pending|intentionally", re.IGNORECASE)
STATUS_DISPLAY = {"failed": "FAIL", "broken": "BROKEN", "skipped": "SKIP", "passed": "PASS"}


def strip_at(number: str) -> str:
    return number.strip().lstrip("@")


def read_expected(path: Path) -> dict:
    """Читает expected_set.json (контракт скилла testops-run-analysis)."""
    if not path.exists():
        raise FileNotFoundError(f"expected_set.json не найден: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    required = ("launch_id", "teams", "expected_numbers", "statuses")
    missing = [k for k in required if k not in data]
    if missing:
        raise ValueError(f"expected_set.json неполный, отсутствуют: {missing}")
    return data


def scan_rerun(launch_id: int, cfg: dict, token: str) -> tuple[list[dict], dict]:
    """Полный постраничный скан rerun (без фильтров — нужны статусы ВСЕХ expected-тестов)."""
    base = cfg["testops_base_url"].rstrip("/")
    size = int(cfg.get("page_size", 100))
    rows = []
    page = 0
    processed = 0
    total = None
    while True:
        data = http_get_json(
            f"{base}/api/v2/launch/{launch_id}/test-result/flat?page={page}&size={size}", token, cfg
        )
        content = data.get("content") or []
        if total is None:
            total = int(data.get("totalElements") or len(content))
        if not content:
            break
        for r in content:
            processed += 1
            rows.append(r)
        if processed >= total:
            break
        page += 1
        if page > 10_000:
            raise RuntimeError("Пагинация превысила 10_000 страниц — total не сходится")
    return rows, {"total": total, "pages": page + 1, "rows_scanned": processed}


def compute_diff(expected: dict, rerun_rows: list[dict]) -> dict:
    # 🔴 База сравнения — ТОЛЬКО не-PASS номера из expected_set.statuses (failed/broken/skipped),
    # а НЕ все expected_numbers (те включают PASS → смещали healed/failed_again/absent).
    expected_numbers = []
    for num, meta in (expected.get("statuses") or {}).items():
        if str(meta.get("status") or "").lower() in NON_PASS_STATUSES:
            expected_numbers.append(strip_at(num))
    # Фолбэк на случай старого formato expected_set без statuses.
    if not expected_numbers:
        expected_numbers = [strip_at(n) for n in expected.get("expected_numbers", [])]
    expected_set = set(expected_numbers)

    by_number: dict[str, list[dict]] = {}
    for r in rerun_rows:
        num = extract_test_number_stub(r)
        if num and num in expected_set:
            by_number.setdefault(num, []).append(r)

    healed = []
    failed_again = []
    skipped = []
    absent = []

    for num in expected_numbers:
        results = by_number.get(num, [])
        if not results:
            absent.append("@" + num)
            continue
        statuses = [str(r.get("status") or "").lower() for r in results]
        nonpass = [r for r, s in zip(results, statuses) if s in NON_PASS_STATUSES]

        if not nonpass:
            # Все Examples PASS — вылечился
            healed.append({"number": "@" + num, "results": len(results)})
            continue

        # Есть не-PASS
        if all(str(r.get("status") or "").lower() == "skipped" for r in nonpass):
            reasons = [extract_message_stub(r) or "без причины" for r in nonpass]
            reasons = list(dict.fromkeys(reasons))
            skipped.append({
                "number": "@" + num,
                "reasons": reasons,
                "hint_intentional": any(INTENTIONAL_SKIP_RE.search(x) for x in reasons),
            })
            continue

        # FAIL/BROKEN (или смешанные) — снова упал
        first_nonpass = next(r for r in nonpass
                             if str(r.get("status") or "").lower() != "skipped")
        messages = [extract_message_stub(r) for r in nonpass if extract_message_stub(r)]
        messages = list(dict.fromkeys(messages))
        failed_again.append({
            "number": "@" + num,
            "status": STATUS_DISPLAY.get(str(first_nonpass.get("status") or "").lower(), "FAIL"),
            "results": len(results),
            "nonpass": len(nonpass),
            "messages": messages,
        })

    healed.sort(key=lambda x: x["number"])
    failed_again.sort(key=lambda x: x["number"])
    skipped.sort(key=lambda x: x["number"])
    absent.sort()

    stats = {
        "healed": len(healed),
        "failed_again": len(failed_again),
        "skipped": len(skipped),
        "absent": len(absent),
        "present": len(healed) + len(failed_again) + len(skipped),
        "expected_count": len(expected_numbers),
    }
    stats["consistent"] = stats["present"] + stats["absent"] == len(expected_numbers)

    return {
        "healed": healed,
        "failed_again": failed_again,
        "skipped": skipped,
        "absent": absent,
        "stats": stats,
    }


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(description="Дифф rerun против expected_set -> rerun_diff.json")
    ap.add_argument("--launch-id", type=int, required=True, help="ID rerun-запуска")
    ap.add_argument("--expected", default=None, help="Путь к expected_set.json")
    ap.add_argument("--expected-launch-id", type=int, default=None,
                    help="ID первичного запуска (строит путь root/launches/{id}/[{team}/]expected_set.json)")
    ap.add_argument("--team", default=None, help="Команда (подпапка в launches/{id}/{team}/; командная изоляция артефактов)")
    ap.add_argument("--root", default=None, help="Корень .qa-artifacts")
    args = ap.parse_args()

    artifacts_root = Path(args.root) if args.root else DEFAULT_ARTIFACTS_ROOT
    cfg = load_config(artifacts_root)
    token = resolve_token(cfg)
    if not token:
        print("ОШИБКА: токен TestOps не задан. Укажи TESTOPS_TOKEN в env или token в config.json.")
        return 1

    if args.expected:
        expected_path = Path(args.expected)
    elif args.expected_launch_id is not None:
        # 🔴 командная изоляция: если передан --team, ищем в launches/{id}/{team}/expected_set.json
        if args.team:
            expected_path = artifacts_root / "launches" / str(args.expected_launch_id) / args.team / "expected_set.json"
        else:
            expected_path = artifacts_root / "launches" / str(args.expected_launch_id) / "expected_set.json"
    else:
        print("ОШИБКА: укажи --expected или --expected-launch-id")
        return 2

    try:
        expected = read_expected(expected_path)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as e:
        print(f"ОШИБКА входа: {e}")
        return 2

    # Инициализация состояния HTTP-клиента (base/projectId/кэши) — без этого
    # extract_message_stub не сможет сходить в /api/testresult/{id} за message.
    init_scan_state(cfg, token, {"projectId": expected.get("projectId")})

    try:
        rerun_rows, scan_info = scan_rerun(args.launch_id, cfg, token)
    except Exception as e:  # noqa: BLE001
        print(f"ОШИБКА скана rerun: {e}")
        return 1

    diff = compute_diff(expected, rerun_rows)
    diff["rerun_launch_id"] = args.launch_id
    diff["expected_launch_id"] = expected.get("launch_id")
    diff["expected_count"] = diff["stats"]["expected_count"]
    diff["teams"] = expected.get("teams", [])
    diff["scan_complete"] = scan_info["rows_scanned"] == scan_info["total"]

    rerun_dir = artifacts_root / "reruns" / str(args.launch_id)
    rerun_dir.mkdir(parents=True, exist_ok=True)
    out_path = rerun_dir / "rerun_diff.json"
    out_path.write_text(json.dumps(diff, ensure_ascii=False, indent=2), encoding="utf-8")

    s = diff["stats"]
    print(f"DIFF rerun {args.launch_id} vs expected launch {diff['expected_launch_id']}")
    print(f"  expected: {diff['expected_count']} | scan_complete: {diff['scan_complete']} "
          f"({scan_info['rows_scanned']}/{scan_info['total']})")
    print(f"  healed: {s['healed']} | failed_again: {s['failed_again']} | skipped: {s['skipped']} "
          f"| absent: {s['absent']} | consistent: {s['consistent']}")
    print(f"  json: {out_path}")

    if not diff["scan_complete"]:
        print("СТАТУС: НЕПОЛНЫЙ СКАН RERUN — пересканировать")
        return 1
    if not s["consistent"]:
        print("СТАТУС: present + absent != expected — проверить маппинг номеров (extract_test_number)")
        return 1
    print("СТАТУС: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

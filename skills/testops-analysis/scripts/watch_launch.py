#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""watch_launch.py — дождаться завершения прогона TestOps и сразу его разобрать.

Зачем: прогон QG идёт часами, а «подождать в чате» дорого (каждый опрос — tool-call
и перепосылка контекста) и ненадёжно (сессия может прерваться). Ожидание — работа
скрипта: он поллит TestOps сам, а агенту/человеку отдаёт готовый артефакт.

Готовность определяется НЕ только флагом progress.ready: счётчики (total/passed/
failed/broken/skipped) должны совпасть N опросов подряд. Причина — реальный кейс
тихой потери хвоста: ready=true, а результаты ещё дописываются.

Шаги:
  1. поллинг до готовности;
  2. scan_testops.py (пагинация + двойной фильтр + гейт хвоста);
  3. (опц.) ожидание рерана и classify_rerun.py;
  4. publish_results.py — локально, и в ветку при --push.

Использование:
  # дождаться и просканировать (без сети публикации)
  py -X utf8 watch_launch.py --launch-id 1327498 --teams "T9,TX" --type QG

  # дождаться, разобрать и опубликовать в базу
  py -X utf8 watch_launch.py --launch-id 1327498 --teams "T9,TX" --type QG --push

  # дождаться ещё и рерана, классифицировать, опубликовать с причинами
  py -X utf8 watch_launch.py --launch-id 1327498 --teams "T9,TX" --type QG \
      --wait-rerun 1334823 --push

  # только проверка готовности (для отладки/ручной проверки)
  py -X utf8 watch_launch.py --launch-id 1327498 --poll-only

Коды возврата:
  0 — готово и разобрано; 1 — ошибка шага; 2 — нет доступа к TestOps;
  6 — не дождались готовности за --timeout.
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import scan_testops  # noqa: E402
from _paths import ARTIFACTS_ROOT, etalon_path  # noqa: E402
import team_canon  # noqa: E402

READY_FLAG_CANDIDATES = ("ready", "isReady", "completed")
COUNTER_NAMES = {
    "total": ("total", "total_count", "expected", "count"),
    "passed": ("passed", "passed_count", "pass"),
    "failed": ("failed", "failed_count", "fail"),
    "broken": ("broken", "broken_count"),
    "skipped": ("skipped", "skipped_count", "skip"),
}


class WatchError(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Чтение состояния запуска
# ---------------------------------------------------------------------------

def _walk(node):
    """Плоский обход вложенных dict/list (для толерантного поиска полей)."""
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _find_flag(payload, names):
    for obj in _walk(payload):
        if isinstance(obj, dict):
            for name in names:
                if name in obj and isinstance(obj[name], bool):
                    return obj[name]
    return None


def _find_counter(payload, names):
    for obj in _walk(payload):
        if isinstance(obj, dict):
            for name in names:
                value = obj.get(name)
                if isinstance(value, int) and not isinstance(value, bool):
                    return value
    return None


def parse_statistic(payload):
    """Статистика TestOps приходит списком [{status, count}] — сводим в счётчики."""
    counters = {"total": 0, "passed": 0, "failed": 0, "broken": 0, "skipped": 0}
    if isinstance(payload, list):
        for row in payload:
            if not isinstance(row, dict):
                continue
            status = str(row.get("status") or "").lower()
            count = row.get("count")
            if not isinstance(count, int) or isinstance(count, bool):
                continue
            if status in counters:
                counters[status] += count
            counters["total"] += count
    return counters


def launch_state(launch_id, cfg, token):
    """(name, closed|None, counters). Счётчики — из /statistic, флаг закрытия — из /api/launch."""
    base = cfg["testops_base_url"].rstrip("/")
    launch = scan_testops.http_get_json(f"{base}/api/launch/{launch_id}", token, cfg)
    name = str(launch.get("name") or launch_id)
    closed = launch.get("closed")
    closed = closed if isinstance(closed, bool) else None

    try:
        statistic = scan_testops.http_get_json(
            f"{base}/api/launch/{launch_id}/statistic", token, cfg)
        counters = parse_statistic(statistic)
    except RuntimeError:
        # статистика недоступна — пробуем вытащить счётчики из самого launch
        counters = {"total": _find_counter(launch, COUNTER_NAMES["total"]) or 0}
        for key in ("passed", "failed", "broken", "skipped"):
            counters[key] = _find_counter(launch, COUNTER_NAMES[key])
    return name, closed, counters


def signature(counters):
    return tuple(counters.get(key) for key in ("total", "passed", "failed", "broken", "skipped"))


def wait_ready(launch_id, cfg, token, interval, timeout, stable_needed, label="прогон"):
    """Ждёт готовности: счётчики не меняются stable_needed опросов (и total > 0).

    Флаг `closed` ускоряет решение (закрытый запуск не изменится), но не требуется:
    у запусков с autoclose=false он остаётся false навсегда.
    Защита от ложной готовности: пока total == 0, готовность не объявляется.
    """
    started = time.time()
    previous = None
    stable = 0
    last = None
    while True:
        elapsed = time.time() - started
        try:
            name, closed, counters = launch_state(launch_id, cfg, token)
            last = (name, closed, counters)
            sig = signature(counters)
            stable = stable + 1 if (previous is not None and sig == previous) else 1
            previous = sig
            closed_txt = "n/a" if closed is None else str(closed)
            print(f"  [{int(elapsed):>4}s] {label} {launch_id}: total={counters.get('total')} "
                  f"p/f/b/s={counters.get('passed')}/{counters.get('failed')}/"
                  f"{counters.get('broken')}/{counters.get('skipped')} "
                  f"closed={closed_txt} стабильно={stable}/{stable_needed}", flush=True)

            total = counters.get("total") or 0
            if total <= 0:
                pass  # ещё нет результатов — не объявляем готовность
            elif closed is True or stable >= stable_needed:
                if closed is not True:
                    print("  [i] запуск не помечен closed — ориентируюсь на стабильность счётчиков "
                          f"({stable} опросов без изменений)")
                return name, counters
        except RuntimeError as e:
            print(f"  [{int(elapsed):>4}s] ошибка опроса ({e}) — повтор через {interval}s", flush=True)
            stable, previous = 0, None

        if elapsed > timeout:
            detail = ""
            if last:
                detail = f" (последнее состояние: {last[2]})"
            raise WatchError(f"не дождались готовности {label} {launch_id} за {timeout}s{detail}", code=6)
        time.sleep(interval)


# ---------------------------------------------------------------------------
# Шаги разбора
# ---------------------------------------------------------------------------

def run_script(name, args, timeout=None):
    """Запускает соседний скрипт тем же интерпретатором. Возвращает rc."""
    cmd = [sys.executable, "-X", "utf8", str(SCRIPT_DIR / name), *args]
    print(f"\n$ {name} {' '.join(args)}", flush=True)
    proc = subprocess.run(cmd, timeout=timeout)
    return proc.returncode


def resolve_etalon(teams, test_type):
    """Путь к эталону в кэше: по канону первой команды (T9/TX -> TX-T9)."""
    first = (teams.split(",")[0] or "").strip()
    canon = team_canon.resolve_canon(first, None) or team_canon.normalize(first)
    return etalon_path(canon, test_type.upper())


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(
        description="Дождаться завершения прогона TestOps и разобрать его (scan + publish)")
    ap.add_argument("--launch-id", type=int, required=True)
    ap.add_argument("--teams", default="", help="команды через запятую: 'T9,TX'")
    ap.add_argument("--type", default="QG", choices=["HF", "QG", "RM", "hf", "qg", "rm"],
                    help="тип прогона (для пути эталона и публикации)")
    ap.add_argument("--etalon", default=None, help="путь к эталону (по умолчанию из кэша)")
    ap.add_argument("--root", default=None, help="корень артефактов (~/.qa-artifacts)")
    ap.add_argument("--interval", type=int, default=90, help="интервал опроса, сек (по умолчанию 90)")
    ap.add_argument("--timeout", type=int, default=10800, help="лимит ожидания прогона, сек (3 ч)")
    ap.add_argument("--stable", type=int, default=2,
                    help="сколько опросов подряд счётчики должны не меняться (по умолчанию 2)")
    ap.add_argument("--poll-only", action="store_true",
                    help="только дождаться готовности, без скана и публикации")
    ap.add_argument("--skip-scan", action="store_true", help="не запускать скан (уже сделан)")
    ap.add_argument("--wait-rerun", type=int, default=None,
                    help="ID рерана: дождаться его, классифицировать и опубликовать с причинами")
    ap.add_argument("--rerun-timeout", type=int, default=7200, help="лимит ожидания рерана, сек")
    ap.add_argument("--push", action="store_true", help="опубликовать результат в ветку базы")
    ap.add_argument("--json", action="store_true", help="машинный вывод итога")
    args = ap.parse_args(argv)

    root = Path(args.root).expanduser().resolve() if args.root else ARTIFACTS_ROOT
    test_type = args.type.upper()

    try:
        cfg = scan_testops.load_config(root)
        token = scan_testops.resolve_token(cfg)
        if not token:
            print(f"ОШИБКА: нет токена TestOps (env {cfg.get('testops_token_env')} или "
                  f"{root / 'config.json'}): задай переменную и открой новый терминал", file=sys.stderr)
            return 2

        print(f"=== Ожидание прогона {args.launch_id} (интервал {args.interval}s, "
              f"лимит {args.timeout}s) ===")
        name, counters = wait_ready(args.launch_id, cfg, token, args.interval, args.timeout,
                                    max(1, args.stable))
        print(f"\nГотов: {name} — total={counters.get('total')}, "
              f"passed={counters.get('passed')}, failed={counters.get('failed')}, "
              f"broken={counters.get('broken')}, skipped={counters.get('skipped')}")

        if args.poll_only:
            if args.json:
                import json
                print(json.dumps({"launch_id": args.launch_id, "name": name, "counters": counters,
                                  "ready": True}, ensure_ascii=False, indent=2))
            return 0

        if not args.teams:
            print("ОШИБКА: для скана нужны --teams (например 'T9,TX')", file=sys.stderr)
            return 1

        etalon = Path(args.etalon).expanduser().resolve() if args.etalon \
            else resolve_etalon(args.teams, test_type)
        if not etalon.exists():
            print(f"ОШИБКА: нет эталона {etalon}. Скачай: fetch_etalons.py --team "
                  f"{etalon.parent.name} --type {test_type}", file=sys.stderr)
            return 1

        if not args.skip_scan:
            rc = run_script("scan_testops.py", [
                "--launch-id", str(args.launch_id), "--teams", args.teams,
                "--etalon", str(etalon), "--tail-check",
                *(["--root", str(root)] if args.root else []),
            ])
            if rc != 0:
                print(f"ОШИБКА: скан завершился с кодом {rc} — дальше не идём", file=sys.stderr)
                return 1

        rerun_id = args.wait_rerun
        if rerun_id:
            print(f"\n=== Ожидание рерана {rerun_id} (лимит {args.rerun_timeout}s) ===")
            wait_ready(rerun_id, cfg, token, args.interval, args.rerun_timeout,
                       max(1, args.stable), label="реран")
            rc = run_script("classify_rerun.py", [
                "--rerun-id", str(rerun_id), "--scan",
                "--expected-launch-id", str(args.launch_id),
                "--team", (args.teams.split(",")[0] or "").strip(),
                *(["--root", str(root)] if args.root else []),
            ])
            if rc != 0:
                print(f"ОШИБКА: классификация рерана вернула {rc}: причины не собраны, "
                      f"публикую только цифры", file=sys.stderr)
                rerun_id = None

        publish_args = ["--team", (args.teams.split(",")[0] or "").strip(),
                        "--type", test_type, "--launch-id", str(args.launch_id)]
        if rerun_id:
            publish_args += ["--rerun-id", str(rerun_id)]
        if args.push:
            publish_args.append("--push")
        if args.root:
            publish_args += ["--root", str(root)]
        rc = run_script("publish_results.py", publish_args)
        if rc != 0:
            print(f"ОШИБКА: публикация вернула {rc}", file=sys.stderr)
            return 1

        if args.json:
            import json
            print(json.dumps({"launch_id": args.launch_id, "name": name, "counters": counters,
                              "rerun_id": rerun_id, "pushed": bool(args.push)},
                             ensure_ascii=False, indent=2))
        print("\nГОТОВО: прогон разобран" + (" и опубликован" if args.push else " (локально)"))
        return 0
    except WatchError as e:
        print(f"ОШИБКА: {e}", file=sys.stderr)
        return e.code
    except RuntimeError as e:
        print(f"ОШИБКА TestOps: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

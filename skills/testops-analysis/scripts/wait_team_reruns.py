#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wait_team_reruns.py — дождаться КОМАНДНЫХ реранов и разобрать каждый.

Зачем: `dispatch_team_reruns.py` раздаёт @-списки по джобам `DEBUG/<TEAM>` и пишет
`build_url`, но анализ требует **launch id в TestOps**. Связи build → launch в API нет,
поэтому она восстанавливается здесь:

  1. из `build_url` берём номер сборки (последний числовой сегмент);
  2. ищем запуски, созданные после `dispatch_at` (AQL `createdDate >`);
  3. клиент-side отбираем тот, в имени которого есть `#<build>` и токен команды
     (имя рерана выглядит как `DEBUG - qa (Команды/TSPB_QG - #1346)`);
  4. проверяем пересечение с @-списком команды (номера из expected_set.json).

Дальше: параллельный (в одном цикле опроса) wait по всем запускам, затем для каждой
команды `classify_rerun.py` (причины) и `publish_results.py` (локально; в ветку — по --push).

Использование:
  py -X utf8 wait_team_reruns.py --launch-id 1327498                      # резолв + ожидание + разбор
  py -X utf8 wait_team_reruns.py --launch-id 1327498 --resolve-only       # только build -> launch
  py -X utf8 wait_team_reruns.py --launch-id 1327498 --map 111=1355850,112=1355860  # вручную
  py -X utf8 wait_team_reruns.py --launch-id 1327498 --push               # + публикация в базу

Коды возврата: 0 — все команды разобраны; 1 — частично (см. rerun_wait.json);
2 — нет доступа к TestOps; 6 — таймаут ожидания.
"""
import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import scan_testops  # noqa: E402
from _paths import ARTIFACTS_ROOT, dispatch_path  # noqa: E402

READY_STABLE = 2
COUNTER_KEYS = ("total", "passed", "failed", "broken", "skipped")


class WaitError(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# TestOps: поиск запуска и его состояние
# ---------------------------------------------------------------------------

def project_id(launch_id, cfg, token):
    """projectId берём из первичного запуска — он же нужен для поиска реранов."""
    base = cfg["testops_base_url"].rstrip("/")
    launch = scan_testops.http_get_json(f"{base}/api/launch/{launch_id}", token, cfg)
    pid = launch.get("projectId")
    if not pid:
        raise WaitError("в первичном запуске нет projectId — не могу искать рераны")
    return int(pid)


def to_millis(iso_text):
    """ISO-время -> epoch ms (createdDate в TestOps в миллисекундах)."""
    if not iso_text:
        return None
    try:
        return int(datetime.fromisoformat(str(iso_text)).timestamp() * 1000)
    except ValueError:
        return None


def search_launches(pid, since_ms, cfg, token, limit=200):
    """Запуски проекта, созданные после since_ms (AQL createdDate >), новые сверху."""
    base = cfg["testops_base_url"].rstrip("/")
    out, page = [], 0
    while len(out) < limit:
        url = (f"{base}/api/launch/__search?projectId={pid}"
               f"&rql=createdDate%20%3E%20{int(since_ms)}&page={page}&size=100")
        data = scan_testops.http_get_json(url, token, cfg)
        content = data.get("content") or []
        if not content:
            break
        out.extend(content)
        if data.get("last", True) or len(content) < 100:
            break
        page += 1
    out.sort(key=lambda x: int(x.get("createdDate") or 0), reverse=True)
    return out[:limit]


def build_number(build_url):
    """Номер сборки из build_url: последний числовой сегмент пути."""
    if not build_url:
        return None
    segments = [s for s in str(build_url).split("/") if s]
    for seg in reversed(segments):
        if seg.isdigit():
            return int(seg)
    return None


def resolve_map(entries, launches, team_tokens):
    """build_url/team -> launch_id по номеру сборки и токену команды в имени."""
    mapping = {}
    for entry in entries:
        team = str(entry.get("team") or "").upper()
        build = build_number(entry.get("build_url"))
        if not team or build is None:
            continue
        token_sets = {team} | set(team_tokens.get(team) or [])
        hit = None
        for launch in launches:
            name = str(launch.get("name") or "").upper()
            if f"#{build}" not in name:
                continue
            if any(tok and tok.upper() in name for tok in token_sets):
                hit = launch
                break
        if hit:
            mapping[team] = {"launch_id": int(hit["id"]), "name": hit.get("name"),
                             "build": build, "created": hit.get("createdDate")}
        else:
            mapping[team] = {"launch_id": None, "build": build,
                             "error": f"не нашли запуск с #{build} для {team}"}
    return mapping


def launch_counters(launch_id, cfg, token):
    """(closed, counters) или None, если запуск недоступен (404/403).

    403 на statistic бывает у запусков вне доступных проектов — это не «ещё не создан»,
    а «не наш/не существует»; наружу такой случай отдаём как None (см. wait_all).
    """
    base = cfg["testops_base_url"].rstrip("/")
    launched = False
    try:
        launch = scan_testops.http_get_json(f"{base}/api/launch/{launch_id}", token, cfg)
        launched = True
    except RuntimeError as e:
        if "404" in str(e) or "403" in str(e):
            return None
        raise
    counters = {key: 0 for key in COUNTER_KEYS}
    try:
        stat = scan_testops.http_get_json(f"{base}/api/launch/{launch_id}/statistic", token, cfg)
    except RuntimeError as e:
        if "404" in str(e) or "403" in str(e):
            stat = []
        else:
            raise
    if isinstance(stat, list):
        for row in stat:
            status = str(row.get("status") or "").lower()
            count = row.get("count")
            if isinstance(count, int) and not isinstance(count, bool):
                if status in counters:
                    counters[status] = count
                counters["total"] += count
    if not launched:
        return None
    closed = launch.get("closed")
    return (closed if isinstance(closed, bool) else None), counters


# ---------------------------------------------------------------------------
# Ожидание и разбор
# ---------------------------------------------------------------------------

def wait_all(resolved, cfg, token, interval, timeout, stable_needed, start_timeout=1800):
    """Ждёт все запуски в одном цикле опроса. Возвращает {team: {status, counters, ...}}.

    Статусы: running -> done | missing (запуск так и не появился за start_timeout) |
    timeout (не завершился за общий timeout). Запуск, которого нет и не будет
    (например, неверный id из --map), не держит цикл до общего таймаута.
    """
    pending = {team: info["launch_id"] for team, info in resolved.items()
               if info.get("launch_id")}
    state = {team: {"status": "waiting", "counters": None, "stable": 0, "prev": None,
                    "first_seen": None}
             for team in pending}
    started, last_round = time.time(), {}
    while pending:
        for team in list(pending):
            launch_id = pending[team]
            try:
                result = launch_counters(launch_id, cfg, token)
            except RuntimeError as e:
                entry_errors = state[team].setdefault("errors", 0) + 1
                state[team]["errors"] = entry_errors
                state[team]["note"] = f"ошибка опроса ({entry_errors}): {e}"
                if entry_errors >= 3:
                    state[team]["status"] = "error"
                    pending.pop(team)
                continue
            entry = state[team]
            if result is None:
                # запуска нет (404/403 — не создан, удалён или чужой проект):
                # даём время появиться, но не бесконечно
                if entry["first_seen"] is None:
                    entry["first_seen"] = time.time()
                if time.time() - entry["first_seen"] > start_timeout:
                    entry["status"] = "missing"
                    pending.pop(team)
                else:
                    entry["status"] = "not_started"
                continue
            closed, counters = result
            sig = tuple(counters.get(k) for k in COUNTER_KEYS)
            entry["status"] = "running"
            entry["stable"] = entry["stable"] + 1 if entry["prev"] == sig else 1
            entry["prev"] = sig
            entry["counters"] = counters
            total = counters.get("total") or 0
            # Готовность: closed=True — единственный надёжный сигнал (TestOps закрывает
            # запуск по завершении джобы). Стабильность счётчиков — только фолбэк, когда
            # флага нет: у живого прогона счётчики могут не меняться минутами.
            if total > 0 and closed is True:
                entry["status"] = "done"
                pending.pop(team)
            elif total > 0 and closed is None and entry["stable"] >= stable_needed:
                entry["status"] = "done"
                entry["note"] = "флага closed нет — готовность по стабильности счётчиков"
                pending.pop(team)
        snapshot = {t: (s["status"], (s["counters"] or {}).get("total")) for t, s in state.items()}
        if snapshot != last_round:
            elapsed = int(time.time() - started)
            detail = ", ".join(f"{t}:{v[0]}({v[1]})" for t, v in sorted(snapshot.items()))
            print(f"  [{elapsed:>4}s] {detail}", flush=True)
            last_round = snapshot
        if not pending:
            break
        if time.time() - started > timeout:
            for team in pending:
                state[team]["status"] = "timeout"
            break
        time.sleep(interval)
    return state


def run_script(name, args):
    cmd = [sys.executable, "-X", "utf8", str(SCRIPT_DIR / name), *args]
    print(f"\n$ {name} {' '.join(args)}", flush=True)
    return subprocess.run(cmd).returncode


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(
        description="Дождаться командных реранов и разобрать каждый (classify + publish)")
    ap.add_argument("--launch-id", type=int, required=True, help="id первичного прогона")
    ap.add_argument("--dispatch", default=None,
                    help="путь к dispatch.json (по умолчанию launches/{id}/dispatch.json)")
    ap.add_argument("--map", default="",
                    help="ручная карта team=launch_id через запятую (обходит авто-резолв)")
    ap.add_argument("--root", default=None, help="корень артефактов")
    ap.add_argument("--type", default="QG", choices=["HF", "QG", "RM", "hf", "qg", "rm"])
    ap.add_argument("--interval", type=int, default=90)
    ap.add_argument("--timeout", type=int, default=10800, help="лимит ожидания всех, сек")
    ap.add_argument("--stable", type=int, default=READY_STABLE)
    ap.add_argument("--start-timeout", type=int, default=1800,
                    help="сколько ждать появления запуска вообще (404), сек")
    ap.add_argument("--since-skew-min", type=int, default=15,
                    help="запас назад от dispatch_at при поиске запусков, мин")
    ap.add_argument("--resolve-only", action="store_true", help="только найти launch id")
    ap.add_argument("--skip-classify", action="store_true", help="не классифицировать (только ожидание)")
    ap.add_argument("--push", action="store_true", help="опубликовать результат в базу")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    root = Path(args.root).expanduser().resolve() if args.root else ARTIFACTS_ROOT
    test_type = args.type.upper()

    try:
        cfg = scan_testops.load_config(root)
        token = scan_testops.resolve_token(cfg)
        if not token:
            print("ОШИБКА: нет токена TestOps — задай env и открой новый терминал", file=sys.stderr)
            return 2

        # --- 1. откуда берём команды и сборки ---
        if args.map:
            resolved = {}
            for pair in args.map.split(","):
                team, _, lid = pair.partition("=")
                if team.strip() and lid.strip().isdigit():
                    resolved[team.strip().upper()] = {"launch_id": int(lid), "source": "manual"}
        else:
            disp = Path(args.dispatch) if args.dispatch else dispatch_path(args.launch_id, args.root)
            if not disp.exists():
                raise WaitError(f"нет {disp}: сначала раздай рераны (dispatch_team_reruns.py)")
            payload = json.loads(disp.read_text(encoding="utf-8"))
            entries = payload.get("teams") or []
            if not entries:
                raise WaitError(f"в {disp} нет записей по командам")
            since = to_millis(payload.get("dispatch_at")) or int(time.time() * 1000)
            since -= args.since_skew_min * 60_000
            pid = project_id(args.launch_id, cfg, token)
            launches = search_launches(pid, since, cfg, token)
            team_tokens = {str(e.get("team") or "").upper(): (e.get("aliases") or []) for e in entries}
            resolved = resolve_map(entries, launches, team_tokens)

        print("=== Карта build -> launch ===")
        for team, info in sorted(resolved.items()):
            if info.get("launch_id"):
                print(f"  {team:<8} build #{info.get('build', '?'):<6} -> launch {info['launch_id']}"
                      f"  {info.get('name') or ''}")
            else:
                print(f"  {team:<8} build #{info.get('build', '?'):<6} -> НЕ НАЙДЕН: {info.get('error')}")

        if args.resolve_only:
            if args.json:
                print(json.dumps(resolved, ensure_ascii=False, indent=2))
            return 0

        found = {t: i for t, i in resolved.items() if i.get("launch_id")}
        if not found:
            raise WaitError("не удалось сопоставить ни одной команды — укажи --map team=launch_id")

        # --- 2. ждём все ---
        print(f"\n=== Ожидание {len(found)} реранов (интервал {args.interval}s, лимит {args.timeout}s) ===")
        state = wait_all(found, cfg, token, args.interval, args.timeout, max(1, args.stable),
                         start_timeout=args.start_timeout)

        # --- 3. разбор ---
        results = []
        if not args.skip_classify:
            for team in sorted(found):
                launch_id = found[team]["launch_id"]
                info = state.get(team) or {}
                if info.get("status") != "done":
                    print(f"\n[i] {team}: реран не завершён ({info.get('status')}) — пропускаю разбор")
                    results.append({"team": team, "launch_id": launch_id,
                                    "status": info.get("status"), "analysed": False})
                    continue
                rc = run_script("classify_rerun.py", [
                    "--rerun-id", str(launch_id), "--scan",
                    "--expected-launch-id", str(args.launch_id), "--team", team,
                    *(["--root", str(root)] if args.root else []),
                ])
                analysed = rc == 0
                if analysed:
                    pub_args = ["--team", team, "--type", test_type,
                                "--launch-id", str(args.launch_id),
                                "--rerun-id", str(launch_id)]
                    if args.push:
                        pub_args.append("--push")
                    if args.root:
                        pub_args += ["--root", str(root)]
                    pub_rc = run_script("publish_results.py", pub_args)
                    analysed = pub_rc == 0
                results.append({"team": team, "launch_id": launch_id, "status": "done",
                                "counters": info.get("counters"), "analysed": analysed})
        else:
            results = [{"team": t, "launch_id": found[t]["launch_id"],
                        "status": (state.get(t) or {}).get("status"),
                        "counters": (state.get(t) or {}).get("counters"), "analysed": False}
                       for t in sorted(found)]

        # --- 4. артефакт ожидания ---
        out_path = root / "launches" / str(args.launch_id) / "rerun_wait.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps({
            "launch_id": args.launch_id,
            "waited_at": datetime.now().isoformat(timespec="seconds"),
            "resolved": resolved,
            "teams": results,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        ok = sum(1 for r in results if r.get("analysed"))
        failed = [r["team"] for r in results if not r.get("analysed")]
        print(f"\n=== Итог: разобрано {ok} из {len(results)} ===")
        for r in results:
            counters = r.get("counters") or {}
            print(f"  {r['team']:<8} {str(r.get('status')):<12} launch {r.get('launch_id')}"
                  f"  total={counters.get('total')} f/b/s="
                  f"{counters.get('failed')}/{counters.get('broken')}/{counters.get('skipped')}"
                  f"  разбор={'да' if r.get('analysed') else 'нет'}")
        if failed:
            print("  не разобраны: " + ", ".join(failed))
        print(f"  артефакт: {out_path}")
        if args.json:
            print(json.dumps({"results": results, "artifact": str(out_path)},
                             ensure_ascii=False, indent=2))
        return 0 if not failed else 1
    except WaitError as e:
        print(f"ОШИБКА: {e}", file=sys.stderr)
        return e.code
    except RuntimeError as e:
        print(f"ОШИБКА TestOps: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

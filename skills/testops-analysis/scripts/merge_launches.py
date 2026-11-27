#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""merge_launches.py — слияние двух запусков TestOps в один (API, без UI).

Штатная операция TestOps: запуск-источник вливается в целевой запуск, после чего
источник перестаёт существовать как отдельный запуск (API отдаёт 404). Операция
НЕОБРАТИМА — поэтому по умолчанию скрипт работает как план и требует --yes.

Контракт API (снят с реального запроса UI 22.11.2026):
    POST /api/launch/merge
    {"from": <id источника>, "to": <id цели>}
    -> 200 {"id": <id цели>, "name": "<имя цели>"}
Поля называются именно `from`/`to`: прочие варианты (`launchIds`, `mainLaunchId`,
`mergedLaunchIds`, ...) Jackson молча игнорирует, и контроллер падает с HTTP 500.

Использование:
  # план без изменений (по умолчанию)
  py -X utf8 merge_launches.py --from 1334823 --to 1363174

  # выполнить слияние
  py -X utf8 merge_launches.py --from 1334823 --to 1363174 --yes

Проверки перед отправкой: оба запуска существуют, это разные запуски, один проект.
После успеха пишет журнал: <artifacts>/launches/<to>/merge.json.
Скрипт использует только стандартную библиотеку и тот же токен, что и scan_testops.py.
"""
import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import scan_testops as S  # noqa: E402
from _paths import artifacts_root  # noqa: E402


def http_post_json(url: str, payload: dict, token: str, cfg: dict, retries: int = 1):
    """POST + JSON с Authorization: Bearer <jwt>. При 401 — переполучение токена."""
    base = cfg["testops_base_url"].rstrip("/")
    last_err = None
    body = json.dumps(payload).encode("utf-8")
    for _ in range(retries + 1):
        if not S._STATE["jwt"]:
            S._STATE["jwt"] = S.acquire_jwt(base, token)
        headers = {
            "Authorization": f"Bearer {S._STATE['jwt']}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:300]}"
            if e.code == 401:
                S._STATE["jwt"] = None
                continue
            break
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
    raise RuntimeError(f"POST {url} не удался: {last_err}")


def launch_info(launch_id: int, token: str, cfg: dict) -> dict:
    """Карточка запуска + число результатов из плоского списка (дёшево)."""
    base = cfg["testops_base_url"].rstrip("/")
    launch = S.http_get_json(f"{base}/api/launch/{launch_id}", token, cfg)
    results = 0
    try:
        page = S.http_get_json(
            f"{base}/api/v2/launch/{launch_id}/test-result/flat?page=0&size=1", token, cfg)
        results = int(page.get("totalElements") or 0)
    except Exception:  # noqa: BLE001 — счётчик не должен блокировать слияние
        results = -1
    return {
        "id": launch_id,
        "name": launch.get("name"),
        "project_id": launch.get("projectId"),
        "closed": launch.get("closed"),
        "results": results,
    }


def write_log(root: Path, src: dict, dst: dict, response: dict) -> Path:
    """Журнал слияний на целевой запуск: launches/<to>/merge.json (дописывается)."""
    path = root / "launches" / str(dst["id"]) / "merge.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"merges": []}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data.setdefault("merges", [])
        except (json.JSONDecodeError, OSError):
            data = {"merges": []}
    data["merges"].append({
        "from": {"id": src["id"], "name": src["name"], "results": src["results"]},
        "to": {"id": dst["id"], "name": dst["name"], "results": dst["results"]},
        "merged_at": datetime.now().isoformat(timespec="seconds"),
        "response": response,
    })
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(description="Слияние двух запусков TestOps (from -> to)")
    ap.add_argument("--from", dest="src_id", type=int, required=True,
                    help="ID запуска-источника (после слияния исчезнет как отдельный запуск)")
    ap.add_argument("--to", dest="dst_id", type=int, required=True,
                    help="ID целевого запуска (в него вливаются результаты)")
    ap.add_argument("--root", default=None, help="Корень артефактов (по умолчанию ~/.qa-artifacts)")
    ap.add_argument("--yes", action="store_true", help="Подтвердить необратимое слияние")
    ap.add_argument("--dry-run", action="store_true", help="Только план (по умолчанию и так план)")
    args = ap.parse_args()

    if args.src_id == args.dst_id:
        print("FAIL: --from и --to совпадают — сливать запуск сам с собой нельзя")
        return 1

    root = artifacts_root(args.root)
    cfg = S.load_config(root)
    token = S.resolve_token(cfg)
    if not token:
        print("FAIL: токен TestOps не задан (TESTOPS_TOKEN / .qa-artifacts/config.json)")
        return 1

    try:
        src = launch_info(args.src_id, token, cfg)
        dst = launch_info(args.dst_id, token, cfg)
    except RuntimeError as e:
        print(f"FAIL: {e}")
        return 1

    print(f"FROM {src['id']} '{src['name']}' results={src['results']} closed={src['closed']}")
    print(f"TO   {dst['id']} '{dst['name']}' results={dst['results']} closed={dst['closed']}")

    if src["project_id"] != dst["project_id"]:
        print(f"FAIL: разные проекты ({src['project_id']} != {dst['project_id']}) — "
              "TestOps не сливает запуски разных проектов")
        return 1

    print("ПЛАН: POST /api/launch/merge "
          + json.dumps({"from": src["id"], "to": dst["id"]}, ensure_ascii=False))
    print(f"ВНИМАНИЕ: после слияния запуск {src['id']} перестанет существовать "
          f"(станет частью {dst['id']}). Операция необратима.")

    if args.dry_run or not args.yes:
        print("СТАТУС: ПЛАН (слияние не выполнено). Для выполнения добавь --yes")
        return 0

    base = cfg["testops_base_url"].rstrip("/")
    try:
        response = http_post_json(f"{base}/api/launch/merge",
                                  {"from": src["id"], "to": dst["id"]}, token, cfg)
    except RuntimeError as e:
        print(f"FAIL: {e}")
        return 1

    print(f"RESULT_ID={response.get('id')}")
    print(f"RESULT_NAME={response.get('name')}")
    try:
        log_path = write_log(root, src, dst, response)
        print(f"LOG={log_path}")
    except OSError as e:  # noqa: BLE001 — журнал не должен отменять состоявшееся слияние
        print(f"WARN: слияние прошло, но журнал не записан: {e}")
    print("СТАТУС: OK — запуски объединены")
    return 0


if __name__ == "__main__":
    sys.exit(main())

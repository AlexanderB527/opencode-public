#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
export_team_etalons.py — выгрузка эталонных списков тестов по командам из прогона TestOps.

Задача: из прогона (у которого нет падающих тестов) собрать полный список тестов для КАЖДОЙ
команды (по полю Team тест-кейса) и записать папки/файлы-эталоны в формате:
    <каталог>/<Команда>/etalon_<тип>.txt
    строка: "<номер> <название>"   (номер по паттерну ^\\d{2}(\\.\\d{2,3})+)

Формат имени файла — etalon_<тип>.txt (тип = QG/HF/RM), внутри папки команды.
Каталог по умолчанию — локальный кэш `<artifacts root>/etalons` (источник истины —
ветка qa-analysis-data репозитория AUTOTEST/sfa; перенос в неё — отдельным PR).

Ключевое отличие от scan_testops.py:
  - НЕТ обязательного входного эталона (список строится из самого прогона).
  - Группировка по ВСЕМ командам, найденным в прогоне.
  - Исключение команд ПО ЖЕЛАНИЮ (--exclude): T9, TX, Tx-T9 и т.п. — для них эталоны уже есть.

Механика HTTP/пагинации/Team-извлечения переиспользована из scan_testops.py:
  - Авторизация: POST /api/uaa/oauth/token (grant_type=apitoken) -> JWT, Bearer.
  - Пагинация: /api/v2/launch/{launchId}/test-result/flat (size=100, гейт rows_scanned == total).
  - Команда: custom field 'Team' тест-кейса (GET /api/testcase/{id}/cfv), кэш по testCaseId.

Использование:
  python export_team_etalons.py --launch-id 1110224 --type QG --root .qa-artifacts \
      [--exclude "T9,TX,Tx-T9"] [--no-pass-filter]
Exit: 0 = ok, 1 = ошибка.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from _paths import ARTIFACTS_ROOT, ETALONS_DIR  # noqa: E402

# Единый нормализатор имён команд (транслит кириллицы, срез префикса, алиасы)
import team_canon  # noqa: E402

DEFAULT_ARTIFACTS_ROOT = ARTIFACTS_ROOT

TEST_NUMBER_RE = re.compile(r"(?<!\d)(\d{2}(?:\.\d{2,3})+)(?!\d)")

_STATE = {"jwt": None, "cfg": None, "token": "", "base": "", "project_id": None,
          "team_cache": {}}


def load_config(artifacts_root: Path) -> dict:
    cfg_path = artifacts_root / "config.json"
    cfg = {
        "testops_base_url": "https://example.invalid",
        "testops_token_env": "TESTOPS_TOKEN",
        "testops_token": "",
        "page_size": 100,
    }
    if cfg_path.exists():
        try:
            cfg.update(json.loads(cfg_path.read_text(encoding="utf-8")))
        except Exception as e:  # noqa: BLE001
            print(f"WARN: не удалось прочитать {cfg_path}: {e}")
    return cfg


def resolve_token(cfg: dict) -> str:
    token = os.environ.get(cfg.get("testops_token_env", "TESTOPS_TOKEN"), "")
    if not token:
        token = cfg.get("testops_token", "")
    return token.strip()


def acquire_jwt(base: str, token: str) -> str:
    url = f"{base}/api/uaa/oauth/token"
    body = urllib.parse.urlencode(
        {"grant_type": "apitoken", "scope": "openid", "token": token}
    ).encode("utf-8")
    headers = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"OAuth token не получен: HTTP {e.code}: "
            f"{e.read().decode('utf-8', errors='replace')[:300]}"
        )
    jwt = str(data.get("access_token") or "")
    if not jwt:
        raise RuntimeError("OAuth token: в ответе нет access_token")
    return jwt


def http_get_json(url: str, token: str, cfg: dict, retries: int = 1):
    base = cfg["testops_base_url"].rstrip("/")
    last_err = None
    for attempt in range(retries + 1):
        if not _STATE["jwt"]:
            _STATE["jwt"] = acquire_jwt(base, token)
        headers = {"Authorization": f"Bearer {_STATE['jwt']}"}
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:300]}"
            if e.code == 401:
                _STATE["jwt"] = None
                continue
            if e.code == 403:
                break
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
    raise RuntimeError(f"GET {url} не удался ({retries + 1} попыток): {last_err}")


def normalize_team(value: str) -> str:
    """Нормализация команды через единый нормализатор team_canon.

    Полный транслит кириллицы (ТММБ -> TMMB, ТСПб -> TSPB) + срез префикса
    'SFA Команда'. Источник истины — team_canon, здесь логику не дублировать.
    """
    return team_canon.normalize(value)


def init_scan_state(cfg: dict, token: str, launch: dict) -> None:
    _STATE["cfg"] = cfg
    _STATE["token"] = token
    _STATE["base"] = cfg["testops_base_url"].rstrip("/")
    _STATE["project_id"] = launch.get("projectId")
    _STATE["team_cache"].clear()


def get_team(testcase_id) -> str:
    """Команда — custom field 'Team' тест-кейса (GET /api/testcase/{id}/cfv). Кэш по testCaseId."""
    if not testcase_id:
        return ""
    cached = _STATE["team_cache"].get(testcase_id)
    if cached is not None:
        return cached
    team = ""
    if _STATE["base"] and _STATE["project_id"] is not None:
        try:
            data = http_get_json(
                f"{_STATE['base']}/api/testcase/{testcase_id}/cfv?projectId={_STATE['project_id']}",
                _STATE["token"], _STATE["cfg"])
            for item in data or []:
                cf = item.get("customField") or {}
                if str(cf.get("name") or "") == "Team":
                    team = str(item.get("name") or "")
                    if not team:
                        values = item.get("values") or []
                        if values:
                            team = str(values[0].get("name") or "")
                    break
        except Exception:  # noqa: BLE001
            team = ""
    _STATE["team_cache"][testcase_id] = team
    return team


def extract_number(name: str) -> str:
    m = TEST_NUMBER_RE.search(str(name or ""))
    return m.group(1) if m else ""


def export_launch(launch_id: int, cfg: dict, token: str, type_label: str,
                  pass_filter: bool, exclude_set: set[str]):
    base = cfg["testops_base_url"].rstrip("/")
    size = int(cfg.get("page_size", 100))
    if size > 100:
        raise ValueError("page_size > 100 запрещён")

    launch = http_get_json(f"{base}/api/launch/{launch_id}", token, cfg)
    init_scan_state(cfg, token, launch)
    launch_name = str(launch.get("name") or launch_id)

    total = 0
    page = 0
    processed = 0
    # testCaseId -> список (number, name) с учётом пасса
    # по умолчанию берём ВСЕ строки (какие запускались), число уникальных номеров считаем.
    seen_numbers_by_team = {}  # team_norm -> set(number)
    name_by_number_team = {}   # (team_norm, number) -> первое название
    rows_without_number = 0

    while True:
        data = http_get_json(
            f"{base}/api/v2/launch/{launch_id}/test-result/flat?page={page}&size={size}", token, cfg
        )
        content = data.get("content") or []
        if page == 0:
            total = int(data.get("totalElements") or len(content))
        if not content:
            break

        for r in content:
            processed += 1
            status = str(r.get("status") or "").lower()
            name = str(r.get("name") or "")
            number = extract_number(name)
            tcid = r.get("testCaseId")

            # фильтр по статусу (pass_filter=False => берём всё)
            if pass_filter and status != "passed":
                continue

            team_raw = get_team(tcid)
            team_norm = normalize_team(team_raw)
            if not team_norm:
                continue
            if team_norm in exclude_set:
                continue
            # у некоторых строк номер есть даже без suite — если нет, пропускаем для эталона
            if not number:
                rows_without_number += 1
                continue

            seen_numbers_by_team.setdefault(team_norm, set()).add(number)
            key = (team_norm, number)
            if key not in name_by_number_team:
                name_by_number_team[key] = name

        if page % 5 == 0:
            print(f"  [progress] page {page}: processed={processed}/{total} "
                  f"teams={sorted(seen_numbers_by_team.keys())}", file=sys.stderr, flush=True)

        if processed >= total:
            break
        page += 1
        if page > 10_000:
            raise RuntimeError("Пагинация превысила 10_000 страниц")

    return {
        "launch_id": launch_id,
        "launch_name": launch_name,
        "total": total,
        "processed": processed,
        "rows_without_number": rows_without_number,
        "seen_numbers_by_team": seen_numbers_by_team,
        "name_by_number_team": name_by_number_team,
    }


def natural_sort_key(n: str):
    """Естественная сортировка числовых компонент '02.01' < '10.01'."""
    return [int(x) for x in n.split(".")]


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(description="Экспорт эталонов тестов по командам из прогона")
    ap.add_argument("--launch-id", type=int, required=True)
    ap.add_argument("--type", default="QG", help="Тип тестов для имени файла (QG/HF/RM)")
    ap.add_argument("--root", default=None, help="Корень .qa-artifacts")
    ap.add_argument("--exclude", default="", help="Команды-исключения через запятую (T9,TX,Tx-T9)")
    ap.add_argument("--pass-filter", action="store_true",
                    help="Брать только PASS-строки (по умолчанию — все)")
    ap.add_argument("--out", default=None,
                    help="Куда писать эталоны (по умолчанию <artifacts root>/etalons — локальный кэш, "
                         "источник истины — ветка qa-analysis-data)")
    args = ap.parse_args()

    artifacts_root = Path(args.root) if args.root else DEFAULT_ARTIFACTS_ROOT
    cfg = load_config(artifacts_root)
    token = resolve_token(cfg)
    if not token:
        print("ОШИБКА: токен TestOps не задан. Укажи TESTOPS_TOKEN в env или testops_token в "
              f"{artifacts_root / 'config.json'}.")
        return 1

    exclude_set = {normalize_team(t) for t in args.exclude.split(",") if t.strip()}

    try:
        res = export_launch(args.launch_id, cfg, token, args.type,
                            pass_filter=args.pass_filter, exclude_set=exclude_set)
    except Exception as e:  # noqa: BLE001
        print(f"ОШИБКА экспорта: {e}")
        return 1

    seen = res["seen_numbers_by_team"]
    name_map = res["name_by_number_team"]

    print(f"EXPORT LAUNCH {args.launch_id} '{res['launch_name']}'")
    print(f"  total_results: {res['total']} | processed: {res['processed']} "
          f"| rows_without_number: {res['rows_without_number']}")

    if not seen:
        print("  Команд не найдено (или все исключены)")
        return 1

    # Эталоны — данные в ветке qa-analysis-data: пишем в локальный кэш,
    # а в ветку они попадают отдельным PR (скилл сам их не коммитит).
    etalons_dir = Path(args.out).expanduser().resolve() if args.out else ETALONS_DIR
    written = []
    for team_norm in sorted(seen.keys(), key=lambda t: t.casefold()):
        numbers = sorted(seen[team_norm], key=natural_sort_key)
        # имя файла: etalon_<тип>.txt (внутри папки команды), тип = QG/HF/RM
        fname = f"etalon_{args.type}.txt"
        fdir = etalons_dir / team_norm
        fdir.mkdir(parents=True, exist_ok=True)
        fpath = fdir / fname
        lines = []
        for num in numbers:
            name = name_map.get((team_norm, num), "")
            # name уже начинается с номера ("02.44.20 Название ...") — НЕ дублируем префикс.
            # Формат строки эталона: "<номер> <Название>" (как etalon_QG.txt).
            lines.append(name.strip())
        fpath.write_text("\n".join(lines) + "\n", encoding="utf-8")
        written.append((team_norm, fpath, len(numbers)))

    for team_norm, fpath, cnt in written:
        print(f"  [{team_norm}] {cnt} тестов -> {fpath}")
    print(f"СТАТУС: OK ({len(written)} команд)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
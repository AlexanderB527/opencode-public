#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fetch_results.py — скачивание базы результатов разбора из ветки qa-analysis-data.

Что тянет (AUTOTEST/sfa, ветка `qa-analysis-data`, каталог `results/`):
  results/<TEAM>/index.json      компакт: последние прогоны + топ причин — основной вход агента
  results/<TEAM>/causes.jsonl    агрегат причин (накопительный)
  results/<TEAM>/launches/*.json сырьё по прогонам — по флагу --last N (по умолчанию 0)

Чтение — через git (ветка-данные крошечная, sparse clone), поэтому персональный
токен не нужен: работают обычные git-креды оператора.

Кладёт в локальный кэш `<artifacts root>/results/<TEAM>/` (тот же стейджинг,
что использует publish_results.py). Офлайн-режим: без сети читает только кэш.

Использование:
  py -X utf8 fetch_results.py --team TX-T9                  # индекс + агрегат причин
  py -X utf8 fetch_results.py --team TX-T9 --last 3         # + 3 последних прогона
  py -X utf8 fetch_results.py --all                         # индексы всех команд в ветке
  py -X utf8 fetch_results.py --check                       # что лежит в кэше (без сети)

Коды возврата: 0 — ок; 1 — ошибка; 2 — нет доступа к репозиторию; 4 — ветки/данных нет.
"""
import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import git_data  # noqa: E402
from _paths import ARTIFACTS_ROOT, RESULTS_DIR  # noqa: E402

RESULTS_SOURCE_DEFAULTS = {
    "bitbucket_base_url": "https://example.invalid",
    "project": "AUTOTEST",
    "repo": "sfa",
    "branch": "qa-analysis-data",
    "path": "results",
}


class FetchError(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def write_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def fetch_team(src, cache, url, team, dest, last, timeout):
    """Скачивает индекс, агрегат причин и (опционально) последние N прогонов."""
    prefix = f"{src['path']}/{team}"
    index_payload = git_data.read_files(cache, url, src["branch"], src["path"],
                                        [f"{prefix}/index.json"], timeout).get(f"{prefix}/index.json")
    if index_payload is None:
        raise FetchError(f"в ветке нет {prefix}/index.json (команда ещё не публиковала разборы)", code=4)

    causes_payload = git_data.read_files(cache, url, src["branch"], src["path"],
                                         [f"{prefix}/causes.jsonl"], timeout).get(f"{prefix}/causes.jsonl")

    team_dir = Path(dest) / team
    write_bytes(team_dir / "index.json", index_payload)
    if causes_payload:
        write_bytes(team_dir / "causes.jsonl", causes_payload)

    try:
        index = json.loads(index_payload.decode("utf-8"))
    except json.JSONDecodeError as e:
        raise FetchError(f"index.json команды {team} не разбирается: {e}")

    launches_fetched = []
    if last > 0:
        rels = []
        for item in (index.get("launches") or [])[:last]:
            rel = str(item.get("file") or "")
            if rel:
                rels.append(f"{prefix}/{rel}")
        if rels:
            payloads = git_data.read_files(cache, url, src["branch"], src["path"], rels, timeout)
            for rel, payload in payloads.items():
                if payload is None:
                    continue
                local_rel = rel[len(prefix) + 1:]
                write_bytes(team_dir / local_rel, payload)
                launches_fetched.append(local_rel)

    return {
        "team": team,
        "index": str(team_dir / "index.json"),
        "causes": str(team_dir / "causes.jsonl") if causes_payload else None,
        "launches": launches_fetched,
        "totals": index.get("totals") or {},
        "causes_top": (index.get("causes_top") or [])[:5],
    }


def check_local(dest, as_json):
    dest = Path(dest)
    teams = []
    if dest.is_dir():
        for team_dir in sorted(p for p in dest.iterdir() if p.is_dir()):
            index_path = team_dir / "index.json"
            totals, causes_top = {}, []
            if index_path.exists():
                try:
                    index = json.loads(index_path.read_text(encoding="utf-8"))
                    totals = index.get("totals") or {}
                    causes_top = (index.get("causes_top") or [])[:5]
                except (OSError, json.JSONDecodeError):
                    pass
            teams.append({
                "team": team_dir.name,
                "has_index": index_path.exists(),
                "has_causes": (team_dir / "causes.jsonl").exists(),
                "launch_files": len(list((team_dir / "launches").glob("*.json")))
                if (team_dir / "launches").is_dir() else 0,
                "totals": totals,
                "causes_top": causes_top,
            })
    if as_json:
        print(json.dumps({"results_dir": str(dest), "teams": teams}, ensure_ascii=False, indent=2))
        return 0 if teams else 1
    print(f"Кэш результатов: {dest}")
    if not teams:
        print("[ОШИБКА] кэш пуст — запусти fetch_results.py --all")
        return 1
    for t in teams:
        print(f"  {t['team']:<8} прогонов {t['launch_files']:>2}, "
              f"причин активно {t['totals'].get('causes_active', '?')}, "
              f"известно {t['totals'].get('causes_known', '?')}")
        for c in t["causes_top"][:3]:
            print(f"      - {c.get('cluster')}: {c.get('occurrences')} вхожд. "
                  f"({c.get('owner') or '—'}), fix: {(c.get('fix') or '—')[:60]}")
    return 0


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    parser = argparse.ArgumentParser(
        description="Скачивание базы результатов разбора (ветка qa-analysis-data) в локальный кэш")
    parser.add_argument("--team", action="append", metavar="TEAM",
                        help="команда (можно несколько раз). По умолчанию — --all")
    parser.add_argument("--all", action="store_true", help="все команды, что есть в ветке")
    parser.add_argument("--last", type=int, default=0,
                        help="скачать N последних файлов прогонов (по умолчанию только индекс)")
    parser.add_argument("--check", action="store_true", help="показать локальный кэш, без сети")
    parser.add_argument("--list", action="store_true", help="список команд в ветке, без скачивания")
    parser.add_argument("--root", default=None, help="корень артефактов (~/.qa-artifacts)")
    parser.add_argument("--dest", default=None, help="папка кэша (по умолчанию <root>/results)")
    parser.add_argument("--branch", default=None)
    parser.add_argument("--project", default=None)
    parser.add_argument("--repo", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--path", default=None)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    root = Path(args.root).expanduser().resolve() if args.root else ARTIFACTS_ROOT
    dest = Path(args.dest).expanduser().resolve() if args.dest else (RESULTS_DIR if not args.root
                                                                    else root / "results")

    if args.check:
        return check_local(dest, args.json)

    try:
        if not git_data.git_available():
            raise FetchError("git не найден в PATH — без него базу не скачать", code=2)

        cfg = {}
        cfg_path = root / "config.json"
        if cfg_path.exists():
            try:
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                cfg = {}
        src = git_data.resolve_source(cfg, RESULTS_SOURCE_DEFAULTS, {
            "bitbucket_base_url": args.base_url, "project": args.project, "repo": args.repo,
            "branch": args.branch, "path": args.path,
        })
        url = git_data.repo_url(src["bitbucket_base_url"], src["project"], src["repo"])
        cache = git_data.cache_dir(root, src["project"], src["repo"], src["branch"])

        teams = [t.upper() for t in args.team] if args.team else []
        if args.all or not teams:
            teams = [t.upper() for t in git_data.list_dirs(cache, url, src["branch"], src["path"],
                                                           timeout=args.timeout)]
            if args.list:
                if args.json:
                    print(json.dumps({"branch": src["branch"], "teams": teams},
                                     ensure_ascii=False, indent=2))
                else:
                    print(f"Ветка {src['branch']} ({src['project']}/{src['repo']}), "
                          f"команды: {', '.join(teams) or '—'}")
                return 0
        if not teams:
            raise FetchError("в ветке нет ни одной команды с результатами", code=4)

        results = []
        for team in teams:
            try:
                results.append(fetch_team(src, cache, url, team, dest, args.last, args.timeout))
            except FetchError as e:
                results.append({"team": team, "error": str(e)})

        if args.json:
            print(json.dumps({"results_dir": str(dest), "teams": results},
                             ensure_ascii=False, indent=2))
            return 0

        print(f"Источник: {src['project']}/{src['repo']} @ {src['branch']} ({src['path']}/)")
        print(f"Кэш: {dest}")
        errors = 0
        for item in results:
            if item.get("error"):
                errors += 1
                print(f"  [--] {item['team']:<8} {item['error']}")
                continue
            print(f"  [OK] {item['team']:<8} прогонов {len(item['launches'])}, "
                  f"причин активно {item['totals'].get('causes_active', '?')}")
            for c in item["causes_top"][:3]:
                print(f"        - {c.get('cluster')}: {c.get('occurrences')} вхожд. "
                      f"({c.get('owner') or '—'}), fix: {(c.get('fix') or '—')[:60]}")
        if args.last == 0:
            print("[i] чтобы забрать файлы прогонов: --last 3")
        return 0 if errors < len(results) else 1
    except (FetchError, git_data.GitError) as e:
        print(f"ОШИБКА: {e}", file=sys.stderr)
        return getattr(e, "code", 1)


if __name__ == "__main__":
    sys.exit(main())

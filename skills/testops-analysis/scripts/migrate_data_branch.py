#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""migrate_data_branch.py — разовая миграция эталонов в единую data-ветку.

Консолидация веток данных скилла testops-analysis. Переносит эталоны из кодовой
ветки в техническую orphan-ветку, где уже лежит база результатов:

    Testops_checkerData/list_Test/<TEAM>/etalon_<HF|QG|RM>.txt
        -> qa-analysis-data/etalons/<TEAM>/etalon_<HF|QG|RM>.txt

`results/` в `qa-analysis-data` уже на месте и НЕ трогается.

Безопасность:
  - по умолчанию — dry-run: печатает план (что и куда перенесётся), ничего не пишет;
  - реальный перенос — только с `--push --yes` (двухфлаговое подтверждение);
  - push делается одним коммитом через git (git_data.push_files), результаты не задеваются.

После успешного переноса операторам нужно: `fetch_etalons.py --refresh` и
`fetch_results.py --all`, а `list_Test/` из кодовой ветки убирается отдельным PR.

Использование:
  py -X utf8 migrate_data_branch.py --dry-run          # план
  py -X utf8 migrate_data_branch.py --push --yes       # выполнить перенос
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import git_data  # noqa: E402
from _paths import artifacts_root  # noqa: E402

# Источник: КОДОВАЯ ветка, где эталоны лежали исторически (до миграции).
SRC_DEFAULTS = {
    "bitbucket_base_url": "https://example.invalid",
    "etalons_project": "AUTOTEST",
    "etalons_repo": "sfa",
    "etalons_branch": "Testops_checkerData",
    "etalons_path": "list_Test",
}

# Цель: единая техническая orphan-ветка (база результатов уже там).
DST_DEFAULTS = {
    "branch": "qa-analysis-data",
    "path": "etalons",
}

TYPE_RE = re.compile(r"^etalon_(HF|QG|RM)\.txt$", re.IGNORECASE)


def load_config(root):
    p = Path(root) / "config.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"WARN: config.json не читается ({p}): {e}")
        return {}


def enumerate_etalons(work: Path, src_path: str):
    """Возвращает [(team, filename, relpath)] для всех etalon_*.txt в источнике."""
    base = Path(work) / src_path
    out = []
    if not base.is_dir():
        return out
    for f in sorted(base.rglob("*.txt")):
        if not TYPE_RE.match(f.name):
            continue
        team = f.parent.relative_to(base).as_posix()  # подпапка = команда
        out.append((team, f.name, f))
    return out


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(description="Миграция эталонов в единую data-ветку")
    ap.add_argument("--root", default=None, help="Корень артефактов (по умолчанию ~/.qa-artifacts)")
    ap.add_argument("--dry-run", action="store_true", help="Только план (по умолчанию и так план)")
    ap.add_argument("--push", action="store_true", help="Выполнить перенос (нужен --yes)")
    ap.add_argument("--yes", action="store_true", help="Подтвердить перенос")
    ap.add_argument("--src-branch", default=None, help="Ветка-источник (по умолчанию Testops_checkerData)")
    ap.add_argument("--src-path", default=None, help="Каталог-источник (по умолчанию list_Test)")
    ap.add_argument("--dst-branch", default=None, help="Ветка-цель (по умолчанию qa-analysis-data)")
    ap.add_argument("--dst-path", default=None, help="Каталог-цель (по умолчанию etalons)")
    args = ap.parse_args()

    root = artifacts_root(args.root)
    cfg = load_config(root)

    # Источник — фиксированное ИСТОРИЧЕСКОЕ место (кодовая ветка). Переопределяется
    # только CLI (--src-branch/--src-path): config.json уже семантически указывает
    # на НОВОЕ место (qa-analysis-data/etalons) и для источника не годится.
    base_url = str(cfg.get("bitbucket_base_url") or SRC_DEFAULTS["bitbucket_base_url"]).rstrip("/")
    project = str(cfg.get("etalons_project") or SRC_DEFAULTS["etalons_project"])
    repo = str(cfg.get("etalons_repo") or SRC_DEFAULTS["etalons_repo"])
    src_branch = args.src_branch or SRC_DEFAULTS["etalons_branch"]
    src_path = (args.src_path or SRC_DEFAULTS["etalons_path"]).strip("/")
    dst_branch = args.dst_branch or cfg.get("results_branch") or DST_DEFAULTS["branch"]
    dst_path = (args.dst_path or DST_DEFAULTS["path"]).strip("/")

    if not git_data.git_available():
        print("FAIL: git не найден в PATH")
        return 1

    url = git_data.repo_url(base_url, project, repo)
    cache = git_data.cache_dir(root, project, repo, src_branch)

    print(f"Источник: {project}/{repo} @ {src_branch} / {src_path}/")
    print(f"Цель:     {dst_branch} / {dst_path}/  (results/ не трогаем)")

    try:
        work = git_data.ensure_cache(cache, url, src_branch, src_path)
    except git_data.GitError as e:
        print(f"FAIL: {e}")
        return e.code

    items = enumerate_etalons(work, src_path)
    if not items:
        print(f"FAIL: в источнике {src_path}/ нет файлов etalon_*.txt")
        return 1

    teams = sorted({t for t, _, _ in items})
    print(f"\nНайдено эталонов: {len(items)} (команд: {len(teams)})")
    for team, name, _ in items:
        print(f"  {team}/{name}  ->  {dst_path}/{team}/{name}")

    if args.dry_run or not (args.push and args.yes):
        print("\nСТАТУС: ПЛАН (перенос не выполнен). Для выполнения: --push --yes")
        return 0

    files = {
        f"{dst_path}/{team}/{name}": f.read_bytes()
        for team, name, f in items
    }
    message = "testops-analysis: перенос эталонов в единую data-ветку (etalons/)"
    try:
        sha = git_data.push_files(url, dst_branch, files, message)
    except git_data.GitError as e:
        print(f"FAIL: {e}")
        return e.code

    if sha == "no-changes":
        print("СТАТУС: OK — изменений нет (эталоны уже на месте)")
    else:
        print(f"СТАТУС: OK — перенесено {len(items)} эталонов, коммит {sha}")
        print(f"Дальше: убрать {src_path}/ из {src_branch} (отдельный PR),")
        print("        операторы: fetch_etalons.py --refresh  и  fetch_results.py --all")
    return 0


if __name__ == "__main__":
    sys.exit(main())

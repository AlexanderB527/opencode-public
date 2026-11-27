#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fetch_etalons.py — выгрузка эталонов команд из ветки qa-analysis-data.

Эталоны НЕ хранятся в репозитории скилла: источник истины — ветка
`qa-analysis-data` репозитория `AUTOTEST/sfa`, каталог `etalons/<TEAM>/etalon_<тип>.txt`.
Скрипт забирает их через git (partial + sparse clone, только каталог `etalons`)
и кладёт в локальный кэш оператора `~/.qa-artifacts/etalons/<TEAM>/etalon_<тип>.txt`
(корень — env `QA_ARTIFACTS_ROOT` или флаг `--root`). Скрипты скилла читают только кэш.

Почему git, а не REST: REST-запись в этом репозитории запрещена hook'ом, а чтение
partial-клоном стоит ~0.8 МБ на ветку (против 224 МБ полного клона) и не требует
персонального токена — работают обычные git-креды (Credential Manager / SSH / CI).

Настройка источника (env или `~/.qa-artifacts/config.json`):
  bitbucket_base_url    https://example.invalid
  etalons_project       AUTOTEST
  etalons_repo          sfa
  etalons_branch        qa-analysis-data
  etalons_path          etalons

Использование:
  py -X utf8 fetch_etalons.py                    # дозаполнить кэш (скачает отсутствующее)
  py -X utf8 fetch_etalons.py --refresh          # перекачать всё заново
  py -X utf8 fetch_etalons.py --team TX-T9 --type QG
  py -X utf8 fetch_etalons.py --check            # проверить кэш, БЕЗ сети
  py -X utf8 fetch_etalons.py --list             # что лежит в ветке, без скачивания
  py -X utf8 fetch_etalons.py --json             # машинный вывод

Коды возврата: 0 — кэш полон/обновлён; 1 — ошибка; 2 — нет доступа к репозиторию;
4 — ветка/каталог не найдены.
"""

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import git_data  # noqa: E402
from _paths import ARTIFACTS_ROOT, ETALONS_DIR  # noqa: E402

# Паттерн номера теста: 39.02.04.01.01.01 — до 6 компонент
NUMBER_RE = re.compile(r"^\s*(\d{2}(?:\.\d{2,3})+)")

SOURCE_DEFAULTS = {
    "bitbucket_base_url": "https://example.invalid",
    "etalons_project": "AUTOTEST",
    "etalons_repo": "sfa",
    "etalons_branch": "qa-analysis-data",
    "etalons_path": "etalons",
}

# Настройки, которые можно переопределить из config.json (env важнее файла)
CONFIG_KEYS = (
    "bitbucket_base_url",
    "etalons_project",
    "etalons_repo",
    "etalons_branch",
    "etalons_path",
)

TYPE_RE = re.compile(r"^etalon_(HF|QG|RM)\.txt$", re.IGNORECASE)
MARKER_NAME = ".source.json"


class FetchError(Exception):
    """Ошибка выгрузки эталонов; code — код возврата процесса."""

    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

def load_config(root):
    """Читает `<root>/config.json`, если он есть. Отсутствие файла — не ошибка."""
    cfg_path = Path(root) / "config.json"
    if not cfg_path.exists():
        return {}
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise FetchError(f"config.json не читается ({cfg_path}): {e}")


def resolve_source(args, cfg):
    """Собирает параметры источника: CLI > env QA_<KEY> > config.json > дефолт."""
    src = dict(SOURCE_DEFAULTS)
    for key in CONFIG_KEYS:
        if cfg.get(key):
            src[key] = str(cfg[key])
        env_value = os.environ.get("QA_" + key.upper())
        if env_value:
            src[key] = env_value
    overrides = {
        "bitbucket_base_url": args.base_url,
        "etalons_project": args.project,
        "etalons_repo": args.repo,
        "etalons_branch": args.branch,
        "etalons_path": args.path,
    }
    for key, value in overrides.items():
        if value:
            src[key] = value
    src["bitbucket_base_url"] = src["bitbucket_base_url"].rstrip("/")
    src["etalons_path"] = src["etalons_path"].strip("/")
    return src


def source_repo_url(src):
    return git_data.repo_url(src["bitbucket_base_url"], src["etalons_project"], src["etalons_repo"])


# ---------------------------------------------------------------------------
# Валидация и запись
# ---------------------------------------------------------------------------

def parse_numbers(data):
    """Номера тестов из байтов файла (UTF-8, при неудаче — cp1251)."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1251")
    numbers = []
    for line in text.splitlines():
        match = NUMBER_RE.match(line)
        if match:
            numbers.append(match.group(1))
    return numbers


def validate_etalon(data, team, test_type):
    """Санити-контроль выгруженного эталона. Возвращает (уникальных, дублей)."""
    if not data.strip():
        raise FetchError(f"{team}/etalon_{test_type}.txt пуст")
    numbers = parse_numbers(data)
    if not numbers:
        raise FetchError(
            f"{team}/etalon_{test_type}.txt: ни одного номера по паттерну "
            "NN.NN[.NNN], файл повреждён или это не эталон"
        )
    unique = len(set(numbers))
    return unique, len(numbers) - unique


def write_atomic(path, data):
    """Пишет байты через временный файл, чтобы не оставить половину эталона."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def write_sha256(path, data):
    """Рядом с эталоном кладём .sha256 — пломба «файл не правили после скачивания».

    scan_testops.py сверяет этот хеш и пишет его в манифест; dispatch/classify
    падают, если эталон в кэше разошёлся с пломбой.
    """
    digest = hashlib.sha256(data).hexdigest()
    side = path.with_suffix(path.suffix + ".sha256")
    side.write_text(digest + "\n", encoding="utf-8")
    return digest


def write_marker(src, commit, files):
    """Маркер источника: что и откуда скачано (для диагностики и --check)."""
    payload = {
        "source": src,
        "commit": commit,
        "files": files,
    }
    ETALONS_DIR.mkdir(parents=True, exist_ok=True)
    (ETALONS_DIR / MARKER_NAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def local_state():
    """Что уже лежит в кэше: {TEAM: [типы]} по файлам etalon_*.txt."""
    state = {}
    if not ETALONS_DIR.exists():
        return state
    for path in sorted(ETALONS_DIR.glob("*/etalon_*.txt")):
        match = TYPE_RE.match(path.name)
        if match:
            state.setdefault(path.parent.name, []).append(match.group(1).upper())
    return {team: sorted(types) for team, types in sorted(state.items())}


def read_marker():
    marker = ETALONS_DIR / MARKER_NAME
    if not marker.exists():
        return {}
    try:
        return json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


# ---------------------------------------------------------------------------
# Чтение ветки через git
# ---------------------------------------------------------------------------

def remote_state(src, args):
    """Листинг ветки: {TEAM: [типы]} — по рабочему дереву partial-клона."""
    cache = git_data.cache_dir(ARTIFACTS_ROOT, src["etalons_project"], src["etalons_repo"],
                               src["etalons_branch"])
    url = source_repo_url(src)
    teams = git_data.list_dirs(cache, url, src["etalons_branch"], src["etalons_path"],
                               timeout=args.timeout)
    if not teams:
        raise FetchError(
            f"в ветке {src['etalons_branch']} нет каталогов команд: {src['etalons_path']}/", code=4)
    result = {}
    for team in teams:
        team_dir = Path(cache) / src["etalons_path"] / team
        types = sorted({
            match.group(1).upper()
            for match in (TYPE_RE.match(p.name) for p in team_dir.iterdir() if p.is_file())
            if match
        })
        if types:
            result[team] = types
    return result, cache, url


def download(src, args, cache, url, targets, refresh):
    """Копирует эталоны из рабочего дерева клона в кэш оператора."""
    fetched, skipped, files_report = 0, 0, []
    for team, test_type in targets:
        dest = ETALONS_DIR / team / f"etalon_{test_type}.txt"
        if dest.exists() and not refresh:
            # Пломба могла не сохраниться от старой версии скрипта — восстанавливаем.
            side = dest.with_suffix(dest.suffix + ".sha256")
            if not side.exists():
                write_sha256(dest, dest.read_bytes())
            skipped += 1
            files_report.append({"team": team, "type": test_type, "path": str(dest),
                                 "numbers": None, "status": "skipped"})
            continue
        rel = f"{src['etalons_path']}/{team}/etalon_{test_type}.txt"
        payload = git_data.read_files(cache, url, src["etalons_branch"], src["etalons_path"],
                                      [rel], timeout=args.timeout).get(rel)
        if payload is None:
            raise FetchError(f"в ветке нет файла {rel}", code=4)
        unique, duplicates = validate_etalon(payload, team, test_type)
        write_atomic(dest, payload)
        digest = write_sha256(dest, payload)
        fetched += 1
        files_report.append({"team": team, "type": test_type, "path": str(dest),
                             "numbers": unique, "duplicates": duplicates,
                             "sha256": digest, "status": "fetched"})
        if duplicates:
            print(f"WARN: {team}/etalon_{test_type}.txt — дублей номеров: {duplicates}",
                  file=sys.stderr)
    return fetched, skipped, files_report


def select_targets(remote, args):
    """Какие пары (team, type) качать по аргументам CLI."""
    teams = [t.upper() for t in args.team] if args.team else sorted(remote)
    types = [t.upper() for t in args.type] if args.type else None
    targets, unknown = [], []
    for team in teams:
        if team not in remote:
            unknown.append(team)
            continue
        allowed = remote[team]
        for test_type in (types or allowed):
            if test_type in allowed:
                targets.append((team, test_type))
    if unknown:
        raise FetchError(
            f"в ветке нет эталонов для команд: {', '.join(unknown)} "
            f"(есть: {', '.join(sorted(remote))})", code=4)
    if not targets:
        raise FetchError("нечего скачивать: фильтры --team/--type ничего не выбрали")
    return targets


# ---------------------------------------------------------------------------
# Сценарии
# ---------------------------------------------------------------------------

def cmd_list(remote, src, as_json):
    if as_json:
        print(json.dumps({"source": src, "teams": remote}, ensure_ascii=False, indent=2))
        return 0
    print(f"Ветка {src['etalons_branch']} ({src['etalons_project']}/{src['etalons_repo']}), "
          f"каталог {src['etalons_path']}/:")
    for team in sorted(remote):
        print(f"  {team:<8} {', '.join(remote[team])}")
    print(f"  Итого: {len(remote)} команд, {sum(len(v) for v in remote.values())} файлов")
    return 0


def cmd_check(as_json):
    """Локальная проверка кэша: без сети. Неполный кэш → код 1."""
    state = local_state()
    marker = read_marker()
    problems = []
    if not state:
        problems.append(f"кэш пуст: {ETALONS_DIR} — запусти fetch_etalons.py")
    for team, types in state.items():
        for test_type in types:
            path = ETALONS_DIR / team / f"etalon_{test_type}.txt"
            try:
                unique, duplicates = validate_etalon(path.read_bytes(), team, test_type)
            except FetchError as e:
                problems.append(str(e))
                continue
            if unique == 0:
                problems.append(f"{team}/{test_type}: 0 номеров")
            if duplicates:
                problems.append(f"{team}/etalon_{test_type}.txt: дублей номеров — {duplicates}")
    if as_json:
        print(json.dumps({
            "etalons_dir": str(ETALONS_DIR),
            "teams": state,
            "files": sum(len(v) for v in state.values()),
            "source": marker.get("source", {}),
            "commit": marker.get("commit", ""),
            "problems": problems,
        }, ensure_ascii=False, indent=2))
    else:
        print(f"Кэш эталонов: {ETALONS_DIR}")
        print(f"  команд: {len(state)}, файлов: {sum(len(v) for v in state.values())}")
        if marker.get("commit"):
            print(f"  источник: {marker.get('source', {}).get('etalons_branch', '?')} "
                  f"@ {marker['commit'][:12]}")
        for problem in problems:
            print(f"[ОШИБКА] {problem}")
        if not problems:
            print("[OK] кэш полон и валиден")
    return 0 if not problems else 1


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    parser = argparse.ArgumentParser(
        description="Скачивание эталонов команд из ветки qa-analysis-data (AUTOTEST/sfa) в локальный кэш."
    )
    parser.add_argument("--team", action="append", metavar="TEAM",
                        help="команда (можно несколько раз): TX-T9, BIT, ...")
    parser.add_argument("--type", action="append", choices=["HF", "QG", "RM", "hf", "qg", "rm"],
                        help="тип эталона (можно несколько раз)")
    parser.add_argument("--refresh", action="store_true", help="перекачать файлы, даже если они есть в кэше")
    parser.add_argument("--check", action="store_true", help="только проверить локальный кэш (без сети)")
    parser.add_argument("--list", action="store_true", help="показать содержимое ветки (без скачивания)")
    parser.add_argument("--json", action="store_true", help="машинный вывод")
    parser.add_argument("--root", help="корень артефактов (по умолчанию env QA_ARTIFACTS_ROOT или ~/.qa-artifacts)")
    parser.add_argument("--dest", help="папка кэша эталонов (по умолчанию <root>/etalons)")
    parser.add_argument("--base-url", help="Bitbucket base URL")
    parser.add_argument("--project", help="project key (по умолчанию AUTOTEST)")
    parser.add_argument("--repo", help="repository slug (по умолчанию sfa)")
    parser.add_argument("--branch", help="ветка с эталонами (по умолчанию qa-analysis-data)")
    parser.add_argument("--path", help="каталог эталонов в ветке (по умолчанию etalons)")
    parser.add_argument("--timeout", type=int, default=300, help="таймаут git-операции, сек")
    args = parser.parse_args(argv)

    global ETALONS_DIR  # noqa: PLW0603 — путь кэша переопределяется флагами
    if args.dest:
        ETALONS_DIR = Path(args.dest).expanduser().resolve()
    elif args.root:
        ETALONS_DIR = Path(args.root).expanduser().resolve() / "etalons"

    try:
        if args.check:
            return cmd_check(args.json)

        if not git_data.git_available():
            raise FetchError("git не найден в PATH — без него эталоны не скачать", code=2)

        root = Path(args.root).expanduser().resolve() if args.root else ARTIFACTS_ROOT
        cfg = load_config(root)
        src = resolve_source(args, cfg)

        remote, cache, url = remote_state(src, args)
        if args.list:
            return cmd_list(remote, src, args.json)

        targets = select_targets(remote, args)
        fetched, skipped, files_report = download(src, args, cache, url, targets, args.refresh)

        commit = git_data.head_commit(cache, args.timeout)
        write_marker(src, commit, files_report)

        total_numbers = sum(f["numbers"] or 0 for f in files_report)
        if args.json:
            print(json.dumps({
                "etalons_dir": str(ETALONS_DIR),
                "source": src,
                "commit": commit,
                "fetched": fetched,
                "skipped": skipped,
                "files": files_report,
            }, ensure_ascii=False, indent=2))
            return 0

        print(f"Источник: {src['etalons_project']}/{src['etalons_repo']} @ {src['etalons_branch']}"
              f" ({commit})")
        print(f"Кэш: {ETALONS_DIR}")
        for item in files_report:
            if item["status"] == "fetched":
                print(f"  [OK]   {item['team']:<8} {item['type']}  {item['numbers']} номеров -> {item['path']}")
            else:
                print(f"  [skip] {item['team']:<8} {item['type']}  уже в кэше")
        print(f"Скачано: {fetched}, пропущено: {skipped}, номеров в скачанных: {total_numbers}")
        if skipped and not args.refresh:
            print("[i] обновить существующие файлы: --refresh")
        return 0
    except FetchError as e:
        print(f"ОШИБКА: {e}", file=sys.stderr)
        return e.code
    except git_data.GitError as e:
        print(f"ОШИБКА git: {e}", file=sys.stderr)
        return e.code


if __name__ == "__main__":
    sys.exit(main())

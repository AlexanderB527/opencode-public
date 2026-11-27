#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dispatch_team_reruns.py — fan-out перезапуска упавших тестов по КОМАНДНЫМ Jenkins-джобам.

Читает снимки скана `launches/{launch_id}/{team}/expected_set.json`, для каждой команды
собирает @-список не-PASS номеров (естественная сортировка, разделитель — ПРОБЕЛ)
и запускает соответствующую джобу `DEBUG/<TEAM>` через `buildWithParameters`.
Итог раздачи пишется в `launches/{launch_id}/dispatch.json`.

Имя папки артефактов и канонический ключ реестра могут не совпадать:
- алиасы из реестра (`aliases`, например `T9-TX` -> `TX-T9`) — регистронезависимо;
- кириллические гомоглифы в имени папки (`TАМUL` с русской А) нормализуются
  ТОЛЬКО для разрешения команды — сама папка не переименовывается.
Если на одну команду претендуют две и более папки — статус `duplicate_scan`,
джоба не запускается (молчаливое слияние запрещено).

Только stdlib. Креды: CLI (`--jenkins-url/--user/--token`) → env
(`JENKINS_URL`/`JENKINS_USERNAME`/`JENKINS_PASSWORD`) → `mcp.jenkins.environment`
в `opencode.json`.

Коды возврата: 0 — всё ок (включая skip/no_scan/disabled),
1 — есть error/unmapped/duplicate_scan либо нет реестра,
2 — не найдено ни одного expected_set.json.
"""

import argparse
import base64
import hashlib
import json
import os
import re
import ssl
import sys
from datetime import datetime
from pathlib import Path
from urllib import error, parse, request

# Скрипты тулкита лежат в одной папке и импортируют друг друга — путь добавляем сами
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import TEAM_JOBS_PATH, artifacts_root, dispatch_path  # noqa: E402

# natural_key/with_at живут в classifier.py (общий классификатор). Раньше брались
# из publish_confluence.py — он удалён вместе с сетевым публикатором: публикация
# идёт через render_markdown.py + MCP atlassian.
from classifier import natural_key, with_at  # noqa: E402
import team_canon  # noqa: E402 — единый нормализатор имён команд

DEFAULT_OPENCODE_JSON = Path.home() / ".config" / "opencode" / "opencode.json"
DEFAULT_TIMEOUT = 30

# Статусы, которые не являются падением (в @-список не попадают)
PASS_STATUSES = ("passed", "skipped", "pass", "skip")

# «Уже разослано»: и первичный запуск, и ранее зафиксированный повтор,
# и успешная раздача объединённых папок (merged_scan)
ALREADY_STATUSES = ("triggered", "already_dispatched", "merged_scan")

# Статусы-проблемы: из-за них код возврата 1
PROBLEM_STATUSES = ("error", "unmapped", "duplicate_scan")

# Значения enabled, которые означают «команда выключена»
DISABLED_VALUES = ("false", "0", "no", "off")


def _log_err(text):
    """Служебные сообщения — только в stderr, чтобы не засорять компактный stdout."""
    print(text, file=sys.stderr)


# --------------------------------------------------------------- реестр и разрешение

def load_registry(path):
    """Читает реестр командных джоб; служебные ключи (`_note`) пропускаются."""
    p = Path(path).expanduser()
    if not p.exists():
        raise SystemExit(
            f"ОШИБКА: реестр командных джоб не найден: {p}\n"
            "Ожидается версионируемый файл config/team_jobs.json "
            "(ключ = канон команды, значение = {job, env, parallel, rerun, enabled, aliases})."
        )
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as e:
        raise SystemExit(f"ОШИБКА: не удалось прочитать реестр {p}: {e}")
    if not isinstance(data, dict):
        raise SystemExit(f"ОШИБКА: реестр {p} должен быть JSON-объектом")
    return {k: v for k, v in data.items() if not str(k).startswith("_")}


def _norm(name):
    """Нормализация имени для сравнения — через единый модуль team_canon."""
    return team_canon.normalize(name)


def build_resolver(registry):
    """Возвращает функцию resolve(folder) -> канонический ключ реестра или None.

    Вся логика (точный ключ → алиас реестра → нормализация транслитом →
    встроенные алиасы) живёт в team_canon.resolve_canon, чтобы скан и раздача
    трактовали имена команд одинаково.
    """
    def resolve(folder):
        return team_canon.resolve_canon(folder, registry)

    return resolve


def is_enabled(conf):
    """Терпимая проверка enabled: false/'false'/0/'0'/no/off → выключено."""
    value = (conf or {}).get("enabled", True)
    if value is None:
        return True
    if value is False:
        return False
    return str(value).strip().lower() not in DISABLED_VALUES


# --------------------------------------------------------------- данные скана

def find_team_dirs(launch_dir):
    """Подпапки запуска, в которых есть снимок скана expected_set.json."""
    if not launch_dir.is_dir():
        return []
    return sorted(
        (d for d in launch_dir.iterdir() if d.is_dir() and (d / "expected_set.json").is_file()),
        key=lambda d: d.name.upper(),
    )


def read_nonpass(expected_path):
    """Возвращает (список не-PASS номеров без '@', общее число тестов в снимке).

    🔴 Незаупавшие и упавшие тесты команды ВНЕ эталона (`new_tests`) тоже учитываются:
    если тест упал в прогоне, его надо перезапустить независимо от того, внесён он
    в эталон или нет. Раньше такие номера молча выбрасывались (реальный кейс:
    39.02.03.07 упал с Net::ReadTimeout и не попал в перезапуск).
    """
    # utf-8-sig — терпим BOM (файл могли поправить руками в Блокноте)
    data = json.loads(Path(expected_path).read_text(encoding="utf-8-sig"))
    numbers = []
    seen = set()
    for num, meta in (data.get("statuses") or {}).items():
        status = str((meta or {}).get("status", "")).strip().lower()
        if status in PASS_STATUSES:
            continue
        num = str(num)
        if num not in seen:
            seen.add(num)
            numbers.append(num)

    # Тесты команды вне эталона: в @-список только если реально не PASS.
    extra_new = []
    for item in data.get("new_tests") or []:
        if not isinstance(item, dict):
            continue
        num = str(item.get("number") or "").strip()
        if not num or num in seen:
            continue
        statuses = [str(x).strip().lower() for x in (item.get("statuses") or [])]
        if statuses and all(s in PASS_STATUSES for s in statuses):
            continue
        seen.add(num)
        numbers.append(num)
        extra_new.append(num)

    counts = data.get("counts") or {}
    try:
        total = int(counts.get("total", 0) or 0)
    except (TypeError, ValueError):
        total = 0
    if extra_new:
        _log_err(
            f"{Path(expected_path).parent.name}: в перезапуск добавлены не-PASS тесты "
            f"вне эталона ({len(extra_new)}): {', '.join(extra_new)}"
        )
    return numbers, total


def _clean_number(number):
    """Чистит номер теста: запятая ломает rake-вызов, пробелы внутри — тоже."""
    text = re.sub(r"[,，]", "", str(number))
    return "".join(text.split())


class ScanIntegrityError(RuntimeError):
    """Снимок скана не прошёл проверку целостности — раздавать по нему нельзя."""


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_scan(folder_dir, include_missing=False):
    """Читает не-PASS снимок папки команды, предварительно проверив его подлинность.

    Гейты (fail-loud, без «мягких» режимов):
      1. scan_manifest.json есть         — иначе скан не проводился;
      2. complete == true                — иначе хвост запуска мог потеряться;
      3. tail_mismatch != true           — расхождение в хвосте;
      4. expected_set_sha256 совпадает   — иначе снимок правили после скана.

    include_missing=True — к не-PASS добавляются номера эталона, которые в прогоне
    вообще не запускались (`etalon_missing_numbers` из манифеста). Нужно для задачи
    «прогнать/перезапустить ВСЕ тесты команды», а не только упавшие.

    Возврат: (список не-PASS номеров, общее число тестов в снимке).
    """
    folder_dir = Path(folder_dir)
    expected_file = folder_dir / "expected_set.json"
    manifest_file = folder_dir / "scan_manifest.json"

    if not manifest_file.is_file():
        raise ScanIntegrityError(
            f"{folder_dir.name}: нет scan_manifest.json — скан не проводился "
            f"(или сделан старой версией скрипта). Пересканировать: "
            f"scan_testops.py --launch-id <id> --teams <...> --etalon <...> --tail-check")
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as e:
        raise ScanIntegrityError(f"{folder_dir.name}: не читается scan_manifest.json: {e}")

    if manifest.get("complete") is not True:
        raise ScanIntegrityError(
            f"{folder_dir.name}: скан неполный (complete={manifest.get('complete')}) — "
            f"прочитано {manifest.get('rows_scanned')} из {manifest.get('total')}. "
            f"Пересканировать, раздача по неполному скану запрещена.")
    if manifest.get("tail_mismatch") is True:
        raise ScanIntegrityError(
            f"{folder_dir.name}: расхождение в хвосте запуска (tail_mismatch=true) — "
            f"не-PASS результаты могли потеряться. Пересканировать.")

    pinned = manifest.get("expected_set_sha256")
    if not pinned:
        raise ScanIntegrityError(
            f"{folder_dir.name}: в манифесте нет expected_set_sha256 "
            f"(скан сделан старой версией скрипта). Пересканировать.")
    if not expected_file.is_file():
        raise ScanIntegrityError(f"{folder_dir.name}: нет expected_set.json")
    actual = _sha256_file(expected_file)
    if actual != pinned:
        raise ScanIntegrityError(
            f"{folder_dir.name}: expected_set.json не совпадает с манифестом "
            f"(ожидалось {pinned[:12]}…, сейчас {actual[:12]}…) — снимок правили вручную. "
            f"Пересканировать.")

    numbers, total = read_nonpass(expected_file)
    if include_missing:
        missing = manifest.get("etalon_missing_numbers") or []
        if missing:
            seen = {_clean_number(n) for n in numbers}
            extra = [str(n) for n in missing if _clean_number(n) not in seen]
            if extra:
                _log_err(
                    f"{folder_dir.name}: добавлено не запускавшихся в прогоне "
                    f"({len(extra)}): {', '.join(extra[:20])}"
                    + (" …" if len(extra) > 20 else "")
                )
            numbers.extend(extra)
    return numbers, total


def build_tags(numbers):
    """@-список для параметра tags: дедуп, естественная сортировка, разделитель — ПРОБЕЛ."""
    unique = {_clean_number(n) for n in numbers if _clean_number(n)}
    return " ".join(with_at(n) for n in sorted(unique, key=natural_key))


# --------------------------------------------------------------- креды и Jenkins

def validate_team_names(names):
    """Проверяет имена команд из --teams: без разделителей пути и без '..'."""
    clean = []
    for raw in names:
        name = str(raw).strip()
        if not name:
            raise SystemExit("ОШИБКА: пустое имя команды в --teams")
        if any(ch in name for ch in ("/", "\\")) or ".." in name:
            raise SystemExit(
                f"ОШИБКА: недопустимое имя команды '{name}' в --teams "
                "(запрещены '/', '\\' и '..')"
            )
        clean.append(name)
    return clean


def _jenkins_env_from(data):
    """Достаёт environment MCP-сервера jenkins из уже распарсенного opencode.json.

    Поддерживаются обе раскладки конфига:
      - плоская:   mcp.jenkins.environment
      - словарная: mcp.servers.jenkins.environment  (актуальная схема opencode)
    """
    mcp = data.get("mcp") or {}
    for node in (mcp, mcp.get("servers") or {}):
        block = (node or {}).get("jenkins") or {}
        env = block.get("environment") if isinstance(block, dict) else None
        if isinstance(env, dict) and env:
            return env
    return {}


def _load_opencode_env(path):
    """Достаёт environment MCP-сервера jenkins из opencode.json (отсутствие файла — не ошибка)."""
    p = Path(path).expanduser()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return _jenkins_env_from(data)


def _truthy(value, default=True):
    """Разбор JENKINS_VERIFY_SSL: 'false'/'0'/'no' → False."""
    if value is None or value == "":
        return default
    return str(value).strip().lower() not in ("false", "0", "no", "off")


def load_credentials(args):
    """Приоритет кредов: CLI → env → mcp.jenkins.environment в opencode.json."""
    oc = _load_opencode_env(args.opencode_json)

    url = args.jenkins_url or os.environ.get("JENKINS_URL") or oc.get("JENKINS_URL") or ""
    user = args.user or os.environ.get("JENKINS_USERNAME") or oc.get("JENKINS_USERNAME") or ""
    token = (
        args.token
        or os.environ.get("JENKINS_PASSWORD")
        or oc.get("JENKINS_PASSWORD")
        or ""
    )
    verify_raw = os.environ.get("JENKINS_VERIFY_SSL", oc.get("JENKINS_VERIFY_SSL"))
    return {
        "url": url.rstrip("/"),
        "user": user,
        "token": token,
        "verify": _truthy(verify_raw),
    }


def _ssl_context(verify):
    """Проверка сертификата отключается для внутреннего self-signed Jenkins."""
    if verify:
        return ssl.create_default_context()
    return ssl._create_unverified_context()


def _auth_header(user, token):
    raw = f"{user}:{token}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def job_base_url(base, job):
    """`DEBUG/BIT` → `{base}/job/DEBUG/job/BIT`."""
    segments = [s for s in str(job).split("/") if s]
    path = "".join(f"/job/{parse.quote(s, safe='')}" for s in segments)
    return base.rstrip("/") + path


def trigger_job(creds, job, params, timeout):
    """POST buildWithParameters. Возвращает (ok, queue_url, error_text)."""
    url = job_base_url(creds["url"], job) + "/buildWithParameters"
    body = parse.urlencode(params).encode("utf-8")
    req = request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": _auth_header(creds["user"], creds["token"]),
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    try:
        with request.urlopen(req, context=_ssl_context(creds["verify"]), timeout=timeout) as resp:
            if resp.status in (200, 201):
                return True, resp.headers.get("Location"), None
            return False, None, f"HTTP {resp.status}"
    except error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="ignore")[:300]
        return False, None, f"HTTP {e.code}: {detail}".strip()
    except (error.URLError, OSError) as e:
        return False, None, f"сетевая ошибка: {e}"


def resolve_build_url(creds, queue_url, timeout):
    """Пробует превратить queue_url в build_url. Любая неудача → None (не критично)."""
    m = re.search(r"/queue/item/(\d+)", queue_url or "")
    if not m:
        return None
    api_url = f'{creds["url"]}/queue/item/{m.group(1)}/api/json'
    req = request.Request(
        api_url,
        headers={
            "Authorization": _auth_header(creds["user"], creds["token"]),
            "Accept": "application/json",
        },
    )
    try:
        with request.urlopen(req, context=_ssl_context(creds["verify"]), timeout=min(timeout, 10)) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (error.URLError, OSError, json.JSONDecodeError, ValueError):
        return None
    executable = data.get("executable") or {}
    build_url = executable.get("url")
    return str(build_url) if build_url else None


# --------------------------------------------------------------- вывод и main

def _label(entry):
    """Метка команды для сводки: канон, а при расхождении — с фактическими папками."""
    team = entry.get("team", "")
    folders = entry.get("folders") or []
    if len(folders) > 1:
        return f"{team}({', '.join(folders)})"
    folder = entry.get("folder") or ""
    if folder and folder != team:
        return f"{team}({folder})"
    return team


def _fmt_line(entry):
    """Одна строка компактной сводки: команда, число тестов, статус, ссылка."""
    link = entry.get("build_url") or entry.get("queue_url") or entry.get("error") or ""
    tag = " [carried_over]" if entry.get("carried_over") else ""
    return (
        f"{_label(entry):<14} "
        f"count={entry.get('count', 0):<4} "
        f"status={entry.get('status', ''):<18} "
        f"{link}{tag}"
    ).rstrip()


def _new_entry(team, folder=None):
    """Пустая запись раздачи команды."""
    folder = folder if folder is not None else team
    return {
        "team": team,
        "folder": folder,
        "folders": [folder],
        "job": None,
        "count": 0,
        "tags": "",
        "status": "",
        "queue_url": None,
        "build_url": None,
        "error": None,
    }


def _select_folders(launch_dir, requested, resolve):
    """Отбирает папки под запрошенные команды; возвращает (папки, команды без скана)."""
    folders = find_team_dirs(launch_dir)
    if not requested:
        return [d.name for d in folders], []

    chosen, missing = [], []
    for name in requested:
        key = _norm(name)
        canon_wanted = resolve(name)
        matched = []
        for d in folders:
            canon = resolve(d.name)
            if _norm(d.name) == key or (canon_wanted and canon == canon_wanted):
                matched.append(d.name)
        if matched:
            chosen.extend(m for m in matched if m not in chosen)
        else:
            missing.append(name)
    return chosen, missing


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Fan-out перезапуска упавших тестов по командным Jenkins-джобам"
    )
    parser.add_argument("--launch-id", type=int, required=True, help="ID запуска TestOps")
    parser.add_argument("--root", default=None, help="Корень артефактов (по умолчанию ~/.qa-artifacts)")
    parser.add_argument("--teams", default=None,
                        help="Список команд через запятую (по умолчанию — все подпапки со снимком)")
    parser.add_argument("--registry", default=str(TEAM_JOBS_PATH),
                        help="Путь к team_jobs.json")
    parser.add_argument("--env", default=None, help="Переопределить env из реестра")
    parser.add_argument("--parallel", default=None, help="Переопределить parallel из реестра")
    parser.add_argument("--rerun", default=None, help="Переопределить rerun из реестра")
    parser.add_argument("--dry-run", action="store_true",
                        help="Только план: креды не нужны, сеть и запись не выполняются")
    parser.add_argument("--force", action="store_true",
                        help="Перезапускать даже поверх уже записанного dispatch.json")
    parser.add_argument("--include-missing", action="store_true",
                        help="Добавить к @-списку номера эталона, которые в прогоне НЕ запускались "
                             "(etalon_missing_numbers). Для задачи «прогнать все тесты команды», "
                             "а не только упавшие")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Таймаут HTTP, сек")
    parser.add_argument("--jenkins-url", default=None, help="URL Jenkins (приоритет над env)")
    parser.add_argument("--user", default=None, help="Jenkins user (приоритет над env)")
    parser.add_argument("--token", default=None, help="Jenkins API-токен (приоритет над env)")
    parser.add_argument("--opencode-json", default=str(DEFAULT_OPENCODE_JSON),
                        help="Путь к opencode.json (fallback для кредов)")
    args = parser.parse_args(argv)

    registry = load_registry(args.registry)
    resolve = build_resolver(registry)

    requested = validate_team_names(args.teams.split(",")) if args.teams else []

    root = artifacts_root(args.root)
    launch_dir = root / "launches" / str(args.launch_id)

    folder_names, missing = _select_folders(launch_dir, requested, resolve)
    if not folder_names:
        _log_err(f"ОШИБКА: в {launch_dir} нет ни одного expected_set.json")
        return 2

    # Разрешение команды по каждой папке и группировка: одна команда может «прийти»
    # из нескольких папок (слаг `T9-TX`, кириллический гомоглиф `TАMUL` и т.п.).
    resolved = {name: resolve(name) for name in folder_names}
    ordered = []       # ("canon", canon) | ("none", folder) — в порядке появления
    canon_folders = {}  # canon -> [папки]
    for name in folder_names:
        canon = resolved[name]
        if canon:
            if canon not in canon_folders:
                canon_folders[canon] = []
                ordered.append(("canon", canon))
            canon_folders[canon].append(name)
        else:
            ordered.append(("none", name))

    # Креды нужны только для реального запуска
    creds = None if args.dry_run else load_credentials(args)
    if creds is not None and (not creds["url"] or not creds["user"] or not creds["token"]):
        _log_err(
            "ОШИБКА: не заданы креды Jenkins. Укажи --jenkins-url/--user/--token, "
            "либо env JENKINS_URL/JENKINS_USERNAME/JENKINS_PASSWORD, "
            "либо mcp.jenkins.environment в opencode.json."
        )
        return 1

    # Прежняя раздача — для идемпотентности и переноса истории
    out_path = dispatch_path(args.launch_id, args.root)
    previous = {}
    if out_path.exists() and not args.force:
        try:
            prev_data = json.loads(out_path.read_text(encoding="utf-8-sig"))
            previous = {str(e.get("team")): e for e in (prev_data.get("teams") or [])}
        except (OSError, json.JSONDecodeError):
            previous = {}

    entries = []
    teams_triggered = teams_skipped_empty = teams_no_scan = 0
    teams_unmapped = teams_error = teams_disabled = teams_merged = 0
    tests_total = tests_dispatched = 0

    for kind, value in ordered:
        # --- папка, для которой команда не разрешилась: unmapped / no_scan ---
        if kind == "none":
            folder = value
            entry = _new_entry(folder, folder)
            entry["folders"] = [folder]
            expected_file = launch_dir / folder / "expected_set.json"
            if not expected_file.is_file():
                entry["status"] = "no_scan"
                teams_no_scan += 1
                entries.append(entry)
                continue
            try:
                numbers, total = load_scan(launch_dir / folder, include_missing=args.include_missing)
            except (ScanIntegrityError, OSError, json.JSONDecodeError) as e:
                entry["status"] = "error"
                entry["error"] = str(e)
                teams_error += 1
                entries.append(entry)
                continue
            tags = build_tags(numbers)
            entry["tags"] = tags
            entry["count"] = len(tags.split()) if tags else 0
            tests_total += total
            entry["status"] = "unmapped"
            entry["error"] = f"команда '{folder}' отсутствует в реестре {args.registry}"
            teams_unmapped += 1
            entries.append(entry)
            continue

        # --- группа папок одной канонической команды ---
        canon = value
        folders = canon_folders[canon]
        conf = registry.get(canon) or {}
        entry = _new_entry(canon, folders[0])
        entry["folders"] = list(folders)

        numbers_union = set()
        total_sum = 0
        read_ok = 0
        read_error = None
        for folder in folders:
            expected_file = launch_dir / folder / "expected_set.json"
            if not expected_file.is_file():
                continue  # папка без снимка просто не участвует
            try:
                nums, total = load_scan(launch_dir / folder, include_missing=args.include_missing)
            except (ScanIntegrityError, OSError, json.JSONDecodeError) as e:
                read_error = str(e)
                break
            numbers_union.update(nums)
            total_sum += total
            read_ok += 1

        if read_error:
            entry["status"] = "error"
            entry["error"] = read_error
            teams_error += 1
            entries.append(entry)
            continue
        if not read_ok:
            entry["status"] = "no_scan"
            teams_no_scan += 1
            entries.append(entry)
            continue

        tests_total += total_sum
        # Не-PASS по объединению: если в одной папке тест PASS, а в другой FAIL —
        # номер остаётся в union (приоритет у падения).
        tags = build_tags(numbers_union)
        entry["tags"] = tags
        entry["count"] = len(tags.split()) if tags else 0

        job = conf.get("job")
        if not job:
            # Джобу по аналогии НЕ придумываем
            entry["status"] = "unmapped"
            entry["error"] = f"в реестре не задана джоба для команды {canon}"
            teams_unmapped += 1
            entries.append(entry)
            continue
        entry["job"] = job

        if not is_enabled(conf):
            entry["status"] = "disabled"
            teams_disabled += 1
            entries.append(entry)
            continue

        # Несколько папок на одну команду: по умолчанию сливаем (union), но только
        # если в реестре разрешено; иначе fail-loud duplicate_scan.
        merged = len(folders) > 1
        if merged and not bool(conf.get("merge_folders", False)):
            entry["status"] = "duplicate_scan"
            entry["error"] = (
                f"несколько папок претендуют на команду {canon}: "
                + ", ".join(folders)
                + " — данные противоречивы, джоба не запускалась (merge_folders=false)"
            )
            entries.append(entry)
            continue

        if not tags:
            entry["status"] = "skipped_empty"
            teams_skipped_empty += 1
            entries.append(entry)
            continue

        params = {
            "tags": tags,  # 🔴 разделитель — ПРОБЕЛ, пустая строка недопустима
            "env": args.env or conf.get("env") or "qa",
            "parallel": args.parallel or conf.get("parallel") or "15",
            "rerun": args.rerun or conf.get("rerun") or "1",
        }
        for optional in ("browser", "browserVersion", "branch"):
            if conf.get(optional):
                params[optional] = conf[optional]

        # Статус успешного запуска: для объединённых папок — merged_scan
        ok_status = "merged_scan" if merged else "triggered"

        prev = previous.get(canon) or {}
        if prev.get("status") in ALREADY_STATUSES and (prev.get("tags") or "") == tags:
            # Идемпотентность: тот же набор тестов уже был разослан (любой из прошлых прогонов)
            entry["status"] = "already_dispatched"
            entry["queue_url"] = prev.get("queue_url")
            entry["build_url"] = prev.get("build_url")
            teams_triggered += 1
            tests_dispatched += entry["count"]
            _log_err(f"WARN: {canon} уже разослан с тем же tags — повтор не выполнялся")
            entries.append(entry)
            continue

        if args.dry_run:
            # В плане объединённая раздача тоже помечается merged_scan, обычная — would_trigger
            entry["status"] = "merged_scan" if merged else "would_trigger"
            teams_triggered += 1
            tests_dispatched += entry["count"]
            if merged:
                teams_merged += 1
            entries.append(entry)
            continue

        ok, queue_url, err = trigger_job(creds, job, params, args.timeout)
        if ok:
            entry["status"] = ok_status
            entry["queue_url"] = queue_url
            entry["build_url"] = resolve_build_url(creds, queue_url, args.timeout) if queue_url else None
            teams_triggered += 1
            tests_dispatched += entry["count"]
            if merged:
                teams_merged += 1
        else:
            entry["status"] = "error"
            entry["error"] = err
            teams_error += 1
        entries.append(entry)

    # Команды, запрошенные через --teams, но без снимка скана
    for name in missing:
        entry = _new_entry(resolve(name) or name, name)
        entry["status"] = "no_scan"
        teams_no_scan += 1
        entries.append(entry)

    teams_total = len(entries)

    # N1: история не теряется — записи прошлых команд переносятся в новый dispatch.json
    current = {str(e.get("team")) for e in entries}
    carried = 0
    for team, prev in previous.items():
        if team in current:
            continue
        item = dict(prev)
        item["carried_over"] = True
        entries.append(item)
        carried += 1

    summary = {
        "teams_total": teams_total,
        "teams_triggered": teams_triggered,
        "teams_skipped_empty": teams_skipped_empty,
        "teams_no_scan": teams_no_scan,
        "teams_unmapped": teams_unmapped,
        "teams_error": teams_error,
        "teams_disabled": teams_disabled,
        "teams_merged": teams_merged,
        "tests_total": tests_total,
        "tests_dispatched": tests_dispatched,
    }

    # Сводка в stdout — только компактные строки, без сырого JSON
    prefix = "DRY " if args.dry_run else ""
    for entry in entries:
        print(prefix + _fmt_line(entry))
    print(f"Итого: {summary}")
    if carried:
        _log_err(f"INFO: {carried} записей прошлых команд перенесено (carried_over)")

    if args.dry_run:
        print(f"dispatch.json НЕ записан (dry-run): {out_path}")
    else:
        payload = {
            "launch_id": args.launch_id,
            "dispatch_at": datetime.now().isoformat(timespec="seconds"),
            "env": args.env or "qa",
            "parallel": args.parallel or "15",
            "rerun": args.rerun or "1",
            "summary": summary,
            "teams": entries,
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"dispatch.json: {out_path}")

    if any(e.get("status") in PROBLEM_STATUSES for e in entries if not e.get("carried_over")):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

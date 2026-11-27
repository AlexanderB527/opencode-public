#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scan_testops.py — детерминированный скан запуска TestOps.

Задача скрипта: вынести из LLM всю механику пагинации и фильтрации.
Делает сам:
  1. Пагинацию по launch (size=100, до total_results, гейт полноты rows_scanned == total).
  2. Двойной фильтр: Team ∈ teams (после нормализации) И номер ∈ etalon.
  3. Сохраняет ТОЛЬКО не-PASS результаты в .qa-artifacts/launches/{launch_id}/nonpass_results.jsonl
  4. Пишет манифест .qa-artifacts/launches/{launch_id}/scan_manifest.json
В stdout — только финальная статистика и пути. Exit code: 0 = ok, 1 = ошибка/неполный скан.

Реализовано под реальный API TestOps (контракт верифицирован эмпирически):
  - Авторизация: POST /api/uaa/oauth/token (grant_type=apitoken) -> JWT, далее Bearer <jwt>;
    при 401 JWT переполучается и запрос повторяется 1 раз.
  - Пагинация: /api/v2/launch/{launchId}/test-result/flat (Spring Page, totalElements).
  - Команда: custom field 'Team' тест-кейса (/api/testcase/{id}/cfv), кэш по testCaseId.
  - Сообщение об ошибке: top-level поле message в /api/testresult/{resultId}.
Контракты файлов и фильтрацию не менять.

Использование:
  python scan_testops.py --launch-id 12345 --teams "T9,TX" --etalon "%USERPROFILE%/.qa-artifacts/etalons/TX-T9/etalon_QG.txt"
  python scan_testops.py --launch-id 12345 --teams "T9,TX" --etalon "<путь>" --tail-check
  python scan_testops.py --launch-id 12345 --teams "T9,TX" --etalon "<путь>" --tester-tag "abodrykh"

Фильтр по тегу (--tester-tag) включается по OR-логике с командой:
  include = (Team∈teams И номер∈etalon) ИЛИ (есть тег тестировщика).
Без --tester-tag поведение бит-в-бит прежнее (только команда).

Конфигурация: .qa-artifacts/config.json или env TESTOPS_BASE_URL / TESTOPS_TOKEN.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Константы и конфигурация
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

# Пути — из единого модуля _paths (QA_HOME: env -> корень репозитория)
from _paths import ARTIFACTS_ROOT, ETALONS_DIR  # noqa: E402

# Единый нормализатор имён команд (транслит кириллицы, срез префикса, алиасы)
import team_canon  # noqa: E402

DEFAULT_ARTIFACTS_ROOT = ARTIFACTS_ROOT

TEST_NUMBER_RE = re.compile(r"(?<!\d)(\d{2}(?:\.\d{2,3})+)(?!\d)")
NON_PASS_STATUSES = {"failed", "broken", "skipped"}
TAIL_PAGES = 6  # гейт хвоста: последние min(6, pages) страниц
MESSAGE_TRUNCATE = 300


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
        except Exception as e:  # noqa: BLE001 — конфиг не должен ронять скрипт молча
            print(f"WARN: не удалось прочитать {cfg_path}: {e}")
    return cfg


def resolve_token(cfg: dict) -> str:
    token = os.environ.get(cfg.get("testops_token_env", "TESTOPS_TOKEN"), "")
    if not token:
        token = cfg.get("testops_token", "")
    return token.strip()


# ---------------------------------------------------------------------------
# HTTP-клиент: OAuth (apitoken -> JWT) + Bearer, повтор запроса при 401
# ---------------------------------------------------------------------------

# Модульное состояние: JWT, base, projectId и кэши (team по testCaseId, параметр по resultId).
# Заполняется в scan_launch (init_scan_state), используется заглушками *_stub.
_STATE = {
    "jwt": None,
    "cfg": None,
    "token": "",
    "base": "",
    "project_id": None,
    "team_cache": {},     # testCaseId -> команда (custom field 'Team')
    "detail_cache": {},   # resultId -> первый параметр из /api/testresult/{id}
    "tag_cache": {},      # tag -> set(testCaseId), найденных через AQL __search
}


def acquire_jwt(base: str, token: str) -> str:
    """POST /api/uaa/oauth/token (grant_type=apitoken, scope=openid) -> access_token (JWT)."""
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
    """GET + JSON с Authorization: Bearer <jwt>. При 401 — сброс кэша JWT,
    переполучение токена и повтор запроса (1 раз)."""
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
                _STATE["jwt"] = None  # сбросить кэш JWT и получить заново
                continue
            if e.code == 403:
                break  # доступ запрещён — ретраить бессмысленно
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
    raise RuntimeError(f"GET {url} не удался ({retries + 1} попыток): {last_err}")


# ---------------------------------------------------------------------------
# Извлечение полей результата (реализовано под реальный API TestOps)
# ---------------------------------------------------------------------------

def normalize_team(value: str) -> str:
    """Нормализация команды через единый нормализатор team_canon.

    Полный транслит кириллицы (ТММБ -> TMMB, ТСПб -> TSPB), срез префикса
    'SFA Команда'. Раньше здесь был урезанный вариант (только Т/т) — он давал
    мусорные папки вида 'TММБ'. Не дублировать логику: источник — team_canon.
    """
    return team_canon.normalize(value)


def extract_team_stub(result: dict) -> str:
    """Команда — custom field 'Team' тест-кейса (GET /api/testcase/{id}/cfv?projectId=).
    Кэш по testCaseId: много результатов делят один тест-кейс."""
    tcid = result.get("testCaseId")
    if not tcid:
        return ""
    cached = _STATE["team_cache"].get(tcid)
    if cached is not None:
        return cached
    team = ""
    if _STATE["base"] and _STATE["project_id"] is not None:
        try:
            data = http_get_json(
                f"{_STATE['base']}/api/testcase/{tcid}/cfv?projectId={_STATE['project_id']}",
                _STATE["token"], _STATE["cfg"])
            for item in data or []:
                cf = item.get("customField") or {}
                if str(cf.get("name") or "") == "Team":
                    team = str(item.get("name") or "")
                    if not team:  # fallback: если name пуст — читаем values[0].name
                        values = item.get("values") or []
                        if values:
                            team = str(values[0].get("name") or "")
                    break
        except Exception:  # noqa: BLE001 — команда не должна ронять скан
            team = ""
    _STATE["team_cache"][tcid] = team
    return team


def extract_test_number_stub(result: dict) -> str:
    """Номер теста из name (формат '16.01.05.01 ПРОДКАТ ...')."""
    m = TEST_NUMBER_RE.search(str(result.get("name") or ""))
    return m.group(1) if m else ""


def extract_message_stub(result: dict) -> str:
    """Сообщение об ошибке — top-level поле message в GET /api/testresult/{resultId}.
    Заодно кэширует первый параметр строки в detail_cache (для поля parameter в row)."""
    rid = result.get("id")
    if not rid:
        return ""
    msg = ""
    if _STATE["base"]:
        try:
            detail = http_get_json(f"{_STATE['base']}/api/testresult/{rid}",
                                   _STATE["token"], _STATE["cfg"])
            msg = str(detail.get("message") or "").strip()
            params = detail.get("parameters") or []
            if params:
                p0 = params[0]
                if isinstance(p0, dict):
                    _STATE["detail_cache"][rid] = str(p0.get("value") or "")
                else:
                    _STATE["detail_cache"][rid] = str(p0)
            else:
                _STATE["detail_cache"][rid] = ""
        except Exception:  # noqa: BLE001 — сообщение не должно ронять скан
            _STATE["detail_cache"][rid] = ""
    if len(msg) > MESSAGE_TRUNCATE:
        msg = msg[:MESSAGE_TRUNCATE] + "..."
    return msg


def init_scan_state(cfg: dict, token: str, launch: dict) -> None:
    """Заполняет модульное состояние HTTP-клиента: base, projectId, кэши. JWT — лениво."""
    _STATE["cfg"] = cfg
    _STATE["token"] = token
    _STATE["base"] = cfg["testops_base_url"].rstrip("/")
    _STATE["project_id"] = launch.get("projectId")
    _STATE["team_cache"].clear()
    _STATE["detail_cache"].clear()
    # tag_cache НЕ чистим: кэш по тегу не зависит от запуска (одна БД тест-кейсов).


# ---------------------------------------------------------------------------
# Тег тестировщика (AQL-поиск тест-кейсов: tag = "<tag>")
# ---------------------------------------------------------------------------

def load_tagged_testcase_ids(cfg: dict, token: str, tag: str) -> set:
    """Возвращает set(testCaseId) тест-кейсов с тегом, через AQL __search.

    GET /api/testcase/__search?projectId={pid}&rql=tag+%3D+%22{tag}%22&page=N&size=100
    Ответ: Spring Page {content:[{id,...}], totalElements} (в части сборок — {testCases,total}).
    Кэшируется в _STATE['tag_cache'].
    При пустом теге/отсутствии projectId/ошибке сети — set() + предупреждение (не падать).
    """
    tag = (tag or "").strip()
    if not tag:
        return set()
    cached = _STATE["tag_cache"].get(tag)
    if cached is not None:
        return set(cached)
    project_id = _STATE["project_id"]
    if project_id is None:
        print("WARN: projectId неизвестен — фильтр по тегу пропущен")
        return set()
    base = cfg["testops_base_url"].rstrip("/")
    size = int(cfg.get("page_size", 100))
    if size > 100:
        size = 100
    rql = f'tag = "{tag}"'
    rql_enc = urllib.parse.quote(rql)
    ids: set = set()
    total = None
    page = 0
    try:
        while True:
            data = http_get_json(
                f"{base}/api/testcase/__search?projectId={project_id}"
                f"&rql={rql_enc}&page={page}&size={size}",
                token, cfg)
            cases = data.get("testCases")
            if cases is None:
                cases = data.get("content") or []
            if total is None:
                total = int(data.get("total", data.get("totalElements")) or 0)
            for tc in cases:
                tcid = tc.get("id") if isinstance(tc, dict) else None
                if tcid is not None:
                    ids.add(tcid)
            if not cases or len(ids) >= total:
                break
            page += 1
            if page > 10_000:
                print("WARN: пагинация поиска по тегу превысила 10_000 страниц — стоп")
                break
    except Exception as e:  # noqa: BLE001 — тег не должен ронять скан
        print(f"WARN: поиск по тегу '{tag}' не удался: {e} → фильтр по тегу отключён")
        return set()
    _STATE["tag_cache"][tag] = set(ids)
    return ids


# ---------------------------------------------------------------------------
# Эталон
# ---------------------------------------------------------------------------

def natural_key(num: str) -> list:
    """Естественная сортировка номеров тестов: 02.x < 10.x < 39.x, строки не падают."""
    parts = []
    for chunk in str(num).split("."):
        parts.append((0, int(chunk)) if chunk.isdigit() else (1, chunk))
    return parts


def read_etalon(path: Path) -> tuple[set[str], int]:
    r"""Читает etalon.txt, извлекает номера `^\d{2}(\.\d{2,3})+`, дедуп. Возвращает (множество, N)."""
    if not path.exists():
        raise FileNotFoundError(f"Эталон не найден: {path}")
    raw = None
    for enc in ("utf-8", "cp1251"):
        try:
            raw = path.read_text(encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    if raw is None:
        raw = path.read_text(encoding="utf-8", errors="replace")
    numbers = set()
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        m = TEST_NUMBER_RE.search(line)
        if m:
            numbers.add(m.group(1))
    return numbers, len(numbers)


# ---------------------------------------------------------------------------
# Скан запуска
# ---------------------------------------------------------------------------

def scan_launch(launch_id: int, teams: list[str], etalon: set[str], cfg: dict, token: str,
                tail_check: bool = False, etalon_path: str = "",
                per_team: bool = False, tester_tag: str = "") -> dict:
    base = cfg["testops_base_url"].rstrip("/")
    size = int(cfg.get("page_size", 100))
    if size > 100:
        raise ValueError("page_size > 100 запрещён: TestOps молча обрезает вывод")

    launch = http_get_json(f"{base}/api/launch/{launch_id}", token, cfg)
    init_scan_state(cfg, token, launch)
    launch_name = str(launch.get("name") or launch_id)
    etalon_path_str = etalon_path or ""  # путь к эталону, как передан (для expected_set.json/trace)

    # Тег тестировщика: AQL-поиск -> set(testCaseId). Пусто при отсутствии тега (старое поведение).
    tagged_ids = load_tagged_testcase_ids(cfg, token, tester_tag)
    teams_norm = {normalize_team(t) for t in teams}

    total = 0  # берётся из totalElements первого ответа пагинации (в GET launch поля нет)
    page = 0
    rows_written = 0
    nonpass_rows = []
    expected_rows = []  # (number, status, result_id, message, origin) — каждая строка после фильтра
    team_result_rows = []  # (team_norm, number, status) — для per-team агрегата (Правка 4)
    origins_by_number: dict[str, str] = {}  # number -> origin (объединение при нескольких строках)
    included_by_tag = 0  # число строк, включённых ТОЛЬКО по тегу (origin == "tag")
    processed = 0
    new_test_by_number: dict[str, dict] = {}  # number -> {name, team, testCaseId, statuses:set} (G1)
    # 🔴 Охват эталона: номера тестов СВОЕЙ команды, реально встреченные в запуске,
    # БЕЗ фильтра по эталону. Нужны, чтобы измерить, какая доля эталона прогнана.
    # Раньше покрытие считалось от expected_numbers (уже отфильтрованных эталоном),
    # из-за чего метрика была тавтологичной и всегда давала 100%.
    launch_team_numbers: set[str] = set()
    team_rows_without_number = 0  # строки своей команды без номера в имени (не сопоставимы с эталоном)

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
            team_raw = extract_team_stub(r)
            number = extract_test_number_stub(r)
            team_norm = normalize_team(team_raw)
            team_ok = team_norm in teams_norm
            number_ok = number in etalon
            # Своя команда, но номер не распознан — такие строки не сопоставить с эталоном.
            if team_ok and not number:
                team_rows_without_number += 1
            # Своя команда + распознанный номер: фиксируем факт присутствия в запуске
            # ДО фильтра по эталону — это и есть настоящий охват.
            if team_ok and number:
                launch_team_numbers.add(number)
            # G1: тест команды, отсутствующий в эталоне (независимо от статуса) — кандидат в эталон.
            if number and team_norm in teams_norm and number not in etalon:
                entry = new_test_by_number.get(number)
                if entry is None:
                    new_test_by_number[number] = {
                        "name": str(r.get("name") or ""),
                        "team": team_norm,
                        "testCaseId": r.get("testCaseId"),
                        "statuses": {status},
                    }
                else:
                    entry["statuses"].add(status)
            # tag_ok: только если тег задан И testCaseId найден AQL-поиском.
            tag_ok = bool(tagged_ids) and (r.get("testCaseId") in tagged_ids)

            by_team = team_ok and number_ok
            # Новая OR-логика: команда ИЛИ тег. Без тега (tagged_ids пусто) — старое поведение БИТ-В-БИТ.
            include = (by_team or tag_ok) if tagged_ids else by_team
            if not include:
                continue

            if by_team and tag_ok:
                origin = "both"
            elif tag_ok:
                origin = "tag"
            else:
                origin = "team"
            if origin == "tag":
                included_by_tag += 1
            prev = origins_by_number.get(number)
            if prev is None:
                origins_by_number[number] = origin
            elif prev != origin:
                # объединение: "both" если встречаются team и tag, иначе оставляем существующий
                origins_by_number[number] = "both" if {prev, origin} == {"team", "tag"} else prev

            # Дополнительно сохраняем статус ЛЮБОЙ прошедшей фильтр строки
            # (включая PASS) для expected_set.json — не влияет на nonpass вывод.
            expected_rows.append((number, status, r.get("id"), "", origin))
            team_result_rows.append((team_norm, number, status))  # per-team (Правка 4)
            if status not in NON_PASS_STATUSES:
                continue  # сохраняем ТОЛЬКО не-PASS

            message = extract_message_stub(r)
            # сообщение не-PASS строки — для messages в expected_set.json
            expected_rows[-1] = (number, status, r.get("id"), message, origin)
            row = {
                "page": page,
                "result_id": r.get("id"),
                "testcase_id": r.get("testCaseId"),
                "number": number,
                "name": str(r.get("name") or ""),
                "status": status,
                "team": team_raw,
                "origin": origin,
                # параметра в flat-ответе нет; берём первый параметр из detail-запроса
                "parameter": _STATE["detail_cache"].get(r.get("id"), ""),
                "message": message,
            }
            nonpass_rows.append(row)
            rows_written += 1

        if processed >= total:
            break
        page += 1
        if page > 10_000:
            raise RuntimeError("Пагинация превысила 10_000 страниц — похоже, total не сходится")

    new_test_numbers = [
        {
            "number": number,
            "name": entry["name"],
            "team": entry["team"],
            "statuses": sorted(entry["statuses"]),
        }
        for number, entry in new_test_by_number.items()
    ]

    manifest = {
        "launch_id": launch_id,
        "launch_name": launch_name,
        "total": total,
        "pages": page + 1,
        "rows_scanned": processed,
        "nonpass_results": len(nonpass_rows),   # строк результатов (Examples учтены)
        "nonpass_numbers": len({str(r.get("number")) for r in nonpass_rows}),  # уникальных номеров
        "nonpass_count": len(nonpass_rows),   # DEPRECATED: исторический alias nonpass_results
        "complete": processed == total,
        "tail_checked": False,
        "tail_mismatch": None,
        "tester_tag": str(tester_tag or ""),
        "tagged_testcases": len(tagged_ids),
        "included_by_tag": included_by_tag,
        "new_test_numbers": new_test_numbers,
        "new_test_count": len(new_test_numbers),
    }

    expected_set = build_expected_set(
        launch_id, launch_name, teams, Path(etalon_path_str), expected_rows,
        origins=origins_by_number, tester_tag=tester_tag,
        new_tests=new_test_numbers,
    )
    if per_team:
        expected_set["teams_stats"] = build_teams_stats(team_result_rows)

    manifest["expected_count"] = expected_set["counts"]["total"]
    manifest["pass_count"] = expected_set["counts"]["passed"]
    manifest["pass_rate"] = expected_set["counts"]["pass_rate"]

    # --- Охват эталона: измеряется в НАПРАВЛЕНИИ «сколько эталона прогнано» ----
    # Ключевой контроль для вопроса «а все ли тесты команды попали в запуск?».
    # В отличие от старой etalon_coverage_pct (доля запуска, объяснённая эталоном —
    # величина, равная 100% по построению), здесь числитель — эталон, знаменатель —
    # эталон: 151/281 = 53.7% сразу показывает недостачу 130 тестов.
    etalon_norm = {str(x).strip().lower() for x in etalon}
    covered = etalon_norm & {str(n).strip().lower() for n in launch_team_numbers}
    missing_numbers = sorted(etalon_norm - covered, key=natural_key)

    coverage_etalon = round(len(covered) / len(etalon_norm) * 100, 1) if etalon_norm else 0.0
    launch_team_norm = {str(n).strip().lower() for n in launch_team_numbers}
    overlap_launch = len(launch_team_norm & etalon_norm)
    coverage_launch = round(overlap_launch / len(launch_team_norm) * 100, 1) if launch_team_norm else 0.0

    manifest["launch_team_numbers_count"] = len(launch_team_norm)
    manifest["etalon_overlap"] = overlap_launch          # номера запуска, найденные в эталоне
    manifest["etalon_coverage_pct"] = coverage_launch    # доля номеров запуска, объяснённая эталоном
    manifest["etalon_total"] = len(etalon_norm)
    manifest["etalon_covered"] = len(covered)
    manifest["etalon_coverage_etalon_pct"] = coverage_etalon
    manifest["etalon_missing_count"] = len(missing_numbers)
    manifest["etalon_missing_numbers"] = missing_numbers
    manifest["team_rows_without_number"] = team_rows_without_number
    expected_set["launch_team_numbers_count"] = len(launch_team_norm)
    expected_set["etalon_total"] = len(etalon_norm)
    expected_set["etalon_covered"] = len(covered)
    expected_set["etalon_coverage_etalon_pct"] = coverage_etalon
    expected_set["etalon_missing_numbers"] = missing_numbers

    # Чем считали: хеш эталона фиксируется в манифесте, чтобы подмена файла в кэше
    # была видна следующим шагам (dispatch / classify_rerun).
    if etalon_path_str:
        ep = Path(etalon_path_str)
        manifest["etalon_path"] = str(ep)
        manifest["etalon_sha256"] = sha256_file(ep) if ep.is_file() else None

    if tail_check:
        manifest["tail_checked"] = True
        manifest["tail_mismatch"] = check_tail(launch_id, teams, etalon, nonpass_rows, cfg, token,
                                               tagged_ids=tagged_ids)

    return {"manifest": manifest, "rows": nonpass_rows, "expected": expected_set}


def build_expected_set(launch_id: int, launch_name: str, teams: list[str], etalon_path: Path,
                       rows: list[tuple[str, str, object, str]],
                       origins: dict | None = None, tester_tag: str = "",
                       new_tests: list[dict] | None = None) -> dict:
    """Собирает expected_set.json — полную картину по каждому номеру эталона команд.

    rows: (number, status, result_id, message) — каждая строка, прошедшая фильтр
          (team ИЛИ tag), независимо от статуса (включая PASS). message — сообщение ошибки
          для не-PASS строк, пусто для PASS. Пятый элемент (origin) опционален (обратная совместимость).
    origins: number -> "team" | "tag" | "both" (пометка происхождения; объединение при дублях).
    tester_tag: значение --tester-tag (пусто, если режим команды).
    """
    order: list[str] = []
    statuses: dict[str, list[str]] = {}
    ids: dict[str, list] = {}
    messages: dict[str, list[str]] = {}
    row_origins: dict[str, str] = {}  # номер -> origin (из строк, если origins не передан)

    for row in rows:
        number, status, rid, message = row[0], row[1], row[2], row[3]
        origin = row[4] if len(row) > 4 else ""
        if number not in statuses:
            order.append(number)
            statuses[number] = []
            ids[number] = []
            messages[number] = []
        statuses[number].append(status)
        ids[number].append(rid)
        msg = (message or "").strip()
        if msg and (not messages[number] or messages[number][-1] != msg):
            messages[number].append(msg)
        if origin:
            prev = row_origins.get(number)
            if prev is None:
                row_origins[number] = origin
            elif prev != origin:
                row_origins[number] = "both" if {prev, origin} == {"team", "tag"} else prev

    origins = origins if origins is not None else row_origins

    passed = 0
    failed = 0
    broken = 0
    skipped = 0
    status_entries: dict[str, dict] = {}
    for number in order:
        st_list = statuses[number]
        agg = _aggregate_status(st_list)
        if agg == "passed":
            passed += 1
        elif agg == "failed":
            failed += 1
        elif agg == "broken":
            broken += 1
        elif agg == "skipped":
            skipped += 1
        status_entries[number] = {
            "status": agg,
            "statuses": st_list,
            "count": len(st_list),
            "result_ids": ids[number],
            "messages": messages[number],
            "origin": origins.get(number, ""),
        }

    total = len(order)
    nonpass = failed + broken + skipped
    pass_rate = round(passed / total * 100, 1) if total else 0.0

    return {
        "launch_id": launch_id,
        "launch_name": launch_name,
        "teams": list(teams),
        "etalon_path": str(etalon_path),
        "tester_tag": str(tester_tag or ""),
        "expected_numbers": order,
        "statuses": status_entries,
        "counts": {
            "total": total,
            "passed": passed,
            "failed": failed,
            "broken": broken,
            "skipped": skipped,
            "nonpass": nonpass,
            "pass_rate": pass_rate,
        },
        # Новые тесты команды, которых ещё нет в эталоне. В @-список НЕ входят
        # автоматически: решение о добавлении в эталон принимает человек.
        "new_tests": list(new_tests or []),
    }


def _aggregate_status(statuses: list[str]) -> str:
    """Агрегат по номеру: 'passed', если все строки passed; иначе первый не-pass в порядке появления."""
    if all(s == "passed" for s in statuses):
        return "passed"
    for s in statuses:
        if s in NON_PASS_STATUSES:
            return s
    return statuses[0] if statuses else "passed"


def check_tail(launch_id: int, teams: list[str], etalon: set[str], rows: list[dict],
               cfg: dict, token: str, tagged_ids: set | None = None) -> bool:
    """Гейт хвоста: последние min(TAIL_PAGES, pages) страниц перезапросить и сверить result_id.

    Сверяет тем же include-правилом, что и основной скан:
    (Team∈teams И номер∈etalon) ИЛИ (testCaseId ∈ tagged_ids).
    """
    base = cfg["testops_base_url"].rstrip("/")
    size = int(cfg.get("page_size", 100))
    tagged_ids = tagged_ids or set()
    teams_norm = {normalize_team(t) for t in teams}
    data0 = http_get_json(f"{base}/api/v2/launch/{launch_id}/test-result/flat?page=0&size={size}", token, cfg)
    total = int(data0.get("totalElements") or 0)
    pages = (total + size - 1) // size
    start = max(0, pages - TAIL_PAGES)
    written_ids = {str(r["result_id"]) for r in rows}
    mismatch = False
    for p in range(start, pages):
        data = http_get_json(f"{base}/api/v2/launch/{launch_id}/test-result/flat?page={p}&size={size}", token, cfg)
        for r in (data.get("content") or []):
            status = str(r.get("status") or "").lower()
            by_team = (normalize_team(extract_team_stub(r)) in teams_norm
                       and extract_test_number_stub(r) in etalon)
            tag_ok = bool(tagged_ids) and (r.get("testCaseId") in tagged_ids)
            include = (by_team or tag_ok) if tagged_ids else by_team
            if include and status in NON_PASS_STATUSES:
                if str(r.get("id")) not in written_ids:
                    mismatch = True
    return mismatch


def write_stability_history(launch_id: int, launch_name: str, expected: dict,
                            artifacts_root: Path) -> Path:
    """Дописывает строку в artifacts_root / 'stability.jsonl'.

    Строка: {"launch_id", "launch_name", "date", "teams", "total", "passed",
             "nonpass", "pass_rate"} — компактный JSON, ensure_ascii=False.
    Идемпотентно: если launch_id уже есть в файле — дубль не добавляется.
    """
    launch_name = launch_name if launch_name is not None else ""
    counts = expected.get("counts", {}) if expected is not None else {}
    entry = {
        "launch_id": launch_id,
        "launch_name": launch_name,
        "date": datetime.date.today().isoformat(),
        "teams": list(expected.get("teams") or []) if expected is not None else [],
        "total": counts.get("total") if isinstance(counts, dict) else None,
        "passed": counts.get("passed") if isinstance(counts, dict) else None,
        "nonpass": counts.get("nonpass") if isinstance(counts, dict) else None,
        "pass_rate": counts.get("pass_rate") if isinstance(counts, dict) else None,
    }
    stability_path = artifacts_root / "stability.jsonl"
    stability_path.parent.mkdir(parents=True, exist_ok=True)

    if stability_path.exists():
        for line in stability_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except (TypeError, ValueError):  # noqa: PERF203 — повреждённая строка не роняет скрипт
                continue
            if record.get("launch_id") == launch_id:
                return stability_path  # уже есть — не добавляем дубль

    with stability_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
    return stability_path


def build_teams_stats(team_results: list[tuple[str, str, str]]) -> dict:
    """Per-team агрегат result-level (Правка 4): PASS/FAIL/BROKEN/SKIP по командам.

    team_results: list[(team_norm, number, status)] — по одной записи на КАЖДЫЙ результат
    после двойного фильтра (team + etalon), включая PASS.
    Возвращает {TEAM: {total,passed,failed,broken,skipped,uniq_nonpass}}.
    Σ total == len(team_results) (каждый результат учтён ровно в одной команде).
    """
    stats: dict[str, dict] = {}
    uniq_nonpass: dict[str, set] = {}
    for team, number, status in team_results:
        s = stats.setdefault(team, {"total": 0, "passed": 0, "failed": 0,
                                    "broken": 0, "skipped": 0, "uniq_nonpass": 0})
        s["total"] += 1
        st = str(status or "").lower()
        if st in s:
            s[st] += 1
        if st in NON_PASS_STATUSES:
            uniq_nonpass.setdefault(team, set()).add(number)
    for team, nums in uniq_nonpass.items():
        stats[team]["uniq_nonpass"] = len(nums)
    return stats


def teams_slug(teams_str: str) -> str:
    """Нормализованный слаг команд для подпапки: 'T9,TX' -> 'T9-TX', 'TREX' -> 'TREX'.

    Части нормализуются через team_canon.normalize (полный транслит), затем
    сортируются — поэтому 'T9,TX' и 'TX,T9' дают один слаг 'T9-TX'.
    ⚠️ Это слаг ПАПКИ артефактов, а не канон-имя команды: канон для T9/TX —
    'TX-T9' (папка эталона), его даёт team_canon.resolve_canon.
    """
    parts = [team_canon.normalize(t) for t in str(teams_str or "").split(",")]
    parts = [p for p in parts if p]
    return "-".join(sorted(parts)) if parts else "ALL"


def find_expected_path(root: Path, launch_id: int, teams: list[str]) -> Path | None:
    """Ищет существующий expected_set.json для запуска.

    Порядок: заданная команда → корень запуска → любая подпапка команды
    (для вызова без --teams: 'launch-id <id> --summary').
    """
    launch_dir = root / "launches" / str(launch_id)
    cands = []
    if teams:
        cands.append(launch_dir / teams_slug(",".join(teams)) / "expected_set.json")
    cands.append(launch_dir / "expected_set.json")
    for cand in cands:
        if cand.exists():
            return cand
    # fallback: единственная/первая подпапка команды с expected_set.json
    if launch_dir.exists():
        subs = sorted(p for p in launch_dir.iterdir()
                      if p.is_dir() and (p / "expected_set.json").exists())
        if len(subs) == 1:
            return subs[0] / "expected_set.json"
        if subs:
            return subs[0] / "expected_set.json"  # неоднозначно — вернём первую (см. stderr-предупреждение в main)
    return None


def emit_slice(expected: dict, mode: str) -> None:
    """Печатает срез уже существующего expected_set.json (без полного дампа файла).

    mode: 'summary' | 'nonpass' | 'numbers'.
      summary — одна строка counts;
      nonpass — по строке 'номер статус первая_причина' для не-PASS (без result_ids/count/statuses);
      numbers — только expected_numbers (по одному в строке).
    """
    if mode == "summary":
        c = expected.get("counts") or {}
        print(
            f"SUMMARY total={c.get('total', 0)} passed={c.get('passed', 0)} "
            f"failed={c.get('failed', 0)} broken={c.get('broken', 0)} "
            f"skipped={c.get('skipped', 0)} nonpass={c.get('nonpass', 0)} "
            f"pass_rate={c.get('pass_rate', 0)}"
        )
        return
    if mode == "numbers":
        for number in expected.get("expected_numbers", []):
            print(number)
        return
    if mode == "nonpass":
        for number, meta in (expected.get("statuses") or {}).items():
            status = str(meta.get("status") or "").lower()
            if status not in NON_PASS_STATUSES:
                continue
            messages = meta.get("messages") or []
            reason = str(messages[0] if messages else "").replace("\n", " ").strip()
            print(f"{number} {status} {reason}")
        return


def sha256_file(path) -> str:
    """SHA-256 файла (hex). Нужен для сертификации артефактов скана."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_etalon_pin(path: Path) -> tuple[bool, str]:
    """Сверяет эталон с пломбой <etalon>.sha256. Нет пломбы — не ошибка (старый кэш)."""
    side = Path(str(path) + ".sha256")
    if not side.is_file():
        return True, "пломбы нет"
    try:
        expected = side.read_text(encoding="utf-8").strip().lower()
    except OSError as e:
        return False, f"не прочитать пломбу {side}: {e}"
    actual = sha256_file(path)
    if expected and expected != actual:
        return False, (f"эталон {path.name} не совпадает с пломбой "
                       f"(ожидалось {expected[:12]}…, сейчас {actual[:12]}…)")
    return True, "ok"


def save_artifacts(launch_id: int, manifest: dict, rows: list[dict], artifacts_root: Path,
                   expected: dict | None = None, subdir: str | None = None) -> tuple[Path, Path, Path]:
    # 🔴 Командная изоляция: снимок пишется в underпапку команд (subdir), корень launches/{id}/ НЕ трогаем.
    # Если subdir не задан — legacy: писать в корень запуска (обратная совместимость).
    launch_dir = artifacts_root / "launches" / str(launch_id)
    out_dir = launch_dir / subdir if subdir else launch_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_dir / "nonpass_results.jsonl"
    manifest_path = out_dir / "scan_manifest.json"
    expected_path = out_dir / "expected_set.json"
    with jsonl_path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    if expected is not None:
        # Контракт для режима классификации перезапуска: абсолютный путь папки
        # команды. Читатели (rerun-classifier) берут отсюда case_meta.json,
        # не вычисляя путь повторно по своим правилам.
        expected["artifacts_path"] = str(out_dir)
        expected_path.write_text(json.dumps(expected, ensure_ascii=False, indent=2), encoding="utf-8")
        # Хеш пишется ПОСЛЕ записи файла: он и есть сертификат «этот снимок не правили».
        # Без него dispatch/classify не могут отличить честный expected_set от подменённого.
        manifest["expected_set_sha256"] = sha256_file(expected_path)
    manifest["nonpass_results_sha256"] = sha256_file(jsonl_path)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return jsonl_path, manifest_path, expected_path


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(description="Скан TestOps: пагинация + двойной фильтр -> nonpass_results.jsonl")
    ap.add_argument("--launch-id", type=int, required=True)
    ap.add_argument("--teams", default="", help="Команды через запятую, напр. 'T9,TX' (не нужен в режимах среза)")
    ap.add_argument("--etalon", default="", help="Путь к etalon.txt (не нужен в режимах среза)")
    ap.add_argument("--root", default=None, help="Корень .qa-artifacts (по умолчанию рядом со скриптом)")
    ap.add_argument("--tail-check", action="store_true", help="Дополнительная сверка хвоста (гейт п.3.8)")
    ap.add_argument("--summary", action="store_true",
                    help="Срез: только counts из существующего expected_set.json (одна строка)")
    ap.add_argument("--nonpass", action="store_true",
                    help="Срез: номер+статус+первая причина для failed/broken/skipped (без result_ids/count/statuses)")
    ap.add_argument("--numbers", action="store_true",
                    help="Срез: только expected_numbers из существующего expected_set.json")
    ap.add_argument("--per-team", action="store_true",
                    help="Добавить teams_stats (per-team PASS/FAIL/BROKEN/SKIP + uniq_nonpass) в expected_set.json")
    ap.add_argument("--tester-tag", default="",
                    help="Тег тестировщика (один), напр. 'abodrykh'. OR-логика с командами: тест "
                         "включается если (Team∈teams И номер∈etalon) ИЛИ (есть этот тег). "
                         "Без флага — старое поведение")
    args = ap.parse_args()

    artifacts_root = Path(args.root) if args.root else DEFAULT_ARTIFACTS_ROOT
    cfg = load_config(artifacts_root)

    # ---- Подрежимы среза: если expected_set.json уже есть — читаем его и печатаем срез,
    #      НЕ запуская повторный скан и НЕ читая полный дамп файла.
    slice_mode = "summary" if args.summary else "nonpass" if args.nonpass else "numbers" if args.numbers else None
    if slice_mode:
        teams = [t.strip() for t in args.teams.split(",") if t.strip()] if args.teams else []
        exp_path = find_expected_path(artifacts_root, args.launch_id, teams)
        if exp_path is None:
            print(f"ОШИБКА: expected_set.json не найден для launch {args.launch_id} "
                  f"(искали в launches/{args.launch_id}/[{teams_slug(','.join(teams))}/]). Сначала обычный скан.")
            return 1
        try:
            expected = json.loads(exp_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"ОШИБКА чтения {exp_path}: {e}")
            return 1
        print(f"# source: {exp_path}", file=sys.stderr)
        emit_slice(expected, slice_mode)
        return 0

    token = resolve_token(cfg)
    if not token:
        print("ОШИБКА: токен TestOps не задан. Укажи TESTOPS_TOKEN в env или token в "
              f"{artifacts_root / 'config.json'} (ключ testops_token).")
        return 1

    teams = [t.strip() for t in args.teams.split(",") if t.strip()]
    if not teams:
        print("ОШИБКА: --teams пуст")
        return 1

    try:
        etalon, n_etalon = read_etalon(Path(args.etalon))
    except FileNotFoundError as e:
        print(f"ОШИБКА: {e}")
        return 1
    if n_etalon == 0:
        print("ОШИБКА: эталон пуст или не содержит номеров формата NN(.NN)+ — проверь путь")
        return 1

    # Пломба эталона: если рядом есть .sha256, файл в кэше не должен с ней расходиться.
    pin_ok, pin_msg = check_etalon_pin(Path(args.etalon))
    if not pin_ok:
        print(f"ОШИБКА: {pin_msg}")
        print("  Эталон в кэше изменён после скачивания. Обнови: "
              "python fetch_etalons.py --team <TEAM> --type <тип> --refresh")
        print("  Если изменение намеренное (состав команды меняется) — это делается через "
              "update_etalon.py и PR в ветку qa-analysis-data, а не правкой кэша.")
        return 1

    try:
        res = scan_launch(args.launch_id, teams, etalon, cfg, token,
                          tail_check=args.tail_check, etalon_path=args.etalon,
                          per_team=args.per_team, tester_tag=args.tester_tag)
    except Exception as e:  # noqa: BLE001
        print(f"ОШИБКА скана: {e}")
        return 1

    m = res["manifest"]

    # --- Контроль эталона уровнем 2: пересечение с запуском -------------------
    # Самая ценная проверка: типы эталонов не пересекаются между собой
    # (RM ∩ QG = 0), поэтому неверный тип/команда даёт НОЛЬ пересечений.
    # Не блокирует скан (артефакты уже собраны), но помечает манифест и
    # возвращает код 1 — публикация при таком сигнале запрещена.
    #
    # 🔴 ВАЖНО: числа берутся из манифеста, посчитанного в scan_launch().
    # Раньше здесь пересечение считалось от expected_numbers (уже отфильтрованных
    # эталоном), из-за чего coverage всегда выходил 100% и не мог поймать
    # неполный прогон (реальный кейс: прогон содержал 151 из 281 теста — «100%»).
    overlap = int(m.get("etalon_overlap") or 0)
    launch_team_count = int(m.get("launch_team_numbers_count") or 0)
    coverage = m.get("etalon_coverage_pct") or 0.0
    coverage_etalon = m.get("etalon_coverage_etalon_pct") or 0.0
    missing_numbers = m.get("etalon_missing_numbers") or []

    if args.tester_tag:
        # В tag-ветке эталон опционален: нулевое пересечение там норма.
        pass
    elif not overlap:
        m["etalon_mismatch"] = True
        print(f"SCAN LAUNCH {args.launch_id} '{m['launch_name']}'")
        print(f"  total_results: {m['total']} | rows_scanned: {m['rows_scanned']}")
        print(f"  etalon_overlap: 0 из {launch_team_count} номеров команды в запуске "
              f"(в эталоне {n_etalon}, новых вне эталона {m.get('new_test_count', 0)})")
        print("ОШИБКА: эталон не пересекается с запуском — вероятно, неверный тип "
              "(HF/QG/RM) или команда.")
        print("СТАТУС: НЕВЕРНЫЙ ЭТАЛОН — артефакты НЕ записаны, публикация ЗАБЛОКИРОВАНА")
        # 🔴 Артефакты не пишем: иначе валидный снимок предыдущего скана
        # будет затёрт результатом заведомо неверного прогона.
        return 1
    elif coverage < 50:
        m["etalon_low_coverage"] = True
        print(f"ВНИМАНИЕ: эталон покрывает {coverage}% номеров запуска — возможно, эталон "
              f"устарел (не блокер: дрейф эталона нормален).")

    jsonl_path, manifest_path, expected_path = save_artifacts(
        args.launch_id, m, res["rows"], artifacts_root, res.get("expected"),
        subdir=teams_slug(args.teams))

    print(f"SCAN LAUNCH {args.launch_id} '{m['launch_name']}'")
    print(f"  total_results: {m['total']} | pages: {m['pages']} | rows_scanned: {m['rows_scanned']}")
    if args.tester_tag:
        print(f"  nonpass: {m['nonpass_results']} результатов / {m['nonpass_numbers']} номеров (команда ИЛИ тег)")
        print(f"  tester_tag: {m['tester_tag']} | tagged_testcases: {m['tagged_testcases']} "
              f"| included_by_tag: {m['included_by_tag']}")
    else:
        print(f"  nonpass: {m['nonpass_results']} результатов / {m['nonpass_numbers']} номеров (Team∈teams И номер∈etalon)")
    print(f"  new_test_numbers: {m['new_test_count']}")
    print(f"  complete: {m['complete']} | tail_checked: {m['tail_checked']} | tail_mismatch: {m['tail_mismatch']}")
    print(f"  ОХВАТ ЭТАЛОНА: {m.get('etalon_covered')}/{m.get('etalon_total')} номеров "
          f"({m.get('etalon_coverage_etalon_pct')}% эталона прогнано) | "
          f"не запускалось: {m.get('etalon_missing_count')}")
    if m.get("team_rows_without_number"):
        print(f"  строки своей команды без номера (не сопоставимы с эталоном): "
              f"{m['team_rows_without_number']}")
    print(f"  jsonl: {jsonl_path}")
    print(f"  manifest: {manifest_path}")
    print(f"  expected_set: {expected_path} | pass_rate: {m['pass_rate']}%")

    if not m["complete"]:
        print("СТАТУС: НЕПОЛНЫЙ СКАН (rows_scanned != total) — пересканировать, публикация ЗАБЛОКИРОВАНА")
        return 1
    if m.get("tail_mismatch"):
        print("СТАТУС: РАСХОЖДЕНИЕ В ХВОСТЕ — пересканировать затронутый диапазон страниц")
        return 1
    if res.get("expected"):
        stability_path = write_stability_history(
            args.launch_id, m["launch_name"], res["expected"], artifacts_root)
        print(f"  stability: {stability_path}")

    # Пересечение эталона с запуском — блокирующий сигнал (см. проверку выше).
    if m.get("etalon_mismatch"):
        print("СТАТУС: НЕВЕРНЫЙ ЭТАЛОН (0 пересечений с запуском) — публикация ЗАБЛОКИРОВАНА")
        return 1

    print(f"  etalon_overlap: {m['etalon_overlap']} номеров запуска из эталона "
          f"({m['etalon_coverage_pct']}% запуска объяснено эталоном)")
    # 🔴 Гейт полноты охвата: прогон может быть корректно отсканирован, но содержать
    # лишь часть тестов команды. Без этого сигнала «нет падений у 130 тестов»
    # читается как «всё хорошо», хотя тесты просто не запускались.
    cov_et = m.get("etalon_coverage_etalon_pct") or 0.0
    miss = m.get("etalon_missing_count") or 0
    if miss and cov_et < 95.0:
        m["etalon_incomplete_run"] = True
        print(f"ВНИМАНИЕ: прогон неполный — прогнано {m.get('etalon_covered')} из "
              f"{m.get('etalon_total')} номеров эталона ({cov_et}%), не запускалось {miss}.")
        print(f"  Не запускавшиеся номера: {m['etalon_missing_numbers']}")
        print("  Эти тесты НЕ имеют статуса в прогоне — судить об их работоспособности нельзя.")
        print("  Для полного покрытия запустить их отдельно (список — etalon_missing_numbers).")
        print("СТАТУС: OK (охват неполный)")
        return 0
    print("СТАТУС: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

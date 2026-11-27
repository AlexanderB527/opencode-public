#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
doctor.py — предполётная проверка окружения Testops_killer (Правка 5).

Проверяет наличие токенов и печатает пути артефактов. Не ходит в сеть.
  - TestOps: env TESTOPS_TOKEN, иначе .qa-artifacts/config.json → testops_token.
  - Данные (эталоны и база результатов) читаются через git: нужны git в PATH
    и доступ к репозиторию AUTOTEST/sfa (креды git оператора).
  - Confluence: публикация идёт через MCP atlassian (токен в opencode.json);
    скриптам токен публикации больше не нужен.
Exit 0 — обязательные ресурсы есть; 1 — чего-то не хватает.

Использование:
  py -X utf8 doctor.py [--root <путь к .qa-artifacts>]
"""
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from _paths import ARTIFACTS_ROOT, CONFIG_DIR, ETALONS_DIR, QA_HOME, RESULTS_DIR, RULES_PATH  # noqa: E402
import git_data  # noqa: E402

DEFAULT_ARTIFACTS_ROOT = ARTIFACTS_ROOT


def package_hygiene():
    """Проверка чистоты поставляемого пакета скилла.

    Возвращает список проблем:
      - `__pycache__` / `*.pyc` внутри скилла (мусор от прогонов, риск импорта
        устаревшего байткода);
      - каталог `temp/` внутри скилла (черновики не должны коммититься).

    Исключение: `evals/` — штатная часть поставки.
    """
    problems = []
    if not QA_HOME.exists():
        return problems
    for cache in QA_HOME.rglob("__pycache__"):
        problems.append(f"__pycache__ в пакете: {cache.relative_to(QA_HOME)}")
    for pyc in QA_HOME.rglob("*.pyc"):
        problems.append(f".pyc в пакете: {pyc.relative_to(QA_HOME)}")
    temp_dir = QA_HOME / "temp"
    if temp_dir.is_dir():
        leftovers = [p.name for p in temp_dir.iterdir() if p.name != ".gitkeep"]
        if leftovers:
            problems.append(f"temp/ не пуст: {len(leftovers)} файл(ов)")
    return problems


# ---------------------------------------------------------------------------
# Проверка согласованности реестра джоб и эталонов
# ---------------------------------------------------------------------------

def registry_consistency():
    """Сверяет config/team_jobs.json с фактическими эталонами и наоборот.

    Ловит разъезд, который иначе проявится только в бою как `unmapped`:
      - команда есть в реестре, но нет папки эталона;
      - есть папка эталона, но команды нет в реестре.
    """
    problems = []
    jobs_path = CONFIG_DIR / "team_jobs.json"
    if not jobs_path.exists():
        return ["config/team_jobs.json отсутствует"]
    try:
        registry = json.loads(jobs_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return [f"config/team_jobs.json не читается: {e}"]

    reg_teams = {k for k in registry if not str(k).startswith("_")}
    etalon_teams = {p.parent.name for p in ETALONS_DIR.glob("*/etalon_*.txt")} \
        if ETALONS_DIR.exists() else set()

    for team in sorted(reg_teams - etalon_teams):
        problems.append(f"команда в реестре без эталона: {team}")
    for team in sorted(etalon_teams - reg_teams):
        problems.append(f"эталон без команды в реестре: {team}")
    for team, conf in sorted(registry.items()):
        if str(team).startswith("_"):
            continue
        if not isinstance(conf, dict) or not conf.get("job"):
            problems.append(f"у команды {team} не задано поле job")
    return problems


# ---------------------------------------------------------------------------
# Валидация артефактов по JSON-схемам (config/contracts/*.schema.json)
# ---------------------------------------------------------------------------

CONTRACTS_DIR = CONFIG_DIR / "contracts"

# Минимальный валидатор без внешних зависимостей: только stdlib.
# Поддерживает подмножество JSON Schema, которое реально используется
# в наших схемах: type, required, properties, items, enum, additionalProperties.
_TYPES = {
    "object": dict, "array": list, "string": str, "integer": int,
    "number": (int, float), "boolean": bool, "null": type(None),
}


def _validate(value, schema, path, errors):
    """Рекурсивная проверка значения по упрощённой схеме."""
    if not isinstance(schema, dict):
        return

    # type может быть строкой или списком строк
    expected = schema.get("type")
    if expected:
        types = expected if isinstance(expected, list) else [expected]
        ok = False
        for t in types:
            py_t = _TYPES.get(t)
            if py_t is None:
                continue
            if t == "integer" and isinstance(value, bool):
                continue  # bool не считается integer
            if isinstance(value, py_t):
                ok = True
                break
        if not ok:
            errors.append(f"{path}: ожидался {expected}, получен {type(value).__name__}")
            return

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: значение {value!r} вне enum {schema['enum']}")

    if isinstance(value, dict):
        for req in schema.get("required", []):
            if req not in value:
                errors.append(f"{path}: отсутствует обязательное поле {req!r}")
        props = schema.get("properties") or {}
        for key, sub in props.items():
            if key in value:
                _validate(value[key], sub, f"{path}.{key}", errors)
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(props)
            for key in sorted(extra):
                errors.append(f"{path}: лишнее поле {key!r} (additionalProperties=false)")
    elif isinstance(value, list):
        item_schema = schema.get("items")
        if item_schema:
            for i, item in enumerate(value):
                _validate(item, item_schema, f"{path}[{i}]", errors)


def validate_value(value, schema, path="$"):
    """Публичная обёртка валидатора: список ошибок (пустой = валидно)."""
    errors = []
    _validate(value, schema, path, errors)
    return errors


def validate_file(document, schema_path):
    """Валидирует документ по JSON-схеме из файла. Возвращает список ошибок."""
    try:
        schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return [f"схема не читается ({schema_path}): {e}"]
    return validate_value(document, schema)


def _find_artifacts(root):
    """Ищет артефакты в корне прогонов, ограничиваясь 3 последними launch_id."""
    found = []
    launches = root / "launches"
    if launches.is_dir():
        launch_dirs = sorted(
            (d for d in launches.iterdir() if d.is_dir() and d.name.isdigit()),
            key=lambda d: int(d.name),
        )[-3:]
        for ld in launch_dirs:
            disp = ld / "dispatch.json"
            if disp.exists():
                found.append(("dispatch", disp))
            for team_dir in sorted(d for d in ld.iterdir() if d.is_dir()):
                exp = team_dir / "expected_set.json"
                if exp.exists():
                    found.append(("expected_set", exp))
                cm = team_dir / "case_meta.json"
                if cm.exists():
                    found.append(("case_meta", cm))
    return found


def validate_contracts(root):
    """Валидирует найденные артефакты по схемам. Возвращает (ok_count, problems)."""
    problems = []
    schema_files = {
        "expected_set": CONTRACTS_DIR / "expected_set.schema.json",
        "case_meta": CONTRACTS_DIR / "case_meta.schema.json",
        "dispatch": CONTRACTS_DIR / "dispatch.schema.json",
    }
    schemas = {}
    for kind, path in schema_files.items():
        if not path.exists():
            problems.append(f"нет схемы {path.name}")
            continue
        try:
            schemas[kind] = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            problems.append(f"{path.name} не читается: {e}")

    ok_count = 0
    legacy = 0
    for kind, path in _find_artifacts(root):
        schema = schemas.get(kind)
        if not schema:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            problems.append(f"{path.name} не читается: {e}")
            continue
        # Legacy-формат case_meta (плоский, без секций run/dispatch/rerun):
        # исторические артефакты не считаем ошибкой, но помечаем как устаревшие.
        if kind == "case_meta" and isinstance(data, dict) and "run" not in data \
                and "launch_id" in data:
            legacy += 1
            continue
        # Legacy expected_set (скан до появления artifacts_path): путь выводим
        # из расположения файла, поэтому отсутствие поля — не ошибка формата.
        if kind == "expected_set" and isinstance(data, dict) \
                and "artifacts_path" not in data:
            legacy += 1
            continue
        errs = []
        _validate(data, schema, path.name, errs)
        if errs:
            short = path.parent.name + "/" + path.name
            for e in errs[:3]:
                problems.append(f"{short}: {e}")
        else:
            ok_count += 1
    if legacy:
        problems.append(
            f"артефактов в legacy-формате: {legacy} "
            f"(case_meta → секции run/dispatch/rerun; expected_set → пересканировать)"
        )
    return ok_count, problems
    return problems


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    import argparse
    ap = argparse.ArgumentParser(description="Предполётная проверка токенов и путей артефактов")
    ap.add_argument("--root", default=None, help="Корень .qa-artifacts (по умолчанию рядом со скриптом)")
    args = ap.parse_args()

    root = Path(args.root) if args.root else DEFAULT_ARTIFACTS_ROOT
    cfg_path = root / "config.json"
    cfg = {}
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"WARN: не удалось прочитать {cfg_path}: {e}")

    # --- TestOps token ---
    token_env = cfg.get("testops_token_env", "TESTOPS_TOKEN")
    testops_env = os.environ.get(token_env, "").strip()
    testops_cfg = str(cfg.get("testops_token", "") or "").strip()
    testops_ok = bool(testops_env or testops_cfg)
    testops_src = f"env {token_env}" if testops_env else ("config.json" if testops_cfg else "—")

    # --- git: единственный транспорт для данных (эталоны и база результатов) ---
    git_version = git_data.git_available()
    bb_project = cfg.get("etalons_project") or "AUTOTEST"
    bb_repo_name = cfg.get("etalons_repo") or "sfa"
    bb_base = (cfg.get("bitbucket_base_url") or "https://example.invalid").rstrip("/")
    bb_branch = cfg.get("etalons_branch") or "qa-analysis-data"
    results_branch = cfg.get("results_branch") or "qa-analysis-data"
    bb_repo = f"{bb_project}/{bb_repo_name}"

    def browse_url(branch, path):
        return (f"{bb_base}/projects/{bb_project}/repos/{bb_repo_name}/browse"
                f"?at=refs%2Fheads%2F{branch}&path={path}")

    def mark(ok):
        return "OK" if ok else "ОШИБКА"

    print("=== QA-toolkit doctor ===")
    print(f"  QA_HOME:        {QA_HOME}")
    print(f"  artifacts root: {root}")
    print(f"  эталоны (кэш):  {ETALONS_DIR}")
    print(f"  эталоны (ветка): {browse_url(bb_branch, 'etalons')}")
    print(f"  результаты (кэш): {RESULTS_DIR}")
    print(f"  результаты (ветка): {browse_url(results_branch, 'results')}")
    print(f"  config dir:     {CONFIG_DIR}")

    # --- поставка: правила и эталоны ---
    rules_ok = RULES_PATH.exists()
    etalon_files = sorted(ETALONS_DIR.glob("*/etalon_*.txt")) if ETALONS_DIR.exists() else []
    teams = sorted({p.parent.name for p in etalon_files})
    autotesters_path = root / "autotesters.json"
    autotesters_ok = autotesters_path.exists()

    print()
    print(f"[{mark(testops_ok)}] TestOps token  (источник: {testops_src}, имя env: {token_env})")
    if not testops_ok:
        print(f"       → задать: setx {token_env} \"<токен>\"  (macOS/Linux: export {token_env}=...) "
              f"и открыть новый терминал")
    py_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    py_ok = sys.version_info >= (3, 9)
    print(f"[{mark(py_ok)}] Python {py_version}" + ("" if py_ok else "  (нужен 3.9+)"))
    git_ok = bool(git_version)
    git_old = False
    if git_version:
        import re as _re
        m = _re.search(r"(\d+)\.(\d+)", git_version)
        if m:
            git_old = (int(m.group(1)), int(m.group(2))) < (2, 25)
    print(f"[{mark(git_ok and not git_old)}] git: {git_version or 'не найден в PATH — данные не скачать'}"
          + ("  (нужен 2.25+ для partial/sparse clone)" if git_old else ""))
    if not git_ok:
        print("       → установить: winget install --id Git.Git -e  (macOS: brew install git; "
              "Linux: sudo apt install git)")
    elif git_old:
        print("       → обновить git до 2.25+")
    print("[i] Confluence: через MCP atlassian (токен в opencode.json)")
    print(f"  base URL: {cfg.get('testops_base_url', 'https://example.invalid')}")
    print(f"[{mark(rules_ok)}] classify_rules.json: {RULES_PATH}")
    print(f"[{mark(bool(etalon_files))}] эталоны в кэше: {len(teams)} команд, {len(etalon_files)} файлов"
          + (f" ({', '.join(teams)})" if teams else ""))
    if not etalon_files:
        print(f"       → скачать: py -X utf8 {QA_HOME / 'scripts' / 'fetch_etalons.py'}"
              f"  (нужен git-доступ к {bb_repo}, ветка {bb_branch})")
    print(f"[{'OK' if autotesters_ok else '—'}] autotesters.json ({autotesters_path}) "
          f"— реестр персональных Jenkins-джоб (шаблон: config/autotesters.example.json)")

    # --- база результатов (кэш) ---
    results_teams = sorted(p.name for p in RESULTS_DIR.iterdir() if p.is_dir()) \
        if RESULTS_DIR.exists() else []
    results_launches = len(list(RESULTS_DIR.glob("*/launches/*.json"))) if RESULTS_DIR.exists() else 0
    print(f"[{'OK' if results_teams else '—'}] база результатов (кэш): {len(results_teams)} команд, "
          f"{results_launches} прогонов" + (f" ({', '.join(results_teams[:6])})" if results_teams else ""))
    if not results_teams:
        print(f"       → скачать: py -X utf8 {QA_HOME / 'scripts' / 'fetch_results.py'} --all")

    # --- гигиена поставляемого пакета ---
    hygiene = package_hygiene()
    if hygiene:
        print(f"[WARN] пакет не чист ({len(hygiene)}):")
        for h in hygiene[:10]:
            print(f"       - {h}")
    else:
        print("[OK] пакет чист: нет __pycache__/.pyc/temp")

    # --- согласованность реестра джоб и эталонов ---
    reg_problems = registry_consistency()
    if reg_problems:
        print(f"[WARN] реестр джоб / эталоны рассинхронизированы ({len(reg_problems)}):")
        for p in reg_problems[:10]:
            print(f"       - {p}")
    else:
        print("[OK] team_jobs.json согласован с эталонами")

    # --- валидация артефактов по контрактам ---
    contracts_ok, contract_problems = validate_contracts(root)
    if contract_problems:
        print(f"[WARN] артефакты не проходят контракты ({len(contract_problems)}):")
        for p in contract_problems[:10]:
            print(f"       - {p}")
    else:
        print(f"[OK] артефакты соответствуют контрактам (проверено: {contracts_ok})")

    print()
    if testops_ok and rules_ok and etalon_files:
        print("СТАТУС: OK — можно работать")
        # Предупреждения не блокируют работу, но их нужно устранить перед переносом.
        notes = []
        if hygiene:
            notes.append("пакет не чист — почистить перед коммитом/переносом")
        if reg_problems:
            notes.append("реестр джоб рассинхронизирован — поправить config/team_jobs.json")
        if contract_problems:
            notes.append("артефакты нарушают контракты — см. config/contracts/")
        for n in notes:
            print(f"ЗАМЕЧАНИЕ: {n}.")
        return 0
    print("СТАТУС: ОШИБКА — не готово:")
    if not testops_ok:
        print(f"  - нет TestOps-токена: задай переменную окружения {token_env}")
    if not rules_ok:
        print(f"  - отсутствует {RULES_PATH}")
    if not etalon_files:
        print(f"  - нет эталонов в кэше {ETALONS_DIR}: запусти scripts/fetch_etalons.py (нужен git-доступ к {bb_repo})")
    return 1


if __name__ == "__main__":
    sys.exit(main())
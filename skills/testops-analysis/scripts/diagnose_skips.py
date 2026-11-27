#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""diagnose_skips.py — ПОЧЕМУ тесты эталона не участвовали в прогоне.

Скилл умеет сказать «этот тест не запускался» (`etalon_missing_numbers` из
`scan_manifest.json`). Но от причины зависит, что с ним делать:

  * сборку прервали / она упала до старта этих тестов → перезапустить, и всё;
  * тест отключён в репозитории (лежит в `features/excluded/`) → перезапуск
    не поможет НИКОГДА, надо вернуть файл или убрать тест из эталона;
  * джоба не захватывает папку с тестом (в схеме не было этих тегов) → править
    джобу, перезапуск тоже не поможет.

Скрипт читает ТОЛЬКО свою сборку (jobRun своего запуска) и СВОЙ репозиторий
автотестов. Сверка с другими/соседними запусками не выполняется намеренно:
это отдельное решение оператора, а не автоматика.

Слои диагностики (каждый отключаемый, падение слоя не роняет отчёт):
  A. Jenkins — статус сборки, обрыв (ABORTED/SIGTERM), какие `tags` запрошены;
  B. Репозиторий — есть ли `<номер>.feature` и не в `features/excluded` ли он.

Выход: `launches/{id}/{team}/skip_diagnosis.json` + таблица в stdout.

Только стандартная библиотека. Креды Jenkins и параметры репозитория берутся
из тех же источников, что у `dispatch_team_reruns.py` / `fetch_etalons.py`.
"""
import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _paths  # noqa: E402 — определяет QA_HOME
import git_data  # noqa: E402
import scan_testops as st  # noqa: E402
import dispatch_team_reruns as dispatch  # noqa: E402

NUMBER_RE = re.compile(r'^(\d{2}(?:\.\d{2,3})+)')

# Диагнозы пропуска — закрытый список. «неизвестно» разрешён, но печатается
# с явной пометкой, чтобы отчёт не выглядел как выдуманная причина.
D_BUILD_ABORTED = "сборка прервана"
D_BUILD_FAILED = "сборка упала до этих тестов"
D_NOT_REQUESTED = "теги не передавались в сборку"
D_EXCLUDED_REPO = "тест отключён в репозитории (features/excluded)"
D_NO_FILE = "нет файла с таким номером (номер может быть тегом внутри feature)"
D_NOT_REACHED = "тег передан, но сценарий не стартовал"
D_UNPROVEN = "не установлено: feature есть, статус сборки недоступен"
D_UNKNOWN = "неизвестно"


class _Http404(Exception):
    """Сборка/джоба не найдена в Jenkins (ротация логов или неверный номер)."""


def load_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# Слой A — Jenkins: своя сборка запуска
# ---------------------------------------------------------------------------

def _jobrun_of_launch(cfg, token, launch_id):
    """jobRun первого результата запуска: {externalId, url, name, stage, status}.

    jobRun живёт в карточке результата (`GET /api/testresult/{id}`), не в самом
    launch. Берём первый результат — у всех результатов одной сборки он общий.
    """
    base = cfg["testops_base_url"].rstrip("/")
    page = st.http_get_json(
        f"{base}/api/v2/launch/{launch_id}/test-result/flat?page=0&size=1", token, cfg)
    content = (page.get("content") or []) if isinstance(page, dict) else []
    if not content:
        return None
    det = st.http_get_json(f"{base}/api/testresult/{content[0]['id']}", token, cfg)
    return det.get("jobRun") or None


def _parse_external_id(external_id):
    """'Main_Launches/QG#93' -> ('Main_Launches/QG', 93). Папки могут быть вложенными."""
    text = str(external_id or "")
    if "#" not in text:
        return None, None
    job, _, num = text.rpartition("#")
    try:
        return job.strip("/"), int(num)
    except ValueError:
        return None, None


def _jenkins_get(creds, path, timeout=30):
    """GET JSON из Jenkins API по относительному пути."""
    import base64
    import ssl
    import urllib.error
    import urllib.request

    url = creds["url"].rstrip("/") + path
    req = urllib.request.Request(url)
    if creds.get("user"):
        raw = f"{creds['user']}:{creds.get('token', '')}".encode("utf-8")
        req.add_header("Authorization", "Basic " + base64.b64encode(raw).decode("ascii"))
    ctx = ssl.create_default_context()
    if not creds.get("verify", False):
        ctx = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise _Http404(url) from e
        raise


def _jenkins_text(creds, path, timeout=60):
    import base64
    import ssl
    import urllib.request

    url = creds["url"].rstrip("/") + path
    req = urllib.request.Request(url)
    if creds.get("user"):
        raw = f"{creds['user']}:{creds.get('token', '')}".encode("utf-8")
        req.add_header("Authorization", "Basic " + base64.b64encode(raw).decode("ascii"))
    ctx = ssl.create_default_context()
    if not creds.get("verify", False):
        ctx = ssl._create_unverified_context()
    with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def layer_jenkins(creds, job, number, timeout=60):
    """Статус своей сборки + переданные tags.

    Возвращает dict: {status, result, aborted, params_tags, note}, где status:
      'ok'        — сборка прочитана;
      'missing'   — сборки нет (вычищена ротацией логов / другой номер);
      'noci'      — Jenkins API недоступен или креды не заданы.
    """
    out = {"status": "noci", "result": None, "aborted": False, "params_tags": None, "note": ""}
    if not job or not number:
        out["note"] = "в jobRun нет пары «джоба#номер»"
        return out
    job_path = "/".join(f"job/{part}" for part in job.split("/"))
    base_path = f"/{job_path}/{number}"
    try:
        data = _jenkins_get(creds, f"{base_path}/api/json?tree=result,building,duration,actions[parameters[name,value]]", timeout)
    except _Http404:
        out["status"] = "missing"
        out["note"] = f"сборки #{number} нет в Jenkins (вычищена ротацией логов)"
        return out
    except Exception as e:  # noqa: BLE001 — слой не должен ронять отчёт
        out["note"] = f"Jenkins API недоступен: {str(e)[:120]}"
        return out
    out["status"] = "ok"
    out["result"] = data.get("result")
    for action in data.get("actions") or []:
        for prm in action.get("parameters") or []:
            if str(prm.get("name")) == "tags":
                out["params_tags"] = str(prm.get("value") or "")
                break
        if out["params_tags"] is not None:
            break
    if str(out["result"] or "").upper() in ("ABORTED", "FAILURE"):
        try:
            log = _jenkins_text(creds, f"{base_path}/consoleText", timeout)
            out["aborted"] = "Aborted by" in log
            if out["aborted"]:
                who = log.split("Aborted by", 1)[1].splitlines()[0].strip()
                out["note"] = f"обрыв вручную: {who[:80]}"
        except Exception:  # noqa: BLE001 — консоль может быть недоступна
            pass
    return out


# ---------------------------------------------------------------------------
# Слой B — репозиторий автотестов
# ---------------------------------------------------------------------------

REPO_DEFAULTS = {
    "bitbucket_base_url": "https://example.invalid",
    "autotests_project": "AUTOTEST",
    "autotests_repo": "sfa",
    "autotests_branch": "master",
    "path": "",  # resolve_source требует ключ; для ls-tree подкаталог не нужен
}


def repo_index(root, cfg, args):
    """Карта «номер -> [пути .feature]» по дереву ветки автотестов.

    Тянется один раз (`git ls-tree`), блобы не скачиваются.
    """
    src = git_data.resolve_source(cfg, REPO_DEFAULTS, {
        "autotests_branch": args.repo_branch,
        "bitbucket_base_url": args.bitbucket_url,
        "autotests_project": args.repo_project,
        "autotests_repo": args.repo_repo,
    })
    url = git_data.repo_url(src["bitbucket_base_url"], src["autotests_project"], src["autotests_repo"])
    cache = git_data.cache_dir(root, src["autotests_project"], src["autotests_repo"],
                               src["autotests_branch"])
    paths = git_data.list_tree(cache, url, src["autotests_branch"], timeout=args.timeout)
    index = {}
    for p in paths:
        if not p.endswith(".feature"):
            continue
        name = Path(p).stem
        m = NUMBER_RE.match(name)
        if m:
            index.setdefault(m.group(1), []).append(p)
    return index


def repo_verdict(index, number):
    """Что говорит репозиторий про тест: путь, отключён ли, есть ли файл.

    ⚠️ Отсутствие файла НЕ доказывает отсутствие теста: номер может жить тегом
    внутри чужого `.feature` (в том числе в step-файле или папке другой команды).
    Поэтому «нет файла» — это ориентир для ручной проверки, а не диагноз.
    """
    paths = index.get(number) or []
    if not paths:
        return D_NO_FILE, None
    live = [p for p in paths if "/excluded/" not in p and not p.startswith("features/excluded/")]
    if live:
        return None, live[0]
    return D_EXCLUDED_REPO, paths[0]


# ---------------------------------------------------------------------------
# Сборка отчёта
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description="Диагностика пропусков прогона (почему тест не запускался)")
    parser.add_argument("--launch-id", type=int, required=True)
    parser.add_argument("--team", default=None, help="подпапка команды в launches/{id}/")
    parser.add_argument("--root", default=None)
    parser.add_argument("--no-jenkins", action="store_true", help="пропустить слой Jenkins")
    parser.add_argument("--no-repo", action="store_true", help="пропустить слой репозитория")
    parser.add_argument("--repo-branch", default=None)
    parser.add_argument("--repo-project", default=None)
    parser.add_argument("--repo-repo", default=None)
    parser.add_argument("--bitbucket-url", default=None)
    parser.add_argument("--jenkins-url", default=None)
    parser.add_argument("--user", default=None)
    parser.add_argument("--token", default=None)
    parser.add_argument("--opencode-json", default=str(Path.home() / ".config" / "opencode" / "opencode.json"))
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args(argv)

    root = Path(args.root).expanduser() if args.root else _paths.artifacts_root()

    launch_dir = root / "launches" / str(args.launch_id)
    if args.team:
        launch_dir = launch_dir / args.team
    elif not (launch_dir / "scan_manifest.json").exists():
        subs = [p for p in launch_dir.iterdir() if (p / "scan_manifest.json").exists()] \
            if launch_dir.is_dir() else []
        if len(subs) == 1:
            launch_dir = subs[0]
        elif len(subs) > 1:
            print("ОШИБКА: несколько команд в запуске, укажи --team " +
                  ", ".join(sorted(p.name for p in subs)))
            return 2
    manifest = load_json(launch_dir / "scan_manifest.json")
    if not manifest:
        print(f"ОШИБКА: нет scan_manifest.json в {launch_dir} — сначала выполни скан")
        return 2

    missing = [str(n) for n in (manifest.get("etalon_missing_numbers") or [])]
    if not missing:
        print("Полнота в норме: все тесты эталона участвовали в прогоне — диагностировать нечего.")
        return 0

    print(f"Пропусков: {len(missing)} из {manifest.get('etalon_total')} тестов эталона")
    print(f"Артефакт: {launch_dir}")
    print()

    cfg = st.load_config(root)
    token = st.resolve_token(cfg)

    # --- слой A: своя сборка ---
    jr_status = {"checked": False}
    if not args.no_jenkins:
        try:
            jobrun = _jobrun_of_launch(cfg, token, args.launch_id)
        except Exception as e:  # noqa: BLE001
            jobrun = None
            jr_status["note"] = f"jobRun не получен: {str(e)[:120]}"
        if jobrun:
            job, number = _parse_external_id(jobrun.get("externalId"))
            jr_status["checked"] = True
            jr_status["external_id"] = jobrun.get("externalId")
            jr_status["job"] = job
            jr_status["number"] = number
            jr_status["stage"] = jobrun.get("stage")
            jr_status["status"] = jobrun.get("status")
            creds = dispatch.load_credentials(args)
            if creds.get("url"):
                jr_status.update(layer_jenkins(creds, job, number))
            else:
                jr_status["note"] = "креды Jenkins не настроены — статус сборки не проверен"
        print("── слой A: своя сборка ──")
        if jr_status.get("checked"):
            print(f"  джоба:   {jr_status.get('external_id')}")
            jstatus = jr_status.get("status")
            if jstatus == "ok":
                print(f"  статус:  {jr_status.get('result') or jr_status.get('status')}"
                      + (f"  ({jr_status['note']})" if jr_status.get("note") else ""))
                tags = jr_status.get("params_tags")
                print("  tags:    " + (f"переданы, {len(str(tags).split())} шт." if tags
                                       else "не передавались (или сборка не публикует параметры)"))
            elif jstatus == "missing":
                print(f"  статус:  {jr_status.get('note')} — статус не проверить")
                print("  tags:    неизвестно (сборки нет в Jenkins)")
            else:
                print(f"  статус:  не проверено — {jr_status.get('note')}")
        else:
            print(f"  не проверено: {jr_status.get('note', 'jobRun отсутствует')}")
        print()

    # --- слой B: репозиторий ---
    index = {}
    repo_note = ""
    if not args.no_repo:
        print("── слой B: репозиторий автотестов ──")
        try:
            index = repo_index(root, cfg, args)
            print(f"  .feature файлов с номером: {sum(len(v) for v in index.values())}"
                  f" (уникальных номеров {len(index)})")
        except git_data.GitError as e:
            repo_note = f"git: {e}"
            print(f"  не проверено: {repo_note}")
        except Exception as e:  # noqa: BLE001
            repo_note = f"{type(e).__name__}: {str(e)[:120]}"
            print(f"  не проверено: {repo_note}")
        print()

    # --- итог по каждому пропуску ---
    requested = None
    if jr_status.get("params_tags"):
        requested = {t.lstrip("@") for t in str(jr_status["params_tags"]).split()}

    rows = []
    for number in missing:
        diag, path = (None, None)
        if index:
            diag, path = repo_verdict(index, number)
        # Приоритет: доказанная причина (репозиторий/сборка) -> иначе «не установлено».
        # «Неизвестно» не подменяется догадкой: статус сборки может быть недоступен
        # (ротация логов), и тогда причина честно помечается как неустановленная.
        if diag == D_EXCLUDED_REPO:
            verdict = D_EXCLUDED_REPO
        elif diag == D_NO_FILE:
            verdict = D_NO_FILE
        elif requested is not None and number not in requested:
            verdict = D_NOT_REQUESTED
        elif jr_status.get("aborted"):
            verdict = D_BUILD_ABORTED
        elif str(jr_status.get("result") or "").upper() == "FAILURE":
            verdict = D_BUILD_FAILED
        elif requested is not None and number in requested:
            verdict = D_NOT_REACHED
        elif path:
            verdict = D_UNPROVEN
        else:
            verdict = D_UNKNOWN
        rows.append({"number": number, "verdict": verdict, "feature": path})

    order = [D_EXCLUDED_REPO, D_NO_FILE, D_NOT_REQUESTED,
             D_BUILD_ABORTED, D_BUILD_FAILED, D_NOT_REACHED, D_UNPROVEN, D_UNKNOWN]
    by_verdict = Counter(r["verdict"] for r in rows)

    print("── итог по пропускам ──")
    print("| Причина | Тестов | Что делать |")
    print("|---|---:|---|")
    advice = {
        D_EXCLUDED_REPO: "вернуть файл в features/Tests или убрать тест из эталона",
        D_NO_FILE: "проверить вручную: номер может быть тегом внутри другого feature",
        D_NOT_REQUESTED: "добавить номера в tags сборки",
        D_BUILD_ABORTED: "перезапустить и НЕ прерывать сборку",
        D_BUILD_FAILED: "разобрать падение сборки, затем перезапустить",
        D_NOT_REACHED: "перезапустить (тег передан, сценарий не дошёл)",
        D_UNPROVEN: "перезапустить их отдельно и смотреть результат",
        D_UNKNOWN: "проверить вручную",
    }
    for v in order:
        if by_verdict.get(v):
            print(f"| {v} | {by_verdict[v]} | {advice[v]} |")
    print()

    for v in order:
        nums = [r["number"] for r in rows if r["verdict"] == v]
        if nums:
            print(f"{v} ({len(nums)}):")
            print(" ".join("@" + n for n in nums))
            print()

    artifact = launch_dir / "skip_diagnosis.json"
    payload = {
        "launch_id": args.launch_id,
        "team_dir": launch_dir.name,
        "etalon_total": manifest.get("etalon_total"),
        "missing_count": len(missing),
        "jenkins": jr_status,
        "repo_checked": bool(index),
        "repo_note": repo_note,
        "verdicts": dict(by_verdict),
        "tests": rows,
    }
    artifact.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"json: {artifact}")

    if by_verdict.get(D_UNKNOWN):
        print()
        print(f"ВНИМАНИЕ: {by_verdict[D_UNKNOWN]} тестов без установленной причины — "
              "причина не выдумывается, нужна ручная проверка.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

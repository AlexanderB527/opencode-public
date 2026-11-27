#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""publish_results.py — публикация результата разбора прогона в ветку данных.

Один файл = один прогон одной команды. Данные кладутся в ветку `qa-analysis-data`
репозитория `AUTOTEST/sfa`:

    results/<TEAM>/launches/<launch_id>_<тип>.json   сырьё: статистика + тесты + причины
    results/<TEAM>/causes.jsonl                     агрегат причин (накопительный)
    results/<TEAM>/index.json                       компакт для агента (последние прогоны + топ причин)

Зачем: `~/.qa-artifacts` живёт у каждого оператора отдельно, знание не накапливается.
Ветка даёт общую базу: агент читает `index.json` за один вызов и видит, что уже
разбиралось (типовой фикс, владелец, частота), а не начинает с нуля.

🔴 Причины падений (`causes`) заполняются ТОЛЬКО фазами rerun/deep:
   - `suspected` — из классификации рерана (message + поведение на перезапуске);
   - `confirmed` — когда есть подтверждение (git-локус, дефект, ручная проверка);
   - `refuted` — опровергнутые (остаются в базе, но не идут в топ).
   На первичном скане массив `causes` пуст: скан знает только статусы и сообщения,
   а выдавать догадку за причину нельзя (иначе база отравится собственными гипотезами).

Использование:
  # локально собрать и посмотреть, ничего не пушить
  py -X utf8 publish_results.py --team TX-T9 --type QG --launch-id 1187354 --dry-run

  # собрать (включая найденный автоматически реран) и опубликовать
  py -X utf8 publish_results.py --team TX-T9 --type QG --launch-id 1187354 --push

Коды возврата: 0 — ок; 1 — ошибка сборки/валидации; 2 — нет доступа к Bitbucket;
3 — нарушен гейт полноты скана (нужен --allow-incomplete).
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import git_data  # noqa: E402
import doctor  # noqa: E402
import team_canon  # noqa: E402
from _paths import ARTIFACTS_ROOT, CONFIG_DIR, RESULTS_DIR  # noqa: E402

SCHEMA_VERSION = 1
INDEX_LIMIT = 10          # сколько прогонов показывать в index.json
CAUSES_TOP_LIMIT = 15     # сколько кластеров причин показывать в index.json
LAUNCHES_PER_CAUSE = 10   # сколько launch_id хранить в одной записи causes.jsonl
MESSAGE_LIMIT = 300       # обрезка сообщений (ПДн/секреты + читаемость)

# Статусы TestOps -> enum контракта run_result.schema.json
STATUS_TO_SCHEMA = {
    "passed": "PASS",
    "failed": "FAIL",
    "broken": "BROKEN",
    "skipped": "SKIP",
}

RESULTS_SOURCE_DEFAULTS = {
    "bitbucket_base_url": "https://example.invalid",
    "project": "AUTOTEST",
    "repo": "sfa",
    "branch": "qa-analysis-data",
    "path": "results",
}


class PublishError(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Чтение артефактов прогона
# ---------------------------------------------------------------------------

def read_json(path, required=False, default=None):
    p = Path(path)
    if not p.exists():
        if required:
            raise PublishError(f"нет файла: {p}")
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise PublishError(f"не читается {p}: {e}")


def canon_team(value, registry):
    """Канон команды: TX-T9 для T9/TX/T9-TX; иначе — нормализованное имя в верхнем регистре."""
    return team_canon.resolve_canon(value, registry) or team_canon.normalize(value)


def load_registry():
    path = CONFIG_DIR / "team_jobs.json"
    return read_json(path, default={}) or {}


def find_team_dir(root, launch_id, wanted_team, registry):
    """Папка команды внутри launches/{id}: сравниваем по канону, а не по имени."""
    base = Path(root) / "launches" / str(launch_id)
    if not base.is_dir():
        raise PublishError(f"нет папки запуска: {base}")
    wanted = canon_team(wanted_team, registry)
    for candidate in sorted(p for p in base.iterdir() if p.is_dir()):
        if canon_team(candidate.name, registry) == wanted:
            return candidate, wanted
    existing = ", ".join(sorted(p.name for p in base.iterdir() if p.is_dir())) or "—"
    raise PublishError(f"в {base} нет папки команды {wanted} (есть: {existing})")


def find_rerun(root, launch_id, team):
    """Последний реран этого запуска для команды: (classification, diff) или (None, None)."""
    root = Path(root) / "reruns"
    if not root.is_dir():
        return None, None
    best = None
    for d in sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name):
        classification = d / "rerun_classification.json"
        if not classification.exists():
            continue
        try:
            data = read_json(classification)
        except PublishError:
            continue
        if int(data.get("expected_launch_id") or 0) != int(launch_id):
            continue
        teams = [str(t) for t in (data.get("teams") or [])]
        if teams and team not in {canon_team(t, None) for t in teams}:
            continue
        rerun_id = int(data.get("rerun_launch_id") or d.name)
        if best is None or rerun_id > best[0]:
            best = (rerun_id, data, read_json(d / "rerun_diff.json"))
    if best is None:
        return None, None
    return best[1], best[2]


def truncate(text, limit=MESSAGE_LIMIT):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def mtime_iso(path):
    """Время последнего изменения артефакта в ISO-8601 (стабильно при повторной публикации)."""
    p = Path(path)
    if p.exists():
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(p.stat().st_mtime))
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------------------
# Сборка документа результата
# ---------------------------------------------------------------------------

def build_cause(item, cluster, rerun_outcome, today):
    """Одна причина падения из item классификатора + привязка к кластеру."""
    evidence = []
    message = truncate(item.get("message"))
    if message:
        evidence.append(f"message:{message}")
    if rerun_outcome:
        evidence.append(f"rerun:{rerun_outcome}")
    for ev in (item.get("evidence_list") or []):
        if isinstance(ev, dict):
            kind = ev.get("kind") or "evidence"
            ref = ev.get("ref") or ev.get("note") or ""
            evidence.append(f"{kind}:{ref}" if ref else str(kind))
        elif ev:
            evidence.append(str(ev))
    plain_ev = item.get("evidence")
    if isinstance(plain_ev, str) and plain_ev.strip():
        evidence.append(f"evidence:{truncate(plain_ev, 120)}")

    if item.get("confirmed") is True:
        status = "confirmed"
    elif str(item.get("verdict") or "").lower() == "refuted":
        status = "refuted"
    else:
        status = "suspected"

    return {
        "category": item.get("category") or "unknown",
        "cluster": (cluster or {}).get("key") or item.get("error_class") or "unknown",
        "cluster_label": (cluster or {}).get("label") or item.get("error_class") or "",
        "owner": item.get("owner") or (cluster or {}).get("owner") or "",
        "confidence": item.get("confidence") or "low",
        "status": status,
        "summary": message,
        "root_cause": item.get("root_cause") or "",
        "fix": item.get("action") or item.get("recommendation") or "",
        "evidence": evidence,
        "first_seen": today,
        "last_seen": today,
        "seen_count": 1,
    }


def semantic_cluster(message, rules):
    """Семантический кластер по message (та же логика, что classify_rerun.semantic_cluster_of)."""
    text = message or ""
    for sc in (rules or {}).get("semantic_clusters") or []:
        for needle in (sc.get("match") or []):
            if needle and needle in text:
                return {
                    "key": sc.get("key") or "",
                    "label": sc.get("label") or sc.get("key") or "",
                    "owner": sc.get("owner") or "",
                    "category": sc.get("category") or "",
                }
    return {}


def cluster_for_number(clusters, number, category, message, rules):
    """Кластер, в который попал номер; при нескольких — совпадающий по категории.

    Порядок: clusters из rerun_classification.json → семантический матч по message
    (stable-ключ вида locator_unfound) → error_class как последний фолбэк.
    """
    with_at = number if number.startswith("@") else "@" + number
    fallback = None
    for cl in clusters or []:
        numbers = [str(n) for n in (cl.get("numbers") or [])]
        if with_at in numbers or number in numbers:
            if cl.get("category") == category:
                return cl
            fallback = fallback or cl
    if fallback:
        return fallback
    return semantic_cluster(message, rules)


def build_document(team, test_type, launch_id, team_dir, expected, manifest, nonpass_rows,
                   classification, diff, allow_incomplete, rules=None,
                   scanned_at=None, rerun_at=None):
    counts = expected.get("counts") or {}
    complete = bool((manifest or {}).get("complete", True))
    tail_checked = bool((manifest or {}).get("tail_checked", False))
    if not complete and not allow_incomplete:
        raise PublishError(
            f"скан неполный (scan_manifest.complete=false) — публиковать нельзя: "
            f"цифры недостоверны. Пересканируй или повтори с --allow-incomplete", code=3)

    today = scanned_at or mtime_iso(team_dir / "scan_manifest.json")
    expected_numbers = [str(n).lstrip("@") for n in (expected.get("expected_numbers") or [])]
    statuses = expected.get("statuses") or {}

    # Фаза 2: что случилось на перезапуске
    rerun_outcome = {}
    healed_numbers = set()
    if diff:
        for row in (diff.get("failed_again") or []):
            rerun_outcome[str(row.get("number", "")).lstrip("@")] = "failed_again"
        for row in (diff.get("healed") or []):
            number = str(row.get("number", "") if isinstance(row, dict) else row).lstrip("@")
            rerun_outcome[number] = "healed"
            healed_numbers.add(number)
        for row in (diff.get("skipped") or []):
            rerun_outcome[str(row.get("number", "")).lstrip("@")] = "skipped"
        for number in (diff.get("absent") or []):
            rerun_outcome[str(number).lstrip("@")] = "absent"

    # Причины: только из классификации рерана
    causes_by_number = {}
    if classification:
        clusters = classification.get("clusters") or []
        for item in (classification.get("items") or []):
            number = str(item.get("number", "")).lstrip("@")
            if not number:
                continue
            cluster = cluster_for_number(clusters, number, item.get("category"),
                                         item.get("message"), rules)
            cause = build_cause(item, cluster, rerun_outcome.get(number), today)
            bucket = causes_by_number.setdefault(number, [])
            # дубли причин внутри теста схлопываем по (category, cluster)
            if not any(c["category"] == cause["category"] and c["cluster"] == cause["cluster"]
                       for c in bucket):
                bucket.append(cause)
            else:
                for c in bucket:
                    if c["category"] == cause["category"] and c["cluster"] == cause["cluster"]:
                        c["seen_count"] += 1

    # Сообщения из сырого jsonl — как evidence, не как причина
    raw_messages = {}
    for row in nonpass_rows:
        number = str(row.get("number", "")).lstrip("@")
        for message in (row.get("messages") or []):
            if message:
                raw_messages.setdefault(number, []).append(truncate(message, 160))

    tests = []
    causes_summary = {}
    for number in expected_numbers:
        info = statuses.get(number) or {}
        state = str(info.get("status") or "").lower()
        schema_status = STATUS_TO_SCHEMA.get(state)
        if schema_status is None:
            continue  # пустой/неизвестный статус — в файл не попадает
        # PASSED включаем только как «вылечившийся» (иначе это не не-PASS тест)
        if schema_status == "PASS" and number not in healed_numbers:
            continue
        numbers_statuses = [str(s).lower() for s in (info.get("statuses") or [])]
        causes = causes_by_number.get(number, [])
        entry = {
            "number": number,
            "status": "PASS" if number in healed_numbers else schema_status,
            "results": int(info.get("count") or len(numbers_statuses) or 0),
            "nonpass": sum(1 for s in numbers_statuses if s != "passed"),
            "rerun": rerun_outcome.get(number, "not_rerun"),
            "causes": causes,
        }
        if raw_messages.get(number):
            entry["messages"] = raw_messages[number][:3]
        tests.append(entry)
        for cause in causes:
            causes_summary[cause["category"]] = causes_summary.get(cause["category"], 0) + 1

    stats = {
        "total": int(counts.get("total") or 0),
        "passed": int(counts.get("passed") or 0),
        "failed": int(counts.get("failed") or 0),
        "broken": int(counts.get("broken") or 0),
        "skipped": int(counts.get("skipped") or 0),
        "nonpass": int(counts.get("nonpass") or 0),
        "not_found": max(0, len(expected_numbers) - len(statuses)),
        "new_tests": len((manifest or {}).get("new_test_numbers") or []),
    }
    if counts.get("pass_rate") is not None:
        stats["pass_rate"] = counts["pass_rate"]

    launch_block = {
        "id": int(launch_id),
        "name": expected.get("launch_name") or "",
        "scanned_at": today,
        "complete": complete,
        "tail_checked": tail_checked,
        "new_tests": stats["new_tests"],
        "artifact": f"launches/{launch_id}/{team_dir.name}",
    }
    overlap = (manifest or {}).get("etalon_coverage_pct")
    if overlap is not None:
        launch_block["etalon_overlap_pct"] = overlap

    rerun_block = None
    if classification:
        rerun_counts = classification.get("counts") or {}
        rerun_counts.pop("gate_detail", None)
        rerun_block = {
            "launch_id": int(classification.get("rerun_launch_id") or 0),
            "analysed_at": rerun_at or today,
            "gate_ok": bool(rerun_counts.get("gate_ok", True)),
            "counts": rerun_counts,
            "healed": len(diff.get("healed") or []) if diff else 0,
            "absent": len(diff.get("absent") or []) if diff else 0,
            "skipped": len(diff.get("skipped") or []) if diff else 0,
        }

    document = {
        "schema_version": SCHEMA_VERSION,
        "team": team,
        "test_type": test_type,
        "launch": launch_block,
        "rerun": rerun_block,
        "stats": stats,
        "causes_summary": causes_summary,
        "tests": tests,
    }
    return document


# ---------------------------------------------------------------------------
# Накопительная база причин и индекс
# ---------------------------------------------------------------------------

def parse_causes(payload):
    """causes.jsonl (bytes|None) → dict[(number, cluster)] → запись.

    Записи без `number` (артефакты ранней версии) не теряем: кладём под ключ
    ("", cluster), чтобы они не затирали корректные и не раздували счётчики.
    """
    if not payload:
        return {}
    text = payload.decode("utf-8") if isinstance(payload, bytes) else str(payload)
    store = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        number = str(row.get("number") or "")
        cluster = str(row.get("cluster") or "")
        if not cluster:
            continue
        store[(number, cluster)] = row
    return store


_STATUS_RANK = {"refuted": 0, "suspected": 1, "confirmed": 2}


def merge_cause(previous, new, launch_id, today):
    """Сливает наблюдение причины с историей: счётчик, даты, evidence, статус."""
    if previous is None:
        merged = dict(new)
        merged["launches"] = [int(launch_id)]
        return merged

    merged = dict(previous)
    launches = [int(x) for x in (previous.get("launches") or [])]
    if int(launch_id) not in launches:
        launches.append(int(launch_id))
        merged["seen_count"] = int(previous.get("seen_count") or 0) + int(new.get("seen_count") or 1)
    merged["launches"] = launches[-LAUNCHES_PER_CAUSE:]
    merged["first_seen"] = previous.get("first_seen") or new.get("first_seen") or today
    merged["last_seen"] = today

    # Статус: подтверждение не понижается гипотезой; refuted фиксируется отдельно
    prev_status = str(previous.get("status") or "suspected")
    new_status = str(new.get("status") or "suspected")
    if new_status == "refuted":
        merged["status"] = "refuted"
    elif prev_status == "refuted" and new_status != "confirmed":
        merged["status"] = "refuted"
    elif prev_status == "confirmed" or new_status == "confirmed":
        merged["status"] = "confirmed"
    else:
        merged["status"] = new_status

    evidence = list(previous.get("evidence") or [])
    for ev in (new.get("evidence") or []):
        if ev not in evidence:
            evidence.append(ev)
    merged["evidence"] = evidence[:12]
    for field in ("cluster_label", "owner", "fix", "root_cause", "category", "confidence", "summary"):
        if new.get(field):
            merged[field] = new[field]
    return merged


def update_kb(existing_causes, document, launch_id):
    """Обновляет накопительную базу причин документом прогона.

    Ключ хранилища — (номер теста, кластер): у одного теста в истории может быть
    несколько причин (в разных прогонах падает по-разному). Поле `number` пишется
    в саму запись — иначе при перечитывании jsonl из ветки ключ не восстановить.
    """
    today = document["launch"]["scanned_at"]
    store = dict(existing_causes)
    for test in document["tests"]:
        for cause in test["causes"]:
            key = (test["number"], cause["cluster"])
            merged = merge_cause(store.get(key), cause, launch_id, today)
            merged["number"] = test["number"]
            store[key] = merged
    return store


def causes_to_jsonl(store):
    rows = sorted(store.values(),
                  key=lambda r: (-int(r.get("seen_count") or 0), str(r.get("number")), str(r.get("cluster"))))
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode("utf-8")


def update_index(existing_index, document, causes_store):
    """Пересобирает index.json: последние прогоны + топ причин."""
    index = dict(existing_index or {})
    launches = [x for x in (index.get("launches") or [])
                if not (int(x.get("launch_id") or 0) == int(document["launch"]["id"])
                        and x.get("test_type") == document["test_type"])]
    launches.append({
        "launch_id": int(document["launch"]["id"]),
        "test_type": document["test_type"],
        "launch_name": document["launch"].get("name", ""),
        "scanned_at": document["launch"]["scanned_at"],
        "file": f"launches/{document['launch']['id']}_{document['test_type']}.json",
        "stats": {
            "total": document["stats"]["total"],
            "nonpass": document["stats"]["nonpass"],
            "pass_rate": document["stats"]["pass_rate"],
            "tests_with_causes": sum(1 for t in document["tests"] if t["causes"]),
        },
        "rerun_launch_id": (document.get("rerun") or {}).get("launch_id"),
        "causes": document.get("causes_summary") or {},
    })
    launches.sort(key=lambda x: (str(x.get("scanned_at") or ""), int(x.get("launch_id") or 0)),
                  reverse=True)

    active = [c for c in causes_store.values() if c.get("status") != "refuted"]
    buckets = {}
    for cause in active:
        key = cause.get("cluster") or "unknown"
        bucket = buckets.setdefault(key, {
            "cluster": key,
            "label": cause.get("cluster_label") or key,
            "category": cause.get("category") or "",
            "owner": cause.get("owner") or "",
            "status": cause.get("status") or "suspected",
            "occurrences": 0,
            "tests": 0,
            "last_seen": "",
            "fix": cause.get("fix") or "",
        })
        bucket["occurrences"] += int(cause.get("seen_count") or 1)
        bucket["tests"] += 1
        if str(cause.get("last_seen") or "") > bucket["last_seen"]:
            bucket["last_seen"] = cause.get("last_seen") or ""
        if _STATUS_RANK.get(cause.get("status"), 0) > _STATUS_RANK.get(bucket["status"], 0):
            bucket["status"] = cause.get("status")
    causes_top = sorted(buckets.values(),
                        key=lambda b: (-b["occurrences"], -b["tests"], b["cluster"]))[:CAUSES_TOP_LIMIT]

    return {
        "schema_version": SCHEMA_VERSION,
        "team": document["team"],
        "updated_at": document["launch"]["scanned_at"],
        "totals": {
            "launches": len(launches),
            "causes_known": len(causes_store),
            "causes_active": len(active),
        },
        "launches": launches[:INDEX_LIMIT],
        "causes_top": causes_top,
    }


# ---------------------------------------------------------------------------
# Запись: локальный стейджинг и публикация
# ---------------------------------------------------------------------------

def write_local(dest, team, document, causes_bytes, index):
    team_dir = Path(dest) / team
    launch_file = team_dir / "launches" / f"{document['launch']['id']}_{document['test_type']}.json"
    launch_file.parent.mkdir(parents=True, exist_ok=True)
    launch_file.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (team_dir / "causes.jsonl").write_bytes(causes_bytes)
    (team_dir / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n",
                                         encoding="utf-8")
    return {
        "launch": launch_file,
        "causes": team_dir / "causes.jsonl",
        "index": team_dir / "index.json",
    }


def publish(src, team, document, causes_bytes, index, timeout, create_branch=False):
    """Публикует три файла одним git-коммитом в ветку данных.

    REST-правка файлов в этом репозитории запрещена repository hook'ом, поэтому
    работаем через git (`git_data.push_files`): shallow-fetch ветки во временный
    каталог → запись файлов → commit → push.
    """
    launch_id = document["launch"]["id"]
    test_type = document["test_type"]
    prefix = f"{src['path']}/{team}"
    files = {
        f"{prefix}/launches/{launch_id}_{test_type}.json":
            (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        f"{prefix}/causes.jsonl": causes_bytes,
        f"{prefix}/index.json":
            (json.dumps(index, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    }
    message = f"{team}: разбор запуска {launch_id} ({test_type})"
    commit = git_data.push_files(src["repo_url"], src["branch"], files, message, timeout,
                                 create_if_missing=create_branch)
    return [{"path": path, "bytes": len(data), "commit": commit} for path, data in files.items()]


def load_base(dest, root, src, team, args):
    """Читает накопительную базу команды: из ветки (git) или из локального стейджинга.

    Возвращает (causes_store, index_dict, from_branch).
    """
    prefix = f"{src['path']}/{team}"
    existing_causes, existing_index = {}, {}
    if args.push and not args.no_fetch:
        cache = git_data.cache_dir(root, src["project"], src["repo"], src["branch"])
        try:
            payloads = git_data.read_files(
                cache, src["repo_url"], src["branch"], src["path"],
                [f"{prefix}/causes.jsonl", f"{prefix}/index.json"], args.timeout)
        except git_data.GitError as e:
            if e.code == 4:  # ветки ещё нет — первая публикация
                payloads = {}
            else:
                raise
        existing_causes = parse_causes(payloads.get(f"{prefix}/causes.jsonl"))
        index_payload = payloads.get(f"{prefix}/index.json")
        if index_payload:
            try:
                existing_index = json.loads(index_payload.decode("utf-8"))
            except json.JSONDecodeError:
                existing_index = {}
        return existing_causes, existing_index, True

    local_team = Path(dest) / team
    existing_causes = parse_causes(
        (local_team / "causes.jsonl").read_bytes() if (local_team / "causes.jsonl").exists() else None)
    existing_index = read_json(local_team / "index.json", default={}) or {}
    return existing_causes, existing_index, False


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    parser = argparse.ArgumentParser(
        description="Публикация результата разбора прогона в ветку данных qa-analysis-data")
    parser.add_argument("--team", required=True, help="команда: TX-T9, T9, TREX, ...")
    parser.add_argument("--type", required=True, choices=["HF", "QG", "RM", "hf", "qg", "rm"])
    parser.add_argument("--launch-id", type=int, required=True, help="id первичного прогона")
    parser.add_argument("--rerun-id", type=int, default=None,
                        help="id рерана (по умолчанию — последний найденный для этого запуска)")
    parser.add_argument("--root", default=None, help="корень артефактов (~/.qa-artifacts)")
    parser.add_argument("--dest", default=None, help="локальный стейджинг (по умолчанию <root>/results)")
    parser.add_argument("--branch", default=None, help="ветка данных (по умолчанию qa-analysis-data)")
    parser.add_argument("--project", default=None)
    parser.add_argument("--repo", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--path", default=None, help="каталог в ветке (по умолчанию results)")
    parser.add_argument("--repo-url", default=None,
                        help="URL репозитория для push (иначе собирается из base-url/project/repo)")
    parser.add_argument("--timeout", type=int, default=300, help="таймаут git-операции, сек")
    parser.add_argument("--dry-run", action="store_true",
                        help="собрать и записать локально, без сети")
    parser.add_argument("--push", action="store_true", help="опубликовать в ветку")
    parser.add_argument("--create-branch", action="store_true",
                        help="разрешить создать ветку, если её нет (защита от опечатки в имени)")
    parser.add_argument("--no-fetch", action="store_true",
                        help="не читать предыдущую базу из ветки (начать счётчики заново)")
    parser.add_argument("--allow-incomplete", action="store_true",
                        help="публиковать даже при scan_manifest.complete=false")
    parser.add_argument("--json", action="store_true", help="машинный вывод")
    args = parser.parse_args(argv)

    root = Path(args.root).expanduser().resolve() if args.root else ARTIFACTS_ROOT
    dest = Path(args.dest).expanduser().resolve() if args.dest else (RESULTS_DIR if not args.root
                                                                    else root / "results")
    test_type = args.type.upper()

    try:
        registry = load_registry()
        team_dir, team = find_team_dir(root, args.launch_id, args.team, registry)
        expected = read_json(team_dir / "expected_set.json", required=True)
        manifest = read_json(team_dir / "scan_manifest.json", default={}) or {}
        nonpass_rows = []
        jsonl = team_dir / "nonpass_results.jsonl"
        if jsonl.exists():
            for line in jsonl.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line:
                    try:
                        nonpass_rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

        if args.rerun_id:
            classification = read_json(root / "reruns" / str(args.rerun_id) / "rerun_classification.json")
            diff = read_json(root / "reruns" / str(args.rerun_id) / "rerun_diff.json")
            if classification is None:
                raise PublishError(f"нет rerun_classification.json для рерана {args.rerun_id}")
        else:
            classification, diff = find_rerun(root, args.launch_id, team)

        document = build_document(team, test_type, args.launch_id, team_dir, expected, manifest,
                                  nonpass_rows, classification, diff, args.allow_incomplete,
                                  rules=read_json(CONFIG_DIR / "classify_rules.json", default={}) or {},
                                  scanned_at=mtime_iso(team_dir / "scan_manifest.json"),
                                  rerun_at=(mtime_iso(root / "reruns" / str(
                                      (classification or {}).get("rerun_launch_id") or "")
                                      / "rerun_classification.json") if classification else None))

        schema_errors = doctor.validate_file(
            document, CONFIG_DIR / "contracts" / "run_result.schema.json")
        if schema_errors:
            raise PublishError("документ не проходит контракт run_result.schema.json:\n  - "
                               + "\n  - ".join(schema_errors[:8]))

        # Накопительная база: читаем из ветки через git (или из локального стейджинга)
        cfg = {}
        cfg_path = root / "config.json"
        if cfg_path.exists():
            try:
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                cfg = {}
        src = git_data.resolve_source(cfg, RESULTS_SOURCE_DEFAULTS, {
            "bitbucket_base_url": args.base_url,
            "project": args.project,
            "repo": args.repo,
            "branch": args.branch,
            "path": args.path,
        })
        src["repo_url"] = args.repo_url or cfg.get("results_repo_url") \
            or os.environ.get("QA_RESULTS_REPO_URL") \
            or git_data.repo_url(src["bitbucket_base_url"], src["project"], src["repo"])

        # Накопительная база + публикация. Повтор до 3 раз: гонка двух публикаций
        # одной команды даёт non-fast-forward — тогда перечитываем базу и считаем заново.
        attempts = 3 if args.push else 1
        paths, pushed, causes_store = None, [], {}
        for attempt in range(1, attempts + 1):
            existing_causes, existing_index, _from_branch = load_base(
                dest, root, src, team, args)
            causes_store = update_kb(existing_causes, document, args.launch_id)
            causes_bytes = causes_to_jsonl(causes_store)
            index = update_index(existing_index, document, causes_store)

            index_errors = doctor.validate_file(
                index, CONFIG_DIR / "contracts" / "results_index.schema.json")
            if index_errors:
                raise PublishError("индекс не проходит контракт results_index.schema.json:\n  - "
                                   + "\n  - ".join(index_errors[:8]))

            paths = write_local(dest, team, document, causes_bytes, index)
            if not args.push:
                break
            try:
                pushed = publish(src, team, document, causes_bytes, index, args.timeout,
                                 create_branch=args.create_branch)
                break
            except git_data.GitError as e:
                if e.code == 5 and attempt < attempts:
                    print(f"  [i] попытка {attempt}: {e} — повтор", file=sys.stderr)
                    continue
                raise

        if args.json:
            print(json.dumps({
                "team": team, "test_type": test_type, "launch_id": args.launch_id,
                "rerun_launch_id": (document.get("rerun") or {}).get("launch_id"),
                "stats": document["stats"],
                "causes_summary": document["causes_summary"],
                "tests": len(document["tests"]),
                "causes_known": len(causes_store),
                "local": {k: str(v) for k, v in paths.items()},
                "pushed": pushed,
            }, ensure_ascii=False, indent=2))
            return 0

        stats = document["stats"]
        print(f"=== Результат разбора: {team} / {test_type} / запуск {args.launch_id} ===")
        print(f"  статистика: total {stats['total']}, passed {stats['passed']}, "
              f"failed {stats['failed']}, broken {stats['broken']}, skipped {stats['skipped']}, "
              f"nonpass {stats['nonpass']}, pass_rate {stats['pass_rate']}")
        print(f"  complete={document['launch']['complete']} "
              f"tail_checked={document['launch']['tail_checked']} "
              f"not_found={stats['not_found']}")
        rerun = document.get("rerun")
        print(f"  реран: {('запуск ' + str(rerun['launch_id'])) if rerun else 'нет — причины не заполняются'}")
        print(f"  тестов в файле: {len(document['tests'])}, "
              f"с причинами: {sum(1 for t in document['tests'] if t['causes'])}")
        if document["causes_summary"]:
            print("  причины: " + ", ".join(f"{k}={v}" for k, v in document["causes_summary"].items()))
        print(f"  база причин: {len(causes_store)} записей")
        print(f"  локально: {paths['launch']}")
        if pushed:
            print("  опубликовано:")
            for item in pushed:
                print(f"    [OK] {item['path']} ({item['bytes']} б, {item['commit']})")
        else:
            print("  в ветку НЕ публиковалось (добавь --push)")
        return 0
    except PublishError as e:
        print(f"ОШИБКА: {e}", file=sys.stderr)
        return e.code
    except git_data.GitError as e:
        print(f"ОШИБКА git: {e}", file=sys.stderr)
        return e.code


if __name__ == "__main__":
    sys.exit(main())

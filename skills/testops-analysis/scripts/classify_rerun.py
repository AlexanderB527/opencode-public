#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
classify_rerun.py — ДЕТЕРМИНИРОВАННАЯ классификация снова-упавших тестов после rerun.

Слой 1 гибридной схемы rerun-разбора:
  - читает reruns/{rerun_id}/rerun_diff.json (секции failed_again/healed/absent/skipped);
  - для каждого снова-упавшего по реальному message присваивает:
      * error_class  — класс ошибки §5 (переиспользует classifier.py);
      * owner        — предварительный владелец: система | автотест | дефект | спор | триаж;
      * confidence   — high | medium | low;
      * action       — что делать (завести дефект / починить тест / отдать DevOps / триаж);
      * locator      — извлечённый xpath/хост:порт (для узкой git-проверки Слоя 2).
  - пишет компактный артефакт reruns/{rerun_id}/rerun_classification.json.
  - печатает матрицу + метрику покрытия (в режиме --chat — markdown страницы
    перезапуска из общего рендерера render_markdown.py).

LLM (узкий шаг) НЕ классифицирует заново: он (а) добивает ТОЛЬКО очередь
needs_evidence через git (bitbucket_grep по AUTOTEST/sfa), (б) выносит оставшиеся
спорные в триаж-список человеку.

Публикация в Confluence — ВНЕ этого скрипта: страницы рендерит render_markdown.py,
в Confluence их вставляет агент через MCP atlassian.

Использование:
  py -X utf8 classify_rerun.py --rerun-id 1201662 [--root .qa-artifacts]
Exit: 0 = ok, 1 = ошибка/неполный вход.
"""
import argparse
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from classifier import classify_error, load_rules, natural_key, with_at  # noqa: E402
from diff_rerun import scan_rerun  # noqa: E402
from render_markdown import render_classification  # noqa: E402
from _paths import ARTIFACTS_ROOT, RULES_PATH  # noqa: E402
from scan_testops import (  # noqa: E402
    NON_PASS_STATUSES,
    extract_message_stub,
    extract_test_number_stub,
    init_scan_state,
    load_config,
    resolve_token,
)

DEFAULT_ARTIFACTS_ROOT = ARTIFACTS_ROOT

# --- ТАКСОНОМИЯ и правила — ДАННЫЕ (из .qa-artifacts/classify_rules.json), не код. ---
# Значения ниже — fallback, если файла правил нет. Переопределяются через _apply_rules().
CATEGORIES = {
    "infra":  ("Инфраструктура (стенд/сеть недоступны)", "Отдать DevOps: поднять стенд/сеть"),
    "env":    ("Окружение (сервис отдал ошибку/таймаут)", "Отдать DevOps: проверить сервис"),
    "locator":("Автотест: не найден элемент (локатор)", "Починить тест: обновить локатор"),
    "testcode":("Автотест: ошибка кода теста", "Починить тест: исправить код/данные теста"),
    "product":("Дефект продукта", "Завести дефект"),
    "testdata":("Тестовые данные", "Проверить/обновить данные теста"),
    "unknown":("Не классифицировано (нужен триаж)", "Триаж: проверить локатор/данные (git)"),
}
CLASS_TO_CATEGORY = {
    "Таймаут выполнения": "infra", "Connection reset by peer": "infra",
    "500 Internal Server Error": "env",
    "Таймаут ожидания элемента": "locator", "Таймаут ожидания статуса": "locator",
    "Элемент не описан в коде": "testcode", "Неоднозначное совпадение": "locator",
    "Ошибка переключения фрейма": "testcode", "element click intercepted": "locator",
    "Ошибка тестовых данных": "testcode",
    "Несовпадение формата даты": "product", "Ошибка утверждения": "product",
    "Несовпадение данных": "product",
    "Проверка наличия в списке": "testdata", "Утверждение состояния элемента": "testdata",
    "Не удалось создать клиента в EQ и MDM": "testdata", "Другое": "unknown",
}
CATEGORY_TO_OWNER = {
    "infra": "система", "env": "система", "locator": "автотест", "testcode": "автотест",
    "product": "дефект", "testdata": "дефект", "unknown": "спор",
}
# V3: семантические кластеры падений (ДАННЫЕ из classify_rules.json "semantic_clusters").
# Пустой список = фолбэк (кластеризация недоступна → рендер по группам). Порядок = порядок проверки.
SEMANTIC_CLUSTERS: list[dict] = []
# Обратная совместимость: owner→действие (старый формат вердиктов/evidence)
ACTION_BY_OWNER = {
    "система": "Отдать DevOps / окружение",
    "автотест": "Починить тест",
    "дефект": "Завести дефект",
    "спор": "Триаж: проверить локатор/данные (git)",
}
DEFAULT_CONFIDENCE = {
    "infra": "high", "env": "high", "locator": "medium", "testcode": "high",
    "product": "medium", "testdata": "medium", "unknown": "low",
}
# V2: веса kind'ов для evidence-gated confidence (score = сумма весов всех kind).
EVIDENCE_WEIGHTS = {
    "message": 1, "steps": 1, "stability": 1,
    "git_locus": 2, "git_history": 2, "regression_commit": 2,
}
RCAUSE_MIN = 2
ROOT_CAUSE_PATTERNS = [
    ("host", re.compile(r"Failed to open TCP connection to\s+([\w.\-]+:\d+)")),
    ("xpath", re.compile(r'xpath\s+"((?:[^"\\]|\\.)*)"')),
    ("xpath", re.compile(r'(\.//(?:[^"\s\\]|\\.)+)')),
    ("css", re.compile(r'css\s+"([^"]+)"')),
    ("http", re.compile(r"(\b[45]\d{2}\b)\s*(?:Internal Server Error|ошибк|error)")),
]

ACTION_BY_CATEGORY = {k: v[1] for k, v in CATEGORIES.items()}
CATEGORY_LABEL = {k: v[0] for k, v in CATEGORIES.items()}


def apply_rules(rules: dict) -> None:
    """Применяет правила из JSON к модулю (данные → поведение, без правки кода)."""
    global CATEGORIES, CLASS_TO_CATEGORY, CATEGORY_TO_OWNER, DEFAULT_CONFIDENCE, RCAUSE_MIN, ROOT_CAUSE_PATTERNS
    global ACTION_BY_CATEGORY, CATEGORY_LABEL, SEMANTIC_CLUSTERS
    if rules.get("categories"):
        CATEGORIES = {k: tuple(v) for k, v in rules["categories"].items()}
    if rules.get("class_to_category"):
        CLASS_TO_CATEGORY = dict(rules["class_to_category"])
    if rules.get("category_to_owner"):
        CATEGORY_TO_OWNER = dict(rules["category_to_owner"])
    if rules.get("default_confidence"):
        DEFAULT_CONFIDENCE = dict(rules["default_confidence"])
    if rules.get("root_cause_patterns"):
        ROOT_CAUSE_PATTERNS = [(k, re.compile(p, re.DOTALL)) for k, p in rules["root_cause_patterns"]]
    if rules.get("rcause_min") is not None:
        RCAUSE_MIN = int(rules["rcause_min"])
    sc = rules.get("semantic_clusters")
    SEMANTIC_CLUSTERS = list(sc) if isinstance(sc, list) else []
    ACTION_BY_CATEGORY = {k: v[1] for k, v in CATEGORIES.items()}
    CATEGORY_LABEL = {k: v[0] for k, v in CATEGORIES.items()}


# --- Root-cause ключ: конкретика, которая дробит общий класс §5 на реальные причины ---
XPATH_RE = re.compile(r'xpath\s+"((?:[^"\\]|\\.)*)"')
HOSTPORT_RE = re.compile(r"(Failed to open TCP connection to\s+[\w.\-]+:\d+)")
GENERIC_XPATH_RE = re.compile(r'(\.//(?:[^"\s\\]|\\.)+)')

# --- Root-cause извлечение локатора (см. ROOT_CAUSE_PATTERNS) ---


def extract_locator(message: str) -> str:
    """Извлекает из message то, что надо проверить в git: xpath или хост:порт."""
    m = XPATH_RE.search(message or "")
    if m:
        return m.group(1)
    m = HOSTPORT_RE.search(message or "")
    if m:
        return m.group(1)
    m = GENERIC_XPATH_RE.search(message or "")
    if m:
        return m.group(1)
    return ""


def root_cause_key(message: str, error_class: str) -> str:
    """Дробящий ключ причины: 'host:host:port', 'xpath:<...>', 'http:500' или ''.

    Конкретные значения (даты, ФИО) НЕ дробят — только устойчивая конкретика
    (хост, локатор, HTTP-статус), иначе групп станет слишком много.
    """
    msg = message or ""
    for kind, rx in ROOT_CAUSE_PATTERNS:
        m = rx.search(msg)
        if m:
            val = m.group(1).strip()
            # нормализуем пробелы в xpath
            val = re.sub(r"\s+", " ", val)
            return f"{kind}:{val}"
    return ""


def category_of(error_class: str) -> str:
    return CLASS_TO_CATEGORY.get(error_class, "unknown")


def classify_items(failed_again: list[dict]) -> list[dict]:
    """Слой 1: по каждому снова-упавшему — класс §5, категория, confidence, действие, локатор."""
    items = []
    for fa in failed_again:
        number = with_at(fa.get("number"))
        status = str(fa.get("status") or "FAIL").upper()
        messages = [m for m in (fa.get("messages") or []) if m]
        msg = messages[0] if messages else ""
        error_class = classify_error(msg)
        category = category_of(error_class)
        confidence = DEFAULT_CONFIDENCE.get(category, "low")
        owner = CATEGORY_TO_OWNER.get(category, "спор")
        items.append({
            "number": number,
            "status": status,
            "error_class": error_class,
            "category": category,
            "owner": owner,
            "confidence": confidence,
            "action": ACTION_BY_CATEGORY.get(category, "Триаж"),
            "root_cause": root_cause_key(msg, error_class),
            "locator": extract_locator(msg),
            "message": msg[:300],
            "results": fa.get("results"),
            "nonpass": fa.get("nonpass"),
        })
    items.sort(key=lambda x: natural_key(x["number"]))
    return items


def group_items(items: list[dict]) -> list[dict]:
    """Двухуровневая группировка.

    Уровень 1 (крупные root cause): root_cause выделяется в отдельную группу, если его
    частота в рамках класса §5 >= RCAUSE_MIN (иначе — уникальные сливаются в класс).
    Это не даёт «30 групп по 1 тесту» и оставляет реальные причины (host:port, частый xpath).
    """
    # частоты root_cause в рамках класса
    freq: dict[tuple[str, str], int] = {}
    for it in items:
        if it["root_cause"]:
            key = (it["error_class"], it["root_cause"])
            freq[key] = freq.get(key, 0) + 1

    groups: dict[tuple, list[dict]] = {}
    for it in items:
        cls, cat = it["error_class"], it["category"]
        rc = it["root_cause"]
        # root cause в отдельную группу только если он «крупный»
        if rc and freq.get((cls, rc), 0) < RCAUSE_MIN:
            rc = ""
        groups.setdefault((cls, cat, rc), []).append(it)

    out = []
    for (cls, cat, rc), arr in groups.items():
        nums = sorted({x["number"] for x in arr}, key=natural_key)
        label = f"{cls} [{rc.split(':', 1)[-1]}]" if rc else cls
        if not rc and len(arr) >= 2 and any(x["root_cause"] for x in arr):
            label = f"{cls} [прочие]"
        out.append({
            "error_class": cls,
            "group_label": label,
            "category": cat,
            "category_label": CATEGORY_LABEL.get(cat, cat),
            "owner": CATEGORY_TO_OWNER.get(cat, "спор"),
            "confidence": arr[0]["confidence"],
            "action": ACTION_BY_CATEGORY.get(cat, "Триаж"),
            "root_cause": rc,
            "count": len(arr),
            "numbers": nums,
        })
    out.sort(key=lambda g: (-g["count"], g["group_label"]))
    return out


def semantic_cluster_of(item: dict) -> dict:
    """V3: семантический кластер item'а по первому совпадению подстроки из rule["match"].

    Совпадение — по подстроке в item["message"] КАК ЕСТЬ (регистрозависимо).
    Правила SEMANTIC_CLUSTERS упорядочены (порядок проверки — сверху вниз).
    Нет совпадения или нет правил → фолбэк: кластер = error_class/owner/category item'а.
    """
    msg = item.get("message") or ""
    for sc in SEMANTIC_CLUSTERS:
        for needle in (sc.get("match") or []):
            if needle and needle in msg:
                return {
                    "key": sc.get("key", ""),
                    "label": sc.get("label", sc.get("key", "")),
                    "owner": sc.get("owner", item.get("owner", "")),
                    "category": sc.get("category", item.get("category", "")),
                }
    return {
        "key": item.get("error_class", ""),
        "label": item.get("error_class", ""),
        "owner": item.get("owner", ""),
        "category": item.get("category", ""),
    }


def _subgroup_label(root_cause: str) -> str:
    """V4: человеко-читаемый label подгруппы по root_cause.

    'xpath:<...>' → селектор; 'host:<...>' → хост:порт; 'http:<...>' → статус;
    пусто/'' → 'прочее'.
    """
    rc = (root_cause or "").strip()
    if not rc:
        return "прочее"
    kind, _, val = rc.partition(":")
    val = val.strip()
    if not val:
        return "прочее"
    if kind == "http":
        return f"HTTP {val}"
    return val


def build_subgroups(arr: list[dict]) -> list[dict]:
    """V4: подгруппы внутри кластера по ТОЧНОМУ root_cause (xpath/host/http).

    Каждая: {root_cause, label, count, numbers, stable, flaky, new}. Пустой
    root_cause → подгруппа 'прочее'. Сортировка по убыванию count (потом label).
    """
    buckets: dict[str, list[dict]] = {}
    for it in arr:
        rc = str(it.get("root_cause") or "").strip()
        buckets.setdefault(rc, []).append(it)
    subs = []
    for rc, items_in in buckets.items():
        prev = [str(x.get("prev_status") or "") for x in items_in]
        subs.append({
            "root_cause": rc,
            "label": _subgroup_label(rc),
            "count": len(items_in),
            "numbers": sorted({x["number"] for x in items_in}, key=natural_key),
            "stable": sum(1 for p in prev if p in ("failed", "broken")),
            "flaky": sum(1 for p in prev if p == "passed"),
            "new": sum(1 for p in prev if p == "absent"),
        })
    subs.sort(key=lambda s: (-s["count"], s["label"]))
    return subs


def build_clusters(items: list[dict]) -> list[dict]:
    """V3: кластеризация items семантически (build_clusters) с авто-стабильностью.

    Каждый кластер: key/label/owner/category + count + разбивка по prev_status
    (stable=failed/broken, flaky=passed, new=absent, unknown_prev=""). Номера — natural_key.
    V4: у каждого кластера добавляется subgroups[] — разбивка по точному root_cause.
    Сортировка: по убыванию count, затем label.
    """
    buckets: dict[tuple, list[dict]] = {}
    meta: dict[tuple, dict] = {}
    for it in items:
        sc = semantic_cluster_of(it)
        k = (sc["key"], sc["label"], sc["owner"], sc["category"])
        buckets.setdefault(k, []).append(it)
        meta.setdefault(k, sc)

    out = []
    for k, arr in buckets.items():
        sc = meta[k]
        prev = [str(x.get("prev_status") or "") for x in arr]
        nums = sorted({x["number"] for x in arr}, key=natural_key)
        out.append({
            "key": sc["key"],
            "label": sc["label"],
            "owner": sc["owner"],
            "category": sc["category"],
            "count": len(arr),
            "stable": sum(1 for p in prev if p in ("failed", "broken")),
            "flaky": sum(1 for p in prev if p == "passed"),
            "new": sum(1 for p in prev if p == "absent"),
            "unknown_prev": sum(1 for p in prev if p == ""),
            "numbers": nums,
            "subgroups": build_subgroups(arr),
        })
    out.sort(key=lambda c: (-c["count"], c["label"]))
    return out


def compute_counts(items: list[dict], groups: list[dict]) -> dict:
    """Счётчики по категориям/владельцам/confidence + гейт сходимости."""
    by_cat: dict[str, int] = {}
    by_owner: dict[str, int] = {}
    by_conf: dict[str, int] = {}
    for it in items:
        by_cat[it["category"]] = by_cat.get(it["category"], 0) + 1
        by_owner[it["owner"]] = by_owner.get(it["owner"], 0) + 1
        by_conf[it["confidence"]] = by_conf.get(it["confidence"], 0) + 1
    total = len(items)
    confirmed = total - by_owner.get("спор", 0)
    # --- ГЕЙТ ВАЛИДНОСТИ: суммы групп == числу items; каждый номер ровно 1 раз ---
    group_sum = sum(g["count"] for g in groups)
    seen: list[str] = []
    for g in groups:
        seen.extend(g["numbers"])
    union = set(seen)
    valid = (group_sum == total) and (len(seen) == len(set(seen)) == len(union))
    return {
        "failed_again": total,
        "by_category": by_cat,
        "by_owner": by_owner,
        "by_confidence": by_conf,
        "confirmed_pct": round(confirmed / total * 100, 1) if total else 0.0,
        "disputed_pct": round(by_owner.get("спор", 0) / total * 100, 1) if total else 0.0,
        "gate_ok": valid,
        "gate_detail": {
            "group_sum": group_sum,
            "items": total,
            "unique_numbers": len(union),
            "duplicates": len(seen) - len(set(seen)),
        },
    }


def build_classification(diff: dict) -> dict:
    items = classify_items(diff.get("failed_again", []))
    groups = group_items(items)
    counts = compute_counts(items, groups)
    needs_evidence = [
        {"number": it["number"], "error_class": it["error_class"],
         "category": it["category"], "locator": it["locator"]}
        for it in items if it["category"] == "unknown"
    ]
    return {
        "rerun_launch_id": diff.get("rerun_launch_id"),
        "expected_launch_id": diff.get("expected_launch_id"),
        "teams": diff.get("teams", []),
        "stats_source": diff.get("stats", {}),
        "counts": counts,
        "groups": groups,
        "items": items,
        "needs_evidence": needs_evidence,
        "healed": diff.get("healed", []),
        "absent": diff.get("absent", []),
        "skipped": diff.get("skipped", []),
    }


def apply_evidence(result: dict, evidence: dict) -> dict:
    """Слой 2: применяет evidence от LLM (git-проверка) к спорным (unknown).

    evidence: {"@номер": {"verdict": "found"|"unfound"|"tool_unavailable",
                          "ref": "путь/файл:строка", "note": "..."}}
    Правило (детерминированное, применяется к unknown-группам):
      found    — локатор есть/актуален в коде → элемент есть, не прогрузилось → env (окружение);
      unfound  — локатор битый/устарел/опечатка → locator (починить тест);
      tool_unavailable → остаётся unknown (в триаж).
    Обновляет категорию/владельца и пересобирает группы/счётчики.
    """
    items = result["items"]
    for it in items:
        ev = evidence.get(it["number"])
        if not ev or it.get("category") != "unknown":
            continue
        verdict = str(ev.get("verdict") or "").lower()
        if verdict == "found":
            it["category"], it["confidence"] = "env", "medium"
        elif verdict == "unfound":
            it["category"], it["confidence"] = "locator", "medium"
        else:  # tool_unavailable / неизвестно — оставить unknown
            continue
        it["owner"] = CATEGORY_TO_OWNER.get(it["category"], "спор")
        it["action"] = ACTION_BY_CATEGORY.get(it["category"], "Триаж")
        it["evidence"] = {"verdict": verdict, "ref": ev.get("ref", ""), "note": ev.get("note", "")}

    result["items"] = items
    result["groups"] = group_items(items)
    result["counts"] = compute_counts(items, result["groups"])
    result["needs_evidence"] = [
        {"number": it["number"], "error_class": it["error_class"],
         "category": it["category"], "locator": it["locator"]}
        for it in items if it["category"] == "unknown"
    ]
    return result


def evidence_score(evidence: list) -> int:
    """V2: score = сумма весов kind по элементам evidence-списка (неизвестные kind = 0)."""
    score = 0
    for e in evidence or []:
        if isinstance(e, dict):
            score += EVIDENCE_WEIGHTS.get(str(e.get("kind") or ""), 0)
    return score


def confidence_from_evidence_score(score: int) -> str:
    """V2: high при score >= 4; medium при 2..3; иначе low."""
    if score >= 4:
        return "high"
    if score >= 2:
        return "medium"
    return "low"


def _apply_verdict_to_dict(target: dict, v: dict) -> None:
    """V4: единый writer вердикта в dict (кластер / группа / item).

    Пишет category→owner/action (если category задана), confidence (по evidence-списку
    либо из поля), verdict/reason/recommendation/ref/routing, evidence-гейт `confirmed`,
    а также опц. "steps"/"files" (расширенные многострочные пояснения, НЕ обрезаются).
    """
    ev_raw = v.get("evidence")
    ev_list = ev_raw if isinstance(ev_raw, list) else []
    ev_str = ev_raw if isinstance(ev_raw, str) else ""
    routing = v.get("routing") if isinstance(v.get("routing"), dict) else None
    if v.get("category") and v["category"] in CATEGORIES:
        target["category"] = v["category"]
        target["owner"] = CATEGORY_TO_OWNER.get(v["category"], target.get("owner", "спор"))
        target["action"] = ACTION_BY_CATEGORY.get(v["category"], target.get("action", "Триаж"))
    elif v.get("owner"):
        target["owner"] = v["owner"]
        target["action"] = ACTION_BY_OWNER.get(v["owner"], target.get("action", "Триаж"))
    # V2: список evidence → confidence по score; иначе — прежнее поле confidence.
    if ev_list:
        score = evidence_score(ev_list)
        target["evidence_score"] = score
        target["evidence_list"] = ev_list
        target["confidence"] = confidence_from_evidence_score(score)
    elif v.get("confidence"):
        target["confidence"] = v["confidence"]
    target["verdict"] = v.get("verdict", "")
    target["reason"] = v.get("reason", "")
    target["recommendation"] = v.get("recommendation", "")
    target["ref"] = v.get("ref", "")
    # D: расширенные поля (многострочные шаги / список файлов) — выводятся как есть.
    if v.get("steps"):
        target["steps"] = v["steps"]
    if v.get("files"):
        target["files"] = v["files"]
    if routing:
        target["routing"] = routing
    # ГЕЙТ: подтверждено, если есть evidence (строка/непустой список), routing или ref.
    target["evidence"] = ev_raw if ev_raw is not None else (v.get("ref") or "")
    target["confirmed"] = bool(ev_str or ev_list or routing or v.get("ref"))


def apply_verdicts(result: dict, verdicts: dict) -> dict:
    """LLM-слой: обогащает КАЖДУЮ группу/кластер ИЛИ отдельный ТЕСТ вердиктом.

    verdicts: {"<group_label>|<error_class>|@<номер>|cluster_key": {...}}

    Порядок матчинга ключа:
      (1) ТОЧНЫЙ номер теста ("@10.02.13" или "10.02.13") → пер-тестовый вердикт в item
          (кластер НЕ трогается; пер-тест поля: verdict/reason/recommendation/routing/
          confidence/evidence_score/evidence_list/confirmed + опц. steps/files);
      (2) cluster key/label → на кластер;
      (3) group_label/error_class → на группы/классы (как было).
    Схема полей вердикта — общая (см. _apply_verdict_to_dict). ГЕЙТ: без evidence
    (строка/непустой список) ИЛИ routing ИЛИ ref → `confirmed: false`.
    V2: непустой evidence-список → confidence вычисляется по весам kind'ов.
    Обратная совместимость: старые verdicts.json без пер-тестовых ключей работают как прежде.
    """
    by_label = {g["group_label"]: g for g in result.get("groups", [])}
    by_class: dict[str, list] = {}
    for g in result.get("groups", []):
        by_class.setdefault(g["error_class"], []).append(g)
    # V3: вердикт может адресовать кластер (по key или label).
    clusters = result.get("clusters") or []
    clusters_by_key = {cl.get("key"): cl for cl in clusters}
    clusters_by_label = {cl.get("label"): cl for cl in clusters}
    # V4: пер-тестовый матч по номеру (ведущий '@' отбрасывается при сравнении).
    items_by_number = {str(it.get("number") or "").lstrip("@"): it
                       for it in result.get("items", [])}
    for key, v in (verdicts or {}).items():
        num = str(key).strip().lstrip("@")
        if num and num in items_by_number:
            # (1) точный номер теста → item; кластеры/группы не изменяем.
            _apply_verdict_to_dict(items_by_number[num], v)
            continue
        if key in clusters_by_key or key in clusters_by_label:
            tgt = [clusters_by_key.get(key) or clusters_by_label.get(key)]
        else:
            tgt = [by_label[key]] if key in by_label else by_class.get(key, [])
        for g in tgt:
            _apply_verdict_to_dict(g, v)
    result["llm_verdicts"] = True
    _recount(result)
    return result


def _recount(result: dict) -> None:
    """Пересчитывает counts после правок групп, сохраняя гейт."""
    groups = result.get("groups", [])
    total = sum(g["count"] for g in groups)
    by_cat: dict[str, int] = {}
    by_owner: dict[str, int] = {}
    by_conf: dict[str, int] = {}
    for g in groups:
        by_cat[g["category"]] = by_cat.get(g["category"], 0) + g["count"]
        by_owner[g["owner"]] = by_owner.get(g["owner"], 0) + g["count"]
        by_conf[g["confidence"]] = by_conf.get(g["confidence"], 0) + g["count"]
    confirmed = total - by_owner.get("спор", 0)
    counts = result.setdefault("counts", {})
    counts["by_category"] = by_cat
    counts["by_owner"] = by_owner
    counts["by_confidence"] = by_conf
    counts["failed_again"] = total
    counts["confirmed_pct"] = round(confirmed / total * 100, 1) if total else 0.0
    counts["disputed_pct"] = round(by_owner.get("спор", 0) / total * 100, 1) if total else 0.0


def scan_launch_all(launch_id: int, cfg: dict, token: str) -> tuple[list[dict], dict]:
    """Постраничный скан ВСЕГО запуска (без эталона/фильтров) → только не-PASS результаты.

    Возвращает (failed_again-строки, scan_info). each: {number,status,messages:[...],results}
    """
    rows, info = scan_rerun(launch_id, cfg, token)
    # группируем по номеру теста
    by_number: dict[str, list[dict]] = {}
    for r in rows:
        num = extract_test_number_stub(r)
        if not num:
            continue
        by_number.setdefault(num, []).append(r)

    failed_again = []
    for num, arr in by_number.items():
        statuses = [str(r.get("status") or "").lower() for r in arr]
        nonpass = [r for r, s in zip(arr, statuses) if s in NON_PASS_STATUSES]
        if not nonpass:
            continue
        first = next((r for r in nonpass if str(r.get("status") or "").lower() != "skipped"), nonpass[0])
        messages = []
        for r in nonpass:
            m = extract_message_stub(r)
            if m and m not in messages:
                messages.append(m)
        failed_again.append({
            "number": "@" + num,
            "status": str(first.get("status") or "").upper(),
            "results": len(arr),
            "nonpass": len(nonpass),
            "messages": messages,
        })
    failed_again.sort(key=lambda x: natural_key(x["number"]))
    return failed_again, info


def build_from_scan(launch_id: int, failed_again: list[dict], info: dict, teams: list[str]) -> dict:
    """Артефакт классификации по ВСЕМУ rerun-запуску (без эталона)."""
    items = classify_items(failed_again)
    groups = group_items(items)
    counts = compute_counts(items, groups)
    needs_evidence = [
        {"number": it["number"], "error_class": it["error_class"],
         "category": it["category"], "locator": it["locator"]}
        for it in items if it["category"] == "unknown"
    ]
    return {
        "rerun_launch_id": launch_id,
        "mode": "scan",
        "teams": teams,
        "scan_info": info,
        "counts": counts,
        "groups": groups,
        "items": items,
        "needs_evidence": needs_evidence,
    }


def _resolve_expected_set_path(root: Path, expected_launch_id: int, team: str) -> Path | None:
    """Находит expected_set.json первичного запуска (для авто-стабильности).

    root/launches/{id}/{team}/expected_set.json при заданной team;
    иначе root/launches/{id}/expected_set.json; иначе единственная подпапка с файлом.
    """
    base = root / "launches" / str(expected_launch_id)
    if team:
        p = base / team / "expected_set.json"
        if p.exists():
            return p
    direct = base / "expected_set.json"
    if direct.exists():
        return direct
    if base.exists():
        subs = sorted(base.glob("*/expected_set.json"))
        if len(subs) == 1:
            return subs[0]
    return None


def _verify_scan_snapshot(folder_dir: Path) -> tuple[bool, str]:
    """Проверяет подлинность снимка скана (scan_manifest.json + хеши).

    Возвращает (ok, сообщение). Не бросает: стабильность — вспомогательные данные,
    но при непройденной проверке они НЕ используются (иначе классификация опиралась
    бы на правленый снимок).
    """
    manifest_file = Path(folder_dir) / "scan_manifest.json"
    expected_file = Path(folder_dir) / "expected_set.json"
    if not manifest_file.is_file():
        return False, "нет scan_manifest.json — снимок не сертифицирован"
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as e:
        return False, f"не читается scan_manifest.json: {e}"
    if manifest.get("complete") is not True:
        return False, f"скан неполный (complete={manifest.get('complete')})"
    if manifest.get("tail_mismatch") is True:
        return False, "расхождение в хвосте запуска (tail_mismatch=true)"
    pinned = manifest.get("expected_set_sha256")
    if not pinned:
        return False, "в манифесте нет expected_set_sha256 (скан старой версией)"
    if not expected_file.is_file():
        return False, "нет expected_set.json"
    actual = _sha256_file(expected_file)
    if actual != pinned:
        return False, (f"expected_set.json не совпадает с манифестом "
                       f"({pinned[:12]}… ≠ {actual[:12]}…)")
    return True, "ok"


def _sha256_file(path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def apply_stability(items: list[dict], root: Path, expected_launch_id: int | None, team: str) -> bool:
    """V3: проставляет item["prev_status"] по statuses первичного expected_set.

    Значения: passed | failed | broken | absent (номера нет в первичном). Если
    --expected-launch-id не задан или файл не найден — prev_status="" у всех,
    печатается предупреждение, исключения не летят. Возвращает True, если данные есть.
    """
    for it in items:
        it["prev_status"] = ""
    if not expected_launch_id:
        return False
    path = _resolve_expected_set_path(root, expected_launch_id, team)
    if not path:
        print(f"  ⚠ стабильность: expected_set.json для launch {expected_launch_id}"
              f"{(' / ' + team) if team else ''} не найден — пропущено.")
        return False
    ok, why = _verify_scan_snapshot(path.parent)
    if not ok:
        print(f"  ✖ стабильность: снимок скана НЕ прошёл проверку ({why}) — "
              f"данные первичного прогона не используются. Пересканировать: "
              f"scan_testops.py --launch-id {expected_launch_id} --tail-check")
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"  ⚠ стабильность: не прочитан {path}: {e} — пропущено.")
        return False
    statuses = data.get("statuses") or {}
    for it in items:
        num = str(it.get("number") or "").lstrip("@")
        entry = statuses.get(num)
        if not entry:
            it["prev_status"] = "absent"
            continue
        st = str(entry.get("status") or "").lower()
        it["prev_status"] = st if st in ("passed", "failed", "broken") else "absent"
    return True


def print_matrix(result: dict) -> None:
    c = result["counts"]
    head = f"CLASSIFY rerun {result['rerun_launch_id']}"
    if result.get("mode") != "scan" and result.get("expected_launch_id"):
        head += f" vs expected {result['expected_launch_id']}"
    else:
        head += " (весь запуск, без эталона)"
    print(head)
    print(f"  упали: {c['failed_again']} | подтверждено: {c['confirmed_pct']}% "
          f"| не подтверждено: {c['disputed_pct']}% | гейт: {'OK' if c.get('gate_ok') else 'FAIL'}")
    print(f"  по категориям: " + ", ".join(
        f"{CATEGORY_LABEL.get(k, k)}={v}" for k, v in sorted(c.get("by_category", {}).items())))
    print(f"  групп: {len(result['groups'])}")
    print("  --- группы (Группа | Категория | Conf | Кол-во) ---")
    for g in result["groups"]:
        print(f"  {g.get('group_label', g['error_class'])} | {g.get('category_label', g['category'])}"
              f" | conf={g['confidence']} | {g['count']}")
    ne = result["needs_evidence"]
    print(f"  needs_evidence (git-проверка LLM): {len(ne)}")
    if not c.get("gate_ok"):
        print(f"  ⚠ ГЕЙТ СХОДИМОСТИ НАРУШЕН: {c.get('gate_detail')}")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(description="Детерминированная классификация rerun (Слой 1)")
    ap.add_argument("--rerun-id", type=int, required=True, help="ID rerun-запуска")
    ap.add_argument("--root", default=None, help="Корень .qa-artifacts")
    ap.add_argument("--scan", action="store_true",
                    help="Скан ВСЕГО запуска (без эталона/rerun_diff): группировка по всем non-PASS")
    ap.add_argument("--teams", default="",
                    help="Опц. команды через запятую (информационно; фильтр не применяется в --scan)")
    ap.add_argument("--expected-launch-id", type=int, default=None,
                    help="ID первичного запуска: проставить item.prev_status для авто-стабильности кластеров")
    ap.add_argument("--team", default="",
                    help="Команда (подпапка launches/{id}/) для поиска expected_set.json первичного запуска")
    ap.add_argument("--evidence", default=None,
                    help="JSON от LLM (git-проверка спорных) — Слой 2: применить вердикты к артефакту")
    ap.add_argument("--verdicts", default=None,
                    help="JSON от LLM: вердикт/обоснование/рекомендация по каждой группе (error_class)")
    ap.add_argument("--rules", default=None,
                    help="Путь к classify_rules.json (по умолчанию <QA_HOME>/config/classify_rules.json)")
    ap.add_argument("--chat", action="store_true",
                    help="Печать markdown страницы перезапуска (общий рендерер render_markdown.py)")
    args = ap.parse_args()

    root = Path(args.root) if args.root else DEFAULT_ARTIFACTS_ROOT
    # 🔴 Правила классификации — ДАННЫЕ из JSON (команды дополняют без правки кода)
    rules_path = args.rules or RULES_PATH
    apply_rules(load_rules(rules_path))
    teams = [t.strip() for t in args.teams.split(",") if t.strip()]
    out_path = root / "reruns" / str(args.rerun_id) / "rerun_classification.json"

    if args.scan:
        # Режим вторичного запуска БЕЗ эталона: сканируем весь запуск, ошибки берём из TestOps.
        cfg = load_config(root)
        token = resolve_token(cfg)
        if not token:
            print("ОШИБКА: токен TestOps не задан (TESTOPS_TOKEN / config.json).")
            return 1
        init_scan_state(cfg, token, {})
        try:
            failed_again, info = scan_launch_all(args.rerun_id, cfg, token)
        except Exception as e:  # noqa: BLE001
            print(f"ОШИБКА скана rerun: {e}")
            return 1
        result = build_from_scan(args.rerun_id, failed_again, info, teams)
    else:
        diff_path = root / "reruns" / str(args.rerun_id) / "rerun_diff.json"
        if not diff_path.exists():
            print(f"ОШИБКА: {diff_path} не найден. Сначала запусти diff_rerun.py или используй --scan.")
            return 1
        try:
            diff = json.loads(diff_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"ОШИБКА чтения {diff_path}: {e}")
            return 1
        result = build_classification(diff)

    # V3: авто-стабильность (prev_status) ДО вердиктов, затем кластеризация.
    apply_stability(result.get("items", []), root, args.expected_launch_id, args.team)
    result["clusters"] = build_clusters(result.get("items", []))

    if args.evidence:
        ev_path = Path(args.evidence)
        if not ev_path.exists():
            print(f"ОШИБКА: {ev_path} не найден.")
            return 1
        try:
            evidence = json.loads(ev_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"ОШИБКА чтения evidence: {e}")
            return 1
        # если артефакт уже есть — берём его (сохранённые Слой-1 вердикты), иначе считаем заново
        if out_path.exists():
            try:
                result = json.loads(out_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        result = apply_evidence(result, evidence)

    if args.verdicts:
        vd_path = Path(args.verdicts)
        if not vd_path.exists():
            print(f"ОШИБКА: {vd_path} не найден.")
            return 1
        try:
            verdicts = json.loads(vd_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"ОШИБКА чтения verdicts: {e}")
            return 1
        result = apply_verdicts(result, verdicts)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.chat:
        text, problems = render_classification(result)
        print(text, end="")
        for problem in problems:
            print(f"  ⚠ {problem}")
    else:
        print_matrix(result)
    print(f"  json: {out_path}")

    print("СТАТУС: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
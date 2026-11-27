"""Модуль классификации сбоев рерана по config/triage_rules.json.

Отличия от классификатора первичного разбора:
  1. Главный сигнал — trace (Ruby backtrace результата), а не только message.
     По одному message «undefined method `each' for nil:NilClass» нельзя понять,
     что упал бэкенд: trace показывает savon/soap_fault.rb -> nori find_value.
  2. Каждое правило даёт label, owner (система/автотест/дефект/триаж) и category.
  3. Confidence считается по evidence-score, а не «на глаз».
"""

import json
import re
from pathlib import Path

from _paths import RULES_PATH

# --- Фолбэк-правила, если файла данных нет (держать в синхроне с triage_rules.json) ---
_FALLBACK = {
    "trace_rules": [
        {"key": "backend_soap_fault", "label": "Бэкенд: SOAP Fault в ответе сервиса",
         "owner": "система", "category": "env",
         "match": ["soap_fault\\.rb", "soapenv:Server", "Savon::SOAPFault"]},
        {"key": "infra_timeout", "label": "Инфраструктура: таймаут TCP/выполнения",
         "owner": "система", "category": "infra",
         "match": ["Failed to open TCP connection", "execution expired", "Net::ReadTimeout"]},
        {"key": "autotest_locator", "label": "Автотест: элемент не найден (локатор)",
         "owner": "автотест", "category": "locator",
         "match": ["capybara-[\\d.]*/lib/capybara/node/finders", "Capybara::ElementNotFound"]},
        {"key": "autotest_nil", "label": "Автотест: NoMethodError по nil",
         "owner": "автотест", "category": "testcode",
         "match": ["NoMethodError", "for nil:NilClass", "undefined method"]},
    ],
    "message_rules": [
        {"key": "autotest_locator", "label": "Автотест: элемент не найден (локатор)",
         "owner": "автотест", "category": "locator",
         "match": ["Unable to find visible", "Capybara::ElementNotFound"]},
        {"key": "autotest_nil", "label": "Автотест: NoMethodError по nil",
         "owner": "автотест", "category": "testcode",
         "match": ["undefined method", "for nil:NilClass"]},
    ],
    "default": {"key": "unclassified", "label": "Не классифицировано (нужен триаж)",
                "owner": "триаж", "category": "unknown"},
    "confidence_weights": {"message": 1, "trace": 1, "own_frame": 1, "git": 2},
}

# Фрейм backtrace: /path/to/file.rb:123:in `method'
FRAME_RE = re.compile(r"([\w./\-]+\.rb):(\d+):in [`']([^']*)'")
VENDOR_MARKERS = ("/vendor/bundle/", "/gems/", "/ruby/")
# Первый аргумент xpath/field/css из сообщения — для точечной проверки в git
LOCATOR_RE = re.compile(
    r'(?:xpath|field|css|link|button)\s+"((?:[^"\\]|\\.)*)"', re.IGNORECASE)
GENERIC_XPATH_RE = re.compile(r'(\.//[^"\s\\]+)')
HOSTPORT_RE = re.compile(r"(Failed to open TCP connection to\s+[\w.\-]+:\d+)")

RULES = {}
_COMPILED_TRACE = []
_COMPILED_MESSAGE = []


def load_rules(path=None):
    """Загружает правила. Отсутствие/битый файл — не ошибка: берётся фолбэк."""
    global RULES, _COMPILED_TRACE, _COMPILED_MESSAGE
    p = Path(path) if path else RULES_PATH
    data = {}
    if p.exists():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            data = {k: v for k, v in raw.items() if not k.startswith("_")}
        except (json.JSONDecodeError, OSError):
            data = {}
    RULES = {
        "trace_rules": data.get("trace_rules") or _FALLBACK["trace_rules"],
        "message_rules": data.get("message_rules") or _FALLBACK["message_rules"],
        "default": data.get("default") or _FALLBACK["default"],
        "confidence_weights": data.get("confidence_weights") or _FALLBACK["confidence_weights"],
        "owners": data.get("owners") or {},
    }
    _COMPILED_TRACE = _compile(RULES["trace_rules"])
    _COMPILED_MESSAGE = _compile(RULES["message_rules"])
    return RULES


def _compile(rules):
    out = []
    for rule in rules:
        pats = []
        for pat in rule.get("match") or []:
            try:
                pats.append(re.compile(pat, re.DOTALL | re.IGNORECASE))
            except re.error:
                continue
        out.append((rule, pats))
    return out


load_rules()


def natural_key(text):
    """Ключ естественной сортировки номеров тестов (02.x < 10.x < 39.x)."""
    parts = []
    for chunk in re.split(r"(\d+)", str(text)):
        if chunk.isdigit():
            parts.append((0, int(chunk)))
        elif chunk:
            parts.append((1, chunk.lower()))
    return parts


def with_at(number):
    """Гарантирует префикс '@' у номера теста."""
    s = str(number).strip()
    return s if s.startswith("@") else "@" + s


def strip_at(number):
    return str(number).strip().lstrip("@")


# Номер теста в имени результата: '16.01.05.01 ПРОДКАТ ...' -> '16.01.05.01'
TEST_NUMBER_RE = re.compile(r"(?<!\d)(\d{2}(?:\.\d{2,3})+)(?!\d)")


def extract_number(name):
    """Номер теста из name результата. Пусто, если номер не распознан."""
    m = TEST_NUMBER_RE.search(str(name or ""))
    return m.group(1) if m else ""


def classify(trace, message):
    """Тип сбоя: сначала trace, потом message. Возвращает dict правила + matched_by."""
    for rule, pats in _COMPILED_TRACE:
        for p in pats:
            if p.search(trace or ""):
                return dict(rule, matched_by="trace", _compiled=pats)
    for rule, pats in _COMPILED_MESSAGE:
        for p in pats:
            if p.search(message or ""):
                return dict(rule, matched_by="message", _compiled=pats)
    return dict(RULES["default"], matched_by="none", _compiled=[])


def parse_frames(trace):
    """Разбирает backtrace на фреймы. Возвращает список dict(file, line, method, vendor)."""
    frames = []
    for path, line, method in FRAME_RE.findall(trace or ""):
        vendor = any(marker in path for marker in VENDOR_MARKERS)
        frames.append({
            "file": path,
            "line": int(line),
            "method": method,
            "vendor": vendor,
            "short": path.rsplit("/", 1)[-1],
        })
    return frames


def own_frame(frames):
    """Первый НЕ библиотечный фрейм — это код автотестов (или продукта), самый информативный."""
    for f in frames:
        if not f["vendor"]:
            return f
    return None


def gem_frame(frames):
    """Первый библиотечный фрейм — по нему видно, ЧЕМ упало (savon, capybara, nori)."""
    for f in frames:
        if f["vendor"]:
            rel = f["file"]
            if "/gems/" in rel:
                rel = rel.split("/gems/", 1)[1]
                return dict(f, short="/".join(rel.split("/")[:2]))
            return f
    return None


def matching_frame(frames, rule):
    """Фрейм, который сработал на правило (для backend — savon/soap_fault, а не nori).

    Показывает точку, ГДЕ ошибка проявилась по смыслу правила, а не самый глубокий
    технический фрейм: «savon-2.12.1/lib » информативнее, чем «nori-2.6.0/lib».
    """
    pats = [p for p in (rule.get("_compiled") or [])]
    for f in frames:
        if f["vendor"] and any(p.search(f["file"]) for p in pats):
            rel = f["file"]
            if "/gems/" in rel:
                rel = rel.split("/gems/", 1)[1]
                return "/".join(rel.split("/")[:2])
            return f["short"]
    gf = gem_frame(frames)
    return gf["short"] if gf else ""


def extract_locator(message):
    """Локатор из сообщения (xpath/field/css) — то, что проверяет глубокий разбор в git."""
    m = LOCATOR_RE.search(message or "")
    if m:
        return m.group(1)
    m = GENERIC_XPATH_RE.search(message or "")
    if m:
        return m.group(1)
    m = HOSTPORT_RE.search(message or "")
    if m:
        return m.group(1)
    return ""


def evidence_score(evidence):
    """Score = сумма весов evidence-элементов по confidence_weights."""
    weights = RULES.get("confidence_weights") or {}
    return sum(weights.get(k, 0) for k in evidence if evidence.get(k))


def confidence_from_score(score):
    """high при score >= 2, medium при 1, иначе low (уходит в триаж)."""
    if score >= 2:
        return "high"
    if score >= 1:
        return "medium"
    return "low"


def subgroup_key(rule, frames, locator):
    """Ключ подгруппы внутри кластера — конкретика, по которой видно «одна причина или разные».

    система/бэкенд -> сработавший gem-фрейм (savon/soap_fault, capybara/finders);
    автотест       -> локатор из сообщения, иначе свой фрейм теста (файл:строка);
    иначе          -> пусто (подгруппа «прочее»).
    """
    key = str(rule.get("key") or "")
    if key.startswith("backend_") or key.startswith("infra_"):
        return matching_frame(frames, rule)
    if key.startswith("autotest_"):
        if locator:
            return locator
        own = own_frame(frames)
        return f"{own['short']}:{own['line']}" if own else ""
    return ""


def category_label(category, owner):
    """Человеко-читаемая подпись категории для отчёта."""
    labels = {
        "env": "Бэкенд/окружение",
        "infra": "Инфраструктура",
        "locator": "Автотест: локатор",
        "testcode": "Автотест: код теста",
        "product": "Продукт",
        "testdata": "Тестовые данные",
        "unknown": "Не классифицировано",
    }
    return labels.get(category, f"{owner}: {category}")

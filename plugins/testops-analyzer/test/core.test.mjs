// Юнит-тесты ядра плагина. Фиксируют правила, по которым панель должна совпадать
// со скиллом testops-analysis / dispatch_team_reruns.py. Запуск: node --test test/core.test.mjs
import test from "node:test"
import assert from "node:assert/strict"
import {
  aggregateStatus, isRerunCandidate, newTestIsRerunCandidate,
  testNumber, TEST_NUMBER_RE, compareNumbers, naturalKey,
  filterResults, etalonCoverage, etalonMismatch, detectRunKind, NON_PASS_STATUSES, RERUN_PASS,
} from "../src/core.mjs"
import { buildTags, cleanNumber, countTags, verifyTags, truncateTags, withAt } from "../src/tags.mjs"

// ── статусы ──────────────────────────────────────────────────────────────────

test("PASS_STATUSES раздачи не совпадают с NON_PASS_STATUSES статистики", () => {
  assert.ok(NON_PASS_STATUSES.includes("skipped"), "skipped входит в NON_PASS (статистика)")
  assert.ok(RERUN_PASS.has("skipped"), "skipped входит в PASS раздачи")
})

test("aggregateStatus: все passed → passed; иначе первый не-pass", () => {
  assert.equal(aggregateStatus(["passed", "passed"]), "passed")
  assert.equal(aggregateStatus([]), "passed")
  assert.equal(aggregateStatus(["passed", "broken"]), "broken")
  assert.equal(aggregateStatus(["skipped", "broken"]), "skipped")
})

test("isRerunCandidate: skipped/failed/broken/passed", () => {
  assert.equal(isRerunCandidate("broken"), true)
  assert.equal(isRerunCandidate("failed"), true)
  assert.equal(isRerunCandidate("skipped"), false, "skipped не идёт в @-список")
  assert.equal(isRerunCandidate("passed"), false)
})

test("newTestIsRerunCandidate: новый тест уходит в реран, только если не все строки PASS", () => {
  assert.equal(newTestIsRerunCandidate(["broken"]), true)
  assert.equal(newTestIsRerunCandidate(["passed", "skipped"]), false)
  assert.equal(newTestIsRerunCandidate([]), true)
})

// ── номера и сортировка ─────────────────────────────────────────────────────

test("testNumber: форматы NN(.NN)+; без номера — пустая строка", () => {
  assert.equal(testNumber("01.10.03.01.01 Подготовка"), "01.10.03.01.01")
  assert.equal(testNumber("16.01.05.01 ПРОДКАТ"), "16.01.05.01")
  assert.equal(testNumber("нет номера"), "")
  assert.equal(testNumber(""), "")
})

test("compareNumbers: естественная сортировка (2 < 10)", () => {
  const arr = ["10.02", "02.01", "10.02.04.02", "02.01.10", "10.02.04.01"]
  arr.sort(compareNumbers)
  assert.deepEqual(arr, ["02.01", "02.01.10", "10.02", "10.02.04.01", "10.02.04.02"])
})

test("naturalKey: числовые куски — числа, прочие — строки", () => {
  assert.deepEqual(naturalKey("02.01"), [[0, 2], [0, 1]])
  assert.deepEqual(naturalKey("abc"), [[1, "abc"]])
})

// ── фильтрация ───────────────────────────────────────────────────────────────

function row(name, status, testCaseId = 1, id = testCaseId) {
  return { name, status, testCaseId, id }
}

test("filterResults: skipped-номер исключается из @-списка, но остаётся в статистике", () => {
  const rows = [
    row("01.01.01 Сценарий", "broken", 10),
    row("01.01.02 Пропущенный", "skipped", 11),
    row("01.01.03 Прошёл", "passed", 12),
  ]
  const etalon = { exists: true, numbers: new Set(["01.01.01", "01.01.02", "01.01.03"]) }
  const teamOfCase = new Map([[10, "TX-T9"], [11, "TX-T9"], [12, "TX-T9"]])
  const r = filterResults({ rows, teamCanon: "TX-T9", etalon, teamOfCase })
  assert.equal(r.total, 3)
  assert.equal(r.numberCounts.broken, 1)
  assert.equal(r.numberCounts.skipped, 1)
  assert.equal(r.numberCounts.passed, 1)
  assert.deepEqual(r.rerunNumbers, ["01.01.01"], "только broken в @-списке")
  assert.deepEqual(r.skippedNumbers, ["01.01.02"])
})

test("filterResults: строки без номера складываются в один бакет \"\"", () => {
  const rows = [
    row("Сводный без номера A", "passed", 1),
    row("Сводный без номера B", "passed", 2),
  ]
  const r = filterResults({ rows, etalon: { exists: false, numbers: new Set() }, teamCanon: null, taggedIds: new Set(), teamOfCase: new Map() })
  assert.equal(r.total, 1, "обе строки — один бакет без номера")
  assert.equal(r.numberCounts.passed, 1)
  assert.equal(r.rowCounts.passed, 2)
})

test("filterResults: параметризованный тест — один номер, несколько строк", () => {
  const rows = [
    row("10.01.01 Тест, Examples (#1)", "passed", 1),
    row("10.01.01 Тест, Examples (#2)", "broken", 1),
  ]
  const etalon = { exists: true, numbers: new Set(["10.01.01"]) }
  const r = filterResults({ rows, teamCanon: "TX-T9", etalon, teamOfCase: new Map([[1, "TX-T9"]]) })
  assert.equal(r.total, 1)
  assert.equal(r.numberCounts.broken, 1, "агрегат по номеру — broken")
  assert.equal(r.rowCounts.passed, 1)
  assert.equal(r.rowCounts.broken, 1)
})

test("filterResults: origin по паре team+tag", () => {
  const rows = [
    row("10.01.01 A", "broken", 1),
  ]
  const etalon = { exists: true, numbers: new Set(["10.01.01"]) }
  const taggedIds = new Set([1])
  const teamOfCase = new Map([[1, "TX-T9"]])
  const r = filterResults({ rows, teamCanon: "TX-T9", etalon, taggedIds, teamOfCase })
  assert.equal(r.nonPassed[0].origin, "both", "team и tag одновременно")
})

test("etalonCoverage: covered/missing/pct и пересечение в обе стороны", () => {
  const etalon = new Set(["01.01", "01.02", "01.03", "01.04"])
  const ran = new Set(["01.01", "01.02"])
  const c = etalonCoverage(etalon, ran)
  assert.equal(c.total, 4)
  assert.equal(c.covered, 2)
  assert.equal(c.missing, 2)
  assert.equal(c.pct, 50)
  assert.equal(c.pctLaunch, 100, "оба прогнанных номера есть в эталоне")
})

// ── тип прогона ─────────────────────────────────────────────────────────────

test("detectRunKind: DEBUG-джоба → реран с high confidence", () => {
  const k = detectRunKind({ name: "DEBUG - qa (DEBUG/TX-T9 - #9)", rowsCount: 269, etalonCount: 283 })
  assert.equal(k.value, "rerun")
  assert.equal(k.confidence, "high")
})

test("detectRunKind: небольшой основной прогон не считается рераном по числу строк", () => {
  const k = detectRunKind({ name: "Обычный прогон", rowsCount: 15, etalonCount: 283 })
  assert.equal(k.value, "main", "правило rowsCount<=20 убрано")
})

test("detectRunKind: явный выбор пользователя приоритетен", () => {
  const k = detectRunKind({ name: "DEBUG/TX-T9", requested: "main" })
  assert.equal(k.value, "main")
  assert.equal(k.source, "user")
})

// ── гейт эталона ────────────────────────────────────────────────────────────

test("etalonMismatch: нулевое пересечение при непустых обеих сторонах — блокировка", () => {
  assert.equal(
    etalonMismatch({ etalonExists: true, launchTeamCount: 188, overlapLaunch: 0 }),
    true,
    "реальный кейс: TREX с эталоном RM (25) на QG-запуске давал 188 «новых» номеров",
  )
})

test("etalonMismatch: частичное пересечение — НЕ блокировка (неполный охват)", () => {
  assert.equal(etalonMismatch({ etalonExists: true, launchTeamCount: 208, overlapLaunch: 152 }), false)
})

test("etalonMismatch: нет эталона или нет номеров команды — не блокировка", () => {
  assert.equal(etalonMismatch({ etalonExists: false, launchTeamCount: 10, overlapLaunch: 0 }), false)
  assert.equal(etalonMismatch({ etalonExists: true, launchTeamCount: 0, overlapLaunch: 0 }), false)
})

test("filterResults: тест команды вне эталона попадает в newFailed (как в read_nonpass скилла)", () => {
  const rows = [
    row("39.02.03.07 Упал вне эталона", "broken", 1),
  ]
  const etalon = { exists: true, numbers: new Set(["01.01.01"]) }
  const r = filterResults({ rows, teamCanon: "TX-T9", etalon, teamOfCase: new Map([[1, "TX-T9"]]) })
  assert.deepEqual(r.rerunNumbers, [], "в эталонном фильтре его нет")
  assert.deepEqual(r.newFailed.map((t) => t.number), ["39.02.03.07"], "но он уходит в перезапуск")
})

// ── @-список ────────────────────────────────────────────────────────────────

test("buildTags: @-префикс, дедуп, естественная сортировка, разделитель пробел", () => {
  assert.equal(buildTags(["10.02", "02.01", "10.02"]), "@02.01 @10.02")
})

test("cleanNumber: убирает запятую и пробелы", () => {
  assert.equal(cleanNumber("10,02.01"), "1002.01")
  assert.equal(cleanNumber(" 10.02.01 "), "10.02.01")
})

test("verifyTags: ловит пустой список, дубль, токен без @", () => {
  assert.equal(verifyTags("", null).ok, false)
  assert.equal(verifyTags("@01.01 @01.01", 2).ok, false, "дубль")
  assert.equal(verifyTags("01.01", 1).ok, false, "без @")
  assert.equal(verifyTags("@01.01 @02.01", 2).ok, true)
})

test("truncateTags: режет по целым токенам", () => {
  const t = truncateTags("@01.01 @02.02 @03.03", { maxChars: 12 })
  assert.equal(t.truncated, true)
  assert.ok(t.text.endsWith("@02.02") || t.text.split(" ").length <= 2)
})

test("countTags: считает токены", () => {
  assert.equal(countTags("@01.01 @02.02"), 2)
  assert.equal(countTags(""), 0)
})

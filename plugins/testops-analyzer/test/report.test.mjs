// Юнит-тесты отчёта о реране и текстового списка автотестов.
// Запуск: node --test test/report.test.mjs
import test from "node:test"
import assert from "node:assert/strict"
import {
  buildRerunReport, buildRerunPlanReport, planRows, rerunRows, splitTags,
  testsTextList, testsTextListByTeam, RERUN_STATUS_LABELS,
} from "../src/report.mjs"

const LAUNCH = { name: "TEST_RM_25.11", url: "https://testops.example/launch/1367360" }

test("splitTags: строка @-списка → массив номеров", () => {
  assert.deepEqual(splitTags("@01.01.01  @02.02.02 "), ["@01.01.01", "@02.02.02"])
  assert.deepEqual(splitTags(""), [])
  assert.deepEqual(splitTags(null), [])
})

test("rerunRows: fan-out берёт results[], одиночная команда — поля в корне", () => {
  const fanout = rerunRows({ all: true, results: [{ team: "TX-T9" }, { team: "TACO" }] })
  assert.equal(fanout.length, 2)

  const single = rerunRows({ all: false, team: "TCAT", job: "DEBUG/TCAT", count: 3, tags: "@01 @02", status: "triggered" })
  assert.deepEqual(single, [{
    team: "TCAT", job: "DEBUG/TCAT", count: 3, tags: "@01 @02", status: "triggered",
    buildUrl: null, queueUrl: null, jobUrl: null, note: null, error: null,
  }])

  assert.deepEqual(rerunRows({ error: "нет кредов" }), [])
  assert.deepEqual(rerunRows(null), [])
})

test("отчёт по всем командам: ссылки на билды, по умолчанию @-список тестов", () => {
  const result = {
    all: true,
    launch: LAUNCH,
    results: [
      { team: "TX-T9", job: "DEBUG/TX-T9", count: 2, tags: "@01.01.01 @02.02.02", status: "triggered", buildUrl: "https://jenkins/job/DEBUG/job/TX-T9/15/" },
      { team: "TACO", job: "DEBUG/TACO", count: 0, tags: "", status: "no_failures", jobUrl: "https://jenkins/job/DEBUG/job/TACO/" },
      { team: "TE", job: "DEBUG/TE", count: 2, tags: "@09.09.09 @10.10.10", status: "etalon_mismatch", error: "эталон не пересекается" },
    ],
  }
  const text = buildRerunReport({ result, context: { mode: "RM", team: "", tag: "" }, now: new Date(2026, 10, 25, 12, 34) })

  assert.match(text, /Отчёт о реране в Jenkins — 25\.11\.2026 12:34/)
  assert.match(text, /Запуск TestOps: TEST_RM_25\.11/)
  assert.match(text, /https:\/\/testops\.example\/launch\/1367360/)
  assert.match(text, /Режим: RM · команда: все команды · тег: —/)
  assert.match(text, /Итого: джоб запущено 1 из 3 · отправлено 2 теста · без упавших 1 · без запуска по другим причинам 1/)
  assert.match(text, /1\. TX-T9 — 2 теста · запущена/)
  assert.match(text, /джоба: DEBUG\/TX-T9/)
  assert.match(text, /билд: https:\/\/jenkins\/job\/DEBUG\/job\/TX-T9\/15\//)
  // По умолчанию — @-список: номера в формате параметра tags.
  assert.match(text, /@-список \(2\):/)
  assert.match(text, /@01\.01\.01 @02\.02\.02/)
  assert.doesNotMatch(text, /тесты \(2\):/, "названия по умолчанию не печатаются")
  assert.match(text, /2\. TACO — тестов нет · не запускалась — нет упавших/)
  assert.match(text, /страница джобы: https:\/\/jenkins\/job\/DEBUG\/job\/TACO\//)
  assert.match(text, /3\. TE — 2 теста не отправлено · заблокирована — эталон не пересекается с запуском/)
  assert.match(text, /причина: эталон не пересекается/)
  assert.doesNotMatch(text, /@09\.09\.09/, "заблокированная команда не показывает состав")
})

test("режимы состава: none — без списка, names — с названиями", () => {
  const result = {
    all: true,
    results: [
      { team: "TX-T9", job: "DEBUG/TX-T9", count: 2, tags: "@01.01.01 @02.02.02", status: "triggered", buildUrl: "https://jenkins/job/DEBUG/job/TX-T9/15/" },
      { team: "TE", job: "DEBUG/TE", count: 2, tags: "@09.09.09 @10.10.10", status: "etalon_mismatch", error: "эталон не пересекается" },
    ],
  }
  const none = buildRerunReport({ result, tests: "none" })
  assert.doesNotMatch(none, /@-список/)
  assert.doesNotMatch(none, /@01\.01\.01/)
  assert.match(none, /1\. TX-T9 — 2 теста · запущена/)

  const names = buildRerunReport({ result, tests: "names", names: { "01.01.01": "Оформление сделки" } })
  assert.match(names, /тесты \(2\):/)
  assert.match(names, /@01\.01\.01 — Оформление сделки/)
  assert.match(names, /@02\.02\.02/)
  assert.doesNotMatch(names, /@09\.09\.09/, "у заблокированной команды состав не печатается ни в одном режиме")

  // Прежний флаг includeTests продолжает работать как «с названиями».
  assert.match(buildRerunReport({ result, includeTests: true }), /тесты \(2\):/)
})

test("@-список длинной команды переносится по строкам", () => {
  const tags = Array.from({ length: 30 }, (_, i) => `@${String(i + 1).padStart(2, "0")}.01.01`).join(" ")
  const text = buildRerunReport({ result: { all: false, ok: true, team: "TX-T9", job: "DEBUG/TX-T9", count: 30, tags, status: "triggered" } })
  const tagLines = text.split("\n").filter((l) => /^@\d/.test(l.trim()))
  assert.ok(tagLines.length > 1, "список должен быть разбит на несколько строк")
  assert.ok(tagLines.every((l) => l.length <= 100), "строки не должны быть шире 100 символов")
  assert.equal(tagLines.join(" ").trim().split(/\s+/).length, 30, "ни один номер не потерян")
})

test("отчёт по одной команде: билд + @-список по умолчанию", () => {
  const result = { all: false, ok: true, team: "TCAT", job: "DEBUG/TCAT", count: 1, tags: "@03.03.03", status: "triggered", buildUrl: "https://jenkins/job/DEBUG/job/TCAT/7/" }
  const text = buildRerunReport({
    result,
    context: { launch: LAUNCH, mode: "QG", team: "TCAT", tag: "ivanov" },
    names: { "03.03.03": "Проверка оплаты картой" },
  })
  assert.match(text, /команда: TCAT · тег: ivanov/)
  assert.match(text, /1\. TCAT — 1 тест · запущена/)
  assert.match(text, /@-список \(1\):\n\s+@03\.03\.03/)
  assert.doesNotMatch(text, /Проверка оплаты картой/, "названия по умолчанию не печатаются")
})

test("отчёт при пустом результате: явно сказано, что Jenkins не вызывался", () => {
  const text = buildRerunReport({ result: null, context: {} })
  assert.match(text, /Реранов нет/)
})

test("отчёт помечает повторный запуск как уже стоявший в очереди", () => {
  const result = {
    all: true,
    results: [{ team: "TFU", job: "DEBUG/TFU", count: 1, tags: "@04.04.04", status: "already_dispatched", buildUrl: "https://jenkins/job/DEBUG/job/TFU/3/", note: "already_dispatched" }],
  }
  const text = buildRerunReport({ result, now: new Date(2026, 0, 2, 3, 4) })
  assert.match(text, /уже запускалась \(в очереди\)/)
  assert.match(text, /билд уже стоял в очереди/)
})

test("testsTextList: номер · статус · название, с пометкой происхождения", () => {
  const text = testsTextList([
    { number: "01.02.03", status: "failed", name: "Оформление сделки", origin: "team" },
    { number: "05.06.07", status: "broken", name: "Проверка лимита", origin: "tag" },
    { number: null, status: "failed", name: "Без номера" },
  ])
  assert.equal(text, [
    "@01.02.03 · failed · Оформление сделки",
    "@05.06.07 · broken · Проверка лимита [по тегу]",
    "— · failed · Без номера",
  ].join("\n"))
})

test("testsTextListByTeam: группировка по командам", () => {
  const text = testsTextListByTeam([
    { number: "01.01.01", status: "failed", name: "A", team: "TX-T9" },
    { number: "02.02.02", status: "broken", name: "B", team: "TACO" },
    { number: "03.03.03", status: "failed", name: "C", team: "TX-T9" },
  ])
  assert.match(text, /TX-T9 \(2\):\n@01\.01\.01 · failed · A\n@03\.03\.03 · failed · C/)
  assert.match(text, /TACO \(1\):\n@02\.02\.02 · broken · B/)
})

test("статусы рерана покрыты человекочитаемыми подписями", () => {
  for (const status of ["triggered", "already_dispatched", "no_failures", "no_job", "disabled", "etalon_mismatch", "error"]) {
    assert.ok(RERUN_STATUS_LABELS[status], `нет подписи для ${status}`)
  }
})

// ── отчёт-план (сразу после «проверить», до запуска рерана) ──────────────────

test("planRows: fan-out плана — count, ссылка на джобу, статус planned", () => {
  const rows = planRows({
    all: true,
    teams: [
      { team: "TX-T9", job: "DEBUG/TX-T9", jobUrl: "https://jenkins/job/DEBUG/job/TX-T9/", count: 19, tags: "@01.01.01", blocked: false },
      { team: "TACO", job: "DEBUG/TACO", jobUrl: "https://jenkins/job/DEBUG/job/TACO/", count: 0, tags: "", blocked: false },
      { team: "TE", job: "DEBUG/TE", jobUrl: "https://jenkins/job/DEBUG/job/TE/", count: 0, suppressed: 4, tags: "@05.05.05", blocked: true, blockedReason: "эталон не пересекается" },
    ],
  })
  assert.equal(rows.length, 3)
  assert.equal(rows[0].status, "planned")
  assert.equal(rows[0].count, 19)
  assert.equal(rows[1].status, "no_failures")
  // у заблокированной команды показываем «сколько было бы», но статус — блокировка
  assert.equal(rows[2].status, "etalon_mismatch")
  assert.equal(rows[2].count, 4)
  assert.equal(rows[2].error, "эталон не пересекается")
})

test("planRows: одиночная команда и пустой план", () => {
  const one = planRows({ all: false, plan: { team: "TCAT", job: "DEBUG/TCAT", jobUrl: "https://jenkins/job/DEBUG/job/TCAT/", count: 3, tags: "@01 @02 @03" } })
  assert.deepEqual(one.map((r) => [r.team, r.status, r.count]), [["TCAT", "planned", 3]])

  const empty = planRows({ all: false, plan: { team: "TCAT", job: "DEBUG/TCAT", count: 0, tags: "" } })
  assert.equal(empty[0].status, "no_failures")

  assert.deepEqual(planRows(null), [])
  assert.deepEqual(planRows({ error: "креды Jenkins не найдены" }), [])
})

test("отчёт-план: ссылки на джобы, пометка «не запускался» и @-список по умолчанию", () => {
  const preview = {
    all: true,
    launch: LAUNCH,
    teams: [
      { team: "TX-T9", job: "DEBUG/TX-T9", jobUrl: "https://jenkins/job/DEBUG/job/TX-T9/", count: 2, tags: "@01.01.01 @02.02.02" },
      { team: "TACO", job: "DEBUG/TACO", jobUrl: "https://jenkins/job/DEBUG/job/TACO/", count: 0, tags: "" },
    ],
  }
  const text = buildRerunPlanReport({
    preview,
    context: { mode: "RM", team: "", tag: "" },
    names: { "01.01.01": "Оформление сделки" },
    now: new Date(2026, 10, 25, 9, 5),
  })

  assert.match(text, /^План рерана в Jenkins — 25\.11\.2026 09:05 \(реран ещё не запускался\)/)
  assert.match(text, /Итого: к отправке 2 теста в 1 команду · без упавших 1/)
  assert.match(text, /1\. TX-T9 — 2 теста · к отправке \(ещё не запускалась\)/)
  assert.match(text, /страница джобы: https:\/\/jenkins\/job\/DEBUG\/job\/TX-T9\//)
  assert.match(text, /2\. TACO — тестов нет · не запускалась — нет упавших/)
  assert.doesNotMatch(text, /билд:/, "в плане билда ещё нет")
  // По умолчанию — @-список (номера), без названий.
  assert.match(text, /@-список \(2\):\n\s+@01\.01\.01 @02\.02\.02/)
  assert.doesNotMatch(text, /Оформление сделки/)

  const detailed = buildRerunPlanReport({ preview, tests: "names", names: { "01.01.01": "Оформление сделки" } })
  assert.match(detailed, /Оформление сделки/)

  const short = buildRerunPlanReport({ preview, tests: "none" })
  assert.doesNotMatch(short, /@01\.01\.01/)
})

test("отчёт-план по одной команде: джоба, ссылка и @-список", () => {
  const text = buildRerunPlanReport({
    preview: { all: false, plan: { team: "TCAT", job: "DEBUG/TCAT", jobUrl: "https://jenkins/job/DEBUG/job/TCAT/", count: 1, tags: "@03.03.03" } },
    context: { launch: LAUNCH, mode: "QG", team: "TCAT", tag: "" },
    now: new Date(2026, 10, 25, 9, 5),
  })
  assert.match(text, /1\. TCAT — 1 тест · к отправке/)
  assert.match(text, /джоба: DEBUG\/TCAT/)
  assert.match(text, /страница джобы: https:\/\/jenkins\/job\/DEBUG\/job\/TCAT\//)
  assert.match(text, /@03\.03\.03/)
})

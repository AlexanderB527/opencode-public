// Текст отчёта о реране в Jenkins + текстовый список автотестов для панели.
//
// Модуль чистый (никаких node-импортов): его тянет и GUI-бандл (src/gui.tsx),
// и юнит-тесты (test/report.test.mjs). Никакой сети и чтения файлов здесь нет —
// на входе уже готовый результат рерана (rerunTeam / rerunAllTeams) либо план
// рерана (collectTeamReruns / rerunPreview).
//
// Два вида отчёта:
//   • план — сразу после «проверить», реран ещё не запускался: по каждой команде
//     ссылка на страницу Jenkins-джобы и список тестов, которые уйдут;
//   • факт — после рерана (в т.ч. автозапуска): по каждой команде ссылка на билд
//     (или очередь) и список реально отправленных тестов.

export const RERUN_STATUS_LABELS = {
  triggered: "запущена",
  planned: "к отправке (ещё не запускалась)",
  already_dispatched: "уже запускалась (в очереди)",
  no_failures: "не запускалась — нет упавших",
  no_job: "нет джобы в реестре",
  disabled: "команда отключена в реестре",
  etalon_mismatch: "заблокирована — эталон не пересекается с запуском",
  error: "ошибка запуска",
}

const str = (v) => String(v ?? "").trim()

// @-список приходит одной строкой через пробел — режем на отдельные номера.
export function splitTags(tags) {
  return str(tags).split(/\s+/).filter(Boolean)
}

// Одиночная команда (rerunTeam) отдаёт поля в корне, fan-out (rerunAllTeams) —
// в results[]. Приводим к одному массиву строк отчёта.
export function rerunRows(result) {
  if (!result || result.error) return []
  if (result.all) return Array.isArray(result.results) ? result.results.slice() : []
  if (!result.team && !result.job) return []
  return [{
    team: result.team,
    job: result.job,
    count: result.count,
    tags: result.tags,
    status: result.status ?? (result.ok ? "triggered" : "error"),
    buildUrl: result.buildUrl ?? null,
    queueUrl: result.queueUrl ?? null,
    jobUrl: result.jobUrl ?? null,
    note: result.note ?? null,
    error: result.error ?? null,
  }]
}

// План рерана (ещё не запущен) → те же строки отчёта, но статус planned
// и ссылка на страницу джобы вместо билда.
export function planRows(preview) {
  if (!preview || preview.error) return []

  if (preview.all) {
    return (preview.teams ?? []).map((t) => {
      const planned = Number(t.count ?? 0)
      const suppressed = Number(t.suppressed ?? 0)
      const payload = t.blocked ? suppressed : planned
      return {
        team: t.team,
        job: t.job,
        jobUrl: t.jobUrl ?? null,
        count: payload,
        tags: t.tags ?? "",
        status: t.blocked
          ? "etalon_mismatch"
          : (planned > 0 ? "planned" : "no_failures"),
        buildUrl: null,
        queueUrl: null,
        note: null,
        error: t.blocked ? (t.blockedReason ?? "эталон не пересекается с запуском") : null,
      }
    })
  }

  const p = preview.plan ?? {}
  if (!p.team && !p.job) return []
  const count = Number(p.count ?? 0)
  return [{
    team: p.team,
    job: p.job,
    jobUrl: p.jobUrl ?? null,
    // План одной команды панель строит уже после гейта эталона: если тестов 0,
    // реран не состоится — так и пишем, а не «к отправке 0».
    count,
    tags: p.tags ?? "",
    status: count > 0 ? "planned" : "no_failures",
    buildUrl: null,
    queueUrl: null,
    note: null,
    error: null,
  }]
}

function pad2(n) {
  return String(n).padStart(2, "0")
}

// Русская форма множественного числа: 1 тест / 2 теста / 5 тестов.
function plural(n, forms) {
  const abs = Math.abs(Number(n) || 0)
  const tail100 = abs % 100
  const tail10 = abs % 10
  if (tail100 > 10 && tail100 < 20) return forms[2]
  if (tail10 === 1) return forms[0]
  if (tail10 >= 2 && tail10 <= 4) return forms[1]
  return forms[2]
}

function testsWord(n) {
  return `${n} ${plural(n, ["тест", "теста", "тестов"])}`
}

function fmtDate(d) {
  const x = d instanceof Date ? d : new Date(d ?? Date.now())
  const date = `${pad2(x.getDate())}.${pad2(x.getMonth() + 1)}.${x.getFullYear()}`
  return `${date} ${pad2(x.getHours())}:${pad2(x.getMinutes())}`
}

// Строки, у которых есть состав тестов (реально отправленный или запланированный).
function hasPayload(row) {
  return row.status === "triggered" || row.status === "already_dispatched" || row.status === "planned"
}

function rowCount(row) {
  const n = Number(row.count ?? NaN)
  return Number.isFinite(n) ? n : splitTags(row.tags).length
}

// Ссылка: билд → очередь → страница джобы. Первое доступное.
function rowLink(row) {
  return row.buildUrl || row.queueUrl || row.jobUrl || ""
}

// Тесты одной команды: «@01.01.01 — Название» (имя берём из снимка, если есть).
function testLines(tags, names = {}) {
  return splitTags(tags).map((t) => {
    const number = t.replace(/^@/, "")
    const name = names[number]
    return name ? `     ${t} — ${name}` : `     ${t}`
  })
}

// @-список одной командой строкой с переносами по ширине: так же, как параметр
// tags в Jenkins (разделитель — пробел), но без «простыни» на весь экран.
const TAG_LINE_WIDTH = 96

function wrapTags(tags, width = TAG_LINE_WIDTH) {
  const indent = "     "
  const out = []
  let current = ""
  for (const tag of tags) {
    const next = current ? `${current} ${tag}` : tag
    if (current && indent.length + next.length > width) {
      out.push(indent + current)
      current = tag
    } else {
      current = next
    }
  }
  if (current) out.push(indent + current)
  return out
}

// Что показывать про состав: "tags" — @-список (по умолчанию), "names" — с названиями,
// "none" — только итог и строки команд.
export const TESTS_MODES = ["tags", "names", "none"]

function normalizeTestsMode({ tests, includeTests }) {
  const mode = String(tests ?? "").toLowerCase()
  if (TESTS_MODES.includes(mode)) return mode
  // Совместимость с прежним флагом: includeTests => «с названиями».
  return includeTests ? "names" : "tags"
}

// Общий рендер: план и факт отличаются шапкой, итогом и подписями ссылок.
// По умолчанию состав печатается @-списком — это то, что реально уходит в Jenkins.
function renderReport({ rows, context = {}, names = {}, now = new Date(), kind = "actual", tests }) {
  const testsMode = normalizeTestsMode({ tests })
  const plan = kind === "plan"
  const launch = context.launch ?? null
  const lines = []

  lines.push(
    plan
      ? `План рерана в Jenkins — ${fmtDate(now)} (реран ещё не запускался)`
      : `Отчёт о реране в Jenkins — ${fmtDate(now)}`,
  )
  if (launch?.name) lines.push(`Запуск TestOps: ${launch.name}`)
  const launchUrl = str(launch?.url) || str(context.link)
  if (launchUrl) lines.push(launchUrl)
  lines.push(
    `Режим: ${str(context.mode) || "—"} · команда: ${str(context.team) || "все команды"}` +
    ` · тег: ${str(context.tag) || "—"}`,
  )
  lines.push("")

  if (rows.length === 0) {
    lines.push(plan
      ? "План не построен: нечего отправлять в Jenkins."
      : "Реранов нет: в Jenkins ничего не отправлялось.")
    return lines.join("\n")
  }

  const payloadRows = rows.filter(hasPayload)
  const testsTotal = payloadRows.reduce((s, r) => s + rowCount(r), 0)
  const noFailures = rows.filter((r) => r.status === "no_failures").length
  const failed = rows.filter((r) => r.status !== "no_failures" && !hasPayload(r)).length

  if (plan) {
    lines.push(
      `Итого: к отправке ${testsWord(testsTotal)} в ${payloadRows.length}`
      + ` ${plural(payloadRows.length, ["команду", "команды", "команд"])}`
      + ` · без упавших ${noFailures}`
      + (failed ? ` · не поедет по другим причинам ${failed}` : ""),
    )
  } else {
    lines.push(
      `Итого: джоб запущено ${payloadRows.length} из ${rows.length}`
      + ` · отправлено ${testsWord(testsTotal)}`
      + ` · без упавших ${noFailures}`
      + (failed ? ` · без запуска по другим причинам ${failed}` : ""),
    )
  }
  lines.push("")

  let i = 0
  for (const row of rows) {
    i += 1
    const status = RERUN_STATUS_LABELS[row.status] ?? (str(row.status) || "—")
    const count = rowCount(row)
    const countText = hasPayload(row)
      ? (count ? testsWord(count) : "тестов нет")
      : (count ? `${testsWord(count)} не отправлено` : "тестов нет")
    lines.push(`${i}. ${str(row.team) || "без команды"} — ${countText} · ${status}`)
    if (row.job) lines.push(`   джоба: ${row.job}`)

    const link = rowLink(row)
    if (link) {
      const kindLabel = row.buildUrl ? "билд" : row.queueUrl ? "очередь" : "страница джобы"
      lines.push(`   ${kindLabel}: ${link}`)
      if (row.note === "already_dispatched" || row.note === "queue item exists") {
        lines.push("   примечание: билд уже стоял в очереди — второй запуск не создавался")
      }
    }
    if (row.error) lines.push(`   причина: ${row.error}`)

    // Состав печатаем по выбранному режиму и только там, где он реальный или
    // запланированный. У заблокированных/ошибочных команд tags — это «что было
    // бы», а не «что запущено», поэтому их не печатаем никогда.
    const tags = hasPayload(row) ? splitTags(row.tags) : []
    if (tags.length) {
      if (testsMode === "tags") {
        lines.push(`   @-список (${tags.length}):`)
        lines.push(...wrapTags(tags))
      } else if (testsMode === "names") {
        lines.push(`   тесты (${tags.length}):`)
        lines.push(...testLines(row.tags, names))
      }
    }
    lines.push("")
  }

  return lines.join("\n").replace(/\s+$/, "")
}

// Отчёт по факту рерана. context: { link, launch: {name,url}, mode, team, tag }.
// tests: "tags" (по умолчанию) | "names" | "none".
export function buildRerunReport({ result, context = {}, names = {}, now = new Date(), tests, includeTests } = {}) {
  const ctx = { ...context }
  if (!ctx.launch && result?.launch) ctx.launch = result.launch
  const mode = normalizeTestsMode({ tests, includeTests })
  return renderReport({ rows: rerunRows(result), context: ctx, names, now, kind: "actual", tests: mode })
}

// Отчёт-план: строится сразу после «проверить», до запуска рерана.
// preview — ответ rerunPreview ({ all, teams } | { all:false, plan }).
export function buildRerunPlanReport({ preview, context = {}, names = {}, now = new Date(), tests, includeTests } = {}) {
  const ctx = { ...context }
  if (!ctx.launch && preview?.launch) ctx.launch = preview.launch
  const mode = normalizeTestsMode({ tests, includeTests })
  return renderReport({ rows: planRows(preview), context: ctx, names, now, kind: "plan", tests: mode })
}

// Текстовый список автотестов для кнопки «проверить»: номер · статус · название.
// rows — это снимок nonPassed (поля number/name/status/team).
export function testsTextList(rows = []) {
  return (rows ?? [])
    .map((r) => {
      const number = str(r?.number) ? `@${str(r.number)}` : "—"
      const status = str(r?.status) || "—"
      const name = str(r?.name) || "(без названия)"
      const mark = r?.origin === "tag" ? " [по тегу]" : r?.origin === "both" ? " [команда+тег]" : ""
      return `${number} · ${status} · ${name}${mark}`
    })
    .join("\n")
}

// Тот же список, но сгруппированный по командам — так он читается в панели
// и так его удобно вставлять в задачу/чат.
export function testsTextListByTeam(rows = []) {
  const groups = new Map()
  for (const r of rows ?? []) {
    const team = str(r?.team) || "без команды"
    if (!groups.has(team)) groups.set(team, [])
    groups.get(team).push(r)
  }
  return [...groups.entries()]
    .map(([team, list]) => `${team} (${list.length}):\n${testsTextList(list)}`)
    .join("\n\n")
}

// Сборка «снимка» запуска TestOps: статистика + фильтры (команда / тег тестировщика).
//
// Правила фильтрации — в src/core.mjs и повторяют scan_testops.py:
//   include = (Team ∈ команда И номер ∈ эталон) ИЛИ (тест-кейс имеет тег тестировщика)
// Без тега поведение сводится к фильтру по команде; без команды — ко всему запуску.
//
// Отличие от скилла (сознательное, для панели): если эталона команды нет на диске,
// скилл падает с ошибкой, а плагин фильтрует по одной команде и возвращает warning.
// Полный разбор (классификация, публикация) всё равно делается скиллом в сессии.
import { apiGet, launchUrl, parseLaunchId, testcaseUrl, testopsConfig } from "./api.mjs"
import { readEtalon, testNumber } from "./etalon.mjs"
import { buildTags } from "./tags.mjs"
import { MODES, loadTeamJobs, normalizeTeam, resolveTeam, teamOptions } from "./teams.mjs"
import { detectRunKind, etalonCoverage, etalonMismatch, filterResults, NON_PASS_STATUSES, compareNumbers } from "./core.mjs"
// Ссылка на страницу Jenkins-джобы команды: нужна в отчёте, который строится
// сразу после «проверить» (реран ещё не запущен, а ссылки на джобы уже есть).
import { jobPageUrl } from "./dispatch.mjs"

const NON_PASS = new Set(NON_PASS_STATUSES)
const PAGE_SIZE = 100
const MAX_PAGES = 10_000
// Сколько тест-кейсов максимум «обогащаем» командой за один вызов панели.
// Каждый тест-кейс — отдельный GET /api/testcase/{id}/cfv, поэтому панель
// ограничивает объём, а полный проход делает скилл в сессии.
const TEAM_LOOKUP_LIMIT = 2000
const TEAM_LOOKUP_CONCURRENCY = 8
const TEAM_CACHE_MAX = 20_000
const MESSAGE_LIMIT = 50
const MESSAGE_TRUNCATE = 300

// Кеш «testCaseId -> команда» на процесс сервера: один тест-кейс встречается
// в десятках прогонов, повторно дёргать /cfv незачем.
const teamCache = new Map()

function cacheTeam(key, value) {
  if (teamCache.size >= TEAM_CACHE_MAX) {
    // Простое вытеснение самой старой записи — память не должна расти бесконечно.
    const oldest = teamCache.keys().next().value
    if (oldest !== undefined) teamCache.delete(oldest)
  }
  teamCache.set(key, value)
}

// Команда из названия запуска. Запуски в основном одно-командные
// («DEBUG/TX-T9», «Команды/TACO_QG_QA»): если в названии ровно одна команда —
// её можно присвоить тестам без поля Team. Несколько команд или ноль — null
// (не гадаем в смешанном запуске).
function teamFromLaunchName(name, registry) {
  const tokens = String(name ?? "").split(/[^A-Za-z0-9А-Яа-яЁё-]+/).filter(Boolean)
  const found = new Set()
  for (const tok of tokens) {
    const canon = resolveTeam(tok, registry)
    if (canon) found.add(canon)
  }
  return found.size === 1 ? [...found][0] : null
}

// ── запросы ──────────────────────────────────────────────────────────────────

async function getLaunch(cfg, id) {
  return apiGet(`/api/launch/${id}`, { cfg })
}

// /api/rs/launch/{id}/statistic — независимый агрегат сервера по статусам.
// Проверено на launch 1363380 и 1344527: сумма по статусам == totalElements.
// Это второй источник правды: с его помощью ловится потеря строк при пагинации.
async function getStatistic(cfg, id) {
  try {
    const st = await apiGet(`/api/rs/launch/${id}/statistic`, { cfg })
    const arr = Array.isArray(st) ? st : (st?.statistic ?? [])
    const counts = {}
    let total = 0
    for (const item of arr) {
      const key = String(item?.status ?? "").toLowerCase() || "unknown"
      const n = Number(item?.count ?? 0)
      counts[key] = (counts[key] ?? 0) + n
      total += n
    }
    const known = (counts.passed ?? 0) + (counts.failed ?? 0) + (counts.broken ?? 0) + (counts.skipped ?? 0)
    return {
      passed: counts.passed ?? 0,
      failed: counts.failed ?? 0,
      broken: counts.broken ?? 0,
      skipped: counts.skipped ?? 0,
      other: total - known,
      total,
      raw: counts,
    }
  } catch {
    return null // статистику не получили — не выдумываем нули
  }
}

// Пагинация результатов с НЕЗАВИСИМОЙ проверкой полноты.
//
// Раньше остановка была по `rows.length >= totalElements`, где totalElements —
// тот же ответ API. Это тавтология: если сервер занизил totalElements, цикл
// останавливался раньше и complete оставался true при потерянных строках.
// Теперь три независимых сигнала:
//   1) `last === true` — сервер сам сказал, что страница последняя;
//   2) `totalPages` — номер страницы дошёл до последней;
//   3) totalElements не менялся между страницами (иначе запуск мутирует).
// complete = дошли до последней страницы И число строк == totalElements И total стабилен.
async function loadResults(cfg, id) {
  const rows = []
  const totalsSeen = new Set()
  let firstTotal = null
  let totalPages = null
  let lastSeen = null
  let pages = 0

  for (let page = 0; page <= MAX_PAGES; page++) {
    const data = await apiGet(`/api/v2/launch/${id}/test-result/flat?page=${page}&size=${PAGE_SIZE}`, { cfg })
    const content = Array.isArray(data?.content) ? data.content : []
    pages += 1

    const t = Number(data?.totalElements)
    if (Number.isFinite(t)) totalsSeen.add(t)
    if (page === 0) {
      firstTotal = Number.isFinite(t) ? t : null
      const tp = Number(data?.totalPages)
      totalPages = Number.isFinite(tp) ? tp : null
    }
    if (typeof data?.last === "boolean") lastSeen = data.last

    if (!content.length) break
    rows.push(...content)
    if (lastSeen === true) break
    if (totalPages !== null && page + 1 >= totalPages) break
  }

  const total = firstTotal ?? rows.length
  const totalsStable = totalsSeen.size <= 1
  // API без last/totalPages: единственный сигнал — счётчик. Помечаем как ненадёжный.
  const terminated = lastSeen === true || (lastSeen === null && totalPages === null && rows.length >= total)
  const complete = terminated && rows.length === total && totalsStable

  return {
    rows,
    total,
    pages,
    totalPages,
    complete,
    totalsStable,
    totalValues: [...totalsSeen],
    lastSeen,
  }
}

// Тест-кейсы с тегом тестировщика: AQL tag = "<tag>". Пагинация — как в скилле,
// но с той же независимой проверкой: `last`/`totalPages` вместо доверия total.
async function loadTaggedIds(cfg, projectId, tag) {
  const clean = String(tag ?? "").trim()
  if (!clean || !projectId) return { ids: new Set(), total: 0, complete: true, error: null }

  const ids = new Set()
  let total = 0
  let complete = false
  try {
    for (let page = 0; page <= MAX_PAGES; page++) {
      const rql = encodeURIComponent(`tag = "${clean}"`)
      const data = await apiGet(
        `/api/testcase/__search?projectId=${projectId}&rql=${rql}&page=${page}&size=${PAGE_SIZE}`,
        { cfg },
      )
      const cases = data?.testCases ?? data?.content ?? []
      if (page === 0) total = Number(data?.total ?? data?.totalElements ?? cases.length) || cases.length
      for (const tc of cases) if (tc?.id !== undefined) ids.add(tc.id)

      const totalPages = Number(data?.totalPages)
      const last = typeof data?.last === "boolean" ? data.last : null
      if (!cases.length) { complete = true; break }
      if (last === true) { complete = true; break }
      if (Number.isFinite(totalPages) && page + 1 >= totalPages) { complete = true; break }
      if (last === null && !Number.isFinite(totalPages) && ids.size >= total) { complete = true; break }
    }
    return { ids, total: total || ids.size, complete, error: null }
  } catch (e) {
    // Тег не должен ронять снимок: отдаём предупреждение, фильтр просто не применяется.
    return { ids: new Set(), total: 0, complete: false, error: String(e?.message ?? e).slice(0, 200) }
  }
}

// Команда тест-кейса: custom field "Team" (/api/testcase/{id}/cfv).
async function fetchTeamOfTestcase(cfg, projectId, testCaseId) {
  const key = `${projectId}:${testCaseId}`
  if (teamCache.has(key)) return teamCache.get(key)
  let team = ""
  try {
    const data = await apiGet(`/api/testcase/${testCaseId}/cfv?projectId=${projectId}`, { cfg })
    for (const item of data ?? []) {
      const name = String(item?.customField?.name ?? "")
      if (name !== "Team") continue
      const direct = String(item?.name ?? "")
      if (direct) { team = direct; break }
      const values = item?.values ?? []
      team = values.length ? String(values[0]?.name ?? "") : ""
      break
    }
  } catch {
    return "" // команда не должна ронять снимок; не кешируем ошибку
  }
  cacheTeam(key, team)
  return team
}

async function fetchMessage(cfg, resultId) {
  try {
    const detail = await apiGet(`/api/testresult/${resultId}`, { cfg })
    let msg = String(detail?.message ?? "").trim()
    if (msg.length > MESSAGE_TRUNCATE) msg = `${msg.slice(0, MESSAGE_TRUNCATE)}...`
    return msg
  } catch {
    return ""
  }
}

// Пул: ограничивает параллельные запросы, чтобы не задушить TestOps.
async function mapPool(items, limit, worker) {
  const out = new Array(items.length)
  let cursor = 0
  const runners = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (cursor < items.length) {
      const index = cursor++
      out[index] = await worker(items[index], index)
    }
  })
  await Promise.all(runners)
  return out
}

// ── команда тест-кейсов ──────────────────────────────────────────────────────

// Нормализует и разрешает команду в канон (T9 / ТX / «SFA Команда Т9» → TX-T9).
function canonOf(rawTeam, registry) {
  const norm = normalizeTeam(rawTeam)
  if (!norm) return ""
  return resolveTeam(norm, registry) ?? norm
}

// Map(testCaseId -> канон). Возвращает также число тест-кейсов, не попавших
// в лимит панели (для честного предупреждения о частичности).
async function resolveTeamByCase(cfg, projectId, ids, registry) {
  const out = new Map()
  if (!projectId || !ids.length) return { map: out, skipped: 0, requested: 0 }
  const toResolve = ids.slice(0, TEAM_LOOKUP_LIMIT)
  const resolved = await mapPool(toResolve, TEAM_LOOKUP_CONCURRENCY, (id) =>
    fetchTeamOfTestcase(cfg, projectId, id))
  toResolve.forEach((id, i) => out.set(id, canonOf(resolved[i], registry)))
  return { map: out, skipped: ids.length - toResolve.length, requested: ids.length }
}

// Номер -> команда по эталонам. Только ОДНОЗНАЧНЫЕ номера: если номер есть
// в эталонах двух команд, приписать его одной из них нельзя — оставляем пусто.
function buildTeamByEtalonNumber(modeUp) {
  const owners = new Map()
  for (const t of teamOptions()) {
    const e = readEtalon(t.id, modeUp)
    if (!e.exists) continue
    for (const n of e.numbers) {
      if (!owners.has(n)) owners.set(n, new Set())
      owners.get(n).add(t.id)
    }
  }
  const unique = new Map()
  let ambiguous = 0
  for (const [n, set] of owners) {
    if (set.size === 1) unique.set(n, [...set][0])
    else ambiguous += 1
  }
  return { map: unique, ambiguous }
}

// ── снимок ───────────────────────────────────────────────────────────────────

export async function buildBrief({
  link,
  team = "",
  tag = "",
  mode = "QG",
  runKind = "auto",
  withMessages = false,
} = {}, cfg = testopsConfig()) {
  const launchId = parseLaunchId(link)
  const launch = await getLaunch(cfg, launchId)
  const projectId = launch?.projectId ?? null

  const modeUp = String(mode ?? "QG").toUpperCase()
  if (!MODES.includes(modeUp)) throw new Error(`неизвестный режим «${mode}»: допустимы ${MODES.join(", ")}`)

  const warnings = []
  const teamCanon = String(team ?? "").trim() ? resolveTeam(team) : null
  if (String(team ?? "").trim() && !teamCanon) {
    warnings.push(`команда «${team}» не распознана по реестру — фильтр по команде не применён`)
  }

  const etalon = teamCanon
    ? readEtalon(teamCanon, modeUp)
    : { exists: false, path: null, numbers: new Set(), count: 0, pin: { checked: false, ok: true, message: "нет команды" } }

  // Реестр нужен, чтобы имя команды из TestOps («SFA Команда T9») сводилось к канону
  // (TX-T9). Без этого сравнение шло бы «T9» vs «TX-T9» и фильтр всегда давал ноль.
  const registry = loadTeamJobs()

  // Fallback по эталону: тест-кейс без команды в TestOps (Team пустой) определяем
  // по номеру через эталоны всех команд — только когда номер однозначен.
  const { map: teamByEtalonNumber, ambiguous: ambiguousNumbers } = buildTeamByEtalonNumber(modeUp)

  // Fallback по названию запуска: одно-командный запуск → тесты без Team
  // принадлежат этой команде (закрывает и тесты вне эталона).
  const launchTeam = teamFromLaunchName(launch?.name, registry)

  const [statistic, results, tagged] = await Promise.all([
    getStatistic(cfg, launchId),
    loadResults(cfg, launchId),
    loadTaggedIds(cfg, projectId, tag),
  ])

  // ── полнота чтения ─────────────────────────────────────────────────────────
  if (!results.complete) {
    warnings.push(
      `прочитано ${results.rows.length} строк, totalElements=${results.total} — `
        + "пагинация не сошлась, данные могут быть неполными. Перечитай запуск",
    )
  }
  if (!results.totalsStable) {
    warnings.push(
      `totalElements менялся во время чтения (${results.totalValues.join(" → ")}) — `
        + "запуск дописывается прямо сейчас; перечитай после остановки",
    )
  }
  if (statistic && statistic.total !== results.rows.length) {
    warnings.push(
      `статистика запуска (${statistic.total}) не совпадает с прочитанными результатами `
        + `(${results.rows.length}) — запуск менялся во время чтения`,
    )
  }
  if (tagged.error) warnings.push(cfg.runRequest ? "поиск по тегу не удался" : `поиск по тегу не удался: ${tagged.error}`)
  if (!tagged.complete && !tagged.error && String(tag ?? "").trim()) {
    warnings.push(`поиск по тегу «${tag}» прочитан не полностью — фильтр по тегу может быть неполным`)
  }
  if (String(tag ?? "").trim() && !tagged.error && tagged.ids.size === 0) {
    warnings.push(`по тегу «${tag}» не найдено ни одного тест-кейса`)
  }
  if (teamCanon && !etalon.exists) {
    warnings.push(
      `эталон ${teamCanon}/${modeUp} не найден (${etalon.path}) — фильтр только по полю Team; `
        + "скачать эталон: scripts/fetch_etalons.py",
    )
  }
  if (etalon.exists && etalon.pin && etalon.pin.checked && !etalon.pin.ok) {
    warnings.push(`эталон ${teamCanon}/${modeUp}: ${etalon.pin.message} — перекачать эталон (fetch_etalons.py)`)
  }

  // ── команда для тест-кейсов ────────────────────────────────────────────────
  const uniqueIds = [...new Set(results.rows.map((r) => r.testCaseId).filter((x) => x !== null && x !== undefined))]
  const { map: teamOfCase, skipped: teamLookupSkipped } = await resolveTeamByCase(cfg, projectId, uniqueIds, registry)
  if (teamLookupSkipped > 0) {
    warnings.push(
      `команда определена для ${uniqueIds.length - teamLookupSkipped} из ${uniqueIds.length} тест-кейсов `
        + `(лимит панели ${TEAM_LOOKUP_LIMIT}) — сводка частичная; полный проход делает скилл`,
    )
  }

  // ── фильтрация по правилам скилла (ядро) ───────────────────────────────────
  const res = filterResults({
    rows: results.rows,
    teamCanon,
    etalon,
    taggedIds: tagged.ids,
    teamOfCase,
    launchTeam,
    teamByEtalonNumber,
  })

  if (res.unknownStatusRows > 0) {
    warnings.push(
      `${res.unknownStatusRows} строк с неизвестным статусом (не passed/failed/broken/skipped) — `
        + "в статистику по статусам они не попали, но в общий счёт номеров входят",
    )
  }
  if (res.otherAggregate > 0) {
    warnings.push(`${res.otherAggregate} номеров имеют статус вне четырёх известных — проверь их вручную`)
  }
  if (res.teamRowsWithoutNumber > 0) {
    warnings.push(`${res.teamRowsWithoutNumber} строк команды без номера в имени — с эталоном не сопоставимы`)
  }
  const fallbackRows = res.teamSource.etalon + res.teamSource.launch
  if (teamCanon && fallbackRows > 0) {
    warnings.push(
      `команда для ${fallbackRows} строк определена не по полю Team `
        + `(по эталону: ${res.teamSource.etalon}, по названию запуска: ${res.teamSource.launch})`,
    )
  }
  if (ambiguousNumbers > 0) {
    warnings.push(
      `${ambiguousNumbers} номеров входят в эталоны нескольких команд — по номеру не приписываются ни одной`,
    )
  }

  // ── охват эталона ──────────────────────────────────────────────────────────
  const coverage = teamCanon && etalon.exists
    ? etalonCoverage(etalon.numbers, res.launchTeamNumbers)
    : null

  // 🔴 Гейт как в скилле: эталон не пересекается с запуском → неверный тип/команда.
  // В этом состоянии цифры и @-список недействительны: каждый тест команды выглядит
  // «вне эталона» и уходит в перезапуск. Скилл в таком случае артефакты не пишет
  // и публикацию блокирует — панель обязана вести себя так же.
  const mismatch = etalonMismatch({
    etalonExists: Boolean(teamCanon && etalon.exists),
    launchTeamCount: res.launchTeamNumbers.size,
    overlapLaunch: coverage ? coverage.overlapLaunch : 0,
  })

  if (mismatch) {
    warnings.push(
      `ЭТАЛОН НЕ ПЕРЕСЕКАЕТСЯ С ЗАПУСКОМ: эталон ${teamCanon}/${modeUp} (${etalon.count} номеров) `
        + `и ${res.launchTeamNumbers.size} номеров команды в запуске не имеют общих тестов — `
        + "вероятно, выбран неверный режим QG/RM/HF или неверная команда. "
        + "Цифры и @-список НЕДЕЙСТВИТЕЛЬНЫ, реран запускать нельзя. Скилл в этом случае блокирует работу.",
    )
  }
  if (teamCanon && res.byNumber.size === 0) {
    warnings.push(
      `по фильтру не осталось ни одного теста команды ${teamCanon}`
        + (etalon.exists ? " — проверь режим и актуальность эталона" : " — эталон не найден"),
    )
  }
  if (res.newTests.length > 0) {
    warnings.push(
      `${res.newTests.length} тестов команды ${teamCanon} нет в эталоне ${modeUp} `
        + `(кандидаты в эталон, в фильтр не входят): `
        + res.newTests.slice(0, 12).map((t) => t.number).join(", ")
        + (res.newTests.length > 12 ? " …" : ""),
    )
  }

  // ── сообщения об ошибках (по запросу) ──────────────────────────────────────
  const nonPassed = res.nonPassed
    .map((r) => ({ ...r, url: r.testCaseId ? testcaseUrl(cfg.url, r.testCaseId) : null }))
    .sort((a, b) => compareForSort(a.number, b.number))

  if (withMessages && nonPassed.length) {
    const slice = nonPassed.slice(0, MESSAGE_LIMIT)
    const messages = await mapPool(slice, TEAM_LOOKUP_CONCURRENCY, (row) =>
      row.resultId ? fetchMessage(cfg, row.resultId) : "")
    slice.forEach((row, i) => { row.message = messages[i] })
    if (nonPassed.length > slice.length) {
      warnings.push(`сообщения об ошибках запрошены только для первых ${slice.length} из ${nonPassed.length}`)
    }
  }

  const numbersTotal = res.total
  const failedTotal = res.numberCounts.failed + res.numberCounts.broken
  const kind = detectRunKind({
    name: launch?.name,
    rowsCount: results.rows.length,
    etalonCount: etalon.count,
    requested: runKind,
  })

  // @-список для Jenkins: упавшие (агрегат вне PASS_STATUSES раздачи) + упавшие
  // вне эталона. skipped в список НЕ входит — так же, как в dispatch_team_reruns.py.
  //
  // 🔴 При сработавшем гейте эталона список обнуляется. Причина: номера «вне
  // эталона» появляются ровно потому, что эталон не подошёл, и отправлять их —
  // значит перезапускать чужой набор. Скилл в этом состоянии артефакты не пишет.
  const rerunNumbers = mismatch ? [] : [...res.rerunNumbers, ...res.newFailed.map((t) => t.number)]
  const rerunTags = buildTags(rerunNumbers)

  return {
    source: "testops-analyzer",
    fetchedAt: new Date().toISOString(),
    input: { link: String(link ?? ""), team: String(team ?? ""), tag: String(tag ?? ""), mode: modeUp, runKind: String(runKind ?? "auto") },
    launch: {
      id: launchId,
      url: launchUrl(cfg.url, launchId),
      name: String(launch?.name ?? `launch ${launchId}`).trim(),
      closed: Boolean(launch?.closed),
      projectId,
      createdBy: launch?.createdBy ?? null,
      modified: launch?.lastModifiedDate
        ? new Date(launch.lastModifiedDate).toISOString().slice(0, 16).replace("T", " ")
        : null,
    },
    // Статистика всего запуска — независимый агрегат сервера, для сверки.
    launchStatistic: statistic,
    filters: {
      team: String(team ?? ""),
      teamCanon,
      tag: String(tag ?? "").trim(),
      mode: modeUp,
      taggedTestcases: tagged.ids.size,
      etalon: {
        exists: etalon.exists,
        path: etalon.path,
        count: etalon.count,
        coverage: coverage ? coverage.pct : null,
        coverageLaunch: coverage ? coverage.pctLaunch : null,
        covered: coverage ? coverage.covered : null,
        missing: coverage ? coverage.missing : null,
        // Полный список, а не срез: это вход для «догнать непрогнанное»
        // (скилл отдаёт etalon_missing_numbers целиком). Обрезка — на выводе.
        missingNumbers: coverage ? coverage.missingNumbers : [],
        // Гейт «эталон не пересекается с запуском»: цифры недействительны.
        mismatch,
        pin: etalon.pin ?? null,
      },
      // Блокирует ли снимок раздачу (гейт эталона). Панель обязана показать это
      // до кнопки «запустить», а не после.
      blocked: mismatch,
    },
    filtered: {
      // Агрегаты по НОМЕРУ теста — так же, как expected_set.json в скилле.
      total: numbersTotal,
      passed: res.numberCounts.passed,
      failed: res.numberCounts.failed,
      broken: res.numberCounts.broken,
      skipped: res.numberCounts.skipped,
      failedTotal,
      passRate: res.passRate,
      // Строки результата: параметризованные тесты дают несколько строк на номер.
      results: res.included,
      passedRows: res.rowCounts.passed,
      failedRows: res.rowCounts.failed,
      brokenRows: res.rowCounts.broken,
      skippedRows: res.rowCounts.skipped,
      nonPassedNumbers: nonPassed.length,
      // @-список: сколько номеров реально уйдёт в Jenkins.
      rerunNumbers: res.rerunNumbers.length + res.newFailed.length,
      skippedExcluded: res.skippedNumbers.length,
      matchedByTeam: res.matchedByTeam,
      includedByTag: res.includedByTag,
      partial: teamLookupSkipped > 0 || Boolean(cfg.runRequest && (!tagged.complete || (tag && !projectId))),
      // Полнота чтения — то, чего раньше не было видно в панели.
      complete: results.complete,
      pages: results.pages,
      totalPages: results.totalPages,
      totalElements: results.total,
      totalsStable: results.totalsStable,
      teamRowsWithoutNumber: res.teamRowsWithoutNumber,
      unknownStatusRows: res.unknownStatusRows,
      // Кандидаты в эталон: тесты команды, которых в эталоне ещё нет.
      newTests: res.newTests.length,
      newFailed: res.newFailed.length,
    },
    newFailed: res.newFailed,
    perTeam: [...res.perTeam.entries()]
      .map(([team_, count]) => ({ team: team_, nonPassed: count }))
      .sort((a, b) => b.nonPassed - a.nonPassed),
    nonPassed,
    // Готовый @-список для Jenkins (параметр tags): упавшие + упавшие вне эталона.
    rerunTags,
    runKind: kind,
    warnings,
  }
}

// Сортировка: естественная по номеру, строки без номера — в конце.
function compareForSort(a, b) {
  if (!a && b) return 1
  if (a && !b) return -1
  if (!a && !b) return 0
  return compareNumbers(a, b)
}

// ── «все команды»: @-список упавших по каждой команде для fan-out рерана ──────
//
// Когда в панели команда не выбрана, реран идёт по ВСЕМ командам с упавшими —
// как dispatch скилла. Тест попадает ТОЛЬКО в список своей команды (по полю Team,
// затем по однозначному номеру эталона, затем по названию запуска). Раньше
// тег-совпадение добавляло тест в списки ВСЕХ 13 команд — это давало 13 запусков
// одного и того же теста.
/** @param {{ link?: string, mode?: string, tag?: string }} [options] */
export async function collectTeamReruns({ link, mode = "QG", tag = "" } = {}) {
  const cfg = testopsConfig()
  const launchId = parseLaunchId(link)
  const launch = await getLaunch(cfg, launchId)
  const projectId = launch?.projectId ?? null
  const modeUp = String(mode ?? "QG").toUpperCase()
  const warnings = []

  const [results, tagged] = await Promise.all([
    loadResults(cfg, launchId),
    String(tag ?? "").trim() ? loadTaggedIds(cfg, projectId, tag) : Promise.resolve({ ids: new Set(), total: 0, complete: true, error: null }),
  ])

  const registry = loadTeamJobs()
  const uniqueIds = [...new Set(results.rows.map((r) => r.testCaseId).filter((x) => x !== null && x !== undefined))]
  const { map: teamOfCase, skipped } = await resolveTeamByCase(cfg, projectId, uniqueIds, registry)
  const { map: teamByEtalonNumber } = buildTeamByEtalonNumber(modeUp)
  const launchTeam = teamFromLaunchName(launch?.name, registry)

  if (!results.complete) {
    warnings.push(`прочитано ${results.rows.length} из ${results.total} строк — пагинация не сошлась, раздача может быть неполной`)
  }
  if (skipped > 0) {
    warnings.push(`команда определена не для всех тест-кейсов (лимит ${TEAM_LOOKUP_LIMIT}) — раздача частичная`)
  }

  const teams = []
  // Команды без эталона: их номера фильтруются ТОЛЬКО по полю Team и в эталон
  // не сверяются — это шире, чем скилл (он без эталона падает). Молчать нельзя:
  // иначе в Jenkins уедет больше тестов, чем скилл бы отправил.
  const teamsWithoutEtalon = []
  const teamsWithBadPin = []

  for (const opt of teamOptions()) {
    const etalon = readEtalon(opt.id, modeUp)
    const res = filterResults({
      rows: results.rows,
      teamCanon: opt.id,
      etalon,
      taggedIds: new Set(), // тег уже учтён распределением по командам
      teamOfCase,
      launchTeam,
      teamByEtalonNumber,
    })
    const numbers = [...res.rerunNumbers, ...res.newFailed.map((t) => t.number)]
    if (!etalon.exists && numbers.length > 0) teamsWithoutEtalon.push(opt.id)
    if (etalon.exists && etalon.pin?.checked && !etalon.pin.ok) teamsWithBadPin.push(opt.id)

    // Гейт эталона по этой команде (как в скилле): без пересечения эталона
    // с запуском фильтр эталона не работал, и в @-список попало всё подряд.
    const cov = etalon.exists ? etalonCoverage(etalon.numbers, res.launchTeamNumbers) : null
    const blocked = etalonMismatch({
      etalonExists: etalon.exists,
      launchTeamCount: res.launchTeamNumbers.size,
      overlapLaunch: cov ? cov.overlapLaunch : 0,
    })

    // Заблокированная команда ничего не отправляет: её список целиком построен
    // на «тестах вне эталона», то есть на неверном эталоне.
    const sendable = blocked ? [] : numbers

    teams.push({
      team: opt.id,
      job: opt.job,
      // Ссылка на страницу джобы: в отчёте-плане (после «проверить») это
      // единственная ссылка в Jenkins, пока билда нет.
      jobUrl: opt.job ? jobPageUrl(opt.job) : null,
      etalonExists: etalon.exists,
      etalonCount: etalon.count,
      etalonPin: etalon.pin ?? null,
      blocked,
      blockedReason: blocked
        ? `эталон ${opt.id}/${modeUp} (${etalon.count} номеров) не пересекается с ${res.launchTeamNumbers.size} номерами команды в запуске — проверь режим QG/RM/HF`
        : null,
      nonPassed: res.rerunNumbers.length,
      newFailed: res.newFailed.length,
      skipped: res.skippedNumbers.length,
      // count — то, что реально уйдёт в Jenkins (0 у заблокированных).
      count: sendable.length,
      suppressed: blocked ? numbers.length : 0,
      tags: buildTags(sendable),
    })
  }

  if (teamsWithoutEtalon.length > 0) {
    warnings.push(
      `нет эталона ${modeUp} у команд: ${teamsWithoutEtalon.join(", ")} — их номера не сверены с эталоном `
        + "(фильтр только по полю Team), @-список может быть шире, чем у скилла; "
        + "скачать эталон: scripts/fetch_etalons.py",
    )
  }
  if (teamsWithBadPin.length > 0) {
    warnings.push(
      `эталон не совпадает с пломбой у команд: ${teamsWithBadPin.join(", ")} — `
        + "перекачать эталон (fetch_etalons.py); раздача по ним недостоверна",
    )
  }
  const blockedTeams = teams.filter((t) => t.blocked)
  if (blockedTeams.length > 0) {
    warnings.push(
      `ЭТАЛОН НЕ ПЕРЕСЕКАЕТСЯ С ЗАПУСКОМ у команд: ${blockedTeams.map((t) => t.team).join(", ")} — `
        + "вероятно, неверный режим QG/RM/HF; их @-список недействителен, запуск заблокирован (как в скилле)",
    )
  }

  // Тесты с тегом, у которых команда не определилась: отправить их некуда.
  let unassigned = 0
  if (tagged.ids.size > 0) {
    for (const row of results.rows) {
      if (row?.testCaseId === null || row?.testCaseId === undefined) continue
      if (!tagged.ids.has(row.testCaseId)) continue
      const status = String(row?.status ?? "").toLowerCase()
      if (!NON_PASS.has(status)) continue
      const number = testNumber(row?.name)
      const canon = teamOfCase.get(row.testCaseId)
        || (number ? teamByEtalonNumber.get(number) : "")
        || launchTeam
        || ""
      if (!canon) unassigned += 1
    }
  }
  if (unassigned > 0) {
    warnings.push(`${unassigned} тестов с тегом «${tag}» без определённой команды — в раздачу не попали`)
  }

  teams.sort((a, b) => b.count - a.count)
  return {
    launch: {
      id: launchId,
      url: launchUrl(cfg.url, launchId),
      name: String(launch?.name ?? `launch ${launchId}`).trim(),
      modified: launch?.lastModifiedDate
        ? new Date(launch.lastModifiedDate).toISOString().slice(0, 16).replace("T", " ")
        : null,
    },
    mode: modeUp,
    teams,
    withFailures: teams.filter((t) => t.count > 0).length,
    totalTests: teams.reduce((s, t) => s + t.count, 0),
    partial: skipped > 0 || !results.complete,
    warnings,
  }
}

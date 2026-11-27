// Реран упавших тестов через командную Jenkins-джобу DEBUG/<TEAM>.
//
// Повторяет режим dispatch скилла (dispatch_team_reruns.py):
//   POST {base}/job/DEBUG/job/{TEAM}/buildWithParameters
//     tags = "@01.10.04 @02.01.06 ..." (разделитель ПРОБЕЛ, дедуп, естественная сортировка)
//     env = qa · parallel = 15 · rerun = 1 (боевые значения, дефолты джобы не использовать)
//   Basic-auth, форма x-www-form-urlencoded; успех = 200/201 → Location (queue_url).
//
// 🔴 Два правила скилла, которые здесь обязательны:
//   1) пустой @-список НЕ отправляется никогда (джоба прогнала бы полный набор);
//   2) команда без упавших тестов НЕ запускается (dispatch: status "skipped_empty").
//
// Креды Jenkins берём из уже настроенного MCP-сервера jenkins в opencode.json,
// чтобы не дублировать секрет в коде плагина.
import { readFileSync } from "node:fs"
import { homedir } from "node:os"
import { join } from "node:path"
import { request } from "node:https"
import { buildTags, cleanNumber, verifyTags } from "./tags.mjs"
import { jobForTeam } from "./teams.mjs"

// ── креды Jenkins ────────────────────────────────────────────────────────────

function readOpencode() {
  const p = join(homedir(), ".config", "opencode", "opencode.json")
  const raw = readFileSync(p, "utf-8").replace(/^\uFEFF+/, "")
  return JSON.parse(raw)
}

// Поддержаны обе раскладки конфига (как в dispatch_team_reruns.py:_jenkins_env_from):
// плоская mcp.jenkins.environment и словарная mcp.servers.jenkins.environment.
// Последний резерв — переменные окружения процесса.
export function jenkinsConfig() {
  let env = {}
  try {
    const mcp = readOpencode().mcp ?? {}
    env = mcp?.servers?.jenkins?.environment ?? mcp?.jenkins?.environment ?? {}
  } catch {
    env = {}
  }
  const url = env.JENKINS_URL ?? process.env.JENKINS_URL
  const user = env.JENKINS_USERNAME ?? process.env.JENKINS_USERNAME
  const token = env.JENKINS_PASSWORD ?? process.env.JENKINS_PASSWORD
  if (!url || !user || !token) {
    throw new Error("креды Jenkins не найдены: проверь mcp.servers.jenkins.environment в opencode.json")
  }
  return { url: String(url).replace(/\/+$/, ""), user: String(user), token: String(token) }
}

// ── @-список для параметра tags ──────────────────────────────────────────────

export { buildTags }

// ── Jenkins HTTP ─────────────────────────────────────────────────────────────

// `DEBUG/BIT` → `{base}/job/DEBUG/job/BIT`.
function jobUrl(base, job) {
  const segments = String(job ?? "").split("/").filter(Boolean)
  return base.replace(/\/+$/, "") + segments.map((s) => `/job/${encodeURIComponent(s)}`).join("")
}

export function jobPageUrl(job) {
  try {
    return jobUrl(jenkinsConfig().url, job) + "/"
  } catch {
    return null
  }
}

function httpPost(url, { headers, body }) {
  return new Promise((resolve, reject) => {
    const req = request(url, { method: "POST", rejectUnauthorized: false, headers, timeout: 30_000 }, (res) => {
      let data = ""
      res.setEncoding("utf-8")
      res.on("data", (c) => (data += c))
      res.on("end", () => resolve({ status: res.statusCode, text: data, location: res.headers.location ?? null }))
    })
    req.on("timeout", () => req.destroy(new Error("таймаут Jenkins 30с")))
    req.on("error", reject)
    if (body) req.write(body)
    req.end()
  })
}

// Jenkins при повторной постановке в очередь отвечает «Queue item exists» и
// ссылкой в теле, без заголовка Location. Это НЕ ошибка: билд уже в очереди,
// нужно только достать его URL из текста (…/queue/item/<id>/…).
function extractQueueUrl(text, base) {
  const m = String(text ?? "").match(/\/queue\/item\/(\d+)/)
  if (!m) return null
  return `${base.replace(/\/+$/, "")}/queue/item/${m[1]}/`
}

export async function triggerJob(cfg, job, params) {
  // Последний рубеж: пустой tags означает «прогнать полный набор» — это не реран.
  if (Object.prototype.hasOwnProperty.call(params ?? {}, "tags") && !String(params.tags ?? "").trim()) {
    throw new Error("пустой параметр tags — запуск отменён (джоба прогнала бы полный набор)")
  }
  const url = jobUrl(cfg.url, job) + "/buildWithParameters"
  const auth = `Basic ${Buffer.from(`${cfg.user}:${cfg.token}`).toString("base64")}`
  const body = new URLSearchParams(
    Object.fromEntries(Object.entries(params ?? {}).filter(([, v]) => v !== undefined && v !== null && v !== "")),
  ).toString()
  const res = await httpPost(url, {
    headers: {
      Authorization: auth,
      "Content-Type": "application/x-www-form-urlencoded",
      Accept: "application/json",
    },
    body,
  })
  // «Queue item exists» — штатный ответ, когда билд этой джобы уже в очереди.
  const queueExists = /queue item exists/i.test(res.text)
  if (res.status !== 201 && res.status !== 200 && !queueExists) {
    throw new Error(`Jenkins вернул HTTP ${res.status}: ${String(res.text).slice(0, 200)}`)
  }
  const queueUrl = res.location ?? extractQueueUrl(res.text, cfg.url)
  return { ok: true, queueUrl, status: res.status, note: queueExists ? "queue item exists" : null }
}

// Очередь → ссылка на билд. Берём executable.url напрямую (…/job/DEBUG/<TEAM>/<n>/),
// а не склеиваем из queue-URL (та склейка давала битый путь …/queue/item/701//5/).
// Пара повторов, пока Jenkins не начнёт сборку из очереди.
function getJson(url, auth, timeout = 10_000) {
  return new Promise((resolve, reject) => {
    const req = request(url, { rejectUnauthorized: false, headers: { Authorization: auth, Accept: "application/json" }, timeout }, (res) => {
      let d = ""
      res.on("data", (c) => (d += c))
      res.on("end", () => { try { resolve(JSON.parse(d)) } catch { reject(new Error("не JSON из очереди")) } })
    })
    req.on("timeout", () => req.destroy(new Error("таймаут очереди")))
    req.on("error", reject)
    req.end()
  })
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

export async function resolveBuildUrl(cfg, queueUrl, attempts = 10, delayMs = 2000) {
  if (!queueUrl) return null
  const auth = `Basic ${Buffer.from(`${cfg.user}:${cfg.token}`).toString("base64")}`
  const apiUrl = queueUrl.replace(/\/+$/, "") + "/api/json?tree=cancelled,executable[url,number]"
  for (let i = 0; i < attempts; i++) {
    try {
      const q = await getJson(apiUrl, auth)
      if (q?.executable?.url) return q.executable.url
      if (q?.cancelled) return null
    } catch { /* повторим */ }
    if (i < attempts - 1) await sleep(delayMs)
  }
  return null
}

// ── идемпотентность ──────────────────────────────────────────────────────────
//
// Скилл не отправляет один и тот же набор дважды (dispatch.json + already_dispatched).
// В панели роль журнала играет этот кеш: повторный клик по «подтвердить» не должен
// ставить второй билд с теми же tags.
const DISPATCH_TTL_MS = 15 * 60_000
const dispatchLog = new Map()

function dispatchKey(job, tags) {
  return `${job}|${tags}`
}

function rememberDispatch(job, tags, result) {
  const now = Date.now()
  for (const [k, v] of dispatchLog) if (now - v.at > DISPATCH_TTL_MS) dispatchLog.delete(k)
  dispatchLog.set(dispatchKey(job, tags), { at: now, ...result })
}

function recallDispatch(job, tags) {
  const hit = dispatchLog.get(dispatchKey(job, tags))
  if (!hit) return null
  if (Date.now() - hit.at > DISPATCH_TTL_MS) { dispatchLog.delete(dispatchKey(job, tags)); return null }
  return hit
}

// ── слой для плагина ─────────────────────────────────────────────────────────

// Параметры джобы из реестра, кроме tags/env/parallel/rerun: скилл передаёт
// browser / browserVersion / branch, если они заданы в team_jobs.json.
function extraParams(conf) {
  const out = {}
  for (const key of ["browser", "browserVersion", "branch"]) {
    if (conf?.[key]) out[key] = String(conf[key])
  }
  return out
}

// План рерана без запуска: джоба, число тестов, готовый @-список.
export function rerunPreview({ teamCanon, nonPassedNumbers, newFailedNumbers }) {
  const conf = jobForTeam(teamCanon)
  if (!conf) throw new Error(`команда «${teamCanon}» не найдена в team_jobs.json`)
  if (!conf.job) throw new Error(`у команды ${teamCanon} нет Jenkins-джобы в team_jobs.json`)
  if (!conf.enabled) throw new Error(`команда ${teamCanon} отключена в реестре (enabled: false)`)

  const numbers = [...new Set([...(nonPassedNumbers ?? []), ...(newFailedNumbers ?? [])].map(cleanNumber).filter(Boolean))]
  const tags = buildTags(numbers)
  const check = verifyTags(tags, numbers.length)
  if (!check.ok) throw new Error(`@-список не прошёл проверку: ${check.problems.join("; ")}`)

  return {
    team: teamCanon,
    job: conf.job,
    // Страница джобы: нужна в отчёте-плане (после «проверить», до запуска).
    jobUrl: jobPageUrl(conf.job),
    env: conf.env,
    parallel: conf.parallel,
    rerun: conf.rerun,
    extra: extraParams(conf),
    count: numbers.length,
    tags,
  }
}

// Запуск джобы рерана. Пустой @-список — запрет скилла: дефолтный tags джобы
// прогнал бы полный набор, поэтому не запускаем. Блокировка эталона — тоже стоп:
// такой @-список собран по неверному эталону.
export async function rerunTeam({ teamCanon, nonPassedNumbers, newFailedNumbers, etalonMismatch: mismatch = false, mismatchReason = null }) {
  if (mismatch) {
    throw new Error(mismatchReason ?? `эталон ${teamCanon} не пересекается с запуском — реран не запускался (проверь режим QG/RM/HF)`)
  }
  const plan = rerunPreview({ teamCanon, nonPassedNumbers, newFailedNumbers })
  if (plan.count === 0) {
    throw new Error(`у команды ${teamCanon} пустой @-список — реран не запускался (иначе джоба прогонит полный набор)`)
  }

  const prev = recallDispatch(plan.job, plan.tags)
  if (prev) {
    return { ...plan, queueUrl: prev.queueUrl, buildUrl: prev.buildUrl, note: "already_dispatched", status: "already_dispatched" }
  }

  const cfg = jenkinsConfig()
  const params = {
    tags: plan.tags,
    env: plan.env,
    parallel: plan.parallel,
    rerun: plan.rerun,
    ...plan.extra,
  }
  const result = await triggerJob(cfg, plan.job, params)
  const buildUrl = await resolveBuildUrl(cfg, result.queueUrl)
  rememberDispatch(plan.job, plan.tags, { queueUrl: result.queueUrl, buildUrl })
  return { ...plan, params, queueUrl: result.queueUrl, buildUrl, note: result.note, status: "triggered" }
}

// ── fan-out: запуск джоб по всем командам ────────────────────────────────────
//
// Вход — массив команд из collectTeamReruns() (team, job, tags, count).
//  • count > 0  → запуск с tags (@-список упавших);
//  • count = 0  → НЕ запускаем (dispatch скилла: skipped_empty), статус no_failures.
// Все запускаемые команды стартуют параллельно, затем параллельно ждём ссылки.
export async function rerunAllTeams(teams) {
  const cfg = jenkinsConfig()
  return Promise.all((teams ?? []).map(async (t) => {
    const conf = jobForTeam(t.team)
    if (!conf || !conf.job) {
      return { team: t.team, count: t.count, tags: t.tags ?? "", status: "no_job", error: "нет Jenkins-джобы в team_jobs.json" }
    }
    if (!conf.enabled) {
      return {
        team: t.team, job: conf.job, count: t.count, tags: t.tags ?? "", status: "disabled",
        jobUrl: jobUrl(cfg.url, conf.job) + "/",
        error: "команда отключена в реестре",
      }
    }

    // 🔴 Гейт эталона: эталон команды не пересекается с запуском → @-список
    // построен на неверном эталоне. Проверяется ДО count: у заблокированной
    // команды count уже 0, и без этого она отчиталась бы как no_failures —
    // то есть причина блокировки потерялась бы.
    if (t.blocked) {
      return {
        team: t.team, job: conf.job, count: t.count, suppressed: t.suppressed ?? 0, tags: t.tags ?? "",
        status: "etalon_mismatch",
        error: t.blockedReason ?? "эталон не пересекается с запуском — вероятно, неверный режим или команда",
      }
    }

    if (!t.count) {
      return {
        team: t.team,
        job: conf.job,
        count: 0,
        tags: "",
        status: "no_failures",
        jobUrl: jobUrl(cfg.url, conf.job) + "/",
      }
    }

    const check = verifyTags(t.tags, t.count)
    if (!check.ok) {
      return { team: t.team, job: conf.job, count: t.count, tags: t.tags, status: "error", error: `@-список не прошёл проверку: ${check.problems.join("; ")}` }
    }
    const prev = recallDispatch(conf.job, t.tags)
    if (prev) {
      return {
        team: t.team, job: conf.job, count: t.count, tags: t.tags, status: "already_dispatched",
        queueUrl: prev.queueUrl, buildUrl: prev.buildUrl, note: "already_dispatched",
      }
    }

    try {
      const params = {
        tags: t.tags,
        env: conf.env,
        parallel: conf.parallel,
        rerun: conf.rerun,
        ...extraParams(conf),
      }
      const r = await triggerJob(cfg, conf.job, params)
      const buildUrl = await resolveBuildUrl(cfg, r.queueUrl)
      rememberDispatch(conf.job, t.tags, { queueUrl: r.queueUrl, buildUrl })
      return { team: t.team, job: conf.job, count: t.count, tags: t.tags, status: "triggered", queueUrl: r.queueUrl, buildUrl, note: r.note }
    } catch (e) {
      return { team: t.team, job: conf.job, count: t.count, tags: t.tags, status: "error", error: String(e?.message ?? e) }
    }
  }))
}

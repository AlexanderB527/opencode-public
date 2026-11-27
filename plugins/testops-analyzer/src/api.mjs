// HTTP-слой TestOps для плагина: конфиг, JWT, GET с повтором при 401.
//
// Логика один-в-один со skills/testops-analysis/scripts/scan_testops.py:
//   1. POST /api/uaa/oauth/token  {grant_type: apitoken, scope: openid, token: <PAT>} -> JWT
//   2. дальше Authorization: Bearer <JWT>
//   3. при 401 — сброс JWT, переполучение, один повтор.
//
// Почему node:https, а не fetch: TestOps подписан внутренним CA банка,
// глобальный fetch валидирует сертификат и падает (проверено на Jira/TestOps).
import { request } from "node:https"
import { existsSync, readFileSync } from "node:fs"
import { homedir } from "node:os"
import { join } from "node:path"

const TIMEOUT_MS = 60_000

// JWT кешируется на процесс; ключ — base url, чтобы смена стенда не путала токены.
let jwtCache = { base: null, jwt: null }

function readJsonFile(p) {
  const raw = readFileSync(p, "utf-8")
  return JSON.parse(raw.replace(/^\uFEFF+/, ""))
}

// Адрес и PAT берём из уже настроенного MCP-сервера testops в opencode.json,
// чтобы не дублировать секрет в коде плагина. Резерв — конфиг скилла
// (~/.qa-artifacts/config.json + TESTOPS_TOKEN), если MCP не настроен.
export function testopsConfig() {
  let url = null
  let token = null

  try {
    const cfgPath = join(homedir(), ".config", "opencode", "opencode.json")
    const cmd = readJsonFile(cfgPath).mcp?.servers?.testops?.command ?? []
    url = cmd[cmd.indexOf("--url") + 1] ?? null
    token = cmd[cmd.indexOf("--token") + 1] ?? null
  } catch {
    /* нет opencode.json — попробуем конфиг скилла */
  }

  if (!url || !token) {
    const qaCfgPath = join(process.env.QA_ARTIFACTS_ROOT || join(homedir(), ".qa-artifacts"), "config.json")
    if (existsSync(qaCfgPath)) {
      try {
        const qa = readJsonFile(qaCfgPath)
        url = url ?? qa.testops_base_url ?? null
        token = token ?? process.env[qa.testops_token_env || "TESTOPS_TOKEN"] ?? qa.testops_token ?? null
      } catch {
        /* конфиг скилла битый — ниже отдадим внятную ошибку */
      }
    }
  }

  if (!url || !token) {
    throw new Error(
      "TestOps url/token не найдены: проверь mcp.servers.testops.command в opencode.json "
        + "(или ~/.qa-artifacts/config.json)."
    )
  }
  return { url: String(url).replace(/\/+$/, ""), token: String(token) }
}

function httpsRaw(url, { method = "GET", headers = {}, body = null } = {}) {
  return new Promise((resolve, reject) => {
    const req = request(url, { method, rejectUnauthorized: false, headers, timeout: TIMEOUT_MS }, (res) => {
      let data = ""
      res.setEncoding("utf-8")
      res.on("data", (chunk) => (data += chunk))
      res.on("end", () => resolve({ status: res.statusCode, text: data }))
    })
    req.on("timeout", () => req.destroy(new Error(`таймаут ${TIMEOUT_MS}мс`)))
    req.on("error", reject)
    if (body) req.write(body)
    req.end()
  })
}

async function acquireJwt(base, token) {
  const body = new URLSearchParams({ grant_type: "apitoken", scope: "openid", token }).toString()
  const res = await httpsRaw(`${base}/api/uaa/oauth/token`, {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/x-www-form-urlencoded" },
    body,
  })
  let json = null
  try { json = JSON.parse(res.text) } catch { /* не JSON — покажем текст ниже */ }
  const jwt = json?.access_token
  if (res.status !== 200 || !jwt) {
    throw new Error(`TestOps JWT: HTTP ${res.status} ${String(res.text).slice(0, 200)}`)
  }
  jwtCache = { base, jwt }
  return jwt
}

// GET + JSON. При 401 — один повтор с новым JWT.
export async function apiGet(path, { cfg = testopsConfig(), retries = 1 } = {}) {
  let lastError = null
  for (let attempt = 0; attempt <= retries; attempt++) {
    const jwt = jwtCache.base === cfg.url && jwtCache.jwt
      ? jwtCache.jwt
      : await acquireJwt(cfg.url, cfg.token)
    const res = await httpsRaw(`${cfg.url}${path}`, {
      headers: { Accept: "application/json", Authorization: `Bearer ${jwt}` },
    })
    if (res.status === 200) {
      try {
        return JSON.parse(res.text)
      } catch {
        throw new Error(`TestOps ответил не-JSON на ${path}: ${String(res.text).slice(0, 160)}`)
      }
    }
    lastError = `HTTP ${res.status} ${String(res.text).slice(0, 160)}`
    if (res.status === 401) {
      jwtCache = { base: null, jwt: null }
      continue
    }
    break // 403/404 и прочее — ретраить бессмысленно
  }
  throw new Error(`TestOps GET ${path}: ${lastError}`)
}

// Ссылка вида https://example.invalid launchId=1356126 или просто число.
export function parseLaunchId(input) {
  const s = String(input ?? "").trim()
  if (!s) throw new Error("пустая ссылка на запуск")
  if (/^\d+$/.test(s)) return Number(s)
  const byPath = s.match(/\/launch\/(\d+)/)
  if (byPath) return Number(byPath[1])
  const byQuery = s.match(/[?&]launchId=(\d+)/)
  if (byQuery) return Number(byQuery[1])
  throw new Error("в ссылке нет id запуска (ожидается …/launch/<id>)")
}

export function launchUrl(base, id) {
  return `${base}/launch/${id}`
}

export function testcaseUrl(base, id) {
  return `${base}/testcase/${id}`
}

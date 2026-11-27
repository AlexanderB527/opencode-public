// Нормализация имён команд TestOps — порт skills/testops-analysis/scripts/team_canon.py.
//
// Зачем: в TestOps названия команд приходят с кириллическими гомоглифами
// («ТАМUL», «ТЕ», «ТММБ», «ТСПб», «ТX»), а канон папок эталонов и реестра
// джоб — латиница. Без полного транслита получаются мусорные имена вида «TАMUL».
import { existsSync, readFileSync } from "node:fs"
import { homedir } from "node:os"
import { join } from "node:path"

// Кириллица → латиница, замена 1:1 (длины строк сохраняются — на этом
// держится срез префикса «SFA Команда»).
const TEAM_TRANSLIT = {
  "А": "A", "Б": "B", "В": "B", "Г": "G", "Д": "D", "Е": "E", "Ё": "E",
  "Ж": "Z", "З": "Z", "И": "I", "Й": "Y", "К": "K", "Л": "L", "М": "M",
  "Н": "H", "О": "O", "П": "P", "Р": "P", "С": "S", "Т": "T", "У": "U",
  "Ф": "F", "Х": "X", "Ц": "C", "Ч": "C", "Ш": "S", "Щ": "S", "Ы": "Y",
  "Э": "E", "Ю": "U", "Я": "Y",
  "а": "A", "б": "B", "в": "B", "г": "G", "д": "D", "е": "E", "ё": "E",
  "ж": "Z", "з": "Z", "и": "I", "й": "Y", "к": "K", "л": "L", "м": "M",
  "н": "H", "о": "O", "п": "P", "р": "P", "с": "S", "т": "T", "у": "U",
  "ф": "F", "х": "X", "ц": "C", "ч": "C", "ш": "S", "щ": "S", "ы": "Y",
  "э": "E", "ю": "U", "я": "Y",
}

export const CANON_TEAMS = [
  "BIT", "ECOM", "TACO", "TAMUL", "TCAT", "TE", "TFU",
  "TMMB", "TPR", "TREX", "TSPB", "TT", "TX-T9",
]

const CANON_SET = new Set(CANON_TEAMS)
const TEAM_PREFIXES = ["SFAКОМАНДА", "SFAКОМАНДЫ"]
const DEFAULT_ALIASES = {
  T9: "TX-T9",
  TX: "TX-T9",
  "T9-TX": "TX-T9",
  "TX-T9": "TX-T9",
  TSPB: "TSPB",
}

export const MODES = ["QG", "RM", "HF"]

function translit(value) {
  if (!value) return ""
  let out = ""
  for (const ch of String(value)) out += TEAM_TRANSLIT[ch] ?? ch
  return out
}

function stripTeamPrefix(value) {
  const text = String(value ?? "").trim()
  if (!text) return ""
  const compact = text.split(/\s+/).join("")
  const probe = translit(compact).toUpperCase()
  for (const prefix of TEAM_PREFIXES) {
    if (probe.startsWith(translit(prefix).toUpperCase())) return compact.slice(prefix.length)
  }
  return text
}

export function normalizeTeam(value) {
  if (value === null || value === undefined) return ""
  const text = stripTeamPrefix(String(value).trim())
  return translit(text.split(/\s+/).join("")).toUpperCase()
}

// Реестр командных джоб — единый источник скилла (13 команд + алиасы).
export function teamJobsPath() {
  return join(homedir(), ".config", "opencode", "skills", "testops-analysis", "config", "team_jobs.json")
}

export function loadTeamJobs() {
  const p = teamJobsPath()
  if (!existsSync(p)) return {}
  try {
    const raw = readFileSync(p, "utf-8").replace(/^\uFEFF+/, "")
    return JSON.parse(raw)
  } catch {
    return {}
  }
}

// Список для селекта: только включённые команды, в порядке канона.
export function teamOptions() {
  const jobs = loadTeamJobs()
  return CANON_TEAMS.map((key) => ({
    id: key,
    job: jobs[key]?.job ?? null,
    enabled: jobs[key]?.enabled !== false,
    aliases: jobs[key]?.aliases ?? [],
  })).filter((t) => t.enabled)
}

// Jenkins-джоба команды для рерана: DEBUG/<TEAM> + боевые параметры.
// Дефолты совпадают со скиллом (dispatch_team_reruns.py): env=qa, parallel=15, rerun=1.
export function jobForTeam(canon) {
  const conf = loadTeamJobs()[canon]
  if (!conf) return null
  return {
    canon,
    job: conf.job ?? null,
    env: conf.env ?? "qa",
    parallel: String(conf.parallel ?? "15"),
    rerun: String(conf.rerun ?? "1"),
    enabled: conf.enabled !== false,
  }
}

function registryAliasMap(registry) {
  const mapping = new Map()
  for (const [key, conf] of Object.entries(registry ?? {})) {
    if (key.startsWith("_")) continue
    const canon = key
    if (!mapping.has(normalizeTeam(canon))) mapping.set(normalizeTeam(canon), canon)
    for (const alias of conf?.aliases ?? []) {
      if (!mapping.has(normalizeTeam(alias))) mapping.set(normalizeTeam(alias), canon)
    }
  }
  return mapping
}

// Порядок как в team_canon.resolve_canon: канон → алиас реестра → нормализованный канон → встроенные алиасы.
export function resolveTeam(value, registry = loadTeamJobs()) {
  const raw = String(value ?? "").trim()
  if (!raw) return null
  if (CANON_SET.has(raw.toUpperCase())) return raw.toUpperCase()

  const norm = normalizeTeam(raw)
  const mapping = registryAliasMap(registry)
  if (mapping.has(norm)) return mapping.get(norm)
  if (CANON_SET.has(norm)) return norm
  if (DEFAULT_ALIASES[norm]) return DEFAULT_ALIASES[norm]
  return null
}

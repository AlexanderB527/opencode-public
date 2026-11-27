// Последние настройки панели: ссылка, команда, тег, режим, тип прогона.
// Хранится рядом с плагином в JSON — переживает перезапуск приложения.
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"

const HERE = dirname(fileURLToPath(import.meta.url))
const STORE = join(HERE, "..", "data", "state.json")
const RECENT_LIMIT = 10

const EMPTY = { last: null, recent: [] }

// Читаем терпимо: файл мог быть сохранён с UTF-8 BOM — тогда JSON.parse падает.
function readStore() {
  try {
    if (!existsSync(STORE)) return { ...EMPTY }
    const raw = readFileSync(STORE, "utf-8")
    const json = JSON.parse(raw.charCodeAt(0) === 0xfeff ? raw.slice(1) : raw)
    return {
      last: json.last ?? null,
      recent: Array.isArray(json.recent) ? json.recent : [],
    }
  } catch {
    return { ...EMPTY }
  }
}

function writeStore(store) {
  mkdirSync(dirname(STORE), { recursive: true })
  writeFileSync(STORE, JSON.stringify(store, null, 2), "utf-8")
}

export function stateSnapshot() {
  const store = readStore()
  return {
    source: "testops-analyzer-state",
    last: store.last,
    recent: store.recent,
  }
}

// Запоминаем удачный запрос: он же становится значением по умолчанию при открытии панели.
export function remember({ link, team, tag, mode, runKind }) {
  const store = readStore()
  const entry = {
    link: String(link ?? ""),
    team: String(team ?? ""),
    tag: String(tag ?? ""),
    mode: String(mode ?? "QG"),
    runKind: String(runKind ?? "auto"),
    at: new Date().toISOString(),
  }
  store.last = entry
  store.recent = [entry, ...store.recent.filter((x) => x.link !== entry.link)].slice(0, RECENT_LIMIT)
  writeStore(store)
  return stateSnapshot()
}

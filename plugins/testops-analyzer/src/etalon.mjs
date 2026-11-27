// Эталоны команд: ~/.qa-artifacts/etalons/<TEAM>/etalon_<QG|RM|HF>.txt
//
// Источник истины — ветка qa-analysis-data (AUTOTEST/sfa); локально лежит копия,
// которую скачивает скилл (fetch_etalons.py). Плагин эталон НЕ создаёт и не правит:
// только читает, а при отсутствии — предупреждает и фильтрует по одной команде.
//
// 🔴 Пломба: рядом с эталоном лежит <etalon>.sha256 (scan_testops.py:check_etalon_pin).
// Без сверки пломбы подменённый или недокачанный эталон молча меняет фильтр —
// именно поэтому проверка обязательна, а не «желательна».
import { createHash } from "node:crypto"
import { existsSync, readFileSync } from "node:fs"
import { homedir } from "node:os"
import { join } from "node:path"

// Разбор номера и естественная сортировка живут в ядре — один источник на весь плагин.
export { TEST_NUMBER_RE, testNumber, compareNumbers, naturalKey } from "./core.mjs"
import { TEST_NUMBER_RE } from "./core.mjs"

export function artifactsRoot() {
  return process.env.QA_ARTIFACTS_ROOT || join(homedir(), ".qa-artifacts")
}

export function etalonPath(team, mode) {
  return join(artifactsRoot(), "etalons", String(team ?? "").toUpperCase(), `etalon_${String(mode ?? "").toUpperCase()}.txt`)
}

function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex")
}

// Сверка с пломбой <etalon>.sha256. Нет пломбы — не ошибка (старый кэш), но факт виден.
function checkPin(path) {
  const side = `${path}.sha256`
  if (!existsSync(side)) return { checked: false, ok: true, message: "пломбы нет" }
  try {
    const expected = readFileSync(side, "utf-8").trim().toLowerCase()
    const actual = sha256(readFileSync(path))
    if (expected && expected !== actual) {
      return {
        checked: true,
        ok: false,
        message: `эталон не совпадает с пломбой (ожидалось ${expected.slice(0, 12)}…, сейчас ${actual.slice(0, 12)}…)`,
      }
    }
    return { checked: true, ok: true, message: "ok" }
  } catch (e) {
    return { checked: true, ok: false, message: `не прочитать пломбу: ${e?.message ?? e}` }
  }
}

// Возвращает { exists, path, numbers:Set, count, pin }. Не бросает: нет эталона —
// штатная ситуация (команда ещё не качала эталон).
export function readEtalon(team, mode) {
  const path = etalonPath(team, mode)
  if (!team || !existsSync(path)) {
    return { exists: false, path, numbers: new Set(), count: 0, pin: { checked: false, ok: true, message: "эталона нет" } }
  }

  let raw = null
  for (const enc of ["utf-8", "cp1251"]) {
    try {
      raw = readFileSync(path, enc)
      break
    } catch {
      /* пробуем следующую кодировку */
    }
  }
  if (raw === null) {
    return { exists: false, path, numbers: new Set(), count: 0, pin: { checked: false, ok: false, message: "эталон не читается" } }
  }

  const numbers = new Set()
  for (const line of raw.split(/\r?\n/)) {
    const m = TEST_NUMBER_RE.exec(line.trim())
    if (m) numbers.add(m[1])
  }
  return { exists: true, path, numbers, count: numbers.size, pin: checkPin(path) }
}

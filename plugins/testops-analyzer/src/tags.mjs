// @-список тестов для Jenkins: параметр `tags` джобы DEBUG/<TEAM>.
//
// Формат один-в-один со скиллом (dispatch_team_reruns.py:build_tags):
//   номера с префиксом '@', дедуп, естественная сортировка, разделитель — ПРОБЕЛ.
// Пример: "@01.10.04 @02.01.06 @10.31.06 ..."
import { compareNumbers } from "./core.mjs"

export function withAt(number) {
  const s = String(number ?? "").trim()
  return s.startsWith("@") ? s : `@${s}`
}

// _clean_number: запятая ломает rake-вызов, пробелы внутри — тоже.
export function cleanNumber(number) {
  return String(number ?? "").replace(/[,，]/g, "").replace(/\s+/g, "")
}

export function buildTags(numbers) {
  const unique = [...new Set((numbers ?? []).map(cleanNumber).filter(Boolean))]
  unique.sort(compareNumbers)
  return unique.map(withAt).join(" ")
}

export function countTags(tagsText) {
  const text = String(tagsText ?? "").trim()
  return text ? text.split(/\s+/).length : 0
}

// Проверка готового @-списка перед отправкой в Jenkins.
// Ловит ровно те дефекты, которые джоба не заметит, а прогон — заметит:
// пустой токен, номер без @, дубль, мусор в номере, расхождение с ожидаемым числом.
export function verifyTags(tagsText, expectedCount = null) {
  const problems = []
  const text = String(tagsText ?? "").trim()
  const tokens = text ? text.split(/\s+/) : []
  if (!text) problems.push("пустой @-список")
  const seen = new Set()
  for (const tok of tokens) {
    if (!tok.startsWith("@")) problems.push(`токен без @: «${tok}»`)
    const bare = tok.replace(/^@/, "")
    if (!/^\d{2}(?:\.\d{2,3})+$/.test(bare)) problems.push(`номер не по формату: «${tok}»`)
    if (bare !== cleanNumber(bare)) problems.push(`в номере мусор: «${tok}»`)
    if (seen.has(bare)) problems.push(`дубль: «${tok}»`)
    seen.add(bare)
  }
  if (expectedCount !== null && tokens.length !== expectedCount) {
    problems.push(`в списке ${tokens.length} номеров, ожидалось ${expectedCount}`)
  }
  return { ok: problems.length === 0, count: tokens.length, problems }
}

// Обрезка готового @-списка для промпта/чата: полный список из сотен номеров
// не должен забивать контекст. Принимает уже собранную строку tags.
// Возвращает { text, truncated, total } — text можно вставлять в параметр tags как есть.
export function truncateTags(tagsText, { maxChars = 2000 } = {}) {
  const full = String(tagsText ?? "").trim()
  const total = countTags(full)
  if (full.length <= maxChars) return { text: full, truncated: false, total }

  // Режем по целым токенам, чтобы не оборвать номер на середине.
  let cut = ""
  for (const token of full.split(/\s+/)) {
    const next = cut ? `${cut} ${token}` : token
    if (next.length > maxChars) break
    cut = next
  }
  return { text: cut, truncated: true, total }
}

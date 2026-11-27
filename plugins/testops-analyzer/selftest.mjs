// Ручная проверка server-части без OpenCode: node selftest.mjs [launchId] [TEAM] [MODE] [tag]
//
// Без аргументов — покажет последние запуски проекта, чтобы выбрать id.
// С аргументом — соберёт снимок теми же функциями, что и панель, и напечатает сводку.
import { apiGet, parseLaunchId, testopsConfig } from "./src/api.mjs"
import { buildBrief } from "./src/brief.mjs"
import { buildPrompt } from "./src/prompt.mjs"
import { teamOptions } from "./src/teams.mjs"
import { readEtalon } from "./src/etalon.mjs"

const [rawLaunch, rawTeamArg = "TX-T9", rawMode = "QG", rawTag = ""] = process.argv.slice(2)
// PowerShell выбрасывает пустые аргументы, поэтому «без фильтра» передаём как «-».
const rawTeam = rawTeamArg === "-" ? "" : rawTeamArg

if (!rawLaunch) {
  const cfg = testopsConfig()
  const data = await apiGet(`/api/rs/launch?projectId=118&page=0&size=15`)
  const items = data?.content ?? data?.items ?? []
  console.log(`стенд: ${cfg.url}`)
  console.log(`команды в реестре: ${teamOptions().map((t) => t.id).join(", ")}`)
  console.log("\nпоследние запуски проекта 118:")
  for (const x of items) {
    const ts = x.lastModifiedDate ? new Date(x.lastModifiedDate).toISOString().slice(0, 16).replace("T", " ") : "—"
    console.log(`  ${String(x.id).padEnd(9)} ${ts}  ${x.name}`)
  }
  console.log("\nпример: node selftest.mjs <launchId> TX-T9 QG <тег_тестера>")
  process.exit(0)
}

const id = parseLaunchId(rawLaunch)
console.log(`эталон ${rawTeam}/${rawMode}: ${JSON.stringify(
  (() => {
    const e = readEtalon(rawTeam, rawMode)
    return { exists: e.exists, count: e.count, path: e.path }
  })(),
)}`)

const started = Date.now()
const brief = await buildBrief({ link: String(id), team: rawTeam, tag: rawTag, mode: rawMode, runKind: "auto" })
const elapsed = ((Date.now() - started) / 1000).toFixed(1)

console.log(`\nзапуск: ${brief.launch.name} (${brief.launch.url})`)
console.log(`изменён: ${brief.launch.modified} · закрыт: ${brief.launch.closed} · проект: ${brief.launch.projectId}`)
console.log(`режим: ${brief.filters.mode} · команда: ${brief.filters.teamCanon} · тег: ${brief.filters.tag || "—"}`)
console.log(`тип прогона: ${brief.runKind.value} (${brief.runKind.source})${brief.runKind.reasons.length ? ` — ${brief.runKind.reasons.join("; ")}` : ""}`)
console.log(`весь запуск: ${JSON.stringify(brief.launchStatistic)}`)
console.log(`по фильтру (по номерам): total=${brief.filtered.total} passed=${brief.filtered.passed} failed=${brief.filtered.failed}`
  + ` broken=${brief.filtered.broken} skipped=${brief.filtered.skipped} passRate=${brief.filtered.passRate}%`)
console.log(`строк результатов: ${brief.filtered.results} (passed=${brief.filtered.passedRows} failed=${brief.filtered.failedRows}`
  + ` broken=${brief.filtered.brokenRows} skipped=${brief.filtered.skippedRows})`)
console.log(`уникальных номеров с ошибками: ${brief.filtered.nonPassedNumbers} · по тегу: ${brief.filtered.includedByTag}`)
console.log(`время: ${elapsed}s`)

if (brief.warnings.length) {
  console.log("\nпредупреждения:")
  for (const w of brief.warnings) console.log(`  - ${w}`)
}

console.log(`\nпервые упавшие:`)
for (const r of brief.nonPassed.slice(0, 10)) console.log(`  @${r.number || "—"} ${r.status} ${r.origin} ${r.name}`)

console.log("\n--- промпт для сессии ---")
console.log(buildPrompt(brief))

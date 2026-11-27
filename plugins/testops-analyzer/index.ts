// Server-часть плагина «Разбор автотестов TestOps».
//
// Два потребителя:
//   1. GUI-панель — через ctx.rpc (brief/analyze), данные считаются в Node;
//   2. модель — через tool testops_analyzer_brief (тот же расчёт, текстовый рендер).
//
// Расчёт фильтров повторяет скилл testops-analysis (команда ИЛИ тег тестировщика,
// эталоны QG/RM/HF), но выполняется по REST прямо в плагине — без python.
import { Plugin } from "@opencode/plugin"
import { TestopsAnalyzerRpc } from "./src/rpc.ts"
import { buildBrief } from "./src/brief.mjs"
import { buildPrompt } from "./src/prompt.mjs"
import { remember, stateSnapshot } from "./src/state.mjs"
import { MODES, teamOptions } from "./src/teams.mjs"
import { rerunPreview, rerunTeam, rerunAllTeams } from "./src/dispatch.mjs"
import { truncateTags } from "./src/tags.mjs"
import { collectTeamReruns } from "./src/brief.mjs"
import { TTL, cacheKey, markCache, withCache } from "./src/cache.mjs"
import { registerCodeMode } from "./src/code-mode/tools.ts"

// ── сетевые ошибки по-человечески ────────────────────────────────────────────

function friendly(e) {
  const msg = String(e?.message ?? e)
  if (msg.includes("ENOTFOUND")) {
    const host = (msg.match(/ENOTFOUND\s+(\S+)/) ?? [])[1] ?? "внутренний хост"
    return `Нет доступа к ${host}: не резолвится DNS. Проверь VPN / корпоративную сеть.`
  }
  if (msg.includes("ETIMEDOUT") || msg.includes("timeout") || msg.includes("таймаут")) {
    return "TestOps не ответил вовремя. Проверь VPN."
  }
  if (msg.includes("401")) {
    return "TestOps отклонил авторизацию (401): проверь токен в opencode.json (mcp.servers.testops.command)."
  }
  if (msg.includes("ECONNREFUSED")) return "Соединение с TestOps отклонено — проверь адрес стенда."
  return msg
}

// RPC и tool НИКОГДА не бросают наружу: платформа отвечает HTTP 500 и панель
// показывает «RPC call failed» без причины. Отдаём { error } — UI покажет текст.
function safe(promise) {
  return Promise.resolve(promise).catch((e) => JSON.stringify({ error: friendly(e) }))
}

function briefParams(input) {
  return {
    link: String(input?.link ?? ""),
    team: String(input?.team ?? ""),
    tag: String(input?.tag ?? ""),
    mode: String(input?.mode ?? "QG").toUpperCase(),
    runKind: String(input?.runKind ?? "auto"),
    withMessages: Boolean(input?.withMessages),
  }
}

// Снимок кешируется по набору параметров: смена команды не должна отдавать чужой результат.
async function briefCached(input, { forDispatch = false } = {}) {
  const params = briefParams(input)
  const key = cacheKey("brief", params)
  const res = await withCache(key, TTL.brief, () => buildBrief(params), {
    force: forDispatch || Boolean(input?.force),
    allowStale: !forDispatch,
  })
  return markCache(res.value, res)
}

// ── текстовый рендер для tool ────────────────────────────────────────────────

function renderBrief(snap) {
  const f = snap.filtered ?? {}
  const fl = snap.filters ?? {}
  const rk = snap.runKind ?? {}
  const kindLabel = rk.value === "rerun" ? "РЕРАН (перезапуск только упавших)" : "основной прогон"
  const kindSource = rk.source === "user" ? "указано пользователем" : "определено автоматически"
  const kindConfidence = rk.source === "detected" && rk.confidence === "low" ? " — НЕ подтверждено, уточни" : ""

  const head = [
    `Запуск: ${snap.launch?.name ?? "—"}`,
    `  ${snap.launch?.url ?? ""}`,
    `  изменён ${snap.launch?.modified ?? "—"} · ${snap.launch?.closed ? "закрыт" : "идёт"} · проект ${snap.launch?.projectId ?? "—"}`,
    `Фильтры: команда ${fl.teamCanon || "не задана"} · тег ${fl.tag || "не задан"} · режим ${fl.mode}`
      + (fl.etalon?.exists
        ? ` · эталон ${fl.etalon.count} тестов, покрытие ${fl.etalon.coverage ?? "?"}%`
          + (fl.etalon.missing ? `, не запускалось ${fl.etalon.missing}` : "")
        : " · эталона нет"),
    `Тип: ${kindLabel} (${kindSource})${kindConfidence}`,
  ]

  const stat = snap.launchStatistic
  if (stat) {
    head.push(`Весь запуск: passed ${stat.passed} · failed ${stat.failed} · broken ${stat.broken} · skipped ${stat.skipped}`
      + (stat.total ? ` · всего ${stat.total}` : ""))
  }
  // Полнота чтения: без этой строки потеря строк при пагинации выглядела бы как норма.
  head.push(
    `Прочитано: страниц ${f.pages ?? "?"} · строк ${f.totalElements ?? f.results ?? 0}`
      + ` · complete ${f.complete ? "да" : "НЕТ"}`,
  )
  head.push(
    `По фильтру (по номерам тестов): ${f.total ?? 0} · passed ${f.passed ?? 0} · failed ${f.failed ?? 0}`
      + ` · broken ${f.broken ?? 0} · skipped ${f.skipped ?? 0}`
      + (f.passRate !== null && f.passRate !== undefined ? ` · проходимость ${f.passRate}%` : "")
      + ` · с ошибками ${f.nonPassedNumbers ?? 0}`,
  )
  head.push(
    `Строк результатов: ${f.results ?? 0}`
      + ` (passed ${f.passedRows ?? 0} · failed ${f.failedRows ?? 0}`
      + ` · broken ${f.brokenRows ?? 0} · skipped ${f.skippedRows ?? 0})`,
  )
  if (f.skippedExcluded) {
    head.push(`В @-список не входят: ${f.skippedExcluded} skipped (для раздачи skipped = PASS, как в dispatch скилла).`)
  }
  if (f.partial) head.push("ВНИМАНИЕ: сводка частичная (лимит определения команды) — полный проход делает скилл.")
  if (f.complete === false) head.push("ВНИМАНИЕ: чтение результатов неполное — цифры могут быть занижены.")

  // Для модели — компактный Jenkins @-список (с обрезкой), а не развёрнутый
  // «номер статус — название» из сотен строк: иначе вывод забивает контекст.
  const tags = truncateTags(snap.rerunTags ?? "", { maxChars: 3000 })
  const warnings = (snap.warnings ?? []).map((w) => `- ${w}`)

  const out = [...head]
  if (tags.text) {
    out.push(
      "",
      `@-список для Jenkins (${tags.total} тестов${tags.truncated ? ", показана часть" : ""}):`,
      tags.text,
    )
    if (tags.truncated) out.push("(полный список — попроси отдельно или открой панель)")
  } else {
    out.push("", "Не прошедших тестов нет.")
  }

  // Первые несколько с именами — чтобы было видно, о чём речь, без раздувания.
  const preview = (snap.nonPassed ?? []).slice(0, 8).map((r) => {
    const mark = r.origin === "tag" ? " [по тегу]" : r.origin === "both" ? " [команда+тег]" : ""
    return `@${r.number || "—"} ${r.status}${mark} ${r.name}`
  })
  if (preview.length) out.push("", "Первые упавшие:", ...preview)

  return [
    ...out,
    ...(warnings.length ? ["", "Предупреждения:", ...warnings] : []),
  ].join("\n")
}

// ── плагин ───────────────────────────────────────────────────────────────────

export default Plugin.define({
  id: "testops-analyzer",

  async setup(ctx) {
    await ctx.rpc.register(TestopsAnalyzerRpc, {
      options: async () => safe(Promise.resolve(JSON.stringify({
        teams: teamOptions(),
        modes: MODES,
        runKinds: ["auto", "main", "rerun"],
        state: stateSnapshot(),
      }))),

      brief: async (input) => safe(briefCached(input).then((snap) => JSON.stringify(snap))),

      // analyze = снимок + готовый промпт + запоминание настроек для следующего открытия панели.
      analyze: async (input) => safe((async () => {
        const snap = await briefCached(input)
        const prompt = buildPrompt(snap)
        const state = remember(briefParams(input))
        return JSON.stringify({ prompt, state, launch: snap.launch, filtered: snap.filtered, runKind: snap.runKind })
      })()),

      // План рерана без запуска. Если команда не выбрана — план по ВСЕМ командам.
      rerunPreview: async (input) => safe((async () => {
        const snap = await briefCached(input)

        // Гейт эталона: план не строится, если эталон не подошёл к запуску —
        // иначе пользователь увидит «N тестов» и запустит неверный набор.
        if (snap.filters?.etalon?.mismatch) {
          return JSON.stringify({
            error: (snap.warnings ?? []).find((w) => w.startsWith("ЭТАЛОН НЕ ПЕРЕСЕКАЕТСЯ"))
              ?? "эталон не пересекается с запуском — проверь режим QG/RM/HF",
            blocked: true,
            filtered: snap.filtered,
          })
        }

        if (!snap.filters?.teamCanon) {
          const all = await collectTeamReruns({ link: input.link, mode: briefParams(input).mode, tag: briefParams(input).tag })
          return JSON.stringify({ all: true, ...all })
        }

        const plan = rerunPreview({
          teamCanon: snap.filters.teamCanon,
          nonPassedNumbers: (snap.nonPassed ?? []).filter((r) => r.rerun !== false).map((r) => r.number),
          newFailedNumbers: (snap.newFailed ?? []).map((r) => r.number),
        })
        return JSON.stringify({ all: false, plan, filtered: snap.filtered, newFailedCount: snap.filtered?.newFailed ?? 0 })
      })()),

      // Фактический запуск Jenkins-джобы. Команда не выбрана → fan-out по всем командам.
      rerun: async (input) => safe((async () => {
        const snap = await briefCached(input, { forDispatch: true })
        if (snap.filtered?.complete !== true || snap.filtered?.partial) {
          return JSON.stringify({ error: "снимок TestOps неполный — отправка в Jenkins заблокирована", blocked: true })
        }

        if (!snap.filters?.teamCanon) {
          const all = await collectTeamReruns({ link: input.link, mode: briefParams(input).mode, tag: briefParams(input).tag })
          if (all.partial) {
            return JSON.stringify({ error: "состав по командам неполный — отправка в Jenkins заблокирована", blocked: true })
          }
          const results = await rerunAllTeams(all.teams)
          const byStatus = (s) => results.filter((r) => r.status === s).length
          return JSON.stringify({
            all: true,
            launch: all.launch,
            totalTests: all.totalTests,
            withFailures: all.withFailures,
            triggered: byStatus("triggered"),
            alreadyDispatched: byStatus("already_dispatched"),
            noFailures: byStatus("no_failures"),
            errors: byStatus("error") + byStatus("no_job") + byStatus("disabled"),
            warnings: all.warnings,
            results,
          })
        }

        const result = await rerunTeam({
          teamCanon: snap.filters.teamCanon,
          nonPassedNumbers: (snap.nonPassed ?? []).filter((r) => r.rerun !== false).map((r) => r.number),
          newFailedNumbers: (snap.newFailed ?? []).map((r) => r.number),
          etalonMismatch: Boolean(snap.filters?.etalon?.mismatch),
          mismatchReason: (snap.warnings ?? []).find((w) => w.startsWith("ЭТАЛОН НЕ ПЕРЕСЕКАЕТСЯ")) ?? null,
        })
        return JSON.stringify({ all: false, ok: true, ...result })
      })()),
    })

    await ctx.tool.transform((editor) => {
      editor.add({
        name: "testops_analyzer_brief",
        description:
          "Снимок запуска TestOps по ссылке: статистика, фильтр по команде (team_jobs) и тегу тестировщика, "
          + "режим QG/RM/HF, основной прогон или реран, список не прошедших тестов с номерами @NN.NN. "
          + "Правила фильтрации совпадают со скиллом testops-analysis. "
          + "Для РЕРАНА разбор делает скилл testops-rerun-triage (кодовое слово TRIAGE::RERUN, режим classify); "
          + "для основного прогона — скилл testops-analysis.",
        input: {
          type: "object",
          properties: {
            link: { type: "string", description: "Ссылка на запуск TestOps (/launch/<id>) или его id" },
            team: { type: "string", description: "Команда: BIT, ECOM, TACO, TAMUL, TCAT, TE, TFU, TMMB, TPR, TREX, TSPB, TT, TX-T9" },
            tag: { type: "string", description: "Тег тестировщика в TestOps (AQL tag = \"...\")" },
            mode: { type: "string", enum: ["QG", "RM", "HF"], description: "Тип прогона: QG, RM или HF" },
            runKind: { type: "string", enum: ["auto", "main", "rerun"], description: "Основной прогон или реран (auto — эвристика)" },
          },
          required: ["link"],
          additionalProperties: false,
        },
        execute: async (args) => {
          try {
            const snap = await briefCached(args ?? {})
            return { content: renderBrief(snap) }
          } catch (e) {
            return { content: `Ошибка разбора: ${friendly(e)}` }
          }
        },
      })
    })
    try {
      return await registerCodeMode(ctx)
    } catch {
      console.warn("[testops-code-mode] Registration unavailable; existing TestOps tools remain active.")
    }
  },
})

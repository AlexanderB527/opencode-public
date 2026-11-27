// GUI-плагин: панель «Разбор автотестов» — ссылка, команда, тег, QG/RM/HF, основной/реран.
//
// Данные считает server-часть (src/brief.mjs) и отдаёт по RPC: сюда нельзя
// ходить в TestOps напрямую (CORS + токен). Ссылки открываем через
// ctx.system.openExternal — обычный <a href> увёл бы окно Electron.
import { Command, Panel, Plugin } from "@opencode/plugin/gui"
import { createMemo, createSignal, For, Show } from "solid-js"
import { TestopsAnalyzerRpc } from "./rpc.ts"
import { buildRerunReport, buildRerunPlanReport, testsTextListByTeam, RERUN_STATUS_LABELS } from "./report.mjs"

const PANEL_ID = "testops-analyzer"
const TAB_ID = "main"

const MODE_LABELS = { QG: "QG", RM: "RM", HF: "HF" }
const RUNKIND_LABELS = { auto: "авто", main: "основной", rerun: "реран" }

function ageText(ms) {
  const s = Math.max(0, Math.round((ms ?? 0) / 1000))
  if (s < 60) return `${s} с назад`
  const m = Math.round(s / 60)
  if (m < 60) return `${m} мин назад`
  return `${Math.round(m / 60)} ч назад`
}

// Короткое представление имени теста без номера — чтобы не растягивать список.
function clipText(s, n) {
  const t = String(s ?? "").trim()
  return t.length > n ? `${t.slice(0, n)}…` : t
}

// Открытие ссылки: штатный openExternal, затем запасной window.open.
function openUrl(ctx, url) {
  if (!url) return false
  try { ctx?.system?.openExternal?.(url); return true } catch { /* ниже */ }
  try { window.open(url, "_blank", "noopener"); return true } catch { /* ниже */ }
  return false
}

function Link(props) {
  const [hover, setHover] = createSignal(false)
  return (
    <span
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      onClick={() => openUrl(props.ctx, props.url)}
      style={{
        cursor: "pointer",
        color: hover() ? "#4a9eff" : "inherit",
        "text-decoration": hover() ? "underline" : "none",
      }}
    >
      {props.label ?? props.url}
    </span>
  )
}

function Chip(props) {
  return (
    <button
      type="button"
      onClick={() => props.onPick(props.id)}
      disabled={props.disabled}
      title={props.title ?? ""}
      style={{
        padding: "4px 9px",
        margin: "0 5px 5px 0",
        border: props.active ? "1px solid #4a9eff" : "1px solid rgba(128,128,128,.35)",
        "border-radius": "6px",
        background: props.active ? "rgba(74,158,255,.14)" : "transparent",
        color: "inherit",
        cursor: props.disabled ? "default" : "pointer",
        "font-size": "12px",
      }}
    >
      {props.label}
    </button>
  )
}

const inputStyle = {
  width: "100%",
  padding: "5px 7px",
  border: "1px solid rgba(128,128,128,.35)",
  "border-radius": "6px",
  background: "transparent",
  color: "inherit",
  "font-size": "12px",
  "box-sizing": "border-box",
}

const labelStyle = { "font-size": "11px", opacity: 0.6, margin: "0 0 3px" }

// ── полоса прогресса: passed / ошибки / skipped ──────────────────────────────

function Bar(props) {
  const total = Math.max(1, props.total)
  const seg = (n) => `${(n / total) * 100}%`
  return (
    <div style={{ display: "flex", height: "6px", "border-radius": "3px", overflow: "hidden", margin: "4px 0" }}>
      <div style={{ width: seg(props.passed ?? 0), background: "#3fb950" }} />
      <div style={{ width: seg(props.failed ?? 0), background: "#e5534b" }} />
      <div style={{ width: seg(props.broken ?? 0), background: "#d29922" }} />
      <div style={{ width: seg(props.skipped ?? 0), background: "#8b949e" }} />
    </div>
  )
}

// ── панель ───────────────────────────────────────────────────────────────────

function AnalyzerPanel(props) {
  const ctx = props.ctx
  const rpc = () => ctx.client.rpc(TestopsAnalyzerRpc)

  const [link, setLink] = createSignal("")
  const [team, setTeam] = createSignal("")
  const [tag, setTag] = createSignal("")
  const [mode, setMode] = createSignal("QG")
  const [runKind, setRunKind] = createSignal("main")

  const [teams, setTeams] = createSignal([])
  const [recentTags, setRecentTags] = createSignal([])
  const [snap, setSnap] = createSignal(null)
  const [error, setError] = createSignal(null)
  const [busy, setBusy] = createSignal(false)
  const [starting, setStarting] = createSignal(false)
  const [rerunPlan, setRerunPlan] = createSignal(null)
  const [rerunBusy, setRerunBusy] = createSignal(false)
  const [rerunResult, setRerunResult] = createSignal(null)
  const [rerunErr, setRerunErr] = createSignal(null)
  // Контекст последнего рерана: отчёт строится по тем параметрам, с которыми
  // его запускали, а не по текущему состоянию формы.
  const [rerunCtx, setRerunCtx] = createSignal(null)
  // Готовый отчёт «что улетело в Jenkins» (текст, показывается в панели).
  const [report, setReport] = createSignal(null)
  const [reportCopied, setReportCopied] = createSignal(false)
  // Вид отчёта: "plan" — сразу после «проверить» (реран ещё не запущен),
  // "actual" — после рерана (есть билды).
  const [reportKind, setReportKind] = createSignal(null)
  const [reportBusy, setReportBusy] = createSignal(false)
  const [reportErr, setReportErr] = createSignal(null)
  // Состав тестов в отчёте: "tags" — @-список (по умолчанию, как параметр tags
  // в Jenkins), "names" — с названиями, "none" — только итог и строки команд.
  const [reportTests, setReportTests] = createSignal("tags")
  // Кеш плана рерана: кнопка отчёта не должна каждый раз перечитывать весь запуск.
  const [previewPlan, setPreviewPlan] = createSignal(null)
  // Вид списка не прошедших: читаемый «список автотестов» или компактный @-список.
  // Вид списка «не прошли» по умолчанию — @-список (номера для Jenkins).
  const [listView, setListView] = createSignal("tags")
  // Автозапуск Jenkins: при «разобрать» сразу запускает реран упавших без подтверждения.
  const [autoRerun, setAutoRerun] = createSignal(false)

  // Список команд. Форму НЕ автозаполняем из прошлого раза: всегда чистые
  // дефолты (ссылка пустая, все команды, QG, основной прогон, автозапуск нет).
  // Оставляем только «частые теги» — как быстрые чипы для ручного выбора.
  const loadOptions = async () => {
    try {
      const opts = JSON.parse(await rpc().options({}))
      if (opts?.error) return
      setTeams(opts.teams ?? [])
      const tags = [...new Set((opts.state?.recent ?? []).map((x) => x.tag).filter(Boolean))].slice(0, 3)
      setRecentTags(tags)
    } catch { /* форма остаётся пустой, но рабочей */ }
  }
  void loadOptions()

  const params = () => ({
    link: link().trim(),
    team: team(),
    tag: tag().trim(),
    mode: mode(),
    runKind: runKind(),
  })

  const check = async (force = false, allowAuto = true) => {
    if (!link().trim()) { setError("вставь ссылку на запуск TestOps"); return }
    setBusy(true); setError(null)
    // Смена входных данных делает прежний план и отчёт недействительными.
    setPreviewPlan(null); setReport(null); setReportKind(null); setReportErr(null)
    // Результат рерана держим, только пока смотрим тот же запуск: иначе отчёт
    // показал бы билды от другого launch.
    const sameRun = Boolean(rerunCtx()?.link) && rerunCtx().link === link().trim()
    if (!sameRun) { setRerunResult(null); setRerunCtx(null); setRerunErr(null); setRerunPlan(null) }
    try {
      const res = JSON.parse(await rpc().brief({ ...params(), force }))
      if (res?.error) { setError(res.error); return }
      setSnap(res)
      // Автозапуск Jenkins и по кнопке «проверить» (если переключатель включён).
      // allowAuto=false — когда check вызывается внутри analyze (там реран стартует сам).
      if (allowAuto && autoRerun()) await confirmRerun()
    } catch (e) {
      setError(String(e?.message ?? e))
    } finally {
      setBusy(false)
    }
  }

  // Разбор: новая сессия + промпт, активирующий скилл. Если сессию создать
  // нельзя — работаем в текущей, чтобы кнопка не была мёртвой.
  const analyze = async () => {
    if (!link().trim()) { setError("вставь ссылку на запуск TestOps"); return }
    setStarting(true); setError(null)
    // Автозапуск фиксируем ДО любых шагов: сбой промпта в чат не должен блокировать реран.
    const shouldAutoRerun = autoRerun()
    try {
      const res = JSON.parse(await rpc().analyze(params()))
      if (res?.error) { setError(res.error); return }
      if (res?.state) {
        setRecentTags([...new Set([res.state.last?.tag, ...recentTags()])].filter(Boolean).slice(0, 3))
      }

      let sessionID = null
      try {
        const created = await ctx.client.session.create({})
        sessionID = created?.id ?? created?.sessionID ?? created?.session?.id ?? null
      } catch { /* ниже — текущая сессия */ }
      if (!sessionID) sessionID = ctx.sessions.current()?.id ?? null
      // Сессии нет — разбор в чате пропускаем, но автозапуск рерана остаётся рабочим.
      if (sessionID) await ctx.client.session.prompt({ sessionID, text: res.prompt })

      try {
        ctx.ui?.toast?.show?.({ title: "Разбор запущен", description: res.launch?.name, variant: "success" })
      } catch { /* тост не критичен */ }
    } catch (e) {
      setError(String(e?.message ?? e))
    } finally {
      setStarting(false)
    }

    // Автозапуск: сразу отправить упавших в Jenkins, без отдельного клика по рерану.
    // Сначала подгружаем снапшот, чтобы результат и ссылка на билд были видны.
    if (shouldAutoRerun) {
      if (!snap()) await check(true, false)
      await confirmRerun()
    }
  }

  // Реран: сначала показать план (джоба + число тестов), запуск — только после
  // явного подтверждения. Автозапуск Jenkins (сигнал выше) пропускает подтверждение.
  const previewRerun = async () => {
    setRerunErr(null); setRerunResult(null)
    try {
      const res = JSON.parse(await rpc().rerunPreview(params()))
      if (res?.error) { setRerunErr(res.error); return }
      setRerunPlan(res.all ? { all: true, ...res } : { all: false, plan: res.plan })
    } catch (e) {
      setRerunErr(String(e?.message ?? e))
    }
  }

  const confirmRerun = async () => {
    setRerunBusy(true); setRerunErr(null)
    // Контекст отчёта фиксируем ДО запроса: если пользователь потом поменяет
    // форму, отчёт всё равно останется про тот запуск, который реально был.
    setRerunCtx({
      link: link().trim(),
      mode: filters()?.mode ?? mode(),
      team: filters()?.teamCanon || team(),
      tag: filters()?.tag ?? tag().trim(),
      launch: snap()?.launch ?? null,
    })
    setReport(null)
    try {
      const res = JSON.parse(await rpc().rerun(params()))
      if (res?.error) {
        setRerunErr(res.error)
        try { ctx.ui?.toast?.show?.({ title: "Реран НЕ запущен", description: res.error, variant: "error" }) } catch { /* тост не критичен */ }
        return
      }
      setRerunResult(res)
      setRerunPlan(null)
      try {
        const desc = res.all ? `${res.triggered} команд` : res.job
        ctx.ui?.toast?.show?.({ title: "Реран запущен", description: desc, variant: "success" })
      } catch { /* тост не критичен */ }
    } catch (e) {
      const msg = String(e?.message ?? e)
      setRerunErr(msg)
      try { ctx.ui?.toast?.show?.({ title: "Реран НЕ запущен", description: msg, variant: "error" }) } catch { /* тост не критичен */ }
    } finally {
      setRerunBusy(false)
    }
  }

  // Кнопка «отправить на реран»: если автозапуск включён — сразу запускаем,
  // иначе показываем план и ждём подтверждения.
  const onRerunClick = async () => {
    if (autoRerun()) await confirmRerun()
    else await previewRerun()
  }

  const filtered = () => snap()?.filtered ?? {}
  const filters = () => snap()?.filters ?? {}
  const runKindInfo = () => snap()?.runKind ?? {}

  // Упавшие, сгруппированные по командам (для смешанных запусков без фильтра команды).
  const groupedNonPassed = createMemo(() => {
    const list = snap()?.nonPassed ?? []
    const groups = new Map()
    for (const r of list) {
      const t = r.team || "без команды"
      if (!groups.has(t)) groups.set(t, [])
      groups.get(t).push(r)
    }
    return [...groups.entries()]
      .map(([team_, rows]) => ({ team: team_, rows }))
      .sort((a, b) => b.rows.length - a.rows.length)
  })

  // Копировать @-список в формате Jenkins (параметр tags) в буфер обмена.
  // navigator.clipboard в Electron не срабатывает — используем штатный ctx.system.copy.
  const [copied, setCopied] = createSignal(false)
  const copyText = async (text) => {
    if (!text) return false
    try {
      if (ctx?.system?.copy) await ctx.system.copy(text)
      else if (typeof navigator !== "undefined" && navigator?.clipboard?.writeText) await navigator.clipboard.writeText(text)
      else return false
      return true
    } catch {
      return false
    }
  }
  const flashCopied = () => {
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }

  // Копировать только номера одной команды (по клику на группу).
  // skipped не копируем: в раздаче скилла он считается PASS и в @-список не входит.
  const copyGroup = async (rows) => {
    const text = rows
      .filter((r) => r.rerun !== false)
      .map((r) => (r.number ? `@${r.number}` : ""))
      .filter(Boolean)
      .join(" ")
    if (await copyText(text)) flashCopied()
  }

  // Текст текущего вида списка: читаемый «список автотестов» или @-список.
  const listText = () => {
    const rows = snap()?.nonPassed ?? []
    if (listView() === "list") return testsTextListByTeam(rows)
    return rows
      .filter((r) => r.rerun !== false)
      .map((r) => (r.number ? `@${r.number}` : ""))
      .filter(Boolean)
      .join(" ")
  }
  const copyList = async () => {
    if (await copyText(listText())) flashCopied()
  }

  // ── отчёт «что улетело в Jenkins» ─────────────────────────────────────────
  // Два режима:
  //   • после рерана (в т.ч. автозапуска) — фактический отчёт с билдами;
  //   • сразу после «проверить» — план: те же команды и ссылки на их джобы в
  //     Jenkins, состав тестов, но билда ещё нет.
  const nameByNumber = () => {
    const names = {}
    for (const r of snap()?.nonPassed ?? []) if (r?.number) names[r.number] = r.name
    return names
  }
  const reportContext = () => rerunCtx() ?? {
    link: link().trim(),
    mode: filters()?.mode ?? mode(),
    team: filters()?.teamCanon || team(),
    tag: filters()?.tag ?? tag().trim(),
    launch: snap()?.launch ?? null,
  }
  const buildReport = async () => {
    setReportErr(null); setReportCopied(false)
    const tests = reportTests()
    // Реран уже был — отчёт фактический, сеть не нужна.
    if (rerunResult()) {
      setReportKind("actual")
      setReport(buildRerunReport({ result: rerunResult(), context: reportContext(), names: nameByNumber(), tests }))
      return
    }
    // Рерана не было — собираем план (один раз, дальше из кеша).
    setReportBusy(true)
    try {
      let preview = previewPlan()
      if (!preview) {
        preview = JSON.parse(await rpc().rerunPreview(params()))
        if (preview?.error) { setReportErr(preview.error); setReport(null); return }
        setPreviewPlan(preview)
      }
      setReportKind("plan")
      setReport(buildRerunPlanReport({ preview, context: reportContext(), names: nameByNumber(), tests }))
    } catch (e) {
      setReportErr(String(e?.message ?? e))
    } finally {
      setReportBusy(false)
    }
  }
  const copyReport = async () => {
    if (!report()) return
    if (await copyText(report())) {
      setReportCopied(true)
      setTimeout(() => setReportCopied(false), 1500)
    }
  }
  // Отчёт в текущую сессию чата — чтобы дальше работать с ним как с сообщением.
  const reportToChat = async () => {
    const text = report()
    if (!text) return
    const sessionID = ctx.sessions.current()?.id ?? null
    if (!sessionID) {
      try { ctx.ui?.toast?.show?.({ title: "Нет активной сессии", description: "скопируй отчёт кнопкой выше", variant: "error" }) } catch { /* тост не критичен */ }
      return
    }
    try {
      await ctx.client.session.prompt({ sessionID, text })
      try { ctx.ui?.toast?.show?.({ title: "Отчёт отправлен в чат", variant: "success" }) } catch { /* тост не критичен */ }
    } catch (e) {
      try { ctx.ui?.toast?.show?.({ title: "Не удалось отправить отчёт", description: String(e?.message ?? e), variant: "error" }) } catch { /* тост не критичен */ }
    }
  }

  return (
    <div style={{ "font-size": "13px", "overflow-y": "auto", "overflow-x": "hidden", padding: "4px 12px 12px" }}>
      {/* ── ссылка ── */}
      <div style={{ "margin-bottom": "8px" }}>
        <div style={labelStyle}>ссылка на запуск</div>
        <input
          style={inputStyle}
          placeholder="https://…/launch/1356126 или 1356126"
          value={link()}
          onInput={(e) => setLink(e.currentTarget.value)}
          onKeyDown={(e) => { if (e.key === "Enter") void check() }}
        />
      </div>

      {/* ── команда ── */}
      <div style={{ "margin-bottom": "8px" }}>
        <div style={labelStyle}>команда</div>
        <select
          style={inputStyle}
          value={team()}
          onChange={(e) => setTeam(e.currentTarget.value)}
        >
          <option value="">все команды (без фильтра)</option>
          <For each={teams()}>
            {(t) => <option value={t.id}>{t.id}{t.job ? ` · ${t.job}` : ""}</option>}
          </For>
        </select>
      </div>

      {/* ── тег тестировщика ── */}
      <div style={{ "margin-bottom": "8px" }}>
        <div style={labelStyle}>тег тестировщика</div>
        <input
          style={inputStyle}
          placeholder="тег тестировщика в TestOps"
          value={tag()}
          onInput={(e) => setTag(e.currentTarget.value)}
          onKeyDown={(e) => { if (e.key === "Enter") void check() }}
        />
        <Show when={recentTags().length > 0}>
          <div style={{ "margin-top": "4px" }}>
            <For each={recentTags()}>
              {(t) => <Chip id={t} label={t} active={tag() === t} onPick={setTag} />}
            </For>
          </div>
        </Show>
      </div>

      {/* ── режим QG / RM / HF ── */}
      <div style={{ "margin-bottom": "8px" }}>
        <div style={labelStyle}>режим</div>
        <For each={["QG", "RM", "HF"]}>
          {(m) => <Chip id={m} label={MODE_LABELS[m]} active={mode() === m} onPick={setMode} />}
        </For>
      </div>

      {/* ── основной / реран ── */}
      <div style={{ "margin-bottom": "10px" }}>
        <div style={labelStyle}>
          прогон
          <Show when={runKindInfo().source === "detected"}>
            <span style={{ opacity: 0.75 }}>{` · авто определило: ${RUNKIND_LABELS[runKindInfo().value] ?? "—"}`}</span>
          </Show>
        </div>
        <For each={["auto", "main", "rerun"]}>
          {(k) => <Chip id={k} label={RUNKIND_LABELS[k]} active={runKind() === k} onPick={setRunKind} />}
        </For>
      </div>

      {/* ── автозапуск Jenkins ── */}
      <div style={{ "margin-bottom": "8px" }}>
        <div style={labelStyle}>автозапуск Jenkins</div>
        <For each={[{ id: "yes", label: "да" }, { id: "no", label: "нет" }]}>
          {(o) => (
            <Chip
              id={o.id}
              label={o.label}
              active={o.id === (autoRerun() ? "yes" : "no")}
              onPick={(v) => setAutoRerun(v === "yes")}
            />
          )}
        </For>
      </div>

      {/* ── кнопки ── */}
      <div style={{ display: "flex", gap: "6px", "margin-bottom": "10px" }}>
        <button
          type="button"
          onClick={() => void check(true)}
          disabled={busy()}
          style={{ flex: "1", padding: "5px 8px", border: "1px solid rgba(128,128,128,.35)", "border-radius": "6px", background: "transparent", color: "inherit", cursor: busy() ? "default" : "pointer", "font-size": "12px" }}
        >
          {busy() ? "считаю…" : "проверить"}
        </button>
        <button
          type="button"
          onClick={() => void analyze()}
          disabled={starting()}
          title="Открыть новую сессию с готовым промптом: для рерана — скилл testops-rerun-triage (код TRIAGE::RERUN), для основного прогона — testops-analysis"
          style={{ flex: "1", padding: "5px 8px", border: "1px solid #4a9eff", "border-radius": "6px", background: "rgba(74,158,255,.14)", color: "inherit", cursor: starting() ? "default" : "pointer", "font-size": "12px" }}
        >
          {starting() ? "запускаю…" : "разобрать"}
        </button>
      </div>

      <Show when={error()}>
        <div style={{ color: "#e5534b", "margin-bottom": "8px" }}>Ошибка: {error()}</div>
      </Show>

      <Show when={snap()}>
        {/* ── шапка запуска ── */}
        <div style={{ "border-top": "1px solid rgba(128,128,128,.2)", "padding-top": "8px" }}>
          <div style={{ "font-size": "12px" }}>
            <Link ctx={ctx} url={snap().launch?.url} label={snap().launch?.name || "открыть запуск"} />
          </div>
          <div style={{ opacity: 0.6, "font-size": "11px" }}>
            {`${snap().launch?.modified ?? "—"} · ${snap().launch?.closed ? "закрыт" : "идёт"} · проект ${snap().launch?.projectId ?? "—"}`}
          </div>
          <div style={{ "font-size": "11px", opacity: 0.75, "margin-top": "3px" }}>
            {`${filters().mode} · ${filters().teamCanon || "все команды"}${filters().tag ? ` · тег ${filters().tag}` : ""}`}
          </div>
          <div style={{ "font-size": "11px", "margin-top": "3px" }}>
            <span style={{ opacity: 0.6 }}>тип: </span>
            <span style={{ color: runKindInfo().value === "rerun" ? "#d29922" : "#3fb950" }}>
              {runKindInfo().value === "rerun" ? "реран" : "основной"}
            </span>
            <span style={{ opacity: 0.5 }}>{runKindInfo().source === "user" ? " (выбрано)" : " (определено)"}</span>
          </div>

          <Show when={snap().cache?.cached}>
            <div style={{ "font-size": "11px", opacity: 0.55, "margin-top": "3px" }}>
              {snap().cache.stale ? "источник недоступен · " : ""}
              данные из кеша, {ageText(snap().cache.ageMs)}
            </div>
          </Show>
        </div>

        {/* ── цифры ── */}
        <div style={{ "margin-top": "8px" }}>
          <div style={{ "font-size": "12px" }}>
            {`${filtered().total ?? 0} номеров · passed ${filtered().passed ?? 0} · ошибок ${filtered().failedTotal ?? 0}`}
            {filtered().passRate !== null && filtered().passRate !== undefined ? ` · ${filtered().passRate}%` : ""}
          </div>
          <Show when={(filtered().passed ?? 0) + (filtered().failed ?? 0) + (filtered().broken ?? 0) + (filtered().skipped ?? 0) > 0}>
            <Bar
              total={filtered().total}
              passed={filtered().passed}
              failed={filtered().failed}
              broken={filtered().broken}
              skipped={filtered().skipped}
            />
          </Show>
          <div style={{ "font-size": "11px", opacity: 0.6 }}>
            {`failed ${filtered().failed ?? 0} · broken ${filtered().broken ?? 0} · skipped ${filtered().skipped ?? 0}`}
            {` · строк ${filtered().results ?? 0}`}
            {filters().etalon?.exists ? ` · эталон ${filters().etalon.count}, покрытие ${filters().etalon.coverage ?? "?"}%` : ""}
            {filters().taggedTestcases ? ` · по тегу ${filters().taggedTestcases}` : ""}
          </div>
          <Show when={snap().launchStatistic}>
            <div style={{ "font-size": "11px", opacity: 0.45, "margin-top": "2px" }}>
              {`весь запуск: passed ${snap().launchStatistic.passed} · failed ${snap().launchStatistic.failed}`
                + ` · broken ${snap().launchStatistic.broken} · skipped ${snap().launchStatistic.skipped}`}
            </div>
          </Show>
        </div>

        {/* ── предупреждения ── */}
        <Show when={(snap().warnings ?? []).length > 0}>
          <div style={{ "margin-top": "6px", "font-size": "11px", color: "#d29922" }}>
            <For each={snap().warnings}>{(w) => <div>{`• ${w}`}</div>}</For>
          </div>
        </Show>

        {/* ── разбивка по командам (когда запуск смешанный) ── */}
        <Show when={(snap().perTeam ?? []).length > 1}>
          <div style={{ "margin-top": "8px", "font-size": "11px" }}>
            <div style={{ opacity: 0.6 }}>ошибки по командам:</div>
            <div>
              <For each={snap().perTeam}>
                {(t) => <span style={{ "margin-right": "8px" }}>{`${t.team}: ${t.nonPassed}`}</span>}
              </For>
            </div>
          </div>
        </Show>

        {/* ── не прошедшие: читаемый список автотестов или @-список ── */}
        <div style={{ "margin-top": "10px" }}>
          <div style={{ display: "flex", "justify-content": "space-between", "align-items": "center", "margin-bottom": "4px" }}>
            <span style={{ "font-size": "12px", opacity: 0.75 }}>
              {`не прошли (${(snap().nonPassed ?? []).length})`}
            </span>
            <div style={{ display: "flex", gap: "4px", "align-items": "center" }}>
              <Chip id="list" label="список" active={listView() === "list"} onPick={setListView} title="Текстом: номер · статус · название" />
              <Chip id="tags" label="@-список" active={listView() === "tags"} onPick={setListView} title="Компактно: только номера для Jenkins" />
              <button
                type="button"
                onClick={() => void copyList()}
                title={listView() === "list" ? "Скопировать список автотестов текстом" : "Скопировать @-список в формате Jenkins (параметр tags)"}
                style={{ border: "1px solid rgba(128,128,128,.35)", "border-radius": "6px", background: "transparent", color: "inherit", cursor: "pointer", "font-size": "11px", padding: "2px 8px" }}
              >
                {copied() ? "скопировано ✓" : "копировать"}
              </button>
            </div>
          </div>

          <Show when={groupedNonPassed().length > 0}>
            <For each={groupedNonPassed()}>
              {(g) => (
                <div style={{ "margin-bottom": "8px" }}>
                  <div
                    style={{ "font-size": "11px", opacity: 0.7, "margin-bottom": "2px" }}
                    title={listView() === "list" ? g.team : "Клик — скопировать @-список этой команды"}
                    onClick={listView() === "tags" ? () => void copyGroup(g.rows) : undefined}
                  >
                    {`${g.team} · ${g.rows.length}`}
                  </div>

                  {/* Читаемый список: номер · статус · название (название — ссылка на тест-кейс). */}
                  <Show when={listView() === "list"}>
                    <For each={g.rows}>
                      {(r) => (
                        <div style={{ "font-size": "12px", "line-height": "1.5", "word-break": "break-word" }}>
                          <span style={{ opacity: 0.7 }}>{r.number ? `@${r.number}` : "—"}</span>
                          <span style={{ opacity: 0.5 }}>{` · ${r.status} · `}</span>
                          <Show when={r.url} fallback={<span>{r.name || "(без названия)"}</span>}>
                            <Link ctx={ctx} url={r.url} label={r.name || "(без названия)"} />
                          </Show>
                        </div>
                      )}
                    </For>
                  </Show>

                  {/* Компактный @-список: номера через пробел, клик по группе — копировать.
                      Разрыв строки — только между номерами (пробел-разделитель в токене),
                      а не посередине номера: break-all резал «@02.07.18.0» и «9» на разные строки. */}
                  <Show when={listView() === "tags"}>
                    <div
                      style={{ "font-size": "12px", "line-height": "1.6", cursor: "pointer" }}
                      title="Клик — скопировать @-список этой команды"
                      onClick={() => void copyGroup(g.rows)}
                    >
                      <For each={g.rows}>
                        {(r) => (
                          <span style={{ "margin-right": "4px" }} title={r.number ? r.number : r.name}>
                            {r.number ? `@${r.number}` : clipText(r.name, 70)}
                            {" "}
                          </span>
                        )}
                      </For>
                    </div>
                  </Show>
                </div>
              )}
            </For>
          </Show>
          <Show when={groupedNonPassed().length === 0}>
            <div style={{ "font-size": "12px", opacity: 0.6 }}>не прошедших тестов нет</div>
          </Show>
        </div>

        {/* ── отчёт по Jenkins: доступен сразу после «проверить» ── */}
        <div style={{ "border-top": "1px solid rgba(128,128,128,.2)", "margin-top": "12px", "padding-top": "10px" }}>
          <div style={{ "font-size": "12px", opacity: 0.75, "margin-bottom": "4px" }}>отчёт по Jenkins</div>
          <div style={{ "font-size": "11px", opacity: 0.55, "margin-bottom": "4px" }}>
            {reportKind() === "actual" || rerunResult()
              ? "по факту рерана: билды и отправленные тесты"
              : "без запуска: джобы команд, ссылки в Jenkins и состав тестов"}
          </div>
          <div style={{ "margin-bottom": "4px" }}>
            <Chip id="none" label="без списка" active={reportTests() === "none"} onPick={() => { setReportTests("none"); setReport(null) }} title="Только итог и команды со ссылками" />
            <Chip id="tags" label="@-список" active={reportTests() === "tags"} onPick={() => { setReportTests("tags"); setReport(null) }} title="Номера тестов через пробел — как параметр tags в Jenkins (по умолчанию)" />
            <Chip id="names" label="с названиями" active={reportTests() === "names"} onPick={() => { setReportTests("names"); setReport(null) }} title="Добавить названия тестов к номерам" />
          </div>
          <button
            type="button"
            onClick={() => void buildReport()}
            disabled={reportBusy()}
            title="Собрать отчёт: по каждой команде джоба, ссылка в Jenkins и состав тестов"
            style={{
              width: "100%",
              padding: "5px 8px",
              border: "1px solid #4a9eff",
              "border-radius": "6px",
              background: "rgba(74,158,255,.14)",
              color: "inherit",
              cursor: reportBusy() ? "default" : "pointer",
              "font-size": "12px",
            }}
          >
            {reportBusy() ? "собираю…" : "сформировать отчёт"}
          </button>

          <Show when={reportErr()}>
            <div style={{ color: "#e5534b", "font-size": "11px", "margin-top": "4px" }}>{reportErr()}</div>
          </Show>

          <Show when={report()}>
            <div style={{ "margin-top": "6px" }}>
              <div style={{ display: "flex", gap: "6px", "margin-bottom": "4px" }}>
                <button
                  type="button"
                  onClick={() => void copyReport()}
                  title="Скопировать отчёт в буфер обмена"
                  style={{ flex: "1", padding: "3px 8px", border: "1px solid rgba(128,128,128,.35)", "border-radius": "6px", background: "transparent", color: "inherit", cursor: "pointer", "font-size": "11px" }}
                >
                  {reportCopied() ? "скопировано ✓" : "копировать"}
                </button>
                <button
                  type="button"
                  onClick={() => void reportToChat()}
                  title="Отправить отчёт сообщением в текущую сессию чата"
                  style={{ padding: "3px 8px", border: "1px solid rgba(128,128,128,.35)", "border-radius": "6px", background: "transparent", color: "inherit", cursor: "pointer", "font-size": "11px" }}
                >
                  в чат
                </button>
                <button
                  type="button"
                  onClick={() => setReport(null)}
                  style={{ padding: "3px 8px", border: "1px solid rgba(128,128,128,.35)", "border-radius": "6px", background: "transparent", color: "inherit", cursor: "pointer", "font-size": "11px" }}
                >
                  скрыть
                </button>
              </div>
              <pre
                style={{
                  margin: "0",
                  padding: "6px 8px",
                  "max-height": "280px",
                  "overflow": "auto",
                  "border": "1px solid rgba(128,128,128,.25)",
                  "border-radius": "6px",
                  "font-size": "11px",
                  "line-height": "1.45",
                  "white-space": "pre-wrap",
                  "word-break": "break-word",
                  "user-select": "text",
                }}
              >
                {report()}
              </pre>
            </div>
          </Show>
        </div>

        {/* ── реран упавших ── */}
        <div style={{ "border-top": "1px solid rgba(128,128,128,.2)", "margin-top": "12px", "padding-top": "10px" }}>
          <div style={{ "font-size": "12px", opacity: 0.75, "margin-bottom": "4px" }}>
            {`реран: ${filters().teamCanon || "все команды"}${filtered().newFailed ? ` · вне эталона упало ${filtered().newFailed}` : ""}`}
          </div>

          <Show when={!rerunPlan() && !rerunResult()}>
            <button
              type="button"
              onClick={() => void onRerunClick()}
              disabled={rerunBusy()}
              style={{
                width: "100%",
                padding: "5px 8px",
                border: "1px solid #d29922",
                "border-radius": "6px",
                background: "rgba(210,153,34,.12)",
                color: "inherit",
                cursor: rerunBusy() ? "default" : "pointer",
                "font-size": "12px",
              }}
            >
              {rerunBusy() ? "запускаю…" : "отправить на реран"}
            </button>
          </Show>

          {/* План: одна команда или все */}
          <Show when={rerunPlan()}>
            <Show when={!rerunPlan().all}>
              <div style={{ "font-size": "12px", "margin-bottom": "6px" }}>
                <div>{`джоба: ${rerunPlan().plan.job}`}</div>
                <div style={{ opacity: 0.7 }}>
                  {`тестов ${rerunPlan().plan.count} · env ${rerunPlan().plan.env} · parallel ${rerunPlan().plan.parallel} · rerun ${rerunPlan().plan.rerun}`}
                </div>
              </div>
            </Show>
            <Show when={rerunPlan().all}>
              <div style={{ "font-size": "12px", "margin-bottom": "6px" }}>
                <div>{`команд: ${rerunPlan().teams.length} · с упавшими ${rerunPlan().teams.filter((t) => t.count > 0).length} · тестов всего ${rerunPlan().totalTests}`}</div>
                <div style={{ opacity: 0.7, "margin-top": "3px" }}>
                  <For each={rerunPlan().teams}>
                    {(t) => <div>{`${t.team}: ${t.count ? `${t.count} тестов` : "полный набор"}`}</div>}
                  </For>
                </div>
              </div>
            </Show>
            <div style={{ display: "flex", gap: "6px" }}>
              <button
                type="button"
                onClick={() => void confirmRerun()}
                disabled={rerunBusy()}
                style={{ flex: "1", padding: "5px 8px", border: "1px solid #d29922", "border-radius": "6px", background: "rgba(210,153,34,.18)", color: "inherit", cursor: rerunBusy() ? "default" : "pointer", "font-size": "12px" }}
              >
                {rerunBusy() ? "запускаю…" : "подтвердить запуск"}
              </button>
              <button
                type="button"
                onClick={() => setRerunPlan(null)}
                disabled={rerunBusy()}
                style={{ padding: "5px 10px", border: "1px solid rgba(128,128,128,.35)", "border-radius": "6px", background: "transparent", color: "inherit", cursor: "pointer", "font-size": "12px" }}
              >
                отмена
              </button>
            </div>
          </Show>

          {/* Результат запуска: одна команда или список */}
          <Show when={rerunResult()}>
            <Show when={!rerunResult().all}>
              <div style={{ "font-size": "12px" }}>
                <div style={{ color: "#3fb950" }}>
                  {rerunResult().status === "already_dispatched"
                    ? `джоба ${rerunResult().job}: билд уже в очереди · тестов ${rerunResult().count}`
                    : `джоба ${rerunResult().job} запущена · тестов ${rerunResult().count}${rerunResult().note ? " · уже в очереди" : ""}`}
                </div>
                <div style={{ "margin-top": "4px" }}>
                  <Show when={rerunResult().buildUrl}>
                    <Link ctx={ctx} url={rerunResult().buildUrl} label="билд →" />
                  </Show>
                  <Show when={!rerunResult().buildUrl && rerunResult().queueUrl}>
                    <Link ctx={ctx} url={rerunResult().queueUrl} label="очередь →" />
                  </Show>
                </div>
              </div>
            </Show>
            <Show when={rerunResult().all}>
              <div style={{ "font-size": "12px" }}>
                <div style={{ color: "#3fb950" }}>{`запущено джоб: ${rerunResult().triggered} из ${rerunResult().results.length}`}</div>
                <div style={{ "margin-top": "4px" }}>
                  <For each={rerunResult().results}>
                    {(r) => (
                      <div style={{ "margin-bottom": "4px" }}>
                        <span>{`${r.team}: ${r.fullRun ? "полный набор" : `${r.count} тестов`} · `}</span>
                        <Show when={r.status === "triggered" || r.status === "already_dispatched"}>
                          <span style={{ color: r.status === "triggered" ? "#3fb950" : "#d29922" }}>
                            {RERUN_STATUS_LABELS[r.status]}
                          </span>
                          <Show when={r.note === "queue item exists"}>
                            <span style={{ opacity: 0.6, "margin-left": "4px" }}>· уже в очереди</span>
                          </Show>
                          <Show when={r.buildUrl}>
                            <span style={{ "margin-left": "6px" }}>
                              <Link ctx={ctx} url={r.buildUrl} label="билд →" />
                            </span>
                          </Show>
                          <Show when={!r.buildUrl && r.queueUrl}>
                            <span style={{ "margin-left": "6px" }}>
                              <Link ctx={ctx} url={r.queueUrl} label="очередь →" />
                            </span>
                          </Show>
                        </Show>
                        <Show when={r.status === "no_failures"}>
                          <span style={{ opacity: 0.5 }}>нет упавших</span>
                          <Show when={r.jobUrl}>
                            <span style={{ "margin-left": "6px" }}>
                              <Link ctx={ctx} url={r.jobUrl} label="джоба →" />
                            </span>
                          </Show>
                        </Show>
                        <Show when={r.status !== "triggered" && r.status !== "already_dispatched" && r.status !== "no_failures"}>
                          <span style={{ color: "#e5534b" }}>
                            {`${RERUN_STATUS_LABELS[r.status] ?? r.status}${r.error ? `: ${r.error}` : ""}`}
                          </span>
                        </Show>
                      </div>
                    )}
                  </For>
                </div>
              </div>
            </Show>
          </Show>

          <Show when={rerunErr()}>
            <div style={{ color: "#e5534b", "font-size": "12px", "margin-top": "6px" }}>{rerunErr()}</div>
          </Show>
        </div>
      </Show>
    </div>
  )
}

// ── регистрация ──────────────────────────────────────────────────────────────

export default Plugin.define({
  i18n: {
    en: { "panel.title": "TestOps Analysis", "command.open": "Open TestOps Analysis" },
    ru: { "panel.title": "Разбор автотестов", "command.open": "Открыть разбор автотестов" },
  },

  setup(ctx) {
    try { console.error("[testops-analyzer-gui] setup start, id=" + ctx.id) } catch { /* noop */ }

    ctx.add(Panel, {
      id: PANEL_ID,
      region: "side",
      width: "narrow",
      list: () => [{
        id: TAB_ID,
        get title() { return ctx.t("panel.title") },
        pinned: true,
        closable: false,
        fallback: true,
      }],
      render: () => <AnalyzerPanel ctx={ctx} />,
    })

    ctx.add(Command, () => ({
      id: "open",
      title: ctx.t("command.open"),
      group: ctx.t("panel.title"),
      bind: "mod+shift+t",
      slash: { name: "testops" },
      featured: true,
      run: () => {
        const session = ctx.sessions.current()
        if (session) ctx.layout.open(`${ctx.id}:${TAB_ID}`, session, { tab: "select" })
      },
    }))
  },
})

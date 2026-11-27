import { randomUUID } from "node:crypto"
import { truncateTags } from "../tags.mjs"
import { adapters, type AnalysisInput, type ToolRegistry } from "./adapters.ts"
import { createRunner, failure } from "./registry.ts"

const TTL = 120_000
const clip = (value: string, max = 220) => value.slice(0, max)
type Evidence = { number: string; status: string; id: number | null; message?: string; trace?: string; confidence?: string }
type Snapshot = { session: string; launchId: number; expires: number; rows: Map<string, Evidence> }
export type EvidenceInput = { snapshot: string; numbers: string[] }

export function createWorkflows(registry: ToolRegistry = adapters) {
  const snapshots = new Map<string, Snapshot>()

  function lookup(session: string, key: string) {
    const snapshot = snapshots.get(key)
    if (!snapshot || snapshot.session !== session || snapshot.expires <= Date.now()) throw failure("SNAPSHOT_UNAVAILABLE")
    return snapshot
  }

  async function analyze(input: AnalysisInput, session: string, signal: AbortSignal, run: ReturnType<typeof createRunner>) {
    let rows: Evidence[]
    let summary
    let pythonRequests = 0
    if (input.runKind === "rerun") {
      const { result, requests } = await registry.triage(input.launchId, signal)
      pythonRequests = requests
      rows = result.items.map(item => ({
        number: item.number, status: item.status, id: item.evidence_result_id,
        message: clip(item.message, 350), trace: clip(item.trace, 350), confidence: item.confidence,
      }))
      summary = {
        launchId: input.launchId, name: clip(result.rerun_name), runKind: "rerun",
        complete: result.scan.complete && result.detail_errors.length === 0,
        scan: result.scan, stats: result.stats, detailErrors: result.detail_errors.length,
        groups: result.groups.slice(0, 10).map(group => ({ label: clip(group.label), owner: group.owner, confidence: group.confidence, count: group.count })),
        groupCount: result.groups.length, rerunTags: truncateTags(result.rerun_tags, { maxChars: 1000 }),
      }
    } else {
      const brief = await registry.brief(input, run)
      const etalon = brief.filters.etalon
      rows = [...brief.newFailed, ...brief.nonPassed].filter(row => row.number).map(row => ({
        number: `@${row.number}`, status: "status" in row ? String(row.status) : "nonpass",
        id: "resultId" in row ? Number(row.resultId) || null : null,
      }))
      summary = {
        launchId: input.launchId, name: clip(brief.launch.name), runKind: brief.runKind.value,
        kindSource: brief.runKind.source, kindConfidence: brief.runKind.confidence,
        next: brief.runKind.value === "rerun" ? "request_rerun_without_filters" : undefined,
        complete: brief.filtered.complete && !brief.filtered.partial,
        filtered: brief.filtered, etalon: { exists: etalon.exists, count: etalon.count, coverage: etalon.coverage, mismatch: etalon.mismatch },
        warnings: brief.warnings.slice(0, 8).map(value => clip(value)), warningCount: brief.warnings.length,
        rerunTags: truncateTags(brief.rerunTags, { maxChars: 1000 }),
      }
    }
    signal.throwIfAborted()
    if (run.limited()) throw failure("LIMIT")
    const retained = new Map(rows.slice(0, 2500).map(row => [row.number, row]))
    const key = randomUUID()
    for (const [id, snapshot] of snapshots) {
      if (snapshot.expires <= Date.now()) snapshots.delete(id)
    }
    if (snapshots.size >= 32) snapshots.delete(snapshots.keys().next().value!)
    snapshots.set(key, { session, launchId: input.launchId, expires: Date.now() + TTL, rows: retained })
    return {
      data: { ...summary, evidence: { snapshot: key, available: retained.size, truncated: rows.length > 2500, sample: [...retained.keys()].slice(0, 10) } },
      pythonRequests,
    }
  }

  async function evidence(input: EvidenceInput, snapshot: Snapshot, run: ReturnType<typeof createRunner>) {
    return Promise.all(input.numbers.map(async number => {
      const row = snapshot.rows.get(number)
      if (!row) return { number, status: "unavailable" }
      const { id, ...data } = row
      if (data.message !== undefined) return { ...data, evidenceAvailable: Boolean(data.message || data.trace) }
      if (!id || !Number.isSafeInteger(id) || id < 1) return { ...data, evidenceAvailable: false }
      try {
        const message = clip(await registry.evidence(id, run), 350)
        snapshot.rows.set(number, { ...row, message })
        return { ...data, message, evidenceAvailable: Boolean(message) }
      } catch {
        return { ...data, evidenceAvailable: false, error: "UPSTREAM" }
      }
    }))
  }

  return { lookup, analyze, evidence }
}

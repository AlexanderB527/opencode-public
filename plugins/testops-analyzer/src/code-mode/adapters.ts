import { spawn } from "node:child_process"
import { fileURLToPath } from "node:url"
import { apiGet, testopsConfig } from "../api.mjs"
import { buildBrief } from "../brief.mjs"
import { createRunner, failure } from "./registry.ts"

type Runner = ReturnType<typeof createRunner>
export type Brief = Awaited<ReturnType<typeof buildBrief>>
export type AnalysisInput = {
  launchId: number
  runKind: "auto" | "main" | "rerun"
  mode: "QG" | "RM" | "HF"
  team: string
  tag: string
}
export type Triage = {
  rerun_name: string
  scan: { complete: boolean; total: number; pages: number; rows_scanned: number }
  stats: { gate_ok: boolean; failed_numbers: number; failed_rows: number; confirmed_pct: number; triage_pct: number; by_owner: Record<string, number> }
  groups: { label: string; owner: string; confidence: string; count: number }[]
  items: { number: string; status: string; confidence: string; evidence_result_id: number | null; message: string; trace: string }[]
  detail_errors: unknown[]
  rerun_tags: string
}
export type ToolRegistry = {
  brief: (input: AnalysisInput, run: Runner) => Promise<Brief>
  triage: (launchId: number, signal: AbortSignal) => Promise<{ result: Triage; requests: number }>
  evidence: (id: number, run: Runner) => Promise<string>
}

const bridge = fileURLToPath(new URL("./triage_bridge.py", import.meta.url))

function triage(launchId: number, signal: AbortSignal): Promise<{ result: Triage; requests: number }> {
  const python = process.env.TESTOPS_CODE_MODE_PYTHON || (process.platform === "win32" ? "python.exe" : "python3")
  const env = Object.fromEntries([
    "PATH", "SystemRoot", "WINDIR", "HOME", "USERPROFILE", "QA_ARTIFACTS_ROOT", "TESTOPS_TOKEN",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTPS_PROXY", "NO_PROXY", "TESTOPS_CODE_MODE_TRIAGE_HOME",
  ].map(key => [key, process.env[key]]).filter(([, value]) => value !== undefined))
  return new Promise((resolve, reject) => {
    const child = spawn(python, ["-I", "-B", bridge, String(launchId)], {
      shell: false, windowsHide: true, signal, env, stdio: ["ignore", "pipe", "ignore"],
    })
    const chunks: Buffer[] = []
    let bytes = 0
    const limit = 8 * 1024 * 1024
    child.stdout.on("data", (chunk: Buffer) => {
      bytes += chunk.length
      if (bytes > limit) child.kill()
      else chunks.push(chunk)
    })
    let childError: Error | undefined
    child.on("error", error => { childError = error })
    child.on("close", code => {
      if (signal.aborted) return reject(signal.reason)
      if (childError) return reject(childError)
      if (code === 3 || bytes > limit) return reject(failure("LIMIT"))
      if (code === 4) return reject(failure("UNAVAILABLE"))
      if (code !== 0) return reject(failure("UPSTREAM"))
      try { resolve(JSON.parse(Buffer.concat(chunks).toString("utf8"))) }
      catch { reject(failure("UPSTREAM")) }
    })
  })
}

export const adapters: ToolRegistry = {
  brief(input: AnalysisInput, run: Runner): Promise<Brief> {
    const cfg = { ...testopsConfig(), runRequest: run }
    const params = { ...input, link: String(input.launchId) }
    return buildBrief(params, cfg)
  },
  triage,
  async evidence(id: number, run: Runner): Promise<string> {
    const cfg = { ...testopsConfig(), runRequest: run }
    const detail = await apiGet(`/api/testresult/${id}`, { cfg })
    return typeof detail?.message === "string" ? detail.message : ""
  },
}

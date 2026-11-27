import assert from "node:assert/strict"
import { execFile } from "node:child_process"
import { EventEmitter } from "node:events"
import { mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises"
import { createServer } from "node:http"
import https from "node:https"
import { syncBuiltinESMExports } from "node:module"
import { tmpdir } from "node:os"
import { join } from "node:path"
import test from "node:test"
import { fileURLToPath } from "node:url"
import { promisify } from "node:util"
import { buildBrief } from "../src/brief.mjs"
import { adapters } from "../src/code-mode/adapters.ts"
import { createRunner } from "../src/code-mode/registry.ts"

const launch = { name: "Fixture", closed: true, projectId: 8 }
const rows = [
  { id: 1, name: "12.34 Param", status: "failed", testCaseId: 10, team: "BIT" },
  { id: 2, name: "12.34 Param", status: "passed", testCaseId: 10, team: "BIT" },
  { id: 3, name: "12.35 Example", status: "skipped", testCaseId: 11, team: "BIT" },
  { id: 4, name: "12.36 Example", status: "passed", testCaseId: 12, team: "BIT" },
]
function response(path: string) {
  if (path.startsWith("/api/uaa")) return { access_token: "fixture-jwt" }
  if (path === "/api/launch/42") return launch
  if (path.startsWith("/api/v2/launch/42/")) return { content: rows, totalElements: rows.length, last: true, totalPages: 1 }
  if (path.startsWith("/api/testcase/__search?")) return { content: [{ id: 10 }, { id: 11 }], last: true }
  if (path.includes("/cfv?")) return [{ customField: { name: "Team" }, name: "BIT" }]
  if (path === "/api/testresult/1") return { message: "fixture failure", trace: "fixture trace" }
  if (path === "/api/testresult/3") return {}
  return {}
}

test("bounded and old JS briefs agree on grouped rows, skipped and tag filter", async () => {
  const original = https.request
  https.request = ((url: string, _options: object, callback: (response: object) => void) => {
    const req = new EventEmitter()
    return Object.assign(req, {
      write() {},
      end() {
        queueMicrotask(() => {
          const res = Object.assign(new EventEmitter(), { setEncoding() {}, statusCode: 200 })
          callback(res)
          res.emit("data", JSON.stringify(response(url.slice("https://fixture.invalid".length))))
          res.emit("end")
        })
      },
    })
  }) as typeof https.request
  syncBuiltinESMExports()
  try {
    const input = { link: "42", team: "BIT", tag: "fixture", runKind: "main" }
    const cfg = { url: "https://fixture.invalid", token: "fixture" }
    const old = await buildBrief(input, cfg)
    const run = createRunner(new AbortController().signal, 2, 512)
    const bounded = await buildBrief(input, { ...cfg, runRequest: run } as typeof cfg)
    assert.deepEqual({ ...bounded, fetchedAt: null }, { ...old, fetchedAt: null })
    assert.equal(bounded.filtered.failed, 1)
    assert.equal(bounded.filtered.skipped, 1)
    assert.equal(bounded.filtered.complete, true)
    assert.ok(run.count() >= 3)
  } finally {
    https.request = original
    syncBuiltinESMExports()
  }
})

const exec = promisify(execFile)
const python = process.env.TESTOPS_CODE_MODE_PYTHON || (process.platform === "win32" ? "python.exe" : "python3")

test("Python bridge matches the existing CLI artifact and leaves the artifact root untouched", async t => {
  try { await exec(python, ["--version"]) }
  catch { return t.skip("Set TESTOPS_CODE_MODE_PYTHON to an installed Python 3") }
  const root = await mkdtemp(join(tmpdir(), "testops-code-mode-"))
  let cancel: (() => void) | undefined
  const server = createServer((req, res) => {
    if (req.url === "/api/launch/43") cancel?.()
    else res.end(JSON.stringify(response(req.url ?? "")))
  })
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject)
    server.listen(0, "127.0.0.1", resolve)
  })
  const previous = process.env.QA_ARTIFACTS_ROOT
  process.env.QA_ARTIFACTS_ROOT = root
  try {
    const address = server.address()
    assert.ok(address && typeof address === "object")
    await writeFile(join(root, "config.json"), JSON.stringify({
      testops_base_url: `http://127.0.0.1:${address.port}`, testops_token: "fixture",
      testops_token_env: "TESTOPS_CODE_MODE_FIXTURE_TOKEN_UNUSED",
    }))
    const bounded = await adapters.triage(42, new AbortController().signal)
    assert.deepEqual(await readdir(root), ["config.json"])
    const cli = fileURLToPath(new URL("../../../skills/testops-rerun-triage/scripts/classify_rerun.py", import.meta.url))
    await exec(python, ["-B", cli, "--launch-id", "42", "--json-only"])
    const expected = JSON.parse(await readFile(join(root, "triage/42/triage_classification.json"), "utf8"))
    assert.deepEqual({ ...bounded.result, generated_at: null }, { ...expected, generated_at: null })
    assert.equal(bounded.result.stats.gate_ok, true)
    assert.ok(bounded.requests >= 3)
    const controller = new AbortController()
    cancel = () => controller.abort()
    await assert.rejects(adapters.triage(43, controller.signal), { name: "AbortError" })
  } finally {
    if (previous === undefined) delete process.env.QA_ARTIFACTS_ROOT
    else process.env.QA_ARTIFACTS_ROOT = previous
    await new Promise<void>(resolve => server.close(() => resolve()))
    await rm(root, { recursive: true, force: true })
  }
})

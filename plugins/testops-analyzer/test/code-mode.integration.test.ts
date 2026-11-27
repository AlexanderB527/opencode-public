import assert from "node:assert/strict"
import test from "node:test"
import { registerCodeMode } from "../src/code-mode/tools.ts"
import { createWorkflows } from "../src/code-mode/workflows.ts"

const brief = {
  launch: { name: "QG" }, runKind: { source: "user", value: "main" },
  filters: { etalon: { exists: true, count: 2, coverage: 100, mismatch: false } },
  filtered: { complete: false, partial: false, skipped: 1, failed: 1 },
  nonPassed: [{ number: "11.22", status: "failed", resultId: 7 }], newFailed: [],
  warnings: ["incomplete page"], rerunTags: "@11.22",
}

async function harness({ enabled = true, deny = false, error, wait = false }: {
  enabled?: boolean; deny?: boolean; error?: Error; wait?: boolean
} = {}) {
  const tools = new Map<string, { execute: (input: unknown, context: object) => Promise<any> }>()
  const calls: (string | number)[] = []
  const workflows = createWorkflows({
    brief: async (_input, run) => {
      calls.push("brief")
      if (error) throw error
      if (wait) await run(signal => new Promise<void>((_, reject) => signal.addEventListener("abort", () => reject(signal.reason), { once: true })))
      return brief as never
    },
    triage: async () => { throw new Error("unexpected triage") },
    evidence: async id => { calls.push(id); return "detail" },
  })
  const ctx = {
    options: { codeMode: enabled },
    permission: { assert: async ({ resources }: { resources: string[] }) => {
      calls.push(...resources)
      if (deny) throw new Error("permission denied")
    } },
    tool: { transform: async (add: (editor: object) => void) => {
      add({ add: (tool: any) => tools.set(tool.name, tool) })
      return { dispose: async () => tools.clear() }
    } },
  }
  await registerCodeMode(ctx as never, workflows)
  const invoke = (name: string, input: unknown, sessionID = "session", signal = new AbortController().signal) =>
    tools.get(`testops_code_${name}`)!.execute(input, { sessionID, signal })
  return { tools, invoke, calls }
}

test("flag off leaves tool catalog untouched and deny precedes TestOps I/O", async () => {
  assert.equal((await harness({ enabled: false })).tools.size, 0)
  const { invoke, calls } = await harness({ deny: true })
  await assert.rejects(invoke("analyze", { launchId: 42 }), /permission denied/)
  assert.deepEqual(calls, ["analyze/42"])
})

test("rejects arbitrary capabilities and filter syntax before permissions", async () => {
  const { invoke, calls } = await harness()
  for (const input of [{ launchId: 42, path: "../config.json" }, { launchId: "https://external.invalid" }, { launchId: 42, tag: 'x" or "y' }]) {
    assert.equal((await invoke("analyze", input)).output.code, "INVALID_INPUT")
  }
  assert.deepEqual(calls, [])
})

test("partial summary has stable counts; evidence is selective and isolated", async () => {
  const { invoke, calls } = await harness()
  const { output, content } = await invoke("analyze", { launchId: 42 })
  assert.deepEqual(output, JSON.parse(content))
  assert.equal(output.schema, "testops-code-mode@1")
  assert.equal(output.data.complete, false)
  assert.deepEqual(output.data.filtered, brief.filtered)
  const input = { snapshot: output.data.evidence.snapshot, numbers: ["@11.22", "@99.99"] }
  assert.equal((await invoke("evidence", input, "other")).output.code, "SNAPSHOT_UNAVAILABLE")
  const selected = await invoke("evidence", input)
  assert.equal(selected.output.data.rows[0].message, "detail")
  assert.deepEqual(selected.output.data.rows[1], { number: "@99.99", status: "unavailable" })
  assert.deepEqual(calls, ["analyze/42", "brief", "evidence/42", 7])
  await invoke("evidence", input)
  assert.deepEqual(calls, ["analyze/42", "brief", "evidence/42", 7, "evidence/42"])
})

test("upstream failures stay structured and do not restart the scan", async () => {
  for (const message of ["HTTP 401", "HTTP 429", "ENOTFOUND internal"]) {
    const { invoke, calls } = await harness({ error: new Error(message) })
    assert.deepEqual(JSON.parse((await invoke("analyze", { launchId: 42 })).content),
      { schema: "testops-code-mode@1", status: "error", code: "UPSTREAM" })
    assert.deepEqual(calls, ["analyze/42", "brief"])
  }
})

test("unavailable adapter returns the legacy route without invoking it", async () => {
  const { invoke, calls } = await harness({ error: Object.assign(new Error("missing config"), { code: "UNAVAILABLE" }) })
  assert.deepEqual((await invoke("analyze", { launchId: 42 })).output,
    { schema: "testops-code-mode@1", status: "error", code: "UNAVAILABLE", fallback: "existing_skill" })
  assert.deepEqual(calls, ["analyze/42", "brief"])
})

test("cancellation reaches the adapter and stops the request", async () => {
  const controller = new AbortController()
  const { invoke, calls } = await harness({ wait: true })
  const running = invoke("analyze", { launchId: 42 }, "session", controller.signal)
  await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(calls, ["analyze/42", "brief"])
  controller.abort()
  assert.equal((await running).output.code, "CANCELLED")
})

test("deadline interrupts work and is reported separately from user cancellation", async t => {
  const timeout = new AbortController()
  t.mock.method(AbortSignal, "timeout", () => timeout.signal)
  const { invoke, calls } = await harness({ wait: true })
  const running = invoke("analyze", { launchId: 42 })
  await new Promise(resolve => setImmediate(resolve))
  timeout.abort()
  assert.equal((await running).output.code, "TIMEOUT")
  assert.deepEqual(calls, ["analyze/42", "brief"])
})

import assert from "node:assert/strict"
import test from "node:test"
import { createRunner } from "../src/code-mode/registry.ts"

test("limits parallel reads and stops queued work on cancellation", async () => {
  const controller = new AbortController()
  const run = createRunner(controller.signal, 2, 10)
  let started = 0
  const calls = Array.from({ length: 5 }, () => run(async signal => {
    started++
    await new Promise<void>((_, reject) => signal.addEventListener("abort", () => reject(signal.reason), { once: true }))
  }))
  assert.equal(started, 2)
  controller.abort()
  assert.ok((await Promise.allSettled(calls)).every(result => result.status === "rejected"))
  assert.equal(started, 2)
})

test("rejects work past its request budget", async () => {
  const run = createRunner(new AbortController().signal, 1, 1)
  assert.equal(await run(async () => 7), 7)
  await assert.rejects(run(async () => 8), { code: "LIMIT" })
})

test("stops further requests when upstream response bytes exceed the shared budget", async () => {
  const run = createRunner(new AbortController().signal, 2, 512)
  assert.equal(run.consumeBytes(32 * 1024 * 1024), true)
  assert.equal(run.consumeBytes(1), false)
  await assert.rejects(run(async () => assert.fail("request after budget exhaustion")), { code: "LIMIT" })
  assert.equal(run.limited(), true)
})

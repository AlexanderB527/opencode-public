import assert from "node:assert/strict"
import test from "node:test"
import { withCache } from "../src/cache.mjs"

test("display can fall back to stale while dispatch requires a successful refresh", async () => {
  const key = "test-dispatch-refresh"
  const snapshot = { launch: 42 }
  await withCache(key, 120_000, async () => snapshot)
  const load = async () => { throw new Error("source unavailable") }

  const display = await withCache(key, 120_000, load, { force: true })
  assert.equal(display.stale, true)
  assert.equal(display.value, snapshot)
  await assert.rejects(
    withCache(key, 120_000, load, { force: true, allowStale: false }),
    /source unavailable/,
  )
})

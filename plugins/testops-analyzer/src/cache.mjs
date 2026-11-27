// TTL-кеш снимков: панель и чат не должны дёргать TestOps на каждый чих.
// Ключ включает все параметры запроса, иначе смена команды/тега отдавала бы чужой снимок.
const store = new Map()

export const TTL = { brief: 120_000 }

export function cacheKey(prefix, params) {
  const sorted = Object.entries(params ?? {})
    .filter(([, v]) => v !== undefined && v !== null)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([k, v]) => `${k}=${v}`)
    .join("&")
  return `${prefix}?${sorted}`
}

export async function withCache(key, ttlMs, fn, { force = false, allowStale = true } = {}) {
  const hit = store.get(key)
  const age = hit ? Date.now() - hit.at : null
  if (!force && hit && age < ttlMs) {
    return { value: hit.value, cached: true, ageMs: age, stale: false }
  }
  try {
    const value = await fn()
    store.set(key, { value, at: Date.now() })
    return { value, cached: false, ageMs: 0, stale: false }
  } catch (e) {
    // Источник упал, но есть прошлый снимок — отдаём его с пометкой stale.
    if (hit && allowStale) return { value: hit.value, cached: true, ageMs: age, stale: true, error: String(e?.message ?? e) }
    throw e
  }
}

export function markCache(snap, meta) {
  if (!snap || typeof snap !== "object") return snap
  return {
    ...snap,
    cache: {
      cached: Boolean(meta?.cached),
      stale: Boolean(meta?.stale),
      ageMs: meta?.ageMs ?? 0,
      servedAt: new Date().toISOString(),
    },
  }
}

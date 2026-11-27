export function failure(code: string) {
  return Object.assign(new Error(code), { code })
}

export function createRunner(signal: AbortSignal, parallel: number, budget: number) {
  let active = 0
  let used = 0
  let limited = false
  let bytes = 0
  const queue: (() => void)[] = []
  return Object.assign(async function run<T>(operation: (signal: AbortSignal) => Promise<T>): Promise<T> {
    signal.throwIfAborted()
    if (limited || used >= budget) {
      limited = true
      throw failure("LIMIT")
    }
    used++
    if (active < parallel) active++
    else await new Promise<void>(resolve => queue.push(resolve))
    try {
      signal.throwIfAborted()
      if (limited) throw failure("LIMIT")
      return await operation(signal)
    } catch (error) {
      if ((error as { code?: string })?.code === "LIMIT") limited = true
      throw error
    } finally {
      const next = queue.shift()
      if (next) next()
      else active--
    }
  }, {
    count: () => used,
    limited: () => limited,
    consumeBytes(size: number) {
      bytes += size
      if (bytes > 32 * 1024 * 1024) limited = true
      return !limited
    },
  })
}

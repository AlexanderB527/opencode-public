// RPC-контракт между server-частью плагина и окном десктопа.
//
// id обязан начинаться с id плагина: контракт `testops-analyzer.data` может
// предоставить только плагин `testops-analyzer`.
//
// Импорт именно из "@opencode/plugin/rpc": GUI-бандлу доступен только этот
// модуль (корневой "@opencode/plugin" в renderer недоступен — сборка падает).
import { Rpc } from "@opencode/plugin/rpc"

const TEXT = { type: "string" }

// Параметры одного разбора. Общая схема для brief/analyze.
const BRIEF_IN = {
  type: "object",
  properties: {
    link: { type: "string" },
    team: { type: "string" },
    tag: { type: "string" },
    mode: { type: "string", enum: ["QG", "RM", "HF"] },
    runKind: { type: "string", enum: ["auto", "main", "rerun"] },
    withMessages: { type: "boolean" },
    force: { type: "boolean" },
  },
  required: ["link"],
  additionalProperties: false,
}

const EMPTY_IN = { type: "object", properties: {}, additionalProperties: false }

// Данные передаём JSON-строкой: одна схема на все методы, окно само разбирает.
export const TestopsAnalyzerRpc = Rpc.define({
  id: "testops-analyzer.data",
  methods: {
    // Команды из team_jobs.json, режимы и последние использованные настройки.
    options: { input: EMPTY_IN, output: TEXT },
    // Быстрый снимок без AI: статистика, не-passed список, предупреждения.
    brief: { input: BRIEF_IN, output: TEXT },
    // Готовый промпт разбора (единый шаблон для панели и tool) + запоминание настроек.
    analyze: { input: BRIEF_IN, output: TEXT },
    // План рерана без запуска: джоба, число тестов, @-список.
    rerunPreview: { input: BRIEF_IN, output: TEXT },
    // Фактический запуск Jenkins-джобы рерана (по подтверждению).
    rerun: { input: BRIEF_IN, output: TEXT },
  },
  events: {},
})

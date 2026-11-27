// Единый шаблон промпта для кнопки «Разобрать»: панель и tool отдают один текст,
// чтобы разбор в чате и в сессии был одинаковым.
//
// ВАЖНО про контекст: полный развёрнутый список «@номер статус — название» из сотен
// тестов раздувает промпт и жжёт контекст. Поэтому сюда попадает только компактный
// @-список для Jenkins (номера через пробел, с обрезкой), а полный список с именами
// остаётся в панели. Скилл сам пересчитает всё по запуску — дублировать не нужно.
//
// РАЗДЕЛЕНИЕ УРОВНЕЙ (см. скиллы testops-analysis / testops-rerun-triage):
//   основной прогон -> скилл testops-analysis (эталон, покрытие, раздача реранов);
//   реран           -> скилл testops-rerun-triage по кодовому слову TRIAGE::RERUN.
// Кодовое слово стоит первой строкой: оно однозначно выбирает скилл и режим,
// иначе задача «разбери реран» совпадает с описанием скилла первичного разбора
// и классификация уходит в неверную логику (тип по message вместо trace).
import { truncateTags } from "./tags.mjs"

// Сколько максимум символов @-списка тащить в промпт (Jenkins-формат компактный).
const TAGS_MAX_CHARS = 3000

// Кодовое слово запуска уровня 2. Должно совпадать с scripts/code_parser.py
// (CODE_WORD) и с описанием скилла testops-rerun-triage.
const CODE_WORD = "TRIAGE::RERUN"

export function buildPrompt(brief) {
  const isRerun = brief.runKind?.value === "rerun"
  const launchId = brief.launch?.id ?? ""

  const runKind = isRerun
    ? "РЕРАН — перезапуск только упавших тестов"
    : "ОСНОВНОЙ прогон"
  const runKindNote = brief.runKind?.source === "user"
    ? "тип прогона указал пользователь"
    : `тип прогона определён автоматически (${(brief.runKind?.reasons ?? []).join("; ") || "по составу"})`
      + (brief.runKind?.confidence === "low" ? " — предположительно, уточни, если не совпадает" : "")

  const filters = brief.filters ?? {}
  const tags = truncateTags(brief.rerunTags ?? "", { maxChars: TAGS_MAX_CHARS })

  // 🔴 Первая строка промпта — кодовая строка для рерана: она выбирает скилл
  // и режим до того, как модель начнёт читать остальной текст.
  const parts = []
  if (isRerun && launchId) {
    parts.push(`${CODE_WORD} launch=${launchId}`, "")
  }

  parts.push(
    `Разбери прогон TestOps: ${brief.launch?.url ?? brief.input?.link}`,
    "",
    `Запуск: ${brief.launch?.name ?? "—"}`,
    `Изменён: ${brief.launch?.modified ?? "—"} · закрыт: ${brief.launch?.closed ? "да" : "нет"}`,
    `Режим: ${filters.mode ?? "QG"} · команда: ${filters.teamCanon || filters.team || "все"}`
      + ` · тег тестировщика: ${filters.tag || "не задан"}`,
    `Тип прогона: ${runKind} (${runKindNote})`,
    "",
    `Уже посчитано панелью по фильтрам (${filters.teamCanon || "весь запуск"}${filters.tag ? `, тег «${filters.tag}»` : ""}), счёт по номерам тестов:`,
    `- номеров: ${brief.filtered?.total ?? 0} · passed ${brief.filtered?.passed ?? 0}`
      + ` · failed ${brief.filtered?.failed ?? 0} · broken ${brief.filtered?.broken ?? 0}`
      + ` · skipped ${brief.filtered?.skipped ?? 0}`
      + (brief.filtered?.passRate !== null ? ` · проходимость ${brief.filtered.passRate}%` : ""),
    `- номеров с ошибками: ${brief.filtered?.nonPassedNumbers ?? 0}`
      + (brief.filtered?.newFailed ? ` · из них вне эталона: ${brief.filtered.newFailed}` : ""),
    `- строк результатов: ${brief.filtered?.results ?? 0} (параметризованные тесты дают несколько строк на номер)`,
    `- чтение запуска: ${brief.filtered?.complete ? "полное" : "НЕПОЛНОЕ — цифры могут быть занижены"}`
      + ` (страниц ${brief.filtered?.pages ?? "?"}, строк ${brief.filtered?.results ?? 0}, totalElements ${brief.filtered?.totalElements ?? "?"})`,
  )

  if (brief.filtered?.skippedExcluded) {
    parts.push(`- skipped в @-список не входят (${brief.filtered.skippedExcluded}): для раздачи skipped считается PASS, как в dispatch скилла`)
  }

  if (filters.etalon?.exists) {
    parts.push(`- эталон ${filters.teamCanon}/${filters.mode}: ${filters.etalon.count} тестов, покрытие запуском ${filters.etalon.coverage ?? "?"}%`
      + (filters.etalon.missing ? `, не запускалось ${filters.etalon.missing}` : ""))
  } else if (filters.teamCanon) {
    parts.push(`- эталон ${filters.teamCanon}/${filters.mode} не найден: фильтр был только по полю Team`)
  }
  if (brief.warnings?.length) parts.push("", "Предупреждения:", ...brief.warnings.map((w) => `- ${w}`))

  if (tags.text) {
    parts.push(
      "",
      `@-список для Jenkins (параметр tags, ${tags.total} тестов${tags.truncated ? ", показана часть" : ""}):`,
      tags.text,
    )
    if (tags.truncated) parts.push(`(полный @-список — в панели, кнопка «копировать @-список»)`)
  }

  // Инструкции различаются по уровню: у рерана — классификация, у основного — полный разбор.
  if (isRerun) {
    parts.push(
      "",
      "Что сделать:",
      `1. Загрузи скилл testops-rerun-triage и выполни его режим classify по коду ${CODE_WORD} из первой строки — это уровень 2 (классификация рерана). Уровень 1 (сбор упавших и перезапуск) уже выполнен панелью, повторять его не нужно.`,
      "2. Источник данных — TestOps API. Тип сбоя определяй по trace (backtrace результата), а не по одному message:",
      "   `undefined method 'each' for nil:NilClass` при `savon/soap_fault.rb` в стеке — это сбой бэкенда, а не тестовых данных.",
      "3. Проверь гейты скилла: скан complete, «Гейт сходимости групп: OK», доля триажа.",
      "4. Отдай результат строго по канону скилла (порядок блоков не менять): шапка → гейты → типы с долями (система / автотест / дефект / триаж) → группы с подгруппами → триаж-хвост → кандидат на deep → @-список.",
      "   🔴 Построчную таблицу по каждому тесту в чат НЕ выводи: это детализация `items` в JSON. Запускай скрипт без флага --with-rows. Самодельные секции и перестановки блоков запрещены.",
      "5. Триаж-хвост (low confidence) назови отдельно как «требует точечного разбора»; крупнейший кластер предложи на уровень 3 (deep).",
      "6. Публикуй сводку в Confluence только если об этом попросили отдельно.",
    )
  } else {
    parts.push(
      "",
      "Что сделать:",
      "1. Используй скилл testops-analysis (источник данных — TestOps MCP / REST), не пересчитывай фильтры заново без причины.",
      "2. Если это РЕРАН — анализируй ТОЛЬКО перезапущенные тесты и прямо скажи в отчёте, что это реран;",
      "   отсутствующие в нём тесты падениями НЕ считай. Если основной — оценивай всю картину.",
      "3. Дай @-список для перезапуска, статистику с процентами и классификацию падений (система / автотест / дефект).",
      `4. Если запуск на самом деле РЕРАН (перезапуск только упавших: состав полный, но это повторный прогон), не ограничивайся первичным разбором — загрузи скилл testops-rerun-triage и выполни режим classify с кодом: ${CODE_WORD} launch=${launchId || "<ID>"}`,
      "   Автоопределение типа идёт по составу запуска и для полных реранов (когда перезапущено много тестов) может ошибиться в сторону «основного».",
      "5. Публикуй сводку в Confluence только если об этом попросили отдельно.",
    )
  }

  return parts.join("\n")
}

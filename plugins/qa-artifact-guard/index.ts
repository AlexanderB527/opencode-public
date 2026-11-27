// QA Artifact Guard — fail-closed защита артефактов разбора TestOps.
//
// Мотив (реальный инцидент): агент вместо запуска скриптов скилла
// `testops-analysis` вручную записал `case_meta.json` в неверную папку
// (`TX-T9` вместо `T9-TX`) и посчитал pass_rate по строкам запуска (52.7%)
// вместо номеров эталона (26.4%). Отчёт выглядел валидным — расхождение
// всплыло только при повторном прогоне скилла.
//
// Что блокируется (fail-closed, до выполнения действия):
//   1. запись в `~/.qa-artifacts/**` — артефакты пишет ТОЛЬКО скрипт скилла
//      (`analyze_launch.py` / `scan_testops.py`), а не агент руками;
//   2. запись в `**/skills/testops-analysis/etalons/**` — эталон команды это
//      источник истины, подгонка эталона под свой @-список = обход верификации;
//   3. запись в `**/config/classify_rules.json` — правила классификации не
//      правятся по ходу разбора;
//   4. shell-команды, которые ПИШУТ в etalons (Set-Content / Out-File /
//      update_etalon / Copy-Item / Move-Item и т.п.).
//
// Чего плагин НЕ делает:
//   - не мешает Python-скриптам писать в `~/.qa-artifacts` (они идут через shell
//     и при разборе etalons не трогают);
//   - не блокирует чтение;
//   - никогда не ломает вызов из-за собственной ошибки.
//
// Реализация — OpenCode V2. Блокировка идёт через хук
// `ctx.permission.hook("evaluate")`: он вызывается после конфигурационных
// правил `permissions` и до выполнения действия, а `event.effect = "deny"` —
// штатный способ отказа. Бросок исключения здесь непригоден: V2-хук исполнения
// ошибку агента не прерывает.
//
// ВАЖНО про имена. Хук отдаёт ИМЯ ДЕЙСТВИЯ ПРАВ, а не имя инструмента:
// запись файла приходит как `edit`, исполнение команды — как `shell`.
// Ресурсы приходят путями ОТНОСИТЕЛЬНО каталога (`.qa-artifacts/x.json`).
// Оба факта проверены снятием события (см. lib/matchers.test.ts).

import { decideDeny, norm } from "./lib/matchers.ts";

export const id = "sfa.qa-artifact-guard";

/**
 * Регистрирует защиту артефактов разбора TestOps.
 * @param {any} ctx контекст плагина OpenCode V2
 */
export async function setup(ctx) {
  // Плагин можно выключить опцией { enabled: false } в opencode.json.
  const options = ctx?.options ?? {};
  if (options.enabled === false) return;

  const disabled = Array.isArray(options.disabled) ? options.disabled.map(norm) : [];

  await ctx.permission.hook("evaluate", (event) => {
    try {
      const reason = decideDeny(event, disabled);
      if (!reason) return;
      event.effect = "deny";
      event.message = `[qa-artifact-guard] ЗАБЛОКИРОВАНО: ${reason}`;
    } catch (error) {
      // Собственная ошибка гарда не должна влиять на работу агента.
      ctx?.log?.warn?.(`[qa-artifact-guard] проверка пропущена: ${error?.message ?? error}`);
    }
  });

  ctx?.log?.info?.(`[qa-artifact-guard] активен (выключено: ${disabled.join(", ") || "—"})`);
}

export default { id, setup };

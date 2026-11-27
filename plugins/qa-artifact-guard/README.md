# qa-artifact-guard

OpenCode-плагин: физический (fail-closed) запрет подмены артефактов разбора TestOps
для скилла [`testops-analysis`](../../skills/testing-skills/testops-analysis/SKILL.md).

## Зачем

Реальный инцидент: агент вместо запуска скриптов скилла вручную записал `case_meta.json`
в неверную папку (`TX-T9` вместо `T9-TX`) и посчитал `pass_rate` по строкам запуска (52.7%)
вместо номеров эталона (26.4%). Отчёт при этом выглядел валидным — расхождение всплыло
только при повторном прогоне скилла.

Плагин не даёт повторить такой обход: артефакты прогона создаёт **только** скрипт скилла.

## Что блокируется

| # | Что | Почему |
|---|---|---|
| 1 | запись в `~/.qa-artifacts/**` — и через `edit`, и через shell | артефакты пишет только скрипт скилла, а не агент руками |
| 2 | запись в `**/skills/testops-analysis/etalons/**` | эталон команды — источник истины; подгонка эталона под свой @-список = обход верификации |
| 3 | запись в `**/config/classify_rules.json` | подгонка правил классификации под результат = обход классификации |
| 4 | shell-команды, ПИШУЩИЕ в `etalons` (маркеры записи, `--out`, `update_etalon`) | требует явного решения пользователя, а не агента |

### Работа со скиллом: белый список скриптов

Плагин знает точки входа скилла `testops-analysis` и отличает санкционированную запись
от ручной. Shell-команда, упоминающая `~/.qa-artifacts`, разрешается **только** если
вызывает один из скриптов скилла (`lib/matchers.ts` → `SKILL_WRITE_SCRIPTS`):

`scan_testops.py`, `analyze_launch.py`, `scan_all_teams.py`, `watch_launch.py`,
`wait_team_reruns.py`, `dispatch_team_reruns.py`, `diff_rerun.py`, `classify_rerun.py`,
`merge_launches.py`, `diagnose_skips.py`, `render_markdown.py`, `verify_sets.py`,
`doctor.py`, `fetch_etalons.py`, `git_data.py`, `migrate_data_branch.py`.

`update_etalon.py` в белый список **не входит** намеренно — он пишет в эталоны, а они
защищены абсолютно.

| Команда | Решение |
|---|---|
| `py -X utf8 scripts/scan_testops.py --root .qa-artifacts` | разрешено (скрипт скилла) |
| `py -X utf8 scripts/diff_rerun.py --root .qa-artifacts` | разрешено (скрипт скилла) |
| `Get-ChildItem .qa-artifacts -Recurse` | разрешено (чтение) |
| `Set-Content .qa-artifacts/.../expected_set.json` | **заблокировано** |
| `echo x > .qa-artifacts/.../scan_manifest.json` | **заблокировано** |
| `py -c "open('.qa-artifacts/x','w')..."` | **заблокировано** (inline-код) |
| `py scripts/scan_testops.py --out etalons/T9/etalon_qg.txt` | **заблокировано** (эталоны) |

Чего плагин **не** делает: не блокирует чтение, не мешает скриптам скилла,
не ломает вызов инструмента из-за собственной ошибки.

## Установка

Плагин лежит в этом репозитории и ставится штатной командой OpenCode из git:

```bash
opencode plugin add "git+ssh://git@git.moscow.alfaintra.net/sfa-agents-it/agentic-flow-playbook.git#QA_SkillMate::path:plugins/qa-artifact-guard"
opencode service restart
```

Проверка:

```bash
opencode plugin list          # ожидается: sfa.qa-artifact-guard
```

> `::path:` обязателен: в репозитории несколько независимых пакетов, плагин — один из них.

## Опции

Передаются объектной формой в `opencode.json(c)`:

```jsonc
{
  "plugins": [
    {
      "package": "git+ssh://git@git.moscow.alfaintra.net/sfa-agents-it/agentic-flow-playbook.git#QA_SkillMate::path:plugins/qa-artifact-guard",
      "options": {
        "enabled": true,
        "disabled": []
      }
    }
  ]
}
```

| Опция | Тип | По умолчанию | Смысл |
|---|---|---|---|
| `enabled` | boolean | `true` | `false` — плагин не регистрирует ни одной проверки |
| `disabled` | string[] | `[]` | точечно выключить проверки: `qa-artifacts`, `etalons`, `classify-rules`, `shell-etalons`, `shell-artifacts`, `all` |

Пример: разрешить ручную запись артефактов, но оставить защиту эталонов —

```jsonc
{ "options": { "disabled": ["qa-artifacts"] } }
```

## Как это работает

Плагин — OpenCode **V2** (`setup(ctx)`), блокировка выполняется хуком
`ctx.permission.hook("evaluate")`. Хук вызывается после конфигурационных правил
`permissions` и до выполнения действия; `event.effect = "deny"` — штатный способ отказа,
а `event.message` становится причиной в отказе.

Два факта, полученных снятием события (без них первая версия не блокировала ничего):

- хук отдаёт **имя действия прав**, а не имя инструмента: запись файла приходит как
  `edit` (не `write`), исполнение команды — как `shell`;
- ресурсы приходят путями **относительно каталога** (`.qa-artifacts/x.json`), поэтому
  проверка пути ищет сегмент, а не обрамлённое `/` вхождение.

Бросок исключения в `ctx.tool.hook("execute.before")` для этой задачи **не годится**:
V2 такой хук исполнение не прерывает. Это же ограничение действует и для V1-адаптера
`plugins/sfa/_adapter/v1.ts` в локальной конфигурации — он намеренно глушит ошибки
`before`-хука, поэтому V1-версия этого плагина блокировку не обеспечивает.

## Проверка

Юнит-тесты сопоставления (без запуска OpenCode):

```bash
node --test "plugins/qa-artifact-guard/lib/*.test.ts"
```

Сквозная проверка в живом OpenCode после установки — попытка записи в защищённый путь
должна завершиться отказом `permission.rejected` с текстом `[qa-artifact-guard] ЗАБЛОКИРОВАНО`.

## Ограничение

Проверка смотрит на `action` и `resources` из события прав. Если инструмент пишет файл
путём, не отражённым в `resources`, проверка его не увидит. Для артефактов скилла
(`edit` и shell-запись в `etalons`) этого достаточно — инцидент закрывается.

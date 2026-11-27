# Режим: раздача перезапусков по командным Jenkins-джобам

> Часть скилла `testops-analysis`. Общие правила — `../SKILL.md`.
> Поток данных и пути — `toolkit-guide.md`.

Запускается **после** публикации Страницы 1 (режим разбора запуска). Берёт готовые
`expected_set.json` каждой команды и раскладывает @-списки по КОМАНДНЫМ джобам.

## Когда включается

- После успешной публикации страницы анализа — штатное продолжение разбора запуска.
- Отдельно: «раздай упавших по командам», «перезапусти по командам».

**Не включается**, если разбор ведётся «по моим тестам» (фильтр по тегу тестировщика) —
тогда перезапуск идёт в персональной джобе, см. раздел «Fallback» ниже.

## Два уровня реестров — не путать

| Реестр | Где лежит | Что описывает | Кто владеет |
|---|---|---|---|
| `config/team_jobs.json` | **внутри скилла**, версионируется | командные джобы `DEBUG/<TEAM>` — общая инфраструктура | команда QA |
| `~/.qa-artifacts/autotesters.json` | **вне репозитория** | персональная джоба автотестера (fallback) | каждый оператор свой |

Образец персонального реестра — `config/autotesters.example.json` в скилле.

### Формат `team_jobs.json`

```json
{
  "TMMB": {"job": "DEBUG/TMMB", "env": "qa", "parallel": "15", "rerun": "1",
           "enabled": true, "merge_folders": true, "aliases": ["ТММБ"]}
}
```

| Поле | Смысл |
|---|---|
| `job` | fullname джобы в Jenkins |
| `env` | стенд; по решению — `qa` |
| `parallel` | потоков; **боевое значение 15** (дефолт джобы `1` — прогон часами) |
| `rerun` | автоперезапуск упавшего внутри билда; **боевое `1`** (отсекает флейки) |
| `enabled` | `false` — команда не участвует в раздаче |
| `merge_folders` | `true` — несколько папок одной команды объединяются (union @-списков, статус `merged_scan`); `false` — fail-loud `duplicate_scan` |
| `aliases` | дополнительные написания команды из TestOps; сравниваются **после** нормализации `team_canon` |

Ключ реестра = канон команды = имя папки эталона в кэше `~/.qa-artifacts/etalons/<TEAM>`.

## Запуск

```powershell
# 1. План без сети и без кредов — всегда сначала
py -X utf8 <QA_HOME>\scripts\dispatch_team_reruns.py --launch-id {id} --dry-run

# 2. Боевая раздача (после подтверждения пользователя)
py -X utf8 <QA_HOME>\scripts\dispatch_team_reruns.py --launch-id {id}
```

Флаги: `--root` (корень артефактов), `--teams` (подмножество команд), `--registry`
(путь к реестру), `--env`/`--parallel`/`--rerun` (переопределение реестра), `--force`
(перезапись уже записанного `dispatch.json`), `--timeout`, `--opencode-json`
(fallback-источник кредов Jenkins), `--jenkins-url`/`--user`/`--token` (креды CLI,
высший приоритет).

**Креды Jenkins** берутся: CLI → env → `~/.qa-artifacts/config.json` → `opencode.json`.
`--dry-run` креды не требует вообще.

## Итог: `launches/{launch_id}/dispatch.json`

| Поле | Содержимое |
|---|---|
| `launch_id`, `dispatch_at`, `env`, `parallel`, `rerun` | параметры раздачи |
| `summary` | счётчики `teams_total` / `teams_triggered` / `teams_skipped_empty` / `teams_no_scan` / `teams_unmapped` / `teams_error` / `teams_disabled` / `teams_merged` / `tests_total` / `tests_dispatched` |
| `teams[]` | по команде: `team`, `folder`, `folders`, `job`, `count`, `tags`, `status`, `queue_url`, `build_url`, `error` |

### Статусы команды

| Статус | Значение | Действие |
|---|---|---|
| `triggered` | билд запущен | ничего, ссылка уже в отчёте |
| `merged_scan` | запущен, папки команды объединены | ничего |
| `skipped_empty` | @-список пуст — не запускали | ничего (так и надо) |
| `already_dispatched` | тот же @-список уже раздавался | ничего; `--force` для повтора |
| `disabled` | `enabled: false` в реестре | ничего |
| `no_scan` | нет `expected_set.json` команды | сначала прогнать скан |
| `unmapped` | команды нет в реестре | **решение человека**: дописать джобу в `team_jobs.json` |
| `duplicate_scan` | несколько папок одной команды при `merge_folders: false` | **решение человека** |
| `error` | Jenkins вернул ошибку | смотреть `error` |

## Коды возврата

| Код | Значение |
|---|---|
| `0` | всё ок (включая `skipped_empty` / `no_scan` / `disabled` / `already_dispatched`) |
| `1` | есть `error` / `unmapped` / `duplicate_scan` либо нет реестра |
| `2` | не найдено ни одного `expected_set.json` |

**Exit 1 ≠ провал раздачи:** `dispatch.json` уже записан, остальные команды ушли
в Jenkins. Проблемные команды перечисляются в отчёте — по ним нужно решение человека.

## Запись в `case_meta.json`

После раздачи дописать **свою секцию** `dispatch` в `launches/{id}/{team}/case_meta.json`
(файл создан режимом разбора запуска; секции `run` и `rerun` не трогать):

```json
{
  "dispatch": {
    "dispatched_at": "<ISO>",
    "jenkins_jobs": [{"team": "<TEAM>", "job": "DEBUG/<TEAM>", "build_url": "<url>", "tests": 96}]
  }
}
```

Схема — `config/contracts/case_meta.schema.json`. Потеря секции `run` = потеря `page1_id`
для рерана: это ошибка.

## Публикация страницы «AI run {launch.name}»

Публикует агент через MCP `atlassian`, markdown готовит скрипт:

```powershell
py -X utf8 <QA_HOME>\scripts\render_markdown.py `
   --dispatch "$env:USERPROFILE\.qa-artifacts\launches\{id}\dispatch.json" `
   --launch-url "https://example.invalid" `
   --out "<QA_HOME>\temp\page-ai-run.md"
```

Секции страницы: «Итог раздачи» → «Перезапуски по командам» → «Требуют решения» →
«Не запускались». Вставка — `atlassian_confluence_create_page` (дочерняя к Странице 1)
или `atlassian_confluence_update_page_section`.

**Гейт страницы** (проверяет сам `render_markdown.py`, exit 1): каждая запись
`teams` из `dispatch.json` попала ровно в одну секцию. Числа и ссылки вручную
не переписывать.

## Fallback: персональная джоба

Используется, только если командной нет (`unmapped`), либо разбор идёт по тегу
тестировщика. Реестр — `~/.qa-artifacts/autotesters.json`:

```json
{"Bodrykh": {"job": "DEBUG/Bodrykh_with_testops", "env": "qa", "parallel": "15", "rerun": "1"}}
```

Записи нет → **спросить** ссылку на джобу у пользователя и запомнить (в реестр и в
Memory как durable finding). Угадывать джобу запрещено.

## Запреты

- ❌ Запускать при пустом @-списке команды (`K == 0`) — дефолтный `tags` джобы
  запустит полный прогон.
- ❌ Полагаться на дефолты джобы (`parallel=1`, `rerun=0`) — передавать `15`/`1` явно.
- ❌ Угадывать джобу «по аналогии» / «как в прошлый раз».
- ❌ Парсить HTML Jenkins UI — только MCP (`jenkins_get_item` / `build_item` / `get_build`).
- ❌ Менять `env` на что-то кроме `qa` без явного указания пользователя.

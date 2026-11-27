# TestOps: настройка и справочник

> Часть скилла `testops-analysis`. Режимы и общие правила — `../SKILL.md`.

Скилл разбирает упавшие автотесты в TestOps: находит тесты своей команды по эталону, считает
статистику, публикует отчёт в Confluence, перезапускает упавшие в Jenkins и классифицирует
результаты перезапуска. Здесь — настройка окружения, поток данных, эталоны, артефакты и
справочник скриптов; исполнение лежит в этом же скилле — `scripts/`, `config/`. Эталоны команд —
данные в отдельной ветке, в скилле их нет (см. «Эталоны команд»).

---

## Режимы

У скилла три режима: разбор запуска, классификация перезапуска и глубокий разбор.
Выбор режима и входные данные — в `../SKILL.md`.

Логика работы: разбор запуска даёт картину «кто упал», классификация перезапуска — «кто виноват»,
глубокий разбор — «что конкретно делать». Режимы запускаются раздельно, каждый со свежим контекстом,
но делят состояние через артефакты (см. ниже).

---

## Как это устроено

```
skills/testing-skills/testops-analysis/
├── SKILL.md                      режимы и общие правила
├── references/
│   ├── run-analysis.md           режим: разбор запуска
│   ├── rerun-classifier.md       режим: классификация перезапуска
│   ├── deep-failure.md           режим: глубокий разбор
│   └── toolkit-guide.md          этот файл
├── scripts/                      12 Python-модулей, только стандартная библиотека
├── config/
│   ├── classify_rules.json       как классифицировать ошибки (таксономия + кластеры)
│   └── autotesters.example.json  образец реестра персональных Jenkins-джоб
└── evals/                        проверка срабатывания скилла
```

Эталонов команд в скилле нет: они живут в ветке `qa-analysis-data`
(`AUTOTEST/sfa`, `etalons/<TEAM>/etalon_<HF|QG|RM>.txt`), а `scripts/fetch_etalons.py`
скачивает их в кэш оператора `~/.qa-artifacts/etalons/`.

**Движок лежит внутри скилла.** `SKILL.md` держит режимы и общие правила, детали режимов —
в `references/`, исполнение — в `scripts/` и `config/`. Движок и инструкция разворачиваются
и версионируются вместе; данные (эталоны, артефакты прогонов) — вне скилла.

**После установки раскладка плоская.** Конфигуратор разворачивает скиллы плоско: каталог скилла
становится `~/.config/opencode/skills/testops-analysis/`, т.е. `<QA_HOME>` =
`~/.config/opencode/skills/testops-analysis`. Вложенный путь `skills/testing-skills/...` — только
раскладка исходников в репозитории. Артефакты прогонов и эталоны лежат вне скилла и
репозитория — в `~/.qa-artifacts/` (`launches/`, `reruns/`, `etalons/`).

**Скрипты лежат в одной папке намеренно.** Они импортируют друг друга:
`classify_rerun` → `classifier` + `diff_rerun` + `scan_testops`, `render_markdown` → `classifier`,
`diff_rerun` → `scan_testops`. Разнести по папкам = `ModuleNotFoundError`.

### Поток данных

```
TestOps API
    │  scan_testops.py        пагинация size=100, фильтр Team ∩ эталон
    ▼
~/.qa-artifacts/launches/{id}/{team}/
    expected_set.json           кто есть и с каким статусом
    nonpass_results.jsonl       только не-PASS, с текстами ошибок
    scan_manifest.json          полнота скана, новые тесты команды
    │
    ├─ verify_sets.py           сверка @-списка с эталоном
    ├─ diagnose_skips.py        почему тесты не участвовали (Jenkins + репозиторий)
    ├─ render_markdown.py       готовый markdown страницы (→ MCP atlassian)
    └─ jenkins buildWithParameters   перезапуск упавших
            │
            ▼
    ~/.qa-artifacts/reruns/{rerun_id}/
        diff_rerun.py           healed / failed_again / skipped / absent
        classify_rerun.py       тип, кластер, confidence, рекомендация
```

Состояние между режимами передаётся **только через `~/.qa-artifacts/`**. Страница в Confluence —
витрина для людей, её парсинг используется лишь как fallback.

---

## Настройка

Скилл приезжает вместе с репозиторием: `testops-analysis` зарегистрирован в пресете `qa`, и
установщик (openconfigurer) разворачивает его плоско — в `~/.config/opencode/skills/testops-analysis`
(конфигуратор раскладывает скиллы по имени каталога, без промежуточной группы);
ставить его отдельно не нужно. Настроить нужно три вещи.

### 1. Доступы

| Что | Зачем |
|---|---|
| `TESTOPS_TOKEN` (env) | чтение TestOps API: сканы, тест-кейсы, шаги |
| git в PATH + доступ к `AUTOTEST/sfa` | эталоны и база результатов читаются/пишутся через git (креды оператора: Credential Manager/SSH) |
| MCP `atlassian` | публикация страниц в Confluence (опционально) — токен `CONFLUENCE_PERSONAL_TOKEN` в `opencode.json`, скриптам не нужен |

```powershell
setx TESTOPS_TOKEN "<токен TestOps>"
```

```bash
# macOS / Linux — в ~/.bashrc или ~/.zshrc
export TESTOPS_TOKEN="<токен TestOps>"
```

Git-доступ проверяется один раз: `git ls-remote https://example.invalid`
(должен отработать без запроса пароля — креды уже сохранены системой или SSH-агентом).
Отдельный персональный токен Bitbucket скиллу не нужен.

Публикация в Confluence идёт не скриптом, а через MCP `atlassian` (уже в пресете). Готовый markdown
для страницы считает `render_markdown.py`.

> После `setx` **закрой и открой терминал заново** — иначе переменная не видна процессу.

Альтернатива: `config.json` в корне артефактов (ключи `testops_token`, `testops_base_url`,
`etalons_project`/`etalons_repo`/`etalons_branch`/`etalons_path`, `results_branch`).
Нужен редко — когда стенд отличается от дефолта.

### 2. MCP: `testops` (глубокий разбор) и `atlassian` (публикация)

Скриптовые режимы MCP не требуют. Глубокий разбор читает определения и шаги тест-кейсов
через MCP `testops` — без него он неполный. Публикация в Confluence идёт через MCP
`atlassian` (уже в пресете; токен — `CONFLUENCE_PERSONAL_TOKEN` в `opencode.json`).

В `~/.config/opencode/opencode.json`:

```jsonc
"testops": {
  "type": "local",
  "enabled": true,                    // ← по умолчанию false
  "command": ["npx", "-y", "@syn7xx/testops-mcp-server",
              "--url", "https://example.invalid",
              "--token", "<токен TestOps>"],
  "environment": { "NODE_TLS_REJECT_UNAUTHORIZED": "0" }
}
```

### 3. Своя Jenkins-джоба (для перезапуска упавших)

У каждого автотестера **своя** джоба, принимающая @-список в параметр `tags`.
Реестр — `~/.qa-artifacts/autotesters.json` (вне репозитория, у каждого оператора свой);
образец для заполнения — `config/autotesters.example.json` внутри скилла:

```json
{
  "Ivanov": {
    "job": "DEBUG/Ivanov_with_testops",
    "env": "qa",
    "parallel": "15",
    "rerun": "1"
  }
}
```

`parallel=15` и `rerun=1` обязательны: дефолты джобы (`1` и `0`) небоевые — прогон на одном
потоке идёт часами, а без автоперезапуска в отчёт попадают флейки.

Если записи нет — скилл спросит ссылку на джобу и предложит сохранить. Угадывать не будет.

---

## Проверка окружения

```powershell
py -X utf8 "$env:USERPROFILE\.config\opencode\skills\testops-analysis\scripts\doctor.py"
# либо, если переменная задана:
py -X utf8 <QA_HOME>/scripts/doctor.py
```

```bash
python3 ~/.config/opencode/skills/testops-analysis/scripts/doctor.py
```

Ожидаемый вывод:

```
=== QA-toolkit doctor ===
  QA_HOME:        ...\skills\testops-analysis
  artifacts root: <local-path><USER>\.qa-artifacts
  эталоны (кэш):  <local-path><USER>\.qa-artifacts\etalons
  эталоны (источник): AUTOTEST/sfa @ qa-analysis-data (etalons/)
  результаты (кэш): <local-path><USER>\.qa-artifacts\results
  результаты (источник): AUTOTEST/sfa @ qa-analysis-data (results/)

[OK] TestOps token  (источник: env TESTOPS_TOKEN, имя env: TESTOPS_TOKEN)
[OK] git: git version 2.45.1.windows.1
[i] Confluence: через MCP atlassian (токен в opencode.json)
[OK] classify_rules.json: ...\config\classify_rules.json
[OK] эталоны в кэше: 13 команд, 36 файлов (BIT, ECOM, TACO, TAMUL, ...)
[OK] autotesters.json (<local-path><USER>\.qa-artifacts\autotesters.json) — реестр персональных Jenkins-джоб (шаблон: config/autotesters.example.json)
[OK] база результатов (кэш): 3 команд, 12 прогонов (TREX, TT, TX-T9)

СТАТУС: OK — можно работать
```

Пустой кэш эталонов — не приговор: `doctor.py` подскажет команду
`fetch_etalons.py` (нужен git-доступ к `AUTOTEST/sfa`). Проверка офлайн: `fetch_etalons.py --check`.

`doctor.py` не ходит в сеть — он проверяет только токен TestOps, наличие git, файлов и папок.

---

## Как пользоваться

Открой opencode в любой папке и напиши задачу обычным языком. Режим находится по триггерам
в описании, дальше ведут диалог сами.

### Разбор запуска

```
разбери упавшие в запуске 1234567, команды T9 и TX, тип QG
```

Что происходит:

1. **Скан.** `scan_testops.py` проходит запуск постранично (size=100), отбирает тесты
   по двойному фильтру — `Team ∈ команды` **и** `номер ∈ эталон`. Пишет артефакты в папку команды.
2. **Сверка.** `verify_sets.py` сравнивает @-список с эталоном → вердикт `ЧИСТО` / `ЕСТЬ ЛИШНИЕ`.
3. **Статистика в чат.** По каждой команде: прошло / всего / процент, @-список в строчку,
   таблица ошибок, сгруппированных по тексту.
4. 🔴 **Стоп.** Публикация в Confluence — только после явного «публикуй».
5. 🔴 **Стоп.** Перезапуск в Jenkins — только после явного «запускай».

Гейты, без которых публикация блокируется: длина @-списка равна числу упавших,
число строк в таблице равно числу не-PASS, скан полный (`complete`), хвост перепроверен.

Если тип (HF/QG/RM) или команда не читаются однозначно — скилл **спросит**. Не угадывай
за него: неверный вход делает весь скан неверным.

### Классификация после перезапуска

```
классифицируй реран 1240000 относительно 1234567
```

Что происходит:

1. `diff_rerun.py` сканирует новый запуск и сравнивает с первичным:
   `healed` (вылечился) / `failed_again` / `skipped` / `absent` (не участвовал).
2. `classify_rerun.py` присваивает каждому снова-упавшему тип по правилам из
   `classify_rules.json` + семантическая кластеризация по смыслу ошибки
   (все «Unable to find visible xpath» — один кластер, а не N по локаторам).
3. Спорные случаи добиваются через git-верификацию в `AUTOTEST/sfa`:
   локус → история → regression timeline.
4. Вердикт в чат, по кластерам + отдельная таблица по каждому тесту.

Три типа и что с ними делать:

| Тип | Кто чинит | Приоритет |
|---|---|---|
| Ошибка системы / инфраструктуры | DevOps, окружение | низкий для кода тестов |
| Ошибка автотеста | QA-автоматизатор | средний |
| Дефект продукта | Разработка | высокий, заводить дефект |

Confidence считается по evidence, а не «на глаз»:
`message=1, steps=1, stability=1, git_locus=2, git_history=2, regression_commit=2`;
score ≥ 4 → high, 2–3 → medium, < 2 → low.

Статус снова-упавших **не меняется** (FAIL остаётся FAIL). Вылечившиеся — наоборот,
помечаются `PASS` в основной таблице.

### Глубокий разбор конкретных тестов

```
почему упал @10.05.03.01 и что чинить
```

На каждый тест — блок:

- **Причина** — корневая, с локусом `файл:строка` или ссылкой на Confluence.
- **Решение** — что именно править и по какому критерию считать готовым.
- **Статус подтверждённости** — «подтверждено» или «не подтверждено» + чего не хватило.

Скилл read-only: ничего не меняет, в Confluence не публикует без просьбы.
Поиск в репозитории идёт по лестнице (номер → префикс группы → фрагмент ошибки → название),
максимум 4 попытки, после чего задаёт вопрос вместо блуждания по дереву.

---

## Эталоны команд

Эталон — список тестов команды. Это **единственный источник истины** о том, чьи тесты;
поле Team в TestOps — только дополнительный сторож.

**Где лежат.** Источник истины — ветка `qa-analysis-data` репозитория `AUTOTEST/sfa`:
https://example.invalid

```
etalons/<TEAM>/etalon_HF.txt     etalon_QG.txt     etalon_RM.txt
```

В репозитории скилла эталонов нет. `scripts/fetch_etalons.py` скачивает их через
git (partial + sparse clone, ~0.5 МБ) в кэш оператора `~/.qa-artifacts/etalons/<TEAM>/etalon_<тип>.txt`
(`QA_ARTIFACTS_ROOT` / `--root` / `--dest` переопределяют путь). Скрипты скилла
(`scan_testops`, `verify_sets`) читают только кэш.

```powershell
# первый запуск на машине (нужен git-доступ к AUTOTEST/sfa)
py -X utf8 <QA_HOME>\scripts\fetch_etalons.py

# точечно / после правки эталона в ветке
py -X utf8 <QA_HOME>\scripts\fetch_etalons.py --team TX-T9 --type QG --refresh

# что есть в ветке / состояние кэша (--check работает без сети)
py -X utf8 <QA_HOME>\scripts\fetch_etalons.py --list
py -X utf8 <QA_HOME>\scripts\fetch_etalons.py --check
```

Формат строки: `номер Название` (номер без `@`). Кодировка UTF-8, дублей быть не должно,
номер разбирается регуляркой `^\d{2}(\.\d{2,3})+`. Параметры источника (project, repo,
branch, path) — в `~/.qa-artifacts/config.json` или env
(`QA_ETALONS_BRANCH`, …); детали — `fetch_etalons.py --help`.

### Имена папок — латиницей

| В TestOps | Папка |
|---|---|
| `SFA Команда BIT` | `BIT` |
| `SFA Команда ТАМУЛ` | `TAMUL` |
| `SFA Команда ТСПб` | `TSPB` |
| `SFA Команда ТЕ` | `TE` |
| `SFA Команда ТММБ` | `TMMB` |
| `SFA Команда T9` + `ТX` | `TX-T9` (пока общая) |

Остальные: `ECOM, TACO, TCAT, TFU, TPR, TREX, TT`.

> `T9` и `TX` пока делят папку `TX-T9` — статистика по ним считается вместе.

### Обновление эталона

Эталоны меняются через PR в ветку `qa-analysis-data`, не правкой на месте в чужой ветке.
Локальный кэш — рабочая копия: правка в кэше действует только у этого оператора.

**Разово выгрузить эталон из доверенного прогона:**

```powershell
py -X utf8 <QA_HOME>\scripts\export_team_etalons.py --launch-id <ID> --type QG
```

Скрипт раскладывает тесты по командам и пишет в `~/.qa-artifacts/etalons/<TEAM>/etalon_<TYPE>.txt`
(переопределить: `--out <папка>`). Полезные флаги: `--exclude "T9,TX"` — не трогать эти команды,
`--pass-filter` — брать только PASS-строки. Дальше — PR в ветку.

**Дописать подтверждённые новые тесты:**

```powershell
# номерами
py -X utf8 <QA_HOME>\scripts\update_etalon.py --etalon "$env:USERPROFILE\.qa-artifacts\etalons\BIT\etalon_QG.txt" --add "16.01.05 16.01.06"

# или JSON [{number, name}]
py -X utf8 <QA_HOME>\scripts\update_etalon.py --etalon "$env:USERPROFILE\.qa-artifacts\etalons\BIT\etalon_QG.txt" --input "<json с new_test_numbers>"
```

Скрипт делает естественную сортировку, дедуп и `.bak` перед записью. Руками эталон
не дописывать — только через скрипт. Перенос в ветку — PR файла
`etalons/<TEAM>/etalon_<тип>.txt` целиком; после мержа остальные делают `--refresh`.

---

## База результатов разбора

Разборы накапливаются в ветке `qa-analysis-data` (`AUTOTEST/sfa`, `results/<TEAM>/`):
https://example.invalid

Это **знание, а не артефакты**: агент читает его в начале следующего разбора, чтобы
не искать причину заново.

```
results/<TEAM>/
├── index.json      КОМПАКТ: последние 10 прогонов + топ причин (главный вход для агента)
├── causes.jsonl    агрегат причин: частота, типичный фикс, владелец, когда видели
└── launches/<launch_id>_<HF|QG|RM>.json
                    сырьё: статистика + НЕ-PASS тесты + причины
```

**Чтение (перед разбором):**

```powershell
py -X utf8 <QA_HOME>\scripts\fetch_results.py --team TX-T9          # индекс + причины
py -X utf8 <QA_HOME>\scripts\fetch_results.py --team TX-T9 --last 3  # + 3 последних прогона
py -X utf8 <QA_HOME>\scripts\fetch_results.py --all                  # все команды
py -X utf8 <QA_HOME>\scripts\fetch_results.py --check                # офлайн, что в кэше
```

**Запись (после рерана или глубокого разбора, по подтверждению):**

```powershell
py -X utf8 <QA_HOME>\scripts\publish_results.py --team TX-T9 --type QG --launch-id 1187354           # план + локально
py -X utf8 <QA_HOME>\scripts\publish_results.py --team TX-T9 --type QG --launch-id 1187354 --push    # в ветку
```

### Полный цикл: большой прогон → рераны по командам → разбор

```
1. scan_testops.py            → non-PASS по командам (expected_set, at_list)
2. dispatch_team_reruns.py    → @-списки в джобы DEBUG/<TEAM> (build_url в dispatch.json)
3. wait_team_reruns.py        → build -> launch, ожидание всех, classify_rerun, publish
4. publish_results.py         → (внутри шага 3) база знаний
```

Связи «Jenkins build → TestOps launch» в API нет, её восстанавливает `wait_team_reruns.py`:
номер сборки из `build_url` + имя рерана (`DEBUG - qa (Команды/TSPB_QG - #1346)`) + окно
`createdDate` от `dispatch_at`. Проверка: `--resolve-only` печатает карту и ничего не ждёт;
`--map TEAM=launch_id` позволяет задать вручную, если имя джобы нестандартное.

**Готовность рерана** — `closed == true` в TestOps (закрывается по завершении джобы).
Стабильность счётчиков используется только если флага нет: у живого прогона счётчики
могут не меняться минутами, и «стабильно 2 опроса» даёт ложную готовность (проверено
на живом DEBUG-запуске: total=3 не менялся 5 с, а прогон продолжался).

```powershell
# дождаться всех реранов и разобрать (публикация — по --push)
py -X utf8 <QA_HOME>\scripts\wait_team_reruns.py --launch-id 1327498 --type QG
# только карта build -> launch
py -X utf8 <QA_HOME>\scripts\wait_team_reruns.py --launch-id 1327498 --resolve-only
```

Статусы в `launches/{id}/rerun_wait.json`: `done` / `missing` (запуск так и не появился) /
`timeout` / `error` (3 подряд ошибки опроса). Частичный результат — норма: разобранные
команды публикуются, неразобранные перечисляются в итоге.

### Слияние запусков

TestOps поддерживает штатное слияние: запуск-источник вливается в целевой и перестаёт
существовать как отдельный запуск (API отдаёт по нему 404). Операция **необратима**.

```powershell
# план (по умолчанию): проверяет запуски, показывает, что будет отправлено
py -X utf8 <QA_HOME>\scripts\merge_launches.py --from 1334823 --to 1363174

# выполнить слияние
py -X utf8 <QA_HOME>\scripts\merge_launches.py --from 1334823 --to 1363174 --yes
```

Контракт API: `POST /api/launch/merge`, тело `{"from": <источник>, "to": <цель>}`
(поля называются именно `from`/`to`; любые другие варианты дают HTTP 500). Требования:
оба запуска существуют, они разные и лежат в одном проекте. Журнал слияний пишется в
`~/.qa-artifacts/launches/<to>/merge.json`.

> ⚠️ После слияния исходный launch id не существует — артефакты разбора по нему
> (`launches/<from>/`) остаются историческим снимком, ссылки на него в TestOps битые.

### Автозапуск одного прогона

`watch_launch.py` — когда реран не нужен или id уже известен:

```powershell
# ждать завершения, затем скан + локальная сборка результата
py -X utf8 <QA_HOME>\scripts\watch_launch.py --launch-id 1327498 --teams "T9,TX" --type QG

# то же с публикацией в базу
py -X utf8 <QA_HOME>\scripts\watch_launch.py --launch-id 1327498 --teams "T9,TX" --type QG --push

# дождаться и рерана: классификация + публикация с причинами
py -X utf8 <QA_HOME>\scripts\watch_launch.py --launch-id 1327498 --teams "T9,TX" --type QG \
    --wait-rerun 1334823 --push

# только готовность (диагностика)
py -X utf8 <QA_HOME>\scripts\watch_launch.py --launch-id 1327498 --poll-only
```

Готовность = счётчики не меняются `--stable` опросов подряд (2 × 90 с) и `total > 0`;
`closed` ускоряет, но не обязателен (у `autoclose=false` он всегда `false`). Ожидание идёт
**в скрипте**, не в чате: иначе каждый опрос — отдельный tool-call и токены.

> ⚠️ Для **командных реранов** интервал стабильности ненадёжен (проверено на живом
> DEBUG-запуске: счётчики стояли 5 с, а джоба шла). Там используй `wait_team_reruns.py`:
> готовность по `closed == true`.

**Контракт документа** — `config/contracts/run_result.schema.json`, индекса —
`config/contracts/results_index.schema.json`. Проверка автоматическая: публикация
не пройдёт с невалидным документом.

🔴 **Причины — только из фаз rerun/deep.** На первичном скане `causes` пуст: статус
и сообщение — не причина. У причины есть `status` (`suspected` / `confirmed` / `refuted`)
и `evidence[]`; `suspected` не повышается до `confirmed` без подтверждения (git-локус,
дефект, ручная проверка). База — источник гипотез: при разборе причину подтверждай заново.

**Прирост базы.** `causes.jsonl` — одна строка на пару **`(номер теста, кластер)`**, поэтому у
одного теста в истории может быть несколько строк, если в разных прогонах он падает
по-разному. `seen_count` увеличивается только при новом `launch_id`, `launches[]` хранит
до 10 последних id — поэтому повторная публикация того же прогона не раздувает счётчики.
`index.json` — производная (10 прогонов, топ-15 причин), пересобирается целиком.
Файлы `launches/*.json` **хранятся все** (решение владельца): ~десятки КБ на прогон,
~1.5 МБ в неделю на 13 команд; если понадобится ротация, снимки удаляются отдельно —
агрегат причин это не затрагивает.

**Технические детали:**
- Чтение — git (partial + sparse clone, ~0.5 МБ на ветку, без токена).
- Запись — **git push** (в этом репозитории REST-правка файлов запрещена hook'ом
  «Branch ... can only be modified through pull requests»; проверено). Используются
  git-креды оператора; для CI — git-креды джобы или `--repo-url`.
- Публикация идемпотентна: повторный запуск того же прогона даёт `no-changes`.
- Если ветки нет — она создаётся автоматически как orphan (без дерева автотестов).

---

## Артефакты прогонов

Хранятся **вне репозитория**: `~/.qa-artifacts/`. Так `git pull` в `~/.config/opencode`
не спотыкается о результаты прогонов, а у каждого оператора своя история.

```
~/.qa-artifacts/
├── launches/<launch_id>/<team>/
│   ├── expected_set.json        ожидаемый набор + статусы + тексты ошибок
│   ├── nonpass_results.jsonl    только не-PASS
│   ├── scan_manifest.json       полнота скана, tail_checked, новые тесты
│   ├── at_list.txt              @-список
│   └── case_meta.json           секции run/dispatch/rerun (page1_id/page2_id)
├── reruns/<rerun_id>/
│   ├── rerun_diff.json          healed / failed_again / skipped / absent
│   └── rerun_classification.json тип, кластеры, confidence
├── etalons/<TEAM>/              кэш эталонов из ветки qa-analysis-data
│   ├── etalon_HF.txt            (+ .source.json — откуда и когда скачано)
│   └── etalon_QG.txt            etalon_RM.txt
└── stability.jsonl              история метрики стабильности
```

Переопределить корень: переменная `QA_ARTIFACTS_ROOT` или флаг `--root` у скрипта.

---

## Справочник скриптов

Для тех, кто сопровождает скилл. Пути определяет `_paths.py`:
`QA_HOME` = env `QA_HOME` → родитель папки `scripts/`.

| Скрипт | Назначение | Ключевые аргументы |
|---|---|---|
| `scan_testops.py` | Скан запуска, двойной фильтр, гейт хвоста | `--launch-id`, `--teams`, `--etalon`, `--tail-check`, `--tester-tag` |
| `diagnose_skips.py` | Почему тесты эталона не участвовали: своя сборка (Jenkins) + свой репозиторий | `--launch-id`, `--team`, `--no-jenkins`, `--no-repo`, `--repo-branch` |
| `verify_sets.py` | Сверка @-списка с эталоном | `--at-list`, `--etalon` или `--expected-set` |
| `diff_rerun.py` | Скан рерана + дифф с первичным | `--launch-id`, `--expected-launch-id`, `--team` |
| `classify_rerun.py` | Классификация, кластеры, confidence | `--rerun-id`, `--scan`, `--expected-launch-id`, `--verdicts`, `--chat`, `--rules` |
| `classifier.py` | Классификатор ошибок + `natural_key`/`with_at` (модуль) | `load_rules(path)`, `classify_error(message)` |
| `render_markdown.py` | Детерминированный markdown страниц (числа считает Python) | `--expected-set`, `--rerun-diff`, `--classification`, `--out` |
| `export_team_etalons.py` | Выгрузка эталона команды | `--launch-id`, `--type`, `--root`, `--out` |
| `fetch_etalons.py` | Скачивание эталонов из ветки `qa-analysis-data` в кэш | `--team`, `--type`, `--refresh`, `--check`, `--list`, `--dest` |
| `publish_results.py` | Публикация разбора в базу `qa-analysis-data` (git push) | `--team`, `--type`, `--launch-id`, `--rerun-id`, `--push`, `--dry-run` |
| `fetch_results.py` | Чтение базы результатов в локальный кэш | `--team`, `--all`, `--last`, `--check`, `--list` |
| `watch_launch.py` | Ожидание прогона + скан + (опц.) реран + публикация | `--launch-id`, `--teams`, `--type`, `--interval`, `--timeout`, `--wait-rerun`, `--push`, `--poll-only` |
| `wait_team_reruns.py` | build → rerun launch, ожидание ВСЕХ команд, классификация и публикация | `--launch-id`, `--dispatch`, `--map`, `--interval`, `--timeout`, `--start-timeout`, `--resolve-only`, `--push` |
| `git_data.py` | Доступ к веткам данных через git: чтение (partial+sparse) и push (модуль) | — |
| `update_etalon.py` | Дописывание новых тестов в эталон | `--etalon`, `--input`, `--add` |
| `merge_launches.py` | Слияние запусков через `POST /api/launch/merge` (`{"from":..,"to":..}`): источник вливается в целевой и исчезает; по умолчанию только план, выполнение — с `--yes` | `--from`, `--to`, `--yes`, `--dry-run`, `--root` |
| `migrate_data_branch.py` | Разовый перенос эталонов `Testops_checkerData/list_Test` → `qa-analysis-data/etalons`; по умолчанию dry-run | `--dry-run`, `--push`, `--yes`, `--src-branch`, `--src-path`, `--dst-branch`, `--dst-path` |
| `doctor.py` | Предполётная проверка окружения | `--root` |
| `_paths.py` | Единый источник путей (модуль) | — |

`classify_rules.json` — данные, не код: классы ошибок и семантические кластеры.
Дополнять можно без правки скриптов.

---

## Окружение: чего-то нет — предложи установить

Правило скилла (`SKILL.md`, rule 14): отсутствие компонента — это **остановка с предложением**,
а не повод обойти шаг. Порядок:

1. **Проверить, чем именно сигнал является** (таблица ниже). Если сомнение — `doctor.py` показывает
   состояние сразу по всем пунктам.
2. **Самолечимое — делать без вопросов:** пустой кэш эталонов/базы, отсутствие `~/.qa-artifacts`,
   устаревший кэш клона. Это `fetch_etalons.py`, `fetch_results.py`, повторный скан.
3. **Требует установки/настройки — предложить команду и спросить разрешение.** Ставить пакеты,
   менять `opencode.json`, прописывать переменные окружения без согласия пользователя запрещено.
4. После установки — **повторить проверку** (`doctor.py`) и продолжить с того же шага.
   Не начинать разбор заново.
5. Если установка невозможна (нет прав, закрытый стенд) — сказать прямо, **какие шаги деградируют**
   (например: «скан возможен, публикация в базу — нет»), и не выдавать частичный результат за полный.

| Сигнал | Чего нет | Что предложить |
|---|---|---|
| `git не найден в PATH` / `git не запускается` | git | Windows: `winget install --id Git.Git -e`; macOS: `brew install git`; Linux: `sudo apt install git` → `git --version` |
| `doctor.py`: `git: ... (нужен 2.25+)` | свежий git (partial/sparse clone) | обновить git тем же пакетным менеджером |
| `TypeError: 'type' object is not subscriptable` / `python -V` < 3.9 | Python 3.9+ | Windows: `winget install --id Python.Python.3.12 -e`; macOS: `brew install python@3.12`; Linux: `sudo apt install python3` |
| `нет токена TestOps` / HTTP 401 от TestOps | `TESTOPS_TOKEN` | `setx TESTOPS_TOKEN "<токен>"` + **новый** терминал; macOS/Linux: `export` в профиле |
| `нет доступа к репозиторию` / `could not read Username` (код 2) | git-креды на `AUTOTEST/sfa` | сохранить креды в Credential Manager / SSH-ключ; проверить `git ls-remote https://example.invalid` |
| `ветки ... нет в репозитории` (код 4) | ветка данных | проверить `--branch`; создавать только осознанно — с `--create-branch` |
| `кэш пуст` (эталоны/результаты) | данные | самолечение: `fetch_etalons.py --refresh`, `fetch_results.py --all` |
| `non-fast-forward` при публикации | параллельная публикация | скрипт повторяет сам (до 3 раз); иначе — повторить команду |
| MCP `atlassian` недоступен | Confluence | не блокер: отчёт в чат + база; включить MCP по `toolkit-guide` (§ Настройка) |
| MCP `testops` недоступен | определения/шаги тест-кейсов | включить в `opencode.json` (`"enabled": true`) — иначе глубокий разбор неполный |
| Jenkins 401 / нет кредов | Jenkins | `--jenkins-url/--user/--token` или `~/.qa-artifacts/autotesters.json`; разбор при этом возможен |
| `VPN` / таймауты до TestOps, Confluence, git | сеть | подключить VPN; до этого — только работа с уже скачанными артефактами |

> Формулировка предложения: «На машине нет X. Это нужно для шага Y. Предлагаю выполнить `<команда>`
> (или настрой `<...>`). Выполнить?» — без установки до ответа «да».

## Если не работает

| Симптом | Причина | Решение |
|---|---|---|
| `токен TestOps не задан` | нет переменной окружения | `setx TESTOPS_TOKEN ...`, затем **новый** терминал |
| `ModuleNotFoundError: scan_testops` | скрипты разложены по разным папкам | все `.py` должны лежать в одной `scripts/` |
| `эталоны в кэше: 0 команд` | кэш пуст (новый оператор / почистили) | `py -X utf8 <QA_HOME>\scripts\fetch_etalons.py` (нужен git-доступ) |
| `authentication failed` / `403` при `fetch_etalons.py` | нет git-кредов до `AUTOTEST/sfa` | настрой Credential Manager/SSH или git-креды джобы |
| `HTTP 404` при `fetch_etalons.py` | команды нет в ветке `qa-analysis-data` | `--list` покажет доступные; новую команду — PR в `etalons/<TEAM>/` |
| `git push` не удался при `publish_results.py --push` | нет прав/кредов на `AUTOTEST/sfa` | проверь git-креды (credential helper/SSH) и права на запись |
| `кто-то опубликовал в эту ветку раньше (non-fast-forward)` | два оператора публиковали одну команду одновременно | скрипт повторяет сам (до 3 раз); если не помогло — просто повтори команду |
| `ветки ... нет в репозитории` при `--push` | опечатка в имени ветки (защита от создания мусорной ветки) | проверь `--branch`; если ветку правда надо создать — добавь `--create-branch` |
| `git: ... (нужен 2.25+ ...)` в `doctor.py` | старый git — partial/sparse clone не поддерживается | обнови git |
| `publish_results.py`: «скан неполный» | `scan_manifest.complete=false` | пересканируй; публиковать неполные цифры нельзя (`--allow-incomplete` — только осознанно) |
| `publish_results.py`: «документ не проходит контракт» | изменился формат артефактов | правь схему `config/contracts/run_result.schema.json` или скрипт, не обходи валидацию |
| база результатов пуста (`fetch_results` → 404) | команда ещё не публиковала разборы | это норма: первая публикация создаст `results/<TEAM>/` |
| Скилл не виден в opencode | скиллы читаются при старте | перезапусти opencode |
| `SSL: CERTIFICATE_VERIFY_FAILED` | корпоративный сертификат / нет VPN | проверь VPN до `testops.moscow.alfaintra.net` |
| `Insufficient permissions` при пуше | нет прав на репозиторий | запроси write-доступ в `SFA-AGENTS-IT` |
| Публикация ушла не туда | неверный `page1_id` | скилл обязан спросить — ответь явно |

---

## Требования

- Python **3.9+** (в коде используются builtin-generics в аннотациях: `list[dict]`)
- **git 2.25+** (нужен `--filter=blob:none` + `sparse-checkout`) и доступ к `AUTOTEST/sfa`
- opencode с настроенным провайдером модели
- VPN до `testops.moscow.alfaintra.net` и `confluence.moscow.alfaintra.net`
- Токен TestOps (`TESTOPS_TOKEN`); Confluence — через MCP `atlassian` (токен в `opencode.json`)
- Право записи в `AUTOTEST/sfa` — для публикации разборов (`publish_results.py --push`)
- Jenkins MCP — только для фазы перезапуска

## Другая машина: что нужно и что переносится

Скилл — обычные файлы Python + git, никакого локального состояния внутри не держит.
Переносится в `~/.config/opencode/skills/testops-analysis` конфигуратором (пресет `qa-skillmate`).

**Нужно на новой машине (3 вещи):**

1. `python` 3.9+ и `git` 2.25+ в PATH.
2. `TESTOPS_TOKEN` — свой, у каждого оператора свой.
3. **git-креды на `AUTOTEST/sfa`** — те же, которыми человек уже работает с автотестами
   (Credential Manager на Windows, SSH-ключ или `~/.git-credentials`). Проверка одной командой:
   `git ls-remote https://example.invalid` — должен отработать
   без запроса пароля. Скрипты запускают git с `GIT_TERMINAL_PROMPT=0`, т.е. интерактивного
   ввода не будет: нет кредов → сразу понятная ошибка «нет доступа к репозиторию», код 2.

**Не нужно:** персональный токен Bitbucket, копирование `~/.qa-artifacts`, клоны репозитория.

**Создаётся само при первом запуске:** `~/.qa-artifacts/` (кэши эталонов, базы результатов,
partial-клон `_git/`), `~/.qa-artifacts/launches/…` после первого скана.

**Остаётся локальным (не синхронизируется):** `~/.qa-artifacts/autotesters.json` (персональная
Jenkins-джоба), `config.json` (если есть локальные переопределения), история прогонов.
Общее между операторами — только ветки данных: эталоны (`qa-analysis-data`) и база
результатов (`qa-analysis-data`).

**Одновременная публикация двух операторов** по одной команде: скрипт перечитывает
`causes.jsonl`/`index.json` из ветки перед публикацией, а при отказе `non-fast-forward`
повторяет попытку (до 3 раз), так что счётчики причин не теряются. Публикация по разным
командам не конфликтует вообще — пути разные.

**Что требует прав записи:** только `publish_results.py --push`. Чтение (`fetch_etalons`,
`fetch_results`), сканы и разборы работают с правом чтения.

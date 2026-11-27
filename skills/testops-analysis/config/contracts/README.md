# Контракты артефактов (единственный источник истины)

Артефакты прогонов живут **вне репозитория**: `~/.qa-artifacts/`
(переопределение: env `QA_ARTIFACTS_ROOT`, флаг `--root`).
Публикуемая база знаний — в ветке `qa-analysis-data` (`AUTOTEST/sfa`, `results/`).
Схемы ниже — машиночитаемый контракт; проза в `references/` ссылается сюда, а не дублирует.

Проверка: `py -X utf8 scripts/doctor.py` (валидирует найденные артефакты);
документы результата проверяет `publish_results.py` перед записью.

## Кто пишет / кто читает

| Артефакт | Путь | Пишет | Читает | Схема |
|---|---|---|---|---|
| `expected_set.json` | `launches/{id}/{team}/` | `scan_testops.py` | `verify_sets`, `render_markdown`, `diff_rerun`, `classify_rerun`, `dispatch_team_reruns`, `publish_results` | `expected_set.schema.json` |
| `nonpass_results.jsonl` | `launches/{id}/{team}/` | `scan_testops.py` | `render_markdown`, `publish_results` | — (jsonl, поля как у строки скана) |
| `scan_manifest.json` | `launches/{id}/{team}/` | `scan_testops.py` | агент (гейт полноты), `publish_results` | — |
| `at_list.txt` | `launches/{id}/{team}/` | агент из `expected_set` | `verify_sets.py` | — (одно число на строку) |
| `case_meta.json` | `launches/{id}/{team}/` | `run-analysis`, `dispatch`, `rerun-classifier` (каждый — свою секцию) | `rerun-classifier`, `run-analysis` | `case_meta.schema.json` |
| `dispatch.json` | `launches/{id}/` | `dispatch_team_reruns.py` | `render_markdown --dispatch` | `dispatch.schema.json` |
| `rerun_diff.json` | `reruns/{rerun_id}/` | `diff_rerun.py` | `classify_rerun`, `render_markdown`, `publish_results` | — |
| `rerun_classification.json` | `reruns/{rerun_id}/` | `classify_rerun.py` | `render_markdown`, `classify_rerun --chat`, `publish_results` | — |
| `stability.jsonl` | корень артефактов | `scan_testops.py` | агент (метрика стабильности) | — |
| `results/<TEAM>/launches/*.json` | ветка `qa-analysis-data` (+ кэш `~/.qa-artifacts/results/`) | `publish_results.py` | агент, `fetch_results.py` | `run_result.schema.json` |
| `results/<TEAM>/index.json` | там же | `publish_results.py` | агент (первый вход в базу), `fetch_results.py` | `results_index.schema.json` |
| `results/<TEAM>/causes.jsonl` | там же | `publish_results.py` | `publish_results` (накопление), агент | — (jsonl, поля как у `causes[]`) |

## Числа: не путать

| Величина | Где | Смысл |
|---|---|---|
| `counts.nonpass` | `expected_set.json` | уникальных НЕ-PASS **номеров** (= длина @-списка) |
| `nonpass_numbers` | `scan_manifest.json` | то же число, для сверки |
| `nonpass_results` | `scan_manifest.json` | **строк результатов** (параметризованные Examples дают несколько на номер) |
| `new_tests` | `expected_set.json` | тесты команды вне эталона; в @-список не входят автоматически |
| `etalon_overlap` | `scan_manifest.json` | сколько номеров запуска нашлось в эталоне (контроль эталона) |

`nonpass_count` в манифесте — **deprecated** alias `nonpass_results` (оставлен для внешних читателей).

## Правила

1. **Один артефакт — один владелец записи.** Другие режимы только читают.
2. **Командная изоляция:** снимки скана — всегда в подпапке `{team}/`, никогда в `launches/{id}/`.
3. **`case_meta.json` — единственный файл состояния.** Второго не существует; секции `run`/`dispatch`/`rerun` дописываются, а не перезаписываются целиком.
4. **Секреты не хранятся** в артефактах: токены — в env или `~/.qa-artifacts/config.json` (вне репозитория).
5. **Схема — источник истины.** Если проза в `references/` расходится со схемой, права схема, а проза — баг.
6. **База результатов — только через `publish_results.py`.** Файлы в ветке `qa-analysis-data` руками не правятся: скрипт идемпотентен и держит `causes.jsonl`/`index.json` согласованными.
7. **Причины падений — только из фаз rerun/deep.** На первичном скане `tests[].causes` пуст: статус и сообщение — не причина. `suspected` не повышается до `confirmed` без evidence (git/дефект/ручная проверка).

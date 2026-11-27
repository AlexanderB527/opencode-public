# OpenCode TestOps

Исходники двух плагинов и двух скиллов. Адреса с доменом `example.invalid` служат заглушками. Рабочие адреса и учётные данные задаются в локальной конфигурации, которую читают интеграции. Переменные `TESTOPS_TOKEN`, `JENKINS_URL`, `JENKINS_USERNAME` и `JENKINS_PASSWORD` передаются процессу из окружения там, где это поддерживает соответствующий клиент. Для автоматизации используйте хранилище секретов CI.

Карту команд и Jenkins jobs задайте локальным `team_jobs.json`. GUI собирается из `src/gui.tsx`; схема сборки описана в [README плагина](plugins/testops-analyzer/README.md). Внутренние артефакты, эталоны, примеры реальных запусков и сгенерированный GUI-бандл исключены из публикации.

Проверки исходников выполняются через `node --test` для тестов обоих плагинов и `python -B skills/testops-rerun-triage/scripts/test_classify_rerun.py` для Python-триажа.

## Экспериментальный Code Mode

На OpenCode V2 с API плагина `2.3.2` добавьте `"codeMode": true` в опции существующей регистрации `testops-analyzer`. После перезапуска сервер зарегистрирует два инструмента:

- `testops_code_analyze` принимает числовой `launchId` и `runKind`. Для `main` доступны `team`, `tag` и `mode`. Для `rerun` используется весь запуск. `auto` возвращает штатную оценку типа. Если в результате есть `next: "request_rerun_without_filters"`, вызовите анализ с `runKind: "rerun"` для классификации.
- `testops_code_evidence` принимает идентификатор снимка и до пяти номеров тестов. Снимок действует две минуты в той же сессии. Детали теста отражают данные TestOps и степень уверенности классификатора.

Разрешение `testops_code_mode` проверяется для ресурсов `analyze/<launchId>` и `evidence/<launchId>`. Сохраните `qa-artifact-guard` в конфигурации. Серверный процесс должен видеть Python 3 и скилл `testops-rerun-triage`. Для отдельной установки задайте переменные процесса `TESTOPS_CODE_MODE_PYTHON` и `TESTOPS_CODE_MODE_TRIAGE_HOME` абсолютными путями к интерпретатору и каталогу скилла. Python-адаптер читает настройки TestOps из `QA_ARTIFACTS_ROOT/config.json` или стандартного каталога скилла. Для передачи секрета из окружения используется `TESTOPS_TOKEN`.

Оставьте `codeModeNative` выключенным на обычном профиле. Опция добавляет инструменты в каталог штатного `execute` на совместимом хосте. Включайте её только на изолированном профиле после проверки запрета произвольного сетевого доступа и доступных инструментов. Опция `codeModeTelemetry` включает серверные JSON-метрики длительности, запросов, байтов ответа и usage связанного шага OpenCode. Для общих затрат задания собирайте usage всей сессии.

Для возврата к действующему pipeline выключите `codeMode` и перезапустите сервер. При ответе `UNAVAILABLE` доступен соответствующий существующий скилл. Ошибки API, отмена и отказ в доступе завершают вызов.

```sh
node --test plugins/testops-analyzer/test/*.test.mjs plugins/testops-analyzer/test/*.test.ts plugins/qa-artifact-guard/lib/matchers.test.ts
python -B skills/testops-rerun-triage/scripts/test_classify_rerun.py
```

Для теста паритета с Python CLI укажите `TESTOPS_CODE_MODE_PYTHON`, если подходящий Python недоступен через PATH. [Архитектура](docs/code-mode/ARCHITECTURE.md) · [ADR](docs/code-mode/ADR.md) · [План](docs/code-mode/IMPLEMENTATION_PLAN.md) · [Benchmark](docs/code-mode/BENCHMARK.md)

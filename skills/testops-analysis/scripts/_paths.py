"""Единый источник путей для QA-скриптов (skills/testing-skills/testops-analysis).

QA_HOME — папка скилла (там лежат scripts/ и config/).
Приоритет определения:
  1. переменная окружения QA_HOME;
  2. родительская папка каталога, где лежит этот файл.

Артефакты прогонов и эталоны живут ВНЕ репозитория: ~/.qa-artifacts
(у каждого оператора свои). Так `git pull` в ~/.config/opencode не спотыкается
о результаты прогонов и о данные, которые меняются чаще кода.

Эталоны команд — данные, а не поставка: источник истины — ветка
`qa-analysis-data` репозитория AUTOTEST/sfa (`etalons/<TEAM>/etalon_<тип>.txt`),
а в кэше оператора лежит их локальная копия. Скачивает `scripts/fetch_etalons.py`.
"""

import os
from pathlib import Path

# <testops-analysis>/scripts
_SCRIPTS_DIR = Path(__file__).resolve().parent

# <testops-analysis> — корень скилла
QA_HOME = Path(os.environ.get("QA_HOME") or _SCRIPTS_DIR.parent).expanduser().resolve()

# Рабочие артефакты прогонов: env QA_ARTIFACTS_ROOT -> ~/.qa-artifacts
ARTIFACTS_ROOT = Path(
    os.environ.get("QA_ARTIFACTS_ROOT") or (Path.home() / ".qa-artifacts")
).expanduser().resolve()

# Поставка: правила классификации (файл данных внутри скилла)
CONFIG_DIR = QA_HOME / "config"
RULES_PATH = CONFIG_DIR / "classify_rules.json"

# Реестр персональных Jenkins-джоб: вне репозитория, у каждого оператора свой.
# В скилле лежит только образец — config/autotesters.example.json.
AUTOTESTERS_PATH = ARTIFACTS_ROOT / "autotesters.json"

# Реестр КОМАНДНЫХ Jenkins-джоб: версионируется вместе со скиллом (общая
# инфраструктура — у всех одна джоба на команду). В отличие от персонального
# реестра, лежит внутри скилла: config/team_jobs.json.
TEAM_JOBS_PATH = CONFIG_DIR / "team_jobs.json"

# Эталоны команд — ДАННЫЕ в отдельной ветке (AUTOTEST/sfa, etalons/<TEAM>/etalon_<тип>.txt).
# В репозитории скилла НЕ версионируются: fetch_etalons.py скачивает их в кэш оператора.
ETALONS_DIR = ARTIFACTS_ROOT / "etalons"

# Результаты разбора прогонов — локальный стейджинг/кэш. Публикуются в ветку
# qa-analysis-data (AUTOTEST/sfa, results/<TEAM>/) через publish_results.py.
RESULTS_DIR = ARTIFACTS_ROOT / "results"


def artifacts_root(arg=None):
    """Корень артефактов: явный --root > QA_ARTIFACTS_ROOT > ~/.qa-artifacts."""
    if arg:
        return Path(arg).expanduser().resolve()
    return ARTIFACTS_ROOT


def dispatch_path(launch_id, root=None):
    """Путь к итогу раздачи перезапусков: launches/<launch_id>/dispatch.json."""
    return artifacts_root(root) / "launches" / str(launch_id) / "dispatch.json"


def etalon_path(team, test_type):
    """Путь к эталону в кэше: ~/.qa-artifacts/etalons/<TEAM>/etalon_<HF|QG|RM>.txt."""
    return ETALONS_DIR / str(team).upper() / f"etalon_{str(test_type).upper()}.txt"

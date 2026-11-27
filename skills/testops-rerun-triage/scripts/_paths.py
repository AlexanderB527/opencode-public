"""Единый источник путей для скилла testops-rerun-triage.

QA_HOME — корень скилла (там лежат scripts/ и config/). Приоритет:
  1. переменная окружения QA_HOME;
  2. родительская папка каталога, где лежит этот файл.

Артефакты триажа — вне репозитория: ~/.qa-artifacts/triage/{rerun_id}/
(переопределение: env QA_ARTIFACTS_ROOT или флаг --root).
Скилл самодостаточен: не читает и не пишет артефакты других скиллов.
"""

import os
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
QA_HOME = Path(os.environ.get("QA_HOME") or _SCRIPTS_DIR.parent).expanduser().resolve()

ARTIFACTS_ROOT = Path(
    os.environ.get("QA_ARTIFACTS_ROOT") or (Path.home() / ".qa-artifacts")
).expanduser().resolve()

CONFIG_DIR = QA_HOME / "config"
# Свой файл правил (не копия правил первичного разбора: другой набор сигналов)
RULES_PATH = CONFIG_DIR / "triage_rules.json"

# Каталог артефактов триажа (свой, не пересекается с launches/ и reruns/)
TRIAGE_DIR_NAME = "triage"


def artifacts_root(arg=None) -> Path:
    """Корень артефактов: явный --root > QA_ARTIFACTS_ROOT > ~/.qa-artifacts."""
    if arg:
        return Path(arg).expanduser().resolve()
    return ARTIFACTS_ROOT


def triage_dir(rerun_id, root=None) -> Path:
    """Каталог триажа конкретного рерана: <root>/triage/<rerun_id>/."""
    return artifacts_root(root) / TRIAGE_DIR_NAME / str(rerun_id)

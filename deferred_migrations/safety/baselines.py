from importlib import import_module
from pathlib import Path

from django.apps import AppConfig
from django.db.migrations.loader import MigrationLoader

BASELINE_FILENAME = "deferred_migrations_baseline.txt"


def is_first_party(app_config: AppConfig) -> bool:
    parts = Path(app_config.path).parts
    return "site-packages" not in parts and "dist-packages" not in parts


def baseline_path(app_label: str) -> Path | None:
    module_name, _explicit = MigrationLoader.migrations_module(app_label)

    if module_name is None:
        return None

    try:
        module = import_module(module_name)
    except ModuleNotFoundError:
        return None

    if module.__file__ is None:
        return None

    return Path(module.__file__).parent / BASELINE_FILENAME


def read_baseline(app_label: str) -> str | None:
    path = baseline_path(app_label)

    if path is None or not path.exists():
        return None

    return path.read_text().strip() or None

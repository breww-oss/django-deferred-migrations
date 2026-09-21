from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from django.apps import apps
from django.db import DEFAULT_DB_ALIAS
from django.db import connections
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.models import Model

from deferred_migrations.models import ModelRename


def contenttypes_installed() -> bool:
    return apps.is_installed("django.contrib.contenttypes")


# Django's RenameContentType silently keeps the old name when a row under the new name exists, which would split generic relations between two rows.
def check_no_stale_content_type(schema_editor: BaseDatabaseSchemaEditor, app_label: str, old_model: str, new_model: str) -> None:
    if schema_editor.collect_sql or not contenttypes_installed():
        return

    table = apps.get_model("contenttypes", "ContentType")._meta.db_table
    connection = schema_editor.connection

    with connection.cursor() as cursor:
        if table not in connection.introspection.table_names(cursor):
            return

        cursor.execute(f"SELECT model, id FROM {schema_editor.quote_name(table)} WHERE app_label = %s AND model IN (%s, %s)", [app_label, old_model, new_model])
        rows = dict(cursor.fetchall())

    if old_model in rows and new_model in rows:
        raise ValueError(f"ContentType rows exist for both {app_label}.{old_model} (id {rows[old_model]}) and {app_label}.{new_model} (id {rows[new_model]}). Django would keep the old name and split generic relations between them. Decide which row survives, delete the other, then migrate again.")


@dataclass(frozen=True)
class RenameRecord:
    old_model: str
    new_model: str
    order: tuple[datetime, int]


def follow_forwards(records: list[RenameRecord], model: str) -> str | None:
    name, after = model, None

    # Each hop must be newer than the last, so a rename back to an earlier name cannot loop.
    while candidates := [record for record in records if record.old_model == name and (after is None or record.order > after)]:
        newest = max(candidates, key=lambda record: record.order)
        name, after = newest.new_model, newest.order

    return None if name == model else name


def follow_backwards(records: list[RenameRecord], model: str) -> list[str]:
    names: list[str] = []
    name, before = model, None

    while candidates := [record for record in records if record.new_model == name and (before is None or record.order < before)]:
        newest = max(candidates, key=lambda record: record.order)
        name, before = newest.old_model, newest.order
        names.append(name)

    return names


class RenameMap:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, list[RenameRecord]]] = {}
        self.reloaded: dict[str, set[tuple[str, ...]]] = {}

    def clear(self) -> None:
        self.records.clear()
        self.reloaded.clear()

    def load(self, using: str) -> None:
        # A process can start before the package's 0002 is applied, and a failed query would poison the caller's transaction.
        if ModelRename._meta.db_table not in connections[using].introspection.table_names():
            self.records[using] = {}
            return

        by_app: dict[str, list[RenameRecord]] = {}

        for record in ModelRename.objects.using(using).order_by("created_at", "pk"):
            by_app.setdefault(record.app_label, []).append(RenameRecord(record.old_model, record.new_model, (record.created_at, record.pk)))

        self.records[using] = by_app

    def reload_once(self, using: str, key: tuple[str, ...]) -> bool:
        reloaded = self.reloaded.setdefault(using, set())

        if key in reloaded:
            return False

        reloaded.add(key)
        self.load(using)
        return True

    # A process that loaded the map before a rename migration ran would otherwise miss the new record, so each unresolved key reloads once. The lookup that triggers a load (the first for a database, or the first after clear()) returns its miss without spending its key's reload.
    def lookup[T](self, using: str, key: tuple[str, ...], find: Callable[[dict[str, list[RenameRecord]]], T]) -> T:
        loaded_now = using not in self.records

        if loaded_now:
            self.load(using)

        if (result := find(self.records[using])) or loaded_now or not self.reload_once(using, key):
            return result

        return find(self.records[using])

    # A name cached before a second rename in the same deploy no longer has a row, so each such stale name gets one reload of its own.
    def current_row[T](self, using: str, app_label: str, model: str, fetch: Callable[[str], T | None]) -> T | None:
        key = ("forwards", app_label, model)

        def find(records: dict[str, list[RenameRecord]]) -> str | None:
            return follow_forwards(records.get(app_label, []), model)

        if (name := self.lookup(using, key, find)) is None:
            return None

        if (row := fetch(name)) is not None or not self.reload_once(using, (*key, name)):
            return row

        return None if (name := find(self.records[using])) is None else fetch(name)

    # model_class() backs generic relations, so a stale map here breaks them in old pods for the life of the process.
    def former_names(self, using: str, app_label: str, model: str) -> list[str]:
        return self.lookup(using, ("backwards", app_label, model), lambda records: follow_backwards(records.get(app_label, []), model))


RENAMES = RenameMap()


def install_content_type_compatibility() -> None:
    if not contenttypes_installed():
        return

    content_type_class = apps.get_model("contenttypes", "ContentType")
    manager_class = type(content_type_class.objects)

    if manager_class.__dict__.get("deferred_migrations_patched"):
        return

    original_get_or_create = manager_class.get_or_create
    original_create = manager_class.create
    original_get_by_natural_key = manager_class.get_by_natural_key
    original_model_class = content_type_class.model_class

    # Historical models in migrations are separate classes built with this same manager, and on a fresh database the rename table does not exist yet, so only the live class may be served renamed rows.
    def is_live(manager: object) -> bool:
        return manager.model is content_type_class

    def renamed_row(manager: object, app_label: str, model: str) -> Model | None:
        def fetch(current: str) -> Model | None:
            try:
                return original_get_by_natural_key(manager, app_label, current)
            except content_type_class.DoesNotExist:
                return None

        if (row := RENAMES.current_row(manager.db, app_label, model, fetch)) is None:
            return None

        manager._cache.setdefault(manager.db, {})[(app_label, model)] = row
        return row

    def get_or_create(self: object, defaults: dict[str, object] | None = None, **kwargs: object) -> tuple[Model, bool]:
        if is_live(self) and set(kwargs) == {"app_label", "model"} and not self.filter(**kwargs).exists() and (row := renamed_row(self, kwargs["app_label"], kwargs["model"])) is not None:
            return row, False

        return original_get_or_create(self, defaults=defaults, **kwargs)

    def create(self: object, **kwargs: object) -> Model:
        if is_live(self) and set(kwargs) == {"app_label", "model"} and not self.filter(**kwargs).exists() and (row := renamed_row(self, kwargs["app_label"], kwargs["model"])) is not None:
            return row

        return original_create(self, **kwargs)

    def get_by_natural_key(self: object, app_label: str, model: str) -> Model:
        try:
            return original_get_by_natural_key(self, app_label, model)
        except self.model.DoesNotExist:
            if not is_live(self) or (row := renamed_row(self, app_label, model)) is None:
                raise

            return row

    def model_class(self: Model) -> type[Model] | None:
        if (found := original_model_class(self)) is not None or type(self) is not content_type_class:
            return found

        for former in RENAMES.former_names(self._state.db or DEFAULT_DB_ALIAS, self.app_label, self.model):
            try:
                return apps.get_model(self.app_label, former)
            except LookupError:
                continue

        return None

    manager_class.get_or_create = get_or_create
    manager_class.create = create
    manager_class.get_by_natural_key = get_by_natural_key
    manager_class.deferred_migrations_patched = True
    content_type_class.model_class = model_class

import re

from django.db import models
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.migrations.state import ProjectState
from django.db.models import CompositePrimaryKey
from django.db.models import F
from django.db.models import GeneratedField
from django.db.models.fields import NOT_PROVIDED

from deferred_migrations.expressions import expression_field_names

# The sync trigger and backfill compare with IS DISTINCT FROM, which these types have no operator for.
NO_EQUALITY_TYPES = frozenset({"json", "xml", "point", "line", "lseg", "box", "path", "polygon", "circle"})
# An array compares element by element, so json[] has no equality operator either.
ARRAY_SUFFIX = re.compile(r"(\s*\[\d*\])+$")


def related_model_key(reference: object, default_app: str, own_key: tuple[str, str]) -> tuple[str, str]:
    if isinstance(reference, str):
        if reference == "self":
            return own_key

        app_label, _, model_name = reference.rpartition(".")
        return (app_label or default_app, model_name.lower())

    return (reference._meta.app_label, reference._meta.model_name)


def auto_many_to_many_involving(state: ProjectState, app_label: str, model_name: str) -> list[str]:
    target = (app_label, model_name.lower())
    involved: list[str] = []

    for owner_key, model_state in state.models.items():
        for field_name, field in model_state.fields.items():
            if not isinstance(field, models.ManyToManyField) or field.remote_field.through is not None:
                continue

            if owner_key == target or related_model_key(field.remote_field.model, owner_key[0], owner_key) == target:
                involved.append(f"{owner_key[0]}.{owner_key[1]}.{field_name}")

    return involved


def model_rename_problem(state: ProjectState, app_label: str, model_name: str) -> str | None:
    model_state = state.models.get((app_label, model_name.lower()))

    if model_state is None:
        return f"{app_label}.{model_name} is not in the migration state."

    options = model_state.options

    if options.get("db_table"):
        return f"{app_label}.{model_name} sets Meta.db_table, so the table name does not change; use a plain RenameModel."

    if not options.get("managed", True):
        return f"{app_label}.{model_name} is unmanaged."

    if options.get("proxy"):
        return f"{app_label}.{model_name} is a proxy model with no table of its own."

    if involved := auto_many_to_many_involving(state, app_label, model_name):
        return f"{app_label}.{model_name} is part of auto-created many-to-many tables ({', '.join(involved)}), whose table and column names derive from the model name. Convert them to explicit through models first."

    return None


# Index, UniqueConstraint, CheckConstraint and ExclusionConstraint name their columns in different attributes, so each is read if present.
def option_field_names(item: object) -> set[str]:
    names = {name.lstrip("-") for name in getattr(item, "fields", ()) or ()}
    names |= set(getattr(item, "include", ()) or ())

    for entry in getattr(item, "expressions", ()) or ():
        expression = entry[0] if isinstance(entry, tuple) else entry
        names |= expression_field_names(F(expression) if isinstance(expression, str) else expression)

    if (condition := getattr(item, "condition", None)) is not None:
        names |= expression_field_names(condition)

    return names


def field_rename_problem(state: ProjectState, app_label: str, model_name: str, field_name: str, connection: BaseDatabaseWrapper) -> str | None:
    model_state = state.models[(app_label, model_name.lower())]
    field = model_state.fields[field_name]
    options = model_state.options
    label = f"{app_label}.{model_name}.{field_name}"

    if not options.get("managed", True) or options.get("proxy"):
        return f"{label} is on an unmanaged or proxy model."

    if any(isinstance(other, CompositePrimaryKey) for other in model_state.fields.values()):
        return f"{label} is on a model with a composite primary key."

    if field.is_relation:
        return f"{label} is a relation (foreign key or many-to-many)."

    if field.primary_key or isinstance(field, GeneratedField):
        return f"{label} is a primary key or generated field."

    if field.db_index or field.unique:
        return f"{label} has an index or unique constraint."

    if field.db_default is not NOT_PROVIDED:
        return f"{label} has a db_default."

    if any(isinstance(other, GeneratedField) and field_name in expression_field_names(other.expression) for other in model_state.fields.values()):
        return f"{label} is used by a generated field."

    options_items = [*options.get("indexes", []), *options.get("constraints", [])]
    together = [*options.get("unique_together", ()), *options.get("index_together", ())]

    if any(field_name in option_field_names(item) for item in options_items) or any(field_name in group for group in together):
        return f"{label} is referenced by Meta indexes, constraints or together options."

    if ARRAY_SUFFIX.sub("", (field.db_type(connection) or "").split("(")[0].strip().lower()) in NO_EQUALITY_TYPES:
        return f"{label} has a column type with no equality operator, so IS DISTINCT FROM cannot compare it."

    return None

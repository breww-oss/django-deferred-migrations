import copy
from dataclasses import dataclass
from typing import Literal

from django.conf import settings
from django.db import models
from django.db.backends.utils import truncate_name
from django.db.migrations.state import ModelState
from django.db.migrations.state import ProjectState
from django.db.models.fields import Field

ModelKey = tuple[str, str]


def has_own_table(model_state: ModelState) -> bool:
    if model_state.options.get("proxy") or model_state.options.get("managed") is False:
        return False

    if swappable := model_state.options.get("swappable"):
        return getattr(settings, swappable, None) in (None, f"{model_state.app_label}.{model_state.name}")

    return True


def table_name(model_state: ModelState, max_name_length: int) -> str:
    return model_state.options.get("db_table") or truncate_name(f"{model_state.app_label}_{model_state.name_lower}", max_name_length)


def column_name(field_name: str, field: Field) -> str | None:
    if isinstance(field, models.ManyToManyField):
        return None

    # copy.copy, not clone(): clone() re-runs __init__ for every field of every model on each walk.
    bound = copy.copy(field)
    bound.name = field_name
    bound.set_attributes_from_name(field_name)
    return bound.column


def resolve_model_key(reference: object, app_label: str, model_name_lower: str) -> ModelKey:
    if not isinstance(reference, str):
        return (reference._meta.app_label, reference._meta.model_name)

    if reference == "self":
        return (app_label, model_name_lower)

    if "." in reference:
        reference_app, reference_model = reference.split(".", 1)
        return (reference_app, reference_model.lower())

    return (app_label, reference.lower())


@dataclass(frozen=True)
class PhysicalNameKey:
    kind: Literal["table", "column", "m2m_table", "m2m_column"]
    app_label: str
    model_name: str
    field_name: str | None = None
    # Which end of an auto-created many-to-many table an m2m_column names.
    side: Literal["from", "to"] | None = None


def physical_names(state: ProjectState, max_name_length: int) -> dict[PhysicalNameKey, str]:
    names: dict[PhysicalNameKey, str] = {}

    for (app_label, model_name), model_state in state.models.items():
        if not has_own_table(model_state):
            continue

        table = table_name(model_state, max_name_length)
        names[PhysicalNameKey("table", app_label, model_name)] = table

        for field_name, field in model_state.fields.items():
            if isinstance(field, models.ManyToManyField):
                through = field.remote_field.through

                if through:
                    continue

                names[PhysicalNameKey("m2m_table", app_label, model_name, field_name)] = field.db_table or truncate_name(f"{table}_{field_name}", max_name_length)
                to_key = resolve_model_key(field.remote_field.model, app_label, model_name)

                # Django's create_many_to_many_intermediary_model compares the model names alone, so a same-named model in another app still gets the from_/to_ prefixes.
                if to_key[1] == model_name:
                    names[PhysicalNameKey("m2m_column", app_label, model_name, field_name, "from")] = f"from_{model_name}_id"
                    names[PhysicalNameKey("m2m_column", app_label, model_name, field_name, "to")] = f"to_{model_name}_id"
                else:
                    names[PhysicalNameKey("m2m_column", app_label, model_name, field_name, "from")] = f"{model_name}_id"
                    names[PhysicalNameKey("m2m_column", app_label, model_name, field_name, "to")] = f"{to_key[1]}_id"

                continue

            if (column := column_name(field_name, field)) is not None:
                names[PhysicalNameKey("column", app_label, model_name, field_name)] = column

    return names

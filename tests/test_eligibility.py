import pytest
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.fields import RangeOperators
from django.db import connection
from django.db import migrations
from django.db import models
from django.db.migrations.state import ProjectState
from django.db.models import F
from django.db.models import Q
from django.db.models.functions import Lower

from deferred_migrations.eligibility import field_rename_problem
from deferred_migrations.eligibility import option_field_names


class PlainJSONField(models.Field):
    def db_type(self, connection: object) -> str:
        return "json"


class PlainPointField(models.Field):
    def db_type(self, connection: object) -> str:
        return "point"


def state_with(fields: list[tuple[str, models.Field]], options: dict[str, object] | None = None) -> ProjectState:
    state = ProjectState()
    migrations.CreateModel("Widget", [("id", models.BigAutoField(primary_key=True)), *fields], options=options or {}).state_forwards("shop", state)
    return state


def test_a_plain_column_is_eligible() -> None:
    assert field_rename_problem(state_with([("flag", models.BooleanField(default=False))]), "shop", "widget", "flag", connection) is None


@pytest.mark.parametrize(
    ("fields", "options", "reason"),
    [
        ([("flag", models.BooleanField(db_index=True))], None, "index or unique"),
        ([("flag", models.CharField(max_length=5, unique=True))], None, "index or unique"),
        ([("flag", models.BooleanField(db_default=False))], None, "db_default"),
        ([("flag", models.ForeignKey("shop.Widget", on_delete=models.CASCADE))], None, "relation"),
        ([("flag", models.IntegerField()), ("double", models.GeneratedField(expression=F("flag") * 2, output_field=models.IntegerField(), db_persist=True))], None, "generated field"),
        ([("flag", models.CharField(max_length=5))], {"indexes": [models.Index(Lower("flag"), name="shop_widget_lower_flag")]}, "Meta"),
        ([("flag", models.IntegerField())], {"constraints": [models.CheckConstraint(condition=Q(flag__gte=0), name="shop_widget_flag_positive")]}, "Meta"),
        ([("flag", models.IntegerField()), ("other", models.IntegerField())], {"unique_together": {("flag", "other")}}, "Meta"),
        ([("flag", PlainJSONField(null=True))], None, "IS DISTINCT FROM"),
        ([("flag", models.IntegerField())], {"managed": False}, "unmanaged"),
    ],
)
def test_ineligible_renames_say_why(fields: list[tuple[str, models.Field]], options: dict[str, object] | None, reason: str) -> None:
    assert reason in field_rename_problem(state_with(fields, options), "shop", "widget", "flag", connection)


@pytest.mark.parametrize(
    ("item", "names"),
    [
        (models.Index(fields=["-flag", "other"], name="shop_widget_flag_other"), {"flag", "other"}),
        (models.Index(Lower("flag"), name="shop_widget_lower_flag"), {"flag"}),
        (models.UniqueConstraint(fields=["flag"], include=["other"], condition=Q(active=True), name="shop_widget_flag_unique"), {"flag", "other", "active"}),
        (ExclusionConstraint(name="shop_widget_period_excl", expressions=[("period", RangeOperators.OVERLAPS)]), {"period"}),
        (ExclusionConstraint(name="shop_widget_lower_excl", expressions=[(Lower("flag"), RangeOperators.EQUAL)]), {"flag"}),
    ],
)
def test_option_field_names_reads_every_column_an_index_or_constraint_names(item: object, names: set[str]) -> None:
    assert option_field_names(item) == names


@pytest.mark.parametrize("field", [ArrayField(PlainJSONField(), null=True), ArrayField(PlainPointField(), null=True), ArrayField(ArrayField(PlainJSONField()), null=True), ArrayField(PlainJSONField(), size=3, null=True)])
def test_an_array_of_a_type_with_no_equality_operator_is_ineligible(field: models.Field) -> None:
    assert "IS DISTINCT FROM" in field_rename_problem(state_with([("flag", field)]), "shop", "widget", "flag", connection)

import pytest
from django.db import connection
from django.db import migrations
from django.db import models
from django.db.migrations.operations.base import Operation
from django.db.migrations.state import ProjectState
from django.db.models import Deferrable
from django.db.models import Q

from deferred_migrations.operations import AddConstraintConcurrently
from deferred_migrations.operations import AddFieldConcurrently
from deferred_migrations.operations import AddIndexConcurrently
from tests.migration_helpers import apply_operations
from tests.schema_snapshot import schema_snapshot


# The drop runs even when an apply fails, so one broken case cannot leave tables behind that fail every later case.
def built_by(operations: list[Operation], atomic: bool) -> dict[str, object]:
    try:
        state = apply_operations(
            "dm_par",
            ProjectState(),
            [
                migrations.CreateModel("Parent", [("id", models.BigAutoField(primary_key=True))]),
                migrations.CreateModel("Child", [("id", models.BigAutoField(primary_key=True)), ("batch", models.IntegerField(default=0)), ("vessel", models.IntegerField(null=True)), ("kind", models.IntegerField(default=0))]),
            ],
        )

        for number, operation in enumerate(operations, start=2):
            state = apply_operations("dm_par", state, [operation], atomic=atomic, name=f"{number:04d}")

        return schema_snapshot(["dm_par_child", "dm_par_parent", "dm_par_quoted"])
    finally:
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS dm_par_child, dm_par_parent, dm_par_quoted CASCADE")


def fk(**options: object) -> models.ForeignKey:
    return models.ForeignKey("dm_par.Parent", models.SET_NULL, null=True, **options)


def unique_code(**options: object) -> models.CharField:
    return models.CharField(max_length=10, null=True, unique=True, **options)


def one_to_one() -> models.OneToOneField:
    return models.OneToOneField("dm_par.Parent", models.SET_NULL, null=True)


LONG_COLUMN = "a_column_name_long_enough_to_push_the_constraint_past_63_bytes"
MULTIBYTE_COLUMN = "x" + "ü" * 30
QUOTED_TABLE = migrations.CreateModel("Quoted", [("id", models.BigAutoField(primary_key=True))], options={"db_table": '"dm_par_quoted"'})
TAKE_KEY_NAME = migrations.RunSQL("CREATE INDEX dm_par_child_code_key ON dm_par_parent (id)", migrations.RunSQL.noop)


# Each case is (Django's plain operations, run atomically) against (the package's operation, run non-atomically).
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("plain", "concurrent"),
    [
        pytest.param([migrations.AddField("child", "parent", fk())], [AddFieldConcurrently("child", "parent", fk())], id="foreign key"),
        pytest.param([migrations.AddField("child", "parent", fk(db_index=False))], [AddFieldConcurrently("child", "parent", fk(db_index=False))], id="foreign key without index"),
        pytest.param([migrations.AddField("child", "code", models.CharField(max_length=10, null=True, db_index=True))], [AddFieldConcurrently("child", "code", models.CharField(max_length=10, null=True, db_index=True))], id="indexed char field"),
        pytest.param([migrations.AddField("child", "code", unique_code())], [AddFieldConcurrently("child", "code", unique_code())], id="unique char field"),
        pytest.param([migrations.AddField("child", "parent", one_to_one())], [AddFieldConcurrently("child", "parent", one_to_one())], id="one to one"),
        # PostgreSQL trims the longer of table and column name a byte at a time to fit 63 bytes, so this one loses the end of its column name.
        pytest.param([migrations.AddField("child", "code", unique_code(db_column=LONG_COLUMN))], [AddFieldConcurrently("child", "code", unique_code(db_column=LONG_COLUMN))], id="unique column with a long name"),
        # 61 bytes, trimmed to 46, which falls in the middle of a two-byte character that must be clipped whole.
        pytest.param([migrations.AddField("child", "code", unique_code(db_column=MULTIBYTE_COLUMN))], [AddFieldConcurrently("child", "code", unique_code(db_column=MULTIBYTE_COLUMN))], id="unique column with a multibyte name"),
        # An unrelated index already holds dm_par_child_code_key, so PostgreSQL falls back to dm_par_child_code_key1.
        pytest.param([TAKE_KEY_NAME, migrations.AddField("child", "code", unique_code())], [TAKE_KEY_NAME, AddFieldConcurrently("child", "code", unique_code())], id="unique column whose name is taken"),
        # Django leaves a quoted db_table as written, and PostgreSQL names the constraint from the unquoted relation name.
        pytest.param([QUOTED_TABLE, migrations.AddField("quoted", "code", unique_code())], [QUOTED_TABLE, AddFieldConcurrently("quoted", "code", unique_code())], id="unique column on a quoted db_table"),
        pytest.param([migrations.AddConstraint("child", models.UniqueConstraint(fields=["batch", "kind"], name="dm_par_u"))], [AddConstraintConcurrently("child", models.UniqueConstraint(fields=["batch", "kind"], name="dm_par_u"))], id="unique constraint"),
        pytest.param(
            [migrations.AddConstraint("child", models.UniqueConstraint(fields=["batch", "vessel", "kind"], name="dm_par_nnd", nulls_distinct=False))],
            [AddConstraintConcurrently("child", models.UniqueConstraint(fields=["batch", "vessel", "kind"], name="dm_par_nnd", nulls_distinct=False))],
            id="nulls not distinct unique constraint",
        ),
        pytest.param(
            [migrations.AddConstraint("child", models.UniqueConstraint(fields=["batch"], name="dm_par_def", deferrable=Deferrable.DEFERRED))],
            [AddConstraintConcurrently("child", models.UniqueConstraint(fields=["batch"], name="dm_par_def", deferrable=Deferrable.DEFERRED))],
            id="deferrable unique constraint",
        ),
        pytest.param(
            [migrations.AddConstraint("child", models.UniqueConstraint(fields=["batch"], condition=Q(kind=1), name="dm_par_partial"))], [AddConstraintConcurrently("child", models.UniqueConstraint(fields=["batch"], condition=Q(kind=1), name="dm_par_partial"))], id="partial unique constraint"
        ),
        pytest.param([migrations.AddConstraint("child", models.CheckConstraint(condition=Q(kind__gte=0), name="dm_par_c"))], [AddConstraintConcurrently("child", models.CheckConstraint(condition=Q(kind__gte=0), name="dm_par_c"))], id="check constraint"),
        pytest.param([migrations.AddIndex("child", models.Index(fields=["batch"], name="dm_par_i"))], [AddIndexConcurrently("child", models.Index(fields=["batch"], name="dm_par_i"))], id="index"),
    ],
)
def test_the_concurrent_operation_builds_exactly_what_django_builds(plain: list[Operation], concurrent: list[Operation]) -> None:
    if connection.pg_version < 150000 and any(isinstance(operation, migrations.AddConstraint) and getattr(operation.constraint, "nulls_distinct", None) is False for operation in plain):
        pytest.skip("nulls_distinct needs PostgreSQL 15")

    base_only = built_by([], atomic=True)
    django_built = built_by(plain, atomic=True)

    assert django_built != base_only, "the case built nothing, so it proves nothing"
    assert built_by(concurrent, atomic=False) == django_built

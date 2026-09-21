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

        return schema_snapshot(["dm_par_child", "dm_par_parent"])
    finally:
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS dm_par_child, dm_par_parent CASCADE")


def fk(**options: object) -> models.ForeignKey:
    return models.ForeignKey("dm_par.Parent", models.SET_NULL, null=True, **options)


# Each case is (Django's plain operations, run atomically) against (the package's operation, run non-atomically).
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("plain", "concurrent"),
    [
        pytest.param([migrations.AddField("child", "parent", fk())], [AddFieldConcurrently("child", "parent", fk())], id="foreign key"),
        pytest.param([migrations.AddField("child", "parent", fk(db_index=False))], [AddFieldConcurrently("child", "parent", fk(db_index=False))], id="foreign key without index"),
        pytest.param([migrations.AddField("child", "code", models.CharField(max_length=10, null=True, db_index=True))], [AddFieldConcurrently("child", "code", models.CharField(max_length=10, null=True, db_index=True))], id="indexed char field"),
        pytest.param(
            [migrations.AddField("child", "code", models.CharField(max_length=10, null=True)), migrations.AlterField("child", "code", models.CharField(max_length=10, null=True, unique=True))],
            [AddFieldConcurrently("child", "code", models.CharField(max_length=10, null=True, unique=True))],
            id="unique char field",
        ),
        pytest.param(
            [migrations.AddField("child", "parent", fk()), migrations.AlterField("child", "parent", models.OneToOneField("dm_par.Parent", models.SET_NULL, null=True))], [AddFieldConcurrently("child", "parent", models.OneToOneField("dm_par.Parent", models.SET_NULL, null=True))], id="one to one"
        ),
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

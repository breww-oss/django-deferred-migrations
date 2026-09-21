from collections.abc import Callable
from collections.abc import Iterator
from datetime import timedelta
from io import StringIO

import pytest
from django.apps import apps
from django.apps.registry import Apps
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.db import connection
from django.db import models
from django.db.migrations.state import ProjectState
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from deferred_migrations.contenttypes import RENAMES
from deferred_migrations.models import ModelRename
from tests.migration_helpers import define_test_model


@pytest.fixture(autouse=True)
def cold_caches() -> Iterator[None]:
    RENAMES.clear()
    ContentType.objects.clear_cache()
    yield
    RENAMES.clear()
    ContentType.objects.clear_cache()


def old_code_model(app_label: str, name: str) -> type[models.Model]:
    registry = Apps()
    meta = type("Meta", (), {"app_label": app_label, "apps": registry})
    return type(name, (models.Model,), {"__module__": __name__, "Meta": meta})


@pytest.mark.django_db
def test_an_old_name_lookup_returns_the_renamed_row_instead_of_creating_one(django_assert_num_queries: Callable) -> None:
    renamed = ContentType.objects.create(app_label="dm_shop", model="gizmo")
    ModelRename.objects.create(app_label="dm_shop", old_model="gadget", new_model="gizmo")
    gadget = old_code_model("dm_shop", "Gadget")
    count = ContentType.objects.count()

    assert ContentType.objects.get_for_model(gadget) == renamed
    assert ContentType.objects.count() == count

    with django_assert_num_queries(0):
        assert ContentType.objects.get_for_model(gadget) == renamed


@pytest.mark.django_db
def test_get_for_models_and_get_by_natural_key_resolve_the_old_name() -> None:
    renamed = ContentType.objects.create(app_label="dm_shop", model="gizmo")
    ModelRename.objects.create(app_label="dm_shop", old_model="gadget", new_model="gizmo")
    gadget = old_code_model("dm_shop", "Gadget")
    count = ContentType.objects.count()

    assert ContentType.objects.get_for_models(gadget) == {gadget: renamed}
    ContentType.objects.clear_cache()
    assert ContentType.objects.get_by_natural_key("dm_shop", "gadget") == renamed
    assert ContentType.objects.count() == count


@pytest.mark.django_db
def test_a_row_under_the_requested_name_always_wins() -> None:
    exact = ContentType.objects.create(app_label="dm_shop", model="gadget")
    ContentType.objects.create(app_label="dm_shop", model="gizmo")
    ModelRename.objects.create(app_label="dm_shop", old_model="gadget", new_model="gizmo")

    assert ContentType.objects.get_for_model(old_code_model("dm_shop", "Gadget")) == exact


@pytest.mark.django_db
@pytest.mark.usefixtures("live_test_app")
def test_model_class_resolves_a_renamed_row_to_the_old_model() -> None:
    gadget = define_test_model("Gadget", {})
    renamed = ContentType.objects.create(app_label="deferred_migrations_testapp", model="gizmo")
    ModelRename.objects.create(app_label="deferred_migrations_testapp", old_model="gadget", new_model="gizmo")

    assert renamed.model_class() is gadget

    ContentType.objects.clear_cache()
    assert ContentType.objects.get_for_id(renamed.pk).model_class() is gadget


@pytest.mark.django_db
def test_a_rename_chain_resolves_to_the_end_and_a_rename_back_does_not_loop() -> None:
    now = timezone.now()
    final = ContentType.objects.create(app_label="dm_shop", model="widget")
    ModelRename.objects.create(app_label="dm_shop", old_model="gadget", new_model="gizmo", created_at=now - timedelta(days=2))
    ModelRename.objects.create(app_label="dm_shop", old_model="gizmo", new_model="widget", created_at=now - timedelta(days=1))
    ModelRename.objects.create(app_label="dm_shop", old_model="widget", new_model="gizmo", created_at=now - timedelta(days=3))

    assert ContentType.objects.get_for_model(old_code_model("dm_shop", "Gadget")) == final


@pytest.mark.django_db
def test_a_map_loaded_before_the_rename_reloads_on_a_miss() -> None:
    RENAMES.load("default")

    renamed = ContentType.objects.create(app_label="dm_shop", model="gizmo")
    ModelRename.objects.create(app_label="dm_shop", old_model="gadget", new_model="gizmo")

    assert ContentType.objects.get_for_model(old_code_model("dm_shop", "Gadget")) == renamed


# Two renames in one deploy: a process that cached the first hop must not create a stray row once the second one runs.
@pytest.mark.django_db
def test_a_cached_rename_whose_row_was_renamed_again_reloads_instead_of_creating_one() -> None:
    renamed = ContentType.objects.create(app_label="dm_shop", model="gizmo")
    ModelRename.objects.create(app_label="dm_shop", old_model="gadget", new_model="gizmo", created_at=timezone.now() - timedelta(minutes=1))
    gadget = old_code_model("dm_shop", "Gadget")

    assert ContentType.objects.get_for_model(gadget) == renamed

    ContentType.objects.filter(pk=renamed.pk).update(model="widget")
    ModelRename.objects.create(app_label="dm_shop", old_model="gizmo", new_model="widget")
    ContentType.objects.clear_cache()
    count = ContentType.objects.count()

    assert ContentType.objects.get_for_model(gadget).pk == renamed.pk
    assert ContentType.objects.count() == count


@pytest.mark.django_db
def test_an_unresolved_name_reloads_the_records_only_once() -> None:
    RENAMES.load("default")

    def rename_queries_for_a_miss() -> int:
        with CaptureQueriesContext(connection) as queries, pytest.raises(ContentType.DoesNotExist):
            ContentType.objects.get_by_natural_key("dm_shop", "gadget")

        return sum(ModelRename._meta.db_table in query["sql"] for query in queries.captured_queries)

    assert rename_queries_for_a_miss() == 1
    assert rename_queries_for_a_miss() == 0


# Historical managers inside migrations must never reach the rename table.
@pytest.mark.django_db
def test_historical_content_type_managers_are_left_alone() -> None:
    ContentType.objects.create(app_label="dm_shop", model="gizmo")
    ModelRename.objects.create(app_label="dm_shop", old_model="gadget", new_model="gizmo")
    historical = ProjectState.from_apps(apps).apps.get_model("contenttypes", "ContentType")

    with CaptureQueriesContext(connection) as queries, pytest.raises(historical.DoesNotExist):
        historical.objects.get_by_natural_key("dm_shop", "gadget")

    assert not any(ModelRename._meta.db_table in query["sql"] for query in queries.captured_queries)


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("plain_rename_test_app")
def test_a_fresh_migrate_over_a_historical_rename_model_never_queries_rename_records() -> None:
    with CaptureQueriesContext(connection) as queries:
        call_command("migrate", "deferred_migrations_testapp", verbosity=0)

    assert not any(ModelRename._meta.db_table in query["sql"] for query in queries.captured_queries)


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("rename_test_app")
def test_after_pre_deploy_old_code_finds_the_renamed_row_and_nothing_is_duplicated() -> None:
    call_command("migrate", "deferred_migrations_testapp", "0001", verbosity=0)
    original = ContentType.objects.create(app_label="deferred_migrations_testapp", model="gadget")

    call_command("migrate_pre_deploy", stdout=StringIO())
    RENAMES.clear()
    ContentType.objects.clear_cache()
    count = ContentType.objects.count()

    original.refresh_from_db()
    assert original.model == "gizmo"
    assert ContentType.objects.get_for_model(old_code_model("deferred_migrations_testapp", "Gadget")) == original
    assert ContentType.objects.count() == count

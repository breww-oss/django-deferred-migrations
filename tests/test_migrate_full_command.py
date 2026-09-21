import sys
from io import StringIO
from unittest import mock

import pytest
from django.core.management import CommandError
from django.core.management import call_command

from deferred_migrations.models import DeferredOperation
from tests.migration_helpers import column_names


class InteractiveStdin(StringIO):
    def isatty(self) -> bool:
        return True


# The package test app's 0002 queues a column drop, so this drives both real phases and the prompt between them.
@pytest.mark.parametrize(("prompt_before_post", "answer", "dropped"), [(False, None, True), (True, "n", False), (True, "y", True)])
@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("test_app")
@mock.patch.object(sys, "stdin", new=InteractiveStdin())
def test_migrate_full_runs_both_phases_and_can_stop_between_them(prompt_before_post: bool, answer: str | None, dropped: bool) -> None:
    stdout = StringIO()

    with mock.patch("builtins.input", return_value=answer) as prompt:
        call_command("migrate_full", prompt_before_post=prompt_before_post, stdout=stdout)

    status = DeferredOperation.objects.get(app_label="deferred_migrations_testapp").status

    assert prompt.call_count == int(prompt_before_post)
    assert ("Would run row" in stdout.getvalue()) == prompt_before_post
    assert ("legacy" not in column_names("deferred_migrations_testapp_child"), status) == (dropped, DeferredOperation.Status.DONE if dropped else DeferredOperation.Status.PENDING)


# A skipped trigger drop on the table blocks the column drop pre-deploy queues, so the queue is not empty yet nothing can run.
@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("test_app")
@mock.patch.object(sys, "stdin", new=InteractiveStdin())
def test_the_prompt_explains_queued_rows_that_cannot_run() -> None:
    DeferredOperation.objects.create(
        app_label="deferred_migrations_testapp", migration_name="0001_initial", operation_index=99, sequence=0, kind=DeferredOperation.Kind.DROP_TRIGGER, table_name="deferred_migrations_testapp_child", column_name="legacy", sql="SELECT 1", status=DeferredOperation.Status.SKIPPED
    )
    stdout = StringIO()

    with mock.patch("builtins.input") as prompt:
        call_command("migrate_full", "--prompt-before-post", stdout=stdout)

    prompt.assert_not_called()
    assert "Blocked row" in stdout.getvalue()
    assert "No post-deploy operations can run now." in stdout.getvalue()
    assert "are queued" not in stdout.getvalue()
    assert "legacy" in column_names("deferred_migrations_testapp_child")


@mock.patch.object(sys, "stdin", new=StringIO())
def test_the_prompt_without_a_terminal_raises() -> None:
    with pytest.raises(CommandError, match="interactive"):
        call_command("migrate_full", "--prompt-before-post", stdout=StringIO())

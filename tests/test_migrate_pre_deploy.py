from io import StringIO

from django.core.management.base import OutputWrapper

from deferred_migrations.management.commands.migrate_pre_deploy import Command as MigratePreDeployCommand


# A caller that passes its own OutputWrapper has it wrapped again by BaseCommand; rich must get the real stream or every redraw gains a newline.
def test_pre_deploy_unwraps_a_stdout_its_caller_already_wrapped() -> None:
    stream = StringIO()

    assert MigratePreDeployCommand(stdout=OutputWrapper(stream)).raw_stdout() is stream

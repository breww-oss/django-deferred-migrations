from django.db import migrations

from deferred_migrations.operations import DeferredRemoveField


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("deferred_migrations", "0001_initial"),
        ("deferred_migrations_testapp", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL("INSERT INTO deferred_migrations_testapp_migrationrun DEFAULT VALUES", migrations.RunSQL.noop),
        DeferredRemoveField(model_name="child", name="legacy"),
    ]

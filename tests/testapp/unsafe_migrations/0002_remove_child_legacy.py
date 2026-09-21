from django.db import migrations

from deferred_migrations.operations import DeferredRemoveField


class Migration(migrations.Migration):
    dependencies = [
        ("deferred_migrations", "0001_initial"),
        ("deferred_migrations_testapp", "0001_initial"),
    ]

    operations = [
        DeferredRemoveField(model_name="child", name="legacy"),
    ]

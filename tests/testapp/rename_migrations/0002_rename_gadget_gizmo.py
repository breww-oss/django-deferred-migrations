from django.db import migrations

from deferred_migrations.operations import DeferredRenameModel


class Migration(migrations.Migration):
    dependencies = [
        ("deferred_migrations", "0002_modelrename"),
        ("deferred_migrations_testapp", "0001_initial"),
    ]

    operations = [
        DeferredRenameModel(old_name="Gadget", new_name="Gizmo"),
    ]

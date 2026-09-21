from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("deferred_migrations_testapp", "0001_initial"),
    ]

    operations = [
        migrations.RenameModel(old_name="Gadget", new_name="Gizmo"),
    ]

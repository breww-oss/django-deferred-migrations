from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("deferred_migrations_testapp", "0002_remove_child_legacy"),
    ]

    operations = [
        migrations.RemoveField(model_name="child", name="note"),
    ]

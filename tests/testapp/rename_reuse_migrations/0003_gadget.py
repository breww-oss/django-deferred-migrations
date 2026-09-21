from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    dependencies = [
        ("deferred_migrations_testapp", "0002_rename_gadget_gizmo"),
    ]

    operations = [
        migrations.CreateModel(name="Gadget", fields=[("id", models.BigAutoField(primary_key=True, serialize=False)), ("label", models.CharField(max_length=50))]),
    ]

from django.db import migrations
from django.db import models


# The migrations this replaces are gone from disk, as they are once a squash has been released, so MigrationLoader drops their nodes from the graph entirely.
class Migration(migrations.Migration):
    initial = True

    replaces = [
        ("deferred_migrations_testapp", "0001_initial"),
        ("deferred_migrations_testapp", "0002_remove_child_legacy"),
    ]

    dependencies = []

    operations = [
        migrations.CreateModel("Parent", [("id", models.BigAutoField(primary_key=True)), ("name", models.CharField(max_length=50))]),
        migrations.CreateModel("Child", [("id", models.BigAutoField(primary_key=True)), ("parent", models.ForeignKey(on_delete=models.CASCADE, to="deferred_migrations_testapp.parent")), ("note", models.TextField(null=True))]),
    ]

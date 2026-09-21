from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel("Parent", [("id", models.BigAutoField(primary_key=True)), ("name", models.CharField(max_length=50))]),
        migrations.CreateModel("Child", [("id", models.BigAutoField(primary_key=True)), ("parent", models.ForeignKey(on_delete=models.CASCADE, to="deferred_migrations_testapp.parent")), ("legacy", models.CharField(max_length=20)), ("note", models.TextField(null=True))]),
        migrations.CreateModel("MigrationRun", [("id", models.BigAutoField(primary_key=True))]),
    ]

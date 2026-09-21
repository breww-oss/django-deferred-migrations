from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    dependencies = [
        ("deferred_migrations_testapp", "0002_remove_child_legacy"),
    ]

    operations = [
        migrations.AddField(model_name="child", name="extra", field=models.IntegerField(null=True)),
    ]

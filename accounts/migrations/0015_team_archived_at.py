from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0014_organization_roles'),
    ]

    operations = [
        migrations.AddField(
            model_name='team',
            name='archived_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]

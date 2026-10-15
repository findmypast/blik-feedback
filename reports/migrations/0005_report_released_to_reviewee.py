from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('reports', '0004_add_uuid_field'),
    ]

    operations = [
        migrations.AddField(
            model_name='report',
            name='released_to_reviewee',
            field=models.BooleanField(
                default=False,
                help_text='Whether a peer report has been explicitly released to the reviewee',
            ),
        ),
    ]

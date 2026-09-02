from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('accounts', '0016_people_import_fields')]

    operations = [
        migrations.AddField(
            model_name='reviewee',
            name='pending_reporting_manager_email',
            field=models.EmailField(
                blank=True,
                help_text='Imported reporting manager to link when their account is provisioned.',
                max_length=254,
            ),
        ),
    ]

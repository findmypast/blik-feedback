from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('accounts', '0019_invitation_reporting_manager_role')]

    operations = [
        migrations.AddField(
            model_name='organizationinvitation',
            name='last_sent_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]

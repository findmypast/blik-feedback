import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('accounts', '0017_reviewee_pending_reporting_manager')]

    operations = [
        migrations.AddField(
            model_name='organizationinvitation',
            name='pending_reporting_manager_email',
            field=models.EmailField(blank=True, max_length=254),
        ),
        migrations.AddField(
            model_name='organizationinvitation',
            name='reporting_manager',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='pending_direct_report_invitations',
                to='accounts.userprofile',
            ),
        ),
    ]

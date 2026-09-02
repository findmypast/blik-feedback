from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('accounts', '0018_invitation_reporting_manager')]

    operations = [
        migrations.AlterField(
            model_name='organizationinvitation',
            name='requested_role',
            field=models.CharField(
                blank=True,
                choices=[
                    ('member', 'Member'),
                    ('reporting_manager', 'Reporting Manager'),
                    ('team_leader', 'Team Leader'),
                    ('admin', 'Organisation Administrator'),
                ],
                help_text='Role to apply when this invitation is accepted.',
                max_length=32,
            ),
        ),
    ]

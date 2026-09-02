import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('accounts', '0015_team_archived_at')]

    operations = [
        migrations.AddField(
            model_name='organizationinvitation',
            name='organization_role',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='pending_invitations',
                to='accounts.organizationrole',
            ),
        ),
        migrations.AddField(
            model_name='organizationinvitation',
            name='requested_role',
            field=models.CharField(
                blank=True,
                choices=[
                    ('member', 'Member'),
                    ('team_leader', 'Team Leader'),
                    ('admin', 'Organisation Administrator'),
                ],
                help_text='Role to apply when this invitation is accepted.',
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name='team',
            name='pending_manager_email',
            field=models.EmailField(
                blank=True,
                help_text='Imported manager to assign when their account is provisioned.',
                max_length=254,
            ),
        ),
    ]

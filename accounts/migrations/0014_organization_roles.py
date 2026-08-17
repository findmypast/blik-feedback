import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [('accounts', '0013_reviewee_multiple_teams')]

    operations = [
        migrations.CreateModel(
            name='OrganizationRole',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('name', models.CharField(max_length=100)),
                ('can_manage_users', models.BooleanField(default=False)),
                ('can_manage_teams', models.BooleanField(default=False)),
                ('can_invite_members', models.BooleanField(default=False)),
                ('can_create_cycles', models.BooleanField(default=False)),
                ('can_manage_questionnaires', models.BooleanField(default=False)),
                ('can_view_reports', models.BooleanField(default=False)),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='roles', to='core.organization')),
                ('parent', models.ForeignKey(blank=True, help_text='Permissions from the parent role are inherited.', null=True, on_delete=django.db.models.deletion.PROTECT, related_name='children', to='accounts.organizationrole')),
            ],
            options={'db_table': 'organization_roles', 'ordering': ['name']},
        ),
        migrations.AddConstraint(
            model_name='organizationrole',
            constraint=models.UniqueConstraint(fields=('organization', 'name'), name='unique_role_name_per_org'),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='organization_role',
            field=models.ForeignKey(blank=True, help_text='Optional organization-defined role. Administrators retain full access.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='members', to='accounts.organizationrole'),
        ),
    ]

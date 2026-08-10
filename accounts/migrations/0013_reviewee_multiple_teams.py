from django.db import migrations, models
import django.db.models.deletion


def copy_primary_teams(apps, schema_editor):
    Reviewee = apps.get_model('accounts', 'Reviewee')
    TeamMembership = apps.get_model('accounts', 'TeamMembership')
    TeamMembership.objects.bulk_create([
        TeamMembership(reviewee_id=reviewee_id, team_id=team_id)
        for reviewee_id, team_id in Reviewee.objects.exclude(
            team_id__isnull=True
        ).values_list('id', 'team_id')
    ], ignore_conflicts=True)


class Migration(migrations.Migration):
    dependencies = [('accounts', '0012_keep_team_manager_in_team')]

    operations = [
        migrations.CreateModel(
            name='TeamMembership',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('reviewee', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='team_memberships', to='accounts.reviewee')),
                ('team', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='memberships', to='accounts.team')),
            ],
            options={'db_table': 'team_memberships'},
        ),
        migrations.AddConstraint(
            model_name='teammembership',
            constraint=models.UniqueConstraint(fields=('reviewee', 'team'), name='unique_reviewee_team_membership'),
        ),
        migrations.AddField(
            model_name='reviewee',
            name='teams',
            field=models.ManyToManyField(blank=True, help_text='All teams this person belongs to. The team field remains the primary team.', related_name='members', through='accounts.TeamMembership', to='accounts.team'),
        ),
        migrations.RunPython(copy_primary_teams, migrations.RunPython.noop),
    ]

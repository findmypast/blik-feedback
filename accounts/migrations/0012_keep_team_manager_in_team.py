from django.db import migrations


def reconcile_manager_memberships(apps, schema_editor):
    Team = apps.get_model('accounts', 'Team')
    Reviewee = apps.get_model('accounts', 'Reviewee')

    for team in Team.objects.exclude(manager_id=None).select_related('manager__user'):
        manager = team.manager
        reviewee = Reviewee.objects.filter(
            organization_id=team.organization_id,
            email__iexact=manager.user.email,
        ).first()
        if reviewee:
            reviewee.profile_id = manager.id
            update_fields = ['profile', 'updated_at']
            if reviewee.team_id is None:
                reviewee.team_id = team.id
                update_fields.append('team')
            reviewee.save(update_fields=update_fields)
        else:
            Reviewee.objects.create(
                organization_id=team.organization_id,
                profile_id=manager.id,
                team_id=team.id,
                name=manager.user.get_full_name() or manager.user.username,
                email=manager.user.email,
                is_active=True,
            )


class Migration(migrations.Migration):
    dependencies = [('accounts', '0011_team_primary_manager')]

    operations = [
        migrations.RunPython(reconcile_manager_memberships, migrations.RunPython.noop),
    ]

"""
One-off data fix: keep the mistyped account that is already in a review cycle,
free the correct email held by the duplicate account, then rename the in-cycle
account to the correct email.

Other databases, including the test suite, do not contain this pair and no-op.
This repair is irreversible.
"""

import uuid

from django.contrib.auth.hashers import make_password
from django.db import migrations
from django.db.utils import OperationalError, ProgrammingError
from django.utils import timezone


# The pair this migration repairs. Absent on every other database, which no-ops.
TYPO_EMAIL = 'vehyslop@findmypast.com'
CORRECT_EMAIL = 'ehyslop@findmypast.com'
# Optional: limit the reviewee search to one organization (None = all).
ORGANIZATION_ID = None


def _targets():
    typo_raw = TYPO_EMAIL or ''
    correct_raw = CORRECT_EMAIL or ''
    typo = typo_raw.strip().lower()
    correct = correct_raw.strip().lower()
    if (
        not typo
        or not correct
        or typo == correct
        or 'REPLACE_WITH_' in typo_raw
        or 'REPLACE_WITH_' in correct_raw
    ):
        return None
    return typo, correct, ORGANIZATION_ID


def _rewrite_email(queryset, field, typo, correct, now):
    queryset.filter(**{f'{field}__iexact': typo}).update(
        **{field: correct, 'updated_at': now}
    )


def _anonymize_axes(schema_editor, username):
    try:
        from axes.models import AccessAttempt, AccessFailureLog, AccessLog
    except ImportError:
        return

    tables = None
    if schema_editor is not None:
        try:
            tables = set(schema_editor.connection.introspection.table_names())
        except (OperationalError, ProgrammingError):
            return

    anonymized = f'deleted-{uuid.uuid4().hex[:12]}'

    def update(model):
        if tables is not None and model._meta.db_table not in tables:
            return
        model.objects.filter(username=username).update(username=anonymized)

    try:
        update(AccessAttempt)
        update(AccessLog)
        update(AccessFailureLog)
    except (OperationalError, ProgrammingError):
        return


def _retarget_invitations(OrganizationInvitation, organization_id, typo, correct, now):
    invites = OrganizationInvitation.objects.filter(organization_id=organization_id)
    invites.filter(email__iexact=correct, accepted_at__isnull=True).delete()
    if invites.filter(email__iexact=correct).exists():
        invites.filter(email__iexact=typo, accepted_at__isnull=True).delete()
        return
    invites.filter(email__iexact=typo).update(email=correct, updated_at=now)


def _merge_lead_grants(TeamLeadGrant, profile_b_id, profile_a_id, now):
    for grant in list(TeamLeadGrant.objects.filter(profile_id=profile_b_id)):
        existing = TeamLeadGrant.objects.filter(
            profile_id=profile_a_id, team_id=grant.team_id
        ).first()
        if existing:
            if grant.include_descendants and not existing.include_descendants:
                existing.include_descendants = True
                existing.save(update_fields=['include_descendants', 'updated_at'])
            grant.delete()
            continue
        grant.profile_id = profile_a_id
        grant.save(update_fields=['profile', 'updated_at'])


def _transfer_profile(apps, profile_b, profile_a, now):
    Team = apps.get_model('accounts', 'Team')
    Reviewee = apps.get_model('accounts', 'Reviewee')
    OrganizationInvitation = apps.get_model('accounts', 'OrganizationInvitation')
    TeamLeadGrant = apps.get_model('accounts', 'TeamLeadGrant')

    Team.objects.filter(manager_id=profile_b.id).update(
        manager_id=profile_a.id, updated_at=now
    )
    Reviewee.objects.filter(reporting_manager_id=profile_b.id).update(
        reporting_manager_id=profile_a.id, updated_at=now
    )
    OrganizationInvitation.objects.filter(reporting_manager_id=profile_b.id).update(
        reporting_manager_id=profile_a.id, updated_at=now
    )
    _merge_lead_grants(TeamLeadGrant, profile_b.id, profile_a.id, now)
    profile_b.delete()


def _ensure_memberships(apps, reviewee_id, team_ids):
    TeamMembership = apps.get_model('accounts', 'TeamMembership')
    for team_id in team_ids:
        if TeamMembership.objects.filter(reviewee_id=reviewee_id, team_id=team_id).exists():
            continue
        TeamMembership.objects.create(reviewee_id=reviewee_id, team_id=team_id)


def _reassign_sign_in(apps, user_b_id, user_a_id, typo, correct):
    SocialAccount = apps.get_model('socialaccount', 'SocialAccount')
    EmailAddress = apps.get_model('account', 'EmailAddress')

    SocialAccount.objects.filter(user_id=user_b_id).update(user_id=user_a_id)

    parked = []
    for row in list(EmailAddress.objects.filter(user_id=user_b_id)):
        original = row.email
        row.email = f'deleted-b-{row.pk}-{user_b_id}@deleted.invalid'
        row.save(update_fields=['email'])
        parked.append((row, original))

    for row in list(EmailAddress.objects.filter(user_id=user_a_id)):
        if (row.email or '').lower() == typo:
            row.email = correct
            row.save(update_fields=['email'])

    owned = {
        (row.email or '').lower()
        for row in EmailAddress.objects.filter(user_id=user_a_id)
    }
    for row, original in parked:
        target = correct if (original or '').lower() == typo else original
        if (target or '').lower() in owned:
            row.delete()
            continue
        row.email = target
        row.user_id = user_a_id
        row.save(update_fields=['email', 'user'])
        owned.add((target or '').lower())


def fix_duplicate_account_email(apps, schema_editor):
    targets = _targets()
    if targets is None:
        return
    typo, correct, organization_id = targets

    User = apps.get_model('auth', 'User')
    UserProfile = apps.get_model('accounts', 'UserProfile')
    Reviewee = apps.get_model('accounts', 'Reviewee')
    TeamMembership = apps.get_model('accounts', 'TeamMembership')
    OrganizationInvitation = apps.get_model('accounts', 'OrganizationInvitation')
    Team = apps.get_model('accounts', 'Team')
    ReviewCycle = apps.get_model('reviews', 'ReviewCycle')
    ReviewCampaign = apps.get_model('reviews', 'ReviewCampaign')
    ReviewerToken = apps.get_model('reviews', 'ReviewerToken')

    reviewee_qs = Reviewee.objects.all()
    if organization_id is not None:
        reviewee_qs = reviewee_qs.filter(organization_id=organization_id)

    reviewee_a = list(reviewee_qs.filter(email__iexact=typo))
    reviewee_b = list(reviewee_qs.filter(email__iexact=correct))
    # User.email is global, not per organization.
    users_a = list(User.objects.filter(email__iexact=typo))
    users_b = list(User.objects.filter(email__iexact=correct))
    if not any((reviewee_a, reviewee_b, users_a, users_b)):
        return
    if (
        len(reviewee_a) != 1
        or len(reviewee_b) != 1
        or len(users_a) != 1
        or len(users_b) != 1
    ):
        raise RuntimeError(
            'Expected one mistyped reviewee, one correct-email reviewee, '
            'one mistyped user, and one correct-email user; '
            f'found reviewees {len(reviewee_a)}/{len(reviewee_b)}, '
            f'users {len(users_a)}/{len(users_b)}.'
        )

    a = reviewee_a[0]
    b = reviewee_b[0]
    user_a = users_a[0]
    user_b = users_b[0]
    if a.organization_id != b.organization_id:
        raise RuntimeError(
            'Reviewees belong to different organizations '
            f'({a.organization_id} vs {b.organization_id}).'
        )

    cycles_a = ReviewCycle.objects.filter(reviewee_id=a.id).count()
    cycles_b = ReviewCycle.objects.filter(reviewee_id=b.id).count()
    if cycles_a == 0:
        raise RuntimeError(
            f'Typo reviewee {a.id} has no review cycles; '
            'refusing to treat it as the in-cycle account.'
        )
    if cycles_b > 0:
        raise RuntimeError(
            f'Correct-email reviewee {b.id} has {cycles_b} review cycle(s); '
            'refusing to anonymize.'
        )
    if ReviewCampaign.objects.filter(individual_id=b.id).exists():
        raise RuntimeError(
            f'Correct-email reviewee {b.id} is the target of a review campaign; '
            'refusing to anonymize.'
        )

    profile_a = UserProfile.objects.filter(user_id=user_a.id).first()
    profile_b = UserProfile.objects.filter(user_id=user_b.id).first()
    if profile_a and profile_a.organization_id != a.organization_id:
        raise RuntimeError('Typo user profile belongs to a different organization.')
    if profile_b and profile_b.organization_id != a.organization_id:
        raise RuntimeError('Correct-email user profile belongs to a different organization.')
    if a.profile_id and (profile_a is None or a.profile_id != profile_a.id):
        raise RuntimeError('Typo reviewee is linked to a different user profile.')
    if b.profile_id and (profile_b is None or b.profile_id != profile_b.id):
        raise RuntimeError('Correct-email reviewee is linked to a different user profile.')

    now = timezone.now()
    ReviewerToken.objects.filter(
        reviewer_email__iexact=typo,
        cycle__reviewee__organization_id=a.organization_id,
    ).update(reviewer_email=correct, updated_at=now)
    _rewrite_email(
        Team.objects.filter(organization_id=a.organization_id),
        'pending_manager_email', typo, correct, now,
    )
    _rewrite_email(
        Reviewee.objects.filter(organization_id=a.organization_id),
        'pending_reporting_manager_email', typo, correct, now,
    )
    _rewrite_email(
        OrganizationInvitation.objects.filter(organization_id=a.organization_id),
        'pending_reporting_manager_email', typo, correct, now,
    )
    _retarget_invitations(
        OrganizationInvitation, a.organization_id, typo, correct, now
    )

    _anonymize_axes(schema_editor, user_b.username)

    # Keep the correct-email person's teams. The in-cycle account often has none.
    b_primary_team_id = b.team_id
    b_team_ids = []
    for team_id in TeamMembership.objects.filter(reviewee_id=b.id).values_list('team_id', flat=True):
        if team_id not in b_team_ids:
            b_team_ids.append(team_id)
    if b_primary_team_id and b_primary_team_id not in b_team_ids:
        b_team_ids.insert(0, b_primary_team_id)
    if profile_b:
        for team_id in Team.objects.filter(manager_id=profile_b.id).values_list('id', flat=True):
            if team_id not in b_team_ids:
                b_team_ids.append(team_id)

    TeamMembership.objects.filter(reviewee_id=b.id).delete()
    b.email = f'deleted-b-{b.id}@deleted.invalid'
    b.name = 'Deleted User'
    b.department = ''
    b.is_active = False
    b.profile_id = None
    b.team_id = None
    b.reporting_manager_id = None
    b.pending_reporting_manager_email = ''
    b.save(update_fields=[
        'email', 'name', 'department', 'is_active', 'profile', 'team',
        'reporting_manager', 'pending_reporting_manager_email', 'updated_at',
    ])

    if profile_b and profile_a is None:
        profile_b.user_id = user_a.id
        profile_b.save(update_fields=['user', 'updated_at'])
        profile_a = profile_b
    elif profile_b and profile_a is not None:
        _transfer_profile(apps, profile_b, profile_a, now)

    user_b.email = f'deleted-b-user-{user_b.id}@deleted.invalid'
    user_b.username = user_b.email
    user_b.first_name = ''
    user_b.last_name = ''
    user_b.is_active = False
    user_b.password = make_password(None)
    user_b.save(update_fields=[
        'email', 'username', 'first_name', 'last_name', 'is_active', 'password',
    ])

    _reassign_sign_in(apps, user_b.id, user_a.id, typo, correct)

    if User.objects.filter(username__iexact=correct).exclude(pk=user_a.id).exists():
        raise RuntimeError(
            'Another account already uses the correct email as its username.'
        )
    user_a.email = correct
    user_a.username = correct
    user_a.is_active = True
    user_a.save(update_fields=['email', 'username', 'is_active'])

    a = Reviewee.objects.get(pk=a.id)
    a.email = correct
    update_fields = ['email', 'updated_at']
    if (
        profile_a is not None
        and a.profile_id is None
        and UserProfile.objects.filter(pk=profile_a.id).exists()
    ):
        a.profile_id = profile_a.id
        update_fields.append('profile')
    if a.team_id is None and (b_primary_team_id or b_team_ids):
        a.team_id = b_primary_team_id or b_team_ids[0]
        update_fields.append('team')
    a.save(update_fields=update_fields)
    _ensure_memberships(apps, a.id, b_team_ids)


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0021_userprofile_exclude_from_review_cycles'),
        ('reviews', '0019_optional_organization_cycle_questionnaires'),
        ('account', '0001_initial'),
        ('socialaccount', '0006_alter_socialaccount_extra_data'),
    ]

    operations = [
        migrations.RunPython(fix_duplicate_account_email),
    ]

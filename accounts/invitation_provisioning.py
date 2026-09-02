from accounts.models import Reviewee, Team, TeamLeadGrant
from accounts.permissions import assign_organization_admin, assign_organization_member


def apply_invitation_access(invitation, profile):
    """Apply access deferred by a spreadsheet-created invitation."""
    user = profile.user
    profile.organization_role = invitation.organization_role
    requested_role = invitation.requested_role or 'member'
    can_create = (
        profile.can_create_cycles_for_others
        or requested_role == 'team_leader'
        or bool(
            invitation.organization_role
            and 'can_create_cycles' in invitation.organization_role.effective_permissions()
        )
    )
    if requested_role == 'admin':
        assign_organization_admin(user)
    else:
        assign_organization_member(user, can_create_cycles_for_others=can_create)
    profile.can_create_cycles_for_others = can_create
    profile.save(update_fields=[
        'organization_role', 'can_create_cycles_for_others', 'updated_at'
    ])

    reviewee = Reviewee.objects.filter(profile=profile).first()
    if reviewee:
        reviewee.reporting_manager = invitation.reporting_manager
        reviewee.pending_reporting_manager_email = (
            invitation.pending_reporting_manager_email
            if not invitation.reporting_manager_id else ''
        )
        reviewee.save(update_fields=[
            'reporting_manager', 'pending_reporting_manager_email', 'updated_at'
        ])
    if requested_role == 'team_leader' and invitation.team_id:
        TeamLeadGrant.objects.get_or_create(profile=profile, team=invitation.team)
    for team in Team.objects.filter(
        organization=invitation.organization,
        pending_manager_email__iexact=user.email,
    ):
        team.manager = profile
        team.pending_manager_email = ''
        team.save(update_fields=['manager', 'pending_manager_email', 'updated_at'])
        TeamLeadGrant.objects.get_or_create(profile=profile, team=team)
        if reviewee:
            reviewee.teams.add(team)
    Reviewee.objects.filter(
        organization=invitation.organization,
        pending_reporting_manager_email__iexact=user.email,
    ).update(
        reporting_manager=profile,
        pending_reporting_manager_email='',
    )

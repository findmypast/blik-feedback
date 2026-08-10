"""Shared row-level authorization for organization people and their records."""
from dataclasses import dataclass

from django.db.models import Q

from accounts.models import Reviewee, Team, TeamLeadGrant
from accounts.permissions import can_view_all_reports


@dataclass(frozen=True)
class EffectiveScope:
    organization_id: int | None
    reviewee_ids: frozenset[int]
    organization_wide: bool = False


def _descendant_ids(root_id, children):
    found, pending = set(), [root_id]
    while pending:
        team_id = pending.pop()
        if team_id in found:
            continue
        found.add(team_id)
        pending.extend(children.get(team_id, ()))
    return found


def descendant_team_ids(team, include_self=True):
    """Return IDs in a team's subtree, constrained to its organization."""
    children = {}
    for team_id, parent_id in Team.objects.for_organization(
        team.organization
    ).values_list('id', 'parent_id'):
        children.setdefault(parent_id, set()).add(team_id)
    result = _descendant_ids(team.id, children)
    if not include_self:
        result.discard(team.id)
    return result


def effective_scope(user, organization=None):
    """Return the union of self, reporting-line, and team-lead access."""
    if not getattr(user, 'is_authenticated', False):
        return EffectiveScope(None, frozenset())
    profile = getattr(user, 'profile', None)
    organization = organization or getattr(profile, 'organization', None)
    if not profile or not organization or profile.organization_id != organization.id:
        return EffectiveScope(getattr(organization, 'id', None), frozenset())
    if can_view_all_reports(user):
        return EffectiveScope(organization.id, frozenset(), True)

    team_rows = Team.objects.for_organization(organization).values_list('id', 'parent_id')
    children = {}
    for team_id, parent_id in team_rows:
        children.setdefault(parent_id, set()).add(team_id)

    granted_team_ids = set()
    granted_team_ids.update(
        Team.objects.for_organization(organization).filter(
            manager=profile
        ).values_list('id', flat=True)
    )
    grants = TeamLeadGrant.objects.filter(
        profile=profile, team__organization=organization
    ).prefetch_related('revocations')
    for grant in grants:
        grant_teams = (_descendant_ids(grant.team_id, children)
                       if grant.include_descendants else {grant.team_id})
        for revocation in grant.revocations.all():
            grant_teams -= _descendant_ids(revocation.team_id, children)
        granted_team_ids |= grant_teams

    visible = Reviewee.objects.for_organization(organization).filter(
        Q(profile=profile) |
        Q(email__iexact=user.email) |
        Q(reporting_manager=profile) |
        Q(team_id__in=granted_team_ids) |
        Q(team_memberships__team_id__in=granted_team_ids)
    ).distinct().values_list('id', flat=True)
    return EffectiveScope(organization.id, frozenset(visible))


def visible_reviewees(user, queryset, organization=None):
    scope = effective_scope(user, organization)
    return queryset if scope.organization_wide else queryset.filter(id__in=scope.reviewee_ids)


def visible_cycles(user, queryset, organization=None, reviewee_field='reviewee_id'):
    scope = effective_scope(user, organization)
    return queryset if scope.organization_wide else queryset.filter(
        **{f'{reviewee_field}__in': scope.reviewee_ids}
    )


def visible_reports(user, queryset, organization=None):
    return visible_cycles(user, queryset, organization, 'cycle__reviewee_id')


def visible_reviewer_tokens(user, queryset, organization=None):
    return visible_cycles(user, queryset, organization, 'cycle__reviewee_id')


def visible_invitations(user, queryset, organization=None):
    """Invitations are scoped by the matching in-scope reviewee email."""
    scope = effective_scope(user, organization)
    if scope.organization_wide:
        return queryset
    emails = Reviewee.objects.filter(id__in=scope.reviewee_ids).values('email')
    return queryset.filter(email__in=emails)


def visible_profiles(user, queryset, organization=None):
    scope = effective_scope(user, organization)
    if scope.organization_wide:
        return queryset
    profile_ids = Reviewee.objects.filter(
        id__in=scope.reviewee_ids, profile__isnull=False
    ).values('profile_id')
    return queryset.filter(id__in=profile_ids)


def manageable_teams(user, organization=None):
    """Teams the user may target directly when creating a campaign."""
    profile = getattr(user, 'profile', None)
    organization = organization or getattr(profile, 'organization', None)
    queryset = Team.objects.for_organization(organization)
    if not profile or not organization or profile.organization_id != organization.id:
        return queryset.none()
    if user.has_perm('accounts.can_manage_organization'):
        return queryset

    allowed_ids = set(queryset.filter(manager=profile).values_list('id', flat=True))
    children = {}
    for team_id, parent_id in queryset.values_list('id', 'parent_id'):
        children.setdefault(parent_id, set()).add(team_id)
    for grant in TeamLeadGrant.objects.filter(
        profile=profile, team__organization=organization
    ).prefetch_related('revocations'):
        team_ids = (_descendant_ids(grant.team_id, children)
                    if grant.include_descendants else {grant.team_id})
        for revocation in grant.revocations.all():
            team_ids -= _descendant_ids(revocation.team_id, children)
        allowed_ids |= team_ids
    return queryset.filter(id__in=allowed_ids)


def is_top_level_team_lead(user, organization=None):
    """Whether the user leads at least one root team in this organization."""
    if not getattr(user, 'is_authenticated', False):
        return False
    profile = getattr(user, 'profile', None)
    organization = organization or getattr(profile, 'organization', None)
    if not profile or not organization or profile.organization_id != organization.id:
        return False
    return (
        Team.objects.for_organization(organization).filter(
            parent__isnull=True, manager=profile
        ).exists()
        or TeamLeadGrant.objects.filter(
            profile=profile,
            team__organization=organization,
            team__parent__isnull=True,
        ).exists()
    )


def can_edit_questionnaire(user, questionnaire):
    """Owners edit their own questionnaires; root leads/admins edit all org questionnaires."""
    profile = getattr(user, 'profile', None)
    if (
        not getattr(user, 'is_authenticated', False)
        or not profile
        or questionnaire.organization_id != profile.organization_id
    ):
        return False
    return bool(
        user.has_perm('accounts.can_manage_organization')
        or questionnaire.created_by_id == user.id
        or is_top_level_team_lead(user, profile.organization)
    )

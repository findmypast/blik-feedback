from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db.models import Q
from django.db import transaction
from django.utils import timezone

from accounts.authorization import descendant_team_ids
from accounts.models import Reviewee
from .models import (
    OrganizationalReviewCycle, ReviewCampaign, ReviewCycle, ReviewerToken,
)


QUESTIONNAIRE_FLAG = {
    'peer': 'allow_peer_review',
    'self': 'allow_self_assessment',
    'manager': 'allow_manager_assessment',
}


def questionnaire_supports(questionnaire, cycle_type):
    flag = QUESTIONNAIRE_FLAG.get(cycle_type)
    return bool(flag and getattr(questionnaire, flag, False))


@transaction.atomic
def launch_organizational_cycle(
    *, organization, created_by, questionnaires, minimum_peer_reviewers,
    start_date=None, due_date=None, name='', audience_type='entire',
    teams=None, participants=None,
):
    """Launch one shared self campaign plus applicable campaigns per team."""
    for cycle_type, questionnaire in questionnaires.items():
        if not questionnaire_supports(questionnaire, cycle_type):
            raise ValidationError(
                f'{questionnaire.name} does not support {cycle_type} assessments.'
            )
    from accounts.models import Team

    if audience_type not in dict(OrganizationalReviewCycle.AUDIENCE_CHOICES):
        raise ValidationError('Select a valid organisation audience.')
    available_teams = Team.objects.for_organization(organization)
    selected_teams = list(teams) if teams is not None else list(available_teams)
    if any(team.organization_id != organization.id for team in selected_teams):
        raise ValidationError('Every selected team must belong to this organisation.')

    if participants is None:
        if audience_type == 'teams':
            team_ids = [team.id for team in selected_teams]
            participants = Reviewee.objects.for_organization(organization).filter(
                Q(team_id__in=team_ids) | Q(team_memberships__team_id__in=team_ids),
                is_active=True,
            ).distinct()
        else:
            participants = Reviewee.objects.for_organization(organization).filter(
                is_active=True
            )
    participant_ids = {
        participant.id for participant in participants
        if participant.organization_id == organization.id and participant.is_active
    }
    if not participant_ids:
        raise ValidationError('The selected audience has no active people.')

    if audience_type == 'individuals':
        selected_teams = list(available_teams.filter(
            Q(reviewees__id__in=participant_ids)
            | Q(memberships__reviewee_id__in=participant_ids)
        ).distinct())
    parent = OrganizationalReviewCycle.objects.create(
        organization=organization,
        created_by=created_by,
        name=name,
        self_questionnaire=questionnaires['self'],
        peer_questionnaire=questionnaires['peer'],
        manager_questionnaire=questionnaires.get('manager'),
        minimum_peer_reviewers=minimum_peer_reviewers,
        start_date=start_date,
        due_date=due_date,
        audience_type=audience_type,
    )
    parent.teams.set(selected_teams)
    parent.selected_reviewees.set(participant_ids)
    self_campaign = ReviewCampaign.objects.create(
        organization=organization,
        created_by=created_by,
        name=name,
        questionnaire=questionnaires['self'],
        target_type='organization',
        cycle_type='self',
        minimum_peer_reviewers=1,
        start_date=start_date,
        due_date=due_date,
        organizational_cycle=parent,
    )
    for participant in Reviewee.objects.filter(id__in=participant_ids):
        cycle = _create_cycle(self_campaign, participant)
        if participant.email:
            ReviewerToken.objects.create(
                cycle=cycle,
                category='self',
                reviewer_email=participant.email,
            )
    self_campaign.status = 'active'
    self_campaign.save(update_fields=['status', 'updated_at'])

    for team in sorted(selected_teams, key=lambda selected: selected.name.lower()):
        team_participants = list(campaign_members_for_team(team).filter(
            id__in=participant_ids
        ))
        if not team_participants:
            continue
        peer_campaign = ReviewCampaign.objects.create(
            organization=organization,
            created_by=created_by,
            name=name,
            questionnaire=questionnaires['peer'],
            target_type='team',
            team=team,
            cycle_type='peer',
            minimum_peer_reviewers=minimum_peer_reviewers,
            start_date=start_date,
            due_date=due_date,
            organizational_cycle=parent,
        )
        for participant in team_participants:
            _create_cycle(peer_campaign, participant)
        peer_campaign.status = 'active'
        peer_campaign.save(update_fields=['status', 'updated_at'])

        if audience_type == 'individuals' or not team.manager_id:
            continue
        manager_reviewee = getattr(team.manager, 'reviewee', None)
        reviewers = [
            participant for participant in team_participants
            if participant.email and (
                not manager_reviewee or participant.id != manager_reviewee.id
            )
        ]
        if not manager_reviewee or not manager_reviewee.is_active or not reviewers:
            continue
        manager_campaign = ReviewCampaign.objects.create(
            organization=organization,
            created_by=created_by,
            name=name,
            questionnaire=questionnaires['manager'],
            target_type='team',
            team=team,
            cycle_type='manager',
            minimum_peer_reviewers=1,
            start_date=start_date,
            due_date=due_date,
            organizational_cycle=parent,
        )
        cycle = _create_cycle(manager_campaign, manager_reviewee)
        ReviewerToken.objects.bulk_create([
            ReviewerToken(
                cycle=cycle,
                category='direct_report',
                reviewer_email=participant.email,
                assigned_team=team,
            )
            for participant in reviewers
        ])
        manager_campaign.status = 'active'
        manager_campaign.save(update_fields=['status', 'updated_at'])
    return parent


def campaign_members_for_team(team):
    """Active members assigned to one exact team, including additive membership."""
    return Reviewee.objects.for_organization(team.organization).filter(
        Q(team=team) | Q(team_memberships__team=team),
        is_active=True,
    ).distinct()


def campaign_members(campaign):
    if campaign.target_type == 'organization':
        return Reviewee.objects.for_organization(campaign.organization).filter(
            is_active=True
        )
    if campaign.target_type == 'individual':
        return Reviewee.objects.filter(
            id=campaign.individual_id,
            organization=campaign.organization,
            is_active=True,
        )
    team_ids = (
        descendant_team_ids(campaign.team)
        if campaign.include_descendants else {campaign.team_id}
    )
    return Reviewee.objects.for_organization(campaign.organization).filter(
        Q(team_id__in=team_ids) | Q(team_memberships__team_id__in=team_ids),
        is_active=True,
    ).distinct()


@transaction.atomic
def launch_campaign(campaign):
    """Create the assessments and reviewer assignments for a draft campaign."""
    if campaign.status != 'draft':
        raise ValidationError('Only draft campaigns can be launched.')
    if not questionnaire_supports(campaign.questionnaire, campaign.cycle_type):
        raise ValidationError('The selected questionnaire does not support this cycle type.')
    if campaign.target_type == 'team' and not campaign.team_id:
        raise ValidationError('Select a team.')
    if campaign.target_type == 'individual' and not campaign.individual_id:
        raise ValidationError('Select an individual.')
    if campaign.target_type == 'individual' and campaign.cycle_type == 'manager':
        raise ValidationError('Manager assessments require a team.')

    members = list(campaign_members(campaign))
    if not members:
        raise ValidationError('The selected audience has no active members.')

    if campaign.cycle_type == 'manager' and campaign.target_type == 'organization':
        _launch_organization_manager_campaign(campaign)
    elif campaign.cycle_type == 'manager':
        manager = campaign.team.manager
        reviewee = getattr(manager, 'reviewee', None) if manager else None
        if not reviewee or not reviewee.is_active:
            raise ValidationError('The selected team manager must have an active reviewee profile.')
        cycle = _create_cycle(campaign, reviewee)
        ReviewerToken.objects.bulk_create([
            ReviewerToken(
                cycle=cycle,
                category='direct_report',
                reviewer_email=member.email,
                assigned_team=campaign.team,
            )
            for member in members
            if member.email and member.id != reviewee.id
        ])
    else:
        for member in members:
            cycle = _create_cycle(campaign, member)
            if campaign.cycle_type == 'self' and member.email:
                ReviewerToken.objects.create(
                    cycle=cycle,
                    category='self',
                    reviewer_email=member.email,
                )
            # Peer cycles intentionally start without reviewer tokens. The
            # reviewee nominates reviewers after receiving the campaign alert.

    campaign.status = 'active'
    campaign.save(update_fields=['status', 'updated_at'])
    return campaign


def _launch_organization_manager_campaign(campaign):
    """Invite employees to assess each distinct manager of their teams."""
    from accounts.models import Team

    invited_assignments = set()
    created_cycles = {}
    teams = Team.objects.for_organization(campaign.organization).filter(
        manager__isnull=False,
    ).select_related('manager__reviewee').prefetch_related('members')
    for team in teams:
        manager_reviewee = getattr(team.manager, 'reviewee', None)
        if not manager_reviewee or not manager_reviewee.is_active:
            continue
        cycle = created_cycles.get(manager_reviewee.id)
        if not cycle:
            cycle = _create_cycle(campaign, manager_reviewee)
            created_cycles[manager_reviewee.id] = cycle
        for member in team.members.filter(is_active=True).exclude(pk=manager_reviewee.pk):
            if not member.email:
                continue
            assignment = (manager_reviewee.id, member.email.lower(), team.id)
            if assignment in invited_assignments:
                continue
            invited_assignments.add(assignment)
            ReviewerToken.objects.create(
                cycle=cycle,
                category='direct_report',
                reviewer_email=member.email,
                assigned_team=team,
            )
    if not created_cycles:
        raise ValidationError(
            'No teams have both an active manager and active members.'
        )


def _create_cycle(campaign, reviewee):
    return ReviewCycle.objects.create(
        campaign=campaign,
        reviewee=reviewee,
        questionnaire=campaign.questionnaire,
        created_by=campaign.created_by,
        status='active',
        cycle_type=campaign.cycle_type,
        start_date=campaign.start_date,
        due_date=campaign.due_date,
    )


@transaction.atomic
def renew_campaign(source, created_by):
    """Create and launch a new campaign using a completed campaign's setup."""
    start_date = timezone.localdate()
    duration = (
        source.due_date - source.start_date
        if source.start_date and source.due_date else timedelta(days=30)
    )
    campaign = ReviewCampaign.objects.create(
        organization=source.organization,
        created_by=created_by,
        name=source.name,
        questionnaire=source.questionnaire,
        target_type=source.target_type,
        team=source.team,
        individual=source.individual,
        include_descendants=source.include_descendants,
        cycle_type=source.cycle_type,
        minimum_peer_reviewers=source.minimum_peer_reviewers,
        start_date=start_date,
        due_date=start_date + duration,
        renewed_from=source,
    )
    return launch_campaign(campaign)

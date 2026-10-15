from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db.models import Q
from django.db import transaction
from django.utils import timezone

from accounts.authorization import descendant_team_ids
from accounts.models import Reviewee
from .models import (
    OrganizationalReviewCycle, OrganizationReviewPhase, ReviewCampaign,
    ReviewCycle, ReviewerToken,
)


QUESTIONNAIRE_FLAG = {
    'peer': 'allow_peer_review',
    'self': 'allow_self_assessment',
    'manager': 'allow_manager_assessment',
}


def _reviewee_for_profile(profile):
    if not profile:
        return None
    return Reviewee.objects.filter(
        Q(profile=profile) | Q(
            organization=profile.organization,
            email__iexact=profile.user.email,
        )
    ).first()


def cycle_eligible_reviewees(queryset):
    """Remove people explicitly exempted from new review cycles."""
    return queryset.exclude(profile__exclude_from_review_cycles=True)


def questionnaire_supports(questionnaire, cycle_type):
    flag = QUESTIONNAIRE_FLAG.get(cycle_type)
    return bool(flag and getattr(questionnaire, flag, False))


@transaction.atomic
def launch_organizational_cycle(
    *, organization, created_by, questionnaires, minimum_peer_reviewers,
    start_date=None, due_date=None, name='', audience_type='entire',
    teams=None, participants=None, phase_definitions=None,
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
            participants = cycle_eligible_reviewees(
                Reviewee.objects.for_organization(organization).filter(
                Q(team_id__in=team_ids) | Q(team_memberships__team_id__in=team_ids),
                is_active=True,
                ).distinct()
            )
        else:
            participants = cycle_eligible_reviewees(
                Reviewee.objects.for_organization(organization).filter(is_active=True)
            )
    else:
        participants = cycle_eligible_reviewees(
            Reviewee.objects.filter(id__in=[participant.id for participant in participants])
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
        self_questionnaire=questionnaires.get('self'),
        peer_questionnaire=questionnaires.get('peer'),
        manager_questionnaire=questionnaires.get('manager'),
        minimum_peer_reviewers=minimum_peer_reviewers,
        start_date=start_date,
        due_date=due_date,
        audience_type=audience_type,
        status='draft' if phase_definitions else 'active',
    )
    parent.teams.set(selected_teams)
    parent.selected_reviewees.set(participant_ids)
    phase_by_type = {}
    for position, definition in enumerate(phase_definitions or [], 1):
        phase = OrganizationReviewPhase.objects.create(
            organizational_cycle=parent, name=definition['name'],
            position=position, due_date=definition['due_date'],
        )
        assessment_types = definition.get('assessment_types') or [
            definition['assessment_type']
        ]
        for assessment_type in assessment_types:
            phase_by_type[assessment_type] = phase

    def campaign_schedule(assessment_type):
        phase = phase_by_type.get(assessment_type)
        return {
            'phase': phase,
            'start_date': None if phase else start_date,
            'due_date': phase.due_date if phase else due_date,
        }

    def activate_unphased(campaign):
        if not campaign.phase_id:
            campaign.status = 'active'
            campaign.save(update_fields=['status', 'updated_at'])

    if questionnaires.get('self'):
        self_campaign = ReviewCampaign.objects.create(
            organization=organization,
            created_by=created_by,
            name=name,
            questionnaire=questionnaires['self'],
            target_type='organization',
            cycle_type='self',
            minimum_peer_reviewers=1,
            organizational_cycle=parent,
            **campaign_schedule('self'),
        )
        for participant in Reviewee.objects.filter(id__in=participant_ids):
            cycle = _create_cycle(self_campaign, participant)
            if participant.email:
                ReviewerToken.objects.create(
                    cycle=cycle,
                    category='self',
                    reviewer_email=participant.email,
                )
        activate_unphased(self_campaign)

    assigned_participant_ids = set()
    assigned_manager_reviews = set()
    for team in sorted(selected_teams, key=lambda selected: selected.name.lower()):
        team_participants = list(campaign_members_for_team(team).filter(
            id__in=participant_ids
        ))
        assigned_participant_ids.update(
            participant.id for participant in team_participants
        )
        if not team_participants:
            continue
        if questionnaires.get('peer'):
            peer_campaign = ReviewCampaign.objects.create(
                organization=organization,
                created_by=created_by,
                name=name,
                questionnaire=questionnaires['peer'],
                target_type='team',
                team=team,
                cycle_type='peer',
                minimum_peer_reviewers=minimum_peer_reviewers,
                organizational_cycle=parent,
                **campaign_schedule('peer'),
            )
            for participant in team_participants:
                _create_cycle(peer_campaign, participant)
            activate_unphased(peer_campaign)

        if audience_type == 'individuals' or not questionnaires.get('manager'):
            continue
        manager_groups = {}
        for participant in team_participants:
            manager = participant.reporting_manager or team.manager
            manager_reviewee = _reviewee_for_profile(manager)
            assignment = (participant.id, getattr(manager_reviewee, 'id', None))
            if (
                not participant.email or not manager_reviewee
                or not manager_reviewee.is_active or participant.id == manager_reviewee.id
                or assignment in assigned_manager_reviews
            ):
                continue
            assigned_manager_reviews.add(assignment)
            manager_groups.setdefault(manager_reviewee, []).append(participant)
        for manager_reviewee, reviewers in manager_groups.items():
            manager_campaign = ReviewCampaign.objects.create(
                organization=organization, created_by=created_by, name=name,
                questionnaire=questionnaires['manager'], target_type='team', team=team,
                cycle_type='manager', minimum_peer_reviewers=1,
                organizational_cycle=parent, **campaign_schedule('manager'),
            )
            cycle = _create_cycle(manager_campaign, manager_reviewee)
            ReviewerToken.objects.bulk_create([
                ReviewerToken(cycle=cycle, category='direct_report',
                              reviewer_email=participant.email, assigned_team=team)
                for participant in reviewers
            ])
            activate_unphased(manager_campaign)

    unassigned_participant_ids = participant_ids - assigned_participant_ids
    if (
        questionnaires.get('peer')
        and audience_type == 'entire'
        and unassigned_participant_ids
    ):
        peer_campaign = ReviewCampaign.objects.create(
            organization=organization,
            created_by=created_by,
            name=name,
            questionnaire=questionnaires['peer'],
            target_type='organization',
            cycle_type='peer',
            minimum_peer_reviewers=minimum_peer_reviewers,
            organizational_cycle=parent,
            **campaign_schedule('peer'),
        )
        for participant in Reviewee.objects.filter(
            id__in=unassigned_participant_ids
        ):
            _create_cycle(peer_campaign, participant)
        activate_unphased(peer_campaign)
    return parent


def campaign_members_for_team(team):
    """Active members assigned to one exact team, including additive membership."""
    return cycle_eligible_reviewees(
        Reviewee.objects.for_organization(team.organization).filter(
            Q(team=team) | Q(team_memberships__team=team),
            is_active=True,
        ).distinct()
    )


def campaign_members(campaign):
    if campaign.target_type == 'organization':
        return cycle_eligible_reviewees(
            Reviewee.objects.for_organization(campaign.organization).filter(
                is_active=True
            )
        )
    if campaign.target_type == 'individual':
        return cycle_eligible_reviewees(
            Reviewee.objects.filter(
                id=campaign.individual_id,
                organization=campaign.organization,
                is_active=True,
            )
        )
    team_ids = (
        descendant_team_ids(campaign.team)
        if campaign.include_descendants else {campaign.team_id}
    )
    return cycle_eligible_reviewees(
        Reviewee.objects.for_organization(campaign.organization).filter(
            Q(team_id__in=team_ids) | Q(team_memberships__team_id__in=team_ids),
            is_active=True,
        ).distinct()
    )


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
    """Invite employees to assess their reporting manager, then their team lead."""
    from accounts.models import Team

    invited_assignments = set()
    created_cycles = {}
    teams = Team.objects.for_organization(campaign.organization).select_related(
        'manager__reviewee'
    ).prefetch_related('members')
    for team in teams:
        for member in cycle_eligible_reviewees(
            team.members.filter(is_active=True)
        ).select_related('reporting_manager__reviewee'):
            if not member.email:
                continue
            manager = member.reporting_manager or team.manager
            manager_reviewee = _reviewee_for_profile(manager)
            if (
                not manager_reviewee or not manager_reviewee.is_active
                or getattr(manager_reviewee.profile, 'exclude_from_review_cycles', False)
                or member == manager_reviewee
            ):
                continue
            assignment = (manager_reviewee.id, member.email.lower())
            if assignment in invited_assignments:
                continue
            invited_assignments.add(assignment)
            cycle = created_cycles.get(manager_reviewee.id)
            if not cycle:
                cycle = _create_cycle(campaign, manager_reviewee)
                created_cycles[manager_reviewee.id] = cycle
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
        status='draft' if campaign.phase_id else 'active',
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

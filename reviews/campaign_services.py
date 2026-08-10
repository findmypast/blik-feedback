from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db.models import Q
from django.db import transaction
from django.utils import timezone

from accounts.authorization import descendant_team_ids
from accounts.models import Reviewee
from .models import ReviewCampaign, ReviewCycle, ReviewerToken


QUESTIONNAIRE_FLAG = {
    'peer': 'allow_peer_review',
    'self': 'allow_self_assessment',
    'manager': 'allow_manager_assessment',
}


def questionnaire_supports(questionnaire, cycle_type):
    flag = QUESTIONNAIRE_FLAG.get(cycle_type)
    return bool(flag and getattr(questionnaire, flag, False))


def campaign_members(campaign):
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

    if campaign.cycle_type == 'manager':
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

"""
Admin dashboard views for Blik
"""
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse, Http404
from django.core.exceptions import ValidationError, PermissionDenied
from django.core.validators import validate_email
from django.template.loader import render_to_string
from django.contrib import messages
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.db import transaction
from django.db.models import Count, Q, Max
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.http import HttpResponseRedirect
from datetime import timedelta
from datetime import date
import re

from accounts.models import (
    Reviewee, UserProfile, OrganizationInvitation, OrganizationRole, Team, TeamLeadGrant,
    TeamLeadRevocation, TeamMembership,
)
from accounts.permissions import can_view_all_reports, visible_cycles
from accounts.authorization import (
    can_edit_questionnaire, descendant_team_ids, visible_reviewees,
    visible_invitations, visible_profiles, led_teams, manageable_teams,
)
from reviews.models import ReviewCampaign, ReviewCycle, ReviewerToken
from reviews.services import (
    assign_tokens_to_emails, send_reviewer_invitations,
    send_reviewee_notifications,
)
from questionnaires.models import Questionnaire
from reports.models import Report
from core.models import Organization
from core.gdpr import GDPRDeletionService
from core.env_config import env_managed_fields

import logging
logger = logging.getLogger(__name__)


def _send_team_ownership_notifications(organization, transfers):
    """Notify newly assigned team managers without failing the saved transfer."""
    from core.email import send_email
    from django.conf import settings

    for team_name, new_manager_name, new_manager_email, assigned_by in transfers:
        try:
            context = {
                'organization': organization,
                'team_name': team_name,
                'new_manager_name': new_manager_name,
                'assigned_by': assigned_by,
                'team_url': f'{settings.SITE_PROTOCOL}://{settings.SITE_DOMAIN}/dashboard/team/',
            }
            send_email(
                subject=f'You now manage the {team_name} team in Blik',
                message=render_to_string('emails/team_ownership_transferred.txt', context),
                html_message=render_to_string('emails/team_ownership_transferred.html', context),
                recipient_list=[new_manager_email],
            )
        except Exception:
            logger.exception(
                'Failed to send team ownership notification for %s', team_name
            )


def get_cycle_or_404(request, cycle_uuid):
    """
    Get a ReviewCycle by UUID, filtered by organization and by what the current
    user is allowed to see. Returns 404 for cycles in another organization or
    belonging to another reviewee.
    """
    cycles_qs = ReviewCycle.objects.select_related('reviewee', 'questionnaire', 'created_by')
    if request.organization:
        cycles_qs = cycles_qs.filter(reviewee__organization=request.organization)
    return get_object_or_404(visible_cycles(request.user, cycles_qs), uuid=cycle_uuid)


def _synchronize_cycle_parent_status(cycle):
    """Complete campaign parents once their last active child has ended."""
    if not cycle.campaign_id:
        return
    campaign = cycle.campaign
    if campaign.cycles.filter(status='active').exists():
        return
    if campaign.status != 'completed':
        campaign.status = 'completed'
        campaign.save(update_fields=['status', 'updated_at'])
    if campaign.organizational_cycle_id:
        parent = campaign.organizational_cycle
        if (
            parent.status != 'completed'
            and not parent.campaigns.filter(status='active').exists()
        ):
            parent.status = 'completed'
            parent.save(update_fields=['status', 'updated_at'])


@login_required
def dashboard(request):
    """Admin dashboard homepage"""
    from subscriptions.utils import get_subscription_status

    org = request.organization

    # Get statistics filtered by organization
    reviewees_qs = visible_reviewees(
        request.user, Reviewee.objects.for_organization(org).filter(is_active=True), org
    )
    cycles_qs = visible_cycles(
        request.user,
        ReviewCycle.objects.for_organization(org).select_related('reviewee', 'questionnaire')
    )

    total_reviewees = reviewees_qs.count()
    active_cycles = cycles_qs.filter(status='active').count()
    completed_cycles = cycles_qs.filter(status='completed').count()

    # Get subscription status
    subscription_status = get_subscription_status(org) if org else None

    # Recent reports the current user is permitted to see.
    recent_reports = Report.objects.filter(
        cycle__in=cycles_qs,
        available=True,
    ).select_related(
        'cycle__reviewee', 'cycle__questionnaire', 'cycle__campaign'
    ).order_by('-generated_at')[:6]

    # Pending reviews (tokens not completed)
    pending_tokens = ReviewerToken.objects.filter(
        cycle__in=cycles_qs,
        completed_at__isnull=True,
        cycle__status='active'
    ).filter(
        Q(cycle__campaign__isnull=True)
        | Q(cycle__campaign__status='active')
    ).filter(
        Q(cycle__campaign__organizational_cycle__isnull=True)
        | Q(cycle__campaign__organizational_cycle__status='active')
    ).count()

    campaigns_qs = (
        ReviewCampaign.objects.filter(
            organization=org, cycles__in=cycles_qs
        )
        .filter(
            Q(organizational_cycle__isnull=True)
            | Q(organizational_cycle__status='active')
        )
        .select_related('team', 'individual', 'questionnaire', 'organizational_cycle')
        .annotate(
            cycle_count=Count('cycles', distinct=True),
            invitation_count=Count('cycles__tokens', distinct=True),
            completed_invitation_count=Count(
                'cycles__tokens',
                filter=Q(cycles__tokens__completed_at__isnull=False),
                distinct=True,
            ),
        )
        .distinct()
        .order_by('-created_at')
    )
    active_campaigns = []
    completed_campaigns = []
    organizational_cycle_cards = {}
    reviewee_names_by_email = {
        email.lower(): name
        for email, name in Reviewee.objects.for_organization(org).filter(
            is_active=True
        ).exclude(email='').values_list('email', 'name')
        if email
    }
    today = timezone.localdate()
    for campaign in campaigns_qs:
        days_remaining = (
            (campaign.due_date - today).days if campaign.due_date else None
        )
        can_view_campaign_progress = _can_view_campaign_progress(
            request.user, campaign, org
        )
        scoped_cycles = visible_cycles(
            request.user,
            campaign.cycles.select_related('reviewee', 'reviewee__team').prefetch_related(
                'tokens__assigned_team', 'report', 'reviewee__team_memberships__team'
            ),
        ).order_by('reviewee__name')
        if not can_view_campaign_progress:
            scoped_cycles = scoped_cycles.filter(
                reviewee__email__iexact=request.user.email
            )
        people = []
        all_complete = True
        scoped_completed = 0
        scoped_total = 0
        for cycle in scoped_cycles:
            tokens = list(cycle.tokens.all())
            team_names = {
                membership.team.name
                for membership in cycle.reviewee.team_memberships.all()
            }
            if cycle.reviewee.team:
                team_names.add(cycle.reviewee.team.name)
            completed_count = sum(token.is_completed for token in tokens)
            if cycle.status != 'completed':
                all_complete = False
            scoped_completed += completed_count
            scoped_total += len(tokens)
            people.append({
                'cycle': cycle,
                'is_own_cycle': bool(request.user.email) and (
                    cycle.reviewee.email.lower() == request.user.email.lower()
                ),
                'tokens': tokens,
                'team_names': sorted(team_names),
                'assigned_team_names': sorted({
                    token.assigned_team.name
                    for token in tokens if token.assigned_team_id
                }),
                'reviewers': [
                    {
                        'token': token,
                        'name': reviewee_names_by_email.get(
                            (token.reviewer_email or '').lower(),
                            token.reviewer_email or 'Reviewer not assigned',
                        ),
                    }
                    for token in tokens
                ],
                'completed_count': completed_count,
                'total_count': len(tokens),
                'completion_rate': (
                    completed_count / len(tokens) * 100 if tokens else 0
                ),
                'awaiting_nominations': campaign.cycle_type == 'peer' and not tokens,
                'report': getattr(cycle, 'report', None),
            })
        card = {
            'campaign': campaign,
            'people': people,
            'can_manage': can_view_campaign_progress,
            'days_remaining': days_remaining,
            'due_soon': days_remaining is not None and 0 <= days_remaining <= 7,
            'overdue': days_remaining is not None and days_remaining < 0,
            'completed_count': scoped_completed,
            'total_count': scoped_total,
            'completion_rate': scoped_completed / scoped_total * 100 if scoped_total else 0,
        }
        if (
            card['can_manage']
            and campaign.status == 'active'
            and campaign.cycle_type != 'peer'
        ):
            card['add_candidates'] = _campaign_add_candidates(campaign)
        if campaign.organizational_cycle_id:
            if card['can_manage']:
                if campaign.organizational_cycle.status == 'active':
                    group = organizational_cycle_cards.setdefault(
                        campaign.organizational_cycle_id,
                        {
                            'cycle': campaign.organizational_cycle,
                            'campaigns': [],
                            'completed_count': 0,
                            'total_count': 0,
                            'is_organization_admin': request.user.has_perm(
                                'accounts.can_manage_organization'
                            ),
                            'is_team_leader': led_teams(request.user, org).exists(),
                        },
                    )
                    group['campaigns'].append(card)
                    group['completed_count'] += card['completed_count']
                    group['total_count'] += sum(
                        max(person['total_count'], 1) for person in people
                    )
                # Completed organisation cycles belong in Cycles and Reports,
                # not among the manager's active dashboard work.
                continue
            if campaign.organizational_cycle.status != 'active':
                continue
        if people and all_complete:
            completed_campaigns.append(card)
        else:
            active_campaigns.append(card)

    pending_peer_nominations = cycles_qs.filter(
        status='active',
        campaign__cycle_type='peer',
        campaign__status='active',
        reviewee__email__iexact=request.user.email,
        tokens__isnull=True,
    ).filter(
        Q(campaign__organizational_cycle__isnull=True)
        | Q(campaign__organizational_cycle__status='active')
    ).select_related(
        'campaign__created_by', 'campaign__organizational_cycle', 'questionnaire'
    ).prefetch_related('reviewee__team_memberships__team')

    my_review_tasks = ReviewerToken.objects.filter(
        cycle__reviewee__organization=org,
        cycle__status='active',
        reviewer_email__iexact=request.user.email,
        completed_at__isnull=True,
    ).filter(
        Q(cycle__campaign__isnull=True)
        | Q(cycle__campaign__status='active')
    ).filter(
        Q(cycle__campaign__organizational_cycle__isnull=True)
        | Q(cycle__campaign__organizational_cycle__status='active')
    ).select_related(
        'cycle__reviewee', 'cycle__questionnaire', 'cycle__created_by',
        'cycle__campaign__created_by', 'cycle__campaign__organizational_cycle',
        'cycle__campaign__team', 'assigned_team', 'cycle__reviewee__team',
    ).prefetch_related(
        'cycle__reviewee__team_memberships__team'
    ).order_by('cycle__due_date', 'created_at') if request.user.email else ReviewerToken.objects.none()

    # Completion stats for active cycles
    active_cycles_data = []
    for cycle in cycles_qs.filter(
        status='active', campaign__isnull=True
    ).select_related('reviewee'):
        total_tokens = cycle.tokens.count()
        completed_tokens = cycle.tokens.filter(completed_at__isnull=False).count()
        completion_rate = (completed_tokens / total_tokens * 100) if total_tokens > 0 else 0

        active_cycles_data.append({
            'cycle': cycle,
            'total_tokens': total_tokens,
            'completed_tokens': completed_tokens,
            'completion_rate': completion_rate,
        })

    # Completed cycles with report availability
    completed_cycles_data = []

    # Check if user has seen welcome modal
    has_seen_welcome = False
    try:
        has_seen_welcome = request.user.profile.has_seen_welcome
    except UserProfile.DoesNotExist:
        pass

    # Check if user has submitted a product review (global, not org-scoped)
    from productreviews.models import ProductReview
    user_has_reviewed = ProductReview.objects.filter(
        reviewer_email=request.user.email,
        is_active=True
    ).exists()

    context = {
        'total_reviewees': total_reviewees,
        'active_cycles': active_cycles,
        'completed_cycles': completed_cycles,
        'pending_tokens': pending_tokens,
        'recent_reports': recent_reports,
        'active_cycles_data': active_cycles_data,
        'completed_cycles_data': completed_cycles_data,
        'active_campaigns': active_campaigns,
        'completed_campaigns': completed_campaigns,
        'organizational_cycle_cards': [
            {
                **group,
                'completion_rate': (
                    group['completed_count'] / group['total_count'] * 100
                    if group['total_count'] else 0
                ),
            }
            for group in organizational_cycle_cards.values()
        ],
        'pending_peer_nominations': pending_peer_nominations,
        'my_review_tasks': my_review_tasks,
        'subscription_status': subscription_status,
        'has_seen_welcome': has_seen_welcome,
        'user_has_reviewed': user_has_reviewed,
    }

    return render(request, 'admin_dashboard/dashboard.html', context)


@login_required
def team_list(request):
    """Team management - users and invitations"""
    from subscriptions.utils import get_subscription_status

    org = request.organization

    if not org:
        messages.error(request, 'No organization found.')
        return redirect('admin_dashboard')

    # Get all active (non-anonymized) users in this organization
    users_qs = visible_profiles(
        request.user,
        UserProfile.objects.for_organization(org).select_related('user'),
        org,
    ).order_by('-user__date_joined')

    # Get per_page from request, default to 25
    per_page = request.GET.get('per_page', '25')
    try:
        per_page = int(per_page)
        if per_page not in [25, 50, 100]:
            per_page = 25
    except (ValueError, TypeError):
        per_page = 25

    # Paginate users
    paginator = Paginator(users_qs, per_page)
    page = request.GET.get('page')
    try:
        users = paginator.page(page)
    except PageNotAnInteger:
        users = paginator.page(1)
    except EmptyPage:
        users = paginator.page(paginator.num_pages)

    # Add permission data as dynamic attribute
    for user_profile in users:
        user_profile.is_org_admin = user_profile.user.has_perm('accounts.can_manage_organization')

    # Get pending invitations
    invitations = visible_invitations(
        request.user,
        OrganizationInvitation.objects.filter(organization=org, accepted_at__isnull=True),
        org,
    ).order_by('-created_at')

    is_org_admin = request.user.has_perm('accounts.can_manage_organization')
    all_teams = list(Team.objects.for_organization(org).select_related('parent', 'manager__user'))
    led_team_ids = set(
        led_teams(request.user, org).values_list('id', flat=True)
    )
    if is_org_admin:
        visible_team_ids = {team.id for team in all_teams}
    else:
        profile = request.user.profile
        visible_team_ids = {team.id for team in all_teams if team.manager_id == profile.id}
        for grant in profile.team_lead_grants.select_related('team').prefetch_related('revocations'):
            grant_ids = (descendant_team_ids(grant.team) if grant.include_descendants
                         else {grant.team_id})
            for revocation in grant.revocations.all():
                grant_ids -= descendant_team_ids(revocation.team)
            visible_team_ids.update(grant_ids)
        own_reviewee = Reviewee.objects.for_organization(org).filter(
            Q(profile=profile) | Q(email__iexact=request.user.email)
        ).first()
        if own_reviewee:
            visible_team_ids.update(
                own_reviewee.team_memberships.values_list('team_id', flat=True)
            )
            if own_reviewee.team_id:
                visible_team_ids.add(own_reviewee.team_id)

        # Preserve the hierarchy so a nested team is always shown beneath its parents.
        teams_by_id = {team.id: team for team in all_teams}
        for team_id in list(visible_team_ids):
            parent_id = teams_by_id.get(team_id).parent_id if teams_by_id.get(team_id) else None
            while parent_id:
                visible_team_ids.add(parent_id)
                parent_id = teams_by_id[parent_id].parent_id

    roster_reviewees = list(Reviewee.objects.for_organization(org).filter(
        profile__isnull=False,
    ).filter(
        Q(team__isnull=False) | Q(team_memberships__isnull=False)
    ).select_related('profile__user', 'team').prefetch_related('team_memberships').distinct())
    pending_invitations = OrganizationInvitation.objects.filter(
        organization=org, accepted_at__isnull=True, team__isnull=False
    ).select_related('team')
    team_cards_by_id = {}
    team_leader_ids = {team.id: set() for team in all_teams}
    for team in all_teams:
        if team.manager_id:
            team_leader_ids[team.id].add(team.manager_id)
    for grant in TeamLeadGrant.objects.filter(
        team__organization=org
    ).prefetch_related('revocations__team'):
        granted_ids = (
            descendant_team_ids(grant.team)
            if grant.include_descendants else {grant.team_id}
        )
        for revocation in grant.revocations.all():
            granted_ids -= descendant_team_ids(revocation.team)
        for team_id in granted_ids:
            if team_id in team_leader_ids:
                team_leader_ids[team_id].add(grant.profile_id)
    for team in all_teams:
        if team.id not in visible_team_ids:
            continue
        members = [
            {'name': r.name, 'email': r.email, 'status': 'active', 'profile': r.profile,
             'reviewee': r, 'is_org_admin': r.profile.user.has_perm(
                 'accounts.can_manage_organization'
             ), 'is_manager': r.profile_id == team.manager_id}
            for r in roster_reviewees if (
                r.team_id == team.id
                or any(m.team_id == team.id for m in r.team_memberships.all())
            )
        ]
        for member in members:
            member['is_team_leader'] = (
                member['profile'].id in team_leader_ids[team.id]
            )
        if team.manager_id and not any(
            member['profile'].id == team.manager_id for member in members
        ):
            manager_reviewee = Reviewee.objects.for_organization(org).filter(
                Q(profile=team.manager)
                | Q(email__iexact=team.manager.user.email)
            ).select_related('profile__user').first()
            if manager_reviewee:
                members.insert(0, {
                    'name': manager_reviewee.name,
                    'email': manager_reviewee.email,
                    'status': 'active',
                    'profile': team.manager,
                    'reviewee': manager_reviewee,
                    'is_org_admin': team.manager.user.has_perm(
                        'accounts.can_manage_organization'
                    ),
                    'is_manager': True,
                    'is_team_leader': True,
                })
        members.extend({
            'name': f'{invite.first_name} {invite.last_name}'.strip() or invite.email,
            'email': invite.email, 'status': 'pending', 'profile': None,
            'reviewee': None, 'invitation': invite,
        } for invite in pending_invitations if invite.team_id == team.id)
        team_cards_by_id[team.id] = {
            'team': team,
            'members': members,
            'can_edit': is_org_admin or team.id in led_team_ids,
            'manager_name': (
                team.manager.user.get_full_name() or team.manager.user.email
                if team.manager else 'Not assigned'
            ),
            'children': [],
        }
    team_tree = []
    for card in team_cards_by_id.values():
        parent_card = team_cards_by_id.get(card['team'].parent_id)
        if parent_card:
            parent_card['children'].append(card)
        else:
            team_tree.append(card)

    current_reviewee = Reviewee.objects.for_organization(org).filter(
        Q(profile__user=request.user) | Q(email__iexact=request.user.email)
    ).select_related('team').prefetch_related('teams').first()
    current_teams = list(current_reviewee.teams.all()) if current_reviewee else []
    if current_reviewee and current_reviewee.team and all(
        team.id != current_reviewee.team_id for team in current_teams
    ):
        current_teams.insert(0, current_reviewee.team)

    # Get subscription status
    subscription_status = get_subscription_status(org) if org else None

    context = {
        'users': users,
        'invitations': invitations,
        'subscription_status': subscription_status,
        'per_page': per_page,
        'organization': org,
        'organization_teams': Team.objects.for_organization(org).select_related('parent'),
        'current_reviewee': current_reviewee,
        'current_teams': current_teams,
        'current_team_ids': {team.id for team in current_teams},
        'team_cards': list(team_cards_by_id.values()),
        'team_tree': team_tree,
        'can_edit_any_team': is_org_admin or any(
            card['can_edit'] for card in team_cards_by_id.values()
        ),
        'editing_team_id': request.GET.get('edit', ''),
        'show_create_team': request.GET.get('create') == '1',
        'pending_peer_cycle': ReviewCycle.objects.filter(
            reviewee__organization=org,
            reviewee__email__iexact=request.user.email,
            campaign__cycle_type='peer',
            campaign__status='active',
            status='active',
        ).order_by('-created_at').first(),
    }
    if is_org_admin:
        context.update({
            'organization_profiles': UserProfile.objects.for_organization(org).select_related('user'),
            'organization_reviewees': Reviewee.objects.for_organization(org).select_related(
                'profile__user', 'team', 'reporting_manager__user'
            ).order_by('name'),
            'team_lead_grants': TeamLeadGrant.objects.filter(
                team__organization=org
            ).select_related('profile__user', 'team').prefetch_related('revocations__team'),
        })
    else:
        # Managers may assign unassigned people or move people between teams
        # that they manage. Do not expose members of unrelated teams in the
        # edit control.
        context['organization_reviewees'] = Reviewee.objects.for_organization(org).filter(
            Q(team__isnull=True)
            | Q(team__manager=request.user.profile)
            | Q(team_memberships__team__manager=request.user.profile)
        ).select_related('profile__user', 'team').prefetch_related('teams').distinct().order_by('name')

    return render(request, 'admin_dashboard/team.html', context)


@login_required
@require_POST
def manage_team_structure(request):
    """Manage hierarchy, person assignments, lead grants, and revocations."""
    org = request.organization
    if not org:
        raise Http404

    action = request.POST.get('action')
    is_org_admin = request.user.has_perm('accounts.can_manage_organization')
    led_team_ids = set(
        led_teams(request.user, org).values_list('id', flat=True)
    )
    if not is_org_admin and action not in {
        'set_member_team', 'cancel_team_invitation', 'update_team', 'delete_team'
    }:
        raise PermissionDenied
    try:
        with transaction.atomic():
            if action == 'create_team':
                parent_id = request.POST.get('parent') or None
                manager = get_object_or_404(
                    UserProfile, organization=org, pk=request.POST.get('manager')
                )
                team = Team(
                    organization=org,
                    name=request.POST.get('name', '').strip(),
                    parent=(get_object_or_404(Team, organization=org, pk=parent_id)
                            if parent_id else None),
                    manager=manager,
                )
                team.full_clean()
                team.save()
                messages.success(request, f'Team “{team.name}” created.')

            elif action == 'update_team':
                team = get_object_or_404(
                    Team.objects.for_organization(org), pk=request.POST.get('team')
                )
                if not is_org_admin and team.id not in led_team_ids:
                    raise PermissionDenied
                parent_id = request.POST.get('parent') or None
                team.name = request.POST.get('name', '').strip()
                team.parent = (get_object_or_404(Team, organization=org, pk=parent_id)
                               if parent_id else None)
                if is_org_admin:
                    team.manager = get_object_or_404(
                        UserProfile, organization=org, pk=request.POST.get('manager')
                    )
                team.full_clean()
                team.save()
                messages.success(request, f'Team “{team.name}” updated.')

            elif action == 'delete_team':
                team = Team.objects.for_organization(org).filter(
                    pk=request.POST.get('team')
                ).first()
                if not team:
                    raise ValidationError(
                        'That team no longer exists. Refresh the page and try again.'
                    )
                if not is_org_admin and team.id not in led_team_ids:
                    raise PermissionDenied
                if team.children.exists():
                    raise ValidationError(
                        'Move or remove this team’s subteams before deleting it.'
                    )
                if request.POST.get('confirmation', '').strip().lower() != 'delete':
                    raise ValidationError('Type delete to confirm team removal.')
                team_name = team.name
                # In-progress work is intentionally removed. Completed campaigns
                # retain this archived team reference for audit/GDPR history.
                team.review_campaigns.exclude(status='completed').delete()
                team.archived_at = timezone.now()
                team.save(update_fields=['archived_at', 'updated_at'])
                Reviewee.objects.filter(team=team).update(team=None)
                TeamMembership.objects.filter(team=team).delete()
                TeamLeadGrant.objects.filter(team=team).delete()
                TeamLeadRevocation.objects.filter(team=team).delete()
                messages.success(
                    request,
                    f'Team “{team_name}” removed. Active campaigns were deleted '
                    'and completed review history was retained.'
                )

            elif action == 'set_member_team':
                reviewee = get_object_or_404(
                    Reviewee.objects.for_organization(org).select_related('team'),
                    pk=request.POST.get('reviewee'),
                )
                team_id = request.POST.get('team') or None
                remove_team_id = request.POST.get('remove_from_team') or (
                    reviewee.team_id if not team_id else None
                )
                destination = (get_object_or_404(Team, organization=org, pk=team_id)
                               if team_id else None)
                removal_team = (get_object_or_404(
                    Team, organization=org, pk=remove_team_id
                ) if remove_team_id else None)
                if removal_team and request.POST.get(
                    'confirmation', ''
                ).strip().lower() != 'delete':
                    raise ValidationError('Type delete to confirm removing this team member.')
                if not destination and not removal_team:
                    raise ValidationError('Select a team membership to add or remove.')
                manages_current = removal_team and removal_team.id in led_team_ids
                manages_destination = destination and destination.id in led_team_ids
                if not is_org_admin:
                    existing_team_ids = set(
                        reviewee.team_memberships.values_list('team_id', flat=True)
                    )
                    if reviewee.team_id:
                        existing_team_ids.add(reviewee.team_id)
                    manages_existing = Team.objects.filter(
                        id__in=existing_team_ids, manager=request.user.profile
                    ).exists()
                    can_add = bool(manages_destination) and (
                        not existing_team_ids or manages_existing
                    )
                    if not (manages_current or can_add):
                        raise PermissionDenied
                if destination:
                    TeamMembership.objects.get_or_create(
                        reviewee=reviewee, team=destination
                    )
                    if reviewee.team_id is None:
                        reviewee.team = destination
                        reviewee.save(update_fields=['team', 'updated_at'])
                    messages.success(
                        request, f'{reviewee.name} added to {destination.name}.'
                    )
                else:
                    TeamMembership.objects.filter(
                        reviewee=reviewee, team=removal_team
                    ).delete()
                    if reviewee.team_id == removal_team.id:
                        replacement = reviewee.team_memberships.select_related(
                            'team'
                        ).first()
                        reviewee.team = replacement.team if replacement else None
                        reviewee.save(update_fields=['team', 'updated_at'])
                    messages.success(
                        request, f'{reviewee.name} removed from {removal_team.name}.'
                    )

            elif action == 'cancel_team_invitation':
                invitation = get_object_or_404(
                    OrganizationInvitation.objects.select_related('team'),
                    pk=request.POST.get('invitation'),
                    organization=org,
                    accepted_at__isnull=True,
                )
                if not is_org_admin and (
                    not invitation.team
                    or invitation.team.manager_id != request.user.profile.id
                ):
                    raise PermissionDenied
                email = invitation.email
                invitation.delete()
                messages.success(request, f'Pending invitation for {email} removed.')

            elif action == 'assign_reviewee':
                reviewee = get_object_or_404(
                    Reviewee.objects.for_organization(org), pk=request.POST.get('reviewee')
                )
                team_id = request.POST.get('team') or None
                manager_id = request.POST.get('reporting_manager') or None
                profile_id = request.POST.get('profile') or None
                reviewee.team = (get_object_or_404(Team, organization=org, pk=team_id)
                                  if team_id else None)
                reviewee.reporting_manager = (get_object_or_404(
                    UserProfile, organization=org, pk=manager_id
                ) if manager_id else None)
                reviewee.profile = (get_object_or_404(
                    UserProfile, organization=org, pk=profile_id
                ) if profile_id else None)
                reviewee.full_clean()
                reviewee.save(update_fields=['team', 'reporting_manager', 'profile', 'updated_at'])
                messages.success(request, f'Assignments updated for {reviewee.name}.')

            elif action == 'save_grant':
                profile = get_object_or_404(
                    UserProfile, organization=org, pk=request.POST.get('profile')
                )
                team = get_object_or_404(Team, organization=org, pk=request.POST.get('team'))
                grant, _ = TeamLeadGrant.objects.get_or_create(profile=profile, team=team)
                grant.include_descendants = request.POST.get('include_descendants') == 'on'
                grant.full_clean()
                grant.save()
                messages.success(request, 'Team lead grant saved.')

            elif action == 'delete_grant':
                grant = get_object_or_404(
                    TeamLeadGrant, pk=request.POST.get('grant'), team__organization=org
                )
                grant.delete()
                messages.success(request, 'Team lead grant removed.')

            elif action == 'add_revocation':
                grant = get_object_or_404(
                    TeamLeadGrant, pk=request.POST.get('grant'), team__organization=org
                )
                team = get_object_or_404(Team, organization=org, pk=request.POST.get('team'))
                if not grant.include_descendants or team.id not in descendant_team_ids(
                    grant.team, include_self=False
                ):
                    raise ValidationError('Only a descendant inherited by this grant can be revoked.')
                revocation = TeamLeadRevocation(grant=grant, team=team)
                revocation.full_clean()
                revocation.save()
                messages.success(request, f'Access to “{team.name}” and its subtree revoked.')

            elif action == 'delete_revocation':
                revocation = get_object_or_404(
                    TeamLeadRevocation,
                    pk=request.POST.get('revocation'),
                    grant__team__organization=org,
                )
                revocation.delete()
                messages.success(request, 'Team revocation removed.')
            else:
                raise ValidationError('Unknown team management action.')
    except ValidationError as exc:
        messages.error(request, '; '.join(exc.messages))
    return redirect('team_list')


@login_required
@require_POST
def update_user_permissions(request):
    """Update user permissions and role"""
    from accounts.permissions import assign_organization_admin, assign_organization_member
    from django.contrib.auth.models import Group

    # Check if requester has permission to manage organization
    if not request.user.has_perm('accounts.can_manage_organization'):
        messages.error(request, 'You do not have permission to manage user permissions.')
        return redirect('team_list')

    org = request.organization
    if not org:
        messages.error(request, 'No organization found.')
        return redirect('admin_dashboard')

    try:
        user_profile_id = request.POST.get('user_profile_id')
        role = request.POST.get('role')  # 'admin' or 'member'
        can_create_cycles_for_others = request.POST.get('can_create_cycles_for_others') == 'on'

        if not user_profile_id or not role:
            messages.error(request, 'Invalid request: missing required fields.')
            return redirect('team_list')

        # Get the user profile being updated
        user_profile = get_object_or_404(
            UserProfile,
            id=user_profile_id,
            organization=org
        )
        target_user = user_profile.user

        # Prevent self-demotion or demoting superusers
        if target_user.id == request.user.id:
            messages.error(request, 'You cannot modify your own permissions.')
            return redirect('team_list')

        if target_user.is_superuser:
            messages.error(request, 'Cannot modify permissions for super admins.')
            return redirect('team_list')

        # Check if this would be the last admin
        if target_user.has_perm('accounts.can_manage_organization') and role == 'member':
            # Count users with organization admin permission
            admin_profiles = UserProfile.objects.filter(organization=org).select_related('user')
            admin_count = sum(1 for p in admin_profiles if p.user.has_perm('accounts.can_manage_organization'))

            if admin_count <= 1:
                messages.error(request, 'Cannot demote the last organization administrator.')
                return redirect('team_list')

        # Update role and permissions
        if role == 'admin':
            assign_organization_admin(target_user)
            messages.success(request, f'Successfully promoted {target_user.username} to Organisation Admin.')
        else:  # member
            # Remove admin permissions
            assign_organization_member(target_user, can_create_cycles_for_others=False)
            messages.success(request, f'Successfully updated {target_user.username} to Member role.')

        # Update can_create_cycles_for_others permission separately
        # (this can be set independently of role)
        user_profile.refresh_from_db()
        user_profile.can_create_cycles_for_others = can_create_cycles_for_others
        user_profile.save()

        if can_create_cycles_for_others:
            messages.success(request, f'{target_user.username} can now create review cycles for others.')

    except Exception as e:
        messages.error(request, f'Error updating permissions: {str(e)}')

    return redirect('team_list')


@login_required
def reviewee_list(request):
    """List and manage reviewees"""
    from subscriptions.utils import get_subscription_status
    from questionnaires.models import Questionnaire

    org = request.organization
    # Filter out anonymized reviewees (those with @deleted.invalid emails)
    reviewees_qs = visible_reviewees(
        request.user, Reviewee.objects.for_organization(org).filter(is_active=True), org
    ).annotate(
        cycle_count=Count('review_cycles')
    ).order_by('name')

    # Get per_page from request, default to 25
    per_page = request.GET.get('per_page', '25')
    try:
        per_page = int(per_page)
        if per_page not in [25, 50, 100]:
            per_page = 25
    except (ValueError, TypeError):
        per_page = 25

    # Paginate reviewees
    paginator = Paginator(reviewees_qs, per_page)
    page = request.GET.get('page')
    try:
        reviewees = paginator.page(page)
    except PageNotAnInteger:
        reviewees = paginator.page(1)
    except EmptyPage:
        reviewees = paginator.page(paginator.num_pages)

    # Get subscription status
    subscription_status = get_subscription_status(org) if org else None

    # Get available questionnaires for quick cycle creation
    questionnaires = Questionnaire.objects.for_organization(org).filter(is_active=True).order_by('-is_default', 'name')

    # Annotate each reviewee with their latest cycle info
    reviewees_with_latest = []
    for reviewee in reviewees:
        latest_cycle = (
            reviewee.review_cycles
            .select_related('questionnaire')
            .prefetch_related('questionnaire__sections__questions')
            .order_by('-created_at')
            .first()
        )

        # Get active cycle
        active_cycle = reviewee.review_cycles.filter(status='active').order_by('-created_at').first()

        # Get latest completed cycle with a report
        latest_completed_report = None
        completed_cycles = reviewee.review_cycles.filter(status='completed').order_by('-created_at')
        for cycle in completed_cycles:
            try:
                report = cycle.report
                if report.available:
                    latest_completed_report = report
                    break
            except:
                continue

        reviewees_with_latest.append({
            'reviewee': reviewee,
            'latest_questionnaire': latest_cycle.questionnaire if latest_cycle else None,
            'active_cycle': active_cycle,
            'latest_completed_report': latest_completed_report,
        })

    context = {
        'reviewees_with_latest': reviewees_with_latest,
        'reviewees': reviewees,  # Paginated object
        'questionnaires': questionnaires,
        'subscription_status': subscription_status,
        'per_page': per_page,
    }

    return render(request, 'admin_dashboard/reviewee_list.html', context)


@login_required
def reviewee_create(request):
    """Create a new reviewee"""
    from subscriptions.utils import check_employee_limit
    from accounts.permissions import is_organization_admin

    if request.method == 'POST':
        name = request.POST.get('name')
        email = request.POST.get('email')
        department = request.POST.get('department', '')

        if name and email:
            organization = request.organization or Organization.objects.first()
            if not organization:
                messages.error(request, 'No organization found. Please run setup first.')
                return redirect('admin_dashboard')

            # Non-admins can only create reviewees for themselves
            if not is_organization_admin(request.user):
                if email.lower() != request.user.email.lower():
                    messages.error(request, 'You can only create a reviewee profile for yourself.')
                    return redirect('reviewee_list')

            # Check employee limit
            allowed, error_message = check_employee_limit(request)
            if not allowed:
                messages.error(request, error_message)
                return redirect('reviewee_list')

            try:
                reviewee = Reviewee.objects.create(
                    organization=organization,
                    name=name,
                    email=email,
                    department=department
                )
                messages.success(request, f'Reviewee "{reviewee.name}" created successfully.')
                return redirect('reviewee_list')
            except Exception as e:
                messages.error(request, f'Error creating reviewee: {str(e)}')
        else:
            messages.error(request, 'Name and email are required.')

    return render(request, 'admin_dashboard/reviewee_form.html', {'action': 'Create'})


@login_required
def reviewee_edit(request, reviewee_id):
    """Edit an existing reviewee - admin only"""
    from accounts.permissions import organization_admin_required

    # Check admin permission
    if not request.user.has_perm('accounts.can_manage_organization'):
        messages.error(
            request,
            'You do not have permission to edit reviewees. Only organization administrators can access this feature.'
        )
        return redirect('reviewee_list')

    reviewee = get_object_or_404(visible_reviewees(
        request.user, Reviewee.objects.for_organization(request.organization), request.organization
    ), id=reviewee_id)

    if request.method == 'POST':
        reviewee.name = request.POST.get('name', reviewee.name)
        reviewee.email = request.POST.get('email', reviewee.email)
        reviewee.department = request.POST.get('department', '')

        try:
            reviewee.save()
            messages.success(request, f'Reviewee "{reviewee.name}" updated successfully.')
            return redirect('reviewee_list')
        except Exception as e:
            messages.error(request, f'Error updating reviewee: {str(e)}')

    context = {
        'reviewee': reviewee,
        'action': 'Edit',
    }

    return render(request, 'admin_dashboard/reviewee_form.html', context)


@login_required
def reviewee_delete(request, reviewee_id):
    """Soft delete a reviewee - admin only"""
    from accounts.permissions import organization_admin_required

    # Check admin permission
    if not request.user.has_perm('accounts.can_manage_organization'):
        messages.error(
            request,
            'You do not have permission to delete reviewees. Only organization administrators can access this feature.'
        )
        return redirect('reviewee_list')

    reviewee = get_object_or_404(visible_reviewees(
        request.user, Reviewee.objects.for_organization(request.organization), request.organization
    ), id=reviewee_id)

    if request.method == 'POST':
        reviewee.is_active = False
        reviewee.save()
        messages.success(request, f'Reviewee "{reviewee.name}" deactivated.')
        return redirect('reviewee_list')

    context = {
        'reviewee': reviewee,
    }

    return render(request, 'admin_dashboard/reviewee_confirm_delete.html', context)


@login_required
@require_POST
def quick_cycle_create(request, reviewee_id):
    """
    Quick cycle creation from reviewee/cycle list.
    Creates a cycle based on a specified source cycle or the most recent cycle for this reviewee.
    Copies token structure and email assignments from the source cycle.
    If no previous cycle exists, creates default tokens (1 self, 3 peers, 1 manager, 0 direct reports).
    """
    from accounts.permissions import organization_admin_required

    # Check admin permission
    if not request.user.has_perm('accounts.can_manage_organization'):
        messages.error(
            request,
            'You do not have permission to create review cycles. Only organization administrators can access this feature.'
        )
        return redirect('reviewee_list')

    org = request.organization
    reviewee = get_object_or_404(visible_reviewees(
        request.user, Reviewee.objects.for_organization(org), org
    ), id=reviewee_id, is_active=True)
    questionnaire_id = request.POST.get('questionnaire_id')
    source_cycle_uuid = request.POST.get('source_cycle_uuid')  # Optional: specific cycle to copy from

    if not questionnaire_id:
        messages.error(request, 'Questionnaire is required.')
        return redirect('reviewee_list')

    try:
        questionnaire = Questionnaire.objects.get(id=questionnaire_id, organization=org)
    except Questionnaire.DoesNotExist:
        messages.error(request, 'Invalid questionnaire selected.')
        return redirect('reviewee_list')

    # Create the cycle
    cycle = ReviewCycle.objects.create(
        reviewee=reviewee,
        questionnaire=questionnaire,
        created_by=request.user,
        status='active'
    )

    # Get the cycle to copy from
    if source_cycle_uuid:
        # Copy from specific cycle if provided
        try:
            previous_cycle = ReviewCycle.objects.get(uuid=source_cycle_uuid, reviewee=reviewee)
        except ReviewCycle.DoesNotExist:
            previous_cycle = None
    else:
        # Otherwise, get the most recent previous cycle for this reviewee
        previous_cycle = reviewee.review_cycles.exclude(id=cycle.id).order_by('-created_at').first()

    total_tokens = 0
    email_invited_count = 0

    if previous_cycle:
        # Copy tokens from previous cycle, including email assignments
        previous_tokens = previous_cycle.tokens.all()

        for prev_token in previous_tokens:
            new_token = ReviewerToken.objects.create(
                cycle=cycle,
                category=prev_token.category,
                reviewer_email=prev_token.reviewer_email
            )
            total_tokens += 1
            if prev_token.reviewer_email:
                email_invited_count += 1

        # Send invitations to all email-assigned tokens
        if email_invited_count > 0:
            send_stats = send_reviewer_invitations(cycle)

            messages.success(
                request,
                f'Review cycle created for "{reviewee.name}" using "{questionnaire.name}". '
                f'Copied {total_tokens} reviewer(s) from previous cycle. '
                f'{send_stats["sent"]} email invitation(s) sent.'
            )
        else:
            messages.success(
                request,
                f'Review cycle created for "{reviewee.name}" using "{questionnaire.name}" with {total_tokens} reviewer token(s). '
                f'Go to the cycle details to assign reviewers and send invitations.'
            )
    else:
        # No previous cycle - use default token distribution
        token_distribution = [
            ('self', 1),
            ('peer', 3),
            ('manager', 1),
            ('direct_report', 0),
        ]

        for category, count in token_distribution:
            for _ in range(count):
                ReviewerToken.objects.create(
                    cycle=cycle,
                    category=category
                )
                total_tokens += 1

        messages.success(
            request,
            f'Review cycle created for "{reviewee.name}" using "{questionnaire.name}" with {total_tokens} reviewer token(s). '
            f'Go to the cycle details to assign reviewers and send invitations.'
        )

    # Redirect to cycle detail page
    return redirect('review_cycle_detail', cycle_uuid=cycle.uuid)


@login_required
def questionnaire_list(request):
    """List available questionnaires"""
    from django.db.models import Subquery, OuterRef
    from questionnaires.models import Question

    org = request.organization

    # Subquery to count questions correctly
    question_count_subquery = Question.objects.filter(
        section__questionnaire=OuterRef('pk')
    ).values('section__questionnaire').annotate(
        count=Count('id')
    ).values('count')

    # Only show questionnaires belonging to the user's organization
    # Template questionnaires (organization=None) are not shown here as they're internal
    questionnaires_qs = Questionnaire.objects.filter(
        organization=org
    ) if org else Questionnaire.objects.filter(organization__isnull=False)

    questionnaires = questionnaires_qs.annotate(
        question_count=Subquery(question_count_subquery),
        cycle_count=Count('review_cycles')
    ).prefetch_related(
        'sections__questions'
    ).order_by('-is_default', 'name')

    for questionnaire in questionnaires:
        questionnaire.can_edit = can_edit_questionnaire(request.user, questionnaire)

    context = {
        'questionnaires': questionnaires,
    }

    return render(request, 'admin_dashboard/questionnaire_list.html', context)


@login_required
def questionnaire_preview(request, questionnaire_id):
    """Preview a questionnaire"""
    questionnaire = get_object_or_404(
        Questionnaire, id=questionnaire_id, organization=request.organization
    )
    sections = questionnaire.sections.prefetch_related('questions').all()

    context = {
        'questionnaire': questionnaire,
        'sections': sections,
    }

    return render(request, 'admin_dashboard/questionnaire_preview.html', context)


@login_required
def questionnaire_sample_report(request, questionnaire_id):
    """Render an illustrative sample report for a questionnaire.

    Builds a synthetic but shape-correct `insights` and `charts` data set so we
    can reuse the real Dreyfus diamond + agency SVGs and Chart.js radar/gap
    graphs. Seeded by questionnaire id so reloads look identical.
    """
    import random
    from reports.dreyfus_service import (
        DREYFUS_STAGES,
        AGENCY_STAGES,
        QUADRANTS,
        calculate_dreyfus_quadrant,
        _level_to_stage,
        _get_development_focus,
    )

    questionnaire = get_object_or_404(
        Questionnaire.objects.prefetch_related('sections__questions'),
        id=questionnaire_id,
        organization=request.organization,
    )

    rng = random.Random(questionnaire.id)

    def score(center, spread=0.35):
        value = rng.uniform(center - spread, center + spread)
        return round(max(1.0, min(5.0, value)), 1)

    # Per-section scores, shaped exactly like what chart.js expects.
    section_scores = {}
    sections_data = []
    overall_totals = []
    for section in questionnaire.sections.all():
        base = rng.uniform(3.4, 4.4)
        cats = {
            'self': score(base - 0.2),
            'peer': score(base + 0.05),
            'manager': score(base + 0.15),
            'direct_report': score(base),
        }
        cats['others_avg'] = round(
            (cats['peer'] + cats['manager'] + cats['direct_report']) / 3, 1
        )
        section_scores[section.title] = cats
        sections_data.append({
            'title': section.title,
            'overall_score': cats['others_avg'],
            'category_scores': [
                ('self', cats['self']),
                ('peer', cats['peer']),
                ('manager', cats['manager']),
                ('direct_report', cats['direct_report']),
            ],
            'question_count': section.questions.count(),
        })
        overall_totals.append(cats['others_avg'])

    overall_score = round(sum(overall_totals) / len(overall_totals), 1) if overall_totals else 0.0

    # Build the `insights` dict that `reports/_dreyfus_profile.html` consumes.
    has_skill, has_agency = questionnaire.dreyfus_dimensions
    insights = {}

    if has_skill:
        skill_level = round(rng.uniform(3.2, 4.1), 2)
        stage_num = _level_to_stage(skill_level)
        stage_info = DREYFUS_STAGES[stage_num].copy()
        next_stage_num = min(5, stage_num + 1)
        if next_stage_num > stage_num:
            stage_info['next_stage'] = DREYFUS_STAGES[next_stage_num]['name']
            stage_info['development_focus'] = _get_development_focus(stage_num, next_stage_num)
        else:
            stage_info['next_stage'] = None
            stage_info['development_focus'] = ['Continue deepening expertise and innovating']

        insights['skill_profile'] = {
            'skill_level': skill_level,
            'skill_stage': stage_info['name'],
            'confidence': 0.85,
            'stage_info': stage_info,
        }

    if has_agency:
        agency_level = round(rng.uniform(3.4, 4.3), 2)
        agency_stage_num = max(1, min(5, round(agency_level)))
        agency_stage_info = AGENCY_STAGES[agency_stage_num]
        insights['agency_profile'] = {
            'agency_level': agency_level,
            'agency_stage': agency_stage_info['name'],
            'confidence': 0.85,
            'description': agency_stage_info['description'],
        }

    if has_skill and has_agency:
        insights['dreyfus_quadrant'] = calculate_dreyfus_quadrant(
            insights['skill_profile']['skill_level'],
            insights['agency_profile']['agency_level'],
        )

    # Minimal development plan so the "next level" cards render.
    if has_skill:
        insights['development_plan'] = {
            'next_level_requirements': insights['skill_profile']['stage_info'].get(
                'development_focus', []
            ),
            'quick_wins': [
                'Pair with a more senior teammate on a challenging problem this sprint',
                'Document one decision you made and why — share it for review',
                'Pick a weak area from the feedback and book focused practice time',
            ],
        }

    context = {
        'questionnaire': questionnaire,
        'sections_data': sections_data,
        'overall_score': overall_score,
        'insights': insights,
        'chart_data': {'section_scores': section_scores},
    }

    return render(request, 'admin_dashboard/questionnaire_sample_report.html', context)


@login_required
def questionnaire_create(request):
    """Create a new questionnaire"""
    from questionnaires.models import QuestionSection, Question

    if request.method == 'POST':
        name = request.POST.get('name')
        description = request.POST.get('description', '')
        is_default = request.POST.get('is_default') == 'on'

        if not name:
            messages.error(request, 'Questionnaire name is required.')
            return render(request, 'admin_dashboard/questionnaire_form.html', {'action': 'Create'})

        try:
            # Get organization from request context
            org = getattr(request, 'organization', None)

            questionnaire = Questionnaire.objects.create(
                name=name,
                description=description,
                is_default=is_default,
                organization=org,
                created_by=request.user,
                allow_peer_review=request.POST.get('allow_peer_review') == 'on',
                allow_self_assessment=request.POST.get('allow_self_assessment') == 'on',
                allow_manager_assessment=request.POST.get('allow_manager_assessment') == 'on',
            )
            messages.success(request, f'Questionnaire "{questionnaire.name}" created successfully.')
            return redirect('questionnaire_edit', questionnaire_id=questionnaire.id)
        except Exception as e:
            messages.error(request, f'Error creating questionnaire: {str(e)}')

    context = {
        'action': 'Create',
    }

    return render(request, 'admin_dashboard/questionnaire_form.html', context)


@login_required
def questionnaire_edit(request, questionnaire_id):
    """Edit an existing questionnaire"""
    from questionnaires.models import QuestionSection, Question

    # Get organization from request context
    org = getattr(request, 'organization', None)

    # Filter by organization to prevent cross-org access
    questionnaire = get_object_or_404(
        Questionnaire,
        id=questionnaire_id,
        organization=org
    )
    if not can_edit_questionnaire(request.user, questionnaire):
        raise PermissionDenied

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'update_info':
            questionnaire.name = request.POST.get('name', questionnaire.name)
            questionnaire.description = request.POST.get('description', '')
            questionnaire.is_default = request.POST.get('is_default') == 'on'
            questionnaire.allow_peer_review = request.POST.get('allow_peer_review') == 'on'
            questionnaire.allow_self_assessment = request.POST.get('allow_self_assessment') == 'on'
            questionnaire.allow_manager_assessment = request.POST.get('allow_manager_assessment') == 'on'

            try:
                questionnaire.save()
                messages.success(request, f'Questionnaire "{questionnaire.name}" updated successfully.')
            except Exception as e:
                messages.error(request, f'Error updating questionnaire: {str(e)}')

        elif action == 'add_section':
            section_title = request.POST.get('section_title')
            section_description = request.POST.get('section_description', '')

            if section_title:
                max_order = questionnaire.sections.aggregate(Max('order'))['order__max']
                next_order = (max_order + 1) if max_order is not None else 0
                try:
                    QuestionSection.objects.create(
                        questionnaire=questionnaire,
                        title=section_title,
                        description=section_description,
                        order=next_order
                    )
                    messages.success(request, f'Section "{section_title}" added.')
                except Exception as e:
                    messages.error(request, f'Error adding section: {str(e)}')

        elif action == 'edit_section':
            section_id = request.POST.get('section_id')
            section_title = request.POST.get('section_title')
            section_description = request.POST.get('section_description', '')

            if section_id and section_title:
                try:
                    section = QuestionSection.objects.get(id=section_id, questionnaire=questionnaire)
                    section.title = section_title
                    section.description = section_description
                    section.save()
                    messages.success(request, f'Section "{section_title}" updated successfully.')
                except QuestionSection.DoesNotExist:
                    messages.error(request, 'Section not found.')
                except Exception as e:
                    messages.error(request, f'Error updating section: {str(e)}')

        elif action == 'add_question':
            section_id = request.POST.get('section_id')
            question_text = request.POST.get('question_text')
            question_type = request.POST.get('question_type', 'rating')
            required = request.POST.get('required') == 'on'
            is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

            if section_id and question_text:
                try:
                    section = QuestionSection.objects.get(id=section_id, questionnaire=questionnaire)
                    max_order = section.questions.aggregate(Max('order'))['order__max']
                    next_order = (max_order + 1) if max_order is not None else 0

                    # Build config based on question type
                    config = {}
                    if question_type == 'rating':
                        config = {
                            'min': 1,
                            'max': 5,
                            'labels': {
                                '1': 'Strongly Disagree',
                                '2': 'Disagree',
                                '3': 'Neutral',
                                '4': 'Agree',
                                '5': 'Strongly Agree'
                            }
                        }
                    elif question_type == 'likert':
                        scale_raw = request.POST.get('likert_scale', '')
                        if scale_raw:
                            scale = [s.strip() for s in scale_raw.split('\n') if s.strip()]
                        else:
                            scale = ['Strongly Disagree', 'Disagree', 'Neutral', 'Agree', 'Strongly Agree']
                        config = {'scale': scale}
                    elif question_type == 'single_choice' or question_type == 'multiple_choice':
                        choices_raw = request.POST.get('choices', '')
                        choices = [c.strip() for c in choices_raw.split('\n') if c.strip()]
                        config = {'choices': choices}

                        # Check if scoring is enabled and weights are provided
                        enable_scoring = request.POST.get('enable_scoring') == 'on'
                        if enable_scoring:
                            weights_raw = request.POST.getlist('weights[]')
                            try:
                                # Parse weights as floats
                                weights = [float(w) for w in weights_raw if w.strip()]
                                # Validate: weights must match choices length
                                if len(weights) == len(choices):
                                    config['weights'] = weights
                                    config['scoring_enabled'] = True
                                else:
                                    messages.warning(request, 'Weights count did not match choices count. Scoring disabled for this question.')
                            except (ValueError, TypeError):
                                messages.warning(request, 'Invalid weight values. Scoring disabled for this question.')
                    elif question_type == 'scale':
                        try:
                            min_val = int(request.POST.get('scale_min', 1))
                            max_val = int(request.POST.get('scale_max', 100))
                            step_val = int(request.POST.get('scale_step', 1))
                            min_label = request.POST.get('scale_min_label', '').strip()
                            max_label = request.POST.get('scale_max_label', '').strip()

                            config = {
                                'min': min_val,
                                'max': max_val,
                                'step': step_val
                            }
                            if min_label:
                                config['min_label'] = min_label
                            if max_label:
                                config['max_label'] = max_label
                        except (ValueError, TypeError):
                            # Use defaults if parsing fails
                            config = {'min': 1, 'max': 100, 'step': 1}
                            messages.warning(request, 'Invalid scale values. Using defaults (1-100, step 1).')

                    # Add Dreyfus/Agency configuration if provided
                    skill_weight = request.POST.get('add_skill_weight', 0)
                    agency_weight = request.POST.get('add_agency_weight', 0)

                    if skill_weight or agency_weight:
                        config['dreyfus_mapping'] = {
                            'skill': float(skill_weight),
                            'agency': float(agency_weight)
                        }

                    # Parse action items for new question
                    action_items = []
                    action_item_indices = set()

                    # Extract all action item indices from POST data
                    for key in request.POST.keys():
                        if key.startswith('add_action_items[') and '][text]' in key:
                            idx_str = key.split('[')[1].split(']')[0]
                            action_item_indices.add(idx_str)

                    # Build action items list
                    for idx in sorted(action_item_indices):
                        text = request.POST.get(f'add_action_items[{idx}][text]', '').strip()
                        threshold = request.POST.get(f'add_action_items[{idx}][threshold]', 3.0)
                        stages_raw = request.POST.getlist(f'add_action_items[{idx}][stages][]')

                        if text:
                            item = {
                                'text': text,
                                'threshold': float(threshold)
                            }
                            if stages_raw:
                                item['stages'] = [int(s) for s in stages_raw]
                            action_items.append(item)

                    question = Question.objects.create(
                        section=section,
                        question_text=question_text,
                        question_type=question_type,
                        config=config,
                        required=required,
                        order=next_order,
                        action_items=action_items
                    )

                    if is_ajax:
                        question_html = render_to_string(
                            'admin_dashboard/partials/question_card.html',
                            {'question': question},
                            request=request,
                        )
                        return JsonResponse({
                            'success': True,
                            'message': 'Question added successfully.',
                            'section_id': section.id,
                            'question_id': question.id,
                            'question_html': question_html,
                        })

                    messages.success(request, 'Question added successfully.')
                except Exception as e:
                    logger.exception('Error adding question')
                    if is_ajax:
                        return JsonResponse({
                            'success': False,
                            'message': 'Error adding question. Please try again.',
                        }, status=400)
                    messages.error(request, 'Error adding question. Please try again.')

        elif action == 'delete_section':
            section_id = request.POST.get('section_id')
            try:
                section = QuestionSection.objects.get(id=section_id, questionnaire=questionnaire)
                section_title = section.title
                section.delete()
                messages.success(request, f'Section "{section_title}" deleted.')
            except Exception as e:
                messages.error(request, f'Error deleting section: {str(e)}')

        elif action == 'delete_question':
            question_id = request.POST.get('question_id')
            try:
                question = Question.objects.get(id=question_id, section__questionnaire=questionnaire)
                question.delete()
                messages.success(request, 'Question deleted.')
            except Exception as e:
                messages.error(request, f'Error deleting question: {str(e)}')

        elif action == 'edit_question':
            question_id = request.POST.get('question_id')
            question_text = request.POST.get('question_text')
            question_type = request.POST.get('question_type', 'rating')
            required = request.POST.get('required') == 'on'
            is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

            if question_id and question_text:
                try:
                    question = Question.objects.get(id=question_id, section__questionnaire=questionnaire)

                    # Update basic fields
                    question.question_text = question_text
                    question.question_type = question_type
                    question.required = required

                    # Build config based on question type
                    config = {}
                    if question_type == 'rating':
                        config = {
                            'min': 1,
                            'max': 5,
                            'labels': {
                                '1': 'Strongly Disagree',
                                '2': 'Disagree',
                                '3': 'Neutral',
                                '4': 'Agree',
                                '5': 'Strongly Agree'
                            }
                        }
                    elif question_type == 'likert':
                        scale_raw = request.POST.get('likert_scale', '')
                        if scale_raw:
                            scale = [s.strip() for s in scale_raw.split('\n') if s.strip()]
                        else:
                            scale = ['Strongly Disagree', 'Disagree', 'Neutral', 'Agree', 'Strongly Agree']
                        config = {'scale': scale}
                    elif question_type == 'single_choice' or question_type == 'multiple_choice':
                        choices_raw = request.POST.get('choices', '')
                        choices = [c.strip() for c in choices_raw.split('\n') if c.strip()]
                        config = {'choices': choices}

                        # Check if scoring is enabled and weights are provided
                        enable_scoring = request.POST.get('enable_scoring') == 'on'
                        if enable_scoring:
                            weights_raw = request.POST.getlist('weights[]')
                            try:
                                # Parse weights as floats
                                weights = [float(w) for w in weights_raw if w.strip()]
                                # Validate: weights must match choices length
                                if len(weights) == len(choices):
                                    config['weights'] = weights
                                    config['scoring_enabled'] = True
                                else:
                                    messages.warning(request, 'Weights count did not match choices count. Scoring disabled for this question.')
                            except (ValueError, TypeError):
                                messages.warning(request, 'Invalid weight values. Scoring disabled for this question.')
                    elif question_type == 'scale':
                        try:
                            min_val = int(request.POST.get('scale_min', 1))
                            max_val = int(request.POST.get('scale_max', 100))
                            step_val = int(request.POST.get('scale_step', 1))
                            min_label = request.POST.get('scale_min_label', '').strip()
                            max_label = request.POST.get('scale_max_label', '').strip()

                            config = {
                                'min': min_val,
                                'max': max_val,
                                'step': step_val
                            }
                            if min_label:
                                config['min_label'] = min_label
                            if max_label:
                                config['max_label'] = max_label
                        except (ValueError, TypeError):
                            # Use defaults if parsing fails
                            config = {'min': 1, 'max': 100, 'step': 1}
                            messages.warning(request, 'Invalid scale values. Using defaults (1-100, step 1).')

                    question.config = config
                    question.save()

                    if is_ajax:
                        return JsonResponse({
                            'success': True,
                            'message': 'Question updated successfully.',
                            'question': {
                                'id': question.id,
                                'question_text': question.question_text,
                                'question_type': question.question_type,
                                'question_type_display': question.get_question_type_display(),
                                'required': question.required,
                                'config': question.config,
                            },
                        })

                    messages.success(request, 'Question updated successfully.')
                except Exception as e:
                    logger.exception('Error updating question')
                    if is_ajax:
                        return JsonResponse({
                            'success': False,
                            'message': 'Error updating question. Please try again.',
                        }, status=400)
                    messages.error(request, 'Error updating question. Please try again.')

        elif action == 'update_dreyfus_config':
            question_id = request.POST.get('question_id')
            skill_weight = request.POST.get('skill_weight', 0)
            agency_weight = request.POST.get('agency_weight', 0)

            if question_id:
                try:
                    question = Question.objects.get(id=question_id, section__questionnaire=questionnaire)

                    # Update dreyfus_mapping in config
                    if 'dreyfus_mapping' not in question.config:
                        question.config['dreyfus_mapping'] = {}

                    question.config['dreyfus_mapping']['skill'] = float(skill_weight)
                    question.config['dreyfus_mapping']['agency'] = float(agency_weight)

                    # Parse action items from form
                    action_items = []
                    action_item_indices = set()

                    # Extract all action item indices from the POST data
                    for key in request.POST.keys():
                        if key.startswith('action_items[') and '][text]' in key:
                            # Extract index from key like "action_items[0][text]"
                            idx_str = key.split('[')[1].split(']')[0]
                            action_item_indices.add(idx_str)

                    # Build action items list
                    for idx in sorted(action_item_indices):
                        text = request.POST.get(f'action_items[{idx}][text]', '').strip()
                        threshold = request.POST.get(f'action_items[{idx}][threshold]', 3.0)
                        stages_raw = request.POST.getlist(f'action_items[{idx}][stages][]')

                        if text:  # Only add if text is not empty
                            item = {
                                'text': text,
                                'threshold': float(threshold)
                            }

                            # Add stages if any are selected
                            if stages_raw:
                                item['stages'] = [int(s) for s in stages_raw]

                            action_items.append(item)

                    question.action_items = action_items
                    question.save()

                    messages.success(request, 'Dreyfus/Agency configuration updated successfully.')
                except Question.DoesNotExist:
                    messages.error(request, 'Question not found.')
                except Exception as e:
                    messages.error(request, f'Error updating Dreyfus configuration: {str(e)}')

        return redirect('questionnaire_edit', questionnaire_id=questionnaire.id)

    sections = questionnaire.sections.prefetch_related('questions').all()

    context = {
        'action': 'Edit',
        'questionnaire': questionnaire,
        'sections': sections,
    }

    return render(request, 'admin_dashboard/questionnaire_form.html', context)


@login_required
def question_dreyfus_config_api(request, question_id):
    """API endpoint to get Dreyfus/Agency configuration for a question"""
    from django.http import JsonResponse
    from questionnaires.models import Question

    try:
        # Get the organization from request
        org = getattr(request, 'organization', None)

        # Get question and verify it belongs to the user's organization
        question = Question.objects.select_related('section__questionnaire').get(
            id=question_id,
            section__questionnaire__organization=org
        )

        # Extract dreyfus_mapping from config
        dreyfus_mapping = question.config.get('dreyfus_mapping', {})

        # Return configuration
        return JsonResponse({
            'dreyfus_mapping': dreyfus_mapping,
            'action_items': question.action_items
        })

    except Question.DoesNotExist:
        return JsonResponse({'error': 'Question not found'}, status=404)
    except Exception as e:
        logger.exception('Error fetching Dreyfus config')
        return JsonResponse({'error': 'Internal server error'}, status=500)


@login_required
def review_cycle_list(request):
    """List all review cycles"""
    org = request.organization

    if org:
        # Filter out cycles for anonymized reviewees
        cycles_qs = ReviewCycle.objects.for_organization(org).select_related(
            'reviewee', 'questionnaire', 'created_by'
        )
    else:
        cycles_qs = ReviewCycle.objects.select_related(
            'reviewee', 'questionnaire', 'created_by'
        )

    cycles_qs = visible_cycles(request.user, cycles_qs)

    campaign_qs = (
        ReviewCampaign.objects.filter(organization=org, cycles__in=cycles_qs)
        .select_related(
            'team', 'individual', 'questionnaire', 'organizational_cycle'
        )
        .annotate(
            participant_count=Count('cycles', distinct=True),
            response_count=Count(
                'cycles__tokens',
                filter=Q(cycles__tokens__completed_at__isnull=False),
                distinct=True,
            ),
            request_count=Count('cycles__tokens', distinct=True),
        )
        .distinct()
        .order_by('-start_date', '-created_at')
    )
    campaigns = []
    organizational_cycle_groups = {}
    for campaign in campaign_qs:
        scoped_cycles = visible_cycles(
            request.user,
            campaign.cycles.select_related('reviewee').prefetch_related('tokens'),
        ).order_by('reviewee__name')
        completed_people = scoped_cycles.filter(status='completed').count()
        campaign_item = {
            'campaign': campaign,
            'participant_count': scoped_cycles.count(),
            'completed_people': completed_people,
            'all_complete': scoped_cycles.exists() and completed_people == scoped_cycles.count(),
            'can_manage': _can_manage_campaign(request.user, campaign, org),
            'people': [],
        }
        completed_responses = 0
        total_responses = 0
        for cycle in scoped_cycles:
            tokens = list(cycle.tokens.all())
            completed_count = sum(token.is_completed for token in tokens)
            completed_responses += completed_count
            total_responses += max(len(tokens), 1)
            campaign_item['people'].append({
                'cycle': cycle,
                'completed_count': completed_count,
                'total_count': len(tokens),
                'status_label': (
                    'Complete' if cycle.status == 'completed'
                    else 'Awaiting peer selection'
                    if campaign.cycle_type == 'peer' and not tokens
                    else 'In progress' if completed_count
                    else 'Invitation pending'
                ),
            })
        campaign_item['completed_responses'] = completed_responses
        campaign_item['total_responses'] = total_responses
        if campaign.organizational_cycle_id:
            group = organizational_cycle_groups.setdefault(
                campaign.organizational_cycle_id,
                {
                    'cycle': campaign.organizational_cycle,
                    'campaigns': [],
                    'completed_count': 0,
                    'total_count': 0,
                },
            )
            group['campaigns'].append(campaign_item)
            group['completed_count'] += completed_responses
            group['total_count'] += total_responses
        else:
            campaigns.append(campaign_item)

    # Campaign cycles are represented by their grouped summaries above. Keeping
    # them out of this table avoids showing every participant as a separate cycle.
    cycles_qs = cycles_qs.filter(campaign__isnull=True).prefetch_related(
        'questionnaire__sections__questions'
    ).annotate(
        token_count=Count('tokens'),
        completed_count=Count('tokens', filter=Q(tokens__completed_at__isnull=False))
    ).order_by('-created_at')

    # Get per_page from request, default to 25
    per_page = request.GET.get('per_page', '25')
    try:
        per_page = int(per_page)
        if per_page not in [25, 50, 100]:
            per_page = 25
    except (ValueError, TypeError):
        per_page = 25

    # Paginate cycles
    paginator = Paginator(cycles_qs, per_page)
    page = request.GET.get('page')
    try:
        cycles = paginator.page(page)
    except PageNotAnInteger:
        cycles = paginator.page(1)
    except EmptyPage:
        cycles = paginator.page(paginator.num_pages)

    # Get available questionnaires for quick cycle creation
    questionnaires = Questionnaire.objects.for_organization(org).filter(is_active=True).order_by('-is_default', 'name')

    # Enhance cycles with latest questionnaire info for each reviewee
    cycles_with_latest = []
    for cycle in cycles:
        latest_cycle = cycle.reviewee.review_cycles.select_related('questionnaire').order_by('-created_at').first()
        cycles_with_latest.append({
            'cycle': cycle,
            'latest_questionnaire': latest_cycle.questionnaire if latest_cycle else None,
        })

    context = {
        'cycles_with_latest': cycles_with_latest,
        'cycles': cycles,  # Paginated object
        'questionnaires': questionnaires,
        'per_page': per_page,
        'campaigns': campaigns,
        'organizational_cycle_groups': organizational_cycle_groups.values(),
    }

    return render(request, 'admin_dashboard/review_cycle_list.html', context)


@login_required
def organisation_cycle_detail(request, cycle_uuid):
    """Show the authorized assessment breakdown for one organisation cycle."""
    from reviews.models import OrganizationalReviewCycle

    organisation_cycle = get_object_or_404(
        OrganizationalReviewCycle,
        uuid=cycle_uuid,
        organization=request.organization,
    )
    permitted_cycles = visible_cycles(
        request.user,
        ReviewCycle.objects.for_organization(request.organization),
    )
    campaigns = (
        organisation_cycle.campaigns.filter(cycles__in=permitted_cycles)
        .select_related('team', 'questionnaire')
        .distinct()
        .order_by('cycle_type', 'team__name', 'created_at')
    )
    if not campaigns.exists():
        raise Http404

    assessment_groups = []
    completed_total = 0
    response_total = 0
    for campaign in campaigns:
        cycles = visible_cycles(
            request.user,
            campaign.cycles.select_related('reviewee').prefetch_related('tokens'),
        ).order_by('reviewee__name')
        people = []
        campaign_completed = 0
        campaign_total = 0
        for cycle in cycles:
            tokens = list(cycle.tokens.all())
            completed = sum(token.is_completed for token in tokens)
            required_total = max(len(tokens), 1)
            campaign_completed += completed
            campaign_total += required_total
            people.append({
                'cycle': cycle,
                'completed_count': completed,
                'total_count': len(tokens),
                'status_label': (
                    'Complete' if cycle.status == 'completed'
                    else 'Awaiting peer selection'
                    if campaign.cycle_type == 'peer' and not tokens
                    else 'In progress' if completed
                    else 'Invitation pending'
                ),
            })
        completed_total += campaign_completed
        response_total += campaign_total
        assessment_groups.append({
            'campaign': campaign,
            'people': people,
            'completed_count': campaign_completed,
            'total_count': campaign_total,
            'can_manage': _can_manage_campaign(
                request.user, campaign, request.organization
            ),
        })

    return render(request, 'admin_dashboard/organisation_cycle_detail.html', {
        'organisation_cycle': organisation_cycle,
        'assessment_groups': assessment_groups,
        'completed_count': completed_total,
        'total_count': response_total,
        'completion_rate': (
            completed_total / response_total * 100 if response_total else 0
        ),
    })


@login_required
def report_list(request):
    """List generated reports within the user's effective authorization scope."""
    cycles = visible_cycles(
        request.user,
        ReviewCycle.objects.for_organization(request.organization),
    )
    reports = Report.objects.filter(
        cycle__in=cycles, available=True
    ).select_related(
        'cycle__reviewee', 'cycle__questionnaire', 'cycle__campaign'
    ).order_by('-generated_at')
    return render(request, 'admin_dashboard/report_list.html', {'reports': reports})


@login_required
@require_POST
def renew_review_cycle(request, cycle_uuid):
    """Renew a visible cycle without modifying its historical responses."""
    if not request.user.has_perm('accounts.can_manage_organization'):
        raise PermissionDenied

    source_cycle = get_cycle_or_404(request, cycle_uuid)
    start_date = request.POST.get('start_date') or None
    due_date = request.POST.get('due_date') or None
    try:
        start_date = date.fromisoformat(start_date) if start_date else None
        due_date = date.fromisoformat(due_date) if due_date else None
    except ValueError:
        messages.error(request, 'Enter valid start and due dates.')
        return redirect('review_cycle_list')
    if start_date and due_date and due_date < start_date:
        messages.error(request, 'The due date cannot be before the start date.')
        return redirect('review_cycle_list')

    from reviews.cycle_services import renew_cycle
    cycle = renew_cycle(
        source_cycle,
        request.user,
        start_date=start_date,
        due_date=due_date,
    )
    messages.success(
        request,
        f'Renewed the cycle for {cycle.reviewee.name}. Review the copied participants before sending invitations.',
    )
    return redirect('review_cycle_detail', cycle_uuid=cycle.uuid)


@login_required
def review_cycle_create(request):
    """Create a new review cycle (single or bulk)"""
    if request.method == 'POST' and request.POST.get('campaign_flow') == '1':
        return _create_review_campaign(request)

    if request.method == 'POST':
        creation_mode = request.POST.get('creation_mode', 'single')
        questionnaire_id = request.POST.get('questionnaire')
        cycle_type = request.POST.get('cycle_type', '360')
        if cycle_type not in dict(ReviewCycle.TYPE_CHOICES):
            messages.error(request, 'Invalid review cycle type.')
            return redirect('review_cycle_create')
        try:
            start_date = date.fromisoformat(request.POST['start_date']) if request.POST.get('start_date') else None
            due_date = date.fromisoformat(request.POST['due_date']) if request.POST.get('due_date') else None
        except ValueError:
            messages.error(request, 'Enter valid start and due dates.')
            return redirect('review_cycle_create')
        if start_date and due_date and due_date < start_date:
            messages.error(request, 'The due date cannot be before the start date.')
            return redirect('review_cycle_create')

        if not questionnaire_id:
            messages.error(request, 'Questionnaire is required.')
            return redirect('review_cycle_create')

        try:
            # Get organization from request context
            org = getattr(request, 'organization', None)

            # Filter by organization to prevent cross-org access
            questionnaire = Questionnaire.objects.get(
                id=questionnaire_id,
                organization=org
            )
            created_cycles = []

            if creation_mode == 'bulk':
                # Create cycles for all active reviewees. Intentionally do NOT
                # send any emails here — the admin confirms sending on the
                # follow-up bulk_send_invitations page. This prevents the
                # request from stalling on SMTP and avoids surprise blasts.
                reviewees = visible_reviewees(
                    request.user,
                    Reviewee.objects.for_organization(org).filter(is_active=True),
                    org,
                )

                with transaction.atomic():
                    for reviewee in reviewees:
                        cycle = ReviewCycle.objects.create(
                            reviewee=reviewee,
                            questionnaire=questionnaire,
                            created_by=request.user,
                            status='active',
                            cycle_type=cycle_type,
                            start_date=start_date,
                            due_date=due_date,
                        )
                        created_cycles.append(cycle)

                # Stash the cycle UUIDs in the session for the send-invitations
                # confirmation step. Session avoids URL-length limits when the
                # org has many reviewees.
                request.session['pending_invitation_cycles'] = [
                    str(c.uuid) for c in created_cycles
                ]

                messages.success(
                    request,
                    f'Created {len(created_cycles)} review cycles. No invitations have been sent yet.'
                )
                return redirect('bulk_send_invitations')

            else:
                # Single reviewee mode
                reviewee_id = request.POST.get('reviewee')
                if not reviewee_id:
                    messages.error(request, 'Reviewee is required for single cycle creation.')
                    return redirect('review_cycle_create')

                reviewee = visible_reviewees(
                    request.user, Reviewee.objects.for_organization(org), org
                ).get(id=reviewee_id)

                # Create review cycle (no tokens created here)
                cycle = ReviewCycle.objects.create(
                    reviewee=reviewee,
                    questionnaire=questionnaire,
                    created_by=request.user,
                    status='active',
                    cycle_type=cycle_type,
                    start_date=start_date,
                    due_date=due_date,
                )

                # Send notification emails to reviewee. Defer to after the
                # transaction commits so a slow SMTP backend can't stall this
                # request (matches the pattern in api/signals.py).
                from reviews.services import send_reviewee_notifications
                transaction.on_commit(
                    lambda c=cycle: send_reviewee_notifications(c, request)
                )

                # Check if user provided reviewer emails
                from django.core.validators import validate_email
                from django.core.exceptions import ValidationError
                import re

                email_assignments = {}
                has_emails = False

                for category_code, category_display in ReviewerToken.CATEGORY_CHOICES:
                    emails_data = request.POST.get(f'{category_code}_emails', '').strip()
                    if emails_data:
                        emails = re.split(r'[,\n]+', emails_data)
                        validated_emails = []
                        for e in emails:
                            e = e.strip()
                            if e:
                                try:
                                    validate_email(e)
                                    validated_emails.append(e)
                                    has_emails = True
                                except ValidationError:
                                    messages.warning(request, f'Invalid email skipped in {category_display}: {e}')
                        email_assignments[category_code] = validated_emails
                    else:
                        email_assignments[category_code] = []

                # If emails were provided, create tokens and assign them
                if has_emails:
                    # Create tokens dynamically based on email count
                    for category_code, emails in email_assignments.items():
                        if emails:
                            for _ in range(len(emails)):
                                ReviewerToken.objects.create(
                                    cycle=cycle,
                                    category=category_code
                                )

                    # Assign tokens to emails with randomization
                    assign_stats = assign_tokens_to_emails(cycle, email_assignments)

                    # Check if user wants to send invitations immediately.
                    # Defer the send to after-commit so SMTP latency never
                    # stalls the request.
                    send_now = request.POST.get('send_invitations_now') == '1'
                    if send_now and assign_stats['assigned'] > 0:
                        transaction.on_commit(
                            lambda c=cycle: send_reviewer_invitations(c)
                        )
                        messages.success(
                            request,
                            f'Review cycle created for "{reviewee.name}" with {assign_stats["assigned"]} reviewer(s) invited. Invitation emails are being sent.'
                        )
                        return redirect('review_cycle_detail', cycle_uuid=cycle.uuid)
                    else:
                        messages.success(
                            request,
                            f'Review cycle created for "{reviewee.name}" with {assign_stats["assigned"]} reviewer(s) assigned. Visit the invitations page to send emails.'
                        )
                        # Redirect to invitations page to send emails
                        return redirect('review_cycle_detail', cycle_uuid=cycle.uuid)
                else:
                    # No emails provided, show success and redirect to invitations.
                    # Reviewee notifications were queued via on_commit above.
                    messages.success(
                        request,
                        f'Review cycle created for "{reviewee.name}". Notification emails are being sent to the reviewee.'
                    )
                    # Redirect to invitations page to add reviewers
                    return redirect('manage_invitations', cycle_uuid=cycle.uuid)

        except Exception as e:
            messages.error(request, f'Error creating review cycle: {str(e)}')
            return redirect('review_cycle_create')

    # Keep the existing one-click teammate request working until peer
    # nominations move to their dedicated form.
    if request.GET.get('reviewer_email'):
        return _render_legacy_cycle_form(request)

    return _render_campaign_form(request)


def _render_campaign_form(request):
    org = request.organization
    teams = manageable_teams(request.user, org).select_related('manager__user').order_by('name')
    reviewees = visible_reviewees(
        request.user,
        Reviewee.objects.for_organization(org).filter(is_active=True),
        org,
    ).order_by('name')
    questionnaires = Questionnaire.objects.for_organization(org).filter(
        is_active=True
    ).order_by('-is_default', 'name')
    return render(request, 'admin_dashboard/review_campaign_form.html', {
        'teams': teams,
        'reviewees': reviewees,
        'questionnaires': questionnaires,
    })


def _create_review_campaign(request):
    from reviews.campaign_services import (
        launch_campaign, launch_organizational_cycle, questionnaire_supports,
    )

    org = request.organization
    cycle_name = request.POST.get('cycle_name', '').strip()
    if len(cycle_name) > 255:
        messages.error(request, 'Cycle name must be 255 characters or fewer.')
        return redirect('review_cycle_create')
    target_type = request.POST.get('target_type')
    cycle_type = request.POST.get('cycle_type')
    if target_type not in dict(ReviewCampaign.TARGET_CHOICES):
        messages.error(request, 'Select either a team or an individual.')
        return redirect('review_cycle_create')
    if target_type != 'organization' and cycle_type not in dict(ReviewCampaign.TYPE_CHOICES):
        messages.error(request, 'Select a valid review type.')
        return redirect('review_cycle_create')
    try:
        minimum_peer_reviewers = int(request.POST.get('minimum_peer_reviewers', 1))
    except (TypeError, ValueError):
        minimum_peer_reviewers = 0
    if (cycle_type == 'peer' or target_type == 'organization') and not 1 <= minimum_peer_reviewers <= 50:
        messages.error(request, 'Minimum peer reviewers must be between 1 and 50.')
        return redirect('review_cycle_create')
    if cycle_type != 'peer' and target_type != 'organization':
        minimum_peer_reviewers = 1

    try:
        start_date = date.fromisoformat(request.POST['start_date']) if request.POST.get('start_date') else None
        due_date = date.fromisoformat(request.POST['due_date']) if request.POST.get('due_date') else None
    except ValueError:
        messages.error(request, 'Enter valid start and due dates.')
        return redirect('review_cycle_create')
    if start_date and due_date and due_date < start_date:
        messages.error(request, 'The due date cannot be before the start date.')
        return redirect('review_cycle_create')

    if target_type == 'organization':
        if not request.user.has_perm('accounts.can_manage_organization'):
            raise PermissionDenied
        audience_type = request.POST.get('organization_audience', 'entire')
        selected_teams = None
        selected_participants = None
        if audience_type == 'teams':
            selected_team_ids = set(request.POST.getlist('organization_teams'))
            selected_teams = list(
                Team.objects.for_organization(org).filter(id__in=selected_team_ids)
            )
            if not selected_team_ids or len(selected_teams) != len(selected_team_ids):
                messages.error(request, 'Select at least one valid team.')
                return redirect('review_cycle_create')
        elif audience_type == 'individuals':
            emails = {
                value.strip().lower()
                for value in re.split(
                    r'[,;\n]+', request.POST.get('organization_individuals', '')
                )
                if value.strip()
            }
            try:
                for email in emails:
                    validate_email(email)
            except ValidationError:
                messages.error(request, 'Enter valid email addresses separated by commas.')
                return redirect('review_cycle_create')
            participant_filter = Q()
            for email in emails:
                participant_filter |= Q(email__iexact=email)
            selected_participants = list(
                Reviewee.objects.for_organization(org).filter(
                    participant_filter, is_active=True
                )
            ) if emails else []
            found_emails = {person.email.lower() for person in selected_participants}
            missing_emails = sorted(emails - found_emails)
            if not emails or missing_emails:
                detail = f" Missing: {', '.join(missing_emails)}." if missing_emails else ''
                messages.error(
                    request,
                    'Enter at least one active organisation member email.' + detail,
                )
                return redirect('review_cycle_create')
        elif audience_type != 'entire':
            messages.error(request, 'Select a valid organisation audience.')
            return redirect('review_cycle_create')
        questionnaires = {}
        assessment_types = (
            ('self', 'peer')
            if audience_type == 'individuals' else ('self', 'peer', 'manager')
        )
        for assessment_type in assessment_types:
            questionnaire = get_object_or_404(
                Questionnaire.objects.for_organization(org).filter(is_active=True),
                id=request.POST.get(f'{assessment_type}_questionnaire'),
            )
            if not questionnaire_supports(questionnaire, assessment_type):
                messages.error(
                    request,
                    f'{questionnaire.name} is not available for {assessment_type} assessments.',
                )
                return redirect('review_cycle_create')
            questionnaires[assessment_type] = questionnaire
        try:
            organizational_cycle = launch_organizational_cycle(
                organization=org,
                created_by=request.user,
                name=cycle_name,
                audience_type=audience_type,
                teams=selected_teams,
                participants=selected_participants,
                questionnaires=questionnaires,
                minimum_peer_reviewers=minimum_peer_reviewers,
                start_date=start_date,
                due_date=due_date,
            )
        except ValidationError as exc:
            messages.error(request, '; '.join(exc.messages))
            return redirect('review_cycle_create')
        from reviews.services import send_organizational_cycle_invitations

        def send_organizational_invitation(parent_id=organizational_cycle.id):
            from reviews.models import OrganizationalReviewCycle
            parent = OrganizationalReviewCycle.objects.get(pk=parent_id)
            send_organizational_cycle_invitations(parent)

        transaction.on_commit(send_organizational_invitation)
        messages.success(
            request,
            'Organisation cycle created. Self, peer, and manager invitations are being sent.',
        )
        return redirect('admin_dashboard')

    questionnaire = get_object_or_404(
        Questionnaire.objects.for_organization(org).filter(is_active=True),
        id=request.POST.get('questionnaire'),
    )
    if not questionnaire_supports(questionnaire, cycle_type):
        messages.error(request, 'That questionnaire is not available for the selected review type.')
        return redirect('review_cycle_create')

    team = None
    individual = None
    if target_type == 'team':
        team = get_object_or_404(
            manageable_teams(request.user, org), id=request.POST.get('team')
        )
    else:
        individual = get_object_or_404(
            visible_reviewees(
                request.user,
                Reviewee.objects.for_organization(org).filter(is_active=True),
                org,
            ),
            id=request.POST.get('individual'),
        )

    campaign = ReviewCampaign.objects.create(
        organization=org,
        created_by=request.user,
        name=cycle_name,
        questionnaire=questionnaire,
        target_type=target_type,
        team=team,
        individual=individual,
        include_descendants=(
            target_type == 'team' and request.POST.get('include_descendants') == '1'
        ),
        cycle_type=cycle_type,
        minimum_peer_reviewers=minimum_peer_reviewers,
        start_date=start_date,
        due_date=due_date,
    )
    try:
        launch_campaign(campaign)
    except ValidationError as exc:
        campaign.delete()
        messages.error(request, '; '.join(exc.messages))
        return redirect('review_cycle_create')

    from reviews.services import send_campaign_invitations
    transaction.on_commit(lambda c=campaign: send_campaign_invitations(c))
    messages.success(
        request,
        f'{campaign.get_cycle_type_display()} campaign created. Invitations are being sent.',
    )
    return redirect('admin_dashboard')


@login_required
def nominate_peer_reviewers(request, cycle_uuid):
    """Create or edit the reviewer list for one peer-campaign participant."""
    org = request.organization
    cycle = get_object_or_404(
        ReviewCycle.objects.select_related(
            'campaign', 'reviewee__profile__user',
            'reviewee__reporting_manager__user', 'questionnaire',
        ),
        uuid=cycle_uuid,
        reviewee__organization=org,
        campaign__cycle_type='peer',
        campaign__status='active',
    )
    is_reviewee = (
        cycle.reviewee.profile_id
        and cycle.reviewee.profile.user_id == request.user.id
    ) or (
        bool(request.user.email)
        and cycle.reviewee.email.lower() == request.user.email.lower()
    )
    if not is_reviewee and not _can_manage_campaign(request.user, cycle.campaign, org):
        raise Http404
    candidates = Reviewee.objects.for_organization(org).filter(
        is_active=True,
    ).exclude(id=cycle.reviewee_id).order_by('name')
    direct_manager_candidate_ids = set()
    direct_manager = cycle.reviewee.reporting_manager
    if direct_manager:
        direct_manager_candidate_ids.update(candidates.filter(
            Q(profile=direct_manager)
            | Q(email__iexact=direct_manager.user.email)
        ).values_list('id', flat=True))
    existing_emails = set(
        cycle.tokens.exclude(reviewer_email__isnull=True).values_list(
            'reviewer_email', flat=True
        )
    )
    protected_emails = set(
        cycle.tokens.filter(
            Q(claimed_at__isnull=False) | Q(completed_at__isnull=False)
        ).exclude(reviewer_email__isnull=True).values_list('reviewer_email', flat=True)
    )

    if request.method == 'POST':
        selected_ids = request.POST.getlist('reviewers')
        selected = list(candidates.filter(id__in=selected_ids).exclude(
            id__in=direct_manager_candidate_ids
        ))
        selected_emails = {person.email for person in selected if person.email}
        desired_emails = selected_emails | protected_emails
        minimum_reviewers = cycle.campaign.minimum_peer_reviewers
        if len(desired_emails) < minimum_reviewers:
            messages.error(
                request,
                f'Select at least {minimum_reviewers} peer reviewer(s). '
                f'You currently have {len(desired_emails)} selected.',
            )
        else:
            removed_count, _ = cycle.tokens.filter(
                category='peer',
                claimed_at__isnull=True,
                completed_at__isnull=True,
            ).exclude(reviewer_email__in=desired_emails).delete()
            tokens = [
                ReviewerToken.objects.create(
                    cycle=cycle,
                    category='peer',
                    reviewer_email=person.email,
                )
                for person in selected
                if person.email and person.email not in existing_emails
            ]
            if tokens:
                token_ids = [token.id for token in tokens]
                transaction.on_commit(
                    lambda c=cycle, ids=token_ids: send_reviewer_invitations(c, ids)
                )
            messages.success(
                request,
                f'Reviewer list updated: {len(tokens)} added and {removed_count} removed.',
            )
            return redirect(
                'review_campaign_detail', campaign_uuid=cycle.campaign.uuid
            ) if _can_manage_campaign(request.user, cycle.campaign, org) else redirect(
                'admin_dashboard'
            )

    preselected = request.GET.get('candidate', '')
    return render(request, 'admin_dashboard/peer_nomination_form.html', {
        'cycle': cycle,
        'candidates': candidates,
        'existing_emails': existing_emails,
        'protected_emails': protected_emails,
        'direct_manager_candidate_ids': direct_manager_candidate_ids,
        'preselected': preselected,
        'is_editing': bool(existing_emails),
        'minimum_reviewers': cycle.campaign.minimum_peer_reviewers,
    })


@login_required
def review_campaign_detail(request, campaign_uuid):
    """Scoped campaign summary with participant progress and report links."""
    org = request.organization
    campaign = get_object_or_404(
        ReviewCampaign.objects.select_related('team', 'individual', 'questionnaire'),
        uuid=campaign_uuid,
        organization=org,
    )
    can_manage = (
        request.user.has_perm('accounts.can_manage_organization')
        or campaign.created_by_id == request.user.id
        or (
            campaign.team_id
            and manageable_teams(request.user, org).filter(id=campaign.team_id).exists()
        )
    )
    cycles = visible_cycles(
        request.user,
        campaign.cycles.select_related('reviewee').prefetch_related('tokens', 'report'),
    ).order_by('reviewee__name')
    if not cycles.exists():
        raise Http404
    rows = []
    reviewer_names = {
        email.lower(): name
        for email, name in Reviewee.objects.for_organization(org).filter(
            is_active=True
        ).exclude(email='').values_list('email', 'name')
        if email
    }
    completed_people = 0
    completed_responses = 0
    total_responses = 0
    for cycle in cycles:
        tokens = list(cycle.tokens.all())
        cycle_completed = sum(token.is_completed for token in tokens)
        completed_responses += cycle_completed
        total_responses += len(tokens)
        if cycle.status == 'completed':
            completed_people += 1
        rows.append({
            'cycle': cycle,
            'tokens': tokens,
            'reviewers': [
                {
                    'token': token,
                    'name': reviewer_names.get(
                        (token.reviewer_email or '').lower(),
                        token.reviewer_email or 'Reviewer not assigned',
                    ),
                }
                for token in tokens
            ],
            'completed_count': cycle_completed,
            'completion_rate': cycle_completed / len(tokens) * 100 if tokens else 0,
            'report': getattr(cycle, 'report', None),
        })
    return render(request, 'admin_dashboard/review_campaign_detail.html', {
        'campaign': campaign,
        'rows': rows,
        'can_manage': can_manage,
        'participant_count': len(rows),
        'completed_people': completed_people,
        'completed_responses': completed_responses,
        'total_responses': total_responses,
        'completion_rate': completed_responses / total_responses * 100 if total_responses else 0,
        'add_candidates': (
            _campaign_add_candidates(campaign)
            if can_manage
            and campaign.status == 'active'
            and campaign.cycle_type != 'peer' else []
        ),
    })


def _can_manage_campaign(user, campaign, organization):
    return bool(
        user.has_perm('accounts.can_manage_organization')
        or campaign.created_by_id == user.id
        or (
            campaign.team_id
            and manageable_teams(user, organization).filter(id=campaign.team_id).exists()
        )
    )


def _can_view_campaign_progress(user, campaign, organization):
    """Team-wide completion data is visible only to current scoped leaders."""
    if user.has_perm('accounts.can_manage_organization'):
        return True
    if campaign.target_type == 'individual':
        return campaign.created_by_id == user.id
    if campaign.target_type == 'organization':
        return manageable_teams(user, organization).exists()
    return bool(
        campaign.team_id
        and manageable_teams(user, organization).filter(id=campaign.team_id).exists()
    )


def _campaign_add_candidates(campaign):
    """Return eligible people in the campaign's configured audience."""
    from reviews.campaign_services import campaign_members

    candidates = campaign_members(campaign).exclude(email='')
    if campaign.cycle_type == 'self':
        return candidates.exclude(id__in=campaign.cycles.values('reviewee_id'))
    if campaign.cycle_type == 'manager':
        cycle = campaign.cycles.first()
        if not cycle:
            return candidates.none()
        return candidates.exclude(id=cycle.reviewee_id).exclude(
            email__in=cycle.tokens.exclude(reviewer_email__isnull=True).values(
                'reviewer_email'
            )
        )
    return candidates.none()


@login_required
@require_POST
def add_campaign_participant(request, campaign_uuid):
    """Add a participant to an active self or manager assessment campaign."""
    campaign = get_object_or_404(
        ReviewCampaign.objects.select_related('team'),
        uuid=campaign_uuid,
        organization=request.organization,
        status='active',
    )
    if not _can_manage_campaign(request.user, campaign, request.organization):
        raise PermissionDenied
    if campaign.cycle_type not in {'self', 'manager'}:
        raise Http404

    person = get_object_or_404(
        _campaign_add_candidates(campaign), id=request.POST.get('reviewee')
    )
    if campaign.cycle_type == 'self':
        from reviews.campaign_services import _create_cycle

        cycle = _create_cycle(campaign, person)
        ReviewerToken.objects.create(
            cycle=cycle, category='self', reviewer_email=person.email
        )
        transaction.on_commit(lambda c=cycle: send_reviewee_notifications(c))
    else:
        cycle = get_object_or_404(campaign.cycles.all())
        token = ReviewerToken.objects.create(
            cycle=cycle, category='direct_report', reviewer_email=person.email
        )
        transaction.on_commit(
            lambda c=cycle, token_id=token.id: send_reviewer_invitations(
                c, [token_id]
            )
        )

    messages.success(request, f'{person.name} was added and invited.')
    if request.POST.get('return_to') == 'campaign_detail':
        return redirect('review_campaign_detail', campaign_uuid=campaign.uuid)
    return redirect('admin_dashboard')


@login_required
@require_POST
def send_campaign_cycle_reminder(request, campaign_uuid, cycle_uuid):
    campaign = get_object_or_404(
        ReviewCampaign, uuid=campaign_uuid, organization=request.organization
    )
    cycle = get_object_or_404(
        campaign.cycles.select_related('reviewee'), uuid=cycle_uuid
    )
    is_reviewee = bool(request.user.email) and (
        cycle.reviewee.email.lower() == request.user.email.lower()
    )
    if not is_reviewee and not _can_manage_campaign(
        request.user, campaign, request.organization
    ):
        raise PermissionDenied

    if campaign.cycle_type == 'peer' and not cycle.tokens.exists():
        from reviews.services import send_peer_nomination_invitation
        result = send_peer_nomination_invitation(cycle)
    else:
        from reviews.services import send_reminder_emails
        result = send_reminder_emails(cycle)
    if result['sent']:
        messages.success(request, f'Sent {result["sent"]} reminder email(s).')
    elif result['errors']:
        messages.error(request, '; '.join(result['errors']))
    else:
        messages.info(request, 'There are no outstanding reminders for this person.')
    return redirect(
        'review_campaign_detail', campaign_uuid=campaign.uuid
    ) if _can_manage_campaign(request.user, campaign, request.organization) else redirect(
        'admin_dashboard'
    )


@login_required
@require_POST
def send_campaign_reviewer_reminder(request, campaign_uuid, cycle_uuid, token_id):
    """Send or resend the invitation for one nominated peer."""
    campaign = get_object_or_404(
        ReviewCampaign, uuid=campaign_uuid, organization=request.organization
    )
    if not _can_manage_campaign(request.user, campaign, request.organization):
        raise PermissionDenied
    cycle = get_object_or_404(campaign.cycles.all(), uuid=cycle_uuid)
    token = get_object_or_404(
        cycle.tokens.all(), id=token_id, completed_at__isnull=True
    )
    if token.invitation_sent_at:
        from reviews.services import send_reminder_emails
        result = send_reminder_emails(cycle, [token.id])
    else:
        result = send_reviewer_invitations(cycle, [token.id])
    if result['sent']:
        messages.success(request, f'Invitation sent to {token.reviewer_email}.')
    elif result['errors']:
        messages.error(request, '; '.join(result['errors']))
    else:
        messages.info(request, 'No invitation was sent.')
    return redirect('admin_dashboard')


@login_required
@require_POST
def renew_review_campaign(request, campaign_uuid):
    source = get_object_or_404(
        ReviewCampaign, uuid=campaign_uuid, organization=request.organization
    )
    if not _can_manage_campaign(request.user, source, request.organization):
        raise PermissionDenied
    if source.cycles.exclude(status='completed').exists() or not source.cycles.exists():
        messages.error(request, 'Only completed campaigns can be renewed.')
        return redirect('admin_dashboard')

    from reviews.campaign_services import renew_campaign
    from reviews.services import send_campaign_invitations
    campaign = renew_campaign(source, request.user)
    transaction.on_commit(lambda c=campaign: send_campaign_invitations(c))
    messages.success(request, 'The campaign was renewed and invitations are being sent.')
    return redirect('admin_dashboard')


@login_required
@require_POST
def close_organizational_cycle_scope(request, cycle_uuid):
    """End the caller's authorized portion of an organizational cycle."""
    from reviews.models import OrganizationalReviewCycle

    organizational_cycle = get_object_or_404(
        OrganizationalReviewCycle,
        uuid=cycle_uuid,
        organization=request.organization,
    )
    is_organization_admin = request.user.has_perm(
        'accounts.can_manage_organization'
    )
    requested_scope = request.POST.get('scope', 'team')
    if requested_scope not in {'team', 'organization'}:
        raise PermissionDenied
    leadership_teams = led_teams(request.user, request.organization)
    if requested_scope == 'organization' and not is_organization_admin:
        raise PermissionDenied
    if requested_scope == 'team' and not leadership_teams.exists():
        raise PermissionDenied

    scoped_cycles = ReviewCycle.objects.filter(
        campaign__organizational_cycle=organizational_cycle,
        status='active',
    ).select_related('campaign', 'reviewee')
    if requested_scope == 'team':
        team_ids = leadership_teams.values_list('id', flat=True)
        scoped_reviewees = Reviewee.objects.for_organization(
            request.organization
        ).filter(
            Q(team_id__in=team_ids)
            | Q(team_memberships__team_id__in=team_ids)
            | Q(profile__managed_teams__id__in=team_ids)
        ).distinct()
        scoped_cycles = scoped_cycles.filter(reviewee__in=scoped_reviewees)
    active_cycles = list(scoped_cycles)
    if not active_cycles:
        messages.info(request, 'There are no active cycles in your scope.')
        return redirect('admin_dashboard')

    incomplete_items = []
    completion_counts = {}
    for cycle in active_cycles:
        required = (
            cycle.campaign.minimum_peer_reviewers
            if cycle.campaign.cycle_type == 'peer' else 1
        )
        completed = cycle.tokens.filter(completed_at__isnull=False).count()
        completion_counts[cycle.pk] = (completed, required)
        if completed < required:
            incomplete_items.append({
                'name': cycle.reviewee.name,
                'assessment': cycle.campaign.get_cycle_type_display(),
                'team': (
                    cycle.campaign.team.name
                    if cycle.campaign.team_id
                    else organizational_cycle.audience_label
                ),
                'completed': completed,
                'required': required,
            })
    if incomplete_items and request.POST.get('confirm_end') != '1':
        return render(
            request,
            'admin_dashboard/confirm_end_organisation_cycle.html',
            {
                'organizational_cycle': organizational_cycle,
                'scope': requested_scope,
                'scope_label': (
                    'the entire organisation'
                    if requested_scope == 'organization'
                    else 'the teams you manage'
                ),
                'incomplete_items': incomplete_items,
            },
        )

    from reports.services import generate_report

    reports_generated = 0
    reports_skipped = 0
    with transaction.atomic():
        for cycle in active_cycles:
            cycle.tokens.filter(
                claimed_at__isnull=True, completed_at__isnull=True
            ).delete()
            cycle.status = 'completed'
            cycle.save(update_fields=['status', 'updated_at'])
            completed, required = completion_counts[cycle.pk]
            if completed >= required:
                generate_report(cycle)
                reports_generated += 1
            else:
                reports_skipped += 1
        for campaign in organizational_cycle.campaigns.filter(status='active'):
            if not campaign.cycles.filter(status='active').exists():
                campaign.status = 'completed'
                campaign.save(update_fields=['status', 'updated_at'])
        if not organizational_cycle.campaigns.filter(status='active').exists():
            organizational_cycle.status = 'completed'
            organizational_cycle.save(update_fields=['status', 'updated_at'])

    scope_label = 'organisation' if requested_scope == 'organization' else 'team'
    report_summary = f' Generated {reports_generated} available report(s).'
    if reports_skipped:
        report_summary += (
            f' Skipped {reports_skipped} report(s) without enough completed feedback.'
        )
    messages.success(
        request,
        f'Ended the {scope_label} cycle scope.{report_summary}',
    )
    return redirect('admin_dashboard')


@login_required
@require_POST
def close_review_campaign(request, campaign_uuid):
    """End every active cycle in a campaign and generate its reports."""
    campaign = get_object_or_404(
        ReviewCampaign, uuid=campaign_uuid, organization=request.organization
    )
    if not _can_manage_campaign(request.user, campaign, request.organization):
        raise PermissionDenied
    active_cycles = list(campaign.cycles.filter(status='active').select_related('reviewee'))
    if not active_cycles:
        messages.info(request, 'This campaign is already complete.')
        return redirect('admin_dashboard')
    minimum_completed = (
        campaign.minimum_peer_reviewers if campaign.cycle_type == 'peer' else 1
    )
    without_feedback = [
        cycle.reviewee.name for cycle in active_cycles
        if cycle.tokens.filter(completed_at__isnull=False).count() < minimum_completed
    ]
    if without_feedback:
        messages.error(
            request,
            f'Cannot end this campaign yet. At least {minimum_completed} completed '
            'response(s) are required for: '
            + ', '.join(without_feedback),
        )
        return redirect('admin_dashboard')
    from reports.services import generate_report
    for cycle in active_cycles:
        cycle.tokens.filter(claimed_at__isnull=True, completed_at__isnull=True).delete()
        cycle.status = 'completed'
        cycle.save(update_fields=['status', 'updated_at'])
        generate_report(cycle)
    campaign.status = 'completed'
    campaign.save(update_fields=['status', 'updated_at'])
    if campaign.organizational_cycle_id:
        parent = campaign.organizational_cycle
        if not parent.campaigns.filter(status='active').exists():
            parent.status = 'completed'
            parent.save(update_fields=['status', 'updated_at'])
    messages.success(
        request,
        f'Ended {campaign.get_cycle_type_display()} campaign and generated its reports.',
    )
    return redirect('admin_dashboard')


def _render_legacy_cycle_form(request):
    # GET request - show the original individual reviewer form.
    org = request.organization or (request.user.profile.organization if hasattr(request.user, 'profile') else None)

    # Filter reviewees based on user permissions
    if hasattr(request.user, 'profile') and not request.user.profile.can_create_cycles_for_others:
        # User can only create cycles for themselves
        reviewees = visible_reviewees(
            request.user, Reviewee.objects.for_organization(org), org
        ).filter(
            is_active=True,
            email=request.user.email
        ).order_by('name')
    else:
        reviewees = visible_reviewees(
            request.user, Reviewee.objects.for_organization(org), org
        ).filter(is_active=True).order_by('name')

    # Only show questionnaires from user's organization.
    # Prefetch sections/questions so report_type_label doesn't N+1 per option.
    questionnaires = (
        Questionnaire.objects.for_organization(org)
        .prefetch_related('sections__questions')
        .order_by('-is_default', 'name')
    )

    context = {
        'reviewees': reviewees,
        'questionnaires': questionnaires,
        'can_create_for_others': hasattr(request.user, 'profile') and request.user.profile.can_create_cycles_for_others,
        'prefill_peer_email': request.GET.get('reviewer_email', '').strip(),
        'cycle_types': ReviewCycle.TYPE_CHOICES,
    }

    return render(request, 'admin_dashboard/review_cycle_form.html', context)


@login_required
def bulk_send_invitations(request):
    """Confirm + send reviewee notifications for cycles just created in bulk.

    The `review_cycle_create` view stashes the UUIDs of freshly-bulk-created
    cycles in the session under 'pending_invitation_cycles'. This view renders
    them for review on GET and defers the actual send on POST.
    """
    from reviews.services import send_reviewee_notifications

    org = request.organization
    uuids = request.session.get('pending_invitation_cycles', [])

    cycles_qs = visible_cycles(request.user, (
        ReviewCycle.objects
        .select_related('reviewee', 'questionnaire')
        .prefetch_related('questionnaire__sections__questions')
        .filter(uuid__in=uuids)
    ))
    if org:
        cycles_qs = cycles_qs.filter(reviewee__organization=org)
    cycles = list(cycles_qs.order_by('reviewee__name'))

    if request.method == 'POST':
        if not cycles:
            messages.info(request, 'No pending cycles to send invitations for.')
            return redirect('review_cycle_list')

        for cycle in cycles:
            transaction.on_commit(
                lambda c=cycle: send_reviewee_notifications(c, request)
            )

        # Clear the session once we've scheduled the sends.
        request.session.pop('pending_invitation_cycles', None)

        messages.success(
            request,
            f'Invitations are being sent for {len(cycles)} review cycle(s).'
        )
        return redirect('review_cycle_list')

    context = {
        'cycles': cycles,
    }
    return render(request, 'admin_dashboard/bulk_send_invitations.html', context)


@login_required
def review_cycle_detail(request, cycle_uuid):
    """View details of a review cycle"""
    cycle = get_cycle_or_404(request, cycle_uuid)

    tokens = cycle.tokens.all().order_by('category', 'created_at')

    # Group tokens by category
    tokens_by_category = {}
    for token in tokens:
        category = token.get_category_display()
        if category not in tokens_by_category:
            tokens_by_category[category] = []
        tokens_by_category[category].append(token)

    # Calculate completion stats
    total_tokens = tokens.count()
    completed_tokens = tokens.filter(completed_at__isnull=False).count()
    claimed_tokens = tokens.filter(claimed_at__isnull=False).count()
    pending_invites = tokens.filter(reviewer_email__isnull=False, invitation_sent_at__isnull=True).count()
    pending_reminders = tokens.filter(invitation_sent_at__isnull=False, completed_at__isnull=True).count()
    email_invited_count = tokens.filter(reviewer_email__isnull=False).exclude(category='self').count()
    completion_rate = (completed_tokens / total_tokens * 100) if total_tokens > 0 else 0
    claimed_completion_rate = (completed_tokens / claimed_tokens * 100) if claimed_tokens > 0 else 0

    # Get report if exists
    try:
        report = Report.objects.get(cycle=cycle)
        report_exists = True
    except Report.DoesNotExist:
        report = None
        report_exists = False

    context = {
        'cycle': cycle,
        'report': report,
        'tokens_by_category': tokens_by_category,
        'total_tokens': total_tokens,
        'completed_tokens': completed_tokens,
        'claimed_tokens': claimed_tokens,
        'pending_invites': pending_invites,
        'pending_reminders': pending_reminders,
        'email_invited_count': email_invited_count,
        'completion_rate': completion_rate,
        'claimed_completion_rate': claimed_completion_rate,
        'report_exists': report_exists,
        'can_manage_cycle': _can_manage_cycle(request.user, cycle, request.organization),
    }

    return render(request, 'admin_dashboard/review_cycle_detail.html', context)


@login_required
def generate_report_view(request, cycle_uuid):
    """Generate or regenerate report for a review cycle"""
    from reports.services import generate_report, send_report_ready_notification

    cycle = get_cycle_or_404(request, cycle_uuid)

    try:
        report = generate_report(cycle)

        # Send notification email to reviewee
        email_stats = send_report_ready_notification(report, request)

        success_msg = f'Report generated successfully for {cycle.reviewee.name}.'
        if email_stats['sent'] > 0:
            success_msg += ' Notification email sent.'
        if email_stats['errors']:
            success_msg += f' (Email errors: {", ".join(email_stats["errors"])})'

        messages.success(request, success_msg)
    except Exception as e:
        messages.error(request, f'Error generating report: {str(e)}')

    return redirect('review_cycle_detail', cycle_uuid=cycle.uuid)


@login_required
def close_cycle(request, cycle_uuid):
    """Close/complete a review cycle and generate report if possible"""
    if request.method != 'POST':
        return redirect('review_cycle_detail', cycle_uuid=cycle_uuid)

    cycle = get_cycle_or_404(request, cycle_uuid)

    if not _can_manage_cycle(request.user, cycle, request.organization):
        raise PermissionDenied

    if cycle.status != 'active':
        messages.warning(request, 'This cycle is already completed.')
        return redirect('review_cycle_detail', cycle_uuid=cycle.uuid)

    # Check if there are any completed reviews
    completed_count = cycle.tokens.filter(completed_at__isnull=False).count()

    required_count = (
        cycle.campaign.minimum_peer_reviewers
        if cycle.campaign_id and cycle.campaign.cycle_type == 'peer' else 1
    )
    if completed_count < required_count:
        messages.error(
            request,
            f'Cannot close cycle: At least {required_count} completed review(s) are required.',
        )
        return redirect('review_cycle_detail', cycle_uuid=cycle_uuid)

    # Remove unclaimed tokens (tokens that are still active but not claimed)
    # Keep claimed tokens as an indication that the report was closed while people were still working
    unclaimed_tokens = cycle.tokens.filter(claimed_at__isnull=True, completed_at__isnull=True)
    unclaimed_count = unclaimed_tokens.count()
    unclaimed_tokens.delete()

    # Mark cycle as completed
    cycle.status = 'completed'
    cycle.save()
    _synchronize_cycle_parent_status(cycle)

    # Generate report
    from reports.services import generate_report, send_report_ready_notification
    try:
        report = generate_report(cycle)

        # Send notification email to reviewee
        email_stats = send_report_ready_notification(report, request)

        success_msg = f'Cycle closed and report generated for {cycle.reviewee.name}.'
        if email_stats['sent'] > 0:
            success_msg += ' Notification email sent.'

        messages.success(request, success_msg)
    except Exception as e:
        messages.error(request, f'Cycle closed but error generating report: {str(e)}')

    return redirect('review_cycle_detail', cycle_uuid=cycle_uuid)


def _can_manage_cycle(user, cycle, organization):
    if cycle.campaign_id:
        return _can_manage_campaign(user, cycle.campaign, organization)
    return bool(
        user.has_perm('accounts.can_manage_organization')
        or cycle.created_by_id == user.id
    )


@login_required
@require_POST
def delete_review_cycle(request, cycle_uuid):
    """Permanently delete a cycle after an explicit typed confirmation."""
    cycle = get_cycle_or_404(request, cycle_uuid)
    if not _can_manage_cycle(request.user, cycle, request.organization):
        raise PermissionDenied
    if request.POST.get('confirmation', '').strip().upper() != 'DELETE':
        messages.error(request, 'Type DELETE to confirm cycle deletion.')
        return redirect('review_cycle_detail', cycle_uuid=cycle.uuid)
    reviewee_name = cycle.reviewee.name
    campaign = cycle.campaign
    cycle.delete()
    if campaign and not campaign.cycles.exists():
        campaign.delete()
    messages.success(request, f'Deleted the review cycle for {reviewee_name}.')
    return redirect('review_cycle_list')


@login_required
def send_reminder_form(request, cycle_uuid):
    """Show form to send reminders for pending reviews"""
    cycle = get_cycle_or_404(request, cycle_uuid)

    # Get pending tokens
    pending_tokens = cycle.tokens.filter(completed_at__isnull=True).order_by('category')

    context = {
        'cycle': cycle,
        'pending_tokens': pending_tokens,
    }

    return render(request, 'admin_dashboard/send_reminder.html', context)


@login_required
def manage_invitations(request, cycle_uuid):
    """Manage reviewer invitations for a cycle"""
    cycle = get_cycle_or_404(request, cycle_uuid)

    # Group tokens by category
    tokens_by_category = {}
    for token in cycle.tokens.all().order_by('category'):
        category = token.get_category_display()
        if category not in tokens_by_category:
            tokens_by_category[category] = []
        tokens_by_category[category].append(token)

    # Statistics
    total_tokens = cycle.tokens.count()
    assigned_tokens = cycle.tokens.filter(reviewer_email__isnull=False).count()
    sent_tokens = cycle.tokens.filter(invitation_sent_at__isnull=False).count()
    completed_tokens = cycle.tokens.filter(completed_at__isnull=False).count()

    context = {
        'cycle': cycle,
        'tokens_by_category': tokens_by_category,
        'total_tokens': total_tokens,
        'assigned_tokens': assigned_tokens,
        'sent_tokens': sent_tokens,
        'completed_tokens': completed_tokens,
    }

    return render(request, 'admin_dashboard/manage_invitations.html', context)


@login_required
def assign_invitations(request, cycle_uuid):
    """Assign email addresses to reviewer tokens (creating tokens dynamically)"""
    from django.core.validators import validate_email
    from django.core.exceptions import ValidationError

    cycle = get_cycle_or_404(request, cycle_uuid)

    if request.method == 'POST':
        # Parse email assignments by category
        import re
        email_assignments = {}

        for category_code, category_display in ReviewerToken.CATEGORY_CHOICES:
            emails_data = request.POST.get(f'{category_code}_emails', '').strip()
            if emails_data:
                emails = re.split(r'[,\n]+', emails_data)
                validated_emails = []
                for e in emails:
                    e = e.strip()
                    if e:
                        try:
                            validate_email(e)
                            validated_emails.append(e)
                        except ValidationError:
                            messages.warning(request, f'Invalid email skipped in {category_display}: {e}')
                email_assignments[category_code] = validated_emails
            else:
                email_assignments[category_code] = []

        # Create tokens dynamically based on email count
        tokens_created = 0
        for category_code, emails in email_assignments.items():
            if not emails:
                continue

            # Get existing unassigned tokens for this category (only count tokens without emails)
            existing_unassigned = cycle.tokens.filter(
                category=category_code,
                reviewer_email__isnull=True
            ).count()
            needed_count = len(emails)

            # Create additional tokens if needed
            if needed_count > existing_unassigned:
                for _ in range(needed_count - existing_unassigned):
                    ReviewerToken.objects.create(
                        cycle=cycle,
                        category=category_code
                    )
                    tokens_created += 1

        # Assign tokens to emails with randomization
        stats = assign_tokens_to_emails(cycle, email_assignments)

        if stats['errors']:
            for error in stats['errors']:
                messages.error(request, error)

        # Check if user wants to send invitations immediately
        action = request.POST.get('action', 'assign')
        if action == 'assign' and stats['assigned'] > 0:
            # Send invitations immediately
            send_stats = send_reviewer_invitations(cycle)

            if send_stats['sent'] > 0:
                messages.success(request, f'Successfully invited {stats["assigned"]} reviewer(s) and sent {send_stats["sent"]} email(s).')
            else:
                messages.success(request, f'Successfully assigned {stats["assigned"]} email(s). Invitations will be sent separately.')

            if send_stats['errors']:
                for error in send_stats['errors']:
                    messages.error(request, error)
        elif stats['assigned'] > 0:
            messages.success(request, f'Successfully assigned {stats["assigned"]} email(s). No invitations sent yet.')

        return redirect('review_cycle_detail', cycle_uuid=cycle.uuid)

    return redirect('manage_invitations', cycle_uuid=cycle.uuid)


@login_required
def send_invitations(request, cycle_uuid):
    """Send email invitations to assigned reviewers"""
    cycle = get_cycle_or_404(request, cycle_uuid)

    if request.method == 'POST':
        # Send invitations
        stats = send_reviewer_invitations(cycle)

        if stats['errors']:
            for error in stats['errors']:
                messages.error(request, error)

        if stats['sent'] > 0:
            messages.success(request, f'Successfully sent {stats["sent"]} invitation email(s).')
        elif stats['sent'] == 0 and not stats['errors']:
            messages.info(request, 'No pending invitations to send.')

        return redirect('review_cycle_detail', cycle_uuid=cycle.uuid)

    return redirect('review_cycle_detail', cycle_uuid=cycle.uuid)


@login_required
def send_reminder(request, cycle_uuid):
    """Send reminder emails for pending reviews"""
    from reviews.services import send_reminder_emails

    cycle = get_cycle_or_404(request, cycle_uuid)

    if request.method == 'POST':
        # Send reminders
        stats = send_reminder_emails(cycle)

        if stats['errors']:
            for error in stats['errors']:
                messages.error(request, error)

        if stats['sent'] > 0:
            messages.success(request, f'Successfully sent {stats["sent"]} reminder(s).')
        elif stats['sent'] == 0 and not stats['errors']:
            messages.info(request, 'No pending reminders to send.')

        return redirect('review_cycle_detail', cycle_uuid=cycle.uuid)

    return redirect('send_reminder_form', cycle_uuid=cycle.uuid)


@login_required
@require_POST
def send_individual_reminder(request, cycle_uuid, token_id):
    """Send a reminder email to a specific reviewer"""
    from django.core.mail import EmailMultiAlternatives
    from django.template.loader import render_to_string
    from django.conf import settings

    cycle = get_cycle_or_404(request, cycle_uuid)

    try:
        # Get the specific token
        token = ReviewerToken.objects.get(id=token_id, cycle=cycle)

        # Check if token has email and invitation was sent
        if not token.reviewer_email:
            messages.error(request, 'Cannot send reminder: no email assigned to this reviewer.')
            return redirect('review_cycle_detail', cycle_uuid=cycle.uuid)

        if not token.invitation_sent_at:
            messages.error(request, 'Cannot send reminder: invitation not sent yet.')
            return redirect('review_cycle_detail', cycle_uuid=cycle.uuid)

        if token.is_completed:
            messages.info(request, 'This reviewer has already completed their feedback.')
            return redirect('review_cycle_detail', cycle_uuid=cycle.uuid)

        # Build feedback URL
        feedback_url = request.build_absolute_uri(
            f'/feedback/{token.token}/'
        )

        # Render email
        context = {
            'reviewee_name': cycle.reviewee.name,
            'questionnaire_name': cycle.questionnaire.name,
            'feedback_url': feedback_url,
            'category': token.get_category_display(),
        }

        html_content = render_to_string('emails/reviewer_reminder.html', context)
        text_content = render_to_string('emails/reviewer_reminder.txt', context)

        # Send email
        from_email = settings.DEFAULT_FROM_EMAIL
        subject = f'Reminder: Feedback Request for {cycle.reviewee.name}'

        email = EmailMultiAlternatives(
            subject=subject,
            body=text_content,
            from_email=from_email,
            to=[token.reviewer_email]
        )
        email.attach_alternative(html_content, "text/html")
        email.send()

        # Update last reminder sent timestamp
        from django.utils import timezone
        token.last_reminder_sent_at = timezone.now()
        token.save()

        messages.success(request, f'Reminder sent to reviewer.')

    except ReviewerToken.DoesNotExist:
        messages.error(request, 'Reviewer token not found.')
    except Exception as e:
        messages.error(request, f'Error sending reminder: {str(e)}')

    return redirect('review_cycle_detail', cycle_uuid=cycle.uuid)


@login_required
@require_POST
def remove_reviewer_token(request, cycle_uuid, token_id):
    """Remove a reviewer token from a cycle (only if not started) - Admin only"""
    # Check organization admin permission
    if not request.user.has_perm('accounts.can_manage_organization'):
        messages.error(request, 'You do not have permission to remove reviewers.')
        return redirect('review_cycle_detail', cycle_uuid=cycle_uuid)

    cycle = get_cycle_or_404(request, cycle_uuid)

    try:
        # Get the specific token
        token = ReviewerToken.objects.get(id=token_id, cycle=cycle)

        # Only allow deletion if reviewer hasn't started (no claimed_at or completed_at)
        if token.claimed_at or token.completed_at:
            messages.error(request, 'Cannot remove: reviewer has already started or completed their feedback.')
            return redirect('review_cycle_detail', cycle_uuid=cycle.uuid)

        # Store info for success message
        category = token.get_category_display()
        email = token.reviewer_email if token.reviewer_email else "unclaimed token"

        # Delete the token
        token.delete()

        messages.success(request, f'Removed {category} reviewer ({email}) from cycle.')

        # Check if all remaining tokens are completed
        remaining_tokens = cycle.tokens.all()
        if remaining_tokens.exists():
            all_completed = not remaining_tokens.filter(completed_at__isnull=True).exists()

            if all_completed and cycle.status == 'active':
                # Auto-close the cycle
                cycle.status = 'completed'
                cycle.save()

                # Auto-generate report
                from reports.services import generate_report, send_report_ready_notification
                try:
                    report = generate_report(cycle)

                    # Send notification email if organization setting enabled
                    organization = cycle.reviewee.organization
                    if organization and organization.auto_send_report_email:
                        email_stats = send_report_ready_notification(report, request)
                        if email_stats.get('errors'):
                            print(f"Errors sending report email for cycle {cycle.id}: {email_stats['errors']}")

                    messages.success(request, 'Cycle automatically closed and report generated (all remaining reviewers completed).')
                except Exception as e:
                    # Log error but don't fail the removal
                    print(f"Error auto-generating report for cycle {cycle.id}: {e}")
                    messages.warning(request, f'Cycle closed but error generating report: {str(e)}')

    except ReviewerToken.DoesNotExist:
        messages.error(request, 'Reviewer token not found.')
    except Exception as e:
        messages.error(request, f'Error removing reviewer: {str(e)}')

    return redirect('review_cycle_detail', cycle_uuid=cycle.uuid)


@login_required
@require_POST
def send_report_email(request, cycle_uuid):
    """Send report notification email to reviewee"""
    from reports.services import send_report_ready_notification

    cycle = get_cycle_or_404(request, cycle_uuid)

    # Check if report exists
    try:
        report = Report.objects.get(cycle=cycle)
    except Report.DoesNotExist:
        messages.error(request, 'No report found for this cycle. Please generate the report first.')
        return redirect('review_cycle_detail', cycle_uuid=cycle.uuid)

    # Send notification email
    email_stats = send_report_ready_notification(report, request)

    if email_stats['sent'] > 0:
        messages.success(request, f'Report email sent to {cycle.reviewee.name} at {cycle.reviewee.email}.')
    else:
        if email_stats['errors']:
            for error in email_stats['errors']:
                messages.error(request, f'Failed to send email: {error}')
        else:
            messages.error(request, 'Failed to send email.')

    return redirect('review_cycle_detail', cycle_uuid=cycle.uuid)


@login_required
def settings_view(request):
    """Organization and SMTP settings page"""
    # Use the organization from the middleware (set based on user's profile)
    organization = request.organization

    if not organization:
        messages.error(request, 'No organization found. Please run setup first.')
        return redirect('admin_dashboard')

    # Fields the environment owns are read-only here — setup_organization
    # rewrites them on every container start, so an edit made here would
    # silently disappear on the next restart.
    locked = env_managed_fields()

    def editable(field):
        return field not in locked

    if request.method == 'POST':
        # Check permission to modify organization settings
        if not request.user.has_perm('accounts.can_manage_organization'):
            messages.error(request, 'You do not have permission to modify organization settings.')
            return redirect('settings')

        # Get which section is being updated
        section = request.POST.get('section', 'all')

        try:
            if section == 'organization':
                # Update organization details
                if editable('name'):
                    organization.name = request.POST.get('name', organization.name)
                if editable('email'):
                    organization.email = request.POST.get('email', organization.email)
                organization.save()
                messages.success(request, 'Organisation details updated successfully.')

            elif section == 'registration':
                # Update registration settings
                organization.allow_registration = request.POST.get('allow_registration') == 'on'
                organization.default_users_can_create_cycles = request.POST.get('default_users_can_create_cycles') == 'on'
                organization.save()
                messages.success(request, 'Registration settings updated successfully.')

            elif section == 'reports':
                # Update report settings
                organization.auto_send_report_email = request.POST.get('auto_send_report_email') == 'on'
                organization.save()
                messages.success(request, 'Report settings updated successfully.')

            elif section == 'email':
                # Update SMTP settings
                if editable('smtp_host'):
                    organization.smtp_host = request.POST.get('smtp_host', '')
                if editable('smtp_port'):
                    try:
                        organization.smtp_port = int(request.POST.get('smtp_port', 587))
                    except (ValueError, TypeError):
                        organization.smtp_port = 587
                if editable('smtp_username'):
                    organization.smtp_username = request.POST.get('smtp_username', '')

                # Only update password if provided
                smtp_password = request.POST.get('smtp_password', '')
                if smtp_password and editable('smtp_password'):
                    organization.smtp_password = smtp_password

                if editable('smtp_use_tls'):
                    organization.smtp_use_tls = request.POST.get('smtp_use_tls') == 'on'
                if editable('from_email'):
                    organization.from_email = request.POST.get('from_email', organization.from_email)
                organization.save()
                messages.success(request, 'Email settings updated successfully.')

            if locked:
                messages.info(request, (
                    'Some settings are managed by environment variables and were '
                    'not changed: ' + ', '.join(sorted(set(locked.values())))
                ))

            return redirect('settings')
        except Exception as e:
            messages.error(request, f'Error updating settings: {str(e)}')

    # Get subscription information if exists
    subscription = None
    try:
        from subscriptions.models import Subscription
        subscription = organization.subscription
        print(f"DEBUG: Found subscription for {organization.name}: {subscription.plan.name} - {subscription.status}")
    except (Subscription.DoesNotExist, AttributeError) as e:
        print(f"DEBUG: No subscription for {organization.name}: {type(e).__name__}")
    except Exception as e:
        print(f"DEBUG: Error getting subscription: {type(e).__name__}: {e}")

    print(f"DEBUG: Passing subscription to template: {subscription}")

    # Check if current user has organization admin permission
    is_org_admin = request.user.has_perm('accounts.can_manage_organization')

    # Count total admin users
    from accounts.models import UserProfile
    admin_profiles = UserProfile.objects.for_organization(organization).select_related('user')
    admin_count = sum(1 for p in admin_profiles if p.user.has_perm('accounts.can_manage_organization'))

    # Get API tokens and webhooks for this organization
    from api.models import APIToken, WebhookEndpoint
    api_tokens = APIToken.objects.for_organization(organization).order_by('-created_at')
    webhooks = WebhookEndpoint.objects.for_organization(organization).order_by('-created_at')

    # Check if there's a newly created token to display
    new_token = request.session.pop('new_api_token', None)
    new_token_name = request.session.pop('new_api_token_name', None)

    context = {
        'organization': organization,
        'subscription': subscription,
        'is_org_admin': is_org_admin,
        'admin_count': admin_count,
        'is_last_admin': is_org_admin and admin_count == 1,
        'api_tokens': api_tokens,
        'webhooks': webhooks,
        'new_token': new_token,
        'new_token_name': new_token_name,
        'locked_fields': locked,
    }

    if is_org_admin:
        organization_roles = list(
            OrganizationRole.objects.for_organization(organization)
            .select_related('parent').annotate(member_count=Count('members'))
            .order_by('name')
        )
        for organization_role in organization_roles:
            organization_role.effective_permission_names = [
                field.replace('can_', '').replace('_', ' ').title()
                for field in OrganizationRole.PERMISSION_FIELDS
                if field in organization_role.effective_permissions()
            ]
        user_query = request.GET.get('user_q', '').strip()
        people_queryset = (
            UserProfile.objects.for_organization(organization)
            .select_related('user')
            .prefetch_related('reviewee__teams', 'managed_teams', 'team_lead_grants__team')
        )
        if user_query:
            people_queryset = people_queryset.filter(
                Q(user__first_name__icontains=user_query)
                | Q(user__last_name__icontains=user_query)
                | Q(user__email__icontains=user_query)
            )
        people = sorted(
            people_queryset,
            key=lambda item: (item.user.get_full_name() or item.user.email).casefold(),
        )
        people_page = Paginator(people, 25).get_page(request.GET.get('users_page'))
        for profile in people_page:
            profile.is_org_admin = profile.user.has_perm(
                'accounts.can_manage_organization'
            )
            reviewee = getattr(profile, 'reviewee', None)
            profile.team_names = list(reviewee.teams.values_list('name', flat=True)) if reviewee else []
            profile.team_ids = list(reviewee.teams.values_list('id', flat=True)) if reviewee else []
            if reviewee and reviewee.team and reviewee.team.name not in profile.team_names:
                profile.team_names.insert(0, reviewee.team.name)
            if reviewee and reviewee.team_id and reviewee.team_id not in profile.team_ids:
                profile.team_ids.insert(0, reviewee.team_id)
            profile.visible_team_names = profile.team_names[:2]
            profile.extra_team_count = max(0, len(profile.team_names) - 2)
            profile.team_names_tooltip = ', '.join(profile.team_names)
            profile.managed_team_list = list(
                profile.managed_teams.filter(archived_at__isnull=True)
            )
            profile.lead_grants_list = list(profile.team_lead_grants.all())
            profile.lead_team_ids = [grant.team_id for grant in profile.lead_grants_list]
            profile.is_team_leader = bool(
                profile.managed_team_list or profile.lead_grants_list
            )
        context.update({
            'organization_roles': organization_roles,
            'role_permission_fields': [
                (field, field.replace('can_', '').replace('_', ' ').title())
                for field in OrganizationRole.PERMISSION_FIELDS
            ],
            'organization_people': people_page,
            'user_query': user_query,
            'organization_teams': Team.objects.for_organization(organization).order_by('name'),
            'organization_manager_candidates': UserProfile.objects.for_organization(
                organization
            ).filter(user__is_active=True).select_related('user').order_by(
                'user__first_name', 'user__last_name', 'user__email'
            ),
            'pending_people_invitations': OrganizationInvitation.objects.filter(
                organization=organization, accepted_at__isnull=True
            ).select_related('team').order_by('email'),
        })

    return render(request, 'admin_dashboard/settings.html', context)


@login_required
@require_POST
def manage_organization_role(request):
    """Create, update, or remove a hierarchical role in this organization."""
    if not request.user.has_perm('accounts.can_manage_organization'):
        raise PermissionDenied
    organization = request.organization
    if not organization:
        raise Http404

    action = request.POST.get('action')
    role = None
    if action in {'update', 'delete'}:
        role = get_object_or_404(
            OrganizationRole.objects.for_organization(organization),
            pk=request.POST.get('role_id'),
        )

    if action in {'create', 'update'}:
        name = request.POST.get('name', '').strip()
        if not name:
            messages.error(request, 'Enter a role name.')
            return HttpResponseRedirect(reverse('settings') + '#organization')
        duplicate = OrganizationRole.objects.for_organization(organization).filter(
            name__iexact=name
        )
        if role:
            duplicate = duplicate.exclude(pk=role.pk)
        if duplicate.exists():
            messages.error(request, 'A role with that name already exists.')
            return HttpResponseRedirect(reverse('settings') + '#organization')

        parent_id = request.POST.get('parent_role')
        parent = None
        if parent_id:
            parent = get_object_or_404(
                OrganizationRole.objects.for_organization(organization), pk=parent_id
            )
        role = role or OrganizationRole(organization=organization)
        role.name = name
        role.parent = parent
        for field in OrganizationRole.PERMISSION_FIELDS:
            setattr(role, field, request.POST.get(field) == 'on')
        try:
            role.full_clean()
        except ValidationError as exc:
            messages.error(request, '; '.join(exc.messages))
            return HttpResponseRedirect(reverse('settings') + '#organization')
        role.save()
        messages.success(request, f'Role {role.name} saved.')
    elif action == 'delete':
        if request.POST.get('confirmation', '').strip().lower() != 'delete':
            messages.error(request, 'Type delete to confirm removing this role.')
        elif role.children.exists():
            messages.error(request, 'Reassign or remove child roles before deleting this role.')
        else:
            name = role.name
            role.delete()
            messages.success(request, f'Role {name} removed. Its members are now standard members.')
    else:
        raise PermissionDenied
    return HttpResponseRedirect(reverse('settings') + '#organization')


@login_required
@require_POST
def manage_organization_person(request):
    """Manage organization access without deleting a person's review history."""
    from accounts.permissions import (
        assign_organization_admin, assign_organization_member,
        remove_from_all_org_groups,
    )

    if not request.user.has_perm('accounts.can_manage_organization'):
        raise PermissionDenied
    organization = request.organization
    if not organization:
        raise Http404

    action = request.POST.get('action')
    if action == 'cancel_invitation':
        invitation = get_object_or_404(
            OrganizationInvitation,
            pk=request.POST.get('invitation'),
            organization=organization,
            accepted_at__isnull=True,
        )
        email = invitation.email
        invitation.delete()
        messages.success(request, f'Pending invitation for {email} removed.')
        return HttpResponseRedirect(reverse('settings') + '#people')

    profile = get_object_or_404(
        UserProfile.objects.select_related('user'),
        pk=request.POST.get('user_profile_id'),
        organization=organization,
    )
    target = profile.user
    if target.is_superuser:
        messages.error(request, 'Super administrator access cannot be changed here.')
        return HttpResponseRedirect(reverse('settings') + '#people')

    if action == 'update_user':
        role = request.POST.get('role')
        is_custom_role = role and role.startswith('custom:')
        custom_role = None
        if is_custom_role:
            custom_role = get_object_or_404(
                OrganizationRole.objects.for_organization(organization),
                pk=role.removeprefix('custom:'),
            )
        if role not in {'admin', 'team_leader', 'member'} and not custom_role:
            messages.error(request, 'Select a valid role.')
            return HttpResponseRedirect(reverse('settings') + '#people')
        if role != 'admin' and target.has_perm('accounts.can_manage_organization'):
            profiles = UserProfile.objects.for_organization(organization).select_related('user')
            if sum(p.user.has_perm('accounts.can_manage_organization') for p in profiles) <= 1:
                messages.error(request, 'The last organization administrator cannot be demoted.')
                return HttpResponseRedirect(reverse('settings') + '#people')
        if target == request.user and request.POST.get('status') != 'active':
            messages.error(request, 'You cannot make your own account inactive.')
            return HttpResponseRedirect(reverse('settings') + '#people')
        from django.contrib.auth import get_user_model
        from django.core.validators import validate_email

        email = request.POST.get('email', '').strip().lower()
        try:
            validate_email(email)
        except ValidationError:
            messages.error(request, 'Enter a valid email address.')
            return HttpResponseRedirect(reverse('settings') + '#people')
        if get_user_model().objects.filter(email__iexact=email).exclude(pk=target.pk).exists():
            messages.error(request, 'That email address is already used by another account.')
            return HttpResponseRedirect(reverse('settings') + '#people')
        reviewee = getattr(profile, 'reviewee', None)
        if Reviewee.objects.for_organization(organization).filter(
            email__iexact=email
        ).exclude(pk=getattr(reviewee, 'pk', None)).exists():
            messages.error(request, 'That email address is already used by another person.')
            return HttpResponseRedirect(reverse('settings') + '#people')

        selected_ids = {int(value) for value in request.POST.getlist('teams') if value.isdigit()}
        teams = list(Team.objects.for_organization(organization).filter(id__in=selected_ids))
        if len(teams) != len(selected_ids):
            raise PermissionDenied
        add_team = request.POST.get('add_team', '')
        new_team_name = request.POST.get('new_team_name', '').strip()
        if add_team.isdigit():
            extra_team = get_object_or_404(
                Team.objects.for_organization(organization), pk=int(add_team)
            )
            if extra_team.id not in selected_ids:
                teams.append(extra_team)
        elif add_team == '__new__':
            if not new_team_name:
                messages.error(request, 'Enter a name for the new team.')
                return HttpResponseRedirect(reverse('settings') + '#people')
            if Team.objects.for_organization(organization).filter(
                name__iexact=new_team_name
            ).exists():
                messages.error(request, 'A team with that name already exists.')
                return HttpResponseRedirect(reverse('settings') + '#people')
        elif add_team:
            raise PermissionDenied
        managed_teams = list(profile.managed_teams.filter(archived_at__isnull=True))
        reassignments = {}
        for managed_team in managed_teams:
            replacement_id = request.POST.get(f'manager_for_{managed_team.id}')
            if replacement_id:
                replacement = get_object_or_404(
                    UserProfile.objects.for_organization(organization).filter(
                        user__is_active=True
                    ),
                    pk=replacement_id,
                )
                reassignments[managed_team.id] = replacement
        selected_by_id = {team.id: team for team in teams}
        selected_by_id.update({
            team.id: team for team in managed_teams
            if team.id not in reassignments or reassignments[team.id] == profile
        })
        teams = list(selected_by_id.values())
        can_create = request.POST.get('can_create_cycles_for_others') == 'on'

        lead_team_ids = {
            int(value) for value in request.POST.getlist('lead_teams') if value.isdigit()
        }
        lead_teams = list(
            Team.objects.for_organization(organization).filter(id__in=lead_team_ids)
        )
        if len(lead_teams) != len(lead_team_ids):
            raise PermissionDenied
        if role == 'team_leader' and not (lead_teams or managed_teams):
            messages.error(request, 'Select at least one team for the Team Leader role.')
            return HttpResponseRedirect(reverse('settings') + '#people')
        include_descendants = request.POST.get('include_lead_descendants') == 'on'

        with transaction.atomic():
            if add_team == '__new__':
                extra_team = Team(
                    organization=organization,
                    name=new_team_name,
                    manager=request.user.profile,
                )
                extra_team.full_clean()
                extra_team.save()
                teams.append(extra_team)
            for managed_team in managed_teams:
                replacement = reassignments.get(managed_team.id)
                if replacement and replacement != profile:
                    managed_team.manager = replacement
                    managed_team.save(update_fields=['manager', 'updated_at'])
            if role == 'admin':
                assign_organization_admin(target)
            else:
                can_create = (
                    can_create or role == 'team_leader'
                    or bool(custom_role and 'can_create_cycles' in custom_role.effective_permissions())
                )
                assign_organization_member(target, can_create_cycles_for_others=can_create)
            if role in {'admin', 'team_leader'}:
                TeamLeadGrant.objects.filter(profile=profile).exclude(
                    team_id__in=lead_team_ids
                ).delete()
                for lead_team in lead_teams:
                    TeamLeadGrant.objects.update_or_create(
                        profile=profile,
                        team=lead_team,
                        defaults={'include_descendants': include_descendants},
                    )
            elif role == 'member':
                TeamLeadGrant.objects.filter(profile=profile).delete()
            profile.organization_role = custom_role
            target.email = email
            target.is_active = request.POST.get('status') == 'active'
            target.save(update_fields=['email', 'is_active'])
            profile.can_create_cycles_for_others = can_create
            profile.save(update_fields=[
                'can_create_cycles_for_others', 'organization_role', 'updated_at'
            ])
            if not reviewee:
                reviewee = Reviewee.objects.create(
                    organization=organization,
                    profile=profile,
                    name=target.get_full_name() or email,
                    email=email,
                )
            reviewee.email = email
            reviewee.is_active = True
            reviewee.team = teams[0] if teams else None
            reviewee.save(update_fields=['email', 'is_active', 'team', 'updated_at'])
            reviewee.teams.set(teams)
            transfer_notifications = [
                (
                    managed_team.name,
                    replacement.user.get_full_name() or replacement.user.email,
                    replacement.user.email,
                    request.user.get_full_name() or request.user.email,
                )
                for managed_team in managed_teams
                if (replacement := reassignments.get(managed_team.id))
                and replacement != profile
            ]
            if transfer_notifications:
                transaction.on_commit(lambda: _send_team_ownership_notifications(
                    organization, transfer_notifications
                ))
        messages.success(request, f'User updated for {target.get_full_name() or target.email}.')

    elif action == 'remove':
        if target == request.user:
            messages.error(request, 'You cannot remove your own account.')
        elif target.has_perm('accounts.can_manage_organization') and sum(
            p.user.has_perm('accounts.can_manage_organization')
            for p in UserProfile.objects.for_organization(organization).select_related('user')
        ) <= 1:
            messages.error(request, 'The last organization administrator cannot be removed.')
        elif request.POST.get('confirmation', '').strip().lower() != 'delete':
            messages.error(request, 'Type delete to confirm removing this user.')
        else:
            display_name = target.get_full_name() or target.email
            managed_teams = list(profile.managed_teams.filter(archived_at__isnull=True))
            replacements = {}
            for managed_team in managed_teams:
                replacement_id = request.POST.get(f'manager_for_{managed_team.id}')
                if not replacement_id:
                    messages.error(
                        request,
                        f'Select a new manager for {managed_team.name} before removing this user.',
                    )
                    return HttpResponseRedirect(reverse('settings') + '#people')
                replacements[managed_team.id] = get_object_or_404(
                    UserProfile.objects.for_organization(organization).filter(
                        user__is_active=True
                    ).exclude(pk=profile.pk),
                    pk=replacement_id,
                )
            with transaction.atomic():
                for managed_team in managed_teams:
                    managed_team.manager = replacements[managed_team.id]
                    managed_team.save(update_fields=['manager', 'updated_at'])
                reviewee = getattr(profile, 'reviewee', None)
                if reviewee:
                    reviewee.teams.clear()
                    reviewee.profile = None
                    reviewee.team = None
                    reviewee.reporting_manager = None
                    reviewee.is_active = False
                    reviewee.save(update_fields=[
                        'profile', 'team', 'reporting_manager', 'is_active', 'updated_at'
                    ])
                remove_from_all_org_groups(target)
                profile.delete()
                target.is_active = False
                target.save(update_fields=['is_active'])
            messages.success(request, f'{display_name} removed from {organization.name}.')
    else:
        messages.error(request, 'Unknown people-management action.')

    return HttpResponseRedirect(reverse('settings') + '#people')


@login_required
def gdpr_management(request):
    """GDPR data management and deletion for organization admins"""
    # Check permission
    if not request.user.has_perm('accounts.can_manage_organization'):
        messages.error(request, 'You do not have permission to access GDPR management.')
        return redirect('team_list')

    org = request.organization
    if not org:
        messages.error(request, 'No organization found.')
        return redirect('admin_dashboard')

    # Get tab parameter (users or reviewees)
    active_tab = request.GET.get('tab', 'reviewees')

    # Get per_page from request
    per_page = request.GET.get('per_page', '25')
    try:
        per_page = int(per_page)
        if per_page not in [25, 50, 100]:
            per_page = 25
    except (ValueError, TypeError):
        per_page = 25

    if active_tab == 'users':
        # List users with data summaries (include GDPR-deleted for audit purposes)
        users_qs = UserProfile.objects.for_organization(
            org, include_deleted=True
        ).select_related('user').order_by('-user__date_joined')

        # Paginate
        paginator = Paginator(users_qs, per_page)
        page = request.GET.get('page')
        try:
            users = paginator.page(page)
        except PageNotAnInteger:
            users = paginator.page(1)
        except EmptyPage:
            users = paginator.page(paginator.num_pages)

        # Add data summaries
        for user_profile in users:
            try:
                user_profile.gdpr_summary = GDPRDeletionService.get_user_data_summary(user_profile.user.id)
            except:
                user_profile.gdpr_summary = None

        context = {
            'active_tab': 'users',
            'users': users,
            'reviewees': None,
            'per_page': per_page,
        }
    else:
        # List reviewees with data summaries (include GDPR-deleted for audit purposes)
        reviewees_qs = Reviewee.objects.for_organization(org, include_deleted=True).select_related('organization').order_by('-created_at')

        # Paginate
        paginator = Paginator(reviewees_qs, per_page)
        page = request.GET.get('page')
        try:
            reviewees = paginator.page(page)
        except PageNotAnInteger:
            reviewees = paginator.page(1)
        except EmptyPage:
            reviewees = paginator.page(paginator.num_pages)

        # Add data summaries
        for reviewee in reviewees:
            try:
                reviewee.gdpr_summary = GDPRDeletionService.get_reviewee_data_summary(reviewee.id)
            except:
                reviewee.gdpr_summary = None

        context = {
            'active_tab': 'reviewees',
            'users': None,
            'reviewees': reviewees,
            'per_page': per_page,
        }

    return render(request, 'admin_dashboard/gdpr_management.html', context)


@login_required
@require_POST
def gdpr_delete_user_view(request, user_id):
    """Delete or anonymize a user (GDPR)"""
    # Check permission
    if not request.user.has_perm('accounts.can_manage_organization'):
        messages.error(request, 'You do not have permission to delete users.')
        return redirect('gdpr_management')

    org = request.organization
    if not org:
        messages.error(request, 'No organization found.')
        return redirect('admin_dashboard')

    try:
        # Get the user profile to verify organization
        user_profile = get_object_or_404(UserProfile, user_id=user_id, organization=org)
        target_user = user_profile.user

        # Prevent self-deletion
        if target_user.id == request.user.id:
            messages.error(request, 'You cannot delete your own account.')
            return redirect('gdpr_management')

        # Prevent deleting superusers
        if target_user.is_superuser:
            messages.error(request, 'Cannot delete super admin accounts.')
            return redirect('gdpr_management')

        # Get deletion type from POST
        deletion_type = request.POST.get('deletion_type', 'soft')
        hard_delete = (deletion_type == 'hard')

        # Perform deletion
        result = GDPRDeletionService.delete_user(
            user_id=target_user.id,
            hard_delete=hard_delete,
            performed_by=request.user
        )

        if result['status'] == 'deleted':
            messages.success(request, f'User {result["username"]} has been permanently deleted.')
        else:
            messages.success(request, f'User {result["username"]} has been anonymized.')

    except Exception as e:
        messages.error(request, f'Error deleting user: {str(e)}')

    return HttpResponseRedirect(reverse('gdpr_management') + '?tab=users')


@login_required
@require_POST
def gdpr_delete_reviewee_view(request, reviewee_id):
    """Delete or anonymize a reviewee (GDPR)"""
    # Check permission
    if not request.user.has_perm('accounts.can_manage_organization'):
        messages.error(request, 'You do not have permission to delete reviewees.')
        return redirect('gdpr_management')

    org = request.organization
    if not org:
        messages.error(request, 'No organization found.')
        return redirect('admin_dashboard')

    try:
        # Get the reviewee to verify organization
        reviewee = get_object_or_404(Reviewee, id=reviewee_id, organization=org)

        # Get deletion type from POST
        deletion_type = request.POST.get('deletion_type', 'soft')

        if deletion_type == 'full_anonymization':
            # Full anonymization (reviewee + reviewer emails)
            result = GDPRDeletionService.delete_reviewee_and_anonymize_reviewer_emails(
                reviewee_id=reviewee.id,
                performed_by=request.user
            )
            messages.success(
                request,
                f'Reviewee {result["name"]} and all associated reviewer emails have been anonymized. '
                f'{result.get("reviewer_emails_anonymized", 0)} reviewer email(s) anonymized.'
            )
        else:
            # Soft or hard delete
            hard_delete = (deletion_type == 'hard')
            result = GDPRDeletionService.delete_reviewee(
                reviewee_id=reviewee.id,
                hard_delete=hard_delete,
                performed_by=request.user
            )

            if result['status'] == 'deleted':
                messages.success(
                    request,
                    f'Reviewee {result["name"]} has been permanently deleted along with '
                    f'{result["review_cycles_affected"]} review cycle(s) and all associated data.'
                )
            else:
                messages.success(
                    request,
                    f'Reviewee {result["name"]} has been anonymized. '
                    f'{result["review_cycles_affected"]} review cycle(s) preserved.'
                )

    except Exception as e:
        messages.error(request, f'Error deleting reviewee: {str(e)}')

    return redirect('gdpr_management')


# ============================================================================
# PRODUCT REVIEW MANAGEMENT
# ============================================================================

@login_required
def product_review_list(request):
    """List and manage product reviews"""
    from productreviews.models import ProductReview
    from django.db.models import Avg, Count

    org = request.organization

    # Get all product reviews (not org-scoped - these are reviews of Blik as a product)
    # Use .all() to explicitly avoid any organization filtering from the manager
    reviews_qs = ProductReview.objects.all().filter(is_active=True)

    # Filter by status if provided
    status_filter = request.GET.get('status', '')
    if status_filter:
        reviews_qs = reviews_qs.filter(status=status_filter)

    # Order by created date (newest first) to show pending reviews at top
    # Pending reviews don't have published_date, so ordering by created_at ensures they appear first
    reviews_qs = reviews_qs.order_by('-created_at')

    # Calculate aggregate stats
    stats = reviews_qs.aggregate(
        avg_rating=Avg('rating'),
        total_count=Count('id'),
        approved_count=Count('id', filter=Q(status='approved')),
        pending_count=Count('id', filter=Q(status='pending')),
    )

    # Get per_page from request, default to 25
    per_page = request.GET.get('per_page', '25')
    try:
        per_page = int(per_page)
        if per_page not in [25, 50, 100]:
            per_page = 25
    except (ValueError, TypeError):
        per_page = 25

    # Paginate reviews
    paginator = Paginator(reviews_qs, per_page)
    page = request.GET.get('page')
    try:
        reviews = paginator.page(page)
    except PageNotAnInteger:
        reviews = paginator.page(1)
    except EmptyPage:
        reviews = paginator.page(paginator.num_pages)

    context = {
        'reviews': reviews,
        'stats': stats,
        'status_filter': status_filter,
        'per_page': per_page,
    }

    return render(request, 'admin_dashboard/product_review_list.html', context)


@login_required
def product_review_create(request):
    """Create a new product review"""
    from productreviews.models import ProductReview
    from datetime import date

    if not request.user.is_superuser:
        messages.error(request, 'You do not have permission to create product reviews.')
        return redirect('product_review_list')

    if request.method == 'POST':
        rating = request.POST.get('rating')
        review_title = request.POST.get('review_title')
        review_text = request.POST.get('review_text')
        reviewer_name = request.POST.get('reviewer_name')
        reviewer_title = request.POST.get('reviewer_title', '')
        reviewer_company = request.POST.get('reviewer_company', '')
        reviewer_email = request.POST.get('reviewer_email')
        verified_customer = request.POST.get('verified_customer') == 'on'
        featured = request.POST.get('featured') == 'on'
        status = request.POST.get('status', 'pending')
        source = request.POST.get('source', '')
        notes = request.POST.get('notes', '')

        # Validation
        if not all([rating, review_title, review_text, reviewer_name, reviewer_email]):
            messages.error(request, 'Please fill in all required fields.')
            return render(request, 'admin_dashboard/product_review_form.html', {
                'action': 'Create',
                'review': request.POST,
            })

        try:
            rating = int(rating)
            if rating < 1 or rating > 5:
                raise ValueError('Rating must be between 1 and 5')
        except (ValueError, TypeError):
            messages.error(request, 'Invalid rating value.')
            return render(request, 'admin_dashboard/product_review_form.html', {
                'action': 'Create',
                'review': request.POST,
            })

        # Create the review
        review = ProductReview.objects.create(
            organization=request.organization,
            rating=rating,
            review_title=review_title,
            review_text=review_text,
            reviewer_name=reviewer_name,
            reviewer_title=reviewer_title,
            reviewer_company=reviewer_company,
            reviewer_email=reviewer_email,
            verified_customer=verified_customer,
            featured=featured,
            status=status,
            source=source,
            notes=notes,
            published_date=date.today() if status == 'approved' else None,
        )

        messages.success(request, f'Product review from "{reviewer_name}" created successfully.')
        return redirect('product_review_detail', review_id=review.id)

    return render(request, 'admin_dashboard/product_review_form.html', {'action': 'Create'})


@login_required
def product_review_detail(request, review_id):
    """View product review details"""
    from productreviews.models import ProductReview

    review = get_object_or_404(
        ProductReview.objects,
        id=review_id
    )

    context = {
        'review': review,
    }

    return render(request, 'admin_dashboard/product_review_detail.html', context)


@login_required
def product_review_edit(request, review_id):
    """Edit an existing product review"""
    from productreviews.models import ProductReview
    from datetime import date

    if not request.user.is_superuser:
        messages.error(request, 'You do not have permission to edit product reviews.')
        return redirect('product_review_list')

    review = get_object_or_404(
        ProductReview.objects.all(),
        id=review_id
    )

    if request.method == 'POST':
        rating = request.POST.get('rating')
        review_title = request.POST.get('review_title')
        review_text = request.POST.get('review_text')
        reviewer_name = request.POST.get('reviewer_name')
        reviewer_title = request.POST.get('reviewer_title', '')
        reviewer_company = request.POST.get('reviewer_company', '')
        reviewer_email = request.POST.get('reviewer_email')
        verified_customer = request.POST.get('verified_customer') == 'on'
        featured = request.POST.get('featured') == 'on'
        status = request.POST.get('status', 'pending')
        source = request.POST.get('source', '')
        notes = request.POST.get('notes', '')

        # Validation
        if not all([rating, review_title, review_text, reviewer_name, reviewer_email]):
            messages.error(request, 'Please fill in all required fields.')
            return render(request, 'admin_dashboard/product_review_form.html', {
                'action': 'Edit',
                'review': review,
            })

        try:
            rating = int(rating)
            if rating < 1 or rating > 5:
                raise ValueError('Rating must be between 1 and 5')
        except (ValueError, TypeError):
            messages.error(request, 'Invalid rating value.')
            return render(request, 'admin_dashboard/product_review_form.html', {
                'action': 'Edit',
                'review': review,
            })

        # Update the review
        old_status = review.status
        review.rating = rating
        review.review_title = review_title
        review.review_text = review_text
        review.reviewer_name = reviewer_name
        review.reviewer_title = reviewer_title
        review.reviewer_company = reviewer_company
        review.reviewer_email = reviewer_email
        review.verified_customer = verified_customer
        review.featured = featured
        review.status = status
        review.source = source
        review.notes = notes

        # Set published date when approved
        if status == 'approved' and old_status != 'approved':
            review.published_date = date.today()

        review.save()

        messages.success(request, f'Product review updated successfully.')
        return redirect('product_review_detail', review_id=review.id)

    return render(request, 'admin_dashboard/product_review_form.html', {
        'action': 'Edit',
        'review': review,
    })


@login_required
def product_review_delete(request, review_id):
    """Delete (soft delete) a product review"""
    from productreviews.models import ProductReview

    if not request.user.is_superuser:
        messages.error(request, 'You do not have permission to delete product reviews.')
        return redirect('product_review_list')

    review = get_object_or_404(
        ProductReview.objects.all(),
        id=review_id
    )

    if request.method == 'POST':
        # Soft delete
        review.is_active = False
        review.save()

        messages.success(request, f'Product review from "{review.reviewer_name}" has been deleted.')
        return redirect('product_review_list')

    return render(request, 'admin_dashboard/product_review_confirm_delete.html', {
        'review': review,
    })


@login_required
def quick_product_review(request):
    """
    Quick review submission for logged-in users.
    Pre-fills user information from their profile.
    """
    from productreviews.models import ProductReview
    from datetime import date

    user = request.user
    org = request.organization

    # Check if user has already submitted a review (global, not org-scoped)
    existing_review = ProductReview.objects.filter(
        reviewer_email=user.email,
        is_active=True
    ).first()

    if request.method == 'POST':
        rating = request.POST.get('rating')
        review_title = request.POST.get('review_title', '').strip()
        review_text = request.POST.get('review_text', '').strip()

        # Validation - only rating is required
        if not rating:
            messages.error(request, 'Please select a rating.')
            return render(request, 'admin_dashboard/quick_product_review.html', {
                'existing_review': existing_review,
            })

        try:
            rating = int(rating)
            if rating < 1 or rating > 5:
                raise ValueError('Rating must be between 1 and 5')
        except (ValueError, TypeError):
            messages.error(request, 'Invalid rating value.')
            return render(request, 'admin_dashboard/quick_product_review.html', {
                'existing_review': existing_review,
            })

        # Generate default title/text if not provided
        if not review_title:
            review_title = f"{rating}-star review"
        if not review_text:
            review_text = f"Rated {rating} out of 5 stars."

        # Get user profile info
        user_profile = user.userprofile if hasattr(user, 'userprofile') else None
        reviewer_name = user.get_full_name() or user.username
        reviewer_email = user.email

        # Create or update review
        if existing_review:
            # Update existing review
            existing_review.rating = rating
            existing_review.review_title = review_title
            existing_review.review_text = review_text
            existing_review.status = 'pending'  # Reset to pending for re-approval
            existing_review.save()
            messages.success(request, 'Your review has been updated and is pending approval. Thank you!')
        else:
            # Create new review
            ProductReview.objects.create(
                organization=org,
                rating=rating,
                review_title=review_title,
                review_text=review_text,
                reviewer_name=reviewer_name,
                reviewer_email=reviewer_email,
                verified_customer=True,  # They're logged-in users, so verified
                status='pending',
                source='Dashboard Quick Review',
            )
            messages.success(request, 'Thank you for your review! It will be published after approval.')

        return redirect('admin_dashboard')

    context = {
        'existing_review': existing_review,
        'user_name': user.get_full_name() or user.username,
        'user_email': user.email,
    }

    return render(request, 'admin_dashboard/quick_product_review.html', context)


@login_required
@require_POST
def product_review_approve(request, review_id):
    """Quick approve a product review"""
    from productreviews.models import ProductReview
    from datetime import date

    if not request.user.is_superuser:
        messages.error(request, 'You do not have permission to approve reviews.')
        return redirect('product_review_list')

    review = get_object_or_404(
        ProductReview.objects.all(),
        id=review_id
    )

    review.status = 'approved'
    if not review.published_date:
        review.published_date = date.today()
    review.save()

    messages.success(request, f'Review from "{review.reviewer_name}" approved successfully.')
    return redirect('product_review_list')


@login_required
@require_POST
def product_review_reject(request, review_id):
    """Quick reject a product review"""
    from productreviews.models import ProductReview

    if not request.user.is_superuser:
        messages.error(request, 'You do not have permission to reject reviews.')
        return redirect('product_review_list')

    review = get_object_or_404(
        ProductReview.objects.all(),
        id=review_id
    )

    review.status = 'rejected'
    review.save()

    messages.success(request, f'Review from "{review.reviewer_name}" rejected.')
    return redirect('product_review_list')


# =============================================================================
# API TOKEN & WEBHOOK MANAGEMENT
# =============================================================================

@login_required
def create_api_token(request):
    """Create a new API token"""
    if request.method != 'POST':
        return redirect('settings')

    if not request.user.has_perm('accounts.can_manage_organization'):
        messages.error(request, 'Permission denied.')
        return redirect('settings')

    org = request.organization
    if not org:
        messages.error(request, 'No organization found.')
        return redirect('settings')

    from api.models import APIToken

    name = request.POST.get('name')
    rate_limit = request.POST.get('rate_limit', 1000)
    is_active = request.POST.get('is_active') == 'on'

    try:
        token = APIToken.objects.create(
            organization=org,
            created_by=request.user,
            name=name,
            rate_limit=int(rate_limit),
            is_active=is_active
        )

        # Redirect to settings with token in session (will be displayed in modal)
        request.session['new_api_token'] = token.token
        request.session['new_api_token_name'] = name
    except Exception as e:
        messages.error(request, f'Error creating API token: {str(e)}')

    return redirect('settings')


@login_required
def update_api_token(request, token_id):
    """Update an existing API token"""
    if request.method != 'POST':
        return redirect('settings')

    if not request.user.has_perm('accounts.can_manage_organization'):
        messages.error(request, 'Permission denied.')
        return redirect('settings')

    org = request.organization
    if not org:
        messages.error(request, 'No organization found.')
        return redirect('settings')

    from api.models import APIToken

    try:
        token = APIToken.objects.for_organization(org).get(id=token_id)

        token.name = request.POST.get('name', token.name)
        token.rate_limit = int(request.POST.get('rate_limit', token.rate_limit))
        token.is_active = request.POST.get('is_active') == 'on'
        token.save()

        messages.success(request, 'API token updated successfully.')
    except APIToken.DoesNotExist:
        messages.error(request, 'API token not found.')
    except Exception as e:
        messages.error(request, f'Error updating API token: {str(e)}')

    return redirect('settings')


@login_required
def delete_api_token(request, token_id):
    """Delete (revoke) an API token"""
    if request.method != 'POST':
        return redirect('settings')

    if not request.user.has_perm('accounts.can_manage_organization'):
        messages.error(request, 'Permission denied.')
        return redirect('settings')

    org = request.organization
    if not org:
        messages.error(request, 'No organization found.')
        return redirect('settings')

    from api.models import APIToken

    try:
        token = APIToken.objects.for_organization(org).get(id=token_id)
        token_name = token.name
        token.delete()

        messages.success(request, f'API token "{token_name}" revoked successfully.')
    except APIToken.DoesNotExist:
        messages.error(request, 'API token not found.')
    except Exception as e:
        messages.error(request, f'Error revoking API token: {str(e)}')

    return redirect('settings')


@login_required
def create_webhook(request):
    """Create a new webhook endpoint"""
    if request.method != 'POST':
        return redirect('settings')

    if not request.user.has_perm('accounts.can_manage_organization'):
        messages.error(request, 'Permission denied.')
        return redirect('settings')

    org = request.organization
    if not org:
        messages.error(request, 'No organization found.')
        return redirect('settings')

    from api.models import WebhookEndpoint

    name = request.POST.get('name')
    url = request.POST.get('url')
    events = request.POST.getlist('events')  # Get multiple checkboxes
    is_active = request.POST.get('is_active') == 'on'

    try:
        webhook = WebhookEndpoint.objects.create(
            organization=org,
            created_by=request.user,
            name=name,
            url=url,
            events=events,
            is_active=is_active
        )

        messages.success(request, f'Webhook "{name}" created successfully.')
    except Exception as e:
        messages.error(request, f'Error creating webhook: {str(e)}')

    return redirect('settings')


@login_required
def update_webhook(request, webhook_id):
    """Update an existing webhook endpoint"""
    if request.method != 'POST':
        return redirect('settings')

    if not request.user.has_perm('accounts.can_manage_organization'):
        messages.error(request, 'Permission denied.')
        return redirect('settings')

    org = request.organization
    if not org:
        messages.error(request, 'No organization found.')
        return redirect('settings')

    from api.models import WebhookEndpoint

    try:
        webhook = WebhookEndpoint.objects.for_organization(org).get(id=webhook_id)

        webhook.name = request.POST.get('name', webhook.name)
        webhook.url = request.POST.get('url', webhook.url)
        webhook.events = request.POST.getlist('events')
        webhook.is_active = request.POST.get('is_active') == 'on'
        webhook.save()

        messages.success(request, f'Webhook "{webhook.name}" updated successfully.')
    except WebhookEndpoint.DoesNotExist:
        messages.error(request, 'Webhook not found.')
    except Exception as e:
        messages.error(request, f'Error updating webhook: {str(e)}')

    return redirect('settings')


@login_required
def delete_webhook(request, webhook_id):
    """Delete a webhook endpoint"""
    if request.method != 'POST':
        return redirect('settings')

    if not request.user.has_perm('accounts.can_manage_organization'):
        messages.error(request, 'Permission denied.')
        return redirect('settings')

    org = request.organization
    if not org:
        messages.error(request, 'No organization found.')
        return redirect('settings')

    from api.models import WebhookEndpoint

    try:
        webhook = WebhookEndpoint.objects.for_organization(org).get(id=webhook_id)
        webhook_name = webhook.name
        webhook.delete()

        messages.success(request, f'Webhook "{webhook_name}" deleted successfully.')
    except WebhookEndpoint.DoesNotExist:
        messages.error(request, 'Webhook not found.')
    except Exception as e:
        messages.error(request, f'Error deleting webhook: {str(e)}')

    return redirect('settings')

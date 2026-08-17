"""
Service functions for review cycles
"""
import random
from django.utils import timezone
from django.template.loader import render_to_string
from django.conf import settings
from django.urls import reverse
from core.email import send_email
from datetime import timedelta
from django.db.models import Q
from .models import ReviewCycle, ReviewerToken


def assign_tokens_to_emails(cycle, email_assignments):
    """
    Assign reviewer tokens to email addresses with smart randomization.

    Args:
        cycle: ReviewCycle instance
        email_assignments: dict like {
            'self': ['reviewee@example.com'],
            'peer': ['peer1@example.com', 'peer2@example.com'],
            'manager': ['manager@example.com'],
            'direct_report': []
        }

    Returns:
        dict: Statistics about assignments
    """
    stats = {
        'assigned': 0,
        'sent': 0,
        'errors': []
    }

    for category, emails in email_assignments.items():
        if not emails:
            continue

        # Get unassigned tokens for this category
        available_tokens = list(
            cycle.tokens.filter(
                category=category,
                reviewer_email__isnull=True
            )
        )

        if len(emails) > len(available_tokens):
            stats['errors'].append(
                f"Not enough tokens for {category}: need {len(emails)}, have {len(available_tokens)}"
            )
            continue

        # Randomly shuffle tokens to prevent any pattern linking
        random.shuffle(available_tokens)

        # Assign emails to tokens
        for email, token in zip(emails, available_tokens):
            token.reviewer_email = email.strip().lower()
            token.save()
            stats['assigned'] += 1

    return stats


def send_reviewer_invitations(cycle, token_ids=None):
    """
    Send email invitations to reviewers.

    Args:
        cycle: ReviewCycle instance
        token_ids: Optional list of specific token IDs to send (defaults to all with emails)

    Returns:
        dict: Statistics about emails sent
    """
    stats = {
        'sent': 0,
        'errors': []
    }

    # Get tokens to send invitations for (exclude 'self' category since
    # reviewees already receive a dedicated self-assessment email via
    # send_reviewee_notifications)
    tokens = cycle.tokens.filter(reviewer_email__isnull=False).exclude(category='self')

    if token_ids:
        tokens = tokens.filter(id__in=token_ids)
    else:
        # Only send to tokens that haven't been sent yet and aren't completed
        tokens = tokens.filter(invitation_sent_at__isnull=True, completed_at__isnull=True)

    for token in tokens:
        try:
            # Generate feedback URL
            feedback_url = f"{settings.SITE_PROTOCOL}://{settings.SITE_DOMAIN}/feedback/{token.token}/"

            # Render email templates
            context = {
                'reviewee_name': cycle.reviewee.name,
                'category': token.get_category_display(),
                'feedback_url': feedback_url,
                'questionnaire_name': cycle.questionnaire.name,
            }

            html_message = render_to_string('emails/reviewer_invitation.html', context)
            text_message = render_to_string('emails/reviewer_invitation.txt', context)

            # Send email
            send_email(
                subject=f'360 Feedback Request: {cycle.reviewee.name}',
                message=text_message,
                recipient_list=[token.reviewer_email],
                html_message=html_message,
            )

            # Mark as sent
            token.invitation_sent_at = timezone.now()
            token.save()

            stats['sent'] += 1

        except Exception as e:
            stats['errors'].append(f"Failed to send to {token.reviewer_email}: {str(e)}")

    return stats


def send_reminder_emails(cycle, token_ids=None):
    """
    Send reminder emails to reviewers who haven't completed feedback.

    Args:
        cycle: ReviewCycle instance
        token_ids: Optional list of specific token IDs to remind

    Returns:
        dict: Statistics about reminders sent
    """
    stats = {
        'sent': 0,
        'errors': []
    }

    # Get incomplete tokens with emails that have been invited
    tokens = cycle.tokens.filter(
        reviewer_email__isnull=False,
        invitation_sent_at__isnull=False,
        completed_at__isnull=True
    )

    if token_ids:
        tokens = tokens.filter(id__in=token_ids)

    for token in tokens:
        try:
            # Generate feedback URL
            feedback_url = f"{settings.SITE_PROTOCOL}://{settings.SITE_DOMAIN}/feedback/{token.token}/"

            # Render email templates
            context = {
                'reviewee_name': cycle.reviewee.name,
                'category': token.get_category_display(),
                'feedback_url': feedback_url,
                'questionnaire_name': cycle.questionnaire.name,
            }

            html_message = render_to_string('emails/reviewer_reminder.html', context)
            text_message = render_to_string('emails/reviewer_reminder.txt', context)

            # Send email
            send_email(
                subject=f'Reminder: 360 Feedback Request for {cycle.reviewee.name}',
                message=text_message,
                recipient_list=[token.reviewer_email],
                html_message=html_message,
            )

            # Update last reminder sent timestamp
            token.last_reminder_sent_at = timezone.now()
            token.save(update_fields=['last_reminder_sent_at'])

            stats['sent'] += 1

        except Exception as e:
            stats['errors'].append(f"Failed to send reminder to {token.reviewer_email}: {str(e)}")

    return stats


def send_reviewee_notifications(cycle, request=None):
    """
    Send the reviewee their direct self-assessment task.

    Reviewer assignment is managed in-app. The old second email containing
    reusable peer/manager/direct-report links is intentionally no longer sent.

    Args:
        cycle: ReviewCycle instance
        request: Optional request object for building absolute URLs

    Returns:
        dict: Statistics about emails sent
    """
    stats = {
        'sent': 0,
        'errors': []
    }

    if not cycle.reviewee.email:
        stats['errors'].append(f"No email address for reviewee {cycle.reviewee.name}")
        return stats

    # Always use SITE_DOMAIN for consistent URLs across all email contexts
    base_url = f"{settings.SITE_PROTOCOL}://{settings.SITE_DOMAIN}"

    # Send only the self-assessment email.
    try:
        self_token = cycle.tokens.filter(
            category='self',
            reviewer_email__iexact=cycle.reviewee.email,
            completed_at__isnull=True,
        ).first()
        if not self_token:
            self_token = ReviewerToken.objects.create(
                cycle=cycle,
                category='self',
                reviewer_email=cycle.reviewee.email,
            )
        self_assessment_url = f"{base_url}{reverse('reviews:feedback_form', kwargs={'token': self_token.token})}"

        context = {
            'reviewee': cycle.reviewee,
            'cycle': cycle,
            'self_assessment_url': self_assessment_url,
        }

        html_message = render_to_string('emails/reviewee_self_assessment.html', context)
        text_message = render_to_string('emails/reviewee_self_assessment.txt', context)

        send_email(
            subject=f'Complete Your Self-Assessment: {cycle.questionnaire.name}',
            message=text_message,
            recipient_list=[cycle.reviewee.email],
            html_message=html_message,
        )

        self_token.invitation_sent_at = timezone.now()
        self_token.save(update_fields=['invitation_sent_at'])

        stats['sent'] += 1

    except Exception as e:
        stats['errors'].append(f"Failed to send self-assessment email: {str(e)}")

    return stats


def send_campaign_invitations(campaign):
    """Send the appropriate launch email for every assessment in a campaign."""
    stats = {'sent': 0, 'errors': []}
    if campaign.cycle_type == 'self':
        for cycle in campaign.cycles.all():
            result = send_reviewee_notifications(cycle)
            stats['sent'] += result['sent']
            stats['errors'].extend(result['errors'])
        return stats
    if campaign.cycle_type == 'manager':
        for cycle in campaign.cycles.all():
            result = send_reviewer_invitations(cycle)
            stats['sent'] += result['sent']
            stats['errors'].extend(result['errors'])
        return stats

    for cycle in campaign.cycles.select_related('reviewee', 'questionnaire'):
        result = send_peer_nomination_invitation(cycle)
        stats['sent'] += result['sent']
        stats['errors'].extend(result['errors'])
    return stats


def send_organizational_cycle_invitations(organizational_cycle):
    """Send one consolidated dashboard email to each active participant."""
    from accounts.models import Reviewee

    stats = {'sent': 0, 'errors': []}
    dashboard_url = f"{settings.SITE_PROTOCOL}://{settings.SITE_DOMAIN}/dashboard/"
    participants = organizational_cycle.selected_reviewees.filter(is_active=True)
    if not participants.exists():
        # Compatibility for organisation cycles created before audience
        # selection was introduced.
        participants = Reviewee.objects.for_organization(
            organizational_cycle.organization
        ).filter(is_active=True)
    participants = participants.exclude(email='').order_by('email').distinct()
    for participant in participants:
        context = {
            'participant': participant,
            'organizational_cycle': organizational_cycle,
            'organization': organizational_cycle.organization,
            'dashboard_url': dashboard_url,
        }
        try:
            send_email(
                subject=(
                    f'It’s time for a review cycle for '
                    f'{organizational_cycle.organization.name}'
                ),
                message=render_to_string(
                    'emails/organizational_cycle_invitation.txt', context
                ),
                recipient_list=[participant.email],
                html_message=render_to_string(
                    'emails/organizational_cycle_invitation.html', context
                ),
            )
            stats['sent'] += 1
        except Exception as exc:
            stats['errors'].append(
                f'Failed to send organizational cycle invitation to '
                f'{participant.email}: {exc}'
            )
    return stats


def send_peer_nomination_invitation(cycle):
    """Invite one reviewee to nominate peers for their campaign cycle."""
    stats = {'sent': 0, 'errors': []}
    dashboard_url = f"{settings.SITE_PROTOCOL}://{settings.SITE_DOMAIN}/dashboard/"
    if not cycle.reviewee.email:
        return stats
    try:
        campaign = cycle.campaign
        context = {
            'reviewee': cycle.reviewee,
            'campaign': campaign,
            'questionnaire_name': cycle.questionnaire.name,
            'dashboard_url': dashboard_url,
        }
        send_email(
            subject=f'Select your peer reviewers: {cycle.questionnaire.name}',
            message=render_to_string('emails/peer_nomination_invitation.txt', context),
            recipient_list=[cycle.reviewee.email],
            html_message=render_to_string('emails/peer_nomination_invitation.html', context),
        )
        stats['sent'] += 1
    except Exception as exc:
        stats['errors'].append(
            f'Failed to send peer nomination invitation to {cycle.reviewee.email}: {exc}'
        )
    return stats


def send_close_check_emails(dry_run=False):
    """
    Send check-in emails to reviewees whose invite-link cycles have been
    open for at least 7 days and have at least one completed review.

    Args:
        dry_run: If True, find eligible cycles but don't send emails.

    Returns:
        dict: Statistics about emails sent
    """
    stats = {
        'sent': 0,
        'eligible': 0,
        'errors': [],
    }

    cutoff = timezone.now() - timedelta(days=7)

    cycles = ReviewCycle.objects.filter(
        status='active',
        close_check_sent_at__isnull=True,
        created_at__lte=cutoff,
    ).filter(
        tokens__completed_at__isnull=False,
    ).distinct().select_related('reviewee', 'questionnaire')

    stats['eligible'] = cycles.count()

    if dry_run:
        return stats

    base_url = f"{settings.SITE_PROTOCOL}://{settings.SITE_DOMAIN}"

    for cycle in cycles:
        try:
            if not cycle.reviewee.email:
                stats['errors'].append(
                    f"No email for reviewee {cycle.reviewee.name} (cycle {cycle.uuid})"
                )
                continue

            completed_count = cycle.tokens.filter(completed_at__isnull=False).count()
            total_count = cycle.tokens.count()
            dashboard_url = f"{base_url}/dashboard/cycles/{cycle.uuid}/"

            context = {
                'reviewee': cycle.reviewee,
                'cycle': cycle,
                'questionnaire_name': cycle.questionnaire.name,
                'completed_count': completed_count,
                'total_count': total_count,
                'dashboard_url': dashboard_url,
            }

            html_message = render_to_string('emails/cycle_close_check.html', context)
            text_message = render_to_string('emails/cycle_close_check.txt', context)

            send_email(
                subject=f'Review Check-In: {cycle.questionnaire.name}',
                message=text_message,
                recipient_list=[cycle.reviewee.email],
                html_message=html_message,
            )

            cycle.close_check_sent_at = timezone.now()
            cycle.save(update_fields=['close_check_sent_at'])

            stats['sent'] += 1

        except Exception as e:
            stats['errors'].append(
                f"Failed to send close check for cycle {cycle.uuid}: {str(e)}"
            )

    return stats

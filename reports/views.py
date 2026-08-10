from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.views.decorators.http import require_http_methods
from accounts.permissions import can_view_all_reports
from accounts.authorization import visible_cycles
from reviews.models import ReviewCycle
from .models import Report
from .services import generate_report, get_report_summary, apply_display_anonymization
import uuid


def get_cycle_or_404(request, cycle_uuid):
    """
    Get a cycle for the admin report views: must be in the user's organization,
    and the user must be an org admin. A cycle UUID alone is not authorization —
    without the org check any admin could read another organization's report.
    """
    cycle = get_object_or_404(
        ReviewCycle.objects.select_related('reviewee', 'questionnaire'),
        uuid=cycle_uuid
    )
    in_org = (not request.organization
              or cycle.reviewee.organization_id == request.organization.id)
    can_access = visible_cycles(
        request.user, ReviewCycle.objects.filter(pk=cycle.pk), request.organization
    ).exists()
    if not in_org or not can_access:
        raise Http404
    return cycle


def is_own_cycle(user, cycle):
    """Reviewees are matched by email — there is no FK from Reviewee to User."""
    return bool(user.email) and cycle.reviewee.email.lower() == user.email.lower()


@login_required
def view_report(request, cycle_uuid):
    """View aggregated feedback report for a review cycle (org admins only)"""
    # Reviewees reach their own report through the token URL, not the admin view.
    if not can_view_all_reports(request.user):
        cycle = get_object_or_404(
            ReviewCycle.objects.select_related('reviewee'), uuid=cycle_uuid
        )
        can_access = visible_cycles(
            request.user, ReviewCycle.objects.filter(pk=cycle.pk), request.organization
        ).exists()
        if not is_own_cycle(request.user, cycle) and not can_access:
            raise Http404
        report = Report.objects.filter(cycle=cycle).first() or generate_report(cycle)
        if is_own_cycle(request.user, cycle):
            return redirect('reports:reviewee_report', access_token=report.access_token)
        summary = get_report_summary(report)
        display_data = apply_display_anonymization(
            report.report_data,
            min_threshold=cycle.organization.min_responses_for_anonymity,
        )
        return render(request, 'reports/admin_view_report.html', {
            'cycle': cycle, 'report': report, 'display_data': display_data,
            'summary': summary, 'questionnaire': cycle.questionnaire,
            'is_admin_view': True,
        })

    cycle = get_cycle_or_404(request, cycle_uuid)

    # Get or generate report
    try:
        report = Report.objects.get(cycle=cycle)
    except Report.DoesNotExist:
        report = generate_report(cycle)

    summary = get_report_summary(report)

    # Apply display-level anonymization based on organization settings
    min_threshold = cycle.organization.min_responses_for_anonymity
    display_data = apply_display_anonymization(
        report.report_data,
        min_threshold=min_threshold
    )

    context = {
        'cycle': cycle,
        'report': report,
        'display_data': display_data,  # For detailed report sections
        'summary': summary,
        'questionnaire': cycle.questionnaire,
        'is_admin_view': True,
    }

    return render(request, 'reports/admin_view_report.html', context)


@login_required
def regenerate_report(request, cycle_uuid):
    """Regenerate report for a review cycle"""
    cycle = get_cycle_or_404(request, cycle_uuid)
    generate_report(cycle)

    return redirect('reports:view_report', cycle_uuid=cycle.uuid)


def reviewee_report(request, access_token):
    """Public-facing report view for reviewees - secured by UUID token"""
    from django.utils import timezone

    # Get report by access token
    try:
        report = Report.objects.select_related(
            'cycle__reviewee',
            'cycle__questionnaire'
        ).get(access_token=access_token)
    except Report.DoesNotExist:
        return render(request, 'reports/access_denied.html', status=403)

    # Check if access token has expired
    if report.access_token_expires and report.access_token_expires < timezone.now():
        return render(request, 'reports/access_denied.html', {
            'error': 'This report link has expired. Please contact your administrator for a new link.'
        }, status=403)

    # Log access for security auditing
    report.last_accessed = timezone.now()
    report.access_count += 1
    report.save(update_fields=['last_accessed', 'access_count'])

    cycle = report.cycle

    # Check if report is available (cycle should be completed)
    # Org admins can bypass this check
    can_bypass = (request.user.is_authenticated and
                  request.user.has_perm('accounts.can_manage_organization'))
    if cycle.status != 'completed' and not can_bypass:
        return render(request, 'reports/report_not_ready.html', {
            'cycle': cycle,
        })

    summary = get_report_summary(report)

    # Apply display-level anonymization based on organization settings
    min_threshold = cycle.organization.min_responses_for_anonymity
    display_data = apply_display_anonymization(
        report.report_data,
        min_threshold=min_threshold
    )

    context = {
        'cycle': cycle,
        'report': report,
        'display_data': display_data,  # For detailed report sections
        'summary': summary,
        'questionnaire': cycle.questionnaire,
        'is_public_view': True,
    }

    return render(request, 'reports/reviewee_report.html', context)

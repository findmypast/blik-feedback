"""
Core views for Blik application
"""
import hashlib

from django.contrib.auth.decorators import login_required
from django.db.models import Max, Q
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from accounts.authorization import (
    effective_scope,
    visible_cycles,
    visible_invitations,
    visible_profiles,
    visible_reports,
    visible_reviewees,
    visible_reviewer_tokens,
)


def health_check(request):
    """Health check endpoint for monitoring"""
    return JsonResponse({
        'status': 'healthy',
        'service': 'blik',
        'version': '0.1.0'
    })


def home(request):
    """Home page - redirect authenticated users to dashboard, others to login"""
    if request.user.is_authenticated:
        return redirect('admin_dashboard')
    return redirect('login')


def _revision_part(label, queryset):
    summary = queryset.aggregate(latest=Max('updated_at'))
    latest = summary['latest'].isoformat() if summary['latest'] else ''
    return f'{label}:{queryset.count()}:{latest}'


@login_required
@require_GET
@never_cache
def dashboard_revision(request):
    """Return an opaque revision for records visible to the signed-in user."""
    from accounts.models import OrganizationInvitation, Reviewee, Team, UserProfile
    from reports.models import Report
    from reviews.models import (
        OrganizationalReviewCycle,
        Response,
        ReviewCampaign,
        ReviewCycle,
        ReviewerToken,
    )

    organization = request.organization or getattr(
        getattr(request.user, 'profile', None), 'organization', None
    )
    if not organization:
        return JsonResponse({'revision': ''})

    scope = effective_scope(request.user, organization)
    reviewees = visible_reviewees(
        request.user,
        Reviewee.objects.for_organization(organization),
        organization,
    )
    profiles = visible_profiles(
        request.user,
        UserProfile.objects.for_organization(organization),
        organization,
    )
    invitations = visible_invitations(
        request.user,
        OrganizationInvitation.objects.for_organization(organization),
        organization,
    )
    cycles = visible_cycles(
        request.user,
        ReviewCycle.objects.for_organization(organization),
        organization,
    )
    tokens = visible_reviewer_tokens(
        request.user,
        ReviewerToken.objects.for_organization(organization),
        organization,
    )
    responses = Response.objects.for_organization(organization).filter(
        cycle_id__in=cycles.values('id')
    )
    reports = visible_reports(
        request.user,
        Report.objects.for_organization(organization),
        organization,
    )

    if scope.organization_wide:
        teams = Team.objects.for_organization(organization)
        campaigns = ReviewCampaign.objects.filter(organization=organization)
        organisation_cycles = OrganizationalReviewCycle.objects.filter(
            organization=organization
        )
    else:
        teams = Team.objects.for_organization(organization).filter(
            Q(reviewees__in=reviewees)
            | Q(members__in=reviewees)
            | Q(manager=getattr(request.user, 'profile', None))
        ).distinct()
        campaigns = ReviewCampaign.objects.filter(
            organization=organization,
            cycles__id__in=cycles.values('id'),
        ).distinct()
        organisation_cycles = OrganizationalReviewCycle.objects.filter(
            organization=organization,
            campaigns__id__in=campaigns.values('id'),
        ).distinct()

    parts = [
        _revision_part('reviewees', reviewees),
        _revision_part('profiles', profiles),
        _revision_part('invitations', invitations),
        _revision_part('teams', teams),
        _revision_part('cycles', cycles),
        _revision_part('campaigns', campaigns),
        _revision_part('organisation_cycles', organisation_cycles),
        _revision_part('tokens', tokens),
        _revision_part('responses', responses),
        _revision_part('reports', reports),
    ]
    revision = hashlib.sha256('|'.join(parts).encode()).hexdigest()
    return JsonResponse({'revision': revision})


def _render_error(request, status_code, title, message):
    """Keep the HTTP status while giving users a safe route back into Blik."""
    return render(request, 'errors/error.html', {
        'status_code': status_code,
        'error_title': title,
        'error_message': message,
    }, status=status_code)


def handler400(request, exception):
    return _render_error(
        request, 400, 'Bad request',
        'We could not process that request. Please return to the application and try again.',
    )


def handler403(request, exception):
    return _render_error(
        request, 403, 'Access denied',
        'You do not have permission to view or change this page.',
    )


def handler404(request, exception):
    return _render_error(
        request, 404, 'Page not found',
        'The page you requested does not exist or is no longer available.',
    )


def handler500(request):
    return _render_error(
        request, 500, 'Something went wrong',
        'An unexpected error occurred. Please return to the application and try again.',
    )

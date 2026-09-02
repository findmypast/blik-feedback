import json

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import JsonResponse
from django.template.loader import render_to_string
from django.urls import reverse
from django.views.decorators.http import require_POST

from accounts.people_import import (
    PeopleImportError,
    apply_people_import,
    read_people_file,
    validate_people_import,
)
from core.email import send_email


def _admin_organization(request):
    if not request.user.has_perm('accounts.can_manage_organization'):
        raise PermissionDenied
    organization = request.organization or getattr(
        getattr(request.user, 'profile', None), 'organization', None
    )
    if not organization:
        raise PermissionDenied
    return organization


def _payload(request):
    try:
        mapping = json.loads(request.POST.get('mapping', '{}'))
        role_mapping = json.loads(request.POST.get('role_mapping', '{}'))
    except json.JSONDecodeError as exc:
        raise PeopleImportError('The import mapping is invalid.') from exc
    return mapping, role_mapping, request.POST.get('mode', 'update')


def _public_preview(preview):
    return {key: value for key, value in preview.items() if not key.startswith('_')}


def _send_import_invitation(request, invitation):
    url = request.build_absolute_uri(
        reverse('accept_invitation', kwargs={'token': invitation.token})
    )
    organization = invitation.organization
    reviewee = organization.reviewees.filter(email__iexact=invitation.email).first()
    team_names = list(reviewee.teams.order_by('name').values_list('name', flat=True)) \
        if reviewee else []
    if not team_names and invitation.team:
        team_names = [invitation.team.name]
    role_name = (
        invitation.organization_role.name
        if invitation.organization_role_id
        else invitation.get_requested_role_display() or 'Member'
    )
    context = {
        'accept_url': url,
        'expires_at': invitation.expires_at,
        'first_name': invitation.first_name,
        'invitation': invitation,
        'organization': organization,
        'product_name': settings.PRODUCT_NAME,
        'role_name': role_name,
        'team_names': team_names,
    }
    send_email(
        subject=f'Welcome to {settings.PRODUCT_NAME}',
        message=render_to_string('emails/organization_invitation.txt', context),
        recipient_list=[invitation.email],
        html_message=render_to_string('emails/organization_invitation.html', context),
        from_email=organization.from_email or None,
    )


@login_required
@require_POST
def people_import_headers(request):
    _admin_organization(request)
    upload = request.FILES.get('file')
    if not upload:
        return JsonResponse({'error': 'Choose a CSV or XLSX file.'}, status=400)
    try:
        headers, rows = read_people_file(upload)
    except ValidationError as exc:
        return JsonResponse({'error': '; '.join(exc.messages)}, status=400)
    return JsonResponse({'headers': headers, 'row_count': len(rows)})


@login_required
@require_POST
def people_import_preview(request):
    organization = _admin_organization(request)
    upload = request.FILES.get('file')
    if not upload:
        return JsonResponse({'error': 'Choose a CSV or XLSX file.'}, status=400)
    try:
        mapping, role_mapping, mode = _payload(request)
        preview = validate_people_import(
            organization, upload, mapping, role_mapping, mode
        )
    except ValidationError as exc:
        return JsonResponse({'error': '; '.join(exc.messages)}, status=400)
    return JsonResponse(_public_preview(preview))


@login_required
@require_POST
def people_import_commit(request):
    organization = _admin_organization(request)
    upload = request.FILES.get('file')
    if not upload:
        return JsonResponse({'error': 'Choose a CSV or XLSX file.'}, status=400)
    try:
        mapping, role_mapping, mode = _payload(request)
        preview = validate_people_import(
            organization, upload, mapping, role_mapping, mode
        )
        if preview['errors']:
            return JsonResponse(_public_preview(preview), status=400)
        deactivate_missing = (
            mode == 'sync' and request.POST.get('deactivate_missing') == 'true'
        )
        counts, invitations = apply_people_import(
            organization, preview, request.user, deactivate_missing
        )
    except ValidationError as exc:
        return JsonResponse({'error': '; '.join(exc.messages)}, status=400)

    email_failures = []
    for invitation in invitations:
        try:
            _send_import_invitation(request, invitation)
        except Exception:  # noqa: BLE001 - each failed delivery must be reported separately
            email_failures.append(invitation.email)
    return JsonResponse({'ok': True, 'counts': counts, 'email_failures': email_failures})

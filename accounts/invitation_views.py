"""
Views for organization invitations
"""
from datetime import timedelta
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.urls import reverse
from django.db import transaction
from django.core.exceptions import ValidationError
from django.template.loader import render_to_string
from accounts.models import OrganizationInvitation, Reviewee, Team, UserProfile
from accounts.name_utils import normalize_name_part
from accounts.permissions import organization_admin_required
from core.email import send_email
from subscriptions.utils import check_user_limit


@login_required
@organization_admin_required
def send_invitation(request):
    """
    Send invitation to join organization.

    Only organization administrators can invite new team members.
    """
    if request.method == 'POST':
        redirect_name = 'settings' if request.POST.get('return_to') == 'settings' else 'team_list'
        email = request.POST.get('email', '').strip().lower()
        first_name = normalize_name_part(request.POST.get('first_name'))
        last_name = normalize_name_part(request.POST.get('last_name'))
        team_id = request.POST.get('team')
        no_team = request.POST.get('no_team') == 'on'
        create_team = not no_team and (
            request.POST.get('create_team') == 'on' or team_id == '__new__'
        )
        team_name = request.POST.get('team_name', '').strip()
        manager_choice = request.POST.get('reporting_manager_choice', 'none')
        manager_email = request.POST.get('reporting_manager_email', '').strip().casefold()

        # Get organization from request or user's profile
        org = request.organization
        if not org and hasattr(request.user, 'profile'):
            org = request.user.profile.organization

        if not org:
            messages.error(request, 'No organization found.')
            return redirect('admin_dashboard')

        if not email:
            messages.error(request, 'Email is required.')
            return redirect(redirect_name)

        # Check user limit
        allowed, error_message = check_user_limit(request)
        if not allowed:
            messages.error(request, error_message)
            return redirect(redirect_name)

        # Check if invitation already exists
        existing = OrganizationInvitation.objects.filter(
            organization=org,
            email=email,
            accepted_at__isnull=True
        ).first()

        if existing and existing.is_valid():
            messages.info(request, f'Invitation already sent to {email}')
            return redirect(redirect_name)

        try:
            with transaction.atomic():
                if no_team:
                    team = None
                elif create_team:
                    if not team_name:
                        raise ValidationError('Enter a name for the new team.')
                    team = Team(
                        organization=org,
                        name=team_name,
                        manager=request.user.profile,
                    )
                    team.full_clean()
                    team.save()
                elif team_id:
                    team = get_object_or_404(Team, pk=team_id, organization=org)
                else:
                    raise ValidationError('Select a team or create a new one.')

                reporting_manager = None
                pending_reporting_manager_email = ''
                if manager_choice == 'team_leader':
                    if not team or not team.manager_id:
                        raise ValidationError(
                            'The selected team does not have a team leader. Choose someone else.'
                        )
                    reporting_manager = team.manager
                elif manager_choice == 'other':
                    if not manager_email:
                        raise ValidationError('Enter the reporting manager’s email address.')
                    if manager_email == email:
                        raise ValidationError('A person cannot be their own reporting manager.')
                    try:
                        from django.core.validators import validate_email
                        validate_email(manager_email)
                    except ValidationError as exc:
                        raise ValidationError(
                            'Enter a valid reporting manager email address.'
                        ) from exc
                    reporting_manager = UserProfile.objects.for_organization(org).filter(
                        user__email__iexact=manager_email,
                        user__is_active=True,
                    ).first()
                    if not reporting_manager:
                        pending_reporting_manager_email = manager_email
                elif manager_choice != 'none':
                    raise ValidationError('Select a valid reporting manager option.')

                invitation = OrganizationInvitation.objects.create(
                    organization=org,
                    email=email,
                    first_name=first_name,
                    last_name=last_name,
                    team=team,
                    reporting_manager=reporting_manager,
                    pending_reporting_manager_email=pending_reporting_manager_email,
                    invited_by=request.user,
                    expires_at=timezone.now() + timedelta(days=7)
                )
        except ValidationError as exc:
            messages.error(request, '; '.join(exc.messages))
            return redirect(redirect_name)

        # Build invitation URL
        invite_url = request.build_absolute_uri(
            reverse('accept_invitation', kwargs={'token': invitation.token})
        )

        role_name = (
            invitation.organization_role.name
            if invitation.organization_role_id
            else invitation.get_requested_role_display() or 'Member'
        )
        reporting_manager_name = ''
        if invitation.reporting_manager_id:
            reporting_manager_name = (
                invitation.reporting_manager.user.get_full_name()
                or invitation.reporting_manager.user.email
            )
        elif invitation.pending_reporting_manager_email:
            reporting_manager_name = invitation.pending_reporting_manager_email
        email_context = {
            'accept_url': invite_url,
            'expires_at': invitation.expires_at,
            'first_name': first_name,
            'invitation': invitation,
            'organization': org,
            'product_name': settings.PRODUCT_NAME,
            'reporting_manager_name': reporting_manager_name,
            'role_name': role_name,
            'team_names': [team.name] if team else [],
        }

        # Send invitation email
        try:
            send_email(
                subject=f'Welcome to {settings.PRODUCT_NAME}',
                message=render_to_string(
                    'emails/organization_invitation.txt', email_context
                ),
                recipient_list=[email],
                html_message=render_to_string(
                    'emails/organization_invitation.html', email_context
                ),
                from_email=org.from_email if org.from_email else None
            )
            messages.success(request, f'Invitation sent to {email}')
        except Exception as e:
            messages.error(request, f'Failed to send invitation: {e}')

    if request.method == 'POST' and request.POST.get('return_to') == 'settings':
        return redirect(reverse('settings') + '#people')
    return redirect('team_list')


def accept_invitation(request, token):
    """Accept invitation - redirects to login if user exists, or shows signup form"""
    invitation = get_object_or_404(OrganizationInvitation, token=token)

    if invitation.team_id and invitation.team.archived_at:
        return render(request, 'accounts/team_removed.html', {
            'invitation': invitation,
            'team': invitation.team,
        }, status=410)

    if not invitation.is_valid():
        messages.error(request, 'This invitation has expired or been used.')
        return redirect('login')

    # Check if user already exists with this email
    from django.contrib.auth.models import User
    existing_user = User.objects.filter(email=invitation.email).first()

    if existing_user:
        # User exists - create profile and mark invitation accepted
        from accounts.models import UserProfile
        from accounts.permissions import assign_organization_member

        profile, created = UserProfile.objects.get_or_create(
            user=existing_user,
            defaults={
                'organization': invitation.organization,
                'can_create_cycles_for_others': invitation.organization.default_users_can_create_cycles
            }
        )

        if not created and profile.organization != invitation.organization:
            messages.error(
                request,
                f'Your account is already linked to {profile.organization.name}. '
                'Please contact support for multi-organization access.'
            )
            return redirect('login')

        # Assign organization member permissions if profile was just created
        if created:
            assign_organization_member(
                existing_user,
                can_create_cycles_for_others=invitation.organization.default_users_can_create_cycles
            )

        if not existing_user.is_active:
            existing_user.is_active = True
            existing_user.save(update_fields=['is_active'])

        if invitation.first_name or invitation.last_name:
            existing_user.first_name = invitation.first_name
            existing_user.last_name = invitation.last_name
            existing_user.save(update_fields=['first_name', 'last_name'])
        reviewee, _ = Reviewee.objects.get_or_create(
            organization=invitation.organization,
            email__iexact=invitation.email,
            defaults={
                'email': invitation.email,
                'name': (
                    f'{invitation.first_name} {invitation.last_name}'.strip()
                    or existing_user.get_full_name()
                    or invitation.email
                ),
            },
        )
        invited_name = f'{invitation.first_name} {invitation.last_name}'.strip()
        if invited_name:
            reviewee.name = invited_name
        reviewee.profile = profile
        reviewee.team = invitation.team
        reviewee.is_active = True
        reviewee.save(update_fields=['name', 'profile', 'team', 'is_active', 'updated_at'])
        if invitation.team:
            reviewee.teams.add(invitation.team)

        from accounts.invitation_provisioning import apply_invitation_access
        apply_invitation_access(invitation, profile)

        # Mark invitation accepted
        invitation.accepted_at = timezone.now()
        invitation.save()

        messages.success(
            request,
            f'Welcome to {invitation.organization.name}! Please log in.'
        )
        return redirect('login')

    # New user - show signup form
    # Store invitation token in session for signup
    request.session['invitation_token'] = token
    request.session['invitation_email'] = invitation.email

    messages.info(
        request,
        f'Welcome! Create your account to join {invitation.organization.name}'
    )
    return redirect('signup_from_invitation')

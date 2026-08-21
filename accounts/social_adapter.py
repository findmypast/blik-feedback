"""Microsoft SSO policy and safe provisioning for Blik accounts."""

from urllib.parse import urlencode

import jwt
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.socialaccount.providers.microsoft.views import MicrosoftGraphOAuth2Adapter
from allauth.socialaccount.providers.oauth2.client import OAuth2Error
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils import timezone

from accounts.models import OrganizationInvitation, Reviewee, UserProfile
from accounts.permissions import assign_organization_member

User = get_user_model()


def _access_denied_response():
    query = urlencode({'sso_error': 'access_denied'})
    return HttpResponseRedirect(f'{reverse("login")}?{query}')


class BlikMicrosoftOAuth2Adapter(MicrosoftGraphOAuth2Adapter):
    """Validate the Entra ID-token tenant before fetching the Graph profile."""

    def complete_login(self, request, app, token, **kwargs):
        response = kwargs.get('response') or {}
        id_token = response.get('id_token')
        if not id_token:
            raise OAuth2Error('Microsoft did not return an ID token.')
        try:
            # The code was exchanged at the configured tenant-specific token
            # endpoint. Reading tid here adds an explicit claim consistency
            # check; the OAuth exchange remains the authenticity boundary.
            claims = jwt.decode(
                id_token,
                options={
                    'verify_signature': False,
                    'verify_aud': False,
                    'verify_exp': False,
                },
                algorithms=['RS256'],
            )
        except jwt.PyJWTError as exc:
            raise OAuth2Error('Microsoft returned an invalid ID token.') from exc
        expected_tenant = settings.MICROSOFT_TENANT_ID.casefold()
        if not expected_tenant or str(claims.get('tid', '')).casefold() != expected_tenant:
            raise OAuth2Error('Microsoft tenant mismatch.')
        sociallogin = super().complete_login(request, app, token, **kwargs)
        sociallogin.account.extra_data['tid'] = claims['tid']
        return sociallogin


class BlikSocialAccountAdapter(DefaultSocialAccountAdapter):
    """Allow Microsoft login only for existing or explicitly invited users."""

    def is_open_for_signup(self, request, sociallogin):
        return False

    def on_authentication_error(
        self, request, provider, error=None, exception=None, extra_context=None
    ):
        raise ImmediateHttpResponse(_access_denied_response())

    def pre_social_login(self, request, sociallogin):
        if sociallogin.account.provider != 'microsoft':
            raise ImmediateHttpResponse(_access_denied_response())
        tenant = str(sociallogin.account.extra_data.get('tid', '')).casefold()
        if tenant != settings.MICROSOFT_TENANT_ID.casefold():
            raise ImmediateHttpResponse(_access_denied_response())

        email = self._verified_email(sociallogin)
        if not email:
            raise ImmediateHttpResponse(_access_denied_response())

        if sociallogin.is_existing:
            user = sociallogin.user
            if (
                not user.is_active
                or user.email.casefold() != email.casefold()
                or not hasattr(user, 'profile')
            ):
                raise ImmediateHttpResponse(_access_denied_response())
            return

        users = list(User.objects.filter(email__iexact=email)[:2])
        if len(users) > 1:
            raise ImmediateHttpResponse(_access_denied_response())
        invitation = OrganizationInvitation.objects.filter(
            email__iexact=email,
            accepted_at__isnull=True,
            expires_at__gt=timezone.now(),
        ).select_related('organization', 'team').order_by('-created_at').first()

        if users:
            user = users[0]
            if not user.is_active or not hasattr(user, 'profile'):
                if not invitation or not user.is_active:
                    raise ImmediateHttpResponse(_access_denied_response())
                self._accept_invitation(user, invitation, sociallogin)
            elif invitation and user.profile.organization_id != invitation.organization_id:
                raise ImmediateHttpResponse(_access_denied_response())
            sociallogin.connect(request, user)
            return

        if not invitation:
            raise ImmediateHttpResponse(_access_denied_response())
        with transaction.atomic():
            user = sociallogin.user
            user.username = email
            user.email = email
            user.set_unusable_password()
            user.save()
            self._accept_invitation(user, invitation, sociallogin)
            sociallogin.connect(request, user)

    @staticmethod
    def _verified_email(sociallogin):
        verified = {
            address.email.strip().lower()
            for address in sociallogin.email_addresses
            if address.verified and address.email
        }
        suggested = (sociallogin.user.email or '').strip().lower()
        return suggested if suggested in verified else None

    @staticmethod
    def _accept_invitation(user, invitation, sociallogin):
        with transaction.atomic():
            profile, created = UserProfile.objects.get_or_create(
                user=user,
                defaults={
                    'organization': invitation.organization,
                    'can_create_cycles_for_others': (
                        invitation.organization.default_users_can_create_cycles
                    ),
                },
            )
            if profile.organization_id != invitation.organization_id:
                raise ImmediateHttpResponse(_access_denied_response())
            if created:
                assign_organization_member(
                    user,
                    can_create_cycles_for_others=(
                        invitation.organization.default_users_can_create_cycles
                    ),
                )
            first_name = user.first_name or sociallogin.user.first_name
            last_name = user.last_name or sociallogin.user.last_name
            if invitation.first_name:
                first_name = invitation.first_name
            if invitation.last_name:
                last_name = invitation.last_name
            if first_name != user.first_name or last_name != user.last_name:
                user.first_name = first_name
                user.last_name = last_name
                user.save(update_fields=['first_name', 'last_name'])
            name = f'{first_name} {last_name}'.strip() or user.email
            reviewee, _ = Reviewee.objects.get_or_create(
                organization=invitation.organization,
                email__iexact=user.email,
                defaults={'email': user.email, 'name': name},
            )
            reviewee.profile = profile
            reviewee.name = name
            reviewee.is_active = True
            if invitation.team_id:
                reviewee.team = invitation.team
            reviewee.save()
            if invitation.team_id:
                reviewee.teams.add(invitation.team)
            invitation.accepted_at = timezone.now()
            invitation.save(update_fields=['accepted_at', 'updated_at'])

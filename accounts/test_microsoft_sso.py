from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import jwt
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.providers.oauth2.client import OAuth2Error
from django.contrib.auth.models import Group
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.factories import OrganizationInvitationFactory, UserProfileFactory
from accounts.models import Reviewee, Team, UserProfile
from accounts.permissions import ORG_ADMIN_GROUP, ORG_MEMBER_GROUP
from accounts.social_adapter import (
    BlikMicrosoftOAuth2Adapter,
    BlikSocialAccountAdapter,
)
from core.factories import OrganizationFactory, UserFactory

TENANT_ID = '11111111-2222-3333-4444-555555555555'
SSO_SETTINGS = {
    'MICROSOFT_SSO_ENABLED': True,
    'MICROSOFT_SSO_CONFIGURED': True,
    'MICROSOFT_TENANT_ID': TENANT_ID,
}


def social_login(email, *, tenant=TENANT_ID, verified=True, first_name='Jamie'):
    user = UserFactory.build(
        username=email, email=email, first_name=first_name, last_name='Member'
    )
    return SimpleNamespace(
        account=SimpleNamespace(provider='microsoft', extra_data={'tid': tenant}),
        user=user,
        email_addresses=[SimpleNamespace(email=email, verified=verified)],
        connect=Mock(),
        is_existing=False,
    )


@override_settings(**SSO_SETTINGS)
class MicrosoftSocialAdapterTests(TestCase):
    def setUp(self):
        self.adapter = BlikSocialAccountAdapter()
        self.request = RequestFactory().get('/')
        self.organization = OrganizationFactory()

    def assert_denied(self, login):
        with self.assertRaises(ImmediateHttpResponse) as caught:
            self.adapter.pre_social_login(self.request, login)
        self.assertEqual(caught.exception.response.status_code, 302)
        self.assertIn('sso_error=access_denied', caught.exception.response.url)

    def test_existing_active_user_is_linked_without_changing_access(self):
        user = UserFactory(email='member@example.com')
        profile = UserProfileFactory(user=user, organization=self.organization)
        admin_group = Group.objects.create(name=ORG_ADMIN_GROUP)
        user.groups.add(admin_group)
        login = social_login(user.email)

        self.adapter.pre_social_login(self.request, login)

        login.connect.assert_called_once_with(self.request, user)
        profile.refresh_from_db()
        self.assertEqual(profile.organization, self.organization)
        self.assertTrue(user.groups.filter(name=ORG_ADMIN_GROUP).exists())

    def test_repeat_login_uses_existing_social_connection_without_reconnecting(self):
        user = UserFactory(email='linked@example.com')
        UserProfileFactory(user=user, organization=self.organization)
        login = social_login(user.email)
        login.user = user
        login.is_existing = True

        self.adapter.pre_social_login(self.request, login)

        login.connect.assert_not_called()

    def test_valid_invitation_creates_member_profile_and_team_membership(self):
        manager = UserProfileFactory(organization=self.organization)
        team = Team.objects.create(
            organization=self.organization, name='Platform', manager=manager
        )
        invitation = OrganizationInvitationFactory(
            organization=self.organization,
            email='invited@example.com',
            first_name='Invited',
            last_name='Person',
            team=team,
        )
        login = social_login(invitation.email)

        self.adapter.pre_social_login(self.request, login)

        user = login.user
        self.assertFalse(user.has_usable_password())
        profile = UserProfile.objects.get(user=user)
        self.assertEqual(profile.organization, self.organization)
        reviewee = Reviewee.objects.get(profile=profile)
        self.assertEqual(reviewee.team, team)
        self.assertTrue(reviewee.teams.filter(pk=team.pk).exists())
        self.assertTrue(user.groups.filter(name=ORG_MEMBER_GROUP).exists())
        self.assertFalse(user.groups.filter(name=ORG_ADMIN_GROUP).exists())
        invitation.refresh_from_db()
        self.assertIsNotNone(invitation.accepted_at)
        login.connect.assert_called_once_with(self.request, user)

    def test_unknown_user_is_rejected(self):
        self.assert_denied(social_login('unknown@example.com'))

    def test_inactive_existing_user_is_rejected(self):
        user = UserFactory(email='inactive@example.com', is_active=False)
        UserProfileFactory(user=user, organization=self.organization)
        self.assert_denied(social_login(user.email))

    def test_wrong_tenant_is_rejected(self):
        user = UserFactory(email='member@example.com')
        UserProfileFactory(user=user, organization=self.organization)
        self.assert_denied(social_login(user.email, tenant='another-tenant'))

    def test_unverified_or_missing_email_is_rejected(self):
        self.assert_denied(social_login('member@example.com', verified=False))

    def test_duplicate_email_is_rejected(self):
        UserFactory(username='first', email='duplicate@example.com')
        UserFactory(username='second', email='duplicate@example.com')
        self.assert_denied(social_login('duplicate@example.com'))

    def test_invitation_cannot_move_existing_user_between_organizations(self):
        user = UserFactory(email='member@example.com')
        UserProfileFactory(user=user, organization=self.organization)
        OrganizationInvitationFactory(
            organization=OrganizationFactory(), email=user.email
        )
        self.assert_denied(social_login(user.email))

    def test_expired_invitation_is_rejected(self):
        OrganizationInvitationFactory(
            organization=self.organization,
            email='expired@example.com',
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        self.assert_denied(social_login('expired@example.com'))


class MicrosoftOAuthAdapterTests(TestCase):
    @override_settings(MICROSOFT_TENANT_ID=TENANT_ID)
    @patch(
        'accounts.social_adapter.MicrosoftGraphOAuth2Adapter.complete_login'
    )
    def test_id_token_tenant_is_copied_to_social_profile(self, complete_login):
        login = social_login('member@example.com')
        login.account.extra_data = {}
        complete_login.return_value = login
        token = jwt.encode({'tid': TENANT_ID}, key='', algorithm='none')
        request = RequestFactory().get('/')

        result = BlikMicrosoftOAuth2Adapter(request).complete_login(
            request, Mock(), Mock(), response={'id_token': token}
        )

        self.assertEqual(result.account.extra_data['tid'], TENANT_ID)

    @override_settings(MICROSOFT_TENANT_ID=TENANT_ID)
    def test_wrong_id_token_tenant_is_rejected(self):
        token = jwt.encode({'tid': 'wrong'}, key='', algorithm='none')
        with self.assertRaises(OAuth2Error):
            BlikMicrosoftOAuth2Adapter(RequestFactory().get('/')).complete_login(
                RequestFactory().get('/'), Mock(), Mock(), response={'id_token': token}
            )


class MicrosoftLoginViewTests(TestCase):
    def setUp(self):
        UserProfileFactory(organization=OrganizationFactory())

    @override_settings(MICROSOFT_SSO_CONFIGURED=True)
    def test_login_shows_microsoft_button_when_configured(self):
        response = self.client.get(reverse('login'))
        self.assertContains(response, 'Continue with Microsoft SSO')
        self.assertContains(response, reverse('microsoft_login'))

    @override_settings(MICROSOFT_SSO_CONFIGURED=False)
    def test_login_hides_microsoft_button_when_not_configured(self):
        response = self.client.get(reverse('login'))
        self.assertNotContains(response, 'Continue with Microsoft SSO')

    @override_settings(MICROSOFT_SSO_CONFIGURED=False)
    def test_password_login_still_works(self):
        user = UserFactory(email='password@example.com', password='valid-password')
        UserProfileFactory(user=user)
        response = self.client.post(
            reverse('login'),
            {'login': user.email, 'password': 'valid-password'},
        )
        self.assertRedirects(response, reverse('admin_dashboard'))

from django.test import TestCase, override_settings
from django.test import RequestFactory
from django.urls import reverse

from accounts.factories import UserProfileFactory
from blik.views import handler500


@override_settings(DEBUG=False)
class ErrorHandlerTests(TestCase):
    def setUp(self):
        self.profile = UserProfileFactory()

    def test_authenticated_user_is_redirected_from_404_to_dashboard(self):
        self.client.force_login(self.profile.user)

        response = self.client.get('/missing-page/')

        self.assertRedirects(
            response,
            reverse('admin_dashboard'),
            fetch_redirect_response=False,
        )

    def test_anonymous_user_is_redirected_from_404_to_login(self):
        response = self.client.get('/missing-page/')

        self.assertRedirects(
            response,
            f'{reverse("login")}?next=/missing-page/',
            fetch_redirect_response=False,
        )

    def test_500_page_only_offers_login_and_retry_actions(self):
        response = handler500(RequestFactory().get('/broken/'))
        content = response.content.decode()

        self.assertEqual(response.status_code, 500)
        self.assertIn(f'href="{reverse("login")}"', content)
        self.assertIn('Back to Login', content)
        self.assertIn('Try Again', content)
        self.assertNotIn('Back to Home', content)
        self.assertNotIn('>Home</a>', content)
        self.assertNotIn('GitHub', content)

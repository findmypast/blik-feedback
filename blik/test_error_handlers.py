from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.factories import UserProfileFactory


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

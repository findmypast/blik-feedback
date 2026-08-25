from django.test import TestCase, override_settings
from django.test import RequestFactory
from django.urls import reverse

from accounts.factories import UserProfileFactory
from blik.views import handler400, handler403, handler500


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

    def test_removed_landing_route_redirects_to_login(self):
        response = self.client.get('/landing/')

        self.assertRedirects(
            response,
            f'{reverse("login")}?next=/landing/',
            fetch_redirect_response=False,
        )

    def test_anonymous_400_403_and_500_redirect_to_login(self):
        request = RequestFactory().get('/broken/')
        request.user = type('Anonymous', (), {'is_authenticated': False})()

        for handler, args in ((handler400, (None,)), (handler403, (None,)), (handler500, ())):
            response = handler(request, *args)
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.url, f'{reverse("login")}?next=/broken/')

    def test_authenticated_500_redirects_to_dashboard(self):
        request = RequestFactory().get('/broken/')
        request.user = self.profile.user

        response = handler500(request)

        self.assertEqual(response.url, reverse('admin_dashboard'))

from django.test import TestCase, override_settings
from django.test import RequestFactory
from django.urls import reverse

from accounts.factories import UserProfileFactory
from blik.views import handler400, handler403, handler500


@override_settings(DEBUG=False)
class ErrorHandlerTests(TestCase):
    def setUp(self):
        self.profile = UserProfileFactory()

    def test_authenticated_404_keeps_status_and_links_to_dashboard(self):
        self.client.force_login(self.profile.user)

        response = self.client.get('/missing-page/')

        self.assertEqual(response.status_code, 404)
        self.assertContains(
            response, reverse('admin_dashboard'), status_code=404
        )
        self.assertContains(response, 'Go back to Dashboard', status_code=404)

    def test_anonymous_404_keeps_status_and_links_to_login(self):
        response = self.client.get('/missing-page/')

        self.assertEqual(response.status_code, 404)
        self.assertContains(response, reverse('login'), status_code=404)
        self.assertContains(response, 'Go to Login', status_code=404)

    def test_removed_landing_route_returns_recoverable_404(self):
        response = self.client.get('/landing/')

        self.assertEqual(response.status_code, 404)
        self.assertContains(response, 'Go to Login', status_code=404)

    def test_anonymous_400_403_and_500_keep_status_and_link_to_login(self):
        request = RequestFactory().get('/broken/')
        request.user = type('Anonymous', (), {'is_authenticated': False})()

        for handler, args, status in (
            (handler400, (None,), 400),
            (handler403, (None,), 403),
            (handler500, (), 500),
        ):
            response = handler(request, *args)
            self.assertEqual(response.status_code, status)
            self.assertIn(reverse('login'), response.content.decode())

    def test_authenticated_500_keeps_status_and_links_to_dashboard(self):
        request = RequestFactory().get('/broken/')
        request.user = self.profile.user

        response = handler500(request)

        self.assertEqual(response.status_code, 500)
        self.assertIn(reverse('admin_dashboard'), response.content.decode())

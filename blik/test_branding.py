from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase, override_settings

from blik.context_processors import branding


class BrandingTestCase(SimpleTestCase):
    @override_settings(PRODUCT_NAME='History Community 360 Feedback')
    def test_branding_context_uses_deployment_settings(self):
        context = branding(RequestFactory().get('/'))

        self.assertEqual(context['product_name'], 'History Community 360 Feedback')

    @override_settings(PRODUCT_NAME='History Community 360 Feedback')
    def test_public_pages_render_product_name(self):
        request = RequestFactory().get('/accounts/login/')

        rendered = render_to_string(
            'accounts/login.html',
            {'microsoft_sso_enabled': False, 'registration_enabled': False},
            request=request,
        )

        self.assertIn('<title>Login - History Community 360 Feedback</title>', rendered)
        self.assertIn('function refreshCsrfTokens(root)', rendered)

    @override_settings(PRODUCT_NAME='History Community 360 Feedback')
    def test_setup_admin_page_renders(self):
        request = RequestFactory().get('/setup/admin/')

        rendered = render_to_string('setup/admin.html', request=request)

        self.assertIn('Create Admin Account - History Community 360 Feedback', rendered)
        self.assertIn('Create your administrator account', rendered)

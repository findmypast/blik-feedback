from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase, override_settings

from blik.context_processors import branding


class BrandingTestCase(SimpleTestCase):
    @override_settings(PRODUCT_NAME='Findmypast 360')
    def test_branding_context_uses_deployment_settings(self):
        context = branding(RequestFactory().get('/'))

        self.assertEqual(context['product_name'], 'Findmypast 360')

    @override_settings(PRODUCT_NAME='Findmypast 360')
    def test_public_pages_render_product_name(self):
        request = RequestFactory().get('/accounts/login/')

        rendered = render_to_string(
            'accounts/login.html',
            {'microsoft_sso_enabled': False, 'registration_enabled': False},
            request=request,
        )

        self.assertIn('<title>Login - Findmypast 360</title>', rendered)

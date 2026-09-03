"""Tests for `core.email`.

The organization's SMTP settings take priority, but when they are absent the
fallback must be the backend from Django settings — not a hardcoded SMTP
connection. Hardcoding SMTP means the console backend never prints and the
locmem backend never fills mail.outbox, so email fails in dev and in tests
regardless of EMAIL_BACKEND.
"""
from django.core import mail
from django.core.mail.backends.locmem import EmailBackend as LocmemBackend
from django.core.mail.backends.smtp import EmailBackend as SMTPBackend
from django.test import TestCase, override_settings

from core.email import add_email_footer, brand_email_subject, get_email_backend, send_email
from core.factories import OrganizationFactory


class GetEmailBackendTests(TestCase):
    def test_falls_back_to_configured_backend_when_org_has_no_smtp_host(self):
        OrganizationFactory(smtp_host='')

        # Tests run under the locmem backend; a hardcoded SMTP backend here
        # would try to open a socket to EMAIL_HOST.
        self.assertIsInstance(get_email_backend(), LocmemBackend)

    def test_organization_smtp_settings_take_priority(self):
        OrganizationFactory(smtp_host='smtp.acme.example.com', smtp_port=2525)

        backend = get_email_backend()

        self.assertIsInstance(backend, SMTPBackend)
        self.assertEqual(backend.host, 'smtp.acme.example.com')
        self.assertEqual(backend.port, 2525)

    @override_settings(DEFAULT_FROM_EMAIL='fallback@example.com')
    def test_send_email_delivers_through_configured_backend(self):
        OrganizationFactory(smtp_host='', from_email='org@example.com')

        sent = send_email(
            subject='Hello',
            message='Body',
            recipient_list=['someone@example.com'],
        )

        self.assertEqual(sent, 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['someone@example.com'])
        self.assertEqual(mail.outbox[0].from_email, 'org@example.com')

    @override_settings(PRODUCT_NAME='Findmypast 360')
    def test_email_subject_and_footer_are_consistently_branded(self):
        OrganizationFactory(smtp_host='')

        send_email(
            subject='360 Feedback Request for Alex Example',
            message='Please provide feedback.',
            recipient_list=['someone@example.com'],
            html_message='<html><body><p>Please provide feedback.</p></body></html>',
        )

        email = mail.outbox[0]
        self.assertEqual(
            email.subject,
            'Findmypast 360 Feedback Request for Alex Example',
        )
        notice = (
            'This is an automated message from Findmypast 360 Feedback system.'
        )
        self.assertIn(notice, email.body)
        self.assertIn(notice, email.alternatives[0].content)
        self.assertIn('cid:findmypast-logo', email.alternatives[0].content)
        self.assertIn('360 Feedback', email.alternatives[0].content)
        self.assertEqual(len(email.attachments), 1)
        self.assertEqual(email.attachments[0].get_content_type(), 'image/png')
        self.assertEqual(
            email.attachments[0]['Content-ID'], '<findmypast-logo>'
        )

    @override_settings(PRODUCT_NAME='Findmypast 360')
    def test_branding_helpers_do_not_duplicate_existing_branding(self):
        self.assertEqual(
            brand_email_subject('Findmypast 360: Welcome'),
            'Findmypast 360: Welcome',
        )
        branded = add_email_footer('Message')
        self.assertEqual(add_email_footer(branded), branded)

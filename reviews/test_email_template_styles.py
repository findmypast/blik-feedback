from io import StringIO
from html.parser import HTMLParser

from django.core import mail
from django.core.management import call_command
from django.template.loader import render_to_string
from django.test import SimpleTestCase, override_settings


class _ButtonLinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.button_styles = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return

        attributes = dict(attrs)
        style = attributes.get("style", "").lower()
        classes = attributes.get("class", "").split()
        if "button" in classes or "cta-button" in classes or "background-color:" in style:
            self.button_styles.append(style)


class EmailButtonStyleTests(SimpleTestCase):
    templates = (
        "emails/assessment_report.html",
        "emails/cycle_close_check.html",
        "emails/password_reset.html",
        "emails/report_ready.html",
        "emails/reviewee_self_assessment.html",
        "emails/reviewer_invitation.html",
        "emails/reviewer_reminder.html",
        "emails/welcome.html",
        "emails/welcome_member.html",
        "notifications/feedback_invitation.html",
    )

    def test_cta_buttons_keep_contrast_when_email_clients_override_colours(self):
        for template_name in self.templates:
            with self.subTest(template=template_name):
                parser = _ButtonLinkParser()
                parser.feed(render_to_string(template_name, {}))

                self.assertTrue(parser.button_styles, "Expected at least one CTA button")
                for style in parser.button_styles:
                    self.assertIn("background-color:", style)
                    self.assertIn("!important", style)
                    self.assertIn("color: #ffffff !important", style)
                    self.assertIn("-webkit-text-fill-color: #ffffff", style)

    def test_invitation_tables_are_fluid_on_mobile(self):
        invitation_templates = (
            "emails/reviewer_invitation.html",
            "emails/reviewer_reminder.html",
            "notifications/feedback_invitation.html",
        )

        for template_name in invitation_templates:
            with self.subTest(template=template_name):
                html = render_to_string(template_name, {})

                self.assertNotIn('width="600"', html)
                self.assertIn("max-width: 600px", html)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class SendTestEmailCommandTests(SimpleTestCase):
    def test_sends_database_free_html_invitation(self):
        output = StringIO()

        call_command("send_test_email", "preview@example.com", stdout=output)

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["preview@example.com"])
        self.assertEqual(mail.outbox[0].alternatives[0].mimetype, "text/html")
        self.assertIn("Sent test invitation", output.getvalue())

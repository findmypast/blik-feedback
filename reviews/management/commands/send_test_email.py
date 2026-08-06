from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.core.management.base import BaseCommand
from django.template.loader import render_to_string


class Command(BaseCommand):
    help = "Send a database-free sample invitation email for visual testing"

    def add_arguments(self, parser):
        parser.add_argument(
            "email",
            nargs="?",
            default="test@example.com",
            help="Recipient address (default: test@example.com)",
        )

    def handle(self, *args, **options):
        recipient = options["email"]
        context = {
            "category": "Peer",
            "reviewee_name": "Alex Example",
            "questionnaire_name": "360 Degree Feedback",
            "feedback_url": "https://example.test/feedback/sample-token/",
        }

        email = EmailMultiAlternatives(
            subject="[Test] 360 Feedback Request",
            body=(
                "You have been invited to provide Peer feedback for Alex Example.\n\n"
                f"Complete the sample review: {context['feedback_url']}"
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[recipient],
        )
        email.attach_alternative(
            render_to_string("emails/reviewer_invitation.html", context),
            "text/html",
        )
        sent = email.send(fail_silently=False)

        if sent != 1:
            raise RuntimeError("The email backend did not accept the test email")

        self.stdout.write(self.style.SUCCESS(f"Sent test invitation to {recipient}"))

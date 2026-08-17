from django.test import TestCase
from django.urls import reverse
from unittest.mock import patch

from questionnaires.factories import RatingQuestionFactory
from reviews.factories import ReviewerTokenFactory
from accounts.factories import UserProfileFactory
from accounts.models import Reviewee
from core.factories import OrganizationFactory, UserFactory


class FeedbackCompletionRedirectTests(TestCase):
    def setUp(self):
        self.organization = OrganizationFactory()
        self.user = UserFactory(email='reviewer@example.com')
        UserProfileFactory(user=self.user, organization=self.organization)
        self.client.force_login(self.user)

    @patch('api.webhooks.send_webhook')
    def test_submission_redirects_to_dashboard(self, send_webhook):
        question = RatingQuestionFactory(
            section__questionnaire__organization=self.organization
        )
        token = ReviewerTokenFactory(
            cycle__reviewee__organization=self.organization,
            cycle__questionnaire=question.section.questionnaire,
            reviewer_email=self.user.email,
        )

        response = self.client.post(
            reverse('reviews:submit_feedback', args=[token.token]),
            {f'question_{question.id}': '4'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['redirect'], reverse('admin_dashboard'))

    def test_completed_feedback_url_redirects_to_dashboard(self):
        from django.utils import timezone
        token = ReviewerTokenFactory(
            cycle__reviewee__organization=self.organization,
            reviewer_email=self.user.email,
            completed_at=timezone.now(),
        )

        response = self.client.get(
            reverse('reviews:feedback_form', args=[token.token])
        )

        self.assertRedirects(response, reverse('admin_dashboard'))

    def test_legacy_self_email_claims_existing_assigned_token(self):
        token = ReviewerTokenFactory(
            cycle__reviewee=Reviewee.objects.get(
                organization=self.organization, email=self.user.email
            ),
            category='self',
            reviewer_email=self.user.email,
        )

        response = self.client.get(
            reverse(
                'reviews:claim_token',
                args=[token.cycle.invitation_token_self],
            ),
            {'force_claim': '1'},
        )

        self.assertRedirects(
            response, reverse('reviews:feedback_form', args=[token.token])
        )
        self.assertEqual(token.cycle.tokens.count(), 1)

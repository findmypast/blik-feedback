"""Tests for the email-dispatch services in reviews.services.

We patch `reviews.services.send_email` rather than relying on Django's
locmem backend because `core.email.send_email` builds an SMTP backend from
the Organization row and bypasses the test mail outbox.
"""
from unittest.mock import patch

from django.test import RequestFactory, TestCase, override_settings

from accounts.factories import RevieweeFactory
from core.factories import OrganizationFactory, UserFactory
from questionnaires.factories import QuestionnaireFactory
from reviews.factories import ReviewCycleFactory, ReviewerTokenFactory
from reviews.models import ReviewCampaign
from reviews.services import (
    send_peer_nomination_invitation,
    send_reviewee_notifications,
    send_reviewer_invitations,
)


class SendReviewerInvitationsTests(TestCase):
    def setUp(self):
        self.org = OrganizationFactory()
        self.reviewee = RevieweeFactory(
            organization=self.org, email='reviewee@example.com',
        )
        self.cycle = ReviewCycleFactory(
            reviewee=self.reviewee,
            questionnaire=QuestionnaireFactory(organization=self.org),
            created_by=UserFactory(),
        )

    @patch('reviews.services.send_email')
    def test_does_not_send_to_self_category_tokens(self, mock_send_email):
        """Self-category tokens already receive a dedicated self-assessment
        email from send_reviewee_notifications. Sending the generic
        reviewer invitation in addition delivers two emails to the same
        person with different subjects and bodies.
        """
        ReviewerTokenFactory(
            cycle=self.cycle, category='self',
            reviewer_email=self.reviewee.email,
        )
        ReviewerTokenFactory(
            cycle=self.cycle, category='manager',
            reviewer_email='manager@example.com',
        )

        stats = send_reviewer_invitations(self.cycle)

        recipients = {
            address
            for call in mock_send_email.call_args_list
            for address in call.kwargs['recipient_list']
        }
        self.assertEqual(recipients, {'manager@example.com'})
        self.assertEqual(stats['sent'], 1)


@override_settings(SITE_DOMAIN='public.example.com', SITE_PROTOCOL='https')
class SendRevieweeNotificationsTests(TestCase):
    def setUp(self):
        self.org = OrganizationFactory()
        self.reviewee = RevieweeFactory(
            organization=self.org, email='reviewee@example.com',
        )
        self.cycle = ReviewCycleFactory(
            reviewee=self.reviewee,
            questionnaire=QuestionnaireFactory(organization=self.org),
            created_by=UserFactory(),
        )

    @patch('reviews.services.send_email')
    def test_uses_site_domain_not_request_host_for_links(self, mock_send_email):
        """Links must always point at SITE_DOMAIN. A request passed in from
        a view may carry a proxy hostname (HTTP_HOST set to the internal
        upstream) — using it would produce links that recipients can't
        reach.
        """
        request = RequestFactory().get('/', HTTP_HOST='proxy.internal:8000')

        send_reviewee_notifications(self.cycle, request=request)

        rendered = '\n'.join(
            (call.kwargs.get('html_message') or '')
            + '\n'
            + call.kwargs['message']
            for call in mock_send_email.call_args_list
        )
        self.assertIn('https://public.example.com/', rendered)
        self.assertNotIn('proxy.internal', rendered)


@override_settings(SITE_DOMAIN='public.example.com', SITE_PROTOCOL='https')
class SendPeerNominationInvitationTests(TestCase):
    @patch('reviews.services.send_email')
    def test_includes_campaign_minimum_peer_reviewers(self, mock_send_email):
        org = OrganizationFactory()
        creator = UserFactory()
        questionnaire = QuestionnaireFactory(organization=org)
        campaign = ReviewCampaign.objects.create(
            organization=org,
            created_by=creator,
            questionnaire=questionnaire,
            target_type='individual',
            cycle_type='peer',
            minimum_peer_reviewers=4,
        )
        cycle = ReviewCycleFactory(
            reviewee=RevieweeFactory(
                organization=org, email='reviewee@example.com',
            ),
            questionnaire=questionnaire,
            created_by=creator,
            campaign=campaign,
        )

        stats = send_peer_nomination_invitation(cycle)

        self.assertEqual(stats['sent'], 1)
        rendered = (
            mock_send_email.call_args.kwargs['message']
            + mock_send_email.call_args.kwargs['html_message']
        )
        self.assertIn('at least 4 colleagues', rendered)

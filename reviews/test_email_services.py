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
from reviews.models import OrganizationalReviewCycle, ReviewCampaign
from reviews.services import (
    send_organizational_cycle_invitations,
    send_peer_nomination_invitation,
    send_reviewee_notifications,
    send_reviewer_invitations,
)


@override_settings(SITE_DOMAIN='public.example.com', SITE_PROTOCOL='https')
class SendOrganizationalCycleInvitationTests(TestCase):
    @patch('reviews.services.send_email')
    def test_sends_one_consolidated_dashboard_email_per_participant(self, mock_send_email):
        org = OrganizationFactory(name='FindMyPast')
        participant = RevieweeFactory(
            organization=org, name='Jamie Member', email='jamie@example.com'
        )
        questionnaire = QuestionnaireFactory(organization=org)
        parent = OrganizationalReviewCycle.objects.create(
            organization=org,
            created_by=UserFactory(),
            self_questionnaire=questionnaire,
            peer_questionnaire=questionnaire,
            manager_questionnaire=questionnaire,
            minimum_peer_reviewers=3,
        )

        stats = send_organizational_cycle_invitations(parent)

        self.assertEqual(stats['sent'], 1)
        self.assertEqual(mock_send_email.call_count, 1)
        call = mock_send_email.call_args.kwargs
        self.assertEqual(call['recipient_list'], [participant.email])
        self.assertIn('FindMyPast', call['subject'])
        rendered = call['message'] + call['html_message']
        self.assertIn('Self-assessment', rendered)
        self.assertIn('Peer review', rendered)
        self.assertIn('Manager assessment', rendered)
        self.assertIn('Go to Dashboard', rendered)
        self.assertIn('https://public.example.com/dashboard/', rendered)


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

        self.assertEqual(mock_send_email.call_count, 1)
        self.assertNotIn(
            'Share Your 360 Feedback Links',
            mock_send_email.call_args.kwargs['subject'],
        )

        rendered = '\n'.join(
            (call.kwargs.get('html_message') or '')
            + '\n'
            + call.kwargs['message']
            for call in mock_send_email.call_args_list
        )
        self.assertIn('https://public.example.com/', rendered)
        self.assertNotIn('proxy.internal', rendered)
        self_token = self.cycle.tokens.get(category='self')
        self.assertIn(
            f'https://public.example.com/feedback/{self_token.token}/', rendered
        )
        self.assertNotIn('/feedback/invite/', rendered)
        self.assertIsNotNone(self_token.invitation_sent_at)


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

from django.core.exceptions import ValidationError
from datetime import date

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from unittest.mock import patch

from accounts.factories import RevieweeFactory, UserProfileFactory
from accounts.models import Team
from core.factories import OrganizationFactory, UserFactory
from questionnaires.factories import QuestionnaireFactory
from reviews.campaign_services import launch_campaign
from reviews.models import ReviewCampaign


class CampaignServiceTests(TestCase):
    def setUp(self):
        self.org = OrganizationFactory()
        self.manager_user = UserFactory(email='manager@example.com')
        self.manager = UserProfileFactory(user=self.manager_user, organization=self.org)
        self.team = Team.objects.create(
            organization=self.org, name='Platform', manager=self.manager
        )
        self.manager_reviewee = self.manager.reviewee
        self.member_one = RevieweeFactory(organization=self.org, team=self.team)
        self.member_two = RevieweeFactory(organization=self.org, team=self.team)

    def campaign(self, cycle_type, questionnaire, **kwargs):
        return ReviewCampaign.objects.create(
            organization=self.org,
            created_by=self.manager_user,
            questionnaire=questionnaire,
            target_type='team',
            team=self.team,
            cycle_type=cycle_type,
            **kwargs,
        )

    def test_self_campaign_creates_one_self_assessment_per_member(self):
        questionnaire = QuestionnaireFactory(
            organization=self.org, allow_self_assessment=True
        )
        campaign = launch_campaign(self.campaign('self', questionnaire))

        self.assertEqual(campaign.cycles.count(), 3)
        self.assertEqual(campaign.cycles.filter(tokens__category='self').count(), 3)

    def test_peer_campaign_waits_for_each_member_to_nominate_reviewers(self):
        questionnaire = QuestionnaireFactory(
            organization=self.org, allow_peer_review=True
        )
        campaign = launch_campaign(self.campaign('peer', questionnaire))

        self.assertEqual(campaign.cycles.count(), 3)
        self.assertFalse(campaign.cycles.filter(tokens__isnull=False).exists())

    def test_manager_campaign_has_one_manager_cycle_and_team_reviewers(self):
        questionnaire = QuestionnaireFactory(
            organization=self.org,
            allow_peer_review=False,
            allow_self_assessment=False,
            allow_manager_assessment=True,
        )
        campaign = launch_campaign(self.campaign('manager', questionnaire))

        cycle = campaign.cycles.get()
        self.assertEqual(cycle.reviewee, self.manager_reviewee)
        emails = set(cycle.tokens.values_list('reviewer_email', flat=True))
        self.assertEqual(emails, {self.member_one.email, self.member_two.email})
        self.assertNotIn(self.manager_user.email, emails)

    def test_incompatible_questionnaire_is_rejected(self):
        questionnaire = QuestionnaireFactory(
            organization=self.org, allow_manager_assessment=False
        )

        with self.assertRaises(ValidationError):
            launch_campaign(self.campaign('manager', questionnaire))

    def test_include_descendants_adds_nested_team_members(self):
        child = Team.objects.create(
            organization=self.org, name='API', parent=self.team, manager=self.manager
        )
        nested = RevieweeFactory(organization=self.org, team=child)
        questionnaire = QuestionnaireFactory(
            organization=self.org, allow_self_assessment=True
        )
        campaign = launch_campaign(self.campaign(
            'self', questionnaire, include_descendants=True
        ))

        self.assertTrue(campaign.cycles.filter(reviewee=nested).exists())


class CampaignCreationViewTests(TestCase):
    def setUp(self):
        self.org = OrganizationFactory()
        self.manager_user = UserFactory(email='lead@example.com')
        self.manager = UserProfileFactory(
            user=self.manager_user, organization=self.org
        )
        self.team = Team.objects.create(
            organization=self.org, name='Design', manager=self.manager
        )
        self.member = RevieweeFactory(organization=self.org, team=self.team)
        self.questionnaire = QuestionnaireFactory(
            organization=self.org,
            allow_peer_review=False,
            allow_self_assessment=False,
            allow_manager_assessment=True,
        )
        self.client.force_login(self.manager_user)

    def test_create_screen_has_team_individual_and_review_type_controls(self):
        response = self.client.get(reverse('review_cycle_create'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Who is this for?')
        self.assertContains(response, self.team.name)
        self.assertContains(response, 'Include nested teams')
        self.assertContains(response, 'Manager assessment')
        self.assertContains(response, 'Minimum peer reviewers')

    @patch('reviews.services.send_campaign_invitations')
    def test_manager_can_launch_team_manager_assessment(self, send):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse('review_cycle_create'), {
                'campaign_flow': '1',
                'target_type': 'team',
                'team': self.team.id,
                'cycle_type': 'manager',
                'questionnaire': self.questionnaire.id,
                'due_date': '2026-09-01',
            })

        self.assertRedirects(response, reverse('admin_dashboard'))
        campaign = ReviewCampaign.objects.get()
        self.assertEqual(campaign.status, 'active')
        self.assertEqual(campaign.cycles.count(), 1)
        self.assertNotIn(
            self.manager_user.email,
            campaign.cycles.get().tokens.values_list('reviewer_email', flat=True),
        )
        send.assert_called_once_with(campaign)

    def test_manager_cannot_target_an_unmanaged_team_identifier(self):
        other_team = Team.objects.create(
            organization=self.org, name='Finance'
        )

        response = self.client.post(reverse('review_cycle_create'), {
            'campaign_flow': '1',
            'target_type': 'team',
            'team': other_team.id,
            'cycle_type': 'manager',
            'questionnaire': self.questionnaire.id,
            'due_date': '2026-09-01',
        })

        self.assertEqual(response.status_code, 404)
        self.assertFalse(ReviewCampaign.objects.exists())

    @patch('blik.admin_views.send_reviewer_invitations')
    def test_manager_can_add_a_later_team_member_to_manager_campaign(self, send):
        campaign = launch_campaign(ReviewCampaign.objects.create(
            organization=self.org,
            created_by=self.manager_user,
            questionnaire=self.questionnaire,
            target_type='team',
            team=self.team,
            cycle_type='manager',
        ))
        new_member = RevieweeFactory(
            organization=self.org, team=self.team, email='new@example.com'
        )

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse('add_campaign_participant', args=[campaign.uuid]),
                {'reviewee': new_member.id},
            )

        self.assertRedirects(response, reverse('admin_dashboard'))
        token = campaign.cycles.get().tokens.get(reviewer_email=new_member.email)
        send.assert_called_once_with(campaign.cycles.get(), [token.id])

    @patch('blik.admin_views.send_reviewee_notifications')
    def test_manager_can_add_a_later_team_member_to_self_campaign(self, send):
        questionnaire = QuestionnaireFactory(
            organization=self.org, allow_self_assessment=True
        )
        campaign = launch_campaign(ReviewCampaign.objects.create(
            organization=self.org,
            created_by=self.manager_user,
            questionnaire=questionnaire,
            target_type='team',
            team=self.team,
            cycle_type='self',
        ))
        new_member = RevieweeFactory(
            organization=self.org, team=self.team, email='later@example.com'
        )

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse('add_campaign_participant', args=[campaign.uuid]),
                {'reviewee': new_member.id},
            )

        self.assertRedirects(response, reverse('admin_dashboard'))
        cycle = campaign.cycles.get(reviewee=new_member)
        self.assertTrue(cycle.tokens.filter(
            category='self', reviewer_email=new_member.email
        ).exists())
        send.assert_called_once_with(cycle)


class PeerNominationFlowTests(TestCase):
    def setUp(self):
        self.org = OrganizationFactory()
        self.manager_user = UserFactory(email='manager2@example.com')
        self.manager = UserProfileFactory(
            user=self.manager_user, organization=self.org
        )
        self.team = Team.objects.create(
            organization=self.org, name='Product', manager=self.manager
        )
        self.member_user = UserFactory(email='member@example.com')
        self.member_profile = UserProfileFactory(
            user=self.member_user, organization=self.org
        )
        from accounts.models import Reviewee
        self.member = Reviewee.objects.get(
            organization=self.org, email=self.member_user.email
        )
        self.member.profile = self.member_profile
        self.member.team = self.team
        self.member.save(update_fields=['profile', 'team'])
        self.peer = RevieweeFactory(
            organization=self.org, team=self.team, email='peer@example.com'
        )
        self.questionnaire = QuestionnaireFactory(
            organization=self.org, allow_peer_review=True
        )
        self.campaign = launch_campaign(ReviewCampaign.objects.create(
            organization=self.org,
            created_by=self.manager_user,
            questionnaire=self.questionnaire,
            target_type='team',
            team=self.team,
            cycle_type='peer',
        ))
        self.cycle = self.campaign.cycles.get(reviewee=self.member)

    @patch('blik.admin_views.send_reviewer_invitations')
    def test_submission_immediately_schedules_only_this_members_invites(self, send):
        self.client.force_login(self.member_user)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse('nominate_peer_reviewers', args=[self.cycle.uuid]),
                {'reviewers': [self.peer.id]},
            )

        self.assertRedirects(response, reverse('admin_dashboard'))
        token = self.cycle.tokens.get()
        self.assertEqual(token.reviewer_email, self.peer.email)
        send.assert_called_once_with(self.cycle, [token.id])
        other_cycles = self.campaign.cycles.exclude(id=self.cycle.id)
        self.assertFalse(other_cycles.filter(tokens__isnull=False).exists())

    @patch('blik.admin_views.send_reviewer_invitations')
    def test_nomination_requires_campaign_minimum(self, send):
        self.campaign.minimum_peer_reviewers = 2
        self.campaign.save(update_fields=['minimum_peer_reviewers'])
        self.client.force_login(self.member_user)

        response = self.client.post(
            reverse('nominate_peer_reviewers', args=[self.cycle.uuid]),
            {'reviewers': [self.peer.id]},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Select at least 2 peer reviewer(s)')
        self.assertFalse(self.cycle.tokens.exists())
        send.assert_not_called()

    def test_another_member_cannot_submit_nominations_for_this_cycle(self):
        outsider = UserFactory(email='outsider@example.com')
        UserProfileFactory(user=outsider, organization=self.org)
        self.client.force_login(outsider)

        response = self.client.get(
            reverse('nominate_peer_reviewers', args=[self.cycle.uuid])
        )

        self.assertEqual(response.status_code, 404)

    def test_campaign_manager_can_view_nominations(self):
        self.cycle.tokens.create(category='peer', reviewer_email=self.peer.email)
        self.client.force_login(self.manager_user)

        response = self.client.get(
            reverse('review_campaign_detail', args=[self.campaign.uuid])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.member.name)
        self.assertContains(response, self.peer.email)
        self.assertNotContains(response, 'answer_data')

    @patch('blik.admin_views.send_reviewer_invitations')
    def test_manager_can_edit_peer_reviewers_organization_wide(self, send):
        removable = self.cycle.tokens.create(
            category='peer', reviewer_email=self.peer.email,
            invitation_sent_at=timezone.now(),
        )
        protected = self.cycle.tokens.create(
            category='peer', reviewer_email='started@example.com',
            claimed_at=timezone.now(),
        )
        other_team = Team.objects.create(
            organization=self.org, name='Finance'
        )
        organization_peer = RevieweeFactory(
            organization=self.org, team=other_team, email='finance@example.com'
        )
        self.client.force_login(self.manager_user)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse('nominate_peer_reviewers', args=[self.cycle.uuid]),
                {'reviewers': [organization_peer.id]},
            )

        self.assertRedirects(response, reverse(
            'review_campaign_detail', args=[self.campaign.uuid]
        ))
        self.assertFalse(self.cycle.tokens.filter(id=removable.id).exists())
        self.assertTrue(self.cycle.tokens.filter(id=protected.id).exists())
        added = self.cycle.tokens.get(reviewer_email=organization_peer.email)
        send.assert_called_once_with(self.cycle, [added.id])

    def test_member_dashboard_shows_compact_campaign_without_reviewer_details(self):
        self.cycle.tokens.create(
            category='peer', reviewer_email=self.peer.email,
            invitation_sent_at=timezone.now(),
        )
        self.client.force_login(self.member_user)

        response = self.client.get(reverse('admin_dashboard'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Peer Review Cycle')
        self.assertContains(
            response, reverse('nominate_peer_reviewers', args=[self.cycle.uuid])
        )
        self.assertContains(response, 'Add or remove peers')
        self.assertNotContains(response, self.peer.email)
        self.assertNotContains(response, 'Resend reminder')
        self.assertNotContains(response, self.manager.reviewee.name)

    def test_manager_dashboard_expands_campaign_with_resend_controls(self):
        self.cycle.tokens.create(
            category='peer', reviewer_email=self.peer.email,
            invitation_sent_at=timezone.now(),
        )
        self.client.force_login(self.manager_user)

        response = self.client.get(reverse('admin_dashboard'))

        self.assertContains(response, self.member.name)
        self.assertContains(response, self.peer.email)
        self.assertContains(response, 'Resend')

    @patch('reviews.services.send_reminder_emails')
    def test_manager_can_resend_to_one_peer_from_dashboard(self, send):
        token = self.cycle.tokens.create(
            category='peer', reviewer_email=self.peer.email,
            invitation_sent_at=timezone.now(),
        )
        send.return_value = {'sent': 1, 'errors': []}
        self.client.force_login(self.manager_user)

        response = self.client.post(reverse('send_campaign_reviewer_reminder', args=[
            self.campaign.uuid, self.cycle.uuid, token.id,
        ]))

        self.assertRedirects(response, reverse('admin_dashboard'))
        send.assert_called_once_with(self.cycle, [token.id])

    def test_member_dashboard_links_their_outstanding_review_task(self):
        token = self.cycle.tokens.create(
            category='self', reviewer_email=self.member_user.email,
            invitation_sent_at=timezone.now(),
        )
        other_token = self.cycle.tokens.create(
            category='peer', reviewer_email='someone-else@example.com',
            invitation_sent_at=timezone.now(),
        )
        self.client.force_login(self.member_user)

        response = self.client.get(reverse('admin_dashboard'))

        self.assertContains(response, 'To do')
        self.assertContains(response, reverse('reviews:feedback_form', args=[token.token]))
        self.assertContains(response, 'Start')
        self.assertNotContains(
            response, reverse('reviews:feedback_form', args=[other_token.token])
        )

    @patch('reviews.services.send_reminder_emails')
    def test_member_can_resend_their_incomplete_reviewer_reminders(self, send):
        self.cycle.tokens.create(
            category='peer', reviewer_email=self.peer.email,
            invitation_sent_at=timezone.now(),
        )
        send.return_value = {'sent': 1, 'errors': []}
        self.client.force_login(self.member_user)

        response = self.client.post(reverse('send_campaign_cycle_reminder', args=[
            self.campaign.uuid, self.cycle.uuid,
        ]))

        self.assertRedirects(response, reverse('admin_dashboard'))
        send.assert_called_once_with(self.cycle)

    @patch('reviews.services.send_campaign_invitations')
    def test_manager_can_renew_a_completed_campaign(self, send):
        self.campaign.cycles.update(status='completed')
        self.campaign.start_date = date(2026, 7, 1)
        self.campaign.due_date = date(2026, 7, 22)
        self.campaign.save(update_fields=['start_date', 'due_date'])
        self.client.force_login(self.manager_user)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse(
                'renew_review_campaign', args=[self.campaign.uuid]
            ))

        self.assertRedirects(response, reverse('admin_dashboard'))
        renewed = ReviewCampaign.objects.get(renewed_from=self.campaign)
        self.assertEqual(renewed.status, 'active')
        self.assertEqual(renewed.due_date - renewed.start_date, self.campaign.due_date - self.campaign.start_date)
        send.assert_called_once_with(renewed)

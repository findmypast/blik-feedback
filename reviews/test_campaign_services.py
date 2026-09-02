from datetime import date, timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.conf import settings
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.authorization import visible_cycles
from accounts.factories import RevieweeFactory, UserProfileFactory
from accounts.models import Reviewee, Team, TeamMembership
from core.factories import OrganizationFactory, UserFactory
from questionnaires.factories import QuestionnaireFactory
from reports.models import Report
from reviews.campaign_services import launch_campaign, launch_organizational_cycle
from reviews.services import _send_campaign_completion_notifications
from reviews.models import (
    OrganizationalReviewCycle,
    ReviewCampaign,
    ReviewCycle,
    ReviewerToken,
)


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

    @patch('reviews.services.send_email')
    def test_team_completion_notifies_leader_and_administrator_once(self, send_email):
        from accounts.permissions import assign_organization_admin

        assign_organization_admin(self.manager_user)
        self_questionnaire = QuestionnaireFactory(
            organization=self.org, allow_self_assessment=True
        )
        peer_questionnaire = QuestionnaireFactory(
            organization=self.org, allow_peer_review=True
        )
        parent = OrganizationalReviewCycle.objects.create(
            organization=self.org,
            created_by=self.manager_user,
            self_questionnaire=self_questionnaire,
            peer_questionnaire=peer_questionnaire,
        )
        parent.teams.add(self.team)
        self_campaign = ReviewCampaign.objects.create(
            organization=self.org,
            created_by=self.manager_user,
            questionnaire=self_questionnaire,
            target_type='organization',
            cycle_type='self',
            status='completed',
            organizational_cycle=parent,
        )
        self_cycle = ReviewCycle.objects.create(
            reviewee=self.member_one,
            questionnaire=self_questionnaire,
            campaign=self_campaign,
            status='completed',
        )
        ReviewerToken.objects.create(
            cycle=self_cycle,
            category='self',
            reviewer_email=self.member_one.email,
            completed_at=timezone.now(),
        )
        peer_campaign = ReviewCampaign.objects.create(
            organization=self.org,
            created_by=self.manager_user,
            questionnaire=peer_questionnaire,
            target_type='team',
            team=self.team,
            cycle_type='peer',
            status='completed',
            organizational_cycle=parent,
        )

        _send_campaign_completion_notifications(peer_campaign.pk)
        _send_campaign_completion_notifications(peer_campaign.pk)

        self.assertEqual(send_email.call_count, 2)
        self.assertEqual(
            [call.kwargs['subject'] for call in send_email.call_args_list],
            [
                'Platform has completed peer reviews',
                'Platform has completed its review cycle',
            ],
        )
        admin_email = send_email.call_args_list[1].kwargs
        expected_cycle_url = (
            f'{settings.SITE_PROTOCOL}://{settings.SITE_DOMAIN}'
            f'{reverse("organisation_cycle_detail", args=[parent.uuid])}'
        )
        self.assertIn('Team review cycle complete', admin_email['html_message'])
        self.assertIn('Review reports', admin_email['html_message'])
        self.assertIn(expected_cycle_url, admin_email['html_message'])
        self.assertIn(
            self.manager_user.get_full_name() or self.manager_user.email,
            admin_email['message'],
        )

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

    def test_organizational_cycle_launches_all_three_assessment_types(self):
        unassigned = RevieweeFactory(organization=self.org, team=None)
        questionnaires = {
            'self': QuestionnaireFactory(
                organization=self.org, allow_self_assessment=True
            ),
            'peer': QuestionnaireFactory(
                organization=self.org, allow_peer_review=True
            ),
            'manager': QuestionnaireFactory(
                organization=self.org, allow_manager_assessment=True
            ),
        }

        parent = launch_organizational_cycle(
            organization=self.org,
            created_by=self.manager_user,
            questionnaires=questionnaires,
            minimum_peer_reviewers=4,
            due_date=date(2026, 9, 1),
        )

        self.assertEqual(parent.campaigns.count(), 4)
        self.assertEqual(
            set(parent.campaigns.values_list('cycle_type', flat=True)),
            {'self', 'peer', 'manager'},
        )
        peer_campaigns = parent.campaigns.filter(cycle_type='peer')
        self.assertTrue(all(
            peer.minimum_peer_reviewers == 4 for peer in peer_campaigns
        ))
        self.assertEqual(
            peer_campaigns.filter(cycles__reviewee=unassigned).count(),
            1,
        )
        self_campaign = parent.campaigns.get(cycle_type='self')
        self.assertTrue(self_campaign.cycles.filter(reviewee=unassigned).exists())
        manager = parent.campaigns.get(cycle_type='manager')
        self.assertEqual(manager.cycles.get().reviewee, self.manager_reviewee)
        self.assertEqual(
            set(manager.cycles.get().tokens.values_list('reviewer_email', flat=True)),
            {self.member_one.email, self.member_two.email},
        )
        self.assertNotIn(
            unassigned.email,
            manager.cycles.get().tokens.values_list('reviewer_email', flat=True),
        )

    def test_organization_manager_review_assigns_one_team_label_per_membership(self):
        second_manager_user = UserFactory(email='second-manager@example.com')
        second_manager = UserProfileFactory(
            user=second_manager_user, organization=self.org
        )
        second_team = Team.objects.create(
            organization=self.org, name='Second Team', manager=second_manager
        )
        TeamMembership.objects.get_or_create(
            reviewee=self.member_one, team=second_team
        )
        questionnaire = QuestionnaireFactory(
            organization=self.org,
            allow_peer_review=False,
            allow_self_assessment=False,
            allow_manager_assessment=True,
        )
        campaign = launch_campaign(ReviewCampaign.objects.create(
            organization=self.org,
            created_by=self.manager_user,
            questionnaire=questionnaire,
            target_type='organization',
            cycle_type='manager',
        ))

        assignments = ReviewerToken.objects.filter(
            cycle__campaign=campaign,
            reviewer_email=self.member_one.email,
        )
        self.assertEqual(assignments.count(), 2)
        self.assertEqual(
            set(assignments.values_list('assigned_team__name', flat=True)),
            {'Platform', 'Second Team'},
        )

    def test_organization_manager_review_prefers_explicit_reporting_manager(self):
        reporting_user = UserFactory(email='reporting@example.com')
        reporting_manager = UserProfileFactory(
            user=reporting_user, organization=self.org
        )
        reporting_reviewee = Reviewee.objects.get(
            organization=self.org, email=reporting_user.email
        )
        self.member_one.reporting_manager = reporting_manager
        self.member_one.save(update_fields=['reporting_manager', 'updated_at'])
        questionnaire = QuestionnaireFactory(
            organization=self.org, allow_peer_review=False,
            allow_self_assessment=False, allow_manager_assessment=True,
        )

        campaign = launch_campaign(ReviewCampaign.objects.create(
            organization=self.org, created_by=self.manager_user,
            questionnaire=questionnaire, target_type='organization',
            cycle_type='manager',
        ))

        token = campaign.cycles.get(reviewee=reporting_reviewee).tokens.get(
            reviewer_email=self.member_one.email
        )
        self.assertEqual(token.assigned_team, self.team)
        self.assertFalse(campaign.cycles.filter(
            reviewee=self.manager_reviewee,
            tokens__reviewer_email=self.member_one.email,
        ).exists())

    def test_organization_manager_review_falls_back_to_team_manager(self):
        questionnaire = QuestionnaireFactory(
            organization=self.org, allow_peer_review=False,
            allow_self_assessment=False, allow_manager_assessment=True,
        )

        campaign = launch_campaign(ReviewCampaign.objects.create(
            organization=self.org, created_by=self.manager_user,
            questionnaire=questionnaire, target_type='organization',
            cycle_type='manager',
        ))

        self.assertTrue(campaign.cycles.filter(
            reviewee=self.manager_reviewee,
            tokens__reviewer_email=self.member_one.email,
        ).exists())

    def test_organizational_cycle_shares_self_but_splits_peer_and_manager_by_team(self):
        second_manager_user = UserFactory(email='second-lead@example.com')
        second_manager = UserProfileFactory(
            user=second_manager_user, organization=self.org
        )
        second_team = Team.objects.create(
            organization=self.org, name='Second Team', manager=second_manager
        )
        TeamMembership.objects.get_or_create(
            reviewee=self.member_one, team=second_team
        )
        questionnaires = {
            'self': QuestionnaireFactory(
                organization=self.org, allow_self_assessment=True
            ),
            'peer': QuestionnaireFactory(
                organization=self.org, allow_peer_review=True
            ),
            'manager': QuestionnaireFactory(
                organization=self.org, allow_manager_assessment=True
            ),
        }

        parent = launch_organizational_cycle(
            organization=self.org,
            created_by=self.manager_user,
            questionnaires=questionnaires,
            minimum_peer_reviewers=2,
        )

        self_campaign = parent.campaigns.get(cycle_type='self')
        self.assertEqual(self_campaign.target_type, 'organization')
        self.assertEqual(
            self_campaign.cycles.filter(reviewee=self.member_one).count(), 1
        )
        shared_self_cycle = self_campaign.cycles.get(reviewee=self.member_one)
        self.assertTrue(visible_cycles(
            self.manager_user,
            ReviewCycle.objects.all(),
            self.org,
        ).filter(pk=shared_self_cycle.pk).exists())
        self.assertTrue(visible_cycles(
            second_manager_user,
            ReviewCycle.objects.all(),
            self.org,
        ).filter(pk=shared_self_cycle.pk).exists())
        peer_campaigns = parent.campaigns.filter(cycle_type='peer')
        self.assertEqual(peer_campaigns.count(), 2)
        self.assertEqual(
            peer_campaigns.filter(cycles__reviewee=self.member_one).count(), 2
        )
        manager_campaigns = parent.campaigns.filter(cycle_type='manager')
        self.assertEqual(manager_campaigns.count(), 2)
        self.assertEqual(
            ReviewerToken.objects.filter(
                cycle__campaign__in=manager_campaigns,
                reviewer_email=self.member_one.email,
            ).count(),
            2,
        )

    def test_full_organisational_cycle_uses_reporting_manager_then_team_fallback(self):
        reporting_user = UserFactory(email='line-manager@example.com')
        reporting_manager = UserProfileFactory(
            user=reporting_user, organization=self.org
        )
        reporting_reviewee = Reviewee.objects.get(email=reporting_user.email)
        self.member_one.reporting_manager = reporting_manager
        self.member_one.save(update_fields=['reporting_manager', 'updated_at'])
        questionnaires = {
            'self': QuestionnaireFactory(
                organization=self.org, allow_self_assessment=True
            ),
            'peer': QuestionnaireFactory(
                organization=self.org, allow_peer_review=True
            ),
            'manager': QuestionnaireFactory(
                organization=self.org, allow_manager_assessment=True
            ),
        }

        parent = launch_organizational_cycle(
            organization=self.org,
            created_by=self.manager_user,
            questionnaires=questionnaires,
            minimum_peer_reviewers=1,
        )

        manager_campaigns = parent.campaigns.filter(cycle_type='manager')
        self.assertTrue(manager_campaigns.filter(
            cycles__reviewee=reporting_reviewee,
            cycles__tokens__reviewer_email=self.member_one.email,
        ).exists())
        self.assertTrue(manager_campaigns.filter(
            cycles__reviewee=self.manager_reviewee,
            cycles__tokens__reviewer_email=self.member_two.email,
        ).exists())
        self.assertFalse(manager_campaigns.filter(
            cycles__reviewee=self.manager_reviewee,
            cycles__tokens__reviewer_email=self.member_one.email,
        ).exists())

    def test_individual_organisation_audience_without_team_gets_only_self(self):
        unteamed = RevieweeFactory(
            organization=self.org, team=None, email='unteamed@example.com'
        )
        questionnaires = {
            'self': QuestionnaireFactory(
                organization=self.org, allow_self_assessment=True
            ),
            'peer': QuestionnaireFactory(
                organization=self.org, allow_peer_review=True
            ),
        }

        parent = launch_organizational_cycle(
            organization=self.org,
            created_by=self.manager_user,
            questionnaires=questionnaires,
            minimum_peer_reviewers=2,
            audience_type='individuals',
            participants=[unteamed],
        )

        self.assertEqual(parent.audience_type, 'individuals')
        self.assertIsNone(parent.manager_questionnaire)
        self.assertEqual(parent.campaigns.count(), 1)
        self.assertTrue(
            parent.campaigns.get(cycle_type='self').cycles.filter(
                reviewee=unteamed
            ).exists()
        )
        self.assertFalse(parent.campaigns.filter(cycle_type='manager').exists())

    def test_individual_organisation_audience_targets_only_selected_people(self):
        excluded = RevieweeFactory(
            organization=self.org, team=self.team, email='excluded@example.com'
        )
        questionnaires = {
            'self': QuestionnaireFactory(
                organization=self.org, allow_self_assessment=True
            ),
            'peer': QuestionnaireFactory(
                organization=self.org, allow_peer_review=True
            ),
        }

        parent = launch_organizational_cycle(
            organization=self.org,
            created_by=self.manager_user,
            questionnaires=questionnaires,
            minimum_peer_reviewers=2,
            audience_type='individuals',
            participants=[self.member_one, self.member_two],
        )

        self.assertEqual(
            set(parent.selected_reviewees.values_list('pk', flat=True)),
            {self.member_one.pk, self.member_two.pk},
        )
        self.assertFalse(parent.campaigns.filter(cycle_type='manager').exists())
        self.assertEqual(
            set(parent.campaigns.get(cycle_type='self').cycles.values_list(
                'reviewee_id', flat=True
            )),
            {self.member_one.pk, self.member_two.pk},
        )
        self.assertEqual(
            set(parent.campaigns.get(cycle_type='peer').cycles.values_list(
                'reviewee_id', flat=True
            )),
            {self.member_one.pk, self.member_two.pk},
        )
        self.assertFalse(
            parent.campaigns.filter(cycles__reviewee=excluded).exists()
        )

    def test_peer_report_requires_campaign_nomination_minimum(self):
        from reports.services import generate_report

        questionnaire = QuestionnaireFactory(
            organization=self.org, allow_peer_review=True
        )
        campaign = launch_campaign(self.campaign(
            'peer', questionnaire, minimum_peer_reviewers=3
        ))
        cycle = campaign.cycles.get(reviewee=self.member_one)
        ReviewerToken.objects.create(
            cycle=cycle, category='peer', reviewer_email='one@example.com',
            completed_at=timezone.now(),
        )
        ReviewerToken.objects.create(
            cycle=cycle, category='peer', reviewer_email='two@example.com',
            completed_at=timezone.now(),
        )

        with self.assertRaisesMessage(ValidationError, 'At least 3 completed'):
            generate_report(cycle)


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
        self.assertNotContains(response, 'Entire organisation')
        self.assertContains(response, 'Member email addresses')
        self.assertContains(response, 'Select one or more teams')

    def test_team_member_can_choose_organisation_but_not_entire_organisation(self):
        member_user = UserFactory(email=self.member.email)
        member_profile = UserProfileFactory(
            user=member_user,
            organization=self.org,
        )
        self.member.profile = member_profile
        self.member.save(update_fields=['profile', 'updated_at'])
        self.client.force_login(member_user)

        response = self.client.get(reverse('review_cycle_create'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<strong>Organisation</strong>', html=False)
        self.assertContains(response, 'Individual(s)')
        self.assertContains(response, 'Team(s)')
        self.assertNotContains(response, 'Entire organisation')
        self.assertContains(response, self.team.name)

    def test_team_member_cannot_post_entire_organisation_cycle(self):
        member_user = UserFactory(email=self.member.email)
        member_profile = UserProfileFactory(
            user=member_user,
            organization=self.org,
        )
        self.member.profile = member_profile
        self.member.save(update_fields=['profile', 'updated_at'])
        self.client.force_login(member_user)

        response = self.client.post(reverse('review_cycle_create'), {
            'campaign_flow': '1',
            'target_type': 'organization',
            'organization_audience': 'entire',
            'minimum_peer_reviewers': 3,
            'due_date': '2026-09-01',
        })

        self.assertEqual(response.status_code, 403)

    def test_create_cycle_rejects_a_past_due_date(self):
        past_due_date = timezone.localdate() - timedelta(days=1)

        response = self.client.post(reverse('review_cycle_create'), {
            'campaign_flow': '1',
            'target_type': 'team',
            'team': self.team.id,
            'cycle_type': 'manager',
            'questionnaire': self.questionnaire.id,
            'due_date': past_due_date.isoformat(),
        })

        self.assertRedirects(response, reverse('review_cycle_create'))
        self.assertFalse(ReviewCampaign.objects.exists())
        messages = list(response.wsgi_request._messages)
        self.assertIn('The due date cannot be in the past.', str(messages[0]))

    def test_create_cycle_date_inputs_disallow_past_dates(self):
        response = self.client.get(reverse('review_cycle_create'))
        minimum = timezone.localdate().isoformat()

        self.assertContains(response, f'min="{minimum}"', count=2)

    @patch('reviews.services.send_campaign_invitations')
    def test_manager_can_send_one_assessment_to_multiple_individuals(self, send):
        self_questionnaire = QuestionnaireFactory(
            organization=self.org,
            allow_self_assessment=True,
        )
        second_member = RevieweeFactory(
            organization=self.org,
            team=self.team,
            email='second@example.com',
        )

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse('review_cycle_create'), {
                'campaign_flow': '1',
                'target_type': 'individual',
                'multiple_individuals': '1',
                'individual_emails': (
                    f'{self.member.email},\n{second_member.email}'
                ),
                'cycle_type': 'self',
                'questionnaire': self_questionnaire.id,
                'due_date': '2026-09-01',
            })

        self.assertRedirects(response, reverse('admin_dashboard'))
        self.assertEqual(ReviewCampaign.objects.count(), 2)
        self.assertEqual(
            set(ReviewCampaign.objects.values_list('individual__email', flat=True)),
            {self.member.email, second_member.email},
        )
        self.assertEqual(send.call_count, 2)

    @patch('reviews.services.send_organizational_cycle_invitations')
    def test_admin_can_launch_organizational_cycle(self, send):
        from accounts.permissions import assign_organization_admin

        assign_organization_admin(self.manager_user)
        self_q = QuestionnaireFactory(
            organization=self.org, allow_self_assessment=True
        )
        peer_q = QuestionnaireFactory(
            organization=self.org, allow_peer_review=True
        )

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse('review_cycle_create'), {
                'campaign_flow': '1',
                'target_type': 'organization',
                'cycle_name': 'Q3 Development Review',
                'self_questionnaire': self_q.id,
                'peer_questionnaire': peer_q.id,
                'manager_questionnaire': self.questionnaire.id,
                'minimum_peer_reviewers': 4,
                'due_date': '2026-09-01',
            })

        self.assertRedirects(response, reverse('admin_dashboard'))
        parent = OrganizationalReviewCycle.objects.get()
        self.assertEqual(parent.name, 'Q3 Development Review')
        self.assertEqual(parent.minimum_peer_reviewers, 4)
        send.assert_called_once_with(parent)
        dashboard = self.client.get(reverse('admin_dashboard'))
        self.assertContains(dashboard, 'Organisation cycle progress')
        self.assertContains(dashboard, 'Active Cycles')
        self.assertNotContains(dashboard, '>Organisation Cycles<')
        self.assertContains(dashboard, 'Q3 Development Review')
        self.assertContains(dashboard, 'End my teams’ cycle')
        self.assertContains(dashboard, 'End entire organisation cycle')
        self.assertContains(
            dashboard,
            reverse('close_organizational_cycle_scope', args=[parent.uuid]),
        )

        cycles_page = self.client.get(reverse('review_cycle_list'))
        self.assertContains(cycles_page, 'Cycles')
        self.assertContains(cycles_page, 'Q3 Development Review')
        self.assertContains(cycles_page, 'View reports')
        summary_url = reverse('organisation_cycle_detail', args=[parent.uuid])
        self.assertContains(cycles_page, summary_url)
        cycle_summary = self.client.get(summary_url)
        self.assertContains(cycle_summary, 'Related Assessment Cycles')
        self.assertContains(cycle_summary, 'Peer Review')
        self.assertContains(cycle_summary, 'Manager Assessment')

        reported_cycle = parent.campaigns.order_by('created_at').first().cycles.first()
        Report.objects.create(cycle=reported_cycle, report_data={})
        cycle_summary = self.client.get(summary_url)
        report_url = reverse('reports:view_report', args=[reported_cycle.uuid])
        self.assertContains(cycle_summary, report_url)
        self.assertContains(cycle_summary, 'View report')
        self.assertNotContains(cycle_summary, 'View assessment details')

        cycles_with_report = self.client.get(reverse('review_cycle_list'))
        self.assertContains(cycles_with_report, report_url)
        self.assertNotContains(cycles_with_report, 'View reports')

        reports_page = self.client.get(reverse('report_list'))
        self.assertContains(reports_page, parent.display_name)
        self.assertContains(reports_page, reported_cycle.reviewee.name)
        self.assertContains(reports_page, report_url)

        dashboard_with_report = self.client.get(reverse('admin_dashboard'))
        self.assertContains(dashboard_with_report, 'Recent Reports')
        self.assertContains(dashboard_with_report, parent.display_name)
        self.assertContains(dashboard_with_report, report_url)

        close_url = reverse(
            'close_organizational_cycle_scope', args=[parent.uuid]
        )
        confirmation = self.client.post(close_url, {'scope': 'organization'})
        self.assertEqual(confirmation.status_code, 200)
        self.assertContains(confirmation, 'Outstanding items')
        self.assertContains(confirmation, 'End cycle anyway')
        self.assertContains(confirmation, '0 / 1 complete')

        closed = self.client.post(close_url, {
            'scope': 'organization',
            'confirm_end': '1',
        })
        self.assertRedirects(closed, reverse('admin_dashboard'))
        parent.refresh_from_db()
        self.assertEqual(parent.status, 'completed')
        self.assertFalse(
            ReviewCycle.objects.filter(
                campaign__organizational_cycle=parent,
                status='active',
            ).exists()
        )
        dashboard_after_close = self.client.get(reverse('admin_dashboard'))
        self.assertContains(dashboard_after_close, 'Recent Reports')
        self.assertContains(dashboard_after_close, 'Q3 Development Review')
        completed_cycles_page = self.client.get(reverse('review_cycle_list'))
        self.assertContains(completed_cycles_page, 'Q3 Development Review')
        self.assertContains(completed_cycles_page, 'Completed')
        completed_summary = self.client.get(summary_url)
        self.assertContains(completed_summary, 'Completed')

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
        self.assertContains(response, 'Go back to Dashboard', status_code=404)
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
        self.assertContains(response, 'Go back to Dashboard', status_code=404)

    def test_linked_reviewee_can_open_nominations_when_email_has_changed(self):
        self.member.email = 'old-member-address@example.com'
        self.member.save(update_fields=['email', 'updated_at'])
        self.client.force_login(self.member_user)

        response = self.client.get(
            reverse('nominate_peer_reviewers', args=[self.cycle.uuid])
        )

        self.assertEqual(response.status_code, 200)

    def test_direct_manager_is_visible_but_cannot_be_selected_as_a_peer(self):
        self.member.reporting_manager = self.manager
        self.member.save(update_fields=['reporting_manager', 'updated_at'])
        manager_reviewee = self.manager.reviewee
        self.client.force_login(self.member_user)

        response = self.client.get(
            reverse('nominate_peer_reviewers', args=[self.cycle.uuid])
        )

        self.assertContains(response, 'Direct manager — unavailable')
        self.assertContains(
            response,
            f'name="reviewers" value="{manager_reviewee.id}"  disabled',
        )

        response = self.client.post(
            reverse('nominate_peer_reviewers', args=[self.cycle.uuid]),
            {'reviewers': [manager_reviewee.id]},
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.cycle.tokens.exists())

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

    def test_peer_picker_has_search_and_team_filter(self):
        self.client.force_login(self.member_user)

        response = self.client.get(
            reverse('nominate_peer_reviewers', args=[self.cycle.uuid])
        )

        self.assertContains(response, 'id="peerSearch"')
        self.assertContains(response, 'id="peerTeamFilter"')
        self.assertContains(response, self.team.name)

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
        self.assertNotContains(response, self.peer.name)
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

    def test_manager_dashboard_peer_header_shows_nomination_status_and_resend(self):
        self.client.force_login(self.manager_user)

        response = self.client.get(reverse('admin_dashboard'))

        self.assertContains(response, 'Awaiting peer selection')
        self.assertContains(response, 'Resend selection invitation')
        self.assertNotContains(response, 'Resend nomination invitation')
        self.assertNotContains(response, 'Awaiting reviewer selections')
        self.assertContains(
            response,
            reverse('send_campaign_cycle_reminder', args=[
                self.campaign.uuid, self.cycle.uuid,
            ]),
        )

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
        self.assertContains(response, 'Cycle')
        self.assertContains(response, 'Reviewee')
        self.assertContains(response, 'Assigned by')
        self.assertContains(response, 'Peer Review')
        self.assertContains(response, self.manager_user.get_full_name())
        self.assertContains(response, reverse('reviews:feedback_form', args=[token.token]))
        self.assertContains(response, 'Start')
        self.assertNotContains(
            response, reverse('reviews:feedback_form', args=[other_token.token])
        )

    def test_ended_cycle_removes_peer_selection_and_review_tasks(self):
        token = self.cycle.tokens.create(
            category='self', reviewer_email=self.member_user.email,
            invitation_sent_at=timezone.now(),
        )
        self.cycle.status = 'completed'
        self.cycle.save(update_fields=['status', 'updated_at'])
        self.client.force_login(self.member_user)

        response = self.client.get(reverse('admin_dashboard'))

        self.assertNotContains(response, 'Select peer reviewers')
        self.assertNotContains(
            response, reverse('reviews:feedback_form', args=[token.token])
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

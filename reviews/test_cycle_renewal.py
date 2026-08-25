from datetime import date

from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.factories import RevieweeFactory, UserProfileFactory
from core.factories import OrganizationFactory, UserFactory
from questionnaires.factories import QuestionnaireFactory
from reviews.cycle_services import renew_cycle
from reviews.models import ReviewCycle, ReviewerToken


class CycleRenewalServiceTests(TestCase):
    def setUp(self):
        self.org = OrganizationFactory()
        self.user = UserFactory()
        UserProfileFactory(user=self.user, organization=self.org)
        self.reviewee = RevieweeFactory(organization=self.org)
        self.questionnaire = QuestionnaireFactory(organization=self.org)
        self.source = ReviewCycle.objects.create(
            reviewee=self.reviewee,
            questionnaire=self.questionnaire,
            created_by=self.user,
            status='completed',
            cycle_type='manager',
            start_date=date(2026, 1, 1),
            due_date=date(2026, 1, 22),
        )
        self.token = ReviewerToken.objects.create(
            cycle=self.source,
            category='direct_report',
            reviewer_email='member@example.com',
            completed_at=timezone.now(),
        )

    def test_renewal_copies_configuration_and_people_not_completion_state(self):
        renewed = renew_cycle(self.source, self.user, start_date=date(2026, 2, 1))

        self.assertEqual(renewed.renewed_from, self.source)
        self.assertEqual(renewed.questionnaire, self.questionnaire)
        self.assertEqual(renewed.cycle_type, 'manager')
        self.assertEqual(renewed.due_date, date(2026, 2, 22))
        copied = renewed.tokens.get()
        self.assertEqual(copied.reviewer_email, 'member@example.com')
        self.assertEqual(copied.category, 'direct_report')
        self.assertIsNone(copied.completed_at)
        self.source.refresh_from_db()
        self.assertEqual(self.source.status, 'completed')
        self.assertIsNotNone(self.token.completed_at)

    def test_renewal_defaults_to_thirty_days_without_previous_dates(self):
        self.source.start_date = None
        self.source.due_date = None
        self.source.save(update_fields=['start_date', 'due_date'])

        renewed = renew_cycle(self.source, self.user, start_date=date(2026, 3, 1))

        self.assertEqual(renewed.due_date, date(2026, 3, 31))


class CycleRenewalViewTests(TestCase):
    def setUp(self):
        self.org = OrganizationFactory()
        self.admin = UserFactory()
        UserProfileFactory(user=self.admin, organization=self.org)
        self.admin.user_permissions.add(
            Permission.objects.get(codename='can_manage_organization')
        )
        self.reviewee = RevieweeFactory(organization=self.org)
        self.questionnaire = QuestionnaireFactory(organization=self.org)
        self.source = ReviewCycle.objects.create(
            reviewee=self.reviewee,
            questionnaire=self.questionnaire,
            created_by=self.admin,
        )

    def test_admin_can_renew_with_selected_dates(self):
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse('renew_review_cycle', args=[self.source.uuid]),
            {'start_date': '2026-04-01', 'due_date': '2026-04-20'},
        )

        renewed = ReviewCycle.objects.get(renewed_from=self.source)
        self.assertRedirects(
            response, reverse('review_cycle_detail', args=[renewed.uuid])
        )
        self.assertEqual(renewed.start_date, date(2026, 4, 1))
        self.assertEqual(renewed.due_date, date(2026, 4, 20))

    def test_ordinary_member_cannot_renew_cycle(self):
        member = UserFactory()
        UserProfileFactory(user=member, organization=self.org)
        self.client.force_login(member)

        response = self.client.post(
            reverse('renew_review_cycle', args=[self.source.uuid])
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(ReviewCycle.objects.filter(renewed_from=self.source).exists())

    def test_admin_cannot_renew_another_organizations_cycle(self):
        other_org = OrganizationFactory()
        other_cycle = ReviewCycle.objects.create(
            reviewee=RevieweeFactory(organization=other_org),
            questionnaire=QuestionnaireFactory(organization=other_org),
            created_by=self.admin,
        )
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse('renew_review_cycle', args=[other_cycle.uuid])
        )

        self.assertEqual(response.status_code, 404)
        self.assertContains(response, 'Go back to Dashboard', status_code=404)
        self.assertFalse(ReviewCycle.objects.filter(renewed_from=other_cycle).exists())

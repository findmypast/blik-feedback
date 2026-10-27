import importlib

from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount
from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TestCase

from accounts.models import (
    OrganizationInvitation,
    Reviewee,
    Team,
    TeamLeadGrant,
    TeamMembership,
    UserProfile,
)
from core.factories import OrganizationFactory
from questionnaires.factories import QuestionnaireFactory
from reviews.models import ReviewCampaign, ReviewCycle, ReviewerToken

migration = importlib.import_module(
    'accounts.migrations.0022_fix_duplicate_account_email'
)
TYPO = migration.TYPO_EMAIL
CORRECT = migration.CORRECT_EMAIL
User = get_user_model()


def run_fix():
    migration.fix_duplicate_account_email(apps, None)


class FixDuplicateAccountEmailTests(TestCase):
    def setUp(self):
        self.org = OrganizationFactory(name='Findmypast')
        self.questionnaire = QuestionnaireFactory(organization=self.org)

    def _user(self, email, with_profile=True):
        user = User.objects.create_user(username=email, email=email, password='secret')
        profile = None
        if with_profile:
            profile = UserProfile.objects.create(user=user, organization=self.org)
        return user, profile

    def _cycle(self, reviewee):
        return ReviewCycle.objects.create(
            reviewee=reviewee,
            questionnaire=self.questionnaire,
            status='active',
        )

    def test_noop_when_pair_is_absent(self):
        run_fix()
        self.assertFalse(User.objects.exists())

    def test_partial_pair_raises_without_writing(self):
        user_a, profile_a = self._user(TYPO)
        reviewee_a = Reviewee.objects.get(email=TYPO)
        reviewee_a.profile = profile_a
        reviewee_a.save(update_fields=['profile', 'updated_at'])
        self._cycle(reviewee_a)

        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                run_fix()

        user_a.refresh_from_db()
        reviewee_a.refresh_from_db()
        self.assertEqual(user_a.email, TYPO)
        self.assertEqual(reviewee_a.email, TYPO)

    def test_refuses_when_correct_email_is_already_in_a_cycle(self):
        user_a, profile_a = self._user(TYPO)
        user_b, profile_b = self._user(CORRECT)
        reviewee_a = Reviewee.objects.get(email=TYPO)
        reviewee_b = Reviewee.objects.get(email=CORRECT)
        reviewee_a.profile = profile_a
        reviewee_b.profile = profile_b
        reviewee_a.save(update_fields=['profile', 'updated_at'])
        reviewee_b.save(update_fields=['profile', 'updated_at'])
        self._cycle(reviewee_a)
        self._cycle(reviewee_b)

        with self.assertRaisesMessage(RuntimeError, 'refusing to anonymize'):
            with transaction.atomic():
                run_fix()

        user_a.refresh_from_db()
        user_b.refresh_from_db()
        self.assertEqual(user_a.email, TYPO)
        self.assertEqual(user_b.email, CORRECT)

    def test_refuses_when_correct_email_is_a_campaign_target(self):
        self._user(TYPO)
        self._user(CORRECT)
        reviewee_a = Reviewee.objects.get(email=TYPO)
        reviewee_b = Reviewee.objects.get(email=CORRECT)
        self._cycle(reviewee_a)
        ReviewCampaign.objects.create(
            organization=self.org,
            questionnaire=self.questionnaire,
            target_type='individual',
            individual=reviewee_b,
            cycle_type='peer',
        )

        with self.assertRaisesMessage(RuntimeError, 'review campaign'):
            with transaction.atomic():
                run_fix()

        reviewee_b.refresh_from_db()
        self.assertEqual(reviewee_b.email, CORRECT)

    def test_renames_in_cycle_account_and_frees_duplicate(self):
        user_a, profile_a = self._user(TYPO)
        user_b, profile_b = self._user(CORRECT)
        user_a.is_active = False
        user_a.save(update_fields=['is_active'])
        reviewee_a = Reviewee.objects.get(email=TYPO)
        reviewee_b = Reviewee.objects.get(email=CORRECT)
        reviewee_a.profile = profile_a
        reviewee_a.is_active = False
        reviewee_b.profile = profile_b
        reviewee_a.save(update_fields=['profile', 'is_active', 'updated_at'])
        reviewee_b.save(update_fields=['profile', 'updated_at'])
        cycle = self._cycle(reviewee_a)
        token = ReviewerToken.objects.create(
            cycle=cycle, category='self', reviewer_email=TYPO,
        )
        team = Team.objects.create(
            organization=self.org, name='Ops', manager=profile_b,
            pending_manager_email=TYPO,
        )
        TeamLeadGrant.objects.create(profile=profile_b, team=team, include_descendants=True)
        report = Reviewee.objects.create(
            organization=self.org,
            name='Direct Report',
            email='report@findmypast.com',
            reporting_manager=profile_b,
        )
        other_org = OrganizationFactory(name='Other Org')
        OrganizationInvitation.objects.create(
            organization=self.org,
            email=TYPO,
            invited_by=user_a,
            expires_at=reviewee_a.created_at,
        )
        OrganizationInvitation.objects.create(
            organization=self.org,
            email=CORRECT,
            invited_by=user_a,
            expires_at=reviewee_a.created_at,
        )
        other_invite = OrganizationInvitation.objects.create(
            organization=other_org,
            email=CORRECT,
            invited_by=user_a,
            expires_at=reviewee_a.created_at,
        )
        SocialAccount.objects.create(
            user=user_b, provider='microsoft', uid='correct-uid', extra_data={},
        )
        EmailAddress.objects.create(
            user=user_a, email=TYPO, verified=True, primary=True,
        )
        EmailAddress.objects.create(
            user=user_b, email=CORRECT, verified=True, primary=True,
        )

        run_fix()

        user_a.refresh_from_db()
        user_b.refresh_from_db()
        reviewee_a.refresh_from_db()
        reviewee_b.refresh_from_db()
        cycle.refresh_from_db()
        token.refresh_from_db()
        team.refresh_from_db()
        report.refresh_from_db()

        self.assertEqual(cycle.reviewee_id, reviewee_a.id)
        self.assertEqual(user_a.email, CORRECT)
        self.assertEqual(user_a.username, CORRECT)
        self.assertTrue(user_a.is_active)
        self.assertEqual(reviewee_a.email, CORRECT)
        self.assertFalse(reviewee_a.is_active)
        self.assertEqual(reviewee_a.profile_id, profile_a.id)
        self.assertEqual(token.reviewer_email, CORRECT)
        self.assertFalse(user_b.is_active)
        self.assertTrue(user_b.email.startswith('deleted-b-user-'))
        self.assertTrue(user_b.password.startswith('!'))
        self.assertTrue(reviewee_b.email.startswith('deleted-b-'))
        self.assertIsNone(reviewee_b.profile_id)
        self.assertFalse(UserProfile.objects.filter(user=user_b).exists())
        self.assertEqual(team.manager_id, profile_a.id)
        self.assertEqual(team.pending_manager_email, CORRECT)
        self.assertEqual(reviewee_a.team_id, team.id)
        self.assertTrue(
            TeamMembership.objects.filter(reviewee=reviewee_a, team=team).exists()
        )
        self.assertEqual(report.reporting_manager_id, profile_a.id)
        grant = TeamLeadGrant.objects.get(team=team)
        self.assertEqual(grant.profile_id, profile_a.id)
        self.assertTrue(grant.include_descendants)
        self.assertEqual(
            SocialAccount.objects.get(provider='microsoft', uid='correct-uid').user_id,
            user_a.id,
        )
        self.assertFalse(EmailAddress.objects.filter(user=user_b).exists())
        self.assertEqual(
            list(EmailAddress.objects.filter(user=user_a).values_list('email', flat=True)),
            [CORRECT],
        )
        self.assertEqual(
            list(OrganizationInvitation.objects.filter(organization=self.org).values_list('email', flat=True)),
            [CORRECT],
        )
        other_invite.refresh_from_db()
        self.assertEqual(other_invite.email, CORRECT)

    def test_moves_profile_when_in_cycle_user_has_none(self):
        user_a, _profile = self._user(TYPO, with_profile=False)
        _user_b, profile_b = self._user(CORRECT)
        reviewee_a = Reviewee.objects.create(
            organization=self.org, name='Typo', email=TYPO,
        )
        self._cycle(reviewee_a)
        report = Reviewee.objects.create(
            organization=self.org,
            name='Direct Report',
            email='report@findmypast.com',
            reporting_manager=profile_b,
        )
        team = Team.objects.create(organization=self.org, name='Ops', manager=profile_b)

        run_fix()

        user_a.refresh_from_db()
        profile_b.refresh_from_db()
        reviewee_a.refresh_from_db()
        report.refresh_from_db()
        self.assertEqual(user_a.email, CORRECT)
        self.assertEqual(profile_b.user_id, user_a.id)
        self.assertEqual(reviewee_a.profile_id, profile_b.id)
        self.assertEqual(report.reporting_manager_id, profile_b.id)
        self.assertEqual(team.manager_id, profile_b.id)
        self.assertEqual(reviewee_a.team_id, team.id)
        self.assertTrue(
            TeamMembership.objects.filter(reviewee=reviewee_a, team=team).exists()
        )

    def test_keeps_correct_account_team_when_typo_has_none(self):
        _user_a, profile_a = self._user(TYPO)
        _user_b, profile_b = self._user(CORRECT)
        reviewee_a = Reviewee.objects.get(email=TYPO)
        reviewee_b = Reviewee.objects.get(email=CORRECT)
        reviewee_a.profile = profile_a
        reviewee_b.profile = profile_b
        reviewee_a.save(update_fields=['profile', 'updated_at'])
        reviewee_b.save(update_fields=['profile', 'updated_at'])
        self._cycle(reviewee_a)
        team = Team.objects.create(organization=self.org, name='Product')
        extra = Team.objects.create(organization=self.org, name='Design')
        reviewee_b.team = team
        reviewee_b.save(update_fields=['team', 'updated_at'])
        TeamMembership.objects.create(reviewee=reviewee_b, team=extra)
        self.assertIsNone(reviewee_a.team_id)

        run_fix()

        reviewee_a.refresh_from_db()
        self.assertEqual(reviewee_a.team_id, team.id)
        self.assertTrue(TeamMembership.objects.filter(reviewee=reviewee_a, team=team).exists())
        self.assertTrue(TeamMembership.objects.filter(reviewee=reviewee_a, team=extra).exists())
        self.assertFalse(TeamMembership.objects.filter(reviewee=reviewee_b).exists())

    def test_keeps_correct_login_when_typo_has_no_user(self):
        reviewee_a = Reviewee.objects.create(
            organization=self.org, name='Typo', email=TYPO,
        )
        cycle = self._cycle(reviewee_a)
        user_b, profile_b = self._user(CORRECT)
        reviewee_b = Reviewee.objects.get(email=CORRECT)
        reviewee_b.profile = profile_b
        reviewee_b.save(update_fields=['profile', 'updated_at'])
        team = Team.objects.create(organization=self.org, name='Product')
        reviewee_b.team = team
        reviewee_b.save(update_fields=['team', 'updated_at'])

        run_fix()

        user_b.refresh_from_db()
        reviewee_a.refresh_from_db()
        reviewee_b.refresh_from_db()
        cycle.refresh_from_db()
        self.assertEqual(cycle.reviewee_id, reviewee_a.id)
        self.assertEqual(user_b.email, CORRECT)
        self.assertTrue(user_b.is_active)
        self.assertEqual(user_b.profile.id, profile_b.id)
        self.assertEqual(reviewee_a.email, CORRECT)
        self.assertEqual(reviewee_a.profile_id, profile_b.id)
        self.assertEqual(reviewee_a.team_id, team.id)
        self.assertTrue(reviewee_b.email.startswith('deleted-b-'))
        self.assertFalse(User.objects.filter(email__iexact=TYPO).exists())

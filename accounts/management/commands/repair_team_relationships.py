from django.core.management.base import BaseCommand
from django.db import transaction

from accounts.models import Reviewee, Team, TeamMembership, UserProfile


class Command(BaseCommand):
    help = (
        'Report and repair legacy team leader memberships and missing reporting managers. '
        'The command is a dry run unless --apply is supplied.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply',
            action='store_true',
            help='Persist safe, unambiguous repairs.',
        )

    @transaction.atomic
    def handle(self, *args, **options):
        apply_changes = options['apply']
        counts = {
            'team_managers': 0,
            'leader_memberships': 0,
            'reporting_managers': 0,
            'ambiguous': 0,
        }

        teams = Team.objects.select_related('organization', 'manager__user').order_by(
            'organization_id', 'name'
        )
        for team in teams:
            if not team.manager_id and team.pending_manager_email:
                matches = UserProfile.objects.filter(
                    organization_id=team.organization_id,
                    user__email__iexact=team.pending_manager_email.strip(),
                )
                if matches.count() == 1:
                    manager = matches.first()
                    self._change(
                        apply_changes,
                        f'Set leader for {team.organization.name} / {team.name} to '
                        f'{manager.user.email}.',
                        lambda team=team, manager=manager: self._set_team_manager(team, manager),
                    )
                    team.manager = manager
                    counts['team_managers'] += 1
                elif matches.count() > 1:
                    self._ambiguous(
                        f'{team.organization.name} / {team.name} has multiple profiles matching '
                        f'{team.pending_manager_email}.'
                    )
                    counts['ambiguous'] += 1

            if not team.manager_id:
                continue
            try:
                manager_reviewee = team.manager.reviewee
            except Reviewee.DoesNotExist:
                self._ambiguous(
                    f'{team.organization.name} / {team.name} leader '
                    f'{team.manager.user.email} has no reviewee record.'
                )
                counts['ambiguous'] += 1
                continue
            if not TeamMembership.objects.filter(
                reviewee=manager_reviewee, team=team
            ).exists():
                self._change(
                    apply_changes,
                    f'Add team leader {team.manager.user.email} to {team.name}.',
                    lambda manager_reviewee=manager_reviewee, team=team: (
                        TeamMembership.objects.get_or_create(
                            reviewee=manager_reviewee, team=team
                        )
                    ),
                )
                counts['leader_memberships'] += 1

        reviewees = Reviewee.objects.filter(reporting_manager__isnull=True).select_related(
            'organization', 'profile__user'
        ).prefetch_related('teams__manager')
        for reviewee in reviewees:
            manager = self._pending_manager(reviewee)
            if manager is None:
                manager_ids = {
                    team.manager_id
                    for team in self._all_teams(reviewee)
                    if team.manager_id and team.manager_id != reviewee.profile_id
                }
                if len(manager_ids) == 1:
                    manager = UserProfile.objects.get(pk=manager_ids.pop())
                elif len(manager_ids) > 1:
                    self._ambiguous(
                        f'{reviewee.email} belongs to teams with different leaders; reporting '
                        'manager was not changed.'
                    )
                    counts['ambiguous'] += 1
                    continue

            if manager and manager.id != reviewee.profile_id:
                self._change(
                    apply_changes,
                    f'Set reporting manager for {reviewee.email} to {manager.user.email}.',
                    lambda reviewee=reviewee, manager=manager: self._set_reporting_manager(
                        reviewee, manager
                    ),
                )
                counts['reporting_managers'] += 1

        if not apply_changes:
            transaction.set_rollback(True)

        mode = 'Would repair' if not apply_changes else 'Repaired'
        self.stdout.write(
            self.style.SUCCESS(
                f'{mode}: {counts["team_managers"]} team leaders, '
                f'{counts["leader_memberships"]} leader memberships, and '
                f'{counts["reporting_managers"]} reporting managers. '
                f'{counts["ambiguous"]} records require manual review.'
            )
        )

    def _all_teams(self, reviewee):
        teams = {team.id: team for team in reviewee.teams.all()}
        if reviewee.team_id and reviewee.team_id not in teams:
            teams[reviewee.team_id] = Team.objects.select_related('manager').get(
                pk=reviewee.team_id
            )
        return teams.values()

    def _pending_manager(self, reviewee):
        if not reviewee.pending_reporting_manager_email:
            return None
        matches = UserProfile.objects.filter(
            organization_id=reviewee.organization_id,
            user__email__iexact=reviewee.pending_reporting_manager_email.strip(),
        )
        if matches.count() == 1:
            return matches.first()
        if matches.count() > 1:
            self._ambiguous(
                f'{reviewee.email} has multiple profiles matching pending manager '
                f'{reviewee.pending_reporting_manager_email}.'
            )
        return None

    def _change(self, apply_changes, message, callback):
        prefix = 'APPLY' if apply_changes else 'DRY RUN'
        self.stdout.write(f'[{prefix}] {message}')
        if apply_changes:
            callback()

    def _ambiguous(self, message):
        self.stdout.write(self.style.WARNING(f'[MANUAL] {message}'))

    @staticmethod
    def _set_team_manager(team, manager):
        team.manager = manager
        team.pending_manager_email = ''
        team.save(update_fields=['manager', 'pending_manager_email', 'updated_at'])

    @staticmethod
    def _set_reporting_manager(reviewee, manager):
        reviewee.reporting_manager = manager
        reviewee.pending_reporting_manager_email = ''
        reviewee.save(
            update_fields=['reporting_manager', 'pending_reporting_manager_email', 'updated_at']
        )

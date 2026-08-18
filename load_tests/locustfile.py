"""Local-only Locust workloads for the seeded Blik scale-test organisation."""

import itertools
import os

from locust import HttpUser, between, events, task

from load_tests.helpers import feedback_form, feedback_links, require_local_target

PASSWORD = os.getenv('LOAD_TEST_PASSWORD', 'blik-test-password')
ENABLE_WRITES = os.getenv('LOAD_TEST_ENABLE_WRITES') == '1'
DEFAULT_EMAILS = ['scale-admin@scale-test.invalid']
EMAILS = [
    email.strip() for email in os.getenv(
        'LOAD_TEST_EMAILS', ','.join(DEFAULT_EMAILS)
    ).split(',') if email.strip()
]
EMAIL_SEQUENCE = itertools.count()


@events.test_start.add_listener
def enforce_local_target(environment, **_kwargs):
    require_local_target(environment.host or '')


@events.quitting.add_listener
def enforce_thresholds(environment, **_kwargs):
    stats = environment.runner.stats.total
    max_failure_ratio = float(os.getenv('LOAD_TEST_MAX_FAILURE_RATIO', '0.01'))
    max_p95_ms = int(os.getenv('LOAD_TEST_MAX_P95_MS', '1500'))
    p95 = stats.get_response_time_percentile(0.95) or 0
    if stats.fail_ratio > max_failure_ratio or p95 > max_p95_ms:
        environment.process_exit_code = 1


class BlikUser(HttpUser):
    """A synthetic member browsing Blik and optionally completing one task."""

    wait_time = between(1, 3)

    def on_start(self):
        self.email = EMAILS[next(EMAIL_SEQUENCE) % len(EMAILS)]
        login_page = self.client.get('/accounts/login/', name='GET login')
        csrf = login_page.cookies.get('csrftoken') or self.client.cookies.get('csrftoken')
        with self.client.post(
            '/accounts/login/',
            {'login': self.email, 'password': PASSWORD, 'csrfmiddlewaretoken': csrf},
            headers={'Referer': f'{self.host}/accounts/login/'},
            name='POST login',
            allow_redirects=True,
            catch_response=True,
        ) as response:
            if response.status_code != 200 or '/accounts/login/' in response.url:
                response.failure(f'Login failed for {self.email}')
                self.stop(force=True)

    @task(6)
    def dashboard(self):
        self.client.get('/dashboard/', name='GET dashboard')

    @task(2)
    def teams(self):
        self.client.get('/dashboard/team/', name='GET teams')

    @task(2)
    def cycles(self):
        self.client.get('/dashboard/cycles/', name='GET cycles')

    @task(1)
    def reports(self):
        self.client.get('/dashboard/reports/', name='GET reports')

    @task(1)
    def complete_one_assignment(self):
        if not ENABLE_WRITES:
            return
        dashboard = self.client.get('/dashboard/', name='GET dashboard for task')
        links = feedback_links(dashboard.text)
        if not links:
            return
        assignment = self.client.get(links[0], name='GET assessment')
        form = feedback_form(assignment.text)
        if not form:
            return
        with self.client.post(
            form['action'],
            form['fields'],
            headers={'Referer': f'{self.host}{links[0]}'},
            name='POST assessment',
            catch_response=True,
        ) as response:
            try:
                redirect = response.json().get('redirect')
            except ValueError:
                redirect = None
            if response.status_code != 200 or redirect != '/dashboard/':
                response.failure('Assessment did not complete and redirect to dashboard')

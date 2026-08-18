from unittest import TestCase

from load_tests.helpers import feedback_form, feedback_links, require_local_target


class LoadTestHelperTests(TestCase):
    def test_only_local_targets_are_allowed(self):
        require_local_target('http://localhost:8000')
        require_local_target('http://127.0.0.1:8000')
        with self.assertRaises(ValueError):
            require_local_target('https://blik.integration.example.com')

    def test_extracts_assignment_link_and_submission_values(self):
        page = '''
        <a href="/feedback/abc/">Start</a>
        <form action="/feedback/abc/submit/" method="post">
          <input name="csrfmiddlewaretoken" value="csrf">
          <input type="radio" name="question_1" value="1">
          <textarea name="question_2"></textarea>
          <select name="question_3"><option value="">Choose</option><option value="Yes">Yes</option></select>
        </form>
        '''
        self.assertEqual(feedback_links(page), ['/feedback/abc/'])
        form = feedback_form(page)
        self.assertEqual(form['action'], '/feedback/abc/submit/')
        self.assertEqual(form['fields']['question_1'], '1')
        self.assertEqual(form['fields']['question_3'], 'Yes')
        self.assertIn('feedback', form['fields']['question_2'])

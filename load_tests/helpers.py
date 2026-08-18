"""Dependency-free helpers shared by the Locust scenarios and their tests."""

from html.parser import HTMLParser
from urllib.parse import urlparse

LOCAL_HOSTS = {'localhost', '127.0.0.1', '::1'}


def require_local_target(host):
    """Reject accidental load tests against integration or production."""
    parsed = urlparse(host)
    if parsed.scheme not in {'http', 'https'} or parsed.hostname not in LOCAL_HOSTS:
        raise ValueError(
            f'Load tests are local-only; received {host!r}. '
            'Use http://localhost:8000.'
        )


class PageParser(HTMLParser):
    """Extract links and enough form data to submit a generated questionnaire."""

    def __init__(self):
        super().__init__()
        self.links = []
        self.forms = []
        self._form = None
        self._textarea = None
        self._select = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a' and attrs.get('href'):
            self.links.append(attrs['href'])
        elif tag == 'form':
            self._form = {'action': attrs.get('action', ''), 'fields': {}}
        elif self._form is not None and tag == 'input':
            name = attrs.get('name')
            value = attrs.get('value', '')
            if (
                name
                and (name == 'csrfmiddlewaretoken' or name.startswith('question_'))
                and value
                and name not in self._form['fields']
            ):
                self._form['fields'][name] = value
        elif self._form is not None and tag == 'textarea':
            self._textarea = attrs.get('name')
        elif self._form is not None and tag == 'select':
            self._select = attrs.get('name')
        elif self._form is not None and tag == 'option' and self._select:
            value = attrs.get('value', '')
            if value and self._select not in self._form['fields']:
                self._form['fields'][self._select] = value

    def handle_endtag(self, tag):
        if tag == 'form' and self._form is not None:
            self.forms.append(self._form)
            self._form = None
        elif tag == 'textarea' and self._textarea:
            self._form['fields'].setdefault(
                self._textarea, 'Useful, specific feedback from the local load test.'
            )
            self._textarea = None
        elif tag == 'select':
            self._select = None


def parse_page(content):
    parser = PageParser()
    parser.feed(content)
    return parser


def feedback_links(content):
    return [link for link in parse_page(content).links if link.startswith('/feedback/')]


def feedback_form(content):
    forms = parse_page(content).forms
    return next(
        (form for form in forms if '/feedback/' in form['action'] and form['action'].endswith('/submit/')),
        None,
    )

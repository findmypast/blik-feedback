"""Which Organization fields the environment owns.

`setup_organization` runs on every container start and writes any field whose
env var is set. Letting the admin UI edit those same fields produces edits that
silently vanish on the next restart — the bug behind issue #4.

So the environment wins, visibly: fields it sets are rendered read-only on the
settings page and rejected server-side if posted anyway. Fields with no env var
(anonymity threshold, registration flags) stay editable in the UI.
"""
import os

# Organization model field -> environment variable that owns it
ENV_MANAGED_FIELDS = {
    'name': 'ORGANIZATION_NAME',
    'email': 'DEFAULT_FROM_EMAIL',
    'from_email': 'DEFAULT_FROM_EMAIL',
    'smtp_host': 'EMAIL_HOST',
    'smtp_port': 'EMAIL_PORT',
    'smtp_username': 'EMAIL_HOST_USER',
    'smtp_password': 'EMAIL_HOST_PASSWORD',
    'smtp_use_tls': 'EMAIL_USE_TLS',
}


def env_managed_fields():
    """
    Return {field: env_var} for the fields the environment currently owns.

    An env var that is set but empty does not count as owning the field —
    that is how you hand a field back to the UI without editing compose files.
    """
    return {
        field: var
        for field, var in ENV_MANAGED_FIELDS.items()
        if os.environ.get(var)
    }

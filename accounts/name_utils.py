def normalize_name_part(value):
    """Return consistent title casing for an entered first or last name."""
    return ' '.join(part.lower().title() for part in str(value or '').strip().split())

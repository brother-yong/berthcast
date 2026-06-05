"""Small input validators for berthcast. Pure functions — no Flask, no DB —
so they're easy to unit-test in isolation."""


def validate_email_change(new_email, target_user_id, find_user_id_by_email):
    """Validate an admin's request to change target_user_id's email.

    `find_user_id_by_email(email)` returns the id of the account that currently
    uses that email, or None if it's free. Injecting the lookup keeps this
    function pure and testable.

    Returns (normalized_email, None) on success, or (None, error_message) on
    failure. The normalized email is trimmed and lower-cased.
    """
    email = (new_email or "").strip().lower()

    if not email:
        return None, "Email can't be empty."
    if " " in email or email.count("@") != 1:
        return None, "That doesn't look like a valid email address."

    local, _, domain = email.partition("@")
    if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
        return None, "That doesn't look like a valid email address."

    owner = find_user_id_by_email(email)
    if owner is not None and owner != target_user_id:
        return None, "Another account already uses that email."

    return email, None

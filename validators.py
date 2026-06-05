"""Small input validators for berthcast. Pure functions — no Flask, no DB —
so they're easy to unit-test in isolation."""

MIN_PASSWORD_LENGTH = 8


def password_error(new_password):
    """Return an error message if new_password isn't acceptable, else None.

    Uses the same 8-character minimum enforced at sign-up and password reset,
    so admin-set passwords are held to the same bar.
    """
    if not new_password:
        return "Password can't be empty."
    if len(new_password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    return None


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

"""Small input validators for berthcast. Pure functions — no Flask, no DB —
so they're easy to unit-test in isolation."""
import re

MIN_PASSWORD_LENGTH = 8

# Hard ceiling on the number of chunks a chunked upload may declare. At 5 MB per
# chunk this allows ~20 GB — far past any real export — while stopping a forged
# total_chunks from making the server loop billions of times (a worker-freezing
# DoS).
MAX_UPLOAD_CHUNKS = 4000

# Only these characters may survive into a temp-chunk filename.
_UPLOAD_ID_RE = re.compile(r"[^A-Za-z0-9_-]")


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


def sanitize_upload_id(upload_id):
    """Strip a client-supplied chunk upload id down to a filesystem-safe token.

    The id is concatenated into a temp filename (tmp_<id>_<n>). Without this, a
    value like "x/../../etc/foo" would escape the upload folder when joined to a
    path — an authenticated user could write bytes anywhere the worker can reach
    (path traversal / arbitrary file write). Returns the cleaned token
    (letters/digits/_/- only, capped at 64 chars), or "" when nothing safe
    remains — callers MUST reject "".
    """
    return _UPLOAD_ID_RE.sub("", str(upload_id or ""))[:64]


# Excel/Sheets treat a cell starting with any of these as a formula when the
# file is opened — so a value like "=HYPERLINK(...)" (straight from an uploaded
# item/supplier name) could execute. Prefixing the cell with a single quote
# makes it show literally.
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe_cell(value):
    """Defuse CSV/Excel formula injection for one exported cell.

    Returns the value as a string, prefixed with a single quote when it begins
    with a formula trigger character. None becomes "". Used on the free-text
    columns of the orders CSV (item/supplier/reason/note), which come from
    uploaded files and the model.
    """
    if value is None:
        return ""
    s = str(value)
    if s[:1] in _CSV_FORMULA_PREFIXES:
        return "'" + s
    return s


def validate_chunk_meta(chunk_index, total_chunks, max_chunks=MAX_UPLOAD_CHUNKS):
    """Validate the chunk counters from a chunked-upload request.

    Returns ((chunk_index, total_chunks), None) as ints on success, or
    (None, error_message) on failure. Bounds total_chunks so a forged huge value
    can't make the server iterate billions of times, and rejects
    non-numeric / negative / out-of-range counters before they touch the loop.
    """
    try:
        ci = int(chunk_index)
        tc = int(total_chunks)
    except (TypeError, ValueError):
        return None, "Bad chunk metadata."
    if tc < 1 or tc > max_chunks:
        return None, "Bad chunk metadata."
    if ci < 0 or ci >= tc:
        return None, "Bad chunk metadata."
    return (ci, tc), None

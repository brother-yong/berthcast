"""Email senders for berthcast (reset, verification, invites, contact form,
analysis-ready and critical-stock alerts).

All mail goes out through Gmail SMTP using three env vars:
  MAIL_USERNAME      the real Gmail account used to log in (owns the app password).
                     Optional - falls back to MAIL_SENDER when unset.
  MAIL_APP_PASSWORD  a Gmail App Password generated on that account.
  MAIL_SENDER        the address shown to recipients in the From header
                     (e.g. admin@berthcast.com, a verified Gmail "send-as" alias).

Splitting login from the From header lets mail be sent *as* admin@berthcast.com
while authenticating as a real Gmail account."""
import os
import smtplib
import html as _html
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import database as db
from logging_setup import logger


def _esc(value):
    """HTML-escape a value before it goes into an HTML email body. Item names,
    observations, org names and addresses all come from user/upload data, so
    interpolating them raw let a name like '<img src=x onerror=...>' inject
    markup into the email."""
    return _html.escape("" if value is None else str(value))


def _oneline(value):
    """Collapse newlines so a value can't inject extra headers when used in an
    email Subject / Reply-To."""
    return ("" if value is None else str(value)).replace("\r", " ").replace("\n", " ").strip()


def _deliver(msg, sender, password, recipient):
    """Send a prepared MIME message via Gmail SMTP. Returns True on success.

    Logs (and never raises) on failure. Email used to fail with a silent
    `except: pass`, so a critical-stock alert could quietly never arrive — now a
    dropped email is at least visible in the logs, with the subject and recipient.
    """
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as smtp:
            smtp.login(os.environ.get("MAIL_USERNAME") or sender, password)
            smtp.sendmail(sender, recipient, msg.as_string())
        logger.info("Sent email %r to %s", msg.get("Subject"), recipient)
        return True
    except Exception:
        logger.warning("Failed to send email %r to %s", msg.get("Subject"), recipient, exc_info=True)
        return False


def _send_critical_alert(user_id: int, upload_session_id: int, new_critical: list,
                          base_url: str = "") -> None:
    """Email the user when items newly enter CRITICAL status versus the previous run."""
    users = db.query("SELECT email FROM users WHERE id=?", (user_id,))
    if not users:
        return
    to_email = users[0]["email"]

    sender   = os.environ.get("MAIL_SENDER", "")
    password = os.environ.get("MAIL_APP_PASSWORD", "")
    if not sender or not password:
        return

    count   = len(new_critical)
    subject = f"⚠ {count} item{'s' if count != 1 else ''} hit critical stock — berthcast"

    results_path = f"{base_url}/results/{upload_session_id}"

    rows_text = "\n".join(
        f"  • {i.get('item','')}  ({i.get('days_of_supply','?')} days of supply remaining)"
        for i in new_critical
    )
    rows_html = "".join(
        f"""<tr>
              <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;font-weight:500;">{_esc(i.get('item',''))}</td>
              <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;color:#c0392b;">{_esc(i.get('days_of_supply','—'))} days</td>
              <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;color:#6b7280;font-size:13px;">{_esc(i.get('observation',''))}</td>
            </tr>"""
        for i in new_critical
    )

    text = (
        f"berthcast stock alert\n\n"
        f"{count} item{'s' if count != 1 else ''} moved to CRITICAL stock level since your last analysis:\n\n"
        f"{rows_text}\n\n"
        f"View the full report: {results_path}\n\n"
        f"— berthcast"
    )
    html = f"""
    <div style="font-family:'Segoe UI',sans-serif;max-width:560px;margin:0 auto;color:#1a2a3a;">
      <div style="background:#fef2f2;border-left:4px solid #c0392b;
                  padding:16px 20px;border-radius:6px;margin-bottom:24px;">
        <div style="font-size:14px;font-weight:700;color:#c0392b;text-transform:uppercase;
                    letter-spacing:0.05em;margin-bottom:4px;">Stock alert</div>
        <div style="font-size:17px;font-weight:600;color:#1a2a3a;">
          {count} item{'s' if count != 1 else ''} moved to critical since your last run
        </div>
      </div>
      <table style="width:100%;border-collapse:collapse;font-size:14px;margin-bottom:24px;">
        <thead>
          <tr style="background:#f9fafb;">
            <th style="padding:8px 12px;text-align:left;font-size:11px;text-transform:uppercase;
                       letter-spacing:0.06em;color:#6b7280;border-bottom:2px solid #e5e7eb;">Item</th>
            <th style="padding:8px 12px;text-align:left;font-size:11px;text-transform:uppercase;
                       letter-spacing:0.06em;color:#6b7280;border-bottom:2px solid #e5e7eb;">Stock runway</th>
            <th style="padding:8px 12px;text-align:left;font-size:11px;text-transform:uppercase;
                       letter-spacing:0.06em;color:#6b7280;border-bottom:2px solid #e5e7eb;">Note</th>
          </tr>
        </thead>
        <tbody>
          {rows_html}
        </tbody>
      </table>
      <a href="{results_path}"
         style="display:inline-block;padding:11px 24px;background:#c8924c;color:#fff;
                text-decoration:none;border-radius:8px;font-weight:600;font-size:14px;">
        View full report →
      </a>
      <p style="font-size:12px;color:#9ca3af;margin-top:24px;">— berthcast</p>
    </div>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = sender
    msg["To"]      = to_email
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    _deliver(msg, sender, password, to_email)


def _send_run_failure_alert(org_name: str, upload_session_id: int, category: str,
                             detail: str = "", base_url: str = "") -> None:
    """Email the OPERATOR (ALERT_EMAIL) when a client's analysis fails in a way
    worth knowing about: it crashed / the worker died ('failed'), or it came back
    blank with zero items ('blank'). The run that produced 'blank' is still saved
    as complete — the client just sees an empty report — so without this the
    operator would never know it happened.

    Deliberately NOT called for a 'refused' run: that's the safety net correctly
    declining unreadable data, a coaching nudge, not an outage. Best-effort: logs
    and never raises, like every other sender here. Goes to ALERT_EMAIL (the
    operator), not to the client."""
    alert_to = os.environ.get("ALERT_EMAIL", "")
    sender   = os.environ.get("MAIL_SENDER", "")
    password = os.environ.get("MAIL_APP_PASSWORD", "")
    if not alert_to or not sender or not password:
        return  # not configured — the admin usage page is still the source of truth

    human = {
        "blank":  "came back BLANK — 0 items reviewed",
        "failed": "FAILED — it crashed or the server restarted mid-run",
    }.get(category, f"ended as '{category}'")

    link = f"{base_url}/results/{upload_session_id}" if base_url else f"session {upload_session_id}"

    body = (
        f"A berthcast analysis for {org_name} {human}.\n\n"
        f"Session: {upload_session_id}\n"
        + (f"Detail: {detail}\n" if detail else "")
        + f"Link: {link}\n\n"
        "This is the operator alert — the client may not have noticed yet.\n"
        "Open the usage page (/admin/usage) for the full picture.\n\n"
        "— berthcast"
    )

    msg = MIMEText(body)
    # org_name and category go into the Subject (a header), so collapse newlines
    # to stop a crafted org name injecting extra email headers.
    msg["Subject"] = f"berthcast alert: {_oneline(org_name)}'s analysis {_oneline(category)}"
    msg["From"]    = sender
    msg["To"]      = alert_to

    _deliver(msg, sender, password, alert_to)


def _send_reset_email(to_email: str, reset_url: str) -> None:
    """Send a password reset link via Gmail SMTP. Fails silently if not configured."""
    sender   = os.environ.get("MAIL_SENDER", "")
    password = os.environ.get("MAIL_APP_PASSWORD", "")
    if not sender or not password:
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Reset your berthcast password"
    msg["From"]    = sender
    msg["To"]      = to_email

    text = (
        f"Hi,\n\n"
        f"Someone requested a password reset for your berthcast account.\n\n"
        f"Click the link below to set a new password. It expires in 1 hour.\n\n"
        f"{reset_url}\n\n"
        f"If you didn't request this, you can ignore this email — your password won't change.\n\n"
        f"— berthcast"
    )
    html = f"""
    <div style="font-family:'Segoe UI',sans-serif;max-width:480px;margin:0 auto;color:#1a2a3a;">
      <p style="font-size:15px;line-height:1.6;">
        Someone requested a password reset for your berthcast account.
      </p>
      <a href="{reset_url}"
         style="display:inline-block;margin:20px 0;padding:12px 28px;
                background:#c8924c;color:#fff;text-decoration:none;
                border-radius:8px;font-weight:600;font-size:14px;">
        Reset password
      </a>
      <p style="font-size:13px;color:#6b7280;line-height:1.5;">
        This link expires in 1 hour. If you didn't request a reset, ignore this email.
      </p>
      <p style="font-size:13px;color:#6b7280;">— berthcast</p>
    </div>
    """
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    _deliver(msg, sender, password, to_email)


def _send_analysis_ready_email(user_id: int, upload_session_id: int,
                                summary: dict, base_url: str = "") -> None:
    """Email the user when their analysis has finished. summary is a small dict
    with: total_items, critical, low, rec_count, flagged."""
    users = db.query("SELECT email FROM users WHERE id=?", (user_id,))
    if not users:
        return
    to_email = users[0]["email"]

    sender   = os.environ.get("MAIL_SENDER", "")
    password = os.environ.get("MAIL_APP_PASSWORD", "")
    if not sender or not password:
        return

    results_path = f"{base_url}/results/{upload_session_id}"
    total    = summary.get("total_items", 0)
    critical = summary.get("critical", 0)
    low      = summary.get("low", 0)
    recs     = summary.get("rec_count", 0)
    flagged  = summary.get("flagged", 0)

    subject = "Your berthcast analysis is ready"

    text = (
        f"Your berthcast analysis is ready.\n\n"
        f"{total} items reviewed.\n"
        f"{critical} flagged as critical, {low} low.\n"
        f"{recs} reorder recommendations ({flagged} flagged for attention).\n\n"
        f"Open it here: {results_path}\n\n"
        f"— berthcast"
    )
    html = f"""
    <div style="font-family:'Inter','Segoe UI',sans-serif;max-width:560px;margin:0 auto;color:#0F1B2D;">
      <div style="font-size:11px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;
                  color:#8B6B3D;margin-bottom:8px;">Analysis ready</div>
      <div style="font-family:'Inter Tight','Inter',sans-serif;font-size:24px;font-weight:600;
                  letter-spacing:-0.01em;color:#0B1424;margin-bottom:16px;">
        Your berthcast analysis is ready
      </div>
      <p style="font-size:14.5px;line-height:1.6;color:#0F1B2D;margin:0 0 14px;">
        {total} items reviewed · <strong style="color:#8B2C2C;">{critical} critical</strong> · {low} low ·
        {recs} reorder recommendation{'s' if recs != 1 else ''}{' · ' + str(flagged) + ' flagged' if flagged else ''}.
      </p>
      <a href="{results_path}"
         style="display:inline-block;margin:18px 0;padding:12px 28px;
                background:#0F1B2D;color:#fff;text-decoration:none;
                border-radius:10px;font-weight:600;font-size:14px;">
        Open the analysis →
      </a>
      <p style="font-size:13px;color:#6B7280;line-height:1.5;margin-top:24px;">
        Tip: edit any recommendation before approving — quantity, supplier, and notes all save automatically.
      </p>
      <p style="font-size:12px;color:#9ca3af;margin-top:18px;">— berthcast</p>
    </div>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = sender
    msg["To"]      = to_email
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    _deliver(msg, sender, password, to_email)


def _send_verification_email(to_email: str, verify_url: str) -> None:
    """Send an email verification link via Gmail SMTP. Fails silently if not configured."""
    sender   = os.environ.get("MAIL_SENDER", "")
    password = os.environ.get("MAIL_APP_PASSWORD", "")
    if not sender or not password:
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Verify your berthcast account"
    msg["From"]    = sender
    msg["To"]      = to_email

    text = (
        f"Hi,\n\n"
        f"Thanks for signing up for berthcast.\n\n"
        f"Click the link below to verify your email and activate your account:\n\n"
        f"{verify_url}\n\n"
        f"This link expires in 24 hours.\n\n"
        f"— berthcast"
    )
    html = f"""
    <div style="font-family:'Segoe UI',sans-serif;max-width:480px;margin:0 auto;color:#1a2a3a;">
      <p style="font-size:15px;line-height:1.6;margin-bottom:8px;">
        Thanks for signing up for berthcast.
      </p>
      <p style="font-size:15px;line-height:1.6;margin-top:0;">
        Click below to verify your email and activate your account.
      </p>
      <a href="{verify_url}"
         style="display:inline-block;margin:20px 0;padding:12px 28px;
                background:#c8924c;color:#fff;text-decoration:none;
                border-radius:8px;font-weight:600;font-size:14px;">
        Verify my email
      </a>
      <p style="font-size:13px;color:#6b7280;line-height:1.5;">
        This link expires in 24 hours. If you didn't sign up, you can ignore this email.
      </p>
      <p style="font-size:13px;color:#6b7280;">— berthcast</p>
    </div>
    """
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    _deliver(msg, sender, password, to_email)


def _send_invite_email(to_email: str, org_name: str, temp_password: str, login_url: str) -> None:
    """Send a team invite email. Includes the temporary password so the user can log in."""
    sender   = os.environ.get("MAIL_SENDER", "")
    password = os.environ.get("MAIL_APP_PASSWORD", "")
    if not sender or not password:
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"You've been invited to join {_oneline(org_name)} on berthcast"
    msg["From"]    = sender
    msg["To"]      = to_email

    text = (
        f"Hi,\n\n"
        f"You've been invited to join {org_name} on berthcast.\n\n"
        f"Sign in at: {login_url}\n\n"
        f"Email: {to_email}\n"
        f"Temporary password: {temp_password}\n\n"
        f"Please change your password after signing in (Settings → Change password).\n\n"
        f"— berthcast"
    )
    html = f"""
    <div style="font-family:'Segoe UI',sans-serif;max-width:480px;margin:0 auto;color:#1a2a3a;">
      <p style="font-size:15px;line-height:1.6;">
        You've been invited to join <strong>{_esc(org_name)}</strong> on berthcast.
      </p>
      <div style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:10px;padding:16px 20px;margin:20px 0;">
        <div style="font-size:13px;color:#6b7280;margin-bottom:4px;">Email</div>
        <div style="font-size:15px;font-weight:600;margin-bottom:12px;">{_esc(to_email)}</div>
        <div style="font-size:13px;color:#6b7280;margin-bottom:4px;">Temporary password</div>
        <div style="font-size:15px;font-weight:600;font-family:monospace;letter-spacing:0.5px;">{_esc(temp_password)}</div>
      </div>
      <a href="{login_url}"
         style="display:inline-block;margin:12px 0;padding:12px 28px;
                background:#c8924c;color:#fff;text-decoration:none;
                border-radius:8px;font-weight:600;font-size:14px;">
        Sign in to berthcast
      </a>
      <p style="font-size:13px;color:#6b7280;line-height:1.5;margin-top:16px;">
        Please change your password after signing in (Settings &rarr; Change password).
      </p>
      <p style="font-size:13px;color:#6b7280;">— berthcast</p>
    </div>
    """
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    _deliver(msg, sender, password, to_email)


def _send_contact_email(name: str, email: str, company: str, message: str) -> None:
    """Send a contact form submission to the berthcast inbox via Gmail SMTP.
    Requires MAIL_SENDER and MAIL_APP_PASSWORD env vars. Fails silently if not set."""
    sender    = os.environ.get("MAIL_SENDER", "")
    password  = os.environ.get("MAIL_APP_PASSWORD", "")
    recipient = os.environ.get("MAIL_RECIPIENT", "tanyonghan41@gmail.com")
    if not sender or not password:
        return  # Not configured — DB record is the fallback

    # Subject + Reply-To are header values: collapse any newlines so a crafted
    # name/email can't inject extra email headers.
    safe_name = _oneline(name)
    subject = f"berthcast contact: {safe_name}" + (f" ({_oneline(company)})" if company else "")
    body = (
        f"Name: {name}\n"
        f"Email: {email}\n"
        f"Company: {company or '—'}\n\n"
        f"Message:\n{message}\n\n"
        f"---\nReply directly to this email to respond to {safe_name}."
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = sender
    msg["To"]      = recipient
    msg["Reply-To"] = _oneline(email)
    msg.attach(MIMEText(body, "plain"))

    _deliver(msg, sender, password, recipient)

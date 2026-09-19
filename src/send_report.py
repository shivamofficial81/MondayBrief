"""Email the weekly report PDF through an API-key email provider (Resend-style).

This is DISABLED by default. Nothing in the project imports or calls it, the workflow's
email step is switched off, and the project runs end to end without any credentials.

    from src.send_report import send_report

    send_report("reports/2026-09-14.pdf", to="owner@example.com", brief=brief)

Credentials are never read from code or files, only from environment variables:

    RESEND_API_KEY   the provider's API key (a secret: never commit it)
    MAIL_FROM        the sender, on a domain you have verified with the provider
    MAIL_TO          recipients, comma separated (used when ``to`` isn't passed)

Try it without a key or a network connection first:

    python -m src.send_report reports/2026-09-14.pdf --to owner@example.com --dry-run

The default endpoint is Resend's (POST /emails with a Bearer key and a base64 attachment).
Any provider with the same request shape works by passing ``api_url``. See the README,
"Enable email", for the full setup and for switching on the workflow step.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests

RESEND_API_URL = "https://api.resend.com/emails"
API_KEY_ENV = "RESEND_API_KEY"
FROM_ENV = "MAIL_FROM"
TO_ENV = "MAIL_TO"
MAX_ATTACHMENT_BYTES = 30 * 1024 * 1024  # providers cap a message near 40 MB once base64-encoded
REQUEST_TIMEOUT_SECONDS = 20
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


class SendError(Exception):
    """The email couldn't be sent. The message is safe to print and never contains the API key."""


def send_report(
    pdf_path: str | Path,
    *,
    to: str | list[str] | None = None,
    subject: str | None = None,
    body: str | None = None,
    brief: dict | None = None,
    sender: str | None = None,
    api_key: str | None = None,
    api_url: str = RESEND_API_URL,
    dry_run: bool = False,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> dict:
    """Email ``pdf_path`` as an attachment and return ``{"id": ..., "to": [...]}``.

    ``to`` and ``sender`` fall back to the MAIL_TO and MAIL_FROM environment variables, and
    ``api_key`` to RESEND_API_KEY. If ``brief`` (from ``analyze_week``) is given, the subject
    and body are built from it; ``subject`` and ``body`` override either.

    With ``dry_run=True`` nothing is sent and no API key is needed: the request that
    would be made is returned (attachment shown as a size, not its content), which makes it
    safe to check recipients, subject and body first.

    Raises SendError, with a plain-English message, for anything that goes wrong.
    """
    pdf = _check_pdf(pdf_path)
    recipients = _recipients(to)
    sender = (sender or os.environ.get(FROM_ENV, "")).strip()
    if not sender:
        raise SendError(f"No sender address. Pass sender=..., or set the {FROM_ENV} environment variable.")
    _check_address(sender, "sender")

    payload = {
        "from": sender,
        "to": recipients,
        "subject": subject or _default_subject(brief),
        "text": body or _default_body(brief),
        "attachments": [
            {"filename": pdf.name, "content": base64.b64encode(pdf.read_bytes()).decode("ascii")}
        ],
    }

    if dry_run:
        preview = {**payload, "attachments": [{"filename": pdf.name, "bytes": pdf.stat().st_size}]}
        return {"dry_run": True, "url": api_url, "request": preview}

    api_key = (api_key or os.environ.get(API_KEY_ENV, "")).strip()
    if not api_key:
        raise SendError(
            f"No API key. Set the {API_KEY_ENV} environment variable (in GitHub: a repository "
            "secret). It is never read from a file and should never be committed."
        )
    _check_url(api_url)
    return _post(payload, api_key, api_url, timeout)


# --- Checks -----------------------------------------------------------------------


def _check_pdf(pdf_path: str | Path) -> Path:
    pdf = Path(pdf_path)
    if not pdf.is_file():
        raise SendError(f"Report not found: '{pdf}'. Generate it first with: python -m src.render_pdf")
    if pdf.suffix.lower() != ".pdf":
        raise SendError(f"'{pdf.name}' isn't a PDF. Only the report PDF should be attached.")
    if pdf.stat().st_size > MAX_ATTACHMENT_BYTES:
        raise SendError(f"'{pdf.name}' is too large to email ({pdf.stat().st_size / 1e6:.0f} MB).")
    return pdf


def _recipients(to: str | list[str] | None) -> list[str]:
    raw = to if to is not None else os.environ.get(TO_ENV, "")
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    recipients = [r.strip() for r in items if r and r.strip()]
    if not recipients:
        raise SendError(f"No recipients. Pass to=..., or set the {TO_ENV} environment variable.")
    for address in recipients:
        _check_address(address, "recipient")
    return recipients


def _check_address(address: str, role: str) -> None:
    # Deliberately loose (the provider does the strict check): catches typos and pasted junk.
    local, _, domain = address.rpartition("@")
    if not local or "." not in domain or any(c.isspace() for c in address):
        raise SendError(f"The {role} address '{address}' doesn't look like an email address.")


def _check_url(api_url: str) -> None:
    """Never send the API key over plain HTTP, except to this machine (for testing)."""
    parsed = urlparse(api_url)
    if parsed.scheme == "https" or (parsed.scheme == "http" and parsed.hostname in _LOOPBACK_HOSTS):
        return
    raise SendError("The email API address must start with https:// so the API key stays private.")


# --- Content ----------------------------------------------------------------------


def _default_subject(brief: dict | None) -> str:
    label = brief["week"]["label"] if brief else ""
    return f"MondayBrief: weekly sales brief, {label}" if label else "MondayBrief: weekly sales brief"


def _default_body(brief: dict | None) -> str:
    if brief and brief.get("summary"):
        return f"{brief['summary']}\n\nThe full report is attached as a PDF."
    return "Your weekly sales brief is attached as a PDF."


# --- Sending ----------------------------------------------------------------------


def _post(payload: dict, api_key: str, api_url: str, timeout: float) -> dict:
    try:
        response = requests.post(
            api_url,
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
    except requests.exceptions.Timeout:
        raise SendError(f"The email service didn't answer within {timeout:g}s. Try again in a minute.") from None
    except requests.exceptions.RequestException:
        # Not str(exc): a request error can echo the URL and headers.
        raise SendError("Couldn't reach the email service. Check your internet connection.") from None

    if response.status_code in (401, 403):
        raise SendError(
            f"The email service rejected the API key (HTTP {response.status_code}). "
            f"Check the {API_KEY_ENV} value and that the key can send from '{payload['from']}'."
        )
    if response.status_code == 429:
        raise SendError("The email service is rate limiting this key. Try again in a minute.")
    if not response.ok:
        raise SendError(f"The email service refused the message (HTTP {response.status_code}){_provider_message(response)}.")

    try:
        message_id = response.json().get("id")
    except ValueError:
        message_id = None
    return {"id": message_id, "to": payload["to"]}


def _provider_message(response: requests.Response) -> str:
    """The provider's own explanation, if it gave one (for example an unverified sender domain)."""
    try:
        message = str(response.json().get("message", "")).strip()
    except (ValueError, AttributeError):
        return ""
    return f": {message[:200]}" if message else ""


# --- Console entry point ----------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Email a weekly report PDF (disabled by default).")
    parser.add_argument("pdf", type=Path, help="the report PDF to send")
    parser.add_argument("--to", action="append", help=f"recipient (repeat, or comma separate); default: ${TO_ENV}")
    parser.add_argument("--subject", help="override the subject line")
    parser.add_argument("--dry-run", action="store_true", help="show what would be sent; needs no API key")
    args = parser.parse_args(argv)

    to = ",".join(args.to) if args.to else None
    try:
        result = send_report(args.pdf, to=to, subject=args.subject, dry_run=args.dry_run)
    except SendError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        print("Dry run. Nothing was sent. The request would be:")
        print(json.dumps(result["request"], indent=2))
    else:
        print(f"Sent to {', '.join(result['to'])} (message id: {result['id']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

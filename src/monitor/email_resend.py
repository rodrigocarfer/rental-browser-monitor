from __future__ import annotations

import os

import httpx

from monitor.models import Listing


def send_listings_resend(*, listings: list[Listing]) -> None:
    """Send using Resend HTTP API (https://resend.com/docs/api-reference/emails/send-email)."""
    api_key = os.environ["RESEND_API_KEY"].strip()
    from_addr = os.environ["RESEND_FROM"].strip()
    to_addr = os.environ["EMAIL_TO"].strip()

    lines = [f"{li.title}\n  {li.url}" for li in listings]
    body = "New listings:\n\n" + "\n\n".join(lines)
    subject = f"Rental monitor: {len(listings)} new listing(s)"

    r = httpx.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "from": from_addr,
            "to": [to_addr],
            "subject": subject,
            "text": body,
        },
        timeout=60.0,
    )
    r.raise_for_status()


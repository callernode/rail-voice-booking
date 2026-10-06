"""Send completed booking requests to the admin on WhatsApp.

Supported channels (first configured one wins, all configured ones are tried in order on failure):
  1. Meta WhatsApp Cloud API  - WA_TOKEN, WA_PHONE_NUMBER_ID, ADMIN_WHATSAPP (+ optional WA_TEMPLATE_NAME)
  2. CallMeBot (free, personal) - CALLMEBOT_APIKEY, ADMIN_WHATSAPP
  3. Any webhook (n8n / Make / Zapier / Twilio relay) - NOTIFY_WEBHOOK_URL (receives JSON)
"""
from __future__ import annotations

import logging
import os
import re
from datetime import date
from urllib.parse import quote

import requests

from agent import CLASSES, GENDERS

log = logging.getLogger("notify")

ADMIN = re.sub(r"\D", "", os.getenv("ADMIN_WHATSAPP", ""))   # country code + number, e.g. 919876543210
WA_TOKEN = os.getenv("WA_TOKEN")
WA_PHONE_ID = os.getenv("WA_PHONE_NUMBER_ID")
WA_TEMPLATE = os.getenv("WA_TEMPLATE_NAME")                   # optional approved template with one {{1}} body var
WA_TEMPLATE_LANG = os.getenv("WA_TEMPLATE_LANG", "en")
CALLMEBOT_KEY = os.getenv("CALLMEBOT_APIKEY")
WEBHOOK = os.getenv("NOTIFY_WEBHOOK_URL")
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")


def format_message(rec: dict) -> str:
    d = rec["data"]
    try:
        jd = date.fromisoformat(d["journey_date"]).strftime("%d %b %Y (%a)")
    except Exception:
        jd = d.get("journey_date")
    pax = "\n".join(
        f"  {i}. {p.get('name')}, {p.get('age')} yrs, {GENDERS.get(p.get('gender'), p.get('gender'))}"
        for i, p in enumerate(d.get("passengers", []), 1)
    )
    lines = [
        f"🚆 *New Train Booking Request* — {rec.get('ref')}",
        "",
        f"*Route:* {d.get('from_station')} → {d.get('to_station')}",
        f"*Date:* {jd}",
        f"*Class:* {d.get('travel_class')} ({CLASSES.get(d.get('travel_class'), '')})",
        f"*Quota:* {d.get('quota') or 'GENERAL'}",
        f"*Train:* {d.get('train_preference') or 'Any'}",
        f"*Berth pref:* {d.get('berth_preference') or 'No preference'}",
        f"*Passengers ({len(d.get('passengers', []))}):*",
        pax,
        f"*Mobile:* +91 {d.get('mobile')}",
    ]
    if d.get("notes"):
        lines.append(f"*Notes:* {d['notes']}")
    lines += ["", f"Language: {rec.get('lang')} · Received {rec.get('updated_at')} IST"]
    if PUBLIC_URL:
        lines.append(f"Admin: {PUBLIC_URL}/admin")
    return "\n".join(lines)


def _cloud_api(text: str) -> str:
    url = f"https://graph.facebook.com/v21.0/{WA_PHONE_ID}/messages"
    h = {"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"}
    if WA_TEMPLATE:
        flat = re.sub(r"\s*\n\s*", " | ", text.replace("*", ""))[:1000]   # template params can't hold newlines
        body = {"messaging_product": "whatsapp", "to": ADMIN, "type": "template",
                "template": {"name": WA_TEMPLATE, "language": {"code": WA_TEMPLATE_LANG},
                             "components": [{"type": "body", "parameters": [{"type": "text", "text": flat}]}]}}
    else:
        body = {"messaging_product": "whatsapp", "to": ADMIN, "type": "text",
                "text": {"preview_url": False, "body": text[:4000]}}
    r = requests.post(url, headers=h, json=body, timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"cloud api {r.status_code}: {r.text[:200]}")
    return "sent:cloud_api"


def _callmebot(text: str) -> str:
    r = requests.get(f"https://api.callmebot.com/whatsapp.php?phone={ADMIN}&text={quote(text)}&apikey={CALLMEBOT_KEY}",
                     timeout=20)
    if r.status_code >= 400 or "ERROR" in r.text.upper()[:300]:
        raise RuntimeError(f"callmebot {r.status_code}: {r.text[:200]}")
    return "sent:callmebot"


def _webhook(text: str, rec: dict) -> str:
    r = requests.post(WEBHOOK, json={"message": text, "admin_whatsapp": ADMIN, "booking": rec}, timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"webhook {r.status_code}")
    return "sent:webhook"


def send_admin(rec: dict) -> str:
    text = format_message(rec)
    attempts = []
    if WA_TOKEN and WA_PHONE_ID and ADMIN:
        attempts.append(lambda: _cloud_api(text))
    if CALLMEBOT_KEY and ADMIN:
        attempts.append(lambda: _callmebot(text))
    if WEBHOOK:
        attempts.append(lambda: _webhook(text, rec))
    if not attempts:
        log.warning("No WhatsApp channel configured; booking %s saved only", rec.get("ref"))
        return "not_configured"
    errors = []
    for fn in attempts:
        try:
            return fn()
        except Exception as e:
            log.error("notify failed: %s", e)
            errors.append(str(e))
    return "failed: " + " ; ".join(errors)


def wa_link(rec: dict) -> str:
    """Click-to-chat link the admin can use to message the customer from the dashboard."""
    mob = rec["data"].get("mobile") or ""
    return f"https://wa.me/91{mob}"

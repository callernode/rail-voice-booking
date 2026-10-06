---
title: Rail Sahayak Voice Booking
emoji: 🚆
colorFrom: red
colorTo: green
sdk: docker
app_port: 7860
pinned: false
---

# Rail Sahayak — voice train-ticket booking requests

A mobile web page with one big **Call / Book Ticket** button. A Sarvam AI voice agent asks the user (in Hindi,
Bengali, English, Odia, Marathi…) for everything needed to book a train ticket, validates it, reads a summary
back for a yes/no confirmation, saves it, and sends the complete request to the admin on WhatsApp.

```
Phone browser ──mic audio──► /api/turn ──► Sarvam STT (saaras:v4)
                                       ──► Sarvam LLM (sarvam-105b-conversations, JSON output)
                                       ──► Python validation + SQLite (+ private HF dataset backup)
                                       ──► Sarvam TTS (bulbul:v3) ──audio──► Phone browser
                       on "yes" ──► WhatsApp to admin (Cloud API / CallMeBot / n8n webhook)
```

## What the agent collects
From/to station · journey date (understands "kal", "parson", weekdays, "10 tarikh") · train preference (optional)
· class (SL/3A/2A/1A/3E/CC/EC/2S) · quota (General/Tatkal/Ladies/Senior) · number of passengers (max 6) ·
each passenger's name, age, gender · berth preference (optional) · 10-digit mobile.

Server-side checks (not just the LLM): date not in the past and within 60 days, stations differ, valid class,
mobile matches `[6-9]XXXXXXXXX`, ages 1–120, passenger count matches, senior/ladies quota eligibility.
Nothing is sent to the admin until every check passes **and** the user says yes to the read-back summary.

## Deploy on Hugging Face Spaces (about 10 minutes)
1. huggingface.co → **New Space** → SDK **Docker** → blank template. Upload all files in this folder.
2. Space **Settings → Variables and secrets** → add secrets:

| Secret | Required | What |
|---|---|---|
| `SARVAM_API_KEY` | yes | From dashboard.sarvam.ai |
| `ADMIN_KEY` | yes | Any long password for `/admin` |
| `ADMIN_WHATSAPP` | yes | Admin number with country code, e.g. `919876543210` |
| one WhatsApp channel (below) | yes | |
| `HF_TOKEN` + `HF_DATASET_REPO` | recommended | Write token + e.g. `yourname/rail-bookings`. Bookings are backed up to this **private** dataset every 5 min (Space disk is wiped on restart unless you buy persistent storage). |
| `PUBLIC_URL` | optional | `https://yourname-rail-sahayak.hf.space` — adds the admin link to WhatsApp messages |

3. Open the app at the direct `https://<owner>-<space>.hf.space` URL (best for microphone on phones) and share that link / a QR code.

### WhatsApp channel — pick one
- **Meta WhatsApp Cloud API** (official, best for production): `WA_TOKEN`, `WA_PHONE_NUMBER_ID`.
  Free-form text only reaches the admin within 24 h of the admin last messaging the business number. For
  reliable delivery create a utility template with one body variable `{{1}}`, get it approved, and set
  `WA_TEMPLATE_NAME` (and `WA_TEMPLATE_LANG`, default `en`).
- **CallMeBot** (free, personal, quickest to test): send the activation message from the admin's WhatsApp as
  described on callmebot.com, then set `CALLMEBOT_APIKEY`.
- **Webhook** (n8n / Make / Zapier / Twilio): `NOTIFY_WEBHOOK_URL` receives JSON
  `{message, admin_whatsapp, booking}` — forward it to WhatsApp however you like.

Channels are tried in that order; the result is stored per booking and shown on the dashboard.

## Admin dashboard
`/admin` → log in with `ADMIN_KEY`. Lists every call (submitted, in-progress, abandoned), full conversation,
one-tap **Booked / Cancel**, a WhatsApp link to the customer, and **CSV export**. JSON for automation:
`GET /api/admin/bookings?status=submitted` with header `X-Admin-Key`.
Abandoned/incomplete calls are kept too, so your team can call those users back.

## Tuning (optional variables)
`SARVAM_TTS_SPEAKER` (default `priya`), `SARVAM_TTS_PACE` (`0.95`; try `0.85` for elderly users),
`SARVAM_LLM_MODEL` (`sarvam-105b-conversations`), `SARVAM_REASONING` (`low`; `none` = fastest),
`SARVAM_STT_MODEL` (`saaras:v4`), `SARVAM_TTS_CODEC` (`mp3`).
Greetings/closing lines are in `agent.py` (`GREETINGS`, `DONE_MESSAGES`); the agent's behaviour is in
`system_prompt()`.

## Run locally
```
pip install -r requirements.txt
SARVAM_API_KEY=... ADMIN_KEY=... uvicorn app:app --port 7860
```
Microphone needs HTTPS or `localhost`. `tests/e2e_check.py` runs a scripted 8-turn call against fake services.

## Files
`app.py` web server & call flow · `agent.py` prompt, state merge, validation · `sarvam.py` STT/LLM/TTS client ·
`storage.py` SQLite + HF dataset backup · `notify.py` WhatsApp · `static/index.html` the phone UI.

**Privacy:** the database holds names, ages and phone numbers. Keep the HF dataset private, keep `ADMIN_KEY`
secret, and tell users the details are only used to book their ticket.

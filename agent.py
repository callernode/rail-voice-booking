"""Conversation brain: prompt, state merging and validation for the booking agent."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

CLASSES = {
    "SL": "Sleeper", "3A": "AC 3 Tier", "2A": "AC 2 Tier", "1A": "AC First Class",
    "3E": "AC 3 Economy", "CC": "AC Chair Car", "EC": "Executive Chair Car", "2S": "Second Sitting",
}
QUOTAS = {"GENERAL", "TATKAL", "LADIES", "SENIOR CITIZEN"}
GENDERS = {"M": "Male", "F": "Female", "T": "Transgender"}
MAX_PASSENGERS = 6          # IRCTC limit per ticket
ADVANCE_DAYS = 60           # advance reservation period

LANG_NAMES = {
    "hi-IN": "Hindi", "bn-IN": "Bengali", "en-IN": "English (simple Indian English)",
    "od-IN": "Odia", "mr-IN": "Marathi", "gu-IN": "Gujarati", "pa-IN": "Punjabi",
    "ta-IN": "Tamil", "te-IN": "Telugu", "kn-IN": "Kannada", "ml-IN": "Malayalam",
}

GREETINGS = {
    "hi-IN": "नमस्ते! मैं रेल टिकट सहायक हूँ। मैं आपकी टिकट बुकिंग के लिए कुछ जानकारी लूँगी। सबसे पहले बताइए, आप किस स्टेशन से किस स्टेशन तक जाना चाहते हैं?",
    "bn-IN": "নমস্কার! আমি রেল টিকিট সহায়ক। টিকিট বুকিংয়ের জন্য কিছু তথ্য নেব। প্রথমে বলুন, কোন স্টেশন থেকে কোন স্টেশনে যেতে চান?",
    "en-IN": "Namaste! I am your rail ticket assistant. I will take a few details for your booking. First, please tell me, from which station to which station do you want to travel?",
}

DONE_MESSAGES = {
    "hi-IN": "धन्यवाद! आपकी बुकिंग रिक्वेस्ट दर्ज हो गई है। आपका रिक्वेस्ट नंबर है {ref}। हमारी टीम जल्द ही {mobile} पर आपसे संपर्क करेगी। आपकी यात्रा शुभ हो!",
    "bn-IN": "ধন্যবাদ! আপনার বুকিং অনুরোধ জমা হয়েছে। আপনার অনুরোধ নম্বর {ref}। আমাদের টিম শীঘ্রই {mobile} নম্বরে যোগাযোগ করবে। শুভ যাত্রা!",
    "en-IN": "Thank you! Your booking request is registered. Your request number is {ref}. Our team will contact you soon on {mobile}. Have a safe journey!",
}

REQUIRED = ["from_station", "to_station", "journey_date", "travel_class", "passengers", "mobile"]


def empty_state() -> dict:
    return {
        "from_station": None, "to_station": None, "journey_date": None,
        "train_preference": None, "travel_class": None, "quota": "GENERAL",
        "berth_preference": None, "passengers": [], "num_passengers": None,
        "mobile": None, "notes": None,
    }


# ---------------------------------------------------------------- normalisers

def norm_mobile(v) -> str | None:
    if v is None:
        return None
    d = re.sub(r"\D", "", str(v))
    if len(d) == 12 and d.startswith("91"):
        d = d[2:]
    if len(d) == 11 and d.startswith("0"):
        d = d[1:]
    return d or None


def norm_class(v) -> str | None:
    if not v:
        return None
    s = str(v).upper().replace(" ", "").replace("-", "")
    aliases = {
        "SLEEPER": "SL", "SL": "SL", "3AC": "3A", "AC3": "3A", "3A": "3A", "THIRDAC": "3A",
        "2AC": "2A", "AC2": "2A", "2A": "2A", "1AC": "1A", "AC1": "1A", "1A": "1A",
        "FIRSTAC": "1A", "3E": "3E", "3ECONOMY": "3E", "CC": "CC", "CHAIRCAR": "CC",
        "ACCHAIRCAR": "CC", "EC": "EC", "EXECUTIVE": "EC", "2S": "2S", "SECONDSITTING": "2S",
        "GENERAL": "2S",
    }
    return aliases.get(s, s)


def norm_gender(v) -> str | None:
    if not v:
        return None
    s = str(v).strip().upper()
    if s.startswith(("M", "PURUSH", "ADMI")):
        return "M"
    if s.startswith(("F", "W", "MAHILA", "AURAT", "STREE")):
        return "F"
    if s.startswith(("T", "O")):
        return "T"
    return s[:1]


def to_int(v):
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return None


def merge(state: dict, updates: dict) -> dict:
    """Merge LLM-proposed updates into the session state (in place) and return it."""
    if not isinstance(updates, dict):
        return state
    for key in ("from_station", "to_station", "train_preference", "berth_preference", "notes"):
        if updates.get(key):
            state[key] = str(updates[key]).strip()[:80]
    if updates.get("journey_date"):
        state["journey_date"] = str(updates["journey_date"]).strip()[:10]
    if updates.get("travel_class"):
        state["travel_class"] = norm_class(updates["travel_class"])
    if updates.get("quota"):
        q = str(updates["quota"]).upper().replace("_", " ")
        state["quota"] = "SENIOR CITIZEN" if q.startswith("SENIOR") else q
    if updates.get("mobile"):
        state["mobile"] = norm_mobile(updates["mobile"])
    if updates.get("num_passengers") is not None:
        state["num_passengers"] = to_int(updates["num_passengers"])
    if isinstance(updates.get("passengers"), list):
        clean = []
        for p in updates["passengers"][:MAX_PASSENGERS + 2]:
            if not isinstance(p, dict):
                continue
            clean.append({
                "name": (str(p.get("name") or "").strip()[:60] or None),
                "age": to_int(p.get("age")),
                "gender": norm_gender(p.get("gender")),
            })
        if clean:
            state["passengers"] = clean
    return state


# ---------------------------------------------------------------- validation

def validate(state: dict, today: date | None = None) -> tuple[list[str], list[str]]:
    """Return (missing_fields, problems). Both empty means the booking is complete."""
    today = today or datetime.now(IST).date()
    missing, problems = [], []

    for f in ("from_station", "to_station", "journey_date", "travel_class", "mobile"):
        if not state.get(f):
            missing.append(f)

    fs, ts = (state.get("from_station") or "").lower(), (state.get("to_station") or "").lower()
    if fs and ts and fs == ts:
        problems.append("from_station and to_station are the same")

    jd = state.get("journey_date")
    if jd:
        try:
            d = date.fromisoformat(jd)
            if d < today:
                problems.append(f"journey_date {jd} is in the past")
            elif d > today + timedelta(days=ADVANCE_DAYS):
                problems.append(f"journey_date {jd} is more than {ADVANCE_DAYS} days ahead; booking not open yet")
        except ValueError:
            problems.append(f"journey_date '{jd}' is not a valid date")

    tc = state.get("travel_class")
    if tc and tc not in CLASSES:
        problems.append(f"travel_class '{tc}' is unknown; must be one of {', '.join(CLASSES)}")

    if state.get("quota") and state["quota"] not in QUOTAS:
        problems.append(f"quota '{state['quota']}' is unknown")

    mob = state.get("mobile")
    if mob and not re.fullmatch(r"[6-9]\d{9}", mob):
        problems.append(f"mobile '{mob}' is not a valid 10-digit Indian mobile number")

    pax = state.get("passengers") or []
    n = state.get("num_passengers")
    if not pax:
        missing.append("passengers")
        if not n:
            missing.append("num_passengers")
    if n is not None and not (1 <= n <= MAX_PASSENGERS):
        problems.append(f"num_passengers must be between 1 and {MAX_PASSENGERS}")
    if len(pax) > MAX_PASSENGERS:
        problems.append(f"maximum {MAX_PASSENGERS} passengers per ticket")
    if n and pax and len(pax) < n:
        missing.append(f"details of passenger {len(pax) + 1} of {n}")
    for i, p in enumerate(pax, 1):
        if not p.get("name"):
            missing.append(f"passenger {i} name")
        elif len(p["name"]) < 2:
            problems.append(f"passenger {i} name looks too short")
        if p.get("age") is None:
            missing.append(f"passenger {i} age")
        elif not (1 <= p["age"] <= 120):
            problems.append(f"passenger {i} age {p['age']} is not realistic")
        if not p.get("gender"):
            missing.append(f"passenger {i} gender")
        elif p["gender"] not in GENDERS:
            problems.append(f"passenger {i} gender must be male, female or transgender")
    if state.get("quota") == "SENIOR CITIZEN":
        for i, p in enumerate(pax, 1):
            if p.get("age") and ((p.get("gender") == "F" and p["age"] < 45) or (p.get("gender") != "F" and p["age"] < 60)):
                problems.append(f"passenger {i} is not eligible for senior citizen quota")
    if state.get("quota") == "LADIES" and any(p.get("gender") == "M" and (p.get("age") or 0) > 12 for p in pax):
        problems.append("ladies quota cannot include adult male passengers")
    return missing, problems


def is_complete(state: dict) -> bool:
    m, p = validate(state)
    return not m and not p


# ---------------------------------------------------------------- prompting

SCHEMA_HINT = """{
  "reply": "<what you will SAY next, in the user's language>",
  "updates": {
    "from_station": "...", "to_station": "...",
    "journey_date": "YYYY-MM-DD",
    "train_preference": "train name/number, or 'any'",
    "travel_class": "SL|3A|2A|1A|3E|CC|EC|2S",
    "quota": "GENERAL|TATKAL|LADIES|SENIOR CITIZEN",
    "berth_preference": "lower/middle/upper/side lower/side upper/no preference",
    "num_passengers": 2,
    "passengers": [{"name": "...", "age": 45, "gender": "M|F|T"}],
    "mobile": "10 digits"
  },
  "user_confirmed": false,
  "user_wants_to_stop": false
}"""


def system_prompt(lang: str, state: dict, ready_for_confirmation: bool) -> str:
    now = datetime.now(IST)
    missing, problems = validate(state)
    lang_name = LANG_NAMES.get(lang, "Hindi")
    upcoming = ", ".join(
        f"{(now.date() + timedelta(days=i)).strftime('%a')}={(now.date() + timedelta(days=i)).isoformat()}"
        for i in range(0, 8)
    )
    status = (
        "ALL DETAILS ARE COMPLETE AND VALID. Read back a short summary of the booking (route, date, class, "
        "train preference, each passenger's name/age/gender, mobile number digit by digit) and ask the user "
        "to confirm with yes/no. If the user already heard the summary and now clearly says yes / haan / "
        "theek hai / sahi hai, set user_confirmed=true."
        if ready_for_confirmation or (not missing and not problems)
        else f"STILL MISSING: {missing or 'nothing'}\nPROBLEMS TO FIX (politely ask again): {problems or 'none'}"
    )
    return f"""You are "Rail Sahayak", a warm, patient female voice assistant on a phone-like call, collecting
train ticket booking details from village and non-technical users in India. A human agent will book the
ticket later; you ONLY collect and confirm details. Never promise seat availability or fares.

LANGUAGE: Speak ONLY in {lang_name}, using simple everyday words (Hindi users: simple Hindustani, it's fine to
use common English words like ticket, AC, sleeper, train). Use respectful forms (aap / apni).

STYLE (this is spoken aloud by text-to-speech):
- Keep each reply to 1-2 short sentences. Ask for ONE or at most TWO things at a time.
- No emojis, no lists, no markdown, no brackets. Write numbers as the user would say them.
- If the user's words are unclear or you are unsure (station name, age, number), repeat back what you heard
  and ask them to confirm. If the user is silent or the transcript is empty, gently ask again.
- If the user asks something unrelated, answer in one line and come back to the booking.

DETAILS TO COLLECT (in a natural order):
1. From station and to station (keep the station/city name as the user said it, in English letters, e.g. "Patna Jn").
2. Journey date. Today is {now.strftime('%A %d %B %Y')} (IST). Upcoming days: {upcoming}.
   Convert "kal"=tomorrow, "parson"=day after tomorrow, weekday names, "10 tarikh" to YYYY-MM-DD.
   Bookings open only up to {ADVANCE_DAYS} days ahead.
3. Train preference (train name or number) - optional, "any" is fine.
4. Class: Sleeper (SL), AC 3 tier (3A), AC 2 tier (2A), AC first (1A), 3 economy (3E), Chair car (CC),
   Executive (EC), Second sitting / general reservation (2S). If they don't know, suggest Sleeper or 3AC.
5. Quota: normally GENERAL; ask only if they mention Tatkal, ladies or senior citizen.
6. Number of passengers (max {MAX_PASSENGERS}), then for EACH passenger: full name, age, gender.
7. Berth preference - optional, ask once briefly.
8. Contact mobile number (10 digits). Read it back digit by digit to confirm.

CURRENT COLLECTED DATA (JSON): {json.dumps(state, ensure_ascii=False)}
{status}

OUTPUT: Reply with ONLY one JSON object, no other text:
{SCHEMA_HINT}
Rules for "updates": include ONLY fields the user just provided or corrected (omit others). When you change
passengers, always send the FULL passengers list. Never invent data the user did not say.
Set user_wants_to_stop=true only if the user clearly wants to end the call / cancel."""


def parse_llm_json(text: str) -> dict:
    """Extract the first JSON object from model output, tolerating code fences and chatter."""
    if not text:
        return {}
    t = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    start = t.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(t)):
            if t[i] == "{":
                depth += 1
            elif t[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(t[start:i + 1])
                    except json.JSONDecodeError:
                        break
        start = t.find("{", start + 1)
    return {"reply": t[:300]}


def spoken_mobile(m: str) -> str:
    return " ".join(m) if m else ""

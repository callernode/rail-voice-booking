"""End-to-end check against a running app (APP_URL) wired to fake Sarvam/WhatsApp services (FAKE_URL)."""
import os
import sys
from datetime import date, timedelta

import requests

APP = os.getenv("APP_URL", "http://127.0.0.1:7861")
FAKE = os.getenv("FAKE_URL", "http://127.0.0.1:7862")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agent  # noqa: E402

# ---- unit checks on validation
today = date.today()
s = agent.empty_state()
agent.merge(s, {"from_station": "Howrah", "to_station": "Howrah", "journey_date": (today - timedelta(days=1)).isoformat(),
                "travel_class": "sleeper", "mobile": "12345", "num_passengers": 9,
                "passengers": [{"name": "A", "age": 200, "gender": "male"}]})
m, p = agent.validate(s, today)
assert s["travel_class"] == "SL", s
assert any("same" in x for x in p) and any("past" in x for x in p) and any("mobile" in x for x in p), p
assert any("between 1 and 6" in x for x in p) and any("age 200" in x for x in p) and any("too short" in x for x in p), p
s2 = agent.empty_state()
agent.merge(s2, {"journey_date": (today + timedelta(days=90)).isoformat()})
assert any("60 days" in x for x in agent.validate(s2, today)[1])
assert agent.norm_mobile("+91 98765-43210") == "9876543210" and agent.norm_mobile("09876543210") == "9876543210"
assert agent.parse_llm_json('sure! {"reply": "hi", "updates": {}} thanks')["reply"] == "hi"
assert agent.parse_llm_json("<think>x</think>```json\n{\"reply\":\"ok\"}\n```")["reply"] == "ok"
print("unit checks passed")


def say(sid, text, as_audio=True):
    if as_audio:  # fake STT echoes the uploaded bytes back as the transcript
        payload = (text + " " * 2000).encode()
        r = requests.post(f"{APP}/api/turn", data={"session_id": sid}, files={"audio": ("speech.webm", payload, "audio/webm")})
    else:
        r = requests.post(f"{APP}/api/turn", data={"session_id": sid, "text": text})
    r.raise_for_status()
    j = r.json()
    print(f"  user: {text!r:32} -> agent: {j['reply'][:70]}  | done={j['done']}")
    return j


r = requests.post(f"{APP}/api/start", json={"lang": "hi-IN"}).json()
sid = r["session_id"]
assert r["audio"] and r["mime"] == "audio/mpeg" and "नमस्ते" in r["reply"]
print("call started:", r["reply"][:60])

say(sid, "Patna se Delhi jana hai")
say(sid, "kal")
say(sid, "3AC koi bhi train")
j = say(sid, "do log")
assert "passengers" in j["state"]["missing"] or any("passenger" in x for x in j["state"]["missing"]), j["state"]
say(sid, "Ramesh Kumar 45 purush, Sita Devi 40 mahila")
j = say(sid, "galat number 12345", as_audio=False)       # invalid mobile must be flagged
assert any("mobile" in x for x in j["state"]["problems"]), j["state"]
j = say(sid, "mera number 98765 43210")
assert j["state"]["complete"] and "सही" in j["reply"], j   # summary read back
assert j["state"]["data"]["travel_class"] == "3A" and j["state"]["data"]["passengers"][0]["gender"] == "M"
j = say(sid, "haan")
assert j["done"] and j["ref"] and j["ref"].startswith("RB"), j
print("booking ref:", j["ref"])

log = requests.get(f"{FAKE}/log").json()
hook = log["webhook"][-1]
assert hook["booking"]["ref"] == j["ref"] and "Ramesh Kumar" in hook["message"] and "+91 9876543210" in hook["message"]
assert log["stt"][0]["lang"] == "hi-IN" and log["stt"][0]["model"] == "saaras:v4" and "Tatkal" in log["stt"][0]["keyterms"]
assert log["tts"][0]["model"] == "bulbul:v3" and log["tts"][0]["output_audio_codec"] == "mp3"
print("\nWhatsApp message sent to admin:\n" + hook["message"])

# confirmed call cannot continue
assert requests.post(f"{APP}/api/turn", data={"session_id": sid, "text": "hello"}).status_code == 409

# admin dashboard
assert "Admin" in requests.get(f"{APP}/admin").text or requests.get(f"{APP}/admin").url.endswith("/admin/login")
sess = requests.Session()
assert sess.post(f"{APP}/admin/login", data={"key": "wrong"}).status_code == 401
r = sess.post(f"{APP}/admin/login", data={"key": "test-admin-key"}, allow_redirects=False)
assert r.status_code == 303
sess.cookies.set("admin_key", "test-admin-key")
page = sess.get(f"{APP}/admin").text
assert j["ref"] in page and "Ramesh Kumar" in page
csv = sess.get(f"{APP}/admin/export.csv").text
assert j["ref"] in csv and "Ramesh Kumar/45/M" in csv
rows = sess.get(f"{APP}/api/admin/bookings?status=submitted").json()
assert rows[0]["whatsapp_status"] == "sent:webhook", rows[0]
print("\nadmin dashboard, CSV export and API OK")
print("ALL CHECKS PASSED")

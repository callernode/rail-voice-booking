"""Rail Sahayak - voice-first train ticket booking request collector.

Run locally:  uvicorn app:app --port 7860
On Hugging Face Spaces this runs from the Dockerfile.
"""
from __future__ import annotations

import csv
import hmac
import html
import io
import json
import logging
import os
import secrets
import threading
import time
import uuid

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import agent
import notify
import sarvam
import storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

ADMIN_KEY = os.getenv("ADMIN_KEY") or ""
SESSION_TTL = 45 * 60
MAX_TURNS = 60
MAX_AUDIO_BYTES = 3 * 1024 * 1024
HERE = os.path.dirname(os.path.abspath(__file__))

SORRY = {
    "hi-IN": "माफ़ कीजिए, मुझे ठीक से सुनाई नहीं दिया। क्या आप दोबारा बोल सकते हैं?",
    "bn-IN": "দুঃখিত, ঠিক শুনতে পাইনি। আবার বলবেন?",
    "en-IN": "Sorry, I could not hear that properly. Could you please say it again?",
}
BYE = {
    "hi-IN": "ठीक है, कॉल समाप्त कर रही हूँ। जब चाहें फिर से कॉल करें। धन्यवाद!",
    "bn-IN": "ঠিক আছে, কল শেষ করছি। যখন খুশি আবার কল করুন। ধন্যবাদ!",
    "en-IN": "Okay, ending the call. Please call again anytime. Thank you!",
}

# ---------------------------------------------------------------- sessions (in memory)
_sessions: dict[str, dict] = {}
_slock = threading.Lock()


def _gc():
    cutoff = time.time() - SESSION_TTL
    with _slock:
        for sid in [s for s, v in _sessions.items() if v["ts"] < cutoff]:
            v = _sessions.pop(sid)
            if not v["done"]:
                storage.upsert_draft(sid, v["lang"], v["state"], v["transcript"], status="abandoned")


def _get(sid: str) -> dict | None:
    with _slock:
        s = _sessions.get(sid)
        if s:
            s["ts"] = time.time()
        return s


def _pick(d: dict, lang: str) -> str:
    return d.get(lang) or d["hi-IN" if lang != "en-IN" else "en-IN"]


async def _voice(text: str, lang: str) -> tuple[str | None, str | None]:
    try:
        return await run_in_threadpool(sarvam.speak, text, lang)
    except Exception as e:
        log.warning("TTS failed: %s", e)
        return None, None


def _public_state(s: dict) -> dict:
    missing, problems = agent.validate(s["state"])
    return {"data": s["state"], "missing": missing, "problems": problems,
            "complete": not missing and not problems}


# ---------------------------------------------------------------- user API
async def index(request: Request):
    return FileResponse(os.path.join(HERE, "static", "index.html"))


async def health(request: Request):
    return JSONResponse({"ok": True, "sarvam_key": bool(sarvam.KEY), "sessions": len(_sessions)})


async def start(request: Request):
    _gc()
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    lang = body.get("lang") if body.get("lang") in agent.LANG_NAMES else "hi-IN"
    sid = uuid.uuid4().hex
    greeting = _pick(agent.GREETINGS, lang)
    if lang not in agent.GREETINGS:   # translate greeting via the LLM for other languages
        try:
            raw = await run_in_threadpool(sarvam.chat, [
                {"role": "system", "content": f"Translate to simple spoken {agent.LANG_NAMES[lang]}. "
                                              'Return JSON {"reply": "..."}'},
                {"role": "user", "content": agent.GREETINGS["en-IN"]}])
            greeting = agent.parse_llm_json(raw).get("reply") or greeting
        except Exception as e:
            log.warning("greeting translation failed: %s", e)
    s = {"lang": lang, "state": agent.empty_state(), "history": [{"role": "assistant",
         "content": json.dumps({"reply": greeting}, ensure_ascii=False)}],
         "transcript": [{"who": "agent", "text": greeting, "t": storage.now()}],
         "ready": False, "done": False, "turns": 0, "ts": time.time(), "ref": None}
    with _slock:
        _sessions[sid] = s
    audio, mime = await _voice(greeting, lang)
    return JSONResponse({"session_id": sid, "reply": greeting, "audio": audio, "mime": mime,
                         "state": _public_state(s), "done": False})


async def _llm_turn(s: dict, user_text: str, extra_instruction: str | None = None) -> dict:
    msgs = [{"role": "system", "content": agent.system_prompt(s["lang"], s["state"], s["ready"])}]
    msgs += s["history"][-16:]
    msgs.append({"role": "user", "content": user_text})
    if extra_instruction:
        msgs.append({"role": "system", "content": extra_instruction})
    raw = await run_in_threadpool(sarvam.chat, msgs)
    return agent.parse_llm_json(raw)


async def turn(request: Request):
    form = await request.form()
    sid = form.get("session_id") or ""
    s = _get(sid)
    if not s:
        return JSONResponse({"error": "session_expired"}, status_code=404)
    if s["done"]:
        return JSONResponse({"error": "call_finished", "ref": s["ref"]}, status_code=409)
    s["turns"] += 1
    if s["turns"] > MAX_TURNS:
        return JSONResponse({"error": "too_many_turns"}, status_code=429)
    lang = s["lang"]

    # 1. what did the user say?
    user_text = (form.get("text") or "").strip()[:500]
    upload = form.get("audio")
    if not user_text and upload is not None and hasattr(upload, "read"):
        audio = await upload.read()
        if len(audio) > MAX_AUDIO_BYTES:
            return JSONResponse({"error": "audio_too_long"}, status_code=413)
        if len(audio) > 1500:
            try:
                user_text = await run_in_threadpool(sarvam.transcribe, audio, upload.filename or "speech.webm",
                                                    upload.content_type, lang)
            except Exception as e:
                log.warning("STT failed: %s", e)
                reply = _pick(SORRY, lang)
                a, m = await _voice(reply, lang)
                return JSONResponse({"transcript": "", "reply": reply, "audio": a, "mime": m,
                                     "state": _public_state(s), "done": False})
    heard = user_text
    if not user_text:
        user_text = "(no clear speech was heard - the user was silent or there was only noise)"
    s["transcript"].append({"who": "user", "text": heard or "(silence)", "t": storage.now()})

    # 2. ask the LLM what to say / what was provided
    try:
        out = await _llm_turn(s, user_text)
    except Exception as e:
        log.error("LLM failed: %s", e)
        reply = _pick(SORRY, lang)
        a, m = await _voice(reply, lang)
        return JSONResponse({"transcript": heard, "reply": reply, "audio": a, "mime": m,
                             "state": _public_state(s), "done": False})

    before = json.dumps(s["state"], sort_keys=True)
    agent.merge(s["state"], out.get("updates") or {})
    changed = json.dumps(s["state"], sort_keys=True) != before
    complete = agent.is_complete(s["state"])
    reply = (out.get("reply") or "").strip() or _pick(SORRY, lang)
    done, ref, status = False, None, "in_progress"

    if out.get("user_wants_to_stop"):
        done, status = True, "abandoned"
        reply = (out.get("reply") or "").strip() or _pick(BYE, lang)
    elif out.get("user_confirmed") and s["ready"] and complete and not changed:
        rec = await run_in_threadpool(storage.submit, sid, lang, s["state"], s["transcript"])
        ref = rec["ref"]
        reply = _pick(agent.DONE_MESSAGES, lang).format(ref=" ".join(ref), mobile=agent.spoken_mobile(s["state"]["mobile"]))
        if lang not in agent.DONE_MESSAGES:
            reply = agent.DONE_MESSAGES["en-IN"].format(ref=" ".join(ref), mobile=agent.spoken_mobile(s["state"]["mobile"]))
        wa = await run_in_threadpool(notify.send_admin, rec)
        storage.set_whatsapp_status(rec["id"], wa)
        done, status = True, "submitted"
    elif complete and (changed or not s["ready"]):
        # Details just became complete (or were corrected): make sure the agent reads back a summary.
        try:
            out2 = await _llm_turn(s, user_text, "All details are now complete. In your reply, read back the full "
                                   "summary in the user's language and ask them to confirm with yes or no. "
                                   "Do not set user_confirmed.")
            reply = (out2.get("reply") or reply).strip()
        except Exception as e:
            log.warning("summary call failed: %s", e)
        s["ready"] = True
    else:
        s["ready"] = complete and s["ready"]

    s["history"].append({"role": "user", "content": user_text})
    s["history"].append({"role": "assistant", "content": json.dumps({"reply": reply}, ensure_ascii=False)})
    s["transcript"].append({"who": "agent", "text": reply, "t": storage.now()})
    s["done"], s["ref"] = done, ref
    if status != "submitted":
        await run_in_threadpool(storage.upsert_draft, sid, lang, s["state"], s["transcript"], status)

    a, m = await _voice(reply, lang)
    return JSONResponse({"transcript": heard, "reply": reply, "audio": a, "mime": m,
                         "state": _public_state(s), "done": done, "ref": ref})


async def end(request: Request):
    body = await request.json()
    s = _get(body.get("session_id") or "")
    if s and not s["done"]:
        s["done"] = True
        if s["turns"]:
            await run_in_threadpool(storage.upsert_draft, body["session_id"], s["lang"], s["state"],
                                    s["transcript"], "abandoned")
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------- admin
def _is_admin(request: Request) -> bool:
    if not ADMIN_KEY:
        return False
    supplied = request.cookies.get("admin_key") or request.headers.get("x-admin-key") or ""
    return hmac.compare_digest(supplied, ADMIN_KEY)


LOGIN_HTML = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Admin login</title><body style="font-family:system-ui;max-width:360px;margin:15vh auto;padding:16px">
<h2>Rail Sahayak admin</h2>{msg}<form method=post action="/admin/login">
<input name=key type=password placeholder="Admin key" style="width:100%;padding:12px;font-size:16px" autofocus>
<button style="margin-top:12px;width:100%;padding:12px;font-size:16px">Login</button></form></body>"""


async def admin_login(request: Request):
    if request.method == "GET":
        msg = "" if ADMIN_KEY else "<p style=color:#b00>Set the ADMIN_KEY secret to enable the dashboard.</p>"
        return HTMLResponse(LOGIN_HTML.format(msg=msg))
    form = await request.form()
    if ADMIN_KEY and hmac.compare_digest(str(form.get("key") or ""), ADMIN_KEY):
        r = RedirectResponse("/admin", status_code=303)
        r.set_cookie("admin_key", ADMIN_KEY, httponly=True, secure=True, samesite="lax", max_age=30 * 86400)
        return r
    return HTMLResponse(LOGIN_HTML.format(msg="<p style=color:#b00>Wrong key</p>"), status_code=401)


def _e(v) -> str:
    return html.escape("" if v is None else str(v))


async def admin(request: Request):
    if not _is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    status = request.query_params.get("status") or None
    rows = await run_in_threadpool(storage.list_bookings, status)
    trs = []
    for b in rows:
        d = b["data"]
        pax = "<br>".join(f"{_e(p.get('name'))}, {_e(p.get('age'))}, {_e(p.get('gender'))}" for p in d.get("passengers", []))
        conv = "".join(f"<div class={_e(t['who'])}><b>{_e(t['who'])}:</b> {_e(t['text'])}</div>" for t in b["transcript"])
        actions = "".join(
            f"<button name=status value={st}>{label}</button>"
            for st, label in (("booked", "✅ Booked"), ("cancelled", "✖ Cancel"), ("submitted", "↺ Reopen")))
        wa = f"<a href='{_e(notify.wa_link(b))}' target=_blank>WhatsApp</a>" if d.get("mobile") else ""
        trs.append(f"""<tr class=s-{_e(b['status'])}>
<td><b>{_e(b['ref'] or '#' + str(b['id']))}</b><br><span class=pill>{_e(b['status'])}</span><br><small>{_e(b['created_at'])}</small></td>
<td>{_e(d.get('from_station'))} → {_e(d.get('to_station'))}<br><b>{_e(d.get('journey_date'))}</b> · {_e(d.get('travel_class'))} · {_e(d.get('quota'))}<br><small>Train: {_e(d.get('train_preference') or 'Any')} · Berth: {_e(d.get('berth_preference') or '-')}</small></td>
<td>{pax}</td>
<td>{_e(d.get('mobile'))}<br>{wa}<br><small>WA: {_e(b['whatsapp_status'] or '-')}</small></td>
<td><form method=post action=/admin/status><input type=hidden name=id value={b['id']}>{actions}</form>
<details><summary>Conversation</summary>{conv}</details></td></tr>""")
    filters = " ".join(f"<a href='/admin{'?status=' + s if s else ''}'>{s or 'all'}</a>"
                       for s in ("", "submitted", "in_progress", "booked", "cancelled", "abandoned"))
    page = f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Bookings · Rail Sahayak</title><style>
body{{font-family:system-ui,sans-serif;margin:0;padding:16px;background:#f6f5f1;color:#1d1d1b}}
h1{{font-size:20px}} nav a{{margin-right:10px}} table{{border-collapse:collapse;width:100%;background:#fff}}
td{{border-bottom:1px solid #e5e2da;padding:10px;vertical-align:top;font-size:14px}}
.pill{{background:#eee;border-radius:10px;padding:1px 8px;font-size:12px}} .s-submitted .pill{{background:#ffe08a}}
.s-booked .pill{{background:#b9efc4}} .s-cancelled .pill,.s-abandoned .pill{{background:#f3c4c4}}
button{{margin:2px;padding:6px 8px}} details div{{font-size:13px;margin:3px 0}} .agent{{color:#555}}
@media(max-width:700px){{table,tr,td{{display:block}} tr{{margin-bottom:12px;border:1px solid #ddd}}}}
</style><h1>🚆 Booking requests</h1><nav>{filters} · <a href=/admin/export.csv>Export CSV</a></nav><br>
<table>{''.join(trs) or '<tr><td>No bookings yet.</td></tr>'}</table>"""
    return HTMLResponse(page)


async def admin_status(request: Request):
    if not _is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    form = await request.form()
    st = form.get("status")
    if st in ("booked", "cancelled", "submitted"):
        await run_in_threadpool(storage.set_status, int(form.get("id")), st, form.get("note"))
    return RedirectResponse(request.headers.get("referer") or "/admin", status_code=303)


async def admin_export(request: Request):
    if not _is_admin(request):
        return Response("forbidden", status_code=403)
    rows = await run_in_threadpool(storage.list_bookings, None, 5000)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "ref", "status", "created_at", "from", "to", "date", "class", "quota", "train", "berth",
                "passengers", "mobile", "lang", "whatsapp_status"])
    for b in rows:
        d = b["data"]
        w.writerow([b["id"], b["ref"], b["status"], b["created_at"], d.get("from_station"), d.get("to_station"),
                    d.get("journey_date"), d.get("travel_class"), d.get("quota"), d.get("train_preference"),
                    d.get("berth_preference"),
                    "; ".join(f"{p.get('name')}/{p.get('age')}/{p.get('gender')}" for p in d.get("passengers", [])),
                    d.get("mobile"), b["lang"], b["whatsapp_status"]])
    return Response("﻿" + buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=bookings.csv"})


async def admin_api(request: Request):
    if not _is_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    rows = await run_in_threadpool(storage.list_bookings, request.query_params.get("status"))
    return JSONResponse(rows)


app = Starlette(routes=[
    Route("/", index),
    Route("/health", health),
    Route("/api/start", start, methods=["POST"]),
    Route("/api/turn", turn, methods=["POST"]),
    Route("/api/end", end, methods=["POST"]),
    Route("/admin", admin),
    Route("/admin/login", admin_login, methods=["GET", "POST"]),
    Route("/admin/status", admin_status, methods=["POST"]),
    Route("/admin/export.csv", admin_export),
    Route("/api/admin/bookings", admin_api),
    Mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static"),
])

if not ADMIN_KEY:
    log.warning("ADMIN_KEY not set - admin dashboard disabled. Suggested key: %s", secrets.token_urlsafe(12))

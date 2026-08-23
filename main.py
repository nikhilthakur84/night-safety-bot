"""
Night Safety WhatsApp Bot - Prototype
--------------------------------------
A check-in/alert bot built on Twilio's WhatsApp Sandbox.

Flow:
1. User texts "START" -> bot asks for emergency contacts (first time only)
2. Bot asks trip duration -> starts a timer
3. User texts "SAFE" before timer ends -> trip closed, no alert
4. If timer expires with no "SAFE" -> bot messages emergency contacts
5. User can text "SOS" anytime -> immediate alert, skips timer

Run locally, expose with ngrok, point Twilio Sandbox webhook to
<ngrok-url>/whatsapp
"""
import json
import os
import sqlite3
from datetime import datetime, timedelta

from flask import Flask, request
from twilio.rest import Client
import requests
# ---------- CONFIG ----------
# Set these as environment variables before running:
#   export TWILIO_ACCOUNT_SID=xxxx
#   export TWILIO_AUTH_TOKEN=xxxx
#   export TWILIO_WHATSAPP_NUMBER=whatsapp:+14155238886   (Twilio sandbox number)
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.environ.get("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
DB_PATH = os.path.join(os.path.dirname(__file__), "safety_bot.db")
DEFAULT_TRIP_MINUTES = 30

app = Flask(__name__)

client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


# ---------- DATABASE ----------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        phone TEXT PRIMARY KEY,
        emergency_contacts TEXT,
        trip_active INTEGER DEFAULT 0,
        trip_started_at TEXT,
        trip_expires_at TEXT,
        awaiting_contacts INTEGER DEFAULT 0,
        awaiting_duration INTEGER DEFAULT 0
    )
""")
    conn.commit()
    conn.close()


def get_user(phone):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE phone = ?", (phone,))
    row = c.fetchone()
    conn.close()
    return row


def upsert_user(phone, **fields):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT phone FROM users WHERE phone = ?", (phone,))
    exists = c.fetchone()
    if exists:
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        c.execute(f"UPDATE users SET {set_clause} WHERE phone = ?", (*fields.values(), phone))
    else:
        columns = ["phone"] + list(fields.keys())
        placeholders = ", ".join(["?"] * len(columns))
        c.execute(f"INSERT INTO users ({', '.join(columns)}) VALUES ({placeholders})",
                  (phone, *fields.values()))
    conn.commit()
    conn.close()


# ---------- MESSAGING ----------
def send_whatsapp(to_number, body):
    """to_number should be in format 'whatsapp:+91XXXXXXXXXX'"""
    if not client:
        print(f"[DRY RUN - no Twilio creds set] Would send to {to_number}: {body}")
        return
    client.messages.create(from_=TWILIO_WHATSAPP_NUMBER, to=to_number, body=body)
def send_telegram(chat_id, text):
    """chat_id is the Telegram chat id (numeric string)"""
    if not TELEGRAM_BOT_TOKEN:
        print(f"[DRY RUN - no Telegram token set] Would send to {chat_id}: {text}")
        return
    requests.post(f"{TELEGRAM_API_URL}/sendMessage", json={"chat_id": chat_id, "text": text})

def trigger_alert(phone):
    """Called when timer expires without a SAFE reply. Returns True if at least one contact was successfully alerted."""
    user = get_user(phone)
    if not user or not user[2]:  # trip_active check
        return False  # user already checked in safe, do nothing

    contacts = user[1].split(",") if user[1] else []
    started_at = user[3]
    alert_msg = (
        f"⚠️ Safety Alert: {phone.replace('whatsapp:', '')} started a trip at "
        f"{started_at} and has not checked in as SAFE. Please check on them."
    )
    sent_ok = False
    for contact in contacts:
        contact = contact.strip()
        if contact:
            try:
                send_whatsapp(f"whatsapp:{contact}", alert_msg)
                sent_ok = True
            except Exception as e:
                print(f"[ERROR] Failed to alert {contact}: {e}")

    # mark trip as closed (alert already sent, avoid duplicate alerts)
    upsert_user(phone, trip_active=0)
    return sent_ok

# ---------- WEBHOOK ----------
@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    incoming_msg = request.values.get("Body", "").strip()
    from_number = request.values.get("From", "")  # e.g. 'whatsapp:+91XXXXXXXXXX'

    user = get_user(from_number)
    reply = ""

    msg_upper = incoming_msg.upper()
    if msg_upper in ("EDIT", "EDIT CONTACTS", "RESET CONTACTS"):
        upsert_user(from_number, awaiting_contacts=1)
        reply = (
            "Let's update your safety contacts.\n"
            "Reply with 1-3 phone numbers (with country code) separated by "
            "commas, e.g. +919812345678, +919898765432"
        )

    # ---- SOS: works anytime, skips everything else ----
    if msg_upper == "SOS":
        if user and user[1]:
            upsert_user(from_number, trip_active=1, trip_started_at=str(datetime.now()))
            alerted = trigger_alert(from_number)
            if alerted:
                reply = "🚨 SOS triggered. Your emergency contacts have been alerted immediately."
            else:
                reply = "🚨 SOS triggered, but we couldn't reach your emergency contacts right now. Please contact them directly if you can."
        else:
            reply = "You haven't set emergency contacts yet. Reply START to set up first."
    # ---- User is mid-setup: awaiting emergency contacts ----
    elif user and user[5]:  # awaiting_contacts
        contacts = incoming_msg
        upsert_user(from_number, emergency_contacts=contacts, awaiting_contacts=0, awaiting_duration=1)
        reply = (
            "Got it, saved your emergency contacts.\n"
            "How many minutes is your trip? (reply with just a number, "
            f"or reply SKIP for default {DEFAULT_TRIP_MINUTES} min)"
        )

    # ---- User is mid-setup: awaiting trip duration ----
    elif user and user[6]:  # awaiting_duration
        try:
            minutes = int(incoming_msg) if msg_upper != "SKIP" else DEFAULT_TRIP_MINUTES
        except ValueError:
            minutes = DEFAULT_TRIP_MINUTES

        expires_at = datetime.now() + timedelta(minutes=minutes)
        upsert_user(
            from_number,
            trip_active=1,
            trip_started_at=str(datetime.now()),
            trip_expires_at=str(expires_at),
            awaiting_duration=0,
        )
        reply = f"✅ Tracking started for {minutes} minutes. Reply SAFE when you arrive."
    # ---- START: begin a new trip ----
    elif msg_upper == "START":
        if user and user[1]:  # contacts already saved
            upsert_user(from_number, awaiting_duration=1)
            reply = (
                f"How many minutes is your trip? (reply with a number, "
                f"or reply SKIP for default {DEFAULT_TRIP_MINUTES} min)"
            )
        else:
            upsert_user(from_number, awaiting_contacts=1)
            reply = (
                "Let's set up your safety contacts first.\n"
                "Reply with 1-3 phone numbers (with country code) separated by "
                "commas, e.g. +919812345678, +919898765432"
            )

    # ---- SAFE: close out an active trip ----
    elif msg_upper == "SAFE":
        if user and user[2]:  # trip_active
            upsert_user(from_number, trip_active=0)
            reply = "Great, glad you're safe! 🎉"
        else:
            reply = "You don't have an active trip right now. Reply START to begin one."

    # ---- Fallback ----
    else:
        reply = (
            "Hi! I'm your night safety bot.\n"
            "Reply START to begin a trip, SAFE when you arrive, or SOS for immediate help."
        )
    
print(f"[DEBUG] Generated reply for {from_number}: {reply}")
 try:
        send_whatsapp(from_number, reply)
    except Exception as e:
        print(f"[ERROR] Failed to send WhatsApp reply: {e}")
    return "", 200
@app.route("/telegram", methods=["POST"])
def telegram_webhook():
    data = request.get_json(silent=True) or {}
    message = data.get("message", {})
    incoming_msg = (message.get("text") or "").strip()
    chat_id = message.get("chat", {}).get("id")
    if not chat_id:
        return "", 200
    from_number = f"tg:{chat_id}"
    location = message.get("location")
    if location:
        lat = location.get("latitude")
        lon = location.get("longitude")
        user = get_user(from_number)
        sent_ok = False
        if user and user[2]:  # trip_active
            maps_link = f"https://maps.google..."
            contacts = user[1].split(",") if user[1] else []
            alert_msg = f"📍 Live location sh..."
            for contact in contacts:
                contact = contact.strip()
                if contact:
                    try:
                        send_whatsapp(f"whatsapp:{contact}", ...)
                        sent_ok = True
                    except Exception as e:
                        print(f"[ERROR] Failed to ale...")
        if sent_ok:
            send_telegram(chat_id, "📍 Got your location, shared with your emergency contacts.")
        else:
            send_telegram(chat_id, "📍 Got your location, but couldn't reach your emergency contacts right now. Please contact them directly if you can.")
        return "", 200

    user = get_user(from_number)
    reply = ""

    msg_upper = incoming_msg.upper()
    if msg_upper in ("EDIT", "EDIT CONTACTS", "RESET CONTACTS"):
        upsert_user(from_number, awaiting_contacts=1)
        reply = (
            "Let's update your safety contacts.\n"
            "Reply with 1-3 phone numbers (with country code) separated by "
            "commas, e.g. +919812345678, +919898765432"
        )
    # ---- SOS: works anytime, skips everything else ----
    elif msg_upper == "SOS":
        if user and user[1]:
            upsert_user(from_number, trip_active=1, trip_started_at=str(datetime.now()))
            trigger_alert(from_number)
            reply = "🚨 SOS triggered. Your emergency contacts have been alerted. Please share your live location now (📎 → Location) so we can send it to them too."
        else:
            reply = "You haven't set emergency contacts yet. Reply START to set them up."

    # ---- User is mid-setup: awaiting emergency contacts ----
    elif user and user[5]:
        contacts = incoming_msg
        upsert_user(from_number, emergency_contacts=contacts, awaiting_contacts=0, awaiting_duration=1)
        reply = (
            "Got it, saved your emergency contacts.\n"
            "How many minutes is your trip? (reply with a number, "
            f"or reply SKIP for default {DEFAULT_TRIP_MINUTES} min)"
        )

    # ---- User is mid-setup: awaiting trip duration ----
    elif user and user[6]:
        try:
            minutes = int(incoming_msg) if incoming_msg.upper() != "SKIP" else DEFAULT_TRIP_MINUTES
        except ValueError:
            minutes = DEFAULT_TRIP_MINUTES
        if minutes <= 0 or minutes > 10080:
            minutes = DEFAULT_TRIP_MINUTES

        expires_at = datetime.now() + timedelta(minutes=minutes)
        upsert_user(
            from_number,
            trip_active=1,
            trip_started_at=str(datetime.now()),
            trip_expires_at=str(expires_at),
            awaiting_duration=0,
        )
        reply = f"✅ Tracking started for {minutes} minutes. Reply SAFE when you arrive, or SOS for immediate help."

    # ---- START: begin a new trip ----
    elif msg_upper == "START":
        if user and user[1]:
            upsert_user(from_number, awaiting_duration=1)
            reply = (
                f"How many minutes is your trip? (reply with a number, "
                f"or reply SKIP for default {DEFAULT_TRIP_MINUTES} min)"
            )
        else:
            upsert_user(from_number, awaiting_contacts=1)
            reply = (
                "Let's set up your safety contacts first.\n"
                "Reply with 1-3 phone numbers (with country code) separated by "
                "commas, e.g. +919812345678, +919898765432"
            )

    # ---- SAFE: close out an active trip ----
    elif msg_upper == "SAFE":
        if user and user[2]:
            upsert_user(from_number, trip_active=0)
            reply = "Great, glad you're safe! 🎉"
        else:
            reply = "You don't have an active trip right now. Reply START to begin one."

    # ---- Fallback ----
    else:
        reply = (
            "Hi! I'm your night safety bot.\n"
            "Reply START to begin a trip, SAFE when you arrive, or SOS for immediate help."
        )

    send_telegram(chat_id, reply)
    return "", 200

@app.route("/check-trips", methods=["GET"])
def check_trips():
    """Called periodically by an external cron job to fire alerts for expired trips."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT phone FROM users WHERE trip_active = 1 AND trip_expires_at IS NOT NULL")
    rows = c.fetchall()
    conn.close()

    now = datetime.now()
    fired = 0
    for (phone,) in rows:
        user = get_user(phone)
        if not user or not user[2]:
            continue
        expires_at_str = user[4]
        if not expires_at_str:
            continue
        try:
            expires_at = datetime.fromisoformat(expires_at_str)
        except ValueError:
            continue
        if now >= expires_at:
            trigger_alert(phone)
            fired += 1

    return {"checked": len(rows), "alerts_fired": fired}, 200


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=8080)

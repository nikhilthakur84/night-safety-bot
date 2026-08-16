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
from apscheduler.schedulers.background import BackgroundScheduler

# ---------- CONFIG ----------
# Set these as environment variables before running:
#   export TWILIO_ACCOUNT_SID=xxxx
#   export TWILIO_AUTH_TOKEN=xxxx
#   export TWILIO_WHATSAPP_NUMBER=whatsapp:+14155238886   (Twilio sandbox number)
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.environ.get("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")

DB_PATH = os.path.join(os.path.dirname(__file__), "safety_bot.db")
DEFAULT_TRIP_MINUTES = 30

app = Flask(__name__)
scheduler = BackgroundScheduler()
scheduler.start()

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


def trigger_alert(phone):
    """Called when timer expires without a SAFE reply."""
    user = get_user(phone)
    if not user or not user[2]:  # trip_active check
        return  # user already checked in safe, do nothing

    contacts = user[1].split(",") if user[1] else []
    started_at = user[3]
    alert_msg = (
        f"⚠️ Safety Alert: {phone.replace('whatsapp:', '')} started a trip at "
        f"{started_at} and has not checked in as SAFE. Please check on them."
    )
    for contact in contacts:
        contact = contact.strip()
        if contact:
            send_whatsapp(f"whatsapp:{contact}", alert_msg)

    # mark trip as closed (alert already sent, avoid duplicate alerts)
    upsert_user(phone, trip_active=0)


# ---------- WEBHOOK ----------
@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    incoming_msg = request.values.get("Body", "").strip()
    from_number = request.values.get("From", "")  # e.g. 'whatsapp:+91XXXXXXXXXX'

    user = get_user(from_number)
    reply = ""

    msg_upper = incoming_msg.upper()

    # ---- SOS: works anytime, skips everything else ----
    if msg_upper == "SOS":
        if user and user[1]:
            upsert_user(from_number, trip_active=1, trip_started_at=str(datetime.now()))
            trigger_alert(from_number)
            reply = "🚨 SOS triggered. Your emergency contacts have been alerted immediately."
        else:
            reply = "You haven't set emergency contacts yet. Reply START to set up first."

    # ---- User is mid-setup: awaiting emergency contacts ----
    elif user and user[4]:  # awaiting_contacts
        contacts = incoming_msg
        upsert_user(from_number, emergency_contacts=contacts, awaiting_contacts=0, awaiting_duration=1)
        reply = (
            "Got it, saved your emergency contacts.\n"
            "How many minutes is your trip? (reply with just a number, "
            f"or reply SKIP for default {DEFAULT_TRIP_MINUTES} min)"
        )

    # ---- User is mid-setup: awaiting trip duration ----
    elif user and user[5]:  # awaiting_duration
        try:
            minutes = int(incoming_msg) if msg_upper != "SKIP" else DEFAULT_TRIP_MINUTES
        except ValueError:
            minutes = DEFAULT_TRIP_MINUTES

        upsert_user(from_number, trip_active=1, trip_started_at=str(datetime.now()), awaiting_duration=0)
        scheduler.add_job(
            trigger_alert,
            "date",
            run_date=datetime.now() + timedelta(minutes=minutes),
            args=[from_number],
            id=f"trip_{from_number}_{datetime.now().timestamp()}",
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

    send_whatsapp(from_number, reply)
    return "", 200


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=8080)

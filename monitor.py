#!/usr/bin/env python3
"""
Vinterbadbryggen Alert System (GitHub Actions friendly)
- Runs once per invocation (ideal for cron via Actions)
- Uses env vars for secrets
- Persists seen_events.json in repo root
"""

import os
import json
import smtplib
import logging
import hashlib
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Set, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from zoneinfo import ZoneInfo

# =========================
# Config (env-overridable)
# =========================
API_BASE_URL = os.environ.get(
    "VINTERBAD_API_URL",
    "https://www.vinterbadbryggen.com/api/activity/event/days",
)
EVENTS_TO_SHOW = int(os.environ.get("VINTERBAD_EVENTS_TO_SHOW", "1000"))
LOOKBACK_DAYS = int(os.environ.get("VINTERBAD_LOOKBACK_DAYS", "0"))
LOOKAHEAD_DAYS = int(os.environ.get("VINTERBAD_LOOKAHEAD_DAYS", "14"))

# Email via secrets
SENDER_EMAIL = os.environ.get("VINTERBAD_EMAIL", "").strip()
SENDER_PASSWORD = os.environ.get("VINTERBAD_APP_PASSWORD", "").strip()
RECIPIENT_EMAILS = [
    e.strip() for e in os.environ.get("RECIPIENT_EMAILS", "").split(",") if e.strip()
]
EMAIL_ENABLED = os.environ.get("VINTERBAD_EMAIL_ENABLED", "true").lower() == "true"

# Timezone
LOCAL_TZ = ZoneInfo("Europe/Copenhagen")
UTC_TZ = ZoneInfo("UTC")

# Paths
REPO_ROOT = Path(__file__).resolve().parent
SEEN_EVENTS_FILE = REPO_ROOT / "seen_events.json"

# Logging (stdout only for Actions)
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("vinterbad-monitor")


def _build_session() -> requests.Session:
    """Requests session with retry/backoff and UA."""
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        }
    )
    retries = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=0.6,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


class VinterbadAlertMonitor:
    """Monitor API and send email alerts for new gus slots."""

    def __init__(self):
        # Ensure seen file exists
        if not SEEN_EVENTS_FILE.exists():
            try:
                SEEN_EVENTS_FILE.write_text("[]", encoding="utf-8")
            except Exception as e:
                logger.warning(f"Could not initialize seen file: {e}")

        self.seen_event_ids: Set[str] = self.load_seen_events()
        logger.info(f"Using seen file: {SEEN_EVENTS_FILE}")
        self.session = _build_session()

    # ---------- state ----------
    def load_seen_events(self) -> Set[str]:
        try:
            if SEEN_EVENTS_FILE.exists():
                data = json.loads(SEEN_EVENTS_FILE.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return set(map(str, data))
        except Exception as e:
            logger.warning(f"Could not load seen events: {e}")
        return set()

    def save_seen_events(self):
        try:
            SEEN_EVENTS_FILE.write_text(
                json.dumps(sorted(list(self.seen_event_ids)), ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error(f"Could not save seen events: {e}")

    # ---------- api ----------
    def get_date_range(self) -> Dict[str, str]:
        """Use full-day window in Copenhagen time, then convert to UTC/Z."""
        now_local = datetime.now(LOCAL_TZ)
        start_local = (now_local - timedelta(days=LOOKBACK_DAYS)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        end_local = (now_local + timedelta(days=LOOKAHEAD_DAYS)).replace(
            hour=23, minute=59, second=59, microsecond=0
        )

        start_utc = start_local.astimezone(UTC_TZ)
        end_utc = end_local.astimezone(UTC_TZ)

        return {
            "fromOffset": start_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "toTime": end_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        }

    def fetch_events(self) -> List[Dict]:
        try:
            params = {"eventsToShow": EVENTS_TO_SHOW, **self.get_date_range()}
            logger.info(f"Requesting {API_BASE_URL} with params: {params}")
            resp = self.session.get(API_BASE_URL, params=params, timeout=15)
            if resp.status_code != 200:
                logger.error(f"API non-200: {resp.status_code} - {resp.text[:300]}...")
            resp.raise_for_status()
            data = resp.json()

            # --- CASE 1: top-level list ---
            if isinstance(data, list):
                # It might be a list of "days", each with slots
                if data and all(isinstance(x, dict) for x in data) and any(
                    "slots" in x for x in data
                ):
                    events: List[Dict] = []
                    for d in data:
                        slots = d.get("slots") or []
                        if isinstance(slots, dict):
                            slots = [slots]
                        if isinstance(slots, list):
                            events.extend(slots)
                    logger.info(
                        f"Flattened {len(data)} day(s) to {len(events)} slot event(s)"
                    )
                    if events:
                        logger.info(
                            "Sample events: "
                            + " | ".join(self.format_event_info(e) for e in events[:2])
                        )
                    return events

                # Otherwise assume it's already a flat list of events
                logger.info(
                    f"API returned a list with {len(data)} items (treated as events)"
                )
                return data

            # --- CASE 2: dict wrapper (fallbacks) ---
            events: List[Dict] = []
            if isinstance(data, dict):
                # days array with nested slots or events
                if "days" in data and isinstance(data["days"], list):
                    day_count = len(data["days"])
                    for d in data["days"]:
                        slots = (
                            d.get("slots")
                            or d.get("events")
                            or d.get("data")
                            or []
                        )
                        if isinstance(slots, dict):
                            slots = [slots]
                        if isinstance(slots, list):
                            events.extend(slots)
                    logger.info(
                        f"Flattened {day_count} day(s) to {len(events)} slot event(s)"
                    )
                    if events:
                        logger.info(
                            "Sample events: "
                            + " | ".join(self.format_event_info(e) for e in events[:2])
                        )
                    return events

                # common direct list keys
                for key in ["events", "data", "activities", "results"]:
                    v = data.get(key)
                    if isinstance(v, list):
                        logger.info(f"API '{key}' list size: {len(v)}")
                        return v
                    if isinstance(v, dict):
                        return [v]

            logger.warning(f"Unexpected API structure: {type(data)}")
            return []
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching events: {e}")
            return []
        except json.JSONDecodeError as e:
            logger.error(f"JSON parse error: {e}")
            return []

    # ---------- parsing ----------
    def _stable_event_uid(self, event: Dict) -> str:
        """Build a stable unique id from reliable fields (no volatile data)."""
        candidates = {
            "activityId": event.get("activityId")
            or event.get("activity_id")
            or event.get("activity"),
            "eventId": event.get("eventId")
            or event.get("event_id")
            or event.get("bookingId")
            or event.get("slotId")
            or event.get("timeSlotId"),
            "date": event.get("date")
            or event.get("startDate")
            or event.get("eventDate")
            or event.get("datetime"),
            "time": event.get("time")
            or event.get("startTime")
            or event.get("eventTime"),
            "name": event.get("name")
            or event.get("title")
            or event.get("activityName")
            or event.get("eventName"),
        }
        clean = {k: str(v) for k, v in candidates.items() if v is not None}
        payload = json.dumps(clean, sort_keys=True, ensure_ascii=False)
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    def extract_booking_info(self, event: Dict) -> Optional[Tuple[str, str, str]]:
        """Return (activity_id, event_id, unique_id) with robust fallback."""
        activity_id = None
        event_id = None

        for field in ["activityId", "activity_id", "activity", "id"]:
            if field in event:
                activity_id = str(event[field])
                break

        for field in ["eventId", "event_id", "id", "bookingId", "slotId", "timeSlotId"]:
            if field in event and field != "activityId":
                event_id = str(event[field])
                break

        # If event_id missing, try construct from date/time
        if not event_id:
            date_val = None
            time_val = None
            for date_field in ["date", "startDate", "eventDate", "datetime"]:
                if date_field in event:
                    date_val = event[date_field]
                    break
            for time_field in ["time", "startTime", "eventTime"]:
                if time_field in event:
                    time_val = event[time_field]
                    break
            if date_val and time_val:
                try:
                    event_id = self.construct_event_id(str(date_val), str(time_val))
                except Exception:
                    event_id = None

        uid = self._stable_event_uid(event)
        if not activity_id or not event_id:
            logger.debug(
                f"Falling back for UID only (no explicit IDs). Keys: {list(event.keys())[:8]}"
            )
        return (activity_id or "NA", event_id or "NA", uid)

    def construct_event_id(self, date_val: str, time_val: str) -> Optional[str]:
        try:
            # normalize date
            if "T" in date_val:
                if date_val.endswith("Z"):
                    date_val = date_val[:-1] + "+00:00"
                from datetime import datetime as dt

                date_obj = dt.fromisoformat(date_val)
            else:
                from datetime import datetime as dt

                date_obj = dt.strptime(date_val.split("T")[0], "%Y-%m-%d")

            if ":" in time_val:
                hh, mm, *_ = time_val.split(":") + ["00", "00"]
            else:
                hh = time_val[:2]
                mm = time_val[2:4] if len(time_val) >= 4 else "00"

            return f"{date_obj.strftime('%Y%m%d')}{hh.zfill(2)}{mm.zfill(2)}LP"
        except Exception as e:
            logger.debug(f"construct_event_id failed: {e}")
            return None

    def is_gus_event(self, event: Dict) -> bool:
        """Return True only for real gus sauna events."""
        name = str(event.get("name", "") or "").lower()
        location = str(
            event.get("locationName") or event.get("meetLocationName") or ""
        ).lower()

        # 1) Name contains "gus" (morgengus, søndagsgus, typisk torsdag gus, etc.)
        if "gus" in name:
            return True

        # 2) Or the location is the gussauna
        if "gussauna" in location or "gussaunaen" in location:
            return True

        return False

    def is_bookable(self, event: Dict) -> bool:
        """Heuristic: True if appears to have free spots / open status."""
        for field in [
            "availableSpots",
            "available",
            "spotsAvailable",
            "remainingSpots",
            "capacity",
            "slots",
            "freeSpots",
        ]:
            if field in event:
                val = event[field]
                if isinstance(val, (int, float)):
                    return val > 0
                if isinstance(val, bool):
                    return val
        for field in ["status", "bookingStatus", "state", "bookable"]:
            if field in event:
                status = str(event[field]).lower()
                if status in {"open", "available", "bookable", "true"}:
                    return True
                if status in {"full", "closed", "cancelled", "false", "booked"}:
                    return False
        return True  # optimistic default

    def format_event_info(self, event: Dict) -> str:
        """Human-friendly summary used in logs + emails."""
        name = (
            event.get("name")
            or event.get("title")
            or event.get("activityName")
            or event.get("eventName")
            or "Unnamed event"
        )

        # Prefer explicit startTime/date-like fields
        raw_time = (
            event.get("startTime")
            or event.get("time")
            or event.get("datetime")
            or event.get("startDate")
            or event.get("date")
            or "Unknown time"
        )

        # Try to parse UTC and show in Copenhagen time if possible
        pretty_time = raw_time
        try:
            dt = None
            txt = str(raw_time)
            if "T" in txt:
                if txt.endswith("Z"):
                    txt = txt[:-1] + "+00:00"
                from datetime import datetime as dtmod

                dt = dtmod.fromisoformat(txt)
            if dt is not None:
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC_TZ)
                dt_local = dt.astimezone(LOCAL_TZ)
                pretty_time = dt_local.strftime("%Y-%m-%d %H:%M (%Z)")
        except Exception:
            # fall back to raw string
            pretty_time = raw_time

        # Spots / waitlist info
        spots = None
        for field in ["availableSpots", "available", "spotsAvailable", "freeSpots"]:
            if field in event and isinstance(event[field], (int, float)):
                spots = int(event[field])
                break

        waitlist = event.get("waitingList") or event.get("waiting", 0)
        waitlist_msg = ""
        try:
            waitlist_int = int(waitlist)
            if waitlist_int > 0:
                waitlist_msg = f" • Waitlist: {waitlist_int}"
        except Exception:
            pass

        if spots is None:
            spot_msg = "Availability unknown"
        elif spots == 0:
            spot_msg = "🔥 Fully booked — waitlist is possible"
        else:
            spot_msg = f"✨ {spots} spot(s) available"

        return f"{name} | {pretty_time} | {spot_msg}{waitlist_msg}"

    def construct_booking_url(self, activity_id: str, event_id: str) -> str:
        return (
            f"https://www.vinterbadbryggen.com/api/activity/{activity_id}/event/{event_id}/book"
        )

    # ---------- email ----------
    def _ensure_email_config(self):
        if not SENDER_EMAIL or not SENDER_PASSWORD:
            raise RuntimeError(
                "Email not configured: missing VINTERBAD_EMAIL or VINTERBAD_APP_PASSWORD"
            )
        if not RECIPIENT_EMAILS:
            raise RuntimeError("Email not configured: RECIPIENT_EMAILS is empty")

    def send_email_alert(self, event: Dict, override_recipients: Optional[List[str]] = None) -> bool:
        """Send email about a new relevant event."""
        if not EMAIL_ENABLED:
            logger.info("Email alerts disabled")
            return False

        try:
            self._ensure_email_config()

            # Who should receive this
            recipients = override_recipients if override_recipients else RECIPIENT_EMAILS

            # Booking URL (fallback to site root if IDs not usable)
            booking_info = self.extract_booking_info(event)
            booking_url = "https://www.vinterbadbryggen.com"
            if booking_info:
                activity_id, event_id, _ = booking_info
                if activity_id != "NA" and event_id != "NA":
                    booking_url = self.construct_booking_url(activity_id, event_id)

            # Human-friendly info (already includes CET + availability text in your version)
            event_info = self.format_event_info(event)

            # ---- Build message ----
            msg = MIMEMultipart("alternative")
            msg["Subject"] = "🏊‍♂️ Nyt gus-event i kalenderen"
            msg["From"] = SENDER_EMAIL
            msg["To"] = ", ".join(recipients)

            # Plain text body
            text = f"""Der er oprettet et nyt gus-event i kalenderen.

Event:
{event_info}

Booking-side:
{booking_url}

— 
Denne mail er sendt automatisk fra dit lille event-monitor-script.
"""

            # HTML body
            html = (
                "<html>\n"
                "<body>\n"
                '<div style="font-family:Arial,Helvetica,sans-serif;max-width:640px;margin:auto">\n'
                '  <div style="background:#5b6ee1;color:#fff;padding:16px;'
                '              border-radius:10px 10px 0 0">\n'
                "    <h2>🏊‍♂️ Nyt gus-event</h2>\n"
                "    <p>Der er kommet et nyt event i kalenderen.</p>\n"
                "  </div>\n"
                '  <div style="background:#f7f7f7;padding:16px;'
                '              border-radius:0 0 10px 10px">\n'
                '    <div style="background:#fff;padding:12px 16px;'
                '                border-left:4px solid #5b6ee1;'
                '                border-radius:6px;margin:12px 0">\n'
                '      <h3 style="margin:0 0 8px 0">Eventdetaljer</h3>\n'
                f'      <p style="margin:0"><strong>{event_info}</strong></p>\n'
                "    </div>\n"
                "    <p>Ventelister og pladser går hurtigt – tjek eventet med det samme.</p>\n"
                "    <p>\n"
                f'      <a href="{booking_url}" '
                '         style="display:inline-block;padding:10px 16px;'
                '                background:#5b6ee1;color:#fff;'
                '                text-decoration:none;border-radius:6px;'
                '                font-weight:bold">\n'
                "        📅 Åbn bookingsiden\n"
                "      </a>\n"
                "    </p>\n"
                '    <p style="font-size:12px;color:#666">\n'
                "      Direkte URL:<br>\n"
                f'      <a href="{booking_url}">{booking_url}</a>\n'
                "    </p>\n"
                "  </div>\n"
                f'  <p style="text-align:center;color:#666;font-size:12px">\n'
                f"    Sendt {datetime.now(LOCAL_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
                "  </p>\n"
                "</div>\n"
                "</body>\n"
                "</html>\n"
            )

            part1 = MIMEText(text, "plain")
            part2 = MIMEText(html, "html")
            msg.attach(part1)
            msg.attach(part2)

            # ---- Send via Gmail SMTP ----
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(SENDER_EMAIL, SENDER_PASSWORD)
                server.send_message(msg)

            logger.info(f"✅ Email alert sent to {len(recipients)} recipient(s)")
            return True

        except smtplib.SMTPAuthenticationError:
            logger.error("Email auth failed (check App Password)")
            return False
        except Exception as e:
            logger.error(f"Failed to send email: {e}")
            return False


    # ---------- run once ----------
    def run_once(self) -> int:
        """Single pass: fetch -> detect new gus events -> email -> persist."""
        logger.info("Checking for new events...")
        events = self.fetch_events()
        if not events:
            logger.info("No events returned from API")
            return 0

        logger.info(f"Found {len(events)} total events")
        new_events: List[Dict] = []
        seen_changed = False
        extracted = 0
        newly_seen = 0

        for ev in events:
            # Skip non-gus events entirely
            if not self.is_gus_event(ev):
                logger.debug(f"Skipping non-gus event: {self.format_event_info(ev)}")
                continue

            info = self.extract_booking_info(ev)
            if not info:
                logger.debug(
                    f"Could not extract IDs from event keys: {list(ev.keys())[:6]}"
                )
                continue
            extracted += 1
            _, _, unique_id = info

            if unique_id not in self.seen_event_ids:
                self.seen_event_ids.add(unique_id)
                newly_seen += 1
                seen_changed = True

                details = self.format_event_info(ev)
                logger.info(f"📌 New gus event detected: {details}")

                if self.is_bookable(ev):
                    logger.info(
                        "✨ Event appears bookable (has free spots or 'open' status)"
                    )
                else:
                    logger.info(
                        "ℹ️ Event currently full/closed, but notifying (waitlist possible)"
                    )

                new_events.append(ev)

        logger.info(f"Extracted IDs for {extracted} event(s); newly seen gus: {newly_seen}")

        if seen_changed:
            self.save_seen_events()
            logger.info(f"Saved seen set to {SEEN_EVENTS_FILE}")
            try:
                raw = SEEN_EVENTS_FILE.read_text(encoding="utf-8")
                logger.info(f"seen_events.json bytes: {len(raw)}")
            except Exception as e:
                logger.warning(f"Could not read back seen file: {e}")

        # Email for any new gus events (bookable or full)
        sent = 0
        for ev in new_events:
            if self.send_email_alert(ev):
                sent += 1

        logger.info(f"Done. Alerts sent: {sent}")
        return sent


# ---------- helpers & entrypoint ----------
def _send_test_email() -> int:
    """Send a one-off test mail to verify SMTP + secrets (to sender only)."""
    now_local = datetime.now(LOCAL_TZ)
    dummy_event = {
        "name": "Vinterbad Monitor Test Email",
        "date": now_local.strftime("%Y-%m-%d"),
        "time": now_local.strftime("%H:%M"),
        "availableSpots": 0,
        "waitingList": 0,
        "activityId": "TEST123",
        "eventId": "TEST456",
    }

    test_recipient = SENDER_EMAIL or "gudlaugerl@gmail.com"
    logger.info(f"Sending test email to {test_recipient}")

    mon = VinterbadAlertMonitor()
    ok = mon.send_email_alert(dummy_event, override_recipients=[test_recipient])
    logger.info("Test email status: %s", "SENT" if ok else "FAILED")
    return 0 if ok else 2


def main():
    """Entry point (matches how GitHub Actions runs it)."""
    # Optional: test email mode
    if os.environ.get("VINTERBAD_TEST_SEND", "").lower() in {"1", "true", "yes"}:
        raise SystemExit(_send_test_email())

    # Normal monitoring mode
    if EMAIL_ENABLED:
        if not SENDER_EMAIL or not SENDER_PASSWORD:
            logger.error("Missing VINTERBAD_EMAIL or VINTERBAD_APP_PASSWORD")
            raise SystemExit(2)
        if not RECIPIENT_EMAILS:
            logger.error("RECIPIENT_EMAILS is empty")
            raise SystemExit(2)

    monitor = VinterbadAlertMonitor()
    monitor.run_once()
    raise SystemExit(0)


if __name__ == "__main__":
    main()

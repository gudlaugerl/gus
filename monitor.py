#!/usr/bin/env python3
"""
Vinterbadbryggen Alert System (GitHub Actions friendly)
- Runs once per invocation (ideal for cron via Actions)
- Uses env vars for secrets
- Persists seen_event.json in repo root
"""

import os
import json
import smtplib
import logging
import hashlib
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Set, Optional, Tuple
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formatdate, parseaddr

# =========================
# Config (env-overridable)
# =========================
API_BASE_URL = os.environ.get(
    "VINTERBAD_API_URL",
    "https://www.vinterbadbryggen.com/api/activity/event/days",
)
EVENTS_TO_SHOW = int(os.environ.get("VINTERBAD_EVENTS_TO_SHOW", "300"))
LOOKBACK_DAYS = int(os.environ.get("VINTERBAD_LOOKBACK_DAYS", "0"))
LOOKAHEAD_DAYS = int(os.environ.get("VINTERBAD_LOOKAHEAD_DAYS", "14"))

# Email via secrets
SENDER_EMAIL = os.environ.get("VINTERBAD_EMAIL", "").strip()
SENDER_PASSWORD = os.environ.get("VINTERBAD_APP_PASSWORD", "").strip()

# Parse recipients "Name <addr>; Other <addr>" or comma-separated
RAW_RECIPIENTS = os.environ.get("RECIPIENT_EMAILS", "")
RECIPIENTS: List[Tuple[str, str]] = []
for part in RAW_RECIPIENTS.replace(",", ";").split(";"):
    p = part.strip()
    if not p:
        continue
    name, addr = parseaddr(p)
    if addr:
        RECIPIENTS.append((name or addr.split("@")[0], addr))

EMAIL_ENABLED = os.environ.get("VINTERBAD_EMAIL_ENABLED", "true").lower() == "true"

# Paths
REPO_ROOT = Path(__file__).resolve().parent
SEEN_EVENTS_FILE = REPO_ROOT / "seen_event.json"  # singular to match workflow

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
    """Monitor API and send email alerts for new bookable slots."""

    def __init__(self):
        self.session = _build_session()
        self.seen_path = SEEN_EVENTS_FILE
        self.seen_event_ids: Set[str] = self.load_seen_events()

    # ---------- persistence ----------
    def load_seen_events(self) -> Set[str]:
        try:
            if self.seen_path.exists():
                raw = self.seen_path.read_text(encoding="utf-8").strip()
                return set(json.loads(raw or "[]"))
        except Exception as e:
            logger.warning(f"Could not load seen events ({self.seen_path}): {e}")
        return set()

    def save_seen_events(self) -> None:
        data = json.dumps(sorted(self.seen_event_ids), ensure_ascii=False, indent=2)
        self.seen_path.write_text(data + "\n", encoding="utf-8")
        logger.info(f"Persisted {len(self.seen_event_ids)} seen IDs to {self.seen_path}")

    # ---------- api ----------
    def get_date_range(self) -> Dict[str, str]:
        tz = ZoneInfo("Europe/Copenhagen")
        now_local = datetime.now(tz)

        start_local = (now_local - timedelta(days=LOOKBACK_DAYS)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        end_local = (now_local + timedelta(days=LOOKAHEAD_DAYS)).replace(
            hour=23, minute=59, second=59, microsecond=999000
        )

        start_utc = start_local.astimezone(ZoneInfo("UTC"))
        end_utc = end_local.astimezone(ZoneInfo("UTC"))

        return {
            "fromOffset": start_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "toTime": end_utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-3],
        }

    def fetch_events(self) -> List[Dict]:
        try:
            params = {"eventsToShow": EVENTS_TO_SHOW, **self.get_date_range()}
            resp = self.session.get(API_BASE_URL, params=params, timeout=15)
            if resp.status_code != 200:
                logger.error(f"API non-200: {resp.status_code} - {resp.text[:300]}...")
            resp.raise_for_status()
            data = resp.json()

            if isinstance(data, list):
                logger.info(f"API returned a list with {len(data)} items")
                return data

            events: List[Dict] = []
            if isinstance(data, dict):
                if "days" in data and isinstance(data["days"], list):
                    for d in data["days"]:
                        evs = d.get("events") or d.get("data") or []
                        if isinstance(evs, dict):
                            evs = [evs]
                        if isinstance(evs, list):
                            events.extend(evs)
                    logger.info(f"Flattened {len(data['days'])} day(s) to {len(events)} event(s)")
                    if events:
                        logger.info(
                            "Sample events: "
                            + " | ".join(self.format_event_info(e) for e in events[:2])
                        )
                    return events

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
    def extract_booking_info(self, event: Dict) -> Optional[Tuple[str, str, str]]:
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

        if not event_id:
            date_val = next(
                (event[f] for f in ["date", "startDate", "eventDate", "datetime"] if f in event),
                None,
            )
            time_val = next(
                (event[f] for f in ["time", "startTime", "eventTime"] if f in event),
                None,
            )
            if date_val and time_val:
                event_id = self.construct_event_id(date_val, time_val)

        try:
            payload = json.dumps(event, sort_keys=True, ensure_ascii=False).encode("utf-8")
            unique_id = hashlib.sha1(payload).hexdigest()
        except Exception:
            unique_id = f"{activity_id or 'NA'}::{event_id or 'NA'}"

        if activity_id and event_id and unique_id:
            return activity_id, event_id, unique_id
        return None

    def construct_event_id(self, date_val: str, time_val: str) -> Optional[str]:
        try:
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

    def is_bookable(self, event: Dict) -> bool:
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
        return True

    def format_event_info(self, event: Dict) -> str:
        parts: List[str] = []
        for field in ["name", "title", "activityName", "eventName"]:
            if field in event:
                parts.append(f"{event[field]}")
                break
        for field in ["date", "startTime", "time", "datetime", "startDate"]:
            if field in event:
                parts.append(f"{event[field]}")
                break
        for field in ["availableSpots", "available", "spotsAvailable", "freeSpots"]:
            if field in event:
                parts.append(f"{event[field]} spots available")
                break
        if not parts:
            parts = [f"{k}: {v}" for k, v in list(event.items())[:3]]
        return " | ".join(parts)

    def construct_booking_url(self, activity_id: str, event_id: str) -> str:
        return f"https://www.vinterbadbryggen.com/api/activity/{activity_id}/event/{event_id}/book"

    # ---------- email ----------
    def _ensure_email_config(self) -> None:
        if not EMAIL_ENABLED:
            return
        if not SENDER_EMAIL or not SENDER_PASSWORD:
            raise RuntimeError("Email not configured: missing VINTERBAD_EMAIL or VINTERBAD_APP_PASSWORD")
        if not RECIPIENTS:
            raise RuntimeError("Email not configured: RECIPIENT_EMAILS is empty")

    def send_email_alert(self, event: Dict) -> bool:
        """Send a formatted email for a newly available slot (personalized per recipient)."""
        if not EMAIL_ENABLED:
            logger.info("Email alerts disabled")
            return False

        try:
            self._ensure_email_config()

            booking_info = self.extract_booking_info(event)
            booking_url = "https://www.vinterbadbryggen.com"
            if booking_info:
                activity_id, event_id, _ = booking_info
                booking_url = self.construct_booking_url(activity_id, event_id)
            event_info = self.format_event_info(event)
            timestamp = formatdate(localtime=False)

            base_text = (
                "New winter swimming slot available at Vinterbadbryggen!\n\n"
                f"Event Details:\n{event_info}\n\n"
                f"Booking URL:\n{booking_url}\n\n"
                "Book now before it fills up!\n\n"
                "---\n"
                "This is an automated alert from your Vinterbad monitor.\n"
            )

            html_template = """
<html>
<body>
<div style="font-family:Arial,Helvetica,sans-serif;max-width:640px;margin:auto">
  <div style="background:#5b6ee1;color:#fff;padding:16px;border-radius:10px 10px 0 0">
    <h2>🏊‍♂️🔥  Hej {name}, komdu með í GUS! 🔥🏊‍♂️</h2>
    <p>A winter swimming slot just opened up at Vinterbadbryggen</p>
  </div>
  <div style="background:#f7f7f7;padding:16px;border-radius:0 0 10px 10px">
    <div style="background:#fff;padding:12px 16px;border-left:4px solid #5b6ee1;border-radius:6px;margin:12px 0">
      <h3 style="margin:0 0 8px 0">Event Details</h3>
      <p style="margin:0"><strong>{event_info}</strong></p>
    </div>
    <p>Don't wait—these slots fill up fast!</p>
    <p>
      <a href="{booking_url}" style="display:inline-block;padding:10px 16px;background:#5b6ee1;color:#fff;text-decoration:none;border-radius:6px;font-weight:bold">📅 Book Now</a>
    </p>
    <p style="font-size:12px;color:#666">
      Direct booking URL:<br>
      <a href="{booking_url}">{booking_url}</a>
    </p>
  </div>
  <p style="text-align:center;color:#666;font-size:12px">
    This is an automated alert • {timestamp}
  </p>
</div>
</body>
</html>
"""

            sent_count = 0
            for name, addr in RECIPIENTS:
                personal_text = f"Hej {name}, a new winter swimming slot is available!\n\n" + base_text
                personal_html = html_template.format(
                    name=name, event_info=event_info, booking_url=booking_url, timestamp=timestamp
                )

                msg = MIMEMultipart("alternative")
                msg["Subject"] = "🏊‍♂️🔥 Það var að koma inn nýtt gus! 🔥🏊‍♂️"
                msg["From"] = SENDER_EMAIL
                msg["To"] = addr
                msg.attach(MIMEText(personal_text, "plain"))
                msg.attach(MIMEText(personal_html, "html"))

                with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                    server.login(SENDER_EMAIL, SENDER_PASSWORD)
                    server.send_message(msg)

                sent_count += 1

            logger.info(f"✅ Email alerts sent to {sent_count} recipient(s)")
            return True

        except smtplib.SMTPAuthenticationError:
            logger.error("Email auth failed (check App Password)")
            return False
        except Exception as e:
            logger.error(f"Failed to send email: {e}")
            return False

    # ---------- run once ----------
    def run_once(self) -> int:
        logger.info(f"Using seen file: {self.seen_path}")
        events = self.fetch_events()
        if not events:
            logger.info("No events returned from API")
            return 0

        logger.info(f"Found {len(events)} total events")
        new_bookable: List[Dict] = []
        seen_changed = False

        for ev in events:
            info = self.extract_booking_info(ev)
            if not info:
                continue
            _, _, unique_id = info

            if unique_id not in self.seen_event_ids:
                seen_changed = True
                self.seen_event_ids.add(unique_id)
                logger.info(f"📌 New event: {self.format_event_info(ev)}")

                if self.is_bookable(ev):
                    logger.info("✨ Bookable")
                    new_bookable.append(ev)

        if seen_changed:
            self.save_seen_events()
        else:
            logger.info("No new events; not touching seen file")

        sent = 0
        for ev in new_bookable:
            if self.send_email_alert(ev):
                sent += 1

        logger.info(f"Done. Alerts sent: {sent}")
        return sent


# ---------- helpers & entrypoint ----------
def _send_test_email() -> int:
    """Send a one-off test mail to verify SMTP + secrets (to sender only)."""
    dummy_event = {
        "name": "Vinterbad Monitor Test Email",
        "date": datetime.utcnow().strftime("%Y-%m-%d"),
        "time": datetime.utcnow().strftime("%H:%M"),
        "availableSpots": 9,
        "activityId": "TEST123",
        "eventId": "TEST456",
    }

    test_recipient = SENDER_EMAIL or "gudlaugerl@gmail.com"
    logger.info(f"Sending test email to {test_recipient}")

    mon = VinterbadAlertMonitor()
    # Override recipients for a one-off local test
    global RECIPIENTS
    RECIPIENTS = [(test_recipient.split("@")[0], test_recipient)]

    ok = mon.send_email_alert(dummy_event)
    logger.info("Test email status: %s", "SENT" if ok else "FAILED")
    return 0 if ok else 2


def main() -> None:
    """Entry point for GitHub Actions."""
    if os.environ.get("VINTERBAD_TEST_SEND", "").lower() in {"1", "true", "yes"}:
        raise SystemExit(_send_test_email())

    if EMAIL_ENABLED:
        if not SENDER_EMAIL or not SENDER_PASSWORD:
            logger.error("Missing VINTERBAD_EMAIL or VINTERBAD_APP_PASSWORD")
            raise SystemExit(2)
        if not RECIPIENTS:
            logger.error("RECIPIENT_EMAILS is empty")
            raise SystemExit(2)

    monitor = VinterbadAlertMonitor()
    monitor.run_once()
    raise SystemExit(0)


if __name__ == "__main__":
    main()

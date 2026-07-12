import os
import time
import json
import requests
import smtplib
import threading
import http.server
import sys
from email.mime.text import MIMEText
from datetime import datetime, timedelta, date
from dotenv import load_dotenv
import pytz
import icalendar
import recurring_ical_events

# Load environment variables from .env file
load_dotenv()

# Environment Configuration
URL = os.getenv("CYRIDE_JSON_URL", "https://cyride.net/sync/open.json")
HTML_URL = os.getenv("CYRIDE_HTML_URL", "https://cyride.net/sync/open.html")
ICS_URL = os.getenv("ICS_URL")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", 45))
RECIPIENT = os.getenv("RECIPIENT_EMAIL")
ZOHO_USER = os.getenv("ZOHO_FROM_EMAIL")
ZOHO_PASS = os.getenv("ZOHO_PASSWORD")
ZOHO_HOST = os.getenv("ZOHO_SMTP_HOST", "smtp.zoho.com")
ZOHO_PORT = int(os.getenv("ZOHO_SMTP_PORT", 587))
WEB_PORT = int(os.getenv("WEB_PORT", 3000))
DISPATCH_PHONE = os.getenv("DISPATCH_PHONE", "515-292-1100")

CACHE_FILE = "cache.json"
TZ = pytz.timezone("America/Chicago")
MESSAGE_FOOTER = f"\n\nCall {DISPATCH_PHONE}\n{HTML_URL}"

# Run-scrape and cache read/write share the cache file; this keeps the
# background loop and any API-triggered scrape from racing each other.
scrape_lock = threading.Lock()


def format_time(time_str):
    """Converts 24h '19:12' into compact '7:12p' format for watches."""
    dt = datetime.strptime(time_str, "%H:%M")
    return dt.strftime("%I:%M%p").lstrip('0').replace('AM', 'a').replace('PM', 'p')


def get_shift_id(shift):
    """Generates a unique string identifier for a shift to compare against cache."""
    return f"{shift.get('date')}_{shift.get('run')}_{shift.get('start')}_{shift.get('end')}_{shift.get('route')}"


def is_real_shift(shift):
    """The board lists placeholder rows (e.g. run 'XXX' with no start/end/route)
    for slots that aren't actually open. Those can't be scheduled or messaged,
    so they're filtered out everywhere except the raw scrape."""
    return bool(shift.get('start')) and bool(shift.get('end')) and bool(shift.get('route'))


def flatten_signups(data):
    """Flattens every day's signups into one list of real (schedulable) shifts."""
    shifts = []
    for day_data in data.get('days', {}).values():
        for shift in day_data.get('signups', []):
            if is_real_shift(shift):
                shifts.append(shift)
    return shifts


def parse_datetime(date_str, time_str, tz_name="America/Chicago"):
    """Parses date and time strings into a timezone-aware datetime object."""
    tz = pytz.timezone(tz_name)
    dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    return tz.localize(dt)


def merge_blocks(blocks):
    """Merges overlapping or touching time blocks."""
    if not blocks:
        return []
    blocks.sort(key=lambda x: x[0])
    merged = [blocks[0]]
    for current in blocks[1:]:
        prev = merged[-1]
        if current[0] <= prev[1]:
            merged[-1] = (prev[0], max(prev[1], current[1]))
        else:
            merged.append(current)
    return merged


def fetch_ics_events(start_dt, end_dt, tz_name="America/Chicago"):
    """Fetches ICS and returns busy blocks within the requested timeframe."""
    blocks = []
    if not ICS_URL:
        return blocks

    try:
        response = requests.get(ICS_URL, timeout=15)
        response.raise_for_status()
        cal = icalendar.Calendar.from_ical(response.text)
        events = recurring_ical_events.of(cal).between(start_dt, end_dt)

        tz = pytz.timezone(tz_name)

        for event in events:
            if type(event["DTSTART"].dt) is date:
                continue

            desc = event.get('DESCRIPTION', '')
            if desc and "*Ignore*" in desc.to_ical().decode('utf-8'):
                continue

            ev_start = event["DTSTART"].dt
            ev_end = event["DTEND"].dt

            if ev_start.tzinfo is None:
                ev_start = tz.localize(ev_start)
            else:
                ev_start = ev_start.astimezone(tz)

            if ev_end.tzinfo is None:
                ev_end = tz.localize(ev_end)
            else:
                ev_end = ev_end.astimezone(tz)

            blocks.append((ev_start, ev_end))
    except Exception as e:
        print(f"[{datetime.now()}] Error fetching/parsing ICS: {e}")

    return blocks


def evaluate_shift_rules(shift_start_dt, shift_end_dt, week_blocks):
    """Evaluates the transit scheduling rules against the proposed shift."""
    for block in week_blocks:
        if max(shift_start_dt, block[0]) < min(shift_end_dt, block[1]):
            return False, "Overlaps with existing schedule"

    test_blocks = week_blocks + [(shift_start_dt, shift_end_dt)]

    if len(test_blocks) > 6:
        return False, "Cannot work more than 6 shifts a week"

    daily_blocks = {}
    for b_start, b_end in test_blocks:
        daily_blocks.setdefault(b_start.date(), []).append((b_start, b_end))

    shift_d = shift_start_dt.date()
    merged_day = merge_blocks(daily_blocks[shift_d])

    total_hours = sum((b[1] - b[0]).total_seconds() / 3600 for b in merged_day)
    if total_hours > 10.5:
        return False, f"Exceeds 10.5 hours a day (Total: {total_hours:.1f}h)"

    spread = (merged_day[-1][1] - merged_day[0][0]).total_seconds() / 3600
    if spread > 16.0:
        return False, f"Exceeds 16 hours spread (Spread: {spread:.1f}h)"

    seq_start = merged_day[0][0]
    seq_end = merged_day[0][1]

    if (seq_end - seq_start).total_seconds() / 3600 > 6.0:
        return False, "More than 6 hours straight without a break"

    for b_start, b_end in merged_day[1:]:
        gap = (b_start - seq_end).total_seconds() / 3600
        if gap < 0.5:
            seq_end = max(seq_end, b_end)
        else:
            seq_start = b_start
            seq_end = b_end

        if (seq_end - seq_start).total_seconds() / 3600 > 6.0:
            return False, "More than 6 hours straight without a half-hour break"

    prev_d = shift_d - timedelta(days=1)
    next_d = shift_d + timedelta(days=1)

    curr_first_start = min(b[0] for b in daily_blocks[shift_d])
    curr_last_end = max(b[1] for b in daily_blocks[shift_d])

    if prev_d in daily_blocks:
        prev_last_end = max(b[1] for b in daily_blocks[prev_d])
        if (curr_first_start - prev_last_end).total_seconds() / 3600 < 9.0:
            return False, "Less than 9 hour break from previous day"

    if next_d in daily_blocks:
        next_first_start = min(b[0] for b in daily_blocks[next_d])
        if (next_first_start - curr_last_end).total_seconds() / 3600 < 9.0:
            return False, "Less than 9 hour break before next day"

    return True, "Valid"


def evaluate_shifts(shift_list):
    """Checks a list of raw shift dicts against the Google Calendar + scheduling
    rules. Returns (all_records, valid_records), where each record carries the
    fields needed for both the API and the outbound message."""
    if not shift_list:
        return [], []

    dates = [datetime.strptime(s['date'], "%Y-%m-%d").date() for s in shift_list]
    fetch_start = TZ.localize(datetime.combine(min(dates) - timedelta(days=7), datetime.min.time()))
    fetch_end = TZ.localize(datetime.combine(max(dates) + timedelta(days=7), datetime.max.time()))
    ics_blocks = fetch_ics_events(fetch_start, fetch_end)

    all_records, valid_records = [], []
    for shift in shift_list:
        s_dt = parse_datetime(shift['date'], shift['start'])
        e_dt = parse_datetime(shift['date'], shift['end'])
        if e_dt <= s_dt:
            e_dt += timedelta(days=1)  # overnight shift

        shift_date = s_dt.date()
        monday = shift_date - timedelta(days=shift_date.weekday())
        sunday = monday + timedelta(days=6)
        week_start = TZ.localize(datetime.combine(monday - timedelta(days=1), datetime.min.time()))
        week_end = TZ.localize(datetime.combine(sunday + timedelta(days=1), datetime.max.time()))
        week_blocks = [b for b in ics_blocks if b[0] >= week_start and b[1] <= week_end]

        works, reason = evaluate_shift_rules(s_dt, e_dt, week_blocks)

        record = {
            "date": shift['date'],
            "day": s_dt.strftime("%A"),
            "run": shift.get('run', ''),
            "route": shift.get('route', ''),
            "start": shift.get('start', ''),
            "end": shift.get('end', ''),
            "hours": round((e_dt - s_dt).total_seconds() / 3600, 2),
            "OT": shift.get('overtime', False),
            "worksWithSchedule": works,
            "failReason": reason,
        }
        all_records.append(record)
        if works:
            valid_records.append(record)

    return all_records, valid_records


def format_shift_block(record):
    """One phone-readable block: day, date, run, route, start-end, hours."""
    date_obj = datetime.strptime(record['date'], "%Y-%m-%d")
    date_str = f"{date_obj.month}/{date_obj.day}"
    start_time = format_time(record['start'])
    end_time = format_time(record['end'])
    ot_str = "Y" if record.get('OT') else "N"

    block = f"{record['day'][:3]} {date_str} | {record['run']}({record['route']})\n"
    block += f"{start_time}-{end_time} ({record['hours']}h) OT:{ot_str}"
    return block


def build_message(records, empty_text):
    """Builds the full outbound message body, always ending with the dispatch
    phone number and a link to the open-runs page."""
    if not records:
        return empty_text + MESSAGE_FOOTER
    return "\n\n".join(format_shift_block(r) for r in records) + MESSAGE_FOOTER


def send_message(body, subject):
    """Sends the message body via Zoho SMTP (email-to-SMS gateway or plain email)."""
    print(f"[{datetime.now()}] Sending message: {subject}")

    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From'] = ZOHO_USER
    msg['To'] = RECIPIENT

    try:
        server = smtplib.SMTP(ZOHO_HOST, ZOHO_PORT)
        server.starttls()
        server.login(ZOHO_USER, ZOHO_PASS)
        server.send_message(msg)
        server.quit()
        print(f"[{datetime.now()}] Message sent successfully.")
    except Exception as e:
        print(f"[{datetime.now()}] Error sending message: {e}")

    return body


def fetch_open_shifts():
    """Scrapes the CyRide open-runs JSON feed. Raises on network/parse failure."""
    response = requests.get(URL, timeout=10)
    response.raise_for_status()
    return response.json()


def load_cache():
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return {}


def save_cache(data):
    """Writes atomically so a concurrent reader never sees a half-written file."""
    tmp_path = CACHE_FILE + ".tmp"
    with open(tmp_path, 'w') as f:
        json.dump(data, f)
    os.replace(tmp_path, CACHE_FILE)


def run_cycle():
    """Scrapes, diffs against the cache, evaluates rules for anything new, and
    persists the fresh scrape as the new cache. Does not send any message -
    callers decide whether/what to send. Returns (data, new_records, valid_records, had_cache)."""
    with scrape_lock:
        data = fetch_open_shifts()
        cached_data = load_cache()
        cached_ids = {get_shift_id(s) for s in flatten_signups(cached_data)}
        new_shifts = [s for s in flatten_signups(data) if get_shift_id(s) not in cached_ids]

        all_records, valid_records = evaluate_shifts(new_shifts)

        if all_records:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            try:
                with open(f"diff_{timestamp}.json", 'w') as f:
                    json.dump(all_records, f, indent=4)
                print(f"[{datetime.now()}] Saved {len(all_records)} new evaluated shifts to diff_{timestamp}.json")
            except Exception as e:
                print(f"[{datetime.now()}] Failed to save diff json: {e}")

        had_cache = bool(cached_data)
        save_cache(data)
        return data, all_records, valid_records, had_cache


def check_for_shifts():
    """Background poll: runs a cycle and emails only newly-valid shifts (and
    only once there's a prior cache to diff against, so the first run doesn't
    blast every currently-open shift as if it were new)."""
    try:
        _, _, valid_records, had_cache = run_cycle()
    except Exception as e:
        print(f"[{datetime.now()}] Failed to fetch or parse JSON: {e}")
        return

    if valid_records and had_cache:
        message = build_message(valid_records, "")
        send_message(message, "New CyRide Shift(s)!")


def run_test_ping():
    """Fetches the latest JSON, picks a shift (or creates a mock one), and sends a test message."""
    print(f"[{datetime.now()}] Running test ping...")
    try:
        data = fetch_open_shifts()
    except Exception as e:
        print(f"[{datetime.now()}] Failed to fetch JSON for test: {e}")
        return

    real_shifts = flatten_signups(data)
    if real_shifts:
        shift = real_shifts[0]
        date_obj = datetime.strptime(shift['date'], "%Y-%m-%d")
        record = {
            "date": shift['date'],
            "day": date_obj.strftime("%A"),
            "run": shift.get('run', ''),
            "route": f"TEST - {shift.get('route', 'Unknown')}",
            "start": shift['start'],
            "end": shift['end'],
            "hours": 4.0,
            "OT": False,
        }
    else:
        print("No open shifts found in JSON. Using mock shift for test...")
        today = datetime.now()
        record = {
            "date": today.strftime("%Y-%m-%d"),
            "day": today.strftime("%A"),
            "run": "99",
            "route": "TEST Route",
            "start": "12:00",
            "end": "16:00",
            "hours": 4.0,
            "OT": False,
        }

    send_message(build_message([record], ""), "[TEST] CyRide Ping")
    print("Test ping complete. Exiting.")


# ── HTTP API ──────────────────────────────────────────────────────────────
# GET /getAll       -> live scrape, all real shifts on the board (evaluated)
# GET /getNew       -> shifts not seen in the last persisted scrape
# GET /getAvalible  -> the above, filtered to ones that fit the schedule
#                      (JSON null, not [], when nothing fits)
# GET /update       -> scrapes, persists, emails/texts new-and-valid shifts
#                      (or a "no open shifts" message), returns "sent \n{message}"
# GET /updateAll    -> scrapes, persists, emails/texts every open shift,
#                      returns "sent \n{message}"

def get_new_and_valid():
    """Read-only: live scrape diffed against the *existing* cache, without
    persisting, so polling these endpoints doesn't suppress the next
    scheduled notification for the same shifts."""
    data = fetch_open_shifts()
    cached_ids = {get_shift_id(s) for s in flatten_signups(load_cache())}
    new_shifts = [s for s in flatten_signups(data) if get_shift_id(s) not in cached_ids]
    return evaluate_shifts(new_shifts)


def handle_update():
    _, _, valid_records, _ = run_cycle()
    message = build_message(valid_records, "No open shifts fit your schedule right now.")
    subject = "New CyRide Shift(s)!" if valid_records else "CyRide: No Open Shifts"
    send_message(message, subject)
    return f"sent \n{message}"


def handle_update_all():
    data, _, _, _ = run_cycle()
    all_records, _ = evaluate_shifts(flatten_signups(data))
    message = build_message(all_records, "No open shifts right now.")
    send_message(message, "CyRide: All Open Shifts")
    return f"sent \n{message}"


class ApiHandler(http.server.BaseHTTPRequestHandler):
    def _json(self, payload, status=200):
        body = json.dumps(payload, indent=2).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, text, status=200):
        body = text.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.rstrip('/') or '/'
        try:
            if path == '/':
                self._text('CyRide Notifier is actively running.')
            elif path == '/getAll':
                data = fetch_open_shifts()
                all_records, _ = evaluate_shifts(flatten_signups(data))
                self._json(all_records)
            elif path == '/getNew':
                all_records, _ = get_new_and_valid()
                self._json(all_records)
            elif path == '/getAvalible':
                _, valid_records = get_new_and_valid()
                self._json(valid_records or None)
            elif path == '/update':
                self._text(handle_update())
            elif path == '/updateAll':
                self._text(handle_update_all())
            else:
                self._text('Not found', status=404)
        except Exception as e:
            print(f"[{datetime.now()}] API error on {self.path}: {e}")
            self._text(f'Error: {e}', status=500)

    def log_message(self, format, *args):
        pass


def start_api_server(port):
    try:
        with http.server.ThreadingHTTPServer(("", port), ApiHandler) as httpd:
            print(f"API server listening on port {port}")
            httpd.serve_forever()
    except Exception as e:
        print(f"Failed to bind web server to port {port}: {e}")


if __name__ == "__main__":
    if "--test" in sys.argv:
        run_test_ping()
        sys.exit(0)

    print(f"Starting CyRide Notifier... checking every {CHECK_INTERVAL} seconds.")

    threading.Thread(target=start_api_server, args=(WEB_PORT,), daemon=True).start()

    while True:
        check_for_shifts()
        time.sleep(CHECK_INTERVAL)

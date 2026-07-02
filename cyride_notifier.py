import os
import time
import json
import requests
import smtplib
import threading
import http.server
import socketserver
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
ICS_URL = os.getenv("ICS_URL")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", 45))
RECIPIENT = os.getenv("RECIPIENT_EMAIL")
ZOHO_USER = os.getenv("ZOHO_FROM_EMAIL")
ZOHO_PASS = os.getenv("ZOHO_PASSWORD")
ZOHO_HOST = os.getenv("ZOHO_SMTP_HOST", "smtp.zoho.com")
ZOHO_PORT = int(os.getenv("ZOHO_SMTP_PORT", 587))
WEB_PORT = int(os.getenv("WEB_PORT", 3000))

CACHE_FILE = "cache.json"

def format_time(time_str):
    """Converts 24h '19:12' into compact '7:12p' format for watches."""
    dt = datetime.strptime(time_str, "%H:%M")
    return dt.strftime("%I:%M%p").lstrip('0').replace('AM', 'a').replace('PM', 'p')

def get_shift_id(shift):
    """Generates a unique string identifier for a shift to compare against cache."""
    return f"{shift.get('date')}_{shift.get('run')}_{shift.get('start')}_{shift.get('end')}_{shift.get('route')}"

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

def evaluate_shift_rules(shift_date, shift_start_dt, shift_end_dt, week_blocks):
    """Evaluates the 5 transit scheduling rules against the proposed shift."""
    for block in week_blocks:
        if max(shift_start_dt, block[0]) < min(shift_end_dt, block[1]):
            return False, "Overlaps with existing schedule"
    
    test_blocks = week_blocks + [(shift_start_dt, shift_end_dt)]
    
    daily_blocks = {}
    for b_start, b_end in test_blocks:
        b_date = b_start.date()
        if b_date not in daily_blocks:
            daily_blocks[b_date] = []
        daily_blocks[b_date].append((b_start, b_end))
        
    if len(daily_blocks) > 6:
        return False, "Cannot work more than 6 days a week"
        
    shift_d = shift_start_dt.date()
    if shift_d in daily_blocks:
        merged_day = merge_blocks(daily_blocks[shift_d])
        
        total_hours = sum((b[1] - b[0]).total_seconds() / 3600 for b in merged_day)
        if total_hours > 10.0:
            return False, f"Exceeds 10 hours a day (Total: {total_hours:.1f}h)"
            
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
    
    curr_first_start = min([b[0] for b in daily_blocks[shift_d]])
    curr_last_end = max([b[1] for b in daily_blocks[shift_d]])
    
    if prev_d in daily_blocks:
        prev_last_end = max([b[1] for b in daily_blocks[prev_d]])
        if (curr_first_start - prev_last_end).total_seconds() / 3600 < 9.0:
            return False, "Less than 9 hour break from previous day"
            
    if next_d in daily_blocks:
        next_first_start = min([b[0] for b in daily_blocks[next_d]])
        if (next_first_start - curr_last_end).total_seconds() / 3600 < 9.0:
            return False, "Less than 9 hour break before next day"
            
    return True, "Valid"

def send_notification(new_shifts, is_test=False):
    """Formats and sends the email containing new shifts."""
    if not new_shifts:
        return

    print(f"[{datetime.now()}] Sending email for {len(new_shifts)} valid new shift(s)...")
    
    shift_blocks = []
    for shift in new_shifts:
        date_obj = datetime.strptime(shift['date'], "%Y-%m-%d")
        day_name = date_obj.strftime("%a")
        date_str = f"{date_obj.month}/{date_obj.day}"
        
        start_time = format_time(shift['start'])
        end_time = format_time(shift['end'])
        
        ot_str = "Y" if shift.get('OT') else "N"
        
        block = f"{day_name} {date_str} | {shift['run']}({shift['route']})\n"
        block += f"{start_time}-{end_time} ({shift['hours']}h) OT:{ot_str}"
        shift_blocks.append(block)

    body = "\n\n".join(shift_blocks)
    body += "\n\ncyride.net/sync/open.html"
    
    msg = MIMEText(body)
    msg['Subject'] = "[TEST] CyRide Ping" if is_test else "New CyRide Shift(s)!"
    msg['From'] = ZOHO_USER
    msg['To'] = RECIPIENT

    try:
        server = smtplib.SMTP(ZOHO_HOST, ZOHO_PORT)
        server.starttls()
        server.login(ZOHO_USER, ZOHO_PASS)
        server.send_message(msg)
        server.quit()
        print(f"[{datetime.now()}] Email sent successfully.")
    except Exception as e:
        print(f"[{datetime.now()}] Error sending email: {e}")

def check_for_shifts():
    """Fetches data, compares with cache, evaluates scheduling rules, and triggers emails."""
    try:
        response = requests.get(URL, timeout=10)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        print(f"[{datetime.now()}] Failed to fetch or parse JSON: {e}")
        return

    cached_data = {}
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r') as f:
                cached_data = json.load(f)
        except Exception:
            cached_data = {}

    cached_shift_ids = set()
    if 'days' in cached_data:
        for day_data in cached_data['days'].values():
            for shift in day_data.get('signups', []):
                cached_shift_ids.add(get_shift_id(shift))

    min_date = None
    max_date = None
    if 'days' in data:
        for day_data in data['days'].values():
            day_date = datetime.strptime(day_data['date'], "%Y-%m-%d").date()
            if not min_date or day_date < min_date:
                min_date = day_date
            if not max_date or day_date > max_date:
                max_date = day_date
    
    ics_week_blocks = []
    tz = pytz.timezone("America/Chicago")
    if min_date and max_date:
        fetch_start = tz.localize(datetime.combine(min_date - timedelta(days=7), datetime.min.time()))
        fetch_end = tz.localize(datetime.combine(max_date + timedelta(days=7), datetime.max.time()))
        ics_week_blocks = fetch_ics_events(fetch_start, fetch_end)

    processed_diffs = []
    valid_shifts_to_notify = []

    if 'days' in data:
        for day_data in data['days'].values():
            for shift in day_data.get('signups', []):
                shift_id = get_shift_id(shift)
                
                if shift_id not in cached_shift_ids:
                    s_dt = parse_datetime(shift['date'], shift['start'])
                    e_dt = parse_datetime(shift['date'], shift['end'])
                    if e_dt <= s_dt:
                        e_dt += timedelta(days=1)
                        
                    calc_hours = round((e_dt - s_dt).total_seconds() / 3600, 2)
                    
                    shift_date_obj = s_dt.date()
                    monday = shift_date_obj - timedelta(days=shift_date_obj.weekday())
                    sunday = monday + timedelta(days=6)
                    
                    week_start = tz.localize(datetime.combine(monday - timedelta(days=1), datetime.min.time()))
                    week_end = tz.localize(datetime.combine(sunday + timedelta(days=1), datetime.max.time()))
                    
                    relevant_blocks = [b for b in ics_week_blocks if b[0] >= week_start and b[1] <= week_end]
                    
                    works, reason = evaluate_shift_rules(shift_date_obj, s_dt, e_dt, relevant_blocks)
                    
                    is_ot = shift.get('overtime', False)
                    
                    diff_record = {
                        "run": shift.get('run', ''),
                        "route": shift.get('route', ''),
                        "start": shift.get('start', ''),
                        "end": shift.get('end', ''),
                        "hours": calc_hours,
                        "OT": is_ot,
                        "worksWithSchedule": works,
                        "failReason": reason
                    }
                    processed_diffs.append(diff_record)
                    
                    if works:
                        shift['hours'] = calc_hours
                        shift['OT'] = is_ot
                        valid_shifts_to_notify.append(shift)

    if processed_diffs:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        diff_filename = f"diff_{timestamp}.json"
        try:
            with open(diff_filename, 'w') as f:
                json.dump(processed_diffs, f, indent=4)
            print(f"[{datetime.now()}] Saved {len(processed_diffs)} new evaluated shifts to {diff_filename}")
        except Exception as e:
            print(f"[{datetime.now()}] Failed to save diff json: {e}")

    if valid_shifts_to_notify and cached_data:
        send_notification(valid_shifts_to_notify)

    with open(CACHE_FILE, 'w') as f:
        json.dump(data, f)

def run_test_ping():
    """Fetches the latest JSON, picks a shift (or creates a mock one), and sends a test email."""
    print(f"[{datetime.now()}] Running test ping...")
    try:
        response = requests.get(URL, timeout=10)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        print(f"[{datetime.now()}] Failed to fetch JSON for test: {e}")
        return
        
    test_shift = None
    if 'days' in data:
        for day_data in data['days'].values():
            if day_data.get('signups'):
                test_shift = day_data['signups'][0].copy()
                test_shift['hours'] = 4.0
                test_shift['OT'] = False
                test_shift['route'] = f"TEST - {test_shift.get('route', 'Unknown')}"
                break
                
    if not test_shift:
        print("No shifts found in JSON. Using mock shift for test...")
        test_shift = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "run": "99",
            "start": "12:00",
            "end": "16:00",
            "hours": 4.0,
            "route": "TEST Route",
            "OT": False
        }
        
    send_notification([test_shift], is_test=True)
    print("Test ping complete. Exiting.")

def start_health_server(port):
    """Starts a dummy web server to keep PaaS providers happy."""
    class HealthHandler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'CyRide Notifier is actively running.')
            
    class QuietServer(socketserver.TCPServer):
        def log_message(self, format, *args):
            pass

    try:
        with QuietServer(("", port), HealthHandler) as httpd:
            print(f"Health server listening on port {port}")
            httpd.serve_forever()
    except Exception as e:
        print(f"Failed to bind web server to port {port}: {e}")

if __name__ == "__main__":
    if "--test" in sys.argv:
        run_test_ping()
        sys.exit(0)

    print(f"Starting CyRide Notifier... checking every {CHECK_INTERVAL} seconds.")
    
    threading.Thread(target=start_health_server, args=(WEB_PORT,), daemon=True).start()

    while True:
        check_for_shifts()
        time.sleep(CHECK_INTERVAL)
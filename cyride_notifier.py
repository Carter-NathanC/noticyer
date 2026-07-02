import os
import time
import json
import requests
import smtplib
import threading
import http.server
import socketserver
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
    """Converts 24h '19:12' into 12h '7:12PM' format."""
    dt = datetime.strptime(time_str, "%H:%M")
    # %I gives 07, lstrip('0') removes leading zero for a cleaner look
    return dt.strftime("%I:%M%p").lstrip('0')

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
            # Overlapping or touching, merge them
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
            # Ignore all-day events (type is date, not datetime)
            if type(event["DTSTART"].dt) is date:
                continue
                
            # Ignore if *Ignore* in description
            desc = event.get('DESCRIPTION', '')
            if desc and "*Ignore*" in desc.to_ical().decode('utf-8'):
                continue
                
            ev_start = event["DTSTART"].dt
            ev_end = event["DTEND"].dt
            
            # Convert to local timezone
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
    # 1. Overlap Check (Cannot work if you already have an event at this time)
    for block in week_blocks:
        if max(shift_start_dt, block[0]) < min(shift_end_dt, block[1]):
            return False, "Overlaps with existing schedule"
    
    # Add proposed shift to blocks and sort them by day
    test_blocks = week_blocks + [(shift_start_dt, shift_end_dt)]
    
    # Group by date
    daily_blocks = {}
    for b_start, b_end in test_blocks:
        b_date = b_start.date()
        if b_date not in daily_blocks:
            daily_blocks[b_date] = []
        daily_blocks[b_date].append((b_start, b_end))
        
    # Rule 1: Max 6 days per week (Mon-Sun)
    if len(daily_blocks) > 6:
        return False, "Cannot work more than 6 days a week"
        
    shift_d = shift_start_dt.date()
    if shift_d in daily_blocks:
        merged_day = merge_blocks(daily_blocks[shift_d])
        
        # Rule 3: Max 10 hours a day
        total_hours = sum((b[1] - b[0]).total_seconds() / 3600 for b in merged_day)
        if total_hours > 10.0:
            return False, f"Exceeds 10 hours a day (Total: {total_hours:.1f}h)"
            
        # Rule 4: Max 16 hours spread
        spread = (merged_day[-1][1] - merged_day[0][0]).total_seconds() / 3600
        if spread > 16.0:
            return False, f"Exceeds 16 hours spread (Spread: {spread:.1f}h)"
            
        # Rule 2: Max 6 hours straight without 30m break
        seq_start = merged_day[0][0]
        seq_end = merged_day[0][1]
        
        if (seq_end - seq_start).total_seconds() / 3600 > 6.0:
            return False, "More than 6 hours straight without a break"
            
        for b_start, b_end in merged_day[1:]:
            gap = (b_start - seq_end).total_seconds() / 3600
            if gap < 0.5:
                # Merge into consecutive block if break is less than 30 mins
                seq_end = max(seq_end, b_end)
            else:
                # Break resets the straight time
                seq_start = b_start
                seq_end = b_end
                
            if (seq_end - seq_start).total_seconds() / 3600 > 6.0:
                return False, "More than 6 hours straight without a half-hour break"
                
    # Rule 5: 9 hour break overnight
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

def send_notification(new_shifts):
    """Formats and sends the email containing new shifts."""
    if not new_shifts:
        return

    print(f"[{datetime.now()}] Sending email for {len(new_shifts)} valid new shift(s)...")
    
    shift_blocks = []
    for shift in new_shifts:
        # Date parsing
        date_obj = datetime.strptime(shift['date'], "%Y-%m-%d")
        day_name = date_obj.strftime("%A")
        date_str = date_obj.strftime("%m/%d/%Y")
        
        # Time parsing
        start_time = format_time(shift['start'])
        end_time = format_time(shift['end'])
        
        # Format overtime output
        ot_str = "Yes" if shift.get('OT') else "No"
        
        # Block assembly based on requested format
        block = f"{day_name} - {date_str}:\n"
        block += f"{shift['run']} ({shift['route']}) | {start_time} - {end_time} ({shift['hours']}h) [OT: {ot_str}]\n"
        block += "https://cyride.net/sync/open.html"
        shift_blocks.append(block)

    body = "\n\n".join(shift_blocks)
    
    # Setup the email
    msg = MIMEText(body)
    msg['Subject'] = "New CyRide Open Shift(s) Available!"
    msg['From'] = ZOHO_USER
    msg['To'] = RECIPIENT

    # Connect to Zoho SMTP and send
    try:
        server = smtplib.SMTP(ZOHO_HOST, ZOHO_PORT)
        server.starttls()
        server.login(ZOHO_USER, ZOHO_PASS)
        server.send_message(msg)
        server.quit()
        print(f"[{datetime.now()}] Email sent successfully.")
    except Exception as e:
        print(f"[{datetime.now()}] Error sending email: {e}")

def
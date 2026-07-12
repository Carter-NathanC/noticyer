# CyRide Shift Notifier

A background service that watches the CyRide open-runs board, checks any newly
opened shift against your Google Calendar and CyRide's scheduling rules, and
texts/emails you a phone-readable summary of the shifts that would actually fit.

## How It Works

1. **Scrape** — polls `https://cyride.net/sync/open.json` every `CHECK_INTERVAL_SECONDS`.
2. **Diff** — compares it against `cache.json` (the last scrape) to find shifts that weren't there before. Placeholder board rows (no start/end/route) are ignored.
3. **Check the calendar** — for each new shift, pulls your Google Calendar (via the ICS private URL) for that week and checks the shift against:
   - No more than 6 hours worked without a 30-minute break.
   - No more than 6 shifts in a week (Monday-Sunday).
   - No more than 10.5 hours worked in a day.
   - At least a 9-hour break overnight (end of one day's last shift to start of the next day's first shift).
   - No more than 16 hours from the start of the first shift to the end of the last shift in a day.
   - No overlap with anything already on the calendar.

   Overnight shifts (end time before start time) are treated as ending the next day.
4. **Notify** — if any new shift fits, emails/texts a summary (date, day of week, start, end, route, run number, hours) via Zoho SMTP, formatted plainly (no emoji/unicode) so it survives an SMS gateway.

## Prerequisites

Python 3.9+.

## Setup

Clone/download the repo, then create and use a virtual environment so
dependencies stay isolated from the rest of your system:

```bash
cd noticyer
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

You'll need to run `source .venv/bin/activate` again in any new terminal
session before running the script by hand.

### Configure `.env`

Copy/edit the `.env` file in the project root:

| Variable | Purpose |
|---|---|
| `RECIPIENT_EMAIL` | Where notifications go — an email address, or your carrier's email-to-SMS gateway (e.g. `5551234567@vtext.com`) for texts. |
| `ZOHO_FROM_EMAIL` / `ZOHO_PASSWORD` | Zoho account used to send. Use an app-specific password if 2FA is on. |
| `ZOHO_SMTP_HOST` / `ZOHO_SMTP_PORT` | Defaults are fine for Zoho. |
| `CYRIDE_JSON_URL` | The board's JSON feed. Defaults to the real one. |
| `CYRIDE_HTML_URL` | The board's human-readable page, linked in every message. Defaults to the real one. |
| `ICS_URL` | Your Google Calendar's **private** ICS URL (Calendar Settings → Integrate calendar → Secret address in iCal format). This is how the script knows what you've already committed to. |
| `WEB_PORT` | Port the HTTP API listens on. |
| `CHECK_INTERVAL_SECONDS` | How often to poll the board. |
| `DISPATCH_PHONE` | Number shown in every message for calling dispatch. Defaults to 515-292-1100. |

`DAILY_DIGEST_TIME` and `SECRET_KEY` are not currently used by the script;
they're safe to ignore or remove.

To exclude a calendar event from the "already busy" check (e.g. a personal
event that doesn't actually block you), put `*Ignore*` in its description.

## Running Manually

```bash
source .venv/bin/activate
python cyride_notifier.py
```

The first run just populates `cache.json` — it won't send a notification
until a *later* run sees something genuinely new, so it doesn't blast you
with every shift already open the first time you start it.

Send yourself a one-off test message (uses a real open shift if one exists,
otherwise a mock one) and exit:

```bash
python cyride_notifier.py --test
```

Run the scheduling-rules self-check any time you touch the rule logic:

```bash
python test_rules.py
```

## HTTP API

While running, the script also serves a small JSON API on `WEB_PORT`
(default `3000`). All endpoints scrape the live board fresh on each call.

| Endpoint | Behavior |
|---|---|
| `GET /getAll` | Scrapes now, returns a JSON array of every real open shift on the board, each evaluated against your schedule (`worksWithSchedule`, `failReason` included). |
| `GET /getNew` | Same, but filtered to shifts not present in the last saved scrape. Read-only — doesn't update the cache, so it won't suppress the next scheduled notification. |
| `GET /getAvalible` | The `/getNew` shifts that pass every scheduling rule. Returns JSON `null` (not `[]`) when nothing currently fits. |
| `GET /update` | Scrapes, saves the new cache, and **always** sends a message — either the new shifts that fit, or a "no open shifts fit your schedule" message if none do. Returns `sent \n{message}` with the exact message that was sent. |
| `GET /updateAll` | Same, but the message lists **every** open shift on the board (not just new ones, not filtered by fit). Returns `sent \n{message}`. |

Every outbound message ends with the dispatch phone number and a link to the
open-runs page.

```bash
curl http://localhost:3000/getAll
curl http://localhost:3000/getNew
curl http://localhost:3000/getAvalible
curl http://localhost:3000/update
curl http://localhost:3000/updateAll
```

## Setting up to Start on Boot

### Linux (systemd)

Running via systemd ensures it restarts automatically on crashes and runs in
the background. Point `ExecStart` at the **venv's** Python interpreter, not
the system one, so it can see the installed dependencies — systemd doesn't
run your shell's `activate` script for you.

```bash
sudo nano /etc/systemd/system/cyride-notifier.service
```

```ini
[Unit]
Description=CyRide Shift Notifier
After=network.target

[Service]
User=your_username
WorkingDirectory=/path/to/your/project/folder
ExecStart=/path/to/your/project/folder/.venv/bin/python3 /path/to/your/project/folder/cyride_notifier.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable cyride-notifier.service
sudo systemctl start cyride-notifier.service
```

View logs anytime: `sudo journalctl -u cyride-notifier.service -f`

### Windows (Task Scheduler)

1. Open Task Scheduler → **Create Basic Task...**
2. Name it "CyRide Notifier", trigger: **When the computer starts**.
3. Action: **Start a program**.
4. Program/script: the `python.exe` inside your venv, e.g. `C:\path\to\project\.venv\Scripts\python.exe`.
5. Add arguments: the full path to `cyride_notifier.py`, e.g. `C:\Users\Name\Desktop\noticyer\cyride_notifier.py`.
6. Start in: the project directory, e.g. `C:\Users\Name\Desktop\noticyer\`.
7. Finish and save.

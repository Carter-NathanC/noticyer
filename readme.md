# CyRide Shift Notifier

A lightweight background service designed to automatically poll the CyRide schedule server, detect newly opened shifts, and notify you instantly via email/SMS.

## How It Works

The script continuously pulls schedule data from https://cyride.net/sync/open.json at a predefined interval. It compares the downloaded data against a locally stored cache.json file. If new shifts are detected in the signups array, it parses the shift details, structures them into a human-readable format, and uses Zoho SMTP to fire off an alert to your configured address (e.g., your carrier's text-to-SMS gateway).

##Prerequisites

Python 3.7+ installed on your machine or server.

Installation & Setup

Clone/Download the repository to your desired directory.

Install Required Libraries:

`pip install -r requirements.txt`


Configure Environment Variables:
Open the .env file and ensure your email credentials and preferences are correct. (Make sure you use an App-Specific Password for Zoho if you have 2FA enabled on the account).

## Running the Script Manually

To run the script in your terminal:

`python cyride_notifier.py`


(The very first time it runs, it will simply populate cache.json and won't send an email until the next cycle detects a completely new shift).

## Setting up to Start on Boot

Linux (Using Systemd)

Running this via systemd ensures it restarts automatically on crashes and runs in the background.

Create a service file:

`sudo nano /etc/systemd/system/cyride-notifier.service`


Paste the following configuration (Modify User, WorkingDirectory, and ExecStart paths to match your setup):
```
[Unit]
Description=CyRide Shift Notifier
After=network.target

[Service]
User=your_username
WorkingDirectory=/path/to/your/project/folder
ExecStart=/usr/bin/python3 /path/to/your/project/folder/cyride_notifier.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start the service:
```
sudo systemctl daemon-reload
sudo systemctl enable cyride-notifier.service
sudo systemctl start cyride-notifier.service
```

View logs anytime: sudo journalctl -u cyride-notifier.service -f

## Windows (Using Task Scheduler)

Open Task Scheduler and click Create Basic Task...

Name it "CyRide Notifier" and set the trigger to When the computer starts.

For Action, select Start a program.

Point the "Program/script" to your python.exe path.

In "Add arguments", enter the full path to cyride_notifier.py (e.g., C:\Users\Name\Desktop\cyride_notifier.py).

In "Start in", enter the directory of the script (e.g., C:\Users\Name\Desktop\).

Finish and save.
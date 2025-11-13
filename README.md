# Vinterbad Monitor ❄️🔥

Automatically monitors Vinterbadbryggen events and sends email alerts when new gus sauna slots become available.

This project runs fully automatically via GitHub Actions, checking for new events every few minutes. It filters for only real gus events (e.g., Morgengus, Lørdag Gus, Typisk torsdag gus, etc.) and ignores unrelated activities such as Kom & syng or Glemt tøj.

## 🚀 Features

- Monitors Vinterbadbryggen API for new events
- Filters to include only gus sauna events
- Avoids duplicate notifications using a persistent seen_events.json
- Sends clean HTML + text email alerts via Gmail SMTP
- Fully automated using GitHub Actions
- Supports manual test email triggers using workflow_dispatch.

## 📸 Example Alert

Emails look like this:
- Subject: New Vinterbad Slot Available!
- Includes event name, time, availability, and direct booking link
- Includes timestamp in Copenhagen local time.

## ⚙️ How it Works
### 1. GitHub Actions runs the monitor
The workflow (.github/workflows/vinterbad.yml) runs every ~5 minutes:
```
on:
  schedule:
    - cron: "*/5 * * * *"
```
It installs Python, reads your secrets, runs monitor.py, and commits updates to seen_events.json.

### 2. monitor.py logic

- Fetches events from
https://www.vinterbadbryggen.com/api/activity/event/days

- Normalizes the API response (the API has many shapes…)

- Extracts stable unique IDs for events

- Filters out non-gus events

- Detects newly added events

- Sends email alerts for newly bookable ones

- Saves seen events

### 3. Secrets required

The Action needs these secrets:

| Secret Name              | Description                                  |
| ------------------------ | -------------------------------------------- |
| `VINTERBAD_EMAIL`        | Gmail address used to send alerts            |
| `VINTERBAD_APP_PASSWORD` | Gmail App Password (not your real password!) |
| `RECIPIENT_EMAILS`       | Comma-separated list of recipients           |


Example:

someone@gmail.com

or for multiple:

someone@gmail.com, someone@else.com

### 4. Optional: Manual Test Email

Trigger the workflow manually and set:
```
test_send: true
```

This bypasses the API and just sends a test email.

## 🧪 Local Development

To run locally:

Install dependencies:
```
pip install -r requirements.txt
```

Disable email sending (optional):
```
export VINTERBAD_EMAIL_ENABLED=false
```

Run:
```
python monitor.py
```

The monitor will fetch events, update seen_events.json, and print debug logs.

📁 Repository Structure

```text
.
├── monitor.py            # Main monitor script
├── seen_events.json      # Auto-persisted list of seen event IDs
├── requirements.txt      # Python dependencies
└── .github/
    └── workflows/
        └── vinterbad.yml # GitHub Actions automation
```


## 🛟 Troubleshooting

### No emails being sent?
- Ensure VINTERBAD_EMAIL_ENABLED=true
- Ensure Gmail App Password is correct
- Check Gmail security settings
- Confirm your RECIPIENT_EMAILS is non-empty

### Events not being detected?
- Check logs for API changes (use LOG_LEVEL=DEBUG)
- Ensure time range (LOOKAHEAD_DAYS) is large enough
- Verify that events have “gus” in their name or location

### seen_events.json not updating?
- Confirm GitHub Action has contents: write permission
- Check if the job prints newly seen: 0

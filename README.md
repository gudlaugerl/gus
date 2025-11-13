# Gus Monitor ❄️🔥
This repository contains a small Python-based monitoring tool that periodically checks a public JSON API and notifies the user when new items appear.
It is designed for personal automation and runs on GitHub Actions at a configured interval.

## 🚀 Features
- Periodic polling via GitHub Actions
- Email notifications (optional, via environment variables)
- State persisted in a small JSON file
- Customizable API endpoint and parameters
- Safe to run locally or in CI environments

## Structure
```
monitor.py          # Main monitoring script
seen_events.json    # Stores previously processed item IDs
.github/workflows   # Automation workflow configuration
requirements.txt    # Dependencies
```

## Usage
### Local 
```
python monitor.py
```
Configure behavior via environment variables:

- ```API_URL```
- ```EMAIL```
- ```APP_PASSWORD```
- ```RECIPIENT_EMAILS```
- ```etc.```

## GitHub Actions
The workflow runs the script on a schedule and persists the state file.
Secrets are injected via repository settings.

## Purpose
This project exists for personal automation/notification purposes.
It is not intended for wider usage or distribution.

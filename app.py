import os
from flask import Flask, jsonify
from monitor import VinterbadAlertMonitor  # your current file

app = Flask(__name__)

@app.get("/run")
def run_monitor():
    mon = VinterbadAlertMonitor()
    sent = mon.run_once()
    return jsonify({"status": "ok", "alerts_sent": sent})

@app.get("/test")
def test_email():
    os.environ["VINTERBAD_TEST_SEND"] = "true"
    mon = VinterbadAlertMonitor()
    # You already have the test path inside monitor.py
    # Just call run_once() which will early-exit via the test flag or send a dummy email
    mon.run_once()
    return jsonify({"status": "ok", "test": True})

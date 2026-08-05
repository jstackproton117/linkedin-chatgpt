"""
Rebel Intel — Daily notification script.
Run once per day via Windows Task Scheduler.

Sends up to 3 types of email:
  1. Day-before reminder for each post scheduled tomorrow
  2. Pipeline warning: last scheduled post is in exactly 7 days
  3. Pipeline warning: last scheduled post is tomorrow (pipeline will be empty)
"""

import json
import smtplib
import ssl
from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

BASE        = Path(__file__).parent
POST_LOG    = BASE / "data" / "post_log.json"
CONFIG_FILE = BASE / "notify_config.json"


def load_config():
    if not CONFIG_FILE.exists():
        print(f"ERROR: {CONFIG_FILE} not found.")
        print("Create it using notify_config.example.json as a template.")
        return None
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def load_post_log():
    if not POST_LOG.exists():
        return []
    with open(POST_LOG, encoding="utf-8") as f:
        return json.load(f)


def send_email(config, subject, body_html):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = config["from_email"]
    msg["To"]      = config["to_email"]
    msg.attach(MIMEText(body_html, "html"))

    context = ssl.create_default_context()
    with smtplib.SMTP(config["smtp_host"], config["smtp_port"]) as server:
        server.ehlo()
        server.starttls(context=context)
        server.login(config["from_email"], config["app_password"])
        server.sendmail(config["from_email"], config["to_email"], msg.as_string())


def card(title, body, cta_label, cta_url, cta_color="#D4941E", cta_text="#000"):
    return f"""
    <div style="font-family:'Courier New',monospace;background:#06090F;color:#CCC4B0;
                padding:28px;max-width:580px;border:1px solid #1C3248;">
      <div style="font-size:20px;color:#D4941E;letter-spacing:4px;margin-bottom:4px;">
        &#9889; REBEL INTEL
      </div>
      <div style="font-size:10px;color:#607080;letter-spacing:3px;margin-bottom:24px;">
        CONTENT PIPELINE
      </div>
      <div style="font-size:15px;line-height:1.6;margin-bottom:16px;">{title}</div>
      <div style="font-size:13px;color:#607080;margin-bottom:24px;">{body}</div>
      <a href="{cta_url}"
         style="background:{cta_color};color:{cta_text};padding:11px 22px;
                text-decoration:none;font-weight:bold;font-size:11px;
                letter-spacing:2px;display:inline-block;">
        {cta_label}
      </a>
    </div>"""


def main():
    config = load_config()
    if not config:
        return

    post_log = load_post_log()
    today    = date.today()
    tomorrow = today + timedelta(days=1)

    dashboard = config.get("dashboard_url", "http://localhost:5000")
    linkedin  = "https://www.linkedin.com"

    def post_date(p):
        try:
            return date.fromisoformat(p["scheduled_for"][:10])
        except Exception:
            return date.max

    # Scheduled posts not yet published
    scheduled = sorted(
        [p for p in post_log if p.get("scheduled_for") and not p.get("published_at")],
        key=post_date
    )

    sent = 0

    # ── Type 1: day-before reminder per post ──────────────────────────────────
    for post in scheduled:
        if post_date(post) == tomorrow:
            title = post.get("title", "LinkedIn Post")
            short = title[:90] + ("…" if len(title) > 90 else "")
            body_html = f"""
            {card(
                title=f'You have a post going out on LinkedIn <strong style="color:#D4941E;">tomorrow</strong>.',
                body=f'<div style="background:#080C14;border:1px solid #1C3248;padding:12px;margin-bottom:8px;">{short}</div>',
                cta_label="OPEN LINKEDIN →",
                cta_url=linkedin
            )}
            <div style="margin-top:8px;">
              <a href="{dashboard}/post-log"
                 style="border:1px solid #2EB8CC;color:#2EB8CC;padding:10px 20px;
                        text-decoration:none;font-size:11px;letter-spacing:2px;display:inline-block;">
                VIEW DASHBOARD →
              </a>
            </div>"""
            send_email(config, "⚡ Post scheduled for tomorrow — Rebel Intel", body_html)
            print(f"[OK] Day-before reminder: {short[:60]}")
            sent += 1

    # ── Types 2 & 3: pipeline-tail warnings ───────────────────────────────────
    if scheduled:
        last      = scheduled[-1]
        last_date = post_date(last)
        days_out  = (last_date - today).days

        if days_out == 7:
            body_html = card(
                title='Your <strong style="color:#D4941E;">last scheduled post</strong> goes out in <strong style="color:#D4941E;">one week</strong>. The pipeline runs dry after that.',
                body="Time to fetch articles and build the next batch so you don't lose momentum.",
                cta_label="OPEN DASHBOARD →",
                cta_url=dashboard,
                cta_color="#2EB8CC",
                cta_text="#000"
            )
            send_email(config, "⚡ Pipeline runs dry in 1 week — Rebel Intel", body_html)
            print("[OK] Pipeline 1-week warning")
            sent += 1

        elif days_out == 1:
            body_html = card(
                title='&#128308; Your <strong style="color:#C43030;">last scheduled post</strong> goes out <strong style="color:#C43030;">tomorrow</strong>. Nothing scheduled after that.',
                body="Fetch articles now to avoid a gap in your posting cadence.",
                cta_label="OPEN DASHBOARD NOW →",
                cta_url=dashboard,
                cta_color="#C43030",
                cta_text="#fff"
            )
            send_email(config, "🔴 Pipeline empty after tomorrow — Rebel Intel", body_html)
            print("[OK] Pipeline critical warning")
            sent += 1

    if sent == 0:
        print("Nothing to notify today.")
    else:
        print(f"Done — {sent} email(s) sent.")


if __name__ == "__main__":
    main()

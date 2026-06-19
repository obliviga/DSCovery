"""
Standalone script that runs all job importers and emails new front-end
engineering jobs.  Designed to run in GitHub Actions on a schedule.

State is kept in a small JSON file (seen_jobs.json) so we only alert on
jobs we haven't seen before.  The file is committed back to the repo by
the workflow so it persists between runs.
"""

import datetime
import importlib.util
import json
import os
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# Minimal Django setup — importers only need settings.IMPORTER_HEADERS
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "DSCovery.settings")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import django
django.setup()

SEEN_JOBS_FILE = Path(__file__).resolve().parent / "seen_jobs.json"
IMPORTERS_DIR = Path(__file__).resolve().parent / "jobsearch" / "importers"
KEYWORD = "front"


def load_seen_jobs():
    if SEEN_JOBS_FILE.exists():
        return set(json.loads(SEEN_JOBS_FILE.read_text()))
    return set()


def save_seen_jobs(seen):
    SEEN_JOBS_FILE.write_text(json.dumps(sorted(seen), indent=2))


def make_job_key(job):
    return f"{job['company']}|{job['title']}|{job.get('job_id', '')}"


def run_importers():
    all_jobs = []
    for file_name in sorted(os.listdir(IMPORTERS_DIR)):
        if not file_name.endswith(".py") or file_name in ("__init__.py", "utils.py"):
            continue
        module_name = file_name[:-3]
        module_path = IMPORTERS_DIR / file_name
        try:
            spec = importlib.util.spec_from_file_location(module_name, str(module_path))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            jobs = module.get_jobs()
            if jobs:
                all_jobs.extend(jobs)
                print(f"  {module_name}: {len(jobs)} jobs")
            else:
                print(f"  {module_name}: 0 jobs")
        except Exception as e:
            print(f"  {module_name}: FAILED ({e})")
    return all_jobs


def filter_frontend_jobs(jobs):
    return [j for j in jobs if KEYWORD in j["title"].lower()]


def send_email(jobs):
    smtp_user = os.environ.get("SMTP_USER", "").strip()
    smtp_pass = os.environ.get("SMTP_PASS", "").strip()
    to_email = os.environ.get("ALERT_EMAIL", "").strip() or smtp_user

    if not smtp_user or not smtp_pass:
        raise RuntimeError(
            "SMTP_USER and SMTP_PASS must be set as GitHub repo secrets. "
            f"SMTP_USER={'set' if smtp_user else 'EMPTY'}, "
            f"SMTP_PASS={'set' if smtp_pass else 'EMPTY'}"
        )
    if not to_email:
        raise RuntimeError("No recipient email: set ALERT_EMAIL or SMTP_USER")

    subject = f"🚀 {len(jobs)} new front-end job{'s' if len(jobs) != 1 else ''} on DSCovery"

    rows = ""
    for job in jobs:
        rows += (
            f"<tr>"
            f"<td style='padding:6px 12px'><a href='{job['link']}'>{job['title']}</a></td>"
            f"<td style='padding:6px 12px'>{job['company']}</td>"
            f"<td style='padding:6px 12px'>{job['location']}</td>"
            f"</tr>\n"
        )

    html = f"""\
<html><body>
<h2>{subject}</h2>
<table border="1" cellspacing="0" style="border-collapse:collapse">
<tr style="background:#f0f0f0">
  <th style="padding:6px 12px">Title</th>
  <th style="padding:6px 12px">Company</th>
  <th style="padding:6px 12px">Location</th>
</tr>
{rows}
</table>
<p style="color:#888;font-size:12px">Sent by your DSCovery job alert</p>
</body></html>"""

    plain = "\n".join(
        f"- {j['title']} at {j['company']} ({j['location']})\n  {j['link']}"
        for j in jobs
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = to_email
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, to_email, msg.as_string())

    print(f"Email sent to {to_email}")


def main():
    print("Running importers...")
    all_jobs = run_importers()
    print(f"\nTotal jobs collected: {len(all_jobs)}")

    frontend_jobs = filter_frontend_jobs(all_jobs)
    print(f"Jobs matching '{KEYWORD}': {len(frontend_jobs)}")

    seen = load_seen_jobs()
    new_jobs = [j for j in frontend_jobs if make_job_key(j) not in seen]
    print(f"New (unseen) matches: {len(new_jobs)}")

    if new_jobs:
        send_email(new_jobs)
        seen.update(make_job_key(j) for j in new_jobs)
    else:
        print("No new jobs to alert on.")

    # Always update seen set with ALL current frontend jobs so we don't
    # re-alert if a job disappears and reappears.
    seen.update(make_job_key(j) for j in frontend_jobs)
    save_seen_jobs(seen)


if __name__ == "__main__":
    main()

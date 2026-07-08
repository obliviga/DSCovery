"""
DSCovery job alert.

Runs every importer, finds jobs whose title contains any of KEYWORDS
(loose, case-insensitive substring match), and emails them via Gmail.

Two modes, selected with the ALERT_MODE env var (default "regular"):

  regular  – email only newly-discovered matches since the last run
  weekly   – ALSO email a full digest of every currently-open match

State (which jobs we've already alerted on) lives in seen_jobs.json,
which the GitHub Actions workflow commits back to the repo so it
persists between runs.

Environment variables:
  SMTP_USER    Gmail address to send from            (required to send)
  SMTP_PASS    Gmail app password                    (required to send)
  ALERT_EMAIL  recipient; defaults to SMTP_USER       (optional)
  ALERT_MODE   "regular" | "weekly"                   (default "regular")
  DRY_RUN      if set, print emails instead of sending (optional)
"""

import datetime
import html
import importlib.util
import json
import os
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# The importers only need Django's settings (for IMPORTER_HEADERS), so a
# minimal django.setup() is enough — no server, no database queries.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "DSCovery.settings")
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import django  # noqa: E402

django.setup()

SEEN_JOBS_FILE = BASE_DIR / "seen_jobs.json"
IMPORTERS_DIR = BASE_DIR / "jobsearch" / "importers"

# Loose, case-insensitive substring match on the job title.
KEYWORDS = ["front", "accessibility"]

# Only surface remote roles. Hybrid / on-site roles are dropped UNLESS the
# location names one of these cities.
ALLOWED_CITIES = ["los angeles"]

# Forget jobs we haven't seen for this long, so the state file can't grow
# forever. A job gone this long is effectively a brand-new posting if it
# ever comes back.
STATE_RETENTION_DAYS = 90


# --------------------------------------------------------------------------- #
# Scraping
# --------------------------------------------------------------------------- #

def run_importers():
    """Run every importer module. Returns (jobs, failures).

    A single importer blowing up never aborts the run — it's recorded in
    `failures` and reported, so scrape breakage is visible instead of
    silently costing us jobs.
    """
    all_jobs = []
    failures = []
    for file_name in sorted(os.listdir(IMPORTERS_DIR)):
        if not file_name.endswith(".py") or file_name in ("__init__.py", "utils.py"):
            continue
        module_name = file_name[:-3]
        module_path = IMPORTERS_DIR / file_name
        try:
            spec = importlib.util.spec_from_file_location(module_name, str(module_path))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            jobs = module.get_jobs() or []
            for job in jobs:
                job.setdefault("location", "")
                job.setdefault("link", "")
                job["_source"] = module_name
            all_jobs.extend(jobs)
            print(f"  {module_name}: {len(jobs)} jobs")
        except Exception as e:  # noqa: BLE001 — one importer must not kill the rest
            failures.append((module_name, str(e)))
            print(f"  {module_name}: FAILED ({e})")
    return all_jobs, failures


# --------------------------------------------------------------------------- #
# Matching + de-duplication
# --------------------------------------------------------------------------- #

def _norm(value):
    return " ".join((value or "").split()).strip().lower()


def title_matches(title):
    lowered = (title or "").lower()
    return any(keyword in lowered for keyword in KEYWORDS)


def location_ok(location):
    """Keep remote roles, plus anything in an allowed city (e.g. Los Angeles).

    Hybrid / on-site roles are dropped. "Remote" appears in every remote
    variant we see ("Remote", "United States - Remote", "Remote (US)",
    "Washington D.C. or Remote", ...); hybrid/on-site strings never contain it,
    so a simple substring test is both sufficient and conservative.
    """
    loc = (location or "").lower()
    if any(city in loc for city in ALLOWED_CITIES):
        return True
    return "remote" in loc


def dedup_key(job):
    """One logical role — stable no matter which board or id it came from."""
    return f"{_norm(job.get('company'))}|{_norm(job.get('title'))}"


def state_key(job):
    """Identity used to remember we've alerted on a job.

    Includes job_id so a genuinely re-posted role (new id) alerts again,
    while the same open posting stays quiet run after run.
    """
    return f"{_norm(job.get('company'))}|{_norm(job.get('title'))}|{_norm(job.get('job_id'))}"


def matching_jobs(jobs):
    return [
        job for job in jobs
        if title_matches(job.get("title")) and location_ok(job.get("location"))
    ]


def dedup(jobs):
    """Collapse to one job per (company, title), sorted for stable output."""
    unique = {}
    for job in jobs:
        unique.setdefault(dedup_key(job), job)
    return sorted(
        unique.values(),
        key=lambda j: (_norm(j.get("company")), _norm(j.get("title"))),
    )


# --------------------------------------------------------------------------- #
# State (seen_jobs.json)
# --------------------------------------------------------------------------- #

def load_state():
    """Return {state_key: last_seen_iso_date}. Migrates the old list format."""
    if not SEEN_JOBS_FILE.exists():
        return {}
    raw = SEEN_JOBS_FILE.read_text().strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # A corrupt state file must never stop alerts forever — treat it as
        # empty and let this run rewrite a clean one.
        print("seen_jobs.json is corrupt; starting from empty state.")
        return {}
    if isinstance(data, list):  # legacy format: a plain list of keys
        today = datetime.date.today().isoformat()
        # Normalize legacy keys to the current state_key form, otherwise every
        # already-seen job looks new right after the format upgrade.
        return {"|".join(_norm(part) for part in key.split("|")): today for key in data}
    return data


def save_state(state):
    SEEN_JOBS_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def prune_state(state):
    cutoff = datetime.date.today() - datetime.timedelta(days=STATE_RETENTION_DAYS)
    pruned = {}
    for key, last_seen in state.items():
        try:
            seen_date = datetime.date.fromisoformat(last_seen)
        except (ValueError, TypeError):
            seen_date = datetime.date.today()
        if seen_date >= cutoff:
            pruned[key] = last_seen
    return pruned


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #

def _smtp_config():
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
    return smtp_user, smtp_pass, to_email


def send_jobs_email(subject, intro, jobs, failures=None):
    smtp_user, smtp_pass, to_email = _smtp_config()

    rows = ""
    for job in jobs:
        title = html.escape(job.get("title", "") or "(no title)")
        company = html.escape(job.get("company", "") or "—")
        location = html.escape(job.get("location", "") or "—")
        link = html.escape(job.get("link", "") or "#", quote=True)
        rows += (
            "<tr>"
            f"<td style='padding:6px 12px'><a href=\"{link}\">{title}</a></td>"
            f"<td style='padding:6px 12px'>{company}</td>"
            f"<td style='padding:6px 12px'>{location}</td>"
            "</tr>\n"
        )

    failure_note = ""
    if failures:
        names = ", ".join(html.escape(name) for name, _ in failures)
        failure_note = (
            f"<p style='color:#b00;font-size:12px'>⚠️ {len(failures)} importer(s) "
            f"failed to run this time ({names}); their jobs may be missing.</p>"
        )

    body_html = f"""\
<html><body style="font-family:sans-serif">
<p>{html.escape(intro)}</p>
<table cellspacing="0" style="border-collapse:collapse;border:1px solid #ddd">
<tr style="background:#f0f0f0;text-align:left">
  <th style="padding:6px 12px">Title</th>
  <th style="padding:6px 12px">Company</th>
  <th style="padding:6px 12px">Location</th>
</tr>
{rows}</table>
{failure_note}
<p style="color:#888;font-size:12px">Sent by your DSCovery job alert.</p>
</body></html>"""

    plain_lines = [intro, ""]
    for job in jobs:
        plain_lines.append(
            f"- {job.get('title') or '(no title)'} — {job.get('company') or '—'} "
            f"({job.get('location') or '—'})\n  {job.get('link', '')}"
        )
    if failures:
        plain_lines.append("")
        plain_lines.append(
            f"WARNING: {len(failures)} importer(s) failed: "
            + ", ".join(name for name, _ in failures)
        )
    body_plain = "\n".join(plain_lines)

    if os.environ.get("DRY_RUN"):
        print("\n===== DRY RUN EMAIL =====")
        print(f"To:      {to_email}")
        print(f"Subject: {subject}")
        print(body_plain)
        print("=========================\n")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = to_email
    msg.attach(MIMEText(body_plain, "plain"))
    msg.attach(MIMEText(body_html, "html"))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, to_email, msg.as_string())

    print(f"Email sent to {to_email}: {subject}")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    mode = os.environ.get("ALERT_MODE", "regular").strip().lower()
    print(f"Mode: {mode}")
    print("Running importers...")
    all_jobs, failures = run_importers()
    print(f"\nTotal jobs collected: {len(all_jobs)}")
    if failures:
        print(f"⚠️  {len(failures)} importer(s) failed: {[name for name, _ in failures]}")

    matches = matching_jobs(all_jobs)
    open_matches = dedup(matches)
    print(f"Open matches for {KEYWORDS}: {len(open_matches)}")

    state = load_state()
    new_matches = dedup([job for job in matches if state_key(job) not in state])
    print(f"New matches since last run: {len(new_matches)}")

    if new_matches:
        plural = "s" if len(new_matches) != 1 else ""
        send_jobs_email(
            subject=f"🔔 {len(new_matches)} new job{plural} matching your keywords",
            intro="New job postings with “front” or “accessibility” in the title:",
            jobs=new_matches,
        )
    else:
        print("No new matches to alert on.")

    # Persist state now: after the new-match alert (so a send failure there
    # re-alerts next run rather than silently dropping a job) but before the
    # optional digest (so a digest failure can't lose what we just recorded).
    # We remember every match we saw this run, not just the ones we emailed,
    # so duplicates under other ids don't re-alert later.
    today = datetime.date.today().isoformat()
    for job in matches:
        state[state_key(job)] = today
    save_state(prune_state(state))

    if mode == "weekly":
        plural = "s" if len(open_matches) != 1 else ""
        try:
            send_jobs_email(
                subject=f"📋 Weekly digest: {len(open_matches)} open job{plural} matching your keywords",
                intro="Every currently-open job with “front” or “accessibility” in the title:",
                jobs=open_matches,
                failures=failures,
            )
        except Exception as e:  # noqa: BLE001 — digest is best-effort; state is already saved
            print(f"Weekly digest failed to send: {e}")


if __name__ == "__main__":
    main()

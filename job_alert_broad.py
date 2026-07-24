"""
Broad job alert.

Emails remote, US-eligible front-end / UI / accessibility roles from companies
OUTSIDE the curated civic-tech importer list, using free remote-job APIs
(Remotive, RemoteOK, Jobicy).

This is deliberately separate from job_alert.py — which covers the civic-tech
company boards — and keeps its own state file (seen_jobs_broad.json), so the
two never interfere. It is always new-only: it emails only newly-seen matches
and has no weekly digest.

Heads up: the free APIs are small and skew non-US, so on any given run this may
find nothing. That's expected; new-only means no email is sent when there's
nothing new.

Environment variables:
  SMTP_USER    Gmail address to send from            (required to send)
  SMTP_PASS    Gmail app password                    (required to send)
  ALERT_EMAIL  recipient; defaults to SMTP_USER       (optional)
  SECRET_KEY   needed only because we reuse job_alert (Django settings)
  DRY_RUN      if set, print emails instead of sending (optional)
"""

import datetime
import importlib.util
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Reuse the civic script's email sender and normalizer. Importing job_alert
# runs its django.setup() (needs SECRET_KEY) but does NOT change its behaviour.
from job_alert import send_jobs_email, _norm

BASE_DIR = Path(__file__).resolve().parent
IMPORTERS_DIR = BASE_DIR / "jobsearch" / "importers"
SEEN_FILE = BASE_DIR / "seen_jobs_broad.json"
STATE_RETENTION_DAYS = 90

HTTP_TIMEOUT = 30
USER_AGENT = "Mozilla/5.0 (DSCovery job alert)"

# Phrase-level title match (not bare "front"/"ui") so we don't hit "front desk"
# or "requirements". Covers Front End / Frontend / Front-End Engineer|Developer,
# Accessibility roles, and UI Engineer / UI Developer.
ROLE_PHRASES = [
    "front end", "frontend", "front-end",
    "accessibility",
    "ui engineer", "ui developer",
]

# Location classification. Aggregator location fields are free-form, so we
# match explicit US signals (word-bounded — "us"/"u.s." must not fire inside
# "business" or "belarus"), a set of foreign regions, and generic global
# signals ("worldwide"/"anywhere"/bare "remote").
_US_RE = re.compile(
    r"\busa\b|\bus\b|\bu\.s\.?a?\.?|united states|north america|\bamericas\b"
    r"|nationwide|\bus[- ]based|\bus[- ]only|\bus[- ]remote|remote[- ]us\b|🇺🇸"
)
_GLOBAL_RE = re.compile(r"\b(worldwide|anywhere|global|remote)\b")
FOREIGN_HINTS = [
    "canada", "brazil", "brasil", "india", "europe", "emea", "apac",
    "united kingdom", " uk", "germany", "france", "spain", "portugal",
    "poland", "peru", "perú", "mexico", "méxico", "argentina", "colombia",
    "uruguay", "chile", "latam", "latin america", "south america",
    "australia", "toronto", "ontario", "london", "madrid", "porto", "saudi",
    "nigeria", "philippines", "indonesia", "vietnam", "ukraine", "belarus",
    "netherlands", "ireland", "israel", "singapore", "japan", "africa",
]


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _job(company, title, link, location, source, ident):
    return {
        "company": (company or "").strip() or "Unknown",
        "title": (title or "").strip(),
        "link": link or "",
        "location": (location or "").strip(),
        "source": source,
        "job_id": f"{source}:{ident}",
    }


def fetch_remotive():
    jobs = []
    try:
        data = _get_json("https://remotive.com/api/remote-jobs")
        for j in data.get("jobs", []):
            jobs.append(_job(
                j.get("company_name"), j.get("title"), j.get("url"),
                j.get("candidate_required_location"), "remotive", j.get("id"),
            ))
    except Exception as e:  # noqa: BLE001 — one source down must not kill the run
        print(f"  remotive: FAILED ({e})")
    return jobs


def fetch_remoteok():
    jobs = []
    seen_ids = set()
    for url in (
        "https://remoteok.com/api?tags=front-end",
        "https://remoteok.com/api?tags=ui",
        "https://remoteok.com/api",
    ):
        try:
            data = _get_json(url)
            for x in data:
                if not isinstance(x, dict) or not x.get("position"):
                    continue
                if x.get("id") in seen_ids:
                    continue
                seen_ids.add(x.get("id"))
                jobs.append(_job(
                    x.get("company"), x.get("position"),
                    x.get("url") or x.get("apply_url"),
                    x.get("location"), "remoteok", x.get("id"),
                ))
        except Exception as e:  # noqa: BLE001
            print(f"  remoteok ({url}): FAILED ({e})")
    return jobs


def fetch_jobicy():
    jobs = []
    try:
        data = _get_json("https://jobicy.com/api/v2/remote-jobs?count=100&geo=usa")
        for j in data.get("jobs", []):
            jobs.append(_job(
                j.get("companyName"), j.get("jobTitle"), j.get("url"),
                j.get("jobGeo"), "jobicy", j.get("id"),
            ))
    except Exception as e:  # noqa: BLE001
        print(f"  jobicy: FAILED ({e})")
    return jobs


# Adzuna searches (broad coverage). Requires free credentials — set the
# ADZUNA_APP_ID / ADZUNA_APP_KEY secrets; without them this source is skipped.
ADZUNA_QUERIES = [
    "front end engineer", "front end developer", "frontend developer",
    "accessibility engineer", "ui engineer",
]


def fetch_adzuna():
    app_id = os.environ.get("ADZUNA_APP_ID", "").strip()
    app_key = os.environ.get("ADZUNA_APP_KEY", "").strip()
    if not app_id or not app_key:
        print("  adzuna: skipped (no ADZUNA_APP_ID / ADZUNA_APP_KEY)")
        return []

    jobs = []
    seen_ids = set()
    for term in ADZUNA_QUERIES:
        params = urllib.parse.urlencode({
            "app_id": app_id,
            "app_key": app_key,
            "results_per_page": 50,
            "what": term,
            "where": "remote",
            "max_days_old": 30,
            "content-type": "application/json",
        })
        try:
            data = _get_json(f"https://api.adzuna.com/v1/api/jobs/us/search/1?{params}")
        except Exception as e:  # noqa: BLE001 — one query failing must not kill the rest
            print(f"  adzuna ({term}): FAILED ({e})")
            continue
        for r in data.get("results", []):
            rid = r.get("id")
            if rid in seen_ids:
                continue
            seen_ids.add(rid)
            title = re.sub(r"<[^>]+>", "", r.get("title") or "")
            company = re.sub(r"<[^>]+>", "", (r.get("company") or {}).get("display_name") or "")
            place = (r.get("location") or {}).get("display_name") or ""
            # Adzuna's US index isn't remote-only; require an explicit remote
            # signal in the title/location/description before including.
            blob = f"{title} {place} {r.get('description') or ''}".lower()
            if "remote" not in blob:
                continue
            jobs.append(_job(company, title, r.get("redirect_url"),
                             "Remote, US", "adzuna", rid))
    return jobs


def fetch_all():
    jobs = []
    for name, fetch in (("remotive", fetch_remotive),
                        ("remoteok", fetch_remoteok),
                        ("jobicy", fetch_jobicy),
                        ("adzuna", fetch_adzuna)):
        got = fetch()
        print(f"  {name}: {len(got)} candidates")
        jobs.extend(got)
    return jobs


# --------------------------------------------------------------------------- #
# Filtering
# --------------------------------------------------------------------------- #

def role_matches(title):
    lowered = " ".join((title or "").lower().split())
    return any(phrase in lowered for phrase in ROLE_PHRASES)


def us_remote_ok(location):
    """Keep US-eligible remote roles; drop clearly foreign-only ones.

    Order matters: an explicit US signal ("USA, Canada") keeps the role even
    when a foreign region is also named; otherwise a named foreign region drops
    it; a bare global signal ("Worldwide"/"Remote") with no foreign region is
    kept; and an empty location (remote-board default) is kept.
    """
    loc = _norm(location)
    if not loc:
        return True
    if _US_RE.search(loc):
        return True
    if any(f in loc for f in FOREIGN_HINTS):
        return False
    if _GLOBAL_RE.search(loc):
        return True
    # Unknown non-empty location (e.g. an unfamiliar city) — be conservative
    # about "US only" and drop it.
    return False


def _alnum(value):
    """Lowercase and reduce runs of punctuation/whitespace to single spaces, so
    "Nava, Inc." and "[Simple]" match "nava" / "simple" as whole words."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", _norm(value)).split())


def civic_company_names():
    """Punctuation-stripped names of every company already covered by the
    civic-tech importers, so Email B can exclude them ("all OTHER companies").
    """
    names = set()
    for file_name in sorted(os.listdir(IMPORTERS_DIR)):
        if not file_name.endswith(".py") or file_name in ("__init__.py", "utils.py"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(
                file_name[:-3], str(IMPORTERS_DIR / file_name))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            for firm in getattr(module, "firms", []):
                if isinstance(firm, (list, tuple)) and firm:
                    names.add(_alnum(firm[0]))
        except Exception:  # noqa: BLE001 — importer that can't load just isn't excluded
            continue
    # Single-company importers that have no `firms` list.
    names.update(_alnum(n) for n in
                 ("Ad Hoc", "Bracari", "Archesys", "Exygy", "Fearless", "For People"))
    names.discard("")
    return names


def is_civic(company, civic_names):
    """True if `company` is one of the civic-tech firms. Matches a civic name
    as a whole word-run so "Nava" also excludes "Nava PBC" / "Nava, Inc.",
    without excluding unrelated names like "Navasota".
    """
    padded = f" {_alnum(company)} "
    return any(f" {name} " in padded for name in civic_names)


def dedup(jobs):
    unique = {}
    for job in jobs:
        key = f"{_norm(job['company'])}|{_norm(job['title'])}"
        unique.setdefault(key, job)
    return sorted(unique.values(), key=lambda j: (_norm(j["company"]), _norm(j["title"])))


def state_key(job):
    # Match the dedup key (no job_id): the same role can surface from different
    # sources with different ids run to run, and keying on id would re-alert it.
    return f"{_norm(job['company'])}|{_norm(job['title'])}"


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #

def load_state():
    if not SEEN_FILE.exists():
        return {}
    raw = SEEN_FILE.read_text().strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print("seen_jobs_broad.json is corrupt; starting from empty state.")
        return {}
    if isinstance(data, list):
        today = datetime.date.today().isoformat()
        return {key: today for key in data}
    if not isinstance(data, dict):
        return {}
    return data


def save_state(state):
    SEEN_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


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
# Main
# --------------------------------------------------------------------------- #

def main():
    print("Broad alert: fetching free remote-job APIs...")
    candidates = fetch_all()
    print(f"Total candidates: {len(candidates)}")

    civic = civic_company_names()
    matches = dedup([
        job for job in candidates
        if role_matches(job["title"])
        and us_remote_ok(job["location"])
        and not is_civic(job["company"], civic)
    ])
    print(f"Matches (role + US + non-civic): {len(matches)}")

    state = load_state()
    new = [job for job in matches if state_key(job) not in state]
    print(f"New matches: {len(new)}")

    if new:
        plural = "s" if len(new) != 1 else ""
        send_jobs_email(
            subject=f"🌐 {len(new)} new remote front-end/UI/accessibility job{plural}",
            intro=("New remote (US) front-end / UI / accessibility roles from "
                   "companies outside your civic-tech list:"),
            jobs=new,
        )
    else:
        print("No new matches to alert on.")

    today = datetime.date.today().isoformat()
    for job in matches:
        state[state_key(job)] = today
    save_state(prune_state(state))


if __name__ == "__main__":
    main()

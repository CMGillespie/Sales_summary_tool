"""
PROJECT: Wordly Sales Intelligence Pipeline
SCRIPT:  matcher_v1_0.py
VERSION: 2.1
CHANGES: - Olivia-only test mode (TARGET_REP filter)
         - 10-day lookback for test run
         - HubSpot note write per matched call (meeting summary only)
         - Customer name from HS contact association, Gemini fallback
         - Audit prompt updated with 1-5 grading rubric, grade at bottom
         - Grade baked into audit filename (e.g. _AUDIT_G3.txt)
         - Management review CSV (rep, date, grade, audit file link)
         - Processed log (JSON) to prevent duplicate summarization
         - salespeople.csv — skip anyone with empty API key
AUTHOR:  Built with Claude
DATE:    2026-06-24
"""

import requests
import json
import os
import sys
import csv
import time
from datetime import datetime, timezone, timedelta
from collections import Counter

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
HS_KEY_FILE        = "HS_Service_key.txt"
GEMINI_KEY_FILE    = "gemini_api_key.txt"
SLACK_WEBHOOK_FILE = "slack_webhook.txt"
PROMPT_HS_FILE     = "prompt_hs.txt"
PROMPT_MGMT_FILE   = "prompt_sales_mgmt.txt"
SALESPEOPLE_FILE   = "salespeople.csv"
PROCESSED_FILE     = "processed_log.json"
REVIEW_CSV_FILE    = "management_review.csv"

WORDLY_BASE_URL    = "https://api.wordly.ai"
HS_BASE_URL        = "https://api.hubapi.com"
GEMINI_URL         = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"

# Filter to one rep for test run. Set to None to run all.
TARGET_REP         = "Olivia O'Donnell"

# RUN_MODE: "single" = 1 meeting per person, "day" = 1 day, "week" = 7 days, "ten" = 10 days
RUN_MODE           = "ten"

# Matching config
MATCH_WINDOW_MINS  = 15
HIGH_THRESHOLD     = 6
MIN_DURATION_MINS  = 5

# HubSpot portal ID for contact links
HS_PORTAL_ID       = "5315820"

OUTPUT_DIR         = "summaries"

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def load_key(filename):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    if not os.path.exists(path):
        print(f"  ❌  Key file not found: {filename}")
        return None
    with open(path, "r", encoding="utf-8") as f:
        key = f.read().strip()
    if not key:
        print(f"  ❌  Key file is empty: {filename}")
        return None
    print(f"  ✅  Loaded: {filename} ({len(key)} chars)")
    return key


def load_prompt(filename):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def load_salespeople():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), SALESPEOPLE_FILE)
    if not os.path.exists(path):
        print(f"  ❌  {SALESPEOPLE_FILE} not found.")
        return []
    people = []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            people.append({
                "name":           row.get("name", "").strip(),
                "email":          row.get("email", "").strip().lower(),
                "wordly_api_key": row.get("wordly_api_key", "").strip()
            })
    print(f"  ✅  Loaded {len(people)} salespeople from {SALESPEOPLE_FILE}")
    return people


def load_processed():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), PROCESSED_FILE)
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except:
            return {}
    return {}


def save_processed(processed):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), PROCESSED_FILE)
    with open(path, "w") as f:
        json.dump(processed, f, indent=2)


def append_review_csv(row):
    """Append one row to the management review CSV."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), REVIEW_CSV_FILE)
    file_exists = os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "rep_name", "meeting_date", "meeting_time", "hs_meeting_id",
            "transcript_id", "match_confidence", "grade", "audit_file"
        ])
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def section(title):
    print()
    print("=" * 70)
    print(f"  {title}")
    print("=" * 70)


def slack_notify(webhook_url, message):
    if not webhook_url:
        return
    try:
        requests.post(webhook_url, json={"text": message}, timeout=10)
    except:
        pass


def parse_dt(iso_str):
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except:
        return None


def duration_mins(start_str, end_str):
    s = parse_dt(start_str)
    e = parse_dt(end_str)
    if s and e:
        return int((e - s).total_seconds() / 60)
    return -1


def confidence_score(delta_mins):
    if delta_mins <= HIGH_THRESHOLD:
        return (3, "HIGH")
    elif delta_mins <= MATCH_WINDOW_MINS:
        return (2, "MEDIUM")
    elif delta_mins <= 30:
        return (1, "LOW")
    else:
        return (0, "NONE")


def safe_filename(s):
    return "".join(c if c.isalnum() or c in "._- " else "_" for c in s).strip()


CONF_ICON = {"HIGH": "✅", "MEDIUM": "🟡", "LOW": "🟠", "NONE": "❌"}


def get_lookback_days():
    if RUN_MODE == "single":
        return 2
    elif RUN_MODE == "day":
        return 1
    elif RUN_MODE == "week":
        return 7
    elif RUN_MODE == "ten":
        return 10
    return 2


# ---------------------------------------------------------------------------
# TRANSCRIPT DOWNLOAD
# ---------------------------------------------------------------------------

def download_transcript(t_id, wordly_key):
    url = f"{WORDLY_BASE_URL}/transcripts/{t_id}/original?format=txt&speaker_names=true"
    chunks = []
    headers = {"x-wordly-api-key": wordly_key}
    try:
        with requests.get(url, headers=headers, timeout=30, stream=True) as res:
            if res.status_code != 200:
                return None, f"http_{res.status_code}"
            try:
                for chunk in res.iter_content(chunk_size=1024, decode_unicode=True):
                    if chunk:
                        chunks.append(chunk)
            except requests.exceptions.ChunkedEncodingError:
                pass
        text = "".join(chunks)
        if not text.strip():
            return None, "empty"
        return text, "ok"
    except Exception as e:
        return None, f"exception: {e}"


# ---------------------------------------------------------------------------
# HUBSPOT
# ---------------------------------------------------------------------------

def resolve_owner(hs_key, email, all_owners=None):
    if all_owners:
        for o in all_owners:
            if o.get("email", "").lower() == email.lower():
                return o
        return None
    headers = {"Authorization": f"Bearer {hs_key}", "Content-Type": "application/json"}
    try:
        res = requests.get(f"{HS_BASE_URL}/crm/v3/owners?limit=100", headers=headers, timeout=10)
        if res.status_code != 200:
            return None
        for o in res.json().get("results", []):
            if o.get("email", "").lower() == email.lower():
                return o
        return None
    except:
        return None


def fetch_all_owners(hs_key):
    headers = {"Authorization": f"Bearer {hs_key}", "Content-Type": "application/json"}
    try:
        res = requests.get(f"{HS_BASE_URL}/crm/v3/owners?limit=100", headers=headers, timeout=10)
        if res.status_code == 200:
            return res.json().get("results", [])
    except:
        pass
    return []


def pull_hs_meetings(hs_key, owner_id, lookback_days):
    headers = {"Authorization": f"Bearer {hs_key}", "Content-Type": "application/json"}
    now_utc   = datetime.now(timezone.utc)
    since_utc = now_utc - timedelta(days=lookback_days)
    now_ms    = int(now_utc.timestamp() * 1000)
    since_ms  = int(since_utc.timestamp() * 1000)

    payload = {
        "filterGroups": [{
            "filters": [
                {"propertyName": "hubspot_owner_id",      "operator": "EQ",  "value": str(owner_id)},
                {"propertyName": "hs_meeting_start_time", "operator": "GTE", "value": str(since_ms)},
                {"propertyName": "hs_meeting_start_time", "operator": "LTE", "value": str(now_ms)}
            ]
        }],
        "properties": [
            "hs_meeting_title", "hs_meeting_start_time", "hs_meeting_end_time",
            "hs_meeting_outcome", "hubspot_owner_id"
        ],
        "sorts": [{"propertyName": "hs_meeting_start_time", "direction": "ASCENDING"}],
        "limit": 100
    }
    try:
        res = requests.post(
            f"{HS_BASE_URL}/crm/v3/objects/meetings/search",
            headers=headers, json=payload, timeout=15
        )
        if res.status_code != 200:
            print(f"    ❌  HS meetings failed: {res.status_code}")
            return []
        results = res.json().get("results", [])
        meetings = []
        for m in results:
            props = m.get("properties", {})
            start = props.get("hs_meeting_start_time", "")
            end   = props.get("hs_meeting_end_time", "")
            meetings.append({
                "hs_id":    m.get("id"),
                "title":    props.get("hs_meeting_title") or "Untitled",
                "start":    parse_dt(start),
                "end":      parse_dt(end),
                "duration": duration_mins(start, end),
                "outcome":  props.get("hs_meeting_outcome") or "—",
                "start_str": start
            })
        return meetings
    except Exception as e:
        print(f"    ❌  Exception: {e}")
        return []


def get_meeting_contact(hs_key, meeting_id):
    """
    Get customer name from HS meeting contact association.
    Returns (contact_id, full_name) or (None, None).
    """
    headers = {"Authorization": f"Bearer {hs_key}", "Content-Type": "application/json"}
    try:
        # Get contact association
        res = requests.get(
            f"{HS_BASE_URL}/crm/v3/objects/meetings/{meeting_id}/associations/contacts",
            headers=headers, timeout=10
        )
        if res.status_code != 200:
            return None, None
        results = res.json().get("results", [])
        if not results:
            return None, None
        contact_id = results[0].get("id")

        # Fetch contact name
        cr = requests.get(
            f"{HS_BASE_URL}/crm/v3/objects/contacts/{contact_id}"
            f"?properties=firstname,lastname,email",
            headers=headers, timeout=10
        )
        if cr.status_code != 200:
            return contact_id, None
        props = cr.json().get("properties", {})
        first = props.get("firstname") or ""
        last  = props.get("lastname") or ""
        name  = f"{first} {last}".strip() or props.get("email") or "Unknown"
        return contact_id, name
    except:
        return None, None


def write_hs_note(hs_key, contact_id, note_body):
    """Write a note to a HubSpot contact record."""
    headers = {"Authorization": f"Bearer {hs_key}", "Content-Type": "application/json"}
    payload = {
        "properties": {
            "hs_note_body":  note_body,
            "hs_timestamp":  str(int(datetime.now().timestamp() * 1000)),
        },
        "associations": [{
            "to":    {"id": contact_id},
            "types": [{"associationCategory": "HUBSPOT_DEFINED",
                       "associationTypeId": 202}]
        }]
    }
    try:
        res = requests.post(
            f"{HS_BASE_URL}/crm/v3/objects/notes",
            headers=headers, json=payload, timeout=15
        )
        if res.status_code in (200, 201):
            return res.json().get("id")
        else:
            print(f"    ⚠️  HS note write failed: {res.status_code} — {res.text[:150]}")
            return None
    except Exception as e:
        print(f"    ⚠️  HS note exception: {e}")
        return None


# ---------------------------------------------------------------------------
# WORDLY
# ---------------------------------------------------------------------------

def get_session_state(session_id, wordly_key):
    try:
        res = requests.get(
            f"{WORDLY_BASE_URL}/sessions/{session_id}",
            headers={"x-wordly-api-key": wordly_key}, timeout=10
        )
        return res.json().get("state", "unknown") if res.status_code == 200 else f"error_{res.status_code}"
    except:
        return "exception"


def pull_wordly_transcripts(wordly_key, lookback_days):
    headers   = {"x-wordly-api-key": wordly_key}
    now_utc   = datetime.now(timezone.utc)
    since_utc = now_utc - timedelta(days=lookback_days)

    try:
        res = requests.get(
            f"{WORDLY_BASE_URL}/transcripts?page=1&limit=100",
            headers=headers, timeout=15
        )
        if res.status_code == 401:
            print(f"    ❌  401 Unauthorized — API key rejected or revoked.")
            return None
        if res.status_code != 200:
            print(f"    ❌  Transcript list failed: {res.status_code}")
            return []

        all_t = res.json().get("transcripts", [])
        transcripts = []
        for t in all_t:
            start = parse_dt(t.get("startTime"))
            if not start or start < since_utc:
                continue
            sid = t.get("sessionId", "?")
            dur = duration_mins(t.get("startTime",""), t.get("endTime",""))
            if 0 <= dur < MIN_DURATION_MINS:
                continue
            state = get_session_state(sid, wordly_key)
            if state != "ended":
                continue
            transcripts.append({
                "transcript_id": t.get("transcriptId"),
                "session_id":    sid,
                "title":         t.get("title", "?"),
                "start":         start,
                "end":           parse_dt(t.get("endTime")),
                "duration":      dur,
                "start_str":     t.get("startTime","")
            })
        return transcripts
    except Exception as e:
        print(f"    ❌  Exception: {e}")
        return []


# ---------------------------------------------------------------------------
# MATCHING
# ---------------------------------------------------------------------------

def match(transcripts, meetings):
    candidates = []
    for t in transcripts:
        for m in meetings:
            if not t["start"] or not m["start"]:
                continue
            delta = abs((t["start"] - m["start"]).total_seconds() / 60)
            score, conf = confidence_score(delta)
            if score > 0:
                candidates.append({
                    "transcript": t, "meeting": m,
                    "delta_mins": round(delta, 1),
                    "score": score, "confidence": conf
                })

    candidates.sort(key=lambda x: (-x["score"], x["delta_mins"]))
    used_t = set()
    used_m = set()
    assigned = []
    for c in candidates:
        t_id = c["transcript"]["transcript_id"]
        m_id = c["meeting"]["hs_id"]
        if t_id not in used_t and m_id not in used_m:
            assigned.append(c)
            used_t.add(t_id)
            used_m.add(m_id)

    for t in transcripts:
        if t["transcript_id"] not in used_t:
            assigned.append({
                "transcript": t, "meeting": None,
                "delta_mins": None, "score": 0, "confidence": "NONE"
            })

    assigned.sort(key=lambda x: x["transcript"]["start"] or datetime.min.replace(tzinfo=timezone.utc))
    return assigned


# ---------------------------------------------------------------------------
# GEMINI
# ---------------------------------------------------------------------------

def gemini_call(prompt_text, gemini_key, retries=2):
    for attempt in range(retries):
        try:
            res = requests.post(
                f"{GEMINI_URL}?key={gemini_key}",
                json={"contents": [{"parts": [{"text": prompt_text}]}]},
                timeout=90
            )
            if res.status_code == 200:
                return res.json()["candidates"][0]["content"]["parts"][0]["text"]
            return f"ERROR {res.status_code}: {res.text[:200]}"
        except requests.exceptions.Timeout:
            if attempt < retries - 1:
                time.sleep(5)
                continue
            return "EXCEPTION: Gemini timed out after retries."
        except Exception as e:
            return f"EXCEPTION: {e}"
    return "EXCEPTION: All retries exhausted."


def extract_grade(audit_text):
    """
    Parse the grade (1-5) from the end of the audit text.
    Looks for 'GRADE: X' pattern.
    Returns int or None.
    """
    import re
    match = re.search(r'GRADE:\s*([1-5])', audit_text[-500:])
    if match:
        return int(match.group(1))
    return None


def get_customer_name_from_transcript(transcript_text, gemini_key):
    """Fallback: ask Gemini to identify the customer name from transcript."""
    prompt = (
        "Read the following sales call transcript excerpt. "
        "Identify the name of the CUSTOMER (not the salesperson). "
        "Reply with ONLY the customer's name — nothing else. "
        "If you cannot determine it, reply with: Unknown\n\n"
        f"TRANSCRIPT:\n{transcript_text[:3000]}"
    )
    result = gemini_call(prompt, gemini_key).strip()
    return result if result and "Unknown" not in result else None


# ---------------------------------------------------------------------------
# SUMMARIZE ONE MATCH
# ---------------------------------------------------------------------------

def summarize_match(r, person_name, hs_key, gemini_key, prompt_hs, prompt_mgmt,
                    processed, output_dir):
    t      = r["transcript"]
    m      = r["meeting"]
    t_id   = t["transcript_id"]
    wk     = r.get("wordly_key")
    conf   = r["confidence"]

    # Skip if already processed
    if t_id in processed:
        print(f"    ⏭️   Already processed: {t_id} — skipping")
        return None

    # Download transcript
    text, status = download_transcript(t_id, wk)
    if not text:
        print(f"    ⚠️  Download failed: {status}")
        return None

    m_title   = m["title"] if m else "Unmatched"
    m_hs_id   = m["hs_id"] if m else None
    m_start   = m["start_str"] if m else t["start_str"]
    dt        = parse_dt(m_start) or parse_dt(t["start_str"])
    date_str  = dt.strftime("%Y-%m-%d") if dt else "unknown"
    time_str  = dt.strftime("%H%M") if dt else "0000"
    date_disp = dt.strftime("%B %d, %Y") if dt else "Unknown Date"
    time_disp = dt.strftime("%I:%M %p UTC") if dt else ""
    safe_n    = safe_filename(person_name.replace(" ", "_"))
    base      = f"{safe_n}_{time_str}"

    # Subfolder: summaries/Person Name/YYYY-MM-DD/
    person_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        output_dir,
        safe_filename(person_name),
        date_str
    )
    os.makedirs(person_dir, exist_ok=True)

    def save(suffix, body, header):
        path = os.path.join(person_dir, f"{base}_{suffix}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(header + "\n" + "="*60 + "\n\n" + body)
        return path

    save("TRANSCRIPT", text, f"RAW TRANSCRIPT — {person_name} — {date_str} {time_str}")

    # Get customer name from HubSpot contact association
    contact_id   = None
    customer_name = None
    if m_hs_id:
        contact_id, customer_name = get_meeting_contact(hs_key, m_hs_id)
        if customer_name:
            print(f"    Customer (HS): {customer_name}")
        else:
            print(f"    Customer not in HS — asking Gemini...")
            customer_name = get_customer_name_from_transcript(text, gemini_key)
            if customer_name:
                print(f"    Customer (Gemini): {customer_name}")
            else:
                customer_name = "Unknown Customer"
    else:
        customer_name = "Unknown Customer"

    # --- HubSpot Summary ---
    hs_header_line = (
        f"AI Summary — {person_name} & {customer_name} | {date_disp} {time_disp}"
    )
    print(f"    Generating HubSpot summary...", end=" ", flush=True)
    hs_summary = gemini_call(
        f"{prompt_hs}\n\nSalesperson: {person_name}\n"
        f"Customer: {customer_name}\n"
        f"Meeting: {m_title}\n"
        f"Date: {date_disp} {time_disp}\n\n"
        f"TRANSCRIPT:\n{text}",
        gemini_key
    )
    ok = not hs_summary.startswith("ERROR") and not hs_summary.startswith("EXCEPTION")
    print("✅" if ok else "❌")

    # Build the full note body for HubSpot
    hs_note_body = f"{hs_header_line}\n\n{hs_summary}"

    hs_file = save("HS", hs_note_body,
         f"HUBSPOT SUMMARY\nSalesperson: {person_name}\nCustomer: {customer_name}\n"
         f"Meeting: {m_title}\nDate: {m_start}\n"
         f"HS Meeting ID: {m_hs_id or 'UNMATCHED'}\nMatch: {conf}")

    # Write to HubSpot contact record
    if contact_id and ok:
        print(f"    Writing note to HubSpot contact {contact_id}...", end=" ", flush=True)
        note_id = write_hs_note(hs_key, contact_id, hs_note_body)
        print(f"✅  Note ID: {note_id}" if note_id else "❌")
    else:
        if not contact_id:
            print(f"    ⚠️  No contact ID — skipping HS write")

    time.sleep(2)

    # --- Sales Audit with 1-5 Grade ---
    print(f"    Generating sales audit...", end=" ", flush=True)

    grading_rubric = """
GRADING RUBRIC — assign one of these grades at the END of your audit:
  5 — Exceptional. Textbook call. Worth sharing as a team example.
  4 — Strong. Minor gaps but solid overall performance.
  3 — Competent. Met the standard. Nothing notable either way.
  2 — Needs attention. Identifiable gaps in discovery, positioning, or follow-through.
  1 — Significant concerns. Multiple issues. Recommend immediate coaching conversation.

After your full written analysis, end your response with exactly this format on its own line:
GRADE: X
(where X is the number 1-5)
"""

    mgmt_summary = gemini_call(
        f"{prompt_mgmt}\n\n{grading_rubric}\n\n"
        f"Salesperson: {person_name}\n"
        f"Customer: {customer_name}\n"
        f"Meeting: {m_title}\n"
        f"Date: {date_disp} {time_disp}\n\n"
        f"TRANSCRIPT:\n{text}",
        gemini_key
    )
    ok_audit = not mgmt_summary.startswith("ERROR") and not mgmt_summary.startswith("EXCEPTION")
    print("✅" if ok_audit else "❌")

    # Extract grade
    grade = extract_grade(mgmt_summary) if ok_audit else None
    grade_str = str(grade) if grade else "X"
    print(f"    Grade: {grade_str}/5")

    audit_file = save(f"AUDIT_G{grade_str}",
        mgmt_summary,
        f"SALES AUDIT — GRADE {grade_str}/5\n"
        f"Salesperson: {person_name}\nCustomer: {customer_name}\n"
        f"Meeting: {m_title}\nDate: {m_start}\n"
        f"HS Meeting ID: {m_hs_id or 'UNMATCHED'}\nMatch: {conf}")

    print(f"    Files: {safe_filename(person_name)}/{date_str}/{base}_*.txt")

    # Append to management review CSV
    append_review_csv({
        "rep_name":        person_name,
        "meeting_date":    date_str,
        "meeting_time":    time_str,
        "hs_meeting_id":   m_hs_id or "",
        "transcript_id":   t_id,
        "match_confidence": conf,
        "grade":           grade_str,
        "audit_file":      audit_file
    })

    # Mark as processed
    processed[t_id] = {
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "rep":          person_name,
        "meeting":      m_title,
        "date":         date_str,
        "grade":        grade_str
    }
    save_processed(processed)

    return {
        "hs":       hs_summary,
        "audit":    mgmt_summary,
        "grade":    grade,
        "base":     base,
        "note_id":  note_id if contact_id and ok else None
    }


# ---------------------------------------------------------------------------
# PER-PERSON PIPELINE
# ---------------------------------------------------------------------------

def run_person(person, hs_key, gemini_key, slack_url, prompt_hs, prompt_mgmt,
               all_owners, lookback_days, run_mode, processed, output_dir):
    name       = person["name"]
    email      = person["email"]
    wordly_key = person["wordly_api_key"]

    section(f"{name} — {email}")

    owner = resolve_owner(hs_key, email, all_owners)
    if not owner:
        print(f"  ⚠️  Not found in HubSpot owners — skipping HS matching.")
        owner_id = None
    else:
        owner_id = owner.get("id")
        print(f"  ✅  HubSpot owner ID: {owner_id}")

    meetings = []
    if owner_id:
        meetings = pull_hs_meetings(hs_key, owner_id, lookback_days)
        print(f"  HS meetings found: {len(meetings)}")
    else:
        print(f"  HS meetings: skipped (no owner)")

    print(f"  Pulling Wordly transcripts (last {lookback_days} days)...")
    transcripts = pull_wordly_transcripts(wordly_key, lookback_days)

    if transcripts is None:
        slack_notify(slack_url,
            f"⚠️ *Wordly API key auth failed* for `{name}` — key may have been rotated.")
        return {"name": name, "status": "auth_failed", "matched": 0, "summarized": 0}

    print(f"  Wordly transcripts found: {len(transcripts)}")

    if not transcripts:
        print(f"  No transcripts to process.")
        return {"name": name, "status": "no_transcripts", "matched": 0, "summarized": 0}

    if meetings:
        matches = match(transcripts, meetings)
    else:
        matches = [{"transcript": t, "meeting": None,
                    "delta_mins": None, "score": 0, "confidence": "NONE"}
                   for t in transcripts]

    counts = Counter(r["confidence"] for r in matches)
    print(f"  Match summary — HIGH: {counts['HIGH']}  MEDIUM: {counts['MEDIUM']}  "
          f"LOW: {counts['LOW']}  NONE: {counts['NONE']}")

    print(f"\n  {'conf':<8} {'delta':>6}  {'transcript_start':<26} {'meeting_start':<26} titles")
    print(f"  {'-'*8} {'-'*6}  {'-'*26} {'-'*26} {'-'*40}")
    for r in matches:
        t       = r["transcript"]
        m       = r["meeting"]
        conf    = r["confidence"]
        delta   = f"{r['delta_mins']}m" if r["delta_mins"] is not None else "—"
        t_disp  = t["title"][:20]
        m_disp  = m["title"][:20] if m else "⚠️  NO MATCH"
        m_start = m["start_str"] if m else "—"
        already = " [done]" if t["transcript_id"] in processed else ""
        print(f"  {CONF_ICON.get(conf,'?')} {conf:<6} {delta:>6}  "
              f"{t['start_str']:<26} {m_start:<26} {t_disp} / {m_disp}{already}")

    eligible = [r for r in matches if r["confidence"] in ("HIGH", "MEDIUM")]
    if run_mode == "single":
        eligible = eligible[:1]

    already_done = sum(1 for r in eligible if r["transcript"]["transcript_id"] in processed)
    to_do        = len(eligible) - already_done
    print(f"\n  {len(eligible)} HIGH/MEDIUM matches — {already_done} already done, {to_do} to process...")

    summarized = 0
    for r in eligible:
        r["wordly_key"] = wordly_key
        result = summarize_match(
            r, name, hs_key, gemini_key,
            prompt_hs, prompt_mgmt, processed, output_dir
        )
        if result:
            summarized += 1
        time.sleep(2)

    return {
        "name":       name,
        "status":     "ok",
        "matched":    len(matches),
        "high":       counts["HIGH"],
        "medium":     counts["MEDIUM"],
        "none":       counts["NONE"],
        "summarized": summarized
    }


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    lookback_days = get_lookback_days()

    print()
    print("=" * 70)
    print("  WORDLY SALES INTELLIGENCE PIPELINE — v2.1")
    print(f"  Mode      : {RUN_MODE.upper()} (last {lookback_days} day(s))")
    print(f"  Filter    : {TARGET_REP or 'ALL'}")
    print(f"  Run at    : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    print("\nLoading keys and config...")
    hs_key      = load_key(HS_KEY_FILE)
    gemini_key  = load_key(GEMINI_KEY_FILE)
    slack_url   = load_key(SLACK_WEBHOOK_FILE)
    prompt_hs   = load_prompt(PROMPT_HS_FILE)
    prompt_mgmt = load_prompt(PROMPT_MGMT_FILE)
    salespeople = load_salespeople()
    processed   = load_processed()

    print(f"  Processed log: {len(processed)} entries")

    if not hs_key or not gemini_key:
        print("\n⛔  Cannot proceed — missing HubSpot or Gemini key.")
        sys.exit(1)
    if not prompt_hs or not prompt_mgmt:
        print("\n⛔  Cannot proceed — missing prompt files.")
        sys.exit(1)
    if not salespeople:
        print(f"\n⛔  Cannot proceed — {SALESPEOPLE_FILE} empty or missing.")
        sys.exit(1)

    # Apply TARGET_REP filter
    if TARGET_REP:
        salespeople = [p for p in salespeople
                       if p["name"].lower() == TARGET_REP.lower()]
        if not salespeople:
            print(f"\n⛔  TARGET_REP '{TARGET_REP}' not found in CSV.")
            sys.exit(1)
        print(f"\n  ⚠️  Filtered to: {TARGET_REP}")

    # Skip anyone with no API key
    salespeople = [p for p in salespeople if p["wordly_api_key"]]
    print(f"  Active salespeople: {len(salespeople)}")

    print(f"\nFetching HubSpot owner list...")
    all_owners = fetch_all_owners(hs_key)
    print(f"  {len(all_owners)} owners found in HubSpot")

    output_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), OUTPUT_DIR
    )

    slack_notify(slack_url,
        f"🚀 *Sales Pipeline v2.1 started* — {RUN_MODE.upper()} | "
        f"Filter: {TARGET_REP or 'ALL'} | Last {lookback_days} days")

    results = []
    for person in salespeople:
        result = run_person(
            person, hs_key, gemini_key, slack_url,
            prompt_hs, prompt_mgmt, all_owners,
            lookback_days, RUN_MODE, processed, output_dir
        )
        results.append(result)
        time.sleep(3)

    section("PIPELINE COMPLETE")
    total_summarized = sum(r.get("summarized", 0) for r in results)
    print(f"\n  {'Name':<30} {'Status':<15} {'Matched':>8} {'Summarized':>12}")
    print(f"  {'-'*30} {'-'*15} {'-'*8} {'-'*12}")
    for r in results:
        print(f"  {r['name']:<30} {r['status']:<15} "
              f"{r.get('matched',0):>8} {r.get('summarized',0):>12}")

    print(f"\n  Total summaries: {total_summarized}")
    print(f"  Review CSV: {REVIEW_CSV_FILE}")
    print(f"  Output: ./{OUTPUT_DIR}/")

    slack_notify(slack_url,
        f"✅ *Sales Pipeline v2.1 complete* — {RUN_MODE.upper()}\n"
        f"Filter: {TARGET_REP or 'ALL'}\n"
        f"Summaries generated: {total_summarized}\n"
        f"Review CSV updated: `{REVIEW_CSV_FILE}`")

    print()


if __name__ == "__main__":
    main()
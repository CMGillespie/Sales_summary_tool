"""
PROJECT: Wordly Sales Intelligence Pipeline
SCRIPT:  matcher_v1_0.py
VERSION: 2.0
CHANGES: Multi-salesperson loop. Reads salespeople.csv for name/email/api_key.
         Runs full pipeline per person — HubSpot meeting pull, Wordly transcript
         pull, fuzzy match, transcript download, Gemini summarization.
         Outputs per-person summary files. Slack summary on completion.
         RUN_MODE controls scope: "single" (one meeting), "day", "week"
AUTHOR:  Built with Claude
DATE:    2026-05-11
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

WORDLY_BASE_URL    = "https://api.wordly.ai"
HS_BASE_URL        = "https://api.hubapi.com"
GEMINI_URL         = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"

# RUN_MODE: "single" = 1 meeting per person, "day" = today, "week" = last 7 days
RUN_MODE           = "single"

# Matching config
MATCH_WINDOW_MINS  = 15
HIGH_THRESHOLD     = 6
MIN_DURATION_MINS  = 5
MAX_TO_SUMMARIZE   = 1      # Per person. Increase when off free tier.

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
    """Load salespeople from CSV. Returns list of dicts."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), SALESPEOPLE_FILE)
    if not os.path.exists(path):
        print(f"  ❌  {SALESPEOPLE_FILE} not found.")
        return []
    people = []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            people.append({
                "name":          row.get("name", "").strip(),
                "email":         row.get("email", "").strip().lower(),
                "wordly_api_key": row.get("wordly_api_key", "").strip()
            })
    print(f"  ✅  Loaded {len(people)} salespeople from {SALESPEOPLE_FILE}")
    return people


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
        return 2    # Narrow window, pick most recent
    elif RUN_MODE == "day":
        return 1
    elif RUN_MODE == "week":
        return 7
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
    """Find HubSpot owner by email. Pass all_owners list to avoid repeat API calls."""
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
        "limit": 50
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
    headers  = {"x-wordly-api-key": wordly_key}
    now_utc  = datetime.now(timezone.utc)
    since_utc = now_utc - timedelta(days=lookback_days)

    try:
        res = requests.get(
            f"{WORDLY_BASE_URL}/transcripts?page=1&limit=50",
            headers=headers, timeout=15
        )
        if res.status_code == 401:
            print(f"    ❌  401 Unauthorized — API key rejected or revoked.")
            return None   # None signals auth failure vs empty list
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


# ---------------------------------------------------------------------------
# SUMMARIZATION
# ---------------------------------------------------------------------------

def summarize_match(r, person_name, gemini_key, prompt_hs, prompt_mgmt):
    t        = r["transcript"]
    m        = r["meeting"]
    t_id     = t["transcript_id"]
    wordly_key = r.get("wordly_key")
    conf     = r["confidence"]

    # Download transcript text
    text, status = download_transcript(t_id, wordly_key)
    if not text:
        print(f"    ⚠️  Download failed: {status}")
        return None

    m_title  = m["title"] if m else "Unmatched"
    m_start  = m["start_str"] if m else t["start_str"]
    dt       = parse_dt(m_start) or parse_dt(t["start_str"])
    date_str = dt.strftime("%Y-%m-%d_%H%M") if dt else "unknown"
    safe_n   = safe_filename(person_name.replace(" ", "_"))
    base     = f"{safe_n}_{date_str}"

    person_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        OUTPUT_DIR,
        safe_filename(person_name)
    )
    os.makedirs(person_dir, exist_ok=True)

    def save(suffix, body, header):
        path = os.path.join(person_dir, f"{base}_{suffix}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(header + "\n" + "="*60 + "\n\n" + body)

    save("TRANSCRIPT", text, f"RAW TRANSCRIPT — {person_name} — {date_str}")

    # HubSpot summary
    print(f"    Generating HubSpot summary...", end=" ", flush=True)
    hs_summary = gemini_call(
        f"{prompt_hs}\n\nSalesperson: {person_name}\nMeeting: {m_title}\n\nTRANSCRIPT:\n{text}",
        gemini_key
    )
    print("✅" if not hs_summary.startswith("ERROR") else "❌")
    save("HS", hs_summary,
         f"HUBSPOT SUMMARY\nSalesperson: {person_name}\nMeeting: {m_title}\nDate: {m_start}\nHS ID: {m['hs_id'] if m else 'UNMATCHED'}\nMatch: {conf}")
    time.sleep(2)

    # Sales audit
    print(f"    Generating sales audit...", end=" ", flush=True)
    mgmt_summary = gemini_call(
        f"{prompt_mgmt}\n\nSalesperson: {person_name}\nMeeting: {m_title}\n\nTRANSCRIPT:\n{text}",
        gemini_key
    )
    print("✅" if not mgmt_summary.startswith("ERROR") else "❌")
    save("AUDIT", mgmt_summary,
         f"SALES AUDIT\nSalesperson: {person_name}\nMeeting: {m_title}\nDate: {m_start}\nHS ID: {m['hs_id'] if m else 'UNMATCHED'}\nMatch: {conf}")

    print(f"    Files saved: {safe_filename(person_name)}/{base}_*.txt")
    return {"hs": hs_summary, "audit": mgmt_summary, "base": base}


# ---------------------------------------------------------------------------
# PER-PERSON PIPELINE
# ---------------------------------------------------------------------------

def run_person(person, hs_key, gemini_key, slack_url, prompt_hs, prompt_mgmt,
               all_owners, lookback_days, run_mode):
    name       = person["name"]
    email      = person["email"]
    wordly_key = person["wordly_api_key"]

    section(f"{name} — {email}")

    # Resolve HubSpot owner
    owner = resolve_owner(hs_key, email, all_owners)
    if not owner:
        print(f"  ⚠️  Not found in HubSpot owners — skipping HS matching.")
        print(f"      Will still pull Wordly transcripts if key is valid.")
        owner_id = None
    else:
        owner_id = owner.get("id")
        print(f"  ✅  HubSpot owner ID: {owner_id}")

    # Pull HubSpot meetings
    meetings = []
    if owner_id:
        meetings = pull_hs_meetings(hs_key, owner_id, lookback_days)
        print(f"  HS meetings found: {len(meetings)}")
    else:
        print(f"  HS meetings: skipped (no owner)")

    # Pull Wordly transcripts
    print(f"  Pulling Wordly transcripts (last {lookback_days} days)...")
    transcripts = pull_wordly_transcripts(wordly_key, lookback_days)

    if transcripts is None:
        # Auth failure
        slack_notify(slack_url,
            f"⚠️ *Wordly API key auth failed* for `{name}` — key may have been rotated. "
            f"Pipeline skipped for this person.")
        return {"name": name, "status": "auth_failed", "matched": 0, "summarized": 0}

    print(f"  Wordly transcripts found: {len(transcripts)}")

    if not transcripts:
        print(f"  No transcripts to process.")
        return {"name": name, "status": "no_transcripts", "matched": 0, "summarized": 0}

    # Match
    if meetings:
        matches = match(transcripts, meetings)
    else:
        # No meetings — mark all as NONE but still process
        matches = [{"transcript": t, "meeting": None,
                    "delta_mins": None, "score": 0, "confidence": "NONE"}
                   for t in transcripts]

    counts = Counter(r["confidence"] for r in matches)
    print(f"  Match summary — HIGH: {counts['HIGH']}  MEDIUM: {counts['MEDIUM']}  "
          f"LOW: {counts['LOW']}  NONE: {counts['NONE']}")

    # Print match table
    print(f"\n  {'conf':<8} {'delta':>6}  {'transcript_start':<26} {'meeting_start':<26} titles")
    print(f"  {'-'*8} {'-'*6}  {'-'*26} {'-'*26} {'-'*40}")
    for r in matches:
        t      = r["transcript"]
        m      = r["meeting"]
        conf   = r["confidence"]
        delta  = f"{r['delta_mins']}m" if r["delta_mins"] is not None else "—"
        t_disp = t["title"][:20]
        m_disp = m["title"][:20] if m else "⚠️  NO MATCH"
        m_start = m["start_str"] if m else "—"
        print(f"  {CONF_ICON.get(conf,'?')} {conf:<6} {delta:>6}  "
              f"{t['start_str']:<26} {m_start:<26} {t_disp} / {m_disp}")

    # Summarize — HIGH and MEDIUM only, cap at MAX_TO_SUMMARIZE
    eligible = [r for r in matches if r["confidence"] in ("HIGH", "MEDIUM")]

    if run_mode == "single":
        eligible = eligible[:1]
    # week/day modes: summarize all HIGH and MEDIUM matches

    total_eligible = len([r for r in matches if r["confidence"] in ("HIGH", "MEDIUM")])
    print(f"\n  Summarizing {len(eligible)} HIGH/MEDIUM matches ({len(matches)} total)...")
    summarized = 0
    for r in eligible:
        r["wordly_key"] = wordly_key
        result = summarize_match(r, name, gemini_key, prompt_hs, prompt_mgmt)
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
    print("  WORDLY SALES INTELLIGENCE PIPELINE — v2.0 (Multi-Salesperson)")
    print(f"  Mode      : {RUN_MODE.upper()} (last {lookback_days} day(s))")
    print(f"  Run at    : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    print("\nLoading keys and config...")
    hs_key      = load_key(HS_KEY_FILE)
    gemini_key  = load_key(GEMINI_KEY_FILE)
    slack_url   = load_key(SLACK_WEBHOOK_FILE)
    prompt_hs   = load_prompt(PROMPT_HS_FILE)
    prompt_mgmt = load_prompt(PROMPT_MGMT_FILE)
    salespeople = load_salespeople()

    if not hs_key or not gemini_key:
        print("\n⛔  Cannot proceed — missing HubSpot or Gemini key.")
        sys.exit(1)
    if not prompt_hs or not prompt_mgmt:
        print("\n⛔  Cannot proceed — missing prompt files.")
        sys.exit(1)
    if not salespeople:
        print(f"\n⛔  Cannot proceed — {SALESPEOPLE_FILE} empty or missing.")
        sys.exit(1)

    print(f"\nFetching HubSpot owner list...")
    all_owners = fetch_all_owners(hs_key)
    print(f"  {len(all_owners)} owners found in HubSpot")

    slack_notify(slack_url,
        f"🚀 *Sales Pipeline started* — Mode: {RUN_MODE.upper()} | "
        f"{len(salespeople)} salespeople | Last {lookback_days} day(s)")

    results = []
    for person in salespeople:
        if not person["wordly_api_key"]:
            print(f"\n  ⚠️  Skipping {person['name']} — no API key in CSV")
            continue
        result = run_person(
            person, hs_key, gemini_key, slack_url,
            prompt_hs, prompt_mgmt, all_owners,
            lookback_days, RUN_MODE
        )
        results.append(result)
        time.sleep(3)  # Pause between people

    # Final summary
    section("PIPELINE COMPLETE")
    total_summarized = sum(r.get("summarized", 0) for r in results)
    print(f"\n  {'Name':<30} {'Status':<15} {'Matched':>8} {'Summarized':>12}")
    print(f"  {'-'*30} {'-'*15} {'-'*8} {'-'*12}")
    for r in results:
        print(f"  {r['name']:<30} {r['status']:<15} "
              f"{r.get('matched',0):>8} {r.get('summarized',0):>12}")

    print(f"\n  Total summaries generated: {total_summarized}")
    print(f"  Output folder: ./{OUTPUT_DIR}/")

    slack_notify(slack_url,
        f"✅ *Sales Pipeline complete* — {RUN_MODE.upper()}\n"
        f"People processed: {len(results)}\n"
        f"Total summaries: {total_summarized}\n"
        f"Files in `./summaries/`")

    print()


if __name__ == "__main__":
    main()
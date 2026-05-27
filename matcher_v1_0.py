"""
PROJECT: Wordly Sales Intelligence Pipeline
SCRIPT:  matcher_v1_0.py
VERSION: 1.0d
CHANGES: Added Step 6 — Gemini summarization. Generates two outputs per
         matched transcript: HubSpot summary (prompt_hs.txt) and sales
         management audit (prompt_sales_mgmt.txt). Results printed and
         saved as local .txt files for review before any CRM writes.
AUTHOR:  Built with Claude
DATE:    2026-05-07
"""

import requests
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from collections import Counter

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
WORDLY_KEY_FILE    = "Aleksandra_Laszczyk_Mendez_WordlyAPI.txt"
HS_KEY_FILE        = "HS_Service_key.txt"
SLACK_WEBHOOK_FILE = "slack_webhook.txt"
GEMINI_KEY_FILE    = "gemini_api_key.txt"
PROMPT_HS_FILE     = "prompt_hs.txt"
PROMPT_MGMT_FILE   = "prompt_sales_mgmt.txt"

WORDLY_BASE_URL    = "https://api.wordly.ai"
HS_BASE_URL        = "https://api.hubapi.com"
GEMINI_URL         = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"

TARGET_EMAIL       = "aleksandra.laszczyk@wordly.ai"

LOOKBACK_DAYS      = 2
MATCH_WINDOW_MINS  = 15
HIGH_THRESHOLD     = 6
MIN_DURATION_MINS  = 5

OUTPUT_DIR         = "summaries"
MAX_TO_SUMMARIZE   = 1     # Gemini free tier rate limit guard. Increase when on paid/Vertex.

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
        print(f"  ❌  Prompt file not found: {filename}")
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


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


CONF_ICON = {"HIGH": "✅", "MEDIUM": "🟡", "LOW": "🟠", "NONE": "❌"}


def safe_filename(s):
    """Strip characters unsafe for filenames."""
    return "".join(c if c.isalnum() or c in "._- " else "_" for c in s).strip()


# ---------------------------------------------------------------------------
# TRANSCRIPT DOWNLOAD
# ---------------------------------------------------------------------------

def download_transcript(t_id, headers):
    url = f"{WORDLY_BASE_URL}/transcripts/{t_id}/original?format=txt&speaker_names=true"
    chunks = []
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
        status = "ok" if text.count("\n") > 2 else "partial"
        return text, status
    except Exception as e:
        return None, f"exception: {e}"


# ---------------------------------------------------------------------------
# STEP 1 — RESOLVE OWNER
# ---------------------------------------------------------------------------

def resolve_owner(hs_key, target_email):
    section("STEP 1 — RESOLVE HUBSPOT OWNER")
    print(f"\n  Looking up: {target_email}")
    headers = {"Authorization": f"Bearer {hs_key}", "Content-Type": "application/json"}
    try:
        res = requests.get(f"{HS_BASE_URL}/crm/v3/owners?limit=100", headers=headers, timeout=10)
        if res.status_code != 200:
            print(f"  ❌  {res.status_code}")
            return None
        for o in res.json().get("results", []):
            if o.get("email", "").lower() == target_email.lower():
                name = f"{o.get('firstName','')} {o.get('lastName','')}".strip()
                print(f"  ✅  Found: {name} | ownerId={o.get('id')}")
                return o
        print(f"  ❌  Not found: {target_email}")
        return None
    except Exception as e:
        print(f"  ❌  Exception: {e}")
        return None



# ---------------------------------------------------------------------------
# CONTACT NAME LOOKUP
# ---------------------------------------------------------------------------

def fetch_meeting_contact(meeting_id, headers):
    """Fetch the first associated contact name for a meeting. Returns string."""
    if not meeting_id:
        return "Unknown"
    try:
        # Get contact associations
        res = requests.get(
            f"{HS_BASE_URL}/crm/v3/objects/meetings/{meeting_id}/associations/contacts",
            headers=headers, timeout=10
        )
        if res.status_code != 200:
            return "Unknown"
        results = res.json().get("results", [])
        if not results:
            return "No contact"
        contact_id = results[0].get("id")
        # Fetch contact name
        cr = requests.get(
            f"{HS_BASE_URL}/crm/v3/objects/contacts/{contact_id}?properties=firstname,lastname,email",
            headers=headers, timeout=10
        )
        if cr.status_code != 200:
            return "Unknown"
        props = cr.json().get("properties", {})
        first = props.get("firstname") or ""
        last  = props.get("lastname") or ""
        name  = f"{first} {last}".strip()
        return name if name else props.get("email", "Unknown")
    except:
        return "Unknown"

# ---------------------------------------------------------------------------
# STEP 2 — PULL HUBSPOT MEETINGS
# ---------------------------------------------------------------------------

def pull_hs_meetings(hs_key, owner_id, lookback_days):
    section("STEP 2 — HUBSPOT MEETINGS FOR OWNER")
    headers = {"Authorization": f"Bearer {hs_key}", "Content-Type": "application/json"}

    now_utc   = datetime.now(timezone.utc)
    since_utc = now_utc - timedelta(days=lookback_days)
    now_ms    = int(now_utc.timestamp() * 1000)
    since_ms  = int(since_utc.timestamp() * 1000)

    print(f"\n  Owner ID : {owner_id}")
    print(f"  Window   : {since_utc.strftime('%Y-%m-%d %H:%M')} UTC -> {now_utc.strftime('%Y-%m-%d %H:%M')} UTC")

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
            "hs_meeting_outcome", "hs_attendee_owner_ids", "hubspot_owner_id"
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
            print(f"  ❌  {res.status_code}: {res.text[:200]}")
            return []

        results = res.json().get("results", [])
        print(f"\n  ✅  {len(results)} meetings found\n")
        print(f"  {'#':<4} {'id':<14} {'outcome':<12} {'dur':>5}  {'start_time':<26} title")
        print(f"  {'-'*4} {'-'*14} {'-'*12} {'-'*5}  {'-'*26} {'-'*35}")

        meetings = []
        for i, m in enumerate(results):
            props   = m.get("properties", {})
            start   = props.get("hs_meeting_start_time", "")
            end     = props.get("hs_meeting_end_time", "")
            dur     = duration_mins(start, end)
            outcome = props.get("hs_meeting_outcome") or "—"
            title   = props.get("hs_meeting_title") or "Untitled"
            # Fetch associated contact name
            contact_name = fetch_meeting_contact(m.get("id"), headers)

            print(f"  {i:<4} {m.get('id','?'):<14} {outcome:<12} {dur:>4}m  {start:<26} {title[:32]} | {contact_name}")
            meetings.append({
                "hs_id": m.get("id"), "title": title,
                "start": parse_dt(start), "end": parse_dt(end),
                "duration": dur, "outcome": outcome, "start_str": start,
                "contact_name": contact_name
            })
        return meetings
    except Exception as e:
        print(f"  ❌  Exception: {e}")
        return []


# ---------------------------------------------------------------------------
# STEP 3 — PULL WORDLY TRANSCRIPTS
# ---------------------------------------------------------------------------

def get_session_state(session_id, headers):
    try:
        res = requests.get(
            f"{WORDLY_BASE_URL}/sessions/{session_id}",
            headers=headers, timeout=10
        )
        return res.json().get("state", "unknown") if res.status_code == 200 else f"error_{res.status_code}"
    except:
        return "exception"


def pull_wordly_transcripts(wordly_key, lookback_days, min_duration):
    section("STEP 3 — WORDLY TRANSCRIPTS")
    headers = {"x-wordly-api-key": wordly_key}

    now_utc   = datetime.now(timezone.utc)
    since_utc = now_utc - timedelta(days=lookback_days)

    print(f"\n  Window       : {since_utc.strftime('%Y-%m-%d %H:%M')} UTC -> {now_utc.strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"  Min duration : {min_duration} mins")

    try:
        res = requests.get(
            f"{WORDLY_BASE_URL}/transcripts?page=1&limit=50",
            headers=headers, timeout=15
        )
        if res.status_code != 200:
            print(f"  ❌  {res.status_code}")
            return []

        all_t     = res.json().get("transcripts", [])
        in_window = [
            t for t in all_t
            if (parse_dt(t.get("startTime")) or datetime.min.replace(tzinfo=timezone.utc)) >= since_utc
        ]

        print(f"  Total from API : {len(all_t)}")
        print(f"  In window      : {len(in_window)}\n")
        print(f"  {'#':<4} {'sessionId':<12} {'state':<10} {'dur':>5}  {'startTime':<26} {'action':<8} title")
        print(f"  {'-'*4} {'-'*12} {'-'*10} {'-'*5}  {'-'*26} {'-'*8} {'-'*30}")

        transcripts = []
        for i, t in enumerate(in_window):
            sid   = t.get("sessionId", "?")
            start = t.get("startTime", "")
            end   = t.get("endTime", "")
            dur   = duration_mins(start, end)
            title = t.get("title", "?")
            t_id  = t.get("transcriptId")
            state = get_session_state(sid, headers)

            if state != "ended":
                action = "SKIP"
            elif 0 <= dur < min_duration:
                action = "SKIP"
            else:
                action = "KEEP"

            state_disp = state.upper() if state in ("created","started","ended") else state
            print(f"  {i:<4} {sid:<12} {state_disp:<10} {dur:>4}m  {start:<26} {action:<8} {title[:30]}")

            if action == "KEEP":
                transcripts.append({
                    "transcript_id": t_id, "session_id": sid, "title": title,
                    "start": parse_dt(start), "end": parse_dt(end),
                    "duration": dur, "start_str": start
                })

        print(f"\n  Kept: {len(transcripts)}  |  Skipped: {len(in_window) - len(transcripts)}")
        return transcripts

    except Exception as e:
        print(f"  ❌  Exception: {e}")
        return []


# ---------------------------------------------------------------------------
# STEP 4 — ONE-TO-ONE GREEDY MATCH
# ---------------------------------------------------------------------------

def match(transcripts, meetings, window_mins):
    section("STEP 4 — MATCHING (one-to-one, greedy)")
    print(f"\n  Match window   : +/- {window_mins} mins")
    print(f"  HIGH threshold : <= {HIGH_THRESHOLD} mins")
    print(f"  Transcripts    : {len(transcripts)}")
    print(f"  HS Meetings    : {len(meetings)}")

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

    used_transcripts = set()
    used_meetings    = set()
    assigned         = []

    for c in candidates:
        t_id = c["transcript"]["transcript_id"]
        m_id = c["meeting"]["hs_id"]
        if t_id not in used_transcripts and m_id not in used_meetings:
            assigned.append(c)
            used_transcripts.add(t_id)
            used_meetings.add(m_id)

    for t in transcripts:
        if t["transcript_id"] not in used_transcripts:
            assigned.append({
                "transcript": t, "meeting": None,
                "delta_mins": None, "score": 0, "confidence": "NONE"
            })

    assigned.sort(key=lambda x: x["transcript"]["start"] or datetime.min.replace(tzinfo=timezone.utc))

    print(f"\n  {'conf':<8} {'delta':>6}  {'transcript_start':<26} {'meeting_start':<26} titles")
    print(f"  {'-'*8} {'-'*6}  {'-'*26} {'-'*26} {'-'*50}")

    for r in assigned:
        t       = r["transcript"]
        m       = r["meeting"]
        conf    = r["confidence"]
        delta   = f"{r['delta_mins']}m" if r["delta_mins"] is not None else "—"
        t_disp  = t["title"][:25]
        m_disp  = m["title"][:25] if m else "⚠️  NO MATCH — unlogged call?"
        m_start = m["start_str"] if m else "—"
        icon    = CONF_ICON.get(conf, "?")
        print(f"  {icon} {conf:<6} {delta:>6}  {t['start_str']:<26} {m_start:<26} {t_disp} / {m_disp}")

    counts = Counter(r["confidence"] for r in assigned)
    print(f"\n  Summary — HIGH: {counts['HIGH']}  MEDIUM: {counts['MEDIUM']}  LOW: {counts['LOW']}  NONE: {counts['NONE']}")
    return assigned


# ---------------------------------------------------------------------------
# STEP 5 — PULL TRANSCRIPT TEXT
# ---------------------------------------------------------------------------

def pull_transcript_texts(matches, wordly_key):
    section("STEP 5 — TRANSCRIPT TEXT PULL (HIGH + MEDIUM)")
    headers = {"x-wordly-api-key": wordly_key}

    eligible = [r for r in matches if r["confidence"] in ("HIGH", "MEDIUM")]
    print(f"\n  Eligible matches: {len(eligible)}")

    pulled = []
    for r in eligible:
        t       = r["transcript"]
        t_id    = t["transcript_id"]
        m       = r["meeting"]
        m_title = m["title"] if m else "—"

        print(f"\n  [{r['confidence']}] {t['title'][:45]} | {t['start_str']}")
        print(f"         -> HS: {m_title[:50]}")

        text, status = download_transcript(t_id, headers)

        if status in ("ok", "partial"):
            flag = "✅" if status == "ok" else "⚠️  PARTIAL"
            print(f"         {flag}  {len(text)} chars")
            r["text"] = text
            pulled.append(r)
        else:
            print(f"         ❌  {status}")

    print(f"\n  Successfully pulled: {len(pulled)} / {len(eligible)}")
    return pulled


# ---------------------------------------------------------------------------
# STEP 6 — GEMINI SUMMARIZATION
# ---------------------------------------------------------------------------

def gemini_summarize(transcript_text, prompt, gemini_key):
    """Send transcript + prompt to Gemini. Returns summary string or None."""
    payload = {
        "contents": [{
            "parts": [{
                "text": f"{prompt}\n\nTRANSCRIPT:\n{transcript_text}"
            }]
        }]
    }
    try:
        res = requests.post(
            f"{GEMINI_URL}?key={gemini_key}",
            json=payload,
            timeout=60
        )
        if res.status_code == 200:
            data = res.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]
        else:
            return f"ERROR {res.status_code}: {res.text[:300]}"
    except Exception as e:
        return f"EXCEPTION: {e}"


def run_summarization(pulled, gemini_key, prompt_hs, prompt_mgmt, salesperson_name):
    section("STEP 6 — GEMINI SUMMARIZATION")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    print(f"\n  Output folder: {OUTPUT_DIR}/")
    print(f"  Processing {len(pulled)} transcript(s)...\n")

    all_summaries = []

    to_process = pulled[:MAX_TO_SUMMARIZE]
    print(f"  Summarizing {len(to_process)} of {len(pulled)} (MAX_TO_SUMMARIZE={MAX_TO_SUMMARIZE})")

    for i, r in enumerate(to_process):
        t        = r["transcript"]
        m        = r["meeting"]
        text     = r.get("text", "")
        m_title  = m["title"] if m else "Unmatched"
        m_start  = m["start_str"] if m else t["start_str"]
        conf     = r["confidence"]

        # Build a clean date string for filenames
        dt       = parse_dt(m_start) or parse_dt(t["start_str"])
        date_str = dt.strftime("%Y-%m-%d_%H%M") if dt else "unknown"
        contact    = m.get("contact_name", "Unknown") if m else "Unmatched"
        safe_name  = safe_filename(f"{salesperson_name}_{date_str}_{contact}")

        print(f"  [{i+1}/{len(pulled)}] {m_title[:50]}")
        print(f"           Start : {m_start}")
        print(f"           Conf  : {conf}")

        # --- HubSpot Summary ---
        print(f"           Generating HubSpot summary...", end=" ", flush=True)
        hs_summary = gemini_summarize(text, prompt_hs, gemini_key)
        print("done" if not hs_summary.startswith("ERROR") and not hs_summary.startswith("EXCEPTION") else hs_summary[:60])

        # Save raw transcript
        raw_file = os.path.join(OUTPUT_DIR, f"{safe_name}_TRANSCRIPT.txt")
        with open(raw_file, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"           Saved: {raw_file}")

        hs_file = os.path.join(OUTPUT_DIR, f"{safe_name}_HS.txt")
        with open(hs_file, "w", encoding="utf-8") as f:
            f.write(f"HUBSPOT MEETING SUMMARY\n")
            f.write(f"{'='*60}\n")
            f.write(f"Salesperson : {salesperson_name}\n")
            f.write(f"Meeting     : {m_title}\n")
            f.write("Customer    : " + (m.get("contact_name", "Unknown") if m else "Unknown") + "\n")
            f.write(f"Start Time  : {m_start}\n")
            f.write(f"HS ID       : {m['hs_id'] if m else 'UNMATCHED'}\n")
            f.write(f"Match Conf  : {conf}\n")
            f.write(f"Generated   : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"{'='*60}\n\n")
            f.write(hs_summary)
        print(f"           Saved: {hs_file}")

        # --- Sales Management Audit ---
        # Brief pause to avoid Gemini rate limiting
        time.sleep(1)
        print(f"           Generating sales audit...", end=" ", flush=True)
        mgmt_summary = gemini_summarize(text, prompt_mgmt, gemini_key)
        print("done" if not mgmt_summary.startswith("ERROR") and not mgmt_summary.startswith("EXCEPTION") else mgmt_summary[:60])

        mgmt_file = os.path.join(OUTPUT_DIR, f"{safe_name}_AUDIT.txt")
        with open(mgmt_file, "w", encoding="utf-8") as f:
            f.write(f"SALES MANAGEMENT AUDIT\n")
            f.write(f"{'='*60}\n")
            f.write(f"Salesperson : {salesperson_name}\n")
            f.write(f"Meeting     : {m_title}\n")
            f.write("Customer    : " + (m.get("contact_name", "Unknown") if m else "Unknown") + "\n")
            f.write(f"Start Time  : {m_start}\n")
            f.write(f"HS ID       : {m['hs_id'] if m else 'UNMATCHED'}\n")
            f.write(f"Match Conf  : {conf}\n")
            f.write(f"Generated   : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"{'='*60}\n\n")
            f.write(mgmt_summary)
        print(f"           Saved: {mgmt_file}\n")

        all_summaries.append({
            "match": r,
            "hs_summary": hs_summary,
            "mgmt_summary": mgmt_summary,
            "hs_file": hs_file,
            "mgmt_file": mgmt_file
        })

        # Rate limit buffer between transcripts
        if i < len(pulled) - 1:
            time.sleep(2)

    print(f"  Done. {len(all_summaries)} transcript(s) summarized.")
    print(f"  Files written to: ./{OUTPUT_DIR}/")
    return all_summaries


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    print()
    print("=" * 70)
    print("  WORDLY SALES INTELLIGENCE PIPELINE — MATCHER v1.0d")
    print(f"  Target : {TARGET_EMAIL}")
    print(f"  Window : last {LOOKBACK_DAYS} days")
    print(f"  Run at : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    print("\nLoading keys and prompts...")
    wordly_key  = load_key(WORDLY_KEY_FILE)
    hs_key      = load_key(HS_KEY_FILE)
    slack_url   = load_key(SLACK_WEBHOOK_FILE)
    gemini_key  = load_key(GEMINI_KEY_FILE)
    prompt_hs   = load_prompt(PROMPT_HS_FILE)
    prompt_mgmt = load_prompt(PROMPT_MGMT_FILE)

    if not wordly_key or not hs_key:
        print("\n⛔  Cannot proceed — missing Wordly or HubSpot key.")
        sys.exit(1)
    if not gemini_key:
        print("\n⛔  Cannot proceed — missing Gemini API key.")
        sys.exit(1)
    if not prompt_hs or not prompt_mgmt:
        print("\n⛔  Cannot proceed — missing prompt files.")
        sys.exit(1)

    owner = resolve_owner(hs_key, TARGET_EMAIL)
    if not owner:
        sys.exit(1)

    salesperson_name = f"{owner.get('firstName','')} {owner.get('lastName','')}".strip()

    meetings    = pull_hs_meetings(hs_key, owner["id"], LOOKBACK_DAYS)
    transcripts = pull_wordly_transcripts(wordly_key, LOOKBACK_DAYS, MIN_DURATION_MINS)

    if not transcripts:
        print("\n⚠️  No transcripts to process.")
        sys.exit(0)

    matches = match(transcripts, meetings, MATCH_WINDOW_MINS)
    pulled  = pull_transcript_texts(matches, wordly_key)

    if not pulled:
        print("\n⚠️  No transcript text pulled. Cannot summarize.")
        sys.exit(0)

    summaries = run_summarization(pulled, gemini_key, prompt_hs, prompt_mgmt, salesperson_name)

    # Slack notification
    counts = Counter(r["confidence"] for r in matches)
    slack_notify(
        slack_url,
        f"✅ *Pipeline complete* — `{salesperson_name}`\n"
        f"Transcripts matched: {len(pulled)}  |  Summaries generated: {len(summaries)}\n"
        f"Match quality — HIGH: {counts['HIGH']}  MEDIUM: {counts['MEDIUM']}  "
        f"LOW: {counts['LOW']}  NONE: {counts['NONE']}\n"
        f"Files saved to `./summaries/`"
    )

    section("PIPELINE COMPLETE")
    print(f"\n  Summaries written: {len(summaries)}")
    print(f"  Folder: ./{OUTPUT_DIR}/")
    print(f"\n  Open the files and review before we wire up HubSpot writes.\n")


if __name__ == "__main__":
    main()
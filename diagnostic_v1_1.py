"""
PROJECT: Wordly Sales Intelligence Pipeline
SCRIPT:  diagnostic_v1_1.py
VERSION: 1.1c
CHANGES: Added Slack webhook test. Added Wordly session state check before
         transcript pull. Fixed HS lookback to correctly bound upper end
         so future meetings are excluded. 2-day window now accurate.
AUTHOR:  Built with Claude
DATE:    2026-05-07
"""

import requests
import json
import os
import sys
from datetime import datetime, timezone, timedelta

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
WORDLY_KEY_FILE    = "Aleksandra_Laszczyk_Mendez_WordlyAPI.txt"
HS_KEY_FILE        = "HS_Service_key.txt"
SLACK_WEBHOOK_FILE = "slack_webhook.txt"

WORDLY_BASE_URL    = "https://api.wordly.ai"
HS_BASE_URL        = "https://api.hubapi.com"

# Lookback window for both Wordly and HubSpot
LOOKBACK_DAYS = 2

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def load_key(filename):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    if not os.path.exists(path):
        print(f"  ❌  Key file not found: {path}")
        return None
    with open(path, "r") as f:
        key = f.read().strip()
    if not key:
        print(f"  ❌  Key file is empty: {filename}")
        return None
    print(f"  ✅  Loaded: {filename} ({len(key)} chars)")
    return key


def pretty(data, indent=2):
    return json.dumps(data, indent=indent, default=str)


def section(title):
    print()
    print("=" * 70)
    print(f"  {title}")
    print("=" * 70)


def result_block(label, data):
    print(f"\n--- {label} ---")
    print(pretty(data))


# ---------------------------------------------------------------------------
# SLACK TEST
# ---------------------------------------------------------------------------

def test_slack(webhook_url):
    section("SLACK — WEBHOOK TEST")
    print("\n[1] Firing test message to #Sales_Summaries...")
    payload = {
        "text": "✅ *Wordly Sales Intelligence Pipeline* — Slack webhook confirmed. Pipeline diagnostics running."
    }
    try:
        res = requests.post(webhook_url, json=payload, timeout=10)
        if res.status_code == 200 and res.text == "ok":
            print("  ✅  Slack message delivered successfully.")
        else:
            print(f"  ❌  Slack returned {res.status_code}: {res.text}")
    except Exception as e:
        print(f"  ❌  Exception: {e}")


# ---------------------------------------------------------------------------
# WORDLY — SESSION STATE CHECK + TRANSCRIPT PULL
# ---------------------------------------------------------------------------

def get_session_state(session_id, headers):
    """Returns state string: created | started | ended | unknown"""
    try:
        res = requests.get(
            f"{WORDLY_BASE_URL}/sessions/{session_id}",
            headers=headers,
            timeout=10
        )
        if res.status_code == 200:
            return res.json().get("state", "unknown")
        else:
            return f"error_{res.status_code}"
    except Exception as e:
        return f"exception: {e}"


def wordly_transcript_pull(api_key):
    section("WORDLY — SESSION STATE + TRANSCRIPT PULL")
    headers = {"x-wordly-api-key": api_key}

    now_utc   = datetime.now(timezone.utc)
    since_utc = now_utc - timedelta(days=LOOKBACK_DAYS)

    print(f"\n[1] Fetching transcripts from last {LOOKBACK_DAYS} days...")
    print(f"    Window: {since_utc.strftime('%Y-%m-%d %H:%M')} UTC -> {now_utc.strftime('%Y-%m-%d %H:%M')} UTC")

    try:
        res = requests.get(
            f"{WORDLY_BASE_URL}/transcripts?page=1&limit=20",
            headers=headers,
            timeout=15
        )
        if res.status_code != 200:
            print(f"  ❌  List fetch failed: {res.status_code}")
            return

        all_transcripts = res.json().get("transcripts", [])

        # Filter to lookback window
        transcripts = []
        for t in all_transcripts:
            try:
                start = datetime.fromisoformat(t["startTime"].replace("Z", "+00:00"))
                if start >= since_utc:
                    transcripts.append(t)
            except:
                pass

        print(f"  Total returned by API: {len(all_transcripts)}")
        print(f"  Within {LOOKBACK_DAYS}-day window: {len(transcripts)}")

        if not transcripts:
            print("  ⚠️  No transcripts in window. Widening to show first 5 available.")
            transcripts = all_transcripts[:5]

        # Table with duration and session state
        print(f"\n  {'#':<4} {'sessionId':<12} {'state':<10} {'dur':>5}  {'startTime':<26} title")
        print(f"  {'-'*4} {'-'*12} {'-'*10} {'-'*5}  {'-'*26} {'-'*35}")

        state_cache = {}
        for i, t in enumerate(transcripts):
            start = t.get("startTime", "?")
            end   = t.get("endTime", "?")
            sid   = t.get("sessionId", "?")
            try:
                s   = datetime.fromisoformat(start.replace("Z", "+00:00"))
                e   = datetime.fromisoformat(end.replace("Z", "+00:00"))
                dur = f"{int((e-s).total_seconds()/60)}m"
            except:
                dur = "?"

            state = get_session_state(sid, headers)
            state_cache[t.get("transcriptId")] = state
            state_display = state.upper() if state in ("created","started","ended") else state

            print(
                f"  {i:<4} {sid:<12} {state_display:<10} {dur:>5}  {start:<26} {t.get('title','?')[:40]}"
            )

        # Attempt transcript pull — ended sessions only
        print(f"\n[2] Attempting transcript text pull (ended sessions only)...")
        pulled = False
        for i, t in enumerate(transcripts):
            t_id  = t.get("transcriptId")
            sid   = t.get("sessionId", "?")
            state = state_cache.get(t_id, "unknown")
            title = t.get("title", "?")

            print(f"\n  Candidate {i}: {t_id} | session {sid} | state={state} | {title[:40]}")

            if state != "ended":
                print(f"  ⏭️   Skipping — session is {state.upper()}, transcript not ready.")
                continue

            url = f"{WORDLY_BASE_URL}/transcripts/{t_id}/original?format=txt&speaker_names=true"
            try:
                dl_res = requests.get(url, headers=headers, timeout=30, stream=False)
                print(f"  HTTP Status: {dl_res.status_code}")

                if dl_res.status_code == 200:
                    text       = dl_res.text
                    char_count = len(text)
                    line_count = text.count("\n")
                    print(f"  ✅  Success — {char_count} chars, ~{line_count} lines")
                    print(f"\n  --- First 600 chars ---")
                    print(text[:600])
                    print(f"\n  --- Last 200 chars ---")
                    print(text[-200:] if len(text) > 200 else "(full text shown above)")
                    pulled = True
                    break

                elif dl_res.status_code == 404:
                    print(f"  ⚠️  404 — file not on server yet. Trying next.")
                else:
                    print(f"  ❌  {dl_res.status_code}: {dl_res.text[:200]}")

            except requests.exceptions.ChunkedEncodingError:
                print(f"  ⚠️  ChunkedEncodingError. Trying next.")
            except requests.exceptions.Timeout:
                print(f"  ⚠️  Timeout. Trying next.")
            except Exception as e:
                print(f"  ⚠️  Exception: {e}. Trying next.")

            if i >= 6:
                print("\n  Reached attempt cap.")
                break

        if not pulled:
            print("\n  ❌  Could not pull text from any ended session in window.")

    except Exception as e:
        print(f"  ❌  Outer exception: {e}")


# ---------------------------------------------------------------------------
# HUBSPOT — RECENT MEETINGS (past only, no future)
# ---------------------------------------------------------------------------

def hubspot_recent_meetings(api_key):
    section(f"HUBSPOT — MEETINGS (last {LOOKBACK_DAYS} days, past only)")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    now_utc   = datetime.now(timezone.utc)
    since_utc = now_utc - timedelta(days=LOOKBACK_DAYS)
    now_ms    = int(now_utc.timestamp() * 1000)
    since_ms  = int(since_utc.timestamp() * 1000)

    print(f"\n    Window: {since_utc.strftime('%Y-%m-%d %H:%M')} UTC -> {now_utc.strftime('%Y-%m-%d %H:%M')} UTC")

    search_payload = {
        "filterGroups": [
            {
                "filters": [
                    {
                        "propertyName": "hs_meeting_start_time",
                        "operator": "GTE",
                        "value": str(since_ms)
                    },
                    {
                        "propertyName": "hs_meeting_start_time",
                        "operator": "LTE",
                        "value": str(now_ms)
                    }
                ]
            }
        ],
        "properties": [
            "hs_meeting_title",
            "hs_meeting_start_time",
            "hs_meeting_end_time",
            "hs_meeting_outcome",
            "hs_attendee_owner_ids",
            "hubspot_owner_id"
        ],
        "sorts": [{"propertyName": "hs_meeting_start_time", "direction": "DESCENDING"}],
        "limit": 10
    }

    print(f"\n[1] Searching HubSpot meetings in window...")
    try:
        res = requests.post(
            f"{HS_BASE_URL}/crm/v3/objects/meetings/search",
            headers=headers,
            json=search_payload,
            timeout=15
        )
        print(f"  HTTP Status: {res.status_code}")

        if res.status_code == 200:
            data    = res.json()
            results = data.get("results", [])
            total   = data.get("total", 0)
            print(f"  ✅  Total matches: {total} | Showing: {len(results)}")

            if results:
                result_block("meetings[0] — full shape", results[0])

                print(f"\n  {'id':<14} {'owner_id':<12} {'attendee_ids':<22} {'start_time':<26} title")
                print(f"  {'-'*14} {'-'*12} {'-'*22} {'-'*26} {'-'*35}")
                for m in results:
                    props = m.get("properties", {})
                    print(
                        f"  {m.get('id','?'):<14} "
                        f"{str(props.get('hubspot_owner_id','null')):<12} "
                        f"{str(props.get('hs_attendee_owner_ids','null'))[:20]:<22} "
                        f"{str(props.get('hs_meeting_start_time','?')):<26} "
                        f"{str(props.get('hs_meeting_title','?'))[:40]}"
                    )

                # Contact associations on first result
                first_id = results[0].get("id")
                print(f"\n[2] Contact associations for meeting {first_id}...")
                ca = requests.get(
                    f"{HS_BASE_URL}/crm/v3/objects/meetings/{first_id}/associations/contacts",
                    headers=headers, timeout=10
                )
                print(f"  HTTP Status: {ca.status_code}")
                if ca.status_code == 200:
                    result_block("Contact associations", ca.json())
                else:
                    print(f"  ⚠️  {ca.status_code}: {ca.text[:200]}")

            else:
                print(f"  ⚠️  No meetings found in window. Try widening LOOKBACK_DAYS.")

        elif res.status_code == 403:
            print("  ❌  403 — Missing scope: crm.objects.meetings.read")
        else:
            print(f"  ❌  {res.status_code}: {res.text[:400]}")

    except Exception as e:
        print(f"  ❌  Exception: {e}")

    # Owner list
    print(f"\n[3] Owner list...")
    try:
        res = requests.get(f"{HS_BASE_URL}/crm/v3/owners?limit=25", headers=headers, timeout=10)
        if res.status_code == 200:
            owners = res.json().get("results", [])
            print(f"  ✅  {len(owners)} owners")
            print(f"\n  {'ownerId':<12} {'email':<38} name")
            print(f"  {'-'*12} {'-'*38} {'-'*25}")
            for o in owners:
                name = f"{o.get('firstName','')} {o.get('lastName','')}".strip()
                print(f"  {str(o.get('id','?')):<12} {o.get('email','?'):<38} {name}")
        else:
            print(f"  ❌  {res.status_code}")
    except Exception as e:
        print(f"  ❌  Exception: {e}")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    print()
    print("=" * 70)
    print("  WORDLY SALES INTELLIGENCE PIPELINE — DIAGNOSTIC v1.1c")
    print(f"  Run at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)
    print("\nLoading keys...")

    wordly_key = load_key(WORDLY_KEY_FILE)
    hs_key     = load_key(HS_KEY_FILE)
    slack_url  = load_key(SLACK_WEBHOOK_FILE)

    if not wordly_key or not hs_key:
        print("\n⛔  Cannot proceed — check Wordly and HubSpot key files.")
        sys.exit(1)

    if slack_url:
        test_slack(slack_url)
    else:
        print("\n  ⚠️  Slack webhook not found — skipping Slack test.")

    wordly_transcript_pull(wordly_key)
    hubspot_recent_meetings(hs_key)

    section("DIAGNOSTIC COMPLETE")
    print("\nPaste output back and we build the matcher.\n")


if __name__ == "__main__":
    main()
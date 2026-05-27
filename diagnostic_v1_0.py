"""
PROJECT: Wordly Sales Intelligence Pipeline
SCRIPT:  diagnostic_v1_0.py
VERSION: 1.0
PURPOSE: Phase 1 diagnostic — confirm Wordly and HubSpot API access,
         map data shapes, surface what we have to work with.
         NO downloading. NO writing. READ ONLY.
AUTHOR:  Built with Claude
DATE:    2026-05-07
"""

import requests
import json
import os
import sys
from datetime import datetime

# ---------------------------------------------------------------------------
# CONFIG — key files expected alongside this script
# ---------------------------------------------------------------------------
WORDLY_KEY_FILE  = "Aleksandra_Laszczyk_Mendez_WordlyAPI.txt"
HS_KEY_FILE      = "HS_Service_key.txt"

WORDLY_BASE_URL  = "https://api.wordly.ai"

# HubSpot base — v3 CRM API
HS_BASE_URL      = "https://api.hubapi.com"

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def load_key(filename):
    """Read a raw key from a single-line text file. Strips whitespace."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    if not os.path.exists(path):
        print(f"  ❌  Key file not found: {path}")
        return None
    with open(path, "r") as f:
        key = f.read().strip()
    if not key:
        print(f"  ❌  Key file is empty: {filename}")
        return None
    print(f"  ✅  Loaded key from {filename} ({len(key)} chars)")
    return key


def pretty(data, indent=2):
    """Pretty-print a dict/list as JSON."""
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
# WORDLY DIAGNOSTICS
# ---------------------------------------------------------------------------

def wordly_diagnostics(api_key):
    section("WORDLY API DIAGNOSTICS")
    headers = {"x-wordly-api-key": api_key}

    # ---- 1. Pull first page of transcripts --------------------------------
    print("\n[1] Fetching first page of transcripts (limit=5)...")
    try:
        res = requests.get(
            f"{WORDLY_BASE_URL}/transcripts?page=1&limit=5",
            headers=headers,
            timeout=15
        )
        print(f"    HTTP Status: {res.status_code}")

        if res.status_code == 200:
            data = res.json()
            total      = data.get("total", "unknown")
            transcripts = data.get("transcripts", [])
            print(f"    Total transcripts on account: {total}")
            print(f"    Returned in this page: {len(transcripts)}")

            if transcripts:
                # Show full shape of first record so we know every field
                print("\n[2] Full object shape — first transcript:")
                result_block("transcript[0]", transcripts[0])

                # Show key fields across all returned records
                print("\n[3] Key fields across returned transcripts:")
                print(f"  {'transcriptId':<30} {'sessionId':<15} {'startTime':<25} {'title'}")
                print(f"  {'-'*30} {'-'*15} {'-'*25} {'-'*30}")
                for t in transcripts:
                    print(
                        f"  {t.get('transcriptId','?'):<30} "
                        f"{t.get('sessionId','?'):<15} "
                        f"{str(t.get('startTime','?')):<25} "
                        f"{t.get('title','?')}"
                    )

                # ---- 2. Try pulling the most recent transcript text --------
                first_id = transcripts[0].get("transcriptId")
                if first_id:
                    print(f"\n[4] Fetching raw text for transcript: {first_id}")
                    dl_url = f"{WORDLY_BASE_URL}/transcripts/{first_id}/original?format=txt&speaker_names=true"
                    dl_res = requests.get(dl_url, headers=headers, timeout=20)
                    print(f"    HTTP Status: {dl_res.status_code}")
                    if dl_res.status_code == 200:
                        preview = dl_res.text[:500]
                        print(f"    ✅  Text received. First 500 chars:\n")
                        print(f"    {preview}")
                        print(f"\n    Total chars in transcript: {len(dl_res.text)}")
                    else:
                        print(f"    ❌  Download failed: {dl_res.text[:300]}")
            else:
                print("    ⚠️  No transcripts returned. Account may be empty or key scoped incorrectly.")

        elif res.status_code == 401:
            print("    ❌  401 Unauthorized — API key rejected or wrong header format.")
        elif res.status_code == 403:
            print("    ❌  403 Forbidden — Key valid but insufficient permissions.")
        else:
            print(f"    ❌  Unexpected status. Body: {res.text[:300]}")

    except requests.exceptions.Timeout:
        print("    ❌  Request timed out.")
    except Exception as e:
        print(f"    ❌  Exception: {e}")


# ---------------------------------------------------------------------------
# HUBSPOT DIAGNOSTICS
# ---------------------------------------------------------------------------

def hubspot_diagnostics(api_key):
    section("HUBSPOT API DIAGNOSTICS")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    # ---- 1. Token introspection — what scopes do we have? -----------------
    print("\n[1] Checking token info / scopes...")
    try:
        res = requests.get(
            f"{HS_BASE_URL}/oauth/v1/access-tokens/{api_key}",
            timeout=10
        )
        print(f"    HTTP Status: {res.status_code}")
        if res.status_code == 200:
            result_block("Token Info", res.json())
        else:
            print(f"    ⚠️  Token introspection returned {res.status_code} — may be a private app key (expected). Continuing.")
    except Exception as e:
        print(f"    ⚠️  Token introspection skipped: {e}")

    # ---- 2. Pull contacts — basic read test --------------------------------
    print("\n[2] Fetching first 3 contacts (basic read test)...")
    try:
        res = requests.get(
            f"{HS_BASE_URL}/crm/v3/objects/contacts?limit=3&properties=firstname,lastname,email",
            headers=headers,
            timeout=15
        )
        print(f"    HTTP Status: {res.status_code}")
        if res.status_code == 200:
            data = res.json()
            results = data.get("results", [])
            print(f"    ✅  Contacts readable. Returned {len(results)} records.")
            if results:
                result_block("contacts[0]", results[0])
        elif res.status_code == 401:
            print("    ❌  401 — Key rejected.")
        elif res.status_code == 403:
            print("    ❌  403 — No contacts read permission.")
        else:
            print(f"    ❌  {res.status_code}: {res.text[:300]}")
    except Exception as e:
        print(f"    ❌  Exception: {e}")

    # ---- 3. Pull meetings (engagements) ------------------------------------
    print("\n[3] Fetching meetings via engagements API...")
    try:
        res = requests.get(
            f"{HS_BASE_URL}/crm/v3/objects/meetings?limit=3&properties=hs_meeting_title,hs_meeting_start_time,hs_meeting_end_time,hs_attendee_owner_ids",
            headers=headers,
            timeout=15
        )
        print(f"    HTTP Status: {res.status_code}")
        if res.status_code == 200:
            data = res.json()
            results = data.get("results", [])
            print(f"    ✅  Meetings readable. Returned {len(results)} records.")
            if results:
                result_block("meetings[0]", results[0])
                print("\n    Key fields across returned meetings:")
                print(f"  {'id':<15} {'title':<35} {'start_time'}")
                print(f"  {'-'*15} {'-'*35} {'-'*25}")
                for m in results:
                    props = m.get("properties", {})
                    print(
                        f"  {m.get('id','?'):<15} "
                        f"{str(props.get('hs_meeting_title','?'))[:34]:<35} "
                        f"{props.get('hs_meeting_start_time','?')}"
                    )
        elif res.status_code == 403:
            print("    ❌  403 — No meetings read permission. Need 'crm.objects.meetings.read' scope added to key.")
        elif res.status_code == 401:
            print("    ❌  401 — Key rejected.")
        else:
            print(f"    ❌  {res.status_code}: {res.text[:300]}")
    except Exception as e:
        print(f"    ❌  Exception: {e}")

    # ---- 4. Pull owners (salespeople) -------------------------------------
    print("\n[4] Fetching HubSpot owners (salespeople)...")
    try:
        res = requests.get(
            f"{HS_BASE_URL}/crm/v3/owners?limit=10",
            headers=headers,
            timeout=15
        )
        print(f"    HTTP Status: {res.status_code}")
        if res.status_code == 200:
            data = res.json()
            results = data.get("results", [])
            print(f"    ✅  Owners readable. Returned {len(results)} records.")
            print(f"\n  {'ownerId':<12} {'email':<35} {'firstName':<15} {'lastName'}")
            print(f"  {'-'*12} {'-'*35} {'-'*15} {'-'*20}")
            for o in results:
                print(
                    f"  {str(o.get('id','?')):<12} "
                    f"{o.get('email','?'):<35} "
                    f"{o.get('firstName','?'):<15} "
                    f"{o.get('lastName','?')}"
                )
        elif res.status_code == 403:
            print("    ❌  403 — No owners read permission.")
        else:
            print(f"    ❌  {res.status_code}: {res.text[:300]}")
    except Exception as e:
        print(f"    ❌  Exception: {e}")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    print()
    print("=" * 70)
    print("  WORDLY SALES INTELLIGENCE PIPELINE — DIAGNOSTIC v1.0")
    print(f"  Run at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)
    print("\nLoading API keys...")

    wordly_key = load_key(WORDLY_KEY_FILE)
    hs_key     = load_key(HS_KEY_FILE)

    if not wordly_key:
        print("\n⛔  Cannot proceed without Wordly API key.")
        sys.exit(1)

    if not hs_key:
        print("\n⛔  Cannot proceed without HubSpot API key.")
        sys.exit(1)

    wordly_diagnostics(wordly_key)
    hubspot_diagnostics(hs_key)

    section("DIAGNOSTIC COMPLETE")
    print("\nNext steps depend on what came back above.")
    print("Share the output and we'll map the data shapes and flag any permission gaps.\n")


if __name__ == "__main__":
    main()

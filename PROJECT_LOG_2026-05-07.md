# WORDLY PIPELINE PROJECT LOG
**Generated:** 2026-05-07  
**Author:** Claude (Senior Engineer) + Chris Gillespie (Growth Engineering Architect)  
**Status:** Two working demos. Both parked for continuation.

---

## PROJECT 1: SALES INTELLIGENCE PIPELINE

### Overview
Automated pipeline that reconciles Wordly transcripts with HubSpot meetings per salesperson, generates two AI summaries per matched call (HubSpot meeting summary + sales management audit), and routes outputs to appropriate destinations. Built for internal Wordly sales team use.

### Current State
Working single-salesperson demo against Aleksandra Laszczyk-Mendez's account. All five core stages functional. Not yet multi-salesperson. No BigQuery or Cloud Run yet. HubSpot write not yet built.

### Working Scripts
- `matcher_v1_0.py` — main pipeline script (currently v1.0d)
- `prompt_hs.txt` — HubSpot summary prompt
- `prompt_sales_mgmt.txt` — sales audit prompt

### Key Files (local working directory)
```
/Users/cmgillespie/Library/CloudStorage/GoogleDrive-chris.gillespie@wordly.ai/My Drive/Code/Sales_summary_tool/
├── matcher_v1_0.py
├── prompt_hs.txt
├── prompt_sales_mgmt.txt
├── Aleksandra_Laszczyk_Mendez_WordlyAPI.txt
├── HS_Service_key.txt
├── gemini_api_key.txt
├── slack_webhook.txt
└── summaries/                  ← output folder, created automatically
```

### Technical Map

**Wordly REST API**
- Base URL: `https://api.wordly.ai`
- Auth header: `x-wordly-api-key` (NO api-version header — breaks auth)
- Transcript list: `GET /transcripts?page={n}&limit={n}`
- Session state: `GET /sessions/{sessionId}` → returns `state: created|started|ended`
- Transcript download: `GET /transcripts/{transcriptId}/original?format=txt&speaker_names=true`
- Transcript object fields: `transcriptId`, `sessionId`, `title`, `label`, `startTime`, `endTime`, `transcriptLanguageCodes`, `summaryLanguageCodes`

**HubSpot API**
- Base URL: `https://api.hubapi.com`
- Auth: `Authorization: Bearer {key}`
- Meetings search: `POST /crm/v3/objects/meetings/search` with filterGroups
- Owner field on meeting: `hubspot_owner_id` (primary), `hs_attendee_owner_ids` (secondary, semicolon-delimited)
- Owners list: `GET /crm/v3/owners?limit=100`
- Contact associations: `GET /crm/v3/objects/meetings/{id}/associations/contacts`
- Contact fetch: `GET /crm/v3/objects/contacts/{id}?properties=firstname,lastname,email`

**Gemini API**
- URL: `https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent`
- Auth: `?key={gemini_api_key}` as query param
- Billing: Pay-as-you-go enabled on Chris's personal CC for now
- Model: `gemini-2.5-flash` (gemini-2.0-flash is dead to new users as of March 2026)

**Slack**
- Channel: `#Sales_Summaries`
- Webhook: stored in `slack_webhook.txt` (raw URL, one line)

### Pipeline Stages (all working)
1. Resolve HubSpot owner by email → get owner ID
2. Pull HubSpot meetings for owner in lookback window (past only, GTE + LTE filter)
3. Pull Wordly transcripts for same window, check session state, filter short sessions
4. One-to-one greedy match by timestamp delta (HIGH ≤6min, MEDIUM ≤15min)
5. Download matched transcript text (chunked streaming — critical, see gotchas)
6. Gemini summarization → two files per transcript (_HS.txt, _AUDIT.txt) + raw _TRANSCRIPT.txt

### Config Variables (in script)
```python
TARGET_EMAIL       = "aleksandra.laszczyk@wordly.ai"
LOOKBACK_DAYS      = 2
MATCH_WINDOW_MINS  = 15
HIGH_THRESHOLD     = 6      # mins delta for HIGH confidence
MIN_DURATION_MINS  = 5      # filter noise/test sessions
MAX_TO_SUMMARIZE   = 1      # Gemini rate limit guard
```

### Owner → API Key Mapping (not yet built)
- Plan: Google Secret Manager
- Structure: HubSpot owner ID → email → Secret Manager secret name
- Secret naming convention: `wordly-apikey-firstname-lastname`
- Rationale: SOC 2 compliance, audit logging, key rotation without code changes
- Slack alert on 401 from Wordly API (key rotated/revoked)

### Last Known Good State
Run at 2026-05-07 16:57. Against Aleksandra's account. 7 HIGH matches, 1 NONE.
Summaries generated for 1 transcript (MAX_TO_SUMMARIZE=1). Files in `summaries/`.

### Next Steps (when returning)
1. Add `default_speaker` fallback config
2. Multi-salesperson loop (iterate owner list, load API key per salesperson)
3. BigQuery schema design + write layer
4. Google Secret Manager integration
5. Cloud Run + Cloud Scheduler deployment
6. HubSpot write (engagements) — needs write scope confirmed on key
7. Looker surface for sales audit output
8. Slack notification refinement (per-salesperson alerts)

---

## PROJECT 2: HOUSES OF WORSHIP (HOW) SUMMARIZER

### Overview
Silent background polling app for churches. Runs locally on Windows. Watches a Wordly account for new ended transcripts. When found, extracts preacher name, generates three denomination-aware social/newsletter outputs, saves files, fires Slack notification. Unattended operation.

### Current State
Working demo. Tested against Steven Furtick / Elevation Church sermon captured via VB Cable from YouTube. Three output formats working. Preacher extraction working when speaker is named in content.

### Working Scripts
- `how_summarizer.py` — main app (v1.0, with MAX_TO_PROCESS fix applied)
- `prompt_how.txt` — denomination context prompt (generic evangelical for demo)

### Key Files (local working directory)
```
/Users/cmgillespie/Library/CloudStorage/GoogleDrive-chris.gillespie@wordly.ai/My Drive/Code/HOW_summarizer/
├── how_summarizer.py
├── prompt_how.txt
├── CGillespie_WordlyAPI.txt
├── gemini_api_key.txt
├── slack_webhook.txt
├── how_processed.json          ← state file, auto-created
└── summaries/                  ← output folder, auto-created
```

### Output Files Per Transcript
```
{date}_{time}_{PreacherName}_TRANSCRIPT.txt   ← raw Wordly text
{date}_{time}_{PreacherName}_TWITTER.txt      ← ≤280 chars, hard enforced
{date}_{time}_{PreacherName}_FACEBOOK.txt     ← ~400-512 chars
{date}_{time}_{PreacherName}_NEWSLETTER.txt   ← 3 paragraphs, bible study narrative
```

### Config Variables (in script)
```python
POLL_INTERVAL_SECS = 60
MIN_DURATION_MINS  = 10     # filters test/accidental sessions
MAX_TO_PROCESS     = 1      # newest transcript only per run
STATE_FILE         = "how_processed.json"
```

### Denomination Customization
- Current: `prompt_how.txt` — generic evangelical/non-denominational
- Plan: One prompt file per denomination, swapped at install time
- Future: JSON config structure with denomination, bible_version, hashtags, salutation
- Bible Gateway link format: `https://www.biblegateway.com/passage/?search={verse}&version={version}`
- Version codes: NIV (evangelical), KJV (Baptist/traditional), DRA (Catholic), ESV, NASB, NLT

### Planned Denomination Configs
- Generic Evangelical (current)
- Catholic
- Baptist
- Mormon / LDS
- Jewish
- Islam / Friday Khutbah

### Last Known Good State
Run 2026-05-07. Furtick sermon via YouTube/VB Cable. Preacher name came back
"Unknown Speaker" (Furtick didn't self-introduce — congregation knows him).
Three outputs generated successfully. Slack fired. Content quality: good.

### Next Steps (when returning)
1. `default_speaker` fallback in config (church sets pastor name once at install)
2. Denomination JSON config structure
3. Bible Gateway link extraction in newsletter prompt
4. PyInstaller packaging for Windows deployment
5. Installer with denomination picker UI
6. Output language selection (non-English congregations)
7. Google Drive / O365 output option (post-install config)
8. Evaluate notification alternatives to Slack for church environments (SMS via Twilio?)

---

## SHARED GOTCHA LOG

| # | What | Symptom | Fix | Source |
|---|------|---------|-----|--------|
| 1 | API Version Header | Auth fails even though Swagger says include it | Never include `api-version` header on Wordly REST API | Jim Firby, CTO |
| 2 | Session ID Format | Mismatched lookups | Always normalize to ABCD-1234 | Known |
| 3 | Auth Split | WSS endpoints reject apikey | REST=apikey header. WSS=sessionID+passcode only, no apikey | Known |
| 4 | Language List CORS | Fails in browser | Use server-side fetch for languages.json | Known |
| 5 | Python Version | PyInstaller breaks | Target 3.11. Avoid 3.13. | Known |
| 6 | Portal UI Changes | Scrapers break | Re-inspect selectors immediately after portal updates | Known |
| 7 | Portal Maintenance Popup | Scraper hangs | Detect modal at session start | Known |
| 8 | Transcript API translation | 404 on translated endpoint | Only `/original` is reliable. Translation is on the roadmap. API only returns source language. | Jim Firby + API behavior |
| 9 | ChunkedEncodingError on transcript download | requests throws error on non-trivial transcripts | Use chunked streaming: `stream=True`, `iter_content(chunk_size=1024, decode_unicode=True)`, catch ChunkedEncodingError mid-loop and use whatever arrived | Confirmed in testing |
| 10 | Transcript not ready when session just ended | ChunkedEncodingError or empty on very recent transcripts | Check `GET /sessions/{sessionId}` for `state=ended` before attempting download. Also allow time for server to finalize file. | Confirmed in testing |
| 11 | Orphaned transcripts (error_404 on session) | Transcript visible in list but session 404s | Salesperson attended a session owned by another account. Skip silently. | CBBY-0176, Aleksandra's account |
| 12 | Non-Latin encoding damage | Polish/etc chars appear as Å, Ä sequences | UTF-8 bytes read as Latin-1. Specify encoding='utf-8' everywhere. Gemini handles mangled diacritics by context. | Aleksandra Polish transcript |
| 13 | Gemini model deprecation | 404 on API call | `gemini-2.0-flash` dead to new users March 2026. Use `gemini-2.5-flash`. Model names change — verify before building. | Confirmed May 2026 |
| 14 | Gemini free tier quota | 429 after ~7 calls | Enable billing on Google AI Studio. Pay-as-you-go, pennies per run. Use MAX_TO_SUMMARIZE=1 during testing. | Confirmed May 2026 |
| 15 | Preacher name extracted from transcript header | Gemini returns account email owner name | Transcript header line contains account email. Skip first lines, feed only spoken content to extraction prompt. | HOW Summarizer testing |
| 16 | Same sessionId reused across multiple transcripts | Cannot use sessionId as unique meeting identifier | SessionId is a persistent link, not a unique session. Use startTime as the match key. Timing is the bible. | Aleksandra's MSKE-2832 |

---

## SHARED INFRASTRUCTURE NOTES

**Credentials pattern (local dev):**
- All keys in flat .txt files alongside script
- One line, raw value, no formatting
- Files: `*_WordlyAPI.txt`, `HS_Service_key.txt`, `gemini_api_key.txt`, `slack_webhook.txt`

**Production credential plan:**
- Google Secret Manager
- Naming: `wordly-apikey-{firstname}-{lastname}`
- Never in database, never in code, never committed to git

**Slack channel:** `#Sales_Summaries`
- Webhook in `slack_webhook.txt`
- Used by both projects currently (separate channel for HOW when productized)

**Gemini:**
- Model: `gemini-2.5-flash`
- Billing: Pay-as-you-go, Chris's personal CC (temporary)
- Long term: Vertex AI through Wordly Google Cloud org (SOC 2 friendly)
- Vertex contact: Whoever manages Wordly's Google Cloud org

**Python version:** 3.11 target. 3.12 on Chris's Mac (working fine for dev). Avoid 3.13.

---

*End of log. Next session: pick up from Next Steps on whichever project is priority.*

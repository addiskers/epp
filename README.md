# EPP Composites Support Helpline

An inbound AI voice helpline for **customers, vendors and employees**. A caller rings the
number; the agent greets them, takes the call in their language, works out who they are,
collects their details and their concern, classifies it, registers a **ticket**
(`EPP-2026-000001`), reads the reference number back, and routes the ticket to the right
department. Callers can also ring back and ask for the **status** of a ticket by number.

Built on the Gemini Live API ([Google Gen AI Python SDK](https://github.com/googleapis/python-genai))
with Plivo telephony, a FastAPI backend, SQLite, and a React admin dashboard.

## Quick start

```bash
# 1. Python deps
python -m venv .venv && .venv\Scripts\activate       # Windows; macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

# 2. Settings (see .env.example — at minimum GEMINI_API_KEY and the admin login)
cp .env.example .env

# 3. Build the admin dashboard
cd admin && npm install && npm run build && cd ..

# 4. Run
python main.py
#   admin:   http://localhost:8000/admin   (EPP_ADMIN_USER / EPP_ADMIN_PASS)
#   landing: http://localhost:8000/
```

Talk to the agent without a phone: **Agent → Edit & test → Talk to it** (browser mic).
For real calls, set the `PLIVO_*` values and a public `PUBLIC_URL`, then point the Plivo
number's Answer URL at `PUBLIC_URL/plivo/answer`.

## How a call works

1. **Greeting** — "Welcome to EPP Composites Support Helpline. Please tell me your preferred
   language." The caller picks one of the twelve enabled languages and the rest of the call is
   in it.
2. **Caller type** — customer, vendor or employee. (Decision, 18 Sep 2026: language is asked
   *before* caller type, so the caller-type question is already in the caller's language. The
   requirements text placed caller type in the greeting itself; the order was chosen
   deliberately and can be reversed on the Agent page without a code change.)
3. **Details** — name; company (customer) / vendor code (vendor) / employee ID, department,
   plant (employee); contact number.
4. **The concern** — the caller explains uninterrupted; the agent asks up to a few context-aware
   follow-ups (when did it start, happened before, reported before, anyone else involved,
   impact on operations / work / safety).
5. **Classify + register** — the agent picks a category from the Routing table and an honest
   priority reason, reads the name and a one-line summary back, then calls `create_ticket`.
   The server resolves the department, runs the priority nets, allocates the number.
6. **Confirmation** — the verbatim line with the reference number read letter by letter,
   digit by digit. The agent never promises an outcome.
7. **After the call** — a Gemini text model writes the summary, intent and sentiment onto the
   ticket, raises priority if the transcript warrants it, and — if the call dropped before
   the agent registered anything — creates the ticket from the transcript.

Status inquiries: "Do you have your reference number?" → `lookup_ticket` → the agent says
only the status the database returns.

## Outbound campaigns

The helpline can also *place* calls. An admin builds a campaign on the Campaigns page:

| Type | Who is called | What the agent does |
|---|---|---|
| **Intake round** | uploaded or typed contacts | Asks whether there is any concern to register; runs the normal intake if so. A concern becomes a ticket. |
| **Ticket follow-up** | the contact number on selected tickets | Reads the reference number and current status, records anything they add onto the ticket timeline. |
| **Announcement** | uploaded or typed contacts | Reads a message written on the campaign, in the person's language; can still register a concern. |

Each campaign has a start time, calling hours, and a retry rule (attempts per day, for how
many days, spaced by N hours). Unanswered dials, voicemails and "call me later" are retried;
everything else closes the recipient out with its outcome. Dials are paced and share
`MAX_LIVE_CALLS` with inbound calls. The **Scheduler** page has the kill switch that stops
all outbound dialing at once.

## The admin dashboard (`/admin`)

| Page | What it does |
|---|---|
| Dashboard | Open / high-priority / today tiles, breakdowns by status, department, caller type, priority, last 7 days, escalated list, live calls with a rolling transcript |
| Tickets | Search and filter (status, priority, department, caller type, escalated, date), CSV export |
| Ticket | Caller card, the concern, AI summary + caller mood + flags, transcript and recording, timeline; ONE "Save changes" for category / department / assignee / priority / status (+ a note), applied all-or-nothing |
| Campaigns | Create (type → recipients → message → schedule), list, detail with per-recipient status, outcome, Call now, cancel |
| Contacts | The pool a campaign calls: xlsx/csv upload with a sample, add one, search, delete |
| Scheduler | Kill switch for all outbound dialing (survives restarts), and the retry queue |
| Departments & Routing | Departments (name, code, active), and the category → department mapping per caller type (high-priority flag, keywords); each table has its own Save |
| Agent | The intake script (placeholders, voice), preview, browser-mic test, "Call me" test; a Shipped / Customised badge says whether deploys update it automatically |
| Call logs | Every call with the caller's name, transcript, recording, analysis and linked tickets |
| Users | Admins and department users; Edit opens a form with Save |
| Audit log | Logins, config changes, ticket updates, every transcript / recording view, plus one row per call: `ticket_created` or `call_no_ticket` (with the reason) |
| Subscription | Minutes purchased vs used (per phone call, rounded up), ₹ at the configured rate, plan period and licence; editable only by `EPP_SUPERADMIN_USERS` |

Roles: **admin** sees everything; **dept_user** sees only tickets assigned to their department.
`EPP_HIDDEN_PAGES` takes pages off the admin menu (default: the three outbound pages) without a rebuild.

## Customising

| What | Where |
|---|---|
| The greeting and the whole script | Agent page (stored in the DB; `epp_seeds.py` is the shipped default) |
| The follow-up and announcement scripts, and the outbound opening | Agent page — three agents; each has an inbound and an outbound opening |
| Campaign pacing, calling hours, retries | `.env` (`EPP_CAMPAIGN_*`, `MAX_LIVE_CALLS`) and per campaign at creation |
| Company / helpline name, ticket prefix, enabled languages | `.env` (`EPP_*`) |
| Department mapping, high-priority categories, keywords | Departments & Routing page (`epp_seeds.py` seeds the first run) |
| Which admin pages the client sees | `.env` (`EPP_HIDDEN_PAGES`) |
| The client's plan (minutes, dates, ₹ rate) | `.env` (`EPP_PLAN_*`, `EPP_RATE_INR_PER_MIN`) or the Subscription page as an `EPP_SUPERADMIN_USERS` user |
| Priority keyword net | `routing.py` |
| Voice / accent | Agent page, or `EO_VOICE_NAME` / `EO_SPEECH_LANGUAGE_CODE` |
| Post-call analysis model | `EPP_ANALYSIS_MODEL` |

## Endpoints

| Route | Purpose |
|---|---|
| `GET /` | Landing page |
| `GET /admin/*` | The admin SPA |
| `/api/epp/*` | The admin API (bearer token) |
| `GET/POST /plivo/answer`, `WS /plivo/media-stream` | Plivo telephony bridge |
| `WS /ws` | Browser-mic test (short-lived token from the Agent page) |
| `WS /live/ws` | Live transcript feed for the dashboard (short-lived token) |
| `WS /live/listen/{call_sid}` | Listen in on one live call: both sides as mono PCM16 8 kHz frames (admin, short-lived token; audited as `call_listened`) |
| `GET/PUT /api/epp/subscription` | The plan and its usage (PUT: superadmins only) |
| `GET /healthz` | Liveness |

## Project structure

```
main.py            FastAPI app: telephony webhooks, /ws test socket, live feed, SPA hosting
eo_api.py          /api/epp/* — tickets, routing config, agent, users, calls, audit
eo_auth.py         login, roles, throttle, signed tokens
eo_db.py           SQLite schema v2 + queries (users, departments, categories, agents, tickets, events, audit)
tickets.py         numbering, create_ticket / lookup_ticket handlers, status machine, post-call refinement
campaigns.py       campaign service: validation, per-call context for a dial, record_outcome handler
campaign_runner.py the paced dial loop: promote, reap, retry, calling hours, fair rotation, kill switch
calling_window.py  calling-hours math
contacts_import.py xlsx/csv contact import
routing.py         category resolution, priority nets, keyword classifier
analysis.py        post-call Gemini text pass (summary, intent, sentiment, classification)
languages.py       the twelve languages
epp_seeds.py       departments, category mapping, the intake agent's script
prompt_render.py   placeholder rendering of the script
agent_tools.py     the three Gemini tools
gemini_live.py     Gemini Live session wrapper
plivo_handler.py   the hardened phone-audio bridge
recorder.py        per-call transcript / token / cost record; hands off to post-call analysis
store.py           JSON call records + recordings
admin/             React admin (Vite)
frontend/          landing page + audio worklet for the browser test
tests/             pytest suite (run from this directory: python -m pytest tests -q)
```

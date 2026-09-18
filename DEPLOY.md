# EPP Support Helpline — deployment

One FastAPI app serves the API, the React admin SPA, and the telephony webhooks.

## What runs where

| URL | What it is | Auth |
|---|---|---|
| `/` | Public landing page | none |
| `/admin` | The admin SPA | user login |
| `/api/epp/*` | The admin API | bearer token |
| `/plivo/answer`, `/plivo/media-stream` | Telephony webhooks — **Plivo calls these** | Plivo |
| `/ws` | Browser-mic test socket | short-lived token minted by the Agent page |
| `/live/ws` | Live transcript feed | short-lived token minted for admins |

---

## Docker (recommended)

```bash
cp .env.example .env       # then edit — see "Required settings" below
docker compose up -d --build
docker compose logs -f
```

A healthy boot logs:

```
INFO:main:EPP helpline ready: model=... languages=en,hi,... plivo=ready public_url=https://...
INFO:     Uvicorn running on http://0.0.0.0:8000
```

**Check `plivo=ready`.** If it says `NOT configured`, inbound calls will not reach the agent.

The image is multi-stage: Node builds the SPA, then it is copied into the Python runtime, so
the container always ships a UI that matches the API.

### HTTPS

The container binds `127.0.0.1:8000` only. Terminate TLS on the host (Caddy):

```
helpline.yourdomain.com {
    reverse_proxy localhost:8000
}
```

**`PUBLIC_URL` must be this HTTPS URL** — Plivo fetches `/plivo/answer` from it, and the
media stream upgrades to `wss://`, which Plivo requires.

### The helpline number

In the Plivo console, on the number's application: **Answer URL** = `PUBLIC_URL/plivo/answer`,
method **GET**. Nothing else is needed; the same URL serves the Agent page's "Call me" test.

### Updating

```bash
git pull
docker compose up -d --build
```

The `epp-data` volume persists: tickets, users, routing config, call records and recordings.

---

## Without Docker

```bash
pip install -r requirements.txt
cd admin && npm install && npm run build && cd ..   # emits admin/dist, served at /admin
python main.py
```

**One worker, always.** The Gemini pre-warm cache and the live-call gauge are in-process:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
```

Re-run `npm run build` after any SPA change — FastAPI serves the built `admin/dist`.

---

## Required settings

| Var | Notes |
|---|---|
| `PUBLIC_URL` | **Required for any phone call.** The public HTTPS base Plivo fetches `/plivo/answer` from. |
| `GEMINI_API_KEY` | |
| `MODEL` | A Gemini **Live** model id valid for your account. |
| `EPP_ANALYSIS_MODEL` | A Gemini **text** model for the post-call pass. Verify the id against your account. |
| `PLIVO_AUTH_ID` / `PLIVO_AUTH_TOKEN` / `PLIVO_FROM_NUMBER` | The helpline number. |
| `EPP_ADMIN_USER` / `EPP_ADMIN_PASS` | Seeds the first admin **only when the users table is empty**. Change the password from Profile after first login. |
| `EPP_SESSION_SECRET` | Long random string; signs login tokens. |
| `EPP_COMPANY_NAME` / `EPP_HELPLINE_NAME` / `EPP_TICKET_PREFIX` | Spoken in the greeting and the ticket number. |
| `EPP_ENABLED_LANGUAGES` | Switch off any language that fails the live-call check below. |
| `DATA_DIR` | Leave blank under compose — it sets `/var/epp-data` (the persistent volume). |

---

## Language verification checklist

Gemini Live fixes the speech language code per session (`en-IN`); the script drives the
switch to the caller's language. Hindi and Gujarati are proven on this stack. The rest must be
verified on a real phone call before they are promised to callers. Record the result here and
remove any failure from `EPP_ENABLED_LANGUAGES`.

| Language | Understood the caller | Spoke back naturally | Read the ticket number correctly | Verified by / date |
|---|---|---|---|---|
| English | ☐ | ☐ | ☐ | |
| Hindi | ☐ | ☐ | ☐ | |
| Gujarati | ☐ | ☐ | ☐ | |
| Marathi | ☐ | ☐ | ☐ | |
| Bengali | ☐ | ☐ | ☐ | |
| Tamil | ☐ | ☐ | ☐ | |
| Telugu | ☐ | ☐ | ☐ | |
| Kannada | ☐ | ☐ | ☐ | |
| Malayalam | ☐ | ☐ | ☐ | |
| Punjabi | ☐ | ☐ | ☐ | |
| Odia | ☐ | ☐ | ☐ | |
| Assamese | ☐ | ☐ | ☐ | |

Per language: ring the helpline, pick the language, register an employee concern, note the
number, ring back and ask for its status.

---

## Data, privacy and backups

Everything lives under `DATA_DIR`:

- `epp.db` — SQLite: tickets, events, users, departments, categories, agent script, audit log.
- `calls/*.json` — one file per call: transcript, tool calls, post-call analysis, cost.
- `recordings/*.wav` — call audio, when `EO_RECORD_CALLS` is on.

**Transcripts and recordings are personal data** — grievances name people. Back up the whole
directory, restrict access to the host, and note that every transcript or recording view in
the admin is written to the audit log. Department users can only reach the calls behind
their own department's tickets.

```bash
docker compose exec epp-helpline tar czf - /var/epp-data > epp-backup-$(date +%F).tgz
```

---

## First run

1. Sign in at `/admin` with `EPP_ADMIN_USER` / `EPP_ADMIN_PASS`; change the password on Profile.
2. **Routing** — check the department list and the category → department mapping against
   EPP's actual org chart. Add departments; move categories; flag anything that must always be
   high priority.
3. **Users** — create one department user per department (they see only their tickets).
4. **Agent → Edit & test → Show the script.** Read what the agent will actually say with the
   routing table filled in. Then **Talk to it** from the browser, and **Call me** on a phone.
5. Run the language checklist above.
6. Point the Plivo number at `PUBLIC_URL/plivo/answer`.

---

## Troubleshooting

**`plivo=NOT configured`** — `PLIVO_AUTH_ID`, `PLIVO_AUTH_TOKEN` or `PLIVO_FROM_NUMBER` is unset.

**Calls connect but there's silence** — the answer webhook must be reachable *from the
internet*. `curl https://your-public-url/plivo/answer` from off-box; anything but XML back
means Plivo can't reach you either.

**`/admin` 503s** — the SPA isn't built. `cd admin && npm run build`.

**A ticket has no AI summary** — `EPP_ANALYSIS_ENABLED` is false, `EPP_ANALYSIS_MODEL` is not a
model your key can use, or the caller said fewer than eight words. Check the log for
`post-call analysis failed`.

**Agent says a sentence with a gap** — a placeholder had no data. Agent → Show the script shows
which, in an amber banner.

**The agent hung up while the caller was writing the number down** — raise
`EO_POST_RSVP_IDLE_SECONDS` (default 20).

**Tests** — `python -m pytest tests -q` from the app directory (the tests isolate `DATA_DIR`).

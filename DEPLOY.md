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

## Fresh GCP VM (Debian 12/13) — end to end

Production values: domain **`epp.globalvoxinc.ai`**, helpline number **+91 80 3182 9020**
(Plivo, Bangalore fixed line → `PLIVO_FROM_NUMBER=+918031829020`). Shape: Compute Engine
`e2-small` or larger, Debian 13, one static external IP. Total time ~20 minutes, most of it
waiting on the first Docker build.

### 1. On the GCP side (console or gcloud, once)

```bash
# Reserve the VM's external IP so DNS never goes stale after a stop/start
gcloud compute addresses create epp-ip --region=<REGION>
gcloud compute instances add-access-config epp --zone=<ZONE> --address=$(gcloud compute addresses describe epp-ip --region=<REGION> --format='value(address)')
# Let the world reach Caddy (80 for the cert challenge, 443 for everything)
gcloud compute instances add-tags epp --zone=<ZONE> --tags=https-server,http-server
```

Or in the console: VM → Edit → tick **Allow HTTP traffic** and **Allow HTTPS traffic**;
VPC network → IP addresses → promote the VM's ephemeral IP to static.

### 2. DNS

In the `globalvoxinc.ai` zone add an **A record** `epp` → the static IP. Wait until
`nslookup epp.globalvoxinc.ai` returns that IP from your laptop; Caddy cannot get a
certificate before DNS resolves.

### 3. On the VM

```bash
# Docker (official script handles Debian 13)
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER && newgrp docker

# Caddy (official repo)
sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gnupg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt-get update && sudo apt-get install -y caddy git

# The code. Two mirrors carry the same main: GitHub (used by the server) and Bitbucket.
git clone https://github.com/addiskers/epp.git ~/epp && cd ~/epp

# Settings
cp .env.example .env
nano .env
```

Set at least these in `.env`:

```
GEMINI_API_KEY=...                       # a key whose project has spend headroom
MODEL=gemini-3.1-flash-live-preview
EPP_ANALYSIS_MODEL=gemini-3.6-flash      # a TEXT model; gemini-3.1-flash does not exist
PLIVO_AUTH_ID=...
PLIVO_AUTH_TOKEN=...
PLIVO_FROM_NUMBER=+918031829020          # the helpline number, E.164
PUBLIC_URL=https://epp.globalvoxinc.ai
EPP_ADMIN_USER=admin
EPP_ADMIN_PASS=<strong password>
EPP_SESSION_SECRET=<output of: openssl rand -hex 32>
EPP_COMPANY_NAME=EPP Composites
EPP_HELPLINE_NAME=EPP Composites Support Helpline
EPP_TICKET_PREFIX=EPP
```

Leave `DATA_DIR` blank (compose sets it to the volume). Then:

```bash
docker compose up -d --build            # first build: 3-5 minutes
docker compose logs -f                  # wait for "EPP helpline ready … plivo=ready"

sudo tee /etc/caddy/Caddyfile >/dev/null <<'EOF'
epp.globalvoxinc.ai {
    reverse_proxy localhost:8000
}
EOF
sudo systemctl reload caddy             # Caddy fetches the certificate itself
```

### 4. Verify from your laptop, not from the VM

```bash
curl https://epp.globalvoxinc.ai/healthz
# {"ok":true,"live_calls":0,"gemini":{"ok":true}}
curl "https://epp.globalvoxinc.ai/plivo/answer?From=%2B919876543210&CallUUID=x"
# must start with <?xml … <Stream … wss://epp.globalvoxinc.ai/plivo/media-stream
```

If the second one is not XML, Plivo cannot reach you either — fix that before touching Plivo.

### 5. Plivo

Voice → Applications → your app: **Answer URL** `https://epp.globalvoxinc.ai/plivo/answer`,
method **GET**. Assign the app to the helpline number. Ring the number: the log shows
`INBOUND call … from +91…`, then `Plivo stream started`.

### 6. Sign in

`https://epp.globalvoxinc.ai/admin` with `EPP_ADMIN_USER` / `EPP_ADMIN_PASS`. Change the
password on Profile, then follow **First run** below.

### Day-2

| Task | Command |
|---|---|
| Update to the latest code | `cd ~/epp && git pull && docker compose up -d --build` — then check the log for `STALE AGENT PROMPT` and reset the script on the Agent page if it says so |
| Logs | `docker compose logs -f --tail=200` |
| Restart | `docker compose restart` |
| Backup | `docker compose exec epp-helpline tar czf - /var/epp-data > epp-backup-$(date +%F).tgz` |
| Caddy status / cert | `sudo systemctl status caddy` · `sudo journalctl -u caddy -n 50` |

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
INFO:campaign_runner:Campaign runner started (interval=30s, enabled=True, plivo_ready=True, ...)
INFO:     Uvicorn running on http://0.0.0.0:8000
```

**Check `plivo=ready` / `plivo_ready=True`.** If either says otherwise, inbound calls will not
reach the agent and campaigns will queue without dialing.

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

**The agent's script lives in the database, not in the code.** A release that changes the
shipped script does not change what a deployed agent says until an admin opens **Agent →
Edit & test → Reset to shipped script**. The boot log tells you when that is needed:

```
WARNING:eo_db:STALE AGENT PROMPT: 'EPP Support Intake' (id=1) is behind the shipped script — missing 'say_now' …
```

Any wording the client customised on the Agent page is lost by the reset; copy it out first.

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
| `MAX_LIVE_CALLS` | Simultaneous live calls, inbound **and** campaign together (default 10). Each is a Gemini session plus telephony. |
| `EPP_CAMPAIGN_*` | Campaign pacing and retries; see `.env.example`. The Scheduler page's ON/OFF switch is the fastest way to stop all outbound dialing. |
| `DATA_DIR` | Leave blank under compose — it sets `/var/epp-data` (the persistent volume). |

---

## Language verification checklist

**Before the checklist:** the Accent field on the Agent page must be **blank**, and
`EO_SPEECH_LANGUAGE_CODE` blank in `.env`. The boot log then shows `language=auto-detect`.
Pinning `en-IN` biased the model toward Hindi/English (a Gujarati caller was answered in
Hindi on the first test round). The script drives the switch to the caller's language. Hindi
and English are proven on this stack. The rest must be verified on a real phone call before
they are promised to callers. Record the result here and remove any failure from
`EPP_ENABLED_LANGUAGES`.

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

**If a language comes out as Hindi (Gujarati and Marathi are the usual cases):** the session's
speech accent is fixed to `en-IN` (`EO_SPEECH_LANGUAGE_CODE`), which biases the model toward
English/Hindi. On the Agent page clear the *Accent* field (or set the env var blank) and repeat
the call. If that fixes it, keep it blank and note it here.

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
7. **Campaigns** — add your own number on Contacts, then run one campaign of each type to it
   (intake round, a follow-up on a test ticket, an announcement). Check the recipient's
   status, the outcome, and — for the follow-up — the note on the ticket timeline.

---

## Troubleshooting

**Agent answers "our systems are down, call back later" on every call** — the call reached
the app without its context. The log shows `prompt_chars=374` (the fallback prompt; the real
script is ~11,000) and, just before it, `INBOUND call - to/from unknown` or an ERROR about a
missing CallUUID. Cause: the answer webhook did not get Plivo's parameters. Update to a build
that reads both the query string and the POST form body (Sept 2026 or later); until then set
the Plivo application's Answer method to **GET**.

**`plivo=NOT configured`** — `PLIVO_AUTH_ID`, `PLIVO_AUTH_TOKEN` or `PLIVO_FROM_NUMBER` is unset.

**Calls connect but there's silence** — the answer webhook must be reachable *from the
internet*. `curl https://your-public-url/plivo/answer` from off-box; anything but XML back
means Plivo can't reach you either.

**`/admin` 503s** — the SPA isn't built. `cd admin && npm run build`.

**A ticket has no AI summary** — `EPP_ANALYSIS_ENABLED` is false, `EPP_ANALYSIS_MODEL` is not a
model your key can use, or the caller said fewer than eight words. Open the call in Call logs: the
drawer shows the analysis status and the reason.

**The agent is silent on every call** — the Gemini project is refusing sessions. The log shows
`APIError: 1011 … exceeded its monthly spending cap` (raise it at https://ai.studio/spend) or an
auth error (`GEMINI_API_KEY`). The Dashboard shows a red banner with the last error; the Agent page's
browser test prints it in the transcript box. No code change fixes this.

**The agent said a reference number but no ticket exists** — it spoke without calling
`create_ticket`. The runtime guard now nudges it to register properly (log line `HALLUCINATION
GUARD`); the call drawer's "Tool calls" section shows whether `create_ticket` actually ran.

**Agent says a sentence with a gap** — a placeholder had no data. Agent → Show the script shows
which, in an amber banner.

**The agent hung up while the caller was writing the number down** — raise
`EO_POST_RSVP_IDLE_SECONDS` (default 20).

**A campaign is live but nobody is being called** — in order: the Scheduler switch is OFF;
it is outside the campaign's calling hours; `MAX_LIVE_CALLS` is used up by inbound calls;
`plivo_ready=False` in the runner log. The recipient's status pill says which.

**Tests** — `python -m pytest tests -q` from the app directory (the tests isolate `DATA_DIR`).

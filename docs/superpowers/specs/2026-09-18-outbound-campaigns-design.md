# Outbound campaigns for the EPP helpline — design

Date: 2026-09-18. Status: approved in chat by Aditya.

## Why

The helpline is inbound only. EPP also wants to *place* calls: proactive grievance rounds
with employees or vendors, follow-ups on registered tickets, and announcements. The 7x
platform this app was converted from had a hardened campaign dialer (pacing, calling hours,
no-answer retries, fair rotation). This design ports that subsystem and re-points it at
three campaign types instead of wedding events.

## Decisions taken

- **Port, do not rewrite.** `campaign_runner.py`, `callbacks.py` (window helpers only), and
  `eo_import.py` come from the sibling `7x/` source with the wedding fields removed.
- **Three campaign types**, chosen per campaign by the admin: `intake`, `followup`,
  `announcement`.
- **Contacts** come from an xlsx/csv upload, a manual add box, or (follow-up only) a picker
  over existing tickets. Contacts live in a reusable pool.
- **Admin only** in this pass. Department users see follow-up calls through their tickets'
  timelines.
- **Not ported:** the RSVP callback scheduler (a caller asking to be called back becomes a
  campaign-contact retry), the wedding fatigue warning, EnableX.

## Campaign types

| Type | Recipients | Agent behaviour | Records |
|---|---|---|---|
| `intake` | contacts | Outbound opening, then the standard intake flow | `create_ticket`, or `record_outcome` ∈ {no_concern, callback, not_reachable, wrong_number} |
| `followup` | tickets → their contact number | States the ticket number and its current status, asks if there is anything to add | `record_outcome` ∈ {confirmed, has_update, callback, not_reachable, wrong_number}; a `has_update` note is appended to the ticket timeline |
| `announcement` | contacts | Reads an admin-written message (per campaign), answers simple questions, offers to register a concern | `record_outcome` ∈ {acknowledged, declined, callback, not_reachable, wrong_number}; `create_ticket` stays available |

`callback`, `not_reachable` and `wrong_number` are retry-or-close signals for the runner:
`callback`/`not_reachable` retry per the campaign settings; `wrong_number` closes the
contact as `done` with that outcome.

## Scripts (agents)

Three agent rows, all editable on the Agent page and seeded idempotently by slug:

- `epp_intake` (existing) gains an **outbound trigger** (`outbound_trigger_template`) used
  when the call is a campaign dial: "This is {helpline_name} calling from {company_name}.
  Is there any concern, complaint or feedback you would like to register today?"
- `epp_followup` (new): identity block, LANGUAGE block, the follow-up flow, ENDING.
  Placeholders: `{ticket_id}`, `{ticket_id_spoken}`, `{ticket_status}`,
  `{ticket_category}`, `{ticket_department}`, `{ticket_created_spoken}`, `{caller_name}`.
- `epp_announcement` (new): identity, LANGUAGE, "THE MESSAGE" section rendered from
  `{campaign_message}`, a short Q&A rule, an offer to register a concern, ENDING.

`prompt_render.KNOWN_PLACEHOLDERS` grows by the names above plus `{campaign_name}`.
Missing values blank out as today.

## Tools

`agent_tools.build_tools(categories, langs, campaign_type=None)`:

- inbound (`campaign_type=None`): `create_ticket`, `lookup_ticket`, `end_call` (unchanged).
- outbound: the same three plus `record_outcome(outcome_status, note, callback_time_text)`
  whose `outcome_status` enum is the type's list above. `COMPLETION_TOOLS` gains
  `record_outcome`.

`record_outcome` is blocking like the others; its result carries an instruction to speak
one closing then `end_call`. `main.tool_mapping` adds it for campaign calls only.

## Data model (schema v3)

```
contacts(id, name, phone E.164 UNIQUE, caller_type '', notes '', source upload|manual,
         status valid|invalid, created_by, created_at, updated_at)
campaigns(id, name, campaign_type, message '', status scheduled|live|completed|cancelled,
          start_at, created_by, contact_count, callback_delay_hours 4, callback_max_per_day 3,
          callback_days 1, call_start_min 540, call_end_min 1260, done_count, failed_count,
          created_at, updated_at)
campaign_contacts(id, campaign_id FK, contact_id, ticket_id, phone, name, call_status
          pending|calling|done|failed|cancelled, attempts, day_attempts, day_key,
          next_attempt_at, last_call_id, last_attempt_at, last_error, outcome, remark,
          created_at, updated_at)
```

Call records gain `campaign_id`, `campaign_contact_id`, `outcome`, `outcome_note`.
Tickets gain nothing; a ticket created on a campaign call has `source = 'campaign'` and a
`created` event note naming the campaign. A follow-up `has_update` writes a `note` event
(actor `ai`) on the ticket.

## Call path

`dialer.place_call(phone, campaign_id=, campaign_contact_id=)` puts both ids on the
answer URL. `/plivo/answer` resolves the campaign → type → agent, renders the prompt with
the contact / ticket context, builds the type's tools, and stashes the outbound trigger.
`/plivo/media-stream` opens the recorder with `campaign_id` and `campaign_contact_id`; the
recorder captures `record_outcome` into `outcome` / `outcome_note` and `create_ticket` as
today. Post-call analysis runs for intake and announcement calls (a concern may have been
raised); it is skipped for follow-ups.

## Runner

`campaign_runner.py` ported: promote → reap → pace → dial. Reap reads the call record by
`campaign_contact_id` (stored on the record) instead of phone matching. Outcomes
`callback`/`not_reachable` and dial failures go through `_apply_failure`; everything else
is `done`. `MAX_LIVE_CALLS` is shared with inbound calls through the existing
`_active_calls` gauge in `main` (exposed as `main.live_room()`). The scheduler ON/OFF
toggle is an in-memory flag in `campaign_runner` plus `EPP_CAMPAIGN_RUNNER_ENABLED`.

## API (`/api/epp`, admin only)

```
GET/POST /contacts, POST /contacts/import, GET /contacts/template, POST /contacts/delete
GET /campaigns, POST /campaigns, GET /campaigns/{id}, POST /campaigns/{id}/cancel
GET /campaigns/{id}/contacts, POST /campaigns/{id}/contacts/{cc}/retry (Call now),
POST /campaigns/{id}/contacts/{cc}/cancel, PATCH /campaigns/{id}/contacts/{cc}/remark
GET /scheduler/queue, POST /scheduler/toggle
GET /agents  → now three rows
```

`POST /campaigns` takes `campaign_type`, `name`, `message` (announcement), `contact_ids`
or `ticket_ids` (follow-up), `start_at`, retry settings, calling hours. Validation: a
follow-up needs ticket ids whose tickets have a contact number; an announcement needs a
message; an intake/announcement needs contact ids. Cap: `EPP_MAX_ACTIVE_CAMPAIGNS` (6).

Every create, cancel, Call now, contact import/delete and toggle writes an audit row.

## Admin pages

- **Contacts**: upload card (sample download), add box, table with search, caller-type
  tag, delete.
- **Campaigns**: list (name, type, status, start, progress, retries), detail (stat tiles,
  recipients with status/outcome/attempts/Call now/cancel/remark, call open, linked
  ticket), cancel.
- **Create Campaign** wizard: 1 type → 2 recipients (contacts table with selection, or
  tickets picker with filters) → 3 message (announcement) → 4 schedule + retries + hours →
  confirm.
- **Scheduler**: kill switch banner + retry queue (Call now / cancel).
- Sidebar: Contacts, Campaigns, Scheduler for admins.

## Testing

- Ported: runner pacing/retry/reap/fair-order, window helpers, importer.
- New: tools per type, follow-up and announcement rendering with no gaps, outbound trigger
  selection, recorder outcome capture, ticket `source=campaign` + follow-up note event,
  campaign API validation and admin-only access, audit rows.
- Live: one campaign of each type to Aditya's phone.

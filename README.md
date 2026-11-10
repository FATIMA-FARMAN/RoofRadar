# RoofRadar AI — lead-to-inspection automation for roofing companies

**Every lead answered in seconds, qualified by AI, and booked into the real calendar, with no double bookings.**
26 production n8n workflows · OpenAI · Twilio · Google Calendar · Postgres/Supabase · Slack · HubSpot.
Built by **Fatima Ali, AI Automation Engineer**.

![RoofRadar AI architecture](docs/architecture.png)

| | |
|---|---|
| ▶ **Demo video (91 s)** | [media/roofradar-demo-flagship.mp4](media/roofradar-demo-flagship.mp4) |
| **The flagship flow** | [docs/END_TO_END.md](docs/END_TO_END.md): web form → first text in under 1 s → 3 real times → booked |
| **Case study** | [docs/CASE_STUDY.md](docs/CASE_STUDY.md): problem, solution, engineering decisions, proof |
| **Screenshots** | [docs/screenshots/](docs/screenshots/): real n8n executions from the end-to-end test run |
| **Run it without accounts** | [test/lab/](test/lab/README.md): local stand-ins for OpenAI, Twilio, Google, Slack |

<p>
<img src="docs/screenshots/03-ai-router-decision.png" width="49%"> <img src="docs/screenshots/04-booking-no-double-booking.png" width="49%">
</p>

---

## The system in detail


AI lead response, qualification, booking and voice reception for roofing companies, built on
**n8n + OpenAI + Twilio + Google Calendar + Postgres/Supabase** (Slack, HubSpot, Meta Lead Ads optional).
Demo client: **PrimeHome Roofing** (NY/NJ).

> **AI decides, deterministic n8n logic executes.** Models only return validated JSON decisions or text.
> Availability, bookings, consent, scores, CRM writes and every outbound message are done by n8n/SQL.

```
db/                    001_schema.sql (tables, constraints, helper functions) · 002_seed.sql (PrimeHome config)
n8n/gen/               Python generator — the single source of truth for all workflows
n8n/workflows/         26 importable n8n JSON files (24 workflows; #24 is split A/B/C) + manifest.json
voice-server/          Workflow 24 real-time voice agent (Twilio ConversationRelay ⇄ OpenAI ⇄ n8n tools)
scripts/deploy.py      creates credentials, imports/updates workflows, links ids, sets error workflow, activates
scripts/validate_workflows.py   static checks (+ PREPARE every SQL statement against your DB)
scripts/smoke-test.sh  curl tests against a deployed instance
test/payloads/         sample requests
docs/TEST_MATRIX.md    the 24-workflow test matrix + the 60–90 s sales-demo script
```

## Which model does what (OpenAI)

| Where | Default model | Why |
|---|---|---|
| 04 Router · 05 Qualifier | `gpt-5.4-mini` | fast, cheap, strict JSON classification/extraction |
| 06 Conversation (customer-facing SMS/WhatsApp) | `gpt-5.4` | best wording where the customer reads it |
| 13 Follow-ups · 16 Quote nudges · 24B Call summary | `gpt-5.4-mini` | short, templated tasks |
| 24 Voice agent (voice-server) | `gpt-5.4-mini` | latency matters most on a live call |

Change per client without redeploying: `organizations.settings.ai.models` (SQL) and `OPENAI_VOICE_MODEL` (voice server).
All calls use Chat Completions with `response_format: json_object` and `reasoning_effort: low` for GPT-5-family models.
Check your OpenAI account has access to these model ids before going live.

## The 24 workflows

| # | Workflow | Trigger | What it guarantees |
|---|---|---|---|
| 01 | Inbound Website | `POST /webhook/primehome-lead` | E.164 normalisation, honeypot, dedupe by phone → email, consent record, nurture scheduled, instant first SMS (only with consent) |
| 02 | Inbound Meta | `GET/POST /webhook/roofradar/meta` | verify handshake; lead fetched from Graph API with *your* token (forged webhooks can't inject data) |
| 03 | Inbound SMS | `POST /webhook/roofradar/sms` (Basic auth) | replies `<Response/>` instantly; **idempotent on MessageSid**; STOP/START handled before any AI |
| 04 | AI Router | sub-workflow | ANSWER/QUALIFY/BOOK/RESCHEDULE/HUMAN/STOP/EMERGENCY; low confidence → HUMAN; EMERGENCY only with a configured criterion; slot accepted only if **we offered it** |
| 05 | AI Qualification | sub-workflow | LLM extracts facts; service area + 0–100 score are deterministic SQL (`rr_score_lead`) |
| 06 | AI Conversation | sub-workflow | guardrails (no prices, no booking claims, no unsafe advice, no "I'm human"), safe fallbacks, slot list appended by n8n |
| 07 | Send SMS/WhatsApp | sub-workflow | the only Twilio sender; consent re-checked at send time; audit trail |
| 08 | Inbound WhatsApp | `POST /webhook/roofradar/whatsapp` | same as 03 |
| 09 | Availability | sub-workflow | Google `freeBusy` + our DB → business hours, notice, horizon → ≤3 slots saved on the lead |
| 10 | Book Appointment | sub-workflow | offered-slot check → idempotency → live re-check → Google event → DB **exclusion constraint** (rollback on conflict) → deal → CRM → confirmation → Slack |
| 11 | Reminders | every 15 min | 24 h + 2 h; claim-then-send, never twice |
| 12 | CRM Sync | sub-workflow | HubSpot contact + deal upsert; Postgres stays the source of truth |
| 13 | Follow-up Engine | every 5 min | 15 m · 4 h · 24 h · 3 d · 7 d; stops on BOOKED/HUMAN/STOPPED/DISQUALIFIED; skips if they replied; quiet hours 9–20 local |
| 14 | Missed Call Recovery | `POST /webhook/roofradar/call-status` | text-back in seconds; race-safe dedupe on CallSid |
| 15 | Human Handoff | sub-workflow | flag lead, open handoff, Slack alert, tell customer, pause AI, stop follow-ups |
| 16 | Quote Follow-up | daily 10:00 | nudges on days 2/5/10 after `quote_sent_at`; never mentions price |
| 17 | Analytics | daily 00:15 | KPIs incl. booking rate and median first-response time |
| 18 | Daily Report | daily 08:00 | Slack + email |
| 19 | Error Handler | Error Trigger (set on all others) | classify → save → retry queue or Slack (deduped) |
| 20 | Retry Queue | every minute | n8n API re-runs the failed execution **from the failed node**; backoff 1/5/30 min → dead letter |
| 21 | Consent | sub-workflow | opt-out/opt-in ledger; STOP wins everywhere |
| 22 | Monitoring | every 5 min | Twilio, DB, Calendar, OpenAI, workflow errors → alert on state change |
| 23 | Maintenance | daily 02:00 | expire sessions, prune, archive, **flag** duplicates/orphans (never auto-delete) |
| 24A | Voice Tool Gateway | `POST /webhook/roofradar/voice-tool` (header key) | voice AI tools reuse 05/07/09/10/15/21 |
| 24B | Voice Post-Call | `POST /webhook/roofradar/voice-call-ended` | transcript → summary → lead/session/CRM → follow-up text |
| 24C | Transfer Result | `POST /webhook/roofradar/voice-transfer-result` | records DialCallStatus; unanswered → priority callback alert |

## Setup

**1. Database** — Supabase SQL editor or `psql`:
```bash
psql "$DATABASE_URL" -f db/001_schema.sql
# edit db/002_seed.sql first: Twilio number, handoff phone, Slack channel, hours, service area, FAQ
psql "$DATABASE_URL" -f db/002_seed.sql
psql "$DATABASE_URL" -c "UPDATE app_config SET value='\"https://YOUR-N8N-HOST\"' WHERE key='n8n_base_url';"
```
Both files are safe to re-run.

**2. n8n** (built and tested on n8n 1.123 self-hosted; keep Code-node task runners enabled — `N8N_RUNNERS_ENABLED=true`)
- Create **"RoofRadar Google Calendar"** (Google Calendar OAuth2 API) in the n8n UI and connect it. Copy its id.
- `cp .env.example .env` and fill it in (put that id in `CRED_GCAL_ID`).
- `python3 scripts/deploy.py` — creates the other credentials, imports all 26 workflows, links them, sets
  **19** as every workflow's Error Workflow, activates the triggers, and prints your webhook URLs.
  Re-run any time; it updates in place (state in `.deploy-state.json`).

**3. Twilio**
- Phone number → *Messaging*: `https://USER:PASS@YOUR-N8N/webhook/roofradar/sms` (USER/PASS = `TWILIO_WEBHOOK_*`).
- WhatsApp sender → `…/webhook/roofradar/whatsapp`.
- Voice, **with Voice AI**: *A call comes in* → `https://VOICE-HOST/twiml/incoming`.
  **Without Voice AI**: forward with `<Dial action="https://USER:PASS@YOUR-N8N/webhook/roofradar/call-status">office number</Dial>` so missed calls hit 14.
- US numbers need an approved A2P 10DLC campaign before SMS will deliver.

**4. Voice server** (needs public HTTPS/WSS)
```bash
cd voice-server && npm ci && npm test
docker build -t roofradar-voice . && docker run --env-file ../.env -p 8080:8080 roofradar-voice
```
Deploy anywhere that supports WebSockets (Render, Fly.io, Railway, a VM behind Caddy/NGINX). It validates
`X-Twilio-Signature` on every webhook **and** on the WebSocket handshake.

**5. Website form** — POST JSON to `…/webhook/primehome-lead` with `name, phone, email, address, problem_type,
message, sms_consent` (+ a hidden `website` honeypot field left empty). Only `sms_consent=true` leads get texted.

**6. Optional** — Meta: subscribe the Page to `leadgen`, set `settings.meta.page_id` / `verify_token`.
HubSpot: set `settings.crm.enabled=true`. Slack: invite the bot to the channels in settings.

## Test it
```bash
python3 scripts/validate_workflows.py --sql "$DATABASE_URL"   # 26 workflows, every SQL statement PREPAREd
bash scripts/smoke-test.sh                                      # live webhooks; TEST_PHONE=+1… to receive texts
cd voice-server && PUBLIC_HOST=… TWILIO_AUTH_TOKEN=… npm run simulate -- wss://VOICE-HOST/voice +1CALLER +1TWILIO "Hi, my roof is leaking"
```
Full matrix and the sales-demo script: [`docs/TEST_MATRIX.md`](docs/TEST_MATRIX.md).

## Day-to-day operations
- **Resume AI after a human handled a lead** — SQL in workflow 15's sticky note.
- **Quote sent** → `UPDATE deals SET stage='QUOTE_SENT', quote_sent_at=NOW(), quote_status='OPEN' WHERE lead_id='…';` (16 takes over).
- **Inspection done** → set the appointment `COMPLETED` / `NO_SHOW` (23 flags forgotten ones).
- **Change behaviour** (hours, slot length, follow-up timing, emergency criteria, models, FAQ) → edit `organizations.settings`; no redeploy.
- **Change a workflow** → edit `n8n/gen/*.py`, `python3 n8n/gen/build.py`, `python3 scripts/deploy.py`. Edits made in the n8n UI are overwritten on the next deploy.

## Security
Secrets live only in n8n credentials and the voice-server environment — none in prompts, Code nodes, SQL or
webhook payloads. Twilio webhooks use Basic auth; voice ↔ n8n uses a header key; voice-server validates Twilio
signatures. Website form endpoint is public by nature: keep the honeypot and consider a Cloudflare rate limit.
Compliance (TCPA/A2P consent wording, recording disclosures, state rules) is your client's responsibility — have it reviewed.

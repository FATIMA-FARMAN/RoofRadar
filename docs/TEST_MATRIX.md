# RoofRadar AI — test matrix

**Build verification** (done before handover, on n8n 1.123 + PostgreSQL 16): all 26 workflows imported,
linked and activated through `scripts/deploy.py`. Every SQL statement was PREPAREd against the real schema.
The flows were then run for real, with local stand-ins for OpenAI, Google Calendar and Twilio send. Those are
marked **✔ local** below. **◻ live** means the step needs your real accounts and has to be checked after deploy.

| # | Test | How | Expected | Build check |
|---|---|---|---|---|
| 01 | Website lead creates DB record | `test/payloads/website-lead.json` → `/webhook/primehome-lead` | lead `NEW`, source `website`, 5 nurture follow-ups, consent row; first SMS if `sms_consent` | ✔ local |
| 01 | Invalid / duplicate lead | empty name; same phone twice | `INVALID_LEAD` event; second submit updates the same lead | ✔ local |
| 02 | Facebook lead creates DB record | Meta *Lead Ads Testing Tool* | lead source `facebook` | verify handshake ✔ local · Graph fetch ◻ live |
| 03 | SMS creates/updates conversation | text the Twilio number | message row, conversation `ACTIVE`, router runs | ✔ local |
| 03 | Twilio retry is idempotent | same `MessageSid` twice | one message row, one reply | ✔ local |
| 04 | Router returns correct action | "can someone inspect?", "person please", "water pouring in" | QUALIFY / HUMAN / EMERGENCY; row in `ai_interactions` | routing ✔ local · model quality ◻ live |
| 05 | Qualification gives a structured result | reply with problem + town | `lead_score` set by SQL, `qualification_status`, next step | ✔ local |
| 06 | AI generates a safe response | ask "how much is a new roof?" | no $ amounts; mentions free inspection / written quote | guardrails ✔ local · wording ◻ live |
| 07 | SMS reaches phone | any flow with `TEST_PHONE` = your phone | delivered; `messages` OUTBOUND with Twilio SID | DB side ✔ local · delivery ◻ live |
| 08 | WhatsApp message reaches system | message the WhatsApp sender | same as 03 on channel `whatsapp` | ✔ local |
| 09 | Calendar returns real availability | say "can you come Friday at 2?" | ≤3 real free slots inside business hours | slot maths ✔ local · Google ◻ live |
| 10 | 60-minute appointment is booked | reply with a slot number | Google event + `appointments` SCHEDULED + deal + confirmation SMS + Slack | ✔ local (mock Google) |
| 10 | No double booking | book the same slot twice / a slot never offered | second attempt returns alternatives | ✔ local |
| 11 | Reminder is sent | appointment starting in 22 h | 24-hour reminder once; `reminder_24h_sent_at` set | ✔ local |
| 12 | CRM contact/deal syncs | `settings.crm.enabled=true`, book a lead | HubSpot contact + deal; ids stored | disabled path ✔ local · HubSpot ◻ live |
| 13 | Follow-up sends | move a follow-up `due_at` into the past | sent 9:00–20:00 local, otherwise deferred to 9:05 | ✔ local (both paths) |
| 14 | Missed call creates recovery SMS | call the number and hang up | one text-back per CallSid, even if Twilio posts twice | ✔ local |
| 15 | Human request disables AI | "I want to talk to a person" | `human_handoff=true`, AI paused, Slack alert, customer told | ✔ local (Slack ◻ live) |
| 16 | Quote follow-up works | deal `QUOTE_SENT`, `quote_sent_at` 3 days ago | nudge without price; `quote_followups+1` | ✔ local |
| 17 | Metrics calculate | run 17 | row in `analytics_daily` | ✔ local |
| 18 | Daily report arrives | run 18 | Slack + email with KPIs, today's inspections, hot leads | text ✔ local · delivery ◻ live |
| 19 | Artificial error triggers alert | bad OpenAI key | `errors` row, category AUTHENTICATION, one alert | classification ✔ local · Slack ◻ live |
| 20 | Failed operation retries | OpenAI 503 then recovery | queued → retried from failed node → COMPLETE; no duplicate SMS | ✔ local |
| 21 | STOP disables outreach | text STOP, then START | lead STOPPED, follow-ups cancelled; START reactivates | ✔ local |
| 22 | Monitoring detects outage | revoke a credential | component CRITICAL, alert on state change | detection ✔ local · Slack ◻ live |
| 23 | Maintenance detects anomalies | past appointment left SCHEDULED | listed in the maintenance log + Slack | detection ✔ local · Slack ◻ live |
| 24 | Voice caller can qualify / book / transfer | call the number | qualify → offer real times → booked → SMS; "person" → warm transfer | ✔ local (`npm run simulate`) · Twilio audio ◻ live |

## Final end-to-end test (the 60–90 second sales demo)

1. Submit the website form: *John Smith · roof leak · Newark, NJ · SMS consent ✔*.
2. **01** creates the lead and **06** texts John within seconds (median first response is in the daily report).
3. John replies: *"Yes, I need someone to inspect my roof."*
4. **03 → 04 → 05**: urgency HIGH, lead score is high, next step BOOK.
5. **09** checks Google Calendar and **06** texts 3 real times.
6. John replies **"2"**.
7. **10** books 2:00–3:00 PM: calendar event, `appointments` SCHEDULED, lead `BOOKED`, deal created.
8. **12** updates the CRM.
9. Sales gets on Slack:
   ```
   🔥 NEW BOOKED LEAD
   John Smith
   roof leak
   123 Main Street, Newark, NJ
   Thursday, Sep 24 at 2:00 PM inspection
   Lead score: 87
   ```
10. John gets: *"Your PrimeHome Roofing roof inspection is confirmed for Thursday, Sep 24 at 2:00 PM…"*

Useful SQL while demoing:
```sql
SELECT name, status, lead_score, urgency FROM leads ORDER BY updated_at DESC LIMIT 5;
SELECT direction, body, created_at FROM messages ORDER BY created_at DESC LIMIT 10;
SELECT workflow, action, confidence, valid FROM ai_interactions ORDER BY created_at DESC LIMIT 10;
```

"""Workflows 13–18 — follow-ups, missed calls, human handoff, quotes, analytics, daily report."""
from lib import WF
from common import FILL_JS, LEAD_CTX_SQL, prompt_js
from wf_intake import PHONE_FN
import prompts

T = "$('EXECUTE_WORKFLOW_TRIGGER').first().json"

PARSE_EACH_JS = r"""
const r = $json;
const content = r.choices?.[0]?.message?.content || '';
let parsed = null, parse_error = null;
try { parsed = JSON.parse(content); } catch (e) {
  const m = content.match(/\{[\s\S]*\}/);
  if (m) { try { parsed = JSON.parse(m[0]); } catch (e2) { parse_error = e2.message; } } else parse_error = e.message;
}
return { json: { parsed, parse_error, raw: content } };
"""


# =====================================================================
def wf13():
    w = WF("13", "RR 13 · Follow-up Engine",
           "Every 5 min: due nurture/missed-call follow-ups → status + consent + quiet hours → AI message (via 06) → 07")
    trig = w.schedule("SCHEDULE_TRIGGER", minutes=5)
    find = w.sql("FIND_FOLLOWUPS_DUE", """
SELECT f.id AS followup_id, f.step, f.type, f.lead_id, f.created_at AS followup_created_at,
       l.status, l.human_handoff, l.last_inbound_at, l.organization_id, l.preferred_channel,
       o.timezone, rr_can_contact(l.id, 'sms') AS can_contact, (l.phone IS NOT NULL) AS has_phone
FROM followups f
JOIN leads l ON l.id = f.lead_id
JOIN organizations o ON o.id = l.organization_id
WHERE f.status = 'PENDING' AND f.due_at <= NOW() AND f.type IN ('NURTURE','MISSED_CALL')
ORDER BY f.due_at
LIMIT 25;""")
    chk = w.code("CHECK_STATUS", r"""
const r = $json;
const stopStatuses = ['BOOKED','HUMAN_HANDOFF','STOPPED','DISQUALIFIED','WON','LOST'];
let decision = 'SEND', reason = null, defer_until = null;
if (stopStatuses.includes(r.status) || r.human_handoff) { decision = 'SKIP'; reason = 'status_' + (r.human_handoff ? 'HUMAN' : r.status); }
else if (!r.can_contact) { decision = 'SKIP'; reason = 'no_consent'; }
else if (!r.has_phone) { decision = 'SKIP'; reason = 'no_phone'; }
else if (r.last_inbound_at && new Date(r.last_inbound_at) > new Date(r.followup_created_at)) { decision = 'SKIP'; reason = 'customer_replied'; }
else {
  // quiet hours: only text 9:00–20:00 in the company timezone
  const now = DateTime.now().setZone(r.timezone || 'America/New_York');
  if (now.hour < 9 || now.hour >= 20) {
    decision = 'DEFER';
    const next = now.hour >= 20 ? now.plus({ days: 1 }) : now;
    defer_until = next.set({ hour: 9, minute: 5, second: 0, millisecond: 0 }).toUTC().toISO();
  }
}
return { json: { ...r, decision, reason, defer_until } };""", each=True)
    sw = w.switch("ROUTE_DECISION", "$json.decision", ["SEND", "DEFER", "SKIP"])
    skip = w.sql("MARK_SKIPPED", "UPDATE followups SET status='SKIPPED', skip_reason=$2::text WHERE id=$1::uuid RETURNING id;",
                 ["$json.followup_id", "$json.reason"])
    defer = w.sql("DEFER_FOLLOWUP", "UPDATE followups SET due_at=$2::timestamptz WHERE id=$1::uuid AND status='PENDING' RETURNING id;",
                  ["$json.followup_id", "$json.defer_until"])
    claim = w.sql("CLAIM_FOLLOWUP", """
UPDATE followups SET status='SENT', sent_at=NOW()
WHERE id=$1::uuid AND status='PENDING'
RETURNING id AS followup_id, lead_id, step, type;""", ["$json.followup_id"], gate="followup_id")
    prep = w.code("PREP_AI_FOLLOWUP", """const f = $json;
return { json: { lead_id: f.lead_id, organization_id: $('CHECK_STATUS').item.json.organization_id,
  channel: $('CHECK_STATUS').item.json.preferred_channel === 'whatsapp' ? 'whatsapp' : 'sms',
  mode: 'FOLLOWUP', step: f.step } };""", each=True)
    gen = w.call("AI_GENERATE_FOLLOWUP", "06", onError="continueRegularOutput")
    upd = w.sql("UPDATE_FOLLOWUP", """
UPDATE followups SET
  status = CASE WHEN $2::boolean THEN 'SENT' ELSE 'FAILED' END,
  skip_reason = CASE WHEN $2::boolean THEN NULL ELSE $3::text END
WHERE id = $1::uuid
RETURNING id, lead_id, status;""", ["$('CLAIM_FOLLOWUP').item.json.followup_id", "$json.sent === true",
                                     "$json.reason || $json.error || 'send_failed'"])
    act = w.sql("LOG_ACTIVITY", """
INSERT INTO activities (organization_id, lead_id, type, description, metadata)
SELECT organization_id, id, 'FOLLOWUP_' || $2::text, 'Follow-up step ' || $3::text, '{}'::jsonb FROM leads WHERE id = $1::uuid
RETURNING id;""", ["$json.lead_id", "$json.status", "$('CLAIM_FOLLOWUP').item.json.step"])
    w.chain(trig, find, chk, sw)
    w.connect(sw, claim, 0)
    w.connect(sw, defer, 1)
    w.connect(sw, skip, 2)
    w.chain(claim, prep, gen, upd, act)
    w.note("""## 13 · Follow-up Engine
Default sequence from `settings.followup_sequence_minutes`: 15 min · 4 h · 24 h · 3 d · 7 d.
Stops for BOOKED / HUMAN / STOPPED / DISQUALIFIED, skips if the customer replied, defers outside 9:00–20:00 local.
Claim-then-send: a follow-up row is marked before sending, so overlapping runs can never double-text.""", width=600)
    return w


# =====================================================================
def wf14():
    w = WF("14", "RR 14 · Missed Call Recovery", "Twilio call status → missed? → text-back within seconds + follow-up")
    hook = w.webhook("TWILIO_CALL_STATUS_WEBHOOK", "roofradar/call-status", respond="responseNode", auth="basic")
    twiml = w.respond("RESPOND_TWIML",
                      "={{ ($json.body.DialCallStatus && $json.body.DialCallStatus !== 'completed') ? "
                      "'<Response><Say>Sorry we missed your call. We will text you right away.</Say><Hangup/></Response>' : '<Response></Response>' }}",
                      content_type="text/xml")
    norm = w.code("NORMALIZE_CALL", PHONE_FN + r"""
const b = $json.body ?? $json;
const status = String(b.DialCallStatus || b.CallStatus || '').toLowerCase();
return [{ json: {
  call_sid: b.CallSid, phone_e164: normalizePhone(b.From), business_phone: normalizePhone(b.To),
  status, direction: b.Direction || 'inbound', duration: Number(b.CallDuration || b.DialCallDuration || 0)
}}];""")
    chk = w.code("CHECK_STATUS", r"""
const missedStates = ['no-answer', 'busy', 'failed', 'canceled'];
const inbound = String($json.direction).startsWith('inbound');
return [{ json: { ...$json, missed: inbound && missedStates.includes($json.status) && !!$json.phone_e164 } }];""")
    org = w.sql("RESOLVE_ORG", "SELECT id, name, settings FROM organizations WHERE business_phone = $1::text AND active LIMIT 1;",
                ["$json.business_phone"])
    C = "$('CHECK_STATUS').first().json"
    missed = w.if_("MISSED?", f"{C}.missed")
    logcall = w.sql("LOG_CALL", """
INSERT INTO activities (organization_id, type, description, metadata)
VALUES ($1::uuid, 'CALL', $2::text, $3::jsonb) RETURNING id;""",
                    ["$json.id", f"'Call ' + {C}.status",
                     f"JSON.stringify({{call_sid: {C}.call_sid, from: {C}.phone_e164, duration: {C}.duration}})"])
    lead = w.sql("FIND_LEAD", """
WITH l AS (
  INSERT INTO leads (organization_id, phone, source, status)
  VALUES ($1::uuid, $2::text, 'missed_call', 'NEW')
  ON CONFLICT (organization_id, phone) WHERE phone IS NOT NULL DO UPDATE SET updated_at = NOW()
  RETURNING *
), claim AS (   -- claim the CallSid first: a duplicate Twilio callback can never text twice
  INSERT INTO activities (organization_id, lead_id, type, description, metadata)
  SELECT l.organization_id, l.id, 'MISSED_CALL', 'Missed call', jsonb_build_object('call_sid', $3::text, 'status', $4::text)
  FROM l
  ON CONFLICT ((metadata->>'call_sid')) WHERE type = 'MISSED_CALL' DO NOTHING
  RETURNING id
)
SELECT l.id AS lead_id, l.name, l.status, l.organization_id, (SELECT id FROM claim) AS activity_id,
       NOT EXISTS (SELECT 1 FROM claim) AS already_handled,
       -- rr_can_contact() can't see a row inserted by this same statement, so check the CTE row directly
       (l.status NOT IN ('STOPPED','BOOKED','HUMAN_HANDOFF') AND COALESCE((SELECT c.consent_status <> 'OPTED_OUT' FROM consents c
                  WHERE c.lead_id = l.id AND c.channel IN ('sms','all') ORDER BY c.captured_at DESC LIMIT 1), true)) AS can_contact
FROM l;""", ["$('RESOLVE_ORG').first().json.id", f"{C}.phone_e164", f"{C}.call_sid", f"{C}.status"])
    consent = w.if_("CHECK_CONSENT", "$json.can_contact && !$json.already_handled")
    prep = w.code("PREP_SMS", r"""const org = $('RESOLVE_ORG').first().json;
const first = ($json.name || '').split(' ')[0];
const body = `Hi${first ? ' ' + first : ''}, this is ${org.name}. We missed your call. How can we help with your roofing needs? Reply STOP to opt out.`;
return [{ json: { lead_id: $json.lead_id, channel: 'sms', body, kind: 'MISSED_CALL', ai_generated: false } }];""")
    send = w.call("SEND_SMS", "07")
    fu = w.sql("CREATE_FOLLOWUP", "SELECT rr_schedule_nurture($1::uuid, NOW(), 2) AS scheduled;",
               ["$('FIND_LEAD').first().json.lead_id"])
    act = w.sql("LOG_ACTIVITY", """
UPDATE activities SET description = 'Missed call — text-back sent', metadata = metadata || jsonb_build_object('sms_sent', $2::boolean)
WHERE id = $1::uuid RETURNING id;""", ["$('FIND_LEAD').first().json.activity_id", "$('SEND_SMS').first().json.sent === true"])
    w.chain(hook, twiml, norm, chk, org, missed)
    w.connect(missed, lead, 0)
    w.connect(missed, logcall, 1)
    w.connect(lead, consent)
    w.connect(consent, prep, 0)
    w.chain(prep, send, fu, act)
    w.note("""## 14 · Missed Call Recovery
Point the Twilio number's **Call status changes** callback (or a `<Dial action>` on your forwarding TwiML) at
`/webhook/roofradar/call-status`. Missed inbound calls get a text-back and enter the nurture sequence at step 2.
De-duplicated by CallSid.""")
    return w


# =====================================================================
def wf15():
    w = WF("15", "RR 15 · Human Handoff",
           "Flag lead, open handoff, alert Slack, tell the customer, pause AI, stop follow-ups")
    trig = w.sub_trigger()
    lead = w.sql("GET_LEAD", LEAD_CTX_SQL, [f"{T}.lead_id", "'sms'"])
    conv = w.sql("GET_CONVERSATION", """
SELECT id FROM conversations
WHERE (id = NULLIF($1::text,'')::uuid) OR (NULLIF($1::text,'') IS NULL AND lead_id = $2::uuid AND status = 'ACTIVE')
ORDER BY last_message_at DESC LIMIT 1;""", [f"{T}.conversation_id || ''", f"{T}.lead_id"], always=True)
    seth = w.sql("SET_HUMAN_HANDOFF", """
UPDATE leads SET human_handoff = true, status = 'HUMAN_HANDOFF',
       urgency = CASE WHEN $2::text = 'EMERGENCY' THEN 'EMERGENCY' ELSE urgency END
WHERE id = $1::uuid
RETURNING *;""", [f"{T}.lead_id", f"{T}.priority || 'NORMAL'"])
    ho = w.sql("CREATE_HANDOFF", """
INSERT INTO human_handoffs (organization_id, lead_id, conversation_id, reason, priority, channel)
VALUES ($1::uuid, $2::uuid, NULLIF($3::text,'')::uuid, $4::text, $5::text, $6::text)
ON CONFLICT (lead_id) WHERE status = 'OPEN'
DO UPDATE SET reason = human_handoffs.reason || ' | ' || EXCLUDED.reason,
              priority = CASE WHEN EXCLUDED.priority = 'EMERGENCY' THEN 'EMERGENCY' ELSE human_handoffs.priority END
RETURNING *;""", ["$json.organization_id", "$json.id", "$('GET_CONVERSATION').first().json.id || ''",
                  f"{T}.reason || 'Customer requested a person'", f"{T}.priority || 'NORMAL'", f"{T}.channel || 'sms'"])
    L = "$('SET_HUMAN_HANDOFF').first().json"
    org = "$('GET_LEAD').first().json.org"
    slack = w.slack("SEND_SLACK_ALERT", f"{org}.settings.slack_channel",
                    f"($json.priority === 'EMERGENCY' ? '🚨🚨 EMERGENCY HANDOFF' : '🚨 HUMAN HANDOFF') + '\\n\\n' + "
                    f"'Lead: ' + ({L}.name || 'Unknown') + '\\nPhone: ' + ({L}.phone || 'n/a') + "
                    f"'\\nProblem: ' + ({L}.problem_type || 'n/a') + '\\nUrgency: ' + ({L}.urgency || 'n/a') + "
                    f"'\\nScore: ' + {L}.lead_score + '\\nChannel: ' + ({T}.channel || 'sms') + "
                    f"'\\n\\nReason:\\n' + ({T}.reason || 'Customer requested a person')")
    notify = w.if_("NOTIFY_CUSTOMER?", f"{T}.notify_customer !== false && ['sms','whatsapp'].includes({T}.channel || 'sms')")
    prep = w.code("PREP_CUSTOMER_CONFIRMATION", f"""const t = {T};
const org = {org};
const body = t.priority === 'EMERGENCY'
  ? `We've alerted the ${{org.name}} emergency team and someone will call you shortly. If anyone is in danger, leave the area and call 911.`
  : `Thanks — I've passed this to the ${{org.name}} team and a team member will get back to you shortly.`;
return [{{ json: {{ lead_id: t.lead_id, channel: t.channel === 'whatsapp' ? 'whatsapp' : 'sms', body, kind: 'HANDOFF', ai_generated: false }} }}];""")
    send = w.call("SEND_CUSTOMER_CONFIRMATION", "07", onError="continueRegularOutput")
    pause = w.sql("PAUSE_AI", """
WITH c AS (UPDATE conversations SET ai_active = false WHERE lead_id = $1::uuid AND status = 'ACTIVE' RETURNING id)
SELECT (SELECT count(*) FROM c) AS paused_conversations, rr_cancel_followups($1::uuid, 'HUMAN_HANDOFF') AS cancelled_followups;""",
                  [f"{T}.lead_id"])
    log = w.sql("LOG_EVENT", "SELECT rr_log_event($1::uuid, $2::uuid, '15_HUMAN_HANDOFF', 'HANDOFF_CREATED', $3::jsonb) AS id;",
                [f"{L}.organization_id", f"{T}.lead_id",
                 f"JSON.stringify({{reason: {T}.reason, priority: {T}.priority || 'NORMAL', channel: {T}.channel}})"])
    ret = w.code("RETURN_HANDOFF", f"""return [{{ json: {{
  handoff: true, handoff_id: $('CREATE_HANDOFF').first().json.id,
  priority: $('CREATE_HANDOFF').first().json.priority,
  handoff_phone: {org}.settings.handoff_phone || null
}} }}];""")
    w.chain(trig, lead, conv, seth, ho, slack, notify)
    w.connect(notify, prep, 0)
    w.connect(notify, pause, 1)
    w.chain(prep, send, pause, log, ret)
    w.note("""## 15 · Human Handoff
Resume AI for a lead after a human is done:
`UPDATE leads SET human_handoff=false, status='ENGAGED' WHERE id='…';`
`UPDATE conversations SET ai_active=true WHERE lead_id='…' AND status='ACTIVE';`
`UPDATE human_handoffs SET status='RESOLVED', resolved_at=NOW() WHERE lead_id='…' AND status='OPEN';`""", width=620)
    return w


# =====================================================================
def wf16():
    w = WF("16", "RR 16 · Quote Follow-up", "Daily: open quotes past their follow-up day → AI nudge (no prices, no pressure) → 07")
    trig = w.schedule("SCHEDULE_TRIGGER", cron="0 10 * * *")
    find = w.sql("FIND_OPEN_QUOTES", """
SELECT d.id AS deal_id, d.lead_id, d.quote_followups, d.quote_sent_at, d.last_contact_at,
       l.name, l.problem_type, l.status, l.organization_id, o.name AS org_name, o.settings,
       rr_can_contact(l.id, 'sms') AS can_contact, l.last_inbound_at
FROM deals d
JOIN leads l ON l.id = d.lead_id
JOIN organizations o ON o.id = d.organization_id
WHERE d.stage = 'QUOTE_SENT'
  AND coalesce(d.quote_status, 'OPEN') IN ('OPEN','FOLLOWED_UP')
  AND d.quote_sent_at IS NOT NULL
  AND d.quote_followups < jsonb_array_length(coalesce(o.settings->'quote_followup_days', '[2,5,10]'::jsonb))
  AND d.quote_sent_at + make_interval(days => (coalesce(o.settings->'quote_followup_days', '[2,5,10]'::jsonb)->>d.quote_followups)::int) <= NOW()
LIMIT 25;""")
    last = w.code("CHECK_LAST_CONTACT", r"""
const r = $json;
const dayMs = 24 * 3600e3;
const recent = [r.last_contact_at, r.last_inbound_at].filter(Boolean).some(t => Date.now() - new Date(t).getTime() < dayMs);
return { json: { ...r, send: !recent && r.status !== 'STOPPED' } };""", each=True)
    consent = w.if_("CHECK_CONSENT", "$json.send && $json.can_contact")
    prep = w.code("PREP_AI_MESSAGE", FILL_JS + f"const P = {prompt_js(prompts.FOLLOWUP_QUOTE)};\n" + r"""
const r = $json;
const user = { customer_first_name: (r.name || '').split(' ')[0] || null, roofing_issue: r.problem_type,
               followup_number: r.quote_followups + 1, company: r.org_name };
return { json: { openai_request: buildOpenAI(r.settings?.ai?.models?.followup || 'gpt-5.4-mini', fill(P, { company: r.org_name }), user, 800) } };""", each=True)
    ai = w.openai("AI_GENERATE_MESSAGE")
    parse = w.code("PARSE_JSON", PARSE_EACH_JS, each=True)
    val = w.code("VALIDATE", r"""
const q = $('CHECK_LAST_CONTACT').item.json;
let reply = typeof $json.parsed?.reply === 'string' ? $json.parsed.reply.trim() : '';
const bad = !reply || reply.length > 320 || /\$\s?\d|\d+\s?(dollars|usd)|\b(expires?|deadline|only today|last chance)\b/i.test(reply);
const first = (q.name || '').split(' ')[0];
if (bad) reply = `Hi${first ? ' ' + first : ''}, it's ${q.org_name} — just checking whether you had any questions about your roofing quote, or if you'd like to go ahead. Happy to help either way.`;
return { json: { lead_id: q.lead_id, deal_id: q.deal_id, channel: 'sms', body: reply, kind: 'QUOTE_FOLLOWUP', ai_generated: !bad } };""", each=True)
    send = w.call("SEND_SMS", "07", onError="continueRegularOutput")
    upd = w.sql("UPDATE_QUOTE_STATUS", """
UPDATE deals SET quote_followups = quote_followups + 1, quote_status = 'FOLLOWED_UP', last_contact_at = NOW()
WHERE id = $1::uuid RETURNING id, lead_id, organization_id, quote_followups;""", ["$('VALIDATE').item.json.deal_id"])
    act = w.sql("LOG_ACTIVITY", """
INSERT INTO activities (organization_id, lead_id, type, description, metadata)
VALUES ($1::uuid, $2::uuid, 'QUOTE_FOLLOWUP', 'Quote follow-up #' || $3::text, '{}'::jsonb) RETURNING id;""",
                ["$json.organization_id", "$json.lead_id", "$json.quote_followups"])
    w.chain(trig, find, last, consent)
    w.connect(consent, prep, 0)
    w.chain(prep, ai, parse, val, send, upd, act)
    w.note("""## 16 · Quote Follow-up
A human moves a deal to `stage='QUOTE_SENT', quote_sent_at=NOW()` (or your CRM sync does).
Nudges go out on the days in `settings.quote_followup_days` (default 2, 5, 10). The AI never sees or states the price.""")
    return w


# =====================================================================
def wf17():
    w = WF("17", "RR 17 · Analytics", "Nightly KPI aggregation per organisation into analytics_daily")
    trig = w.schedule("SCHEDULE_TRIGGER", cron="15 0 * * *")
    q = w.sql("QUERY_DAILY_METRICS", """
WITH o AS (
  SELECT id, timezone, ((NOW() AT TIME ZONE timezone)::date - 1) AS day FROM organizations WHERE active
), w AS (
  SELECT id, day, (day::timestamp AT TIME ZONE timezone) AS s, ((day + 1)::timestamp AT TIME ZONE timezone) AS e FROM o
)
SELECT w.id AS organization_id, to_char(w.day, 'YYYY-MM-DD') AS day,   -- text: avoids JS Date timezone shifts
  (SELECT count(*) FROM leads l WHERE l.organization_id = w.id AND l.created_at >= w.s AND l.created_at < w.e)::int AS new_leads,
  (SELECT count(*) FROM leads l WHERE l.organization_id = w.id AND l.created_at >= w.s AND l.created_at < w.e
      AND l.qualification_status = 'QUALIFIED')::int AS qualified_leads,
  (SELECT count(*) FROM appointments a WHERE a.organization_id = w.id AND a.created_at >= w.s AND a.created_at < w.e
      AND a.rescheduled_from IS NULL)::int AS bookings,
  (SELECT coalesce(round(avg(l.lead_score), 2), 0) FROM leads l WHERE l.organization_id = w.id
      AND l.created_at >= w.s AND l.created_at < w.e) AS average_lead_score,
  (SELECT count(*) FROM human_handoffs h WHERE h.organization_id = w.id AND h.created_at >= w.s AND h.created_at < w.e)::int AS human_handoffs,
  (SELECT count(*) FROM messages m WHERE m.organization_id = w.id AND m.direction = 'OUTBOUND' AND m.created_at >= w.s AND m.created_at < w.e)::int AS response_count,
  (SELECT count(*) FROM messages m WHERE m.organization_id = w.id AND m.direction = 'INBOUND' AND m.created_at >= w.s AND m.created_at < w.e)::int AS inbound_messages,
  (SELECT count(*) FROM activities a WHERE a.organization_id = w.id AND a.type = 'MISSED_CALL' AND a.created_at >= w.s AND a.created_at < w.e)::int AS missed_calls,
  (SELECT count(*) FROM followups f WHERE f.organization_id = w.id AND f.status = 'SENT' AND f.sent_at >= w.s AND f.sent_at < w.e)::int AS followup_count,
  (SELECT count(*) FROM voice_sessions v WHERE v.organization_id = w.id AND v.started_at >= w.s AND v.started_at < w.e)::int AS voice_calls,
  (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM fo.first_out - l.created_at))
     FROM leads l CROSS JOIN LATERAL (SELECT min(m.created_at) AS first_out FROM messages m
                                      WHERE m.lead_id = l.id AND m.direction = 'OUTBOUND') fo
    WHERE l.organization_id = w.id AND l.created_at >= w.s AND l.created_at < w.e AND fo.first_out IS NOT NULL)::int
    AS median_first_response_seconds,
  (SELECT coalesce(jsonb_object_agg(source, n), '{}'::jsonb) FROM (
     SELECT source, count(*) AS n FROM leads l WHERE l.organization_id = w.id AND l.created_at >= w.s AND l.created_at < w.e
     GROUP BY source) x) AS by_source
FROM w;""", gate="organization_id")
    kpi = w.code("CALCULATE_KPIS", r"""
const r = $json;
const booking_rate = r.new_leads > 0 ? Math.round((r.bookings / r.new_leads) * 10000) / 10000 : 0;
return { json: { ...r, booking_rate } };""", each=True)
    save = w.sql("SAVE_ANALYTICS", """
INSERT INTO analytics_daily (organization_id, day, new_leads, qualified_leads, bookings, booking_rate, average_lead_score,
  human_handoffs, response_count, inbound_messages, missed_calls, followup_count, voice_calls,
  median_first_response_seconds, by_source, computed_at)
VALUES ($1::uuid, $2::date, $3::int, $4::int, $5::int, $6::numeric, $7::numeric, $8::int, $9::int, $10::int, $11::int,
        $12::int, $13::int, $14::int, $15::jsonb, NOW())
ON CONFLICT (organization_id, day) DO UPDATE SET
  new_leads = EXCLUDED.new_leads, qualified_leads = EXCLUDED.qualified_leads, bookings = EXCLUDED.bookings,
  booking_rate = EXCLUDED.booking_rate, average_lead_score = EXCLUDED.average_lead_score,
  human_handoffs = EXCLUDED.human_handoffs, response_count = EXCLUDED.response_count,
  inbound_messages = EXCLUDED.inbound_messages, missed_calls = EXCLUDED.missed_calls,
  followup_count = EXCLUDED.followup_count, voice_calls = EXCLUDED.voice_calls,
  median_first_response_seconds = EXCLUDED.median_first_response_seconds, by_source = EXCLUDED.by_source,
  computed_at = NOW()
RETURNING organization_id, day;""",
                 ["$json.organization_id", "$json.day", "$json.new_leads", "$json.qualified_leads", "$json.bookings",
                  "$json.booking_rate", "$json.average_lead_score", "$json.human_handoffs", "$json.response_count",
                  "$json.inbound_messages", "$json.missed_calls", "$json.followup_count", "$json.voice_calls",
                  "$json.median_first_response_seconds", "JSON.stringify($json.by_source || {})"])
    w.chain(trig, q, kpi, save)
    return w


# =====================================================================
def wf18():
    w = WF("18", "RR 18 · Daily Report", "08:00: yesterday's KPIs + today's inspections + hot leads → Slack + email")
    trig = w.schedule("SCHEDULE_TRIGGER", cron="0 8 * * *")
    an = w.sql("GET_DAILY_ANALYTICS", """
SELECT o.id AS organization_id, o.name AS org_name, o.timezone, o.settings,
       to_char((NOW() AT TIME ZONE o.timezone)::date - 1, 'YYYY-MM-DD') AS day,
       to_jsonb(a.*) AS metrics
FROM organizations o
LEFT JOIN analytics_daily a ON a.organization_id = o.id AND a.day = ((NOW() AT TIME ZONE o.timezone)::date - 1)
WHERE o.active;""")
    ap = w.sql("GET_APPOINTMENTS", """
SELECT coalesce(jsonb_agg(jsonb_build_object('start', a.start_at, 'name', l.name, 'address', l.address,
                'problem', l.problem_type, 'phone', l.phone) ORDER BY a.start_at), '[]'::jsonb) AS appointments
FROM appointments a JOIN leads l ON l.id = a.lead_id
WHERE a.organization_id = $1::uuid AND a.status IN ('SCHEDULED','CONFIRMED')
  AND a.start_at >= ((NOW() AT TIME ZONE $2::text)::date)::timestamp AT TIME ZONE $2::text
  AND a.start_at <  (((NOW() AT TIME ZONE $2::text)::date) + 1)::timestamp AT TIME ZONE $2::text;""",
               ["$json.organization_id", "$json.timezone"])
    hot = w.sql("GET_HOT_LEADS", """
SELECT coalesce(jsonb_agg(jsonb_build_object('name', coalesce(name, phone), 'score', lead_score, 'status', status,
                'problem', problem_type) ORDER BY lead_score DESC), '[]'::jsonb) AS hot_leads
FROM (SELECT * FROM leads
      WHERE organization_id = $1::uuid AND lead_score >= 70
        AND status NOT IN ('BOOKED','WON','LOST','STOPPED','DISQUALIFIED')
        AND created_at > NOW() - INTERVAL '14 days'
      ORDER BY lead_score DESC LIMIT 5) t;""", ["$('GET_DAILY_ANALYTICS').item.json.organization_id"])
    rep = w.code("GENERATE_REPORT", r"""
const a = $('GET_DAILY_ANALYTICS').item.json;
const m = a.metrics || {};
const tz = a.timezone;
const appts = $('GET_APPOINTMENTS').item.json.appointments || [];
const hot = $json.hot_leads || [];
const day = DateTime.fromISO(a.day).toFormat('ccc LLL d');
const pct = v => (Number(v || 0) * 100).toFixed(0) + '%';
const lines = [
  `${a.org_name.toUpperCase()} DAILY REPORT — ${day}`,
  '',
  `New leads: ${m.new_leads ?? 0}`,
  `Qualified: ${m.qualified_leads ?? 0}`,
  `Appointments booked: ${m.bookings ?? 0} (booking rate ${pct(m.booking_rate)})`,
  `Human handoffs: ${m.human_handoffs ?? 0}`,
  `Missed calls: ${m.missed_calls ?? 0}`,
  `Voice AI calls: ${m.voice_calls ?? 0}`,
  `Median first response: ${m.median_first_response_seconds != null ? m.median_first_response_seconds + 's' : 'n/a'}`,
  '',
  `Today's inspections (${appts.length}):`,
  ...(appts.length ? appts.map(x => `• ${DateTime.fromISO(x.start).setZone(tz).toFormat('h:mm a')} — ${x.name || x.phone}${x.address ? ', ' + x.address : ''}${x.problem ? ' (' + x.problem + ')' : ''}`) : ['• none']),
  '',
  'Hot leads (not yet booked):',
  ...(hot.length ? hot.map(h => `• ${h.name} — ${h.score}`) : ['• none'])
];
if (!a.metrics) lines.splice(1, 0, '(analytics not computed yet — workflow 17 runs at 00:15)');
return { json: { text: lines.join('\n'), subject: `${a.org_name} daily report — ${day}`,
                 slack_channel: a.settings?.slack_channel, report_email: a.settings?.report_email } };""", each=True)
    slack = w.slack("SEND_SLACK", "$json.slack_channel", "'```' + $json.text + '```'")
    email = w.add("SEND_EMAIL", "n8n-nodes-base.emailSend", 2.1, {
        "fromEmail": "RoofRadar AI <reports@roofradar.ai>",
        "toEmail": "={{ $('GENERATE_REPORT').item.json.report_email }}",
        "subject": "={{ $('GENERATE_REPORT').item.json.subject }}",
        "emailFormat": "text",
        "text": "={{ $('GENERATE_REPORT').item.json.text }}",
        "options": {},
    }, ["SMTP"], onError="continueRegularOutput")
    w.chain(trig, an, ap, hot, rep, slack, email)
    return w

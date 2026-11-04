"""Workflow 24 — Voice AI (n8n side). Real-time audio runs in voice-server/ (Twilio ConversationRelay).
24A = tool gateway the voice AI calls · 24B = post-call processing · 24C = warm-transfer result."""
from lib import WF
from common import FILL_JS, PARSE_JS, prompt_js
from wf_intake import PHONE_FN
import prompts

W = "$('VOICE_TOOL_WEBHOOK').first().json.body"


def wf24a():
    w = WF("24A", "RR 24A · Voice AI Tool Gateway",
           "Controlled tools for the voice agent: every action is validated here and executed by the same workflows as SMS")
    hook = w.webhook("VOICE_TOOL_WEBHOOK", "roofradar/voice-tool", respond="responseNode", auth="header")
    norm = w.code("NORMALIZE_TOOL_CALL", PHONE_FN + r"""
const b = $json.body || {};
const allowed = ['find_lead','get_lead','qualify_lead','get_available_slots','book_appointment',
                 'reschedule_appointment','send_sms_confirmation','human_handoff','opt_out'];
const call = b.call || {};
const tool = allowed.includes(b.tool) ? (b.tool === 'get_lead' ? 'find_lead' : b.tool) : 'INVALID';
return [{ json: {
  tool, requested_tool: b.tool, args: b.args || {},
  call_sid: call.call_sid || null,
  from: normalizePhone(call.from), to: normalizePhone(call.to),
  lead_id: call.lead_id || null, conversation_id: call.conversation_id || null,
  session_id: call.session_id || null
}}];""")
    sw = w.switch("ROUTE_TOOL", "$json.tool", ["find_lead", "qualify_lead", "get_available_slots", "book_appointment",
                                               "reschedule_appointment", "send_sms_confirmation", "human_handoff", "opt_out"])
    N = "$('NORMALIZE_TOOL_CALL').first().json"
    J = lambda body: "={{ JSON.stringify(" + body + ") }}"

    # find_lead ---------------------------------------------------------
    fl = w.sql("FIND_OR_CREATE_VOICE_LEAD", """
WITH o AS (SELECT id, name, settings FROM organizations WHERE business_phone = $2::text AND active LIMIT 1),
l AS (
  INSERT INTO leads (organization_id, phone, source, status, preferred_channel)
  SELECT o.id, $1::text, 'voice', 'NEW', 'sms' FROM o
  ON CONFLICT (organization_id, phone) WHERE phone IS NOT NULL DO UPDATE SET updated_at = NOW()
  RETURNING *, (xmax = 0) AS inserted
),
c AS (
  INSERT INTO conversations (organization_id, lead_id, channel, status, ai_active, external_session_id, last_message_at)
  SELECT l.organization_id, l.id, 'voice', 'ACTIVE', true, $3::text, NOW() FROM l
  ON CONFLICT (lead_id, channel) WHERE status = 'ACTIVE'
  DO UPDATE SET external_session_id = EXCLUDED.external_session_id, last_message_at = NOW()
  RETURNING id
),
s AS (
  UPDATE voice_sessions SET lead_id = (SELECT id FROM l), organization_id = (SELECT organization_id FROM l)
  WHERE call_sid = $3::text RETURNING id
)
SELECT l.id AS lead_id, (SELECT id FROM c) AS conversation_id, l.organization_id,
       l.name, l.address, l.problem_type, l.urgency, l.status, l.lead_score, l.human_handoff,
       l.inserted AS is_new_caller,
       (l.status <> 'STOPPED' AND COALESCE((SELECT c.consent_status <> 'OPTED_OUT' FROM consents c
                  WHERE c.lead_id = l.id AND c.channel IN ('sms','all') ORDER BY c.captured_at DESC LIMIT 1), true)) AS can_text,
       (SELECT jsonb_build_object('appointment_id', a.id, 'start_at', a.start_at)
          FROM appointments a WHERE a.lead_id = l.id AND a.status IN ('SCHEDULED','CONFIRMED') AND a.start_at > NOW()
          ORDER BY a.start_at LIMIT 1) AS upcoming_appointment,
       (SELECT count(*) FROM s) AS session_linked
FROM l;""", [f"{N}.from", f"{N}.to", f"{N}.call_sid"])
    r_fl = w.respond("RESPOND_FIND_LEAD", J("{ ok: true, lead: $json }"))

    # qualify_lead -------------------------------------------------------
    ql = w.sql("SAVE_CALLER_FACTS", """
UPDATE leads SET
  name = COALESCE(NULLIF($2::text,''), name),
  address = COALESCE(NULLIF($3::text,''), address),
  problem_type = COALESCE(NULLIF($4::text,''), problem_type),
  email = COALESCE(email, NULLIF($5::text,''))
WHERE id = $1::uuid RETURNING id AS lead_id, organization_id;""",
               [f"{N}.lead_id", f"{N}.args.name || ''", f"{N}.args.address || ''", f"{N}.args.problem_type || ''",
                f"{N}.args.email || ''"])
    pq = w.code("PREP_QUALIFY", f"return [{{ json: {{ lead_id: {N}.lead_id, conversation_id: {N}.conversation_id, organization_id: $json.organization_id, channel: 'voice', mode: 'SCORE_ONLY' }} }}];")
    xq = w.call("EXECUTE_05_QUALIFICATION", "05")
    r_q = w.respond("RESPOND_QUALIFY", J("{ ok: true, qualification: $json }"))

    # get_available_slots ------------------------------------------------
    ps = w.code("PREP_SLOTS", f"""const a = {N}.args;
return [{{ json: {{ lead_id: {N}.lead_id,
  preferred_date: /^\\d{{4}}-\\d{{2}}-\\d{{2}}$/.test(a.date || '') ? a.date : null,
  preferred_time: /^\\d{{2}}:\\d{{2}}$/.test(a.preferred_time || '') ? a.preferred_time : null }} }}];""")
    xs = w.call("EXECUTE_09_AVAILABILITY", "09")
    r_s = w.respond("RESPOND_SLOTS", J("{ ok: true, available: $json.available, slots: $json.available_slots, timezone: $json.timezone }"))

    # book / reschedule --------------------------------------------------
    pb = w.code("PREP_BOOK", f"""return [{{ json: {{ lead_id: {N}.lead_id, conversation_id: {N}.conversation_id,
  slot: {N}.args.slot, reschedule: {N}.tool === 'reschedule_appointment', channel: 'voice', source: 'voice' }} }}];""")
    xb = w.call("EXECUTE_10_BOOK_APPOINTMENT", "10")
    r_b = w.respond("RESPOND_BOOKING", J("{ ok: true, ...$json }"))

    # send_sms_confirmation ----------------------------------------------
    sc = w.sql("GET_SMS_CONTEXT", """
SELECT l.id AS lead_id, l.name, o.name AS org_name, o.timezone,
       (SELECT a.start_at FROM appointments a WHERE a.lead_id = l.id AND a.status IN ('SCHEDULED','CONFIRMED')
          AND a.start_at > NOW() ORDER BY a.start_at LIMIT 1) AS next_appointment
FROM leads l JOIN organizations o ON o.id = l.organization_id WHERE l.id = $1::uuid;""", [f"{N}.lead_id"])
    pc = w.code("PREP_SMS", r"""const r = $json;
const when = r.next_appointment ? DateTime.fromJSDate(new Date(r.next_appointment)).setZone(r.timezone).toFormat("cccc, LLL d 'at' h:mm a") : null;
const body = when
  ? `Thanks for calling ${r.org_name}! Your roof inspection is on ${when}. Reply here if you have any questions.`
  : `Thanks for calling ${r.org_name}! Reply to this text anytime and we'll help you get your roof sorted.`;
return [{ json: { lead_id: r.lead_id, channel: 'sms', body, kind: 'VOICE_FOLLOWUP_SMS', ai_generated: false } }];""")
    xc = w.call("EXECUTE_07_SEND_SMS", "07")
    r_c = w.respond("RESPOND_SMS", J("{ ok: true, sent: $json.sent === true }"))

    # human_handoff ------------------------------------------------------
    ph = w.code("PREP_HANDOFF", f"""const a = {N}.args;
return [{{ json: {{ lead_id: {N}.lead_id, conversation_id: {N}.conversation_id, channel: 'voice',
  reason: a.reason || 'Caller asked for a person', priority: a.emergency ? 'EMERGENCY' : 'NORMAL', notify_customer: false }} }}];""")
    xh = w.call("EXECUTE_15_HUMAN_HANDOFF", "15")
    r_h = w.respond("RESPOND_HANDOFF", J("{ ok: true, transfer: !!$json.handoff_phone, handoff_phone: $json.handoff_phone, priority: $json.priority }"))

    # opt_out ------------------------------------------------------------
    po = w.code("PREP_OPT_OUT", f"return [{{ json: {{ lead_id: {N}.lead_id, channel: 'voice', body: 'voice opt-out', action: 'OPT_OUT', source: 'voice' }} }}];")
    xo = w.call("EXECUTE_21_CONSENT", "21")
    r_o = w.respond("RESPOND_OPT_OUT", J("{ ok: true, opted_out: true }"))

    r_bad = w.respond("RESPOND_INVALID_TOOL", J(f"{{ ok: false, error: 'unknown tool: ' + {N}.requested_tool }}"), code=400)

    w.chain(hook, norm, sw)
    w.connect(sw, fl, 0); w.connect(fl, r_fl)
    w.connect(sw, ql, 1); w.chain(ql, pq, xq, r_q)
    w.connect(sw, ps, 2); w.chain(ps, xs, r_s)
    w.connect(sw, pb, 3); w.connect(sw, pb, 4); w.chain(pb, xb, r_b)
    w.connect(sw, sc, 5); w.chain(sc, pc, xc, r_c)
    w.connect(sw, ph, 6); w.chain(ph, xh, r_h)
    w.connect(sw, po, 7); w.chain(po, xo, r_o)
    w.connect(sw, r_bad, 8)
    w.note("""## 24A · Voice Tool Gateway
The voice server calls `POST /webhook/roofradar/voice-tool` with header `X-RoofRadar-Key` and
`{ tool, args, call: { call_sid, from, to, lead_id, conversation_id } }`.
The voice AI never touches the database or calendar directly — every tool reuses workflows 05/07/09/10/15/21,
so voice gets the same validation, consent, idempotency and audit trail as SMS.""", width=620)
    return w


def wf24b():
    w = WF("24B", "RR 24B · Voice Post-Call",
           "Call ended → transcript → AI summary → update lead/session → CRM → text follow-up if needed")
    hook = w.webhook("CALL_ENDED_WEBHOOK", "roofradar/voice-call-ended", auth="header")
    sess = w.sql("SAVE_TRANSCRIPT", """
WITH t AS (
  SELECT string_agg(speaker || ': ' || text, E'\\n' ORDER BY created_at) AS transcript
  FROM voice_turns WHERE session_id = (SELECT id FROM voice_sessions WHERE call_sid = $1::text)
)
UPDATE voice_sessions s SET
  transcript = (SELECT transcript FROM t),
  ended_at = COALESCE(s.ended_at, NOW()),
  duration_seconds = COALESCE($2::int, EXTRACT(epoch FROM (COALESCE(s.ended_at, NOW()) - s.started_at))::int),
  status = CASE WHEN s.status = 'IN_PROGRESS' THEN 'COMPLETED' ELSE s.status END
WHERE s.call_sid = $1::text
RETURNING s.*, (SELECT name FROM organizations WHERE id = s.organization_id) AS org_name,
          (SELECT settings FROM organizations WHERE id = s.organization_id) AS org_settings;""",
                ["$json.body.call_sid", "$json.body.duration_seconds ?? null"])
    has = w.if_("HAS_TRANSCRIPT?", "!!$json.transcript && !!$json.lead_id")
    prep = w.code("PREP_SUMMARY", FILL_JS + f"const P = {prompt_js(prompts.VOICE_SUMMARY)};\n" + r"""
const s = $json;
return [{ json: { openai_request: buildOpenAI(s.org_settings?.ai?.models?.summary || 'gpt-5.4-mini',
  fill(P, { company: s.org_name }), { transcript: String(s.transcript).slice(-24000) }, 1500) } }];""")
    ai = w.openai("AI_SUMMARY")
    parse = w.code("PARSE_JSON", PARSE_JS)
    val = w.code("VALIDATE_SUMMARY", r"""
const o = $json.parsed || {};
const s = $('SAVE_TRANSCRIPT').first().json;
const pick = (v, l, d) => l.includes(v) ? v : d;
// never trust the model on booking: check the transcript for a tool result
const toolBooked = /TOOL: .*"booked":\s*true/.test(s.transcript || '');
const toolHandoff = /TOOL: .*"handoff":\s*true|TOOL: .*"transfer":\s*true/.test(s.transcript || '');
const outcome = toolBooked ? 'BOOKED' : toolHandoff ? 'HANDOFF'
  : pick(String(o.outcome || '').toUpperCase(), ['QUALIFIED','INFO','ABANDONED','OPTED_OUT'], 'INFO');
return [{ json: {
  summary: typeof o.summary === 'string' ? o.summary.slice(0, 600) : null,
  problem_type: typeof o.problem_type === 'string' ? o.problem_type.slice(0, 120) : null,
  urgency: pick(String(o.urgency || '').toUpperCase(), ['LOW','MEDIUM','HIGH','EMERGENCY'], null),
  caller_name: typeof o.caller_name === 'string' ? o.caller_name.slice(0, 120) : null,
  address: typeof o.address === 'string' ? o.address.slice(0, 200) : null,
  appointment_booked: toolBooked, human_handoff: toolHandoff,
  followup_required: !toolBooked && !toolHandoff && outcome !== 'OPTED_OUT' && o.followup_required !== false,
  followup_reason: o.followup_reason || null, outcome
}}];""")
    S = "$('SAVE_TRANSCRIPT').first().json"
    V = "$('VALIDATE_SUMMARY').first().json"
    upd_s = w.sql("UPDATE_SESSION", "UPDATE voice_sessions SET summary = $2::text, outcome = $3::text WHERE id = $1::uuid RETURNING id;",
                  [f"{S}.id", f"{V}.summary", f"{V}.outcome"])
    upd_l = w.sql("UPDATE_LEAD", """
UPDATE leads SET
  name = COALESCE(name, NULLIF($2::text,'')), address = COALESCE(address, NULLIF($3::text,'')),
  problem_type = COALESCE(problem_type, NULLIF($4::text,'')), urgency = COALESCE(NULLIF($5::text,''), urgency),
  status = CASE WHEN status IN ('NEW','CONTACTED') THEN 'ENGAGED' ELSE status END,
  last_inbound_at = NOW(), last_message_at = NOW(),
  metadata = metadata || jsonb_build_object('last_call_summary', $6::text)
WHERE id = $1::uuid
RETURNING id, organization_id, status, human_handoff;""",
                  [f"{S}.lead_id", f"{V}.caller_name || ''", f"{V}.address || ''", f"{V}.problem_type || ''",
                   f"{V}.urgency || ''", f"{V}.summary || ''"])
    upd_a = w.sql("UPDATE_APPOINTMENT", """
UPDATE appointments SET notes = coalesce(notes, '') || E'\\nCall summary: ' || $2::text
WHERE id = (SELECT id FROM appointments WHERE lead_id = $1::uuid AND status IN ('SCHEDULED','CONFIRMED')
            AND created_at > NOW() - INTERVAL '2 hours' ORDER BY created_at DESC LIMIT 1)
RETURNING id;""", [f"{S}.lead_id", f"{V}.summary || ''"], always=True)
    pcrm = w.code("PREP_CRM", f"return [{{ json: {{ lead_id: {S}.lead_id }} }}];")
    crm = w.call("UPDATE_CRM", "12", wait=False)
    need = w.if_("SEND_SMS_IF_NEEDED", f"{V}.followup_required")
    psms = w.code("PREP_POST_CALL_SMS", f"""return [{{ json: {{ lead_id: {S}.lead_id, organization_id: {S}.organization_id,
  channel: 'sms', mode: 'POST_CALL', router_reason: {V}.followup_reason || 'we will follow up about your roof' }} }}];""")
    x06 = w.call("EXECUTE_06_AI_CONVERSATION", "06", onError="continueRegularOutput")
    nur = w.sql("SCHEDULE_NURTURE", "SELECT rr_schedule_nurture($1::uuid, NOW(), 2) AS scheduled;", [f"{S}.lead_id"])
    act = w.sql("LOG_ACTIVITY", """
INSERT INTO activities (organization_id, lead_id, type, description, metadata)
VALUES ($1::uuid, $2::uuid, 'VOICE_CALL', $3::text, $4::jsonb) RETURNING id;""",
                [f"{S}.organization_id", f"{S}.lead_id", f"'Voice call — ' + {V}.outcome",
                 f"JSON.stringify({{call_sid: {S}.call_sid, duration: {S}.duration_seconds, summary: {V}.summary}})"])
    w.chain(hook, sess, has)
    w.connect(has, prep, 0)
    w.chain(prep, ai, parse, val, upd_s, upd_l, upd_a, pcrm, crm, need)
    w.connect(need, psms, 0)
    w.chain(psms, x06, nur, act)
    w.connect(need, act, 1)
    return w


def wf24c():
    w = WF("24C", "RR 24C · Voice Transfer Result",
           "<Dial action> callback after a warm transfer: record DialCallStatus; if nobody answered, priority callback")
    hook = w.webhook("TRANSFER_RESULT_WEBHOOK", "roofradar/voice-transfer-result", respond="responseNode", auth="basic")
    norm = w.code("NORMALIZE", r"""
const b = $json.body || {};
const status = String(b.DialCallStatus || 'failed').toLowerCase();
return [{ json: { call_sid: b.CallSid, status, answered: status === 'completed' } }];""")
    resp = w.respond("RESPOND_TWIML",
                     "={{ $json.answered ? '<Response><Hangup/></Response>' : "
                     "'<Response><Say>Sorry, our team could not pick up right now. We have flagged your call as a priority and someone will call you back shortly. Goodbye.</Say><Hangup/></Response>' }}",
                     content_type="text/xml")
    upd = w.sql("UPDATE_HANDOFF", """
WITH s AS (
  UPDATE voice_sessions SET status = CASE WHEN $2::text = 'completed' THEN 'TRANSFERRED' ELSE status END,
         metadata = metadata || jsonb_build_object('transfer_status', $2::text)
  WHERE call_sid = $1::text RETURNING lead_id, organization_id
), h AS (
  UPDATE human_handoffs SET transfer_status = $2::text
  WHERE id = (SELECT id FROM human_handoffs WHERE lead_id = (SELECT lead_id FROM s) AND status = 'OPEN'
              ORDER BY created_at DESC LIMIT 1)
  RETURNING id
)
SELECT l.name, l.phone, l.problem_type, o.settings->>'slack_channel' AS slack_channel, (SELECT count(*) FROM h) AS updated
FROM s JOIN leads l ON l.id = s.lead_id JOIN organizations o ON o.id = s.organization_id;""",
                ["$('NORMALIZE').first().json.call_sid", "$('NORMALIZE').first().json.status"], always=True)
    failed = w.if_("TRANSFER_FAILED?", "!$('NORMALIZE').first().json.answered && !!$json.slack_channel")
    slack = w.slack("ALERT_PRIORITY_CALLBACK", "$json.slack_channel",
                    "'📞 PRIORITY CALLBACK — transfer ' + $('NORMALIZE').first().json.status + '\\n' + ($json.name || 'Unknown caller') + ' · ' + $json.phone + '\\n' + ($json.problem_type || '')")
    w.chain(hook, norm, resp, upd, failed)
    w.connect(failed, slack, 0)
    return w

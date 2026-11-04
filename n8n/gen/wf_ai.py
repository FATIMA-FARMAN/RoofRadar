"""Workflows 04–07 — AI router, qualification, conversation, outbound send."""
from lib import WF
from common import FILL_JS, PARSE_JS, LEAD_CTX_SQL, SAVE_AI_SQL, save_ai_args, prompt_js
import prompts

T = "$('EXECUTE_WORKFLOW_TRIGGER').first().json"


def ctx_nodes(w, channel_expr=f"{T}.channel", history=12):
    """GET_LEAD → GET_CONVERSATION? → GET_MESSAGES → GET_COMPANY_RULES (shared read pattern)."""
    a = w.sql("GET_LEAD", LEAD_CTX_SQL, [f"{T}.lead_id", f"{channel_expr} || 'sms'"])
    b = w.sql("GET_MESSAGES", f"SELECT rr_recent_messages($1::uuid, {history}) AS history;", [f"{T}.lead_id"])
    c = w.sql("GET_COMPANY_RULES",
              "SELECT id, name, timezone, settings FROM organizations WHERE id = $1::uuid;",
              ["$('GET_LEAD').first().json.lead.organization_id"])
    return a, b, c


# =====================================================================
def wf04():
    w = WF("04", "RR 04 · AI Router",
           "Decides the next action (ANSWER/QUALIFY/BOOK/RESCHEDULE/HUMAN/STOP/EMERGENCY); n8n executes it deterministically")
    trig = w.sub_trigger()
    lead = w.sql("GET_LEAD", LEAD_CTX_SQL, [f"{T}.lead_id", f"{T}.channel || 'sms'"])
    conv = w.sql("GET_CONVERSATION", "SELECT * FROM conversations WHERE id = $1::uuid;", [f"{T}.conversation_id"])
    msgs = w.sql("GET_RECENT_MESSAGES", "SELECT rr_recent_messages($1::uuid, 12) AS history;", [f"{T}.lead_id"])
    rules = w.sql("GET_COMPANY_RULES", "SELECT id, name, timezone, settings FROM organizations WHERE id = $1::uuid;",
                  ["$('GET_LEAD').first().json.lead.organization_id"])
    allowed = w.if_("AI_ALLOWED?",
                    "$('GET_CONVERSATION').first().json.ai_active && !$('GET_LEAD').first().json.lead.human_handoff "
                    "&& $('GET_LEAD').first().json.lead.status !== 'STOPPED'")
    paused = w.sql("LOG_AI_PAUSED",
                   "SELECT rr_log_event($1::uuid, $2::uuid, '04_AI_ROUTER', 'AI_PAUSED_SKIP', '{}'::jsonb) AS id;",
                   ["$('GET_COMPANY_RULES').first().json.id", f"{T}.lead_id"])
    prep = w.code("PREPARE_ROUTER_INPUT", FILL_JS + f"const ROUTER = {prompt_js(prompts.ROUTER)};\n" + r"""
const t = $('EXECUTE_WORKFLOW_TRIGGER').first().json;
const ctx = $('GET_LEAD').first().json;
const org = $('GET_COMPANY_RULES').first().json;
const s = org.settings || {};
const lead = ctx.lead;
const tz = org.timezone || 'America/New_York';
const fmt = iso => DateTime.fromISO(String(iso)).setZone(tz).toFormat("ccc LLL d, h:mm a");
const offered = (lead.metadata && lead.metadata.offered_slots) || [];
const user = {
  now_local: DateTime.now().setZone(tz).toFormat("cccc yyyy-LL-dd HH:mm ZZZZ"),
  company: {
    name: org.name, services: s.services, service_area: s.service_area?.description,
    faq: s.faq || {}, emergency_criteria: s.emergency_criteria || []
  },
  lead: {
    name: lead.name, status: lead.status, qualification_status: lead.qualification_status,
    problem_type: lead.problem_type, urgency: lead.urgency, lead_score: lead.lead_score,
    upcoming_appointment: ctx.upcoming_appointment ? { start_local: fmt(ctx.upcoming_appointment.start_at) } : null
  },
  offered_slots: offered.map(o => ({ iso: o.iso, label: o.label })),
  history: $('GET_RECENT_MESSAGES').first().json.history || [],
  latest_message: t.body
};
const model = s.ai?.models?.router || 'gpt-5.4-mini';
return [{ json: { openai_request: buildOpenAI(model, fill(ROUTER, { company: org.name }), user, 1500) } }];""")
    ai = w.openai("OPENAI_ROUTER")
    parse = w.code("PARSE_JSON", PARSE_JS)
    val = w.code("VALIDATE_ROUTER_OUTPUT", r"""
const p = $('PARSE_JSON').first().json;
const ctx = $('GET_LEAD').first().json;
const s = $('GET_COMPANY_RULES').first().json.settings || {};
const o = p.parsed || {};
const allowed = ['ANSWER','QUALIFY','BOOK','RESCHEDULE','HUMAN','STOP','EMERGENCY'];
const errors = [];
if (!p.parsed) errors.push('unparseable router output: ' + p.parse_error);
let action = String(o.action || '').toUpperCase();
if (!allowed.includes(action)) errors.push('invalid action: ' + o.action);
let confidence = Number(o.confidence);
if (typeof o.confidence !== 'number' || confidence < 0 || confidence > 1) { errors.push('invalid confidence'); confidence = 0; }
const valid = errors.length === 0;
let overridden = null;
if (!valid) { action = 'HUMAN'; overridden = 'invalid_router_output'; }

// business rules (deterministic, cannot be talked around)
const minConf = s.ai?.min_router_confidence ?? 0.6;
if (valid && action !== 'STOP' && confidence < minConf) { overridden = 'low_confidence'; action = 'HUMAN'; }
if (action === 'EMERGENCY' && !(s.emergency_criteria || []).includes(o.emergency_criteria_matched)) {
  overridden = 'emergency_without_configured_criterion'; action = 'HUMAN';
}
if (action === 'RESCHEDULE' && !ctx.upcoming_appointment) action = 'BOOK';
const sentiment = ['POSITIVE','NEUTRAL','NEGATIVE','ANGRY'].includes(String(o.sentiment || '').toUpperCase())
  ? String(o.sentiment).toUpperCase() : 'NEUTRAL';
// an upset customer always gets a person (STOP and EMERGENCY keep their own handling)
if (sentiment === 'ANGRY' && !['STOP','EMERGENCY','HUMAN'].includes(action)) { overridden = 'upset_customer'; action = 'HUMAN'; }

// a slot is only accepted if WE offered it, recently, and it is still in the future
const md = ctx.lead.metadata || {};
const offered = md.offered_slots || [];
const offeredAt = md.offered_at ? new Date(md.offered_at).getTime() : 0;
let selected_slot = null;
if (o.selected_slot) {
  const ok = offered.some(x => x.iso === o.selected_slot)
          && (Date.now() - offeredAt) < 72 * 3600e3
          && new Date(o.selected_slot).getTime() > Date.now();
  if (ok) selected_slot = o.selected_slot;
  else errors.push('selected_slot rejected (not in current offered list)');
}
const preferred_date = /^\d{4}-\d{2}-\d{2}$/.test(o.preferred_date || '') ? o.preferred_date : null;
const preferred_time = /^\d{2}:\d{2}$/.test(o.preferred_time || '') ? o.preferred_time : null;

return [{ json: {
  action, model_action: o.action || null, confidence, overridden, sentiment,
  reason: o.reason || null,
  selected_slot, preferred_date, preferred_time,
  emergency_criteria_matched: action === 'EMERGENCY' ? o.emergency_criteria_matched : null,
  handoff_reason: o.handoff_reason || overridden || o.reason || null,
  valid, errors
}}];""")
    V = "$('VALIDATE_ROUTER_OUTPUT').first().json"
    save = w.sql("SAVE_AI_INTERACTION", SAVE_AI_SQL,
                 save_ai_args("GET_LEAD", "04_AI_ROUTER", "PARSE_JSON", "VALIDATE_ROUTER_OUTPUT",
                              f"{V}.action", f"{V}.confidence", "PREPARE_ROUTER_INPUT", f"{T}.conversation_id"))
    sw = w.switch("SWITCH_ACTION", f"{V}.action",
                  ["ANSWER", "QUALIFY", "BOOK", "RESCHEDULE", "HUMAN", "STOP", "EMERGENCY"])

    base = """const t = $('EXECUTE_WORKFLOW_TRIGGER').first().json;
const v = $('VALIDATE_ROUTER_OUTPUT').first().json;
const base = { lead_id: t.lead_id, conversation_id: t.conversation_id, organization_id: t.organization_id, channel: t.channel };
"""
    p_ans = w.code("PREP_ANSWER", base + "return [{ json: { ...base, mode: 'ANSWER', router_reason: v.reason } }];")
    p_qual = w.code("PREP_QUALIFY", base + "return [{ json: { ...base, mode: 'ROUTE' } }];")
    x06a = w.call("EXECUTE_06_ANSWER", "06")
    x05 = w.call("EXECUTE_05_QUALIFICATION", "05")

    p_book = w.code("PREP_BOOKING", base + """return [{ json: { ...base, mode: 'SCORE_ONLY',
  reschedule: v.action === 'RESCHEDULE', selected_slot: v.selected_slot,
  preferred_date: v.preferred_date, preferred_time: v.preferred_time } }];""")
    score_bg = w.call("SCORE_LEAD_IN_BACKGROUND", "05", wait=False)
    has_slot = w.if_("HAS_VALID_SLOT?", "!!$json.selected_slot")
    p10 = w.code("PREP_BOOK", "return [{ json: { lead_id: $json.lead_id, conversation_id: $json.conversation_id, channel: $json.channel, slot: $json.selected_slot, reschedule: $json.reschedule, source: 'router' } }];")
    x10 = w.call("EXECUTE_10_BOOK_APPOINTMENT", "10")
    booked = w.if_("BOOKED?", "$json.booked === true")
    p_alts = w.code("PREP_OFFER_ALTERNATIVES", base + """const alts = $json.alternatives || [];
return [{ json: { ...base, mode: alts.length ? 'OFFER_SLOTS' : 'NO_SLOTS', slots: alts } }];""")
    p09 = w.code("PREP_AVAILABILITY", "return [{ json: { lead_id: $json.lead_id, preferred_date: $json.preferred_date, preferred_time: $json.preferred_time } }];")
    x09 = w.call("EXECUTE_09_AVAILABILITY", "09")
    p_offer = w.code("PREP_OFFER_SLOTS", base + """const slots = $json.available_slots || [];
return [{ json: { ...base, mode: slots.length ? 'OFFER_SLOTS' : 'NO_SLOTS', slots } }];""")
    x06o = w.call("EXECUTE_06_OFFER_SLOTS", "06")

    p_h = w.code("PREP_HUMAN", base + """return [{ json: { ...base, reason: v.handoff_reason || 'Customer needs a person', priority: 'NORMAL', notify_customer: true } }];""")
    p_e = w.code("PREP_EMERGENCY", base + """return [{ json: { ...base, reason: 'EMERGENCY: ' + v.emergency_criteria_matched, priority: 'EMERGENCY', notify_customer: true } }];""")
    x15 = w.call("EXECUTE_15_HUMAN_HANDOFF", "15")
    p_s = w.code("PREP_STOP", base + """return [{ json: { lead_id: t.lead_id, organization_id: t.organization_id, channel: t.channel, body: t.body, action: 'OPT_OUT', source: 'ai_router' } }];""")
    x21 = w.call("EXECUTE_21_CONSENT", "21")

    w.chain(trig, lead, conv, msgs, rules, allowed)
    w.connect(allowed, prep, 0)
    w.connect(allowed, paused, 1)
    w.chain(prep, ai, parse, val, save, sw)
    w.connect(sw, p_ans, 0); w.connect(p_ans, x06a)
    w.connect(sw, p_qual, 1); w.connect(p_qual, x05)
    w.connect(sw, p_book, 2); w.connect(sw, p_book, 3)
    w.connect(p_book, score_bg); w.connect(p_book, has_slot)
    w.connect(has_slot, p10, 0); w.connect(p10, x10); w.connect(x10, booked)
    w.connect(booked, p_alts, 1); w.connect(p_alts, x06o)
    w.connect(has_slot, p09, 1); w.connect(p09, x09); w.connect(x09, p_offer); w.connect(p_offer, x06o)
    w.connect(sw, p_h, 4); w.connect(sw, p_h, 7)
    w.connect(sw, p_s, 5); w.connect(p_s, x21)
    w.connect(sw, p_e, 6)
    w.connect(p_h, x15); w.connect(p_e, x15)
    w.note("""## 04 · AI Router — *AI decides, n8n executes*
The model only returns a JSON decision. `VALIDATE_ROUTER_OUTPUT` enforces:
allowed actions · confidence 0–1 · low confidence → HUMAN · EMERGENCY only with a configured criterion ·
a slot is accepted only if we offered it in the last 72h.
Every decision is stored in `ai_interactions`.""", width=600)
    return w


# =====================================================================
def wf05():
    w = WF("05", "RR 05 · AI Qualification",
           "LLM extracts facts → schema + business validation → deterministic SQL score → update lead → route")
    trig = w.sub_trigger()
    lead, msgs, rules = ctx_nodes(w, history=20)
    prep = w.code("PREPARE_QUALIFICATION", FILL_JS + f"const QUALIFIER = {prompt_js(prompts.QUALIFIER)};\n" + r"""
const ctx = $('GET_LEAD').first().json;
const org = $('GET_COMPANY_RULES').first().json;
const s = org.settings || {};
const lead = ctx.lead;
const system = fill(QUALIFIER, {
  company: org.name,
  service_area: s.service_area?.description || (s.service_area?.states || []).join(', '),
  services: (s.services || []).join(', ')
});
const user = {
  known_lead_fields: { name: lead.name, email: lead.email, address: lead.address, problem_type: lead.problem_type, source: lead.source },
  first_message: lead.first_message,
  conversation: $('GET_MESSAGES').first().json.history || []
};
return [{ json: { openai_request: buildOpenAI(s.ai?.models?.qualifier || 'gpt-5.4-mini', system, user, 2000) } }];""")
    ai = w.openai("OPENAI_QUALIFIER")
    parse = w.code("PARSE_JSON", PARSE_JS)
    val = w.code("VALIDATE", r"""
const p = $('PARSE_JSON').first().json;
const o = p.parsed || {};
const ctx = $('GET_LEAD').first().json;
const lead = ctx.lead;
const s = $('GET_COMPANY_RULES').first().json.settings || {};
const errors = [];
if (!p.parsed) errors.push('unparseable qualifier output: ' + p.parse_error);
const pick = (v, list, d) => list.includes(v) ? v : d;
const up = v => String(v ?? '').toUpperCase();
const bool = v => v === true ? true : v === false ? false : null;
const str = v => (typeof v === 'string' && v.trim() && v.trim().toLowerCase() !== 'null') ? v.trim().slice(0, 200) : null;

const urgency = pick(up(o.urgency), ['LOW','MEDIUM','HIGH','EMERGENCY','UNKNOWN'], 'UNKNOWN');
const timeline = pick(up(o.timeline), ['ASAP','THIS_WEEK','THIS_MONTH','FLEXIBLE','UNKNOWN'], 'UNKNOWN');
const service_type = pick(String(o.service_type || '').toLowerCase(), ['inspection','repair','replacement','other','unknown'], 'unknown');
const property_type = pick(String(o.property_type || '').toLowerCase(), ['single_family','multi_family','commercial','unknown'], 'unknown');
const budget_signal = pick(String(o.budget_signal || '').toLowerCase(), ['insurance_claim','price_sensitive','ready_to_spend','unknown'], 'unknown');
let qs = pick(up(o.qualification_status), ['QUALIFIED','PARTIAL','UNQUALIFIED','DISQUALIFIED'], 'PARTIAL');
let rec = pick(up(o.recommended_action), ['BOOK','QUALIFY','ANSWER','HUMAN'], 'QUALIFY');
let confidence = Number(o.confidence); if (!(confidence >= 0 && confidence <= 1)) { confidence = 0; errors.push('invalid confidence'); }

// service area is decided by config, not by the model
const states = (s.service_area?.states || []).map(x => x.toUpperCase());
const st = /^[A-Za-z]{2}$/.test(o.address_state || '') ? o.address_state.toUpperCase() : null;
const in_service_area = st ? states.includes(st) : null;
if (in_service_area === false) qs = 'DISQUALIFIED';
else if (qs === 'DISQUALIFIED' && !(service_type === 'other' && confidence >= 0.8)) qs = 'PARTIAL';

const emailRaw = str(o.email);
const email = emailRaw && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(emailRaw) ? emailRaw.toLowerCase() : null;
const active_problem = o.active_problem === true;
const appointment_intent = o.appointment_intent === true;
const valid_contact = !!(lead.phone || lead.email || email);
const missing = Array.isArray(o.missing_fields) ? o.missing_fields.filter(x => typeof x === 'string').slice(0, 5) : [];

let next = 'QUALIFY';
if (qs === 'DISQUALIFIED') next = 'DISQUALIFIED';
else if (rec === 'HUMAN') next = 'HUMAN';
else if (rec === 'BOOK' || (appointment_intent && active_problem) || (qs === 'QUALIFIED' && missing.length === 0)) next = 'BOOK';

return [{ json: {
  name: str(o.name), email, address: str(o.address), address_state: st, in_service_area,
  service_type, problem_type: str(o.problem_type), active_problem, urgency, timeline, property_type, budget_signal,
  is_decision_maker: bool(o.is_decision_maker), appointment_intent, valid_contact,
  qualification_status: qs, recommended_action: rec, next, missing_fields: missing,
  confidence, evidence: Array.isArray(o.evidence) ? o.evidence.slice(0, 6) : [],
  valid: errors.length === 0, errors
}}];""")
    V = "$('VALIDATE').first().json"
    score = w.sql("CALCULATE_SCORE",
                  "SELECT rr_score_lead($1::boolean, $2::text, $3::boolean, $4::boolean, $5::boolean, $6::boolean, $7::text) AS lead_score;",
                  [f"{V}.active_problem", f"{V}.urgency", f"{V}.is_decision_maker === true",
                   f"{V}.in_service_area === true", f"{V}.appointment_intent", f"{V}.valid_contact", f"{V}.timeline"])
    upd = w.sql("UPDATE_LEAD", """
WITH u AS (
  UPDATE leads SET
    name                 = COALESCE(name, $2::text),
    email                = COALESCE(email, $3::text),
    address              = COALESCE(address, $4::text),   -- never overwrite an address the customer gave us
    service_type         = $5::text,
    problem_type         = COALESCE($6::text, problem_type),
    urgency              = $7::text,
    timeline             = $8::text,
    property_type        = $9::text,
    budget_signal        = $10::text,
    is_decision_maker    = $11::boolean,
    in_service_area      = $12::boolean,
    qualification_status = $13::text,
    lead_score           = $14::int,
    status = CASE
      WHEN status IN ('BOOKED','HUMAN_HANDOFF','STOPPED','WON','LOST','QUOTED') THEN status
      WHEN $13::text = 'DISQUALIFIED' THEN 'DISQUALIFIED'
      WHEN $13::text = 'QUALIFIED'    THEN 'QUALIFIED'
      ELSE status END
  WHERE id = $1::uuid
  RETURNING *
)
SELECT u.*, CASE WHEN u.status = 'DISQUALIFIED' THEN rr_cancel_followups(u.id, 'DISQUALIFIED') ELSE 0 END AS cancelled_followups
FROM u;""", ["$('GET_LEAD').first().json.lead.id", f"{V}.name", f"{V}.email", f"{V}.address", f"{V}.service_type",
             f"{V}.problem_type", f"{V}.urgency", f"{V}.timeline", f"{V}.property_type", f"{V}.budget_signal",
             f"{V}.is_decision_maker", f"{V}.in_service_area", f"{V}.qualification_status",
             "$('CALCULATE_SCORE').first().json.lead_score"])
    save = w.sql("SAVE_AI_INTERACTION", SAVE_AI_SQL,
                 save_ai_args("GET_LEAD", "05_AI_QUALIFICATION", "PARSE_JSON", "VALIDATE",
                              f"{V}.next", f"{V}.confidence", "PREPARE_QUALIFICATION", f"{T}.conversation_id"))
    log = w.sql("LOG_EVENT", "SELECT rr_log_event($1::uuid, $2::uuid, '05_AI_QUALIFICATION', 'LEAD_QUALIFIED', $3::jsonb) AS id;",
                ["$('GET_LEAD').first().json.lead.organization_id", "$('GET_LEAD').first().json.lead.id",
                 f"JSON.stringify({{score: $('CALCULATE_SCORE').first().json.lead_score, status: {V}.qualification_status, next: {V}.next}})"])
    route = w.if_("ROUTE?", f"{T}.mode === 'ROUTE' && ['sms','whatsapp'].includes({T}.channel)")
    result = w.code("RETURN_RESULT", """const v = $('VALIDATE').first().json;
return [{ json: {
  lead_id: $('GET_LEAD').first().json.lead.id,
  lead_score: $('CALCULATE_SCORE').first().json.lead_score,
  qualification_status: v.qualification_status, urgency: v.urgency, problem_type: v.problem_type,
  missing_fields: v.missing_fields, recommended_action: v.next, in_service_area: v.in_service_area
}}];""")
    nxt = w.switch("NEXT_STEP", f"{V}.next", ["BOOK", "HUMAN", "DISQUALIFIED", "QUALIFY"])
    base = """const t = $('EXECUTE_WORKFLOW_TRIGGER').first().json;
const v = $('VALIDATE').first().json;
const base = { lead_id: t.lead_id, conversation_id: t.conversation_id, organization_id: t.organization_id, channel: t.channel };
"""
    p09 = w.code("PREP_AVAILABILITY", base + "return [{ json: { lead_id: t.lead_id } }];")
    x09 = w.call("EXECUTE_09_AVAILABILITY", "09")
    p_off = w.code("PREP_OFFER_SLOTS", base + """const slots = $json.available_slots || [];
return [{ json: { ...base, mode: slots.length ? 'OFFER_SLOTS' : 'NO_SLOTS', slots } }];""")
    p15 = w.code("PREP_HANDOFF", base + "return [{ json: { ...base, reason: 'Qualification recommends a person', priority: v.urgency === 'EMERGENCY' ? 'EMERGENCY' : 'NORMAL', notify_customer: true } }];")
    x15 = w.call("EXECUTE_15_HUMAN_HANDOFF", "15")
    p_dq = w.code("PREP_DISQUALIFIED", base + "return [{ json: { ...base, mode: 'DISQUALIFIED', router_reason: v.in_service_area === false ? 'property is outside our service area' : 'service not offered' } }];")
    p_q = w.code("PREP_ASK_MISSING", base + "return [{ json: { ...base, mode: 'QUALIFY', missing_fields: v.missing_fields } }];")
    x06 = w.call("EXECUTE_06_AI_CONVERSATION", "06")

    w.chain(trig, lead, msgs, rules, prep, ai, parse, val, score, upd, save, log, route)
    w.connect(route, nxt, 0)
    w.connect(route, result, 1)
    w.connect(nxt, p09, 0); w.connect(p09, x09); w.connect(x09, p_off); w.connect(p_off, x06)
    w.connect(nxt, p15, 1); w.connect(p15, x15)
    w.connect(nxt, p_dq, 2); w.connect(p_dq, x06)
    w.connect(nxt, p_q, 3); w.connect(nxt, p_q, 4); w.connect(p_q, x06)
    w.note("""## 05 · Qualification
The LLM **extracts facts only**. Service-area and the 0–100 score are deterministic
(`rr_score_lead()` in SQL: active problem 20 · urgent 20 · decision maker 15 · service area 15 ·
appointment intent 10 · valid contact 10 · timeline 10).
`mode=ROUTE` (SMS/WhatsApp) continues to booking/handoff/next question; `SCORE_ONLY`/voice just returns the result.""", width=620)
    return w


# =====================================================================
def wf06():
    w = WF("06", "RR 06 · AI Conversation",
           "Writes the customer message for a given mode; guardrails + safe fallbacks; deterministic slot list; → 07")
    trig = w.sub_trigger()
    lead, msgs, rules = ctx_nodes(w, channel_expr=f"({T}.channel === 'whatsapp' ? 'whatsapp' : 'sms')")
    can = w.if_("CAN_CONTACT?", "$('GET_LEAD').first().json.can_contact && !$('GET_LEAD').first().json.lead.human_handoff && !!$('GET_LEAD').first().json.lead.phone")
    skip = w.sql("LOG_SKIPPED",
                 "SELECT rr_log_event($1::uuid, $2::uuid, '06_AI_CONVERSATION', 'SKIPPED_NO_CONTACT', $3::jsonb) AS id, false AS sent, 'blocked_or_paused' AS reason, $2::uuid AS lead_id;",
                 ["$('GET_LEAD').first().json.lead.organization_id", f"{T}.lead_id", f"JSON.stringify({{mode: {T}.mode}})"])
    modes_js = "const MODES = " + __import__("json").dumps(prompts.CONVERSATION_MODES) + ";\n"
    prep = w.code("PREPARE_CONTEXT", FILL_JS + f"const CONV = {prompt_js(prompts.CONVERSATION)};\n" + modes_js + r"""
const t = $('EXECUTE_WORKFLOW_TRIGGER').first().json;
const ctx = $('GET_LEAD').first().json;
const org = $('GET_COMPANY_RULES').first().json;
const s = org.settings || {};
const lead = ctx.lead;
const mode = t.mode || 'ANSWER';
const channel = t.channel === 'whatsapp' ? 'WhatsApp' : 'SMS';
const system = fill(CONV, { company: org.name, channel, brand_voice: s.ai?.brand_voice || 'friendly, professional' });
// never ask again for what we already know (e.g. the address from the web form)
const known = {};
if (lead.name) known.name = lead.name;
if (lead.address) known.address = lead.address;
if (lead.problem_type) known.problem_type = lead.problem_type;
if (lead.email) known.email = lead.email;
const missing = (t.missing_fields || []).filter(f => !(
  (f === 'address' && lead.address) || (f === 'name' && lead.name) || (f === 'problem_type' && lead.problem_type)));
const instruction = fill(MODES[mode] || MODES.ANSWER, {
  missing: missing.join(', ') || (lead.address ? 'urgency and how soon they need help' : 'problem details and property address'),
  reason: t.router_reason || 'n/a', step: t.step || 1
});
const user = {
  task: instruction,
  mode,
  company_info: { name: org.name, services: s.services, service_area: s.service_area?.description, faq: s.faq || {} },
  customer: { first_name: (lead.name || '').split(' ')[0] || null, problem_type: lead.problem_type, urgency: lead.urgency, status: lead.status },
  already_known_do_not_ask_again: known,
  conversation: $('GET_MESSAGES').first().json.history || [],
  latest_customer_message: t.body || t.inbound_text || null
};
const model = (['FOLLOWUP','REENGAGE'].includes(mode) ? s.ai?.models?.followup : s.ai?.models?.conversation) || 'gpt-5.4';
return [{ json: { openai_request: buildOpenAI(model, system, user, 1200) } }];""")
    ai = w.openai("OPENAI_CONVERSATION")
    parse = w.code("PARSE_JSON", PARSE_JS)
    val = w.code("VALIDATE_RESPONSE", r"""
const t = $('EXECUTE_WORKFLOW_TRIGGER').first().json;
const p = $('PARSE_JSON').first().json;
const org = $('GET_COMPANY_RULES').first().json;
const s = org.settings || {};
const lead = $('GET_LEAD').first().json.lead;
const mode = t.mode || 'ANSWER';
const first = (lead.name || '').split(' ')[0];
const hi = first ? `Hi ${first}` : 'Hi';
const o = p.parsed || {};
let reply = typeof o.reply === 'string' ? o.reply.trim() : '';
const errors = [];
if (!p.parsed) errors.push('unparseable: ' + p.parse_error);
if (!reply) errors.push('empty reply');
const faqText = JSON.stringify(s.faq || {});
if (/\$\s?\d|\b\d[\d,]*\s?(dollars|usd)\b/i.test(reply) && !/\$\s?\d/.test(faqText)) errors.push('mentions a price');
if (/\b(you('| a)?re (all )?(set|booked|scheduled|confirmed) for|appointment (is |has been )?(booked|confirmed|scheduled))\b/i.test(reply)) errors.push('claims a booking');
if (/\b(climb|get|go) (up )?(on|onto) (the|your) roof\b/i.test(reply)) errors.push('unsafe instruction');
if (/\b(i am|i'm) (a )?(human|real person)\b/i.test(reply)) errors.push('claims to be human');
const SLOT_MODES = ['OFFER_SLOTS','NO_SHOW'];
if (SLOT_MODES.includes(mode) && /\b\d{1,2}(:\d{2})?\s?(am|pm)\b|\b(mon|tues|wednes|thurs|fri|satur|sun)day\b|\btomorrow\b/i.test(reply)) errors.push('mentions times itself');
if (reply.length > 600) errors.push('too long');
const valid = errors.length === 0;

const FALLBACK = {
  FIRST_CONTACT: `${hi}, thanks for contacting ${org.name}! Is water coming in right now, or is this a non-urgent issue?`,
  ANSWER: `Thanks for your message! A ${org.name} team member will follow up shortly.`,
  QUALIFY: `Thanks! Could you share a little more about the roof issue and the property address?`,
  OFFER_SLOTS: `Here are our next available free inspection times:`,
  NO_SLOTS: `Thanks! Our inspection calendar is full for that window — a team member will reach out to find a time.`,
  DISQUALIFIED: `Thanks for reaching out to ${org.name}. Unfortunately we're not able to help with this request.`,
  FOLLOWUP: `${hi}, just checking in from ${org.name} — would a free roof inspection help? Reply anytime.`,
  POST_CALL: `Thanks for calling ${org.name}! Reply to this text anytime if you have questions.`,
  NO_SHOW: `${hi}, sorry we missed you for your roof inspection, no problem at all. Here are the next free times:`,
  REENGAGE: `${hi}, it's ${org.name} checking in. Is your roof still giving you trouble? We're happy to do a free inspection whenever suits you.`
};
let body = valid ? reply : (FALLBACK[mode] || FALLBACK.ANSWER);

// availability is appended by the system — the model can never invent a time
const slots = (t.slots || []).slice(0, s.max_slots_offered || 3);
if (SLOT_MODES.includes(mode)) {
  body = slots.length
    ? body + '\n' + slots.map((x, i) => `${i + 1}) ${x.label}`).join('\n') + '\nReply with the number that works best.'
    : FALLBACK.NO_SLOTS;
}
if (['FIRST_CONTACT','REENGAGE'].includes(mode) && !/\bSTOP\b/.test(body)) body += ' Reply STOP to opt out.';
return [{ json: { reply: body, ai_reply: reply, valid, errors, mode, used_fallback: !valid,
                  next_action: o.next_action || 'WAIT_FOR_CUSTOMER' } }];""")
    V = "$('VALIDATE_RESPONSE').first().json"
    save = w.sql("SAVE_AI_INTERACTION", SAVE_AI_SQL,
                 save_ai_args("GET_LEAD", "06_AI_CONVERSATION", "PARSE_JSON", "VALIDATE_RESPONSE",
                              f"'REPLY_' + {V}.mode", "null", "PREPARE_CONTEXT", f"{T}.conversation_id"))
    psend = w.code("PREP_SEND", """const t = $('EXECUTE_WORKFLOW_TRIGGER').first().json;
const v = $('VALIDATE_RESPONSE').first().json;
return [{ json: {
  lead_id: t.lead_id,
  channel: t.channel === 'whatsapp' ? 'whatsapp' : 'sms',
  body: v.reply,
  ai_generated: !v.used_fallback,
  kind: 'AI_' + v.mode,
  metadata: { mode: v.mode, used_fallback: v.used_fallback, errors: v.errors }
}}];""")
    x07 = w.call("EXECUTE_07_SEND_SMS", "07")
    w.chain(trig, lead, msgs, rules, can)
    w.connect(can, prep, 0)
    w.connect(can, skip, 1)
    w.chain(prep, ai, parse, val, save, psend, x07)
    w.note("""## 06 · Conversation
Modes: FIRST_CONTACT · ANSWER · QUALIFY · OFFER_SLOTS · NO_SLOTS · DISQUALIFIED · FOLLOWUP · POST_CALL.
Guardrails reject prices, booking claims, unsafe advice, "I'm human", and self-invented times → safe fallback text.
In OFFER_SLOTS the model writes one sentence; the numbered slot list is appended from workflow 09 output.""", width=600)
    return w


# =====================================================================
def wf07():
    w = WF("07", "RR 07 · Send SMS / WhatsApp",
           "Single exit point for every outbound text: E.164 + body validation, consent gate, Twilio, audit trail")
    trig = w.sub_trigger()
    ctx = w.sql("GET_CONTEXT", """
WITH conv AS (
  INSERT INTO conversations (organization_id, lead_id, channel, status, ai_active, last_message_at)
  SELECT l.organization_id, l.id, $2::text, 'ACTIVE', true, NOW() FROM leads l WHERE l.id = $1::uuid
  ON CONFLICT (lead_id, channel) WHERE status = 'ACTIVE'
  DO UPDATE SET last_message_at = conversations.last_message_at
  RETURNING id
)
SELECT l.id AS lead_id, l.phone AS phone_e164, l.name, l.status AS lead_status,
       o.id AS organization_id, o.name AS org_name, o.business_phone, o.whatsapp_number,
       (SELECT id FROM conv) AS conversation_id,
       rr_can_contact(l.id, $2::text) AS can_contact
FROM leads l JOIN organizations o ON o.id = l.organization_id
WHERE l.id = $1::uuid;""", [f"{T}.lead_id", f"{T}.channel === 'whatsapp' ? 'whatsapp' : 'sms'"])
    vphone = w.code("VALIDATE_PHONE", r"""
const c = $json;
if (!c.lead_id) throw new Error('VALIDATION: lead not found');
if (!/^\+[1-9]\d{7,14}$/.test(c.phone_e164 || '')) throw new Error('VALIDATION: Invalid E.164 phone');
return $input.all();""")
    vmsg = w.code("VALIDATE_MESSAGE", r"""
const t = $('EXECUTE_WORKFLOW_TRIGGER').first().json;
const c = $json;
const body = String(t.body || t.reply || '').trim();
if (!body || body.length > 1000) throw new Error('VALIDATION: Invalid SMS body (empty or > 1000 chars)');
const channel = t.channel === 'whatsapp' ? 'whatsapp' : 'sms';
const from = channel === 'whatsapp' ? (c.whatsapp_number || c.business_phone) : c.business_phone;
if (!from) throw new Error('VALIDATION: organization has no sending number');
return [{ json: { ...c, body, channel, from, kind: t.kind || 'AI_REPLY', ai_generated: t.ai_generated !== false,
                  metadata: t.metadata || {} } }];""")
    consent = w.if_("CONSENT_OK?", "$json.can_contact === true")
    blocked = w.sql("LOG_BLOCKED", """
SELECT rr_log_event($1::uuid, $2::uuid, '07_SEND_SMS', 'BLOCKED_NO_CONSENT', $3::jsonb) AS event_id,
       false AS sent, 'no_consent_or_stopped' AS reason, $2::uuid AS lead_id;""",
                    ["$json.organization_id", "$json.lead_id", "JSON.stringify({kind: $json.kind})"])
    send = w.twilio_sms("TWILIO_SEND_SMS", "$json.from", "$json.phone_e164", "$json.body",
                        whatsapp_expr="$json.channel === 'whatsapp'")
    M = "$('VALIDATE_MESSAGE').first().json"
    save = w.sql("SAVE_OUTBOUND_MESSAGE", """
INSERT INTO messages (organization_id, conversation_id, lead_id, external_message_id, direction, channel,
                      sender, recipient, body, ai_generated, delivery_status, metadata)
VALUES ($1::uuid, $2::uuid, $3::uuid, $4::text, 'OUTBOUND', $5::text, $6::text, $7::text, $8::text,
        $9::boolean, $10::text, $11::jsonb)
ON CONFLICT (external_message_id) WHERE external_message_id IS NOT NULL DO NOTHING
RETURNING id, external_message_id;""",
                 [f"{M}.organization_id", f"{M}.conversation_id", f"{M}.lead_id", "$json.sid", f"{M}.channel",
                  f"{M}.from", f"{M}.phone_e164", f"{M}.body", f"{M}.ai_generated", "$json.status || 'queued'",
                  f"JSON.stringify({{kind: {M}.kind, ...{M}.metadata}})"])
    upd = w.sql("UPDATE_LEAD", """
WITH l AS (
  UPDATE leads SET last_message_at = NOW(), last_outbound_at = NOW(),
         status = CASE WHEN status = 'NEW' THEN 'CONTACTED' ELSE status END
  WHERE id = $1::uuid RETURNING id
), c AS (
  UPDATE conversations SET last_message_at = NOW() WHERE id = $2::uuid RETURNING id
)
SELECT (SELECT id FROM l) AS lead_id;""", [f"{M}.lead_id", f"{M}.conversation_id"])
    act = w.sql("LOG_ACTIVITY", """
INSERT INTO activities (organization_id, lead_id, type, description, metadata)
VALUES ($1::uuid, $2::uuid, 'SMS_SENT', $3::text, $4::jsonb)
RETURNING true AS sent, $5::text AS message_sid, lead_id;""",
                [f"{M}.organization_id", f"{M}.lead_id", f"{M}.kind",
                 f"JSON.stringify({{channel: {M}.channel, sid: $('TWILIO_SEND_SMS').first().json.sid}})",
                 "$('TWILIO_SEND_SMS').first().json.sid"])
    w.chain(trig, ctx, vphone, vmsg, consent)
    w.connect(consent, send, 0)
    w.connect(consent, blocked, 1)
    w.chain(send, save, upd, act)
    w.note("""## 07 · Send SMS / WhatsApp
The **only** node in the system that talks to Twilio Messaging. Input: `lead_id, channel, body, kind`.
Consent is re-checked at send time (`rr_can_contact`), so a STOP that arrives mid-flow always wins.""")
    return w

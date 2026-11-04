"""Workflows 09–12 — availability, booking, reminders, CRM sync."""
from lib import WF
from common import LEAD_CTX_SQL

T = "$('EXECUTE_WORKFLOW_TRIGGER').first().json"
GCAL = "https://www.googleapis.com/calendar/v3"

CAL_SETTINGS_JS = r"""
const org = $('GET_COMPANY_RULES').first().json;
const s = org.settings || {};
const bh = s.business_hours || {};
return [{ json: {
  tz: org.timezone || 'America/New_York',
  calendar_id: s.calendar_id || 'primary',
  open: bh.start || '08:00', close: bh.end || '17:00',
  days: bh.days || [1,2,3,4,5],
  slot_minutes: s.slot_minutes || 60,
  step_minutes: s.slot_step_minutes || s.slot_minutes || 60,
  min_notice_minutes: s.min_notice_minutes ?? 120,
  horizon_days: s.booking_horizon_days || 7,
  max_slots: s.max_slots_offered || 3
}}];
"""


# =====================================================================
def wf09():
    w = WF("09", "RR 09 · Booking Availability",
           "Google Calendar free/busy → n8n slot maths → AI may only present the slots returned here")
    trig = w.sub_trigger()
    lead = w.sql("GET_LEAD", "SELECT * FROM leads WHERE id = $1::uuid;", [f"{T}.lead_id"])
    rules = w.sql("GET_COMPANY_RULES", "SELECT id, name, timezone, settings FROM organizations WHERE id = $1::uuid;",
                  ["$json.organization_id"])
    cal = w.code("GET_CALENDAR_SETTINGS", CAL_SETTINGS_JS)
    win = w.code("CALCULATE_REQUESTED_WINDOW", r"""
const t = $('EXECUTE_WORKFLOW_TRIGGER').first().json;
const c = $json;
const now = DateTime.now().setZone(c.tz);
const earliest = now.plus({ minutes: c.min_notice_minutes });
let start = earliest, end = now.plus({ days: c.horizon_days }).endOf('day');
if (t.preferred_date && /^\d{4}-\d{2}-\d{2}$/.test(t.preferred_date)) {
  const d = DateTime.fromISO(t.preferred_date, { zone: c.tz });
  if (d.isValid && d.endOf('day') > earliest) {
    start = DateTime.max(d.startOf('day'), earliest);
    end = d.endOf('day');
  }
}
return [{ json: { ...c, window_start: start.toISO(), window_end: end.toISO(),
                  preferred_date: t.preferred_date || null, preferred_time: t.preferred_time || null } }];""")
    fb = w.http("GOOGLE_CALENDAR_FREEBUSY", "POST", f"{GCAL}/freeBusy",
                body_expr="={{ JSON.stringify({ timeMin: DateTime.fromISO($json.window_start).toUTC().toISO(), "
                          "timeMax: DateTime.fromISO($json.window_end).toUTC().toISO(), timeZone: $json.tz, "
                          "items: [{ id: $json.calendar_id }] }) }}",
                cred_key="GCAL")
    dbappts = w.sql("GET_BOOKED_APPOINTMENTS", """
SELECT coalesce(jsonb_agg(jsonb_build_object('start', start_at, 'end', end_at)), '[]'::jsonb) AS busy
FROM appointments
WHERE organization_id = $1::uuid AND status IN ('SCHEDULED','CONFIRMED')
  AND start_at < $3::timestamptz AND end_at > $2::timestamptz;""",
                     ["$('GET_COMPANY_RULES').first().json.id",
                      "$('CALCULATE_REQUESTED_WINDOW').first().json.window_start",
                      "$('CALCULATE_REQUESTED_WINDOW').first().json.window_end"])
    gen = w.code("GENERATE_SLOTS", r"""
const c = $('CALCULATE_REQUESTED_WINDOW').first().json;
const ws = DateTime.fromISO(c.window_start, { zone: c.tz });
const we = DateTime.fromISO(c.window_end, { zone: c.tz });
const [oh, om] = c.open.split(':').map(Number);
const [ch, cm] = c.close.split(':').map(Number);
const candidates = [];
for (let day = ws.startOf('day'); day <= we; day = day.plus({ days: 1 })) {
  if (!c.days.includes(day.weekday)) continue;               // luxon: 1 = Monday … 7 = Sunday
  const open = day.set({ hour: oh, minute: om });
  const close = day.set({ hour: ch, minute: cm });
  for (let s = open; s.plus({ minutes: c.slot_minutes }) <= close; s = s.plus({ minutes: c.step_minutes })) {
    if (s < ws || s > we) continue;
    candidates.push({ start: s.toISO(), end: s.plus({ minutes: c.slot_minutes }).toISO() });
  }
}
return [{ json: { candidates } }];""")
    rm = w.code("REMOVE_CONFLICTS", r"""
const c = $('CALCULATE_REQUESTED_WINDOW').first().json;
const fb = $('GOOGLE_CALENDAR_FREEBUSY').first().json;
const calKey = Object.keys(fb.calendars || {})[0];
const cal = (fb.calendars || {})[calKey] || {};
if (cal.errors && cal.errors.length) throw new Error('CALENDAR: freeBusy error ' + JSON.stringify(cal.errors));
const busy = [...(cal.busy || []), ...($('GET_BOOKED_APPOINTMENTS').first().json.busy || [])]
  .map(b => [new Date(b.start).getTime(), new Date(b.end).getTime()]);
const free = $json.candidates.filter(s => {
  const a = new Date(s.start).getTime(), b = new Date(s.end).getTime();
  return !busy.some(([x, y]) => a < y && b > x);
});
return [{ json: { free, busy_count: busy.length } }];""")
    bh = w.code("CHECK_BUSINESS_HOURS", r"""
const c = $('CALCULATE_REQUESTED_WINDOW').first().json;
const earliest = DateTime.now().setZone(c.tz).plus({ minutes: c.min_notice_minutes });
let slots = $json.free
  .map(s => ({ ...s, dt: DateTime.fromISO(s.start, { zone: c.tz }) }))
  .filter(s => s.dt >= earliest && c.days.includes(s.dt.weekday));
if (c.preferred_time) {
  const [ph, pm] = c.preferred_time.split(':').map(Number);
  const target = ph * 60 + pm;
  slots.sort((a, b) => (Math.abs(a.dt.hour * 60 + a.dt.minute - target) - Math.abs(b.dt.hour * 60 + b.dt.minute - target)) || (a.dt - b.dt));
} else {
  // spread choices: at most 2 per day, earliest first
  const perDay = {};
  slots = slots.filter(s => { const k = s.dt.toISODate(); perDay[k] = (perDay[k] || 0) + 1; return perDay[k] <= 2; });
}
const out = slots.slice(0, c.max_slots).sort((a, b) => a.dt - b.dt).map(s => ({
  iso: s.dt.toISO({ suppressMilliseconds: true }),
  end_iso: DateTime.fromISO(s.end, { zone: c.tz }).toISO({ suppressMilliseconds: true }),
  label: s.dt.toFormat("ccc LLL d, h:mm a")
}));
return [{ json: { available_slots: out } }];""")
    save = w.sql("SAVE_OFFERED_SLOTS", """
UPDATE leads
SET metadata = metadata || jsonb_build_object('offered_slots', $2::jsonb, 'offered_at', NOW())
WHERE id = $1::uuid
RETURNING id;""", [f"{T}.lead_id", "JSON.stringify($json.available_slots)"])
    ret = w.code("RETURN_AVAILABLE_SLOTS", """const s = $('CHECK_BUSINESS_HOURS').first().json.available_slots;
const c = $('CALCULATE_REQUESTED_WINDOW').first().json;
return [{ json: { available: s.length > 0, available_slots: s, timezone: c.tz,
                  window_start: c.window_start, window_end: c.window_end } }];""")
    w.chain(trig, lead, rules, cal, win, fb, dbappts, gen, rm, bh, save, ret)
    w.note("""## 09 · Availability — never ask the AI what's free
Google `freeBusy` + our own active appointments → candidate slots inside business hours,
min-notice and horizon (all from `organizations.settings`) → at most 3 options.
The options are saved on the lead (`metadata.offered_slots`) so 04/10 can verify the customer picked a real one.""", width=600)
    return w


# =====================================================================
def wf10():
    w = WF("10", "RR 10 · Book Appointment",
           "Validate → idempotency → re-check calendar → Google event → DB (exclusion constraint) → deal → CRM → confirmation")
    trig = w.sub_trigger()
    lead = w.sql("GET_LEAD", LEAD_CTX_SQL, [f"{T}.lead_id", "'sms'"])
    rules = w.sql("GET_COMPANY_RULES", "SELECT id, name, timezone, settings FROM organizations WHERE id = $1::uuid;",
                  ["$json.lead.organization_id"])
    val = w.code("VALIDATE_REQUEST", r"""
const t = $('EXECUTE_WORKFLOW_TRIGGER').first().json;
const ctx = $('GET_LEAD').first().json;
const org = $('GET_COMPANY_RULES').first().json;
const s = org.settings || {};
const tz = org.timezone || 'America/New_York';
const md = ctx.lead.metadata || {};
const offered = md.offered_slots || [];
const errors = [];
const start = DateTime.fromISO(String(t.slot || t.requested_datetime || ''), { setZone: true });
if (!start.isValid) errors.push('slot is not a valid ISO datetime');
else if (start.toMillis() <= Date.now()) errors.push('slot is in the past');
const match = offered.find(o => DateTime.fromISO(o.iso).toMillis() === start.toMillis());
if (!match) errors.push('slot was not offered to this lead');
if (md.offered_at && (Date.now() - new Date(md.offered_at).getTime()) > 72 * 3600e3) errors.push('offer expired');
if (!ctx.lead.phone && !ctx.lead.email) errors.push('lead has no contact method');
const mins = s.slot_minutes || 60;
const local = start.isValid ? start.setZone(tz) : null;
return [{ json: {
  valid: errors.length === 0, errors,
  lead_id: ctx.lead.id, organization_id: ctx.lead.organization_id,
  reschedule: !!t.reschedule, channel: t.channel || 'sms', source: t.source || 'router',
  start_iso: start.isValid ? start.toUTC().toISO() : null,
  end_iso: start.isValid ? start.plus({ minutes: mins }).toUTC().toISO() : null,
  local_start: local ? local.toISO() : null,
  local_end: local ? local.plus({ minutes: mins }).toISO() : null,
  label: local ? local.toFormat("cccc, LLL d 'at' h:mm a") : null,
  tz, calendar_id: s.calendar_id || 'primary'
}}];""")
    V = "$('VALIDATE_REQUEST').first().json"
    valid = w.if_("REQUEST_VALID?", "$json.valid")
    idem = w.sql("IDEMPOTENCY_CHECK", """
SELECT * FROM appointments
WHERE lead_id = $1::uuid AND start_at = $2::timestamptz AND status IN ('SCHEDULED','CONFIRMED')
LIMIT 1;""", ["$json.lead_id", "$json.start_iso"], always=True)
    dup = w.if_("ALREADY_BOOKED?", "!!$json.id")
    ret_dup = w.code("RETURN_EXISTING", f"""const v = {V};
return [{{ json: {{ booked: true, duplicate: true, appointment_id: $json.id, start_iso: v.local_start, label: v.label }} }}];""")
    recheck = w.http("RECHECK_AVAILABILITY", "POST", f"{GCAL}/freeBusy",
                     body_expr=f"={{{{ JSON.stringify({{ timeMin: {V}.start_iso, timeMax: {V}.end_iso, "
                               f"timeZone: {V}.tz, items: [{{ id: {V}.calendar_id }}] }}) }}}}",
                     cred_key="GCAL")
    free = w.if_("SLOT_STILL_AVAILABLE?",
                 "Object.values($json.calendars || {}).every(c => !(c.errors || []).length && !(c.busy || []).length)")
    ev = w.http("CREATE_GOOGLE_EVENT", "POST",
                f"={GCAL}/calendars/{{{{ encodeURIComponent({V}.calendar_id) }}}}/events",
                body_expr="={{ JSON.stringify({"
                          f" summary: 'Roof inspection — ' + ($('GET_LEAD').first().json.lead.name || $('GET_LEAD').first().json.lead.phone),"
                          " location: $('GET_LEAD').first().json.lead.address || '',"
                          " description: 'Booked by RoofRadar AI\\nPhone: ' + ($('GET_LEAD').first().json.lead.phone || '') + "
                          "'\\nProblem: ' + ($('GET_LEAD').first().json.lead.problem_type || 'n/a') + "
                          "(['LOW','MEDIUM','HIGH','EMERGENCY'].includes($('GET_LEAD').first().json.lead.urgency) ? '\\nUrgency: ' + $('GET_LEAD').first().json.lead.urgency : '') + "
                          "($('GET_LEAD').first().json.lead.lead_score > 0 ? '\\nLead score: ' + $('GET_LEAD').first().json.lead.lead_score : ''),"
                          f" start: {{ dateTime: {V}.local_start, timeZone: {V}.tz }},"
                          f" end: {{ dateTime: {V}.local_end, timeZone: {V}.tz }},"
                          f" extendedProperties: {{ private: {{ roofradar_lead_id: {V}.lead_id }} }}"
                          " }) }}",
                cred_key="GCAL", retry=False)
    appt = w.sql("CREATE_APPOINTMENT_DB", """
INSERT INTO appointments (organization_id, lead_id, external_event_id, start_at, end_at, status, notes)
VALUES ($1::uuid, $2::uuid, $3::text, $4::timestamptz, $5::timestamptz, 'SCHEDULED', $6::text)
RETURNING *;""", [f"{V}.organization_id", f"{V}.lead_id", "$json.id", f"{V}.start_iso", f"{V}.end_iso",
                  f"'Booked via ' + {V}.channel"], on_error="continueErrorOutput")
    rollback = w.http("ROLLBACK_GOOGLE_EVENT", "DELETE",
                      f"={GCAL}/calendars/{{{{ encodeURIComponent({V}.calendar_id) }}}}/events/{{{{ $('CREATE_GOOGLE_EVENT').first().json.id }}}}",
                      cred_key="GCAL", never_error=True, retry=False)
    prev = w.sql("CANCEL_PREVIOUS", """
WITH u AS (
  UPDATE appointments SET status = 'RESCHEDULED'
  WHERE lead_id = $1::uuid AND id <> $2::uuid AND status IN ('SCHEDULED','CONFIRMED') AND $3::boolean
  RETURNING id, external_event_id, start_at
), link AS (
  UPDATE appointments SET rescheduled_from = (SELECT id FROM u ORDER BY start_at LIMIT 1)
  WHERE id = $2::uuid AND EXISTS (SELECT 1 FROM u) RETURNING id
)
SELECT u.*, (SELECT count(*) FROM link) AS linked FROM u ORDER BY start_at LIMIT 1;""",
                 [f"{V}.lead_id", "$json.id", f"{V}.reschedule"], always=True)
    has_old = w.if_("HAS_OLD_EVENT?", "!!$json.external_event_id")
    del_old = w.http("DELETE_OLD_GOOGLE_EVENT", "DELETE",
                     f"={GCAL}/calendars/{{{{ encodeURIComponent({V}.calendar_id) }}}}/events/{{{{ $json.external_event_id }}}}",
                     cred_key="GCAL", never_error=True, retry=False)
    A = "$('CREATE_APPOINTMENT_DB').first().json"
    upd = w.sql("UPDATE_LEAD", """
UPDATE leads SET status = 'BOOKED',
       metadata = (metadata - 'offered_slots') || jsonb_build_object('last_booked_at', NOW())
WHERE id = $1::uuid
RETURNING id, rr_cancel_followups(id, 'BOOKED') AS cancelled_followups;""", [f"{V}.lead_id"])
    deal = w.sql("CREATE_DEAL", """
INSERT INTO deals (organization_id, lead_id, appointment_id, stage)
VALUES ($1::uuid, $2::uuid, $3::uuid, 'INSPECTION_BOOKED')
ON CONFLICT (lead_id) WHERE stage NOT IN ('WON','LOST')
DO UPDATE SET appointment_id = EXCLUDED.appointment_id,
              stage = CASE WHEN deals.stage IN ('INSPECTION_BOOKED','INSPECTED') THEN 'INSPECTION_BOOKED' ELSE deals.stage END
RETURNING *;""", [f"{V}.organization_id", f"{V}.lead_id", f"{A}.id"])
    pcrm = w.code("PREP_CRM", f"return [{{ json: {{ lead_id: {V}.lead_id }} }}];")
    crm = w.call("CRM_SYNC", "12", wait=False)
    pconf = w.code("PREP_CONFIRMATION", f"""const v = {V};
const org = $('GET_COMPANY_RULES').first().json;
const lead = $('GET_LEAD').first().json.lead;
const verb = v.reschedule ? 'has been moved to' : 'is confirmed for';
const body = `Your ${{org.name}} roof inspection ${{verb}} ${{v.label}}.` +
  (lead.address ? ` Address: ${{lead.address}}.` : '') +
  ` Our inspector will call when on the way. Need to change it? Just reply here.`;
return [{{ json: {{ lead_id: v.lead_id, channel: v.channel === 'whatsapp' ? 'whatsapp' : 'sms', body,
                   kind: 'BOOKING_CONFIRMATION', ai_generated: false }} }}];""")
    send = w.call("SEND_CONFIRMATION", "07", onError="continueRegularOutput")
    slack = w.slack("ALERT_SALES", "$('GET_COMPANY_RULES').first().json.settings.slack_channel",
                    f"(({V}.reschedule) ? '🔁 RESCHEDULED' : '🔥 NEW BOOKED LEAD') + '\\n\\n' + "
                    "($('GET_LEAD').first().json.lead.name || 'Unknown') + '\\n' + "
                    "($('GET_LEAD').first().json.lead.problem_type || 'Roof issue') + '\\n' + "
                    "($('GET_LEAD').first().json.lead.address || 'Address TBC') + '\\n' + "
                    f"{V}.label + ' inspection' + ($('GET_LEAD').first().json.lead.lead_score > 0 ? '\\nLead score: ' + $('GET_LEAD').first().json.lead.lead_score : '') + "
                    "'\\nPhone: ' + $('GET_LEAD').first().json.lead.phone")
    log = w.sql("LOG_EVENT", "SELECT rr_log_event($1::uuid, $2::uuid, '10_BOOK_APPOINTMENT', $3::text, $4::jsonb) AS id;",
                [f"{V}.organization_id", f"{V}.lead_id", f"{V}.reschedule ? 'APPOINTMENT_RESCHEDULED' : 'APPOINTMENT_BOOKED'",
                 f"JSON.stringify({{appointment_id: {A}.id, start: {V}.start_iso, source: {V}.source}})"])
    ret = w.code("RETURN_BOOKED", f"""const v = {V};
return [{{ json: {{ booked: true, duplicate: false, appointment_id: {A}.id, start_iso: v.local_start,
                   label: v.label, reschedule: v.reschedule }} }}];""")
    # alternatives path (invalid slot / taken / DB conflict)
    palt = w.code("PREP_ALTERNATIVES", f"""const v = {V};
let reason = 'slot_unavailable';
if (!v.valid) reason = (v.errors || []).join('; ');
return [{{ json: {{ lead_id: v.lead_id, preferred_date: v.local_start ? v.local_start.slice(0, 10) : null,
                   preferred_time: v.local_start ? v.local_start.slice(11, 16) : null, _reason: reason }} }}];""")
    x09 = w.call("EXECUTE_09_AVAILABILITY", "09")
    ralt = w.code("RETURN_ALTERNATIVES", """return [{ json: { booked: false,
  reason: $('PREP_ALTERNATIVES').first().json._reason,
  alternatives: $json.available_slots || [] } }];""")

    w.chain(trig, lead, rules, val, valid)
    w.connect(valid, idem, 0)
    w.connect(valid, palt, 1)
    w.connect(idem, dup)
    w.connect(dup, ret_dup, 0)
    w.connect(dup, recheck, 1)
    w.connect(recheck, free)
    w.connect(free, ev, 0)
    w.connect(free, palt, 1)
    w.connect(ev, appt)
    w.connect(appt, prev, 0)
    w.connect(appt, rollback, 1)
    w.connect(rollback, palt)
    w.connect(prev, has_old)
    w.connect(has_old, del_old, 0)
    w.connect(has_old, upd, 1)
    w.connect(del_old, upd)
    w.chain(upd, deal, pcrm, crm, pconf, send, slack, log, ret)
    w.chain(palt, x09, ralt)
    w.note("""## 10 · Book Appointment
Three layers against double-booking: (1) the slot must be one we **offered** this lead,
(2) idempotency check on (lead, start) + live `freeBusy` re-check, (3) the DB exclusion constraint
`appt_no_overlap` — if it fires, the Google event is rolled back and fresh alternatives are returned.
Reschedule = book new → mark old RESCHEDULED → delete old Google event.""", width=620)
    return w


# =====================================================================
def wf11():
    w = WF("11", "RR 11 · Appointment Reminders", "Every 15 min: 24-hour and 2-hour SMS reminders (claim-then-send, never twice)")
    trig = w.schedule("SCHEDULE_TRIGGER", minutes=15)
    find = w.sql("FIND_UPCOMING_APPOINTMENTS", """
SELECT a.id AS appointment_id, a.start_at, a.lead_id, l.phone, l.name, l.address,
       o.name AS org_name, o.timezone, o.settings->'reminders' AS reminder_cfg,
       CASE
         WHEN a.reminder_24h_sent_at IS NULL AND a.start_at BETWEEN NOW() + INTERVAL '20 hours' AND NOW() + INTERVAL '25 hours' THEN '24_HOUR'
         WHEN a.reminder_2h_sent_at  IS NULL AND a.start_at BETWEEN NOW() + INTERVAL '1 hour'   AND NOW() + INTERVAL '2 hours 15 minutes' THEN '2_HOUR'
         ELSE 'NONE'
       END AS reminder_type,
       rr_can_contact(l.id, 'sms') AS can_contact
FROM appointments a
JOIN leads l ON l.id = a.lead_id
JOIN organizations o ON o.id = a.organization_id
WHERE a.status IN ('SCHEDULED','CONFIRMED')
  AND a.start_at BETWEEN NOW() + INTERVAL '1 hour' AND NOW() + INTERVAL '25 hours';""")
    calc = w.code("CALCULATE_REMINDER_TYPE", r"""
const r = $json;
const when = DateTime.fromJSDate(new Date(r.start_at)).setZone(r.timezone || 'America/New_York');
const first = (r.name || '').split(' ')[0];
const hi = first ? `Hi ${first}, ` : 'Hi, ';
const body = r.reminder_type === '24_HOUR'
  ? `${hi}reminder: your ${r.org_name} roof inspection is tomorrow, ${when.toFormat("cccc 'at' h:mm a")}. Reply here if you need to reschedule.`
  : `${hi}your ${r.org_name} inspector arrives around ${when.toFormat('h:mm a')} today. Reply here if anything has changed.`;
const enabled = r.reminder_type === '24_HOUR' ? (r.reminder_cfg?.h24 !== false) : (r.reminder_cfg?.h2 !== false);
return { json: { ...r, body, reminder_type: enabled ? r.reminder_type : 'NONE' } };""", each=True)
    sw = w.switch("SWITCH", "$json.reminder_type", ["24_HOUR", "2_HOUR"])
    consent = w.if_("CHECK_CONSENT", "$json.can_contact === true")
    claim = w.sql("CLAIM_REMINDER", """
UPDATE appointments SET
  reminder_24h_sent_at = CASE WHEN $2::text = '24_HOUR' THEN NOW() ELSE reminder_24h_sent_at END,
  reminder_2h_sent_at  = CASE WHEN $2::text = '2_HOUR'  THEN NOW() ELSE reminder_2h_sent_at  END
WHERE id = $1::uuid
  AND ( ($2::text = '24_HOUR' AND reminder_24h_sent_at IS NULL)
     OR ($2::text = '2_HOUR'  AND reminder_2h_sent_at  IS NULL) )
RETURNING id AS appointment_id, lead_id, $2::text AS reminder_type, $3::text AS body;""",
                  ["$json.appointment_id", "$json.reminder_type", "$json.body"], gate="appointment_id")
    prep = w.code("PREP_SMS", "return { json: { lead_id: $json.lead_id, channel: 'sms', body: $json.body, kind: 'REMINDER_' + $json.reminder_type, ai_generated: false } };", each=True)
    send = w.call("SEND_SMS", "07", onError="continueRegularOutput")
    log = w.sql("LOG_REMINDER", """
INSERT INTO activities (organization_id, lead_id, type, description, metadata)
SELECT organization_id, id, 'REMINDER_SENT', 'Appointment reminder', $2::jsonb FROM leads WHERE id = $1::uuid
RETURNING id;""", ["$json.lead_id", "JSON.stringify({sent: $json.sent, sid: $json.message_sid || null})"])
    w.chain(trig, find, calc, sw)
    w.connect(sw, consent, 0)
    w.connect(sw, consent, 1)
    w.connect(consent, claim, 0)
    w.chain(claim, prep, send, log)
    return w


# =====================================================================
def wf12():
    w = WF("12", "RR 12 · CRM Sync (HubSpot)",
           "Mirror lead + deal to HubSpot. Postgres stays the source of truth; CRM failures never block a booking")
    trig = w.sub_trigger()
    lead = w.sql("GET_LEAD", """
SELECT to_jsonb(l.*) AS lead, o.settings->'crm' AS crm, o.timezone,
       (SELECT to_jsonb(d.*) FROM deals d WHERE d.lead_id = l.id ORDER BY d.created_at DESC LIMIT 1) AS deal,
       (SELECT to_jsonb(a.*) FROM appointments a WHERE a.lead_id = l.id AND a.status IN ('SCHEDULED','CONFIRMED')
         ORDER BY a.start_at LIMIT 1) AS appointment
FROM leads l JOIN organizations o ON o.id = l.organization_id
WHERE l.id = $1::uuid;""", [f"{T}.lead_id"])
    en = w.if_("CRM_ENABLED?", "$json.crm && $json.crm.enabled === true")
    skip = w.sql("LOG_CRM_DISABLED", "SELECT 'crm_disabled' AS result;")
    mp = w.code("MAP_CRM_FIELDS", r"""
const { lead, crm, deal, appointment, timezone } = $json;
const [firstname, ...rest] = (lead.name || '').trim().split(/\s+/);
const props = {
  firstname: firstname || null, lastname: rest.join(' ') || null,
  phone: lead.phone, email: lead.email, address: lead.address,
  lifecyclestage: lead.status === 'BOOKED' ? 'opportunity' : 'lead'
};
if (crm.custom_properties) Object.assign(props, {
  roofradar_lead_score: lead.lead_score,
  roofradar_qualification_status: lead.qualification_status,
  roofradar_source: lead.source,
  roofradar_status: lead.status,
  roofradar_appointment: appointment ? DateTime.fromISO(appointment.start_at).setZone(timezone).toFormat('yyyy-LL-dd HH:mm') : null
});
Object.keys(props).forEach(k => (props[k] === null || props[k] === undefined || props[k] === '') && delete props[k]);
const filterGroups = [];
if (lead.phone) filterGroups.push({ filters: [{ propertyName: 'phone', operator: 'EQ', value: lead.phone }] });
if (lead.email) filterGroups.push({ filters: [{ propertyName: 'email', operator: 'EQ', value: lead.email }] });
return [{ json: {
  lead_id: lead.id, crm_contact_id: lead.crm_contact_id, props, filterGroups,
  deal_id: deal?.id || null, crm_deal_id: deal?.crm_deal_id || null,
  deal_props: deal ? {
    dealname: `Roof inspection — ${lead.name || lead.phone}`,
    pipeline: crm.pipeline || 'default',
    dealstage: (crm.stage_map || {})[deal.stage] || 'appointmentscheduled'
  } : null
}}];""")
    HS = "https://api.hubapi.com/crm/v3/objects"
    find = w.http("FIND_CRM_CONTACT", "POST", f"{HS}/contacts/search",
                  body_expr="={{ JSON.stringify({ filterGroups: $json.filterGroups, limit: 1, properties: ['phone','email'] }) }}",
                  cred_key="HUBSPOT")
    ex = w.if_("CONTACT_EXISTS?", "($json.total || 0) > 0")
    M = "$('MAP_CRM_FIELDS').first().json"
    upd = w.http("UPDATE_CRM_CONTACT", "PATCH", f"={HS}/contacts/{{{{ $json.results[0].id }}}}",
                 body_expr=f"={{{{ JSON.stringify({{ properties: {M}.props }}) }}}}", cred_key="HUBSPOT")
    crt = w.http("CREATE_CRM_CONTACT", "POST", f"{HS}/contacts",
                 body_expr=f"={{{{ JSON.stringify({{ properties: {M}.props }}) }}}}", cred_key="HUBSPOT")
    cid = w.code("CONTACT_ID", "return [{ json: { contact_id: String($json.id), deal_action: $('MAP_CRM_FIELDS').first().json.deal_props ? ($('MAP_CRM_FIELDS').first().json.crm_deal_id ? 'UPDATE' : 'CREATE') : 'NONE' } }];")
    dsw = w.switch("UPSERT_CRM_DEAL", "$json.deal_action", ["CREATE", "UPDATE"])
    dcreate = w.http("CREATE_CRM_DEAL", "POST", f"{HS}/deals",
                     body_expr=f"={{{{ JSON.stringify({{ properties: {M}.deal_props, associations: [{{ to: {{ id: $json.contact_id }}, "
                               "types: [{ associationCategory: 'HUBSPOT_DEFINED', associationTypeId: 3 }] }] }) }}",
                     cred_key="HUBSPOT")
    dupdate = w.http("UPDATE_CRM_DEAL", "PATCH", f"={HS}/deals/{{{{ {M}.crm_deal_id }}}}",
                     body_expr=f"={{{{ JSON.stringify({{ properties: {M}.deal_props }}) }}}}", cred_key="HUBSPOT")
    log = w.sql("LOG_CRM_SYNC", """
WITH l AS (UPDATE leads SET crm_contact_id = $2::text WHERE id = $1::uuid RETURNING organization_id),
     d AS (UPDATE deals SET crm_deal_id = COALESCE(crm_deal_id, NULLIF($4::text,'')) WHERE id = NULLIF($3::text,'')::uuid RETURNING id)
INSERT INTO activities (organization_id, lead_id, type, description, metadata)
SELECT organization_id, $1::uuid, 'CRM_SYNC', 'HubSpot contact/deal upserted',
       jsonb_build_object('contact_id', $2::text, 'deal_id', NULLIF($4::text,''))
FROM l RETURNING id;""", [f"{M}.lead_id", "$('CONTACT_ID').first().json.contact_id",
                          f"{M}.deal_id || ''", f"($json.id ? String($json.id) : ({M}.crm_deal_id || ''))"])
    w.chain(trig, lead, en)
    w.connect(en, mp, 0)
    w.connect(en, skip, 1)
    w.chain(mp, find, ex)
    w.connect(ex, upd, 0)
    w.connect(ex, crt, 1)
    w.connect(upd, cid)
    w.connect(crt, cid)
    w.connect(cid, dsw)
    w.connect(dsw, dcreate, 0)
    w.connect(dsw, dupdate, 1)
    w.connect(dsw, log, 2)
    w.connect(dcreate, log)
    w.connect(dupdate, log)
    w.note("""## 12 · CRM Sync
Enable with `settings.crm.enabled=true`. Uses HubSpot private-app token.
Set `settings.crm.custom_properties=true` after creating the `roofradar_*` contact properties in HubSpot.
Called fire-and-forget from 10, 24B — a CRM outage never blocks a customer.""")
    return w

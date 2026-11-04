"""Workflows 25–26 — no-show recovery and old-lead re-engagement (added after the first 24)."""
from lib import WF


# =====================================================================
def wf25():
    w = WF("25", "RR 25 · No-show Recovery",
           "Every 30 min: appointments a person marked NO_SHOW → claim once → real open slots (09) → kind rebook text (06)")
    trig = w.schedule("SCHEDULE_TRIGGER", minutes=30)
    find = w.sql("FIND_NO_SHOWS", """
SELECT a.id AS appointment_id, a.lead_id, a.organization_id
FROM appointments a
JOIN leads l ON l.id = a.lead_id
JOIN organizations o ON o.id = a.organization_id
WHERE a.status = 'NO_SHOW'
  AND a.no_show_recovery_sent_at IS NULL
  AND a.start_at > NOW() - INTERVAL '7 days'                      -- don't text about old misses
  AND o.active AND l.phone IS NOT NULL AND NOT l.human_handoff
  AND l.status NOT IN ('STOPPED','LOST','WON','DISQUALIFIED')
  AND rr_can_contact(l.id, 'sms')
  AND extract(hour FROM NOW() AT TIME ZONE o.timezone) BETWEEN 9 AND 19   -- quiet hours: 9:00–19:59 local
  AND NOT EXISTS (SELECT 1 FROM appointments b WHERE b.lead_id = a.lead_id
                  AND b.status IN ('SCHEDULED','CONFIRMED') AND b.start_at > NOW())  -- already rebooked
ORDER BY a.start_at
LIMIT 10;""", gate="appointment_id")
    claim = w.sql("CLAIM_NO_SHOW", """
WITH a AS (
  UPDATE appointments SET no_show_recovery_sent_at = NOW()
  WHERE id = $1::uuid AND no_show_recovery_sent_at IS NULL
  RETURNING id AS appointment_id, lead_id, organization_id
), l AS (   -- let the lead book again
  UPDATE leads SET status = CASE WHEN status = 'BOOKED' THEN 'ENGAGED' ELSE status END
  WHERE id = (SELECT lead_id FROM a) RETURNING id
)
SELECT a.* FROM a;""", ["$json.appointment_id"], gate="appointment_id")
    p09 = w.code("PREP_AVAILABILITY", "return { json: { lead_id: $json.lead_id } };", each=True)
    x09 = w.call("EXECUTE_09_AVAILABILITY", "09")
    p06 = w.code("PREP_REBOOK_TEXT", """const c = $('CLAIM_NO_SHOW').item.json;
const slots = $json.available_slots || [];
return { json: { lead_id: c.lead_id, organization_id: c.organization_id, channel: 'sms',
                 mode: slots.length ? 'NO_SHOW' : 'NO_SLOTS', slots } };""", each=True)
    x06 = w.call("EXECUTE_06_AI_CONVERSATION", "06", onError="continueRegularOutput")
    log = w.sql("LOG_ACTIVITY", """
INSERT INTO activities (organization_id, lead_id, type, description, metadata)
VALUES ($1::uuid, $2::uuid, 'NO_SHOW_RECOVERY', 'Rebook text after missed inspection', $3::jsonb)
RETURNING id;""", ["$('CLAIM_NO_SHOW').item.json.organization_id", "$('CLAIM_NO_SHOW').item.json.lead_id",
                   "JSON.stringify({appointment_id: $('CLAIM_NO_SHOW').item.json.appointment_id, sent: $json.sent === true})"])
    w.chain(trig, find, claim, p09, x09, p06, x06, log)
    w.note("""## 25 · No-show Recovery
Only a person knows an inspection was missed. Mark it in Supabase:
`UPDATE appointments SET status='NO_SHOW' WHERE id='…';`
Within 30 minutes (9 AM–8 PM local) the customer gets one kind text with real open times; replying with a number rebooks through 04 → 10.
Claim-then-send (`no_show_recovery_sent_at`) means it is never sent twice.""", width=620)
    return w


# =====================================================================
def wf26():
    w = WF("26", "RR 26 · Lead Re-engagement",
           "Daily 11:00: quiet leads (30–180 days, never booked, still reachable) → one check-in text (06), at most every 90 days")
    trig = w.schedule("SCHEDULE_TRIGGER", cron="0 11 * * *")
    find = w.sql("FIND_QUIET_LEADS", """
SELECT l.id AS lead_id, l.organization_id
FROM leads l
JOIN organizations o ON o.id = l.organization_id
WHERE o.active
  AND coalesce((o.settings->'reengagement'->>'enabled')::boolean, true)
  AND l.status IN ('CONTACTED','ENGAGED','QUALIFIED')
  AND NOT l.human_handoff AND l.phone IS NOT NULL
  AND coalesce(l.last_message_at, l.created_at) < NOW() - INTERVAL '30 days'
  AND coalesce(l.last_message_at, l.created_at) > NOW() - INTERVAL '180 days'
  AND (l.metadata->>'reengaged_at' IS NULL OR (l.metadata->>'reengaged_at')::timestamptz < NOW() - INTERVAL '90 days')
  AND NOT EXISTS (SELECT 1 FROM followups f WHERE f.lead_id = l.id AND f.status = 'PENDING')
  AND NOT EXISTS (SELECT 1 FROM appointments a WHERE a.lead_id = l.id
                  AND a.status IN ('SCHEDULED','CONFIRMED') AND a.start_at > NOW())
  AND rr_can_contact(l.id, 'sms')
ORDER BY l.lead_score DESC
LIMIT 20;""", gate="lead_id")
    claim = w.sql("CLAIM_LEAD", """
UPDATE leads SET metadata = metadata || jsonb_build_object('reengaged_at', NOW())
WHERE id = $1::uuid
  AND (metadata->>'reengaged_at' IS NULL OR (metadata->>'reengaged_at')::timestamptz < NOW() - INTERVAL '90 days')
RETURNING id AS lead_id, organization_id;""", ["$json.lead_id"], gate="lead_id")
    p06 = w.code("PREP_CHECK_IN", "return { json: { lead_id: $json.lead_id, organization_id: $json.organization_id, channel: 'sms', mode: 'REENGAGE' } };", each=True)
    x06 = w.call("EXECUTE_06_AI_CONVERSATION", "06", onError="continueRegularOutput")
    log = w.sql("LOG_ACTIVITY", """
INSERT INTO activities (organization_id, lead_id, type, description, metadata)
VALUES ($1::uuid, $2::uuid, 'REENGAGEMENT', 'Check-in text to quiet lead', $3::jsonb)
RETURNING id;""", ["$('CLAIM_LEAD').item.json.organization_id", "$('CLAIM_LEAD').item.json.lead_id",
                   "JSON.stringify({sent: $json.sent === true})"])
    w.chain(trig, find, claim, p06, x06, log)
    w.note("""## 26 · Lead Re-engagement
One friendly check-in to leads quiet for 30+ days (never older than 180 days), only if they're still reachable,
not booked, not handed to a person and have no follow-ups pending. At most once every 90 days per lead.
Turn off per client with `settings.reengagement.enabled = false`.""", width=620)
    return w

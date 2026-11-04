"""Workflows 19–23 — error handler, retry queue, consent, monitoring, maintenance."""
from lib import WF

T = "$('EXECUTE_WORKFLOW_TRIGGER').first().json"
ALERT_CH = "(SELECT value #>> '{}' FROM app_config WHERE key = 'alert_slack_channel')"


# =====================================================================
def wf19():
    w = WF("19", "RR 19 · Error Handler",
           "Central Error Workflow for all 24: normalise → classify → save → retry queue or Slack alert",
           error_workflow=False)
    trig = w.error_trigger()
    norm = w.code("NORMALIZE_ERROR", r"""
const e = $json;
const ex = e.execution || {};
const err = ex.error || e.trigger?.error || {};
const msg = String(err.message || err.description || 'Unknown error');
const codeMatch = msg.match(/\b([45]\d\d)\b/);
const http = Number(err.httpCode || err.context?.httpCode || (codeMatch ? codeMatch[1] : 0)) || null;
return [{ json: {
  workflow_id: e.workflow?.id || null,
  workflow_name: e.workflow?.name || null,
  execution_id: ex.id || null,
  execution_url: ex.url || null,
  retry_of: ex.retryOf || null,
  mode: ex.mode || (e.trigger ? 'trigger' : null),
  node_name: ex.lastNodeExecuted || err.node?.name || null,
  node_type: err.node?.type || null,
  message: msg.slice(0, 2000),
  description: String(err.description || '').slice(0, 1000),
  http_code: http
}}];""")
    cls = w.code("CLASSIFY_ERROR", r"""
const j = $json;
const m = (j.message + ' ' + j.description).toLowerCase();
const where = ((j.node_type || '') + ' ' + (j.node_name || '')).toLowerCase();
const h = j.http_code;
let category = 'UNKNOWN', retryable = false;

if (/^validation|invalid e\.164|invalid sms body|not a valid|bad request/.test(m) || h === 400 || h === 422) category = 'VALIDATION';
else if (h === 401 || h === 403 || /unauthori[sz]ed|forbidden|invalid api key|incorrect api key|authentication|invalid_grant|credentials? (are|is) (invalid|missing)/.test(m)) category = 'AUTHENTICATION';
else if (h === 429 || /rate limit|too many requests|quota/.test(m)) { category = 'TRANSIENT_API'; retryable = !/insufficient_quota/.test(m); }
else if ((h && h >= 500) || /timeout|timed out|econnreset|econnrefused|etimedout|socket hang up|network|enotfound|eai_again|service unavailable|bad gateway/.test(m)) { category = 'TRANSIENT_API'; retryable = true; }
else if (/duplicate key|violates|constraint|syntax error|does not exist|column|relation|postgres|pg_|deadlock|could not serialize/.test(m) || /postgres/.test(where)) {
  category = 'DATABASE'; retryable = /deadlock|could not serialize|connection terminated|too many clients/.test(m);
}

// attribute to the service when the failure is service-side
if (category === 'TRANSIENT_API' || category === 'UNKNOWN' || category === 'AUTHENTICATION') {
  if (/twilio/.test(where + m)) category = category === 'AUTHENTICATION' ? category : 'TWILIO';
  else if (/calendar|googleapis|freebusy|google_event/.test(where + m)) category = category === 'AUTHENTICATION' ? category : 'CALENDAR';
  else if (/hubspot|crm/.test(where + m)) category = category === 'AUTHENTICATION' ? category : 'CRM';
  else if (/openai|router|qualifier|conversation|ai_generate|summary/.test(where + m)) category = category === 'AUTHENTICATION' ? category : 'AI';
}

// never blindly retry these
if (/21610|21211|21614|unsubscribed|opted out|blacklist|not a valid phone/.test(m)) { category = 'TWILIO'; retryable = false; }
if (/exclusion|appt_no_overlap|duplicate booking/.test(m)) retryable = false;
if (j.mode === 'manual' || !j.execution_id || j.retry_of) retryable = false;   // retries of retries are owned by workflow 20
if (j.mode === 'integrated') retryable = false;   // a failing sub-workflow also fails its parent; the parent run is the one we retry

const severity = (category === 'AUTHENTICATION' || category === 'DATABASE' || !retryable) ? 'CRITICAL' : 'WARNING';
return [{ json: { ...j, category, retryable, severity } }];""")
    save = w.sql("SAVE_ERROR", f"""
WITH ins AS (
  INSERT INTO errors (workflow_id, workflow_name, execution_id, execution_url, node_name, message, http_code, category, retryable, payload)
  VALUES ($1::text, $2::text, $3::text, $4::text, $5::text, $6::text, $7::int, $8::text, $9::boolean, $10::jsonb)
  RETURNING *
)
SELECT ins.id AS error_id, ins.category, ins.retryable, {ALERT_CH} AS alert_channel,
       -- the same failure surfaces from a sub-workflow and its parent: alert once
       EXISTS (SELECT 1 FROM errors e WHERE e.message = ins.message AND e.created_at > NOW() - INTERVAL '2 minutes') AS duplicate
FROM ins;""",
                 ["$json.workflow_id", "$json.workflow_name", "$json.execution_id", "$json.execution_url",
                  "$json.node_name", "$json.message", "$json.http_code", "$json.category", "$json.retryable",
                  "JSON.stringify($json)"])
    isr = w.if_("IS_RETRYABLE?", "$json.retryable === true")
    C = "$('CLASSIFY_ERROR').first().json"
    enq = w.sql("ENQUEUE_RETRY", """
INSERT INTO retry_queue (error_id, workflow_name, workflow_id, operation, payload, next_attempt_at)
SELECT $1::uuid, $2::text, $3::text, 'RERUN_EXECUTION', jsonb_build_object('execution_id', $4::text), NOW() + INTERVAL '1 minute'
WHERE NOT EXISTS (SELECT 1 FROM retry_queue WHERE payload->>'execution_id' = $4::text)
RETURNING id;""", ["$json.error_id", f"{C}.workflow_name", f"{C}.workflow_id", f"{C}.execution_id"])
    slack = w.slack("SEND_SLACK_ALERT", "$json.alert_channel || '#roofradar-alerts'",
                    f"'🔴 ' + {C}.severity + ' · ' + {C}.category + '\\n' + "
                    f"'Workflow: ' + {C}.workflow_name + '\\nNode: ' + ({C}.node_name || 'n/a') + "
                    f"'\\nError: ' + {C}.message.slice(0, 500) + ({C}.execution_url ? '\\n' + {C}.execution_url : '')")
    crit = w.sql("LOG_CRITICAL_ERROR", "UPDATE errors SET alerted = true WHERE id = $1::uuid RETURNING id;",
                 ["$('SAVE_ERROR').first().json.error_id"])
    w.chain(trig, norm, cls, save, isr)
    first = w.if_("NOT_A_DUPLICATE?", "$('SAVE_ERROR').first().json.duplicate !== true")
    w.connect(isr, enq, 0)
    w.connect(isr, first, 1)
    w.connect(first, slack, 0)
    w.connect(slack, crit)
    w.note("""## 19 · Error Handler
Set as **Error Workflow** on every other RoofRadar workflow (deploy script does it).
Retryable: 429 · 5xx · timeouts · transient DB. Never retried: 400/401/403, invalid phone/body,
opted-out recipients, double-booking, manual runs. Retryable errors go to workflow 20; the rest alert Slack.""", width=600)
    return w


# =====================================================================
def wf20():
    w = WF("20", "RR 20 · Retry Queue",
           "Every minute: due retries → n8n API re-runs the failed execution from the failed node → backoff 1/5/30 min → dead letter")
    trig = w.schedule("SCHEDULE_TRIGGER", minutes=1)
    due = w.sql("GET_DUE_RETRIES", """
UPDATE retry_queue SET status = 'PROCESSING', attempts = attempts + 1
WHERE id IN (
  SELECT id FROM retry_queue
  WHERE status = 'PENDING' AND next_attempt_at <= NOW()
  ORDER BY next_attempt_at
  LIMIT 5
  FOR UPDATE SKIP LOCKED)
RETURNING *, (SELECT value #>> '{}' FROM app_config WHERE key = 'n8n_base_url') AS n8n_base_url;""", gate="id")
    one = w.code("PROCESS_ONE", r"""
const r = $json;
const base = String(r.n8n_base_url || '').replace(/\/+$/, '');
if (!base) throw new Error('VALIDATION: app_config.n8n_base_url is not set');
return { json: { queue_id: r.id, attempts: r.attempts, max_attempts: r.max_attempts,
                 url: `${base}/api/v1/executions/${r.payload.execution_id}/retry`,
                 execution_id: r.payload.execution_id, workflow_name: r.workflow_name } };""", each=True)
    op = w.http("EXECUTE_OPERATION", "POST", "={{ $json.url }}",
                body_expr="={{ JSON.stringify({ loadWorkflow: true }) }}", cred_key="N8N_API",
                full_response=True, never_error=True, retry=False, onError="continueRegularOutput")
    ok = w.if_("SUCCESS?", "$json.statusCode >= 200 && $json.statusCode < 300 && ['success'].includes(($json.body || {}).status)")
    done = w.sql("MARK_COMPLETE", "UPDATE retry_queue SET status = 'COMPLETE', last_error = NULL WHERE id = $1::uuid RETURNING id;",
                 ["$('PROCESS_ONE').item.json.queue_id"])
    inc = w.sql("INCREMENT_ATTEMPT", """
UPDATE retry_queue SET status = 'PENDING', last_error = $2::text
WHERE id = $1::uuid
RETURNING id, attempts, max_attempts, workflow_name, payload;""",
                ["$('PROCESS_ONE').item.json.queue_id",
                 "String(($json.body && ($json.body.status || $json.body.message)) || $json.statusCode || $json.error || 'unknown').slice(0, 500)"])
    maxed = w.if_("ATTEMPTS >= 3?", "$json.attempts >= $json.max_attempts")
    dead = w.sql("DEAD_LETTER", f"""
UPDATE retry_queue SET status = 'DEAD_LETTER' WHERE id = $1::uuid
RETURNING id, workflow_name, payload, last_error, {ALERT_CH} AS alert_channel;""", ["$json.id"])
    alert = w.slack("ALERT_DEAD_LETTER", "$json.alert_channel || '#roofradar-alerts'",
                    "'☠️ DEAD LETTER after 3 retries\\nWorkflow: ' + $json.workflow_name + '\\nExecution: ' + $json.payload.execution_id + '\\nLast error: ' + $json.last_error")
    back = w.sql("CALCULATE_BACKOFF", """
UPDATE retry_queue
SET next_attempt_at = NOW() + CASE attempts WHEN 1 THEN INTERVAL '5 minutes' ELSE INTERVAL '30 minutes' END
WHERE id = $1::uuid RETURNING id, next_attempt_at;""", ["$json.id"])
    w.chain(trig, due, one, op, ok)
    w.connect(ok, done, 0)
    w.connect(ok, inc, 1)
    w.connect(inc, maxed)
    w.connect(maxed, dead, 0)
    w.connect(maxed, back, 1)
    w.connect(dead, alert)
    w.note("""## 20 · Retry Queue
Backoff: 1st retry +1 min (set by 19) · 2nd +5 min · 3rd +30 min → DEAD_LETTER + Slack.
Uses the n8n public API `POST /executions/{id}/retry` (credential **RoofRadar n8n API**), which resumes from the failed
node — upstream side effects (a sent SMS, a created event) are not repeated.""", width=600)
    return w


# =====================================================================
def wf21():
    w = WF("21", "RR 21 · Consent", "Opt-out / opt-in ledger. STOP wins everywhere, instantly")
    trig = w.sub_trigger()
    norm = w.code("NORMALIZE", f"""const t = {T};
const channel = t.channel === 'voice' ? 'all' : (t.channel || 'sms');
return [{{ json: {{ lead_id: t.lead_id, channel, text: String(t.body || '').trim(), action: t.action || null, source: t.source || 'unknown' }} }}];""")
    cls = w.sql("CLASSIFY", """
SELECT CASE
         WHEN $2::text = 'OPT_OUT' OR rr_is_opt_out($1::text) THEN 'OPT_OUT'
         WHEN $2::text = 'OPT_IN'  OR rr_is_opt_in($1::text)  THEN 'OPT_IN'
         ELSE 'NONE'
       END AS consent_action;""", ["$json.text", "$json.action || ''"])
    N = "$('NORMALIZE').first().json"
    upd = w.sql("UPDATE_CONSENT", """
INSERT INTO consents (lead_id, channel, consent_status, source, metadata)
SELECT $1::uuid, $2::text, CASE WHEN $3::text = 'OPT_OUT' THEN 'OPTED_OUT' ELSE 'OPTED_IN' END, $4::text,
       jsonb_build_object('text', $5::text)
WHERE $3::text IN ('OPT_OUT','OPT_IN')
RETURNING id, consent_status, $3::text AS consent_action;""",
                [f"{N}.lead_id", f"{N}.channel", "$json.consent_action", f"{N}.source", f"{N}.text"], gate="id")
    stop = w.if_("STOP?", "$json.consent_action === 'OPT_OUT'")
    dis = w.sql("DISABLE_AI_OUTREACH", """
WITH l AS (UPDATE leads SET status = 'STOPPED' WHERE id = $1::uuid RETURNING organization_id),
     c AS (UPDATE conversations SET ai_active = false WHERE lead_id = $1::uuid AND status = 'ACTIVE' RETURNING id),
     f AS (UPDATE followups SET status = 'CANCELLED', skip_reason = 'OPTED_OUT' WHERE lead_id = $1::uuid AND status = 'PENDING' RETURNING id)
SELECT rr_log_event((SELECT organization_id FROM l), $1::uuid, '21_CONSENT', 'OPTED_OUT',
       jsonb_build_object('source', $2::text, 'paused_conversations', (SELECT count(*) FROM c), 'cancelled_followups', (SELECT count(*) FROM f))) AS event_id,
       'OPTED_OUT' AS consent_status, true AS stopped;""", [f"{N}.lead_id", f"{N}.source"])
    log = w.sql("LOG_CONSENT", """
WITH l AS (
  UPDATE leads SET status = CASE WHEN status = 'STOPPED' THEN 'CONTACTED' ELSE status END
  WHERE id = $1::uuid AND $2::text = 'OPT_IN' RETURNING organization_id
), c AS (
  UPDATE conversations SET ai_active = true
  WHERE lead_id = $1::uuid AND status = 'ACTIVE' AND $2::text = 'OPT_IN'
    AND NOT EXISTS (SELECT 1 FROM leads WHERE id = $1::uuid AND human_handoff) RETURNING id
)
SELECT rr_log_event((SELECT organization_id FROM leads WHERE id = $1::uuid), $1::uuid, '21_CONSENT', 'CONSENT_' || $2::text,
       jsonb_build_object('reactivated', (SELECT count(*) FROM l), 'ai_resumed', (SELECT count(*) FROM c))) AS event_id,
       $2::text AS consent_status, false AS stopped;""", [f"{N}.lead_id", "$('CLASSIFY').first().json.consent_action"])
    w.chain(trig, norm, cls, upd, stop)
    w.connect(stop, dis, 0)
    w.connect(stop, log, 1)
    w.note("""## 21 · Consent
Called from 03/08 (keyword gate, before any AI), 04 (router STOP), 24A (voice opt-out).
Keywords: STOP · STOPALL · UNSUBSCRIBE · CANCEL · END · QUIT · OPT OUT · "do not contact me" (see `rr_is_opt_out`).
Twilio also auto-replies to STOP/START on standard numbers — we record, we don't double-reply.
`rr_can_contact()` is re-checked by 07 at send time.""", width=600)
    return w


# =====================================================================
def wf22():
    w = WF("22", "RR 22 · Monitoring", "Every 5 min health check: workflows, Twilio, database, calendar, OpenAI → alert on change")
    trig = w.schedule("SCHEDULE_TRIGGER", minutes=5)
    wf = w.sql("CHECK_WORKFLOW_FAILURES", """
SELECT
  (SELECT count(*) FROM errors WHERE created_at > NOW() - INTERVAL '15 minutes')::int AS errors_15m,
  (SELECT count(*) FROM errors WHERE created_at > NOW() - INTERVAL '15 minutes' AND category = 'AUTHENTICATION')::int AS auth_errors_15m,
  (SELECT count(*) FROM retry_queue WHERE status = 'DEAD_LETTER' AND updated_at > NOW() - INTERVAL '24 hours')::int AS dead_letters_24h,
  (SELECT count(*) FROM retry_queue WHERE status = 'PENDING')::int AS retries_pending;""", always=True)
    tw = w.http("CHECK_TWILIO", "GET", "https://status.twilio.com/api/v2/status.json",
                full_response=True, never_error=True, retry=False, onError="continueRegularOutput", alwaysOutputData=True)
    db = w.sql("CHECK_DATABASE", """
SELECT 1 AS ok, pg_database_size(current_database()) AS db_bytes,
  (SELECT count(*) FROM pg_stat_activity)::int AS connections,
  (SELECT count(*) FROM messages WHERE direction = 'OUTBOUND' AND created_at > NOW() - INTERVAL '1 hour'
      AND delivery_status IN ('failed','undelivered'))::int AS failed_sms_1h,
  (SELECT count(*) FROM human_handoffs WHERE status = 'OPEN' AND created_at < NOW() - INTERVAL '2 hours')::int AS stale_handoffs;""",
                always=True, onError="continueRegularOutput")
    cal = w.http("CHECK_CALENDAR", "GET", "https://www.googleapis.com/calendar/v3/users/me/calendarList",
                 cred_key="GCAL", query={"maxResults": "1"}, full_response=True, never_error=True, retry=False,
                 onError="continueRegularOutput", alwaysOutputData=True)
    oa = w.http("CHECK_CREDENTIALS_API", "GET", "https://api.openai.com/v1/models", cred_key="OPENAI",
                full_response=True, never_error=True, retry=False, onError="continueRegularOutput", alwaysOutputData=True)
    calc = w.code("CALCULATE_HEALTH", r"""
const get = n => { try { return $(n).first().json; } catch (e) { return { error: String(e) }; } };
const wf = get('CHECK_WORKFLOW_FAILURES'), tw = get('CHECK_TWILIO'), db = get('CHECK_DATABASE'),
      cal = get('CHECK_CALENDAR'), oa = get('CHECK_CREDENTIALS_API');
const http = (r, name) => {
  if (r.error) return ['CRITICAL', `${name}: ${String(r.error.message || r.error).slice(0, 200)}`];
  const c = Number(r.statusCode || 0);
  if (c >= 200 && c < 300) return ['HEALTHY', `${name}: HTTP ${c}`];
  if (c === 401 || c === 403) return ['CRITICAL', `${name}: credentials rejected (HTTP ${c})`];
  if (c === 429) return ['WARNING', `${name}: rate limited`];
  return ['CRITICAL', `${name}: HTTP ${c || 'no response'}`];
};
const rows = [];
// Twilio: public status page + our own delivery failures
let [ts, td] = http(tw, 'Twilio status page');
const ind = tw.body?.status?.indicator;
if (ts === 'HEALTHY' && ind && ind !== 'none') { ts = ind === 'minor' ? 'WARNING' : 'CRITICAL'; td = 'Twilio incident: ' + tw.body.status.description; }
if (ts === 'HEALTHY' && (db.failed_sms_1h || 0) >= 5) { ts = 'WARNING'; td = `${db.failed_sms_1h} failed SMS in the last hour`; }
rows.push({ component: 'TWILIO', status: ts, detail: td });
rows.push(db.ok === 1
  ? { component: 'DATABASE', status: (db.stale_handoffs || 0) > 0 ? 'WARNING' : 'HEALTHY',
      detail: `${db.connections} connections, ${(db.db_bytes / 1048576).toFixed(1)} MB` + ((db.stale_handoffs || 0) ? `, ${db.stale_handoffs} handoffs open > 2h` : '') }
  : { component: 'DATABASE', status: 'CRITICAL', detail: String(db.error?.message || db.error || 'query failed') });
const [cs, cd] = http(cal, 'Google Calendar'); rows.push({ component: 'CALENDAR', status: cs, detail: cd });
const [os, od] = http(oa, 'OpenAI'); rows.push({ component: 'OPENAI', status: os, detail: od });
let wstat = 'HEALTHY';
if ((wf.auth_errors_15m || 0) > 0 || (wf.errors_15m || 0) >= 10) wstat = 'CRITICAL';
else if ((wf.errors_15m || 0) > 0 || (wf.dead_letters_24h || 0) > 0) wstat = 'WARNING';
rows.push({ component: 'WORKFLOWS', status: wstat,
            detail: `${wf.errors_15m ?? '?'} errors/15m, ${wf.dead_letters_24h ?? '?'} dead letters/24h, ${wf.retries_pending ?? '?'} retries pending` });
const rank = { HEALTHY: 0, WARNING: 1, CRITICAL: 2 };
const overall = rows.reduce((a, r) => rank[r.status] > rank[a] ? r.status : a, 'HEALTHY');
return [{ json: { rows, overall } }];""")
    save = w.sql("SAVE_MONITORING_RESULT", f"""
WITH run AS (SELECT gen_random_uuid() AS run_id),
prev AS (
  SELECT coalesce(max(CASE status WHEN 'CRITICAL' THEN 2 WHEN 'WARNING' THEN 1 ELSE 0 END), 0) AS lvl
  FROM monitoring_checks
  WHERE run_id = (SELECT run_id FROM monitoring_checks ORDER BY checked_at DESC LIMIT 1)
), ins AS (
  INSERT INTO monitoring_checks (run_id, component, status, detail)
  SELECT (SELECT run_id FROM run), r.component, r.status, r.detail
  FROM jsonb_to_recordset($1::jsonb) AS r(component text, status text, detail text)
  RETURNING 1
)
SELECT (SELECT count(*) FROM ins) AS saved,
       CASE (SELECT lvl FROM prev) WHEN 2 THEN 'CRITICAL' WHEN 1 THEN 'WARNING' ELSE 'HEALTHY' END AS previous_overall,
       $2::text AS overall, {ALERT_CH} AS alert_channel;""",
                 ["JSON.stringify($json.rows)", "$json.overall"])
    alert = w.if_("ALERT_IF_FAILURE", "$json.overall !== $json.previous_overall")
    slack = w.slack("SEND_HEALTH_ALERT", "$json.alert_channel || '#roofradar-alerts'",
                    "($json.overall === 'HEALTHY' ? '✅ RoofRadar recovered — all systems HEALTHY' : ($json.overall === 'CRITICAL' ? '🔴' : '🟠') + ' RoofRadar health: ' + $json.overall) + '\\n' + "
                    "$('CALCULATE_HEALTH').first().json.rows.map(r => (r.status === 'HEALTHY' ? '🟢' : r.status === 'WARNING' ? '🟠' : '🔴') + ' ' + r.component.padEnd(10) + ' ' + r.status + ' — ' + r.detail).join('\\n')")
    w.chain(trig, wf, tw, db, cal, oa, calc, save, alert)
    w.connect(alert, slack, 0)
    return w


# =====================================================================
def wf23():
    w = WF("23", "RR 23 · Data Maintenance", "02:00 daily: expire sessions, prune temp data, archive, flag duplicates & orphans, DB health")
    trig = w.schedule("SCHEDULE_TRIGGER", cron="0 2 * * *")
    s1 = w.sql("CLEAN_EXPIRED_SESSIONS", """
WITH c AS (UPDATE conversations SET status = 'EXPIRED'
           WHERE status = 'ACTIVE' AND last_message_at < NOW() - INTERVAL '30 days' RETURNING id),
     v AS (UPDATE voice_sessions SET status = 'ABANDONED', ended_at = coalesce(ended_at, NOW())
           WHERE status = 'IN_PROGRESS' AND started_at < NOW() - INTERVAL '3 hours' RETURNING id)
SELECT (SELECT count(*) FROM c)::int AS expired_conversations, (SELECT count(*) FROM v)::int AS abandoned_voice_sessions;""")
    s2 = w.sql("CLEAN_OLD_TEMP_DATA", """
WITH a AS (DELETE FROM monitoring_checks WHERE checked_at < NOW() - INTERVAL '30 days' RETURNING 1),
     b AS (DELETE FROM retry_queue WHERE status = 'COMPLETE' AND updated_at < NOW() - INTERVAL '30 days' RETURNING 1),
     c AS (DELETE FROM workflow_events WHERE created_at < NOW() - INTERVAL '180 days' RETURNING 1),
     d AS (UPDATE leads SET metadata = metadata - 'offered_slots' - 'offered_at'
           WHERE metadata ? 'offered_at' AND (metadata->>'offered_at')::timestamptz < NOW() - INTERVAL '7 days' RETURNING 1)
SELECT (SELECT count(*) FROM a)::int AS monitoring_rows_deleted, (SELECT count(*) FROM b)::int AS retries_deleted,
       (SELECT count(*) FROM c)::int AS events_deleted, (SELECT count(*) FROM d)::int AS stale_offers_cleared;""")
    s3 = w.sql("ARCHIVE_OLD_MESSAGES", """
WITH m AS (UPDATE messages SET archived = true WHERE NOT archived AND created_at < NOW() - INTERVAL '365 days' RETURNING 1)
SELECT (SELECT count(*) FROM m)::int AS archived_messages;""")
    s4 = w.sql("CHECK_DUPLICATES", """
WITH d AS (
  SELECT organization_id, lower(email) AS email, array_agg(id ORDER BY created_at) AS lead_ids, count(*) AS n
  FROM leads WHERE email IS NOT NULL
  GROUP BY organization_id, lower(email) HAVING count(*) > 1
), flagged AS (
  INSERT INTO workflow_events (organization_id, workflow, event, payload)
  SELECT organization_id, '23_MAINTENANCE', 'DUPLICATE_SUSPECTED', jsonb_build_object('email', email, 'lead_ids', lead_ids)
  FROM d
  WHERE NOT EXISTS (SELECT 1 FROM workflow_events e WHERE e.event = 'DUPLICATE_SUSPECTED'
                    AND e.payload->>'email' = d.email AND e.created_at > NOW() - INTERVAL '7 days')
  RETURNING 1
)
SELECT (SELECT count(*) FROM d)::int AS duplicate_groups, (SELECT count(*) FROM flagged)::int AS newly_flagged;""")
    s5 = w.sql("CHECK_ORPHAN_RECORDS", """
SELECT
  (SELECT count(*) FROM followups f JOIN leads l ON l.id = f.lead_id
    WHERE f.status = 'PENDING' AND l.status IN ('STOPPED','BOOKED','DISQUALIFIED'))::int AS followups_for_closed_leads,
  (SELECT count(*) FROM appointments a JOIN leads l ON l.id = a.lead_id
    WHERE a.status IN ('SCHEDULED','CONFIRMED') AND a.start_at > NOW() AND l.status IN ('STOPPED','LOST'))::int AS future_appts_for_closed_leads,
  (SELECT count(*) FROM appointments WHERE status IN ('SCHEDULED','CONFIRMED') AND external_event_id IS NULL)::int AS appts_without_calendar_event,
  (SELECT count(*) FROM appointments WHERE status IN ('SCHEDULED','CONFIRMED') AND end_at < NOW() - INTERVAL '1 day')::int AS past_appts_not_closed,
  (SELECT count(*) FROM human_handoffs WHERE status = 'OPEN' AND created_at < NOW() - INTERVAL '24 hours')::int AS handoffs_open_over_24h;""")
    s6 = w.sql("DATABASE_HEALTH", """
SELECT pg_database_size(current_database()) AS db_bytes,
  (SELECT coalesce(jsonb_object_agg(relname, n_live_tup), '{}'::jsonb) FROM pg_stat_user_tables
    WHERE relname IN ('leads','messages','conversations','appointments','ai_interactions','workflow_events')) AS row_counts,
  (SELECT coalesce(max(n_dead_tup), 0) FROM pg_stat_user_tables) AS max_dead_tuples;""")
    log = w.code("LOG_MAINTENANCE", r"""
const r = {};
for (const n of ['CLEAN_EXPIRED_SESSIONS','CLEAN_OLD_TEMP_DATA','ARCHIVE_OLD_MESSAGES','CHECK_DUPLICATES','CHECK_ORPHAN_RECORDS','DATABASE_HEALTH'])
  Object.assign(r, $(n).first().json);
const anomalies = [];
if (r.newly_flagged) anomalies.push(`${r.newly_flagged} new duplicate-lead groups (flagged, not deleted)`);
if (r.followups_for_closed_leads) anomalies.push(`${r.followups_for_closed_leads} pending follow-ups for closed leads`);
if (r.future_appts_for_closed_leads) anomalies.push(`${r.future_appts_for_closed_leads} future appointments for STOPPED/LOST leads`);
if (r.appts_without_calendar_event) anomalies.push(`${r.appts_without_calendar_event} appointments without a calendar event`);
if (r.past_appts_not_closed) anomalies.push(`${r.past_appts_not_closed} past appointments still SCHEDULED (mark COMPLETED/NO_SHOW)`);
if (r.handoffs_open_over_24h) anomalies.push(`${r.handoffs_open_over_24h} human handoffs open > 24h`);
return [{ json: { report: r, anomalies } }];""")
    save = w.sql("SAVE_MAINTENANCE_LOG", f"""
SELECT rr_log_event(NULL, NULL, '23_MAINTENANCE', 'MAINTENANCE_RUN', $1::jsonb) AS id, {ALERT_CH} AS alert_channel;""",
                 ["JSON.stringify($json)"])
    anom = w.if_("ANOMALIES?", "$('LOG_MAINTENANCE').first().json.anomalies.length > 0")
    slack = w.slack("ALERT_ANOMALIES", "$json.alert_channel || '#roofradar-alerts'",
                    "'🧹 Nightly maintenance found:\\n• ' + $('LOG_MAINTENANCE').first().json.anomalies.join('\\n• ')")
    w.chain(trig, s1, s2, s3, s4, s5, s6, log, save, anom)
    w.connect(anom, slack, 0)
    return w

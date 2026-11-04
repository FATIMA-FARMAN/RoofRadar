"""Workflows 01, 02, 03, 08 — lead & message intake."""
from lib import WF, ORG_DEFAULT

PHONE_FN = r"""
// E.164 normaliser. Assumes US/Canada (+1) when a bare 10/11-digit number arrives.
function normalizePhone(phone) {
  if (!phone) return null;
  let p = String(phone).trim().replace(/^whatsapp:/i, '').replace(/[^\d+]/g, '');
  if (p.startsWith('00')) p = '+' + p.substring(2);
  if (!p.startsWith('+') && p.length === 10) p = '+1' + p;
  if (!p.startsWith('+') && p.length === 11 && p.startsWith('1')) p = '+' + p;
  return /^\+[1-9]\d{7,14}$/.test(p) ? p : null;
}
"""

UPDATE_LEAD_SQL = """
UPDATE leads SET
  name         = COALESCE(NULLIF($2::text,''), name),
  email        = COALESCE(NULLIF($3::text,''), email),
  address      = COALESCE(NULLIF($4::text,''), address),
  problem_type = COALESCE(NULLIF($5::text,''), problem_type),
  phone        = COALESCE(phone, NULLIF($6::text,'')),
  external_id  = COALESCE(external_id, NULLIF($8::text,'')),
  first_message = COALESCE(first_message, NULLIF($7::text,'')),
  metadata     = metadata || jsonb_build_object('last_form_message', $7::text, 'last_form_at', NOW()),
  status       = CASE WHEN status IN ('LOST','DISQUALIFIED') THEN 'NEW' ELSE status END
WHERE id = $1::uuid
RETURNING *, false AS is_new;
"""

CREATE_LEAD_SQL = """
INSERT INTO leads (organization_id, name, phone, email, address, source, problem_type,
                   first_message, external_id, status, preferred_channel)
VALUES ($1::uuid, NULLIF($2::text,''), NULLIF($3::text,''), NULLIF($4::text,''), NULLIF($5::text,''),
        $6::text, NULLIF($7::text,''), NULLIF($8::text,''), NULLIF($9::text,''), 'NEW', 'sms')
ON CONFLICT (organization_id, phone) WHERE phone IS NOT NULL
DO UPDATE SET updated_at = NOW()
RETURNING *, (xmax = 0) AS is_new;
"""

POST_UPSERT_SQL = """
WITH c AS (
  INSERT INTO consents (lead_id, channel, consent_status, source, metadata)
  SELECT $1::uuid, 'sms', 'OPTED_IN', $3::text, jsonb_build_object('disclosure', $4::text)
  WHERE $2::boolean
  RETURNING id
), n AS (
  SELECT CASE WHEN $5::boolean THEN rr_schedule_nurture($1::uuid) ELSE 0 END AS scheduled
), e AS (
  SELECT rr_log_event($6::uuid, $1::uuid, $7::text,
         CASE WHEN $5::boolean THEN 'LEAD_CREATED' ELSE 'LEAD_UPDATED' END, $8::jsonb) AS event_id
)
SELECT $1::uuid AS lead_id, $6::uuid AS organization_id, $5::boolean AS is_new,
       (SELECT scheduled FROM n) AS followups_scheduled,
       (SELECT count(*) FROM c)::int AS consent_rows,
       (SELECT event_id FROM e) AS event_id,
       rr_can_contact($1::uuid, 'sms') AS can_contact;
"""


def lead_upsert_tail(w, src_node, org_node, workflow_label, source_value, consent_expr, consent_source):
    """FIND -> EXISTS? -> UPDATE|CREATE -> POST_UPSERT -> SHOULD_TEXT? -> 06 FIRST_CONTACT.
    src_node: node whose json holds normalised lead fields (use .item for multi-lead batches)."""
    S = f"$('{src_node}').item.json"
    O = f"$('{org_node}').item.json"
    find = w.sql("FIND_EXISTING_LEAD", """
SELECT *
FROM leads
WHERE organization_id = $1::uuid
  AND (
        phone = $2::text
     OR ($3::text IS NOT NULL AND lower(email) = lower($3::text))
     OR ($4::text IS NOT NULL AND external_id = $4::text)
  )
ORDER BY (phone = $2::text) DESC NULLS LAST, created_at
LIMIT 1;
""", [f"{O}.id", f"{S}.phone_e164", f"{S}.email", f"{S}.external_id"], always=True)
    exists = w.if_("LEAD_EXISTS?", "!!$json.id")
    upd = w.sql("UPDATE_LEAD", UPDATE_LEAD_SQL,
                ["$json.id", f"{S}.name", f"{S}.email", f"{S}.address", f"{S}.problem_type",
                 f"{S}.phone_e164", f"{S}.message", f"{S}.external_id"])
    crt = w.sql("CREATE_LEAD", CREATE_LEAD_SQL,
                [f"{O}.id", f"{S}.name", f"{S}.phone_e164", f"{S}.email", f"{S}.address",
                 f"'{source_value}'", f"{S}.problem_type", f"{S}.message", f"{S}.external_id"])
    post = w.sql("LOG_EVENT", POST_UPSERT_SQL,
                 ["$json.id", consent_expr, f"'{consent_source}'", f"{S}.consent_text || ''",
                  "$json.is_new", "$json.organization_id", f"'{workflow_label}'",
                  f"JSON.stringify({{source: '{source_value}', problem_type: {S}.problem_type, message: {S}.message}})"])
    should = w.if_("SHOULD_TEXT_NOW?",
                   f"$json.is_new && $json.can_contact && !!{S}.phone_e164 && ({consent_expr})")
    prep = w.code("PREP_FIRST_CONTACT", """
return { json: {
  lead_id: $json.lead_id,
  organization_id: $json.organization_id,
  channel: 'sms',
  mode: 'FIRST_CONTACT',
  inbound_text: $('%s').item.json.message || null
}};""".strip() % src_node, each=True)
    ex = w.call("EXECUTE_06_AI_CONVERSATION", "06")
    w.connect(find, exists)
    w.connect(exists, upd, 0)
    w.connect(exists, crt, 1)
    w.connect(upd, post)
    w.connect(crt, post)
    w.connect(post, should)
    w.connect(should, prep, 0)
    w.connect(prep, ex)
    return find


# =====================================================================
def wf01():
    w = WF("01", "RR 01 · Inbound Website Lead",
           "Website form → normalise → validate → find/update/create lead → consent + nurture → instant first SMS")
    hook = w.webhook("WEBSITE_LEAD_WEBHOOK", "primehome-lead")
    norm = w.code("NORMALIZE_LEAD", PHONE_FN + r"""
const b = $json.body ?? $json;
const clean = v => (v === undefined || v === null) ? null : (String(v).trim() || null);
const email = clean(b.email);
const phone_e164 = normalizePhone(b.phone);
const errors = [];
if (!clean(b.name)) errors.push('name is required');
if (!phone_e164 && !email) errors.push('a valid phone (E.164) or email is required');
if (email && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) errors.push('email is invalid');
if (clean(b.website)) errors.push('honeypot filled (spam)');           // hidden field bots fill in
const consent = b.sms_consent === true || b.sms_consent === 'true' || b.sms_consent === 'on';
return { json: {
  org_slug: clean(b.org) || 'primehome',
  name: clean(b.name),
  phone_e164,
  phone_raw: clean(b.phone),
  email: email ? email.toLowerCase() : null,
  address: clean(b.address),
  problem_type: clean(b.problem_type),
  message: clean(b.message),
  external_id: null,
  sms_consent: consent,
  consent_text: clean(b.consent_text) || (consent ? 'Website form SMS consent checkbox' : null),
  valid: errors.length === 0,
  errors
}};""", each=True)   # per-item mode keeps paired-item links for $('NORMALIZE_LEAD').item
    valid = w.if_("VALIDATE_LEAD", "$json.valid")
    bad = w.sql("LOG_INVALID_LEAD",
                "SELECT rr_log_event(NULL, NULL, '01_INBOUND_WEBSITE', 'INVALID_LEAD', $1::jsonb) AS id;",
                ["JSON.stringify($json)"])
    org = w.sql("RESOLVE_ORG", "SELECT id, name, settings FROM organizations WHERE slug = $1::text AND active LIMIT 1;",
                ["$json.org_slug"])
    w.chain(hook, norm, valid)
    w.connect(valid, org, 0)
    w.connect(valid, bad, 1)
    find = lead_upsert_tail(w, "NORMALIZE_LEAD", "RESOLVE_ORG", "01_INBOUND_WEBSITE", "website",
                            "$('NORMALIZE_LEAD').item.json.sms_consent", "web_form")
    w.connect(org, find)
    w.note("""## 01 · Inbound Website
POST `/webhook/primehome-lead` with `name, phone, email, address, problem_type, message, sms_consent`.
Duplicate protection: phone (unique index) → email → external id.
New lead + SMS consent → **06 FIRST_CONTACT** texts them within seconds.""")
    return w


# =====================================================================
def wf02():
    w = WF("02", "RR 02 · Inbound Meta Lead Ads",
           "Facebook/Instagram lead ads webhook → Graph API fetch → same duplicate logic as 01")
    # --- verification handshake (GET) ---
    vhook = w.webhook("META_VERIFY_WEBHOOK", "roofradar/meta", method="GET", respond="responseNode")
    vtok = w.sql("GET_VERIFY_TOKEN", """
SELECT settings->'meta'->>'verify_token' AS token
FROM organizations WHERE settings->'meta'->>'verify_token' = $1::text LIMIT 1;""",
                 ["$json.query['hub.verify_token'] || ''"], always=True)
    vok = w.if_("TOKEN_MATCHES?", "!!$json.token && $('META_VERIFY_WEBHOOK').first().json.query['hub.mode'] === 'subscribe'")
    r_ok = w.respond("RESPOND_CHALLENGE", "={{ $('META_VERIFY_WEBHOOK').first().json.query['hub.challenge'] }}",
                     content_type="text/plain")
    r_no = w.respond("RESPOND_FORBIDDEN", "forbidden", content_type="text/plain", code=403)
    w.chain(vhook, vtok, vok)
    w.connect(vok, r_ok, 0)
    w.connect(vok, r_no, 1)

    # --- lead events (POST) ---
    hook = w.webhook("META_LEAD_TRIGGER", "roofradar/meta")
    ext = w.code("EXTRACT_LEADGEN_IDS", r"""
const body = $json.body ?? $json;
const out = [];
for (const entry of body.entry || []) {
  for (const ch of entry.changes || []) {
    if (ch.field === 'leadgen' && ch.value?.leadgen_id) {
      out.push({ json: {
        leadgen_id: String(ch.value.leadgen_id),
        page_id: String(ch.value.page_id || entry.id || ''),
        form_id: ch.value.form_id ? String(ch.value.form_id) : null,
        ad_id: ch.value.ad_id ? String(ch.value.ad_id) : null
      }});
    }
  }
}
return out;   // zero items → workflow ends quietly""")
    org = w.sql("RESOLVE_ORG", """
SELECT id, name, settings FROM organizations
WHERE settings->'meta'->>'page_id' = $1::text AND active LIMIT 1;""", ["$json.page_id"])
    get = w.http("GET_META_LEAD", "GET",
                 "=https://graph.facebook.com/v23.0/{{ $('EXTRACT_LEADGEN_IDS').item.json.leadgen_id }}",
                 cred_key="META", query={"fields": "id,created_time,field_data,form_id,ad_id"})
    norm = w.code("NORMALIZE_META_LEAD", PHONE_FN + r"""
const fields = {};
for (const f of $json.field_data || []) fields[f.name] = f.values?.[0] ?? null;
const email = fields.email ? String(fields.email).toLowerCase().trim() : null;
const phone_e164 = normalizePhone(fields.phone_number || fields.phone);
const name = fields.full_name || [fields.first_name, fields.last_name].filter(Boolean).join(' ') || fields.name || null;
const org = $('RESOLVE_ORG').item.json;
const consent = !!(org.settings?.meta?.sms_consent_in_form);
return { json: {
  external_id: String($json.id),
  name, email, phone_e164,
  address: fields.street_address || fields.address || [fields.city, fields.state].filter(Boolean).join(', ') || null,
  problem_type: fields.problem_type || fields['what_do_you_need_help_with?'] || null,
  message: fields.message || fields.details || null,
  source: 'facebook',
  received_at: $json.created_time,
  sms_consent: consent,
  consent_text: consent ? 'Meta lead form consent disclaimer' : null,
  valid: !!name && (!!phone_e164 || !!email)
}};""", each=True)
    valid = w.if_("VALIDATE", "$json.valid")
    bad = w.sql("LOG_INVALID_META_LEAD",
                "SELECT rr_log_event($1::uuid, NULL, '02_INBOUND_META', 'INVALID_LEAD', $2::jsonb) AS id;",
                ["$('RESOLVE_ORG').item.json.id", "JSON.stringify($json)"])
    w.chain(hook, ext, org, get, norm, valid)
    w.connect(valid, bad, 1)
    find = lead_upsert_tail(w, "NORMALIZE_META_LEAD", "RESOLVE_ORG", "02_INBOUND_META", "facebook",
                            "$('NORMALIZE_META_LEAD').item.json.sms_consent", "meta_lead_form")
    w.connect(valid, find, 0)
    # rename per spec
    w.note("""## 02 · Inbound Meta
Subscribe your Page to the **leadgen** webhook field and point it at `/webhook/roofradar/meta`
(GET = verification using `settings.meta.verify_token`, POST = lead events).
The payload only carries a `leadgen_id`; we fetch the real lead from the Graph API with *our* token,
so a forged webhook cannot inject data. Set `settings.meta.sms_consent_in_form=true` only if your
form includes an SMS consent disclaimer.""")
    return w


# =====================================================================
def inbound_message_wf(key, name, path, channel):
    w = WF(key, name, f"Twilio {channel} webhook → lead + conversation → idempotent save → consent gate → 04 AI Router")
    hook = w.webhook(f"TWILIO_{channel.upper()}_INBOUND", path, respond="responseNode", auth="basic")
    twiml = w.respond("RESPOND_TWIML", "<Response></Response>", content_type="text/xml")
    norm = w.code(f"NORMALIZE_{channel.upper()}", PHONE_FN + r"""
const b = $json.body ?? $json;
const media = Number(b.NumMedia || 0);
return [{ json: {
  message_sid: b.MessageSid || b.SmsSid || null,
  phone_e164: normalizePhone(b.From),
  business_phone: normalizePhone(b.To),
  body: (b.Body || '').trim() || (media ? '[media message]' : ''),
  num_media: media,
  media_url: b.MediaUrl0 || null,
  profile_name: b.ProfileName || null,
  channel: '%s',
  received_at: new Date().toISOString()
}}];""" % channel)
    valid = w.if_("VALID_SENDER?", "!!$json.phone_e164 && !!$json.message_sid")
    org = w.sql("RESOLVE_ORG", """
SELECT id, name, settings, business_phone, whatsapp_number FROM organizations
WHERE active AND (business_phone = $1::text OR whatsapp_number = $1::text)
LIMIT 1;""", ["$json.business_phone"])
    N = f"$('NORMALIZE_{channel.upper()}').first().json"
    find = w.sql("FIND_LEAD", """
SELECT * FROM leads WHERE organization_id = $1::uuid AND phone = $2::text LIMIT 1;""",
                 ["$json.id", f"{N}.phone_e164"], always=True)
    found = w.if_("LEAD_FOUND?", "!!$json.id")
    create = w.sql("CREATE_LEAD", """
INSERT INTO leads (organization_id, phone, name, source, status, preferred_channel, first_message)
VALUES ($1::uuid, $2::text, NULLIF($3::text,''), $4::text, 'NEW', $4::text, $5::text)
ON CONFLICT (organization_id, phone) WHERE phone IS NOT NULL
DO UPDATE SET updated_at = NOW()
RETURNING *;""", ["$('RESOLVE_ORG').first().json.id", f"{N}.phone_e164", f"{N}.profile_name || ''",
                  f"'{channel}'", f"{N}.body"])
    ready = w.code("LEAD_READY", "return $input.all();  // single merge point: found or created lead row")
    fconv = w.sql("FIND_CONVERSATION", """
SELECT * FROM conversations
WHERE lead_id = $1::uuid AND channel = $2::text AND status = 'ACTIVE'
ORDER BY last_message_at DESC LIMIT 1;""", ["$json.id", f"'{channel}'"], always=True)
    cexists = w.if_("CONVERSATION_EXISTS?", "!!$json.id")
    ccreate = w.sql("CREATE_CONVERSATION_IF_NEEDED", """
INSERT INTO conversations (organization_id, lead_id, channel, status, ai_active, last_message_at)
VALUES ($1::uuid, $2::uuid, $3::text, 'ACTIVE', true, NOW())
ON CONFLICT (lead_id, channel) WHERE status = 'ACTIVE'
DO UPDATE SET last_message_at = NOW()
RETURNING *;""", ["$('RESOLVE_ORG').first().json.id",
                  "$('LEAD_READY').first().json.id",
                  f"'{channel}'"])
    save = w.sql("SAVE_INBOUND_MESSAGE", """
WITH m AS (
  INSERT INTO messages (organization_id, conversation_id, lead_id, external_message_id, direction,
                        channel, sender, recipient, body, ai_generated, delivery_status, metadata)
  VALUES ($1::uuid, $2::uuid, $3::uuid, $4::text, 'INBOUND', $5::text, $6::text, $7::text, $8::text,
          false, 'RECEIVED', $9::jsonb)
  ON CONFLICT (external_message_id) WHERE external_message_id IS NOT NULL DO NOTHING
  RETURNING *
), l AS (
  UPDATE leads SET last_message_at = NOW(), last_inbound_at = NOW(),
         status = CASE WHEN status IN ('NEW','CONTACTED') THEN 'ENGAGED' ELSE status END
  WHERE id = $3::uuid AND EXISTS (SELECT 1 FROM m) RETURNING id
), c AS (
  UPDATE conversations SET last_message_at = NOW()
  WHERE id = $2::uuid AND EXISTS (SELECT 1 FROM m) RETURNING id
), f AS (   -- customer is talking: restart the nurture clock from step 2
  SELECT rr_schedule_nurture($3::uuid, NOW(), 2) AS n WHERE EXISTS (SELECT 1 FROM m)
)
SELECT m.*, (SELECT count(*) FROM l) + (SELECT count(*) FROM c) + coalesce((SELECT n FROM f),0) AS side_effects
FROM m;   -- duplicate Twilio retry → zero rows → workflow stops here""",
                 ["$('RESOLVE_ORG').first().json.id", "$json.id", "$('LEAD_READY').first().json.id",
                  f"{N}.message_sid", f"'{channel}'", f"{N}.phone_e164", f"{N}.business_phone", f"{N}.body",
                  f"JSON.stringify({{num_media: {N}.num_media, media_url: {N}.media_url}})"], gate="id")
    classify = w.sql("CLASSIFY_GATE", """
SELECT
  CASE
    WHEN rr_is_opt_out($1::text)                         THEN 'OPT_OUT'
    WHEN rr_is_opt_in($1::text) AND l.status = 'STOPPED' THEN 'OPT_IN'
    WHEN l.status = 'STOPPED'                            THEN 'STOPPED'
    WHEN l.human_handoff OR NOT c.ai_active              THEN 'PAUSED'
    ELSE 'AI'
  END AS route,
  l.id AS lead_id, l.name, l.phone, l.organization_id, c.id AS conversation_id,
  o.settings->>'slack_channel' AS slack_channel
FROM leads l
JOIN conversations c ON c.id = $3::uuid
JOIN organizations o ON o.id = l.organization_id
WHERE l.id = $2::uuid;""", [f"{N}.body", "$json.lead_id", "$json.conversation_id"])
    sw = w.switch("ROUTE", "$json.route", ["OPT_OUT", "OPT_IN", "STOPPED", "PAUSED", "AI"])
    prep21 = w.code("PREP_CONSENT", """
return [{ json: {
  lead_id: $json.lead_id, organization_id: $json.organization_id, channel: '%s',
  body: %s.body, action: $json.route, source: 'inbound_keyword'
}}];""" % (channel, N))
    c21 = w.call("EXECUTE_21_CONSENT", "21")
    ignore = w.sql("LOG_IGNORED_STOPPED",
                   "SELECT rr_log_event($1::uuid, $2::uuid, $3::text, 'INBOUND_FROM_STOPPED_LEAD', '{}'::jsonb) AS id;",
                   ["$json.organization_id", "$json.lead_id", f"'{key}_INBOUND_{channel.upper()}'"])
    fwd = w.slack("FORWARD_TO_HUMAN", "$json.slack_channel",
                  f"'💬 New {channel} reply (AI paused — human owns this lead)\\n' + ($json.name || 'Unknown') + ' · ' + $json.phone + '\\n> ' + {N}.body")
    prep04 = w.code("PREP_ROUTER_INPUT", """
return [{ json: {
  lead_id: $json.lead_id,
  conversation_id: $json.conversation_id,
  organization_id: $json.organization_id,
  channel: '%s',
  body: %s.body,
  message_sid: %s.message_sid
}}];""" % (channel, N, N))
    r04 = w.call("EXECUTE_04_AI_ROUTER", "04")

    w.chain(hook, twiml, norm, valid)
    w.connect(valid, org, 0)
    w.chain(org, find, found)
    w.connect(found, ready, 0)
    w.connect(found, create, 1)
    w.connect(create, ready)
    w.connect(ready, fconv)
    w.connect(fconv, cexists)
    w.connect(cexists, save, 0)
    w.connect(cexists, ccreate, 1)
    w.connect(ccreate, save)
    w.connect(save, classify)
    w.connect(classify, sw)
    w.connect(sw, prep21, 0)
    w.connect(sw, prep21, 1)
    w.connect(sw, ignore, 2)
    w.connect(sw, fwd, 3)
    w.connect(sw, prep04, 4)
    w.connect(prep21, c21)
    w.connect(prep04, r04)
    w.note(f"""## {key} · Inbound {channel.upper()}
Twilio → `/webhook/{path}` (HTTP Basic auth in the URL: `https://user:pass@your-n8n/webhook/{path}`).
Replies `<Response/>` instantly so Twilio never times out.
**Idempotent:** a retried MessageSid hits the unique index and the run stops.
Opt-out / opt-in keywords are handled deterministically **before** any AI sees the message.""")
    return w


def wf03():
    return inbound_message_wf("03", "RR 03 · Inbound SMS", "roofradar/sms", "sms")


def wf08():
    return inbound_message_wf("08", "RR 08 · Inbound WhatsApp", "roofradar/whatsapp", "whatsapp")

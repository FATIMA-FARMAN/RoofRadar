SET client_min_messages = warning;
-- =====================================================================
-- RoofRadar AI — core schema (PostgreSQL 14+ / Supabase)
-- Source of truth for every workflow. The CRM is a mirror, never primary.
-- Safe to re-run: every object uses IF NOT EXISTS / OR REPLACE.
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;     -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS btree_gist;   -- double-booking exclusion constraint

-- ---------------------------------------------------------------------
-- updated_at helper
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION rr_touch_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := NOW();
  RETURN NEW;
END $$;

-- ---------------------------------------------------------------------
-- organizations  (one row per roofing client; settings = non-secret config)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS organizations (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug            text UNIQUE NOT NULL,
  name            text NOT NULL,
  timezone        text NOT NULL DEFAULT 'America/New_York',
  business_phone  text UNIQUE,                 -- Twilio number, E.164
  whatsapp_number text,
  settings        jsonb NOT NULL DEFAULT '{}'::jsonb,
  active          boolean NOT NULL DEFAULT true,
  created_at      timestamptz NOT NULL DEFAULT NOW(),
  updated_at      timestamptz NOT NULL DEFAULT NOW()
);

-- infrastructure config (URLs only — never secrets)
CREATE TABLE IF NOT EXISTS app_config (
  key        text PRIMARY KEY,
  value      jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS users (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  name            text NOT NULL,
  email           text,
  phone           text,
  role            text NOT NULL DEFAULT 'SALES',   -- OWNER | SALES | OFFICE
  receives_handoffs boolean NOT NULL DEFAULT true,
  created_at      timestamptz NOT NULL DEFAULT NOW()
);

-- ---------------------------------------------------------------------
-- leads
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS leads (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id      uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  name                 text,
  phone                text,
  email                text,
  address              text,
  source               text NOT NULL DEFAULT 'unknown',  -- website | facebook | sms | whatsapp | voice | missed_call
  problem_type         text,
  service_type         text,
  urgency              text,            -- LOW | MEDIUM | HIGH | EMERGENCY
  timeline             text,
  property_type        text,
  budget_signal        text,
  is_decision_maker    boolean,
  in_service_area      boolean,
  qualification_status text NOT NULL DEFAULT 'UNQUALIFIED', -- UNQUALIFIED | PARTIAL | QUALIFIED | DISQUALIFIED
  lead_score           integer NOT NULL DEFAULT 0 CHECK (lead_score BETWEEN 0 AND 100),
  status               text NOT NULL DEFAULT 'NEW',
      -- NEW | CONTACTED | ENGAGED | QUALIFIED | BOOKED | QUOTED | WON | LOST | HUMAN_HANDOFF | STOPPED | DISQUALIFIED
  human_handoff        boolean NOT NULL DEFAULT false,
  external_id          text,            -- e.g. Meta leadgen id
  crm_contact_id       text,
  last_message_at      timestamptz,
  last_inbound_at      timestamptz,
  last_outbound_at     timestamptz,
  timezone             text,
  preferred_channel    text,
  first_message        text,
  metadata             jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at           timestamptz NOT NULL DEFAULT NOW(),
  updated_at           timestamptz NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_org_phone
  ON leads(organization_id, phone) WHERE phone IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_leads_org_email ON leads(organization_id, lower(email));
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(organization_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_org_external
  ON leads(organization_id, external_id) WHERE external_id IS NOT NULL;

-- ---------------------------------------------------------------------
-- conversations & messages
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS conversations (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id     uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  lead_id             uuid NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  channel             text NOT NULL,          -- sms | whatsapp | voice | web
  status              text NOT NULL DEFAULT 'ACTIVE',  -- ACTIVE | CLOSED | EXPIRED
  ai_active           boolean NOT NULL DEFAULT true,
  external_session_id text,
  last_message_at     timestamptz NOT NULL DEFAULT NOW(),
  created_at          timestamptz NOT NULL DEFAULT NOW()
);
-- one ACTIVE conversation per lead per channel (makes "create if needed" race-safe)
CREATE UNIQUE INDEX IF NOT EXISTS idx_conversations_active
  ON conversations(lead_id, channel) WHERE status = 'ACTIVE';

CREATE TABLE IF NOT EXISTS messages (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id     uuid REFERENCES organizations(id) ON DELETE CASCADE,
  conversation_id     uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  lead_id             uuid REFERENCES leads(id) ON DELETE CASCADE,
  external_message_id text,                    -- Twilio MessageSid
  direction           text NOT NULL,           -- INBOUND | OUTBOUND
  channel             text NOT NULL,
  sender              text,
  recipient           text,
  body                text NOT NULL,
  ai_generated        boolean NOT NULL DEFAULT false,
  delivery_status     text,
  metadata            jsonb NOT NULL DEFAULT '{}'::jsonb,
  archived            boolean NOT NULL DEFAULT false,
  created_at          timestamptz NOT NULL DEFAULT NOW()
);
-- Twilio retries the same webhook: this index makes re-delivery a no-op
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_external
  ON messages(external_message_id) WHERE external_message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_messages_conv_time ON messages(conversation_id, created_at DESC);

-- ---------------------------------------------------------------------
-- AI audit trail
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ai_interactions (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid REFERENCES organizations(id) ON DELETE CASCADE,
  lead_id         uuid REFERENCES leads(id) ON DELETE SET NULL,
  conversation_id uuid REFERENCES conversations(id) ON DELETE SET NULL,
  workflow        text NOT NULL,     -- 04_AI_ROUTER, 05_AI_QUALIFICATION ...
  model           text,
  input           jsonb,
  output          jsonb,
  action          text,
  confidence      numeric(4,3),
  valid           boolean NOT NULL DEFAULT true,
  error           text,
  prompt_tokens   integer,
  completion_tokens integer,
  created_at      timestamptz NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ai_lead ON ai_interactions(lead_id, created_at DESC);

-- ---------------------------------------------------------------------
-- appointments & deals
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS appointments (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id      uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  lead_id              uuid NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  external_event_id    text,
  appointment_type     text NOT NULL DEFAULT 'INSPECTION',
  start_at             timestamptz NOT NULL,
  end_at               timestamptz NOT NULL,
  status               text NOT NULL DEFAULT 'SCHEDULED', -- SCHEDULED | CONFIRMED | RESCHEDULED | CANCELLED | COMPLETED | NO_SHOW
  reminder_24h_sent_at timestamptz,
  reminder_2h_sent_at  timestamptz,
  no_show_recovery_sent_at timestamptz,
  rescheduled_from     uuid REFERENCES appointments(id),
  notes                text,
  created_at           timestamptz NOT NULL DEFAULT NOW(),
  updated_at           timestamptz NOT NULL DEFAULT NOW(),
  CHECK (end_at > start_at)
);
-- Idempotency: same lead + same start can only exist once while active
CREATE UNIQUE INDEX IF NOT EXISTS idx_appt_lead_start_active
  ON appointments(lead_id, start_at) WHERE status IN ('SCHEDULED','CONFIRMED');
-- Hard guarantee against double-booking the crew (single-crew default)
DO $$ BEGIN
  ALTER TABLE appointments ADD CONSTRAINT appt_no_overlap
    EXCLUDE USING gist (organization_id WITH =, tstzrange(start_at, end_at, '[)') WITH &&)
    WHERE (status IN ('SCHEDULED','CONFIRMED'));
EXCEPTION WHEN duplicate_object OR duplicate_table THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS deals (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  lead_id         uuid NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  appointment_id  uuid REFERENCES appointments(id) ON DELETE SET NULL,
  stage           text NOT NULL DEFAULT 'INSPECTION_BOOKED',
      -- INSPECTION_BOOKED | INSPECTED | QUOTE_SENT | NEGOTIATION | WON | LOST
  amount          numeric(12,2),              -- only ever set by a human
  quote_sent_at   timestamptz,
  quote_status    text,                       -- OPEN | FOLLOWED_UP | ACCEPTED | DECLINED | EXPIRED
  quote_followups integer NOT NULL DEFAULT 0,
  last_contact_at timestamptz,
  crm_deal_id     text,
  created_at      timestamptz NOT NULL DEFAULT NOW(),
  updated_at      timestamptz NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_deals_lead_open
  ON deals(lead_id) WHERE stage NOT IN ('WON','LOST');

-- ---------------------------------------------------------------------
-- follow-ups, activities, events
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS followups (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  lead_id         uuid NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  type            text NOT NULL DEFAULT 'NURTURE',  -- NURTURE | MISSED_CALL | QUOTE
  step            integer NOT NULL DEFAULT 1,
  due_at          timestamptz NOT NULL,
  status          text NOT NULL DEFAULT 'PENDING',  -- PENDING | SENT | SKIPPED | CANCELLED | FAILED
  message         text,
  sent_at         timestamptz,
  skip_reason     text,
  created_at      timestamptz NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_followups_due ON followups(status, due_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_followups_lead_type_step
  ON followups(lead_id, type, step) WHERE status = 'PENDING';

CREATE TABLE IF NOT EXISTS activities (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid REFERENCES organizations(id) ON DELETE CASCADE,
  lead_id         uuid REFERENCES leads(id) ON DELETE CASCADE,
  type            text NOT NULL,   -- SMS_SENT | MISSED_CALL | REMINDER_SENT | CRM_SYNC | FOLLOWUP_SENT | CALL ...
  description     text,
  metadata        jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at      timestamptz NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_activities_org_type_time ON activities(organization_id, type, created_at);
-- one missed-call record per Twilio CallSid (Twilio can post the status callback more than once)
CREATE UNIQUE INDEX IF NOT EXISTS idx_activities_missed_call_sid
  ON activities ((metadata->>'call_sid')) WHERE type = 'MISSED_CALL';

CREATE TABLE IF NOT EXISTS workflow_events (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid REFERENCES organizations(id) ON DELETE CASCADE,
  lead_id         uuid REFERENCES leads(id) ON DELETE SET NULL,
  workflow        text NOT NULL,
  event           text NOT NULL,
  payload         jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at      timestamptz NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_wf_events_time ON workflow_events(workflow, created_at DESC);

-- ---------------------------------------------------------------------
-- consent
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS consents (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  lead_id        uuid NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  channel        text NOT NULL,          -- sms | whatsapp | voice | email | all
  consent_status text NOT NULL,          -- OPTED_IN | OPTED_OUT
  source         text,                   -- web_form | inbound_sms | keyword | voice | manual
  captured_at    timestamptz NOT NULL DEFAULT NOW(),
  metadata       jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_consents_lead ON consents(lead_id, captured_at DESC);

-- ---------------------------------------------------------------------
-- human handoffs
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS human_handoffs (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  lead_id         uuid NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  conversation_id uuid REFERENCES conversations(id) ON DELETE SET NULL,
  reason          text,
  priority        text NOT NULL DEFAULT 'NORMAL',  -- NORMAL | EMERGENCY
  channel         text,
  status          text NOT NULL DEFAULT 'OPEN',    -- OPEN | ACKNOWLEDGED | RESOLVED
  assigned_to     uuid REFERENCES users(id),
  transfer_status text,                            -- Twilio DialCallStatus for voice
  created_at      timestamptz NOT NULL DEFAULT NOW(),
  resolved_at     timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_handoff_open_per_lead
  ON human_handoffs(lead_id) WHERE status = 'OPEN';

-- ---------------------------------------------------------------------
-- reliability: errors, retry queue, monitoring
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS errors (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workflow_id   text,
  workflow_name text,
  execution_id  text,
  execution_url text,
  node_name     text,
  message       text,
  http_code     integer,
  category      text NOT NULL DEFAULT 'UNKNOWN',
      -- TRANSIENT_API | AUTHENTICATION | VALIDATION | DATABASE | AI | TWILIO | CALENDAR | CRM | UNKNOWN
  retryable     boolean NOT NULL DEFAULT false,
  payload       jsonb NOT NULL DEFAULT '{}'::jsonb,
  alerted       boolean NOT NULL DEFAULT false,
  created_at    timestamptz NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_errors_time ON errors(created_at DESC);

CREATE TABLE IF NOT EXISTS retry_queue (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  error_id        uuid REFERENCES errors(id) ON DELETE SET NULL,
  workflow_name   text NOT NULL,
  workflow_id     text,
  operation       text NOT NULL,          -- RERUN_EXECUTION | EXECUTE_WORKFLOW
  payload         jsonb NOT NULL,
  attempts        integer NOT NULL DEFAULT 0,
  max_attempts    integer NOT NULL DEFAULT 3,
  next_attempt_at timestamptz NOT NULL,
  status          text NOT NULL DEFAULT 'PENDING',  -- PENDING | PROCESSING | COMPLETE | DEAD_LETTER
  last_error      text,
  created_at      timestamptz NOT NULL DEFAULT NOW(),
  updated_at      timestamptz NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_retry_due ON retry_queue(status, next_attempt_at);

CREATE TABLE IF NOT EXISTS monitoring_checks (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id      uuid NOT NULL,
  component   text NOT NULL,          -- DATABASE | TWILIO | CALENDAR | OPENAI | CRM | WORKFLOWS
  status      text NOT NULL,          -- HEALTHY | WARNING | CRITICAL
  detail      text,
  latency_ms  integer,
  checked_at  timestamptz NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_monitoring_time ON monitoring_checks(checked_at DESC);

-- ---------------------------------------------------------------------
-- analytics
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS analytics_daily (
  organization_id    uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  day                date NOT NULL,
  new_leads          integer NOT NULL DEFAULT 0,
  qualified_leads    integer NOT NULL DEFAULT 0,
  bookings           integer NOT NULL DEFAULT 0,
  booking_rate       numeric(6,4) NOT NULL DEFAULT 0,
  average_lead_score numeric(6,2) NOT NULL DEFAULT 0,
  human_handoffs     integer NOT NULL DEFAULT 0,
  response_count     integer NOT NULL DEFAULT 0,
  inbound_messages   integer NOT NULL DEFAULT 0,
  missed_calls       integer NOT NULL DEFAULT 0,
  followup_count     integer NOT NULL DEFAULT 0,
  voice_calls        integer NOT NULL DEFAULT 0,
  median_first_response_seconds integer,
  by_source          jsonb NOT NULL DEFAULT '{}'::jsonb,
  computed_at        timestamptz NOT NULL DEFAULT NOW(),
  PRIMARY KEY (organization_id, day)
);

-- ---------------------------------------------------------------------
-- voice
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS voice_sessions (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id  uuid REFERENCES organizations(id) ON DELETE CASCADE,
  lead_id          uuid REFERENCES leads(id) ON DELETE SET NULL,
  call_sid         text UNIQUE,
  from_number      text,
  to_number        text,
  status           text,             -- IN_PROGRESS | COMPLETED | TRANSFERRED | FAILED
  started_at       timestamptz NOT NULL DEFAULT NOW(),
  ended_at         timestamptz,
  duration_seconds integer,
  transcript       text,
  summary          text,
  outcome          text,             -- BOOKED | QUALIFIED | HANDOFF | INFO | ABANDONED | OPTED_OUT
  metadata         jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS voice_turns (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id uuid NOT NULL REFERENCES voice_sessions(id) ON DELETE CASCADE,
  speaker    text NOT NULL,          -- CALLER | AI | TOOL | SYSTEM
  text       text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_voice_turns_session ON voice_turns(session_id, created_at);

-- ---------------------------------------------------------------------
-- updated_at triggers
-- ---------------------------------------------------------------------
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['organizations','leads','appointments','deals','retry_queue'] LOOP
    EXECUTE format('DROP TRIGGER IF EXISTS trg_%1$s_touch ON %1$s', t);
    EXECUTE format('CREATE TRIGGER trg_%1$s_touch BEFORE UPDATE ON %1$s
                    FOR EACH ROW EXECUTE FUNCTION rr_touch_updated_at()', t);
  END LOOP;
END $$;

-- =====================================================================
-- Helper functions used by the n8n workflows
-- (deterministic logic lives here so it is testable in plain SQL)
-- =====================================================================

-- Can we send an automated message to this lead on this channel?
CREATE OR REPLACE FUNCTION rr_can_contact(p_lead_id uuid, p_channel text DEFAULT 'sms')
RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT
    COALESCE((SELECT l.status NOT IN ('STOPPED') FROM leads l WHERE l.id = p_lead_id), false)
    AND COALESCE((
      SELECT c.consent_status <> 'OPTED_OUT'
      FROM consents c
      WHERE c.lead_id = p_lead_id AND c.channel IN (p_channel, 'all')
      ORDER BY c.captured_at DESC
      LIMIT 1
    ), true);
$$;

-- STOP keyword detector (carrier-standard words + common phrases)
CREATE OR REPLACE FUNCTION rr_is_opt_out(p_body text)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
  SELECT upper(trim(regexp_replace(coalesce(p_body,''), '[^a-zA-Z ]', '', 'g')))
         IN ('STOP','STOPALL','UNSUBSCRIBE','CANCEL','END','QUIT','OPTOUT','OPT OUT','REVOKE')
      OR coalesce(p_body,'') ~* '\m(do not (contact|text|message|call) me|stop (messaging|texting|contacting) me|remove me from)';
$$;

CREATE OR REPLACE FUNCTION rr_is_opt_in(p_body text)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
  SELECT upper(trim(regexp_replace(coalesce(p_body,''), '[^a-zA-Z]', '', 'g')))
         IN ('START','UNSTOP','YES','SUBSCRIBE');
$$;

-- Deterministic lead score (the LLM extracts facts, SQL assigns points)
CREATE OR REPLACE FUNCTION rr_score_lead(
  p_active_problem boolean, p_urgency text, p_decision_maker boolean,
  p_in_service_area boolean, p_appointment_intent boolean,
  p_valid_contact boolean, p_timeline text)
RETURNS integer LANGUAGE sql IMMUTABLE AS $$
  SELECT GREATEST(0, LEAST(100,
      CASE WHEN p_active_problem THEN 20 ELSE 0 END
    + CASE upper(coalesce(p_urgency,'')) WHEN 'EMERGENCY' THEN 20 WHEN 'HIGH' THEN 20 WHEN 'MEDIUM' THEN 10 ELSE 0 END
    + CASE WHEN p_decision_maker THEN 15 ELSE 0 END
    + CASE WHEN p_in_service_area THEN 15 ELSE 0 END
    + CASE WHEN p_appointment_intent THEN 10 ELSE 0 END
    + CASE WHEN p_valid_contact THEN 10 ELSE 0 END
    + CASE WHEN upper(coalesce(p_timeline,'')) IN ('ASAP','THIS_WEEK','THIS_MONTH') THEN 10
           WHEN coalesce(p_timeline,'') NOT IN ('','unknown','UNKNOWN') THEN 5 ELSE 0 END
  ))::int;
$$;

-- Append-only event log shortcut
CREATE OR REPLACE FUNCTION rr_log_event(p_org uuid, p_lead uuid, p_workflow text, p_event text, p_payload jsonb DEFAULT '{}'::jsonb)
RETURNS uuid LANGUAGE sql AS $$
  INSERT INTO workflow_events(organization_id, lead_id, workflow, event, payload)
  VALUES (p_org, p_lead, p_workflow, p_event, coalesce(p_payload,'{}'::jsonb))
  RETURNING id;
$$;

DROP FUNCTION IF EXISTS rr_schedule_nurture(uuid, timestamptz);
-- Nurture sequence: (re)schedule follow-ups for a lead from a start time
CREATE OR REPLACE FUNCTION rr_schedule_nurture(p_lead uuid, p_from timestamptz DEFAULT NOW(), p_start_step int DEFAULT 1)
RETURNS integer LANGUAGE plpgsql AS $$
DECLARE
  v_org uuid; v_mins int[]; i int; n int := 0;
BEGIN
  SELECT l.organization_id,
         coalesce(ARRAY(SELECT jsonb_array_elements_text(o.settings->'followup_sequence_minutes')::int),
                  ARRAY[15,240,1440,4320,10080])
    INTO v_org, v_mins
  FROM leads l JOIN organizations o ON o.id = l.organization_id WHERE l.id = p_lead;
  IF v_org IS NULL THEN RETURN 0; END IF;
  -- cancel any pending nurture steps, then lay the sequence down again
  UPDATE followups SET status='CANCELLED', skip_reason='RESCHEDULED'
   WHERE lead_id=p_lead AND type='NURTURE' AND status='PENDING';
  -- never nurture leads that are booked, handed off, stopped or disqualified
  IF EXISTS (SELECT 1 FROM leads WHERE id=p_lead
             AND (human_handoff OR status IN ('BOOKED','HUMAN_HANDOFF','STOPPED','DISQUALIFIED','WON','LOST'))) THEN
    RETURN 0;
  END IF;
  FOR i IN greatest(p_start_step,1)..coalesce(array_length(v_mins,1),0) LOOP
    INSERT INTO followups(organization_id, lead_id, type, step, due_at)
    VALUES (v_org, p_lead, 'NURTURE', i, p_from + make_interval(mins => v_mins[i]));
    n := n + 1;
  END LOOP;
  RETURN n;
END $$;

-- Cancel outstanding follow-ups when a lead books / hands off / stops
CREATE OR REPLACE FUNCTION rr_cancel_followups(p_lead uuid, p_reason text)
RETURNS integer LANGUAGE sql AS $$
  WITH u AS (
    UPDATE followups SET status='CANCELLED', skip_reason=p_reason
    WHERE lead_id=p_lead AND status='PENDING' AND type <> 'QUOTE'
    RETURNING 1)
  SELECT count(*)::int FROM u;
$$;

-- Recent conversation history as a compact JSON array (oldest → newest)
CREATE OR REPLACE FUNCTION rr_recent_messages(p_lead uuid, p_limit int DEFAULT 12)
RETURNS jsonb LANGUAGE sql STABLE AS $$
  SELECT coalesce(jsonb_agg(jsonb_build_object(
            'role', CASE WHEN m.direction='INBOUND' THEN 'customer' ELSE 'business' END,
            'channel', m.channel, 'body', m.body, 'at', m.created_at) ORDER BY m.created_at), '[]'::jsonb)
  FROM (SELECT * FROM messages WHERE lead_id = p_lead AND NOT archived
        ORDER BY created_at DESC LIMIT p_limit) m;
$$;

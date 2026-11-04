-- =====================================================================
-- Seed: PrimeHome Roofing (demo client). Edit values, then run.
-- Non-secret business config lives in organizations.settings;
-- secrets live ONLY in n8n credentials / voice-server env.
-- =====================================================================
INSERT INTO organizations (id, slug, name, timezone, business_phone, whatsapp_number, settings)
VALUES (
  '00000000-0000-4000-8000-000000000001',
  'primehome',
  'PrimeHome Roofing',
  'America/New_York',
  '+15550100000',            -- ← your Twilio number (E.164)
  '+14155238886',            -- ← Twilio WhatsApp sender (sandbox number shown)
  $json${
    "services": ["Roof inspection", "Roof repair", "Roof replacement"],
    "service_area": {"states": ["NY", "NJ"], "description": "New York and New Jersey"},
    "business_hours": {"start": "08:00", "end": "17:00", "days": [1,2,3,4,5,6]},
    "slot_minutes": 60,
    "slot_step_minutes": 60,
    "min_notice_minutes": 120,
    "booking_horizon_days": 7,
    "max_slots_offered": 3,
    "calendar_id": "primary",
    "slack_channel": "#roofradar-leads",
    "handoff_phone": "+15550001111",
    "report_email": "owner@primehome.example",
    "emergency_criteria": [
      "water actively entering the home right now",
      "roof or ceiling visibly sagging or collapsed",
      "storm damage leaving the roof open to weather",
      "tree or debris currently on the roof"
    ],
    "faq": {
      "inspection_cost": "Roof inspections are free.",
      "insurance": "We work with most insurance carriers and can meet your adjuster on site.",
      "warranty": "Repairs carry a workmanship warranty; details are in your written quote.",
      "licensed": "PrimeHome Roofing is licensed and insured in New York and New Jersey."
    },
    "followup_sequence_minutes": [15, 240, 1440, 4320, 10080],
    "quote_followup_days": [2, 5, 10],
    "reminders": {"h24": true, "h2": true},
    "crm": {"provider": "hubspot", "enabled": false},
    "meta": {"page_id": "YOUR_FACEBOOK_PAGE_ID", "verify_token": "roofradar-meta-verify"},
    "ai": {
      "min_router_confidence": 0.6,
      "brand_voice": "friendly, concise, professional",
      "models": {
        "router": "gpt-5.4-mini",
        "qualifier": "gpt-5.4-mini",
        "conversation": "gpt-5.4",
        "followup": "gpt-5.4-mini",
        "summary": "gpt-5.4-mini"
      }
    }
  }$json$::jsonb
)
ON CONFLICT (id) DO UPDATE
  SET name = EXCLUDED.name, timezone = EXCLUDED.timezone,
      business_phone = EXCLUDED.business_phone,
      whatsapp_number = EXCLUDED.whatsapp_number,
      settings = EXCLUDED.settings;

INSERT INTO users (organization_id, name, email, phone, role)
SELECT '00000000-0000-4000-8000-000000000001', 'Office Manager', 'office@primehome.example', '+15550001111', 'OFFICE'
WHERE NOT EXISTS (SELECT 1 FROM users WHERE email = 'office@primehome.example');

-- Infrastructure URLs used by workflows (edit to your hosts)
INSERT INTO app_config(key, value) VALUES
  ('n8n_base_url',     '"https://n8n.example.com"'),
  ('voice_server_url', '"https://voice.example.com"')
ON CONFLICT (key) DO NOTHING;

INSERT INTO app_config(key, value) VALUES
  ('alert_slack_channel', '"#roofradar-alerts"')
ON CONFLICT (key) DO NOTHING;

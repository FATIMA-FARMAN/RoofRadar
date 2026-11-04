-- RoofRadar AI — adds what workflows 25 (no-show recovery) and 26 (re-engagement) need.
-- Safe to run more than once.
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS no_show_recovery_sent_at timestamptz;
CREATE INDEX IF NOT EXISTS idx_appt_no_show_pending
  ON appointments(status, no_show_recovery_sent_at) WHERE status = 'NO_SHOW';

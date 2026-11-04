'use strict';
// Logging only: sessions, turns and the unified message history. Business actions go through n8n tools.
const { Pool } = require('pg');
const config = require('./config');

// sslmode in the URL would override the ssl option below (pg treats "require" as full verification),
// so strip it and configure TLS explicitly — works with Supabase / managed Postgres certificates.
const dbUrl = new URL(config.databaseUrl);
const wantSsl = /require|verify/.test(dbUrl.searchParams.get('sslmode') || '');
dbUrl.searchParams.delete('sslmode');
const pool = new Pool({
  connectionString: dbUrl.toString(),
  max: 10,
  ssl: wantSsl ? { rejectUnauthorized: false } : undefined,
});

async function getOrganizationByNumber(to) {
  const { rows } = await pool.query(
    `SELECT id, name, timezone, settings FROM organizations WHERE business_phone = $1 AND active LIMIT 1`, [to]);
  return rows[0] || null;
}

async function createSession({ callSid, from, to, organizationId }) {
  const { rows } = await pool.query(
    `INSERT INTO voice_sessions (call_sid, from_number, to_number, organization_id, status)
     VALUES ($1, $2, $3, $4, 'IN_PROGRESS')
     ON CONFLICT (call_sid) DO UPDATE SET status = 'IN_PROGRESS'
     RETURNING id`, [callSid, from, to, organizationId]);
  return rows[0].id;
}

async function addTurn(sessionId, speaker, text, ctx = {}) {
  if (!text) return;
  await pool.query(`INSERT INTO voice_turns (session_id, speaker, text) VALUES ($1, $2, $3)`, [sessionId, speaker, text]);
  // Mirror caller/AI turns into the shared message history so qualification (05) and SMS follow-ups see the call.
  if (ctx.conversationId && ctx.leadId && (speaker === 'CALLER' || speaker === 'AI')) {
    await pool.query(
      `INSERT INTO messages (organization_id, conversation_id, lead_id, direction, channel, sender, recipient, body, ai_generated, delivery_status)
       VALUES ($1, $2, $3, $4, 'voice', $5, $6, $7, $8, 'SPOKEN')`,
      [ctx.organizationId, ctx.conversationId, ctx.leadId,
       speaker === 'CALLER' ? 'INBOUND' : 'OUTBOUND',
       speaker === 'CALLER' ? ctx.from : ctx.to, speaker === 'CALLER' ? ctx.to : ctx.from,
       text, speaker === 'AI']);
  }
}

async function finishSession(sessionId, { status, outcome, metadata }) {
  await pool.query(
    `UPDATE voice_sessions SET status = CASE WHEN status = 'TRANSFERRED' THEN status ELSE $2 END,
            outcome = COALESCE(outcome, $3), ended_at = COALESCE(ended_at, NOW()),
            duration_seconds = EXTRACT(epoch FROM (NOW() - started_at))::int,
            metadata = metadata || $4::jsonb
     WHERE id = $1`, [sessionId, status, outcome || null, JSON.stringify(metadata || {})]);
}

module.exports = { pool, getOrganizationByNumber, createSession, addTurn, finishSession };

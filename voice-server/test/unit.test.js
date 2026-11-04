'use strict';
// Offline unit tests (no network, no DB): node --test test/
process.env.PUBLIC_HOST ||= 'voice.test';
process.env.OPENAI_API_KEY ||= 'sk-test';
process.env.DATABASE_URL ||= 'postgresql://localhost/none';
process.env.N8N_VOICE_TOOL_URL ||= 'http://localhost/tool';
process.env.N8N_VOICE_CALL_ENDED_URL ||= 'http://localhost/ended';
process.env.N8N_VOICE_TRANSFER_RESULT_URL ||= 'http://localhost/transfer';
process.env.VOICE_GATEWAY_KEY ||= 'k';
const test = require('node:test');
const assert = require('node:assert');
const { buildSystemPrompt } = require('../src/prompt');
const { TOOL_DEFS } = require('../src/tools');

test('system prompt carries company rules and emergency criteria', () => {
  const p = buildSystemPrompt({
    org: { name: 'PrimeHome Roofing', settings: { emergency_criteria: ['water actively entering the home right now'],
      service_area: { description: 'New York and New Jersey' } } },
    lead: { name: 'John Smith', address: '123 Main St' }, nowLocal: 'Wednesday'
  });
  assert.match(p, /PrimeHome Roofing/);
  assert.match(p, /water actively entering the home right now/);
  assert.match(p, /Never|must NOT/);
  assert.match(p, /Returning caller — name John Smith/);
});

test('tool set is the controlled allow-list', () => {
  const names = TOOL_DEFS.map(t => t.name).sort();
  assert.deepStrictEqual(names, ['book_appointment', 'end_call', 'get_available_slots', 'human_handoff',
    'opt_out', 'qualify_lead', 'reschedule_appointment', 'send_sms_confirmation']);
  for (const t of TOOL_DEFS) assert.strictEqual(t.parameters.additionalProperties, false, t.name);
});

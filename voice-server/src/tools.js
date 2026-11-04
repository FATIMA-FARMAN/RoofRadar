'use strict';
// Tools the voice model may call. Every business action is executed by n8n (workflow 24A), never here.
const config = require('./config');

const TOOL_DEFS = [
  {
    name: 'qualify_lead',
    description: 'Save what the caller told you (name, property address, problem) and get an updated qualification. Call once you know the problem and the address.',
    parameters: {
      type: 'object',
      properties: {
        name: { type: 'string', description: "Caller's name, only if they said it" },
        address: { type: 'string', description: 'Property address including town and state' },
        problem_type: { type: 'string', description: 'Short description, e.g. "roof leak", "missing shingles"' },
        email: { type: 'string' },
      },
      additionalProperties: false,
    },
  },
  {
    name: 'get_available_slots',
    description: 'Look up REAL free inspection times. Always call this before offering any time. Never guess availability.',
    parameters: {
      type: 'object',
      properties: {
        date: { type: 'string', description: 'Preferred date YYYY-MM-DD in the company timezone, if the caller named one' },
        preferred_time: { type: 'string', description: 'Preferred time HH:MM 24h, if the caller named one' },
      },
      additionalProperties: false,
    },
  },
  {
    name: 'book_appointment',
    description: 'Book one of the slots returned by get_available_slots. Pass the slot "iso" value exactly. Only say the caller is booked if the result has booked=true.',
    parameters: {
      type: 'object',
      properties: { slot: { type: 'string', description: 'The exact iso value of the chosen slot' } },
      required: ['slot'],
      additionalProperties: false,
    },
  },
  {
    name: 'reschedule_appointment',
    description: "Move the caller's existing upcoming appointment to one of the slots returned by get_available_slots.",
    parameters: {
      type: 'object',
      properties: { slot: { type: 'string' } },
      required: ['slot'],
      additionalProperties: false,
    },
  },
  {
    name: 'send_sms_confirmation',
    description: 'Text the caller a short confirmation / recap. Use after booking or when they ask for details by text.',
    parameters: { type: 'object', properties: {}, additionalProperties: false },
  },
  {
    name: 'human_handoff',
    description: 'Transfer the caller to a team member. Use immediately if they ask for a person, have a complaint, pricing negotiation, insurance/legal question, or an emergency per the company criteria.',
    parameters: {
      type: 'object',
      properties: {
        reason: { type: 'string' },
        emergency: { type: 'boolean', description: 'true only if a configured emergency criterion is met' },
      },
      required: ['reason'],
      additionalProperties: false,
    },
  },
  {
    name: 'opt_out',
    description: 'The caller asked not to be contacted again (calls or texts).',
    parameters: { type: 'object', properties: {}, additionalProperties: false },
  },
  {
    name: 'end_call',
    description: 'End the call politely after saying goodbye, when the caller is done.',
    parameters: {
      type: 'object',
      properties: { reason: { type: 'string' } },
      additionalProperties: false,
    },
  },
];

const OPENAI_TOOLS = TOOL_DEFS.map(t => ({ type: 'function', function: t }));
const SLOW_TOOLS = new Set(['get_available_slots', 'book_appointment', 'reschedule_appointment', 'qualify_lead']);

async function callGateway(tool, args, call) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), config.toolTimeoutMs);
  try {
    const res = await fetch(config.n8nToolUrl, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'X-RoofRadar-Key': config.gatewayKey },
      body: JSON.stringify({ tool, args: args || {}, call }),
      signal: ctrl.signal,
    });
    const text = await res.text();
    let body;
    try { body = JSON.parse(text); } catch { body = { ok: false, error: `non-JSON response (${res.status})` }; }
    if (!res.ok && body.ok !== false) body = { ok: false, error: `HTTP ${res.status}` };
    return body;
  } catch (e) {
    return { ok: false, error: e.name === 'AbortError' ? 'tool timed out' : String(e.message || e) };
  } finally {
    clearTimeout(timer);
  }
}

async function postCallEnded(payload) {
  try {
    await fetch(config.n8nCallEndedUrl, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'X-RoofRadar-Key': config.gatewayKey },
      body: JSON.stringify(payload),
    });
  } catch (e) {
    console.error('[voice] call-ended webhook failed', e.message);
  }
}

module.exports = { TOOL_DEFS, OPENAI_TOOLS, SLOW_TOOLS, callGateway, postCallEnded };

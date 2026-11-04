#!/usr/bin/env node
'use strict';
// Simulate a Twilio ConversationRelay call against a running voice server — no phone needed.
//
//   PUBLIC_HOST=voice.example.com TWILIO_AUTH_TOKEN=... \
//   node test/simulate-call.js ws://localhost:8080/voice +14155551234 +15550100000 \
//        "Hi, my roof is leaking" "It's 5 Elm Street, Trenton NJ" "The first one works"
//
// Signs the handshake exactly like Twilio (X-Twilio-Signature over wss://PUBLIC_HOST/voice),
// sends setup + one prompt per argument, prints what the agent says, and reports the end/handoff message.
const WebSocket = require('ws');
const twilio = require('twilio');

const [wsUrl = 'ws://localhost:8080/voice', from = '+14155551234', to = '+15550100000', ...lines] = process.argv.slice(2);
const prompts = lines.length ? lines : ['Hi, I think my roof is leaking.', 'Yes please, what times do you have?', 'The first one.', 'No, that is all. Thanks!'];
const headers = {};
if (process.env.TWILIO_AUTH_TOKEN && process.env.PUBLIC_HOST) {
  headers['X-Twilio-Signature'] = twilio.getExpectedTwilioSignature(
    process.env.TWILIO_AUTH_TOKEN, `wss://${process.env.PUBLIC_HOST}/voice`, {});
}
const callSid = 'CAsim' + Date.now();
const ws = new WebSocket(wsUrl, { headers });
let reply = '';
let i = 0;
const next = () => {
  if (i >= prompts.length) { setTimeout(() => ws.close(), 1500); return; }
  const text = prompts[i++];
  console.log(`\n📞 CALLER: ${text}`);
  ws.send(JSON.stringify({ type: 'prompt', voicePrompt: text, lang: 'en-US', last: true }));
};

ws.on('open', () => {
  console.log(`connected (${callSid})`);
  ws.send(JSON.stringify({ type: 'setup', sessionId: 'VX' + Date.now(), callSid, from, to, direction: 'inbound',
                           customParameters: {} }));
  setTimeout(next, 800);
});
let quiet;
ws.on('message', raw => {
  const m = JSON.parse(raw);
  if (m.type === 'text') {
    reply += m.token;
    if (m.last) { console.log(`🤖 AI: ${reply.trim()}`); reply = ''; clearTimeout(quiet); quiet = setTimeout(next, 3000); }  // a real caller waits for the agent to finish
  } else if (m.type === 'end') {
    console.log(`\n⏹  END ${m.handoffData}`);
    clearTimeout(quiet);
    setTimeout(() => ws.close(), 300);
  }
});
ws.on('unexpected-response', (req, res) => { console.error('handshake rejected', res.statusCode); process.exit(1); });
ws.on('close', () => { console.log('\ncall closed'); process.exit(0); });
ws.on('error', e => { console.error(e.message); process.exit(1); });

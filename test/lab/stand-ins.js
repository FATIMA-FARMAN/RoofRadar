// Local stand-ins for OpenAI, Google Calendar, Twilio, Slack, HubSpot (lab only).
const https = require('https'), http = require('http'), fs = require('fs');
const LOG = [];   // outbound SMS + Slack, shown in the demo phone/slack views
const REQ = fs.createWriteStream(__dirname + '/requests.log', { flags: 'a' });

function body(req) { return new Promise(r => { let b = ''; req.on('data', c => b += c); req.on('end', () => r(b)); }); }
function json(res, code, obj) { res.writeHead(code, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(obj)); }
const rid = p => p + Math.random().toString(36).slice(2, 12) + Date.now().toString(36);

function textOf(m) { return typeof m.content === 'string' ? m.content : (m.content || []).map(c => c.text || '').join(' '); }

function deep(o, keys) { if (!o || typeof o !== 'object') return null; for (const k of keys) if (o[k] && typeof o[k] !== 'object') return o[k];
  for (const v of Object.values(o)) { const r = deep(v, keys); if (r) return r; } return null; }
function openai(reqBody) {
  const b = JSON.parse(reqBody || '{}');
  const msgs = b.messages || [];
  const sys = textOf(msgs.find(m => m.role === 'system') || { content: '' });
  const user = msgs.filter(m => m.role !== 'system').map(textOf).join('\n');
  let ctx = {}; try { const m = user.match(/\{[\s\S]*\}/); if (m) ctx = JSON.parse(m[0]); } catch (e) {}
  // latest customer text
  let latest = ctx.latest_message || ctx.message || ctx.latest || '';
  if (!latest) { const m = user.match(/LATEST[^:]*:\s*(.+)/i); latest = m ? m[1] : user.slice(-300); }
  const L = String(latest).toLowerCase();
  let out;
  if (/routing agent/.test(sys)) {
    const slots = ctx.offered_slots || (ctx.context && ctx.context.offered_slots) || [];
    let a = { action: 'QUALIFY', confidence: 0.86, reason: 'needs inspection, details still missing', sentiment: 'NEUTRAL',
      selected_slot: null, preferred_date: null, preferred_time: null, emergency_criteria_matched: null, handoff_reason: null };
    const pick = L.match(/^\s*"?\s*([1-3])\b/) || L.match(/option\s*([1-3])/);
    if (/\bstop\b|unsubscribe/.test(L)) a = { ...a, action: 'STOP', confidence: 1, reason: 'opt-out keyword' };
    else if (pick && slots.length) a = { ...a, action: 'BOOK', confidence: 0.97, reason: 'picked an offered slot', sentiment: 'POSITIVE', selected_slot: (slots[+pick[1] - 1] || slots[0]).iso };
    else if (/person|human|manager|someone real/.test(L)) a = { ...a, action: 'HUMAN', confidence: 0.95, reason: 'asked for a person', handoff_reason: 'customer requested a person' };
    else if (/how much|price|cost|quote/.test(L)) a = { ...a, action: 'HUMAN', confidence: 0.9, reason: 'pricing request', handoff_reason: 'pricing question' };
    else if (/inspect|come out|schedule|appointment|book/.test(L)) a = { ...a, action: 'QUALIFY', confidence: 0.91, reason: 'wants inspection; confirm facts', sentiment: 'POSITIVE' };
    out = a;
  } else if (/qualification facts/.test(sys)) {
    const all = user.toLowerCase(); const leak = /leak|water|ceiling/.test(all);
    out = { name: null, email: null, address: null, address_state: 'NJ', service_type: 'inspection', problem_type: leak ? 'roof leak' : (/shingle/.test(all) ? 'missing shingles' : 'roof repair'),
      active_problem: true, urgency: leak ? 'HIGH' : 'MEDIUM', timeline: 'THIS_WEEK', property_type: 'single_family', budget_signal: 'unknown',
      is_decision_maker: true, appointment_intent: true, qualification_status: 'QUALIFIED', missing_fields: [],
      recommended_action: 'BOOK', confidence: 0.9, evidence: ['"I need someone to inspect my roof"', 'leak reported on the web form'] };
  } else if (/communication assistant/.test(sys)) {
    const S = sys + '\n' + user;
    let reply = 'Thanks for reaching out to PrimeHome Roofing. Could you tell me a little more about the issue?';
    const nm = String((ctx.customer && ctx.customer.first_name) || deep(ctx, ['first_name']) || 'there').split(' ')[0];
    const pr = String((ctx.customer && ctx.customer.problem_type) || deep(ctx, ['problem_type']) || 'your roof').toLowerCase();
    if (/FIRST_CONTACT|just submitted a form/.test(S)) reply = /leak/.test(pr)
      ? `Hi ${nm}, thanks for contacting PrimeHome Roofing about your ${pr}. Is water coming in right now, and would a free inspection this week help?`
      : `Hi ${nm}, thanks for contacting PrimeHome Roofing about the ${pr}. Would a free inspection this week work for you?`;
    else if (/OFFER_SLOTS|introducing available/.test(S)) reply = 'Great news, we have free inspection times open this week:';
    else if (/NO_SLOTS/.test(S)) reply = 'Sorry, there are no open times in that window. A team member will reach out shortly to schedule.';
    else if (/FOLLOWUP|went quiet/.test(S)) reply = `Hi ${nm}, just checking in on your ${pr}. Happy to set up a free inspection whenever suits you.`;
    else if (/NO_SHOW/.test(S)) reply = 'Sorry we missed you today, no problem at all. Here are new free inspection times:';
    out = { reply, next_action: 'WAIT_FOR_CUSTOMER' };
  } else if (/received a quote/.test(sys)) {
    out = { reply: 'Hi John, just following up on your PrimeHome Roofing quote. Any questions, or would you like to go ahead?' };
  } else if (/Summarise this phone call/.test(sys)) {
    out = { summary: 'Caller reported a roof leak and booked a free inspection.', problem_type: 'roof leak', urgency: 'HIGH',
      appointment_booked: false, human_handoff: false, followup_required: true, followup_reason: 'confirm inspection time',
      caller_name: null, address: null, outcome: 'QUALIFIED' };
  } else out = { reply: 'OK', next_action: 'NONE' };
  return { id: rid('chatcmpl-'), object: 'chat.completion', created: Math.floor(Date.now() / 1000), model: b.model || 'gpt-5.4-mini',
    choices: [{ index: 0, finish_reason: 'stop', message: { role: 'assistant', content: JSON.stringify(out) } }],
    usage: { prompt_tokens: 812, completion_tokens: 96, total_tokens: 908 } };
}

async function handler(req, res) {
  const host = (req.headers.host || '').split(':')[0], url = req.url, b = await body(req);
  REQ.write(`${new Date().toISOString()} ${req.method} ${host}${url} ${b.slice(0, 1500)}\n`); if (host === "api.openai.com") fs.writeFileSync(__dirname + "/last-openai.json", b);
  if (host === 'api.openai.com') {
    if (url.startsWith('/v1/models')) return json(res, 200, { object: 'list', data: [{ id: 'gpt-5.4-mini' }] });
    return json(res, 200, openai(b));
  }
  if (host === 'www.googleapis.com') {
    if (url.includes('/freeBusy')) { const q = JSON.parse(b || '{}'); const cals = {}; (q.items || [{ id: 'primary' }]).forEach(i => cals[i.id] = { busy: [] }); return json(res, 200, { kind: 'calendar#freeBusy', calendars: cals }); }
    if (url.includes('/calendarList')) return json(res, 200, { items: [{ id: 'primary' }] });
    if (req.method === 'POST' && url.includes('/events')) { const e = JSON.parse(b || '{}'); const id = rid('evt'); return json(res, 200, { id, status: 'confirmed', htmlLink: 'https://calendar.google.com/event?eid=' + id, ...e }); }
    if (req.method === 'DELETE') { res.writeHead(204); return res.end(); }
    return json(res, 200, {});
  }
  if (host === 'oauth2.googleapis.com') return json(res, 200, { access_token: 'lab-token', expires_in: 3600, token_type: 'Bearer' });
  if (host === 'api.twilio.com') {
    const p = new URLSearchParams(b); const sid = 'SM' + rid('').slice(0, 30);
    LOG.push({ kind: 'sms', to: p.get('To'), from: p.get('From'), body: p.get('Body'), at: new Date().toISOString() });
    return json(res, 201, { sid, status: 'queued', to: p.get('To'), from: p.get('From'), body: p.get('Body'), date_created: new Date().toUTCString() });
  }
  if (host === 'status.twilio.com') return json(res, 200, { status: { indicator: 'none', description: 'All Systems Operational' } });
  if (host === 'slack.com') {
    let p = {}; try { p = JSON.parse(b); } catch (e) { p = Object.fromEntries(new URLSearchParams(b)); }
    LOG.push({ kind: 'slack', channel: p.channel, text: p.text, at: new Date().toISOString() });
    return json(res, 200, { ok: true, channel: p.channel || 'C0LAB', ts: (Date.now() / 1000).toFixed(6), message: { text: p.text } });
  }
  if (host === 'api.hubapi.com') return json(res, 200, { id: String(Date.now()), results: [], properties: {} });
  json(res, 404, { error: 'no mock for ' + host + url });
}

https.createServer({ key: fs.readFileSync(process.env.LAB_KEY || __dirname + '/srv.key'), cert: fs.readFileSync(process.env.LAB_CERT || __dirname + '/srv.crt') }, handler).listen(443, '127.0.0.1');
http.createServer((req, res) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  if (req.url.startsWith('/reset')) { LOG.length = 0; return json(res, 200, { ok: true }); }
  json(res, 200, LOG);
}).listen(9100, '127.0.0.1');
console.log('mock up');

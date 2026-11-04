'use strict';
// RoofRadar AI — Workflow 24 voice server.
//   POST /twiml/incoming        Twilio number "A call comes in" webhook → <Connect><ConversationRelay>
//   WS   /voice                 ConversationRelay WebSocket (signature-validated)
//   POST /twiml/connect-action  Twilio calls this when the relay session ends → <Dial> for warm transfer or <Hangup>
//   GET  /health
const http = require('http');
const { URL } = require('url');
const querystring = require('querystring');
const { WebSocketServer } = require('ws');
const twilio = require('twilio');
const OpenAI = require('openai');
const config = require('./config');
const db = require('./db');
const { CallSession } = require('./session');

const openai = new OpenAI({ apiKey: config.openaiApiKey, baseURL: config.openaiBaseUrl });
const xml = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

function validTwilio(signature, url, params) {
  if (!config.validateSignatures) return true;
  if (!config.twilioAuthToken || !signature) return false;
  return twilio.validateRequest(config.twilioAuthToken, signature, url, params || {});
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let b = '';
    req.on('data', c => { b += c; if (b.length > 1e6) req.destroy(); });
    req.on('end', () => resolve(b));
    req.on('error', reject);
  });
}

async function greetingFor(to) {
  const org = await db.getOrganizationByNumber(to).catch(() => null);
  const name = (org && org.name) || 'PrimeHome Roofing';
  const custom = org && org.settings && org.settings.voice && org.settings.voice.greeting;
  return custom || `Hi, you've reached ${name}. I'm the virtual assistant. How can I help with your roof today?`;
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `https://${config.publicHost}`);
  try {
    if (req.method === 'GET' && url.pathname === '/health') {
      res.writeHead(200, { 'content-type': 'application/json' });
      return res.end(JSON.stringify({ ok: true }));
    }
    if (req.method === 'POST' && url.pathname.startsWith('/twiml/')) {
      const raw = await readBody(req);
      const params = querystring.parse(raw);
      const fullUrl = `https://${config.publicHost}${req.url}`;
      if (!validTwilio(req.headers['x-twilio-signature'], fullUrl, params)) {
        res.writeHead(403); return res.end('invalid signature');
      }
      res.writeHead(200, { 'content-type': 'text/xml' });

      if (url.pathname === '/twiml/incoming') {
        const greeting = await greetingFor(params.To);
        const attrs = [
          `url="wss://${xml(config.publicHost)}/voice"`,
          `welcomeGreeting="${xml(greeting)}"`,
          `language="${xml(config.language)}"`,
          'interruptible="any"',
          'dtmfDetection="true"',
        ];
        if (config.ttsProvider) attrs.push(`ttsProvider="${xml(config.ttsProvider)}"`);
        if (config.ttsVoice) attrs.push(`voice="${xml(config.ttsVoice)}"`);
        if (config.transcriptionProvider) attrs.push(`transcriptionProvider="${xml(config.transcriptionProvider)}"`);
        return res.end(`<?xml version="1.0" encoding="UTF-8"?><Response><Connect action="https://${xml(config.publicHost)}/twiml/connect-action">` +
          `<ConversationRelay ${attrs.join(' ')}><Parameter name="greeting" value="${xml(greeting)}"/></ConversationRelay></Connect></Response>`);
      }

      if (url.pathname === '/twiml/connect-action') {
        let data = {};
        try { data = JSON.parse(params.HandoffData || '{}'); } catch { /* ignore */ }
        if (data.reason === 'handoff' && data.phone) {
          return res.end(`<?xml version="1.0" encoding="UTF-8"?><Response><Say>Connecting you now.</Say>` +
            `<Dial timeout="25" action="${xml(config.n8nTransferResultUrl)}">${xml(data.phone)}</Dial></Response>`);
        }
        return res.end('<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>');
      }
      return res.end('<Response/>');
    }
    res.writeHead(404); res.end('not found');
  } catch (e) {
    console.error('[http]', e);
    if (!res.headersSent) res.writeHead(500);
    res.end();
  }
});

const wss = new WebSocketServer({ noServer: true });
server.on('upgrade', (req, socket, head) => {
  const url = new URL(req.url, `wss://${config.publicHost}`);
  if (url.pathname !== '/voice') { socket.destroy(); return; }
  const wsUrl = `wss://${config.publicHost}${req.url}`;
  if (!validTwilio(req.headers['x-twilio-signature'], wsUrl, {})) {
    socket.write('HTTP/1.1 403 Forbidden\r\n\r\n'); socket.destroy(); return;
  }
  wss.handleUpgrade(req, socket, head, ws => wss.emit('connection', ws, req));
});

wss.on('connection', ws => {
  // one session per socket, created synchronously so no early message can race it
  const session = new CallSession(ws, openai, { greetingFor });
  ws.on('message', raw => session.handle(raw).catch(e => console.error('[ws]', e)));
  ws.on('close', () => session.onClose());
});

if (require.main === module) {
  server.listen(config.port, () => console.log(`RoofRadar voice server on :${config.port} (public host ${config.publicHost})`));
}

module.exports = { server };

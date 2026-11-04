'use strict';
// One live phone call: ConversationRelay WebSocket <-> OpenAI (streaming + tools) <-> n8n tool gateway.
const config = require('./config');
const db = require('./db');
const { OPENAI_TOOLS, SLOW_TOOLS, callGateway, postCallEnded } = require('./tools');
const { buildSystemPrompt } = require('./prompt');

const FILLERS = ['One moment while I check that.', 'Let me check that for you.', 'Just a second.'];

class CallSession {
  constructor(ws, openai, { greetingFor }) {
    this.ws = ws;
    this.openai = openai;
    this.greetingFor = greetingFor;
    this.call = {};               // sent to n8n with every tool call
    this.ctx = {};                // for message mirroring
    this.messages = [];
    this.lastSlots = [];          // slots returned by the most recent availability lookup
    this.outcome = null;
    this.pendingEnd = null;       // { handoffData } — end the relay once the current reply is spoken
    this.ended = false;
    this.abort = null;
    this.run = Promise.resolve();
    this.buffer = '';
    this.ready = null;
    this.dbq = Promise.resolve();  // turn writes are serialised so the transcript keeps its real order
  }

  persist(speaker, text) {
    this.dbq = this.dbq.then(() => db.addTurn(this.sessionId, speaker, text, speaker === 'TOOL' ? {} : this.ctx))
      .catch(e => this.log('db', e.message));
    return this.dbq;
  }

  log(...a) { console.log(`[call ${this.call.call_sid || '?'}]`, ...a); }

  send(obj) {
    if (this.ws.readyState === 1) this.ws.send(JSON.stringify(obj));
  }

  speak(token, last) { this.send({ type: 'text', token, last }); }

  // ------------------------------------------------------------ inbound
  async handle(raw) {
    let msg;
    try { msg = JSON.parse(raw); } catch { return; }
    switch (msg.type) {
      case 'setup': this.ready = this.onSetup(msg); return this.ready;
      case 'prompt': await this.ready; return this.onPrompt(msg);
      case 'interrupt': return this.onInterrupt(msg);
      case 'dtmf': await this.ready; return this.onUserText(`(caller pressed ${msg.digit})`);
      case 'error': this.log('relay error', msg.description); return;
      default: return;
    }
  }

  async onSetup(msg) {
    this.call = { call_sid: msg.callSid, from: msg.from, to: msg.to };
    const org = await db.getOrganizationByNumber(msg.to).catch(() => null);
    this.org = org;
    this.sessionId = await db.createSession({ callSid: msg.callSid, from: msg.from, to: msg.to, organizationId: org && org.id });
    this.call.session_id = this.sessionId;

    const r = await callGateway('find_lead', {}, this.call);
    const lead = r.ok ? r.lead : null;
    if (lead) {
      Object.assign(this.call, { lead_id: lead.lead_id, conversation_id: lead.conversation_id, organization_id: lead.organization_id });
      this.ctx = { leadId: lead.lead_id, conversationId: lead.conversation_id, organizationId: lead.organization_id,
                   from: msg.from, to: msg.to };
    } else {
      this.log('find_lead failed:', r.error);
    }
    const tz = (org && org.timezone) || 'America/New_York';
    const nowLocal = new Date().toLocaleString('en-US', { timeZone: tz, weekday: 'long', year: 'numeric', month: 'long',
      day: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'short' });
    this.messages = [{ role: 'system', content: buildSystemPrompt({ org, lead, nowLocal }) }];
    // Twilio already spoke the welcomeGreeting from the TwiML; keep it in the history
    const greeting = (msg.customParameters && msg.customParameters.greeting) || await this.greetingFor(msg.to);
    this.messages.push({ role: 'assistant', content: greeting });
    await this.persist('AI', greeting);

    this.maxTimer = setTimeout(() => {
      this.speak('I need to wrap up this call now. We will follow up by text. Goodbye!', true);
      this.finish({ reason: 'max_duration' });
    }, config.maxCallMinutes * 60e3);
    this.log('setup complete, lead', this.call.lead_id);
  }

  onPrompt(msg) {
    this.buffer += (msg.voicePrompt || '');
    if (msg.last === false) return;          // partial transcript — wait for the final one
    const text = this.buffer.trim();
    this.buffer = '';
    if (text) return this.onUserText(text);
  }

  onInterrupt(msg) {
    if (this.abort) this.abort.abort();
    // keep the history honest: the caller only heard part of our last reply
    const last = [...this.messages].reverse().find(m => m.role === 'assistant' && typeof m.content === 'string' && m.content);
    if (last && msg.utteranceUntilInterrupt !== undefined) last.content = String(msg.utteranceUntilInterrupt) + ' …';
  }

  async onUserText(text) {
    if (this.ended) return;
    if (this.abort) this.abort.abort();
    await this.run.catch(() => {});
    this.messages.push({ role: 'user', content: text });
    this.persist('CALLER', text);
    this.run = this.agent();
    return this.run;
  }

  // ------------------------------------------------------------ agent loop
  async agent() {
    const ctrl = new AbortController();
    this.abort = ctrl;
    try {
      for (let step = 0; step < config.maxAgentSteps; step++) {
        const params = {
          model: config.openaiModel,
          messages: this.messages,
          tools: OPENAI_TOOLS,
          tool_choice: 'auto',
          parallel_tool_calls: false,
          stream: true,
          max_completion_tokens: 800,
        };
        if (config.reasoningEffort && /^(gpt-5|o\d)/.test(config.openaiModel)) params.reasoning_effort = config.reasoningEffort;
        const stream = await this.openai.chat.completions.create(params, { signal: ctrl.signal });

        let text = '';
        const calls = {};
        for await (const chunk of stream) {
          const delta = chunk.choices && chunk.choices[0] && chunk.choices[0].delta;
          if (!delta) continue;
          if (delta.content) {
            text += delta.content;
            this.speak(delta.content, false);          // stream tokens straight to TTS
          }
          for (const tc of delta.tool_calls || []) {
            const c = calls[tc.index] || (calls[tc.index] = { id: '', name: '', args: '' });
            if (tc.id) c.id = tc.id;
            if (tc.function && tc.function.name) c.name += tc.function.name;
            if (tc.function && tc.function.arguments) c.args += tc.function.arguments;
          }
        }
        if (text) this.speak('', true);

        const toolCalls = Object.values(calls).filter(c => c.name);
        const assistant = { role: 'assistant', content: text || null };
        if (toolCalls.length) {
          assistant.tool_calls = toolCalls.map(c => ({ id: c.id, type: 'function', function: { name: c.name, arguments: c.args || '{}' } }));
        }
        this.messages.push(assistant);
        if (text) this.persist('AI', text);
        if (!toolCalls.length) break;

        for (const c of toolCalls) {
          let args = {};
          try { args = JSON.parse(c.args || '{}'); } catch { /* keep {} */ }
          if (SLOW_TOOLS.has(c.name) && !text) {
            this.speak(FILLERS[Math.floor(Math.random() * FILLERS.length)], true);
            text = 'filler';
          }
          const result = await this.execTool(c.name, args);
          this.messages.push({ role: 'tool', tool_call_id: c.id, content: JSON.stringify(result) });
          this.persist('TOOL', `${c.name}(${JSON.stringify(args)}) -> ${JSON.stringify(result)}`);
        }
        if (this.pendingEnd && this.pendingEnd.immediate) break;
        if (ctrl.signal.aborted) break;
      }
    } catch (e) {
      if (ctrl.signal.aborted || e.name === 'AbortError' || /abort/i.test(e.message || '')) return;
      this.log('agent error', e.message);
      this.speak("Sorry, I'm having a little trouble on my end. Let me get a team member to help you.", true);
      await callGateway('human_handoff', { reason: 'Voice agent error: ' + String(e.message).slice(0, 120) }, this.call);
      this.pendingEnd = { handoffData: { reason: 'agent_error' } };
    } finally {
      if (this.abort === ctrl) this.abort = null;
    }
    if (this.pendingEnd && !ctrl.signal.aborted) this.finish(this.pendingEnd.handoffData);
  }

  async execTool(name, args) {
    switch (name) {
      case 'end_call':
        this.pendingEnd = { handoffData: { reason: 'completed', detail: args.reason || null }, immediate: true };
        return { ok: true };
      case 'book_appointment':
      case 'reschedule_appointment':
        // first line of defence (n8n re-validates): only slots we actually looked up
        if (!this.lastSlots.some(s => s.iso === args.slot)) {
          return { ok: false, booked: false, error: 'That slot was not in the latest availability. Call get_available_slots and offer one of those.' };
        }
        break;
      default:
    }
    const r = await callGateway(name, args, this.call);
    if (name === 'get_available_slots' && r.ok) this.lastSlots = r.slots || [];
    if ((name === 'book_appointment' || name === 'reschedule_appointment') && r.booked) this.outcome = 'BOOKED';
    if ((name === 'book_appointment' || name === 'reschedule_appointment') && r.alternatives) this.lastSlots = r.alternatives;
    if (name === 'opt_out' && r.ok) this.outcome = 'OPTED_OUT';
    if (name === 'human_handoff' && r.ok) {
      this.outcome = 'HANDOFF';
      if (r.transfer && r.handoff_phone) this.pendingEnd = { handoffData: { reason: 'handoff', phone: r.handoff_phone, priority: r.priority } };
    }
    return r;
  }

  finish(handoffData) {
    if (this.ended) return;
    this.ended = true;
    // tell ConversationRelay to end the session; Twilio then calls <Connect action> with HandoffData
    setTimeout(() => this.send({ type: 'end', handoffData: JSON.stringify(handoffData || { reason: 'completed' }) }), 400);
  }

  async onClose() {
    clearTimeout(this.maxTimer);
    if (this.abort) this.abort.abort();
    if (!this.sessionId) return;
    await this.dbq;
    await db.finishSession(this.sessionId, {
      status: 'COMPLETED', outcome: this.outcome,
      metadata: { handoff: this.pendingEnd ? this.pendingEnd.handoffData : null },
    }).catch(e => this.log('db', e.message));
    await postCallEnded({ call_sid: this.call.call_sid, session_id: this.sessionId, lead_id: this.call.lead_id || null });
    this.log('closed, outcome', this.outcome);
  }
}

module.exports = { CallSession };

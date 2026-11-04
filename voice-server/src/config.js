'use strict';
// All configuration comes from environment variables (see ../.env.example). Secrets never live in code.

function req(name) {
  const v = process.env[name];
  if (!v) throw new Error(`Missing required env var ${name}`);
  return v;
}

const config = {
  port: Number(process.env.PORT || 8080),
  // Public host Twilio reaches, without scheme, e.g. "voice.primehome-roofing.com"
  publicHost: req('PUBLIC_HOST'),

  twilioAuthToken: process.env.TWILIO_AUTH_TOKEN || '',
  validateSignatures: process.env.VALIDATE_TWILIO_SIGNATURE !== 'false',

  openaiApiKey: req('OPENAI_API_KEY'),
  openaiBaseUrl: process.env.OPENAI_BASE_URL || undefined,          // optional (proxy / gateway)
  openaiModel: process.env.OPENAI_VOICE_MODEL || 'gpt-5.4-mini',   // fast model for low-latency speech
  reasoningEffort: process.env.OPENAI_REASONING_EFFORT || 'low',   // '' disables the parameter

  databaseUrl: req('DATABASE_URL'),

  // n8n endpoints (workflow 24A / 24B / 24C)
  n8nToolUrl: req('N8N_VOICE_TOOL_URL'),                   // https://n8n.example.com/webhook/roofradar/voice-tool
  n8nCallEndedUrl: req('N8N_VOICE_CALL_ENDED_URL'),        // https://n8n.example.com/webhook/roofradar/voice-call-ended
  n8nTransferResultUrl: req('N8N_VOICE_TRANSFER_RESULT_URL'), // https://user:pass@n8n.example.com/webhook/roofradar/voice-transfer-result
  gatewayKey: req('VOICE_GATEWAY_KEY'),                    // must match n8n credential "RoofRadar Voice Gateway Key"
  toolTimeoutMs: Number(process.env.TOOL_TIMEOUT_MS || 12000),

  // ConversationRelay voice settings (optional)
  language: process.env.VOICE_LANGUAGE || 'en-US',
  ttsProvider: process.env.TTS_PROVIDER || '',             // e.g. ElevenLabs | Google | Amazon  (empty = Twilio default)
  ttsVoice: process.env.TTS_VOICE || '',
  transcriptionProvider: process.env.STT_PROVIDER || '',   // e.g. Deepgram | Google
  maxAgentSteps: Number(process.env.MAX_AGENT_STEPS || 5),
  maxCallMinutes: Number(process.env.MAX_CALL_MINUTES || 15),
};

module.exports = config;

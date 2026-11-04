"""Shared JS snippets + SQL for AI workflows."""
import json


def prompt_js(p):
    """Python prompt template -> JS string literal (single braces, runtime {placeholders})."""
    return json.dumps(p.replace("{{", "{").replace("}}", "}"))


FILL_JS = r"""
function fill(t, vars) { return t.replace(/\{(\w+)\}/g, (m, k) => (vars[k] ?? m)); }
function buildOpenAI(model, system, userObj, maxTokens) {
  const req = {
    model,
    messages: [
      { role: 'system', content: system },
      { role: 'user', content: typeof userObj === 'string' ? userObj : JSON.stringify(userObj) }
    ],
    response_format: { type: 'json_object' },
    max_completion_tokens: maxTokens || 1500
  };
  // GPT-5 family / o-series are reasoning models: keep reasoning light for chat latency
  if (/^(gpt-5|o\d)/.test(model)) req.reasoning_effort = 'low';
  return req;
}
"""

PARSE_JS = r"""
const r = $json;
const content = r.choices?.[0]?.message?.content || '';
let parsed = null, parse_error = null;
try { parsed = JSON.parse(content); }
catch (e) {
  const m = content.match(/\{[\s\S]*\}/);
  if (m) { try { parsed = JSON.parse(m[0]); } catch (e2) { parse_error = e2.message; } }
  else parse_error = e.message || 'empty response';
}
return [{ json: {
  parsed, parse_error, raw: content,
  model: r.model || null,
  prompt_tokens: r.usage?.prompt_tokens ?? null,
  completion_tokens: r.usage?.completion_tokens ?? null
}}];
"""

LEAD_CTX_SQL = """
SELECT to_jsonb(l.*) AS lead,
       to_jsonb(o.*) AS org,
       rr_can_contact(l.id, coalesce(NULLIF($2::text,''), 'sms')) AS can_contact,
       (SELECT to_jsonb(a.*) FROM appointments a
         WHERE a.lead_id = l.id AND a.status IN ('SCHEDULED','CONFIRMED') AND a.start_at > NOW()
         ORDER BY a.start_at LIMIT 1) AS upcoming_appointment
FROM leads l JOIN organizations o ON o.id = l.organization_id
WHERE l.id = $1::uuid;
"""

SAVE_AI_SQL = """
INSERT INTO ai_interactions (organization_id, lead_id, conversation_id, workflow, model, input, output,
                             action, confidence, valid, error, prompt_tokens, completion_tokens)
VALUES ($1::uuid, $2::uuid, NULLIF($3::text,'')::uuid, $4::text, $5::text, $6::jsonb, $7::jsonb,
        NULLIF($8::text,''), $9::numeric, $10::boolean, NULLIF($11::text,''), $12::int, $13::int)
RETURNING id;
"""


def save_ai_args(ctx_node, wf_label, parse_node, validate_node, action_expr, conf_expr, req_node, conv_expr):
    P = f"$('{parse_node}').first().json"
    V = f"$('{validate_node}').first().json"
    C = f"$('{ctx_node}').first().json"
    return [
        f"{C}.org.id", f"{C}.lead.id", f"{conv_expr} || ''", f"'{wf_label}'",
        f"{P}.model || $('{req_node}').first().json.openai_request.model",
        f"JSON.stringify($('{req_node}').first().json.openai_request.messages.slice(1))",
        f"JSON.stringify({V})",
        f"{action_expr} || ''", f"{conf_expr} ?? null",
        f"{V}.valid", f"({V}.errors || []).join('; ')",
        f"{P}.prompt_tokens", f"{P}.completion_tokens",
    ]

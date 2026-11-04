#!/usr/bin/env bash
# Smoke-test a deployed RoofRadar instance with curl. Reads .env for URLs/secrets.
#   bash scripts/smoke-test.sh                 # safe tests (no real SMS unless TEST_PHONE is your phone)
#   TEST_PHONE=+1XXXXXXXXXX bash scripts/smoke-test.sh
# Then check results in Postgres (queries printed at the end).
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; [ -f .env ] && source <(grep -v '^\s*#' .env | sed 's/\s\+#.*$//' | grep '='); set +a

N8N="${N8N_URL:?set N8N_URL}"
PHONE="${TEST_PHONE:-+14155551234}"
BIZ="${BUSINESS_PHONE:-+15550100000}"
AUTH="${TWILIO_WEBHOOK_USER}:${TWILIO_WEBHOOK_PASSWORD}"
SID="SMtest$(date +%s)"
enc() { python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" "$1"; }
step() { printf "\n\033[1m%s\033[0m\n" "$*"; }

step "01  website lead (no consent → stored, nurture scheduled, no SMS)"
curl -sS -X POST "$N8N/webhook/primehome-lead" -H 'content-type: application/json' \
  -d "{\"name\":\"Smoke Test\",\"phone\":\"$PHONE\",\"email\":\"smoke@example.com\",\"address\":\"1 Test St, Newark, NJ\",\"problem_type\":\"roof leak\",\"message\":\"smoke test\"}"
echo

step "01  invalid lead (rejected + logged as INVALID_LEAD)"
curl -sS -X POST "$N8N/webhook/primehome-lead" -H 'content-type: application/json' -d '{"name":"","phone":"12"}'; echo

step "03  inbound SMS without auth (expect 401/403)"
curl -s -o /dev/null -w "HTTP %{http_code}\n" -X POST "$N8N/webhook/roofradar/sms" -d "MessageSid=$SID&From=$(enc "$PHONE")&To=$(enc "$BIZ")&Body=hi"

step "03  inbound SMS 'Do you do free inspections?' (→ 04 router → AI reply via 07)"
curl -sS -u "$AUTH" -X POST "$N8N/webhook/roofradar/sms" -d "MessageSid=$SID&From=$(enc "$PHONE")&To=$(enc "$BIZ")&Body=$(enc 'Do you do free inspections?')"; echo

step "03  same MessageSid again (idempotent: no second message row)"
curl -sS -u "$AUTH" -X POST "$N8N/webhook/roofradar/sms" -d "MessageSid=$SID&From=$(enc "$PHONE")&To=$(enc "$BIZ")&Body=$(enc 'Do you do free inspections?')"; echo

step "14  missed call (→ text-back)"
curl -sS -u "$AUTH" -X POST "$N8N/webhook/roofradar/call-status" -d "CallSid=CAtest$(date +%s)&From=$(enc "$PHONE")&To=$(enc "$BIZ")&DialCallStatus=no-answer&Direction=inbound"; echo

if [ -n "${VOICE_GATEWAY_KEY:-}" ]; then
  step "24A voice tool gateway find_lead"
  curl -sS -X POST "$N8N/webhook/roofradar/voice-tool" -H 'content-type: application/json' -H "X-RoofRadar-Key: $VOICE_GATEWAY_KEY" \
    -d "{\"tool\":\"find_lead\",\"args\":{},\"call\":{\"call_sid\":\"CAtest$(date +%s)\",\"from\":\"$PHONE\",\"to\":\"$BIZ\"}}"; echo
fi

step "21  STOP (→ lead STOPPED, follow-ups cancelled)"
curl -sS -u "$AUTH" -X POST "$N8N/webhook/roofradar/sms" -d "MessageSid=${SID}b&From=$(enc "$PHONE")&To=$(enc "$BIZ")&Body=STOP"; echo

cat <<SQL

Check in Postgres:
  SELECT name, status, lead_score FROM leads WHERE phone = '$PHONE';
  SELECT direction, left(body,80), created_at FROM messages m JOIN leads l ON l.id = m.lead_id WHERE l.phone = '$PHONE' ORDER BY m.created_at;
  SELECT workflow, event, created_at FROM workflow_events ORDER BY created_at DESC LIMIT 10;
  SELECT workflow_name, category, message FROM errors ORDER BY created_at DESC LIMIT 5;
SQL

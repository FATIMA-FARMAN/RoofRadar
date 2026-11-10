#!/bin/bash
# Lab demo scenarios: real webhooks into the deployed RoofRadar workflows.
B=${N8N_URL:-http://localhost:5678}/webhook
sms(){ curl -s -u twilio:${TWILIO_WEBHOOK_PASSWORD:-labhook} -X POST $B/roofradar/sms --data-urlencode "MessageSid=SM$(date +%s%N)" \
  --data-urlencode "AccountSid=${TWILIO_ACCOUNT_SID:-AClab0000000000000000000000000000}" --data-urlencode "From=$1" \
  --data-urlencode "To=+15550100000" --data-urlencode "Body=$2" >/dev/null; echo "SMS $1: $2"; }
lead(){ curl -s -X POST $B/primehome-lead -H 'Content-Type: application/json' -d "$1" >/dev/null; echo "LEAD $1" | cut -c1-90; }

# 1) Hero flow: web form -> instant text -> qualify -> real slots -> booked -> Slack
lead '{"name":"John Smith","phone":"+14155551234","email":"john@example.com","address":"123 Main Street, Newark, NJ","problem_type":"roof leak","message":"Water is coming through my ceiling","sms_consent":true,"consent_text":"I agree to receive texts from PrimeHome Roofing. Reply STOP to opt out."}'
sleep 10; sms +14155551234 "Yes, I need someone to inspect my roof."
sleep 14; sms +14155551234 "2"
sleep 14

# 2) Pricing question -> guarded -> human handoff (AI paused, Slack alert)
lead '{"name":"Sarah Lee","phone":"+12015550142","email":"sarah@example.com","address":"48 Oak Ave, Jersey City, NJ","problem_type":"missing shingles","message":"Storm took some shingles off","sms_consent":true,"consent_text":"I agree to receive texts. Reply STOP to opt out."}'
sleep 10; sms +12015550142 "How much does a new roof cost?"
sleep 12

# 3) Missed call -> text-back in seconds
curl -s -X POST $B/roofradar/call-status --data-urlencode "CallSid=CA$(date +%s%N)" --data-urlencode "From=+19735550177" \
  --data-urlencode "To=+15550100000" --data-urlencode "DialCallStatus=no-answer" -u twilio:${TWILIO_WEBHOOK_PASSWORD:-labhook} >/dev/null; echo "MISSED CALL +19735550177"
sleep 10

# 4) Duplicate Twilio retry (same MessageSid twice) -> processed once
SID="SMdup$(date +%s)"
for i in 1 2; do curl -s -u twilio:${TWILIO_WEBHOOK_PASSWORD:-labhook} -X POST $B/roofradar/sms --data-urlencode "MessageSid=$SID" --data-urlencode "From=+19735550177" \
  --data-urlencode "To=+15550100000" --data-urlencode "Body=Hi, can someone call me back about my gutters and roof?" >/dev/null; done; echo "DUP x2"
sleep 12

# 5) STOP -> opted out everywhere
sms +19735550177 "STOP"
sleep 8
echo done

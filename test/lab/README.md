# Lab: run the full system without live accounts

This is how the screenshots and demo video were produced. Every n8n node, SQL statement and branch runs for real;
only the external APIs (OpenAI, Twilio, Google Calendar, Slack, HubSpot) are answered by `stand-ins.js`, which
returns each API's real response format. The workflow JSON is not modified.

## 1. Stand-ins over HTTPS

```bash
# local CA + a certificate for the API hostnames
openssl req -x509 -newkey rsa:2048 -nodes -keyout ca.key -out ca.crt -days 30 -subj "/CN=RR Lab CA"
printf '[req]\ndistinguished_name=dn\n[dn]\n[ext]\nsubjectAltName=DNS:api.openai.com,DNS:www.googleapis.com,DNS:api.twilio.com,DNS:slack.com,DNS:api.hubapi.com,DNS:status.twilio.com,DNS:oauth2.googleapis.com\n' > san.cnf
openssl req -newkey rsa:2048 -nodes -keyout srv.key -out srv.csr -subj "/CN=lab"
openssl x509 -req -in srv.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out srv.crt -days 30 -extfile san.cnf -extensions ext

# point those hostnames at this machine (lab machine only!)
echo "127.0.0.1 api.openai.com www.googleapis.com api.twilio.com slack.com api.hubapi.com status.twilio.com oauth2.googleapis.com" | sudo tee -a /etc/hosts

sudo LAB_KEY=$PWD/srv.key LAB_CERT=$PWD/srv.crt node stand-ins.js   # :443 APIs, :9100 = log of every SMS/Slack sent
```

## 2. n8n + Postgres

```bash
NODE_EXTRA_CA_CERTS=$PWD/ca.crt N8N_RUNNERS_ENABLED=true GENERIC_TIMEZONE=America/New_York n8n start
psql "$DATABASE_URL" -f db/001_schema.sql -f db/002_seed.sql -f db/003_recovery.sql
psql "$DATABASE_URL" -c "UPDATE app_config SET value='\"http://localhost:5678\"' WHERE key='n8n_base_url';"
```

Create an owner and API key in n8n, fill `.env` (any non-empty OpenAI/Twilio/Slack secrets), import a Google
Calendar OAuth credential with a dummy token (`n8n import:credentials`), put its id in `CRED_GCAL_ID`, then:

```bash
python3 scripts/deploy.py
```

## 3. Run the demo

```bash
bash test/lab/demo-scenarios.sh      # web lead → booking, price question → handoff, missed call, duplicate webhook, STOP
curl -s localhost:9100 | jq          # every text and Slack alert the system sent
```

Expected: John Smith `BOOKED`, Sarah Lee `HUMAN_HANDOFF`, the missed caller `STOPPED`, and every execution
`success` in n8n.

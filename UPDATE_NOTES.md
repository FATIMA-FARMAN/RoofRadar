# RoofRadar update: workflows 25–26 + fixes

## What changed
| Workflow | Change |
|---|---|
| RR 04 · AI Router | Rates each message's sentiment; an ANGRY customer always goes to a person |
| RR 06 · AI Conversation | Never re-asks for details already known (address from the form, name, problem). New NO_SHOW and REENGAGE message modes |
| RR 10 · Book Appointment | Calendar note and Slack alert no longer show "Urgency: UNKNOWN" or "Lead score: 0" |
| RR 25 · No-show Recovery (new) | After a person marks an inspection NO_SHOW, texts one kind rebook message with real open times |
| RR 26 · Lead Re-engagement (new) | Daily: one check-in text to leads quiet 30–180 days, at most every 90 days |
| db/003_recovery.sql (new) | Adds appointments.no_show_recovery_sent_at |

The 2-hour reminder already existed in RR 11 (settings.reminders.h2), so nothing was needed there.

## Mark a missed inspection (for RR 25)
    UPDATE appointments SET status = 'NO_SHOW' WHERE id = '<appointment id>';

## Turn re-engagement off for a client
    UPDATE organizations SET settings = jsonb_set(settings, '{reengagement}', '{"enabled": false}');

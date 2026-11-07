# Kestrel Incident Runbook

Owner: Platform Reliability team, Harbor Lane Logistics (fictional company, sample document for Pop Quiz).
Applies to: the Kestrel parcel tracking platform (public tracking API, courier mobile app backend, and the event pipeline).
Last reviewed: 3 October 2026. Next review: 3 January 2027.

## 1. Purpose and scope

This runbook tells the on-call engineer what to do from the first alert to the post-incident review. It covers outages and degradations of the tracking API, the courier backend, the event pipeline and the PostgreSQL clusters behind them. It does not cover warehouse hardware, the billing system or the marketing website, which have their own runbooks.

Follow the steps in order. When a step does not apply, write down why in the incident channel and move on. Speed matters, but a written trail matters more: every action taken during an incident must be posted in the incident channel with a timestamp.

## 2. Severity levels

Kestrel uses four severity levels. The incident commander may raise or lower the severity at any time.

- **SEV1**: tracking is unavailable for more than 25 percent of requests, or parcel events are being lost. Customer impact is immediate and visible.
- **SEV2**: the platform works but is degraded, for example when the p95 latency of the tracking API stays above 800 milliseconds for 10 minutes, or when courier scans are delayed by more than 15 minutes.
- **SEV3**: a single internal component is failing with no visible customer impact yet, such as one replica out of rotation.
- **SEV4**: cosmetic issues and anything that can wait for the next business day.

A SEV1 is declared by anyone who sees the symptoms. You never need permission to declare a SEV1; you only need permission to downgrade one.

## 3. Roles

Every SEV1 and SEV2 has three roles, filled in this order.

The **incident commander** coordinates the response, decides on severity, and is the only person who approves risky changes such as failovers or rollbacks of more than one service. The commander does not type commands; their job is to keep the team focused.

The **operations lead** investigates and executes changes. For a SEV1 the operations lead may pull in up to two additional engineers without asking the commander.

The **communications lead** writes the public status page updates and answers the customer support team. Status page updates are due every 30 minutes during a SEV1 and every 60 minutes during a SEV2, even when there is nothing new to report.

The primary on-call engineer acts as incident commander until someone else takes over explicitly with the words "I am taking command".

## 4. On-call and response times

The on-call rotation changes every Monday at 10:00 local time. Each shift has a primary and a secondary engineer.

The primary must acknowledge a page within 5 minutes. If the page is not acknowledged after 5 minutes, it escalates to the secondary. If the secondary has not acknowledged after another 10 minutes, the page escalates to the engineering manager on duty.

For a SEV1, the incident channel must be opened within 15 minutes of the first page, and the first status page update must be published within 20 minutes of the first page. For a SEV2, the first status page update is due within 45 minutes.

On-call engineers must be able to reach a laptop within 15 minutes at any time during their shift. Swapping shifts is allowed if the swap is recorded in the rotation tool at least 24 hours in advance.

## 5. Alert thresholds

The following alerts page the primary on-call engineer directly. Other alerts go to the team channel only.

| Alert | Threshold | Severity on page |
|---|---|---|
| Tracking API error rate | above 2 percent for 5 minutes | SEV2 |
| Tracking API error rate | above 10 percent for 2 minutes | SEV1 |
| Tracking API p95 latency | above 800 ms for 10 minutes | SEV2 |
| Event pipeline consumer lag | above 50,000 messages | SEV2 |
| PostgreSQL replication lag | above 120 seconds | SEV2 |
| Disk usage on a database node | above 85 percent | SEV3 |
| Certificate expiry | less than 14 days | SEV3 |

When an alert fires more than 3 times in 24 hours without a real incident, the on-call engineer opens a ticket to tune it. Noisy alerts are treated as bugs.

## 6. First fifteen minutes

1. Acknowledge the page.
2. Open the incident channel with the bot command `/incident open` and paste the alert link.
3. Check the deploy log. About 70 percent of Kestrel incidents in the last year started within one hour of a deploy, so a recent deploy is the first suspect.
4. Check the status dashboards: error rate, latency, consumer lag and database health.
5. Decide the severity and announce it in the channel.
6. If the incident is a SEV1, page the secondary on-call engineer immediately instead of waiting for escalation.

Do not start debugging in a private message. If it is not in the incident channel, it did not happen.

## 7. Rollback policy

If a deploy happened in the 2 hours before the incident started, roll it back first and investigate afterwards. A rollback of a single service does not need approval from the incident commander. A rollback that touches more than one service needs the commander's explicit approval in the channel.

Database migrations are never rolled back automatically. A migration rollback requires the database owner on call and a written plan in the incident channel.

Feature flags are the preferred way to disable new behaviour. Turning a flag off is always allowed during an incident and does not count as a rollback.

## 8. Database failover

The primary PostgreSQL cluster for Kestrel runs in the Northgate region with a synchronous standby in the same region and an asynchronous replica in the Eastport region.

Fail over to the synchronous standby when the primary is unreachable for more than 3 minutes and the standby reports a replication lag below 5 seconds. This failover is automated, but the operations lead must confirm it in the channel.

Fail over to the Eastport replica only as a last resort, because it can lose up to 60 seconds of writes. This cross-region failover requires approval from the incident commander and from the database owner on call. Once it is done, set the courier app to read-only mode until the data team has checked for lost parcel events.

Never fail over during an active schema migration.

## 9. Data retention and backups

Kestrel follows the company retention policy below. The values are minimums for compliance and maximums for privacy; do not keep data longer without approval from the privacy office.

- Application logs are kept for 30 days.
- Audit logs are kept for 400 days.
- Raw GPS traces from courier devices are deleted after 7 days; only the computed delivery route is kept.
- Parcel event history is kept for 2 years after delivery.
- Incident channel transcripts are exported and kept for 3 years.

Full database backups run every night at 02:00 UTC, and write-ahead logs are archived every 5 minutes, which gives a recovery point objective of 5 minutes. Backups are encrypted with AES-256 and kept for 35 days. A restore test is performed every quarter on a randomly chosen backup; a failed restore test is a SEV3 incident by itself.

## 10. Communication rules

The communications lead owns all external messages. Engineers must not post on social media or answer customers directly during an incident.

Status page updates follow a fixed template: what is affected, what we are doing, and when the next update will be published. Never promise a time of resolution. Never name a supplier or a cloud provider as the cause before the post-incident review confirms it.

Customer support receives a short summary every 30 minutes during a SEV1, at the same time as the status page update, so that the two messages stay consistent.

When the incident is resolved, the communications lead posts a final status update and keeps the status page entry open for 24 hours in monitoring state before closing it.

## 11. Closing an incident

An incident is resolved when the metrics that triggered it have been back to normal for 30 minutes. The incident commander announces the resolution in the channel and records the start time, detection time and resolution time.

Every SEV1 and SEV2 needs a post-incident review. The written review is due within 5 business days, and the review meeting is held within 10 business days. Reviews are blameless: they describe what the system and the process allowed to happen, not who made a mistake.

Each review produces action items with an owner and a due date. Action items from a SEV1 review must be finished within 30 days; if one cannot be finished in time, the owner explains why at the weekly reliability meeting.

## 12. Useful contacts and tools

- Incident bot: `/incident open`, `/incident severity`, `/incident resolve`.
- Rotation tool: the team calendar named "Kestrel On-call".
- Database owner on call: listed in the rotation tool under "DB owner".
- Privacy office: privacy@harborlane.example (fictional address).

Keep this runbook short. If a section grows beyond one screen, move the details into a linked document and keep only the decision rule here.

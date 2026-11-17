---
date: 2026-11-17
type: added
impact: high
title: Escalate a withdrawal to destruction while keeping its holds
area: Consent records
link: distribution:consent-list
---
Managers can escalate an active no-further-use withdrawal to destruction from its consent record. Consent stays withdrawn and its existing holds remain in place; the original instruction and escalation remain visible in history. Disposal still requires an ordinary request and a different person's approval.

## Technical notes

Adds the additive migration `distribution.0003_withdrawal_escalation` with append-only history protection and a database guard that keeps older writers from bypassing escalation-specific reinstatement checks. Keep this migration applied during code rollback; older pages do not display escalation details and an unsafe reinstatement fails. Do not reverse the migration after history has been recorded. No dependency or environment changes.

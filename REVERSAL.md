# Autopay reversal — the decision layer

This document covers the part of `qna-action-mcp` that decides whether a UPI
Autopay debit gets reversed automatically or goes to a human advisor.

## What was missing

Before this, `initiate_refund_or_reversal` could detect the intent, collect
the required fields and open a ticket. What it could not do was reverse
anything — there was no money movement in `core.execute_action`, only
`create_ticket` and `create_notification`.

Escalation was decided by `escalate_keywords`: if the customer's own words
contained "fraud", "twice", "not me" and so on, the case went to a human;
otherwise it became a ticket. That has two failure modes that matter:

- **It cannot confirm anything.** A customer saying "this was unauthorised"
  is a claim, not evidence. Keyword matching has no way to check it.
- **It punishes accurate description.** Someone correctly reporting a real
  duplicate debit says "twice" — and gets escalated, when their case is the
  most objectively provable one in the whole set.

So no complaint ever resolved itself, and the word that decided a case was
chosen by the person asking for the money.

## The rule

> Reverse money automatically **only** when the bank can prove the breach
> from its own records. Everything else goes to a human.

"Genuine" is not a mood read off the complaint text. A complaint is genuine,
for auto-reversal purposes, when the mandate registry, the debit ledger and
the pre-debit notice log — all bank-side data the customer cannot influence —
contradict the debit that was taken.

That gives two families of reason code.

**Verifiable** — the ledger alone settles it, no merchant needs to be asked:

| Reason code | What the ledger check proves |
|---|---|
| `MANDATE_REVOKED_BEFORE_DEBIT` | debit timestamp is after the recorded revocation |
| `DEBIT_AFTER_MANDATE_EXPIRY` | debit timestamp is after `valid_until` |
| `AMOUNT_EXCEEDS_MANDATE_CAP` | debit amount is above the registered cap |
| `DUPLICATE_DEBIT` | same mandate, same amount, inside 24 hours |
| `MISSING_PRE_DEBIT_NOTIFICATION` | no notice on record, or one sent under 24h ahead |

**Contested** — settling it needs someone outside the bank's records, so it
is never auto-reversible however sympathetic the complaint:

| Reason code | Why a human |
|---|---|
| `UNAUTHORIZED_MANDATE` | a false positive funds the fraudster; analyst signs off |
| `SERVICE_NOT_RENDERED` | turns on what the merchant did |
| `SUBSCRIPTION_CANCELLED_WITH_MERCHANT` | cancelled with the merchant, not with us |
| `AMOUNT_DISPUTED_WITH_MERCHANT` | pricing dispute, needs representment |

## Guardrails

A confirmed breach is *necessary* for auto-reversal, not *sufficient*. Any
of these pulls a verified case back to a human, with the finding and the
amount already worked out so the advisor only has to approve:

- amount over ₹25,000
- customer already granted 3 auto-reversals in 90 days
- account frozen, or KYC not verified
- debit older than the 90-day dispute window
- debit already reversed (idempotency — a retried tool call must not pay twice)

All tunable in `reversal_policy.Limits`.

## Note on rejection

There is no auto-reject outcome. A bank does not close a customer's money
complaint without a person signing off, so when the ledger contradicts the
claim the case still escalates — with `recommendation: LIKELY_INVALID` and
the contradiction spelled out. The worst the agent does on its own is
disagree in writing.

## How classification stays safe

`classify_complaint` is keyword matching, which is crude. It is safe anyway,
because classification only chooses **which ledger check runs** — it never
decides that money moves. Route a complaint to the wrong check and the check
fails against the ledger and the case escalates. The failure mode of a
mis-read complaint is a slower answer, never a wrong payout.
(`test_misclassification_cannot_cause_a_payout` pins this.)

An LLM host can pass `reason_code` explicitly when it has read the complaint
and is confident. The engine will use that code's check — and still refuse
to reverse if the check fails.

## Files

```
mcp-server/
  reversal_models.py         dataclasses; money is integer paise, never float
  reversal_policy.py         the decision engine — PURE, no I/O, no clock, no model calls
  reversal_ledger.py         account records (JSON, same style as core.py) + reversal/mandate writes + audit log
  reversal.py                orchestration: carry a decision out, open the ticket, draft the customer SMS
  seed_reversal_ledger.py    builds the nine demo scenarios
  demo_reversal.py           end-to-end run, no LLM and no network
  test_reversal_policy.py    66 tests over the engine
data/
  accounts.json  mandates.json  transactions.json  predebit_notices.json  reversal_audit.json
```

The split between `reversal_policy.py` and `reversal.py` is the important
one. The policy engine is a pure function of `ComplaintContext`: same inputs,
same decision, forever — which is what makes the audit log worth anything,
and what makes 66 tests run in 0.14 seconds. Everything that can move money
lives in the other file, which is short enough to read in one sitting.

## Running it

```bash
cd mcp-server
python seed_reversal_ledger.py     # build the account records (once)
python demo_reversal.py            # all nine complaints
python demo_reversal.py TXN1001    # just one
python -m pytest test_reversal_policy.py -q
```

The demo needs no API key and no network — it exercises the real decision
path, not a script.

## New MCP tools

`mcp_server.py` gained four read/judgement tools and its refund tool now
actually reverses:

| Tool | Effect |
|---|---|
| `get_transaction` | look up a debit and its mandate terms |
| `list_customer_debits` | when the customer doesn't know the txn id |
| `assess_reversal` | run the full engine, return decision + evidence, **change nothing** |
| `get_reversal_audit` | append-only log of every reversal, mandate change and escalation |
| `initiate_refund_or_reversal` | now reverses when the engine confirms, escalates otherwise |

`assess_reversal` exists so the model can explain what will happen before it
happens. The old `initiate_refund_or_reversal` behaviour is preserved as a
fallback for any transaction the ledger has no record of, and for the other
three domains — nothing regresses.

## The nine demo scenarios

| Txn | Situation | Outcome |
|---|---|---|
| TXN1001 | debited 3 days after the mandate was revoked | AUTO_REVERSE |
| TXN1002 | ₹999 debited against a ₹500 cap | AUTO_REVERSE |
| TXN1004 | ₹1,199 debited twice, 3 hours apart | AUTO_REVERSE |
| TXN1005 | no pre-debit notice on record at all | AUTO_REVERSE |
| TXN1006 | mandate expired — but ₹48,000, over the ceiling | ESCALATE → high-value-reversals |
| TXN1007 | notice sent 2h ahead — but 4th claim in 90 days | ESCALATE → risk-review |
| TXN1008 | "I never set up this mandate" | ESCALATE → fraud-ops, mandate paused |
| TXN1009 | cancelled with the merchant, not with the bank | ESCALATE → merchant-disputes |
| TXN1010 | claims a duplicate; ledger shows one debit | ESCALATE → LIKELY_INVALID |

Four escalate for four different reasons, which is the point: "escalate to a
human" is not one behaviour. A fraud case gets the mandate stopped in the
same second; a high-value case arrives with the amount pre-computed; a
contradicted claim arrives with the contradiction spelled out.

## Known gaps

- `execute_reversal` writes to a JSON file. Real money movement needs a
  transactional store and an idempotency key from the caller, not just the
  txn id.
- Reversals increment `auto_reversals_90d` but nothing ages it back down —
  a real implementation would count reversal rows inside a 90-day window.
- The pre-debit notice check trusts that a missing row means no notice was
  sent. If the notification service can drop rows, that assumption pays out
  money it shouldn't.
- `mcp` 2.x renamed `FastMCP` to `MCPServer`. `requirements-mcp.txt` pins
  `mcp[cli]==1.28.1`, so this is fine — but don't unpin it casually.

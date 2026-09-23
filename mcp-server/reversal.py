"""Orchestration: turn a reversal decision into the actions that carry it out.

The split is deliberate. `reversal_policy.decide` works out *what should
happen* and touches nothing. This module carries it out — moves money,
stops mandates, opens a ticket through `core.execute_action` so escalations
land in the same tickets.json the rest of the system already uses.

Keeping them apart means the hard part (the decision) is a pure function
you can test exhaustively in milliseconds, and the risky part (the side
effects) is a short file you can read in one sitting.
"""

from __future__ import annotations

from typing import Optional

import reversal_ledger as ledger
from reversal_models import MandateStatus, rupees
from reversal_policy import Decision, Limits, Outcome, ReasonCode, decide


# ---------------------------------------------------------------------------
# Customer-facing copy
# ---------------------------------------------------------------------------


def _reversal_message(ctx, decision: Decision, reversal: dict, mandate_stopped: bool) -> str:
    why = {
        ReasonCode.MANDATE_REVOKED_BEFORE_DEBIT:
            "this debit was taken after you had already cancelled the autopay mandate",
        ReasonCode.DEBIT_AFTER_MANDATE_EXPIRY:
            "this debit was taken after your mandate had expired",
        ReasonCode.AMOUNT_EXCEEDS_MANDATE_CAP:
            f"this debit was above the {rupees(ctx.mandate.max_amount_paise)} limit you "
            "had approved for this mandate",
        ReasonCode.DUPLICATE_DEBIT:
            "you were charged the same amount twice on this mandate",
        ReasonCode.MISSING_PRE_DEBIT_NOTIFICATION:
            "we did not send you the required 24-hour notice before this debit",
    }.get(decision.reason_code, "this debit did not match the mandate on record")

    if mandate_stopped:
        extra = (f" We have also stopped the {ctx.mandate.merchant_name} mandate so this "
                 "cannot happen again.")
    elif decision.pause_mandate:
        extra = (f" The mandate is already cancelled on our side — we're taking this up "
                 f"with {ctx.mandate.merchant_name} so it doesn't recur.")
    else:
        extra = ""

    return (
        f"Hi {ctx.customer.name.split()[0]}, we checked your complaint about the "
        f"{rupees(ctx.transaction.amount_paise)} {ctx.mandate.merchant_name} debit on "
        f"{ctx.transaction.debited_at:%d %b}. You're right — {why}. We've reversed "
        f"{reversal['amount']} to your account (ref {reversal['reversal_txn_id']}); it "
        f"should show within 1 working day.{extra}"
    )


def _escalation_message(ctx, decision: Decision, ticket_id: Optional[str]) -> str:
    sla = {"HIGH": "4 hours", "NORMAL": "24 hours", "LOW": "48 hours"}.get(
        decision.priority or "NORMAL", "24 hours"
    )
    ref = f" as case {ticket_id}" if ticket_id else ""
    extra = ""
    if decision.pause_mandate:
        extra = (f" As a precaution we've paused the {ctx.mandate.merchant_name} mandate, "
                 "so nothing further will be debited while we look into it.")
    return (
        f"Hi {ctx.customer.name.split()[0]}, thanks for flagging the "
        f"{rupees(ctx.transaction.amount_paise)} {ctx.mandate.merchant_name} debit on "
        f"{ctx.transaction.debited_at:%d %b}. This one needs a person to look at it, so "
        f"I've passed it to our team{ref}. They'll come back to you within {sla}.{extra}"
    )


def _case_summary(ctx, decision: Decision) -> str:
    """What the human advisor opens the ticket to. Front-load the finding."""
    lines = [
        f"{decision.reason_code.value} — {rupees(ctx.transaction.amount_paise)} debited by "
        f"{ctx.mandate.merchant_name} on {ctx.transaction.debited_at:%d %b %Y}",
        "",
        f"Agent assessment: {decision.rationale}",
        "",
        "Customer said:",
        f'  "{ctx.complaint_text.strip()}"',
        "",
        "Checks run:",
    ]
    lines += [f"  {e}" for e in decision.evidence]
    if decision.reversal_amount_paise:
        lines += ["", f"Reversal amount if approved: {rupees(decision.reversal_amount_paise)}"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def resolve(
    txn_id: str,
    complaint_text: str,
    reason_code: str | None = None,
    limits: Limits | None = None,
    dry_run: bool = False,
    actor: str = "autopay-agent",
    domain: str = "banking",
    intent: dict | None = None,
    domains: dict | None = None,
    fields: dict | None = None,
) -> dict:
    """Assess one autopay complaint and, unless dry_run, carry the decision out.

    Pass `intent` and `domains` (as the MCP server has them loaded) and any
    escalation is written into tickets.json through `core.execute_action`,
    so it shows up wherever tickets already show up. Without them the
    decision still runs — useful for tests and the demo script — and the
    escalation is recorded in the reversal audit log only.
    """
    ctx = ledger.build_context(txn_id, complaint_text)
    decision = decide(
        ctx, limits=limits, reason_code=ReasonCode(reason_code) if reason_code else None
    )

    result: dict = {
        "ok": True,
        "txn_id": txn_id,
        "outcome": decision.outcome.value,
        "reason_code": decision.reason_code.value,
        "rationale": decision.rationale,
        "evidence": [str(e) for e in decision.evidence],
        "evidence_details": [
            {"check": e.check, "result": e.result, "detail": e.detail}
            for e in decision.evidence
        ],
        "amount": rupees(decision.reversal_amount_paise) if decision.reversal_amount_paise else None,
        "queue": decision.queue,
        "priority": decision.priority,
        "recommendation": decision.recommendation.value if decision.recommendation else None,
        "actions": [],
        "customer_message": "",
        "case_id": None,
        "reversal_txn_id": None,
    }

    if dry_run:
        if decision.outcome is Outcome.AUTO_REVERSE:
            result["customer_message"] = _reversal_message(
                ctx, decision,
                {"amount": rupees(decision.reversal_amount_paise), "reversal_txn_id": "PREVIEW"},
                decision.pause_mandate,
            )
        else:
            result["customer_message"] = _escalation_message(ctx, decision, ticket_id=None)
        result["actions"].append({"action": "DRY_RUN", "detail": "no side effects performed"})
        return result

    if decision.outcome is Outcome.AUTO_REVERSE:
        reversal = ledger.execute_reversal(
            txn_id=txn_id,
            amount_paise=decision.reversal_amount_paise,
            reason_code=decision.reason_code.value,
            decided_by=actor,
        )
        result["reversal_txn_id"] = reversal.get("reversal_txn_id")
        result["actions"].append({"action": "REVERSAL", **reversal})

        stopped = False
        if decision.pause_mandate:
            if ctx.mandate.status is not MandateStatus.REVOKED:
                result["actions"].append({
                    "action": "MANDATE_STOP",
                    **ledger.set_mandate_status(ctx.mandate.mandate_id, "CANCEL", actor),
                })
                stopped = True
            else:
                # Already revoked on our side, so the failure was the merchant
                # continuing to present it. Stopping it again changes nothing;
                # the merchant is what needs chasing.
                result["actions"].append({
                    "action": "MANDATE_NOOP",
                    "mandate_id": ctx.mandate.mandate_id,
                    "message": (
                        f"{ctx.mandate.mandate_id} was already REVOKED at the bank; "
                        f"{ctx.mandate.merchant_name} presented a collection against a "
                        "revoked mandate. Flagged for merchant follow-up."
                    ),
                })

        result["customer_message"] = _reversal_message(ctx, decision, reversal, stopped)
        return result

    # --- escalation ------------------------------------------------------
    # Containment first: if the mandate is suspect, stop it before the case
    # sits in a queue waiting for a human.
    if decision.pause_mandate:
        result["actions"].append({
            "action": "MANDATE_PAUSE",
            **ledger.set_mandate_status(ctx.mandate.mandate_id, "PAUSE", actor),
        })

    ticket_id = None
    summary = _case_summary(ctx, decision)

    if intent is not None and domains is not None:
        import core  # imported lazily so tests and the demo don't need the configs

        ticket = core.execute_action(
            action_name="create_ticket",
            domain_name=domain,
            intent=intent,
            fields={
                **(fields or {}),
                "reversal_queue": decision.queue,
                "reversal_priority": decision.priority,
                "recommendation": decision.recommendation.value if decision.recommendation else "",
                "reason_code": decision.reason_code.value,
                "amount_paise_if_approved": decision.reversal_amount_paise,
            },
            context=summary,
            domains=domains,
        )
        ticket_id = ticket["id"]
        result["actions"].append({"action": "CASE", "ticket": ticket})

    ledger.audit(actor, "ESCALATION", txn_id, {
        "queue": decision.queue,
        "priority": decision.priority,
        "recommendation": decision.recommendation.value if decision.recommendation else None,
        "reason_code": decision.reason_code.value,
        "ticket_id": ticket_id,
    })

    result["case_id"] = ticket_id
    result["queue"] = decision.queue
    result["priority"] = decision.priority
    result["recommendation"] = decision.recommendation.value if decision.recommendation else None
    result["case_summary"] = summary
    result["escalated"] = True
    result["customer_message"] = _escalation_message(ctx, decision, ticket_id)
    return result

"""The reversal decision policy.

The whole system turns on one rule:

    Reverse money automatically ONLY when the bank can prove the breach
    from its own records. Everything else goes to a human.

"Genuine" is not a mood the model reads off the complaint text. A complaint
is genuine, for auto-reversal purposes, when the mandate registry, the
transaction ledger and the pre-debit notification log — all bank-side data
the customer cannot influence — contradict the debit that was taken.

That gives two families of reason code:

  VERIFIABLE   the ledger alone settles it. Debited after the customer
               revoked the mandate; debited above the mandate cap; debited
               twice; debited with no pre-debit notice; debited after
               expiry. No merchant needs to be asked. Auto-reversible.

  CONTESTED    settling it needs someone outside the bank's records — the
               merchant's word on whether a service was delivered, a fraud
               analyst's read on whether a mandate was really authorised.
               Never auto-reversible, however sympathetic the complaint.

On top of a verified breach sit guardrails that can still pull a case back
to a human: amount too large, a customer claiming too often, a frozen
account, a stale transaction. A breach being real is necessary for
auto-reversal, not sufficient.

Everything here is a pure function of ComplaintContext. No I/O, no clock
reads, no model calls. Same inputs, same decision, forever — which is what
makes the audit log worth anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum
from typing import Optional

from reversal_models import (
    ComplaintContext,
    Frequency,
    MandateStatus,
    TxnStatus,
    rupees,
)


# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------


class ReasonCode(str, Enum):
    # Verifiable from bank records alone -> eligible for auto-reversal
    MANDATE_REVOKED_BEFORE_DEBIT = "MANDATE_REVOKED_BEFORE_DEBIT"
    DEBIT_AFTER_MANDATE_EXPIRY = "DEBIT_AFTER_MANDATE_EXPIRY"
    AMOUNT_EXCEEDS_MANDATE_CAP = "AMOUNT_EXCEEDS_MANDATE_CAP"
    DUPLICATE_DEBIT = "DUPLICATE_DEBIT"
    MISSING_PRE_DEBIT_NOTIFICATION = "MISSING_PRE_DEBIT_NOTIFICATION"

    # Needs a party outside the bank -> always human
    SERVICE_NOT_RENDERED = "SERVICE_NOT_RENDERED"
    SUBSCRIPTION_CANCELLED_WITH_MERCHANT = "SUBSCRIPTION_CANCELLED_WITH_MERCHANT"
    AMOUNT_DISPUTED_WITH_MERCHANT = "AMOUNT_DISPUTED_WITH_MERCHANT"
    UNAUTHORIZED_MANDATE = "UNAUTHORIZED_MANDATE"

    UNKNOWN = "UNKNOWN"


VERIFIABLE_CODES = {
    ReasonCode.MANDATE_REVOKED_BEFORE_DEBIT,
    ReasonCode.DEBIT_AFTER_MANDATE_EXPIRY,
    ReasonCode.AMOUNT_EXCEEDS_MANDATE_CAP,
    ReasonCode.DUPLICATE_DEBIT,
    ReasonCode.MISSING_PRE_DEBIT_NOTIFICATION,
}

CONTESTED_CODES = {
    ReasonCode.SERVICE_NOT_RENDERED,
    ReasonCode.SUBSCRIPTION_CANCELLED_WITH_MERCHANT,
    ReasonCode.AMOUNT_DISPUTED_WITH_MERCHANT,
    ReasonCode.UNAUTHORIZED_MANDATE,
}


class Outcome(str, Enum):
    AUTO_REVERSE = "AUTO_REVERSE"
    ESCALATE = "ESCALATE"


class Recommendation(str, Enum):
    """What the agent tells the human reviewer it thinks, when it escalates.

    Note there is no auto-reject. A bank does not close a customer's money
    complaint without a person signing off, so the worst the agent does on
    its own is escalate with LIKELY_INVALID and the evidence attached.
    """

    LIKELY_VALID = "LIKELY_VALID"
    LIKELY_INVALID = "LIKELY_INVALID"
    NEEDS_MERCHANT_INPUT = "NEEDS_MERCHANT_INPUT"
    FRAUD_REVIEW = "FRAUD_REVIEW"
    ABUSE_REVIEW = "ABUSE_REVIEW"


@dataclass
class Limits:
    """Tunables. Everything a risk team would want to move lives here."""

    auto_reverse_max_paise: int = 25_000_00      # ₹25,000
    max_auto_reversals_90d: int = 3
    dispute_window_days: int = 90
    pre_debit_notice_hours: int = 24
    duplicate_window_hours: int = 24


@dataclass
class Evidence:
    check: str
    result: str  # PASS | FAIL | INFO
    detail: str

    def __str__(self) -> str:
        mark = {"PASS": "✓", "FAIL": "✗", "INFO": "·"}.get(self.result, "?")
        return f"{mark} {self.check}: {self.detail}"


@dataclass
class Decision:
    outcome: Outcome
    reason_code: ReasonCode
    rationale: str
    evidence: list[Evidence] = field(default_factory=list)
    reversal_amount_paise: int = 0
    queue: Optional[str] = None
    priority: Optional[str] = None
    recommendation: Optional[Recommendation] = None
    # Fraud and revoked-mandate cases want the mandate stopped immediately,
    # whether or not the money moves back today.
    pause_mandate: bool = False

    @property
    def is_auto(self) -> bool:
        return self.outcome is Outcome.AUTO_REVERSE


# --------------------------------------------------------------------------
# Step 1 — classify the complaint into a reason code
# --------------------------------------------------------------------------

# Ordered: the first pattern that matches wins, so put the narrow and the
# high-stakes patterns above the broad ones. Fraud outranks everything.
_PATTERNS: list[tuple[ReasonCode, str]] = [
    (
        ReasonCode.UNAUTHORIZED_MANDATE,
        r"never (set ?up|created|authoris|authoriz|approved|registered)"
        r"|didn'?t (set ?up|authoris|authoriz|approve)"
        r"|don'?t recognis|do not recognis|don'?t recogniz"
        r"|fraud|unauthoris|unauthoriz|someone else|not my (mandate|subscription)",
    ),
    (
        ReasonCode.MANDATE_REVOKED_BEFORE_DEBIT,
        # Verb, then up to three intervening words (a merchant name, "the",
        # "my"), then the mandate noun: "cancelled the Netflix autopay mandate".
        r"(cancel|revok|stopp?ed|withdrew|withdrawn|remov)\w*\s+(?:\w+\s+){0,3}"
        r"(mandate|autopay|auto ?pay|auto[- ]?debit|standing instruction)\b"
        r"|(mandate|autopay|auto ?pay|auto[- ]?debit)\s+(?:\w+\s+){0,2}"
        r"(was\s+)?(cancel|revok|stopp)\w*",
    ),
    (
        ReasonCode.DUPLICATE_DEBIT,
        r"twice|two times|double|duplicate|deducted again|charged again|same amount again",
    ),
    (
        ReasonCode.AMOUNT_EXCEEDS_MANDATE_CAP,
        r"more than (i|the|my)|higher than|exceed|above the (limit|cap|max)"
        r"|charged extra|took more|limit was",
    ),
    (
        ReasonCode.MISSING_PRE_DEBIT_NOTIFICATION,
        r"no (sms|notification|notice|intimation|alert|message|warning)"
        r"|without (any )?(sms|notification|notice|intimation|alert|warning|informing)"
        r"|wasn'?t (notified|informed)|never (notified|informed|got (an? )?(sms|alert))",
    ),
    (
        ReasonCode.DEBIT_AFTER_MANDATE_EXPIRY,
        r"expir|mandate (had )?ended|after (it|the mandate) ended|lapsed",
    ),
    (
        ReasonCode.SUBSCRIPTION_CANCELLED_WITH_MERCHANT,
        r"cancel\w*\s+(?:\w+\s+){0,3}(subscription|plan|membership|account)"
        r"|unsubscrib|ended my (plan|subscription|membership)",
    ),
    (
        ReasonCode.SERVICE_NOT_RENDERED,
        r"never (got|received|delivered)|not deliver|didn'?t (get|receive)"
        r"|no service|service (was )?(not|never)|stopped working|never activated",
    ),
    (
        ReasonCode.AMOUNT_DISPUTED_WITH_MERCHANT,
        r"wrong (amount|price)|overcharg|should (have been|be) (only )?\d"
        r"|price (went up|increased) without",
    ),
]


def classify_complaint(text: str) -> tuple[ReasonCode, float]:
    """Map free-text complaint to a reason code, with a rough confidence.

    A keyword pass, deliberately. The LLM agent (see agent.py) can propose a
    code too, but classification is only ever a *routing* decision here — it
    picks which verification to run. It never decides that money moves. That
    is why a crude classifier is safe: if it routes wrongly, verification
    fails against the ledger and the case escalates to a human. The failure
    mode of a mis-read complaint is a slower answer, never a wrong payout.
    """
    lowered = text.lower()
    for code, pattern in _PATTERNS:
        if re.search(pattern, lowered):
            return code, 0.8
    return ReasonCode.UNKNOWN, 0.2


# --------------------------------------------------------------------------
# Step 2 — verify the claimed breach against the ledger
# --------------------------------------------------------------------------


def _verify(
    code: ReasonCode, ctx: ComplaintContext, limits: Limits
) -> tuple[bool, list[Evidence], int]:
    """Return (breach_confirmed, evidence, reversal_amount_paise)."""

    txn = ctx.transaction
    mandate = ctx.mandate
    ev: list[Evidence] = []

    if code is ReasonCode.MANDATE_REVOKED_BEFORE_DEBIT:
        revoked_at = mandate.revoked_at
        if revoked_at is None or mandate.status is not MandateStatus.REVOKED:
            ev.append(
                Evidence(
                    "mandate_revocation",
                    "FAIL",
                    f"mandate {mandate.mandate_id} shows status {mandate.status.value}; "
                    "no revocation on record",
                )
            )
            return False, ev, 0
        if revoked_at <= txn.debited_at:
            gap = txn.debited_at - revoked_at
            ev.append(
                Evidence(
                    "mandate_revocation",
                    "PASS",
                    f"revoked {revoked_at:%d %b %Y %H:%M}, debited "
                    f"{txn.debited_at:%d %b %Y %H:%M} — "
                    f"{gap.days}d {gap.seconds // 3600}h after revocation",
                )
            )
            return True, ev, txn.amount_paise
        ev.append(
            Evidence(
                "mandate_revocation",
                "FAIL",
                f"revocation recorded {revoked_at:%d %b %Y %H:%M}, which is AFTER "
                f"the debit at {txn.debited_at:%d %b %Y %H:%M}",
            )
        )
        return False, ev, 0

    if code is ReasonCode.DEBIT_AFTER_MANDATE_EXPIRY:
        if txn.debited_at > mandate.valid_until:
            ev.append(
                Evidence(
                    "mandate_validity",
                    "PASS",
                    f"mandate valid until {mandate.valid_until:%d %b %Y}, "
                    f"debited {txn.debited_at:%d %b %Y}",
                )
            )
            return True, ev, txn.amount_paise
        ev.append(
            Evidence(
                "mandate_validity",
                "FAIL",
                f"debit at {txn.debited_at:%d %b %Y} falls inside the mandate window "
                f"({mandate.valid_from:%d %b %Y} – {mandate.valid_until:%d %b %Y})",
            )
        )
        return False, ev, 0

    if code is ReasonCode.AMOUNT_EXCEEDS_MANDATE_CAP:
        if txn.amount_paise > mandate.max_amount_paise:
            excess = txn.amount_paise - mandate.max_amount_paise
            ev.append(
                Evidence(
                    "mandate_cap",
                    "PASS",
                    f"debited {rupees(txn.amount_paise)} against a cap of "
                    f"{rupees(mandate.max_amount_paise)} — {rupees(excess)} over",
                )
            )
            # The whole debit is unauthorised, not just the excess: the
            # mandate never permitted a debit of this size, so there is no
            # authorised portion to keep.
            return True, ev, txn.amount_paise
        ev.append(
            Evidence(
                "mandate_cap",
                "FAIL",
                f"debited {rupees(txn.amount_paise)}, within the "
                f"{rupees(mandate.max_amount_paise)} cap",
            )
        )
        return False, ev, 0

    if code is ReasonCode.DUPLICATE_DEBIT:
        window = timedelta(hours=limits.duplicate_window_hours)
        twins = [
            t
            for t in ctx.sibling_transactions
            if t.txn_id != txn.txn_id
            and t.mandate_id == txn.mandate_id
            and t.amount_paise == txn.amount_paise
            and t.status is TxnStatus.SUCCESS
            and abs(t.debited_at - txn.debited_at) <= window
        ]
        if twins:
            earliest = min(twins, key=lambda t: t.debited_at)
            # Only the later debit is the duplicate. If the complaint points
            # at the earlier one, reverse the later one instead.
            if txn.debited_at < earliest.debited_at:
                ev.append(
                    Evidence(
                        "duplicate_debit",
                        "INFO",
                        f"{txn.txn_id} is the ORIGINAL debit; {earliest.txn_id} is the "
                        "duplicate and is the one that should be reversed",
                    )
                )
                return False, ev, 0
            ev.append(
                Evidence(
                    "duplicate_debit",
                    "PASS",
                    f"{earliest.txn_id} debited {rupees(earliest.amount_paise)} at "
                    f"{earliest.debited_at:%d %b %H:%M}; {txn.txn_id} repeated the same "
                    f"amount at {txn.debited_at:%d %b %H:%M} on the same mandate",
                )
            )
            return True, ev, txn.amount_paise
        ev.append(
            Evidence(
                "duplicate_debit",
                "FAIL",
                f"no second debit of {rupees(txn.amount_paise)} on mandate "
                f"{mandate.mandate_id} within {limits.duplicate_window_hours}h",
            )
        )
        return False, ev, 0

    if code is ReasonCode.MISSING_PRE_DEBIT_NOTIFICATION:
        deadline = txn.debited_at - timedelta(hours=limits.pre_debit_notice_hours)
        served = [
            n
            for n in ctx.notifications
            if n.txn_id == txn.txn_id and n.sent_at <= txn.debited_at
        ]
        in_time = [n for n in served if n.sent_at <= deadline]
        if not served:
            ev.append(
                Evidence(
                    "pre_debit_notice",
                    "PASS",
                    f"no pre-debit notification of any kind on record for {txn.txn_id}",
                )
            )
            return True, ev, txn.amount_paise
        if not in_time:
            late = min(served, key=lambda n: n.sent_at)
            hours = (txn.debited_at - late.sent_at).total_seconds() / 3600
            ev.append(
                Evidence(
                    "pre_debit_notice",
                    "PASS",
                    f"notice sent via {late.channel} only {hours:.1f}h before the debit; "
                    f"{limits.pre_debit_notice_hours}h required",
                )
            )
            return True, ev, txn.amount_paise
        good = min(in_time, key=lambda n: n.sent_at)
        hours = (txn.debited_at - good.sent_at).total_seconds() / 3600
        ev.append(
            Evidence(
                "pre_debit_notice",
                "FAIL",
                f"notice sent via {good.channel} {hours:.1f}h before the debit "
                f"({good.sent_at:%d %b %H:%M}) — requirement met",
            )
        )
        return False, ev, 0

    # Contested and unknown codes are never verified here — by construction
    # the bank's own records cannot settle them.
    ev.append(
        Evidence(
            "verifiability",
            "INFO",
            f"{code.value} cannot be settled from bank records alone",
        )
    )
    return False, ev, 0


# --------------------------------------------------------------------------
# Step 3 — guardrails that can override a confirmed breach
# --------------------------------------------------------------------------


def _guardrails(ctx: ComplaintContext, limits: Limits) -> list[Evidence]:
    """Return evidence for every guardrail that TRIPPED (empty = all clear)."""
    tripped: list[Evidence] = []
    txn = ctx.transaction
    cust = ctx.customer

    if txn.amount_paise > limits.auto_reverse_max_paise:
        tripped.append(
            Evidence(
                "value_limit",
                "FAIL",
                f"{rupees(txn.amount_paise)} exceeds the "
                f"{rupees(limits.auto_reverse_max_paise)} auto-reversal ceiling",
            )
        )

    if cust.auto_reversals_90d >= limits.max_auto_reversals_90d:
        tripped.append(
            Evidence(
                "claim_frequency",
                "FAIL",
                f"{cust.auto_reversals_90d} auto-reversals already granted in 90 days "
                f"(limit {limits.max_auto_reversals_90d})",
            )
        )

    if cust.account_frozen:
        tripped.append(
            Evidence("account_status", "FAIL", "account is frozen — no automated credits")
        )

    if not cust.kyc_verified:
        tripped.append(
            Evidence("kyc_status", "FAIL", "KYC not verified — no automated credits")
        )

    age_days = (ctx.now - txn.debited_at).days
    if age_days > limits.dispute_window_days:
        tripped.append(
            Evidence(
                "dispute_window",
                "FAIL",
                f"debit is {age_days} days old; window is {limits.dispute_window_days} days",
            )
        )

    if txn.status is TxnStatus.REVERSED:
        tripped.append(
            Evidence(
                "already_reversed",
                "FAIL",
                f"{txn.txn_id} was already reversed as {txn.reversal_txn_id}",
            )
        )

    return tripped


# --------------------------------------------------------------------------
# Step 4 — put it together
# --------------------------------------------------------------------------

_QUEUE = {
    ReasonCode.UNAUTHORIZED_MANDATE: "fraud-ops",
    ReasonCode.SERVICE_NOT_RENDERED: "merchant-disputes",
    ReasonCode.SUBSCRIPTION_CANCELLED_WITH_MERCHANT: "merchant-disputes",
    ReasonCode.AMOUNT_DISPUTED_WITH_MERCHANT: "merchant-disputes",
}


def decide(
    ctx: ComplaintContext,
    limits: Limits | None = None,
    reason_code: ReasonCode | None = None,
) -> Decision:
    """Decide what to do about one autopay complaint.

    Pass `reason_code` to skip the keyword classifier — the LLM agent does
    this when it has read the complaint itself. Either way the code only
    selects which ledger check runs; it never substitutes for the check.
    """
    limits = limits or Limits()
    code, confidence = (
        (reason_code, 0.9) if reason_code else classify_complaint(ctx.complaint_text)
    )

    evidence: list[Evidence] = [
        Evidence(
            "classification",
            "INFO",
            f"complaint read as {code.value} (confidence {confidence:.0%})",
        )
    ]

    # --- already reversed: idempotency beats everything else -------------
    if ctx.transaction.status is TxnStatus.REVERSED:
        evidence.append(
            Evidence(
                "already_reversed",
                "INFO",
                f"{ctx.transaction.txn_id} already reversed as "
                f"{ctx.transaction.reversal_txn_id}; nothing further to do",
            )
        )
        return Decision(
            outcome=Outcome.ESCALATE,
            reason_code=code,
            rationale=(
                f"This debit was already reversed ({ctx.transaction.reversal_txn_id}). "
                "Routing to an advisor to confirm the credit reached the customer "
                "rather than reversing a second time."
            ),
            evidence=evidence,
            queue="general-disputes",
            priority="LOW",
            recommendation=Recommendation.LIKELY_INVALID,
        )

    # --- contested classes never auto-reverse ----------------------------
    if code in CONTESTED_CODES:
        fraud = code is ReasonCode.UNAUTHORIZED_MANDATE
        evidence.append(
            Evidence(
                "verifiability",
                "INFO",
                "resolving this needs the merchant's or a fraud analyst's input; "
                "bank records cannot settle it",
            )
        )
        evidence.extend(_context_notes(ctx))
        return Decision(
            outcome=Outcome.ESCALATE,
            reason_code=code,
            rationale=(
                "The customer disputes a mandate they say they never authorised. "
                "Mandate paused immediately as a containment step; the reversal "
                "decision itself needs a fraud analyst."
                if fraud
                else "This turns on what the merchant did, not on what the bank's "
                "records show, so it cannot be settled automatically. Sent to the "
                "merchant-disputes queue with the debit history attached."
            ),
            evidence=evidence,
            queue=_QUEUE.get(code, "general-disputes"),
            priority="HIGH" if fraud else "NORMAL",
            recommendation=(
                Recommendation.FRAUD_REVIEW if fraud else Recommendation.NEEDS_MERCHANT_INPUT
            ),
            pause_mandate=fraud,
        )

    # --- unknown: we could not even tell what is being claimed -----------
    if code is ReasonCode.UNKNOWN:
        evidence.extend(_context_notes(ctx))
        return Decision(
            outcome=Outcome.ESCALATE,
            reason_code=code,
            rationale=(
                "The complaint does not map to a reason code the automated checks "
                "cover. An advisor should read it and classify it by hand."
            ),
            evidence=evidence,
            queue="general-disputes",
            priority="NORMAL",
            recommendation=Recommendation.LIKELY_VALID,
        )

    # --- verifiable class: check it against the ledger -------------------
    confirmed, check_evidence, amount = _verify(code, ctx, limits)
    evidence.extend(check_evidence)

    if not confirmed:
        evidence.extend(_context_notes(ctx))
        return Decision(
            outcome=Outcome.ESCALATE,
            reason_code=code,
            rationale=(
                f"The customer's account of events ({code.value}) is not borne out by "
                "the ledger. That is not proof they are wrong — records can be "
                "incomplete and people describe things loosely — so this goes to an "
                "advisor with the contradiction spelled out rather than being refused."
            ),
            evidence=evidence,
            queue="general-disputes",
            priority="NORMAL",
            recommendation=Recommendation.LIKELY_INVALID,
        )

    tripped = _guardrails(ctx, limits)
    if tripped:
        evidence.extend(tripped)
        abuse = any(e.check == "claim_frequency" for e in tripped)
        return Decision(
            outcome=Outcome.ESCALATE,
            reason_code=code,
            rationale=(
                "The breach is confirmed against the ledger and the customer is very "
                "likely owed this money — but "
                + "; ".join(e.detail for e in tripped)
                + ". A human authorises the credit."
            ),
            evidence=evidence,
            reversal_amount_paise=amount,
            queue="risk-review" if abuse else "high-value-reversals",
            priority="HIGH",
            recommendation=(
                Recommendation.ABUSE_REVIEW if abuse else Recommendation.LIKELY_VALID
            ),
            pause_mandate=code is ReasonCode.MANDATE_REVOKED_BEFORE_DEBIT,
        )

    evidence.append(
        Evidence("guardrails", "PASS", "value, frequency, account and window checks all clear")
    )
    return Decision(
        outcome=Outcome.AUTO_REVERSE,
        reason_code=code,
        rationale=(
            f"{code.value} is confirmed by the bank's own records, and every guardrail "
            f"is clear. Reversing {rupees(amount)} to the customer's account now."
        ),
        evidence=evidence,
        reversal_amount_paise=amount,
        # A debit that ran against a revoked mandate means the revocation did
        # not take at the merchant end. Stop it before it happens again.
        pause_mandate=code is ReasonCode.MANDATE_REVOKED_BEFORE_DEBIT,
    )


def _context_notes(ctx: ComplaintContext) -> list[Evidence]:
    """Facts worth putting in front of a human reviewer on any escalation."""
    m, t = ctx.mandate, ctx.transaction
    return [
        Evidence(
            "mandate",
            "INFO",
            f"{m.mandate_id} ({m.merchant_name}), {m.frequency.value.lower()}, cap "
            f"{rupees(m.max_amount_paise)}, status {m.status.value}",
        ),
        Evidence(
            "debit",
            "INFO",
            f"{t.txn_id} — {rupees(t.amount_paise)} on {t.debited_at:%d %b %Y %H:%M}",
        ),
        Evidence(
            "history",
            "INFO",
            f"{len(ctx.sibling_transactions)} other debit"
            f"{'' if len(ctx.sibling_transactions) == 1 else 's'} on this mandate; "
            f"{ctx.customer.auto_reversals_90d} auto-reversal"
            f"{'' if ctx.customer.auto_reversals_90d == 1 else 's'} granted to this "
            "customer in 90 days",
        ),
    ]

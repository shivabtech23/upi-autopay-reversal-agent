"""Tests for the decision policy.

The policy engine is a pure function, so these need no database and no
network. That is the point of the split: the part that decides whether
money moves is exhaustively testable in milliseconds.

    python -m pytest tests/ -q
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from reversal_models import (  # noqa: E402
    ComplaintContext, Customer, Frequency, Mandate, MandateStatus,
    PreDebitNotification, Transaction, TxnStatus, to_paise,
)
from reversal_policy import (  # noqa: E402
    Limits, Outcome, ReasonCode, Recommendation, classify_complaint, decide,
)

NOW = datetime(2026, 6, 1, 12, 0, 0)


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


def make_ctx(
    *,
    complaint: str = "",
    reversals_90d: int = 0,
    frozen: bool = False,
    kyc: bool = True,
    mandate_status: MandateStatus = MandateStatus.ACTIVE,
    cap_rupees: int = 1000,
    revoked_days_ago: int | None = None,
    valid_until_days: int = 300,
    amount_rupees: int = 500,
    debited_days_ago: int = 3,
    txn_status: TxnStatus = TxnStatus.SUCCESS,
    siblings: list[Transaction] | None = None,
    notices: list[PreDebitNotification] | None = None,
) -> ComplaintContext:
    debited_at = NOW - timedelta(days=debited_days_ago)
    mandate = Mandate(
        mandate_id="MND1", umn="UMN1", customer_id="C1", merchant_name="Acme",
        max_amount_paise=to_paise(cap_rupees), frequency=Frequency.MONTHLY,
        status=mandate_status, created_at=NOW - timedelta(days=365),
        valid_from=NOW - timedelta(days=365),
        valid_until=NOW + timedelta(days=valid_until_days),
        revoked_at=None if revoked_days_ago is None else NOW - timedelta(days=revoked_days_ago),
    )
    txn = Transaction(
        txn_id="T1", mandate_id="MND1", customer_id="C1", merchant_name="Acme",
        amount_paise=to_paise(amount_rupees), debited_at=debited_at, status=txn_status,
        reversal_txn_id="REVOLD" if txn_status is TxnStatus.REVERSED else None,
    )
    return ComplaintContext(
        complaint_text=complaint,
        customer=Customer("C1", "Test User", "+91", kyc, frozen, reversals_90d),
        mandate=mandate, transaction=txn,
        sibling_transactions=siblings or [], notifications=notices or [],
        now=NOW,
    )


def notice(hours_before: int, txn_id: str = "T1") -> PreDebitNotification:
    return PreDebitNotification(
        notif_id="N", mandate_id="MND1", txn_id=txn_id,
        sent_at=NOW - timedelta(days=3) - timedelta(hours=hours_before), channel="SMS",
    )


# --------------------------------------------------------------------------
# Classification routes; it never decides
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("I cancelled the Netflix autopay mandate but they still charged me",
         ReasonCode.MANDATE_REVOKED_BEFORE_DEBIT),
        ("they deducted the same amount twice today", ReasonCode.DUPLICATE_DEBIT),
        ("charged 999 but my limit was 500, that exceeds it",
         ReasonCode.AMOUNT_EXCEEDS_MANDATE_CAP),
        ("no sms was sent before the debit", ReasonCode.MISSING_PRE_DEBIT_NOTIFICATION),
        ("the mandate had expired already", ReasonCode.DEBIT_AFTER_MANDATE_EXPIRY),
        ("I never set up this mandate, I don't recognise it",
         ReasonCode.UNAUTHORIZED_MANDATE),
        ("I cancelled my MealBox subscription with them last month",
         ReasonCode.SUBSCRIPTION_CANCELLED_WITH_MERCHANT),
        ("I never received the service I paid for", ReasonCode.SERVICE_NOT_RENDERED),
        ("what is going on with my account", ReasonCode.UNKNOWN),
    ],
)
def test_classifier_routes_complaints(text, expected):
    code, _ = classify_complaint(text)
    assert code is expected


def test_fraud_wins_over_cancellation_language():
    """A complaint that mentions both must route to fraud, not to a refund path."""
    code, _ = classify_complaint(
        "I cancelled my autopay mandate and anyway I never authorised it in the first place"
    )
    assert code is ReasonCode.UNAUTHORIZED_MANDATE


def test_misclassification_cannot_cause_a_payout():
    """Route a complaint to the wrong check and it escalates — never pays out."""
    ctx = make_ctx(amount_rupees=500, cap_rupees=1000)  # no cap breach exists
    d = decide(ctx, reason_code=ReasonCode.AMOUNT_EXCEEDS_MANDATE_CAP)
    assert d.outcome is Outcome.ESCALATE


# --------------------------------------------------------------------------
# The five verifiable breaches auto-reverse
# --------------------------------------------------------------------------


def test_debit_after_revocation_auto_reverses():
    ctx = make_ctx(
        complaint="I cancelled the autopay mandate but was charged",
        mandate_status=MandateStatus.REVOKED, revoked_days_ago=10, debited_days_ago=3,
        notices=[notice(48)],
    )
    d = decide(ctx)
    assert d.outcome is Outcome.AUTO_REVERSE
    assert d.reason_code is ReasonCode.MANDATE_REVOKED_BEFORE_DEBIT
    assert d.reversal_amount_paise == to_paise(500)
    assert d.pause_mandate is True


def test_revocation_after_the_debit_does_not_auto_reverse():
    """Cancelling today does not make last week's debit unauthorised."""
    ctx = make_ctx(
        complaint="I cancelled the autopay mandate but was charged",
        mandate_status=MandateStatus.REVOKED, revoked_days_ago=1, debited_days_ago=5,
    )
    d = decide(ctx)
    assert d.outcome is Outcome.ESCALATE
    assert d.recommendation is Recommendation.LIKELY_INVALID


def test_debit_above_cap_reverses_the_whole_debit_not_the_excess():
    ctx = make_ctx(
        complaint="charged more than my limit, this exceeds what I approved",
        cap_rupees=500, amount_rupees=999,
    )
    d = decide(ctx)
    assert d.outcome is Outcome.AUTO_REVERSE
    assert d.reversal_amount_paise == to_paise(999)


def test_debit_within_cap_escalates():
    ctx = make_ctx(complaint="they charged more than the limit", cap_rupees=1000,
                   amount_rupees=500)
    assert decide(ctx).outcome is Outcome.ESCALATE


def test_duplicate_debit_auto_reverses_the_later_one():
    twin = Transaction(
        txn_id="T0", mandate_id="MND1", customer_id="C1", merchant_name="Acme",
        amount_paise=to_paise(500), debited_at=NOW - timedelta(days=3, hours=4),
    )
    ctx = make_ctx(complaint="charged twice for the same thing", siblings=[twin],
                   notices=[notice(48)])
    d = decide(ctx)
    assert d.outcome is Outcome.AUTO_REVERSE
    assert d.reason_code is ReasonCode.DUPLICATE_DEBIT


def test_original_debit_is_not_the_one_reversed():
    """If the complaint names the FIRST debit, don't reverse it — the second is the dup."""
    later = Transaction(
        txn_id="T2", mandate_id="MND1", customer_id="C1", merchant_name="Acme",
        amount_paise=to_paise(500), debited_at=NOW - timedelta(days=2, hours=20),
    )
    ctx = make_ctx(complaint="charged twice", siblings=[later], notices=[notice(48)])
    d = decide(ctx)
    assert d.outcome is Outcome.ESCALATE
    assert any("ORIGINAL" in e.detail for e in d.evidence)


def test_debits_far_apart_are_not_duplicates():
    old = Transaction(
        txn_id="T0", mandate_id="MND1", customer_id="C1", merchant_name="Acme",
        amount_paise=to_paise(500), debited_at=NOW - timedelta(days=33),
    )
    ctx = make_ctx(complaint="charged twice", siblings=[old])
    assert decide(ctx).outcome is Outcome.ESCALATE


def test_missing_notice_auto_reverses():
    ctx = make_ctx(complaint="no sms or notification was sent to me", notices=[])
    d = decide(ctx)
    assert d.outcome is Outcome.AUTO_REVERSE
    assert d.reason_code is ReasonCode.MISSING_PRE_DEBIT_NOTIFICATION


def test_late_notice_counts_as_missing():
    ctx = make_ctx(complaint="no notification was sent", notices=[notice(2)])
    assert decide(ctx).outcome is Outcome.AUTO_REVERSE


def test_notice_exactly_at_the_boundary_is_compliant():
    ctx = make_ctx(complaint="no notification was sent", notices=[notice(24)])
    d = decide(ctx)
    assert d.outcome is Outcome.ESCALATE
    assert any("requirement met" in e.detail for e in d.evidence)


def test_debit_after_expiry_auto_reverses():
    ctx = make_ctx(complaint="the mandate had expired", valid_until_days=-10,
                   debited_days_ago=3, notices=[notice(48)])
    d = decide(ctx)
    assert d.outcome is Outcome.AUTO_REVERSE
    assert d.reason_code is ReasonCode.DEBIT_AFTER_MANDATE_EXPIRY


# --------------------------------------------------------------------------
# Contested classes never auto-reverse
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "complaint,queue",
    [
        ("I never set up this mandate, someone else did", "fraud-ops"),
        ("I cancelled my subscription with them last month", "merchant-disputes"),
        ("I never received the service", "merchant-disputes"),
    ],
)
def test_contested_complaints_always_escalate(complaint, queue):
    d = decide(make_ctx(complaint=complaint))
    assert d.outcome is Outcome.ESCALATE
    assert d.queue == queue


def test_fraud_pauses_the_mandate_immediately():
    d = decide(make_ctx(complaint="I never authorised this mandate"))
    assert d.pause_mandate is True
    assert d.priority == "HIGH"
    assert d.recommendation is Recommendation.FRAUD_REVIEW


def test_a_tiny_fraud_claim_still_escalates():
    """Amount is irrelevant to the fraud path — small amounts are how it starts."""
    d = decide(make_ctx(complaint="I never set up this mandate", amount_rupees=1))
    assert d.outcome is Outcome.ESCALATE


def test_unknown_complaints_escalate():
    d = decide(make_ctx(complaint="please help me with my account thing"))
    assert d.outcome is Outcome.ESCALATE
    assert d.reason_code is ReasonCode.UNKNOWN


# --------------------------------------------------------------------------
# Guardrails override a confirmed breach
# --------------------------------------------------------------------------


def test_high_value_breach_escalates_with_amount_preserved():
    ctx = make_ctx(complaint="no notification was sent", amount_rupees=48000,
                   cap_rupees=60000, notices=[])
    d = decide(ctx)
    assert d.outcome is Outcome.ESCALATE
    assert d.queue == "high-value-reversals"
    assert d.recommendation is Recommendation.LIKELY_VALID
    # The advisor still gets told what to pay if they approve.
    assert d.reversal_amount_paise == to_paise(48000)


def test_repeat_claimant_goes_to_risk_review():
    ctx = make_ctx(complaint="no notification was sent", reversals_90d=3, notices=[])
    d = decide(ctx)
    assert d.outcome is Outcome.ESCALATE
    assert d.queue == "risk-review"
    assert d.recommendation is Recommendation.ABUSE_REVIEW


def test_frozen_account_blocks_automatic_credit():
    ctx = make_ctx(complaint="no notification was sent", frozen=True, notices=[])
    assert decide(ctx).outcome is Outcome.ESCALATE


def test_unverified_kyc_blocks_automatic_credit():
    ctx = make_ctx(complaint="no notification was sent", kyc=False, notices=[])
    assert decide(ctx).outcome is Outcome.ESCALATE


def test_stale_debit_falls_outside_the_dispute_window():
    ctx = make_ctx(complaint="no notification was sent", debited_days_ago=200, notices=[])
    assert decide(ctx).outcome is Outcome.ESCALATE


def test_already_reversed_debit_never_pays_twice():
    ctx = make_ctx(complaint="no notification was sent", txn_status=TxnStatus.REVERSED,
                   notices=[])
    d = decide(ctx)
    assert d.outcome is Outcome.ESCALATE
    assert d.reversal_amount_paise == 0


def test_limits_are_tunable():
    ctx = make_ctx(complaint="no notification was sent", amount_rupees=48000, notices=[])
    assert decide(ctx).outcome is Outcome.ESCALATE
    generous = Limits(auto_reverse_max_paise=to_paise(100000))
    assert decide(ctx, limits=generous).outcome is Outcome.AUTO_REVERSE


# --------------------------------------------------------------------------
# Invariants that must hold across every path
# --------------------------------------------------------------------------


ALL_COMPLAINTS = [
    "I cancelled the autopay mandate but was charged",
    "charged twice today",
    "this exceeds my limit",
    "no sms was sent",
    "the mandate had expired",
    "I never set up this mandate",
    "I cancelled my subscription with them",
    "I never received the service",
    "something is wrong",
    "",
]


@pytest.mark.parametrize("complaint", ALL_COMPLAINTS)
def test_every_decision_carries_evidence_and_a_rationale(complaint):
    d = decide(make_ctx(complaint=complaint))
    assert d.evidence, "a decision with no evidence cannot be audited"
    assert len(d.rationale) > 40


@pytest.mark.parametrize("complaint", ALL_COMPLAINTS)
def test_auto_reversal_always_has_a_positive_amount(complaint):
    d = decide(make_ctx(complaint=complaint, notices=[]))
    if d.outcome is Outcome.AUTO_REVERSE:
        assert d.reversal_amount_paise > 0
        assert d.reason_code in {
            ReasonCode.MANDATE_REVOKED_BEFORE_DEBIT,
            ReasonCode.DEBIT_AFTER_MANDATE_EXPIRY,
            ReasonCode.AMOUNT_EXCEEDS_MANDATE_CAP,
            ReasonCode.DUPLICATE_DEBIT,
            ReasonCode.MISSING_PRE_DEBIT_NOTIFICATION,
        }


@pytest.mark.parametrize("complaint", ALL_COMPLAINTS)
def test_never_reverse_more_than_was_debited(complaint):
    ctx = make_ctx(complaint=complaint, notices=[])
    d = decide(ctx)
    assert d.reversal_amount_paise <= ctx.transaction.amount_paise


def test_decisions_are_deterministic():
    ctx = make_ctx(complaint="no sms was sent", notices=[])
    first, second = decide(ctx), decide(ctx)
    assert first.outcome is second.outcome
    assert [str(e) for e in first.evidence] == [str(e) for e in second.evidence]

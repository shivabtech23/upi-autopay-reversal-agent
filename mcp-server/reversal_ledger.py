"""Account-side records for the banking domain, and the writes that touch them.

Why this file exists
--------------------
`core.py` knows about intents, tickets and notifications. What it does not
have is any record of the customer's actual *account*: which mandates they
hold, what was debited, whether a pre-debit notice went out. Without that,
"is this complaint genuine?" can only be answered from the customer's own
words — which is why the current `initiate_refund_or_reversal` can open a
ticket but can never reverse anything.

This module adds those records, in the same JSON-file style `core.py`
already uses, plus the two writes that a reversal actually needs: crediting
the money back (idempotently) and stopping a mandate. Every write appends
to an audit log.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from reversal_models import (
    ComplaintContext,
    Customer,
    Frequency,
    Mandate,
    MandateStatus,
    PreDebitNotification,
    Transaction,
    TxnStatus,
    rupees,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "..", "data")

ACCOUNTS_FILE      = os.path.join(DATA_DIR, "accounts.json")
MANDATES_FILE      = os.path.join(DATA_DIR, "mandates.json")
TRANSACTIONS_FILE  = os.path.join(DATA_DIR, "transactions.json")
NOTICES_FILE       = os.path.join(DATA_DIR, "predebit_notices.json")
AUDIT_FILE         = os.path.join(DATA_DIR, "reversal_audit.json")

ISO = "%Y-%m-%dT%H:%M:%S"


# ---------------------------------------------------------------------------
# File I/O — same read_json/write_json contract as core.py
# ---------------------------------------------------------------------------


def read_json(filepath: str) -> list:
    try:
        with open(filepath) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def write_json(filepath: str, data: list) -> None:
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2)


def _dt(value: str | None) -> Optional[datetime]:
    return datetime.strptime(value, ISO) if value else None


def _s(value: datetime | None) -> Optional[str]:
    return value.strftime(ISO) if value else None


def audit(actor: str, action: str, entity: str, payload: dict) -> None:
    """Append-only. Nothing in this module ever rewrites an existing entry."""
    log = read_json(AUDIT_FILE)
    log.append({
        "seq": len(log) + 1,
        "at": datetime.now(timezone.utc).strftime(ISO),
        "actor": actor,
        "action": action,
        "entity": entity,
        "payload": payload,
    })
    write_json(AUDIT_FILE, log)


# ---------------------------------------------------------------------------
# Row -> dataclass
# ---------------------------------------------------------------------------


def _customer(row: dict) -> Customer:
    return Customer(
        customer_id=row["customer_id"], name=row["name"], phone=row["phone"],
        kyc_verified=row.get("kyc_verified", True),
        account_frozen=row.get("account_frozen", False),
        auto_reversals_90d=row.get("auto_reversals_90d", 0),
    )


def _mandate(row: dict) -> Mandate:
    return Mandate(
        mandate_id=row["mandate_id"], umn=row["umn"], customer_id=row["customer_id"],
        merchant_name=row["merchant_name"], max_amount_paise=row["max_amount_paise"],
        frequency=Frequency(row["frequency"]), status=MandateStatus(row["status"]),
        created_at=_dt(row["created_at"]), valid_from=_dt(row["valid_from"]),
        valid_until=_dt(row["valid_until"]), revoked_at=_dt(row.get("revoked_at")),
    )


def _txn(row: dict) -> Transaction:
    return Transaction(
        txn_id=row["txn_id"], mandate_id=row["mandate_id"], customer_id=row["customer_id"],
        merchant_name=row["merchant_name"], amount_paise=row["amount_paise"],
        debited_at=_dt(row["debited_at"]), status=TxnStatus(row.get("status", "SUCCESS")),
        rrn=row.get("rrn", ""), reversal_txn_id=row.get("reversal_txn_id"),
    )


def _notice(row: dict) -> PreDebitNotification:
    return PreDebitNotification(
        notif_id=row["notif_id"], mandate_id=row["mandate_id"], txn_id=row["txn_id"],
        sent_at=_dt(row["sent_at"]), channel=row["channel"],
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def get_transaction(txn_id: str) -> Optional[Transaction]:
    for row in read_json(TRANSACTIONS_FILE):
        if row["txn_id"] == txn_id:
            return _txn(row)
    return None


def get_mandate(mandate_id: str) -> Optional[Mandate]:
    for row in read_json(MANDATES_FILE):
        if row["mandate_id"] == mandate_id:
            return _mandate(row)
    return None


def get_customer(customer_id: str) -> Optional[Customer]:
    for row in read_json(ACCOUNTS_FILE):
        if row["customer_id"] == customer_id:
            return _customer(row)
    return None


def list_transactions_for_customer(customer_id: str) -> list[Transaction]:
    rows = [r for r in read_json(TRANSACTIONS_FILE) if r["customer_id"] == customer_id]
    return sorted((_txn(r) for r in rows), key=lambda t: t.debited_at, reverse=True)


def build_context(txn_id: str, complaint_text: str) -> ComplaintContext:
    """Assemble everything the policy engine is allowed to look at.

    Raises LookupError if the transaction, its mandate or its customer is
    missing — a reversal decision on partial records is worse than none.
    """
    txn = get_transaction(txn_id)
    if txn is None:
        raise LookupError(f"no such transaction: {txn_id}")

    mandate = get_mandate(txn.mandate_id)
    if mandate is None:
        raise LookupError(f"transaction {txn_id} references unknown mandate {txn.mandate_id}")

    customer = get_customer(txn.customer_id)
    if customer is None:
        raise LookupError(f"transaction {txn_id} references unknown customer {txn.customer_id}")

    siblings = [
        _txn(r) for r in read_json(TRANSACTIONS_FILE)
        if r["mandate_id"] == txn.mandate_id and r["txn_id"] != txn_id
    ]
    notices = [
        _notice(r) for r in read_json(NOTICES_FILE) if r["mandate_id"] == txn.mandate_id
    ]

    return ComplaintContext(
        complaint_text=complaint_text, customer=customer, mandate=mandate,
        transaction=txn, sibling_transactions=siblings, notifications=notices,
        now=datetime.utcnow(),
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def execute_reversal(
    txn_id: str, amount_paise: int, reason_code: str, decided_by: str = "agent"
) -> dict:
    """Credit the customer back. Idempotent — a debit reverses at most once.

    Idempotency is not a nicety here. An MCP host can retry a tool call on a
    timeout, and a retry that pays out twice is a real loss.
    """
    txns = read_json(TRANSACTIONS_FILE)
    row = next((r for r in txns if r["txn_id"] == txn_id), None)
    if row is None:
        raise LookupError(f"no such transaction: {txn_id}")

    if row.get("status") == TxnStatus.REVERSED.value:
        return {
            "status": "ALREADY_REVERSED",
            "txn_id": txn_id,
            "reversal_txn_id": row.get("reversal_txn_id"),
            "message": f"{txn_id} was already reversed as {row.get('reversal_txn_id')}.",
        }

    if amount_paise <= 0 or amount_paise > row["amount_paise"]:
        raise ValueError(
            f"reversal of {rupees(amount_paise)} is not valid against a debit of "
            f"{rupees(row['amount_paise'])}"
        )

    reversal_id = f"REV{uuid.uuid4().hex[:10].upper()}"
    row["status"] = TxnStatus.REVERSED.value
    row["reversal_txn_id"] = reversal_id
    write_json(TRANSACTIONS_FILE, txns)

    accounts = read_json(ACCOUNTS_FILE)
    for acct in accounts:
        if acct["customer_id"] == row["customer_id"]:
            acct["auto_reversals_90d"] = acct.get("auto_reversals_90d", 0) + 1
    write_json(ACCOUNTS_FILE, accounts)

    audit(decided_by, "REVERSAL", txn_id, {
        "reversal_txn_id": reversal_id,
        "amount_paise": amount_paise,
        "reason_code": reason_code,
    })

    return {
        "status": "REVERSED",
        "txn_id": txn_id,
        "reversal_txn_id": reversal_id,
        "amount_paise": amount_paise,
        "amount": rupees(amount_paise),
        "credited_to": row["customer_id"],
        "message": (
            f"{rupees(amount_paise)} credited back to {row['customer_id']}; "
            f"reference {reversal_id}. Expect it within 1 working day."
        ),
    }


def set_mandate_status(mandate_id: str, action: str, actor: str = "agent") -> dict:
    """action: PAUSE | CANCEL | RESUME."""
    action = action.upper()
    target = {
        "PAUSE": MandateStatus.PAUSED,
        "CANCEL": MandateStatus.REVOKED,
        "RESUME": MandateStatus.ACTIVE,
    }.get(action)
    if target is None:
        raise ValueError(f"action must be PAUSE, CANCEL or RESUME (got {action!r})")

    mandates = read_json(MANDATES_FILE)
    row = next((m for m in mandates if m["mandate_id"] == mandate_id), None)
    if row is None:
        raise LookupError(f"no such mandate: {mandate_id}")

    before = row["status"]
    row["status"] = target.value
    if target is MandateStatus.REVOKED and not row.get("revoked_at"):
        row["revoked_at"] = datetime.utcnow().strftime(ISO)
    write_json(MANDATES_FILE, mandates)

    audit(actor, f"MANDATE_{action}", mandate_id, {"from": before, "to": target.value})
    return {
        "status": "OK", "mandate_id": mandate_id,
        "previous_status": before, "new_status": target.value,
        "message": f"Mandate {mandate_id} moved from {before} to {target.value}.",
    }


def audit_trail(entity: str | None = None, limit: int = 50) -> list[dict]:
    log = read_json(AUDIT_FILE)
    if entity:
        log = [r for r in log if r["entity"] == entity]
    return list(reversed(log))[:limit]

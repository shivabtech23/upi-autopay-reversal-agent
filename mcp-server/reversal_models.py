"""Domain records shared by the ledger, the policy engine and the MCP tools.

Money is always an integer number of *paise*. Never floats — a rupee value
that survives a round trip through float is not a value you want on a
reversal instruction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


def rupees(paise: int) -> str:
    """Format paise for humans: 49900 -> '₹499.00'."""
    sign = "-" if paise < 0 else ""
    paise = abs(paise)
    return f"{sign}₹{paise // 100:,}.{paise % 100:02d}"


def to_paise(rupee_value: float | int | str) -> int:
    """Convert a rupee amount to paise without float drift."""
    from decimal import Decimal

    return int((Decimal(str(rupee_value)) * 100).to_integral_value())


class MandateStatus(str, Enum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"


class Frequency(str, Enum):
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"
    QUARTERLY = "QUARTERLY"
    YEARLY = "YEARLY"
    AS_PRESENTED = "AS_PRESENTED"


class TxnStatus(str, Enum):
    SUCCESS = "SUCCESS"
    REVERSED = "REVERSED"
    FAILED = "FAILED"


@dataclass
class Customer:
    customer_id: str
    name: str
    phone: str
    kyc_verified: bool = True
    account_frozen: bool = False
    # How many auto-reversals this customer has already been granted in the
    # trailing 90 days. The abuse guardrail reads this.
    auto_reversals_90d: int = 0


@dataclass
class Mandate:
    """A UPI Autopay mandate (recurring e-mandate) as the bank holds it."""

    mandate_id: str
    umn: str  # Unique Mandate Number
    customer_id: str
    merchant_name: str
    max_amount_paise: int
    frequency: Frequency
    status: MandateStatus
    created_at: datetime
    valid_from: datetime
    valid_until: datetime
    revoked_at: Optional[datetime] = None


@dataclass
class Transaction:
    """One autopay debit executed against a mandate."""

    txn_id: str
    mandate_id: str
    customer_id: str
    merchant_name: str
    amount_paise: int
    debited_at: datetime
    status: TxnStatus = TxnStatus.SUCCESS
    rrn: str = ""
    reversal_txn_id: Optional[str] = None


@dataclass
class PreDebitNotification:
    """NPCI requires the customer be notified before an autopay debit.

    Absence of this record is, on its own, sufficient grounds to reverse.
    """

    notif_id: str
    mandate_id: str
    txn_id: str
    sent_at: datetime
    channel: str  # SMS / PUSH / EMAIL


@dataclass
class Case:
    case_id: str
    customer_id: str
    txn_id: str
    queue: str
    priority: str
    summary: str
    recommendation: str
    status: str
    created_at: datetime


@dataclass
class ComplaintContext:
    """Everything the policy engine is allowed to look at.

    Assembled by the ledger, passed to a pure function. The engine never
    touches the database itself — that is what makes it testable and what
    makes every decision reproducible from its inputs alone.
    """

    complaint_text: str
    customer: Customer
    mandate: Mandate
    transaction: Transaction
    sibling_transactions: list[Transaction] = field(default_factory=list)
    notifications: list[PreDebitNotification] = field(default_factory=list)
    now: datetime = field(default_factory=datetime.utcnow)

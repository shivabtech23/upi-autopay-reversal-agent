"""Seed the banking-domain account records.

    python seed_reversal_ledger.py

Writes data/accounts.json, data/mandates.json, data/transactions.json and
data/predebit_notices.json. Dates are generated relative to today, so the
demo tells the same story whenever you run it.

There is one scenario per branch the policy engine can take — five that
auto-reverse, four that must not. If you change the engine, run
demo_reversal.py and check all nine still land where they should.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import reversal_ledger as L  # noqa: E402
from reversal_models import to_paise  # noqa: E402

NOW = datetime.utcnow()


def ago(days: float = 0, hours: float = 0) -> str:
    return (NOW - timedelta(days=days, hours=hours)).strftime(L.ISO)


def ahead(days: float) -> str:
    return (NOW + timedelta(days=days)).strftime(L.ISO)


ACCOUNTS = [
    {"customer_id": "CUST001", "name": "Ananya Iyer", "phone": "+9198XXXXX210",
     "kyc_verified": True, "account_frozen": False, "auto_reversals_90d": 0},
    {"customer_id": "CUST002", "name": "Rohan Mehta", "phone": "+9197XXXXX884",
     "kyc_verified": True, "account_frozen": False, "auto_reversals_90d": 1},
    # Three reversals already granted — trips the abuse guardrail.
    {"customer_id": "CUST003", "name": "Priya Nair", "phone": "+9199XXXXX037",
     "kyc_verified": True, "account_frozen": False, "auto_reversals_90d": 3},
    {"customer_id": "CUST004", "name": "Vikram Shah", "phone": "+9198XXXXX551",
     "kyc_verified": True, "account_frozen": False, "auto_reversals_90d": 0},
]

MANDATES = [
    # Revoked five days ago — yet a debit landed two days ago.
    {"mandate_id": "MND001", "umn": "UMN0001", "customer_id": "CUST001",
     "merchant_name": "Netflix India", "max_amount_paise": to_paise(649),
     "frequency": "MONTHLY", "status": "REVOKED", "created_at": ago(400),
     "valid_from": ago(400), "valid_until": ahead(300), "revoked_at": ago(5)},
    # Cap ₹500; ₹999 was taken.
    {"mandate_id": "MND002", "umn": "UMN0002", "customer_id": "CUST002",
     "merchant_name": "FitPass Gym", "max_amount_paise": to_paise(500),
     "frequency": "MONTHLY", "status": "ACTIVE", "created_at": ago(200),
     "valid_from": ago(200), "valid_until": ahead(160), "revoked_at": None},
    # Same amount presented twice, three hours apart.
    {"mandate_id": "MND003", "umn": "UMN0003", "customer_id": "CUST002",
     "merchant_name": "CloudStore", "max_amount_paise": to_paise(1200),
     "frequency": "MONTHLY", "status": "ACTIVE", "created_at": ago(150),
     "valid_from": ago(150), "valid_until": ahead(200), "revoked_at": None},
    # A debit with no pre-debit notice on record at all.
    {"mandate_id": "MND004", "umn": "UMN0004", "customer_id": "CUST001",
     "merchant_name": "EduPrime", "max_amount_paise": to_paise(2000),
     "frequency": "MONTHLY", "status": "ACTIVE", "created_at": ago(90),
     "valid_from": ago(90), "valid_until": ahead(270), "revoked_at": None},
    # Expired twenty days ago; ₹48,000 was still collected.
    {"mandate_id": "MND005", "umn": "UMN0005", "customer_id": "CUST004",
     "merchant_name": "InsureCo", "max_amount_paise": to_paise(60000),
     "frequency": "YEARLY", "status": "EXPIRED", "created_at": ago(400),
     "valid_from": ago(400), "valid_until": ago(20), "revoked_at": None},
    # Notice sent, but only two hours ahead — and the customer claims often.
    {"mandate_id": "MND006", "umn": "UMN0006", "customer_id": "CUST003",
     "merchant_name": "ShopMax Plus", "max_amount_paise": to_paise(999),
     "frequency": "MONTHLY", "status": "ACTIVE", "created_at": ago(60),
     "valid_from": ago(60), "valid_until": ahead(300), "revoked_at": None},
    # The customer says they never authorised this one.
    {"mandate_id": "MND007", "umn": "UMN0007", "customer_id": "CUST004",
     "merchant_name": "StreamPlus", "max_amount_paise": to_paise(299),
     "frequency": "MONTHLY", "status": "ACTIVE", "created_at": ago(45),
     "valid_from": ago(45), "valid_until": ahead(320), "revoked_at": None},
    # Cancelled with the merchant, never with the bank.
    {"mandate_id": "MND008", "umn": "UMN0008", "customer_id": "CUST001",
     "merchant_name": "MealBox Daily", "max_amount_paise": to_paise(800),
     "frequency": "MONTHLY", "status": "ACTIVE", "created_at": ago(120),
     "valid_from": ago(120), "valid_until": ahead(240), "revoked_at": None},
]


def txn(txn_id, mandate_id, customer_id, merchant, rupees_amount, debited_at, rrn):
    return {
        "txn_id": txn_id, "mandate_id": mandate_id, "customer_id": customer_id,
        "merchant_name": merchant, "amount_paise": to_paise(rupees_amount),
        "debited_at": debited_at, "status": "SUCCESS", "rrn": rrn, "reversal_txn_id": None,
    }


TRANSACTIONS = [
    txn("TXN1001", "MND001", "CUST001", "Netflix India",   649, ago(2),          "RRN500001"),
    txn("TXN1002", "MND002", "CUST002", "FitPass Gym",     999, ago(3),          "RRN500002"),
    txn("TXN1003", "MND003", "CUST002", "CloudStore",     1199, ago(4),          "RRN500003"),
    txn("TXN1004", "MND003", "CUST002", "CloudStore",     1199, ago(4, -3),      "RRN500004"),
    txn("TXN1005", "MND004", "CUST001", "EduPrime",       1499, ago(6),          "RRN500005"),
    txn("TXN1006", "MND005", "CUST004", "InsureCo",      48000, ago(5),          "RRN500006"),
    txn("TXN1007", "MND006", "CUST003", "ShopMax Plus",    899, ago(3),          "RRN500007"),
    txn("TXN1008", "MND007", "CUST004", "StreamPlus",      299, ago(7),          "RRN500008"),
    txn("TXN1009", "MND008", "CUST001", "MealBox Daily",   799, ago(8),          "RRN500009"),
    txn("TXN1010", "MND004", "CUST001", "EduPrime",       1499, ago(35),         "RRN500010"),
]


def notice(notif_id, mandate_id, txn_id, sent_at, channel):
    return {"notif_id": notif_id, "mandate_id": mandate_id, "txn_id": txn_id,
            "sent_at": sent_at, "channel": channel}


NOTICES = [
    notice("NTF001", "MND001", "TXN1001", ago(3, 6),  "SMS"),
    notice("NTF002", "MND002", "TXN1002", ago(4, 12), "PUSH"),
    notice("NTF003", "MND003", "TXN1003", ago(5, 8),  "SMS"),
    notice("NTF004", "MND003", "TXN1004", ago(5, 8),  "SMS"),
    # TXN1005: deliberately absent — no notice was ever sent.
    notice("NTF006", "MND005", "TXN1006", ago(6, 10), "EMAIL"),
    # TXN1007: sent, but only two hours before the debit.
    notice("NTF007", "MND006", "TXN1007", ago(3, 2),  "SMS"),
    notice("NTF008", "MND007", "TXN1008", ago(8, 5),  "SMS"),
    notice("NTF009", "MND008", "TXN1009", ago(9, 4),  "PUSH"),
    notice("NTF010", "MND004", "TXN1010", ago(36, 6), "SMS"),
]


def main() -> None:
    L.write_json(L.ACCOUNTS_FILE, ACCOUNTS)
    L.write_json(L.MANDATES_FILE, MANDATES)
    L.write_json(L.TRANSACTIONS_FILE, TRANSACTIONS)
    L.write_json(L.NOTICES_FILE, NOTICES)
    L.write_json(L.AUDIT_FILE, [])
    L.audit("system", "SEED", "ledger", {
        "accounts": len(ACCOUNTS), "mandates": len(MANDATES),
        "transactions": len(TRANSACTIONS), "notices": len(NOTICES),
    })
    print(f"seeded {len(ACCOUNTS)} accounts, {len(MANDATES)} mandates, "
          f"{len(TRANSACTIONS)} transactions, {len(NOTICES)} notices "
          f"→ {os.path.normpath(L.DATA_DIR)}")


if __name__ == "__main__":
    main()

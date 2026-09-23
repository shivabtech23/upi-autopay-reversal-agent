"""End-to-end demo of the autopay reversal decision. No LLM, no network.

    python seed_reversal_ledger.py     # once, to build the account records
    python demo_reversal.py            # all nine complaints
    python demo_reversal.py TXN1001    # just one

Nine complaints, one per branch the engine can take. Five get their money
back automatically; four go to a human, each for a different reason.
"""

from __future__ import annotations

import os
import sys
import textwrap

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import reversal  # noqa: E402
import reversal_ledger as ledger  # noqa: E402
from reversal_models import rupees  # noqa: E402

SCENARIOS: list[tuple[str, str, str]] = [
    ("TXN1001",
     "I cancelled the Netflix autopay mandate last week from my UPI app but they "
     "still took 649 rupees from my account on Monday. Please refund it.",
     "revoked mandate, still debited"),

    ("TXN1002",
     "My gym autopay limit was 500 but FitPass charged me 999. That is more than "
     "I ever approved.",
     "debit above the mandate cap"),

    ("TXN1004",
     "CloudStore has deducted 1199 twice from my account on the same day. I only "
     "have one subscription.",
     "duplicate debit"),

    ("TXN1005",
     "EduPrime took 1499 from my account and I got no SMS, no notification, "
     "nothing. I only found out when I checked my balance.",
     "no pre-debit notification"),

    ("TXN1006",
     "My InsureCo policy mandate had expired but they still collected 48,000 "
     "rupees from my account.",
     "verified breach, but above the auto-reversal ceiling"),

    ("TXN1007",
     "ShopMax charged me 899 without informing me first. No warning at all.",
     "verified breach, but the customer is a repeat claimant"),

    ("TXN1008",
     "There is a StreamPlus mandate on my account that I never set up. I don't "
     "recognise this merchant at all. Someone else has done this.",
     "suspected unauthorised mandate"),

    ("TXN1009",
     "I cancelled my MealBox subscription with them last month but they charged "
     "me 799 again anyway.",
     "cancelled with the merchant, not with the bank"),

    ("TXN1010",
     "EduPrime charged me twice, I want the second one back.",
     "claim contradicted by the ledger"),
]

W = 78
BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
GREEN, YELLOW, CYAN = "\033[32m", "\033[33m", "\033[36m"

if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    BOLD = DIM = RESET = GREEN = YELLOW = CYAN = ""


def wrap(text: str, indent: str = "    ") -> str:
    return "\n".join(
        textwrap.fill(line, width=W, initial_indent=indent, subsequent_indent=indent)
        for line in text.split("\n")
    )


def run(txn_id: str, complaint: str, label: str) -> str:
    txn = ledger.get_transaction(txn_id)
    print(f"\n{BOLD}{'─' * W}{RESET}")
    print(f"{BOLD}{txn_id}{RESET}  {DIM}{label}{RESET}")
    print(f"{DIM}{txn.merchant_name} · {rupees(txn.amount_paise)} · "
          f"{txn.debited_at:%d %b %Y %H:%M}{RESET}")
    print(f"\n{CYAN}Customer:{RESET}")
    print(wrap(f'"{complaint}"'))

    res = reversal.resolve(txn_id, complaint)

    colour = GREEN if res["outcome"] == "AUTO_REVERSE" else YELLOW
    print(f"\n{colour}{BOLD}→ {res['outcome']}{RESET}  {DIM}({res['reason_code']}){RESET}")

    print(f"\n{CYAN}Checks:{RESET}")
    for line in res["evidence"]:
        print(f"    {line}")

    print(f"\n{CYAN}Why:{RESET}")
    print(wrap(res["rationale"]))

    print(f"\n{CYAN}Actions taken:{RESET}")
    for a in res["actions"]:
        name = a.get("action")
        if name == "REVERSAL":
            print(f"    · reversed {a['amount']} → {a['reversal_txn_id']}")
        elif name in ("MANDATE_STOP", "MANDATE_PAUSE"):
            print(f"    · mandate {a['mandate_id']}: {a['previous_status']} → {a['new_status']}")
        elif name == "MANDATE_NOOP":
            print(f"    · {a['message']}")
        elif name == "CASE":
            print(f"    · ticket {a['ticket']['id']}")
    if res["outcome"] == "ESCALATE":
        print(f"    · queued to {res.get('queue')} ({res.get('priority')}), "
              f"recommendation {res.get('recommendation')}")

    print(f"\n{CYAN}Customer hears:{RESET}")
    print(wrap(res["customer_message"]))
    return res["outcome"]


def main() -> None:
    if not ledger.read_json(ledger.TRANSACTIONS_FILE):
        print("No account records found. Run:  python seed_reversal_ledger.py")
        raise SystemExit(1)

    wanted = sys.argv[1:]
    scenarios = [s for s in SCENARIOS if not wanted or s[0] in wanted]
    if not scenarios:
        print(f"no scenario matching {wanted}. "
              f"Known: {', '.join(s[0] for s in SCENARIOS)}")
        raise SystemExit(1)

    print(f"\n{BOLD}UPI Autopay reversal desk — {len(scenarios)} complaints{RESET}")
    outcomes = [run(*s) for s in scenarios]

    auto = outcomes.count("AUTO_REVERSE")
    print(f"\n{BOLD}{'─' * W}{RESET}")
    print(f"{BOLD}Summary{RESET}  {GREEN}{auto} auto-reversed{RESET} · "
          f"{YELLOW}{len(outcomes) - auto} escalated{RESET}")

    trail = ledger.audit_trail(limit=200)
    money = [r for r in trail if r["action"] == "REVERSAL"]
    print(f"{DIM}audit log: {len(trail)} entries, {len(money)} of them money "
          f"movements{RESET}\n")


if __name__ == "__main__":
    main()

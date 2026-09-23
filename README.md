# UPI Autopay Reversal Agent

**Deciding when a machine may give a customer their money back.**

An MCP server that resolves UPI Autopay debit complaints. When the bank's own records prove the debit should never have happened, it reverses the money in seconds. When they don't, it hands a human everything they need — and gets out of the way.

| | |
|---|---|
| **Domain** | Banking / UPI Autopay |
| **Interfaces** | Model Context Protocol · REST API · Web UI |
| **Tests** | 66 passing, 0.03s |
| **Status** | Working end to end |

📄 [Full concept note (PDF)](UPI_Autopay_Reversal_Agent_Concept.pdf) · 📘 [Engine documentation](REVERSAL.md)

---

## The problem

A UPI Autopay mandate lets a merchant pull money from your account on a schedule without asking again. When it goes wrong, the money is already gone, and getting it back means convincing someone it shouldn't have left.

The naive fix is to have a language model read the complaint and decide. That fails in the direction that costs money: **the model is reading the account of the person asking to be paid.** A confident, well-written complaint is not evidence. Neither is an emotional one.

## The rule the whole design rests on

> Reverse money automatically **only** when the bank can prove the breach from its own records. Everything else goes to a human.

"Genuine" is not a mood read off the complaint text. A complaint is genuine, for the purposes of automatic reversal, when the mandate registry, the debit ledger and the pre-debit notice log — bank-side data the customer cannot influence — contradict the debit that was taken.

---

## Two families of complaint

**Verifiable** — the ledger settles it alone. These reverse automatically.

| Reason code | What the check proves |
|---|---|
| `MANDATE_REVOKED_BEFORE_DEBIT` | Debit timestamp falls after the recorded revocation |
| `DEBIT_AFTER_MANDATE_EXPIRY` | Debit falls outside the mandate's validity window |
| `AMOUNT_EXCEEDS_MANDATE_CAP` | Amount is above the cap agreed at registration |
| `DUPLICATE_DEBIT` | Same mandate, same amount, inside 24 hours |
| `MISSING_PRE_DEBIT_NOTIFICATION` | No notice on record, or one sent under the required 24h |

**Contested** — someone outside the bank holds the evidence. These never reverse automatically, however sympathetic the complaint.

| Reason code | Why a person decides |
|---|---|
| `UNAUTHORIZED_MANDATE` | Suspected fraud — a false positive funds the fraudster. The mandate is paused in the same second, but an analyst signs off. |
| `SERVICE_NOT_RENDERED` | Turns on what the merchant did or failed to do |
| `SUBSCRIPTION_CANCELLED_WITH_MERCHANT` | Cancelled with the merchant, never with the bank |
| `AMOUNT_DISPUTED_WITH_MERCHANT` | A pricing dispute needing merchant representment |

---

## Guardrails: proof is necessary, not sufficient

A confirmed breach earns a reversal only if nothing else about the case argues for a person.

- **Value ceiling** — above ₹25,000, a person authorises the credit
- **Claim frequency** — three auto-reversals in ninety days routes the fourth to risk review
- **Account standing** — frozen accounts and unverified KYC take no automated credits
- **Dispute window** — debits older than ninety days need supervisory approval
- **Idempotency** — a debit already reversed is never reversed again. An MCP host can retry a tool call on timeout, and a retry that pays twice is a real loss.

**There is no automatic rejection.** A bank does not close a customer's money complaint without a person signing off. When the ledger flatly contradicts the claim, the case still goes to a human carrying a `LIKELY_INVALID` recommendation. The strongest thing this system does on its own authority is disagree in writing.

---

## Two cases, end to end

### `TXN1001` · Netflix India · ₹649 → **AUTO-REVERSE**

> "I cancelled the Netflix autopay mandate last week from my UPI app but they still took 649 rupees on Monday."

- **Classified** `MANDATE_REVOKED_BEFORE_DEBIT`
- **Check** — revocation recorded 18 Sep, debit executed 21 Sep. Three days after. Confirmed.
- **Guardrails** — all clear
- **Action** — ₹649 credited back with a reversal reference; merchant flagged for follow-up

### `TXN1006` · InsureCo · ₹48,000 → **ESCALATE**

> "My InsureCo policy mandate had expired but they still collected 48,000 rupees."

- **Classified** `DEBIT_AFTER_MANDATE_EXPIRY`
- **Check** — mandate valid until 03 Sep, debit executed 18 Sep. Confirmed; the customer is almost certainly owed this money.
- **Guardrails** — ₹48,000 exceeds the ₹25,000 ceiling. Stop.
- **Action** — routed to `high-value-reversals` with the evidence attached and the reversal amount pre-computed. The advisor approves a finding; they do not repeat the investigation.

Nine scenarios ship with the project, one per branch the engine can take. Four escalate, for four different reasons.

---

## Why a crude classifier is safe here

Mapping free text to a reason code is done with keyword matching. It is safe anyway, and the reason is the argument the whole design rests on:

**Classification never decides that money moves. It decides which verification to run.** Route a complaint to the wrong check and the check fails against the ledger, and the case escalates to a person. The failure mode of a misread complaint is a slower answer — never a wrong payout. A test pins exactly this.

This is also what makes it safe to let a language model participate. A model can propose a reason code and the engine will use it — and will still refuse to reverse if the ledger does not agree.

> **The model gets to be helpful about understanding. It never gets to be authoritative about paying.**

---

## Architecture

```
mcp-server/
├── reversal_policy.py    Decision engine. A pure function of the case — no I/O,
│                         no clock reads, no model calls. 66 tests in 0.03s.
├── reversal_ledger.py    Mandates, debits, notices, accounts + append-only audit log
├── reversal.py           Carries a decision out: moves money, stops mandates,
│                         opens tickets, drafts what the customer is told
├── mcp_server.py         MCP surface (official Python SDK)
├── qna_action_mcp_server.py   FastAPI REST API
└── static/index.html     Decision-trace web UI
configs/                  Per-domain intents, actions, knowledge, persona
data/                     JSON stores
```

The split between the first and third entries is the one that matters. The hard part — deciding — is a pure function you can test exhaustively in milliseconds. The risky part — acting — is a short, boring file. Neither is allowed to become the other.

**Tool tiering is the security model.** A model may call the read tools freely, should call `assess_reversal` before acting, and finds the effecting tools gated. `assess_reversal` runs the full engine and changes nothing, so a model can say what will happen before it happens. `initiate_refund_or_reversal` re-runs the engine internally and refuses if it does not independently reach the same verdict.

---

## Running it

```bash
cd mcp-server
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

python seed_reversal_ledger.py                          # load the 9 scenarios
uvicorn qna_action_mcp_server:app --port 8000            # then open http://localhost:8000/ui
```

Click **TXN1001** or **TXN1006** and the decision trace renders: classified reason code, the ledger check and its evidence, the guardrail outcome, the final action, and what the customer hears.

**As an MCP server**, add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "qna-action-mcp": {
      "command": "/abs/path/to/mcp-server/venv-mcp/bin/python",
      "args": ["/abs/path/to/mcp-server/mcp_server.py"]
    }
  }
}
```

Then describe the problem in plain language and let the model work. It reads the complaint, picks the tools, and explains the outcome — while the engine holds the authority.

**Tests:**

```bash
pytest test_reversal_policy.py -q      # 66 passed
```

---

## What this is honest about

The demo is real — no API key, no network, the actual decision path rather than a script. But a demo is not a deployment. The gaps worth naming:

- **Reversals are written to a JSON store.** Real money movement needs a transactional store and an idempotency key supplied by the caller, not just the transaction id.
- **The ninety-day counter increments but never ages back down.** A real implementation counts reversal rows inside a rolling window.
- **The notice check treats a missing row as proof no notice was sent.** If the notification service can silently drop rows, that assumption pays out money it should not. This is the single most dangerous assumption in the system, and it is a data-integrity problem, not a logic one.
- **Reason codes cover the common breaches, not every one.** An unmapped complaint escalates, which is the correct failure — but it is still an unmapped complaint.

---

## The one-line version

Most of the value here is not automation. It is drawing a defensible line between the complaints a machine can settle and the ones it must not — and then making the machine unable to cross it, even when a persuasive customer or a confident model would like it to.

---

Built for the Neutrinos Venture Studio Hackathon by [Shiv Arora](https://github.com/shivabtech23).

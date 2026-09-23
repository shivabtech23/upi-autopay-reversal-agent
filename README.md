# QnA Action MCP Server

A local, hackathon-style project that handles structured workflows instead of plain chat replies. It ships two interfaces to the same domain-driven workflow engine:

- **`mcp-server/qna_action_mcp_server.py`** — a plain REST API (FastAPI) used by the CLI client and the browser chat UI. Good for demos, not a real MCP server despite the name.
- **`mcp-server/mcp_server.py`** — a genuine **Model Context Protocol** server built on the official `mcp` Python SDK, exposing MCP Resources and Tools that a real MCP host (Claude Desktop, Claude Code) can connect to. See [MCP Server](#mcp-server-real-model-context-protocol) below.

Both share the same business logic in `mcp-server/core.py`, so behavior never drifts between the two.

When a user sends a message like *"my autopay subscription got auto debited and I want a refund"*, the server:

1. **Detects intent** using keyword/pattern matching
2. **Identifies the domain** (telecom, healthcare, etc.)
3. **Validates required fields** before executing any action
4. **Asks for missing fields** if any are absent
5. **Executes a structured action** — creates a ticket or notification
6. **Persists outputs** to local JSON files
7. **Logs every request** for auditability

---

## Project Structure

```
qna-action-mcp/
├── configs/
│   ├── domain_config.json        # Global domain registry
│   ├── telecom/
│   │   ├── intents.json          # Intent definitions + required fields
│   │   ├── actions.json          # Action metadata
│   │   ├── knowledge.json        # Policies and notes
│   │   └── persona.json          # Bot tone and rules
│   └── healthcare/
│       ├── intents.json
│       ├── actions.json
│       ├── knowledge.json
│       └── persona.json
├── data/
│   ├── tickets.json              # All created tickets
│   ├── notifications.json        # All created notifications
│   └── records.json              # Full request/response log
├── mcp-client/
│   ├── client.py                 # CLI client (interactive + one-shot)
│   └── requirements.txt
├── mcp-server/
│   ├── core.py                   # Shared business logic (config, intent detection, actions)
│   ├── qna_action_mcp_server.py  # REST API (FastAPI) — used by CLI client + web UI
│   ├── mcp_server.py             # Real MCP server (official `mcp` SDK, stdio transport)
│   ├── test_mcp_client.py        # Scripted client that exercises mcp_server.py end to end
│   ├── static/index.html         # Browser chat UI, served at /ui
│   ├── requirements.txt          # REST API deps (Python 3.9+)
│   ├── requirements-mcp.txt      # MCP server deps (Python 3.10+ required)
│   └── Dockerfile
└── README.md
```

---

## Quickstart

### 1. Run the Server

```bash
cd mcp-server
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
uvicorn qna_action_mcp_server:app --reload --port 8000
```

Server starts at: `http://localhost:8000`

---

### 2. Run the Client

In a separate terminal:

```bash
cd mcp-client
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Interactive mode
python client.py

# One-shot mode
python client.py --message "I want a refund for autopay" --domain telecom \
  --fields account_number=ACC123 amount=499 transaction_date=2024-04-01
```

---

### 3. Or use the Web UI

With the server running (step 1), just open a browser:

```
http://localhost:8000
```

A chat-style interface — pick a domain, click a suggested issue or type your own, and it walks you through context and field collection in the browser. No client install needed.

---

## MCP Server (real Model Context Protocol)

`mcp_server.py` is a genuine MCP server built on the official [`mcp` Python SDK](https://pypi.org/project/mcp/), which requires **Python 3.10+**. It runs separately from the REST API above, in its own venv, and exposes the same underlying engine (`core.py`) as real MCP Resources and Tools instead of HTTP endpoints.

### Resources (read-only)

| URI                   | Returns                                              |
|------------------------|-------------------------------------------------------|
| `domains://list`       | Domain registry (`domain_config.json`)                |
| `knowledge://{domain}` | Knowledge base / policy notes / SLAs                   |
| `intents://{domain}`   | Intent definitions: name, description, required fields, priority |
| `persona://{domain}`   | Bot tone, behavioral rules, escalation message          |

### Tools (the only way to write data)

| Tool                              | Purpose                                                                 |
|------------------------------------|--------------------------------------------------------------------------|
| `search_knowledge`                 | Free-text search over a domain's knowledge base                         |
| `create_case`                      | Detect intent from a message and open a ticket once fields are complete |
| `initiate_refund_or_reversal`      | Same, but refuses to run unless the detected intent is refund/reversal/dispute-type |
| `pause_or_cancel_mandate`          | Same, but refuses to run unless the intent is cancel/pause/mandate-type |
| `send_notification_or_escalation`  | Raises a notification, or escalates to a human agent if context/reason warrants it |

The two restricted tools are the "controlled interface" from the design doc in practice: an MCP host can't use `initiate_refund_or_reversal` to block a card, or `pause_or_cancel_mandate` to file a refund — each tool checks the detected intent against an allow-list and returns an error instead of acting.

### Setup

```bash
cd mcp-server
brew install python@3.12        # or any Python 3.10+
python3.12 -m venv venv-mcp
venv-mcp/bin/pip install -r requirements-mcp.txt
```

### Test it

Scripted, no host required — spawns the server over stdio, lists resources/tools, and calls all 5 tools with sample data:

```bash
venv-mcp/bin/python test_mcp_client.py
```

Or interactively, via the official MCP Inspector (browser-based):

```bash
venv-mcp/bin/mcp dev mcp_server.py
```

### Connect it to a real MCP host

To let Claude Desktop or Claude Code actually call these tools, add to its MCP config (e.g. `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "qna-action-mcp": {
      "command": "/absolute/path/to/mcp-server/venv-mcp/bin/python",
      "args": ["/absolute/path/to/mcp-server/mcp_server.py"]
    }
  }
}
```

**Scope note:** intent detection here is still keyword matching (same as the REST API) — it does not do LLM-based reasoning or the confidence/risk-based autonomy tiers described in the original design doc. The MCP layer makes the tool/resource boundary real; deeper agentic reasoning would be a separate follow-up.

---

## API Endpoints

| Method | Endpoint   | Description                          |
|--------|------------|--------------------------------------|
| GET    | `/health`  | Server health and loaded domains     |
| GET    | `/domains` | Available domains and their intents  |
| GET    | `/tools`   | Available actions per domain         |
| POST   | `/run`     | Main workflow endpoint               |

### POST /run — Request Body

```json
{
  "message": "my autopay got debited and I want a refund",
  "domain": "telecom",
  "fields": {
    "account_number": "ACC123",
    "amount": "499",
    "transaction_date": "2024-04-01"
  }
}
```

### POST /run — Response

```json
{
  "ok": true,
  "domain": "telecom",
  "detected_intent": "autopay_refund",
  "next_step": "action_executed",
  "missing_fields": [],
  "action_result": {
    "id": "A1B2C3D4",
    "timestamp": "2024-04-04T10:30:00+00:00",
    "domain": "telecom",
    "intent": "autopay_refund",
    "action": "create_ticket",
    "priority": "high",
    "fields": { "account_number": "ACC123", "amount": "499", "transaction_date": "2024-04-01" },
    "status": "created",
    "message": "A support ticket has been created. Our team will contact you within 24-48 hours."
  },
  "notes": [
    "Autopay and auto-debit refunds are processed within 5-7 business days.",
    "For urgent issues, call 1-800-TELECOM (Mon-Sat, 9AM-6PM)."
  ]
}
```

---

## Example Requests

### Telecom — Autopay Refund (full fields)

```bash
curl -X POST http://localhost:8000/run \
  -H "Content-Type: application/json" \
  -d '{
    "message": "my autopay subscription got auto debited and I want a refund",
    "domain": "telecom",
    "fields": {
      "account_number": "ACC-9921",
      "amount": "799",
      "transaction_date": "2024-04-01"
    }
  }'
```

### Healthcare — Book Appointment (full fields)

```bash
curl -X POST http://localhost:8000/run \
  -H "Content-Type: application/json" \
  -d '{
    "message": "I need to book an appointment with a cardiologist",
    "domain": "healthcare",
    "fields": {
      "patient_name": "Jane Doe",
      "doctor_specialization": "Cardiologist",
      "preferred_date": "2024-04-10"
    }
  }'
```

### Missing Fields (server asks for more info)

```bash
curl -X POST http://localhost:8000/run \
  -H "Content-Type: application/json" \
  -d '{
    "message": "I want a refund for autopay",
    "domain": "telecom",
    "fields": {}
  }'
```
Response will have `"next_step": "collect_fields"` and `"missing_fields": ["account_number", "amount", "transaction_date"]`.

---

## Example Ticket Output (`data/tickets.json`)

```json
[
  {
    "id": "A1B2C3D4",
    "timestamp": "2024-04-04T10:30:00+00:00",
    "domain": "telecom",
    "intent": "autopay_refund",
    "action": "create_ticket",
    "priority": "high",
    "fields": {
      "account_number": "ACC-9921",
      "amount": "799",
      "transaction_date": "2024-04-01"
    },
    "status": "created",
    "message": "A support ticket has been created. Our team will contact you within 24-48 hours."
  }
]
```

---

## Supported Domains

### Telecom
| Intent          | Required Fields                               | Action              |
|-----------------|-----------------------------------------------|---------------------|
| autopay_refund  | account_number, amount, transaction_date      | create_ticket       |
| bill_dispute    | account_number, bill_month                    | create_ticket       |
| service_outage  | account_number, location                      | create_ticket       |
| plan_upgrade    | account_number, desired_plan                  | create_notification |

### Healthcare
| Intent               | Required Fields                                    | Action              |
|----------------------|----------------------------------------------------|---------------------|
| book_appointment     | patient_name, doctor_specialization, preferred_date| create_ticket       |
| cancel_appointment   | patient_name, appointment_id                       | create_notification |
| prescription_refill  | patient_name, prescription_id, pharmacy_name       | create_ticket       |
| lab_report_query     | patient_name, test_date                            | create_notification |

---

## Privacy Guardrails

The server **refuses** to collect or process:
- `otp` / `one_time_password`
- `verification_code`
- `passcode`

Any request containing these fields receives a `privacy_violation` response.

---

## Adding a New Domain

1. Create a folder under `configs/<domain_name>/`
2. Add `intents.json`, `actions.json`, `knowledge.json`, `persona.json`
3. Add the domain name to `configs/domain_config.json` → `supported_domains`
4. Restart the server — it auto-loads all configs on startup

---

## 1-Minute Demo Script

```
1. Start server:    cd mcp-server && uvicorn qna_action_mcp_server:app --port 8000
2. Open browser:    http://localhost:8000/docs  (Swagger UI auto-generated)
3. Run demo curl:   (autopay refund with missing fields → server asks for them)
4. Run curl again:  (with all fields → ticket created)
5. Show file:       cat data/tickets.json
6. Show log:        cat data/records.json
```

---

## Tech Stack

- **FastAPI** — web framework + auto Swagger docs
- **Pydantic** — request/response validation
- **Uvicorn** — ASGI server
- **requests** — client HTTP calls
- **JSON files** — zero-dependency persistence

---

## License

MIT — built for hackathon demos. Fork it, break it, ship it.

---

## Autopay reversal decision layer

`initiate_refund_or_reversal` now reverses money when the bank's own records
confirm the breach, and escalates to a human when they don't. The rule, the
reason codes, the guardrails and the nine demo scenarios are documented in
[REVERSAL.md](REVERSAL.md).

Quick start:

```bash
cd mcp-server
python seed_reversal_ledger.py
python demo_reversal.py
python -m pytest test_reversal_policy.py -q
```

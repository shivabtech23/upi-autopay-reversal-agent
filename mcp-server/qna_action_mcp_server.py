"""
QnA Action REST API
--------------------
A FastAPI server that handles structured, multi-turn workflows:

  Stage 1 — "new"
    User sends a message. Server detects intent and asks an open-ended
    context question ("what happened?") before doing anything else.

  Stage 2 — "context_given"
    User explains the situation. Server reads the context:
      - If escalate_keywords match → transfer to human agent
      - Otherwise           → ask for the required fields

  Stage 3 — "fields_given"
    All required fields are present. Server executes the action
    (create_ticket or create_notification) and returns the result.

Business logic (config loading, intent detection, action execution) lives in
core.py, shared with the real MCP server (mcp_server.py) so both interfaces
stay in sync.

Run with:
    uvicorn qna_action_mcp_server:app --reload --port 8000
"""

import os
import re
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import core
import reversal
import reversal_ledger

STATIC_DIR = os.path.join(core.BASE_DIR, "static")

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="QnA Action REST API",
    description="Multi-turn, domain-driven workflow API (REST interface over core.py)",
    version="2.0.0",
)

# In-memory config store
domain_config, domains = core.load_all_configs()


# ---------------------------------------------------------------------------
# Startup: load all configs
# ---------------------------------------------------------------------------
@app.on_event("startup")
def load_configs():
    global domain_config, domains
    domain_config, domains = core.load_all_configs()
    print(f"[startup] Loaded domains: {list(domains.keys())}")


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------
class AssessRequest(BaseModel):
    transaction_id: str
    complaint_text: str
    reason_code: Optional[str] = None


class RunRequest(BaseModel):
    message: str
    domain:  Optional[str] = None
    stage:   Optional[str] = "new"      # "new" | "context_given" | "fields_given"
    context: Optional[str] = ""         # user's explanation of the situation
    fields:  Optional[dict] = {}


class RunResponse(BaseModel):
    ok:               bool
    domain:           str
    detected_intent:  Optional[str]
    stage:            str               # current stage after this response
    next_step:        str               # what the client should do next
    question:         Optional[str]     # question to show the user (if any)
    missing_fields:   list
    escalated:        bool
    action_result:    Optional[dict]
    notes:            list
    decision_trace:   Optional[dict] = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {
        "status": "ok",
        "server": "QnA Action REST API",
        "version": "2.0.0",
        "loaded_domains": list(domains.keys()),
    }


@app.get("/domains")
def get_domains():
    result = {}
    for name, config in domains.items():
        result[name] = {
            "intents": [i["name"] for i in config["intents"]],
            "intent_descriptions": {
                i["name"]: i.get("description", "") for i in config["intents"]
            },
            "persona": config["persona"]["name"],
        }
    return {
        "default_domain":    domain_config["default_domain"],
        "supported_domains": result,
    }


@app.get("/tools")
def get_tools():
    tools = {}
    for name, config in domains.items():
        tools[name] = [
            {"name": a["name"], "description": a["description"],
             "output_store": a.get("output_store")}
            for a in config["actions"]
        ]
    return {"tools": tools}


# ---------------------------------------------------------------------------
# POST /assess — dry-run reversal decision engine
# ---------------------------------------------------------------------------
@app.post("/assess")
def assess(request: AssessRequest):
    """
    Dry-run the reversal policy. Returns the decision and its evidence,
    changing nothing. Mirrors assess_reversal in mcp_server.py.
    """
    try:
        return reversal.resolve(
            txn_id=request.transaction_id,
            complaint_text=request.complaint_text,
            reason_code=request.reason_code,
            dry_run=True,
        )
    except LookupError as exc:
        return {"ok": False, "error": str(exc)}
    except ValueError as exc:
        return {"ok": False, "error": f"unknown reason_code: {exc}"}


DEMO_COMPLAINTS = {
    "TXN1001": "I cancelled the Netflix autopay mandate last week from my UPI app but they still took 649 rupees from my account on Monday. Please refund it.",
    "TXN1002": "FitPass Gym debited 999 rupees from my account. My autopay limit was 500 rupees only.",
    "TXN1003": "CloudStore debited 1199 rupees from my account.",
    "TXN1004": "they deducted the same amount twice today",
    "TXN1005": "EduPrime took 1499 from my account and I got no SMS, no notification, nothing. I only found out when I checked my balance.",
    "TXN1006": "My InsureCo policy mandate had expired but they still collected 48,000 rupees from my account.",
    "TXN1007": "ShopMax charged me 899 without informing me first. No warning at all.",
    "TXN1008": "There is a StreamPlus mandate on my account that I never set up. I don't recognise this merchant at all. Someone else has done this.",
    "TXN1009": "I cancelled my MealBox subscription with them last month but they charged me 799 again anyway.",
    "TXN1010": "EduPrime charged me twice, I want the second one back.",
}


def extract_recognized_transaction_id(fields: Optional[dict], message: str, context: Optional[str]) -> Optional[str]:
    fields = fields or {}
    for key in ("transaction_id", "txn_id", "txn"):
        val = fields.get(key)
        if val and reversal_ledger.get_transaction(str(val).strip().upper()):
            return str(val).strip().upper()

    combined = f"{message} {context or ''}"
    candidates = re.findall(r'\b[A-Za-z0-9_-]+\b', combined)
    for c in candidates:
        cu = c.upper()
        if reversal_ledger.get_transaction(cu):
            return cu

    return None


@app.post("/run", response_model=RunResponse)
def run(request: RunRequest):
    """
    Multi-turn workflow endpoint.
    If a recognised transaction ID is present, calls the reversal decision engine
    (reversal.resolve()) and returns the outcome (AUTO_REVERSE or ESCALATE).
    Otherwise falls through to the existing legacy stage flow.
    """

    # --- Resolve domain ---
    domain_name = request.domain or domain_config["default_domain"]
    if domain_name not in domains:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown domain '{domain_name}'. Supported: {list(domains.keys())}",
        )

    persona   = domains[domain_name]["persona"]
    knowledge = domains[domain_name]["knowledge"]
    stage     = request.stage or "new"

    # --- Privacy guardrail (always active) ---
    blocked = core.check_privacy_violation(request.fields or {})
    if blocked:
        resp = RunResponse(
            ok=False, domain=domain_name, detected_intent=None,
            stage=stage, next_step="privacy_violation", question=None,
            missing_fields=[], escalated=False, action_result=None,
            notes=[
                f"Field '{blocked}' cannot be collected for privacy reasons.",
                persona["rules"][0],
            ],
        )
        core.log_record(request.dict(), resp.dict())
        return resp

    # --- Check for recognized transaction ID (reversal decision engine) ---
    txn_id = extract_recognized_transaction_id(request.fields, request.message, request.context)
    if txn_id:
        complaint_text = f"{request.message} {request.context or ''}".strip()
        # If user provided only the transaction id, enrich with benchmark scenario complaint
        if complaint_text.upper().strip() == txn_id and txn_id in DEMO_COMPLAINTS:
            complaint_text = DEMO_COMPLAINTS[txn_id]

        reversal_res = reversal.resolve(
            txn_id=txn_id,
            complaint_text=complaint_text,
            domain=domain_name,
            intent={"name": "autopay_refund"},
            domains=domains,
            fields=request.fields or {"transaction_id": txn_id},
            dry_run=False,
        )

        resp = RunResponse(
            ok=True,
            domain=domain_name,
            detected_intent=reversal_res.get("reason_code") or "autopay_refund",
            stage="completed",
            next_step="reversal_resolved",
            question=None,
            missing_fields=[],
            escalated=(reversal_res.get("outcome") == "ESCALATE"),
            action_result={
                "id": reversal_res.get("reversal_txn_id") or reversal_res.get("case_id") or txn_id,
                "outcome": reversal_res.get("outcome"),
                "reason_code": reversal_res.get("reason_code"),
                "queue": reversal_res.get("queue"),
                "reversal_txn_id": reversal_res.get("reversal_txn_id"),
                "case_id": reversal_res.get("case_id"),
                "amount": reversal_res.get("amount"),
                "message": reversal_res.get("customer_message"),
                "actions": reversal_res.get("actions", []),
            },
            notes=reversal_res.get("evidence", []),
            decision_trace=reversal_res,
        )
        core.log_record(request.dict(), resp.dict())
        return resp

    # --- Detect intent (needed in all stages for legacy flow) ---
    intent = core.detect_intent(request.message, domain_name, domains)

    if not intent:
        resp = RunResponse(
            ok=False, domain=domain_name, detected_intent=None,
            stage="new", next_step="clarify_intent", question=None,
            missing_fields=[], escalated=False, action_result=None,
            notes=[
                "Could not detect a clear intent from your message.",
                "Please describe your issue more specifically.",
                f"Available intents: {[i['name'] for i in domains[domain_name]['intents']]}",
            ],
        )
        core.log_record(request.dict(), resp.dict())
        return resp

    # ================================================================
    # STAGE 1 — "new": ask the open-ended context question
    # ================================================================
    if stage == "new":
        resp = RunResponse(
            ok=True,
            domain=domain_name,
            detected_intent=intent["name"],
            stage="new",
            next_step="ask_context",
            question=intent.get(
                "context_question",
                "Could you please describe what happened and when?"
            ),
            missing_fields=[],
            escalated=False,
            action_result=None,
            notes=[],
        )
        core.log_record(request.dict(), resp.dict())
        return resp

    # ================================================================
    # STAGE 2 — "context_given": assess the situation
    # ================================================================
    if stage == "context_given":
        context = request.context or ""

        # Check if this needs a human
        if core.should_escalate(context, intent):
            resp = RunResponse(
                ok=True,
                domain=domain_name,
                detected_intent=intent["name"],
                stage="context_given",
                next_step="human_transfer",
                question=None,
                missing_fields=[],
                escalated=True,
                action_result=None,
                notes=[
                    persona.get("escalation_message", "Transferring to a human agent."),
                    "Based on what you've described, this requires immediate human attention.",
                    knowledge.get("contact_info", knowledge.get("emergency_note", "")),
                ],
            )
            core.log_record(request.dict(), resp.dict())
            return resp

        # No escalation — check for missing fields
        missing = core.get_missing_fields(intent["required_fields"], request.fields or {})
        if missing:
            resp = RunResponse(
                ok=True,
                domain=domain_name,
                detected_intent=intent["name"],
                stage="context_given",
                next_step="collect_fields",
                question=f"Thank you for explaining. I'll need a few more details to proceed.",
                missing_fields=missing,
                escalated=False,
                action_result=None,
                notes=[],
            )
            core.log_record(request.dict(), resp.dict())
            return resp

        # All fields already provided — fall through to execute
        stage = "fields_given"

    # ================================================================
    # STAGE 3 — "fields_given": validate + execute action
    # ================================================================
    if stage == "fields_given":
        missing = core.get_missing_fields(intent["required_fields"], request.fields or {})
        if missing:
            resp = RunResponse(
                ok=False,
                domain=domain_name,
                detected_intent=intent["name"],
                stage="fields_given",
                next_step="collect_fields",
                question="Still need a few more details.",
                missing_fields=missing,
                escalated=False,
                action_result=None,
                notes=[],
            )
            core.log_record(request.dict(), resp.dict())
            return resp

        # Execute the action
        try:
            action_result = core.execute_action(
                intent["action"], domain_name, intent,
                request.fields, request.context or "", domains
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        # Pull the relevant policy note
        notes = []
        policy_key = core.INTENT_KNOWLEDGE_MAP.get(intent["name"])
        if policy_key and policy_key in knowledge:
            notes.append(knowledge[policy_key])
        notes.append(knowledge.get("contact_info", knowledge.get("emergency_note", "")))

        resp = RunResponse(
            ok=True,
            domain=domain_name,
            detected_intent=intent["name"],
            stage="fields_given",
            next_step="action_executed",
            question=None,
            missing_fields=[],
            escalated=False,
            action_result=action_result,
            notes=[n for n in notes if n],
        )
        core.log_record(request.dict(), resp.dict())
        return resp

    # Should never reach here
    raise HTTPException(status_code=400, detail=f"Unknown stage: '{stage}'")


# ---------------------------------------------------------------------------
# Web UI (static chat client)
# ---------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/ui/")


app.mount("/ui", StaticFiles(directory=STATIC_DIR, html=True), name="ui")

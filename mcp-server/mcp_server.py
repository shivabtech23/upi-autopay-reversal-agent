"""
QnA Action — real MCP server
------------------------------
Exposes the same domain-driven workflow engine as qna_action_mcp_server.py
(REST API), but through the actual Model Context Protocol: MCP Resources for
read-only config/knowledge access, and MCP Tools for the controlled actions
an MCP host (Claude Desktop, Claude Code, etc.) is allowed to take.

  Resources (read-only):
    knowledge://{domain}   knowledge base / policies
    intents://{domain}     intent definitions + required fields (eligibility rules)
    persona://{domain}     tone, rules, response guidelines
    domains://list         domain registry

  Tools (actions — the only way to write data):
    search_knowledge
    create_case
    initiate_refund_or_reversal   (restricted to refund/reversal/dispute intents)
    pause_or_cancel_mandate       (restricted to cancel/pause/mandate intents)
    send_notification_or_escalation

Run over stdio (the standard MCP host transport):
    venv-mcp/bin/python mcp_server.py

Or interactively via the MCP Inspector:
    venv-mcp/bin/mcp dev mcp_server.py
"""

from typing import Optional

from mcp.server.fastmcp import FastMCP

import core
import reversal
import reversal_ledger
from reversal_models import rupees

mcp = FastMCP("qna-action-mcp")

# Configs are static for the process lifetime — load once at import time.
domain_config, domains = core.load_all_configs()


# ---------------------------------------------------------------------------
# Resources (read-only)
# ---------------------------------------------------------------------------
@mcp.resource("domains://list")
def list_domains() -> dict:
    """Registry of supported domains and the default domain."""
    return domain_config


@mcp.resource("knowledge://{domain}")
def get_knowledge(domain: str) -> dict:
    """Knowledge base / policy notes / SLAs for a domain."""
    if domain not in domains:
        return {"error": f"Unknown domain '{domain}'. Supported: {list(domains.keys())}"}
    return domains[domain]["knowledge"]


@mcp.resource("intents://{domain}")
def get_intents(domain: str) -> list:
    """Intent definitions for a domain: name, description, required fields, priority."""
    if domain not in domains:
        return [{"error": f"Unknown domain '{domain}'. Supported: {list(domains.keys())}"}]
    return [
        {
            "name": i["name"],
            "description": i.get("description", ""),
            "required_fields": i.get("required_fields", []),
            "priority": i.get("priority", "medium"),
            "action": i.get("action"),
        }
        for i in domains[domain]["intents"]
    ]


@mcp.resource("persona://{domain}")
def get_persona(domain: str) -> dict:
    """Bot tone, behavioral rules, and escalation message for a domain."""
    if domain not in domains:
        return {"error": f"Unknown domain '{domain}'. Supported: {list(domains.keys())}"}
    return domains[domain]["persona"]


# ---------------------------------------------------------------------------
# Tools (actions)
# ---------------------------------------------------------------------------
@mcp.tool()
def search_knowledge(domain: str, query: str) -> dict:
    """Search a domain's knowledge base for policy notes matching a free-text query."""
    if domain not in domains:
        return {"ok": False, "error": f"Unknown domain '{domain}'. Supported: {list(domains.keys())}"}
    matches = core.search_knowledge(domain, query, domains)
    return {"ok": True, "domain": domain, "query": query, "matches": matches}


def _resolve_intent(domain: str, message: str, fields: Optional[dict]):
    """Shared preamble for the action tools: domain check, privacy check, intent detection."""
    fields = fields or {}
    if domain not in domains:
        return None, {"ok": False, "error": f"Unknown domain '{domain}'. Supported: {list(domains.keys())}"}

    blocked = core.check_privacy_violation(fields)
    if blocked:
        return None, {"ok": False, "error": f"privacy_violation: field '{blocked}' cannot be collected for privacy reasons"}

    intent = core.detect_intent(message, domain, domains)
    if not intent:
        return None, {
            "ok": False,
            "error": "No matching intent detected for this message.",
            "available_intents": [i["name"] for i in domains[domain]["intents"]],
        }
    return intent, None


def _gate_on_context(domain: str, intent: dict, context: str) -> Optional[dict]:
    """
    Enforces the "resolve as much as possible conversationally before acting"
    design: refuses to let an action tool proceed until context has been
    gathered, then checks whether that context calls for human escalation.
    Returns a response dict if execution should stop here, else None.
    """
    if not context:
        return {
            "ok": False,
            "next_step": "need_context",
            "intent": intent["name"],
            "question": intent.get(
                "context_question", "Could you please describe what happened and when?"
            ),
        }

    if core.should_escalate(context, intent):
        persona = domains[domain]["persona"]
        knowledge = domains[domain]["knowledge"]
        return {
            "ok": True,
            "escalated": True,
            "intent": intent["name"],
            "message": persona.get("escalation_message", "Transferring to a human agent."),
            "reason": "Escalation keywords detected in context.",
            "contact_info": knowledge.get("contact_info", knowledge.get("emergency_note", "")),
        }

    return None


@mcp.tool()
def create_case(domain: str, message: str, context: str = "", fields: Optional[dict] = None) -> dict:
    """Detect the user's intent and create a support case (ticket) once all required fields are present."""
    fields = fields or {}
    intent, error = _resolve_intent(domain, message, fields)
    if error:
        return error

    gate = _gate_on_context(domain, intent, context)
    if gate:
        return gate

    missing = core.get_missing_fields(intent["required_fields"], fields)
    if missing:
        return {
            "ok": False,
            "next_step": "collect_fields",
            "intent": intent["name"],
            "missing_fields": missing,
        }

    result = core.execute_action("create_ticket", domain, intent, fields, context, domains)
    return {"ok": True, "intent": intent["name"], "action_result": result}


@mcp.tool()
def initiate_refund_or_reversal(domain: str, message: str, context: str = "", fields: Optional[dict] = None) -> dict:
    """Assess an autopay debit dispute and either reverse it or escalate to a human.

    For the banking domain, when the transaction is one the ledger knows,
    this runs the reversal policy engine: the claim is checked against the
    mandate registry, the debit ledger and the pre-debit notice log. A
    breach the bank's own records confirm — and that clears the value,
    frequency, account-status and dispute-window guardrails — is reversed
    automatically. Everything else opens a ticket for a human advisor with
    the evidence attached.

    Other domains, or a transaction the ledger has no record of, fall back
    to opening a ticket exactly as before.
    """
    fields = fields or {}
    intent, error = _resolve_intent(domain, message, fields)
    if error:
        return error

    if not core.intent_matches(intent["name"], core.REFUND_REVERSAL_PATTERNS):
        return {
            "ok": False,
            "error": (
                f"Intent '{intent['name']}' is not a refund/reversal/dispute action — "
                "this tool only handles those. Use create_case instead."
            ),
        }

    if not context:
        return {
            "ok": False,
            "next_step": "need_context",
            "intent": intent["name"],
            "question": intent.get(
                "context_question", "Could you please describe what happened and when?"
            ),
        }

    missing = core.get_missing_fields(intent["required_fields"], fields)
    if missing:
        return {
            "ok": False,
            "next_step": "collect_fields",
            "intent": intent["name"],
            "missing_fields": missing,
        }

    txn_id = fields.get("transaction_id")
    if txn_id and reversal_ledger.get_transaction(txn_id):
        # The policy engine decides here, not the escalate_keywords list.
        # Keyword matching on the customer's own words cannot tell a real
        # duplicate debit from someone who merely used the word "twice" —
        # and it can never confirm that a debit was genuinely unauthorised,
        # which is what has to be true before money moves on its own.
        return reversal.resolve(
            txn_id=txn_id,
            complaint_text=(message + " " + context).strip(),
            domain=domain,
            intent=intent,
            domains=domains,
            fields=fields,
        )

    # No ledger record for this transaction: keep the original behaviour so
    # nothing regresses for the other domains.
    gate = _gate_on_context(domain, intent, context)
    if gate:
        return gate

    result = core.execute_action("create_ticket", domain, intent, fields, context, domains)
    return {
        "ok": True,
        "intent": intent["name"],
        "action_result": result,
        "note": (
            f"No account record for transaction {txn_id!r}, so the reversal checks could "
            "not run. Opened a ticket for a human advisor instead."
        ),
    }


@mcp.tool()
def assess_reversal(transaction_id: str, complaint_text: str, reason_code: Optional[str] = None) -> dict:
    """Dry-run the reversal policy. Returns the decision and its evidence, changes nothing.

    Call this when you want to explain to the customer what will happen
    before it happens, or to show your reasoning. `reason_code` is optional:
    pass one if you have read the complaint and are confident of the
    category, and the engine runs that category's ledger check instead of
    guessing from keywords. It still refuses to reverse if the check fails.
    """
    try:
        return reversal.resolve(
            transaction_id, complaint_text, reason_code=reason_code, dry_run=True
        )
    except LookupError as exc:
        return {"ok": False, "error": str(exc)}
    except ValueError as exc:
        return {"ok": False, "error": f"unknown reason_code: {exc}"}


@mcp.tool()
def get_transaction(transaction_id: str) -> dict:
    """Look up one autopay debit and the mandate it ran against.

    Use this first when a customer names a transaction, so you know the
    amount, the merchant and the mandate terms before saying anything.
    """
    txn = reversal_ledger.get_transaction(transaction_id)
    if txn is None:
        return {"ok": False, "error": f"no transaction {transaction_id}"}
    mandate = reversal_ledger.get_mandate(txn.mandate_id)
    return {
        "ok": True,
        "txn_id": txn.txn_id,
        "amount": rupees(txn.amount_paise),
        "amount_paise": txn.amount_paise,
        "merchant": txn.merchant_name,
        "debited_at": txn.debited_at.isoformat(),
        "status": txn.status.value,
        "reversal_txn_id": txn.reversal_txn_id,
        "mandate": {
            "mandate_id": mandate.mandate_id,
            "umn": mandate.umn,
            "status": mandate.status.value,
            "cap": rupees(mandate.max_amount_paise),
            "frequency": mandate.frequency.value,
            "valid_until": mandate.valid_until.isoformat(),
            "revoked_at": mandate.revoked_at.isoformat() if mandate.revoked_at else None,
        },
    }


@mcp.tool()
def list_customer_debits(customer_id: str) -> dict:
    """List a customer's autopay debits, newest first.

    Use this when the customer describes a debit but does not know its
    transaction id.
    """
    txns = reversal_ledger.list_transactions_for_customer(customer_id)
    if not txns:
        return {"ok": False, "error": f"no debits on record for {customer_id}"}
    return {
        "ok": True,
        "customer_id": customer_id,
        "debits": [
            {
                "txn_id": t.txn_id,
                "merchant": t.merchant_name,
                "amount": rupees(t.amount_paise),
                "debited_at": t.debited_at.isoformat(),
                "status": t.status.value,
            }
            for t in txns
        ],
    }


@mcp.tool()
def get_reversal_audit(entity: Optional[str] = None, limit: int = 25) -> dict:
    """Read the append-only reversal audit log, newest first.

    `entity` filters to one transaction or mandate id. Every reversal,
    mandate change and escalation the agent performed is here, with the
    reason code that justified it.
    """
    return {"ok": True, "entries": reversal_ledger.audit_trail(entity, limit)}


@mcp.tool()
def pause_or_cancel_mandate(domain: str, message: str, context: str = "", fields: Optional[dict] = None) -> dict:
    """Pause or cancel a standing mandate/autopay/subscription. Refuses to run for non-cancel-type intents."""
    fields = fields or {}
    intent, error = _resolve_intent(domain, message, fields)
    if error:
        return error

    if not core.intent_matches(intent["name"], core.MANDATE_PAUSE_PATTERNS):
        return {
            "ok": False,
            "error": (
                f"Intent '{intent['name']}' is not a cancel/pause/mandate action — "
                "this tool only handles those. Use create_case instead."
            ),
        }

    gate = _gate_on_context(domain, intent, context)
    if gate:
        return gate

    missing = core.get_missing_fields(intent["required_fields"], fields)
    if missing:
        return {
            "ok": False,
            "next_step": "collect_fields",
            "intent": intent["name"],
            "missing_fields": missing,
        }

    action_name = intent.get("action", "create_notification")
    result = core.execute_action(action_name, domain, intent, fields, context, domains)
    return {"ok": True, "intent": intent["name"], "action_result": result}


@mcp.tool()
def send_notification_or_escalation(
    domain: str, message: str, context: str = "", fields: Optional[dict] = None, reason: str = ""
) -> dict:
    """Raise a notification, or escalate to a human agent if the context or an explicit reason warrants it."""
    fields = fields or {}
    intent, error = _resolve_intent(domain, message, fields)
    if error:
        return error

    if reason:
        persona = domains[domain]["persona"]
        knowledge = domains[domain]["knowledge"]
        return {
            "ok": True,
            "escalated": True,
            "intent": intent["name"],
            "message": persona.get("escalation_message", "Transferring to a human agent."),
            "reason": reason,
            "contact_info": knowledge.get("contact_info", knowledge.get("emergency_note", "")),
        }

    gate = _gate_on_context(domain, intent, context)
    if gate:
        return gate

    missing = core.get_missing_fields(intent["required_fields"], fields)
    if missing:
        return {
            "ok": False,
            "next_step": "collect_fields",
            "intent": intent["name"],
            "missing_fields": missing,
        }

    result = core.execute_action("create_notification", domain, intent, fields, context, domains)
    return {"ok": True, "escalated": False, "intent": intent["name"], "action_result": result}


if __name__ == "__main__":
    mcp.run(transport="stdio")

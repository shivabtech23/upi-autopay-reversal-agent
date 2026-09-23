"""
Core business logic for the QnA Action system — domain config loading,
intent detection, field validation, and action execution.

Framework-agnostic: no FastAPI/MCP imports here. Both the REST API
(qna_action_mcp_server.py) and the real MCP server (mcp_server.py) import
from this module so the two interfaces can never drift apart.
"""

import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
CONFIGS_DIR = os.path.join(BASE_DIR, "..", "configs")
DATA_DIR    = os.path.join(BASE_DIR, "..", "data")

TICKETS_FILE       = os.path.join(DATA_DIR, "tickets.json")
NOTIFICATIONS_FILE = os.path.join(DATA_DIR, "notifications.json")
RECORDS_FILE       = os.path.join(DATA_DIR, "records.json")
DOMAIN_CONFIG_FILE = os.path.join(CONFIGS_DIR, "domain_config.json")


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------
def load_all_configs():
    """Returns (domain_config, domains) — same shape the FastAPI startup hook built."""
    with open(DOMAIN_CONFIG_FILE) as f:
        domain_config = json.load(f)

    domains = {}
    for domain_name in domain_config["supported_domains"]:
        domain_dir = os.path.join(CONFIGS_DIR, domain_name)
        domains[domain_name] = {}
        for config_file in ["intents", "actions", "knowledge", "persona"]:
            with open(os.path.join(domain_dir, f"{config_file}.json")) as f:
                domains[domain_name][config_file] = json.load(f)

    return domain_config, domains


# ---------------------------------------------------------------------------
# JSON file I/O
# ---------------------------------------------------------------------------
def read_json(filepath: str) -> list:
    try:
        with open(filepath) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def write_json(filepath: str, data: list):
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# Privacy guardrail
# ---------------------------------------------------------------------------
BLOCKED_FIELDS = {"otp", "one_time_password", "one-time-password", "passcode", "verification_code"}


def check_privacy_violation(fields: dict) -> Optional[str]:
    for key in fields:
        if key.lower() in BLOCKED_FIELDS:
            return key
    return None


# ---------------------------------------------------------------------------
# Intent detection
# ---------------------------------------------------------------------------
def detect_intent(message: str, domain_name: str, domains: dict) -> Optional[dict]:
    message_lower = message.lower()
    for intent in domains[domain_name]["intents"]:
        for keyword in intent["keywords"]:
            if re.compile(re.escape(keyword.lower())).search(message_lower):
                return intent
    return None


# ---------------------------------------------------------------------------
# Escalation check
# ---------------------------------------------------------------------------
def should_escalate(context: str, intent: dict) -> bool:
    context_lower = context.lower()
    for keyword in intent.get("escalate_keywords", []):
        if keyword.lower() in context_lower:
            return True
    return False


# ---------------------------------------------------------------------------
# Field validation
# ---------------------------------------------------------------------------
def get_missing_fields(required: list, provided: dict) -> list:
    return [f for f in required if f not in provided or not provided[f]]


# ---------------------------------------------------------------------------
# Action execution
# ---------------------------------------------------------------------------
def execute_action(action_name: str, domain_name: str, intent: dict,
                    fields: dict, context: str, domains: dict) -> dict:
    record_id = str(uuid.uuid4())[:8].upper()
    timestamp = datetime.now(timezone.utc).isoformat()

    action_meta = next(
        (a for a in domains[domain_name]["actions"] if a["name"] == action_name), {}
    )

    result = {
        "id":        record_id,
        "timestamp": timestamp,
        "domain":    domain_name,
        "intent":    intent["name"],
        "action":    action_name,
        "priority":  intent.get("priority", "medium"),
        "context":   context,
        "fields":    fields,
        "status":    "created",
        "message":   action_meta.get("response_message", "Action completed."),
    }

    if action_name == "create_ticket":
        store = read_json(TICKETS_FILE)
        store.append(result)
        write_json(TICKETS_FILE, store)
    elif action_name == "create_notification":
        store = read_json(NOTIFICATIONS_FILE)
        store.append(result)
        write_json(NOTIFICATIONS_FILE, store)
    else:
        raise ValueError(f"Unknown action: {action_name}")

    return result


# ---------------------------------------------------------------------------
# Request logging
# ---------------------------------------------------------------------------
def log_record(request_data: dict, response_data: dict):
    records = read_json(RECORDS_FILE)
    records.append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "request":   request_data,
        "response":  response_data,
    })
    write_json(RECORDS_FILE, records)


# ---------------------------------------------------------------------------
# Knowledge lookup by key (used by the REST /run flow)
# ---------------------------------------------------------------------------
INTENT_KNOWLEDGE_MAP = {
    # banking
    "autopay_refund":     "autopay_refund_policy",
    "autopay_cancel":     "autopay_cancel_policy",
    "charge_dispute":     "charge_dispute_policy",
    "card_block":         "card_block_policy",
    # government
    "pan_update":         "pan_update_policy",
    "aadhaar_update":     "aadhaar_update_policy",
    "pan_status":         "pan_status_policy",
    "aadhaar_correction": "aadhaar_correction_policy",
    # healthcare
    "book_appointment":   "appointment_policy",
    "prescription_refill":"prescription_policy",
    # telecom
    "bill_dispute":       "bill_dispute_policy",
    "network_issue":      "outage_policy",
}


# ---------------------------------------------------------------------------
# Knowledge lookup by free-text query (new — used by the MCP search_knowledge tool)
# ---------------------------------------------------------------------------
def search_knowledge(domain_name: str, query: str, domains: dict) -> list:
    """Case-insensitive substring match of `query` against a domain's knowledge.json values."""
    knowledge = domains[domain_name]["knowledge"]
    query_lower = query.lower()
    matches = []
    for key, text in knowledge.items():
        if query_lower in key.lower() or query_lower in text.lower():
            matches.append({"key": key, "text": text})
    return matches


# ---------------------------------------------------------------------------
# Intent allow-lists for the restricted MCP tools
# (name-pattern based, mirrors the doc's "controlled interface" design)
# ---------------------------------------------------------------------------
REFUND_REVERSAL_PATTERNS = ("refund", "reversal", "dispute", "charge")
MANDATE_PAUSE_PATTERNS   = ("cancel", "pause", "mandate")


def intent_matches(intent_name: str, patterns: tuple) -> bool:
    return any(p in intent_name for p in patterns)

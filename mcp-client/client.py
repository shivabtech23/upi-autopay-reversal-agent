"""
QnA Action MCP Client
----------------------
Multi-turn conversational client for the QnA Action MCP Server.

Conversation flow:
  1. User types their problem
  2. Server asks "what happened?" (context question)
  3. User explains the situation
  4. Server either:
       a. Escalates to human agent  (if situation is severe)
       b. Asks for required fields  (if self-serviceable)
  5. User provides fields
  6. Server creates ticket / notification and returns result

Usage:
    python client.py              # interactive mode
    python client.py --help       # one-shot mode options
"""

import argparse
import json
import sys

import requests

SERVER_URL = "http://localhost:8000"


# ---------------------------------------------------------------------------
# Pretty print
# ---------------------------------------------------------------------------

def print_sep():
    print("\n" + "─" * 60 + "\n")


def print_response(resp: dict):
    next_step = resp.get("next_step", "")
    print_sep()

    if next_step == "ask_context":
        # Server wants to understand the situation first
        print(f"  Intent detected : {resp.get('detected_intent')}")
        print(f"\n  Agent           : {resp.get('question')}")

    elif next_step == "human_transfer":
        print("  ⚠  ESCALATED TO HUMAN AGENT")
        print(f"\n  Domain   : {resp.get('domain')}")
        print(f"  Intent   : {resp.get('detected_intent')}")
        for note in resp.get("notes", []):
            print(f"  Note     : {note}")

    elif next_step == "collect_fields":
        print(f"  {resp.get('question', 'Please provide the following details:')}")
        print(f"  Missing  : {', '.join(resp.get('missing_fields', []))}")

    elif next_step == "action_executed":
        result = resp.get("action_result", {})
        print(f"  ✓ Done!")
        print(f"\n  Ticket ID : {result.get('id')}")
        print(f"  Action    : {result.get('action')}")
        print(f"  Priority  : {result.get('priority')}")
        print(f"  Status    : {result.get('status')}")
        print(f"  Message   : {result.get('message')}")
        for note in resp.get("notes", []):
            print(f"\n  Policy    : {note}")

    elif next_step == "clarify_intent":
        print("  Could not understand the request.")
        for note in resp.get("notes", []):
            print(f"  {note}")

    elif next_step == "privacy_violation":
        print("  ✗ Privacy violation — request blocked.")
        for note in resp.get("notes", []):
            print(f"  {note}")

    else:
        print(json.dumps(resp, indent=2))

    print_sep()


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def api_get(path: str):
    resp = requests.get(f"{SERVER_URL}{path}")
    print(json.dumps(resp.json(), indent=2))


def fetch_domains():
    resp = requests.get(f"{SERVER_URL}/domains")
    resp.raise_for_status()
    return resp.json()


def select_domain(domains_data: dict):
    supported = domains_data["supported_domains"]
    names = list(supported.keys())

    print("\nWhich domain is this about?\n")
    for idx, name in enumerate(names, start=1):
        print(f"  {idx}. {name}  ({supported[name]['persona']})")

    choice = input("\nPick a number (or Enter for default): ").strip()
    if not choice:
        return None
    if choice.isdigit() and 1 <= int(choice) <= len(names):
        return names[int(choice) - 1]
    if choice in names:
        return choice
    print(f"  Didn't recognize '{choice}' — using default domain.")
    return None


def show_problem_hints(domains_data: dict, domain_name: str):
    if not domain_name:
        return
    supported = domains_data["supported_domains"][domain_name]
    descriptions = supported.get("intent_descriptions", {})

    print(f"\nCommon issues handled in '{domain_name}':\n")
    for name in supported["intents"]:
        print(f"  • {descriptions.get(name, name.replace('_', ' '))}")
    print()


def call_run(message: str, domain: str = None, stage: str = "new",
             context: str = "", fields: dict = None) -> dict:
    payload = {
        "message": message,
        "domain":  domain,
        "stage":   stage,
        "context": context,
        "fields":  fields or {},
    }
    resp = requests.post(f"{SERVER_URL}/run", json=payload)
    if resp.status_code != 200:
        print(f"Server error {resp.status_code}: {resp.text}")
        sys.exit(1)
    return resp.json()


# ---------------------------------------------------------------------------
# Interactive conversation loop
# ---------------------------------------------------------------------------

def interactive_mode():
    print("\nQnA Action MCP — Conversational Client")
    print("Commands: 'health' | 'domains' | 'tools' | 'quit'\n")

    try:
        domains_data = fetch_domains()
    except requests.exceptions.RequestException:
        domains_data = None
        print("  (Could not reach server to list domains — is it running?)\n")

    while True:
        # ── Step 0: pick a domain from a menu, then describe the problem ───
        command = input("Press Enter to report an issue (or type health/domains/tools/quit): ").strip()
        if command.lower() == "quit":
            print("Goodbye!")
            break
        if command.lower() in ("health", "domains", "tools"):
            api_get(f"/{command.lower()}")
            continue

        domain_input = select_domain(domains_data) if domains_data else \
            (input("Domain (Enter for default): ").strip() or None)
        show_problem_hints(domains_data, domain_input)

        user_message = input("Describe your problem, in your own words: ").strip()
        if not user_message:
            continue

        # ── Step 1: send message → server returns context question ────────
        resp = call_run(user_message, domain=domain_input, stage="new")
        print_response(resp)

        if resp["next_step"] != "ask_context":
            # Intent not detected or privacy issue — loop back
            continue

        # ── Step 2: user explains the situation ───────────────────────────
        context = input("You (describe the situation): ").strip()

        resp = call_run(
            user_message,
            domain=domain_input,
            stage="context_given",
            context=context,
        )
        print_response(resp)

        # ── Step 2a: escalate to human — done ─────────────────────────────
        if resp["next_step"] == "human_transfer":
            print("  A human agent will contact you shortly. Session ended.\n")
            continue

        # ── Step 2b: collect required fields ──────────────────────────────
        if resp["next_step"] != "collect_fields":
            continue

        fields = {}
        missing = resp.get("missing_fields", [])

        print(f"  Please provide the following: {', '.join(missing)}\n")
        for field in missing:
            # Client-side privacy guard
            if field.lower() in {"otp", "one_time_password", "verification_code", "passcode"}:
                print(f"  [BLOCKED] '{field}' cannot be collected for privacy reasons.")
                continue
            value = input(f"  {field}: ").strip()
            if value:
                fields[field] = value

        # ── Step 3: send fields → server executes action ──────────────────
        resp = call_run(
            user_message,
            domain=domain_input,
            stage="fields_given",
            context=context,
            fields=fields,
        )
        print_response(resp)


# ---------------------------------------------------------------------------
# One-shot mode (for curl-style scripted demos)
# ---------------------------------------------------------------------------

def oneshot_mode(args):
    fields = {}
    if args.fields:
        for kv in args.fields:
            if "=" in kv:
                k, v = kv.split("=", 1)
                fields[k.strip()] = v.strip()

    resp = call_run(
        args.message,
        domain=args.domain,
        stage=args.stage,
        context=args.context or "",
        fields=fields,
    )
    print(json.dumps(resp, indent=2))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="QnA Action MCP Client")
    parser.add_argument("--message", type=str, help="User message")
    parser.add_argument("--domain",  type=str, help="Domain (optional)")
    parser.add_argument("--stage",   type=str, default="new",
                        help="Stage: new | context_given | fields_given")
    parser.add_argument("--context", type=str, default="",
                        help="User's situation description (for stage=context_given)")
    parser.add_argument("--fields",  nargs="*", metavar="KEY=VALUE",
                        help="Fields as key=value pairs")
    args = parser.parse_args()

    if args.message:
        oneshot_mode(args)
    else:
        interactive_mode()

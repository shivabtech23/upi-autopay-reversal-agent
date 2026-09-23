"""
Scripted MCP client — proves the real MCP server actually works end to end,
without needing Claude Desktop or the browser-based Inspector.

Spawns mcp_server.py as a subprocess over stdio (the standard MCP transport),
lists its resources/tools, then calls each of the 5 tools with sample data
and prints the results.

Run with:
    venv-mcp/bin/python test_mcp_client.py
"""

import asyncio
import json
import os
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

SERVER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_server.py")


def hr(title: str):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def show(label: str, value):
    print(f"\n--- {label} ---")
    if hasattr(value, "content"):
        for block in value.content:
            text = getattr(block, "text", str(block))
            try:
                print(json.dumps(json.loads(text), indent=2))
            except (json.JSONDecodeError, TypeError):
                print(text)
    else:
        print(value)


async def main():
    params = StdioServerParameters(command=sys.executable, args=[SERVER_SCRIPT])

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            hr("RESOURCES")
            resources = await session.list_resources()
            templates = await session.list_resource_templates()
            print("Static resources:", [str(r.uri) for r in resources.resources])
            print("Resource templates:", [t.uriTemplate for t in templates.resourceTemplates])

            show("knowledge://banking", await session.read_resource("knowledge://banking"))
            show("intents://banking", await session.read_resource("intents://banking"))
            show("persona://healthcare", await session.read_resource("persona://healthcare"))
            show("domains://list", await session.read_resource("domains://list"))

            hr("TOOLS")
            tools = await session.list_tools()
            print("Available tools:", [t.name for t in tools.tools])

            show(
                "search_knowledge(banking, 'autopay')",
                await session.call_tool("search_knowledge", {"domain": "banking", "query": "autopay"}),
            )

            show(
                "create_case — missing fields",
                await session.call_tool(
                    "create_case",
                    {
                        "domain": "healthcare",
                        "message": "I need to book an appointment with a cardiologist",
                        "context": "routine checkup",
                        "fields": {},
                    },
                ),
            )

            show(
                "create_case — no context (should ask before acting)",
                await session.call_tool(
                    "create_case",
                    {
                        "domain": "healthcare",
                        "message": "I need to book an appointment with a cardiologist",
                        "context": "",
                        "fields": {
                            "patient_name": "Test Patient",
                            "doctor_specialization": "Cardiologist",
                            "preferred_date": "2026-08-01",
                        },
                    },
                ),
            )

            show(
                "create_case — complete",
                await session.call_tool(
                    "create_case",
                    {
                        "domain": "healthcare",
                        "message": "I need to book an appointment with a cardiologist",
                        "context": "routine checkup",
                        "fields": {
                            "patient_name": "Test Patient",
                            "doctor_specialization": "Cardiologist",
                            "preferred_date": "2026-08-01",
                        },
                    },
                ),
            )

            show(
                "initiate_refund_or_reversal — happy path",
                await session.call_tool(
                    "initiate_refund_or_reversal",
                    {
                        "domain": "banking",
                        "message": "my autopay subscription got auto debited and I want a refund",
                        "context": "It got debited yesterday, I recognize the merchant",
                        "fields": {"user_id": "U-MCP", "transaction_id": "TXN-MCP", "amount": "150"},
                    },
                ),
            )

            show(
                "initiate_refund_or_reversal — wrong intent (should refuse)",
                await session.call_tool(
                    "initiate_refund_or_reversal",
                    {
                        "domain": "banking",
                        "message": "block my card, it was lost",
                        "context": "left it in a taxi",
                        "fields": {},
                    },
                ),
            )

            show(
                "pause_or_cancel_mandate — happy path",
                await session.call_tool(
                    "pause_or_cancel_mandate",
                    {
                        "domain": "banking",
                        # NOTE: banking's autopay_refund intent has a bare "autopay" keyword,
                        # so any message containing that word matches it first (keyword lookup
                        # is first-match-in-list-order, no specificity ranking). Avoid the word
                        # "autopay" here so this actually reaches the autopay_cancel intent.
                        "message": "please cancel subscription",
                        "context": "don't need it anymore",
                        "fields": {"user_id": "U-MCP", "subscription_id": "SUB-MCP"},
                    },
                ),
            )

            show(
                "send_notification_or_escalation — escalation path",
                await session.call_tool(
                    "send_notification_or_escalation",
                    {
                        "domain": "banking",
                        "message": "block card lost card",
                        "context": "I think this is fraud, someone used my card",
                        "fields": {},
                    },
                ),
            )

            show(
                "send_notification_or_escalation — privacy guardrail",
                await session.call_tool(
                    "send_notification_or_escalation",
                    {
                        "domain": "banking",
                        "message": "cancel autopay",
                        "context": "",
                        "fields": {"otp": "123456"},
                    },
                ),
            )

            hr("DONE")


if __name__ == "__main__":
    asyncio.run(main())

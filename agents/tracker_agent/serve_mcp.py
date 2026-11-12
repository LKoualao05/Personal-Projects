"""Expose the ADK agent itself as an MCP server (one ``internship_tracker`` tool).

MCP hosts such as Claude Desktop or Claude Code can then delegate whole
questions ("what's my interview pipeline this week?") to the Gemini agent:

    python -m agents.tracker_agent.serve_mcp            # stdio
    python -m agents.tracker_agent.serve_mcp --http     # streamable HTTP at /mcp

This variant is read-only: an MCP host cannot answer ADK's confirmation
prompts, so the sheet-writing tools are filtered out.
"""

from __future__ import annotations

import argparse

from google.adk.tools.mcp_tool import to_mcp_server

from .agent import build_agent


def create_agent_mcp_server():
    agent = build_agent(allow_writes=False, confirm_writes=False)
    return to_mcp_server(
        agent,
        instructions="Ask about the user's internship applications: statuses, stats, recruiter emails, deadlines.",
    )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--http", action="store_true")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8766)
    args = p.parse_args()
    server = create_agent_mcp_server()
    if args.http:
        server.run("streamable-http", host=args.host, port=args.port)
    else:
        server.run("stdio")


if __name__ == "__main__":
    main()

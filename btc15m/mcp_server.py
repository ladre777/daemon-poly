#!/usr/bin/env python3
"""MCP server: real-time reads of the BTC 15m agent's data and activity.

Stdio transport. Tools are read-only — no orders, no state mutation.

Connect from Claude Code / Claude Desktop by adding to the MCP config:

  {
    "mcpServers": {
      "btc-agent": {
        "command": "/home/hatch/workspace/polymarket-env/bin/python",
        "args": ["/home/hatch/workspace/polymarket/btc15m/mcp_server.py"]
      }
    }
  }
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.mcpserver import MCPServer  # noqa: E402
import readers  # noqa: E402

mcp = MCPServer("btc-agent")


@mcp.tool()
def status() -> dict:
    """Current engine state: paper/live PnL, open positions, kill switches."""
    return readers.get_status()


@mcp.tool()
def activity(n: int = 10) -> list:
    """Latest agent activity: decisions, opens, settles, errors. n=1..100."""
    return readers.get_activity(n)


@mcp.tool()
def positions() -> list:
    """Currently open positions (paper + live)."""
    return readers.get_positions()


@mcp.tool()
def pnl() -> dict:
    """Settled P&L by sleeve, paper vs live."""
    return readers.get_pnl()


@mcp.tool()
def gate() -> dict:
    """Paper-to-live 'poppin' gate: does the sample clear it yet?"""
    return readers.get_gate()


@mcp.tool()
def config() -> dict:
    """Current guardrails: risk, caps, kill switches, gate criteria."""
    return readers.get_config()


if __name__ == "__main__":
    mcp.run()

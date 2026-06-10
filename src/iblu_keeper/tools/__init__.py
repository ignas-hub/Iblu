"""MCP tool implementations: Google Chat, Gmail, Calendar, and context stubs.

Each module exposes plain Python functions. `server.py` wraps them as FastMCP
tools. Keeping the logic outside the FastMCP decorators makes the tools unit
testable and lets backends (e.g. the Chat backend) be swapped freely.
"""

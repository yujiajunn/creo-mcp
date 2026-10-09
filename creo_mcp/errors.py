"""Domain errors returned as actionable MCP tool-execution errors."""

from __future__ import annotations


class CreoMcpError(RuntimeError):
    """A recoverable error while communicating with the local Creo session."""

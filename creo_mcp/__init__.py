"""Standards-based MCP server for the official PTC Creo VB API."""

from .version import SERVER_VERSION as __version__

__all__ = ["__version__", "create_server"]


def __getattr__(name: str):
    """Load the server lazily so ``python -m creo_mcp.broker`` stays clean."""
    if name == "create_server":
        from .server import create_server

        return create_server
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

"""Standards-based MCP server for the official PTC Creo VB API."""

from .server import create_server
from .version import SERVER_VERSION as __version__

__all__ = ["__version__", "create_server"]

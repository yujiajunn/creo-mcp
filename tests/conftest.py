"""Keep the whole test session out of the real Creo MCP runtime directory.

The broker writes ``broker.json`` and ``logs/broker.log`` under
``%LOCALAPPDATA%\\CreoMCP``. Test brokers used to append there as well, so a
plain ``pytest`` run polluted the operator's live broker log with
``creo-mcp-test-*`` start/stop records. Redirecting the runtime root once for the
session keeps production state untouched while tests exercise the real code.
"""

import os
import tempfile

_RUNTIME_DIR = tempfile.mkdtemp(prefix="creo-mcp-test-runtime-")
os.environ["CREO_MCP_RUNTIME_DIR"] = _RUNTIME_DIR

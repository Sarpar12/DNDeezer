"""DroppedNeedle plugin entrypoint.

The host loads this module with importlib from its file location and does NOT
add the plugin directory to sys.path, so make the bundled ``dndeezer`` package
importable before touching it.
"""

import sys
from pathlib import Path

_PLUGIN_DIR = str(Path(__file__).resolve().parent)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

# Deferred import: only valid after the sys.path bootstrap above.
from dndeezer.indexer import DeezerIndexer


class DNDeezer(DeezerIndexer):
    """Plugin entry point. Indexer capability only until the
    DownloadClientProtocol adapter lands."""

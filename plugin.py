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

# Deferred imports: only valid after the sys.path bootstrap above.
from dndeezer.download_client import DeezerDownloadClient
from dndeezer.indexer import DeezerIndexer


class DNDeezer(DeezerIndexer, DeezerDownloadClient):
    """One complete source: Deezer search plus Deezer downloads (folder
    mode). Both parents share the same host ``context``."""

    def __init__(self, context):
        DeezerIndexer.__init__(self, context)
        DeezerDownloadClient.__init__(self, context)

    def is_configured(self) -> bool:
        # "One plugin, one complete source": search needs the ARL, downloads
        # additionally need a staging directory.
        return (
            DeezerIndexer.is_configured(self)
            and DeezerDownloadClient.is_configured(self)
        )

    async def health_check(self):
        # DeezerDownloadClient.health_check covers settings, authentication,
        # and directory writability in a single auth round-trip.
        return await DeezerDownloadClient.health_check(self)

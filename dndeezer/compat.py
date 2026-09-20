"""Plugin-side compatibility with the host's persisted per-file candidates."""

from infrastructure.plugins.protocols import DownloadFileRef


class SearchFileRef(DownloadFileRef):
    """A file ref that also decodes as a host DownloadSearchResult.

    Some hosts place the plugin's ref directly in ScoredCandidate.files,
    whose persisted schema expects DownloadSearchResult. Keep these additional
    fields on the serialised ref until that host path retains its search shim.
    Normal DownloadFileRef readers ignore the additional fields.
    """

    parent_directory: str = ""
    extension: str = ""

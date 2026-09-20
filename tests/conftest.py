"""sys.modules stubs for the DroppedNeedle host packages.

``dndeezer.indexer`` and ``dndeezer.download_client`` import boundary types
from the host application at runtime (see PLUGINS.md). These dataclasses
mirror those shapes so the plugin code is importable - and testable -
outside the host. Installed before any test module imports them.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field


@dataclass
class ServiceStatus:
    status: str
    version: str | None = None
    message: str | None = None


@dataclass
class DownloadFileRef:
    username: str = ""
    filename: str = ""
    size: int = 0


@dataclass
class EnqueueRequest:
    task_id: str = ""
    source: str = ""
    files: list = field(default_factory=list)
    nzb_url: str | None = None
    job_name: str | None = None
    category: str | None = None
    priority: int | None = None
    post_processing: int | None = None
    payload: str = ""


@dataclass
class TaskHandle:
    source: str = ""
    username: str = ""
    filenames: list = field(default_factory=list)
    job_name: str = ""
    nzo_id: str = ""
    plugin_token: str = ""


@dataclass
class DownloadTaskStatus:
    task_id: str = ""
    status: str = ""
    files_total: int = 0
    files_completed: int = 0
    files_failed: int = 0
    bytes_total: int = 0
    bytes_downloaded: int = 0
    progress_percent: float = 0.0
    error: str | None = None
    succeeded_filenames: list = field(default_factory=list)
    has_active_transfer: bool = False
    matched_transfers: int = 0
    queue_position_start: int | None = None
    queue_position_end: int | None = None


@dataclass
class DownloadMaterialization:
    state: str = ""
    nzo_id: str = ""
    remote_storage: str = ""
    mount_root: str = ""
    workspace_path: str = ""
    file_paths: list = field(default_factory=list)
    mount_healthy: bool = False


@dataclass
class MountDiagnosis:
    supported: bool = False
    completed_downloads: int = 0
    mount_has_files: bool = True
    resolvable_downloads: int = 0
    sampled_downloads: int = 0
    client_downloads_dir: str | None = None


@dataclass
class PluginSearchResult:
    title: str = ""
    size_bytes: int = 0
    score: float = 0.0
    quality_tier: str = ""
    files: list = field(default_factory=list)
    payload: str = ""


@dataclass
class IndexerResult:
    source: str = ""
    soulseek: object | None = None
    usenet: object | None = None
    plugin: object | None = None


def _install(name: str, **attrs: object) -> None:
    if name in sys.modules:
        return
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module


_install("models")
_install("models.common", ServiceStatus=ServiceStatus)

_install("repositories")
_install("repositories.protocols")
_install(
    "repositories.protocols.download_client",
    DownloadFileRef=DownloadFileRef,
    EnqueueRequest=EnqueueRequest,
    TaskHandle=TaskHandle,
    DownloadTaskStatus=DownloadTaskStatus,
    DownloadMaterialization=DownloadMaterialization,
    MountDiagnosis=MountDiagnosis,
)

_install("infrastructure")
_install("infrastructure.plugins")
_install(
    "infrastructure.plugins.protocols",
    IndexerResult=IndexerResult,
    PluginSearchResult=PluginSearchResult,
    DownloadFileRef=DownloadFileRef,
    EnqueueRequest=EnqueueRequest,
    TaskHandle=TaskHandle,
    DownloadTaskStatus=DownloadTaskStatus,
    DownloadMaterialization=DownloadMaterialization,
    MountDiagnosis=MountDiagnosis,
    ServiceStatus=ServiceStatus,
)

# Wire attribute access (import infrastructure.plugins.protocols style).
sys.modules["repositories"].protocols = sys.modules["repositories.protocols"]
sys.modules["repositories.protocols"].download_client = sys.modules[
    "repositories.protocols.download_client"
]
sys.modules["infrastructure"].plugins = sys.modules["infrastructure.plugins"]
sys.modules["infrastructure.plugins"].protocols = sys.modules[
    "infrastructure.plugins.protocols"
]
sys.modules["models"].common = sys.modules["models.common"]

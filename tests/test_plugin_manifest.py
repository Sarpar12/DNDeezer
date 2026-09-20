"""Manifest regression tests mirroring DroppedNeedle's documented v1 rules
(infrastructure/plugins/manifest.py) without importing the host."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

V1_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
PLUGIN_TARGET_RE = re.compile(r"^plugin:[a-z0-9][a-z0-9-]{0,31}$")
V1_CAPABILITIES = {
    "scrobbler",
    "purchase_links",
    "download_client",
    "indexer",
    "subscriber",
    "publisher",
    "metadata_provider",
    "scheduler",
    "streaming_source",
}


@pytest.fixture(scope="module")
def manifest() -> dict:
    return tomllib.loads((ROOT / "plugin.toml").read_text(encoding="utf-8"))


def test_plugin_name_matches_v1_regex(manifest):
    assert V1_NAME_RE.match(manifest["plugin"]["name"])


def test_api_version_supported(manifest):
    assert manifest["plugin"]["api_version"] == 1


def test_entrypoint_resolves(manifest):
    entrypoint = manifest["plugin"]["entrypoint"]
    module_name, sep, class_name = entrypoint.partition(":")
    assert sep, "entrypoint must be '<module>:<ClassName>'"

    source = (ROOT / f"{module_name}.py").read_text(encoding="utf-8")
    assert f"class {class_name}" in source


def test_capabilities_known_and_capability_tables_consistent(manifest):
    capabilities = manifest["plugin"]["capabilities"]
    assert capabilities
    assert set(capabilities) <= V1_CAPABILITIES
    assert set(capabilities) == {"download_client", "indexer"}

    for table in manifest.get("capability", []):
        assert table["id"] in capabilities


def test_indexer_only_declares_matching_target_source(manifest):
    plugin = manifest["plugin"]
    capabilities = set(plugin["capabilities"])

    if "indexer" in capabilities and "download_client" not in capabilities:
        targets = [
            table.get("target_source", "")
            for table in manifest.get("capability", [])
            if table["id"] == "indexer"
        ]
        assert targets and all(PLUGIN_TARGET_RE.match(t) for t in targets)
        assert f"plugin:{plugin['name']}" in targets


def test_settings_shape_and_arl_declared(manifest):
    allowed = {"key", "label", "help", "secret"}
    settings = manifest.get("settings", [])

    for entry in settings:
        assert set(entry) <= allowed

    arl = next((s for s in settings if s["key"] == "arl"), None)
    assert arl is not None, "the indexer reads settings.get('arl')"
    assert arl.get("secret") is True

    downloads_dir = next(
        (s for s in settings if s["key"] == "downloads_dir"), None
    )
    assert downloads_dir is not None, (
        "the download client stages files via settings.get('downloads_dir')"
    )


def test_plugin_py_bootstraps_sys_path_before_package_import():
    source = (ROOT / "plugin.py").read_text(encoding="utf-8")

    assert "sys.path" in source, (
        "the host loads plugin.py via importlib without adding the plugin "
        "dir to sys.path; the dndeezer package would not be importable"
    )
    assert source.index("sys.path.insert") < source.index("from dndeezer")


def test_indexer_source_matches_manifest_name(manifest):
    source = (ROOT / "dndeezer" / "indexer.py").read_text(encoding="utf-8")
    assert re.search(
        rf'^SOURCE = "plugin:{re.escape(manifest["plugin"]["name"])}"$',
        source,
        re.MULTILINE,
    )

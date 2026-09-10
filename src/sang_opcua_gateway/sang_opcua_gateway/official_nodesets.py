"""Pinned OPC Foundation NodeSet dependency loading and verification."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
from typing import Any, Mapping
import xml.etree.ElementTree as ET

from asyncua import Server
import yaml


NODESET_XML_NS = "http://opcfoundation.org/UA/2011/03/UANodeSet.xsd"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
MODEL_NAMES = frozenset({"core", "di", "ia", "robotics"})


class OfficialNodeSetError(ValueError):
    """Raised when pinned official model evidence is incomplete or changed."""


@dataclass(frozen=True)
class OfficialModelPin:
    name: str
    uri: str
    version: str
    publication_date: str
    source_path: str
    path: Path
    configured_path: str
    sha256: str
    runtime_import: bool
    required_model_uris: tuple[str, ...]


@dataclass(frozen=True)
class OfficialNodeSetBundle:
    repository: str
    release_tag: str
    commit: str
    import_order: tuple[str, ...]
    models: Mapping[str, OfficialModelPin]

    def verify_files(self) -> None:
        for model in self.models.values():
            digest = hashlib.sha256(model.path.read_bytes()).hexdigest()
            if digest != model.sha256:
                raise OfficialNodeSetError(
                    f"official NodeSet digest changed for {model.name}: {digest}"
                )


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise OfficialNodeSetError(f"{label} must be a mapping")
    return value


def _load_yaml(path: Path) -> Mapping[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise OfficialNodeSetError(f"failed to load {path}: {exc}") from exc
    return _mapping(value, str(path))


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OfficialNodeSetError(f"{label} must be a non-empty string")
    return value.strip()


def _resolve_path(lock_path: Path, configured_path: str) -> Path:
    candidate = Path(configured_path)
    if candidate.is_absolute():
        return candidate.resolve()
    candidates = (
        Path.cwd() / candidate,
        lock_path.parents[2] / candidate,
    )
    for resolved in candidates:
        if resolved.is_file():
            return resolved.resolve()
    raise OfficialNodeSetError(f"official NodeSet file is missing: {configured_path}")


def _read_model_metadata(path: Path) -> tuple[str, str, str, tuple[str, ...]]:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise OfficialNodeSetError(f"invalid NodeSet XML {path}: {exc}") from exc
    namespace = {"u": NODESET_XML_NS}
    model = root.find("./u:Models/u:Model", namespace)
    if model is None:
        raise OfficialNodeSetError(f"NodeSet has no Model metadata: {path}")
    required = tuple(
        _text(entry.get("ModelUri"), "RequiredModel ModelUri")
        for entry in model.findall("./u:RequiredModel", namespace)
    )
    return (
        _text(model.get("ModelUri"), "ModelUri"),
        _text(model.get("Version"), "Model Version"),
        _text(model.get("PublicationDate"), "Model PublicationDate"),
        required,
    )


def _verify_catalog(
    bundle_models: Mapping[str, OfficialModelPin], catalog_path: Path
) -> None:
    catalog = _load_yaml(catalog_path)
    definitions = _mapping(catalog.get("official_models"), "official_models")
    if set(definitions) != MODEL_NAMES:
        raise OfficialNodeSetError("profile catalog official model set is incomplete")
    for name, pin in bundle_models.items():
        definition = _mapping(definitions[name], f"catalog model {name}")
        observed = {
            "uri": definition.get("uri"),
            "version": definition.get("version"),
            "path": definition.get("path"),
            "sha256": definition.get("sha256"),
        }
        expected = {
            "uri": pin.uri,
            "version": pin.version,
            "path": pin.configured_path,
            "sha256": pin.sha256,
        }
        if observed != expected:
            raise OfficialNodeSetError(
                f"profile catalog and NodeSet lock disagree for {name}"
            )


def load_official_nodeset_bundle(
    lock_path: str | Path,
    *,
    catalog_path: str | Path | None = None,
) -> OfficialNodeSetBundle:
    lock_path = Path(lock_path).resolve()
    raw = _load_yaml(lock_path)
    if raw.get("schema_version") != "1.0.0":
        raise OfficialNodeSetError("unsupported NodeSet lock schema_version")
    commit = _text(raw.get("commit"), "commit")
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise OfficialNodeSetError("NodeSet repository commit must be a full SHA-1")
    import_order_raw = raw.get("runtime_import_order")
    if not isinstance(import_order_raw, list) or any(
        not isinstance(item, str) for item in import_order_raw
    ):
        raise OfficialNodeSetError("runtime_import_order must be a string list")
    import_order = tuple(import_order_raw)
    if import_order != ("di", "ia", "robotics"):
        raise OfficialNodeSetError("official runtime import order must be DI, IA, Robotics")

    definitions = _mapping(raw.get("models"), "models")
    if set(definitions) != MODEL_NAMES:
        raise OfficialNodeSetError("NodeSet lock model set is incomplete")
    models: dict[str, OfficialModelPin] = {}
    for name in ("core", *import_order):
        definition = _mapping(definitions[name], f"model {name}")
        configured_path = _text(definition.get("path"), f"{name}.path")
        path = _resolve_path(lock_path, configured_path)
        digest = _text(definition.get("sha256"), f"{name}.sha256")
        if SHA256.fullmatch(digest) is None:
            raise OfficialNodeSetError(f"{name}.sha256 is invalid")
        uri, version, publication_date, required_uris = _read_model_metadata(path)
        expected_metadata = (
            _text(definition.get("uri"), f"{name}.uri"),
            _text(definition.get("version"), f"{name}.version"),
            _text(definition.get("publication_date"), f"{name}.publication_date"),
        )
        if (uri, version, publication_date) != expected_metadata:
            raise OfficialNodeSetError(f"NodeSet metadata changed for {name}")
        runtime_import = definition.get("runtime_import")
        if not isinstance(runtime_import, bool):
            raise OfficialNodeSetError(f"{name}.runtime_import must be Boolean")
        models[name] = OfficialModelPin(
            name=name,
            uri=uri,
            version=version,
            publication_date=publication_date,
            source_path=_text(definition.get("source_path"), f"{name}.source_path"),
            path=path,
            configured_path=configured_path,
            sha256=digest,
            runtime_import=runtime_import,
            required_model_uris=required_uris,
        )

    if models["core"].runtime_import or any(
        not models[name].runtime_import for name in import_order
    ):
        raise OfficialNodeSetError("Core must be retained-only and all companions imported")
    available_uris = {models["core"].uri}
    for name in import_order:
        missing = set(models[name].required_model_uris) - available_uris
        if missing:
            raise OfficialNodeSetError(
                f"{name} has unavailable or out-of-order dependencies: "
                + ", ".join(sorted(missing))
            )
        available_uris.add(models[name].uri)

    bundle = OfficialNodeSetBundle(
        repository=_text(raw.get("repository"), "repository"),
        release_tag=_text(raw.get("release_tag"), "release_tag"),
        commit=commit,
        import_order=import_order,
        models=models,
    )
    bundle.verify_files()
    if catalog_path is not None:
        _verify_catalog(models, Path(catalog_path).resolve())
    return bundle


async def import_official_nodesets(
    server: Server, bundle: OfficialNodeSetBundle
) -> Mapping[str, int]:
    """Import official companion models without replacing asyncua's Core model."""
    bundle.verify_files()
    namespaces: dict[str, int] = {}
    for name in bundle.import_order:
        model = bundle.models[name]
        await server.import_xml(str(model.path), strict_mode=True)
        namespaces[name] = await server.get_namespace_index(model.uri)
    return namespaces

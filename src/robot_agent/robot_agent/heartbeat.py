"""Heartbeat construction and loopback-only transport for Robot Agent."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import ssl
from typing import Any
from uuid import UUID
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from robot_agent import __version__
from robot_agent.recording_inventory import disabled_inventory


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SUPPORTED_HEARTBEAT_SCHEMA_VERSIONS = frozenset({1, 2, 3})


def require_loopback_http(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in LOOPBACK_HOSTS:
        raise ValueError(
            "Control Plane must use loopback HTTP in this release; remote transport "
            "requires per-Agent authentication and TLS"
        )


@dataclass(frozen=True)
class AgentTLSConfig:
    ca_bundle: Path
    client_certificate_chain: Path
    client_private_key: Path

    def context(self) -> ssl.SSLContext:
        context = ssl.create_default_context(
            purpose=ssl.Purpose.SERVER_AUTH,
            cafile=str(self.ca_bundle),
        )
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = True
        context.load_cert_chain(
            certfile=str(self.client_certificate_chain),
            keyfile=str(self.client_private_key),
        )
        context.verify_flags |= (
            ssl.VERIFY_CRL_CHECK_LEAF | ssl.VERIFY_X509_PARTIAL_CHAIN
        )
        return context


def require_control_plane_transport(
    url: str,
    tls: AgentTLSConfig | None,
) -> None:
    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Control Plane URL contains an invalid port") from exc
    if parsed.hostname is None or port is None or not 1 <= port <= 65535:
        raise ValueError("Control Plane URL must include a host and valid port")
    if parsed.scheme == "http" and parsed.hostname in LOOPBACK_HOSTS and tls is None:
        return
    if parsed.scheme == "https" and tls is not None:
        return
    raise ValueError(
        "remote Control Plane transport requires HTTPS with an Agent client certificate"
    )


def validate_agent_identity(agent_id: str, agent_name: str) -> None:
    if not SAFE_ID.fullmatch(agent_id):
        raise ValueError("agent_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")
    if not agent_name.strip() or len(agent_name.strip()) > 128:
        raise ValueError("agent_name must contain 1 to 128 characters")


class SystemMetrics:
    def __init__(self, storage_path: str | Path) -> None:
        self.storage_path = Path(storage_path)
        self._previous_cpu: tuple[int, int] | None = None

    @staticmethod
    def _cpu_times() -> tuple[int, int]:
        fields = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()
        values = [int(value) for value in fields[1:]]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return sum(values), idle

    def _cpu_pct(self) -> float:
        current = self._cpu_times()
        previous = self._previous_cpu
        self._previous_cpu = current
        if previous is None:
            return 0.0
        total_delta = current[0] - previous[0]
        idle_delta = current[1] - previous[1]
        if total_delta <= 0:
            return 0.0
        return 100.0 * (1.0 - idle_delta / total_delta)

    @staticmethod
    def _ram_free_gb() -> float:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / (1024**2)
        raise RuntimeError("/proc/meminfo does not contain MemAvailable")

    def _disk_free_gb(self) -> float:
        path = self.storage_path
        while not path.exists() and path != path.parent:
            path = path.parent
        return shutil.disk_usage(path).free / (1024**3)

    def sample(self) -> dict[str, float | None]:
        values = {}
        for key, sampler in (
            ("cpu_pct", self._cpu_pct),
            ("ram_free_gb", self._ram_free_gb),
            ("disk_free_gb", self._disk_free_gb),
        ):
            try:
                values[key] = round(float(sampler()), 3)
            except (OSError, RuntimeError, ValueError):
                values[key] = None
        return values


def build_heartbeat(
    *,
    agent_id: str,
    agent_name: str,
    robots: list[dict[str, Any]],
    storage: dict[str, float | None],
    recordings: dict[str, Any] | None = None,
    sync: dict[str, Any] | None = None,
    sent_at: datetime | None = None,
    boot_id: str | None = None,
    sequence: int | None = None,
    schema_version: int | None = None,
) -> dict[str, Any]:
    validate_agent_identity(agent_id, agent_name)
    timestamp = sent_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("sent_at must include a timezone")
    if (boot_id is None) != (sequence is None):
        raise ValueError("boot_id and sequence must be supplied together")
    effective_schema_version = (
        1 if boot_id is None else 2
    ) if schema_version is None else schema_version
    if (
        not isinstance(effective_schema_version, int)
        or isinstance(effective_schema_version, bool)
        or effective_schema_version not in SUPPORTED_HEARTBEAT_SCHEMA_VERSIONS
    ):
        raise ValueError("schema_version must be one of 1, 2 or 3")
    if effective_schema_version == 1 and boot_id is not None:
        raise ValueError("heartbeat v1 must not contain boot_id or sequence")
    if effective_schema_version in {2, 3} and boot_id is None:
        raise ValueError(
            f"heartbeat v{effective_schema_version} requires boot_id and sequence"
        )
    runtime_kinds = [robot.get("runtime_kind") for robot in robots]
    if effective_schema_version in {1, 2} and any(
        "runtime_kind" in robot for robot in robots
    ):
        raise ValueError(
            f"heartbeat v{effective_schema_version} must not contain runtime_kind"
        )
    if effective_schema_version == 3 and any(
        value not in {"physical", "simulation"} for value in runtime_kinds
    ):
        raise ValueError(
            "heartbeat v3 requires physical or simulation runtime_kind for every robot"
        )
    identity = {}
    if boot_id is not None:
        canonical_boot_id = str(UUID(boot_id))
        if canonical_boot_id != boot_id:
            raise ValueError("boot_id must use canonical UUID form")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence <= 0:
            raise ValueError("sequence must be a positive integer")
        identity = {"boot_id": boot_id, "sequence": sequence}
    return {
        "schema_version": effective_schema_version,
        "agent_id": agent_id,
        "agent_name": agent_name.strip(),
        "sw_version": __version__,
        "sent_at": timestamp.isoformat(),
        **identity,
        "storage": storage,
        "datasets": {"count": 0, "total_episodes": 0, "total_size_mb": 0.0},
        "recordings": disabled_inventory() if recordings is None else recordings,
        "sync": (
            {"supported": False, "state": "unsupported", "pending": None}
            if sync is None
            else sync
        ),
        "robots": robots,
    }


def post_heartbeat(
    control_plane_url: str,
    payload: dict[str, Any],
    *,
    timeout_s: float,
    tls: AgentTLSConfig | None = None,
) -> dict[str, Any]:
    require_control_plane_transport(control_plane_url, tls)
    endpoint = control_plane_url.rstrip("/") + "/api/v1/agents/heartbeat"
    request = Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(
        request,
        timeout=timeout_s,
        context=None if tls is None else tls.context(),
    ) as response:
        if response.status != 202:
            raise RuntimeError(f"heartbeat rejected with HTTP {response.status}")
        return json.loads(response.read())

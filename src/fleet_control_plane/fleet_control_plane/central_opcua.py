"""Read-only OPC UA projection of the Central operations catalog."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import signal
from typing import Any
from urllib.parse import quote, urlsplit

from asyncua import Server, ua

from fleet_control_plane.recording_storage import RecordingCatalog
from fleet_control_plane.status import FleetTTLs, present_agent
from fleet_control_plane.store import FleetStore
from sang_security.opcua import OpcUaServerSecurity, create_secure_server
from sang_security.refs import SecretResolver


NAMESPACE_URI = "urn:sang:fleet:central:operations:1"
APPLICATION_URI = "urn:sang:fleet:central:opcua"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _node_key(value: str) -> str:
    return quote(value, safe="A-Za-z0-9_.-")


def collect_projection(
    store: FleetStore,
    catalog: RecordingCatalog,
    *,
    now: datetime,
    ttls: FleetTTLs,
) -> dict[str, Any]:
    agents = [
        present_agent(snapshot, now=now, ttls=ttls)
        for snapshot in store.list_snapshots()
    ]
    assets = store.list_assets()
    runs = store.list_task_runs()
    sessions = catalog.list_sessions(limit=500)
    return {
        "generated_at": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "agents": agents,
        "assets": assets,
        "task_runs": runs,
        "recording_sessions": sessions,
    }


class CentralAddressSpace:
    """Dynamic deterministic nodes with server-side-only value updates."""

    def __init__(self, server: Server) -> None:
        self.server = server
        self.namespace_index = 0
        self.root = None
        self.groups: dict[str, Any] = {}
        self.entities: dict[tuple[str, str], Any] = {}
        self.variables: dict[tuple[str, str, str], Any] = {}

    def node_id(self, value: str) -> ua.NodeId:
        return ua.NodeId(value, self.namespace_index)

    async def _add_object(self, parent, identifier: str, browse_name: str):
        return await parent.add_object(
            self.node_id(identifier),
            ua.QualifiedName(browse_name, self.namespace_index),
        )

    async def _add_variable(
        self,
        parent,
        identifier: str,
        browse_name: str,
        value: Any,
        variant_type: ua.VariantType,
    ):
        node = await parent.add_variable(
            self.node_id(identifier),
            ua.QualifiedName(browse_name, self.namespace_index),
            value,
            variant_type,
        )
        await node.set_read_only()
        return node

    @staticmethod
    async def _write_value(
        node,
        value: Any,
        variant_type: ua.VariantType,
        status_code: ua.StatusCode,
    ) -> None:
        await node.write_attribute(
            ua.AttributeIds.Value,
            ua.DataValue(
                Value=ua.Variant(value, variant_type),
                StatusCode=status_code,
                ServerTimestamp=datetime.now(timezone.utc),
            ),
        )

    async def build(self) -> None:
        self.namespace_index = await self.server.register_namespace(NAMESPACE_URI)
        self.root = await self._add_object(
            self.server.nodes.objects,
            "central/fleet",
            "SANG Fleet Operations",
        )
        for key, browse_name in (
            ("summary", "Summary"),
            ("agents", "Agents"),
            ("robots", "Observed Robots"),
            ("assets", "Assets"),
            ("task_runs", "Task Runs"),
            ("recordings", "Recording Sessions"),
        ):
            self.groups[key] = await self._add_object(
                self.root,
                f"central/fleet/{key}",
                browse_name,
            )

    async def _project_entity(
        self,
        group: str,
        entity_id: str,
        browse_name: str,
        fields: dict[
            str,
            tuple[str, Any, ua.VariantType]
            | tuple[str, Any, ua.VariantType, ua.StatusCode],
        ],
        desired_variables: set[tuple[str, str, str]],
    ) -> None:
        identity = (group, entity_id)
        encoded_id = _node_key(entity_id)
        entity = self.entities.get(identity)
        if entity is None:
            entity = await self._add_object(
                self.groups[group],
                f"central/fleet/{group}/{encoded_id}",
                browse_name,
            )
            self.entities[identity] = entity
        for field, field_spec in fields.items():
            field_browse_name, value, variant_type = field_spec[:3]
            status_code = (
                field_spec[3]
                if len(field_spec) == 4
                else ua.StatusCode(ua.StatusCodes.Good)
            )
            variable_key = (group, entity_id, field)
            desired_variables.add(variable_key)
            variable = self.variables.get(variable_key)
            if variable is None:
                variable = await self._add_variable(
                    entity,
                    f"central/fleet/{group}/{encoded_id}/field/{field}",
                    field_browse_name,
                    value,
                    variant_type,
                )
                self.variables[variable_key] = variable
            await self._write_value(variable, value, variant_type, status_code)

    async def _reconcile(
        self,
        desired_entities: set[tuple[str, str]],
        desired_variables: set[tuple[str, str, str]],
    ) -> None:
        removed_entities = set(self.entities) - desired_entities
        for identity in sorted(removed_entities):
            node = self.entities.pop(identity)
            await node.delete(recursive=True)

        for key in list(self.variables):
            identity = (key[0], key[1])
            if identity in removed_entities:
                self.variables.pop(key)
                continue
            if key not in desired_variables:
                node = self.variables.pop(key)
                await node.delete()

    async def project(self, snapshot: dict[str, Any]) -> None:
        agents = snapshot["agents"]
        assets = snapshot["assets"]
        runs = snapshot["task_runs"]
        sessions = snapshot["recording_sessions"]
        desired_entities: set[tuple[str, str]] = {("summary", "fleet")}
        desired_variables: set[tuple[str, str, str]] = set()
        summary_fields = {
            "generated_at": (
                "Generated At",
                snapshot["generated_at"],
                ua.VariantType.String,
            ),
            "agent_count": ("Agent Count", len(agents), ua.VariantType.UInt32),
            "online_agent_count": (
                "Online Agent Count",
                sum(agent["status"] == "online" for agent in agents),
                ua.VariantType.UInt32,
            ),
            "asset_count": ("Asset Count", len(assets), ua.VariantType.UInt32),
            "task_run_count": ("Task Run Count", len(runs), ua.VariantType.UInt32),
            "active_task_run_count": (
                "Active Task Run Count",
                sum(run["state"] == "ACTIVE" for run in runs),
                ua.VariantType.UInt32,
            ),
            "recording_session_count": (
                "Recording Session Count",
                len(sessions),
                ua.VariantType.UInt32,
            ),
            "complete_recording_count": (
                "Complete Recording Count",
                sum(session["state"] == "COMPLETE" for session in sessions),
                ua.VariantType.UInt32,
            ),
        }
        await self._project_entity(
            "summary",
            "fleet",
            "Fleet",
            summary_fields,
            desired_variables,
        )

        for agent in agents:
            robots = agent.get("robots", [])
            desired_entities.add(("agents", agent["agent_id"]))
            await self._project_entity(
                "agents",
                agent["agent_id"],
                agent["agent_name"],
                {
                    "agent_id": ("Agent Id", agent["agent_id"], ua.VariantType.String),
                    "status": ("Status", agent["status"], ua.VariantType.String),
                    "software_version": (
                        "Software Version",
                        agent["sw_version"],
                        ua.VariantType.String,
                    ),
                    "received_at": (
                        "Received At",
                        agent["received_at"],
                        ua.VariantType.String,
                    ),
                    "age_seconds": (
                        "Age Seconds",
                        float(agent["age_s"]),
                        ua.VariantType.Double,
                    ),
                    "robot_count": ("Robot Count", len(robots), ua.VariantType.UInt32),
                    "healthy_robot_count": (
                        "Healthy Robot Count",
                        sum(
                            robot.get("fault") is None
                            and robot.get("sources_fresh") is True
                            and robot.get("rates_ok") is True
                            for robot in robots
                        ),
                        ua.VariantType.UInt32,
                    ),
                },
                desired_variables,
            )
            for robot in robots:
                robot_identity = f"{agent['agent_id']}--{robot['robot_id']}"
                desired_entities.add(("robots", robot_identity))
                await self._project_entity(
                    "robots",
                    robot_identity,
                    robot["robot_id"],
                    {
                        "agent_id": (
                            "Agent Id",
                            agent["agent_id"],
                            ua.VariantType.String,
                        ),
                        "robot_id": (
                            "Robot Id",
                            robot["robot_id"],
                            ua.VariantType.String,
                        ),
                        "model": ("Model", robot["model"], ua.VariantType.String),
                        "runtime_kind": (
                            "Runtime Kind",
                            robot.get("runtime_kind") or "",
                            ua.VariantType.String,
                        ),
                        "fsm_state": (
                            "FSM State",
                            robot["fsm_state"],
                            ua.VariantType.String,
                        ),
                        "phase": ("Phase", robot["phase"], ua.VariantType.String),
                        "sources_fresh": (
                            "Sources Fresh",
                            bool(robot["sources_fresh"]),
                            ua.VariantType.Boolean,
                        ),
                        "rates_ok": (
                            "Rates Ok",
                            bool(robot["rates_ok"]),
                            ua.VariantType.Boolean,
                        ),
                        "fault": (
                            "Fault",
                            robot.get("fault") or "",
                            ua.VariantType.String,
                        ),
                    },
                    desired_variables,
                )

        observed_robots = {
            (agent["agent_id"], robot["robot_id"]): (agent, robot)
            for agent in agents
            for robot in agent.get("robots", [])
        }
        for asset in assets:
            desired_entities.add(("assets", asset["asset_id"]))
            binding = asset.get("binding") or {}
            observed = observed_robots.get(
                (binding.get("agent_id"), binding.get("robot_id"))
            )
            agent_status = "unobserved" if observed is None else observed[0]["status"]
            robot = None if observed is None else observed[1]
            source = None
            if robot is not None and binding.get("source_key") is not None:
                source = next(
                    (
                        item
                        for item in robot.get("sources", [])
                        if item["key"] == binding["source_key"]
                    ),
                    None,
                )
            fresh_value = (
                source.get("fresh")
                if source is not None
                else robot.get("sources_fresh") if robot is not None else None
            )
            rate_value = (
                source.get("rate_ok")
                if source is not None
                else robot.get("rates_ok") if robot is not None else None
            )
            if observed is None:
                observation_quality = ua.StatusCode(
                    ua.StatusCodes.BadNoCommunication
                )
            elif agent_status != "online":
                observation_quality = ua.StatusCode(
                    ua.StatusCodes.UncertainLastUsableValue
                )
            else:
                observation_quality = ua.StatusCode(ua.StatusCodes.Good)
            await self._project_entity(
                "assets",
                asset["asset_id"],
                asset["display_name"],
                {
                    "asset_id": ("Asset Id", asset["asset_id"], ua.VariantType.String),
                    "asset_type": (
                        "Asset Type",
                        asset["asset_type"],
                        ua.VariantType.String,
                    ),
                    "lifecycle_state": (
                        "Lifecycle State",
                        asset.get("lifecycle_state", "active"),
                        ua.VariantType.String,
                    ),
                    "runtime_kind": (
                        "Runtime Kind",
                        asset.get("runtime_kind") or "",
                        ua.VariantType.String,
                    ),
                    "node_id": (
                        "Processing Node Id",
                        asset.get("node_id") or "",
                        ua.VariantType.String,
                    ),
                    "agent_id": (
                        "Agent Id",
                        binding.get("agent_id") or "",
                        ua.VariantType.String,
                    ),
                    "robot_id": (
                        "Robot Id",
                        binding.get("robot_id") or "",
                        ua.VariantType.String,
                    ),
                    "source_key": (
                        "Source Key",
                        binding.get("source_key") or "",
                        ua.VariantType.String,
                    ),
                    "revision": (
                        "Revision",
                        int(asset["revision"]),
                        ua.VariantType.UInt32,
                    ),
                    "observation_status": (
                        "Observation Status",
                        agent_status,
                        ua.VariantType.String,
                    ),
                    "fresh": (
                        "Fresh",
                        bool(fresh_value),
                        ua.VariantType.Boolean,
                        observation_quality,
                    ),
                    "rate_ok": (
                        "Rate Ok",
                        bool(rate_value),
                        ua.VariantType.Boolean,
                        observation_quality,
                    ),
                },
                desired_variables,
            )

        for run in runs:
            desired_entities.add(("task_runs", run["run_id"]))
            await self._project_entity(
                "task_runs",
                run["run_id"],
                run["display_name"],
                {
                    "run_id": ("Run Id", run["run_id"], ua.VariantType.String),
                    "state": ("State", run["state"], ua.VariantType.String),
                    "task_definition_id": (
                        "Task Definition Id",
                        run["task_definition_id"],
                        ua.VariantType.String,
                    ),
                    "task_version": (
                        "Task Version",
                        int(run["task_version"]),
                        ua.VariantType.UInt32,
                    ),
                    "workcell_id": (
                        "Workcell Id",
                        run.get("workcell_id") or "",
                        ua.VariantType.String,
                    ),
                    "revision": (
                        "Revision",
                        int(run["revision"]),
                        ua.VariantType.UInt32,
                    ),
                    "assignment_asset_ids": (
                        "Assignment Asset Ids",
                        json.dumps(
                            [item["asset_id"] for item in run["assignments"]],
                            separators=(",", ":"),
                        ),
                        ua.VariantType.String,
                    ),
                    "started_at": (
                        "Started At",
                        run.get("started_at") or "",
                        ua.VariantType.String,
                    ),
                    "finished_at": (
                        "Finished At",
                        run.get("finished_at") or "",
                        ua.VariantType.String,
                    ),
                    "terminal_reason": (
                        "Terminal Reason",
                        run.get("terminal_reason") or "",
                        ua.VariantType.String,
                    ),
                },
                desired_variables,
            )

        for session in sessions:
            desired_entities.add(("recordings", session["session_id"]))
            await self._project_entity(
                "recordings",
                session["session_id"],
                session["session_id"],
                {
                    "session_id": (
                        "Session Id",
                        session["session_id"],
                        ua.VariantType.String,
                    ),
                    "state": ("State", session["state"], ua.VariantType.String),
                    "agent_id": (
                        "Agent Id",
                        session["agent_id"],
                        ua.VariantType.String,
                    ),
                    "robot_id": (
                        "Robot Id",
                        session["robot_id"],
                        ua.VariantType.String,
                    ),
                    "run_id": (
                        "Run Id",
                        session.get("run_id") or "",
                        ua.VariantType.String,
                    ),
                    "profile_id": (
                        "Profile Id",
                        session["profile_id"],
                        ua.VariantType.String,
                    ),
                    "total_size_bytes": (
                        "Total Size Bytes",
                        int(session["total_size_bytes"]),
                        ua.VariantType.UInt64,
                    ),
                    "manifest_sha256": (
                        "Manifest SHA-256",
                        session["manifest_sha256"],
                        ua.VariantType.String,
                    ),
                    "started_at": (
                        "Started At",
                        session["started_at"],
                        ua.VariantType.String,
                    ),
                    "finished_at": (
                        "Finished At",
                        session["finished_at"],
                        ua.VariantType.String,
                    ),
                },
                desired_variables,
            )

        await self._reconcile(desired_entities, desired_variables)

    async def audit_read_only(self) -> dict[str, Any]:
        writable = []
        quality_counts: dict[str, int] = {}
        for key, node in self.variables.items():
            access = await node.get_access_level()
            user_access = await node.get_user_access_level()
            if (
                ua.AccessLevel.CurrentWrite in access
                or ua.AccessLevel.CurrentWrite in user_access
            ):
                writable.append("/".join(key))
            data_value = await node.read_attribute(
                ua.AttributeIds.Value,
                raise_on_bad_status=False,
            )
            status_name = data_value.StatusCode.name
            quality_counts[status_name] = quality_counts.get(status_name, 0) + 1
        method_count = 0
        for node in [self.root, *self.groups.values(), *self.entities.values()]:
            method_count += len(await node.get_methods())
        return {
            "namespace_uri": NAMESPACE_URI,
            "variable_count": len(self.variables),
            "method_count": method_count,
            "writable_variables": sorted(writable),
            "quality_status_counts": dict(sorted(quality_counts.items())),
            "audited_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "passed": method_count == 0 and not writable,
        }


def _write_audit_report(path: Path, audit: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(audit, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


async def serve(
    *,
    store: FleetStore,
    endpoint: str,
    publish_interval_s: float,
    ttls: FleetTTLs,
    security: OpcUaServerSecurity | None,
    stop: asyncio.Event,
    ready: asyncio.Event | None = None,
    conformance_report: Path | None = None,
) -> None:
    if security is None:
        server = Server()
        await server.init()
        server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        server.set_identity_tokens([ua.AnonymousIdentityToken])
        trust_store = None
    else:
        server, trust_store = await create_secure_server(
            security,
            application_uri=APPLICATION_URI,
        )
    server.set_endpoint(endpoint)
    server.set_server_name("SANG Central Read-Only Operations Aggregator")
    await server.set_application_uri(APPLICATION_URI)
    address_space = CentralAddressSpace(server)
    await address_space.build()
    catalog = RecordingCatalog(store.path)
    await address_space.project(
        collect_projection(
            store,
            catalog,
            now=datetime.now(timezone.utc),
            ttls=ttls,
        )
    )
    audit = await address_space.audit_read_only()
    if not audit["passed"]:
        raise RuntimeError("Central OPC UA address space is not read-only")
    if conformance_report is not None:
        _write_audit_report(conformance_report, audit)
    await server.start()
    if ready is not None:
        ready.set()
    next_trust_reload = asyncio.get_running_loop().time() + 5.0
    try:
        while not stop.is_set():
            await address_space.project(
                collect_projection(
                    store,
                    catalog,
                    now=datetime.now(timezone.utc),
                    ttls=ttls,
                )
            )
            audit = await address_space.audit_read_only()
            if not audit["passed"]:
                raise RuntimeError(
                    "Central OPC UA address space lost its read-only invariant"
                )
            if conformance_report is not None:
                _write_audit_report(conformance_report, audit)
            if trust_store is not None and asyncio.get_running_loop().time() >= next_trust_reload:
                await trust_store.load()
                next_trust_reload = asyncio.get_running_loop().time() + 5.0
            try:
                await asyncio.wait_for(stop.wait(), timeout=publish_interval_s)
            except TimeoutError:
                pass
    finally:
        await server.stop()


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="SANG Central read-only OPC UA operations aggregator"
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=Path.home() / ".local/share/sang-fleet/fleet.db",
    )
    parser.add_argument(
        "--endpoint",
        default="opc.tcp://127.0.0.1:4850/sang/central/",
    )
    parser.add_argument("--publish-interval-s", type=float, default=1.0)
    parser.add_argument("--online-ttl-s", type=float, default=12.0)
    parser.add_argument("--offline-ttl-s", type=float, default=30.0)
    parser.add_argument(
        "--security-mode",
        choices=("none", "sign-and-encrypt"),
        default="none",
    )
    parser.add_argument("--secret-root", type=Path)
    parser.add_argument("--server-certificate-ref")
    parser.add_argument("--server-private-key-ref")
    parser.add_argument("--client-trust-directory-ref")
    parser.add_argument("--client-crl-directory-ref")
    parser.add_argument("--authorized-user-certificate-ref", action="append", default=[])
    parser.add_argument("--conformance-report", type=Path)
    options = parser.parse_args(args)
    parsed = urlsplit(options.endpoint)
    if parsed.scheme != "opc.tcp" or parsed.hostname is None or parsed.port is None:
        parser.error("endpoint must be an opc.tcp URL with host and port")
    if not 1 <= parsed.port <= 65535:
        parser.error("endpoint port must be between 1 and 65535")
    if not math.isfinite(options.publish_interval_s) or options.publish_interval_s <= 0:
        parser.error("publish-interval-s must be positive")
    material = (
        options.secret_root,
        options.server_certificate_ref,
        options.server_private_key_ref,
        options.client_trust_directory_ref,
        options.client_crl_directory_ref,
        *options.authorized_user_certificate_ref,
    )
    if options.security_mode == "none":
        if parsed.hostname not in LOOPBACK_HOSTS:
            parser.error("insecure Central OPC UA must bind to loopback")
        if any(value is not None for value in material):
            parser.error("security material requires sign-and-encrypt mode")
    else:
        required = {
            "secret-root": options.secret_root,
            "server-certificate-ref": options.server_certificate_ref,
            "server-private-key-ref": options.server_private_key_ref,
            "client-trust-directory-ref": options.client_trust_directory_ref,
            "client-crl-directory-ref": options.client_crl_directory_ref,
        }
        missing = [name for name, value in required.items() if value is None]
        if not options.authorized_user_certificate_ref:
            missing.append("authorized-user-certificate-ref")
        if missing:
            parser.error("secure Central OPC UA is missing: " + ", ".join(missing))
    return options


async def _main(options: argparse.Namespace) -> None:
    security = None
    if options.security_mode == "sign-and-encrypt":
        resolver = SecretResolver(options.secret_root)
        security = OpcUaServerSecurity.from_secret_refs(
            resolver,
            certificate_ref=options.server_certificate_ref,
            private_key_ref=options.server_private_key_ref,
            trust_directory_ref=options.client_trust_directory_ref,
            crl_directory_ref=options.client_crl_directory_ref,
            authorized_user_certificate_refs=tuple(
                options.authorized_user_certificate_ref
            ),
        )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:
            pass
    await serve(
        store=FleetStore(options.db_path),
        endpoint=options.endpoint,
        publish_interval_s=options.publish_interval_s,
        ttls=FleetTTLs(options.online_ttl_s, options.offline_ttl_s),
        security=security,
        stop=stop,
        conformance_report=options.conformance_report,
    )


def main(args: list[str] | None = None) -> None:
    asyncio.run(_main(parse_args(args)))


if __name__ == "__main__":
    main()

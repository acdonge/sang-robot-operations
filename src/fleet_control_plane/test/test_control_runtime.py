from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import sqlite3
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from fleet_control_plane.agent_execution import (
    CommandConflict,
    DeferredExecution,
    LedgerCorruption,
    LeaseRejected,
    SimulationAgent,
)
from fleet_control_plane.cell_orchestrator import (
    AgentTargetV1,
    CellOrchestrator,
    OrchestratorRunCreateV1,
    SimulatedProcessCrash,
    SimulationTransport,
)
from fleet_control_plane.control_contracts import (
    CancelExecutionRequestV1,
    ExecuteSkillRequestV1,
)
from fleet_control_plane.control_signing import SigningKey, VerificationKeyRing
from fleet_control_plane.control_signing import SignatureInvalid
from fleet_control_plane.control_transport import (
    TransportIntegrityError,
    decode_cancel_request,
    decode_execute_skill_request,
    decode_policy_demand,
    encode_transport_contract,
)
from fleet_control_plane.demand_arbiter import (
    DemandArbiter,
    DemandAuthorizationContextV1,
    DemandRejected,
    OutputDisconnectedDemandSink,
    SignedPolicyDemandV1,
)
from fleet_control_plane.commissioning_readiness import (
    DeviceAgentSkeleton,
    FailBehaviorContractV1,
    G6ReadinessInputV1,
    OutputDisconnectedController,
    OutputIsolationEvidenceV1,
    SoftwareTimingEvidenceV1,
    evaluate_g6_readiness,
)
from fleet_control_plane.orchestration_models import (
    ControlEnvelopeV1,
    ControlSubleaseV1,
    CapabilityProfileV1,
    DelegationGrantV1,
    DeviceManifestV1,
    ExecuteSkillGoalV1,
    PolicyDemandV1,
    WorkflowDefinitionV2,
)
from fleet_control_plane.orchestration_store import canonical_json, payload_digest
from fleet_control_plane.simulation_adapters import (
    IsaacSimulationAgent,
    UnitySimulationAgent,
)


NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
BOOT_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
BOOT_B = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


def _keys():
    authority = SigningKey.generate("central-authority-key-01")
    delegated = SigningKey.generate("orchestrator-key-01")
    ring = VerificationKeyRing()
    for key in (authority, delegated):
        ring.add(
            key_id=key.key_id,
            public_key_b64=key.public_key_b64(),
            valid_from=NOW - timedelta(hours=1),
        )
    return authority, delegated, ring


def _envelope() -> ControlEnvelopeV1:
    return ControlEnvelopeV1(
        envelope_id="simulation-envelope",
        version=1,
        capabilities=["execute-plan@1"],
        max_rate_hz=20.0,
        max_demand_age_ms=100.0,
        max_clock_uncertainty_ms=5.0,
        declared_time_to_safe_state_ms=100.0,
        max_delta_by_axis={"joint": 0.1},
        approval={
            "state": "approved",
            "approver_id": "simulation-owner",
            "approved_at": NOW - timedelta(hours=1),
        },
    )


def _request(
    authority: SigningKey,
    delegated: SigningKey,
    *,
    command_id: str = "command-01",
    step_id: str = "step-01",
    holder_boot_id: UUID = BOOT_A,
    epoch: int = 1,
    issued_at: datetime = NOW,
    lease_expires_at: datetime | None = None,
    parameters: dict | None = None,
) -> ExecuteSkillRequestV1:
    command_uuid = uuid5(NAMESPACE_URL, f"test:{command_id}")
    grant_id = uuid5(NAMESPACE_URL, f"test:grant:{command_id}")
    lease_id = uuid5(NAMESPACE_URL, f"test:lease:{command_id}")
    envelope = _envelope()
    envelope_sha256 = payload_digest(envelope.model_dump(mode="json"))
    expires_at = lease_expires_at or issued_at + timedelta(seconds=30)
    grant_expires_at = issued_at + timedelta(minutes=5)
    grant = DelegationGrantV1(
        grant_id=grant_id,
        run_id="run-01",
        orchestrator_id="cell-orchestrator",
        orchestrator_boot_id=holder_boot_id,
        asset_id="robot-01",
        execution_mode="SIMULATION",
        authorization_id="simulation-authorization",
        manifest_sha256="a" * 64,
        control_envelope_id=envelope.envelope_id,
        control_envelope_sha256=envelope_sha256,
        epoch=epoch,
        capabilities=["execute-plan@1"],
        delegated_key_id=delegated.key_id,
        delegated_public_key_ref=f"key://{delegated.key_id}",
        issued_at=issued_at,
        expires_at=grant_expires_at,
        renewable_until=grant_expires_at,
        max_sublease_ttl_ms=120_000,
        issuer_key_id=authority.key_id,
        signature="unsigned-grant-0",
    )
    grant = authority.sign_contract(grant)
    sublease = ControlSubleaseV1(
        lease_id=lease_id,
        lease_kind="auto",
        parent_grant_id=grant_id,
        run_id="run-01",
        orchestrator_id="cell-orchestrator",
        orchestrator_boot_id=holder_boot_id,
        command_id=command_uuid,
        subject={"kind": "service", "principal_id": "cell-orchestrator"},
        asset_id="robot-01",
        capabilities=["execute-plan@1"],
        execution_mode="SIMULATION",
        authorization_id="simulation-authorization",
        control_envelope_id=envelope.envelope_id,
        control_envelope_sha256=envelope_sha256,
        epoch=epoch,
        issued_at=issued_at,
        expires_at=expires_at,
        renewable_until=grant_expires_at,
        issuer_key_id=delegated.key_id,
        signature="unsigned-lease-0",
    )
    sublease = delegated.sign_contract(sublease)
    goal = ExecuteSkillGoalV1(
        command_id=command_uuid,
        run_id="run-01",
        step_instance_id=step_id,
        attempt=1,
        asset_id="robot-01",
        capability="execute-plan@1",
        parameters=parameters or {"fixture": 1},
        control_envelope_id=envelope.envelope_id,
        control_envelope_sha256=envelope_sha256,
        authorization_id="simulation-authorization",
        parent_grant_id=grant_id,
        lease_id=lease_id,
        lease_epoch=epoch,
        orchestrator_boot_id=holder_boot_id,
        execution_mode="SIMULATION",
        manifest_sha256="a" * 64,
        not_valid_after=expires_at,
    )
    return ExecuteSkillRequestV1(
        agent_id="sim-agent-01",
        delegation_grant=grant,
        control_sublease=sublease,
        control_envelope=envelope,
        control_envelope_sha256=envelope_sha256,
        goal=goal,
        goal_sha256=hashlib.sha256(
            canonical_json(goal.model_dump(mode="json")).encode("utf-8")
        ).hexdigest(),
        goal_signature=delegated.sign_payload(goal),
    )


def _cancellation(
    delegated: SigningKey,
    request: ExecuteSkillRequestV1,
    *,
    reason: str = "operator requested controlled cancellation",
) -> CancelExecutionRequestV1:
    unsigned = CancelExecutionRequestV1(
        command_id=request.goal.command_id,
        run_id=request.run_id,
        agent_id=request.agent_id,
        reason=reason,
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=2),
        signing_key_id=delegated.key_id,
        signature="unsigned-cancel-0",
    )
    return unsigned.model_copy(update={"signature": delegated.sign_payload(unsigned)})


def test_simulation_agent_is_idempotent_and_detects_command_reuse(tmp_path):
    authority, delegated, ring = _keys()
    agent = SimulationAgent(
        tmp_path / "agent.db",
        agent_id="sim-agent-01",
        keyring=ring,
        capabilities={"execute-plan@1"},
        boot_id=BOOT_A,
    )
    request = _request(authority, delegated)
    first = agent.execute(request, now=NOW)
    replay = agent.execute(request, now=NOW + timedelta(minutes=1))
    assert first.state == "SUCCEEDED"
    assert replay == first
    assert first.command_sink_present is False

    changed = _request(authority, delegated, parameters={"fixture": 2})
    with pytest.raises(CommandConflict, match="reused"):
        agent.execute(changed, now=NOW)


def test_agent_fences_expired_and_same_epoch_competing_holders(tmp_path):
    authority, delegated, ring = _keys()
    agent = SimulationAgent(
        tmp_path / "agent.db",
        agent_id="sim-agent-01",
        keyring=ring,
        capabilities={"execute-plan@1"},
    )
    with pytest.raises(LeaseRejected, match="expired"):
        agent.execute(
            _request(
                authority,
                delegated,
                command_id="expired-command",
                issued_at=NOW - timedelta(minutes=2),
            ),
            now=NOW,
        )

    agent.execute(_request(authority, delegated, command_id="epoch-one"), now=NOW)
    with pytest.raises(LeaseRejected, match="competing"):
        agent.execute(
            _request(
                authority,
                delegated,
                command_id="split-brain",
                step_id="step-02",
                holder_boot_id=BOOT_B,
            ),
            now=NOW,
        )
    accepted = agent.execute(
        _request(
            authority,
            delegated,
            command_id="epoch-two",
            step_id="step-03",
            holder_boot_id=BOOT_B,
            epoch=2,
        ),
        now=NOW,
    )
    assert accepted.lease_epoch == 2


def test_agent_detects_durable_ledger_corruption(tmp_path):
    authority, delegated, ring = _keys()
    database = tmp_path / "agent.db"
    agent = SimulationAgent(
        database,
        agent_id="sim-agent-01",
        keyring=ring,
        capabilities={"execute-plan@1"},
    )
    request = _request(authority, delegated)
    agent.execute(request, now=NOW)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE agent_execution_commands SET record_json = '{}' WHERE command_id = ?",
            (request.command_id,),
        )
    with pytest.raises(LedgerCorruption, match="corrupt"):
        agent.query(request.command_id)


def test_running_command_lease_can_renew_then_expires_to_safe_state(tmp_path):
    authority, delegated, ring = _keys()
    safe_state_reasons = []
    agent = SimulationAgent(
        tmp_path / "long-action.db",
        agent_id="sim-agent-01",
        keyring=ring,
        capabilities={"execute-plan@1"},
        executor=lambda request: DeferredExecution({"started": request.command_id}),
        safe_state_handler=safe_state_reasons.append,
    )
    request = _request(authority, delegated, command_id="long-action")
    assert agent.execute(request, now=NOW).state == "RUNNING"
    renewed = _request(
        authority,
        delegated,
        command_id="long-action",
        lease_expires_at=NOW + timedelta(seconds=60),
    )
    agent.renew(request.command_id, renewed, now=NOW + timedelta(seconds=10))
    assert agent.expire_due(now=NOW + timedelta(seconds=31)) == []
    expired = agent.expire_due(now=NOW + timedelta(seconds=61))
    assert [record.state for record in expired] == ["EXPIRED"]
    assert safe_state_reasons == [f"lease expired: {request.command_id}"]


def test_cancel_requires_a_live_signature_from_the_command_lease_issuer(tmp_path):
    authority, delegated, ring = _keys()
    agent = SimulationAgent(
        tmp_path / "cancel.db",
        agent_id="sim-agent-01",
        keyring=ring,
        capabilities={"execute-plan@1"},
        executor=lambda request: DeferredExecution({"started": request.command_id}),
    )
    request = _request(authority, delegated, command_id="cancel-authenticated")
    assert agent.execute(request, now=NOW).state == "RUNNING"

    signed = _cancellation(delegated, request)
    with pytest.raises(SignatureInvalid, match="verification failed"):
        agent.cancel(signed.model_copy(update={"reason": "tampered"}), now=NOW)

    other = SigningKey.generate("other-orchestrator-key")
    ring.add(
        key_id=other.key_id,
        public_key_b64=other.public_key_b64(),
        valid_from=NOW - timedelta(minutes=1),
    )
    wrong_unsigned = signed.model_copy(
        update={"signing_key_id": other.key_id, "signature": "unsigned-cancel-0"}
    )
    wrong_signer = wrong_unsigned.model_copy(
        update={"signature": other.sign_payload(wrong_unsigned)}
    )
    with pytest.raises(LeaseRejected, match="lease issuer"):
        agent.cancel(wrong_signer, now=NOW)

    assert agent.cancel(signed, now=NOW).state == "CANCELLED"


def test_ros_transport_codec_rejects_digest_or_header_mismatch():
    authority, delegated, _ = _keys()
    request = _request(authority, delegated)
    encoded, digest = encode_transport_contract(request)
    decoded = decode_execute_skill_request(
        encoded,
        digest,
        agent_id=request.agent_id,
        command_id=request.command_id,
        run_id=request.run_id,
        step_id=request.step_id,
    )
    assert decoded == request
    with pytest.raises(TransportIntegrityError, match="digest"):
        decode_execute_skill_request(
            encoded,
            "0" * 64,
            agent_id=request.agent_id,
            command_id=request.command_id,
            run_id=request.run_id,
            step_id=request.step_id,
        )
    with pytest.raises(TransportIntegrityError, match="header"):
        decode_execute_skill_request(
            encoded,
            digest,
            agent_id=request.agent_id,
            command_id="wrong-command",
            run_id=request.run_id,
            step_id=request.step_id,
        )

    cancellation = _cancellation(delegated, request)
    cancel_json, cancel_sha256 = encode_transport_contract(cancellation)
    assert decode_cancel_request(
        cancel_json,
        cancel_sha256,
        command_id=request.command_id,
    ) == cancellation

    policy_session_id = UUID("99999999-9999-4999-8999-999999999999")
    signed_demand = _signed_demand(
        delegated,
        request,
        session_id=policy_session_id,
        sequence=1,
        sampled_at=NOW,
        value=0.0,
    )
    demand_json, demand_sha256 = encode_transport_contract(signed_demand)
    assert decode_policy_demand(
        demand_json,
        demand_sha256,
        command_id=request.command_id,
        session_id=str(policy_session_id),
        run_id=request.run_id,
        sequence=1,
        active_request=request,
    ) == signed_demand
    with pytest.raises(TransportIntegrityError, match="header"):
        decode_policy_demand(
            demand_json,
            demand_sha256,
            command_id=request.command_id,
            session_id=str(policy_session_id),
            run_id="wrong-run",
            sequence=1,
            active_request=request,
        )


def _workflow() -> WorkflowDefinitionV2:
    return WorkflowDefinitionV2.model_validate(
        {
            "workflow_id": "two-robot-sequence",
            "version": 1,
            "roles": [
                {
                    "role": "first-robot",
                    "cardinality": "1",
                    "selector": {"requires": ["execute-plan@1"]},
                    "access": "exclusive",
                },
                {
                    "role": "second-robot",
                    "cardinality": "1",
                    "selector": {"requires": ["execute-plan@1"]},
                    "access": "exclusive",
                },
            ],
            "steps": [
                {
                    "step_id": "first-move",
                    "kind": "action",
                    "role": "first-robot",
                    "capability": "execute-plan@1",
                    "parameters": {"plan_ref": "artifact://fixture/first@1"},
                    "timeout_s": 10,
                },
                {
                    "step_id": "second-move",
                    "kind": "action",
                    "depends_on": ["first-move"],
                    "role": "second-robot",
                    "capability": "execute-plan@1",
                    "parameters": {"plan_ref": "artifact://fixture/second@1"},
                    "timeout_s": 10,
                },
            ],
        }
    )


def _orchestrator_fixture(tmp_path):
    authority, delegated, ring = _keys()
    agents = {
        "sim-agent-01": SimulationAgent(
            tmp_path / "agent-01.db",
            agent_id="sim-agent-01",
            keyring=ring,
            capabilities={"execute-plan@1"},
        ),
        "sim-agent-02": SimulationAgent(
            tmp_path / "agent-02.db",
            agent_id="sim-agent-02",
            keyring=ring,
            capabilities={"execute-plan@1"},
        ),
    }
    transport = SimulationTransport(agents)
    database = tmp_path / "orchestrator.db"
    orchestrator = CellOrchestrator(
        database,
        orchestrator_id="cell-orchestrator",
        authority_signer=authority,
        delegated_signer=delegated,
        transport=transport,
        boot_id=BOOT_A,
    )
    envelope = _envelope()
    envelope_sha256 = payload_digest(envelope.model_dump(mode="json"))
    orchestrator.create_run(
        OrchestratorRunCreateV1(
            run_id="sequence-run",
            workflow=_workflow(),
            execution_mode="SIMULATION",
            role_targets={
                "first-robot": [
                    AgentTargetV1(
                        asset_id="robot-01",
                        agent_id="sim-agent-01",
                        manifest_sha256="a" * 64,
                        authorization_id="simulation-authorization",
                        control_envelope=envelope,
                        control_envelope_sha256=envelope_sha256,
                    )
                ],
                "second-robot": [
                    AgentTargetV1(
                        asset_id="robot-02",
                        agent_id="sim-agent-02",
                        manifest_sha256="b" * 64,
                        authorization_id="simulation-authorization",
                        control_envelope=envelope,
                        control_envelope_sha256=envelope_sha256,
                    )
                ],
            },
        ),
        now=NOW,
    )
    return authority, delegated, agents, transport, database, orchestrator


def test_orchestrator_executes_dag_in_dependency_order(tmp_path):
    _, _, _, transport, _, orchestrator = _orchestrator_fixture(tmp_path)
    result = orchestrator.tick("sequence-run", now=NOW)
    assert result["state"] == "COMPLETE"
    assert [step["state"] for step in result["steps"]] == ["SUCCEEDED", "SUCCEEDED"]
    assert transport.dispatch_order == [
        CellOrchestrator._command_id("sequence-run", "first-move", "robot-01"),
        CellOrchestrator._command_id("sequence-run", "second-move", "robot-02"),
    ]


def test_lost_reply_and_orchestrator_restart_query_before_redispatch(tmp_path):
    authority, delegated, agents, transport, database, orchestrator = (
        _orchestrator_fixture(tmp_path)
    )
    first_command = CellOrchestrator._command_id(
        "sequence-run", "first-move", "robot-01"
    )
    transport.drop_next_reply_for.add(first_command)
    uncertain = orchestrator.tick("sequence-run", now=NOW)
    assert uncertain["state"] == "RECONCILING"
    assert agents["sim-agent-01"].query(first_command).state == "SUCCEEDED"

    restarted = CellOrchestrator(
        database,
        orchestrator_id="cell-orchestrator",
        authority_signer=authority,
        delegated_signer=delegated,
        transport=transport,
        boot_id=BOOT_B,
    )
    reconciled = restarted.tick("sequence-run", now=NOW + timedelta(seconds=1))
    assert reconciled["state"] == "ACTIVE"
    assert reconciled["lease_epoch"] == 2
    completed = restarted.tick("sequence-run", now=NOW + timedelta(seconds=2))
    assert completed["state"] == "COMPLETE"
    assert transport.dispatch_count[first_command] == 1


def test_process_crash_after_agent_accepts_recovers_without_redispatch(tmp_path):
    authority, delegated, agents, transport, database, orchestrator = (
        _orchestrator_fixture(tmp_path)
    )
    first_command = CellOrchestrator._command_id(
        "sequence-run", "first-move", "robot-01"
    )
    transport.crash_after_accept_for.add(first_command)

    with pytest.raises(SimulatedProcessCrash):
        orchestrator.tick("sequence-run", now=NOW)
    assert agents["sim-agent-01"].query(first_command).state == "SUCCEEDED"

    restarted = CellOrchestrator(
        database,
        orchestrator_id="cell-orchestrator",
        authority_signer=authority,
        delegated_signer=delegated,
        transport=transport,
        boot_id=BOOT_B,
    )
    assert restarted.tick("sequence-run", now=NOW + timedelta(seconds=1))["state"] == "ACTIVE"
    assert restarted.tick("sequence-run", now=NOW + timedelta(seconds=2))["state"] == "COMPLETE"
    assert transport.dispatch_count[first_command] == 1


class _Backend:
    def __init__(self) -> None:
        self.command_sink_present = False
        self.process_output_connected = False
        self.commands = []

    def execute_skill(self, request):
        self.commands.append(request.command_id)
        return {"simulated": True}


@pytest.mark.parametrize("adapter_type", [UnitySimulationAgent, IsaacSimulationAgent])
def test_simulation_adapters_have_no_physical_sink(tmp_path, adapter_type):
    authority, delegated, ring = _keys()
    backend = _Backend()
    adapter = adapter_type(
        tmp_path / f"{adapter_type.__name__}.db",
        agent_id="sim-agent-01",
        keyring=ring,
        capabilities={"execute-plan@1"},
        backend=backend,
    )
    request = _request(authority, delegated)
    result = adapter.execute(request, now=NOW)
    assert result.state == "SUCCEEDED"
    assert result.result["command_sink_present"] is False
    assert backend.commands == [request.command_id]


def test_simulation_adapter_rejects_an_untrusted_or_connected_backend(tmp_path):
    _, _, ring = _keys()

    class ConnectedBackend(_Backend):
        def __init__(self) -> None:
            super().__init__()
            self.command_sink_present = True

    with pytest.raises(ValueError, match="physical outputs disconnected"):
        UnitySimulationAgent(
            tmp_path / "connected.db",
            agent_id="sim-agent-01",
            keyring=ring,
            capabilities={"execute-plan@1"},
            backend=ConnectedBackend(),
        )


def _signed_demand(
    delegated: SigningKey,
    request: ExecuteSkillRequestV1,
    *,
    session_id: UUID,
    sequence: int,
    sampled_at: datetime,
    value: float,
) -> SignedPolicyDemandV1:
    demand = PolicyDemandV1(
        policy_session_id=session_id,
        command_id=request.goal.command_id,
        sequence=sequence,
        sampled_at=sampled_at,
        policy_sha256="c" * 64,
        lease_id=request.control_sublease.lease_id,
        lease_epoch=request.control_sublease.epoch,
        frame_id="robot-base",
        units={"joint": "rad"},
        demand={"joint": value},
    )
    return SignedPolicyDemandV1(
        demand=demand,
        signing_key_id=delegated.key_id,
        signature=delegated.sign_payload(demand),
    )


def test_demand_arbiter_enforces_sequence_rate_delta_age_and_clock(tmp_path):
    authority, delegated, ring = _keys()
    request = _request(authority, delegated)
    sink = OutputDisconnectedDemandSink()
    arbiter = DemandArbiter(tmp_path / "arbiter.db", keyring=ring, sink=sink)
    context = DemandAuthorizationContextV1(
        expected_policy_sha256="c" * 64,
        expected_frame_id="robot-base",
        expected_units={"joint": "rad"},
    )
    session_id = UUID("55555555-5555-4555-8555-555555555555")
    first = _signed_demand(
        delegated,
        request,
        session_id=session_id,
        sequence=1,
        sampled_at=NOW,
        value=0.0,
    )
    accepted = arbiter.accept(
        first,
        request=request,
        context=context,
        received_at=NOW,
        measured_clock_uncertainty_ms=0.5,
    )
    assert accepted["command_sink_present"] is False

    with pytest.raises(DemandRejected, match="sequence"):
        arbiter.accept(
            first,
            request=request,
            context=context,
            received_at=NOW,
            measured_clock_uncertainty_ms=0.5,
        )
    with pytest.raises(DemandRejected, match="maximum rate"):
        arbiter.accept(
            _signed_demand(
                delegated,
                request,
                session_id=session_id,
                sequence=2,
                sampled_at=NOW + timedelta(milliseconds=10),
                value=0.01,
            ),
            request=request,
            context=context,
            received_at=NOW + timedelta(milliseconds=10),
            measured_clock_uncertainty_ms=0.5,
        )
    with pytest.raises(DemandRejected, match="delta"):
        arbiter.accept(
            _signed_demand(
                delegated,
                request,
                session_id=session_id,
                sequence=2,
                sampled_at=NOW + timedelta(milliseconds=60),
                value=0.5,
            ),
            request=request,
            context=context,
            received_at=NOW + timedelta(milliseconds=60),
            measured_clock_uncertainty_ms=0.5,
        )
    second = arbiter.accept(
        _signed_demand(
            delegated,
            request,
            session_id=session_id,
            sequence=2,
            sampled_at=NOW + timedelta(milliseconds=60),
            value=0.05,
        ),
        request=request,
        context=context,
        received_at=NOW + timedelta(milliseconds=60),
        measured_clock_uncertainty_ms=0.5,
    )
    assert second["accepted"] is True
    assert len(sink.accepted) == 2

    stale_session = UUID("66666666-6666-4666-8666-666666666666")
    with pytest.raises(DemandRejected, match="stale"):
        arbiter.accept(
            _signed_demand(
                delegated,
                request,
                session_id=stale_session,
                sequence=1,
                sampled_at=NOW,
                value=0.0,
            ),
            request=request,
            context=context,
            received_at=NOW + timedelta(seconds=1),
            measured_clock_uncertainty_ms=0.5,
        )
    with pytest.raises(DemandRejected, match="clock uncertainty is missing"):
        arbiter.accept(
            _signed_demand(
                delegated,
                request,
                session_id=stale_session,
                sequence=1,
                sampled_at=NOW,
                value=0.0,
            ),
            request=request,
            context=context,
            received_at=NOW,
            measured_clock_uncertainty_ms=None,
        )


def test_key_rotation_revocation_and_signature_tamper_fail_closed():
    authority, delegated, ring = _keys()
    request = _request(authority, delegated)
    ring.verify_contract(request.control_sublease, at=NOW)
    tampered = request.control_sublease.model_copy(update={"signature": "A" * 88})
    with pytest.raises(SignatureInvalid, match="verification failed"):
        ring.verify_contract(tampered, at=NOW)

    replacement = SigningKey.generate("orchestrator-key-02")
    ring.add(
        key_id=replacement.key_id,
        public_key_b64=replacement.public_key_b64(),
        valid_from=NOW - timedelta(minutes=1),
    )
    replacement_request = _request(authority, replacement, command_id="replacement")
    ring.verify_contract(replacement_request.control_sublease, at=NOW)
    ring.revoke(delegated.key_id, revoked_at=NOW)
    with pytest.raises(SignatureInvalid, match="not active"):
        ring.verify_contract(request.control_sublease, at=NOW)
    with pytest.raises(SignatureInvalid, match="revocation is immutable"):
        ring.revoke(delegated.key_id, revoked_at=NOW + timedelta(seconds=1))
    with pytest.raises(SignatureInvalid, match="identity is immutable"):
        ring.add(
            key_id=replacement.key_id,
            public_key_b64=SigningKey.generate("replacement-material").public_key_b64(),
            valid_from=NOW - timedelta(minutes=1),
        )


def test_g6_device_agent_faults_remain_output_disconnected(tmp_path):
    authority, delegated, ring = _keys()
    _, _, _, fail_behavior, _ = _g6_contracts()
    database = tmp_path / "g6-agent.db"
    controller = OutputDisconnectedController()
    agent = DeviceAgentSkeleton(
        database,
        agent_id="sim-agent-01",
        keyring=ring,
        capabilities={"execute-plan@1"},
        controller=controller,
        fail_behaviors=[fail_behavior],
    )
    normal = _request(authority, delegated, command_id="g6-normal")
    succeeded = agent.execute(normal, now=NOW)
    assert succeeded.state == "SUCCEEDED"
    assert controller.commands == [normal.command_id]

    controller.inject_fault_once("restart")
    faulted = agent.execute(
        _request(authority, delegated, command_id="g6-restart"),
        now=NOW,
    )
    assert faulted.state == "FAILED"
    assert faulted.command_sink_present is False
    assert controller.command_sink_present is False
    assert controller.process_output_connected is False
    assert controller.fail_actions[-1]["action"] == "safe_state"

    controller.inject_fault_once("defer")
    communication_request = _request(authority, delegated, command_id="g6-communication")
    assert agent.execute(communication_request, now=NOW).state == "RUNNING"
    communication_failed = agent.handle_communication_loss(
        communication_request.command_id,
        now=NOW + timedelta(seconds=1),
    )
    assert communication_failed.error_code == "communication-lost"
    assert controller.fail_actions[-1]["action"] == "safe_state"

    controller.inject_fault_once("defer")
    restart_request = _request(authority, delegated, command_id="g6-inflight-restart")
    assert agent.execute(restart_request, now=NOW).state == "RUNNING"
    restarted_controller = OutputDisconnectedController()
    restarted_agent = DeviceAgentSkeleton(
        database,
        agent_id="sim-agent-01",
        keyring=ring,
        capabilities={"execute-plan@1"},
        controller=restarted_controller,
        fail_behaviors=[fail_behavior],
        boot_id=BOOT_B,
    )
    recovered = restarted_agent.recover_after_restart(now=NOW + timedelta(seconds=1))
    assert [item.command_id for item in recovered] == [restart_request.command_id]
    assert restarted_controller.fail_actions[-1]["action"] == "safe_state"

    controller.inject_fault_once("defer")
    expiring_request = _request(
        authority,
        delegated,
        command_id="g6-expiring",
        lease_expires_at=NOW + timedelta(seconds=1),
    )
    assert agent.execute(expiring_request, now=NOW).state == "RUNNING"
    expired = agent.expire_due(now=NOW + timedelta(seconds=2))
    assert [item.command_id for item in expired] == [expiring_request.command_id]
    assert controller.fail_actions[-1]["action"] == "safe_state"

    controller.inject_fault_once("defer")
    cancel_request = _request(authority, delegated, command_id="g6-cancel")
    assert agent.execute(cancel_request, now=NOW).state == "RUNNING"
    assert agent.cancel(_cancellation(delegated, cancel_request), now=NOW).state == "CANCELLED"
    assert controller.fail_actions[-1]["action"] == "controlled_cancel"

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE agent_execution_commands SET record_json = '{}' WHERE command_id = ?",
            (normal.command_id,),
        )
    with pytest.raises(LedgerCorruption):
        agent.query(normal.command_id)
    assert controller.fail_actions[-1]["action"] == "safe_state"


def _g6_contracts():
    profile = CapabilityProfileV1.model_validate(
        {
            "profile_id": "g6-fixture-profile",
            "version": 1,
            "kind_tags": ["hil"],
            "capabilities": [
                {
                    "capability_id": "execute-plan",
                    "version": 1,
                    "effect": "actuation",
                    "parameter_schema_ref": "contracts://execute-plan.v1",
                    "required_frames": ["base", "tool"],
                    "required_units": ["joint-position"],
                    "required_limits": ["joint-position"],
                    "control_envelope_schema_ref": "contracts://envelope.v1",
                    "completion_contract_ref": "contracts://complete.v1",
                    "cancellation_contract_ref": "contracts://cancel.v1",
                    "recovery_contract_ref": "contracts://recover.v1",
                    "evidence_contract_ref": "contracts://evidence.v1",
                    "allowed_execution_modes": ["SIMULATION", "HIL"],
                    "max_clock_uncertainty_ms": 5.0,
                    "max_demand_age_ms": 100.0,
                    "on_lease_expiry": "safe_state",
                    "on_agent_restart_with_motion_in_flight": "safe_state",
                }
            ],
        }
    )
    manifest = DeviceManifestV1.model_validate(
        {
            "manifest_id": "g6-fixture-manifest",
            "version": 1,
            "profile_ref": {"profile_id": profile.profile_id, "version": 1},
            "vendor": {"make": "fixture", "model": "disconnected"},
            "capability_refs": ["execute-plan@1"],
            "frames": {
                "mappings": {"base": "base", "tool": "tool"},
                "world_calibration_required": True,
            },
            "units": {"mappings": {"joint-position": "rad"}},
            "limits_ref": {
                "object_id": "g6-limits",
                "version": 1,
                "sha256": "d" * 64,
            },
            "observation_contract": [],
            "execution_contract": {
                "agent_api": "sang_control_msgs/action/ExecuteSkill@1",
                "supported_modes": ["SIMULATION", "HIL"],
                "command_sink_present": False,
            },
            "local_fail_behavior_ref": "contracts://fail/g6-fixture@1",
        }
    )
    isolation = OutputIsolationEvidenceV1(
        evidence_id="g6-output-isolation",
        version=1,
        agent_id="g6-agent",
        controller_id="output-disconnected-controller",
        observed_at=NOW,
        observer_id="test-observer",
        artifact_refs=["artifact://tests/g6-output-isolation"],
    )
    fail_behavior = FailBehaviorContractV1(
        contract_id="g6-fail-behavior",
        version=1,
        capability_ref="execute-plan@1",
        on_lease_expiry="safe_state",
        on_communication_loss="safe_state",
        on_agent_restart="safe_state",
        on_cancel="controlled_cancel",
        declared_time_to_safe_state_ms=20.0,
    )
    timing = SoftwareTimingEvidenceV1(
        evidence_id="g6-software-timing",
        version=1,
        capability_ref="execute-plan@1",
        measured_at=NOW,
        method="monotonic-fixture",
        isolation_evidence_ref={
            "object_id": isolation.evidence_id,
            "version": isolation.version,
            "sha256": payload_digest(isolation.model_dump(mode="json")),
        },
        safe_state_latency_ms=[1.0, 1.2, 1.1, 1.3, 1.0],
    )
    return profile, manifest, isolation, fail_behavior, timing


def test_g6_readiness_is_software_only_and_never_authorizes_g7():
    profile, manifest, isolation, fail_behavior, timing = _g6_contracts()
    body = G6ReadinessInputV1(
        agent_id="g6-agent",
        manifest=manifest,
        capability_profile=profile,
        fail_behaviors=[fail_behavior],
        output_isolation=isolation,
        timing_evidence=[timing],
    )
    report = evaluate_g6_readiness(body, generated_at=NOW)
    assert report.software_status == "SOFTWARE_READY"
    assert report.g6b_lab_evidence_status == "UNVERIFIED"
    assert report.g7_authorized is False
    assert report.physical_motion_enabled is False
    assert report.process_output_enabled is False
    assert len(report.lab_blockers) == 4

    connected_manifest = manifest.model_copy(
        update={
            "execution_contract": manifest.execution_contract.model_copy(
                update={"command_sink_present": True}
            )
        }
    )
    blocked = evaluate_g6_readiness(
        body.model_copy(update={"manifest": connected_manifest}),
        generated_at=NOW,
    )
    assert blocked.software_status == "NOT_READY"
    assert blocked.g7_authorized is False


def test_g6_rejects_timing_evidence_bound_to_the_wrong_isolation_digest():
    profile, manifest, isolation, fail_behavior, timing = _g6_contracts()
    bad_timing = timing.model_copy(
        update={
            "isolation_evidence_ref": timing.isolation_evidence_ref.model_copy(
                update={"sha256": "e" * 64}
            )
        }
    )

    with pytest.raises(ValueError, match="output-isolation digest"):
        G6ReadinessInputV1(
            agent_id="g6-agent",
            manifest=manifest,
            capability_profile=profile,
            fail_behaviors=[fail_behavior],
            output_isolation=isolation,
            timing_evidence=[bad_timing],
        )

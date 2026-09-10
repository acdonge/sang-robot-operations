from pathlib import Path

import pytest
import yaml

from sang_opcua_gateway.robotics_contract import (
    MANDATORY_COVERAGE,
    RoboticsContractError,
    load_robotics_profile_catalog,
)


ROOT = Path(__file__).resolve().parents[3]
CATALOG_PATH = ROOT / "contracts/opcua/robotics-profiles.v1.yaml"


def test_robotics_profile_catalog_separates_identity_roles_and_support_states():
    catalog = load_robotics_profile_catalog(CATALOG_PATH)

    assert set(catalog.official_models) == {"core", "di", "ia", "robotics"}
    assert {profile.robot_id for profile in catalog.profiles} == {
        "bi_so101_01",
        "omy_01",
        "rb5_850e_01",
        "robotics_fixture_01",
    }
    for profile in catalog.profiles:
        identities = tuple(profile.identities.__dict__.values())
        assert len(identities) == len(set(identities))
        assert set(profile.coverage) >= MANDATORY_COVERAGE
        assert profile.conformance_claim is False


@pytest.mark.parametrize("robot_id", ("bi_so101_01", "omy_01", "rb5_850e_01"))
def test_physical_profiles_fail_honestly_as_partial(robot_id):
    profile = load_robotics_profile_catalog(CATALOG_PATH).for_robot(robot_id)

    assert profile.runtime_kind == "physical"
    assert profile.support_status == "partial"
    missing = {
        element
        for element, coverage in profile.coverage.items()
        if coverage.mandatory and coverage.state == "missing"
    }
    assert missing
    assert {"controllers", "power_trains", "safety_states"} <= missing


def test_reference_fixture_has_truthful_complete_base_candidate_shape():
    profile = load_robotics_profile_catalog(CATALOG_PATH).for_robot(
        "robotics_fixture_01"
    )

    assert profile.runtime_kind == "reference_fixture"
    assert profile.support_status == "base_candidate"
    assert profile.controller is not None
    assert len(profile.motion_devices) == 1
    assert len(profile.axes) == 2
    assert len(profile.safety_states) == 1
    assert all(
        profile.coverage[element].state == "available"
        for element in MANDATORY_COVERAGE
    )


def test_catalog_rejects_unverified_conformance_claim(tmp_path):
    raw = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))
    fixture = next(
        profile
        for profile in raw["profiles"]
        if profile["robot_id"] == "robotics_fixture_01"
    )
    fixture["conformance_claim"] = True
    path = tmp_path / "invalid-claim.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    with pytest.raises(RoboticsContractError, match="CTT evidence"):
        load_robotics_profile_catalog(path)

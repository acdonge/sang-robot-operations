import os
from pathlib import Path
import subprocess

from sang_security.pki import bootstrap_lab


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "sync_phase3_edge_crls.sh"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _edge_role(tmp_path: Path) -> Path:
    result = bootstrap_lab(
        tmp_path / "pki",
        central_host="central.sang.local",
        central_ip="127.0.0.1",
        edge_host="edge.sang.local",
        edge_ip="127.0.0.1",
        agent_id="workcell-soarm-01",
        key_size=2048,
    )
    return Path(result["roles"]["edge"])


def test_crl_sync_allocates_remote_archive_with_mktemp(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "commands.log"
    _write_executable(
        fake_bin / "ssh",
        """#!/usr/bin/env bash
set -euo pipefail
printf 'ssh:%s\\n' "$*" >>"${SANG_TEST_LOG}"
if [[ "${2:-}" == "umask 077; mktemp /tmp/sang-phase3-crls.XXXXXXXXXX" ]]; then
  printf '/tmp/sang-phase3-crls.safe123\\n'
elif [[ "${2:-}" == bash\\ -s\\ --* ]]; then
  cat >/dev/null
fi
""",
    )
    _write_executable(
        fake_bin / "scp",
        """#!/usr/bin/env bash
set -euo pipefail
printf 'scp:%s\\n' "$*" >>"${SANG_TEST_LOG}"
""",
    )
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "SANG_TEST_LOG": str(log),
    }

    completed = subprocess.run(
        [str(SCRIPT), str(_edge_role(tmp_path)), "edge-user@test", "/srv/sang/edge"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 0, completed.stderr
    commands = log.read_text(encoding="utf-8").splitlines()
    assert commands[0].endswith(
        "umask 077; mktemp /tmp/sang-phase3-crls.XXXXXXXXXX"
    )
    assert commands[1].startswith("scp:")
    assert commands[1].endswith(
        "edge-user@test:/tmp/sang-phase3-crls.safe123"
    )
    assert commands[2].startswith(
        "ssh:edge-user@test bash -s -- /srv/sang/edge "
        "/tmp/sang-phase3-crls.safe123"
    )
    assert commands[3] == (
        "ssh:edge-user@test rm -f '/tmp/sang-phase3-crls.safe123'"
    )


def test_crl_sync_dry_run_does_not_contact_edge(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for command in ("ssh", "scp"):
        _write_executable(
            fake_bin / command,
            "#!/usr/bin/env bash\nexit 99\n",
        )
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "SANG_CRL_SYNC_DRY_RUN": "1",
    }

    completed = subprocess.run(
        [str(SCRIPT), str(_edge_role(tmp_path)), "unused", "/unused"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 0, completed.stderr
    assert "valid for at least seven days" in completed.stdout

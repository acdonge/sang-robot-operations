"""Bounded chrony status sampling for Agent and capture provenance."""

from __future__ import annotations

from datetime import datetime, timezone
import math
import subprocess
import time
from typing import Any, Callable


CHRONY_FIELDS = 14
LOCKED_OFFSET_MS = 50.0
DEGRADED_OFFSET_MS = 250.0


def unsupported_sync() -> dict[str, Any]:
    return {
        "supported": False,
        "state": "unsupported",
        "pending": None,
    }


def _unknown(error_code: str) -> dict[str, Any]:
    return {
        "supported": True,
        "state": "unknown",
        "pending": None,
        "source": "chrony",
        "sampled_at": datetime.now(timezone.utc).isoformat(),
        "error_code": error_code,
    }


def parse_chrony_tracking(value: str, *, sampled_at: datetime | None = None) -> dict[str, Any]:
    fields = value.strip().split(",")
    if len(fields) != CHRONY_FIELDS:
        raise ValueError("chrony tracking response has an unexpected field count")
    try:
        stratum = int(fields[2])
        reference_time = float(fields[3])
        system_offset_ms = float(fields[4]) * 1000.0
        last_offset_ms = float(fields[5]) * 1000.0
        rms_offset_ms = float(fields[6]) * 1000.0
        root_delay_ms = float(fields[10]) * 1000.0
        root_dispersion_ms = float(fields[11]) * 1000.0
        update_interval_s = float(fields[12])
    except ValueError as exc:
        raise ValueError("chrony tracking response contains invalid numbers") from exc
    numbers = (
        reference_time,
        system_offset_ms,
        last_offset_ms,
        rms_offset_ms,
        root_delay_ms,
        root_dispersion_ms,
        update_interval_s,
    )
    if not all(math.isfinite(item) for item in numbers):
        raise ValueError("chrony tracking response contains non-finite numbers")
    leap_status = fields[13].strip()
    absolute_offset = abs(system_offset_ms)
    if leap_status != "Normal" or not 1 <= stratum <= 15:
        state = "unsynchronized"
    elif absolute_offset <= LOCKED_OFFSET_MS:
        state = "locked"
    elif absolute_offset <= DEGRADED_OFFSET_MS:
        state = "degraded"
    else:
        state = "unsynchronized"
    observed = sampled_at or datetime.now(timezone.utc)
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("sampled_at must include a timezone")
    return {
        "supported": True,
        "state": state,
        "pending": None,
        "source": "chrony",
        "sampled_at": observed.isoformat(),
        "reference_id": fields[0],
        "reference_name": fields[1],
        "stratum": stratum,
        "system_offset_ms": system_offset_ms,
        "last_offset_ms": last_offset_ms,
        "rms_offset_ms": rms_offset_ms,
        "root_delay_ms": root_delay_ms,
        "root_dispersion_ms": root_dispersion_ms,
        "update_interval_s": update_interval_s,
        "leap_status": leap_status,
        "error_code": None,
    }


def sample_chrony(
    host: str,
    *,
    timeout_s: float = 1.0,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    try:
        result = run(
            ["chronyc", "-h", host, "-c", "tracking"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _unknown("timeout")
    except OSError:
        return _unknown("command_unavailable")
    if result.returncode != 0 or len(result.stdout) > 4096:
        return _unknown("query_failed")
    try:
        return parse_chrony_tracking(result.stdout)
    except ValueError:
        return _unknown("invalid_response")


class ClockSyncSampler:
    """Cache chrony subprocess reads so ROS callbacks are not probed every tick."""

    def __init__(
        self,
        host: str | None,
        *,
        timeout_s: float = 1.0,
        refresh_interval_s: float = 15.0,
    ) -> None:
        if timeout_s <= 0.0 or refresh_interval_s <= 0.0:
            raise ValueError("clock sync timing values must be positive")
        self.host = host
        self.timeout_s = timeout_s
        self.refresh_interval_s = refresh_interval_s
        self._sample = unsupported_sync()
        self._sampled_monotonic = float("-inf")

    def sample(self, *, force: bool = False) -> dict[str, Any]:
        if self.host is None:
            return dict(self._sample)
        now = time.monotonic()
        if force or now - self._sampled_monotonic >= self.refresh_interval_s:
            self._sample = sample_chrony(self.host, timeout_s=self.timeout_s)
            self._sampled_monotonic = now
        return dict(self._sample)


def recording_clock(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Freeze a live sync sample into the existing recording clock contract."""
    state = str(snapshot.get("state", "unknown"))
    return {
        "sync_source": str(snapshot.get("source", "unverified_host_clock")),
        "quality": state.upper(),
        "provenance": (
            "chronyc tracking sampled on the recorder host immediately before capture."
            if snapshot.get("source") == "chrony"
            else "No host clock synchronization evidence was available at capture start."
        ),
        "sample": snapshot,
    }

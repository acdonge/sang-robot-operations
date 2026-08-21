"""Finite read-only SO-101 hardware probe without ROS publishers."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

from so101_readonly_adapter.arm_node import select_arms
from so101_readonly_adapter.calibration import load_calibration
from so101_readonly_adapter.config import load_hardware_config
from so101_readonly_adapter.feetech_bus import ReadOnlyFeetechBus


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Finite read-only SO-101 bus probe")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--arm", action="append", required=True)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--interval-s", type=float, default=0.1)
    options = parser.parse_args(args)
    if options.samples <= 0 or options.interval_s < 0.0:
        parser.error("samples must be positive and interval-s must not be negative")
    return options


def main(args: list[str] | None = None) -> None:
    options = parse_args(args)
    hardware = load_hardware_config(options.config)
    try:
        arms = select_arms(hardware.arms, options.arm)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    results = []
    for arm in arms:
        calibration = load_calibration(arm.calibration_path)
        bus = ReadOnlyFeetechBus(
            arm.port,
            calibration,
            baudrate=arm.baudrate,
            expected_model=arm.expected_model,
        )
        samples = []
        try:
            bus.open()
            for index in range(options.samples):
                raw = bus.read_positions()
                samples.append(
                    {
                        "raw": [raw[motor_id] for motor_id in calibration.motor_ids],
                        "normalized": [
                            round(value, 3) for value in calibration.normalize(raw)
                        ],
                    }
                )
                if index + 1 < options.samples:
                    time.sleep(options.interval_s)
        finally:
            bus.close()
        results.append(
            {
                "arm": arm.key,
                "port": arm.port,
                "mode": "read_only",
                "samples": samples,
            }
        )
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()

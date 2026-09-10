"""Read-only evidence sampler for the live Isaac -> ROS -> Unity loop."""

import argparse
import json
from pathlib import Path
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path("/tmp/sang-isaac-rb5-twin")
    samples = []
    started = time.monotonic()
    while time.monotonic() - started < args.seconds:
        row = {"wall_unix_ms": int(time.time()*1000)}
        for name, path in (("state", root/"heartbeat.json"),
                           ("ros", root/"observer/telemetry.json"),
                           ("mirror", root/"observer/mirror-status.json"),
                           ("receiver", root/"observer/receiver-status.json")):
            try:
                row[name] = json.loads(path.read_text())
            except (OSError, ValueError) as exc:
                row[name] = {"read_error": str(exc)}
        samples.append(row)
        time.sleep(.05)
    positions = [row["ros"]["joint_positions_rad"][0] for row in samples
                 if "joint_positions_rad" in row["ros"]]
    mirrored = [row["mirror"] for row in samples if row["mirror"].get("state") == "mirroring"]
    report = {"schema_version": 1, "wall_seconds": time.monotonic()-started,
              "sample_count": len(samples), "physical_output_connected": False,
              "base_min_rad": min(positions) if positions else None,
              "base_max_rad": max(positions) if positions else None,
              "mirroring_samples": len(mirrored), "samples": samples}
    if mirrored:
        report["max_mirror_error_rad"] = max(row["max_abs_mirror_error_rad"] for row in mirrored)
        report["max_mirror_receive_age_ms"] = max(row["reported_at_unix_ms"]-row["received_at_unix_ms"] for row in mirrored)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2))
    print(json.dumps({k:v for k,v in report.items() if k != "samples"}))


if __name__ == "__main__":
    main()

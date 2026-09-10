"""Read-only, bounded evidence collection for the Scenario 1 observer link."""

import argparse
from collections import OrderedDict
import json
from pathlib import Path
import time

from .contract import ROOT, atomic_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=240)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 1800:
        parser.error("seconds must be 1..1800")
    sources = OrderedDict()
    checked = set()
    phases, owners = set(), set()
    misses = samples = failures = 0
    max_joint = max_position = max_visual = 0.
    first = last = None
    started = time.monotonic()
    while time.monotonic()-started < args.seconds:
        try:
            source = json.loads((ROOT/"observer/source-provenance.json").read_text())
            mirror = json.loads((ROOT/"observer/mirror-status.json").read_text())
            key = (source["session_id"],source["epoch"],source["sequence"])
            sources[key] = source
            if len(sources) > 300:
                sources.popitem(last=False)
            mkey = (mirror["session_id"],mirror["epoch"],mirror["sequence"])
            if mkey in checked:
                time.sleep(.005)
                continue
            original = sources.get(mkey)
            if original is None:
                misses += 1
                time.sleep(.005)
                continue
            checked.add(mkey)
            samples += 1
            first = original if first is None else first
            last = original
            phases.add(original["phase"])
            owners.add(original["plate_owner"])
            if (not mirror["fresh"] or mirror["state"] != "mirroring"
                    or mirror["scene_sha256"] != original["scene_sha256"]
                    or mirror["sim_time_ns"] != original["sim_time_ns"]):
                failures += 1
            expected = {a["asset_id"]:a for a in original["assets"]}
            for asset in mirror["assets"]:
                source_asset = expected[asset["asset_id"]]
                names = source_asset["joint_names"]
                q = dict(zip(names,source_asset["joint_positions_rad"]))
                for name,value in zip(asset["joint_names"],asset["rendered_joint_positions_rad"]):
                    max_joint = max(max_joint,abs(q[name]-value))
                max_position = max(max_position,max(abs(a-b) for a,b in zip(source_asset["position_m"],asset["rendered_position_m"])))
                max_visual = max(max_visual,max(asset["visual_joint_error_rad"],default=0.))
        except (OSError,ValueError,KeyError,TypeError):
            failures += 1
        time.sleep(.005)
    live = json.loads((ROOT/"live-evidence.json").read_text())
    receiver = json.loads((ROOT/"observer/receiver-status.json").read_text())
    delta = None if first is None or last is None else {
        "messages":last["sequence"]-first["sequence"],
        "source_wall_s":(last["source_unix_ms"]-first["source_unix_ms"])/1000,
        "simulation_s":(last["sim_time_ns"]-first["sim_time_ns"])/1e9,
    }
    result = dict(kind="readonly_scenario1_source_to_unity_probe", matched_samples=samples,
        unmatched_poll_attempts=misses, failures=failures, max_joint_error_rad=max_joint,
        max_position_error_m=max_position, max_visual_joint_error_rad=max_visual,
        phases=sorted(phases), owners=sorted(owners), delta=delta, final_execution=live,
        receiver=receiver, physical_output_connected=False)
    result["mirror_passed"] = samples >= 100 and failures == 0 and max_joint < .0001 and max_position < .0001 and max_visual < .0001
    atomic_json(args.output,result)
    print(json.dumps({k:v for k,v in result.items() if k not in ("final_execution","receiver")}),flush=True)


if __name__ == "__main__":
    main()

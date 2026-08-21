"""Validate vendor scene provenance without starting Isaac Sim."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from .profile import DEFAULT_PROFILES_PATH, ProfileError, load_profiles, verify_profile_files
except ImportError:
    from profile import DEFAULT_PROFILES_PATH, ProfileError, load_profiles, verify_profile_files


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiles", type=Path, default=DEFAULT_PROFILES_PATH)
    parser.add_argument("--profile", action="append", dest="profile_ids")
    return parser.parse_args(args)


def main(args: list[str] | None = None) -> int:
    options = parse_args(args)
    try:
        profiles = load_profiles(options.profiles)
        selected = options.profile_ids or list(profiles)
        unknown = sorted(set(selected) - set(profiles))
        if unknown:
            raise ProfileError(f"unknown profiles: {', '.join(unknown)}")
        results = []
        for profile_id in selected:
            profile = profiles[profile_id]
            files = verify_profile_files(profile)
            results.append(
                {
                    "profile_id": profile.profile_id,
                    "manufacturer": profile.manufacturer,
                    "model": profile.model,
                    "source_url": profile.source_url,
                    "source_revision": profile.source_revision,
                    "controlled_joints": list(profile.controlled_joints),
                    "files": files,
                    "valid": all(item["matches"] for item in files),
                }
            )
        payload = {
            "schema_version": "sang_isaac_profile_validation_v1",
            "valid": all(result["valid"] for result in results),
            "profiles": results,
        }
    except ProfileError as exc:
        payload = {
            "schema_version": "sang_isaac_profile_validation_v1",
            "valid": False,
            "error": str(exc),
        }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

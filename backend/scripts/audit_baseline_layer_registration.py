from __future__ import annotations

import argparse
import json
import urllib.request


def _fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit that empty baseline output layers are not registered in project metadata.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--baseline-release", default=None)
    args = parser.parse_args()
    project = _fetch_json(f"{args.base_url}/api/temporal-projects/{args.project_id}")
    milestones = project.get("milestones") or []
    baseline = (
        next((milestone for milestone in milestones if milestone.get("release_identifier") == args.baseline_release), None)
        if args.baseline_release
        else None
    ) or (milestones[0] if milestones else {})
    artifacts = baseline.get("artifacts") or []
    empty_output_artifacts = [
        artifact
        for artifact in artifacts
        if artifact.get("media_type") == "application/geo+json" and (artifact.get("feature_count") or 0) == 0
    ]
    result = {
        "project_id": args.project_id,
        "baseline_release_identifier": baseline.get("release_identifier"),
        "baseline_reference_imagery_registered": bool(baseline.get("reference_imagery")),
        "baseline_artifact_count": len(artifacts),
        "empty_baseline_output_layers_registered": len(empty_output_artifacts) > 0,
        "empty_baseline_output_artifact_keys": [artifact.get("key") for artifact in empty_output_artifacts],
        "milestone_count": len(milestones),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if result["empty_baseline_output_layers_registered"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

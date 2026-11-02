"""Preview HH IPSF history; corrections require a pinned exact-row review."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge.hha_provider_reference import import_reference_package

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--expected-catalog-sha256", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--ccn", action="append", help="Optional reviewed import subset; repeated for each CCN.")
    parser.add_argument("--conflict-review", type=Path, help="Explicit exact-row correction approvals; ordinary imports never overwrite facts.")
    parser.add_argument("--expected-conflict-review-sha256", help="Independently reviewed SHA-256 of the complete correction manifest.")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    # Reserve evidence before any possible reference write; never erase an older
    # import report or discover an invalid report path after committing.
    with args.report.open("x", encoding="utf-8") as report:
        try:
            result = import_reference_package(args.package, args.database,
                expected_catalog_sha256=args.expected_catalog_sha256, apply=args.apply, backup_path=args.backup,
                provider_ccns=args.ccn, conflict_review_path=args.conflict_review,
                expected_conflict_review_sha256=args.expected_conflict_review_sha256)
        except Exception as error:
            failure = dict(status="Failed", requestedApply=args.apply, error=str(error))
            if hasattr(error, "conflicts"):
                failure["conflicts"] = error.conflicts
            report.write(json.dumps(failure, indent=2))
            raise
        report.write(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    if not result["safeToApply"]:
        raise SystemExit(2)

"""Preview or append a reviewed effective-dated ESRD reference package."""
import argparse
import getpass
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge.esrd_catalog import import_catalog

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--expected-catalog-sha256', required=True)
    parser.add_argument('--revision', type=int, required=True)
    parser.add_argument('--operator', default=getpass.getuser())
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup', type=Path)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--supersedes', help='Exact fingerprint of the predecessor release; required for an overlapping correction.')
    parser.add_argument('--correction-reason')
    parser.add_argument('--approved-at-utc', help='UTC approval timestamp; defaults to this import, never a future time.')
    args = parser.parse_args()
    with args.report.open('x', encoding='utf-8') as report:
        try:
            result = import_catalog(args.catalog, args.database, expected_catalog_sha256=args.expected_catalog_sha256,
                revision=args.revision, operator=args.operator, apply=args.apply, backup_path=args.backup,
                supersedes=args.supersedes, reason=args.correction_reason, approved_at=args.approved_at_utc)
        except Exception as error:
            report.write(json.dumps(dict(status='Failed', requestedApply=args.apply, error=str(error)), indent=2) + '\n')
            raise
        report.write(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))

"""Preview or append a reviewed year/revision HH separate-payment DB release."""
import argparse
import getpass
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge.hha_separate_payment_reference import import_catalog


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
    args = parser.parse_args()
    with args.report.open('x', encoding='utf-8') as report:
        try:
            result = import_catalog(args.catalog, args.database, expected_catalog_sha256=args.expected_catalog_sha256,
                revision=args.revision, operator=args.operator, apply=args.apply, backup_path=args.backup)
        except Exception as error:
            report.write(json.dumps(dict(status='Failed', requestedApply=args.apply, error=str(error)), indent=2) + '\n')
            raise
        report.write(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))

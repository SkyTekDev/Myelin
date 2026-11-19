"""Check that a Myelin database lets the bridge start with ESRD enabled.

Opens the database read-only, runs the checks the bridge runs at startup and lists
the sealed ESRD reference releases it would serve. Exits non-zero, as startup would
fail, when the reference tables or a sealed release are missing or altered.

    python -m bridge.esrd_check [--database data/myelin.db]
"""
import argparse
import os
from pathlib import Path
import sqlite3
import sys

from sqlalchemy import create_engine

from bridge.esrd_catalog import OPTIONAL, EsrdCatalog

DEFAULT_DATABASE = Path(__file__).resolve().parent.parent / 'data' / 'myelin.db'


def readiness(database):
    path = Path(database).expanduser().resolve()
    if not path.is_file():
        raise ValueError('The Myelin database file was not found.')
    # mode=ro: the check can never change the reference store.
    engine = create_engine('sqlite://', creator=lambda: sqlite3.connect(path.as_uri() + '?mode=ro', uri=True))
    try:
        return EsrdCatalog(engine).readiness()
    finally:
        engine.dispose()


def describe(ready):
    superseded = {row['supersededCatalogSha256'] for row in ready['supersessions']}
    for release in ready['releases']:
        sections = sorted(OPTIONAL & set(release))
        yield ('{effectiveFrom} to {effectiveThrough}  revision {revision}  {referenceVersion}  {catalogSha256}'.format(**release)
            + ('  with ' + ', '.join(sections) if sections else '  base rates only')
            + ('  (superseded)' if release['catalogSha256'] in superseded else ''))


def main(argv=None):
    """Print the releases and return None, or return the reason ESRD cannot be enabled."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--database', type=Path, default=Path(os.getenv('MYELIN_DB_PATH', str(DEFAULT_DATABASE))))
    args = parser.parse_args(argv)
    try:
        ready = readiness(args.database)
    except Exception as error:
        return f'ESRD cannot be enabled with {args.database}: {error}'
    print(f'Sealed ESRD reference releases in {args.database}: {len(ready["releases"])}')
    for line in describe(ready):
        print('  ' + line)


if __name__ == '__main__':
    sys.exit(main())

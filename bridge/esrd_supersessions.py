"""Explicit, append-only ESRD supersession and historical selection rules."""
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit

TABLE = 'esrd_release_supersessions'
SCHEMA = Path(__file__).parent / 'references/esrd-supersessions-schema-v1.sql'


def utc(value):
    if not isinstance(value, str):
        raise ValueError('An explicit UTC timestamp is required.')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None or result.utcoffset().total_seconds() != 0:
        raise ValueError('Timestamp must have a UTC offset.')
    return result


def statements():
    statement = ''
    for line in SCHEMA.read_text(encoding='utf-8').splitlines(True):
        statement += line
        if sqlite3.complete_statement(statement):
            yield statement.strip()
            statement = ''
    if statement.strip():
        raise ValueError('Incomplete ESRD supersession schema.')


def check_schema(connection):
    query = "SELECT name,sql FROM sqlite_master WHERE tbl_name='esrd_release_supersessions' AND type IN ('table','trigger') ORDER BY name"
    actual = dict(connection.execute(query))
    if not actual:
        return False
    with closing(sqlite3.connect(':memory:')) as expected:
        # Foreign keys reference the base release table; DDL creation does not require its rows.
        for statement in statements():
            expected.execute(statement)
        if actual != dict(expected.execute(query)):
            raise ValueError('Unexpected ESRD supersession schema.')
    return True


def read_records(connection):
    if not check_schema(connection):
        return {}
    cursor = connection.execute('SELECT * FROM esrd_release_supersessions')
    names = [column[0] for column in cursor.description]
    return {row[0]: dict(zip(names, row)) for row in cursor}


def releases(connection):
    cursor = connection.execute('SELECT * FROM esrd_rate_releases')
    names = [column[0] for column in cursor.description]
    return {row[0]: dict(zip(names, row)) for row in cursor}


def ancestors(key, records):
    result = set()
    while key in records:
        key = records[key]['superseded_catalog_sha256']
        if key in result:
            raise ValueError('ESRD supersession cycle.')
        result.add(key)
    return result


def validate_graph(loaded, records):
    for key, record in records.items():
        if key not in loaded or record['superseded_catalog_sha256'] not in loaded or key == record['superseded_catalog_sha256']:
            raise ValueError('ESRD supersession must link existing distinct releases.')
        child, parent = loaded[key], loaded[record['superseded_catalog_sha256']]
        source = urlsplit(record['correction_source_url'])
        if (child['payment_year'] != parent['payment_year'] or child['revision'] <= parent['revision']
                or not parent['effective_from'] <= child['effective_from'] <= child['effective_through'] <= parent['effective_through']
                or utc(record['approved_at_utc']) < utc(parent['loaded_at_utc'])
                or utc(record['approved_at_utc']) > utc(child['loaded_at_utc'])
                or not isinstance(record['correction_reason'], str) or not 1 <= len(record['correction_reason'].strip()) <= 500
                or not isinstance(record['approved_by'], str) or not record['approved_by'].strip()
                or source.scheme != 'https' or not (source.hostname == 'cms.gov' or (source.hostname or '').endswith('.cms.gov'))
                or source.username or source.password or source.query or source.fragment):
            raise ValueError('Invalid ESRD supersession scope, revision, approval or provenance.')
        chain = ancestors(key, records)
        if key in chain:
            raise ValueError('ESRD supersession cycle.')
        if record['superseded_catalog_sha256'] in records and utc(record['approved_at_utc']) < utc(records[record['superseded_catalog_sha256']]['approved_at_utc']):
            raise ValueError('A correction cannot precede its predecessor approval.')
    items = list(loaded.items())
    for index, (left_key, left) in enumerate(items):
        for right_key, right in items[index+1:]:
            overlap = left['effective_from'] <= right['effective_through'] and right['effective_from'] <= left['effective_through']
            if overlap and left_key not in ancestors(right_key, records) and right_key not in ancestors(left_key, records):
                raise ValueError('Overlapping ESRD releases require explicit, unambiguous supersession.')


def prepare(connection, document, key, revision, operator, *, supersedes=None, reason=None, approved_at=None):
    loaded, records = releases(connection), read_records(connection)
    validate_graph(loaded, records)
    existing = records.get(key)
    if supersedes is None:
        if reason is not None or approved_at is not None or existing is not None:
            raise ValueError('A correction requires its explicit predecessor and reason.')
        record = None
    else:
        if not re.fullmatch('[a-f0-9]{64}', supersedes) or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
            raise ValueError('Supply the superseded catalog fingerprint and a correction reason.')
        timestamp = approved_at or (existing['approved_at_utc'] if existing else datetime.now(timezone.utc).isoformat())
        if utc(timestamp) > datetime.now(timezone.utc):
            raise ValueError('A correction cannot be approved in the future.')
        record = dict(replacement_catalog_sha256=key, superseded_catalog_sha256=supersedes, approved_at_utc=timestamp,
            correction_reason=reason.strip(), correction_source_url=document['sources']['policySourceUrl'], approved_by=operator)
        if existing:
            if any(existing[name] != record[name] for name in ('superseded_catalog_sha256','approved_at_utc','correction_reason','correction_source_url')):
                raise ValueError('An immutable correction declaration already exists with different contents.')
            record = existing
    if key not in loaded:
        loaded[key] = dict(catalog_sha256=key, payment_year=document['paymentYear'], revision=revision,
            effective_from=document['effectiveFrom'], effective_through=document['effectiveThrough'], loaded_at_utc=datetime.now(timezone.utc).isoformat())
        if record:
            records[key] = record
        validate_graph(loaded, records)
    elif record and not existing:
        raise ValueError('An existing release cannot be retroactively declared a correction.')
    return record


def install(connection, record):
    if record is None:
        return
    if not check_schema(connection):
        for statement in statements():
            connection.execute(statement)
    connection.execute('INSERT INTO esrd_release_supersessions VALUES (?,?,?,?,?,?)', tuple(record[name] for name in
        ('replacement_catalog_sha256','superseded_catalog_sha256','approved_at_utc','correction_reason','correction_source_url','approved_by')))


def select(loaded, records, service_date, *, as_of=None, pinned=None):
    if pinned is not None:
        row = loaded.get(pinned)
        if row and row['effective_from'] <= service_date.isoformat() <= row['effective_through']:
            return pinned
        raise ValueError('The pinned ESRD catalog does not cover this service date.')
    instant = utc(as_of) if as_of else datetime.now(timezone.utc)
    candidates = {key for key, row in loaded.items() if row['effective_from'] <= service_date.isoformat() <= row['effective_through']
        and utc(row['loaded_at_utc']) <= instant
        and (key not in records or utc(records[key]['approved_at_utc']) <= instant)}
    superseded = set()
    for key in candidates:
        superseded.update(ancestors(key, records))
    matches = candidates - superseded
    if len(matches) != 1:
        raise ValueError('No unique approved ESRD reference covers this service date and selection.')
    return matches.pop()


def evidence(record):
    if record is None:
        return None
    return dict(replacementCatalogSha256=record['replacement_catalog_sha256'], supersededCatalogSha256=record['superseded_catalog_sha256'],
        approvedAtUtc=record['approved_at_utc'], correctionReason=record['correction_reason'], correctionSourceUrl=record['correction_source_url'])

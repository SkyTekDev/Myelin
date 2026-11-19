"""Effective-dated ESRD rates shared through Myelin's immutable reference DB."""
from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit

from bridge.immutable_reference import import_versioned_catalog
from bridge import esrd_supersessions as supersessions
from bridge import esrd_body_size as body_size
from bridge import esrd_facility_adjustments as facility
from bridge import esrd_final_payment as final_payment

SCHEMA = Path(__file__).parent / 'references/esrd-rates-schema-v1.sql'
TABLES = {'esrd_rate_releases', 'esrd_base_rates', 'esrd_age_factors', 'esrd_wage_indexes', 'esrd_release_supersessions',
          body_size.TABLE, facility.TABLE, final_payment.TABLE}
# Sections a release may omit; an older package without them keeps its original fingerprint.
OPTIONAL = {'bodySize', 'facilityAdjustments', 'finalPayment'}
PARAMETERS = ('baseRate', 'laborShare', 'nonLaborShare', 'wageFloor', 'wageCapMultiplier')
SOURCES = {'jarName', 'jarSha256', 'wageTableName', 'wageTableSha256', 'policySourceUrl', 'providerSourceUrl'}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode('ascii')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate ESRD reference field.')
        result[key] = value
    return result


def exact(value):
    if not isinstance(value, str) or not re.fullmatch(r'(?:0|[1-9][0-9]*)(?:\.[0-9]{1,9})?', value):
        raise ValueError('ESRD references require exact decimal strings.')
    return Decimal(value)


def normalized(value):
    return format(Decimal(value).normalize(), 'f')


def validate_source(key, value):
    if not isinstance(value, str) or not value or not value.isascii():
        raise ValueError('Invalid ESRD source metadata.')
    if key.endswith('Sha256') and not re.fullmatch('[a-f0-9]{64}', value):
        raise ValueError('Invalid ESRD source fingerprint.')
    if key.endswith('Url'):
        url = urlsplit(value)
        if (url.scheme != 'https' or not (url.hostname == 'cms.gov' or (url.hostname or '').endswith('.cms.gov'))
                or url.username or url.password or url.query or url.fragment or url.port not in (None, 443)):
            raise ValueError('ESRD provenance requires a CMS HTTPS source.')
    if key.endswith('Name') and not re.fullmatch('[A-Za-z0-9_.-]{1,150}', value):
        raise ValueError('Invalid ESRD source filename.')


def validate_document(raw, expected_sha256):
    if (not re.fullmatch('[a-f0-9]{64}', expected_sha256) or not 0 < len(raw) <= 2*1024*1024
            or hashlib.sha256(raw).hexdigest() != expected_sha256):
        raise ValueError('ESRD catalog checksum mismatch.')
    doc = json.loads(raw, object_pairs_hook=unique)
    if set(doc) - OPTIONAL != {'schemaVersion','paymentYear','effectiveFrom','effectiveThrough','referenceVersion','algorithm','sources','parameters','ageFactors','entries'}:
        raise ValueError('Unexpected ESRD reference fields.')
    year = doc['paymentYear']
    if (doc['schemaVersion'] != 'ESRDReferenceRates/1' or doc['algorithm'] != 'BASE_WAGE_ADULT_AGE_V1'
            or type(year) is not int or not 2000 <= year <= 2100 or canonical(doc) != raw
            or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', doc['effectiveFrom'])
            or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', doc['effectiveThrough'])
            or not date(year, 1, 1) <= date.fromisoformat(doc['effectiveFrom']) <= date.fromisoformat(doc['effectiveThrough']) <= date(year, 12, 31)
            or not re.fullmatch('[A-Za-z0-9_.-]{1,100}', doc['referenceVersion'])):
        raise ValueError('Invalid ESRD release version or effective period.')
    if set(doc['sources']) != SOURCES or set(doc['parameters']) != set(PARAMETERS):
        raise ValueError('Missing ESRD provenance or rate parameters.')
    for key, value in doc['sources'].items():
        validate_source(key, value)
    if 'bodySize' in doc:
        body_size.validate(doc['bodySize'], exact, validate_source)
    if 'facilityAdjustments' in doc:
        facility.validate(doc['facilityAdjustments'], exact)
    if 'finalPayment' in doc:
        final_payment.validate(doc['finalPayment'], exact, doc)
    p = {key: exact(value) for key, value in doc['parameters'].items()}
    if (not 0 < p['baseRate'] <= 10000 or not 0 < p['laborShare'] < 1 or not 0 < p['nonLaborShare'] < 1
            or p['laborShare'] + p['nonLaborShare'] != 1 or not 0 < p['wageFloor'] <= 5
            or not 0 < p['wageCapMultiplier'] <= 1):
        raise ValueError('Invalid ESRD base, share or wage factors.')
    next_age = 18
    if not isinstance(doc['ageFactors'], list) or not doc['ageFactors']:
        raise ValueError('Missing ESRD adult age factors.')
    for band in doc['ageFactors']:
        if (set(band) != {'minimumAge','maximumAge','factor'} or type(band['minimumAge']) is not int
                or type(band['maximumAge']) is not int or band['minimumAge'] != next_age
                or not next_age <= band['maximumAge'] <= 124 or not 0 < exact(band['factor']) <= 10):
            raise ValueError('ESRD age factors overlap, have gaps, or contain invalid values.')
        next_age = band['maximumAge'] + 1
    if next_age != 125:
        raise ValueError('ESRD age factors must cover the supported adult ages.')
    if not isinstance(doc['entries'], dict) or not 0 < len(doc['entries']) <= 10000:
        raise ValueError('Missing ESRD wage references.')
    for cbsa, row in doc['entries'].items():
        if (not re.fullmatch(r'(?:[0-9]{2}|[0-9]{5})', cbsa) or set(row) != {'cbsa','wageIndex'}
                or row['cbsa'] != cbsa or not 0 < exact(row['wageIndex']) <= 5):
            raise ValueError('Invalid ESRD wage row.')
    return doc


def read_package(path, expected_sha256):
    return validate_document(Path(path).read_bytes(), expected_sha256)


def policy(document, revision, catalog_sha256):
    result = dict(schemaVersion='ESRDRateSet/1', algorithm=document['algorithm'], paymentYear=document['paymentYear'], revision=revision,
        effectiveFrom=document['effectiveFrom'], effectiveThrough=document['effectiveThrough'], referenceVersion=document['referenceVersion'],
        **{key: normalized(value) for key, value in document['parameters'].items()},
        ageFactors=[dict(minimumAge=row['minimumAge'], maximumAge=row['maximumAge'], factor=normalized(row['factor'])) for row in document['ageFactors']],
        **document['sources'])
    if 'bodySize' in document:
        result['bodySize'] = {key: normalized(value) if key in body_size.NUMBERS else value
                              for key, value in document['bodySize'].items()}
    if 'facilityAdjustments' in document:
        result['facilityAdjustments'] = facility.normalize(document['facilityAdjustments'], normalized)
    if 'finalPayment' in document:
        result['finalPayment'] = final_payment.normalize(document['finalPayment'], normalized)
    return dict(result, catalogSha256=catalog_sha256, policySha256=digest(result))


def statements():
    statement = ''
    for line in SCHEMA.read_text(encoding='utf-8').splitlines(True):
        statement += line
        if sqlite3.complete_statement(statement):
            yield statement.strip()
            statement = ''
    if statement.strip():
        raise ValueError('Incomplete ESRD reference schema.')


def schema(connection):
    return dict(connection.execute("SELECT name,sql FROM sqlite_master WHERE tbl_name IN ('esrd_rate_releases','esrd_base_rates','esrd_age_factors','esrd_wage_indexes') AND type IN ('table','trigger') ORDER BY name"))


def check_schema(connection):
    body_size.check_schema(connection)
    facility.check_schema(connection)
    final_payment.check_schema(connection)
    existing = schema(connection)
    if not existing:
        return False
    with closing(sqlite3.connect(':memory:')) as expected:
        for statement in statements():
            expected.execute(statement)
        if existing != schema(expected):
            raise ValueError('Unexpected or incomplete ESRD reference schema.')
    return True


def rows(cursor):
    return [dict(zip([column[0] for column in cursor.description], row)) for row in cursor]


def read_release(connection, catalog_sha256):
    releases = rows(connection.execute('SELECT * FROM esrd_rate_releases WHERE catalog_sha256=?', (catalog_sha256,)))
    if len(releases) != 1 or releases[0]['sealed'] != 1:
        raise ValueError('ESRD reference release is missing or unsealed.')
    release = releases[0]
    bases = connection.execute('SELECT base_rate_exact,labor_share_exact,non_labor_share_exact,wage_floor_exact,wage_cap_multiplier_exact FROM esrd_base_rates WHERE catalog_sha256=?', (catalog_sha256,)).fetchall()
    if len(bases) != 1:
        raise ValueError('ESRD base-rate row is missing or ambiguous.')
    bands = [dict(minimumAge=minimum, maximumAge=maximum, factor=factor) for minimum, maximum, factor in connection.execute(
        'SELECT minimum_age,maximum_age,factor_exact FROM esrd_age_factors WHERE catalog_sha256=? ORDER BY minimum_age', (catalog_sha256,))]
    entries = {cbsa: dict(cbsa=cbsa, wageIndex=wage) for cbsa, wage in connection.execute(
        'SELECT cbsa,wage_index_exact FROM esrd_wage_indexes WHERE catalog_sha256=? ORDER BY cbsa', (catalog_sha256,))}
    doc = dict(schemaVersion=release['schema_version'], paymentYear=release['payment_year'], effectiveFrom=release['effective_from'],
        effectiveThrough=release['effective_through'], referenceVersion=release['reference_version'], algorithm=release['algorithm'],
        sources=json.loads(release['sources_json']), parameters=dict(zip(PARAMETERS, bases[0])), ageFactors=bands, entries=entries)
    parameters = body_size.read(connection, catalog_sha256)
    if parameters is not None:
        doc['bodySize'] = parameters
    factors = facility.read(connection, catalog_sha256)
    if factors is not None:
        doc['facilityAdjustments'] = factors
    amounts = final_payment.read(connection, catalog_sha256)
    if amounts is not None:
        doc['finalPayment'] = amounts
    validate_document(canonical(doc), catalog_sha256)
    if len(entries) != release['rate_count'] or policy(doc, release['revision'], catalog_sha256)['policySha256'] != release['policy_sha256']:
        raise ValueError('ESRD row count or policy fingerprint mismatch.')
    return doc, release


def install_release(connection, document, catalog_sha256, revision, operator, *, supersedes=None, reason=None, approved_at=None):
    validate_document(canonical(document), catalog_sha256)
    if not check_schema(connection):
        for statement in statements():
            connection.execute(statement)
    correction = supersessions.prepare(connection, document, catalog_sha256, revision, operator,
        supersedes=supersedes, reason=reason, approved_at=approved_at)
    connection.execute('INSERT INTO esrd_rate_releases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (
        catalog_sha256, document['schemaVersion'], document['paymentYear'], revision, document['effectiveFrom'], document['effectiveThrough'],
        document['referenceVersion'], document['algorithm'], canonical(document['sources']).decode(), policy(document, revision, catalog_sha256)['policySha256'],
        len(document['entries']), datetime.now(timezone.utc).isoformat(), operator, 0))
    connection.execute('INSERT INTO esrd_base_rates VALUES (?,?,?,?,?,?)', (catalog_sha256, *(document['parameters'][key] for key in PARAMETERS)))
    connection.executemany('INSERT INTO esrd_age_factors VALUES (?,?,?,?)', [
        (catalog_sha256, row['minimumAge'], row['maximumAge'], row['factor']) for row in document['ageFactors']])
    connection.executemany('INSERT INTO esrd_wage_indexes VALUES (?,?,?)', [
        (catalog_sha256, cbsa, row['wageIndex']) for cbsa, row in document['entries'].items()])
    body_size.install(connection, catalog_sha256, document.get('bodySize'))
    facility.install(connection, catalog_sha256, document.get('facilityAdjustments'))
    final_payment.install(connection, catalog_sha256, document.get('finalPayment'))
    connection.execute('UPDATE esrd_rate_releases SET sealed=1 WHERE catalog_sha256=?', (catalog_sha256,))
    supersessions.install(connection, correction)
    supersessions.validate_graph(supersessions.releases(connection), supersessions.read_records(connection))
    if read_release(connection, catalog_sha256)[0] != document:
        raise ValueError('ESRD DB release differs from its reviewed package.')


def import_catalog(catalog_path, database, *, expected_catalog_sha256, revision, operator, apply=False, backup_path=None,
        supersedes=None, reason=None, approved_at=None):
    def checked_schema(connection):
        exists = check_schema(connection)
        if exists:
            supersessions.prepare(connection, read_package(catalog_path, expected_catalog_sha256), expected_catalog_sha256,
                revision, operator, supersedes=supersedes, reason=reason, approved_at=approved_at)
        return exists
    def installer(connection, document, catalog_sha256, revision, operator):
        install_release(connection, document, catalog_sha256, revision, operator, supersedes=supersedes, reason=reason, approved_at=approved_at)
    return import_versioned_catalog(catalog_path, database, expected_catalog_sha256=expected_catalog_sha256,
        revision=revision, operator=operator, apply=apply, backup_path=backup_path, read_package=read_package,
        check_schema=checked_schema, read_release=read_release, install_release=installer,
        tables=TABLES, release_table='esrd_rate_releases')


class EsrdCatalog:
    def __init__(self, engine):
        if engine.dialect.name != 'sqlite':
            raise ValueError('ESRD references require the prepared Myelin SQLite store.')
        self.releases = []
        self.documents = {}
        with engine.connect() as connection:
            connection.exec_driver_sql('BEGIN')
            raw = connection.connection.driver_connection
            if not check_schema(raw):
                raise ValueError('Load the ESRD reference tables before enabling ESRD.')
            for (catalog_sha256,) in raw.execute('SELECT catalog_sha256 FROM esrd_rate_releases ORDER BY effective_from'):
                doc, release = read_release(raw, catalog_sha256)
                self.releases.append((doc, policy(doc, release['revision'], catalog_sha256)))
                self.documents[catalog_sha256] = self.releases[-1]
            self.loaded = supersessions.releases(raw)
            self.supersessions = supersessions.read_records(raw)
            supersessions.validate_graph(self.loaded, self.supersessions)
        if not self.releases:
            raise ValueError('No sealed ESRD reference releases are loaded.')

    def for_date(self, service_date, *, as_of=None, pinned=None):
        key = supersessions.select(self.loaded, self.supersessions, service_date, as_of=as_of, pinned=pinned)
        return self.documents[key]

    def readiness(self):
        return dict(status='ready', isBenchmarkOnly=True, referenceStore='MyelinSQLite',
            releases=[rates for _, rates in self.releases], supersessions=[supersessions.evidence(row) for row in self.supersessions.values()])

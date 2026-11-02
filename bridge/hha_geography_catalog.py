"""Reviewed geography packages and verified, per-processor DB snapshots."""
from copy import deepcopy
from datetime import date
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from bridge.hha_provider import HhaInputError
from bridge.hha_provider_annual import canonical_json

CATALOG_FILENAME = 'hha-geography-2026-v1.json'
CATALOG_SHA256 = 'b4d5e582fa73d2eb15f6ee02781ad8bfccab9db8a780eba006df3b0d5063688d'
# A load does not activate a release. Pricing support and both registries need review.
APPROVED_RELEASES = {2026: (1, CATALOG_SHA256)}
ENTRY_FIELDS = {'serviceCountyFips', 'countyName', 'stateFips', 'stateAbbreviation', 'status',
                'baseWageIndexCode', 'cmsWageIndexCode', 'wageIndex', 'sourceRows'}
SOURCE_ROW_FIELDS = {'county', 'census', 'hhUrban', 'transition', 'wage'}


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Repeated geography reference field.')
        result[key] = value
    return result


def validate_document(raw, expected_sha256):
    """Validate an explicitly reviewed import package, including future years."""
    if (not isinstance(expected_sha256, str) or not re.fullmatch('[a-f0-9]{64}', expected_sha256)
            or not 0 < len(raw) <= 8 * 1024 * 1024
            or hashlib.sha256(raw).hexdigest() != expected_sha256):
        raise ValueError('HH geography catalog checksum mismatch.')
    doc = json.loads(raw, object_pairs_hook=_unique)
    if not isinstance(doc, dict) or set(doc) != {
            'schemaVersion', 'paymentYear', 'effectiveFrom', 'effectiveThrough', 'sources', 'entries'}:
        raise ValueError('Invalid geography catalog fields.')
    year = doc['paymentYear']
    if (doc['schemaVersion'] != 'HHAGeographyCatalog/1' or type(year) is not int or not 2000 <= year <= 2100
            or doc['effectiveFrom'] != date(year, 1, 1).isoformat()
            or doc['effectiveThrough'] != date(year, 12, 31).isoformat()
            or canonical_json(doc) != raw):
        raise ValueError('Invalid geography catalog version, dates or canonical encoding.')
    sources = doc['sources']
    if not isinstance(sources, dict) or set(sources) != {
            'countyRegister', 'censusDelineation', 'hhWageIndex', 'hhCountyWorkbook', 'policy'}:
        raise ValueError('Missing geography source provenance.')
    for name, source in sources.items():
        fields = {'sourceUrl', 'sourceSha256'}
        if name in {'countyRegister', 'hhWageIndex', 'hhCountyWorkbook'}:
            fields |= {'memberName', 'memberSha256'}
        if not isinstance(source, dict) or set(source) != fields or not all(isinstance(v, str) for v in source.values()):
            raise ValueError('Invalid geography source fields.')
        url = urlsplit(source['sourceUrl'])
        if (url.scheme != 'https' or url.netloc not in {'www.cms.gov', 'www2.census.gov', 'www.census.gov'}
                or url.query or url.fragment or not re.fullmatch('[a-f0-9]{64}', source['sourceSha256'])
                or ('memberSha256' in source and (not source['memberName']
                    or not re.fullmatch('[a-f0-9]{64}', source['memberSha256'])))):
            raise ValueError('Invalid geography source URL or digest.')
    if not isinstance(doc['entries'], dict) or not 0 < len(doc['entries']) <= 10000:
        raise ValueError('Invalid geography county coverage.')
    for county, row in doc['entries'].items():
        if (not re.fullmatch('[0-9]{5}', county) or not isinstance(row, dict) or set(row) != ENTRY_FIELDS
                or row['serviceCountyFips'] != county or row['stateFips'] != county[:2]
                or not isinstance(row['countyName'], str) or not row['countyName']
                or not isinstance(row['stateAbbreviation'], str) or not re.fullmatch('[A-Z]{2}', row['stateAbbreviation'])
                or row['status'] not in {'Verified', 'SourceConflict'}
                or any(not isinstance(row[key], str) or not re.fullmatch('[0-9]{5}', row[key])
                       for key in ('baseWageIndexCode', 'cmsWageIndexCode'))
                or not isinstance(row['wageIndex'], str) or not re.fullmatch(r'[0-9]+\.[0-9]{4}', row['wageIndex'])
                or Decimal(row['wageIndex']) <= 0
                or not isinstance(row['sourceRows'], dict) or set(row['sourceRows']) != SOURCE_ROW_FIELDS
                or any(value is not None and (type(value) is not int or value <= 0) for value in row['sourceRows'].values())
                or row['sourceRows']['county'] is None or row['sourceRows']['wage'] is None):
            raise ValueError('Invalid reviewed county row.')
    return doc


def read_catalog_package(path, expected_sha256):
    return validate_document(Path(path).read_bytes(), expected_sha256)


class GeographyCatalog:
    """One immutable, hash-checked release cached by its owning processor."""
    def __init__(self, document, sha256, revision=1):
        self._document = validate_document(canonical_json(document), sha256)
        self.sha256 = sha256
        self._reference_info = dict(store='MyelinDatabase', paymentYear=document['paymentYear'], revision=revision,
            catalogSha256=sha256, countyCount=len(document['entries']),
            verifiedCountyCount=sum(row['status'] == 'Verified' for row in document['entries'].values()))

    @property
    def reference_info(self):
        return dict(self._reference_info)

    @property
    def document(self):
        return deepcopy(self._document)

    def county_entry(self, county):
        row = self._document['entries'].get(county)
        if row is None:
            raise HhaInputError('HHA_COUNTY_NOT_FOUND', 'The service county is not in the reviewed county register.')
        if row['status'] != 'Verified':
            raise HhaInputError('HHA_GEOGRAPHY_SOURCE_CONFLICT', 'Published county/area sources conflict for this county; no wage area is accepted.')
        return deepcopy(row)

    def mapping_evidence(self, county):
        row = self.county_entry(county)
        return dict(schemaVersion='HHACountyMappingEvidence/1', paymentYear=self._document['paymentYear'],
            serviceCountyFips=county, catalogSha256=self.sha256,
            catalogEntrySha256=hashlib.sha256(canonical_json(row)).hexdigest(),
            cmsWageIndexCode=row['cmsWageIndexCode'], wageIndex=row['wageIndex'],
            effectiveFrom=self._document['effectiveFrom'], effectiveThrough=self._document['effectiveThrough'],
            sources=deepcopy(self._document['sources']), sourceRows=row['sourceRows'])

    def validate_rates(self, wage_rows):
        for row in self._document['entries'].values():
            if row['status'] != 'Verified':
                continue
            source = wage_rows.get(row['cmsWageIndexCode'])
            if not isinstance(source, dict):
                raise HhaInputError('HHA_WAGE_REFERENCE_MISSING', 'A reviewed county wage row is missing from the HHA JAR.')
            if (str(source.get('cbsa')) != row['cmsWageIndexCode']
                    or str(source.get('effectiveDate')) != str(self._document['paymentYear']) + '0101'
                    or Decimal(str(source.get('geographicWageIndex'))) != Decimal(row['wageIndex'])):
                raise HhaInputError('HHA_WAGE_REFERENCE_MISMATCH', 'The HHA JAR wage rows disagree with the reviewed county wage reference.')


def load_catalog(engine, payment_year=2026):
    """Read the approved DB release once; no file fallback or runtime writes."""
    from bridge.hha_geography_reference import read_release, check_schema
    try:
        revision, digest = APPROVED_RELEASES[payment_year]
        if engine.dialect.name != 'sqlite':
            raise ValueError('The HH geography reference store requires SQLite.')
        with engine.connect() as connection:
            connection.exec_driver_sql('BEGIN')
            raw = connection.connection.driver_connection
            if not check_schema(raw):
                raise ValueError('HH geography schema is not loaded.')
            document, release = read_release(raw, digest)
            if release['payment_year'] != payment_year or release['revision'] != revision:
                raise ValueError('HH geography version is not approved.')
        return GeographyCatalog(document, digest, revision)
    except Exception:
        raise HhaInputError('HHA_GEOGRAPHY_REFERENCE_INVALID',
            'The approved HH geography DB release is missing or invalid; run the reviewed loader before enabling HH.') from None

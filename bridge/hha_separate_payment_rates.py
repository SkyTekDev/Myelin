"""Reviewed separate-payment rate packages and immutable DB snapshots."""
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

CATALOG_FILENAME = 'hha-separate-payment-rates-2026-v1.json'
CATALOG_SHA256 = '9c952692c6c27afb4c2357e9b417f0333c6de18e67968097655fd7f3f3e4a67e'
APPROVED_RELEASES = {2025: (1, '90c05423f8c13a82fc5cb03f5440395c1f39eb89b4fc8b3f2bf4f2a7af065e94'), 2026: (1, CATALOG_SHA256)}


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate separate-payment reference field.')
        value[key] = item
    return value


def validate_document(raw, expected_sha256):
    if (not re.fullmatch('[a-f0-9]{64}', expected_sha256) or not 0 < len(raw) <= 1024 * 1024
            or hashlib.sha256(raw).hexdigest() != expected_sha256):
        raise ValueError('Separate-payment catalog checksum mismatch.')
    doc = json.loads(raw, object_pairs_hook=_unique)
    if set(doc) != {'schemaVersion','paymentYear','effectiveFrom','effectiveThrough','sources','entries'}:
        raise ValueError('Invalid separate-payment catalog fields.')
    year = doc['paymentYear']
    if (doc['schemaVersion'] != 'HHASeparatePaymentRates/1' or type(year) is not int or not 2000 <= year <= 2100
            or doc['effectiveFrom'] != date(year,1,1).isoformat() or doc['effectiveThrough'] != date(year,12,31).isoformat()
            or canonical_json(doc) != raw or not isinstance(doc['entries'],dict) or not 0 < len(doc['entries']) <= 1000
            or set(doc['sources']) != {'rate','billing','coinsurance','deductible'}):
        raise ValueError('Invalid separate-payment catalog version, dates or coverage.')
    for source in doc['sources'].values():
        if set(source) != {'sourceUrl','sourceSha256','rowLocator'} or not all(isinstance(value,str) and value for value in source.values()):
            raise ValueError('Invalid rate source provenance.')
        url = urlsplit(source['sourceUrl'])
        if (url.scheme != 'https' or url.netloc not in {'www.cms.gov','www.govinfo.gov'} or url.query or url.fragment
                or not re.fullmatch('[a-f0-9]{64}',source['sourceSha256'])):
            raise ValueError('Unreviewed rate source URL or fingerprint.')
    for code,row in doc['entries'].items():
        if (not re.fullmatch('[A-Z][0-9]{4}',code) or set(row) != {'hcpcsCode','unitRate','coinsuranceRate','annualPartBDeductible'}
                or row['hcpcsCode'] != code
                or any(not isinstance(row[key],str) or not re.fullmatch(r'[0-9]+\.[0-9]{2}',row[key])
                       for key in ('unitRate','coinsuranceRate','annualPartBDeductible'))
                or Decimal(row['unitRate']) <= 0 or not 0 <= Decimal(row['coinsuranceRate']) <= 1
                or Decimal(row['annualPartBDeductible']) < 0):
            raise ValueError('Invalid exact separate-payment rate row.')
    return doc


def read_package(path, digest):
    return validate_document(Path(path).read_bytes(), digest)


class SeparatePaymentRates:
    def __init__(self, document, digest, revision):
        self._document = validate_document(canonical_json(document), digest)
        self.sha256, self.revision = digest, revision

    def a9272_evidence(self):
        row = self._document['entries']['A9272']
        return dict(schemaVersion='HHAA9272RateEvidence/1', paymentYear=self._document['paymentYear'], revision=self.revision,
            catalogSha256=self.sha256, catalogEntrySha256=hashlib.sha256(canonical_json(row)).hexdigest(),
            effectiveFrom=self._document['effectiveFrom'], effectiveThrough=self._document['effectiveThrough'],
            **row, sources=deepcopy(self._document['sources']))


def load_rates(engine, payment_year=2026):
    from bridge.hha_separate_payment_reference import check_schema, read_release
    try:
        revision,digest = APPROVED_RELEASES[payment_year]
        if engine.dialect.name != 'sqlite':
            raise ValueError('HH separate-payment references require SQLite.')
        with engine.connect() as connection:
            connection.exec_driver_sql('BEGIN')
            raw = connection.connection.driver_connection
            if not check_schema(raw):
                raise ValueError('Separate-payment schema is not loaded.')
            document,release = read_release(raw,digest)
            if release['payment_year'] != payment_year or release['revision'] != revision or 'A9272' not in document['entries']:
                raise ValueError('The rate release is not approved.')
        return SeparatePaymentRates(document,digest,revision)
    except Exception:
        raise HhaInputError('HHA_A9272_RATE_REFERENCE_INVALID',
            'The approved A9272 database rate is missing or invalid; load the reviewed rate release before pricing devices.') from None

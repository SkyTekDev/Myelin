"""Optional, sealed body-size parameters belonging to an ESRD rate release."""
from contextlib import closing
from pathlib import Path
import sqlite3

TABLE = 'esrd_body_size_parameters'
SCHEMA = Path(__file__).parent / 'references/esrd-body-size-schema-v1.sql'
NUMBERS = ('bsaNationalAverage', 'bsaMultiplier', 'lowBmiThreshold', 'lowBmiMultiplier',
           'maximumHeightCm', 'maximumWeightKg', 'maximumBmi')
FIELDS = ('algorithm', 'sourceUrl', 'sourceSha256', *NUMBERS)


def validate(value, exact, source):
    if not isinstance(value, dict) or set(value) != set(FIELDS):
        raise ValueError('Incomplete ESRD body-size parameters.')
    if value['algorithm'] != 'CMS_BSA_BMI_9_DIGIT_V1':
        raise ValueError('Unsupported ESRD body-size algorithm.')
    source('bodySizeSourceUrl', value['sourceUrl'])
    source('bodySizeSourceSha256', value['sourceSha256'])
    p = {name: exact(value[name]) for name in NUMBERS}
    if (not 0 < p['bsaNationalAverage'] <= 5 or not 1 <= p['bsaMultiplier'] <= 2
            or not 0 < p['lowBmiThreshold'] <= 100 or not 1 <= p['lowBmiMultiplier'] <= 2
            or not 0 < p['maximumHeightCm'] <= 1000 or not 0 < p['maximumWeightKg'] <= 1000
            or not p['lowBmiThreshold'] < p['maximumBmi'] <= 10000):
        raise ValueError('Invalid ESRD body-size parameters.')


def statements():
    statement = ''
    for line in SCHEMA.read_text(encoding='utf-8').splitlines(True):
        statement += line
        if sqlite3.complete_statement(statement):
            yield statement.strip()
            statement = ''
    if statement.strip():
        raise ValueError('Incomplete ESRD body-size schema.')


def check_schema(connection):
    query = "SELECT name,sql FROM sqlite_master WHERE tbl_name='esrd_body_size_parameters' AND type IN ('table','trigger') ORDER BY name"
    actual = dict(connection.execute(query))
    if not actual:
        return False
    with closing(sqlite3.connect(':memory:')) as expected:
        for statement in statements():
            expected.execute(statement)
        if actual != dict(expected.execute(query)):
            raise ValueError('Unexpected ESRD body-size schema.')
    return True


def read(connection, key):
    if not check_schema(connection):
        return None
    row = connection.execute('SELECT algorithm,source_url,source_sha256,bsa_national_average_exact,bsa_multiplier_exact,'
        'low_bmi_threshold_exact,low_bmi_multiplier_exact,maximum_height_cm_exact,maximum_weight_kg_exact,maximum_bmi_exact '
        'FROM esrd_body_size_parameters WHERE catalog_sha256=?', (key,)).fetchone()
    return dict(zip(FIELDS, row)) if row else None


def install(connection, key, parameters):
    if parameters is None:
        return
    if not check_schema(connection):
        for statement in statements():
            connection.execute(statement)
    connection.execute('INSERT INTO esrd_body_size_parameters VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (key, *(parameters[name] for name in FIELDS)))

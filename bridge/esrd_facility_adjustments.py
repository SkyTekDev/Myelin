"""Optional, sealed facility-level payment factors belonging to an ESRD rate release.

The factors are the ones the CMS ESRD pricer applies from the facility's provider record alone:
the rural multiplier, the formerly rural transition multiplier, the two low-volume tiers, the
non-contiguous-area factor on the non-labor amount (keyed by the CCN's state code) and the
quality (QIP) reduction (keyed by the provider record's quality indicator).
"""
from contextlib import closing
from decimal import Decimal
import json
from pathlib import Path
import re
import sqlite3

TABLE = 'esrd_facility_adjustments'
SCHEMA = Path(__file__).parent / 'references/esrd-facility-adjustments-schema-v1.sql'
ALGORITHM = 'CMS_FACILITY_LEVEL_V1'
MULTIPLIERS = ('ruralMultiplier', 'ruralTransitionMultiplier', 'lowVolumeTier1Multiplier', 'lowVolumeTier2Multiplier')
# List name, its key field, the key pattern and the inclusive factor range.
LISTS = (('nonContiguousNonLaborFactors', 'stateCode', '[0-9]{2}', Decimal(1), Decimal(2)),
         ('qualityReductionFactors', 'indicator', '[1-9]', Decimal('0.5'), Decimal(1)))
FIELDS = ('algorithm', *MULTIPLIERS, *(name for name, *_ in LISTS))


def validate(value, exact):
    if not isinstance(value, dict) or set(value) != set(FIELDS):
        raise ValueError('Incomplete ESRD facility-level adjustment parameters.')
    if value['algorithm'] != ALGORITHM:
        raise ValueError('Unsupported ESRD facility-level adjustment algorithm.')
    if any(not 1 <= exact(value[name]) <= 2 for name in MULTIPLIERS):
        raise ValueError('Invalid ESRD rural or low-volume multiplier.')
    for name, key, pattern, minimum, maximum in LISTS:
        rows = value[name]
        if not isinstance(rows, list) or len(rows) > 100:
            raise ValueError('Invalid ESRD facility-level factor list.')
        keys = []
        for row in rows:
            if (not isinstance(row, dict) or set(row) != {key, 'factor'} or not isinstance(row[key], str)
                    or not re.fullmatch(pattern, row[key]) or not minimum <= exact(row['factor']) <= maximum):
                raise ValueError('Invalid ESRD facility-level factor row.')
            keys.append(row[key])
        # One canonical order keeps the package fingerprint independent of how a list was typed.
        if keys != sorted(set(keys)):
            raise ValueError('ESRD facility-level factors must be unique and in ascending key order.')


def normalize(value, normalized):
    result = {name: normalized(value[name]) for name in MULTIPLIERS}
    for name, key, *_ in LISTS:
        result[name] = [{key: row[key], 'factor': normalized(row['factor'])} for row in value[name]]
    return dict(result, algorithm=value['algorithm'])


def statements():
    statement = ''
    for line in SCHEMA.read_text(encoding='utf-8').splitlines(True):
        statement += line
        if sqlite3.complete_statement(statement):
            yield statement.strip()
            statement = ''
    if statement.strip():
        raise ValueError('Incomplete ESRD facility-level adjustment schema.')


def check_schema(connection):
    query = "SELECT name,sql FROM sqlite_master WHERE tbl_name='esrd_facility_adjustments' AND type IN ('table','trigger') ORDER BY name"
    actual = dict(connection.execute(query))
    if not actual:
        return False
    with closing(sqlite3.connect(':memory:')) as expected:
        for statement in statements():
            expected.execute(statement)
        if actual != dict(expected.execute(query)):
            raise ValueError('Unexpected ESRD facility-level adjustment schema.')
    return True


def read(connection, key):
    if not check_schema(connection):
        return None
    row = connection.execute('SELECT algorithm,rural_multiplier_exact,rural_transition_multiplier_exact,'
        'low_volume_tier1_multiplier_exact,low_volume_tier2_multiplier_exact,non_contiguous_non_labor_factors_json,'
        'quality_reduction_factors_json FROM esrd_facility_adjustments WHERE catalog_sha256=?', (key,)).fetchone()
    if not row:
        return None
    result = dict(zip(FIELDS, row))
    for name, *_ in LISTS:
        result[name] = json.loads(result[name])
    return result


def install(connection, key, parameters):
    if parameters is None:
        return
    if not check_schema(connection):
        for statement in statements():
            connection.execute(statement)
    connection.execute('INSERT INTO esrd_facility_adjustments VALUES (?,?,?,?,?,?,?,?)', (key, *(
        json.dumps(parameters[name], sort_keys=True, ensure_ascii=True, separators=(',', ':'))
        if isinstance(parameters[name], list) else parameters[name] for name in FIELDS)))

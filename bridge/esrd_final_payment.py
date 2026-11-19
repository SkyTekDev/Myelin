"""Optional, sealed final-payment amounts belonging to an ESRD rate release.

These are the two amounts the CMS ESRD pricer applies to every treatment after the adjusted base:
the post-TDAPA add-on, a per-treatment amount that changes on dated boundaries within the year,
and the ESRD network reduction.
"""
from contextlib import closing
from datetime import date, timedelta
import json
from pathlib import Path
import re
import sqlite3

TABLE = 'esrd_final_payment'
SCHEMA = Path(__file__).parent / 'references/esrd-final-payment-schema-v1.sql'
ALGORITHM = 'CMS_FINAL_PAYMENT_V1'
FIELDS = ('algorithm', 'networkReductionAmount', 'postTdapaAddOns')
PERIOD = ('effectiveFrom', 'effectiveThrough', 'amount')


def validate(value, exact, document):
    if not isinstance(value, dict) or set(value) != set(FIELDS):
        raise ValueError('Incomplete ESRD final-payment amounts.')
    if value['algorithm'] != ALGORITHM:
        raise ValueError('Unsupported ESRD final-payment algorithm.')
    # CMS applies the quality reduction in the same step, so a release cannot carry one without the other.
    if 'facilityAdjustments' not in document:
        raise ValueError('ESRD final-payment amounts require the facility-level factors of the same release.')
    if not 0 <= exact(value['networkReductionAmount']) <= 10:
        raise ValueError('Invalid ESRD network reduction amount.')
    periods = value['postTdapaAddOns']
    if not isinstance(periods, list) or len(periods) > 24:
        raise ValueError('Invalid ESRD post-TDAPA add-on periods.')
    start, end = date.fromisoformat(document['effectiveFrom']), date.fromisoformat(document['effectiveThrough'])
    previous = None
    for row in periods:
        if (not isinstance(row, dict) or set(row) != set(PERIOD)
                or any(not isinstance(row[name], str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', row[name]) for name in PERIOD[:2])):
            raise ValueError('Invalid ESRD post-TDAPA add-on period.')
        first, last = date.fromisoformat(row['effectiveFrom']), date.fromisoformat(row['effectiveThrough'])
        # Whole calendar months, in order and without overlap. CMS selects the amount by the claim's
        # through date; a claim within one month then gets the same amount from any of its dates.
        if (not start <= first <= last <= end or first.day != 1 or (last + timedelta(days=1)).day != 1
                or (previous is not None and first <= previous) or not 0 <= exact(row['amount']) <= 100):
            raise ValueError('ESRD post-TDAPA add-on periods must be whole months in order within the release period.')
        previous = last


def normalize(value, normalized):
    return dict(algorithm=value['algorithm'], networkReductionAmount=normalized(value['networkReductionAmount']),
                postTdapaAddOns=[dict(effectiveFrom=row['effectiveFrom'], effectiveThrough=row['effectiveThrough'],
                                      amount=normalized(row['amount'])) for row in value['postTdapaAddOns']])


def statements():
    statement = ''
    for line in SCHEMA.read_text(encoding='utf-8').splitlines(True):
        statement += line
        if sqlite3.complete_statement(statement):
            yield statement.strip()
            statement = ''
    if statement.strip():
        raise ValueError('Incomplete ESRD final-payment schema.')


def check_schema(connection):
    query = "SELECT name,sql FROM sqlite_master WHERE tbl_name='esrd_final_payment' AND type IN ('table','trigger') ORDER BY name"
    actual = dict(connection.execute(query))
    if not actual:
        return False
    with closing(sqlite3.connect(':memory:')) as expected:
        for statement in statements():
            expected.execute(statement)
        if actual != dict(expected.execute(query)):
            raise ValueError('Unexpected ESRD final-payment schema.')
    return True


def read(connection, key):
    if not check_schema(connection):
        return None
    row = connection.execute('SELECT algorithm,network_reduction_amount_exact,post_tdapa_add_ons_json '
        'FROM esrd_final_payment WHERE catalog_sha256=?', (key,)).fetchone()
    if not row:
        return None
    return dict(algorithm=row[0], networkReductionAmount=row[1], postTdapaAddOns=json.loads(row[2]))


def install(connection, key, parameters):
    if parameters is None:
        return
    if not check_schema(connection):
        for statement in statements():
            connection.execute(statement)
    connection.execute('INSERT INTO esrd_final_payment VALUES (?,?,?,?)', (key, parameters['algorithm'], parameters['networkReductionAmount'],
        json.dumps(parameters['postTdapaAddOns'], sort_keys=True, ensure_ascii=True, separators=(',', ':'))))

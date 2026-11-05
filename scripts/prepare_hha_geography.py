"""Reproduce the reviewed CY2026 HH county catalog from retained official files."""
from pathlib import Path
from io import BytesIO, StringIO
from decimal import Decimal
import argparse
import ast
import csv
import hashlib
import json
import re
import unicodedata
import zipfile

PINS = {
    'fy2026-ipps-fr-county-cbsa-crosswalk-file.zip': ('https://www.cms.gov/files/zip/fy2026-ipps-fr-county-cbsa-crosswalk-file.zip', 'fcae5937ff6faef50c42170f7439ede3b6c7a14fa4c79e041194b1ee44cf3d5d'),
    'cy-2026-final-hh-pps-wage-index.zip': ('https://www.cms.gov/files/zip/cy-2026-final-hh-pps-wage-index.zip', 'c8734767077549704faf9627a8c92b34cb8ce13626379500239e07defc2aeca3'),
    'list1_2023.xlsx': ('https://www2.census.gov/programs-surveys/metro-micro/geographies/reference-files/2023/delineation-files/list1_2023.xlsx', '952c4b1e78acbb54e6ec9412434b7602fedacbf021736351a63c181bdb753629'),
    'r13488cp.pdf': ('https://www.cms.gov/files/document/r13488cp.pdf', '56752ec4506a89bdc9c762808e58b4f7cf0bd3013228d1beea561a33b8d384dd'),
}

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()

def sha(raw):
    return hashlib.sha256(raw).hexdigest()

def normal(name):
    return re.sub('[^A-Z0-9]', '', unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode().upper())

def prepare(source_dir, output, state_names):
    from openpyxl import load_workbook
    sources = {}
    for name, (url, digest) in PINS.items():
        raw = (source_dir / name).read_bytes()
        if sha(raw) != digest:
            raise ValueError('Unreviewed or changed source: ' + name)
        sources[name] = dict(sourceUrl=url, sourceSha256=digest)
    z = zipfile.ZipFile(source_dir / 'fy2026-ipps-fr-county-cbsa-crosswalk-file.zip')
    register_name = 'FY 2026 FR County to CBSA Crosswalk.txt'
    raw = z.read(register_name)
    register_source = dict(sources['fy2026-ipps-fr-county-cbsa-crosswalk-file.zip'], memberName=register_name, memberSha256=sha(raw))
    rows = list(csv.reader(StringIO(raw.decode('utf-8-sig')), delimiter='\t'))
    if rows[:2] != [['FY 2026 Crosswalk','','','',''], ['FIPS County Code','County Name','State','FY 2025 CBSA ','FY 2025 CBSA Name ']]:
        raise ValueError('The reviewed county register layout changed.')
    register = {}
    names = {}
    for number, row in enumerate(rows[2:], 3):
        if len(row) != 5 or not re.fullmatch('[0-9]{5}', row[0]) or row[0] in register:
            raise ValueError('Invalid/duplicated county register row.')
        register[row[0]] = dict(name=row[1], state=row[2], cbsa=row[3], row=number)
        key = (normal(row[1]), row[2])
        if key in names:
            raise ValueError('Ambiguous county name/state.')
        names[key] = row[0]
    if len(register) != 3222:
        raise ValueError('Unexpected county register coverage.')
    # CMS swaps the directional words in these two register names. These are
    # independently bound by FIPS in the Census workbook and HH urban table.
    names.update({('EASTBATONROUGE','LA'): '22033', ('WESTBATONROUGE','LA'): '22121'})
    census = {}
    for number, row in enumerate(load_workbook(source_dir / 'list1_2023.xlsx', read_only=True, data_only=True).active.iter_rows(values_only=True), 1):
        if isinstance(row[0], str) and row[0].isdigit() and row[9] and row[10]:
            county = str(row[9]).zfill(2) + str(row[10]).zfill(3)
            if county in census:
                raise ValueError('Repeated Census county.')
            census[county] = dict(code=str(row[1] or row[0]), kind=row[4], row=number)
    if len(census) != 1915:
        raise ValueError('Unexpected Census delineation coverage.')
    z = zipfile.ZipFile(source_dir / 'cy-2026-final-hh-pps-wage-index.zip')
    wage_name = 'CY 2026 Final HH PPS Wage Index - All areas - no counties listed tab.csv'
    wage_raw = z.read(wage_name)
    rates, rural = {}, {}
    for number, row in enumerate(csv.reader(StringIO(wage_raw.decode('cp1252'))), 1):
        if re.fullmatch('[0-9]{5}', row[0]):
            if row[0] in rates:
                raise ValueError('Duplicated published wage area.')
            rate = Decimal(row[2])
            if rate <= 0 or rate != rate.quantize(Decimal('.0001')):
                raise ValueError('Invalid published wage value.')
            rates[row[0]] = (format(rate, '.4f'), number)
            if row[0].startswith('999'):
                rural[row[1].strip().upper()] = row[0]
    if len(rates) != 489:
        raise ValueError('Unexpected published wage-area coverage.')
    workbook_name = 'CY 2026 Final HH PPS Wage Index.xlsx'
    workbook_raw = z.read(workbook_name)
    workbook = load_workbook(BytesIO(workbook_raw), read_only=True, data_only=True)
    transitions = {}
    for number, row in enumerate(workbook['Transition Codes'].iter_rows(values_only=True), 1):
        if number == 1:
            continue
        county, code = str(row[0]), str(int(row[5]))
        if county not in register or county in transitions or code not in rates or Decimal(str(row[7])).quantize(Decimal('.0001')) != Decimal(rates[code][0]):
            raise ValueError('Invalid transition source row.')
        transitions[county] = (code, number)
    if len(transitions) != 41:
        raise ValueError('Unexpected transition coverage.')
    urban = {}
    area = None
    for number, row in enumerate(workbook['Urban Areas'].iter_rows(values_only=True), 1):
        if isinstance(row[0], (int,float)):
            area = str(int(row[0]))
        if not area or not isinstance(row[2], str):
            continue
        label = row[2]
        if ',' in label:
            name, state = label.rsplit(',',1)
            county = names.get((normal(name), state.strip()))
        else:
            # One reviewed HH cell omits ', CT'. Its complete unique name and
            # Census/FIPS/CBSA corroboration identify it without guessing.
            matches = [c for c,r in register.items() if normal(r['name']) == normal(label)]
            county = matches[0] if len(matches) == 1 else None
        if county is None or county in urban:
            raise ValueError('Unmatched or ambiguous HH urban county: ' + label)
        expected = transitions.get(county, (register[county]['cbsa'], None))[0]
        if area != expected:
            raise ValueError('HH urban assignment differs from the register/transition source.')
        urban[county] = (area, number)
    if len(urban) != 1252:
        raise ValueError('Unexpected HH urban county coverage.')
    entries, conflicts = {}, []
    for county, row in register.items():
        census_row = census.get(county)
        census_area = census_row['code'] if census_row and census_row['kind'] == 'Metropolitan Statistical Area' else ''
        # Fail closed where sources actually disagree. This must never turn a
        # missing urban row into an inferred favorable rural payment.
        conflict = row['cbsa'] != census_area or bool(row['cbsa']) != (county in urban)
        base = row['cbsa'] or rural[state_names[county[:2]]]
        code, transition_row = transitions.get(county, (base, None))
        if conflict:
            conflicts.append(county)
        if code not in rates:
            raise ValueError('County has no published HH wage code.')
        entries[county] = dict(serviceCountyFips=county, countyName=row['name'], stateFips=county[:2],
            stateAbbreviation=row['state'], status='SourceConflict' if conflict else 'Verified',
            baseWageIndexCode=base, cmsWageIndexCode=code, wageIndex=rates[code][0],
            sourceRows=dict(county=row['row'], census=census_row['row'] if census_row else None,
                hhUrban=urban[county][1] if county in urban else None, transition=transition_row, wage=rates[code][1]))
    if conflicts != ['37167']:
        raise ValueError('Unreviewed source discrepancy: ' + repr(conflicts))
    document = dict(schemaVersion='HHAGeographyCatalog/1', paymentYear=2026,
        effectiveFrom='2026-01-01', effectiveThrough='2026-12-31', entries=entries,
        sources=dict(countyRegister=register_source, censusDelineation=sources['list1_2023.xlsx'],
            hhWageIndex=dict(sources['cy-2026-final-hh-pps-wage-index.zip'], memberName=wage_name, memberSha256=sha(wage_raw)),
            hhCountyWorkbook=dict(sources['cy-2026-final-hh-pps-wage-index.zip'], memberName=workbook_name, memberSha256=sha(workbook_raw)),
            policy=sources['r13488cp.pdf']))
    with output.open('xb') as stream:
        stream.write(canonical(document))
    return dict(status='ReviewedSourcesReconciled', catalogSha256=sha(output.read_bytes()),
        counties=len(entries), verifiedCounties=len(entries)-len(conflicts), sourceConflictCounties=conflicts,
        metropolitanCounties=len(urban), transitionCounties=len(transitions), publishedWageCodes=len(rates),
        discrepancy='37167: CMS crosswalk says 16740; Census says micropolitan 10620; HH urban table omits it. No payment mapping accepted.')

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--state-source',type=Path,default=Path(__file__).resolve().parents[1]/'bridge/hha_geography.py')
    args=parser.parse_args()
    state_names = next(ast.literal_eval(node.value) for node in ast.parse(args.state_source.read_text(encoding='utf-8')).body
                       if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='FIPS_STATE_NAMES' for t in node.targets))
    print(json.dumps(prepare(args.sources,args.output,state_names),indent=2))

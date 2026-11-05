"""Build the reviewed CY 2025 HH reference packages from pinned CMS files."""

import argparse
import csv
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import re
from zipfile import ZipFile

WAGE_URL = "https://www.cms.gov/files/zip/cy-2025-final-home-health-pps-wage-index.zip"
RATE_URL = "https://www.cms.gov/files/document/r12911cp.pdf"
DEDUCTIBLE_URL = "https://www.cms.gov/files/document/mm13796-medicare-deductible-coinsurance-premium-rates-cy-2025-update.pdf"


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def pinned(path, expected):
    raw = path.read_bytes()
    if digest(raw) != expected:
        raise ValueError(f"Unexpected digest for {path.name}")
    return raw


def catalog(schema, sources, entries):
    return dict(schemaVersion=schema, paymentYear=2025, effectiveFrom="2025-01-01",
                effectiveThrough="2025-12-31", sources=sources, entries=entries)


def build_geography(wage_zip, jar, policy):
    sources = {"policy": dict(sourceUrl=RATE_URL, sourceSha256=digest(policy))}
    with ZipFile(io.BytesIO(wage_zip)) as archive:
        def read_source(key, name):
            raw = archive.read(name)
            sources[key] = dict(sourceUrl=WAGE_URL, sourceSha256=digest(wage_zip),
                                memberName=name, memberSha256=digest(raw))
            return list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))[1:]

        counties = read_source("hhCountyWages", "508_Version_Final_CY2025_HH_wageindex.csv")
        regions = read_source("hhConnecticutRegions", "508_Version_CT_Counties.csv")
        transitions = read_source("hhTransitionCounties", "508_Version_Transition_Code_Counties.csv")
    with ZipFile(io.BytesIO(jar)) as archive:
        raw = archive.read("cbsa-wage-index-2025.csv").decode("cp1252")
    wages = {row["cbsa"]: row for row in csv.DictReader(io.StringIO(raw))}
    transition_by_fips = {row[0]: (line, row) for line, row in enumerate(transitions, 2)}
    if len(transition_by_fips) != 41:
        raise ValueError("Unexpected CY 2025 transition coverage")

    entries = {}
    seen_transitions = set()

    def add(fips, name, state, code, wage, line):
        base_code, transition_line = code, None
        if code.startswith("50"):
            transition_line, transition = transition_by_fips[fips]
            if code != transition[6] or Decimal(wage) != Decimal(transition[7]):
                raise ValueError(f"Conflicting transition mapping for {fips}")
            base_code = transition[4]
            seen_transitions.add(fips)
        source = wages[code]
        if source["effectiveDate"] != "20250101" or Decimal(source["geographicWageIndex"]) != Decimal(wage):
            raise ValueError(f"Pricer wage mismatch for {fips}")
        row = dict(serviceCountyFips=fips, countyName=name.strip(), stateFips=fips[:2],
                   stateAbbreviation=state, status="Verified", baseWageIndexCode=base_code,
                   cmsWageIndexCode=code, wageIndex=f"{Decimal(wage):.4f}",
                   sourceRows=dict(county=line, census=None, hhUrban=None, transition=transition_line, wage=line))
        if fips in entries:
            previous = entries[fips]
            if dict(previous, sourceRows=row["sourceRows"]) != row:
                raise ValueError(f"Conflicting county mapping for {fips}")
            return
        entries[fips] = row

    for line, row in enumerate(counties, 2):
        if re.fullmatch("[0-9]{5}", row[0]) and row[1] != "STATEWIDE":
            add(row[0], row[1], row[2], row[5], row[7], line)
    for line, row in enumerate(regions, 2):
        if re.fullmatch("09[0-9]{3}", row[4]):
            add(row[4], row[5], "CT", row[6], row[8], line)

    if seen_transitions != transition_by_fips.keys():
        raise ValueError("The county files do not cover every published CY 2025 transition")
    return catalog("HHAGeographyCatalog/1", sources, entries)


def build_devices(policy, deductible):
    sources = dict(
        billing=dict(rowLocator="CR13244; requirements 04.1,04.2,04.5,04.5.1",
                     sourceSha256="ff58282722d2758fe85c337f1c4a8cf07639f74298afc9d576c3e4c1e1713f1e",
                     sourceUrl="https://www.cms.gov/files/document/r12382cp.pdf"),
        coinsurance=dict(rowLocator="42 CFR 409.50(b); device coinsurance",
                         sourceSha256="f8eb0851732ac1f7e94800fd1133a743199f32885665c76e7b664a492b391ace",
                         sourceUrl="https://www.govinfo.gov/content/pkg/CFR-2024-title42-vol2/pdf/CFR-2024-title42-vol2-part409.pdf"),
        deductible=dict(rowLocator="CY 2025 Part B annual deductible", sourceSha256=digest(deductible),
                        sourceUrl=DEDUCTIBLE_URL),
        rate=dict(rowLocator="Table 8; CY2025 dNPWT device rate; PDF page 13",
                  sourceSha256=digest(policy), sourceUrl=RATE_URL),
    )
    return catalog("HHASeparatePaymentRates/1", sources, {
        "A9272": dict(hcpcsCode="A9272", unitRate="276.57", coinsuranceRate="0.20", annualPartBDeductible="257.00"),
    })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("wage_zip", "pricer_jar", "rate_pdf", "deductible_pdf", "output_directory"):
        parser.add_argument(arg, type=Path)
    args = parser.parse_args()
    wages = pinned(args.wage_zip, "349874c0314db3f49651191106e5c6ad84c019b8219fbc645193181d5bf0122d")
    jar = pinned(args.pricer_jar, "7a70ae359434f4919d3aa171d0a2e79c98079d8d01b27d328171f23517f2d6f4")
    policy = pinned(args.rate_pdf, "53574a8b503392ff3a1e3953a343d95993c6af2f7c8e78925ba60fdbd69d9853")
    deductible = pinned(args.deductible_pdf, "029e79062e6a89b1c2a386c8dbdb90432d151800955ed98bd520f6a432405cbe")
    for key, document in (("geography", build_geography(wages, jar, policy)),
                          ("separate-payment-rates", build_devices(policy, deductible))):
        raw = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
        path = args.output_directory / f"hha-{key}-2025-v1.json"
        path.write_bytes(raw)
        print(f"{path.name} {len(document['entries'])} entries SHA-256 {digest(raw)}")


if __name__ == "__main__":
    main()

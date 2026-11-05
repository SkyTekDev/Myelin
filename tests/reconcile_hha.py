"""Compare the embedded adapter with the unmodified standalone CMS REST JAR.

Run the CMS executable separately and pass its effective base URL. This tool
uses only an isolated in-memory provider database and synthetic visits.
"""

import argparse
import json
from decimal import Decimal
from pathlib import Path
from urllib.request import Request, urlopen

from pydantic_core import to_json

from bridge.hha_models import HhaRequest
from tests.test_hha_models import request_data
from tests.test_hha_integration import HhaCmsIntegrationChecks


OPTIONAL_NULL_TRANSPORT_FIELDS = ("finalCbsa", "finalWageIndex")


def normalize_transport_nulls(response):
    """Drop only generic optional geography nulls omitted by CMS REST JSON.

    The standalone Dropwizard mapper omits these nulls, whereas the bridge's
    isolated plain Jackson mapper emits them. All money, counts, components,
    return codes, versions and any non-null geography values remain compared.
    """
    normalized = dict(response)
    payment = dict(normalized["paymentData"])
    for field in OPTIONAL_NULL_TRANSPORT_FIELDS:
        if field in payment and payment[field] is None:
            del payment[field]
    normalized["paymentData"] = payment
    return normalized


def differences(left, right, path=""):
    if isinstance(left, dict) and isinstance(right, dict):
        result = []
        for key in sorted(left.keys() | right.keys()):
            if key not in left or key not in right:
                result.append(f"{path}/{key}: missing from {'standalone' if key not in left else 'embedded'}")
            else:
                result.extend(differences(left[key], right[key], f"{path}/{key}"))
        return result
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return [f"{path}: list lengths {len(left)} != {len(right)}"]
        result = []
        for index, (lv, rv) in enumerate(zip(left, right)):
            result.extend(differences(lv, rv, f"{path}/{index}"))
        return result
    return [] if left == right else [f"{path}: {left!r} != {right!r}"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--standalone-url", default="http://127.0.0.1:18087")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    HhaCmsIntegrationChecks.setUpClass()
    processor = HhaCmsIntegrationChecks.processor
    from bridge.hha_provider import provider_for_period
    from bridge.hha_geography import resolve_geography
    changes = [
        ("rural_alabama", {}, "1602.87"),
        ("urban_abilene", {"serviceCountyFips": "48441", "cbsaCode": "10180"}, "2084.49"),
        ("qrp_reduced", {"providerCcn": "017998"}, "1571.57"),
        ("negative_vbp", {"providerCcn": "017997"}, "1522.73"),
        ("positive_vbp", {"providerCcn": "017996"}, "1683.01"),
        ("county_transition", {"serviceCountyFips": "01129", "cbsaCode": "50001", "hippsCode": "1FC21"}, "1762.31"),
    ]
    report = []
    try:
        for name, overrides, expected in changes:
            data = request_data()
            data.update(overrides)
            model = HhaRequest.model_validate(data)
            provider = provider_for_period(processor.engine, model.providerCcn, model.fromDate, model.throughDate)
            geography = resolve_geography(model.serviceCountyFips, model.cbsaCode, processor.wage_rows, processor.geography_catalog)
            payload = processor.build_payload(model, provider, geography)
            with urlopen(Request(args.standalone_url.rstrip("/") + "/v2/price-claim",
                                 data=to_json(payload), headers={"Content-Type": "application/json"}), timeout=30) as response:
                standalone = json.load(response, parse_float=Decimal)
            embedded = processor.process(model)
            if embedded.status != "Success" or embedded.totalPayment != Decimal(expected):
                raise AssertionError(f"Embedded ordinary calculation failed for {name}.")
            standalone_comparison = normalize_transport_nulls(standalone)
            embedded_comparison = normalize_transport_nulls(embedded.cms)
            if standalone_comparison != embedded_comparison:
                raise AssertionError(f"Standalone and embedded CMS outputs differ for {name}: {differences(standalone_comparison, embedded_comparison)}")
            report.append(dict(name=name, request=data, cmsInput=payload, standalone=standalone,
                               bridge=embedded.model_dump(mode="json"), expectedPayment=expected, matched=True))
        summary = dict(pricerRelease="2.6.0", calculationVersion="2026.0",
                       fixtureBasis="Independent published-CMS Decimal arithmetic and standalone executable parity",
                       transportNormalization=dict(
                           optionalNullPaymentFields=OPTIONAL_NULL_TRANSPORT_FIELDS,
                           reason="CMS REST JSON omits optional geography nulls emitted by the embedded Jackson mapper; all other output fields remain exact comparisons."),
                       synthetic=True, liveProviderDatabaseUsed=False, comparisons=report)
        if args.output:
            args.output.write_bytes(to_json(summary, indent=2))
        print(f"PASS {len(report)} complete standalone/embedded HHA output comparisons.")
    finally:
        HhaCmsIntegrationChecks.tearDownClass()


if __name__ == "__main__":
    main()

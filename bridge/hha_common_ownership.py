"""Compare reviewed HH geography at the move date for § 484.235(b)(2)(ii)."""

from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path

from bridge.hha_provider import HhaInputError


DIVISION_CATALOG_HASH = "95b94bac69cb5cf950cf115f502bdd35df1495fe9abe4f01928acb2ca454b650"


@lru_cache(maxsize=1)
def _division_parents():
    path = Path(__file__).parent / "references" / "hha-metro-divisions-2023-v1.json"
    try:
        content = path.read_text(encoding="utf-8").encode()
        if sha256(content).hexdigest() == DIVISION_CATALOG_HASH:
            return json.loads(content)["divisions"]
    except OSError:
        pass
    raise HhaInputError("HHA_TRANSFER_AREA_REFERENCE_INVALID", "The reviewed HH metropolitan division reference is unavailable.")


def _area(catalog, county, divisions):
    base = catalog.county_entry(county)["baseWageIndexCode"]
    if base.startswith("999") or not base.endswith("4"):
        return base
    return divisions.get(base)


def area_evidence(catalog, origin_county, destination_county):
    divisions = _division_parents()
    origin = _area(catalog, origin_county, divisions)
    destination = _area(catalog, destination_county, divisions)
    if origin is None or destination is None or origin == destination:
        raise HhaInputError("HHA_TRANSFER_AREA_NOT_DISTINCT", "A common-ownership transfer requires a move to a different reviewed MSA or non-MSA.")
    return dict(areaYear=catalog.reference_info["paymentYear"], originAreaCode=origin, destinationAreaCode=destination,
                geographyCatalogSha256=catalog.sha256, metroDivisionCatalogSha256=DIVISION_CATALOG_HASH)

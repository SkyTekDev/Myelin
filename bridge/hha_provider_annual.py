"""Pinned annual HH provider facts, independently corroborated by CMS sources.

The exact payment factor remains IPSF field 30. Public HHVBP percentages are
rounded annual corroboration, never a replacement factor or a source of extra
precision. The catalog is built by the reviewed preparation tool and is pinned
in both Myelin and MedicareServiceAPI.
"""

from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit


CATALOG_SCHEMA = "HHAAnnualProviderCatalog/1"
EVIDENCE_SCHEMA = "HHAAnnualPaymentEvidence/1"
CATALOG_FILENAME = "hha-provider-annual-2026-v1.json"
# Independently reviewed against all retained CMS source files and complete history.
CATALOG_SHA256 = "f422f6459620f453c31b17ae412b8966a3e90b952bd886256367dc552471a461"
NO_APR_FOOTNOTE = (
    "HHA ineligible to receive an APR due to Medicare certification date and/or no or "
    "insufficient data on at least five measures to calculate a Total Performance Score (TPS)."
)
POLICY_SOURCES = {
    "PerformanceToPaymentYear": (
        "https://www.cms.gov/priorities/innovation/media/document/hhvbp-exp-model-guide",
        "02992937d26c78c4b22b7b8fd53d88c466d524da9acc84ac78a01d89583931bc"),
    "NoAprNoAdjustment": (
        "https://www.cms.gov/priorities/innovation/media/document/hhvbp-exp-faqs",
        "79862328d9a5e54a4d6eb71d475e78847ef49f0dbce128da732a3957da1f19ef"),
}

_HASH = re.compile(r"^[0-9a-f]{64}$")
_CCN = re.compile(r"^[A-Z0-9]{6,13}$")
_DECIMAL = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_ENTRY_FIELDS = {
    "providerCcn", "providerEffectiveDate", "providerNextEffectiveDate",
    "rawTerminationDate", "providerType", "qualityReportingProgramIndicator",
    "vbpAdjustment", "providerRowContentSha256", "providerHistoryCutoffDate",
    "qualityReportingSource", "valueBasedPurchasingSource", "providerHistorySource",
}


class AnnualProviderReferenceError(ValueError):
    def __init__(self, code, reason):
        super().__init__(reason)
        self.code, self.reason = code, reason


def canonical_json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("utf-8")


def canonical_decimal(value) -> str:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Invalid annual payment decimal.") from None
    if not amount.is_finite():
        raise ValueError("Invalid annual payment decimal.")
    text = format(amount, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def _day(value):
    if not isinstance(value, str):
        raise ValueError("Annual source dates must be ISO calendar dates.")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("Annual source dates must be ISO calendar dates.")
    return parsed


def _hash(value):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ValueError("Annual source fingerprint must be lowercase SHA-256.")


def provider_row_content_sha256(evidence: dict) -> str:
    """Hash the normalized used provider facts, excluding local database IDs."""
    used = {
        "providerCcn": evidence["providerCcn"],
        "effectiveDate": evidence["effectiveDate"],
        "rawTerminationDate": evidence["rawTerminationDate"],
        "providerType": evidence["providerType"],
        "federalPpsBlend": evidence["federalPpsBlend"],
        "specialProviderUpdateFactor": canonical_decimal(evidence["specialProviderUpdateFactor"]),
    }
    return hashlib.sha256(canonical_json(used)).hexdigest()


def _source(source, kind, *, entry=False):
    if not isinstance(source, dict):
        raise ValueError("Annual source metadata is required.")
    common = {"sourceUrl", "sourceSha256"}
    fields = common | ({"cutoffDate"} if kind == "providerHistory" else {"publishedDate"})
    if kind == "valueBasedPurchasing":
        fields |= {"factorAuthority", "annualCorroboration"}
        no_apr = source.get("annualCorroboration") == "CMS_HHVBP_CY2026_INELIGIBLE_FOR_APR"
        if no_apr:
            fields |= {"performanceYear", "observedPaymentYears", "policySources"}
        if entry:
            fields |= {"publishedAdjustedPaymentPercentage"}
            fields |= ({"publishedPaymentYear", "annualPerformanceReportFootnote", "adjustedPaymentPercentageFootnote"}
                       if no_apr else {"publishedPercentageDecimalPlaces"})
    if entry:
        fields.add("rowLocator")
    if set(source) != fields:
        raise ValueError("Annual source metadata has unsupported or missing fields.")
    _hash(source["sourceSha256"])
    if not isinstance(source["sourceUrl"], str) or len(source["sourceUrl"]) > 2048:
        raise ValueError("Annual source URLs must be explicit CMS HTTPS locations.")
    parsed = urlsplit(source["sourceUrl"])
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or parsed.fragment or parsed.port not in (None, 443)):
        raise ValueError("Annual sources require official CMS HTTPS URLs.")
    if kind == "providerHistory":
        cutoff = _day(source["cutoffDate"])
        query = parse_qs(parsed.query)
        if (parsed.hostname != "pds.mps.cms.gov" or parsed.path != "/fiss/v2/inpatient/export"
                or query.get("fromDate") != ["1980-01-01"]
                or query.get("toDate") != [cutoff.isoformat()]
                or query.get("format") != ["csv"]
                or set(query) != {"fromDate", "toDate", "format", "oscarNumber"}):
            raise ValueError("A complete, explicitly bounded CMS IPSF history export is required.")
        ccns = query.get("oscarNumber", [])
        if (not ccns or len(ccns) > 15 or len(set(ccns)) != len(ccns)
                or any(not re.fullmatch(r"[0-9]{6}", ccn) for ccn in ccns)):
            raise ValueError("CMS provider history must identify its explicit CCN cohort.")
    else:
        _day(source["publishedDate"])
        expected_host = "www.cms.gov" if kind == "qualityReporting" else "data.cms.gov"
        if parsed.hostname != expected_host or parsed.query:
            raise ValueError("The annual reference source is outside the approved CMS authority.")
    if entry:
        locator = source["rowLocator"]
        if (not isinstance(locator, str) or not 1 <= len(locator) <= 512 or locator.strip() != locator
                or any(ord(character) < 32 or ord(character) > 126 for character in locator)):
            raise ValueError("Annual source row locators must be explicit printable ASCII.")
    if kind == "valueBasedPurchasing":
        if (source["factorAuthority"] != "CMS_IPSF_FIELD_30"
                or source["annualCorroboration"] not in {"CMS_HHVBP_PAYMENT_YEAR_ROUNDED_APP", "CMS_HHVBP_CY2026_INELIGIBLE_FOR_APR"}):
            raise ValueError("HHVBP requires exact IPSF authority and rounded annual corroboration.")
        if no_apr:
            if (type(source["performanceYear"]) is not int or source["performanceYear"] != 2024
                    or source["observedPaymentYears"] != [2026]
                    or any(type(year) is not int for year in source["observedPaymentYears"])):
                raise ValueError("Ineligible APR evidence must identify the actual CY2026 annual dataset.")
            validate_policy_sources(source["policySources"])
            if entry and (source["publishedPaymentYear"] != "NA"
                    or source["publishedAdjustedPaymentPercentage"] != "NA"
                    or source["annualPerformanceReportFootnote"] != NO_APR_FOOTNOTE
                    or source["adjustedPaymentPercentageFootnote"] != NO_APR_FOOTNOTE):
                raise ValueError("Ineligible APR evidence requires the exact annual agency row and both published footnotes.")
            return
        if entry:
            value, places = source["publishedAdjustedPaymentPercentage"], source["publishedPercentageDecimalPlaces"]
            if (not isinstance(value, str) or not _DECIMAL.fullmatch(value)
                    or type(places) is not int or not 0 <= places <= 5
                    or len(value.partition(".")[2]) != places
                    or not Decimal("-5") <= Decimal(value) <= Decimal("5")):
                raise ValueError("The published HHVBP percentage must retain its actual precision.")


def _source_metadata(source, kind):
    excluded = {"rowLocator"}
    if kind == "valueBasedPurchasing":
        excluded |= {"publishedAdjustedPaymentPercentage", "publishedPercentageDecimalPlaces", "publishedPaymentYear",
                     "annualPerformanceReportFootnote", "adjustedPaymentPercentageFootnote"}
    return {key: value for key, value in source.items() if key not in excluded}


def validate_policy_sources(sources):
    if not isinstance(sources, list) or len(sources) != 2:
        raise ValueError("Two reviewed CMS policy authorities are required for ineligible APR evidence.")
    roles = set()
    for source in sources:
        if not isinstance(source, dict) or set(source) != {"basis", "sourceUrl", "sourceSha256", "rowLocator"}:
            raise ValueError("Policy source metadata is incomplete.")
        role = source["basis"]
        if role not in POLICY_SOURCES or role in roles or (source["sourceUrl"], source["sourceSha256"]) != POLICY_SOURCES[role]:
            raise ValueError("The policy source differs from the reviewed CMS role, URL or document fingerprint.")
        locator = source["rowLocator"]
        if (not isinstance(locator, str) or not 1 <= len(locator) <= 512
                or any(ord(character) < 32 or ord(character) > 126 for character in locator)
                or (role == "NoAprNoAdjustment" and "Q4006" not in locator)):
            raise ValueError("The policy source requires its explicit reviewed section locator.")
        roles.add(role)


def _validate_entry(ccn, entry, sources):
    if not isinstance(entry, dict) or set(entry) != _ENTRY_FIELDS:
        raise ValueError("Annual provider entries have unsupported or missing fields.")
    if not _CCN.fullmatch(ccn) or entry["providerCcn"] != ccn or entry["providerType"] != "36":
        raise ValueError("Annual entries must bind a normalized HH provider CCN.")
    effective = _day(entry["providerEffectiveDate"])
    cutoff = _day(entry["providerHistoryCutoffDate"])
    next_date = _day(entry["providerNextEffectiveDate"]) if entry["providerNextEffectiveDate"] is not None else None
    if (cutoff.year != 2026 or effective > cutoff
            or (next_date is not None and not effective < next_date <= cutoff)):
        raise ValueError("Annual provider history coverage is invalid.")
    termination = entry["rawTerminationDate"]
    if termination is not None and (type(termination) is not int or not 10000101 <= termination <= 99999999):
        # Existing open-termination sentinel 0 remains a supported source value.
        if termination != 0 or isinstance(termination, bool):
            raise ValueError("Annual provider termination facts must preserve the source representation.")
    if termination not in (None, 0, 19000101, 99991231):
        _day(f"{termination:08d}"[:4] + "-" + f"{termination:08d}"[4:6] + "-" + f"{termination:08d}"[6:])
    if entry["qualityReportingProgramIndicator"] not in {"0", "2"}:
        raise ValueError("Annual QRP evidence must be a supported final-payment indicator.")
    factor = entry["vbpAdjustment"]
    if (not isinstance(factor, str) or canonical_decimal(factor) != factor
            or len(factor.partition(".")[2]) > 5
            or not Decimal("0.95") <= Decimal(factor) <= Decimal("1.05")):
        raise ValueError("Annual HHVBP factors must be canonical exact supported decimals.")
    for kind in ("qualityReporting", "valueBasedPurchasing", "providerHistory"):
        source = entry[kind + "Source"]
        _source(source, kind, entry=True)
        if _source_metadata(source, kind) not in sources[kind]:
            raise ValueError("Annual entry source is not in the retained source manifest.")
    history = entry["providerHistorySource"]
    if history["cutoffDate"] != cutoff.isoformat() or ccn not in parse_qs(urlsplit(history["sourceUrl"]).query)["oscarNumber"]:
        raise ValueError("Annual provider entry does not bind its history source cohort and cutoff.")
    vbp = entry["valueBasedPurchasingSource"]
    if vbp["annualCorroboration"] == "CMS_HHVBP_CY2026_INELIGIBLE_FOR_APR":
        if Decimal(factor) != 1:
            raise ValueError("Ineligible APR evidence cannot corroborate a non-neutral exact IPSF factor.")
    else:
        exact_percentage = (Decimal(factor) - Decimal(1)) * Decimal(100)
        half_increment = Decimal(5).scaleb(-vbp["publishedPercentageDecimalPlaces"] - 1)
        if abs(exact_percentage - Decimal(vbp["publishedAdjustedPaymentPercentage"])) >= half_increment:
            raise ValueError("The exact IPSF factor is not corroborated by the published annual HHVBP percentage.")
    expected_content = provider_row_content_sha256({
        "providerCcn": ccn, "effectiveDate": effective.isoformat(),
        "rawTerminationDate": termination, "providerType": "36",
        "federalPpsBlend": entry["qualityReportingProgramIndicator"],
        "specialProviderUpdateFactor": factor,
    })
    _hash(entry["providerRowContentSha256"])
    if expected_content != entry["providerRowContentSha256"]:
        raise ValueError("Annual provider entry content fingerprint does not reconcile.")


@dataclass(frozen=True)
class AnnualProviderCatalog:
    sha256: str
    document: dict


def load_catalog(path: Path, expected_sha256: str) -> AnnualProviderCatalog:
    _hash(expected_sha256)
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("The annual provider catalog differs from its reviewed fingerprint.")
    document = json.loads(raw)
    if (not isinstance(document, dict) or set(document) != {"schemaVersion", "paymentYear", "sources", "entries"}
            or document["schemaVersion"] != CATALOG_SCHEMA or type(document["paymentYear"]) is not int
            or document["paymentYear"] != 2026 or not isinstance(document["entries"], dict)
            or not document["entries"] or canonical_json(document) != raw):
        raise ValueError("The annual provider catalog schema or canonical serialization is invalid.")
    sources = document["sources"]
    if not isinstance(sources, dict) or set(sources) != {"qualityReporting", "valueBasedPurchasing", "providerHistory"}:
        raise ValueError("The annual provider catalog source manifest is incomplete.")
    for kind, rows in sources.items():
        if not isinstance(rows, list) or not rows:
            raise ValueError("The annual provider source manifest must contain reviewed sources.")
        for row in rows:
            _source(row, kind)
        if len({canonical_json(row) for row in rows}) != len(rows):
            raise ValueError("The annual provider source manifest contains duplicate sources.")
    for ccn, entry in document["entries"].items():
        _validate_entry(ccn, entry, sources)
    return AnnualProviderCatalog(expected_sha256, document)


@lru_cache(maxsize=1)
def load_annual_catalog() -> AnnualProviderCatalog:
    return load_catalog(Path(__file__).parent / "data" / CATALOG_FILENAME, CATALOG_SHA256)


def annual_payment_evidence(provider_evidence: dict, start: date, end: date) -> dict:
    """Verify older used facts against the pinned complete-history annual entry."""
    try:
        catalog = load_annual_catalog()
    except FileNotFoundError as missing:
        raise AnnualProviderReferenceError("HHA_PROVIDER_REFERENCE_STALE",
            "The older effective HH provider record lacks reviewed CY 2026 annual payment evidence.") from missing
    except (OSError, ValueError, TypeError) as invalid:
        raise AnnualProviderReferenceError("HHA_PROVIDER_ANNUAL_REFERENCE_INVALID",
            "The reviewed CY 2026 HH annual provider reference is unavailable or invalid.") from invalid
    entry = catalog.document["entries"].get(provider_evidence["providerCcn"])
    if entry is None:
        raise AnnualProviderReferenceError("HHA_PROVIDER_REFERENCE_STALE",
            "The older effective HH provider record lacks reviewed CY 2026 annual payment evidence.")
    content = provider_row_content_sha256(provider_evidence)
    if content != entry["providerRowContentSha256"]:
        raise AnnualProviderReferenceError("HHA_PROVIDER_ANNUAL_FACTS_CONFLICT",
            "The local HH provider facts differ from the verified CMS provider history and annual evidence.")
    effective = _day(entry["providerEffectiveDate"])
    cutoff = _day(entry["providerHistoryCutoffDate"])
    next_date = _day(entry["providerNextEffectiveDate"]) if entry["providerNextEffectiveDate"] else None
    if start.year != 2026 or end.year != 2026 or start < effective or end > cutoff or (next_date and end >= next_date):
        raise AnnualProviderReferenceError("HHA_PROVIDER_ANNUAL_PERIOD_UNCOVERED",
            "The verified full CMS provider history does not cover this complete HH period.")
    return {
        "schemaVersion": EVIDENCE_SCHEMA, "paymentYear": 2026,
        "providerCcn": provider_evidence["providerCcn"],
        "providerRowId": provider_evidence["providerRowId"],
        "sourceRowFingerprint": provider_evidence["sourceRowFingerprint"],
        "providerRowContentSha256": content,
        "catalogSha256": catalog.sha256,
        "catalogEntrySha256": hashlib.sha256(canonical_json(entry)).hexdigest(),
        "qualityReportingProgramIndicator": entry["qualityReportingProgramIndicator"],
        "vbpAdjustment": entry["vbpAdjustment"],
        "providerNextEffectiveDate": entry["providerNextEffectiveDate"],
        "providerHistoryCutoffDate": entry["providerHistoryCutoffDate"],
        **{kind + "Source": deepcopy(entry[kind + "Source"])
           for kind in ("qualityReporting", "valueBasedPurchasing", "providerHistory")},
    }

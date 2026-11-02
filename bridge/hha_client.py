"""Strict supplied-HIPPS adapter around the existing CMS HHA bindings."""

import csv
import hashlib
import io
import json
import os
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN
from pathlib import Path
from threading import Lock
from zipfile import ZipFile

import jpype
from pydantic_core import to_json

from bridge.hha_geography import resolve_geography, validate_geography_references
from bridge.hha_models import HhaRequest, HhaResponse
from bridge.hha_geography_catalog import load_catalog
from bridge.hha_late_noa import apply as apply_late_noa
from bridge.hha_outlier import process_period as process_outlier_period, lupa_evidence as outlier_lupa_evidence
from bridge.hha_provider import HhaInputError, provider_for_period
from bridge.hha_release import (
    CALCULATION_VERSIONS, DISCIPLINE_REVENUES, JAR_FILENAME, JAR_SHA256,
    PRICER_RELEASE, SOURCE_URL, LUPA_INCREMENTAL_FACTORS,
)
from myelin.pricers.hha import HhaClient
from myelin.pricers.url_loader import UrlLoader

CENT = Decimal("0.01")
BASE_RATES = {"0": Decimal("2038.22"), "2": Decimal("1998.41")}
SKILLED_PRIORITY = {"SN": 0, "PT": 1, "OT": 2, "SLP": 3}


def money(value) -> Decimal:
    if isinstance(value, (bool, float)) or not isinstance(value, (Decimal, int, str)):
        raise RuntimeError("Invalid CMS monetary value.")
    try:
        amount = Decimal(value)
    except Exception:
        raise RuntimeError("Invalid CMS monetary value.") from None
    if not amount.is_finite():
        raise RuntimeError("Invalid CMS monetary value.")
    return amount


def rounded(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def ordinal(value: str) -> bytes:
    # Match .NET StringComparer.Ordinal, including supplementary Unicode IDs.
    return value.encode("utf-16-be", errors="surrogatepass")


def adjusted(amount: Decimal, wage: Decimal) -> Decimal:
    return rounded(wage * Decimal("0.749") * amount) + rounded(Decimal("0.251") * amount)


class HhaProcessor:
    def __init__(self, engine, jar_root):
        jar = Path(os.getenv("HHA_JAR_PATH", str(Path(jar_root) / "pricers" / JAR_FILENAME))).expanduser().resolve()
        if not jar.is_file() or hashlib.sha256(jar.read_bytes()).hexdigest() != JAR_SHA256:
            raise RuntimeError("HHA Pricer release has not been reconciled.")
        if not jpype.isJVMStarted():
            raise RuntimeError("Start Myelin JVM before HHA processor.")
        self.engine = engine
        self.lock = Lock()
        self.hrg_rows = self._reference_rows(jar, "hrg-2026.csv", "code")
        self.wage_rows = self._reference_rows(jar, "cbsa-wage-index-2026.csv", "cbsa")
        self.revenue_rows = self._reference_rows(jar, "rev-2026.csv", "code")
        if set(self.revenue_rows) != set(DISCIPLINE_REVENUES.values()):
            raise RuntimeError("Incomplete HHA per-visit rate reference.")
        for row in self.revenue_rows.values():
            for field in ("ruralDollarRate", "nonruralDollarRate", "ruralUnsubmittedDollarRate", "nonruralUnsubmittedDollarRate"):
                if money(row[field]) <= 0:
                    raise RuntimeError("Invalid HHA per-visit rate reference.")
        self.geography_catalog = load_catalog(engine)
        validate_geography_references(self.wage_rows, self.geography_catalog)
        # Only use the existing Java bindings. Do not invoke the generic claim
        # mapper or its rolling-year, geography, notice, and unit defaults.
        self.client = HhaClient.__new__(HhaClient)
        self.client.url_loader = UrlLoader()
        self.client.url_loader.load_urls([jar.as_uri()])
        self.client.db = engine
        self.client.load_classes()
        config = self.client.hha_pricer_config_class()
        config.setCsvIngestionConfiguration(self.client.hha_csv_ingest_class())
        years = self.client.array_list_class()
        for year in CALCULATION_VERSIONS:
            years.add(self.client.java_integer_class(year))
        config.setSupportedYears(years)
        self.client.hha_data_table_class.loadDataTables(config)
        self.client.dispatch_obj = self.client.hha_pricer_dispatch_class(config)
        loader = self.client.url_loader.class_loader
        self.mapper = jpype.JClass("com.fasterxml.jackson.databind.ObjectMapper", loader=loader)()
        self.mapper.registerModule(jpype.JClass("com.fasterxml.jackson.datatype.jsr310.JavaTimeModule", loader=loader)())

    @staticmethod
    def _reference_rows(jar: Path, filename: str, key: str) -> dict:
        with ZipFile(jar) as archive:
            # The pinned CMS wage table contains Windows-1252 place names.
            rows = list(csv.DictReader(io.StringIO(archive.read(filename).decode("cp1252"))))
        result = {}
        for row in rows:
            if not row.get(key) or row[key] in result or row.get("effectiveDate") != "20260101":
                raise RuntimeError("Invalid HHA bundled reference data.")
            result[row[key]] = row
        if not result:
            raise RuntimeError("Missing HHA bundled reference data.")
        return result

    def calculate(self, payload: dict) -> dict:
        # Decimal -> exact JSON string -> Java BigDecimal, matching SNF's bridge.
        request = self.mapper.readValue(to_json(payload).decode(), self.client.hha_pricer_request_class)
        response = self.client.dispatch_obj.process(request)
        return json.loads(str(self.mapper.writeValueAsString(response)), parse_float=Decimal)

    @staticmethod
    def build_payload(request: HhaRequest, provider: dict, geography: dict) -> dict:
        revenues = []
        for discipline, revenue in DISCIPLINE_REVENUES.items():
            visits = [visit for visit in request.visits if visit.discipline == discipline]
            revenues.append({
                "revenueCode": revenue,
                "quantityOfCoveredVisits": len(visits),
                "quantityOfOutlierUnits": sum(visit.outlierUnits for visit in visits),
                "earliestLineItemDate": min(visit.serviceDate for visit in visits).isoformat() if visits else None,
            })
        provider_data = dict(provider["providerData"],
                             countyCode=request.serviceCountyFips,
                             cbsaActualGeographicLocation=geography["cbsaCode"])
        return {
            "providerData": provider_data,
            "claimData": {
                "providerCcn": request.providerCcn,
                "typeOfBill": request.billType,
                "admissionDate": request.admissionDate.isoformat(),
                "serviceFromDate": request.fromDate.isoformat(),
                "serviceThroughDate": request.throughDate.isoformat(),
                "noticeReceiptDate": request.noticeReceiptDate.isoformat(),
                "hhrgInputCode": request.hippsCode,
                "hhrgNumberOfDays": ((max(visit.serviceDate for visit in request.visits)
                                     - min(visit.serviceDate for visit in request.visits)).days + 1
                                    if request.partialPeriod else 30),
                "initialPaymentQualityReportingProgramIndicator": provider["qualityReportingProgramIndicator"],
                "partialEpisodePaymentIndicator": "Y" if request.partialPeriod else "N",
                "lateFilingPenaltyWaiverIndicator": "N",
                "lupaSourceAdmissionIndicator": request.lupa.sourceAdmissionIndicator if request.lupa else "1",
                "adjustmentIndicator": "2" if request.lupa and not request.lupa.isFirstOrOnlyPeriod else "0",
                # Without a submitted snapshot, neutral totals expose any
                # candidate and require explicit history before accepting it.
                "priorPaymentTotal": request.outlier.priorPaymentTotal if request.outlier else Decimal(0),
                "priorOutlierTotal": request.outlier.priorOutlierTotal if request.outlier else Decimal(0),
                "revenueLines": revenues,
            },
        }

    def process(self, request: HhaRequest) -> HhaResponse:
        provider, geography, cms, payload = {}, {}, {}, {}
        lupa_detail = None
        partial_detail = None
        late_detail = None
        outlier_detail = None
        shortened_detail = None
        try:
            with self.lock:
                hrg = self.hrg_rows.get(request.hippsCode)
                if hrg is None:
                    raise HhaInputError("HHA_HIPPS_NOT_FOUND", "The supplied HIPPS code is not in the verified CY 2026 HHA reference.")
                is_lupa = len(request.visits) < int(hrg["threshold"])
                if is_lupa and request.lupa is None:
                    raise HhaInputError("HHA_LUPA_CONTEXT_REQUIRED", "LUPA pricing requires explicit source-admission and first-or-only-period facts.")
                if (is_lupa and request.lupa.isFirstOrOnlyPeriod
                        and not any(visit.discipline in SKILLED_PRIORITY for visit in request.visits)):
                    raise HhaInputError("HHA_LUPA_CONTEXT_UNSUPPORTED", "An initial or only LUPA period requires a covered skilled nursing or therapy visit.")
                provider = provider_for_period(self.engine, request.providerCcn, request.fromDate, request.throughDate)
                geography = resolve_geography(request.serviceCountyFips, request.cbsaCode, self.wage_rows, self.geography_catalog)
                geography.update(jarSha256=JAR_SHA256, wageIndexReference="cbsa-wage-index-2026.csv")
                payload = self.build_payload(request, provider, geography)
                cms = self.calculate(payload)
                code = cms.get("returnCodeData", {}).get("code") if isinstance(cms, dict) else None
                if code in {"01", "02", "11"} and request.outlier is None:
                    raise HhaInputError("HHA_OUTLIER_CONTEXT_REQUIRED", "Outlier pricing requires explicit provider CY2026 payment and outlier totals excluding this claim.")
                if code not in ({"06", "14"} if is_lupa else {"00","01","02","09","11"} if request.outlier else {"09"} if request.partialPeriod else {"00"}):
                    raise HhaInputError("HHA_CMS_REJECTED", "CMS did not return a supported HH payment for the supplied visit threshold.")
                if is_lupa:
                    total, lupa_detail = self.validate_lupa_result(request, provider, geography, hrg, cms)
                elif request.outlier:
                    total, outlier_detail, late_detail = process_outlier_period(request, provider, geography, hrg, cms, self.revenue_rows)
                else:
                    total = self.validate_result(request, provider, geography, hrg, cms)
                if request.partialPeriod:
                    first = min(visit.serviceDate for visit in request.visits)
                    last = max(visit.serviceDate for visit in request.visits)
                    partial_detail = dict(**request.partialPeriod.model_dump(), firstBillableDate=first,
                                          lastBillableDate=last, coveredDays=(last - first).days + 1,
                                          periodDays=30, paymentRatio=rounded(Decimal((last - first).days + 1) / 30),
                                          adjustmentApplied=not is_lupa)
                if request.shortenedPeriod:
                    shortened_detail = dict(**request.shortenedPeriod.model_dump(),
                        firstBillableDate=min(visit.serviceDate for visit in request.visits),
                        lastBillableDate=max(visit.serviceDate for visit in request.visits),
                        statementDays=(request.throughDate-request.fromDate).days+1,
                        periodDays=30, paymentRatio=Decimal("1.00"), adjustmentApplied=False)
                if request.outlier is None or is_lupa:
                    total, lupa_detail, late_detail = apply_late_noa(request, provider, geography, hrg, cms,
                        total, lupa_detail, self.revenue_rows, LUPA_INCREMENTAL_FACTORS)
                if request.outlier and is_lupa:
                    outlier_detail = outlier_lupa_evidence(request, provider, geography, hrg, cms, self.revenue_rows)
            evidence = dict(provider["providerEvidence"],
                            initialPaymentQualityReportingProgramIndicator=provider["qualityReportingProgramIndicator"],
                            pricerJarSha256=JAR_SHA256, pricerSourceUrl=SOURCE_URL,
                            hippsReference="hrg-2026.csv", hippsCode=request.hippsCode,
                            caseMixWeight=money(hrg["weight"]), lupaVisitThreshold=int(hrg["threshold"]),
                            priorAgencyTotalsUsed=request.outlier is not None and not is_lupa)
            if is_lupa:
                evidence["visitRateReference"] = "rev-2026.csv"
            limitations = [
                "Medicare PPS payment estimate only; beneficiary eligibility, coverage and final remittance are not determined.",
                "HIPPS, complete covered in-person visits and service location are supplied by the caller; no OASIS grouping is performed.",
                ("Agency totals are submitted estimates, exclude this claim, and are not independently verified or reserved in a ledger." if request.outlier else
                 "Agency outlier history is not used; an outlier outcome requires explicit CY2026 history."),
            ]
            if not geography.get("countyMappingVerified"):
                limitations.extend(geography["limitations"])
            if request.shortenedPeriod:
                limitations.append("Discharge/death, event date and no subsequent care within the original 30-day period are submitted facts, not CWF or history verification; later home health care may require claim adjustment. The shortened statement does not prorate its period payment.")
            return HhaResponse(claimId=request.claimId, status="Success",
                               reasonCode="HHA_LUPA_ESTIMATED" if is_lupa else "HHA_PARTIAL_PERIOD_ESTIMATED" if request.partialPeriod else "HHA_SHORTENED_PERIOD_ESTIMATED" if request.shortenedPeriod else "HHA_ORDINARY_PERIOD_ESTIMATED",
                               reason="CMS CY 2026 LUPA calculation completed." if is_lupa else "CMS CY 2026 partial-period HH PPS calculation completed." if request.partialPeriod else "CMS CY 2026 shortened-statement HH PPS calculation completed without partial-period proration." if request.shortenedPeriod else "CMS CY 2026 ordinary-period HH PPS calculation completed.",
                               totalPayment=total, pricerRelease=PRICER_RELEASE, calculationVersion=cms["calculationVersion"],
                               providerData=payload["providerData"], providerEvidence=evidence,
                               geographyEvidence=geography, cms=cms, limitations=limitations, lupa=lupa_detail,
                               partialPeriod=partial_detail, lateNoa=late_detail, outlier=outlier_detail, shortenedPeriod=shortened_detail)
        except HhaInputError as exc:
            return HhaResponse(claimId=request.claimId, status="UnableToPrice", reasonCode=exc.code, reason=exc.reason,
                               pricerRelease=PRICER_RELEASE, calculationVersion=cms.get("calculationVersion"),
                               providerData=payload.get("providerData", provider.get("providerData", {})),
                               providerEvidence=provider.get("providerEvidence", {}), geographyEvidence=geography, cms=cms)

    def validate_lupa_result(self, request: HhaRequest, provider: dict, geography: dict, hrg: dict, cms: dict):
        if (cms.get("calculationVersion") != CALCULATION_VERSIONS[2026]
                or request.lupa is None or len(request.visits) >= int(hrg["threshold"])):
            raise RuntimeError("Unverified LUPA calculation identity or threshold.")
        payment = cms.get("paymentData")
        if not isinstance(payment, dict):
            raise RuntimeError("Missing CMS LUPA payment data.")
        revenues = payment.get("revenuePayments")
        if (not isinstance(revenues, list) or len(revenues) != 6
                or any(not isinstance(row, dict) for row in revenues)
                or [row.get("revenueCode") for row in revenues] != list(DISCIPLINE_REVENUES.values())):
            raise RuntimeError("Invalid ordered CMS LUPA discipline components.")
        wage = money(geography["wageIndex"])
        vbp = money(provider["providerData"]["vbpAdjustment"])
        qrp = provider["qualityReportingProgramIndicator"]
        rural = geography["cbsaCode"].startswith("999")
        selected = None
        if request.lupa.isFirstOrOnlyPeriod and request.lupa.sourceAdmissionIndicator != "B":
            skilled = [visit for visit in request.visits if visit.discipline in SKILLED_PRIORITY]
            selected = min(skilled, key=lambda visit: (visit.serviceDate, SKILLED_PRIORITY[visit.discipline], ordinal(visit.visitId))) if skilled else None
        expected, before_vbp, standardized = Decimal(0), Decimal(0), Decimal(0)
        allocations = {}
        for (discipline, revenue), component in zip(DISCIPLINE_REVENUES.items(), revenues):
            visits = sorted((visit for visit in request.visits if visit.discipline == discipline), key=lambda visit: (visit.serviceDate, ordinal(visit.visitId)))
            reference = self.revenue_rows[revenue]
            field = ("rural" if rural else "nonrural") + ("UnsubmittedDollarRate" if qrp == "2" else "DollarRate")
            rate = money(reference[field])
            cost_before = adjusted(len(visits) * rate, wage)
            addon_before = Decimal(0)
            if selected is not None and selected.discipline == discipline:
                factor = money(LUPA_INCREMENTAL_FACTORS[discipline])
                addon_before = adjusted(rounded(rate * factor), wage)
            cost, addon = rounded(cost_before * vbp), rounded(addon_before * vbp)
            if (money(component.get("dollarRate")) != rate
                    or money(component.get("cost")) != cost
                    or money(component.get("addOnVisitAmount")) != addon):
                raise RuntimeError("CMS LUPA discipline rate, visit cost or add-on does not reconcile.")
            expected += cost + addon
            before_vbp += cost_before + addon_before
            full_rate = money(reference["nonruralDollarRate"])
            standardized += len(visits) * full_rate
            if selected is not None and selected.discipline == discipline:
                standardized += rounded(full_rate * money(LUPA_INCREMENTAL_FACTORS[discipline]))
            if visits:
                cents, residual = divmod(int(cost / CENT), len(visits))
                for index, visit in enumerate(visits):
                    base = Decimal(cents + (index < residual)) * CENT
                    visit_addon = addon if selected is not None and selected.visitId == visit.visitId else Decimal("0.00")
                    allocations[visit.visitId] = dict(visitId=visit.visitId, discipline=discipline, revenueCode=revenue,
                        serviceDate=visit.serviceDate, basePayment=base, addOnPayment=visit_addon, totalPayment=base + visit_addon)
        if len(request.visits) > 4:
            standardized = rounded(money(hrg["weight"]) * BASE_RATES["0"])
        expected_code = "14" if selected is not None else "06"
        if (cms.get("returnCodeData", {}).get("code") != expected_code
                or money(payment.get("totalPayment")) != expected
                or money(payment.get("hhrgPayment")) != 0
                or money(payment.get("hhrgWeight")) != money(hrg["weight"])
                or money(payment.get("outlierPayment")) != 0
                or money(payment.get("lateSubmissionPenaltyAmount")) != 0
                or money(payment.get("valueBasedPurchasingAdjustmentAmount")) != expected - before_vbp
                or money(payment.get("standardizedPayment")) != rounded(standardized)
                or type(payment.get("totalQuantityOfCoveredVisits")) is not int
                or payment["totalQuantityOfCoveredVisits"] != len(request.visits)):
            raise RuntimeError("CMS LUPA total, add-on disposition or payment evidence does not reconcile.")
        if payment.get("finalWageIndex") is not None and money(payment["finalWageIndex"]) != wage:
            raise RuntimeError("CMS LUPA wage index does not reconcile.")
        if payment.get("finalCbsa") is not None and payment["finalCbsa"] != geography["cbsaCode"]:
            raise RuntimeError("CMS LUPA wage area does not reconcile.")
        ordered = sorted(request.visits, key=lambda visit: (visit.serviceDate, ordinal(visit.visitId)))
        return expected, dict(sourceAdmissionIndicator=request.lupa.sourceAdmissionIndicator,
            isFirstOrOnlyPeriod=request.lupa.isFirstOrOnlyPeriod,
            visitPayments=[allocations[visit.visitId] for visit in ordered])

    def validate_result(self, request: HhaRequest, provider: dict, geography: dict, hrg: dict, cms: dict) -> Decimal:
        if cms.get("calculationVersion") != CALCULATION_VERSIONS[2026]:
            raise RuntimeError("Unverified HHA calculation release.")
        payment = cms.get("paymentData")
        if not isinstance(payment, dict):
            raise RuntimeError("Missing CMS HHA payment data.")
        weight, wage = money(hrg["weight"]), money(geography["wageIndex"])
        qrp = provider["qualityReportingProgramIndicator"]
        case_mix = rounded(weight * BASE_RATES[qrp])
        before_vbp = rounded(wage * Decimal("0.749") * case_mix) + rounded(Decimal("0.251") * case_mix)
        if request.partialPeriod:
            days = (max(visit.serviceDate for visit in request.visits) - min(visit.serviceDate for visit in request.visits)).days + 1
            before_vbp = rounded(before_vbp * rounded(Decimal(days) / 30))
        raw_penalty = Decimal(0)
        if request.lateNoa:
            raw_days = max(0, min(30, (request.noticeReceiptDate - request.fromDate).days))
            if raw_days > 5:
                raw_penalty = rounded(before_vbp * raw_days / 30)
        net_before_vbp = before_vbp - raw_penalty
        expected = rounded(net_before_vbp * provider["providerData"]["vbpAdjustment"])
        total = money(payment.get("totalPayment"))
        if (total < 0 or total != expected or money(payment.get("hhrgPayment")) != total
                or money(payment.get("hhrgWeight")) != weight
                or money(payment.get("outlierPayment")) != 0
                or money(payment.get("lateSubmissionPenaltyAmount")) != raw_penalty
                or money(payment.get("valueBasedPurchasingAdjustmentAmount")) != expected - net_before_vbp
                or type(payment.get("totalQuantityOfCoveredVisits")) is not int
                or payment["totalQuantityOfCoveredVisits"] != len(request.visits)):
            raise RuntimeError("CMS HHA ordinary-period payment does not reconcile.")
        revenues = payment.get("revenuePayments")
        if (not isinstance(revenues, list) or len(revenues) != 6
                or any(not isinstance(row, dict) for row in revenues)
                or [row.get("revenueCode") for row in revenues] != list(DISCIPLINE_REVENUES.values())
                or any(money(row.get("addOnVisitAmount")) != 0 or money(row.get("cost")) < 0
                       or money(row.get("dollarRate")) <= 0 for row in revenues)):
            raise RuntimeError("Invalid CMS HHA discipline components.")
        if payment.get("finalWageIndex") is not None and money(payment["finalWageIndex"]) != wage:
            raise RuntimeError("CMS HHA wage index does not reconcile.")
        if request.partialPeriod:
            standardized = sum(money(self.revenue_rows[DISCIPLINE_REVENUES[visit.discipline]]["nonruralDollarRate"]) for visit in request.visits)
            if len(request.visits) > 4:
                ratio = (Decimal(days) / 30).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
                standardized = rounded(weight * BASE_RATES["0"] * ratio)
            if (cms.get("returnCodeData", {}).get("code") != "09"
                    or money(payment.get("standardizedPayment")) != standardized):
                raise RuntimeError("CMS HHA partial-period standardized payment does not reconcile.")
            rural = geography["cbsaCode"].startswith("999")
            cost_before_vbp = Decimal(0)
            for (discipline, revenue), component in zip(DISCIPLINE_REVENUES.items(), revenues):
                prefix = "rural" if rural else "nonrural"
                reference = self.revenue_rows[revenue]
                qrp_part = "Unsubmitted" if qrp == "2" else ""
                rate = money(reference[prefix + qrp_part + "DollarRate"])
                unit_rate = money(reference[prefix + qrp_part + "UnitDollarRate"])
                units = sum(visit.outlierUnits for visit in request.visits if visit.discipline == discipline)
                discipline_cost = adjusted(rounded(unit_rate * units), wage)
                cost_before_vbp += discipline_cost
                # CMS retains pre-HHVBP unit costs as non-LUPA outlier evidence.
                cost = discipline_cost
                if money(component.get("cost")) != cost or money(component.get("dollarRate")) != rate:
                    raise RuntimeError("CMS HHA partial-period discipline costs or rates do not reconcile.")
            if cost_before_vbp > before_vbp + adjusted(rounded(BASE_RATES[qrp] * Decimal(".37")), wage):
                raise RuntimeError("CMS HHA partial-period costs require an unsupported outlier payment.")
            if payment.get("finalCbsa") is not None and payment["finalCbsa"] != geography["cbsaCode"]:
                raise RuntimeError("CMS HHA partial-period wage area does not reconcile.")
        return total

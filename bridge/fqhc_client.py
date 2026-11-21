"""Runs the CMS FQHC Pricer on CMS I/OCE output.

Requests and responses cross into Java as JSON through the jar's own Jackson
mapper, so amounts stay exact decimals and the field names are the jar's own
(Myelin's FqhcClient sets carrier/locality through setters the jar lacks).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import jpype

from bridge.claim_mapper import to_myelin_claim
from bridge.fqhc_models import FqhcClaimEdits, FqhcClaimRequest, FqhcEdit, FqhcLineResult, FqhcResponse
from bridge.fqhc_release import (
    FIRST_SERVICE_DATE,
    JAR_FILENAME,
    JAR_SHA256,
    LAST_SERVICE_DATE,
    MENTAL_HEALTH_REVENUE_CODE,
    PRICER_RELEASE,
    SUPPORTED_YEARS,
    TELEHEALTH_EFFECTIVE_DATE,
    TELEHEALTH_IOCE_MINIMUM,
    TELEHEALTH_MODIFIERS,
    UNSUPPORTED_DATES_REASON,
)
from myelin.pricers.url_loader import UrlLoader

PACKAGE = "gov.cms.fiss.pricers.fqhc"


def _decimal(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _version(value: str | None) -> tuple[int, int] | None:
    match = re.match(r"^(\d+)\.(\d+)", value or "")
    return (int(match.group(1)), int(match.group(2))) if match else None


def _edits(items) -> list[FqhcEdit]:
    return [FqhcEdit(edit=item.edit, description=item.description) for item in items or [] if item.edit]


def telehealth_lines(payload: FqhcClaimRequest) -> list[int]:
    """Line numbers billed as distant-site telehealth under CR 14468 (from October 1, 2026)."""
    return [line.lineNumber for line in payload.claimLines
            if line.dateOfService >= TELEHEALTH_EFFECTIVE_DATE
            and line.revenueCode != MENTAL_HEALTH_REVENUE_CODE
            and TELEHEALTH_MODIFIERS.intersection(line.modifiers)]


class FqhcProcessor:
    def __init__(self, jar_root):
        jar = Path(os.getenv("FQHC_JAR_PATH", str(Path(jar_root) / "pricers" / JAR_FILENAME))).resolve()
        digest = hashlib.sha256(jar.read_bytes()).hexdigest()
        if digest != JAR_SHA256:
            raise RuntimeError("FQHC Pricer release has not been reconciled.")
        if not jpype.isJVMStarted():
            raise RuntimeError("Start the Myelin JVM before the FQHC processor.")

        # The I/OCE comes from the shared classpath (the top level of the jar directory); report which one.
        ioce_jars = sorted(Path(jar_root).glob("ioce-standalone-*.jar"))
        self.ioce_jar = ioce_jars[-1].name if ioce_jars else None
        self.url_loader = UrlLoader()
        self.url_loader.load_urls([jar.as_uri()])
        loader = self.url_loader.class_loader

        def java(name):
            return jpype.JClass(name, loader=loader)

        config = java(PACKAGE + ".FqhcPricerConfiguration")()
        config.setCsvIngestionConfiguration(java("gov.cms.fiss.pricers.common.csv.CsvIngestionConfiguration")())
        years = java("java.util.ArrayList")()
        for year in SUPPORTED_YEARS:
            years.add(java("java.lang.Integer")(year))
        config.setSupportedYears(years)
        java(PACKAGE + ".core.tables.DataTables").loadDataTables(config)
        self.dispatch = java(PACKAGE + ".core.FqhcPricerDispatch")(config)
        self.request_class = java(PACKAGE + ".api.v2.FqhcClaimPricingRequest")
        self.mapper = java("com.fasterxml.jackson.databind.ObjectMapper")()
        self.mapper.registerModule(java("com.fasterxml.jackson.datatype.jsr310.JavaTimeModule")())

    def calculate(self, claim_data: dict) -> dict:
        # Decimals travel as JSON strings; Jackson reads them as BigDecimal.
        request = self.mapper.readValue(json.dumps({"claimData": claim_data}, default=str), self.request_class)
        response = self.dispatch.process(request)
        return json.loads(str(self.mapper.writeValueAsString(response)), parse_float=Decimal)

    @staticmethod
    def claim_data(payload: FqhcClaimRequest, ioce_lines) -> dict:
        data = {
            "providerCcn": payload.providerNumber,
            "carrierCode": payload.carrierCode,
            "localityCode": payload.localityCode,
            "demoCodes": [],
            "serviceFromDate": payload.fromDate.isoformat(),
            "serviceThroughDate": payload.throughDate.isoformat(),
            "ioceServiceLines": [
                {
                    "lineNumber": index,
                    "hcpcsCode": output.hcpcs,
                    "revenueCode": output.revenue_code,
                    "dateOfService": line.dateOfService.isoformat(),
                    "billedUnits": line.units,
                    "serviceUnits": output.units_output or 0,
                    "coveredCharges": line.chargeAmount,
                    "actionFlag": output.action_flag_output,
                    "denyOrRejectFlag": output.rejection_denial_flag,
                    "compositeAdjustmentFlag": output.composite_adjustment_flag,
                    "discountingFormula": output.discounting_formula or 0,
                    "hcpcsModifiers": [modifier.hcpcs_modifier for modifier in output.hcpcs_modifier_output_list],
                    "paymentAdjustmentFlags": [flag.flag for flag in (output.payment_adjustment_flag01,
                                                                      output.payment_adjustment_flag02) if flag.flag.strip()],
                    "packageFlag": output.packaging_flag.flag,
                    "paymentIndicator": output.payment_indicator,
                    "paymentMethodFlag": output.payment_method_flag,
                    "statusIndicator": output.status_indicator,
                }
                for index, (line, output) in enumerate(zip(payload.claimLines, ioce_lines), start=1)
            ],
        }
        if payload.medicareAdvantagePlanAmount is not None:
            data["medicareAdvantagePlanAmount"] = payload.medicareAdvantagePlanAmount
        return data

    def process(self, payload: FqhcClaimRequest, ioce_client, lock, bridge_version: str) -> FqhcResponse:
        base = dict(claimId=str(payload.claimId), bridgeVersion=bridge_version, pricerRelease=PRICER_RELEASE,
                    jarSha256=JAR_SHA256, carrierCode=payload.carrierCode, localityCode=payload.localityCode)

        def unable(code: str, reason: str, **extra) -> FqhcResponse:
            return FqhcResponse(**base, status="UnableToPrice", reasonCode=code, reason=reason, **extra)

        if payload.fromDate < FIRST_SERVICE_DATE or payload.throughDate > LAST_SERVICE_DATE:
            return unable("FQHC_DATES_NOT_SUPPORTED", UNSUPPORTED_DATES_REASON)

        # Without a CCN Myelin's I/OCE client supplies its own placeholder certification number.
        claim = to_myelin_claim(payload.model_copy(update={"providerNumber": payload.providerNumber or ""}))
        with lock:
            ioce = ioce_client.process(claim, include_descriptions=True)
            ioce_version = ioce.version or ioce.processing_information.version
            ioce_lines = ioce.line_item_list or []
            claim_edits = FqhcClaimEdits(
                returnToProvider=_edits(ioce.claim_return_to_provider_edit_list),
                suspension=_edits(ioce.claim_suspension_edit_list),
                rejection=_edits(ioce.claim_rejection_edit_list),
                denial=_edits(ioce.claim_denial_edit_list))
            context = dict(ioceVersion=ioce_version, claimProcessedFlag=ioce.claim_processed_flag, claimEdits=claim_edits)
            telehealth = telehealth_lines(payload)
            if telehealth and (_version(ioce_version) or (0, 0)) < TELEHEALTH_IOCE_MINIMUM:
                return unable("FQHC_TELEHEALTH_IOCE_UNAVAILABLE",
                              "Lines " + ", ".join(map(str, telehealth)) + " are distant-site telehealth (modifier 93 or 95) "
                              "on or after October 1, 2026. That billing needs the October 2026 I/OCE (27.3) or later; "
                              f"this bridge applied I/OCE {ioce_version}.", **context)
            if len(ioce_lines) != len(payload.claimLines):
                return unable("FQHC_IOCE_LINE_MISMATCH", "The I/OCE did not return one result per submitted line.", **context)
            cms = self.calculate(self.claim_data(payload, ioce_lines))

        return_code = cms.get("returnCodeData") or {}
        payment = cms.get("paymentData") or {}
        line_payments = {item.get("lineNumber"): item for item in payment.get("serviceLinePayments") or []}
        if set(line_payments) != set(range(1, len(payload.claimLines) + 1)):
            return unable("FQHC_PRICER_LINE_MISMATCH", "The CMS FQHC Pricer did not return one result per submitted line.", **context)

        lines = []
        for index, (line, output) in enumerate(zip(payload.claimLines, ioce_lines), start=1):
            priced = line_payments[index]
            line_code = priced.get("returnCodeData") or {}
            lines.append(FqhcLineResult(
                claimLineId=line.claimLineId, lineNumber=line.lineNumber, revenueCode=line.revenueCode, hcpcs=line.hcpcs,
                dateOfService=line.dateOfService, units=line.units, chargeAmount=line.chargeAmount,
                statusIndicator=output.status_indicator, paymentIndicator=output.payment_indicator,
                paymentIndicatorDescription=output.payment_indicator_description,
                paymentMethodFlag=output.payment_method_flag, compositeAdjustmentFlag=output.composite_adjustment_flag,
                packageFlag=output.packaging_flag.flag, actionFlag=output.action_flag_output,
                denialOrRejectionFlag=output.rejection_denial_flag, serviceUnits=output.units_output,
                ioceEdits=_edits([*output.hcpcs_edit_list, *output.revenue_edit_list, *output.service_date_edit_list]),
                pricerReturnCode=line_code.get("code"), pricerReturnDescription=line_code.get("description"),
                payment=_decimal(priced.get("payment")), coinsuranceAmount=_decimal(priced.get("coinsuranceAmount")),
                addOnPayment=_decimal(priced.get("addOnPayment"))))

        claim_code = return_code.get("code")
        result = dict(context, calculationVersion=cms.get("calculationVersion"), pricerReturnCode=claim_code,
                      pricerReturnDescription=return_code.get("description"),
                      geographicAdjustmentFactor=_decimal(payment.get("geographicAdjustmentFactor")),
                      totalPayment=_decimal(payment.get("totalPayment")),
                      totalCoinsurance=_decimal(payment.get("totalClaimCoinsuranceAmount")), lines=lines)
        if claim_code != "01":
            return unable("FQHC_PRICER_CLAIM_REJECTED",
                          f"The CMS FQHC Pricer did not process the claim (return code {claim_code}).", **result)
        return FqhcResponse(**base, status="Success", reason="CMS I/OCE and FQHC Pricer processing completed.", **result)

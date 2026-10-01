import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path
from threading import Lock
import jpype
from pydantic_core import to_json
from myelin.pricers.snf import SnfClient
from myelin.pricers.url_loader import UrlLoader
from bridge.snf_models import SnfRequest, SnfResponse, SegmentResult
from bridge.snf_provider import provider_for_segment, SnfInputError
from bridge.snf_release import JAR_FILENAME, JAR_SHA256, CALCULATION_VERSIONS


class SnfProcessor:
    def __init__(self, engine, jar_root):
        jar = Path(os.getenv("SNF_JAR_PATH", str(Path(jar_root) / "pricers" / JAR_FILENAME))).resolve()
        digest = hashlib.sha256(jar.read_bytes()).hexdigest()
        if digest != JAR_SHA256:
            raise RuntimeError("SNF Pricer release has not been reconciled.")
        if not jpype.isJVMStarted():
            raise RuntimeError("Start Myelin JVM before SNF processor.")
        self.engine = engine
        self.lock = Lock()
        # Use the existing Myelin class loader with a Windows-safe URI.
        self.client = SnfClient.__new__(SnfClient)
        self.client.url_loader = UrlLoader()
        self.client.url_loader.load_urls([jar.as_uri()])
        self.client.db = engine
        self.client.load_classes()
        self.client.snf_config_obj = self.client.snf_pricer_config_class()
        self.client.snf_config_obj.setCsvIngestionConfiguration(self.client.snf_csv_ingest_class())
        years = self.client.array_list_class()
        for year in CALCULATION_VERSIONS:
            years.add(self.client.java_integer_class(year))
        self.client.snf_config_obj.setSupportedYears(years)
        self.client.snf_data_table_class.loadDataTables(self.client.snf_config_obj)
        self.client.dispatch_obj = self.client.create_dispatch()
        loader = self.client.url_loader.class_loader
        self.mapper = jpype.JClass("com.fasterxml.jackson.databind.ObjectMapper", loader=loader)()
        self.mapper.registerModule(jpype.JClass("com.fasterxml.jackson.datatype.jsr310.JavaTimeModule", loader=loader)())

    def calculate(self, payload):
        # pydantic serializes Decimal as exact JSON strings; Jackson accepts them
        # as BigDecimal without a binary floating-point intermediate.
        request = self.mapper.readValue(to_json(payload).decode(), self.client.snf_pricer_request_class)
        response = self.client.dispatch_obj.process(request)
        return json.loads(str(self.mapper.writeValueAsString(response)), parse_float=Decimal)

    def process(self, request: SnfRequest) -> SnfResponse:
        results = []
        try:
            with self.lock:
                # Resolve every segment before pricing any of them.
                providers = [provider_for_segment(self.engine, request.providerCcn, s.fromDate, s.throughDate)
                             for s in request.segments]
                for segment, provider in zip(request.segments, providers):
                    payload = dict(providerData=provider, claimData=dict(
                        providerCcn=request.providerCcn, hippsCode=segment.hippsCode,
                        serviceFromDate=segment.fromDate.isoformat(), serviceThroughDate=segment.throughDate.isoformat(),
                        serviceUnits=segment.units, pdpmPriorDays=segment.pdpmPriorDays,
                        diagnosisCodes=request.diagnosisCodes))
                    cms = self.calculate(payload)
                    results.append(SegmentResult(lineId=segment.lineId, providerEffectiveDate=provider["effectiveDate"], providerData=provider, cms=cms))
            if any(r.cms.get("returnCodeData", {}).get("code") != "00" for r in results):
                return SnfResponse(claimId=request.claimId, status="UnableToPrice", reason="CMS rejected one or more HIPPS segments.", segments=results)
            amounts = [r.cms.get("paymentData", {}).get("totalPayment") for r in results]
            if any(x is None or x < 0 for x in amounts):
                raise RuntimeError("Invalid CMS total.")
            return SnfResponse(claimId=request.claimId, status="Success", reason="CMS SNF PPS calculation completed.",
                               totalPayment=sum((Decimal(str(x)) for x in amounts), Decimal(0)), segments=results)
        except SnfInputError as exc:
            return SnfResponse(claimId=request.claimId, status="UnableToPrice", reason=str(exc))

"""Nonprorated terminal statements with unchanged pinned CMS payment arithmetic."""
import copy
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
import unittest

from pydantic import ValidationError
from bridge.hha_models import HhaRequest, HhaResponse
from tests import test_hha_integration as integration
from tests.test_hha_models import request_data
from tests.reconcile_hha import normalize_transport_nulls


def request_cases():
    def make(name, days=15, event="Discharge", visits=(1, 3, 5, 8, 10, 15), units=4,
             admission="2026-06-01", notice="2026-06-01", lupa=None, outlier=False, provider="017009"):
        result = request_data()
        result.update(claimId="SYNTHETIC-SHORTENED-"+name.upper(), providerCcn=provider,
            admissionDate=admission, noticeReceiptDate=notice, throughDate=f"2026-06-{days:02d}",
            periodType="Shortened30Day", shortenedPeriod=dict(eventType=event,
                patientStatus="01" if event == "Discharge" else "20", eventDate=f"2026-06-{days:02d}",
                noSubsequentCareWithin30Days=True))
        result["visits"] = [dict(visitId=str(i+1), serviceDate=f"2026-06-{day:02d}", discipline="SN", outlierUnits=units)
            for i, day in enumerate(visits)]
        if lupa is not None:
            result["lupa"] = dict(sourceAdmissionIndicator="1", isFirstOrOnlyPeriod=lupa)
        if (date.fromisoformat(notice)-date.fromisoformat(admission)).days > 5:
            result["lateNoa"] = dict(exceptionStatus="NoException")
        if outlier:
            result["outlier"] = dict(calendarYear=2026,asOfDate="2026-06-30",priorPaymentTotal="100000.00",
                priorOutlierTotal="1000.00",totalsExcludeCurrentClaim=True,basis="SubmittedProviderYtdTotals")
        return name, result
    return [
        make("discharge_15"), make("death_15", event="Death"),
        make("death_same_day", days=1, event="Death", visits=(1,1,1,1,1,1)),
        make("discharge_29", days=29, visits=(1,5,10,15,20,28)),
        make("continuing_death_7",days=7,event="Death",visits=(2,3,4,5,6,7),admission="2026-05-01",notice="2026-05-02"),
        make("lupa_discharge_initial",visits=(1,),lupa=True),
        make("lupa_death_initial",days=7,event="Death",visits=(1,7),lupa=True),
        make("lupa_death_continuing",days=7,event="Death",visits=(2,7),lupa=False,admission="2026-05-01",notice="2026-05-02"),
        make("discharge_late6",notice="2026-06-07"),
        make("death_receipt_after_through",days=7,event="Death",visits=(1,2,3,4,5,7),notice="2026-06-16"),
        make("discharge_receipt_after_through19",notice="2026-06-20"),
        make("death_late30_zero",days=1,event="Death",visits=(1,1,1,1,1,1),notice="2026-07-01"),
        make("continuing_discharge_overlap3",admission="2026-05-01",notice="2026-06-04"),
        make("outlier_death_15",event="Death",units=16,outlier=True),
        make("outlier_discharge_7",days=7,visits=(1,2,3,4,5,7),units=16,outlier=True),
        make("outlier_death_late15",days=7,event="Death",visits=(1,2,3,4,5,7),units=16,outlier=True,notice="2026-06-16"),
        make("lupa_death_partial_exclusion",days=7,event="Death",visits=(1,7),lupa=True,notice="2026-06-07"),
        make("lupa_death_all_excluded",days=7,event="Death",visits=(1,7),lupa=True,notice="2026-06-16")]


def fixture_cases():
    return json.loads((Path(__file__).parent/"fixtures/hha-2026-shortened.json").read_text(encoding="utf-8"))["comparisons"]


class HhaShortenedChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls): integration.HhaCmsIntegrationChecks.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls): integration.HhaCmsIntegrationChecks.tearDownClass.__func__(cls)
    def price(self, payload): return self.processor.process(HhaRequest.model_validate(payload))

    def test_complete_standalone_outputs_and_terminal_context_match(self):
        self.assertEqual(len(fixture_cases()),18)
        for case in fixture_cases():
            with self.subTest(case=case["name"]):
                actual=self.price(case["request"])
                self.assertEqual(actual.status,"Success")
                self.assertEqual(actual.totalPayment,Decimal(case["expectedPayment"]))
                self.assertEqual(normalize_transport_nulls(json.loads(actual.model_dump_json())["cms"]),case["standalone"])
                self.assertEqual(json.loads(actual.model_dump_json())["shortenedPeriod"],case["bridge"]["shortenedPeriod"])
                self.assertIsNone(actual.partialPeriod)
                self.assertFalse(actual.shortenedPeriod.adjustmentApplied)
                self.assertEqual(actual.shortenedPeriod.paymentRatio,1)
                self.assertEqual(actual.shortenedPeriod.periodDays,30)
                self.assertEqual(actual.reasonCode,"HHA_LUPA_ESTIMATED" if actual.lupa else "HHA_SHORTENED_PERIOD_ESTIMATED")

    def test_through_date_does_not_prorate_any_payment_branch(self):
        for name, short in request_cases():
            with self.subTest(case=name):
                full=copy.deepcopy(short);full.update(periodType="Full30Day",throughDate="2026-06-30");full.pop("shortenedPeriod")
                terminal=self.price(short);ordinary=self.price(full)
                self.assertEqual(terminal.totalPayment,ordinary.totalPayment)
                self.assertEqual(terminal.cms,ordinary.cms)
                self.assertEqual(terminal.lupa,ordinary.lupa)
                self.assertEqual(terminal.outlier,ordinary.outlier)

    def test_payload_retains_real_through_date_and_full_payment_basis(self):
        from bridge.hha_provider import provider_for_period
        from bridge.hha_geography import resolve_geography
        for name,payload in request_cases():
            request=HhaRequest.model_validate(payload)
            provider=provider_for_period(self.db,request.providerCcn,request.fromDate,request.throughDate)
            geo=resolve_geography(request.serviceCountyFips,request.cbsaCode,self.processor.wage_rows, self.processor.geography_catalog)
            cms=self.processor.build_payload(request,provider,geo)["claimData"]
            with self.subTest(case=name):
                self.assertEqual(cms["serviceThroughDate"],payload["throughDate"])
                self.assertEqual(cms["hhrgNumberOfDays"],30)
                self.assertEqual(cms["partialEpisodePaymentIndicator"],"N")

    def test_missing_context_fields_and_conflicting_events_are_rejected(self):
        original=request_cases()[0][1]
        for field in ("eventType","patientStatus","eventDate","noSubsequentCareWithin30Days"):
            payload=copy.deepcopy(original);payload["shortenedPeriod"].pop(field)
            with self.subTest(field=field),self.assertRaises(ValidationError):HhaRequest.model_validate(payload)
        for field,value in [("eventType","Transfer"),("patientStatus","20"),("eventDate","2026-06-14"),
                ("eventDate","2026-06-15T00:00:00"),("noSubsequentCareWithin30Days",False),
                ("noSubsequentCareWithin30Days","true"),("paymentRatio",1)]:
            payload=copy.deepcopy(original);payload["shortenedPeriod"][field]=value
            with self.subTest(field=field,value=value),self.assertRaises(ValidationError):HhaRequest.model_validate(payload)
        for changes in [dict(shortenedPeriod=None),dict(periodType="Full30Day"),dict(throughDate="2026-06-30"),
                dict(periodType="Partial30Day",partialPeriod=dict(eventType="TransferToAnotherHha",patientStatus="06",nextCareStartDate="2026-06-16",isCommonOwnershipTransfer=False))]:
            payload=copy.deepcopy(original);payload.update(changes)
            with self.subTest(changes=changes),self.assertRaises(ValidationError):HhaRequest.model_validate(payload)

    def test_post_event_visits_and_death_discharge_status_mismatch_are_rejected(self):
        payload=request_cases()[1][1];payload["visits"][-1]["serviceDate"]="2026-06-16"
        with self.assertRaises(ValidationError):HhaRequest.model_validate(payload)
        payload=request_cases()[1][1];payload["shortenedPeriod"]["patientStatus"]="01"
        with self.assertRaises(ValidationError):HhaRequest.model_validate(payload)

    def test_late_noa_uses_existing_admission_rule_not_statement_length(self):
        cases=dict(request_cases())
        late=self.price(cases["death_receipt_after_through"])
        self.assertEqual(late.lateNoa.noncoveredDays,15)
        self.assertEqual(late.shortenedPeriod.statementDays,7)
        self.assertGreater(late.lateNoa.noncoveredDays,late.shortenedPeriod.statementDays)
        zero=self.price(cases["death_late30_zero"])
        self.assertEqual(zero.lateNoa.noncoveredDays,30)
        self.assertEqual(zero.totalPayment,0)
        self.assertTrue(zero.lateNoa.nonPayment)
        later=self.price(cases["continuing_discharge_overlap3"])
        self.assertEqual(later.lateNoa.adjustmentMethod,"SubsequentPeriodPenalty")
        self.assertEqual(later.lateNoa.noncoveredDays,3)

    def test_context_and_nonproration_evidence_cannot_be_forged_or_retained_on_failure(self):
        original=json.loads(self.price(request_cases()[0][1]).model_dump_json())
        for field,value in [("statementDays",14),("periodDays",15),("paymentRatio",".5"),("adjustmentApplied",True),
                ("eventDate","2026-06-14"),("noSubsequentCareWithin30Days",False),("patientStatus","20")]:
            response=copy.deepcopy(original);response["shortenedPeriod"][field]=value
            with self.subTest(field=field),self.assertRaises(ValidationError):HhaResponse.model_validate(response)
        failed=copy.deepcopy(original);failed.update(status="UnableToPrice",totalPayment=None)
        with self.assertRaises(ValidationError):HhaResponse.model_validate(failed)
        missing=copy.deepcopy(original);missing.pop("shortenedPeriod")
        with self.assertRaises(ValidationError):HhaResponse.model_validate(missing)

    def test_existing_request_and_response_optional_shapes_are_preserved(self):
        ordinary=HhaRequest.model_validate(request_data())
        self.assertNotIn("shortenedPeriod",json.loads(ordinary.model_dump_json()))
        self.assertNotIn("shortenedPeriod",json.loads(self.price(request_data()).model_dump_json()))
        failed=self.price(dict(request_cases()[0][1],hippsCode="ZZZZZ"))
        self.assertEqual(failed.reasonCode,"HHA_HIPPS_NOT_FOUND")
        self.assertIsNone(failed.shortenedPeriod)
        self.assertNotIn("shortenedPeriod",json.loads(failed.model_dump_json()))

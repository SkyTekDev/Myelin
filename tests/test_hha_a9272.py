"""A9272 fees remain separate from actual CMS PPS calculations."""
from copy import deepcopy
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from pydantic import ValidationError
from sqlalchemy.orm import Session

from bridge.hha_separate_payment_rates import APPROVED_RELEASES, read_package
from bridge.hha_separate_payment_reference import install_release
from myelin.pricers.ipsf import IPSF
from bridge.hha_models import HhaRequest, HhaResponse
from tests.test_hha_models import request_data
from tests import test_hha_integration as integration


def devices(data=None, remaining='0.00', units=1):
    data = deepcopy(data or request_data())
    data['a9272'] = dict(benefitYear=2026, asOfDate=data['throughDate'], remainingPartBDeductible=remaining,
        basis='SubmittedRemainingPartBDeductible', excludesCurrentClaim=True, medicarePrimary=True)
    data['a9272Lines'] = [dict(claimLineNumber=100, serviceDate=data['throughDate'], revenueCode='0270',
        hcpcsCode='A9272', units=units, billedCharge='500.00')]
    return data


def crossover_devices():
    data = devices(remaining="283.00")
    offset = date(2025, 12, 3) - date.fromisoformat(data["fromDate"])
    for name in ("admissionDate", "noticeReceiptDate", "fromDate", "throughDate"):
        data[name] = (date.fromisoformat(data[name]) + offset).isoformat()
    for visit in data["visits"]:
        visit["serviceDate"] = (date.fromisoformat(visit["serviceDate"]) + offset).isoformat()
    january = data.pop("a9272")
    january["asOfDate"] = data["throughDate"]
    december = dict(january, benefitYear=2025, asOfDate="2025-12-31", remainingPartBDeductible="257.00")
    data["a9272BenefitSnapshots"] = [january, december]
    current = data["a9272Lines"][0]
    current["serviceDate"] = data["throughDate"]
    data["a9272Lines"] += [dict(current, claimLineNumber=101, serviceDate="2025-12-31"),
                            dict(current, claimLineNumber=102)]
    return data


class A9272Checks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        integration.HhaCmsIntegrationChecks.setUpClass()
        cls.processor = integration.HhaCmsIntegrationChecks.processor
        db = integration.HhaCmsIntegrationChecks.db
        with Session(db) as session:
            session.add(IPSF(provider_ccn="017999", provider_type="36", effective_date=20250101,
                             federal_pps_blend="0", special_provider_update_factor=1, termination_date=None))
            session.commit()
        revision, checksum = APPROVED_RELEASES[2025]
        package = Path(__file__).resolve().parents[1] / "bridge/data/hha-separate-payment-rates-2025-v1.json"
        with db.begin() as connection:
            install_release(connection.connection.driver_connection, read_package(package, checksum),
                            checksum, revision, "isolated-test-fixture")
        cls.processor.prior_a9272_rates = cls.processor._optional_device_rates(2025)

    @classmethod
    def tearDownClass(cls):
        integration.HhaCmsIntegrationChecks.tearDownClass()

    def price(self, data):
        result = self.processor.process(HhaRequest.model_validate(data))
        HhaResponse.model_validate_json(result.model_dump_json())
        return result

    def test_full_remaining_and_partial_deductibles_with_unit_fees(self):
        for remaining, units, allowed, deductible, coins, net in [
            ('0.00',1,'282.10','0.00','56.42','225.68'),
            ('283.00',1,'282.10','282.10','0.00','0.00'),
            ('100.01',1,'282.10','100.01','36.42','145.67'),
            ('283.00',2,'564.20','283.00','56.24','224.96')]:
            with self.subTest(remaining=remaining,units=units):
                result = self.price(devices(remaining=remaining,units=units))
                self.assertEqual(result.status,'Success',result)
                paid = result.a9272
                for field, expected in [('totalDeviceAllowance',allowed),('totalAppliedDeductible',deductible),
                    ('totalCoinsurance',coins),('totalDeviceMedicarePaymentBeforeSequestration',net)]:
                    self.assertEqual(getattr(paid,field),Decimal(expected))
                self.assertEqual(result.totalPayment,Decimal('1602.87')+Decimal(allowed))

    def test_national_fee_is_not_limited_by_submitted_charge(self):
        data=devices(); data['a9272Lines'][0]['billedCharge']='0.01'
        self.assertEqual(self.price(data).a9272.totalDeviceAllowance,Decimal('282.10'))

    def test_multiple_lines_sort_and_share_one_deductible(self):
        data=devices(remaining='283.00')
        for number,revenue in enumerate(('0271','0272','0273','0279'),101):
            row=deepcopy(data['a9272Lines'][0]); row.update(claimLineNumber=number,revenueCode=revenue)
            data['a9272Lines'].append(row)
        result=self.price(data)
        data['a9272Lines'].reverse()
        self.assertEqual(result.a9272,self.price(data).a9272)
        self.assertEqual(result.a9272.totalDeviceAllowance,Decimal('1410.50'))
        self.assertEqual(result.a9272.totalAppliedDeductible,Decimal('283.00'))
        self.assertEqual(result.a9272.linePayments[1].appliedDeductible,Decimal('0.90'))

    def test_device_does_not_change_any_existing_cms_pps_scenarios(self):
        count=0
        for name in ('ordinary','lupa','partial','shortened','late-noa','outlier'):
            path=Path(__file__).parent/f'fixtures/hha-2026-{name}.json'
            if not path.exists():
                matches=list(path.parent.glob(f'*{name}*.json'))
                self.assertTrue(matches,name); path=matches[0]
            cases=json.loads(path.read_text(),parse_float=Decimal)['comparisons']
            for case in cases:
                data=case['request']
                original=self.price(data)
                if original.status!='Success': continue
                payload=devices(data)
                if payload['a9272Lines'][0]['serviceDate'] < data['noticeReceiptDate']: continue
                result=self.price(payload)
                self.assertEqual(result.status,'Success',(case.get('name'),result))
                self.assertEqual(result.cms,original.cms)
                self.assertEqual(result.a9272.ppsPayment,original.totalPayment)
                self.assertEqual(result.totalPayment,original.totalPayment+Decimal('282.10'))
                for field in ('lupa','partialPeriod','shortenedPeriod','lateNoa','outlier'):
                    self.assertEqual(getattr(result,field),getattr(original,field))
                count+=1
        self.assertGreater(count,50)

    def test_timely_pre_acceptance_and_late_pre_acceptance_guard(self):
        data=devices(); data['noticeReceiptDate']='2026-06-05'; data['a9272Lines'][0]['serviceDate']='2026-06-01'
        self.assertEqual(self.price(data).status,'Success')
        data['noticeReceiptDate']='2026-06-10'; data['lateNoa']={'exceptionStatus':'NoException'}
        failed=self.price(data)
        self.assertEqual(failed.reasonCode,'HHA_A9272_PRE_NOA_UNSUPPORTED')
        self.assertIsNone(failed.totalPayment); self.assertIsNone(failed.a9272)
        data['a9272Lines'][0]['serviceDate']='2026-06-10'
        self.assertEqual(self.price(data).status,'Success')

    def test_unavailable_db_rate_fails_device_claim_but_preserves_ordinary(self):
        with patch.object(self.processor,'a9272_rates',None):
            failed=self.price(devices())
            self.assertEqual(failed.reasonCode,'HHA_A9272_RATE_REFERENCE_INVALID')
            self.assertIsNone(failed.totalPayment); self.assertIsNone(failed.a9272)
            self.assertEqual(self.price(request_data()).totalPayment,Decimal('1602.87'))

    def test_crossover_devices_use_service_year_and_independent_deductibles(self):
        data = crossover_devices()
        control = self.price({key: value for key, value in data.items()
                              if key not in {"a9272BenefitSnapshots", "a9272Lines"}})
        result = self.price(data)
        self.assertEqual(result.status, "Success", result)
        self.assertEqual(result.cms, control.cms)
        self.assertEqual(result.a9272.ppsPayment, control.totalPayment)
        paid = result.a9272
        self.assertEqual([line.unitRate for line in paid.linePayments],
                         [Decimal("276.57"), Decimal("282.10"), Decimal("282.10")])
        self.assertEqual([line.appliedDeductible for line in paid.linePayments],
                         [Decimal("257"), Decimal("282.10"), Decimal("0.90")])
        self.assertEqual([year.rateEvidence["paymentYear"] for year in paid.benefitYearPayments], [2025, 2026])
        self.assertEqual([year.remainingPartBDeductibleAfterClaim for year in paid.benefitYearPayments], [0, 0])
        self.assertEqual(result.totalPayment, control.totalPayment + Decimal("840.77"))
        adjusted = self.price(dict(data, billType="0327"))
        self.assertEqual(adjusted.status, "Success", adjusted)
        self.assertEqual(adjusted.cms, result.cms)
        self.assertEqual(adjusted.a9272, paid)

    def test_crossover_requires_rates_only_for_years_with_devices(self):
        data = crossover_devices()
        with patch.object(self.processor, "prior_a9272_rates", None):
            self.assertEqual(self.price(data).reasonCode, "HHA_A9272_RATE_REFERENCE_INVALID")
            data["a9272BenefitSnapshots"].pop()
            data["a9272Lines"].pop(1)
            self.assertEqual(self.price(data).a9272.totalDeviceAllowance, Decimal("564.20"))

    def test_crossover_rejects_missing_duplicate_and_early_device_year_snapshots(self):
        for edit in (
            lambda data: data["a9272BenefitSnapshots"].pop(),
            lambda data: data["a9272BenefitSnapshots"][1].update(data["a9272BenefitSnapshots"][0]),
            lambda data: data["a9272BenefitSnapshots"][1].update(asOfDate="2025-12-30"),
            lambda data: data["a9272Lines"].pop(1),
        ):
            data = crossover_devices()
            edit(data)
            with self.subTest(data=data), self.assertRaises(ValidationError):
                HhaRequest.model_validate(data)

    def test_invalid_device_contract_is_rejected_before_pricing(self):
        changes=[lambda x:x.pop('a9272'), lambda x:x.pop('a9272Lines'),
            lambda x:x['a9272'].update(remainingPartBDeductible='283.01'),
            lambda x:x['a9272'].update(remainingPartBDeductible='NaN'),
            lambda x:x['a9272'].update(remainingPartBDeductible='0.001'),
            lambda x:x['a9272'].update(medicarePrimary=False),
            lambda x:x['a9272'].update(excludesCurrentClaim=False),
            lambda x:x['a9272'].update(asOfDate='2026-06-29'),
            lambda x:x['a9272'].update(benefitYear=2027),
            lambda x:x['a9272'].update(unitRate='1.00'),
            lambda x:x['a9272Lines'][0].update(units=0),
            lambda x:x['a9272Lines'][0].update(units=1.5),
            lambda x:x['a9272Lines'][0].update(units=True),
            lambda x:x['a9272Lines'][0].update(revenueCode='0274'),
            lambda x:x['a9272Lines'][0].update(billedCharge='0.00'),
            lambda x:x['a9272Lines'][0].update(serviceDate='2026-07-01'),
            lambda x:x['a9272Lines'].append(deepcopy(x['a9272Lines'][0]))]
        for change in changes:
            data=devices(); change(data)
            with self.subTest(payload=data), self.assertRaises(ValidationError): HhaRequest.model_validate(data)


if __name__=='__main__': unittest.main()

"""Pinned raw CMS evidence and independently captured late-NOA policy cases."""
import copy,json,unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from pydantic import ValidationError
from bridge.hha_models import HhaRequest,HhaResponse
from tests import test_hha_integration as integration

def cases():
    return json.loads((Path(__file__).parent/'fixtures/hha-2026-late-noa.json').read_text())['comparisons']

def data(name='ordinary_late6'):
    return next(copy.deepcopy(c['request']) for c in cases() if c['name']==name)

class HhaLateNoaChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):integration.HhaCmsIntegrationChecks.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls):integration.HhaCmsIntegrationChecks.tearDownClass.__func__(cls)
    def price(self,payload):return self.processor.process(HhaRequest.model_validate(payload))

    def test_twenty_raw_cms_and_independent_policy_expectations(self):
        self.assertEqual(len(cases()),20)
        for case in cases():
            with self.subTest(case=case['name']):
                result=self.price(case['request']);self.assertEqual(result.status,'Success')
                self.assertEqual(result.totalPayment,Decimal(case['expectedPayment']))
                actual=json.loads(result.model_dump_json());expected=copy.deepcopy(case['standalone'])
                for value in (actual['cms'],expected):
                    for field in ('finalCbsa','finalWageIndex'):
                        if value['paymentData'].get(field) is None:value['paymentData'].pop(field,None)
                self.assertEqual(actual['cms'],expected)
                self.assertEqual(actual.get('lateNoa'),case['bridge'].get('lateNoa'))

    def test_timely_five_day_boundary_preserves_historical_shape(self):
        result=self.price(data('timely_day5'))
        self.assertEqual(result.totalPayment,Decimal('1610.68'));self.assertIsNone(result.lateNoa)
        self.assertNotIn('lateNoa',json.loads(result.model_dump_json()))
        late=data('timely_day5');late['noticeReceiptDate']='2026-06-07'
        with self.assertRaises(ValidationError):HhaRequest.model_validate(late)

    def test_explicit_no_exception_context_and_dates_are_strict(self):
        for context in (None,{},dict(exceptionStatus='Granted'),dict(exceptionStatus='Requested'),
                        dict(exceptionStatus='NoException',penaltyDays=0)):
            invalid=data();invalid['lateNoa']=context
            with self.subTest(context=context),self.assertRaises(ValidationError):HhaRequest.model_validate(invalid)
        timely=data('timely_day5');timely['lateNoa']=dict(exceptionStatus='NoException')
        with self.assertRaises(ValidationError):HhaRequest.model_validate(timely)
        invalid=data();invalid['noticeReceiptDate']='2026-05-31'
        with self.assertRaises(ValidationError):HhaRequest.model_validate(invalid)

    def test_period_penalty_is_before_vbp_and_final_liability_reconciles(self):
        result=self.price(data());detail=result.lateNoa
        self.assertEqual(detail.grossPayment,Decimal('1610.68'));self.assertEqual(detail.cmsPenaltyAmount,Decimal('320.57'))
        self.assertEqual(detail.penaltyAmount,Decimal('322.14'));self.assertEqual(detail.netPayment,Decimal('1288.54'))
        self.assertTrue(detail.providerLiability);self.assertEqual(detail.beneficiaryLiabilityAmount,0)
        self.assertFalse(detail.nonPayment)

    def test_subsequent_period_has_no_new_notice_grace(self):
        result=self.price(data('subsequent_overlap3'));detail=result.lateNoa
        self.assertEqual(detail.adjustmentMethod,'SubsequentPeriodPenalty');self.assertEqual(detail.noncoveredDays,3)
        self.assertEqual(detail.cmsPayment,Decimal('1610.68'));self.assertEqual(detail.cmsPenaltyAmount,0)
        self.assertEqual(result.totalPayment,Decimal('1449.61'));self.assertEqual(detail.penaltyAmount,Decimal('161.07'))
        before=self.price(data('subsequent_receipt_before'));self.assertEqual(before.lateNoa.noncoveredDays,0)
        self.assertEqual(before.lateNoa.penaltyAmount,0);self.assertEqual(before.totalPayment,Decimal('1610.68'))

    def test_lupa_excludes_original_addon_without_moving_it(self):
        result=self.price(data('lupa_initial_excluded'));detail=result.lateNoa
        self.assertEqual(result.totalPayment,Decimal('129.44'));self.assertEqual(detail.cmsPayment,Decimal('352.05'))
        self.assertEqual(detail.adjustmentMethod,'LupaVisitExclusion');self.assertEqual(detail.cmsPenaltyAmount,0)
        self.assertEqual([v.totalPayment for v in result.lupa.visitPayments],[Decimal('0'),Decimal('129.44')])
        self.assertEqual(sum(v.addOnPayment for v in result.lupa.visitPayments),0)
        self.assertEqual(sum(v.addOnPenaltyAmount for v in detail.visitPenalties),Decimal('93.19'))
        self.assertEqual(result.cms['paymentData']['totalQuantityOfCoveredVisits'],2)

    def test_addon_on_accepted_receipt_date_is_payable(self):
        result=self.price(data('lupa_addon_on_receipt'))
        self.assertEqual(result.totalPayment,Decimal('222.63'))
        self.assertEqual(result.lupa.visitPayments[0].totalPayment,0)
        self.assertEqual(result.lupa.visitPayments[1].addOnPayment,Decimal('93.19'))

    def test_eligible_count_recalculation_preserves_residual_cents(self):
        result=self.price(data('lupa_three_to_one_cent'));detail=result.lateNoa
        self.assertEqual(result.totalPayment,Decimal('129.44'))
        self.assertEqual(sum(v.grossPayment for v in detail.visitPenalties),Decimal('388.30'))
        self.assertEqual(sum(v.penaltyAmount for v in detail.visitPenalties),Decimal('258.86'))
        self.assertTrue(all(v.grossPayment>=v.netPayment for v in detail.visitPenalties))
        self.assertEqual(detail.visitPenalties[-1].netPayment,Decimal('129.44'))

    def test_zero_payment_is_priced_and_capped_with_provider_liability(self):
        for name in ('ordinary_late30_zero','ordinary_late45_zero','partial_late30_zero','lupa_all_excluded_zero'):
            with self.subTest(name=name):
                result=self.price(data(name));self.assertEqual(result.status,'Success');self.assertEqual(result.totalPayment,0)
                self.assertTrue(result.lateNoa.nonPayment);self.assertEqual(result.lateNoa.grossPayment,result.lateNoa.penaltyAmount)
                self.assertEqual(result.lateNoa.beneficiaryLiabilityAmount,0)

    def test_partial_and_lupa_precedence_remain_explicit(self):
        result=self.price(data('partial_late6'));self.assertEqual(result.totalPayment,Decimal('644.27'))
        self.assertTrue(result.partialPeriod.adjustmentApplied);self.assertEqual(result.cms['returnCodeData']['code'],'09')
        result=self.price(data('partial_lupa_late'));self.assertFalse(result.partialPeriod.adjustmentApplied)
        self.assertEqual(result.totalPayment,Decimal('129.44'));self.assertEqual(result.cms['returnCodeData']['code'],'14')

    def test_raw_cms_penalty_and_money_tampering_is_rejected(self):
        original=self.price(data()).cms
        for field in ('totalPayment','hhrgPayment','lateSubmissionPenaltyAmount','valueBasedPurchasingAdjustmentAmount'):
            forged=copy.deepcopy(original);forged['paymentData'][field]+=Decimal('.01')
            with self.subTest(field=field),patch.object(self.processor,'calculate',return_value=forged),self.assertRaises(RuntimeError):self.price(data())

    def test_response_liability_and_failure_evidence_reconcile(self):
        original=json.loads(self.price(data()).model_dump_json())
        for field,value in [('grossPayment','1'),('penaltyAmount','1'),('netPayment','1'),('nonPayment',True),('beneficiaryLiabilityAmount','1')]:
            forged=copy.deepcopy(original);forged['lateNoa'][field]=value
            with self.subTest(field=field),self.assertRaises(ValidationError):HhaResponse.model_validate(forged)
        failed=copy.deepcopy(original);failed.update(status='UnableToPrice',totalPayment=None)
        with self.assertRaises(ValidationError):HhaResponse.model_validate(failed)

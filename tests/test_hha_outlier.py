"""Pinned CMS outlier fixtures, cap history, late NOA, and failure safeguards."""
import copy
import json
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError
from bridge.hha_models import HhaRequest, HhaResponse
from tests import test_hha_integration as integration
from tests.test_hha_models import request_data


class HhaOutlierChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        integration.HhaCmsIntegrationChecks.setUpClass()
        cls.processor = integration.HhaCmsIntegrationChecks.processor
        cls.cases = json.loads((Path(__file__).parent/'fixtures/hha-2026-outlier.json').read_text(), parse_float=Decimal)['comparisons']
        cls.by_name = {case['name']: case for case in cls.cases}

    @classmethod
    def tearDownClass(cls):
        integration.HhaCmsIntegrationChecks.tearDownClass()

    def model(self, name='ordinary_paid'):
        return HhaRequest.model_validate(copy.deepcopy(self.by_name[name]['request']))

    @staticmethod
    def normalize(cms):
        cms = copy.deepcopy(cms)
        for field in ('finalCbsa','finalWageIndex'):
            if cms['paymentData'].get(field) is None:
                cms['paymentData'].pop(field,None)
        money_fields={'totalPayment','hhrgPayment','hhrgWeight','lateSubmissionPenaltyAmount','outlierPayment','standardizedPayment','valueBasedPurchasingAdjustmentAmount','finalWageIndex','addOnVisitAmount','cost','dollarRate'}
        def walk(value):
            if isinstance(value,dict):
                for key,child in value.items():
                    if key in money_fields and child is not None:value[key]=Decimal(child)
                    else:walk(child)
            elif isinstance(value,list):
                for child in value:walk(child)
        walk(cms)
        return cms

    def test_all_independent_standalone_and_decimal_cases_match(self):
        self.assertEqual(len(self.cases),25)
        for case in self.cases:
            with self.subTest(name=case['name']):
                output = self.processor.process(HhaRequest.model_validate(case['request']))
                self.assertEqual(output.status,'Success',output)
                self.assertEqual(output.totalPayment,Decimal(case['expectedPayment']))
                self.assertEqual(self.normalize(output.cms),self.normalize(case['standalone']))
                for field, value in case['expectedOutlier'].items():
                    actual = getattr(output.outlier,field)
                    self.assertEqual(actual,value if isinstance(actual,bool) else Decimal(value))

    def test_positive_outlier_is_added_to_period_once(self):
        output = self.processor.process(self.model())
        self.assertEqual(output.outlier.candidateOutlierBeforeVbp,Decimal('1590.74'))
        self.assertEqual(output.outlier.netOutlierPayment,Decimal('1598.49'))
        self.assertEqual(output.totalPayment,Decimal('3209.17'))
        self.assertEqual(output.cms['paymentData']['hhrgPayment']+output.outlier.netOutlierPayment,output.totalPayment)

    def test_cap_equality_pays_and_insufficient_pool_denies_whole_candidate(self):
        equal = self.processor.process(self.model('ordinary_cap_equality'))
        denied = self.processor.process(self.model('ordinary_cap_denied'))
        self.assertEqual(equal.outlier.availableOutlierPool,equal.outlier.candidateOutlierBeforeVbp)
        self.assertFalse(equal.outlier.capLimited)
        self.assertEqual(denied.outlier.availableOutlierPool,Decimal('999.00'))
        self.assertTrue(denied.outlier.capLimited)
        self.assertEqual(denied.outlier.netOutlierPayment,0)
        self.assertEqual(denied.cms['returnCodeData']['code'],'02')

    def test_zero_history_cap_correction_preserves_raw_cms(self):
        output = self.processor.process(self.model('ordinary_zero_history'))
        self.assertEqual(output.cms['returnCodeData']['code'],'01')
        self.assertEqual(output.cms['paymentData']['outlierPayment'],Decimal('1598.49'))
        self.assertEqual(output.outlier.effectiveReturnCode,'02')
        self.assertEqual(output.outlier.adjustmentMethod,'ZeroHistoryCapEnforcement')
        self.assertTrue(output.outlier.capBypassedByCms)
        self.assertEqual(output.totalPayment,Decimal('1610.68'))

    def test_zero_prior_outliers_only_enforces_declared_pool(self):
        low = self.processor.process(self.model('zero_outlier_low_pool'))
        enough = self.processor.process(self.model('zero_outlier_sufficient'))
        self.assertTrue(low.outlier.capLimited)
        self.assertTrue(low.outlier.capBypassedByCms)
        self.assertFalse(enough.outlier.capLimited)
        self.assertTrue(enough.outlier.capBypassedByCms)
        self.assertEqual(enough.totalPayment,Decimal('3209.17'))

    def test_partial_period_cap_denial_keeps_adjustment_evidence(self):
        output = self.processor.process(self.model('partial_cap_denied'))
        self.assertEqual(output.cms['returnCodeData']['code'],'02')
        self.assertTrue(output.partialPeriod.adjustmentApplied)
        self.assertEqual(output.outlier.periodPaymentBeforeVbp,Decimal('801.44'))
        self.assertEqual(output.totalPayment,Decimal('805.34'))

    def test_late_noa_penalty_includes_paid_outlier(self):
        output = self.processor.process(self.model('ordinary_late6_paid'))
        self.assertEqual(output.lateNoa.grossPayment,Decimal('3209.17'))
        self.assertEqual(output.totalPayment,Decimal('2567.33'))
        self.assertEqual(output.lateNoa.penaltyAmount,Decimal('641.84'))
        self.assertEqual(output.outlier.netOutlierPayment,Decimal('1278.79'))

    def test_zero_history_cap_is_applied_before_late_noa(self):
        output = self.processor.process(self.model('late6_zero_history'))
        self.assertEqual(output.lateNoa.grossPayment,Decimal('1610.68'))
        self.assertEqual(output.lateNoa.cmsPayment,Decimal('2567.33'))
        self.assertEqual(output.totalPayment,Decimal('1288.54'))
        self.assertEqual(output.outlier.netOutlierPayment,0)

    def test_subsequent_period_correction_includes_outlier(self):
        output = self.processor.process(self.model('subsequent_overlap3'))
        self.assertEqual(output.lateNoa.adjustmentMethod,'SubsequentPeriodPenalty')
        self.assertEqual(output.lateNoa.cmsPayment,Decimal('3209.17'))
        self.assertEqual(output.totalPayment,Decimal('2888.25'))

    def test_late_full_nonpayment_remains_success(self):
        output = self.processor.process(self.model('ordinary_late30_zero'))
        self.assertEqual(output.status,'Success')
        self.assertEqual(output.totalPayment,0)
        self.assertTrue(output.lateNoa.nonPayment)
        self.assertEqual(output.outlier.netOutlierPayment,0)
        self.assertGreater(output.outlier.grossOutlierPayment,0)

    def test_lupa_never_receives_outlier_even_with_high_units(self):
        for name in ('lupa_not_applicable','partial_lupa_not_applicable'):
            output = self.processor.process(self.model(name))
            self.assertFalse(output.outlier.appliesToPeriod)
            self.assertEqual(output.outlier.adjustmentMethod,'LupaNotApplicable')
            self.assertEqual(output.outlier.candidateOutlierBeforeVbp,0)
            self.assertEqual(output.outlier.netOutlierPayment,0)
            self.assertGreater(output.outlier.imputedCost,0)

    def test_missing_history_fails_without_amount(self):
        data = copy.deepcopy(self.by_name['ordinary_paid']['request']);data.pop('outlier')
        output = self.processor.process(HhaRequest.model_validate(data))
        self.assertEqual(output.reasonCode,'HHA_OUTLIER_CONTEXT_REQUIRED')
        self.assertIsNone(output.totalPayment)
        self.assertIsNone(output.outlier)

    def test_context_requires_typed_complete_truthful_history(self):
        mutations = [lambda d:d['outlier'].pop('basis'),lambda d:d['outlier'].update(calendarYear=2025),
            lambda d:d['outlier'].update(asOfDate='2026-06-29'),lambda d:d['outlier'].update(asOfDate='2027-01-01'),
            lambda d:d['outlier'].update(totalsExcludeCurrentClaim=False),lambda d:d['outlier'].update(totalsExcludeCurrentClaim='true'),
            lambda d:d['outlier'].update(priorPaymentTotal='-1'),lambda d:d['outlier'].update(priorPaymentTotal='1.001'),
            lambda d:d['outlier'].update(priorPaymentTotal='1000000000'),lambda d:d['outlier'].update(priorPaymentTotal='NaN'),
            lambda d:d['outlier'].update(priorOutlierTotal='100001'),lambda d:d['outlier'].update(availableOutlierPool='99999')]
        for mutate in mutations:
            data=copy.deepcopy(self.by_name['ordinary_paid']['request']);mutate(data)
            with self.subTest(data=data['outlier']),self.assertRaises(ValidationError):HhaRequest.model_validate(data)

    def test_tampered_cms_money_counts_codes_and_components_fail_closed(self):
        model=self.model();original=self.processor.process(model).cms
        mutations=[lambda c:c.update(calculationVersion='2027.0'),lambda c:c['returnCodeData'].update(code='00'),
            lambda c:c['paymentData'].update(totalPayment=Decimal('3209.18')),lambda c:c['paymentData'].update(hhrgPayment=Decimal('1610.69')),
            lambda c:c['paymentData'].update(outlierPayment=Decimal('1598.50')),lambda c:c['paymentData'].update(totalQuantityOfCoveredVisits=True),
            lambda c:c['paymentData'].update(lateSubmissionPenaltyAmount=Decimal('.01')),lambda c:c['paymentData'].update(standardizedPayment=Decimal('4398.16')),
            lambda c:c['paymentData']['revenuePayments'][3].update(cost=Decimal('4140.24')),
            lambda c:c['paymentData']['revenuePayments'][3].update(dollarRate=Decimal('1')),
            lambda c:c['paymentData']['revenuePayments'][3].update(addOnVisitAmount=Decimal('.01')),
            lambda c:c['paymentData'].update(finalWageIndex=Decimal('1'))]
        for mutate in mutations:
            altered=copy.deepcopy(original);mutate(altered)
            with self.subTest(altered=altered),patch.object(self.processor,'calculate',return_value=altered):
                if altered['returnCodeData']['code']=='00':
                    with self.assertRaises(RuntimeError):self.processor.process(model)
                else:
                    with self.assertRaises(RuntimeError):self.processor.process(model)

    def test_original_requests_and_responses_omit_absent_outlier(self):
        model=HhaRequest.model_validate(request_data())
        output=self.processor.process(model)
        self.assertNotIn('outlier',json.loads(output.model_dump_json()))
        self.assertIsNone(model.outlier)

    def test_failure_cannot_retain_outlier_payment_evidence(self):
        output=self.processor.process(self.model()).model_dump()
        output.update(status='UnableToPrice',totalPayment=None)
        with self.assertRaises(ValidationError):HhaResponse.model_validate(output)


if __name__=='__main__':unittest.main()

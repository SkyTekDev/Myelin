"""Approved admission-wide MAC waivers with real pinned CMS calculations."""
import copy
import json
import unittest
from datetime import date, timedelta
from pathlib import Path
from pydantic import ValidationError
from bridge.hha_models import HhaRequest, HhaResponse
from tests.test_hha_late_noa import cases, data
from tests import test_hha_integration as integration


def approved(payload):
    payload = copy.deepcopy(payload)
    payload['lateNoa'] = dict(exceptionStatus='ApprovedException', approval=dict(
        basis='SubmittedMacApproval', scope='EntireAdmission', decisionReference='SYNTHETIC-MAC-NOA-001',
        decisionDate=max(payload['noticeReceiptDate'], payload['throughDate']), providerCcn=payload['providerCcn'],
        admissionDate=payload['admissionDate'], noticeReceiptDate=payload['noticeReceiptDate']))
    return payload


class HhaNoaExceptionChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls): integration.HhaCmsIntegrationChecks.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls): integration.HhaCmsIntegrationChecks.tearDownClass.__func__(cls)

    def test_approved_waivers_restore_gross_without_changing_actual_dates_or_thresholds(self):
        for case in cases():
            payload = case['request']
            if not payload.get('lateNoa'): continue
            with self.subTest(case=case['name']):
                original = self.processor.process(HhaRequest.model_validate(payload))
                request = HhaRequest.model_validate(approved(payload))
                result = self.processor.process(request)
                self.assertEqual(result.status, 'Success')
                self.assertEqual(result.totalPayment, original.lateNoa.grossPayment)
                self.assertEqual(result.cms['paymentData']['lateSubmissionPenaltyAmount'], 0)
                self.assertEqual(result.lateNoa.adjustmentMethod, 'ApprovedExceptionWaiver')
                self.assertEqual(result.lateNoa.noncoveredDays, 0)
                self.assertEqual(result.lateNoa.penaltyAmount, 0)
                self.assertFalse(result.lateNoa.providerLiability)
                self.assertEqual(result.lateNoa.approval, request.lateNoa.approval)
                self.assertEqual(result.lateNoa.noticeReceiptDate, request.noticeReceiptDate)
                cms_input = self.processor.build_payload(request,
                    dict(providerData=result.providerData, qualityReportingProgramIndicator=result.providerEvidence['initialPaymentQualityReportingProgramIndicator']),
                    result.geographyEvidence)
                self.assertEqual(cms_input['claimData']['lateFilingPenaltyWaiverIndicator'], 'Y')
                self.assertEqual(cms_input['claimData']['noticeReceiptDate'], payload['noticeReceiptDate'])
                self.assertEqual(result.cms['paymentData']['totalQuantityOfCoveredVisits'], len(request.visits))
                if result.lupa:
                    self.assertEqual(sum(v.totalPayment for v in result.lupa.visitPayments), result.totalPayment)
                    self.assertTrue(all(v.penaltyAmount == 0 for v in result.lateNoa.visitPenalties))

    def test_outlier_and_shortened_waivers_preserve_other_adjustments(self):
        for file in ('hha-2026-outlier.json', 'hha-2026-shortened.json'):
            fixture = json.loads((Path(__file__).parent/'fixtures'/file).read_text())
            for case in fixture['comparisons']:
                payload = copy.deepcopy(case['request'])
                payload['noticeReceiptDate'] = (date.fromisoformat(payload['admissionDate'])+timedelta(days=6)).isoformat()
                payload['lateNoa'] = dict(exceptionStatus='NoException')
                with self.subTest(file=file, case=case['name']):
                    original = self.processor.process(HhaRequest.model_validate(payload))
                    if original.status != 'Success': continue
                    result = self.processor.process(HhaRequest.model_validate(approved(payload)))
                    self.assertEqual(result.status, 'Success')
                    self.assertEqual(result.totalPayment, original.lateNoa.grossPayment)
                    if result.outlier:
                        self.assertEqual(result.outlier.netOutlierPayment, result.outlier.grossOutlierPayment)
                        self.assertEqual(result.outlier.capLimited, original.outlier.capLimited)
                    if result.shortenedPeriod: self.assertFalse(result.shortenedPeriod.adjustmentApplied)

    def test_approval_is_explicit_and_binds_the_admission(self):
        changes = [('basis','KX'), ('scope','PartialAdmission'), ('decisionReference',' '),
            ('decisionReference','bad\nreference'), ('decisionReference','x'*129),
            ('providerCcn','017998'), ('admissionDate','2026-05-31'),
            ('noticeReceiptDate','2026-06-08'), ('decisionDate','2026-06-06')]
        for key,value in changes:
            payload=approved(data());payload['lateNoa']['approval'][key]=value
            with self.subTest(field=key,value=value), self.assertRaises(ValidationError): HhaRequest.model_validate(payload)
        for status in ('Requested','Pending','Denied','Granted','NoException'):
            payload=approved(data());payload['lateNoa']['exceptionStatus']=status
            with self.subTest(status=status), self.assertRaises(ValidationError): HhaRequest.model_validate(payload)
        payload=approved(data());del payload['lateNoa']['approval']
        with self.assertRaises(ValidationError): HhaRequest.model_validate(payload)
        payload=approved(data('timely_day5'))
        with self.assertRaises(ValidationError): HhaRequest.model_validate(payload)

    def test_waiver_evidence_cannot_claim_penalties_or_provider_liability(self):
        result=self.processor.process(HhaRequest.model_validate(approved(data())))
        for field,value in [('noncoveredDays',6),('providerLiability',True),('adjustmentMethod','CmsPeriodPenalty'),
                            ('cmsPenaltyAmount','1'),('approval',None)]:
            payload=json.loads(result.model_dump_json());payload['lateNoa'][field]=value
            with self.subTest(field=field), self.assertRaises(ValidationError): HhaResponse.model_validate(payload)

    def test_old_no_exception_shape_is_unchanged(self):
        request=HhaRequest.model_validate(data())
        result=self.processor.process(request)
        self.assertNotIn('approval',json.loads(request.model_dump_json())['lateNoa'])
        self.assertNotIn('approval',json.loads(result.model_dump_json())['lateNoa'])

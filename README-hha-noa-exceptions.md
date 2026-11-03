# CY 2026 approved late-NOA exceptions (Myelin)

Original institutional 329/0329 claims with supplied HIPPS can now use a submitted, admission-wide MAC approval to waive late-NOA reductions. Ordinary PPS, LUPA, partial periods, shortened discharge/death statements and outlier payments retain their existing rules. Eligibility and the approval itself are supplied facts; neither service authenticates a decision with the MAC.

CMS distinguishes a requested exception from an approved exception. KX on the revenue 0023 HIPPS line identifies the request; it does not establish approval. See [CMS Claims Processing Manual, chapter 10](https://www.cms.gov/regulations-and-guidance/guidance/manuals/downloads/clm104c10.pdf), sections 10.1.10.3 and 40.2 (reviewed November 2, 2026).

## Request

Keep the actual admission and accepted NOA dates. On the public .NET request, use exactly one KX modifier on the 0023 HIPPS line and supply:

```json
"lateNoa": {
  "exceptionStatus": "ApprovedException",
  "approval": {
    "basis": "SubmittedMacApproval",
    "scope": "EntireAdmission",
    "decisionReference": "SYNTHETIC-MAC-NOA-001",
    "decisionDate": "2026-06-30",
    "providerCcn": "067541",
    "admissionDate": "2026-06-01",
    "noticeReceiptDate": "2026-06-07"
  }
}
```

The provider, admission and accepted notice must match the claim. The decision date must be on or after NOA acceptance. The decision reference must contain 1–128 characters, without surrounding whitespace or control characters. This first pass supports an approval covering the entire admission; partial waivers and requested/pending decisions are rejected. Supply a real retained decision reference for actual claims. The synthetic example is only for tests.

A late NOA without an exception still uses `{"exceptionStatus":"NoException"}` without approval or KX. Timely NOAs omit `lateNoa`. An approved context without KX, KX without approval, duplicate KX, or other HIPPS modifiers is rejected by the .NET API before persistence. Visit modifiers remain unsupported. All adjusted claims retain complete, uniquely bound covered visits.

Myelin receives the typed approval context. The API validates the actual claim-line KX; callers cannot supply a pricer waiver flag or monetary override. Myelin sets the native CMS `lateFilingPenaltyWaiverIndicator` to `Y` only after validating approval, and retains the actual NOA date. Existing `NoException` requests still set `N`.

## Calculation and evidence

Approved exceptions retain the complete HIPPS visit threshold and original LUPA add-on eligibility. All otherwise payable visits remain in the LUPA allocation. Partial-period proration, shortened-statement rules, provider adjustments and the annual outlier cap continue to apply; an NOA waiver never waives an outlier cap.

The `lateNoa` response includes the exact approval, original dates, `adjustmentMethod: ApprovedExceptionWaiver`, zero late-NOA penalty/noncovered days, and `providerLiability: false` for that penalty. Raw CMS evidence remains separate from any outlier-cap policy adjustment. The API independently recalculates amounts and verifies the returned approval matches the request before accepting payment.

A9272 supplied on/after late-NOA acceptance can accompany an approved waiver, using the existing versioned national rate and beneficiary cost-sharing context. **A9272 supplied before acceptance of a late NOA remains unsupported even with approval**, returning `HHA_A9272_PRE_NOA_UNSUPPORTED` for the whole claim. The reviewed guidance does not sufficiently establish that separate-device coverage case. No accepted financial values are saved for that failure.

## Persistence and rollout

No schema migration or reference-data load is needed in either database. Approval facts persist in the existing HH input JSON; decision evidence, raw CMS calculation and amounts persist in the existing response JSON, receipt and pricing audits. The change does not create an approval registry or contact a MAC. Deploy both application changes together before sending the new context. Existing HH settings and reference loads remain required.

Absent approval properties are omitted from serialization, preserving historical `NoException` request hashes and receipts. New successful originals replay without additional runs, changed inputs return 409, unpriced retries preserve prior failure evidence, and malformed inputs return 400 without claim records. A later approval changes the original payload; replacing a previously submitted original remains outside this pass until 0327 adjustment support exists.

Run `python -m unittest tests.test_hha_noa_exception` for pinned CMS waiver checks. Existing late-NOA fixtures must remain unchanged. The companion MedicareServiceAPI repository contains live .NET/SQL fixtures, independent response validation, and an end-to-end example. No recurring approval-data load is introduced.

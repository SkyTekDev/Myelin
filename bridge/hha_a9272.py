"""Separate national device allowance and explicit beneficiary cost sharing."""
from decimal import Decimal, ROUND_HALF_UP

from bridge.hha_provider import HhaInputError

CENT = Decimal('.01')


def rounded(value):
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def calculate(request, pps_payment, rates):
    if not request.a9272Lines:
        return pps_payment, None
    if rates is None:
        raise HhaInputError('HHA_A9272_RATE_REFERENCE_INVALID', 'The approved A9272 database rate is unavailable.')
    if request.lateNoa is not None and any(line.serviceDate < request.noticeReceiptDate for line in request.a9272Lines):
        raise HhaInputError('HHA_A9272_PRE_NOA_UNSUPPORTED',
            'Devices furnished before acceptance of a late NOA require separate coverage assessment; no payment is accepted.')
    proof = rates.a9272_evidence()
    rate, fraction = Decimal(proof['unitRate']), Decimal(proof['coinsuranceRate'])
    context = request.a9272
    if context.remainingPartBDeductible > Decimal(proof['annualPartBDeductible']):
        raise HhaInputError('HHA_A9272_BENEFIT_CONTEXT_INVALID', 'The supplied remaining deductible exceeds the annual CMS amount.')
    remaining = context.remainingPartBDeductible
    lines = []
    for line in sorted(request.a9272Lines, key=lambda row: (row.serviceDate, row.claimLineNumber)):
        allowed = rounded(rate * line.units)
        deductible = min(remaining, allowed)
        remaining -= deductible
        coinsurance = rounded((allowed - deductible) * fraction)
        lines.append(dict(claimLineNumber=line.claimLineNumber, serviceDate=line.serviceDate,
            revenueCode=line.revenueCode, hcpcsCode='A9272', units=line.units, billedCharge=line.billedCharge,
            unitRate=rate, allowedAmount=allowed, appliedDeductible=deductible,
            coinsuranceAmount=coinsurance, beneficiaryLiabilityAmount=deductible + coinsurance,
            medicarePaymentBeforeSequestration=allowed - deductible - coinsurance))
    total = lambda name: sum((row[name] for row in lines), Decimal('0.00'))
    allowed = total('allowedAmount')
    detail = dict(**context.model_dump(), ppsPayment=pps_payment, rateEvidence=proof, linePayments=lines,
        totalDeviceAllowance=allowed, totalAppliedDeductible=total('appliedDeductible'),
        totalCoinsurance=total('coinsuranceAmount'), totalBeneficiaryLiability=total('beneficiaryLiabilityAmount'),
        totalDeviceMedicarePaymentBeforeSequestration=total('medicarePaymentBeforeSequestration'),
        totalMedicarePaymentBeforeSequestration=pps_payment + total('medicarePaymentBeforeSequestration'),
        remainingPartBDeductibleAfterClaim=remaining, allowanceBasis='NationalDeviceFeeBeforeCostSharing')
    return pps_payment + allowed, detail

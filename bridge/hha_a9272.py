"""Separate national device allowance and explicit beneficiary cost sharing."""
from decimal import Decimal, ROUND_HALF_UP

from bridge.hha_provider import HhaInputError

CENT = Decimal('.01')


def rounded(value):
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def calculate(request, pps_payment, rates, prior_rates=None):
    if not request.a9272Lines:
        return pps_payment, None
    if request.lateNoa is not None and any(line.serviceDate < request.noticeReceiptDate for line in request.a9272Lines):
        raise HhaInputError('HHA_A9272_PRE_NOA_UNSUPPORTED',
            'Devices furnished before acceptance of a late NOA require separate coverage assessment; no payment is accepted.')
    contexts = sorted(request.a9272BenefitSnapshots or [request.a9272], key=lambda context: context.benefitYear)
    proofs, remaining, unit_rates, fractions = {}, {}, {}, {}
    for context in contexts:
        year = context.benefitYear
        reference = rates if year == request.release.payment_year else prior_rates
        proof = reference.a9272_evidence() if reference is not None else None
        if proof is None or proof['paymentYear'] != year:
            raise HhaInputError('HHA_A9272_RATE_REFERENCE_INVALID', 'The approved A9272 database rate is unavailable for a device year.')
        if context.remainingPartBDeductible > Decimal(proof['annualPartBDeductible']):
            raise HhaInputError('HHA_A9272_BENEFIT_CONTEXT_INVALID', 'The supplied remaining deductible exceeds the annual CMS amount.')
        proofs[year] = proof
        remaining[year] = context.remainingPartBDeductible
        unit_rates[year] = Decimal(proof['unitRate'])
        fractions[year] = Decimal(proof['coinsuranceRate'])
    lines = []
    for line in sorted(request.a9272Lines, key=lambda row: (row.serviceDate, row.claimLineNumber)):
        year = line.serviceDate.year
        rate = unit_rates[year]
        allowed = rounded(rate * line.units)
        deductible = min(remaining[year], allowed)
        remaining[year] -= deductible
        coinsurance = rounded((allowed - deductible) * fractions[year])
        lines.append(dict(claimLineNumber=line.claimLineNumber, serviceDate=line.serviceDate,
            revenueCode=line.revenueCode, hcpcsCode='A9272', units=line.units, billedCharge=line.billedCharge,
            unitRate=rate, allowedAmount=allowed, appliedDeductible=deductible,
            coinsuranceAmount=coinsurance, beneficiaryLiabilityAmount=deductible + coinsurance,
            medicarePaymentBeforeSequestration=allowed - deductible - coinsurance))
    total = lambda name: sum((row[name] for row in lines), Decimal('0.00'))
    allowed = total('allowedAmount')
    detail = dict(ppsPayment=pps_payment, linePayments=lines,
        totalDeviceAllowance=allowed, totalAppliedDeductible=total('appliedDeductible'),
        totalCoinsurance=total('coinsuranceAmount'), totalBeneficiaryLiability=total('beneficiaryLiabilityAmount'),
        totalDeviceMedicarePaymentBeforeSequestration=total('medicarePaymentBeforeSequestration'),
        totalMedicarePaymentBeforeSequestration=pps_payment + total('medicarePaymentBeforeSequestration'),
        allowanceBasis='NationalDeviceFeeBeforeCostSharing')
    year_payments = [dict(**context.model_dump(), rateEvidence=proofs[context.benefitYear],
                          remainingPartBDeductibleAfterClaim=remaining[context.benefitYear]) for context in contexts]
    if request.a9272BenefitSnapshots is None:
        detail.update(year_payments[0])
    else:
        detail['benefitYearPayments'] = year_payments
    return pps_payment + allowed, detail

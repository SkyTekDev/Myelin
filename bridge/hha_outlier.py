"""CY 2026 outlier arithmetic, exact CMS evidence, and explicit annual cap facts.

The zero-prior-total bypass in the executable is retained as raw evidence; the
submitted annual cap is enforced separately, without invented sentinel inputs.
"""
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN


def rounded(value):
    return value.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)


def adjusted(value, wage):
    return rounded(value * Decimal('.749') * wage) + rounded(value * Decimal('.251'))


def number(value):
    if isinstance(value, (bool, float)) or not isinstance(value, (str, int, Decimal)):
        raise RuntimeError('Invalid outlier monetary evidence.')
    value = Decimal(value)
    if not value.is_finite():
        raise RuntimeError('Nonfinite outlier monetary evidence.')
    return value


def expected(request, provider, geography, hrg, revenue_rows, is_lupa=False):
    context = request.outlier
    wage = number(geography['wageIndex'])
    vbp = number(provider['providerData']['vbpAdjustment'])
    qrp = provider['qualityReportingProgramIndicator']
    base = Decimal('2038.22' if qrp == '0' else '1998.41')
    full_h = adjusted(rounded(number(hrg['weight']) * base), wage)
    days = (max(v.serviceDate for v in request.visits)-min(v.serviceDate for v in request.visits)).days+1
    h = rounded(full_h * rounded(Decimal(days)/30)) if request.partialPeriod else full_h
    fdl = adjusted(rounded(base * Decimal('.37')), wage)
    components = []
    cost = Decimal('0.00')
    rural = geography['cbsaCode'].startswith('999')
    prefix = ('rural' if rural else 'nonrural') + ('Unsubmitted' if qrp == '2' else '')
    for discipline, revenue in (('PT','0420'),('OT','0430'),('SLP','0440'),('SN','0550'),('MSW','0560'),('HHA','0570')):
        row = revenue_rows[revenue]
        visits = [v for v in request.visits if v.discipline == discipline]
        unit_cost = adjusted(rounded(number(row[prefix+'UnitDollarRate']) * sum(v.outlierUnits for v in visits)), wage)
        cost += unit_cost
        components.append(dict(revenueCode=revenue, dollarRate=number(row[prefix+'DollarRate']), cost=unit_cost, addOnVisitAmount=Decimal('0.00')))
    threshold = h + fdl
    candidate = rounded(max(Decimal(0), cost-threshold) * Decimal('.8')) if not is_lupa else Decimal('0.00')
    pool = max(Decimal('0.00'), rounded(context.priorPaymentTotal*Decimal('.1'))-context.priorOutlierTotal)
    limited = candidate > pool
    bypassed = candidate > 0 and (context.priorPaymentTotal == 0 or context.priorOutlierTotal == 0)
    cms_o = candidate if bypassed or not limited else Decimal('0.00')
    policy_o = Decimal('0.00') if limited else candidate
    delay = max(0, min(30, (request.noticeReceiptDate-request.fromDate).days)) if request.lateNoa else 0
    cms_days = delay if delay > 5 else 0
    cms_h_penalty = rounded(h*cms_days/30)
    cms_o_penalty = rounded(cms_o*cms_days/30)
    cms_h = rounded((h-cms_h_penalty)*vbp)
    cms_outlier = rounded((cms_o-cms_o_penalty)*vbp)
    cms_total = cms_h+cms_outlier
    gross_h, gross_o = rounded(h*vbp), rounded(policy_o*vbp)
    net_h = rounded((h-rounded(h*delay/30))*vbp)
    net_o = rounded((policy_o-rounded(policy_o*delay/30))*vbp)
    raw_code = ('02' if not bypassed and limited else '11' if request.partialPeriod else '01') if candidate > 0 else '09' if request.partialPeriod else '00'
    effective = ('02' if limited else '11' if request.partialPeriod else '01') if candidate > 0 else '09' if request.partialPeriod else '00'
    standardized = sum(number(revenue_rows[revenue]['nonruralDollarRate']) * len([v for v in request.visits if v.discipline == discipline])
        for discipline,revenue in (('PT','0420'),('OT','0430'),('SLP','0440'),('SN','0550'),('MSW','0560'),('HHA','0570')))
    if len(request.visits) > 4:
        ratio = (Decimal(days if request.partialPeriod else 30)/30).quantize(Decimal('.00000001'), rounding=ROUND_DOWN)
        standardized = rounded(number(hrg['weight'])*Decimal('2038.22')*ratio) + rounded(cms_outlier/(Decimal('.749')*wage+Decimal('.251')))
    detail = dict(**context.model_dump(), fixedDollarLossRatio=Decimal('.37'), lossSharingRatio=Decimal('.8'), capRatio=Decimal('.1'),
        imputedCost=cost, periodPaymentBeforeVbp=h if not is_lupa else Decimal('0.00'),
        fixedDollarLossAmount=fdl if not is_lupa else Decimal('0.00'), thresholdAmount=threshold if not is_lupa else Decimal('0.00'),
        candidateOutlierBeforeVbp=candidate, availableOutlierPool=pool, capLimited=limited,
        capBypassedByCms=bypassed, cmsOutlierPayment=cms_outlier if not is_lupa else Decimal('0.00'),
        grossOutlierPayment=gross_o, netOutlierPayment=net_o,
        effectiveReturnCode=effective, adjustmentMethod='ZeroHistoryCapEnforcement' if bypassed and limited else 'CmsOutlierCalculation', appliesToPeriod=not is_lupa)
    return dict(detail=detail, components=components, rawCode=raw_code, cmsH=cms_h, cmsO=cms_outlier, cmsTotal=cms_total,
        cmsPenalty=cms_h_penalty+cms_o_penalty, cmsVbp=cms_total-(h+cms_o-cms_h_penalty-cms_o_penalty), standardized=standardized,
        total=net_h+net_o, gross=gross_h+gross_o, netH=net_h, netO=net_o, days=delay)


def process_period(request, provider, geography, hrg, cms, revenue_rows):
    calculated = expected(request, provider, geography, hrg, revenue_rows)
    payment = cms['paymentData']
    fields = dict(totalPayment=calculated['cmsTotal'], hhrgPayment=calculated['cmsH'], outlierPayment=calculated['cmsO'],
        lateSubmissionPenaltyAmount=calculated['cmsPenalty'], valueBasedPurchasingAdjustmentAmount=calculated['cmsVbp'], standardizedPayment=calculated['standardized'], hhrgWeight=number(hrg['weight']))
    if (cms.get('calculationVersion') != '2026.0' or cms.get('returnCodeData',{}).get('code') != calculated['rawCode']
        or type(payment.get('totalQuantityOfCoveredVisits')) is not int or payment['totalQuantityOfCoveredVisits'] != len(request.visits)
        or any(number(payment.get(key)) != value for key,value in fields.items())
        or len(payment.get('revenuePayments',[])) != 6):
        raise RuntimeError('Raw CMS outlier amounts, code, visits, or release do not reconcile.')
    for actual,expected_component in zip(payment['revenuePayments'],calculated['components']):
        if actual.get('revenueCode') != expected_component['revenueCode'] or any(number(actual.get(key)) != expected_component[key] for key in ('dollarRate','cost','addOnVisitAmount')):
            raise RuntimeError('Raw CMS outlier cost components do not reconcile.')
    if (payment.get('finalCbsa') is not None and payment['finalCbsa'] != geography['cbsaCode']) or (payment.get('finalWageIndex') is not None and number(payment['finalWageIndex']) != number(geography['wageIndex'])):
        raise RuntimeError('CMS outlier geography does not reconcile.')
    late = None
    if request.lateNoa:
        late = dict(exceptionStatus='NoException', admissionDate=request.admissionDate,noticeReceiptDate=request.noticeReceiptDate,
            periodFromDate=request.fromDate,periodThroughDate=request.throughDate,noncoveredDays=calculated['days'],grossPayment=calculated['gross'],
            penaltyAmount=calculated['gross']-calculated['total'],netPayment=calculated['total'],cmsPayment=calculated['cmsTotal'],cmsPenaltyAmount=calculated['cmsPenalty'],
            adjustmentMethod='SubsequentPeriodPenalty' if 0 < calculated['days'] <= 5 else 'CmsPeriodPenalty', providerLiability=True,beneficiaryLiabilityAmount=Decimal('0.00'),nonPayment=calculated['total']==0)
    return calculated['total'],calculated['detail'],late


def lupa_evidence(request, provider, geography, hrg, cms, revenue_rows):
    calculated = expected(request,provider,geography,hrg,revenue_rows,True)
    detail = calculated['detail']
    detail.update(effectiveReturnCode=cms['returnCodeData']['code'],adjustmentMethod='LupaNotApplicable')
    return detail

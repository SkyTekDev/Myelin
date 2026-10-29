"""Explicit NOA evidence and CMS-manual LUPA visit exclusions.

The unmodified CMS executable prices period penalties itself. Its LUPA branch
exits before that rule, so the documented pre-receipt visit exclusion is applied
here while retaining the complete original CMS calculation as evidence.
"""
from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal(".01")
PRIORITY = {"SN": 0, "PT": 1, "OT": 2, "SLP": 3}


def rounded(value):
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def adjusted(value, wage):
    return rounded(value * Decimal(".749") * wage) + rounded(value * Decimal(".251"))


def period_days(request):
    days = max(0, min(30, (request.noticeReceiptDate - request.fromDate).days))
    return days


def ordinal(value):
    return value.encode("utf-16-be", errors="surrogatepass")


def apply(request, provider, geography, hrg, cms, raw_total, lupa_detail, revenue_rows, factors):
    if request.lateNoa is None:
        return raw_total, lupa_detail, None
    wage = Decimal(geography["wageIndex"])
    vbp = Decimal(provider["providerData"]["vbpAdjustment"])
    payment = cms["paymentData"]
    visit_penalties = None
    if lupa_detail is None:
        rate = Decimal("2038.22" if provider["qualityReportingProgramIndicator"] == "0" else "1998.41")
        before = adjusted(rounded(Decimal(hrg["weight"]) * rate), wage)
        if request.partialPeriod:
            span = (max(v.serviceDate for v in request.visits) - min(v.serviceDate for v in request.visits)).days + 1
            before = rounded(before * rounded(Decimal(span) / 30))
        gross = rounded(before * vbp)
        days = period_days(request)
        method = "SubsequentPeriodPenalty" if 0 < days <= 5 else "CmsPeriodPenalty"
        net = rounded((before-rounded(before * days / 30)) * vbp) if method == "SubsequentPeriodPenalty" else raw_total
    else:
        gross = raw_total
        days = max(0, min(30, (request.noticeReceiptDate - request.fromDate).days))
        method = "LupaVisitExclusion"
        selected = None
        if request.lupa.isFirstOrOnlyPeriod and request.lupa.sourceAdmissionIndicator != "B":
            selected = min((v for v in request.visits if v.discipline in PRIORITY),
                           key=lambda v: (v.serviceDate, PRIORITY[v.discipline], ordinal(v.visitId)))
        allocations = {}
        visit_penalties = []
        net = Decimal("0.00")
        qrp = provider["qualityReportingProgramIndicator"]
        rural = geography["cbsaCode"].startswith("999")
        for component in payment["revenuePayments"]:
            revenue = component["revenueCode"]
            original = [v for v in request.visits if next(row["revenueCode"] for row in lupa_detail["visitPayments"] if row["visitId"] == v.visitId) == revenue]
            ordered = sorted(original, key=lambda v: (v.serviceDate, ordinal(v.visitId)))
            payable = [v for v in ordered if v.serviceDate >= request.noticeReceiptDate]
            excluded = [v for v in ordered if v.serviceDate < request.noticeReceiptDate]
            reference = revenue_rows[revenue]
            field = ("rural" if rural else "nonrural") + ("UnsubmittedDollarRate" if qrp == "2" else "DollarRate")
            rate = Decimal(reference[field])
            gross_cost = Decimal(component["cost"])
            net_cost = rounded(adjusted(len(payable) * rate, wage) * vbp)
            gross_addon = Decimal(component["addOnVisitAmount"])
            net_addon = gross_addon if selected is not None and selected in payable else Decimal("0.00")
            if net_cost > gross_cost:
                raise RuntimeError("NOA payable cost exceeds its original CMS component.")
            net += net_cost + net_addon
            for visits, cost, is_payable in ((payable, net_cost, True), (excluded, gross_cost - net_cost, False)):
                if not visits:
                    if cost != 0:
                        raise RuntimeError("NOA cost cannot be allocated to missing visits.")
                    continue
                cents, residual = divmod(int(cost / CENT), len(visits))
                for index, visit in enumerate(visits):
                    base = Decimal(cents + (index < residual)) * CENT
                    addon = gross_addon if selected is not None and selected.visitId == visit.visitId else Decimal("0.00")
                    gross_visit = base + addon
                    net_visit = gross_visit if is_payable else Decimal("0.00")
                    allocations[visit.visitId] = dict(visitId=visit.visitId, discipline=visit.discipline, revenueCode=revenue,
                        serviceDate=visit.serviceDate, basePayment=base if is_payable else Decimal("0.00"),
                        addOnPayment=addon if is_payable else Decimal("0.00"), totalPayment=net_visit)
                    visit_penalties.append(dict(visitId=visit.visitId, grossPayment=gross_visit,
                        penaltyAmount=gross_visit-net_visit, netPayment=net_visit,
                        grossBasePayment=base, grossAddOnPayment=addon,
                        basePenaltyAmount=Decimal("0.00") if is_payable else base,
                        addOnPenaltyAmount=Decimal("0.00") if is_payable else addon))
        ordered = sorted(request.visits, key=lambda v: (v.serviceDate, ordinal(v.visitId)))
        lupa_detail = dict(sourceAdmissionIndicator=request.lupa.sourceAdmissionIndicator,
            isFirstOrOnlyPeriod=request.lupa.isFirstOrOnlyPeriod,
            visitPayments=[allocations[v.visitId] for v in ordered])
        by_id = {v["visitId"]: v for v in visit_penalties}
        visit_penalties = [by_id[v.visitId] for v in ordered]
        if sum(v["grossPayment"] for v in visit_penalties) != gross or sum(v["netPayment"] for v in visit_penalties) != net:
            raise RuntimeError("NOA LUPA gross, exclusions and net allocations do not reconcile.")
    detail = dict(exceptionStatus=request.lateNoa.exceptionStatus,
        admissionDate=request.admissionDate, noticeReceiptDate=request.noticeReceiptDate,
        periodFromDate=request.fromDate, periodThroughDate=request.throughDate,
        noncoveredDays=days, grossPayment=gross, penaltyAmount=gross-net, netPayment=net,
        cmsPayment=Decimal(payment["totalPayment"]), cmsPenaltyAmount=Decimal(payment["lateSubmissionPenaltyAmount"]),
        adjustmentMethod=method, providerLiability=True, beneficiaryLiabilityAmount=Decimal("0.00"), nonPayment=net == 0)
    if visit_penalties is not None:
        detail["visitPenalties"] = visit_penalties
    if gross < net:
        raise RuntimeError("NOA adjustment cannot increase payment.")
    return net, lupa_detail, detail

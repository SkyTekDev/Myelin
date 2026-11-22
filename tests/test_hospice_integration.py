"""Live CMS Hospice Pricer reconciliation on synthetic claims with a synthetic provider file; no database writes.

Run explicitly: python -m tests.test_hospice_integration

Expected amounts are computed here from CMS's published national rates, not from the jar: FY 2026 from
CR 14190 (Transmittal 13331, July 31, 2025) Tables 1-2 and FY 2027 from CR 14557 (Transmittal 13976,
September 30, 2026) Tables 1-2, as labor portion x wage index + non-labor portion (Claims Processing
Manual chapter 11, section 30.2). Wage indexes are the jar's table values for the CBSA. Rounding follows
the pricer: routine home care high and low days, respite and general inpatient amounts are rounded half
up to cents; the continuous home care hourly rate (daily rate / 24) is truncated to four decimals and
multiplied by the hours (units / 4).
"""
from datetime import date
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from pathlib import Path
from threading import Lock

import jpype
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
D = Decimal

# (labor, non-labor) by fiscal year, rate set and level. "full" = hospices that submit the required quality data.
RATES = {
    2026: {"full": {"high": (D("152.35"), D("78.48")), "low": (D("120.08"), D("61.86")), "chc": (D("1259.07"), D("415.22")),
                    "irc": (D("324.81"), D("207.67")), "gip": (D("761.91"), D("437.95"))},
           "reduced": {"high": (D("146.41"), D("75.42")), "low": (D("115.39"), D("59.45")), "chc": (D("1209.98"), D("399.04")),
                       "irc": (D("312.15"), D("199.57")), "gip": (D("732.21"), D("420.87"))}},
    2027: {"full": {"high": (D("155.98"), D("80.35")), "low": (D("122.98"), D("63.35")), "chc": (D("1298.07"), D("428.09")),
                    "irc": (D("333.02"), D("212.91")), "gip": (D("782.01"), D("449.50"))},
           "reduced": {"high": (D("149.88"), D("77.21")), "low": (D("118.17"), D("60.87")), "chc": (D("1247.31"), D("411.35")),
                       "irc": (D("319.99"), D("204.59")), "gip": (D("751.43"), D("431.92"))}},
}
# Published totals (Tables 1-2): labor + non-labor must equal them.
TOTALS = {2026: {"full": ("230.83", "181.94", "1674.29", "532.48", "1199.86"), "reduced": ("221.83", "174.84", "1609.02", "511.72", "1153.08")},
          2027: {"full": ("236.33", "186.33", "1726.16", "545.93", "1231.51"), "reduced": ("227.09", "179.04", "1658.66", "524.58", "1183.35")}}
# The jar's wage index table values.
WI = {2026: {"16984": D("1.0228"), "99914": D("0.8743")}, 2027: {"16984": D("1.0104"), "99914": D("0.8692"), "50025": D("0.4076")}}


def cents(value):
    return value.quantize(D("0.01"), rounding=ROUND_HALF_UP)


def daily(year, level, wi, reduced=False):
    labor, other = RATES[year]["reduced" if reduced else "full"][level]
    return labor * wi + other


def rhc(year, wi, high, low, reduced=False):
    return cents(daily(year, "high", wi, reduced) * high) + cents(daily(year, "low", wi, reduced) * low)


def chc(year, wi, units, election_day, reduced=False):
    if units < 32:
        return cents(daily(year, "high" if election_day <= 60 else "low", wi, reduced))
    hourly = (daily(year, "chc", wi, reduced) / 24).quantize(D("0.0001"), rounding=ROUND_DOWN)
    return cents(hourly * (D(units) / 4).quantize(D("0.01"), rounding=ROUND_HALF_UP))


def inpatient(year, level, wi, days, reduced=False):
    return cents(daily(year, level, wi, reduced) * days)


def line(number, revenue, dos, units, hcpcs="Q5001"):
    return dict(claimLineId=f"L{number}", lineNumber=number, revenueCode=revenue, hcpcs=hcpcs, dateOfService=dos, units=units)


def claim(claim_id, lines, admission="2026-07-01", start="2026-07-01", end="2026-07-31", **changes):
    data = dict(claimId=claim_id, billType="811", providerNumber="141513", npi="1972322725", admissionDate=admission,
                fromDate=start, throughDate=end, patientCbsa="16984", providerCbsa="99914", claimLines=lines)
    data.update(changes)
    return data


def run():
    for year, sets in TOTALS.items():
        for rate_set, totals in sets.items():
            levels = ("high", "low", "chc", "irc", "gip")
            assert all(sum(RATES[year][rate_set][level]) == D(total) for level, total in zip(levels, totals)), (year, rate_set)

    if not jpype.isJVMStarted():
        jpype.startJVM(classpath=[str(ROOT / "jars" / "*")])
    from bridge.hospice_client import HospiceProcessor
    from bridge.hospice_models import HospiceClaimRequest
    from myelin.pricers.opsf import OPSF

    engine = create_engine("sqlite://")
    OPSF.__table__.create(engine)
    with Session(engine) as session:
        session.add_all([
            OPSF(provider_ccn="141513", effective_date=20251001, provider_type="35", hospital_quality_indicator=" ",
                 national_provider_identifier="1972322725"),
            OPSF(provider_ccn="141524", effective_date=20251001, provider_type="35", hospital_quality_indicator="1",
                 national_provider_identifier="1467410282"),
            OPSF(provider_ccn="140001", effective_date=20251001, provider_type="00", hospital_quality_indicator=" "),
            OPSF(provider_ccn="141600", effective_date=20260507, provider_type="35", hospital_quality_indicator=" "),
        ])
        session.commit()

    processor, lock = HospiceProcessor(ROOT / "jars", engine), Lock()
    assert processor.records_through == date(2026, 5, 7)

    def price(data):
        return processor.process(HospiceClaimRequest.model_validate(data), lock, "test")

    def payment(result, number):
        return next(item for item in result.lines if item.lineNumber == number).payment

    # Routine home care from admission: every day at the high rate.
    a = price(claim("A", [line(1, "0651", "2026-07-01", 31)]))
    assert a.status == "Success" and a.calculationVersion == "2026.0" and a.fiscalYear == 2026, a
    assert a.totalPayment == rhc(2026, WI[2026]["16984"], 31, 0) == D("7263.41"), a.totalPayment
    assert (a.highRoutineHomeCareDays, a.lowRoutineHomeCareDays, a.lines[0].pricerReturnCode) == (31, 0, "75")
    assert a.patientWageIndex == D("1.0228") and a.providerWageIndex == D("0.8743") and type(a.totalPayment) is Decimal
    assert (a.qualityReduction, a.qualitySource, a.provider.ccn) == (False, "ProviderFile", "141513")

    # A month of mixed care, admitted May 17 (July 1 is election day 46): each line priced on its own.
    b = price(claim("B", [line(1, "0651", "2026-07-01", 9), line(2, "0652", "2026-07-10", 40), line(3, "0656", "2026-07-11", 3, "Q5006"),
                          line(4, "0655", "2026-07-14", 5, "Q5006"), line(5, "0651", "2026-07-19", 13)], admission="2026-05-17"))
    expected = [rhc(2026, WI[2026]["16984"], 9, 0), chc(2026, WI[2026]["16984"], 40, 55),
                inpatient(2026, "gip", WI[2026]["99914"], 3), inpatient(2026, "irc", WI[2026]["99914"], 5),
                rhc(2026, WI[2026]["16984"], 0, 13)]
    assert [item.payment for item in b.lines] == expected, ([item.payment for item in b.lines], expected)
    assert expected[1] == D("709.58") and expected[2] == D("3312.26") and expected[3] == D("2458.26")
    assert [item.electionDay for item in b.lines] == [46, 55, 56, 59, 64]
    assert [(item.highRoutineHomeCareDays, item.lowRoutineHomeCareDays) for item in b.lines] == [(9, 0), (0, 0), (0, 0), (0, 0), (0, 13)]
    assert [item.pricerReturnCode for item in b.lines] == ["75", "00", "00", "00", "73"]
    assert b.totalPayment == sum(expected) and (b.highRoutineHomeCareDays, b.lowRoutineHomeCareDays) == (9, 13)

    # Day 60 falls inside the first routine line (days 58-62 split 3 high / 2 low); the second line is all low.
    c = price(claim("C", [line(1, "0651", "2026-07-13", 5), line(2, "0656", "2026-07-18", 2, "Q5006"), line(3, "0651", "2026-07-20", 12)],
                    admission="2026-05-17", start="2026-07-13"))
    assert (c.lines[0].highRoutineHomeCareDays, c.lines[0].lowRoutineHomeCareDays) == (3, 2)
    assert payment(c, 1) == rhc(2026, WI[2026]["16984"], 3, 2) and payment(c, 3) == rhc(2026, WI[2026]["16984"], 0, 12)

    # Continuous home care: 8 hours is the minimum; fewer is one routine day at the line's own high or low rate.
    d = price(claim("D", [line(1, "0652", "2026-07-15", 31), line(2, "0652", "2026-07-16", 31), line(3, "0652", "2026-07-17", 32),
                          line(4, "0652", "2026-07-18", 96)], admission="2026-05-17", start="2026-07-15"))
    assert [item.payment for item in d.lines] == [chc(2026, WI[2026]["16984"], 31, 60), chc(2026, WI[2026]["16984"], 31, 61),
                                                  chc(2026, WI[2026]["16984"], 32, 62), chc(2026, WI[2026]["16984"], 96, 63)]
    assert [item.paidAsRoutineHomeCareDay for item in d.lines] == [True, True, False, False]
    assert payment(d, 1) == D("234.30") and payment(d, 2) == D("184.68") and payment(d, 4) == D("1702.99")

    # The hospice's quality reporting reduction comes from its provider file record.
    e = price(claim("E", [line(1, "0651", "2026-07-01", 31)], providerNumber="141524", npi="1467410282"))
    assert (e.qualityReduction, e.qualitySource) == (True, "ProviderFile")
    assert e.totalPayment == rhc(2026, WI[2026]["16984"], 31, 0, reduced=True) == D("6980.21")

    # Prior benefit days count toward the 60 high-rate days.
    f = price(claim("F", [line(1, "0651", "2026-07-01", 31)], priorBenefitDays=50))
    assert (f.highRoutineHomeCareDays, f.lowRoutineHomeCareDays) == (10, 21) and f.totalPayment == rhc(2026, WI[2026]["16984"], 10, 21)

    # FY 2027: the provider file predates the fiscal year, so a flagged hospice is priced at full rates.
    october = dict(admission="2026-10-01", start="2026-10-01", end="2026-10-31")
    g = price(claim("G", [line(1, "0651", "2026-10-01", 31)], providerNumber="141524", npi="1467410282", **october))
    assert g.status == "Success" and g.calculationVersion == "2027.0" and g.fiscalYear == 2027
    assert (g.qualityReduction, g.qualitySource) == (False, "FullRatesProviderFileBeforeFiscalYear")
    assert g.totalPayment == rhc(2027, WI[2027]["16984"], 31, 0) and g.patientWageIndex == D("1.0104")
    processor.records_through = date(2026, 10, 1)  # as if an October 2026 provider file were loaded
    g2 = price(claim("G2", [line(1, "0651", "2026-10-01", 31)], providerNumber="141524", npi="1467410282", **october))
    assert g2.qualitySource == "ProviderFile" and g2.totalPayment == rhc(2027, WI[2027]["16984"], 31, 0, reduced=True)
    processor.records_through = date(2026, 5, 7)

    # FY 2027 inpatient care, continuous care and a 50xxx transition code.
    h = price(claim("H", [line(1, "0651", "2026-10-01", 4), line(2, "0652", "2026-10-05", 48), line(3, "0655", "2026-10-06", 5, "Q5006"),
                          line(4, "0656", "2026-10-11", 3, "Q5006")], **october))
    assert [item.payment for item in h.lines] == [rhc(2027, WI[2027]["16984"], 4, 0), chc(2027, WI[2027]["16984"], 48, 5),
                                                  inpatient(2027, "irc", WI[2027]["99914"], 5), inpatient(2027, "gip", WI[2027]["99914"], 3)]
    assert h.providerWageIndex == D("0.8692")
    i = price(claim("I", [line(1, "0651", "2026-10-01", 3)], patientCbsa="50025", providerCbsa=None, **october))
    assert i.totalPayment == rhc(2027, WI[2027]["50025"], 3, 0) and i.patientWageIndex == D("0.4076") and i.providerWageIndex is None

    # Refusals.
    j = price(claim("J", [line(1, "0651", "2026-07-01", 5)], patientCbsa="12345"))
    assert j.status == "UnableToPrice" and j.reasonCode == "HOSPICE_CBSA_INVALID" and not j.lines, j
    for start in ("2027-10-01", "2025-09-01"):
        k = price(claim("K", [line(1, "0651", start, 5)], admission=start, start=start, end=start[:8] + "30"))
        assert k.status == "UnableToPrice" and k.reasonCode == "HOSPICE_DATES_NOT_SUPPORTED", k
    m = price(claim("M", [line(1, "0651", "2026-07-01", 5)], providerNumber="140001", npi=None))
    assert m.status == "UnableToPrice" and m.reasonCode == "HOSPICE_PROVIDER_NOT_HOSPICE" and m.fiscalYear == 2026, m
    n = price(claim("N", [line(1, "0651", "2026-07-01", 5)], providerNumber="149999", npi=None))
    assert n.status == "Success" and n.qualitySource == "FullRatesProviderNotFound" and n.provider is None
    print("PASS hospice live CMS Hospice Pricer reconciliation (FY 2026 and FY 2027) against CMS's published rates.")


if __name__ == "__main__":
    run()

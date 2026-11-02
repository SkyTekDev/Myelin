"""Reproduce the reviewed CY2026 A9272 rate/policy package from retained sources."""
import argparse
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import html
import json
from pathlib import Path
import re

SOURCES = {
    'rate': ('r13488cp.pdf', 'https://www.cms.gov/files/document/r13488cp.pdf',
        '56752ec4506a89bdc9c762808e58b4f7cf0bd3013228d1beea561a33b8d384dd', 'Table 8; CY2026 dNPWT device rate; PDF page 11'),
    'billing': ('r12382cp.pdf', 'https://www.cms.gov/files/document/r12382cp.pdf',
        'ff58282722d2758fe85c337f1c4a8cf07639f74298afc9d576c3e4c1e1713f1e', 'CR13244; requirements 04.1,04.2,04.5,04.5.1'),
    'coinsurance': ('cfr-part409-2024.pdf', 'https://www.govinfo.gov/content/pkg/CFR-2024-title42-vol2/pdf/CFR-2024-title42-vol2-part409.pdf',
        'f8eb0851732ac1f7e94800fd1133a743199f32885665c76e7b664a492b391ace', '42 CFR 409.50(b); device coinsurance'),
    'deductible': ('part-b-2026.html', 'https://www.cms.gov/newsroom/fact-sheets/2026-medicare-parts-b-premiums-deductibles',
        '2c68ad73181183784a2e5fd0d909b014fd9abc339abeb84dc83886683464e878', 'Medicare Part B Premium and Deductible; annual deductible 2026'),
}


def prepare(sources, output):
    from pypdf import PdfReader
    manifest = {}
    texts = {}
    for key, (filename, url, digest, locator) in SOURCES.items():
        path = sources / filename
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError('Unreviewed or changed source: ' + filename)
        manifest[key] = dict(sourceUrl=url, sourceSha256=digest, rowLocator=locator)
        text = '\n'.join(page.extract_text() for page in PdfReader(path).pages) if filename.endswith('.pdf') else html.unescape(re.sub('<[^>]+>', ' ', raw.decode('utf-8')))
        texts[key] = ' '.join(text.split())
    if ('CY 2026 final payment amount for an applicable dNPWT device will be $282.10' not in texts['rate']
            or 'A9272' not in texts['billing'] or 'deductible and coinsurance' not in texts['billing']
            or not re.search(r'20 percent of the payment amount for the disposable', texts['coinsurance'])
            or 'annual deductible for all Medicare Part B beneficiaries will be $283 in 2026' not in texts['deductible']
            or (Decimal('276.57') * Decimal('1.02')).quantize(Decimal('.01'), rounding=ROUND_HALF_UP) != Decimal('282.10')):
        raise ValueError('The reviewed rate and beneficiary-policy source facts do not reconcile.')
    document = dict(schemaVersion='HHASeparatePaymentRates/1', paymentYear=2026,
        effectiveFrom='2026-01-01', effectiveThrough='2026-12-31', sources=manifest,
        entries={'A9272': dict(hcpcsCode='A9272', unitRate='282.10', coinsuranceRate='0.20', annualPartBDeductible='283.00')})
    raw = json.dumps(document, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()
    with output.open('xb') as stream:
        stream.write(raw)
    return dict(status='ReviewedSourcesReconciled', paymentYear=2026, rateRows=1,
        catalogSha256=hashlib.sha256(raw).hexdigest(), unitRate='282.10')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.sources, args.output), indent=2))

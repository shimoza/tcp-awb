"""The knowledge base, the price lookup and the API usage of the reader service as JSON (step 10 of the F1 plan).

The answers the old portal pages gave (awb/tcp/portal.py, removed on 2026-10-17): the same limits, the same name
check on every fact, the same price records and the same day counts. Codes, counts and the checked text of a fact;
never a name. Each function returns (status, data) and reads only the knowledge base, the price API and the usage
record.
"""
import datetime
import re

from awb import config

MAX_QUERY = 200
MAX_HITS = 30
MAX_PRICE_ROWS = 60
PRICE_TERMS = ('PAYG', 'R12', 'R36')
REGIONS = ('eu-de', 'eu-nl', 'eu-ch2')
SERVICE = re.compile(r'^[a-z][a-z0-9-]{1,30}$')
NO_QUERY = 'Give the words to search for in q.'
NAME_CHECK_DOWN = 'The name check cannot run, so no fact is shown.'
NO_SERVICE = 'A service name reads like ecs or rds.'
NO_PRICES = 'The price API gave no usable answer for this service right now.'
NO_USAGE = 'The usage record is unavailable.'
_UTC = datetime.timezone.utc


def _now():
    return datetime.datetime.now(_UTC)


def kb_answer(paths: config.Paths, query: dict):
    from awb import kb

    text = query.get('q', '')[:MAX_QUERY].strip()
    if not text:
        return 400, {'error': NO_QUERY}
    hits = [e for _, e in kb.find(text, where=paths) if not e.is_retired][:MAX_HITS]
    # every entry passes the name check of today's register first: a name registered after its add is withheld,
    # and without the check nothing is shown
    try:
        hits, withheld = kb.screen(hits, paths.register)
    except kb.KBError:
        return 503, {'error': NAME_CHECK_DOWN}
    today = datetime.date.today().isoformat()
    return 200, {'source': 'AWB knowledge base', 'fetched_at': _now().isoformat(), 'query': text, 'withheld': withheld,
                 'facts': [{'id': e.id, 'statement': e.statement, 'grade': e.grade, 'class': e.cls,
                            'checked': e.checked, 'expires': e.expires or None,
                            'expired': bool(e.expires and e.expires < today)} for e in hits]}


def price_answer(paths: config.Paths, query: dict):
    from awb.tcp import price

    service = query.get('service', '').strip().lower()[:40]
    flavor = query.get('flavor', '').strip()[:60]
    grep = query.get('grep', '').strip()[:60]
    region = query.get('region', 'eu-de')
    region = region if region in REGIONS else 'eu-de'
    if not SERVICE.match(service):
        return 400, {'error': NO_SERVICE}
    try:
        got = price.fetch(service, region)
        if got.listing.state != 'list':
            return 503, {'error': NO_PRICES}
        records = price.select(got.records, flavor=flavor or None, grep=grep or None)
        source = got.source_line()
    except Exception:
        return 503, {'error': NO_PRICES}
    rows = [{'flavor': r.flavor, 'name': r.name, 'os': r.os, 'unit': r.unit, 'currency': r.currency,
             'prices': {t: None if r.prices.get(t) is None else str(r.prices[t]) for t in PRICE_TERMS}}
            for r in records[:MAX_PRICE_ROWS]]
    return 200, {'source': source, 'service': service, 'region': region, 'terms': list(PRICE_TERMS),
                 'total': len(records), 'truncated': len(records) > len(rows), 'records': rows}


def usage_answer(paths: config.Paths, query: dict):
    from awb.tcp import throttle

    month = datetime.date.today().strftime('%Y-%m')
    try:
        days = throttle.usage_days(paths.shared, month)
    except (OSError, ValueError, TypeError):
        return 503, {'error': NO_USAGE}
    rows = [{'day': day, 'calls': d['calls'], 'throttled': d['429'], 'errors': d['errors'],
             'waited_seconds': round(d['waited'], 1), 'services': [{'service': k, 'calls': v} for k, v in sorted(d['services'].items())]}
            for day, d in sorted(days.items())]
    return 200, {'source': 'calls to the TCP API', 'month': month, 'days': rows,
                 'total': {'calls': sum(r['calls'] for r in rows), 'throttled': sum(r['throttled'] for r in rows),
                           'errors': sum(r['errors'] for r in rows)}}


ROUTES = {'/api/kb': kb_answer, '/api/price': price_answer, '/api/usage': usage_answer}

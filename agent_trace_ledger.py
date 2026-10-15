"""Find retry loops, orphan spans and hidden token costs in offline agent traces."""

from collections import defaultdict
from decimal import Decimal
import json
import math


def analyze(data):
    spans = data['spans']
    by_id, repeated, findings = {}, defaultdict(list), []
    total, unknown = Decimal(0), []
    for span in spans:
        sid = span['id']
        if sid in by_id:
            raise ValueError('duplicate span id: ' + sid)
        by_id[sid] = span
        start, end = float(span['start_ms']), float(span['end_ms'])
        if not math.isfinite(start) or not math.isfinite(end) or end < start:
            raise ValueError('invalid time interval for ' + sid)
        if span['kind'] == 'tool':
            signature = json.dumps([span['name'],span.get('arguments',{})],sort_keys=True,separators=(',',':'),allow_nan=False)
            repeated[signature].append(sid)
        if span['kind'] == 'llm':
            model = span['model']
            counts = [span.get('input_tokens',0),span.get('output_tokens',0)]
            if any(type(n) is not int or n < 0 for n in counts):
                raise ValueError('token counts must be nonnegative integers')
            rate = data.get('rates',{}).get(model)
            if rate is None:
                unknown.append(sid)
            else:
                for count, key in zip(counts,['input','output']):
                    price = Decimal(str(rate[key]))
                    if not price.is_finite() or price < 0:
                        raise ValueError('invalid token price')
                    total += Decimal(count) * price / Decimal(1000000)
    seen_cycles = set()
    for sid, span in by_id.items():
        parent = span.get('parent')
        if parent is not None and parent not in by_id:
            findings.append({'kind':'orphan','span':sid,'parent':parent})
        chain, cursor = [], sid
        while cursor in by_id:
            if cursor in chain:
                cycle = tuple(sorted(chain[chain.index(cursor):]))
                if cycle not in seen_cycles:
                    seen_cycles.add(cycle)
                    findings.append({'kind':'parent_cycle','spans':list(cycle)})
                break
            chain.append(cursor)
            cursor = by_id[cursor].get('parent')
    repeats = []
    for signature, ids in sorted(repeated.items()):
        if len(ids)>1:
            overlaps=[]
            for i,a in enumerate(ids):
                for b in ids[i+1:]:
                    if max(by_id[a]['start_ms'],by_id[b]['start_ms']) < min(by_id[a]['end_ms'],by_id[b]['end_ms']):
                        overlaps.append([a,b])
            name,args=json.loads(signature)
            repeats.append({'tool':name,'arguments':args,'spans':ids,'overlaps':overlaps})
    return {'span_count':len(spans),'estimated_cost':str(total),'unpriced_spans':unknown,'graph_findings':findings,'repeated_calls':repeats}


def main():
    """Read one UTF-8 JSON document and emit one machine-readable report."""
    import argparse
    import json
    import sys
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="JSON input path, or - for stdin")
    args = parser.parse_args()
    try:
        raw = sys.stdin.read() if args.input == "-" else Path(args.input).read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("input must be a JSON object")
        result = analyze(data)
        print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as error:
        print(f"Invalid input: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

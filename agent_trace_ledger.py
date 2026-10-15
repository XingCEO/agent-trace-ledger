"""Audit an offline OTLP/JSON trace export, with explicit cost coverage."""

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
import heapq
import json
import re

VERSION = "0.2.0"
MAX_INPUT_BYTES = 32 * 1024 * 1024
MODEL_OPERATIONS = {"chat", "generate_content", "text_completion", "embeddings"}
ATTRIBUTES = {
    "gen_ai.operation.name", "gen_ai.request.model", "gen_ai.response.model",
    "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens",
    "gen_ai.tool.name", "gen_ai.tool.call.arguments", "service.name",
}
MISSING = object()


def _object(value, where):
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be an object")
    return value


def _array(value, where):
    if not isinstance(value, list):
        raise ValueError(f"{where} must be an array")
    return value


def _text(value, where):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where} must be a nonempty string")
    return value


def _integer(value, where, *, encoded=False):
    if encoded and isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        value = int(value)
    if type(value) is not int or not 0 <= value <= 2**64 - 1:
        raise ValueError(f"{where} must be an unsigned 64-bit integer")
    return value


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _any_value(value, where, depth=0):
    """Decode the AnyValue forms used by OTLP attributes, not a span SDK."""
    _object(value, where)
    if depth > 20:
        raise ValueError(f"{where} exceeds the attribute nesting limit (20)")
    fields = {"stringValue", "boolValue", "intValue", "doubleValue",
              "arrayValue", "kvlistValue", "bytesValue"}.intersection(value)
    if not value:
        return None
    if len(fields) != 1:
        raise ValueError(f"{where} must contain exactly one AnyValue variant")
    field = next(iter(fields))
    raw = value[field]
    if field == "bytesValue":
        raise ValueError(f"{where}: bytesValue is unsupported for analyzed attributes")
    if field == "stringValue":
        if not isinstance(raw, str):
            raise ValueError(f"{where}.{field} must be a string")
        return raw
    if field == "boolValue":
        if type(raw) is not bool:
            raise ValueError(f"{where}.boolValue must be boolean")
        return raw
    if field == "intValue":
        if isinstance(raw, str) and re.fullmatch(r"-?[0-9]+", raw):
            raw = int(raw)
        if type(raw) is not int or not -(2**63) <= raw < 2**63:
            raise ValueError(f"{where}.intValue must be a signed 64-bit integer")
        return raw
    if field == "doubleValue":
        if type(raw) not in (int, float):
            raise ValueError(f"{where}.doubleValue must be a finite number")
        try:
            decoded = float(raw)
        except OverflowError as error:
            raise ValueError(f"{where}.doubleValue is outside the finite double range") from error
        _json(decoded)
        return decoded
    container = _object(raw, f"{where}.{field}")
    items = _array(container.get("values", []), f"{where}.{field}.values")
    if field == "arrayValue":
        return [_any_value(item, where, depth + 1) for item in items]
    result = {}
    for item in items:
        _object(item, where)
        key = _text(item.get("key"), f"{where}.key")
        if key in result:
            raise ValueError(f"duplicate attribute key: {key}")
        result[key] = _any_value(item.get("value"), where, depth + 1)
    return result


def _attributes(raw, where):
    result, seen = {}, set()
    for item in _array(raw, where):
        _object(item, where)
        key = _text(item.get("key"), f"{where}.key")
        if key in seen:
            raise ValueError(f"duplicate attribute key: {key}")
        seen.add(key)
        # Ignore unknown attributes, including their value representations.
        if key in ATTRIBUTES:
            result[key] = _any_value(item.get("value"), f"{where}.{key}")
    return result


def _otlp_id(value, size, where, *, optional=False):
    if optional and value in (None, ""):
        return None
    if not isinstance(value, str) or not re.fullmatch(f"[0-9a-fA-F]{{{size}}}", value):
        raise ValueError(f"{where} must be {size} hexadecimal characters")
    if int(value, 16) == 0:
        raise ValueError(f"{where} cannot be all zeroes; omit an absent parentSpanId")
    return value.lower()


def _local_id(value, where):
    value = _text(value, where)
    if "/" in value:
        raise ValueError(f"{where} cannot contain /")
    return value


def _milliseconds(value, where):
    if type(value) not in (int, float, str):
        raise ValueError(f"{where} must be numeric milliseconds")
    try:
        ms = Decimal(str(value))
        if not ms.is_finite() or ms < 0:
            raise ValueError(f"{where} is outside the supported timestamp range")
        digits = ms.as_tuple()
        if digits.exponent < -6 and any(digits.digits[digits.exponent + 6:]):
            raise ValueError(f"{where} has sub-nanosecond precision")
        with localcontext() as context:
            context.prec = 80
            if ms > Decimal(2**64 - 1) / 1000000:
                raise ValueError(f"{where} is outside the supported timestamp range")
            ns = ms * 1000000
            if ns != ns.to_integral_value():
                raise ValueError(f"{where} has sub-nanosecond precision")
            return int(ns)
    except InvalidOperation as error:
        raise ValueError(f"{where} must be numeric milliseconds") from error


@dataclass(frozen=True)
class Span:
    trace_id: str
    span_id: str
    parent: str | None
    start: int
    end: int
    kind: str
    name: str
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    arguments: object = MISSING
    service: str | None = None
    scope: str | None = None

    @property
    def ref(self):
        return f"{self.trace_id}/{self.span_id}"


def _validated_span(**fields):
    for field in ("model", "service", "scope"):
        if fields.get(field) is not None:
            _text(fields[field], field)
    for field in ("input_tokens", "output_tokens"):
        if fields.get(field) is not None:
            _integer(fields[field], field)
    if fields["end"] < fields["start"]:
        raise ValueError(f"end precedes start for {fields['span_id']}")
    if fields.get("arguments", MISSING) is not MISSING:
        _json(fields["arguments"])
    return Span(**fields)


def _normalized(data):
    for raw in _array(data.get("spans"), "spans"):
        _object(raw, "span")
        kind = raw.get("kind")
        if not isinstance(kind, str) or kind not in {"tool", "llm", "other"}:
            raise ValueError("normalized kind must be tool, llm or other")
        yield _validated_span(
            trace_id=_local_id(raw.get("trace_id", "local"), "trace_id"),
            span_id=_local_id(raw.get("id"), "id"),
            parent=None if raw.get("parent") is None else _local_id(raw["parent"], "parent"),
            start=_milliseconds(raw.get("start_ms"), "start_ms"),
            end=_milliseconds(raw.get("end_ms"), "end_ms"), kind=kind,
            name=_text(raw.get("name", raw.get("model", kind)), "name"),
            model=raw.get("model"), input_tokens=raw.get("input_tokens"),
            output_tokens=raw.get("output_tokens"), arguments=raw.get("arguments", MISSING),
        )


def _otlp(data):
    for resource in _array(data.get("resourceSpans"), "resourceSpans"):
        _object(resource, "resourceSpans entry")
        if "instrumentationLibrarySpans" in resource:
            raise ValueError("legacy instrumentationLibrarySpans is unsupported; export scopeSpans")
        attrs = _attributes(_object(resource.get("resource", {}), "resource").get("attributes", []), "resource.attributes")
        for scope in _array(resource.get("scopeSpans", []), "scopeSpans"):
            _object(scope, "scopeSpans entry")
            scope_name = _object(scope.get("scope", {}), "scope").get("name")
            if scope_name == "":
                scope_name = None
            for raw in _array(scope.get("spans", []), "scopeSpans.spans"):
                _object(raw, "OTLP span")
                span_attrs = _attributes(raw.get("attributes", []), "span.attributes")
                operation = span_attrs.get("gen_ai.operation.name")
                if operation is not None:
                    _text(operation, "gen_ai.operation.name")
                kind = "tool" if operation == "execute_tool" else "llm" if operation in MODEL_OPERATIONS else "other"
                name = span_attrs.get("gen_ai.tool.name") if kind == "tool" else raw.get("name")
                yield _validated_span(
                    trace_id=_otlp_id(raw.get("traceId"), 32, "traceId"),
                    span_id=_otlp_id(raw.get("spanId"), 16, "spanId"),
                    parent=_otlp_id(raw.get("parentSpanId"), 16, "parentSpanId", optional=True),
                    start=_integer(raw.get("startTimeUnixNano"), "startTimeUnixNano", encoded=True),
                    end=_integer(raw.get("endTimeUnixNano"), "endTimeUnixNano", encoded=True),
                    kind=kind, name=_text(name, "gen_ai.tool.name" if kind == "tool" else "span.name"),
                    model=span_attrs.get("gen_ai.response.model", span_attrs.get("gen_ai.request.model")),
                    input_tokens=span_attrs.get("gen_ai.usage.input_tokens"),
                    output_tokens=span_attrs.get("gen_ai.usage.output_tokens"),
                    arguments=span_attrs.get("gen_ai.tool.call.arguments", MISSING),
                    service=attrs.get("service.name"), scope=scope_name,
                )


def _rates(raw):
    result = {}
    for model, prices in _object(raw, "rates").items():
        _text(model, "rate model")
        _object(prices, f"rates.{model}")
        result[model] = {}
        for direction in ("input", "output"):
            if direction not in prices:
                continue
            value = prices[direction]
            if type(value) not in (str, int, float):
                raise ValueError(f"rates.{model}.{direction} must be a nonnegative decimal")
            try:
                price = Decimal(str(value))
            except InvalidOperation as error:
                raise ValueError(f"rates.{model}.{direction} must be a nonnegative decimal") from error
            if not price.is_finite() or price < 0 or len(price.as_tuple().digits) > 30 or abs(price.as_tuple().exponent) > 30:
                raise ValueError(f"rates.{model}.{direction} must be a finite nonnegative decimal (at most 30 digits/exponent)")
            result[model][direction] = price
    return result


def _cost(spans, rates, currency, limit):
    unknown, incomplete, full, total, model_count = [], 0, 0, Decimal(0), 0
    # Input integers are uint64 and prices have at most 30 digits/exponent.
    with localcontext() as context:
        context.prec = 160
        for span in spans:
            if span.kind != "llm":
                continue
            model_count += 1
            reasons = []
            if span.model is None:
                reasons.append("missing_model")
            prices = rates.get(span.model, {})
            for direction in ("input", "output"):
                count = getattr(span, f"{direction}_tokens")
                if count is None:
                    reasons.append(f"missing_{direction}_tokens")
                if direction not in prices:
                    reasons.append(f"missing_{direction}_rate")
                if count is not None and direction in prices:
                    total += Decimal(count) * prices[direction] / 1000000
            if reasons:
                incomplete += 1
                if len(unknown) < limit:
                    unknown.append({"span": span.ref, "reasons": reasons})
            else:
                full += 1
    return {"currency": currency, "known_subtotal": format(total, "f"),
            "complete": incomplete == 0, "model_span_count": model_count,
            "fully_priced_span_count": full, "incomplete_span_count": incomplete,
            "incomplete_spans": unknown, "truncated": incomplete > len(unknown)}


def _graph(spans, limit):
    by_id = {span.ref: span for span in spans}
    findings, count = [], 0

    def add(finding):
        nonlocal count
        count += 1
        if len(findings) < limit:
            findings.append(finding)

    for span in spans:
        if span.parent is not None and f"{span.trace_id}/{span.parent}" not in by_id:
            add({"kind": "missing_parent", "span": span.ref, "parent": f"{span.trace_id}/{span.parent}"})
    done = set()
    for span in spans:
        cursor, path, position = span.ref, [], {}
        while cursor in by_id and cursor not in done:
            if cursor in position:
                cycle = sorted(path[position[cursor]:])
                add({"kind": "parent_cycle", "span_count": len(cycle), "spans": cycle[:limit], "truncated": len(cycle) > limit})
                break
            position[cursor] = len(path)
            path.append(cursor)
            node = by_id[cursor]
            cursor = f"{node.trace_id}/{node.parent}" if node.parent is not None else None
        done.update(path)
    return {"count": count, "findings": findings, "truncated": count > len(findings)}


def _overlaps(spans, limit):
    heap, active, examples, count = [], {}, [], 0
    for span in sorted(spans, key=lambda item: (item.start, item.end, item.ref)):
        while heap and heap[0][0] <= span.start:
            _, ref = heapq.heappop(heap)
            del active[ref]
        if span.start == span.end:
            continue
        count += len(active)
        if len(examples) < limit:
            for ref in active:
                examples.append([ref, span.ref])
                if len(examples) == limit:
                    break
        active[span.ref] = span
        heapq.heappush(heap, (span.end, span.ref))
    return count, examples


def _repeats(spans, limit):
    groups = defaultdict(list)
    for span in spans:
        if span.kind == "tool" and span.arguments is not MISSING:
            groups[(span.trace_id, span.name, _json(span.arguments))].append(span)
    results, count = [], 0
    for (trace, name, arguments), group in sorted(groups.items()):
        if len(group) < 2:
            continue
        count += 1
        if len(results) == limit:
            continue
        overlaps, examples = _overlaps(group, limit)
        refs = sorted(item.ref for item in group)
        results.append({"trace_id": trace, "tool": name, "arguments": json.loads(arguments),
                        "call_count": len(group), "spans": refs[:limit], "spans_truncated": len(refs) > limit,
                        "overlap_pair_count": overlaps, "overlap_examples": examples,
                        "overlap_examples_truncated": overlaps > len(examples)})
    return {"group_count": count, "groups": results, "truncated": count > len(results)}


def _timelines(spans):
    traces = defaultdict(list)
    for span in spans:
        traces[span.trace_id].append(span)
    result = []
    for trace, group in sorted(traces.items()):
        origin = min(span.start for span in group)
        result.append({"trace_id": trace, "origin_ns": str(origin),
                       "duration_ns": str(max(span.end for span in group) - origin),
                       "spans": [{"span": span.ref, "kind": span.kind, "name": span.name,
                                  "service": span.service, "scope": span.scope,
                                  "start_offset_ns": str(span.start - origin),
                                  "duration_ns": str(span.end - span.start)}
                                 for span in sorted(group, key=lambda item: (item.start, item.ref))]})
    return result


def analyze(data, *, rates=None, currency=None, input_format="auto", max_spans=10000, max_findings=100):
    """Return a deterministic v0.2 report. Missing usage is never assumed zero."""
    _object(data, "input")
    for value, name in ((max_spans, "max_spans"), (max_findings, "max_findings")):
        if type(value) is not int or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    if input_format == "auto":
        if ("resourceSpans" in data) == ("spans" in data):
            raise ValueError("input must contain exactly one of resourceSpans or spans")
        input_format = "otlp" if "resourceSpans" in data else "normalized"
    if not isinstance(input_format, str) or input_format not in {"otlp", "normalized"}:
        raise ValueError("input_format must be auto, otlp or normalized")
    spans, seen = [], set()
    for span in _otlp(data) if input_format == "otlp" else _normalized(data):
        if len(spans) >= max_spans:
            raise ValueError(f"span limit exceeded ({max_spans})")
        if span.ref in seen:
            raise ValueError(f"duplicate span id in trace: {span.ref}")
        seen.add(span.ref)
        spans.append(span)
    spans.sort(key=lambda item: item.ref)
    prices = _rates(data.get("rates", {}) if rates is None else rates)
    chosen_currency = _text(data.get("currency", "unspecified") if currency is None else currency, "currency")
    return {"schema_version": "0.2", "input_format": input_format, "span_count": len(spans),
            "trace_count": len({span.trace_id for span in spans}),
            "cost": _cost(spans, prices, chosen_currency, max_findings),
            "graph": _graph(spans, max_findings), "repeated_calls": _repeats(spans, max_findings),
            "coverage": {"unclassified_span_count": sum(span.kind == "other" for span in spans),
                         "tool_spans_without_arguments": sum(span.kind == "tool" and span.arguments is MISSING for span in spans)},
            "limits": {"max_spans": max_spans, "max_findings_per_category": max_findings},
            "timelines": _timelines(spans)}


def _load_json(path):
    import sys
    from pathlib import Path
    stream = sys.stdin.buffer if path == "-" else Path(path).open("rb")
    try:
        raw = stream.read(MAX_INPUT_BYTES + 1)
    finally:
        if path != "-":
            stream.close()
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError(f"input exceeds {MAX_INPUT_BYTES} bytes")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def constant(value):
        raise ValueError(f"non-finite JSON number: {value}")

    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)


def main(argv=None):
    """Read a local file or stdin, write the audit report to stdout."""
    import argparse
    import sys
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="OTLP JSON or normalized JSON path, or - for stdin")
    parser.add_argument("--format", choices=("auto", "otlp", "normalized"), default="auto")
    parser.add_argument("--rates", help="JSON file with currency and models (per-million input/output rates)")
    parser.add_argument("--max-spans", type=int, default=10000)
    parser.add_argument("--max-findings", type=int, default=100)
    parser.add_argument("--fail-on", choices=("never", "graph", "incomplete-cost", "any"), default="never")
    parser.add_argument("--version", action="version", version=VERSION)
    args = parser.parse_args(argv)
    try:
        data = _load_json(args.input)
        rates, currency = None, None
        if args.rates is not None:
            if args.rates == "-":
                raise ValueError("--rates must name a file")
            pricing = _object(_load_json(args.rates), "rates file")
            rates = pricing.get("models")
            _object(rates, "rates file models")
            currency = _text(pricing.get("currency"), "rates file currency")
        result = analyze(data, rates=rates, currency=currency, input_format=args.format,
                         max_spans=args.max_spans, max_findings=args.max_findings)
        print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    except (OSError, ValueError, TypeError, OverflowError, RecursionError) as error:
        print(f"Invalid input: {error}", file=sys.stderr)
        return 2
    has_graph = result["graph"]["count"] > 0
    incomplete = not result["cost"]["complete"]
    repeated = result["repeated_calls"]["group_count"] > 0
    failed = ((args.fail_on == "graph" and has_graph)
              or (args.fail_on == "incomplete-cost" and incomplete)
              or (args.fail_on == "any" and (has_graph or incomplete or repeated)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Optional schema check using official protobuf classes; install .[interop]."""
import base64
import copy
import json
from pathlib import Path
import sys

from google.protobuf.json_format import ParseDict
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / 'examples/otlp.json'
    original = json.loads(path.read_text(encoding='utf-8'))
    converted = copy.deepcopy(original)
    expected = []
    for resource in converted['resourceSpans']:
        for scope in resource.get('scopeSpans', []):
            for span in scope.get('spans', []):
                expected.append((span['traceId'].lower(), span['spanId'].lower(),
                                 int(span['startTimeUnixNano']), int(span['endTimeUnixNano'])))
                # OTLP JSON deliberately differs from protobuf JSON for these bytes fields.
                for key in ('traceId', 'spanId', 'parentSpanId'):
                    if key in span:
                        span[key] = base64.b64encode(bytes.fromhex(span[key])).decode('ascii')
    message = ParseDict(converted, ExportTraceServiceRequest())
    actual = [(span.trace_id.hex(), span.span_id.hex(), span.start_time_unix_nano, span.end_time_unix_nano)
              for resource in message.resource_spans for scope in resource.scope_spans for span in scope.spans]
    if actual != expected:
        raise ValueError('fixture schema round-trip changed identities or nanosecond timestamps')
    print(f'OTLP fixture schema round-trip passed: {len(actual)} spans; IDs and uint64 timestamps preserved.')


if __name__ == '__main__':
    main()

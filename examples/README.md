# Fixture provenance

All values here are synthetic and were authored for this repository. No provider SDK,
customer trace, credential, prompt text or vendor price was used.

- `otlp.json`: one OTLP JSON `ExportTraceServiceRequest`, two resources/scopes,
  two traces, repeated IDs across traces, structured tool arguments, overlapping
  calls, a missing usage counter, an external/missing parent and epoch-scale
  nanosecond timestamps.
- `rates.json`: fictional `demo-small` per-million token prices in a chosen USD label.
- `otlp-output.json`: generated with `python -m agent_trace_ledger examples/otlp.json --rates examples/rates.json`.
- `input.json`: the original compact normalized format, with explicit currency.
- `output.json`: generated with `python -m agent_trace_ledger examples/input.json`.

`python scripts/check_otlp_schema.py` validates `otlp.json` using the official
`opentelemetry-proto` message classes. OTLP JSON specifies hexadecimal IDs, whereas
standard protobuf JSON decoders expect base64 for byte fields. The check converts
only `traceId`, `spanId`, and `parentSpanId` to the generic representation, parses
with `ExportTraceServiceRequest`, then checks exact identities and uint64 times.
This tests schema shape and those values; it does not exercise an HTTP collector,
a real instrumentation library, or a provider export.

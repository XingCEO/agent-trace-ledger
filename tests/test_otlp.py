import copy
import json
import unittest

from agent_trace_ledger import analyze


def fixture():
    with open('examples/otlp.json') as stream:
        return json.load(stream)


def first_span(data):
    return data['resourceSpans'][0]['scopeSpans'][0]['spans'][0]


class OtlpImportTests(unittest.TestCase):
    def test_multiresource_batch_reuses_ids_and_preserves_metadata(self):
        data = fixture()
        report = analyze(data, rates={'demo-small': {'input': '0.20', 'output': '0.80'}}, currency='USD')
        self.assertEqual(report['span_count'], 7)
        self.assertEqual(report['trace_count'], 2)
        self.assertEqual(report['input_format'], 'otlp')
        self.assertEqual(report['cost']['known_subtotal'], '0.00046')
        self.assertFalse(report['cost']['complete'])
        self.assertEqual(report['cost']['incomplete_span_count'], 1)
        self.assertEqual(report['cost']['incomplete_spans'][0]['reasons'], ['missing_output_tokens'])
        self.assertEqual(report['repeated_calls']['groups'][0]['overlap_pair_count'], 1)
        self.assertEqual(report['coverage']['tool_spans_without_arguments'], 1)
        self.assertEqual(report['coverage']['unclassified_span_count'], 1)
        self.assertEqual(report['graph']['count'], 1)
        self.assertEqual(report['graph']['findings'][0]['kind'], 'missing_parent')
        root, child = report['timelines'][0]['spans'][:2]
        self.assertEqual(root['service'], 'agent-api')
        self.assertEqual(child['service'], 'tool-worker')
        self.assertEqual(child['scope'], 'synthetic-tool-fixture')

    def test_absolute_nanoseconds_survive_without_float_rounding(self):
        report = analyze(fixture())
        self.assertEqual(report['timelines'][0]['origin_ns'], '1720000000000000001')
        self.assertEqual(report['timelines'][0]['spans'][1]['start_offset_ns'], '5')
        self.assertEqual(report['timelines'][1]['spans'][0]['duration_ns'], '1')

    def test_response_model_takes_precedence_then_request_fallback(self):
        data = fixture()
        span = first_span(data)
        span['attributes'] = [attr for attr in span['attributes'] if attr['key'] != 'gen_ai.response.model']
        report = analyze(data, rates={'demo-small': {'input': 0, 'output': 0}})
        self.assertIn('missing_input_rate', report['cost']['incomplete_spans'][0]['reasons'])
        report = analyze(data, rates={'requested-alias': {'input': 0, 'output': 0}, 'demo-small': {'input': 0, 'output': 0}})
        self.assertEqual(report['cost']['incomplete_span_count'], 1)

    def test_uppercase_hex_ids_are_canonical_and_duplicates_rejected(self):
        data = fixture()
        span = first_span(data)
        span['traceId'] = 'AB' * 16
        span['spanId'] = 'CD' * 8
        report = analyze(data)
        self.assertTrue(any(trace['trace_id'] == 'ab' * 16 for trace in report['timelines']))
        duplicate = copy.deepcopy(span)
        duplicate['traceId'] = duplicate['traceId'].lower()
        duplicate['spanId'] = duplicate['spanId'].lower()
        data['resourceSpans'][0]['scopeSpans'][0]['spans'].append(duplicate)
        with self.assertRaisesRegex(ValueError, 'duplicate span id in trace'):
            analyze(data)

    def test_invalid_otlp_identity_times_and_typed_usage_are_rejected(self):
        mutations = [('traceId', 'bad'), ('spanId', '0' * 16), ('parentSpanId', '0' * 16),
                     ('startTimeUnixNano', True), ('startTimeUnixNano', '1.5'), ('endTimeUnixNano', '1')]
        for key, value in mutations:
            data = fixture()
            first_span(data)[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                analyze(data)
        for encoded in ({'intValue': '-1'}, {'stringValue': '10'}, {'boolValue': True},
                        {'doubleValue': 1}, {'doubleValue': 1.0}):
            data = fixture()
            first_span(data)['attributes'].append({'key': 'gen_ai.usage.output_tokens', 'value': encoded})
            # Replace original so validation tests type rather than duplicate detection.
            first_span(data)['attributes'] = [attr for attr in first_span(data)['attributes'][:-1] if attr['key'] != 'gen_ai.usage.output_tokens'] + first_span(data)['attributes'][-1:]
            with self.subTest(encoded=encoded), self.assertRaises(ValueError):
                analyze(data)

    def test_unknown_attributes_are_ignored_and_duplicates_rejected(self):
        data = fixture()
        first_span(data)['attributes'].append({'key': 'future.unused', 'value': {'futureValue': 'x'}})
        self.assertEqual(analyze(data)['span_count'], 7)
        first_span(data)['attributes'].append({'key': 'future.unused', 'value': {'stringValue': 'x'}})
        with self.assertRaisesRegex(ValueError, 'duplicate attribute key'):
            analyze(data)

    def test_tool_argument_strings_remain_opaque(self):
        data = fixture()
        tool_spans = data['resourceSpans'][1]['scopeSpans'][0]['spans'][:2]
        for span, text in zip(tool_spans, ['{"x":1,"y":2}', '{"y":2,"x":1}']):
            span['attributes'][-1]['value'] = {'stringValue': text}
        self.assertEqual(analyze(data)['repeated_calls']['group_count'], 0)
        tool_spans[1]['attributes'][-1]['value'] = copy.deepcopy(tool_spans[0]['attributes'][-1]['value'])
        self.assertEqual(analyze(data)['repeated_calls']['group_count'], 1)

    def test_anyvalue_scalars_nested_objects_and_null_are_distinct(self):
        data = fixture()
        tool_spans = data['resourceSpans'][1]['scopeSpans'][0]['spans'][:2]
        value = {'kvlistValue': {'values': [
            {'key': 'enabled', 'value': {'boolValue': True}},
            {'key': 'score', 'value': {'doubleValue': 0.5}},
            {'key': 'count', 'value': {'intValue': '4'}},
            {'key': 'nullable', 'value': {}},
        ]}}
        for span in tool_spans:
            span['attributes'][-1]['value'] = copy.deepcopy(value)
        group = analyze(data)['repeated_calls']['groups'][0]
        self.assertEqual(group['arguments'], {'enabled': True, 'score': 0.5, 'count': 4, 'nullable': None})
        tool_spans[0]['attributes'][-1]['value'] = {'bytesValue': 'YQ=='}
        with self.assertRaisesRegex(ValueError, 'bytesValue is unsupported'):
            analyze(data)

    def test_deep_or_ambiguous_anyvalues_fail_cleanly(self):
        for value in ({'stringValue': 'x', 'intValue': '1'}, {'boolValue': 1}):
            data = fixture()
            first_span(data)['attributes'][0]['value'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                analyze(data)
        data = fixture()
        value = {'stringValue': 'x'}
        for _ in range(22):
            value = {'arrayValue': {'values': [value]}}
        data['resourceSpans'][1]['scopeSpans'][0]['spans'][0]['attributes'][-1]['value'] = value
        with self.assertRaisesRegex(ValueError, 'nesting limit'):
            analyze(data)

    def test_unknown_operations_stay_unclassified_instead_of_becoming_billable(self):
        data = fixture()
        first_span(data)['attributes'][0]['value'] = {'stringValue': 'invoke_agent'}
        report = analyze(data)
        self.assertEqual(report['coverage']['unclassified_span_count'], 2)
        self.assertEqual(report['cost']['model_span_count'], 2)

    def test_legacy_batches_are_rejected_instead_of_silently_dropping_spans(self):
        data = fixture()
        resource = data['resourceSpans'][0]
        resource['instrumentationLibrarySpans'] = resource.pop('scopeSpans')
        with self.assertRaisesRegex(ValueError, 'legacy instrumentationLibrarySpans'):
            analyze(data)

    def test_otlp_example_matches_committed_report(self):
        with open('examples/otlp-output.json') as stream:
            expected = json.load(stream)
        with open('examples/rates.json') as stream:
            pricing = json.load(stream)
        self.assertEqual(analyze(fixture(), rates=pricing['models'], currency=pricing['currency']), expected)

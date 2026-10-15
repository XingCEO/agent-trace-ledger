import json
from decimal import Decimal, localcontext
import random
import unittest

from agent_trace_ledger import analyze


def tool(sid, *, start=0, end=10, **extra):
    return dict(id=sid, kind='tool', name='search', start_ms=start, end_ms=end, **extra)


def model(sid='model', **extra):
    return dict(id=sid, kind='llm', model='m', start_ms=0, end_ms=1, **extra)


class CostCoverageTests(unittest.TestCase):
    rates = {'m': {'input': '0.1', 'output': '0.2'}}

    def test_missing_usage_is_not_assumed_zero(self):
        cost = analyze({'spans': [model()], 'rates': self.rates})['cost']
        self.assertFalse(cost['complete'])
        self.assertEqual(cost['known_subtotal'], '0')
        self.assertEqual(cost['incomplete_spans'][0]['reasons'], ['missing_input_tokens', 'missing_output_tokens'])

    def test_explicit_zero_usage_is_complete(self):
        cost = analyze({'spans': [model(input_tokens=0, output_tokens=0)], 'rates': self.rates})['cost']
        self.assertTrue(cost['complete'])
        self.assertEqual(cost['fully_priced_span_count'], 1)

    def test_partial_usage_retains_known_subtotal(self):
        cost = analyze({'spans': [model(input_tokens=1000000)], 'rates': self.rates})['cost']
        self.assertEqual(Decimal(cost['known_subtotal']), Decimal('0.1'))
        self.assertFalse(cost['complete'])

    def test_missing_rates_and_model_are_explicit(self):
        span = model(input_tokens=10, output_tokens=20)
        del span['model']
        reasons = analyze({'spans': [span]})['cost']['incomplete_spans'][0]['reasons']
        self.assertEqual(reasons, ['missing_model', 'missing_input_rate', 'missing_output_rate'])

    def test_partial_price_only_prices_recorded_components(self):
        cost = analyze({'spans': [model(input_tokens=1000000, output_tokens=1)], 'rates': {'m': {'input': '0.1'}}})['cost']
        self.assertEqual(Decimal(cost['known_subtotal']), Decimal('0.1'))
        self.assertEqual(cost['incomplete_spans'][0]['reasons'], ['missing_output_rate'])

    def test_invalid_counts_and_prices_are_rejected(self):
        for invalid in (-1, True, 1.5, '10', 2**64):
            with self.subTest(count=invalid), self.assertRaises(ValueError):
                analyze({'spans': [model(input_tokens=invalid)]})
        for invalid in ('NaN', '-1', 'Infinity', True, '1e31', 'x'):
            with self.subTest(price=invalid), self.assertRaises(ValueError):
                analyze({'spans': [], 'rates': {'m': {'input': invalid}}})

    def test_decimal_math_does_not_depend_on_caller_context(self):
        rates = {'m': {'input': '999999999999999999999999999999e30', 'output': '1e-30'}}
        with localcontext() as context:
            context.prec = 160
            expected = Decimal(2**64 - 1) * Decimal(rates['m']['input']) / 1000000 + Decimal('1e-36')
        with localcontext() as context:
            context.prec = 6
            cost = analyze({'spans': [model(input_tokens=2**64 - 1, output_tokens=1)], 'rates': rates})['cost']
        self.assertEqual(Decimal(cost['known_subtotal']), expected)


class GraphAndTimingTests(unittest.TestCase):
    def test_ids_are_scoped_to_trace(self):
        spans = [tool('same', trace_id='a'), tool('same', trace_id='b')]
        self.assertEqual(analyze({'spans': spans})['span_count'], 2)
        with self.assertRaisesRegex(ValueError, 'duplicate span id in trace'):
            analyze({'spans': [spans[0], spans[0]]})

    def test_parent_in_another_trace_does_not_resolve(self):
        report = analyze({'spans': [tool('child', trace_id='a', parent='p'), tool('p', trace_id='b')]})
        self.assertEqual(report['graph']['findings'], [{'kind': 'missing_parent', 'span': 'a/child', 'parent': 'a/p'}])

    def test_long_parent_chain_and_cycle_are_not_recursive(self):
        spans = [tool(str(i), parent=str(i + 1)) for i in range(2000)]
        spans[-1]['parent'] = '1998'
        graph = analyze({'spans': spans})['graph']
        self.assertEqual(graph['count'], 1)
        self.assertEqual(graph['findings'][0]['spans'], ['local/1998', 'local/1999'])

    def test_cycle_and_graph_diagnostics_are_bounded(self):
        spans = [tool(str(i), parent=str((i + 1) % 5)) for i in range(5)]
        spans.extend(tool('orphan' + str(i), parent='absent') for i in range(5))
        graph = analyze({'spans': spans}, max_findings=2)['graph']
        self.assertEqual(graph['count'], 6)
        self.assertEqual(len(graph['findings']), 2)
        self.assertTrue(graph['truncated'])
        cycle = analyze({'spans': spans[:5]}, max_findings=2)['graph']['findings'][0]
        self.assertEqual(cycle['span_count'], 5)
        self.assertEqual(len(cycle['spans']), 2)
        self.assertTrue(cycle['truncated'])

    def test_normalized_strings_preserve_nanoseconds(self):
        report = analyze({'spans': [tool('a', start='1720000000000.000001', end='1720000000000.000002')]})
        self.assertEqual(report['timelines'][0]['origin_ns'], '1720000000000000001')
        self.assertEqual(report['timelines'][0]['spans'][0]['duration_ns'], '1')

    def test_upper_timestamp_boundary_is_independent_of_decimal_context(self):
        with localcontext() as context:
            context.prec = 6
            report = analyze({'spans': [tool('a', start='18446744073709.551614', end='18446744073709.551615')]})
        self.assertEqual(report['timelines'][0]['spans'][0]['duration_ns'], '1')
        with self.assertRaises(ValueError):
            analyze({'spans': [tool('a', end='18446744073709.551616')]})

    def test_invalid_intervals_and_subnanoseconds(self):
        for start, end in ((2, 1), ('NaN', 1), (-1, 1), (0, '0.0000001'),
                           (0, '1.' + '0' * 100 + '1')):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                analyze({'spans': [tool('a', start=start, end=end)]})


class RepeatTests(unittest.TestCase):
    def test_missing_arguments_do_not_match_empty_object(self):
        spans = [tool('a'), tool('b'), tool('c', arguments={}), tool('d', arguments={})]
        report = analyze({'spans': spans})
        self.assertEqual(report['coverage']['tool_spans_without_arguments'], 2)
        self.assertEqual(report['repeated_calls']['groups'][0]['spans'], ['local/c', 'local/d'])

    def test_object_key_order_is_ignored_but_trace_is_not(self):
        spans = [tool('a', arguments={'x': 1, 'y': 2}), tool('b', arguments={'y': 2, 'x': 1}),
                 tool('c', trace_id='other', arguments={'x': 1, 'y': 2})]
        report = analyze({'spans': spans})['repeated_calls']
        self.assertEqual(report['group_count'], 1)
        self.assertEqual(report['groups'][0]['call_count'], 2)

    def test_touching_and_zero_length_intervals_do_not_overlap(self):
        spans = [tool('a', start=0, end=10, arguments={}), tool('b', start=10, end=20, arguments={}),
                 tool('c', start=5, end=5, arguments={})]
        self.assertEqual(analyze({'spans': spans})['repeated_calls']['groups'][0]['overlap_pair_count'], 0)

    def test_sweep_count_matches_bruteforce_and_samples_are_bounded(self):
        rng = random.Random(76)
        spans = [tool(str(i), start=rng.randrange(20), end=30, arguments={}) for i in range(200)]
        for span in spans:
            span['end_ms'] = rng.randrange(span['start_ms'], 31)
        expected = sum(max(a['start_ms'], b['start_ms']) < min(a['end_ms'], b['end_ms'])
                       for index, a in enumerate(spans) for b in spans[index + 1:])
        group = analyze({'spans': spans}, max_findings=3)['repeated_calls']['groups'][0]
        self.assertEqual(group['overlap_pair_count'], expected)
        self.assertEqual(len(group['overlap_examples']), 3)
        self.assertEqual(len(group['spans']), 3)
        self.assertTrue(group['overlap_examples_truncated'])

    def test_all_diagnostic_categories_keep_exact_counts_when_truncated(self):
        spans = [model(str(i)) for i in range(8)]
        spans += [tool(f't{i}-{j}', arguments={'n': i}) for i in range(8) for j in range(2)]
        report = analyze({'spans': spans}, max_findings=2)
        self.assertEqual(report['cost']['incomplete_span_count'], 8)
        self.assertEqual(len(report['cost']['incomplete_spans']), 2)
        self.assertTrue(report['cost']['truncated'])
        self.assertEqual(report['repeated_calls']['group_count'], 8)
        self.assertEqual(len(report['repeated_calls']['groups']), 2)
        self.assertTrue(report['repeated_calls']['truncated'])


class InputTests(unittest.TestCase):
    def test_limits_and_top_level_contract(self):
        with self.assertRaisesRegex(ValueError, 'span limit exceeded'):
            analyze({'spans': [tool('a'), tool('b')]}, max_spans=1)
        for data in ([], {}, {'spans': [], 'resourceSpans': []}, {'spans': {}}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                analyze(data)
        for limit in (0, -1, True):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                analyze({'spans': []}, max_findings=limit)

    def test_report_is_deterministic_under_span_permutation(self):
        spans = [tool('b', arguments={'x': 1}), tool('a', arguments={'x': 1}), model()]
        self.assertEqual(analyze({'spans': spans}), analyze({'spans': spans[::-1]}))

    def test_legacy_example_matches_committed_report(self):
        with open('examples/input.json') as stream:
            source = json.load(stream)
        with open('examples/output.json') as stream:
            expected = json.load(stream)
        self.assertEqual(analyze(source), expected)

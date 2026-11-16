"""Bounded processing preserves JSON content without joining long strings."""
import gc
import json
from pathlib import Path
import random
import subprocess
import sys
import tracemalloc
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import proxy


def encoded(value):
    text = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    try:
        return text.encode('utf-8')
    except UnicodeEncodeError:
        return json.dumps(value, separators=(',', ':')).encode('utf-8')


class ChunkedJsonTests(unittest.TestCase):
    def compare(self, body):
        previous = json.loads(body.decode('utf-8'))
        candidate = proxy._ChunkedJSONDecoder(body).decode()
        self.assertEqual(proxy._encode_responses_payload(candidate, source_size=2**20), encoded(previous))

    def test_small_and_dense_bodies_keep_the_stdlib_decoder(self):
        for body in (b'{"input":"small"}', json.dumps({'input': ['short'] * 160000}).encode()):
            with mock.patch.object(proxy, '_ChunkedJSONDecoder', side_effect=AssertionError('chunked path')):
                self.assertEqual(proxy._decode_responses_payload(body, chunked=True), json.loads(body))

    def test_default_decoder_remains_materialized(self):
        body = b'{"input":"' + b'A' * (2**20) + b'"}'
        result = proxy._decode_responses_payload(body)
        self.assertIsInstance(result['input'], str)

    def test_processing_keeps_large_strings_in_bounded_pieces(self):
        body = b'{"input":"' + b'A' * (2**20) + b'"}'
        parsed = proxy._decode_responses_payload(body, chunked=True)
        self.assertIsInstance(parsed['input'], proxy._ChunkedJSONText)
        self.assertLessEqual(max(map(len, parsed['input'].chunks)), proxy._JSON_DECODE_CHUNK_BYTES)
        self.compare(body)

    def test_mixed_differential_values_and_string_escape_boundaries(self):
        rng = random.Random(1611193)
        scalars = [None, True, False, 0, -0.0, 2**200, -2**63, 1.23e-100,
                   float('inf'), float('-inf'), float('nan'), '日本語', '"\\\n\x00',
                   '\ud800', '\udc00', '\ud800\udc00', '🌍', '\\uD800\\uDC00', '\x7f', '\x85']
        def value(depth):
            if not depth or rng.randrange(3) == 0:
                return rng.choice(scalars)
            if rng.randrange(2):
                return [value(depth - 1) for _ in range(rng.randrange(6))]
            return {str(i) + rng.choice(('', '🌍')): value(depth - 1) for i in range(rng.randrange(6))}
        with mock.patch.multiple(proxy, _JSON_DECODE_GROUP_BYTES=96, _JSON_DECODE_CHUNK_BYTES=17):
            for index in range(500):
                payload = {'input': value(4), 'index': index, 'tail': 'Привет 🌍' * rng.randrange(10)}
                try:
                    body = json.dumps(payload, ensure_ascii=bool(rng.randrange(2))).encode()
                except UnicodeEncodeError:
                    body = json.dumps(payload).encode()
                with self.subTest(index=index):
                    self.compare(body)
            for text in ('🌍' * 100 + '"\\\n\x00', '\ud800\udc00' * 100,
                         '\\uD800\\uDC00' * 100, 'é日🌍' * 100):
                for offset in range(40):
                    self.compare(json.dumps({'input': 'A' * offset + text}).encode())
                    self.compare(json.dumps({'input': 'A' * offset + text}, ensure_ascii=False).encode()
                                 if '\ud800' not in text else json.dumps({'input': text}).encode())

    def test_duplicates_and_large_keys_preserve_order_and_last_value(self):
        with mock.patch.multiple(proxy, _JSON_DECODE_GROUP_BYTES=96, _JSON_DECODE_CHUNK_BYTES=17):
            self.compare(b'{"a":1,"input":"' + b'A' * 200 + b'","\\u0061":2}')
            self.compare(json.dumps({'日本語' * 100: 'value', 'input': ['🌍' * 500, 'tail']}, ensure_ascii=False).encode())
            self.compare(json.dumps({'input': '\x7f🌍' * 500, 'surrogate': '\ud800'}).encode())

    def test_large_schema_keeps_arbitrary_integer_precision(self):
        body = json.dumps({'input': '🌍' * 100000, 'enum': [2**7000 + i for i in range(500)]}, ensure_ascii=False).encode()
        self.compare(body)

    def test_deep_accepted_input_falls_back_to_the_c_decoder(self):
        body = b'{"input":' + b'[' * 600 + b'"' + b'A' * (2**20) + b'"' + b']' * 600 + b'}'
        expected = json.loads(body)
        result = proxy._decode_responses_payload(body, chunked=True)
        self.assertIsNotNone(result)
        for _ in range(600):
            expected, result = expected['input'] if isinstance(expected, dict) else expected[0], result['input'] if isinstance(result, dict) else result[0]
        self.assertEqual(expected[0], result[0])

    def test_malformed_input_is_rejected_before_sanitization(self):
        large = b'A' * (2**20)
        for suffix in (b'\x01"}', b'\\x22"}', b'\\uZZZZ"}', b'\xff"}', b'"} trailing', b'",}', b'", "x":[1,2,]}'):
            body = b'{"input":"' + large + suffix
            with self.subTest(suffix=suffix):
                self.assertIsNone(proxy._decode_responses_payload(body, chunked=True))
        with mock.patch.multiple(proxy, _JSON_DECODE_GROUP_BYTES=96, _JSON_DECODE_CHUNK_BYTES=17):
            for body in (b'{"a":[}', b'{"a":01}', b'{"a": true false}', b'{"a":[1\v,2]}'):
                with self.assertRaises((ValueError, IndexError)):
                    proxy._ChunkedJSONDecoder(body).decode()

    def test_invalid_utf8_continuations_finish_in_a_separate_process(self):
        code = '''
import sys
sys.path.insert(0, sys.argv[1])
import proxy
for suffix in (b'', b'\\\\n'):
    body = b'{"input":"' + b'\\x80' * (2**20) + suffix + b'"}'
    assert proxy._decode_responses_payload(body, chunked=True) is None
'''
        result = subprocess.run([sys.executable, '-c', code, str(Path(proxy.__file__).resolve().parent)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))

    def test_sanitization_preserves_chunked_tool_text_and_pruning_order(self):
        payload = {'previous_response_id': 'state', 'input': [
            {'type': 'reasoning', 'encrypted_content': 'A' * 1000},
            {'type': 'function_call', 'id': 'state', 'call_id': 'pair', 'arguments': '🌍' * 500},
            {'type': 'function_call_output', 'call_id': 'pair', 'output': '日本語' * 500},
            {'type': 'message', 'content': [
                {'type': 'input_image', 'image_url': 'data:image/png;base64,' + 'A' * 500},
                {'type': 'input_image', 'image_url': 'data:image/png;base64,' + 'B' * 500},
            ]},
        ]}
        body = json.dumps(payload, ensure_ascii=False).encode()
        with mock.patch.multiple(proxy, _JSON_DECODE_GROUP_BYTES=96, _JSON_DECODE_CHUNK_BYTES=17), mock.patch.object(proxy, 'MAX_IMAGES_PER_REQUEST', 1):
            previous = proxy.sanitize_responses_payload(json.loads(body))
            parsed = proxy._ChunkedJSONDecoder(body).decode()
            result = proxy.sanitize_responses_payload(parsed)
            self.assertEqual(proxy._encode_responses_payload(result, source_size=2**20), encoded(previous))

    def test_ascii_decode_peak_does_not_retain_full_document_string(self):
        body = b'{"input":"' + b'A' * (4 * 1024 * 1024) + b'"}'
        gc.collect()
        tracemalloc.start(1)
        try:
            parsed = proxy._decode_responses_payload(body, chunked=True)
            _, peak = tracemalloc.get_traced_memory()
            self.assertIsInstance(parsed['input'], proxy._ChunkedJSONText)
            self.assertLess(peak, 6 * 1024 * 1024)
        finally:
            tracemalloc.stop()

    def test_unicode_decode_peak_avoids_full_ucs4_capacity_and_join(self):
        unit = 'История текста 🌍. '.encode()
        body = b'{"input":"' + unit * (4 * 1024 * 1024 // len(unit)) + b'"}'
        gc.collect()
        tracemalloc.start(1)
        try:
            parsed = proxy._decode_responses_payload(body, chunked=True)
            _, peak = tracemalloc.get_traced_memory()
            self.assertIsInstance(parsed['input'], proxy._ChunkedJSONText)
            self.assertLess(peak, 12 * 1024 * 1024)
        finally:
            tracemalloc.stop()


if __name__ == '__main__':
    unittest.main()

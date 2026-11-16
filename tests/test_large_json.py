"""Bounded modified-body encoding preserves stdlib JSON semantics and lifetimes."""
import gc
import hashlib
import json
from pathlib import Path
import random
import sys
import tracemalloc
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import proxy


def previous_encode(value):
    text = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    try:
        return text.encode('utf-8')
    except UnicodeEncodeError:
        return json.dumps(value, separators=(',', ':')).encode('utf-8')


class LargeJsonTests(unittest.TestCase):
    def test_large_strings_preserve_escapes_unicode_and_chunk_boundaries(self):
        text = ('Привет 🌍"\\\n\r\t\x00/日本語' * 18000)
        payload = {'input': [{'type': 'message', 'content': [{'type': 'input_text', 'text': text}]}],
                   'metadata': {'decimal': 0.12345678901234567, 'large_int': 2**200, 'negative_zero': -0.0}}
        self.assertEqual(proxy._encode_responses_payload(payload, source_size=2**20), previous_encode(payload))

    def test_lone_surrogate_preserves_the_existing_whole_body_ascii_fallback(self):
        payload = {'input': '🌍' * 100000 + '\ud800', 'metadata': {'русский': '日本語', 'pair': '\ud800\udc00'}}
        self.assertEqual(proxy._encode_responses_payload(payload, source_size=2**20), previous_encode(payload))

    def test_groups_preserve_key_order_empty_containers_and_numeric_values(self):
        payload = {'input': [{'index': i, 'value': 'abc' * 1200, 'empty': [{}, []],
                             'float': float('inf') if i % 2 else float('nan'), 'flags': [True, False, None]}
                            for i in range(450)], 'tail': 'end'}
        result = proxy._encode_responses_payload(payload, source_size=2**20)
        self.assertEqual(result, previous_encode(payload))
        self.assertTrue(result.endswith(b',"tail":"end"}'))

    def test_large_keys_and_non_ascii_group_sizes_preserve_output(self):
        payload = {'日本語' * 100000: 'value', 'input': ['🌍' * 65000, '\x00' * 100000]}
        self.assertEqual(proxy._encode_responses_payload(payload, source_size=2**20), previous_encode(payload))

    def test_small_request_keeps_original_encoder(self):
        with mock.patch.object(proxy, '_encode_large_responses_payload', side_effect=AssertionError('large path called')):
            self.assertEqual(proxy._encode_responses_payload({'input': 'small'}, source_size=100), b'{"input":"small"}')

    def test_differential_mixed_json_with_forced_small_groups(self):
        rng = random.Random(1611192)
        scalars = [None, True, False, 0, -0.0, 2**100, -2**63, 1.23e-100, '日本語', '"\\\n\x00', '\ud800', '🌍']
        def value(depth):
            if not depth or rng.randrange(3) == 0:
                return rng.choice(scalars)
            if rng.randrange(2):
                return [value(depth - 1) for _ in range(rng.randrange(6))]
            return {str(i) + rng.choice(('', '🌍')): value(depth - 1) for i in range(rng.randrange(6))}
        with mock.patch.object(proxy, '_JSON_GROUP_CHARACTERS', 96), mock.patch.object(proxy, '_JSON_STRING_CHARACTERS', 13):
            for index in range(300):
                payload = {'input': value(4), 'index': index}
                self.assertEqual(proxy._encode_responses_payload(payload, source_size=2**20), previous_encode(payload),
                                 f'JSON output differs in fixture {index}')

    def test_large_image_encoding_peak_avoids_full_sized_unicode_and_utf8_copies(self):
        payload = {'input': [{'content': [{'type': 'input_image', 'image_url': 'A' * (4 * 1024 * 1024)}]}]}
        expected_hash = hashlib.sha256(previous_encode(payload)).hexdigest()
        gc.collect()
        tracemalloc.start(1)
        try:
            result = proxy._encode_responses_payload(payload, source_size=2**22)
            _, peak = tracemalloc.get_traced_memory()
            self.assertEqual(hashlib.sha256(result).hexdigest(), expected_hash)
            # Includes the returned bytes and growth slack, excludes the fixture.
            self.assertLess(peak, 7 * 1024 * 1024)
        finally:
            tracemalloc.stop()

    def test_large_integer_groups_keep_precision_without_full_body_copies(self):
        payload = {'input': 'synthetic', 'schema': {'enum': [2**7000 + i for i in range(900)]}}
        expected = previous_encode(payload)
        expected_hash, source_size = hashlib.sha256(expected).hexdigest(), len(expected)
        del expected
        gc.collect()
        tracemalloc.start(1)
        try:
            result = proxy._encode_responses_payload(payload, source_size=source_size)
            _, peak = tracemalloc.get_traced_memory()
            self.assertEqual(hashlib.sha256(result).hexdigest(), expected_hash)
            self.assertLess(peak, source_size + 2 * 1024 * 1024)
        finally:
            tracemalloc.stop()


if __name__ == '__main__':
    unittest.main()

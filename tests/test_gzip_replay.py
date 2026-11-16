"""Owned-gzip reconstruction preserves bytes and checks the gzip trailer."""
import gzip
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import proxy


class GzipReplayTests(unittest.TestCase):
    def test_known_size_reconstruction_preserves_exact_bytes_in_both_backends(self):
        body = ('{ "input":"' + 'Привет 🌍 ' * 100000 + '", "n":0.12345678901234567890123 }\n').encode()
        compressed = gzip.compress(body, compresslevel=1, mtime=0)
        for backend in (None, proxy._native_gzip):
            with self.subTest(native=backend is not None), mock.patch.object(proxy, '_native_gzip', backend):
                self.assertEqual(proxy._decompress_proxy_gzip(compressed, len(body)), body)

    def test_corrupt_crc_and_truncated_trailers_are_rejected(self):
        body = b'synthetic exact bytes ' * 10000
        compressed = gzip.compress(body, compresslevel=1, mtime=0)
        corrupt = bytearray(compressed)
        corrupt[-8] ^= 1
        for backend in (None, proxy._native_gzip):
            for wire in (bytes(corrupt), compressed[:-8]):
                with self.subTest(native=backend is not None, truncated=wire == compressed[:-8]), mock.patch.object(proxy, '_native_gzip', backend):
                    with self.assertRaises(Exception):
                        proxy._decompress_proxy_gzip(wire, len(body))


if __name__ == '__main__':
    unittest.main()

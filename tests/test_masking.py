"""QA masks and the rules the manifest records for them."""
from __future__ import annotations

import unittest

import numpy as np

from data_loader.masking import (MASKS, QA_DESCRIPTIONS, describe_mask, dn_to_reflectance, landsat_qa_mask,
                                 sentinel2_scl_mask)


class Sentinel2SclTests(unittest.TestCase):
    def test_masks_shadow_cloud_and_cirrus_only(self):
        scl = np.arange(12, dtype="uint16")
        bad = sentinel2_scl_mask(scl)
        self.assertEqual(list(np.flatnonzero(bad)), [3, 8, 9, 10])

    def test_manifest_rule_matches_the_mask(self):
        rule = describe_mask("sentinel2_scl")
        self.assertEqual(rule["qaBand"], "scl")
        self.assertEqual(sorted(int(k) for k in rule["classes"]), [3, 8, 9, 10])
        self.assertEqual(rule["classes"]["9"], "cloud_high_probability")
        self.assertEqual(QA_DESCRIPTIONS["sentinel2_scl"]["classes"]["0"], "no_data")


class LandsatQaTests(unittest.TestCase):
    def test_bits_1_to_4_are_masked_and_rule_says_so(self):
        qa = np.array([21824, 1 << 1, 1 << 2, 1 << 3, 1 << 4, 1 << 5, 1], dtype="uint16")
        self.assertEqual(list(landsat_qa_mask(qa)), [False, True, True, True, True, False, False])
        self.assertEqual(sorted(describe_mask("landsat_qa_pixel")["bits"]), ["1", "2", "3", "4"])

    def test_every_mask_has_a_description(self):
        self.assertEqual(set(MASKS), set(QA_DESCRIPTIONS))


class ReflectanceTests(unittest.TestCase):
    def test_fill_is_nan_not_offset(self):
        r = dn_to_reflectance(np.array([0, 7273, 10000], dtype="uint16"), 2.75e-5, -0.2)
        self.assertTrue(np.isnan(r[0]))
        self.assertAlmostEqual(float(r[1]), 7273 * 2.75e-5 - 0.2, places=6)
        r = dn_to_reflectance(np.array([0, 1000, 2000], dtype="uint16"), 1e-4, -0.1)
        self.assertTrue(np.isnan(r[0]))
        self.assertAlmostEqual(float(r[2]), 0.1, places=6)


if __name__ == "__main__":
    unittest.main()

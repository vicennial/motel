"""The tag and label codecs shared by the DigitalOcean, Linode and Vultr providers."""

import unittest

from motel.errors import ProviderError
from motel.providers._common import decode_label, decode_tags, encode_label, encode_tags, matches

REAL = {"managed-by": "motel", "role": "floor", "host": "400100001", "floor": "f-ab12", "suite": "0", "created": "20261109t1200z"}
NASTY = ["", "a", "A", "a_b", "_", "__", "_41", "a-b", "-", "é", "日本", "x" * 30, "~=%", "m_x_y", " ", "\n", "\x00", "UPPER_lower-09"]


class TagCodecTest(unittest.TestCase):
    def test_real_labels_round_trip_and_stay_short(self):
        tags = encode_tags(REAL, 50)
        self.assertEqual(decode_tags(tags), REAL)
        self.assertTrue(all(len(t) <= 50 for t in tags))
        self.assertTrue(all(set(t) <= set("abcdefghijklmnopqrstuvwxyz0123456789-_") for t in tags))

    def test_every_value_round_trips_and_has_one_spelling(self):
        for value in NASTY:
            tag = encode_tags({"k": value}, 500)[0]
            self.assertEqual(decode_tags([tag]), {"k": value}, value)
            self.assertTrue(set(tag) <= set("abcdefghijklmnopqrstuvwxyz0123456789-_"), tag)

    def test_distinct_labels_never_share_a_tag(self):
        seen = {}
        for key in ("a", "a-b", "ab"):
            for value in NASTY:
                tag = encode_tags({key: value}, 500)[0]
                self.assertNotIn(tag, seen, (key, value, seen.get(tag)))
                seen[tag] = (key, value)

    def test_foreign_and_non_canonical_tags_are_ignored(self):
        self.assertEqual(decode_tags(["prod", "m_", "m_k", "M_k_v", "m_k_V", "m_k_%41", "m_K_v", "m__v", "m_k_v_", "m_k_v_4", "m_k_v_zz",
                                      "m_k_v_2d", "m_k_é", None, 5, "m_k-_v"]), {})
        self.assertEqual(decode_tags(None), {})
        self.assertEqual(decode_tags(["x", "m_k_v"]), {"k": "v"})

    def test_a_key_with_two_values_is_dropped(self):
        self.assertEqual(decode_tags(["m_k_a", "m_k_b", "m_j_c"]), {"j": "c"})
        self.assertEqual(decode_tags(["m_k_a", "m_k_a"]), {"k": "a"})

    def test_too_long_or_badly_named_labels_are_refused_not_truncated(self):
        with self.assertRaises(ProviderError):
            encode_tags({"host": "x" * 60}, 50)
        for key in ("", "Host", "a_b", "a b", "-a", "a-", "a--b", 5):
            with self.assertRaises(ProviderError):
                encode_tags({key: "v"}, 50)
        self.assertEqual(len(encode_tags({"host": "x" * 43}, 50)[0]), 50)

    def test_matches_needs_every_pair_and_compares_as_text(self):
        self.assertTrue(matches(REAL, {"managed-by": "motel", "host": 400100001}))
        self.assertFalse(matches(REAL, {"managed-by": "motel", "host": "1"}))
        self.assertFalse(matches({}, {"managed-by": "motel"}))
        self.assertTrue(matches(REAL, {}))


class LabelCodecTest(unittest.TestCase):
    NAME = "motel-f-ab12-cd34"

    def test_real_labels_round_trip(self):
        text = encode_label(self.NAME, REAL, 255)
        self.assertEqual(decode_label(text), (self.NAME, REAL))
        self.assertLessEqual(len(text), 255)

    def test_every_value_round_trips(self):
        for value in NASTY:
            text = encode_label(self.NAME, {"k": value}, 500)
            self.assertEqual(decode_label(text), (self.NAME, {"k": value}), value)
            self.assertEqual(text.count("~"), 1)
            self.assertEqual(text.count("="), 1)

    def test_distinct_labels_never_share_a_spelling(self):
        seen = {}
        for value in NASTY:
            text = encode_label(self.NAME, {"k": value}, 500)
            self.assertNotIn(text, seen)
            seen[text] = value

    def test_a_plain_label_is_a_name_with_no_labels(self):
        self.assertEqual(decode_label("Remote-Block-Storage"), ("Remote-Block-Storage", {}))
        self.assertEqual(decode_label(""), ("", {}))
        self.assertEqual(decode_label(None), ("", {}))

    def test_non_canonical_labels_yield_no_labels(self):
        for text in ("n~k", "n~k=v~", "n~~k=v", "n~K=v", "n~k=V%41", "n~k=%41", "n~k=v%", "n~k=v%4", "n~k=v%zz", "n~k=a~k=b", "n~k=a~k=a",
                     "n m~k=v", "~k=v", "n~k=v w", "n~k=é", "n~=v", "n~a_b=v"):
            self.assertEqual(decode_label(text)[1], {}, text)

    def test_refusals(self):
        for name in ("", "a b", "a~b", "é", "x" * 65):
            with self.assertRaises(ProviderError):
                encode_label(name, {}, 255)
        with self.assertRaises(ProviderError):
            encode_label(self.NAME, {"Bad": "v"}, 255)
        with self.assertRaises(ProviderError):
            encode_label(self.NAME, {"k": "v" * 300}, 255)


if __name__ == "__main__":
    unittest.main()

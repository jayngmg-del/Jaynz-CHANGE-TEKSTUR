import unittest
from app.main import astc_mip_size, mip_dimensions, texture_format_info, clean_name

class CoreTests(unittest.TestCase):
    def test_astc_payload_sizes_match_sample(self):
        dims = mip_dimensions(512, 512, 10)
        self.assertEqual(sum(astc_mip_size(w, h, 8, 8) for w, h in dims), 87424)
        self.assertEqual(astc_mip_size(512, 512, 8, 8), 65536)
        self.assertEqual(astc_mip_size(256, 256, 10, 10), 10816)

    def test_astc_format_parser(self):
        self.assertEqual(texture_format_info("ASTC_RGBA_8x8"), (8, 8, "RGBA"))
        self.assertEqual(texture_format_info("ASTC_RGB_10x10"), (10, 10, "RGB"))
        self.assertIsNone(texture_format_info("ASTC_HDR_8x8"))
        self.assertIsNone(texture_format_info("ETC2_RGBA8"))

    def test_filename_cleanup(self):
        self.assertEqual(clean_name(" GAROU / V2 "), "GAROU _ V2")
        self.assertEqual(clean_name("   "), "Texture")

if __name__ == "__main__":
    unittest.main()

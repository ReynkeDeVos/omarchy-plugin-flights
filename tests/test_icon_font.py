import re
import subprocess
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
QML_PATH = PLUGIN_ROOT / "FlightTracker.qml"


class IconFontTests(unittest.TestCase):
    def test_state_icons_use_a_font_that_contains_every_glyph(self):
        qml = QML_PATH.read_text(encoding="utf-8")
        family_match = re.search(
            r'readonly property string iconFontFamily: "([^"]+)"', qml
        )
        self.assertIsNotNone(
            family_match,
            "State icons need an explicit icon font instead of the configurable text font",
        )

        family = family_match.group(1)
        glyphs = sorted(set(re.findall(r"\\u(f[0-9a-fA-F]{3})", qml)))
        self.assertTrue(glyphs, "No state icon glyphs found")

        for codepoint in glyphs:
            result = subprocess.run(
                ["fc-list", f"{family}:charset={codepoint}", "file"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertTrue(
                result.stdout.strip(),
                f"{family} does not contain U+{codepoint.upper()}",
            )

    def test_setup_icons_come_from_the_nerd_font_like_omarchy_panels(self):
        setup = (PLUGIN_ROOT / "SetupView.qml").read_text(encoding="utf-8")
        self.assertNotIn("iconFontFamily", setup)
        for codepoint in sorted(set(re.findall(r"\\u(f[0-9a-fA-F]{3})", setup))):
            families = subprocess.run(
                ["fc-list", f":charset={codepoint}", "family"], check=True, capture_output=True, text=True
            ).stdout
            self.assertIn("Nerd Font", families, f"No Nerd Font contains U+{codepoint.upper()}")

    def test_glyphs_are_escaped_so_editors_cannot_drop_them(self):
        for path in PLUGIN_ROOT.glob("*.qml"):
            self.assertIsNone(re.search("[\ue000-\uf8ff]", path.read_text(encoding="utf-8")), path.name)


if __name__ == "__main__":
    unittest.main()

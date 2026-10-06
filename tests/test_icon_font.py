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


if __name__ == "__main__":
    unittest.main()

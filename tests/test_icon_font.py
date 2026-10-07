import re
import subprocess
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
QML_PATH = PLUGIN_ROOT / "FlightTracker.qml"
GLYPH = re.compile(r"\\u\{?(f[0-9a-fA-F]{3,4})\}?")
STATE_ICONS = re.compile(r"function iconGlyph\(\) \{.*?\n  \}", re.S)


def nerd_font_has(codepoint: str) -> bool:
    families = subprocess.run(
        ["fc-list", f":charset={codepoint}", "family"], check=True, capture_output=True, text=True
    ).stdout
    return "Nerd Font" in families


class IconFontTests(unittest.TestCase):
    def test_panel_icons_use_a_font_that_contains_every_glyph(self):
        qml = QML_PATH.read_text(encoding="utf-8")
        family_match = re.search(
            r'readonly property string iconFontFamily: "([^"]+)"', qml
        )
        self.assertIsNotNone(
            family_match,
            "Panel icons need an explicit icon font instead of the configurable text font",
        )

        family = family_match.group(1)
        glyphs = sorted(set(GLYPH.findall(STATE_ICONS.sub("", qml))))
        self.assertTrue(glyphs, "No panel icon glyphs found")

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

    # Omarchy centres each bar icon's line box and keeps one shared baseline, so
    # an icon in a font of its own sits off the line of the built-in ones.
    def test_state_icons_come_from_the_bar_font_like_omarchy_status_icons(self):
        qml = QML_PATH.read_text(encoding="utf-8")
        button = re.search(r"\n  BarIconButton \{.*?\n  \}", qml, re.S).group(0)
        self.assertNotIn("fontFamily", button)
        glyphs = sorted(set(GLYPH.findall(STATE_ICONS.search(qml).group(0))))
        self.assertTrue(glyphs, "No state icon glyphs found")
        for codepoint in glyphs:
            self.assertTrue(nerd_font_has(codepoint), f"No Nerd Font contains U+{codepoint.upper()}")

    def test_setup_icons_come_from_the_nerd_font_like_omarchy_panels(self):
        setup = (PLUGIN_ROOT / "SetupView.qml").read_text(encoding="utf-8")
        self.assertNotIn("iconFontFamily", setup)
        for codepoint in sorted(set(GLYPH.findall(setup))):
            self.assertTrue(nerd_font_has(codepoint), f"No Nerd Font contains U+{codepoint.upper()}")

    def test_glyphs_are_escaped_so_editors_cannot_drop_them(self):
        for path in PLUGIN_ROOT.glob("*.qml"):
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(re.search("[\ue000-\uf8ff\U000f0000-\U000ffffd]", text), path.name)


if __name__ == "__main__":
    unittest.main()

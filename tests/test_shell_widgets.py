"""The widget inside a real Quickshell, with Omarchy's own UI components.

Each test starts a private, invisible Quickshell (Qt's offscreen platform, its
own runtime and XDG folders, a notify-send that only writes to a file) and
creates one widget per "monitor" the way the bar does: create first, then hand
over bar, module name and settings. Offscreen Qt has no layer shell, so only
the host's KeyboardPanel popup is replaced, by a window-less stand-in with the
same open/fade lifecycle. Feeds and clock come from test_offline_journeys.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_offline_journeys import A, B, Feeds, at, flight, ms  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OMARCHY_SHELL = Path("/usr/share/omarchy/shell")
MODULE = "reynkedevos.flights"
PLUGIN_QML = ("FlightTracker.qml", "Service.qml", "SetupView.qml")

HARNESS = r"""
import QtQuick
import Quickshell
import Quickshell.Io
import qs.Ui

ShellRoot {
  id: harness

  readonly property string pluginDir: "file://" + Quickshell.env("FLIGHTS_PLUGIN_DIR")
  readonly property bool replacementBar: Quickshell.env("FLIGHTS_BAR") === "replacement"
  property var service: null
  property var widgets: []
  property var saved: null

  FileView { id: manifestFile; path: Quickshell.env("FLIGHTS_PLUGIN_DIR") + "/manifest.json"; blockLoading: true }

  // What the host hands a third-party plugin: its own service (none under a replacement bar) and settings.
  QtObject {
    id: pluginShell
    function serviceFor(id) { return harness.replacementBar ? null : harness.service }
    function updateEntryInline(id, entry) { harness.saved = entry; harness.inject(entry); return true }
  }

  Component { id: monitor; FloatingWindow { implicitWidth: 420; implicitHeight: 640; visible: true } }
  Component {
    id: barApi
    PluginBarApi {
      pluginId: "reynkedevos.flights"; moduleName: "reynkedevos.flights"; shell: pluginShell
      foreground: "white"; barForeground: "white"; urgent: "red"; fontFamily: "sans-serif"; barSize: 30
    }
  }

  function inject(entry) {
    var settings = {}
    for (var key in entry) if (key !== "id") settings[key] = entry[key]
    for (var i = 0; i < widgets.length; i++) {
      widgets[i].item.bar = widgets[i].api
      widgets[i].item.moduleName = "reynkedevos.flights"
      widgets[i].item.settings = JSON.parse(JSON.stringify(settings))
    }
  }

  Component.onCompleted: {
    var manifest = JSON.parse(manifestFile.text())
    if (manifest.kinds.indexOf("service") !== -1) {
      var serviceComponent = Qt.createComponent(pluginDir + "/" + manifest.entryPoints.service, Component.PreferSynchronous)
      service = serviceComponent.createObject(null)
      if (!service) console.log("HARNESS service failed: " + serviceComponent.errorString())
    }
    var widgetComponent = Qt.createComponent(pluginDir + "/" + manifest.entryPoints.barWidget, Component.PreferSynchronous)
    var created = []
    for (var i = 0; i < Number(Quickshell.env("FLIGHTS_WIDGETS") || "1"); i++) {
      var window = monitor.createObject(harness)
      var item = widgetComponent.createObject(window.contentItem)
      if (!item) console.log("HARNESS widget failed: " + widgetComponent.errorString())
      created.push({ item: item, window: window, api: barApi.createObject(null) })
    }
    widgets = created
    var entry = JSON.parse(Quickshell.env("FLIGHTS_SETTINGS") || "{}")
    Qt.callLater(function() { harness.inject(entry) })
    console.log("HARNESS ready")
  }

  function descendants(item) {
    var all = [item]
    for (var i = 0; i < item.children.length; i++) all = all.concat(descendants(item.children[i]))
    return all
  }

  function find(index, test) {
    var all = descendants(widgets[index].item)
    for (var i = 0; i < all.length; i++) if (test(all[i])) return all[i]
    return null
  }

  function setupView(index) {
    return find(index, function(item) { return typeof item.addFlight === "function" })
  }

  function textFields(index) {
    var view = setupView(index)
    return view ? descendants(view).filter(function(item) { return item.placeholderText !== undefined && item.selectAll }) : []
  }

  function focusedText(index) {
    for (var probe = widgets[index].item.Window.activeFocusItem; probe; probe = probe.parent)
      if (probe.placeholderText !== undefined && probe.selectAll) return probe.text
    return ""
  }

  function describeFocus(index) {
    var item = widgets[index].item.Window.activeFocusItem
    for (var probe = item; probe; probe = probe.parent)
      if (probe.placeholderText !== undefined && probe.selectAll) return "field:" + (probe.placeholderText || probe.text)
    return item ? String(item).split(/[_(]/)[0] : "none"
  }

  IpcHandler {
    target: "flights"

    function state(index: int): string {
      var widget = harness.widgets[index].item
      var view = harness.setupView(index)
      return JSON.stringify({
        opened: widget.opened, setupShown: widget.setupShown, editing: widget.editingTrip,
        loading: widget.loading, legs: widget.report.legs.length, stage: widget.report.journey.stage || "",
        lastError: widget.lastError, items: harness.descendants(widget).length, focus: harness.describeFocus(index),
        focusText: harness.focusedText(index),
        setupLegs: view ? view.legs.length : -1, setupStep: view ? view.step : 0, setupError: view ? view.error : "",
        looking: view ? view.looking : false, saved: harness.saved
      })
    }
    function open(index: int): string { var start = Date.now(); harness.widgets[index].item.open(); return String(Date.now() - start) }
    function close(index: int): void { harness.widgets[index].item.close() }
    function refresh(index: int): void { harness.widgets[index].item.refresh() }
    function collect(): void { gc() }
    function editTrip(index: int): void { harness.widgets[index].item.editTrip() }
    function type(index: int, field: int, text: string): string {
      var fields = harness.textFields(index)
      if (field >= fields.length) return "no field"
      fields[field].text = text
      return "ok"
    }
    function setup(index: int, action: string): string {
      var view = harness.setupView(index)
      if (!view) return "no setup"
      if (action === "add") view.addFlight()
      else if (action === "next") view.next()
      else if (action === "save") view.save()
      else if (action === "cancel") view.cancelled()
      return "ok"
    }
  }
}
"""

# The host's KeyboardPanel without the layer-shell window: same properties, open/fade lifecycle and focus hand-off.
KEYBOARD_PANEL = r"""
import QtQuick
import qs.Commons

Item {
  id: root
  required property Item anchorItem
  required property QtObject bar
  property var owner: null
  property int padding: Style.spacing.popupPadding
  property int contentWidth: Style.space(280)
  property int contentHeight: Style.space(200)
  property bool open: false
  property Item focusTarget: null
  default property alias contentItem: contentHolder.children

  visible: open || card.opacity > 0

  function fittedContentWidth(width, cap) { return Math.round(cap ? Math.min(width, cap) : width) }
  function fittedContentHeight(height, cap) { var desired = height + padding * 2; return Math.round(cap ? Math.min(desired, cap) : desired) }
  function close() { if (owner && "close" in owner) owner.close(); else root.open = false }

  onOpenChanged: if (open && focusTarget) Qt.callLater(function() { if (root.open && root.focusTarget) root.focusTarget.forceActiveFocus() })

  Item {
    id: card
    width: root.contentWidth
    height: root.contentHeight
    opacity: root.open ? 1 : 0
    Behavior on opacity { NumberAnimation { duration: 140; easing.type: Easing.OutCubic } }
    Item { id: contentHolder; anchors.fill: parent; anchors.margins: root.padding }
  }
}
"""

NOTIFY_SEND = """#!/bin/sh
printf '%s\\n' "$*" >> "$FLIGHTS_NOTIFICATIONS"
"""


def shell_available() -> bool:
    return shutil.which("qs") is not None and (OMARCHY_SHELL / "Ui" / "KeyboardPanel.qml").exists()


class Shell:
    """One private Quickshell running the harness above."""

    def __init__(self, plugin: Path, folder: Path, feeds: Feeds, *, widgets: int, bar: str, settings: dict, now):
        config = folder / "config"
        (config / "Ui").mkdir(parents=True)
        (config / "Commons").symlink_to(OMARCHY_SHELL / "Commons")
        for path in (OMARCHY_SHELL / "Ui").iterdir():
            if path.name != "KeyboardPanel.qml":
                (config / "Ui" / path.name).symlink_to(path)
        (config / "Ui" / "KeyboardPanel.qml").write_text(KEYBOARD_PANEL, encoding="utf-8")
        (config / "shell.qml").write_text(HARNESS, encoding="utf-8")
        fake_bin = folder / "bin"
        fake_bin.mkdir()
        (fake_bin / "notify-send").write_text(NOTIFY_SEND, encoding="utf-8")
        (fake_bin / "notify-send").chmod(0o755)
        self.notifications_file = folder / "notifications.txt"
        self.config = str(config / "shell.qml")
        self.env = {
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '/usr/bin')}",
            "HOME": str(folder / "home"),
            "XDG_RUNTIME_DIR": str(folder / "run"),
            "XDG_STATE_HOME": str(folder / "state"),
            "XDG_CACHE_HOME": str(folder / "cache"),
            "XDG_CONFIG_HOME": str(folder / "config-home"),
            "QT_QPA_PLATFORM": "offscreen",
            "TZ": "UTC",
            "FLIGHTS_PLUGIN_DIR": str(plugin),
            "FLIGHTS_WIDGETS": str(widgets),
            "FLIGHTS_BAR": bar,
            "FLIGHTS_SETTINGS": json.dumps({"id": MODULE, **settings}),
            "FLIGHTS_NOTIFICATIONS": str(self.notifications_file),
            "FLIGHTS_TEST_ORIGIN": feeds.origin,
            "FLIGHTS_TEST_NOW_MS": str(ms(now)),
        }
        (folder / "run").mkdir(mode=0o700)
        self.log = open(folder / "shell.log", "w+", encoding="utf-8")
        self.process = subprocess.Popen(["qs", "-p", self.config], env=self.env, stdout=self.log, stderr=subprocess.STDOUT)
        self.wait(lambda: "HARNESS ready" in self.output(), what="the harness to start")

    def output(self) -> str:
        self.log.flush()
        self.log.seek(0)
        return self.log.read()

    def call(self, function: str, *args) -> str:
        done = subprocess.run(
            ["qs", "-p", self.config, "ipc", "call", "flights", function, *map(str, args)],
            env=self.env, capture_output=True, text=True, timeout=20,
        )
        if done.returncode != 0:
            raise AssertionError(f"ipc {function} failed: {done.stderr}\n{self.output()}")
        return done.stdout.strip()

    def state(self, index: int = 0) -> dict:
        return json.loads(self.call("state", index))

    def wait(self, condition, what: str, timeout: float = 20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            if self.process.poll() is not None:
                break
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for {what}\n{self.output()}")

    def notifications(self) -> list[str]:
        return self.notifications_file.read_text().splitlines() if self.notifications_file.exists() else []

    def stop(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()


@unittest.skipUnless(shell_available(), "needs Quickshell (qs) and the Omarchy shell in /usr/share/omarchy/shell")
class ShellTest(unittest.TestCase):
    plugin = ROOT

    def setUp(self):
        self.feeds = Feeds()
        self.addCleanup(self.feeds.close)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.feeds.flight(A, flight("FRA", "MUC", "2026-12-23T08:00", "2026-12-23T09:00", gate="A12"))
        self.feeds.flight(B, flight("MUC", "DXB", "2026-12-23T10:30", "2026-12-23T16:30"))

    def start(self, *, widgets: int = 1, bar: str = "builtin", legs: str = A, now=at("2026-12-23T06:00")) -> Shell:
        shell = Shell(
            self.plugin, self.folder / f"shell-{len(list(self.folder.glob('shell-*')))}", self.feeds,
            widgets=widgets, bar=bar, settings={"legs": legs, "homeTimezone": "Europe/Berlin"}, now=now,
        )
        self.addCleanup(self.assert_quiet, shell)
        self.addCleanup(shell.stop)
        return shell

    def assert_quiet(self, shell: Shell):
        """No QML warning may come from the plugin's own files."""
        output = shell.output()
        shell.log.close()
        noisy = [line for line in output.splitlines() if any(name in line for name in PLUGIN_QML)]
        self.assertEqual(noisy, [])

    def runs(self) -> int:
        """Backend runs so far: each one asks FlightStats for the first leg once."""
        return sum(1 for path in self.feeds.requests if path.startswith("/v2/flight-tracker/LH/1001"))

    def reported(self, shell: Shell, widgets: int = 1):
        for index in range(widgets):
            shell.wait(lambda index=index: shell.state(index)["legs"] > 0 and not shell.state(index)["loading"],
                       what=f"widget {index} to show the trip")


class SharedFeedTests(ShellTest):
    def test_monitors_share_one_backend_run_and_one_notification(self):
        shell = self.start(widgets=3)
        self.reported(shell, 3)
        self.assertEqual(self.runs(), 1)

        self.feeds.flight(A, flight("FRA", "MUC", "2026-12-23T08:00", "2026-12-23T09:00", departed=True))
        shell.call("refresh", 2)
        shell.wait(lambda: self.runs() == 2 and not shell.state(0)["loading"], what="the second run")
        time.sleep(0.5)
        self.assertEqual(self.runs(), 2)
        self.assertEqual([state["stage"] for state in map(shell.state, range(3))], ["en-route"] * 3)
        self.assertEqual(shell.notifications(), ["--app-name Flights --urgency normal --expire-time 12000 LH1001 is airborne Now heading to Munich · 10:00 at home"])

    def test_widgets_under_a_replacement_bar_poll_on_their_own_and_still_notify_once(self):
        shell = self.start(widgets=2, bar="replacement")
        self.reported(shell, 2)
        self.assertEqual(self.runs(), 2)

        self.feeds.flight(A, flight("FRA", "MUC", "2026-12-23T08:00", "2026-12-23T09:00", departed=True))
        shell.call("refresh", 0)
        shell.call("refresh", 1)
        shell.wait(lambda: self.runs() == 4 and not shell.state(0)["loading"] and not shell.state(1)["loading"],
                   what="both widgets to refresh")
        self.assertEqual(len(shell.notifications()), 1)

    def test_opening_the_panel_reuses_a_fresh_report(self):
        shell = self.start()
        self.reported(shell)
        shell.call("open", 0)
        shell.wait(lambda: shell.state()["opened"], what="the panel to open")
        time.sleep(0.5)
        self.assertEqual(self.runs(), 1)
        shell.call("refresh", 0)  # R and right-click still ask for one
        shell.wait(lambda: self.runs() == 2, what="the requested refresh")


class OnDemandPanelTests(ShellTest):
    def test_panel_content_exists_only_while_the_panel_shows(self):
        shell = self.start(widgets=2)
        self.reported(shell, 2)
        # Offscreen, only the last window is active and can report focus, so widget 1 opens.
        closed = shell.state(1)["items"]
        shell.call("open", 1)
        opened = shell.state(1)["items"]
        self.assertGreater(opened, closed + 20)
        self.assertEqual(shell.state(0)["items"], closed, "the other monitor's panel stays empty")
        shell.call("close", 1)
        self.assertEqual(shell.state(1)["items"], opened, "still there while it fades out")
        shell.wait(lambda: shell.state(1)["items"] == closed, what="the content to go after the fade")
        shell.call("open", 1)
        self.assertEqual(shell.state(1)["items"], opened)
        shell.wait(lambda: shell.state(1)["focus"] == "PanelKeyCatcher", what="focus in the reopened panel")

    def test_a_first_run_setup_keeps_its_flights_and_typing_while_closed(self):
        shell = self.start(legs="")
        empty = shell.state()["items"]
        shell.call("open", 0)
        shell.wait(lambda: shell.state()["focus"] == "field:Flight, e.g. LH400", what="the setup")
        shell.call("type", 0, 0, "LH2002")
        shell.call("type", 0, 1, "2026-12-23")
        shell.call("setup", 0, "add")
        shell.wait(lambda: shell.state()["setupLegs"] == 1, what="the looked-up flight")
        shell.call("type", 0, 0, "LH10")
        shell.call("close", 0)
        time.sleep(0.5)
        shell.call("open", 0)
        shell.wait(lambda: shell.state()["focus"] == "field:Next flight", what="focus back in the flight field")
        state = shell.state()
        self.assertEqual((state["setupLegs"], state["focusText"]), (1, "LH10"))
        self.assertGreater(state["items"], empty)


class SetupTests(ShellTest):
    def test_first_run_setup_adds_flights_and_starts_tracking(self):
        shell = self.start(legs="")
        shell.call("open", 0)
        shell.wait(lambda: shell.state()["focus"] == "field:Flight, e.g. LH400", what="focus in the flight field")
        shell.call("type", 0, 0, "lh 2002")
        shell.call("type", 0, 1, "2026-12-23")
        shell.call("setup", 0, "add")
        shell.wait(lambda: shell.state()["setupLegs"] == 1, what="the looked-up flight")
        shell.call("type", 0, 0, "LH1001")
        shell.call("setup", 0, "add")
        shell.wait(lambda: shell.state()["setupLegs"] == 2, what="the second flight")
        shell.call("setup", 0, "next")
        shell.wait(lambda: shell.state()["focus"].startswith("field:"), what="focus in the minutes field")
        shell.call("type", 0, 2, "45")
        shell.call("setup", 0, "save")
        shell.wait(lambda: shell.state()["legs"] == 2, what="the trip to be tracked")
        state = shell.state()
        # Kept in departure order, whatever order they were added in.
        self.assertEqual(state["saved"], {"id": MODULE, "legs": f"{A},{B}", "homeTimezone": "Europe/Berlin", "leaveLeadMinutes": 45})
        self.assertEqual((state["setupShown"], state["focus"]), (False, "PanelKeyCatcher"))

    def test_editing_the_trip_and_closing_drops_the_edit(self):
        shell = self.start()
        self.reported(shell)
        shell.call("open", 0)
        shell.call("editTrip", 0)
        shell.wait(lambda: shell.state()["setupLegs"] == 1, what="the setup with the current flight")
        self.assertEqual(shell.state()["focus"], "field:Next flight")
        shell.call("close", 0)
        time.sleep(0.4)
        shell.call("open", 0)
        shell.wait(lambda: shell.state()["opened"], what="the panel to reopen")
        state = shell.state()
        self.assertEqual((state["setupShown"], state["editing"], state["focus"]), (False, False, "PanelKeyCatcher"))

    def test_editing_the_trip_adds_a_flight(self):
        shell = self.start()
        self.reported(shell)
        shell.call("open", 0)
        shell.call("editTrip", 0)
        shell.wait(lambda: shell.state()["setupLegs"] == 1, what="the setup")
        shell.call("type", 0, 0, "LH2002")
        shell.call("type", 0, 1, "2026-12-23")
        shell.call("setup", 0, "add")
        shell.wait(lambda: shell.state()["setupLegs"] == 2, what="the added flight")
        shell.call("setup", 0, "next")
        shell.call("setup", 0, "save")
        shell.wait(lambda: shell.state()["legs"] == 2, what="the edited trip")
        self.assertEqual(shell.state()["saved"]["legs"], f"{A},{B}")
        self.assertEqual(self.runs(), 2)


if __name__ == "__main__":
    unittest.main()

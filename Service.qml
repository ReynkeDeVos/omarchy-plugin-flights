import QtQuick
import Quickshell
import Quickshell.Io

// The trip feed every monitor's widget shows: one refresh timer, one backend
// run per refresh, one report, one countdown clock and one round of
// notifications. The host keeps a single instance (manifest kind "service").
// A replacement bar hides services from the widgets it hosts; there each
// widget loads its own copy of this file, as every widget polled before.
Item {
  id: root

  // The widget's entry from shell.json; the widgets hand it over.
  property var settings: ({})
  // The widgets showing this feed. With none left, for example after a switch
  // to a replacement bar whose widgets load their own copy, it stops refreshing.
  property var widgets: []
  readonly property bool watched: widgets.length > 0
  readonly property int shortestRefreshSeconds: 30

  property var report: emptyReport()
  property bool loading: false
  property bool refreshQueued: false
  property string lastError: ""
  property real reportAt: 0
  property real now: Date.now()

  readonly property string configuredLegs: String(setting("legs", ""))
  readonly property bool needsSetup: configuredLegs.trim() === ""
  readonly property int refreshSeconds: Math.max(shortestRefreshSeconds, parseInt(setting("refreshSeconds", 60), 10) || 60)
  readonly property int leaveLeadMinutes: Math.max(0, parseInt(setting("leaveLeadMinutes", 60), 10) || 0)
  // Empty means the system zone; the report names the zone the backend actually used.
  readonly property string homeTimezone: String(setting("homeTimezone", ""))
  readonly property bool notificationsEnabled: setting("notifications", true) === true
  // The prebuilt Rust backend; it answers and exits, so nothing stays running between refreshes.
  readonly property string backendPath: decodeURIComponent(String(Qt.resolvedUrl("bin/flight-status")).replace(/^file:\/\//, ""))
  readonly property var pickup: report && report.pickup ? report.pickup : ({})

  function setting(name, fallback) {
    var value = settings ? settings[name] : undefined
    return value === undefined || value === null ? fallback : value
  }

  function attach(widget) {
    if (widgets.indexOf(widget) === -1) widgets = widgets.concat([widget])
  }

  function detach(widget) {
    widgets = widgets.filter(function(item) { return item !== widget })
  }

  function emptyReport() {
    return { "legs": [], "journey": {}, "events": [], "errors": [] }
  }

  function command() {
    return [root.backendPath, "--legs", root.configuredLegs,
            "--home-timezone", root.homeTimezone,
            "--leave-lead-minutes", String(root.leaveLeadMinutes)]
  }

  function refresh() {
    if (root.needsSetup || !root.watched) return
    if (fetchProc.running) {
      refreshQueued = true
      return
    }
    loading = true
    lastError = ""
    fetchProc.command = command()
    fetchProc.running = true
  }

  // Opening a panel shows what is there; a report younger than the shortest
  // refresh interval, or one on its way, is fresh enough.
  function refreshIfStale() {
    if (!loading && Date.now() - reportAt >= shortestRefreshSeconds * 1000) refresh()
  }

  // A saved trip starts over, unless its first run is already under way.
  function restart() {
    report = emptyReport()
    reportAt = 0
    Qt.callLater(function() {
      if (!(fetchProc.running && JSON.stringify(fetchProc.command) === JSON.stringify(root.command()))) root.refresh()
    })
  }

  function announce(events) {
    if (!root.notificationsEnabled || !events) return
    for (var index = 0; index < events.length; index++) {
      var item = events[index]
      Quickshell.execDetached([
        "notify-send", "--app-name", "Flights", "--urgency", String(item.urgency || "normal"),
        "--expire-time", item.urgency === "critical" ? "0" : "12000",
        String(item.title || "Flight update"), String(item.body || "")
      ])
    }
  }

  function acceptReport(text) {
    var raw = String(text || "").trim()
    if (raw === "") {
      lastError = "No flight data. Try again shortly."
      return
    }
    try {
      var parsed = JSON.parse(raw)
      if (!parsed || !parsed.legs) throw new Error("missing legs")
      report = parsed
      reportAt = Date.now()
      lastError = parsed.errors && parsed.errors.length ? parsed.errors.join(" · ") : ""
      announce(parsed.events)
    } catch (error) {
      lastError = "Flight data could not be read."
    }
  }

  Process {
    id: fetchProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.acceptReport(text)
    }
    onExited: function(exitCode) {
      root.loading = false
      if (exitCode !== 0 && root.lastError === "")
        root.lastError = "Flight update failed. Try again."
      if (root.refreshQueued) {
        root.refreshQueued = false
        Qt.callLater(root.refresh)
      }
    }
  }

  // Ticks the leave-by countdown between refreshes, while there is one to tick.
  Timer {
    interval: 30000
    running: root.watched && !!root.pickup.leaveEpochMs && !root.pickup.done
    repeat: true
    onRunningChanged: root.now = Date.now()
    onTriggered: root.now = Date.now()
  }

  Timer {
    interval: root.refreshSeconds * 1000
    running: root.watched && !root.needsSetup
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }
}

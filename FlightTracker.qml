pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "reynkedevos.flights"
  ipcTarget: "reynkedevos.flights"

  property var report: ({ "legs": [], "journey": {}, "events": [], "errors": [] })
  property bool loading: false
  property bool refreshQueued: false
  property bool editingTrip: false
  property string lastError: ""

  readonly property var legs: report && report.legs ? report.legs : []
  readonly property var journey: report && report.journey ? report.journey : ({})
  readonly property int activeLegIndex: Number(journey.activeLegIndex || 0)
  readonly property var activeLeg: activeLegIndex >= 0 && activeLegIndex < legs.length ? legs[activeLegIndex] : null
  readonly property var nextLeg: activeLegIndex + 1 < legs.length ? legs[activeLegIndex + 1] : null
  // Python decides what counts as late, so the red icon and the delay notification agree.
  readonly property int activeDelay: activeLeg ? Number(activeLeg.delayMinutes || 0) : 0
  readonly property bool activeLate: !!(activeLeg && activeLeg.late)
  readonly property int refreshSeconds: Math.max(30, parseInt(setting("refreshSeconds", 60), 10) || 60)
  readonly property string configuredLegs: String(setting("legs", ""))
  readonly property bool needsSetup: configuredLegs.trim() === ""
  readonly property bool setupShown: editingTrip || needsSetup
  readonly property int leaveLeadMinutes: Math.max(0, parseInt(setting("leaveLeadMinutes", 60), 10) || 0)
  readonly property var pickup: report && report.pickup ? report.pickup : ({})
  readonly property real leaveMinutes: pickup.leaveEpochMs ? Math.round((pickup.leaveEpochMs - clock.now) / 60000) : NaN
  readonly property bool leaveSoon: !pickup.done && isFinite(leaveMinutes) && leaveMinutes <= 15 && leaveMinutes > -90
  readonly property bool transferring: journey.stage === "connection" && activeLeg && activeLeg.phase === "scheduled"
  // Empty means the system zone; the report names the zone Python actually used.
  readonly property string homeTimezone: String(setting("homeTimezone", ""))
  readonly property bool notificationsEnabled: setting("notifications", true) === true
  readonly property string scriptPath: decodeURIComponent(String(Qt.resolvedUrl("bin/flight_status.py")).replace(/^file:\/\//, ""))
  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.5)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property string iconFontFamily: "Font Awesome 7 Free Solid"
  // The triangle is reserved for trouble: cancelled, diverted, or a connection in danger.
  readonly property bool hasAlert: (activeLeg ? !!activeLeg.alert : false) || !!journey.tightConnection
  readonly property bool approaching: activeLeg
    && ["arriving", "final-approach"].indexOf(String(activeLeg.phase || "")) >= 0
  readonly property bool boardingAttention: activeLeg && activeLeg.phase === "boarding-soon"
  readonly property bool departureAttention: activeLeg
    && ["departing-soon", "awaiting-departure"].indexOf(String(activeLeg.phase || "")) >= 0
  readonly property bool nextLegDelayed: !!nextLeg && nextLegStatus() !== "On time"

  function refresh() {
    if (root.needsSetup) return
    if (fetchProc.running) {
      refreshQueued = true
      return
    }
    loading = true
    lastError = ""
    var command = ["python3", root.scriptPath, "--legs", root.configuredLegs,
                   "--home-timezone", root.homeTimezone,
                   "--leave-lead-minutes", String(root.leaveLeadMinutes)]
    if (!root.notificationsEnabled) command.push("--no-events")
    fetchProc.command = command
    fetchProc.running = true
  }

  // Like the built-in clock: updateEntryInline replaces the whole entry, so the other keys ride along.
  function persistSettings(values) {
    var entry = { id: root.moduleName }
    for (var existing in root.settings) if (existing !== "id") entry[existing] = root.settings[existing]
    for (var key in values) entry[key] = values[key]
    root.settings = entry
    if (root.bar && root.bar.shell && typeof root.bar.shell.updateEntryInline === "function")
      root.bar.shell.updateEntryInline(root.moduleName, entry)
  }

  // The last report has route and times; before the first one, the configured keys have to do.
  function editTrip() {
    var records = legs.length ? legs : configuredLegs.split(",").filter(function(item) { return item.trim() !== "" })
      .map(function(item) {
        var key = item.replace(/\s+/g, "").toUpperCase()
        return { "key": key, "code": key.split("@")[0] }
      })
    editingTrip = true
    setup.start(records, leaveLeadMinutes)
  }

  function saveTrip(legKeys, leadMinutes) {
    persistSettings({ "legs": legKeys, "leaveLeadMinutes": leadMinutes })
    editingTrip = false
    report = { "legs": [], "journey": {}, "events": [], "errors": [] }
    Qt.callLater(function() {
      root.refresh()
      keyCatcher.forceActiveFocus()
    })
  }

  function formatDuration(minutes) {
    var value = Math.max(0, Math.round(Number(minutes)))
    if (!isFinite(value)) return "—"
    var hours = Math.floor(value / 60)
    var rest = value % 60
    if (hours >= 24) return Math.floor(hours / 24) + "d " + (hours % 24) + "h"
    if (hours > 0) return hours + "h " + (rest < 10 ? "0" : "") + rest + "m"
    return rest + "m"
  }

  function phaseLabel(leg) {
    if (!leg) return loading ? "Updating" : "Waiting"
    switch (String(leg.phase || "unknown")) {
      case "scheduled": return "Waiting"
      case "boarding-soon": return "Boarding soon"
      case "departing-soon": return "Departure soon"
      case "awaiting-departure": return "Waiting to depart"
      case "airborne": return "In the air"
      case "arriving": return "Landing soon"
      case "final-approach": return "Final approach"
      case "landed": return "Landed"
      case "cancelled": return "Cancelled"
      default: return "Status unavailable"
    }
  }

  function iconGlyph() {
    if (hasAlert || (activeLeg && activeLeg.phase === "cancelled")) return "\uf071"
    if (journey.stage === "complete" || (activeLeg && activeLeg.phase === "landed")) return "\uf058"
    if (leaveSoon) return "\uf1b9"
    if (transferring) return "\uf362"
    if (!activeLeg || activeLeg.phase === "scheduled") return "\uf0f2"
    if (boardingAttention) return "\uf145"
    if (departureAttention) return "\uf5b0"
    if (approaching) return "\uf5af"
    if (activeLeg.phase === "airborne") return "\uf072"
    return "\uf017"
  }

  function iconColor() {
    if (hasAlert || (activeLeg && activeLeg.phase === "cancelled")) return root.urgent
    if (journey.stage === "complete") return Color.accent
    if (leaveSoon || activeLate) return root.urgent
    if (approaching || boardingAttention || departureAttention) return Color.accent
    return root.foreground
  }

  function countdownValue() {
    if (journey.stage === "complete") return "Arrived"
    if (journey.stage === "disrupted") return "Cancelled"
    var minutes = Number(journey.nextEventMinutes)
    return isFinite(minutes) && minutes >= 0 ? formatDuration(minutes) : "—"
  }

  function countdownLabel() {
    if (journey.stage === "complete") return "in " + (journey.destination || "destination")
    if (journey.stage === "disrupted") return "Check with " + (activeLeg && activeLeg.airline || "the airline")
    if (journey.nextEventKind === "departure") return "until departure"
    if (journey.nextEventKind === "arrival") return "until landing"
    return phaseLabel(activeLeg)
  }

  function eventAirport() {
    if (!activeLeg) return null
    if (journey.nextEventKind === "departure") return activeLeg.departure || null
    return activeLeg.arrival || null
  }

  function homeCity() {
    var zone = String(report.homeTimezone || root.homeTimezone)
    var parts = zone.split("/")
    return String(parts[parts.length - 1] || zone).replace(/_/g, " ")
  }

  // "10:05 Frankfurt · 04:05 New York"; the home half is dropped when the clocks agree.
  function placeTimes(airport) {
    if (!airport || !airport.time) return ""
    var text = airport.time + " " + String(airport.city || airport.code || "")
    if (airport.homeTime && airport.homeTime !== airport.time) text += " · " + airport.homeTime + " " + homeCity()
    return text
  }

  function countdownCaption() {
    var times = journey.stage === "complete" || journey.stage === "disrupted" ? "" : placeTimes(eventAirport())
    return times ? countdownLabel() + " · " + times : countdownLabel()
  }

  function progressPercent() {
    if (!activeLeg || !activeLeg.progress) return 0
    return Math.max(0, Math.min(100, Math.round(Number(activeLeg.progress.percent || 0))))
  }

  function nextLegStatus() {
    if (!nextLeg) return ""
    if (nextLeg.cancelled) return "Cancelled"
    if (nextLeg.late) return nextLeg.delayMinutes + " min late"
    return "On time"
  }

  function leaveText() {
    if (pickup.done) return "Pickup now"
    if (!pickup.leaveHomeTime) return "Leave time unknown"
    // ponytail: weekday uses the machine's timezone, assumed to equal homeTimezone.
    var leave = new Date(pickup.leaveEpochMs || clock.now)
    var day = leave.toDateString() === new Date(clock.now).toDateString()
      ? "" : ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][leave.getDay()] + " "
    return "Leave by " + day + pickup.leaveHomeTime
  }

  function leaveCountdown() {
    if (pickup.done || !isFinite(leaveMinutes)) return ""
    return leaveMinutes > 0 ? "in " + formatDuration(leaveMinutes) : "now"
  }

  function arrivalDetail() {
    var details = ["Landing " + String(pickup.landingHomeTime || "—")]
    if (pickup.airport) details.push(pickup.airport + (pickup.terminal ? " T" + pickup.terminal : ""))
    if (pickup.gate) details.push("Gate " + pickup.gate)
    if (pickup.baggage) details.push("Belt " + pickup.baggage)
    return details.join(" · ")
  }

  // Only the transfer into the "Then" leg; during a transfer the countdown already covers it.
  function connectText() {
    var minutes = Number(journey.connectionMinutes)
    if (journey.connectionLegIndex !== activeLegIndex + 1 || !isFinite(minutes)) return ""
    return minutes < 0 ? "Connection at risk" : formatDuration(minutes) + " to connect"
  }

  function departureDetail() {
    if (!activeLeg || !activeLeg.departure || (!boardingAttention && !departureAttention)) return ""
    var details = []
    if (activeLeg.departure.gate) details.push("Gate " + activeLeg.departure.gate)
    if (activeLeg.departure.terminal) details.push("Terminal " + activeLeg.departure.terminal)
    return details.join(" · ")
  }

  function tooltipText() {
    if (needsSetup) return "Add the flights to follow"
    if (!activeLeg) return loading ? "Updating flight…" : "Flight status unavailable"
    var lines = [activeLeg.code + " · " + phaseLabel(activeLeg)]
    var minutes = Number(journey.nextEventMinutes)
    if (isFinite(minutes) && minutes >= 0) {
      var action = journey.nextEventKind === "departure" ? "Departs" : "Lands"
      var airport = eventAirport()
      lines.push(action + " in " + formatDuration(minutes) + " · " + String(airport && airport.homeTime || "—") + " your time")
    }
    if (pickup.leaveHomeTime && !pickup.done) lines.push(leaveText() + " · " + leaveCountdown())
    lines.push("Left: details · Middle: map · Right: refresh")
    return lines.join("\n")
  }

  function openMap(leg) {
    if (!leg) return
    var aircraft = leg.aircraft || {}
    var url = aircraft.hex ? "https://globe.adsb.lol/?icao=" + aircraft.hex : String(leg.url || "")
    if (url !== "") Quickshell.execDetached(["omarchy-launch-browser", url])
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
      lastError = parsed.errors && parsed.errors.length ? parsed.errors.join(" · ") : ""
      announce(parsed.events)
    } catch (error) {
      lastError = "Flight data could not be read."
    }
  }

  onOpenedChanged: {
    if (!opened) {
      editingTrip = false  // like clock and weather: closing drops an edit; a first-run setup keeps its flights
      return
    }
    refresh()
    Qt.callLater(function() { panel.focusTarget.forceActiveFocus() })
  }

  Component.onCompleted: if (needsSetup) setup.start([], leaveLeadMinutes)

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

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

  // Ticks the leave-by countdown between feed refreshes.
  Timer {
    id: clock
    property real now: Date.now()
    interval: 30000
    running: true
    repeat: true
    onTriggered: now = Date.now()
  }

  Timer {
    interval: root.refreshSeconds * 1000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.iconGlyph()
    fontFamily: root.iconFontFamily
    slotSize: Style.bar.statusSlot
    fontSize: Style.font.body
    active: root.hasAlert || root.approaching || root.leaveSoon || root.transferring || root.boardingAttention
      || root.departureAttention || root.activeLate || root.journey.stage === "complete"
    activeColor: root.iconColor()
    tooltipText: root.tooltipText()
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) root.refresh()
      else if (buttonCode === Qt.MiddleButton) root.openMap(root.activeLeg)
      else root.toggle()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: root.setupShown ? setup.focusItem : keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(350))
    contentHeight: panel.fittedContentHeight(root.setupShown ? setup.implicitHeight : content.implicitHeight, Style.space(470))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: root.setupShown
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(text) {
        if (text === "r" || text === "R") root.refresh()
        else if (text === "e" || text === "E") root.editTrip()
        else if (text === "m" || text === "M") root.openMap(root.activeLeg)
      }

      SetupView {
        id: setup
        anchors.left: parent.left
        anchors.right: parent.right
        visible: root.setupShown
        scriptPath: root.scriptPath
        homeTimezone: root.homeTimezone
        foreground: root.foreground
        dim: root.dim
        urgent: root.urgent
        fontFamily: root.fontFamily
        canCancel: !root.needsSetup
        onSaved: function(legKeys, leadMinutes) { root.saveTrip(legKeys, leadMinutes) }
        onCancelled: {
          if (root.needsSetup) {
            root.close()
            return
          }
          root.editingTrip = false
          keyCatcher.forceActiveFocus()
        }
      }

      Flickable {
        anchors.fill: parent
        visible: !root.setupShown
        contentWidth: width
        contentHeight: content.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds

        ColumnLayout {
          id: content
          width: parent.width
          spacing: Style.space(14)

          RowLayout {
            Layout.fillWidth: true
            spacing: Style.space(10)

            Text {
              text: root.iconGlyph()
              color: root.iconColor()
              font.family: root.iconFontFamily
              font.pixelSize: Style.font.title
            }

            ColumnLayout {
              Layout.fillWidth: true
              spacing: 0

              Text {
                Layout.fillWidth: true
                textFormat: Text.PlainText
                text: root.journey.label || root.phaseLabel(root.activeLeg)
                color: root.iconColor()
                font.family: root.fontFamily
                font.pixelSize: Style.font.title
                font.bold: true
                elide: Text.ElideRight
              }

              Text {
                Layout.fillWidth: true
                textFormat: Text.PlainText
                text: root.activeLeg ? root.activeLeg.code + " · " + root.activeLeg.route
                  + (root.activeLate ? " · " + root.activeDelay + " min late" : "") : "Updating…"
                color: root.activeLate ? root.urgent : root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                elide: Text.ElideRight
              }
            }
          }

          ColumnLayout {
            Layout.fillWidth: true
            spacing: 0

            Text {
              Layout.fillWidth: true
              textFormat: Text.PlainText
              text: root.countdownValue()
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.display
              font.bold: true
            }

            Text {
              Layout.fillWidth: true
              textFormat: Text.PlainText
              text: root.countdownCaption()
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              elide: Text.ElideRight
            }
          }

          ColumnLayout {
            Layout.fillWidth: true
            visible: !!root.pickup.landingEpochMs
            spacing: Style.space(2)

            RowLayout {
              Layout.fillWidth: true

              Text {
                Layout.fillWidth: true
                textFormat: Text.PlainText
                text: root.leaveText()
                color: root.leaveSoon ? root.urgent : root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.title
                font.bold: true
              }

              Text {
                textFormat: Text.PlainText
                text: root.leaveCountdown()
                color: root.leaveSoon ? root.urgent : root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                font.bold: root.leaveSoon
              }
            }

            Text {
              Layout.fillWidth: true
              textFormat: Text.PlainText
              text: root.arrivalDetail()
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              elide: Text.ElideRight
            }
          }

          ColumnLayout {
            Layout.fillWidth: true
            visible: root.activeLeg && root.activeLeg.progress
              && root.activeLeg.departed && !root.activeLeg.landed
            spacing: Style.space(5)

            Rectangle {
              Layout.fillWidth: true
              implicitHeight: Style.space(3)
              radius: Style.cornerRadius > 0 ? height / 2 : 0
              color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.12)

              Rectangle {
                width: parent.width * root.progressPercent() / 100
                height: parent.height
                radius: parent.radius
                color: Style.selectedStateColor(root.foreground, Color.accent)
                // Newer shells scale motion and honour reduce-motion through Style.duration.
                Behavior on width { NumberAnimation { duration: typeof Style.duration === "function" ? Style.duration(420) : 420; easing.type: Easing.OutCubic } }
              }
            }

            RowLayout {
              Layout.fillWidth: true

              Text {
                textFormat: Text.PlainText
                text: root.activeLeg && root.activeLeg.departure ? root.activeLeg.departure.code : ""
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }

              Text {
                Layout.fillWidth: true
                text: root.progressPercent() + "% of this flight"
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
                horizontalAlignment: Text.AlignHCenter
              }

              Text {
                textFormat: Text.PlainText
                text: root.activeLeg && root.activeLeg.arrival ? root.activeLeg.arrival.code : ""
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
            }
          }

          Text {
            Layout.fillWidth: true
            visible: root.departureDetail() !== ""
            textFormat: Text.PlainText
            text: root.departureDetail()
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          Text {
            Layout.fillWidth: true
            visible: root.lastError !== ""
            textFormat: Text.PlainText
            text: root.lastError
            color: root.urgent
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }

          ColumnLayout {
            Layout.fillWidth: true
            visible: !!root.nextLeg
            spacing: Style.space(4)

            PanelSeparator {
              Layout.fillWidth: true
              foreground: root.foreground
            }

            RowLayout {
              Layout.fillWidth: true

              Text {
                Layout.fillWidth: true
                textFormat: Text.PlainText
                text: root.nextLeg ? "Then " + root.nextLeg.code + " · " + root.nextLeg.route : ""
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                font.bold: true
                elide: Text.ElideRight
              }

              Text {
                textFormat: Text.PlainText
                text: root.nextLegStatus()
                color: root.nextLegDelayed ? root.urgent : root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: root.nextLegDelayed
              }
            }

            RowLayout {
              Layout.fillWidth: true

              Text {
                Layout.fillWidth: true
                textFormat: Text.PlainText
                text: root.nextLeg ? root.placeTimes(root.nextLeg.departure) : ""
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                elide: Text.ElideRight
              }

              Text {
                textFormat: Text.PlainText
                text: root.connectText()
                color: root.journey.tightConnection ? root.urgent : root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: !!root.journey.tightConnection
              }
            }
          }

          RowLayout {
            Layout.fillWidth: true

            Text {
              Layout.fillWidth: true
              textFormat: Text.PlainText
              text: root.loading ? "Updating…" : "Updated " + Qt.formatDateTime(new Date(root.report.generatedAtMs || Date.now()), "HH:mm")
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            Text {
              visible: root.loading
              text: "\uf2f1"
              color: root.dim
              font.family: root.iconFontFamily
              font.pixelSize: Style.font.caption

              RotationAnimator on rotation {
                running: root.loading && Style.reduceMotion !== true
                from: 0
                to: 360
                duration: 900
                loops: Animation.Infinite
              }
            }

            PanelActionButton {
              visible: !!root.activeLeg
              iconText: "\uf279"
              tooltipText: "Live map (M)"
              fontFamily: root.iconFontFamily
              fontSize: Style.font.caption
              foreground: root.dim
              onClicked: root.openMap(root.activeLeg)
            }

            PanelActionButton {
              iconText: "\uf304"
              tooltipText: "Edit trip (E)"
              fontFamily: root.iconFontFamily
              fontSize: Style.font.caption
              foreground: root.dim
              onClicked: root.editTrip()
            }
          }
        }
      }
    }
  }
}

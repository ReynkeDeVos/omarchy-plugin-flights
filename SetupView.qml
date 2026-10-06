pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts
import Quickshell.Io
import qs.Commons
import qs.Ui

// Trip setup in two steps: the flights, then the time it takes to get to the
// airport. Each flight goes through `flight_status.py --lookup`, so only
// flights FlightStats knows reach the list. A form, so Tab walks every
// control; Esc leaves the setup from anywhere.
ColumnLayout {
  id: root

  property string scriptPath: ""
  property string homeTimezone: ""
  property color foreground: Color.foreground
  property color dim: Qt.darker(foreground, 1.5)
  property color urgent: Color.urgent
  property string fontFamily: Style.font.family
  property bool canCancel: false

  property int step: 1
  property var legs: []
  property int leadMinutes: 60
  property string error: ""
  property string pendingCode: ""
  property string pendingDate: ""
  readonly property bool looking: lookupProc.running
  readonly property var lastLeg: legs.length ? legs[legs.length - 1] : null
  readonly property Item focusItem: step === 1 ? flightField : leadField

  signal saved(string legs, int leadMinutes)
  signal cancelled()

  function start(records, lead) {
    legs = records
    step = 1
    error = ""
    flightField.text = ""
    dateField.text = lastLeg ? legDate(lastLeg) : isoDate(new Date())
    setLead(lead)
    Qt.callLater(focusStep)
  }

  function focusStep() {
    focusItem.selectAll()
    focusItem.forceActiveFocus()
  }

  function pad(number) {
    return (number < 10 ? "0" : "") + number
  }

  function isoDate(date) {
    return date.getFullYear() + "-" + pad(date.getMonth() + 1) + "-" + pad(date.getDate())
  }

  function parseIso(text) {
    var match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(text).trim())
    return match ? new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3])) : null
  }

  // "Tue 23 Dec", English like the rest of the shell.
  function dayLabel(iso) {
    var date = parseIso(iso)
    if (!date) return String(iso)
    return ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][date.getDay()] + " " + date.getDate() + " "
      + ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][date.getMonth()]
  }

  function legDate(leg) {
    return String(leg.key || "").split("@")[1] || ""
  }

  function shiftDate(days) {
    var date = parseIso(dateField.text) || new Date()
    date.setDate(date.getDate() + days)
    dateField.text = isoDate(date)
  }

  function setLead(minutes) {
    leadMinutes = Math.max(0, Math.min(240, Math.round(Number(minutes) || 0)))
    leadField.text = String(leadMinutes)
  }

  function hasLeg(key) {
    return legs.some(function(leg) { return leg.key === key })
  }

  function addFlight() {
    if (looking) return
    var code = flightField.text.replace(/\s+/g, "").toUpperCase()
    var date = dateField.text.trim()
    if (code === "") {
      if (legs.length) next()
      return
    }
    if (hasLeg(code + "@" + date)) {
      error = code + " on " + dayLabel(date) + " is already on the list."
      return
    }
    error = ""
    pendingCode = code
    pendingDate = date
    lookupProc.command = ["python3", scriptPath, "--lookup", code + "@" + date, "--home-timezone", homeTimezone]
    lookupProc.running = true
  }

  function acceptLookup(text) {
    var leg = null
    try { leg = JSON.parse(String(text || "")) } catch (parseError) {}
    if (!leg || !leg.key) {
      error = "Use a flight number like LH400 and a date like " + isoDate(new Date()) + "."
    } else if (leg.phase === "unknown") {
      error = "Couldn't find " + pendingCode + " on " + dayLabel(pendingDate) + ". Check the number and the local departure date."
    } else if (hasLeg(leg.key)) {
      error = leg.code + " on " + dayLabel(pendingDate) + " is already on the list."
    } else {
      // Kept in departure order, so flights can be added in any order.
      legs = legs.concat([leg]).sort(function(a, b) {
        return Number(a.departure && a.departure.epochMs || 0) - Number(b.departure && b.departure.epochMs || 0)
      })
      flightField.text = ""
    }
    flightField.forceActiveFocus()
  }

  function removeLeg(index) {
    legs = legs.filter(function(leg, at) { return at !== index })
    flightField.forceActiveFocus()
  }

  function next() {
    if (!legs.length || looking) return
    error = ""
    step = 2
    Qt.callLater(focusStep)
  }

  function back() {
    step = 1
    Qt.callLater(focusStep)
  }

  function save() {
    setLead(leadField.text)
    saved(legs.map(function(leg) { return leg.key }).join(","), leadMinutes)
  }

  function leaveExample() {
    var arrival = lastLeg && lastLeg.arrival || {}
    if (!arrival.epochMs) return ""
    var leave = new Date(arrival.epochMs - leadMinutes * 60000)
    var day = isoDate(leave) === isoDate(new Date()) ? "" : " on " + dayLabel(isoDate(leave))
    return "Leave by " + Qt.formatTime(leave, "HH:mm") + day + " to be at " + (arrival.code || "the airport")
      + " when " + lastLeg.code + " lands."
  }

  spacing: Style.spacing.panelGap

  Keys.onEscapePressed: function(event) {
    root.cancelled()
    event.accepted = true
  }

  Process {
    id: lookupProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.acceptLookup(text)
    }
  }

  PanelHero {
    Layout.fillWidth: true
    foreground: root.foreground
    fontFamily: root.fontFamily
    title: root.step === 1 ? "Which flights?" : "How long to the airport?"
    meta: "Step " + root.step + " of 2"
    iconComponent: Component {
      Text {
        textFormat: Text.PlainText
        text: root.step === 1 ? "\uf072" : "\uf1b9"
        color: Color.accent
        font.family: root.fontFamily
        font.pixelSize: Style.font.display
      }
    }
  }

  // Step 1: the flights.
  ColumnLayout {
    Layout.fillWidth: true
    visible: root.step === 1
    spacing: Style.spacing.rowGap

    Repeater {
      model: root.legs

      RowLayout {
        id: legRow
        required property var modelData
        required property int index
        Layout.fillWidth: true
        spacing: Style.spacing.controlGap

        ColumnLayout {
          Layout.fillWidth: true
          spacing: 0

          Text {
            Layout.fillWidth: true
            textFormat: Text.PlainText
            text: legRow.modelData.code + " · " + (legRow.modelData.route || "—")
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            elide: Text.ElideRight
          }

          Text {
            Layout.fillWidth: true
            textFormat: Text.PlainText
            text: root.dayLabel(root.legDate(legRow.modelData))
              + (legRow.modelData.departure && legRow.modelData.departure.time
                ? " · departs " + legRow.modelData.departure.time + " " + (legRow.modelData.departure.city || "") : "")
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            elide: Text.ElideRight
          }
        }

        PanelActionButton {
          iconText: "\uf00d"
          tooltipText: "Remove " + legRow.modelData.code
          fontFamily: root.fontFamily
          foreground: root.dim
          hoverColor: root.urgent
          focusable: true
          onClicked: root.removeLeg(legRow.index)
        }
      }
    }

    PanelSeparator {
      Layout.fillWidth: true
      visible: root.legs.length > 0
      foreground: root.foreground
    }

    RowLayout {
      Layout.fillWidth: true
      spacing: Style.spacing.controlGap

      TextField {
        id: flightField
        Layout.fillWidth: true
        placeholderText: root.legs.length ? "Next flight" : "Flight, e.g. LH400"
        foreground: root.foreground
        font.family: root.fontFamily
        readOnly: root.looking
        onTextEdited: {
          var at = cursorPosition
          text = text.toUpperCase()
          cursorPosition = at
        }
        Keys.onReturnPressed: root.addFlight()
        Keys.onEnterPressed: root.addFlight()
      }

      TextField {
        id: dateField
        Layout.preferredWidth: Style.space(96)
        foreground: root.foreground
        font.family: root.fontFamily
        inputMethodHints: Qt.ImhDate
        readOnly: root.looking
        Keys.onReturnPressed: root.addFlight()
        Keys.onEnterPressed: root.addFlight()
        Keys.onUpPressed: root.shiftDate(1)
        Keys.onDownPressed: root.shiftDate(-1)
      }

      Button {
        Layout.preferredWidth: Style.space(52)
        text: root.looking ? "" : "Add"
        iconText: root.looking ? "\uf2f1" : ""
        iconSpinning: root.looking
        tooltipText: "Look up and add the flight"
        foreground: root.foreground
        fontFamily: root.fontFamily
        bordered: true
        focusable: true
        onClicked: root.addFlight()
      }
    }

    Text {
      Layout.fillWidth: true
      textFormat: Text.PlainText
      text: root.looking ? "Looking up " + root.pendingCode + "…"
        : (root.parseIso(dateField.text) ? root.dayLabel(dateField.text) + " · " : "") + "↑ ↓ change the date, Enter adds"
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      elide: Text.ElideRight
    }
  }

  // Step 2: the time to the airport, with the leave time it produces.
  ColumnLayout {
    Layout.fillWidth: true
    visible: root.step === 2
    spacing: Style.spacing.rowGap

    RowLayout {
      Layout.alignment: Qt.AlignHCenter
      spacing: Style.spacing.controlGap

      PanelActionButton {
        iconText: "\uf068"
        tooltipText: "5 minutes less"
        fontFamily: root.fontFamily
        foreground: root.foreground
        focusable: true
        onClicked: root.setLead(root.leadMinutes - 5)
      }

      TextField {
        id: leadField
        Layout.preferredWidth: Style.space(84)
        foreground: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.display
        font.bold: true
        horizontalAlignment: TextInput.AlignHCenter
        inputMethodHints: Qt.ImhDigitsOnly
        validator: IntValidator { bottom: 0; top: 240 }
        onTextEdited: root.leadMinutes = Math.min(240, parseInt(text, 10) || 0)
        Keys.onUpPressed: root.setLead(root.leadMinutes + 5)
        Keys.onDownPressed: root.setLead(root.leadMinutes - 5)
        Keys.onReturnPressed: root.save()
        Keys.onEnterPressed: root.save()
      }

      PanelActionButton {
        iconText: "\uf067"
        tooltipText: "5 minutes more"
        fontFamily: root.fontFamily
        foreground: root.foreground
        focusable: true
        onClicked: root.setLead(root.leadMinutes + 5)
      }

      Text {
        textFormat: Text.PlainText
        text: "min"
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
      }
    }

    Text {
      Layout.fillWidth: true
      visible: text !== ""
      textFormat: Text.PlainText
      text: root.leaveExample()
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.body
      horizontalAlignment: Text.AlignHCenter
      wrapMode: Text.WordWrap
    }

    Text {
      Layout.fillWidth: true
      textFormat: Text.PlainText
      text: "Count from your door to arrivals. A reminder comes 15 minutes before and when it's time; if the landing moves, so do they."
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      horizontalAlignment: Text.AlignHCenter
      wrapMode: Text.WordWrap
    }
  }

  Text {
    Layout.fillWidth: true
    visible: root.error !== ""
    textFormat: Text.PlainText
    text: root.error
    color: root.urgent
    font.family: root.fontFamily
    font.pixelSize: Style.font.bodySmall
    wrapMode: Text.WordWrap
  }

  RowLayout {
    Layout.fillWidth: true
    spacing: Style.spacing.controlGap

    Button {
      visible: root.step === 2 || root.canCancel
      text: root.step === 2 ? "Back" : "Cancel"
      tooltipText: root.step === 2 ? "Back to the flights" : "Keep the current trip (Esc)"
      foreground: root.foreground
      fontFamily: root.fontFamily
      focusable: true
      onClicked: root.step === 2 ? root.back() : root.cancelled()
    }

    Item { Layout.fillWidth: true }

    Button {
      text: root.step === 1 ? "Next" : "Start tracking"
      tooltipText: root.step === 1 ? "Enter on an empty flight field" : "Enter in the minutes field"
      foreground: root.foreground
      fontFamily: root.fontFamily
      bordered: true
      focusable: true
      enabled: root.legs.length > 0 && !root.looking
      opacity: enabled ? 1 : 0.45
      onClicked: root.step === 1 ? root.next() : root.save()
    }
  }
}

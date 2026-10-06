# Flights

I wanted to track my partner's flights, so we can stay in touch better during her long trips (like Germany to Japan). This Omarchy bar widget follows one dated trip of up to two flights and tells me when to leave for the airport.

<p>
  <img src="docs/assets/en-route.avif" width="350" alt="En route: countdown to landing, leave-by time, flight progress and the connecting flight">
  <img src="docs/assets/transfer.avif" width="350" alt="Transfer: countdown to the connecting flight">
  <img src="docs/assets/boarding.avif" width="350" alt="Departing soon: gate and terminal">
  <img src="docs/assets/disrupted.avif" width="350" alt="A cancelled flight">
  <img src="docs/assets/arrived.avif" width="350" alt="Arrived: terminal, gate and baggage belt">
</p>

<sub>Real Lufthansa flights on 6 October 2026.</sub>

## Install

```bash
omarchy plugin add https://github.com/ReynkeDeVos/omarchy-plugin-flights.git
omarchy plugin enable reynkedevos.flights --section center
```

Then add the trip to the widget's entry in `~/.config/omarchy/shell.json`:

```json
{ "id": "reynkedevos.flights", "legs": "LH400@2026-12-23,LH2054@2026-12-24" }
```

Optional: `leaveLeadMinutes` (60), `homeTimezone` (`Europe/Berlin`), `refreshSeconds` (60), `notifications` (`true`).

Left-click opens the panel, middle-click the live map, right-click refreshes. Notifications cover takeoff, delays, gate changes, landing in 60/30/15 minutes, the transfer, when to leave, and arrival.

Data comes from FlightStats and adsb.lol/adsbdb without API keys. It can be late or wrong, so check with the airline before you drive.

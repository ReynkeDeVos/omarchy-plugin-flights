# Flights

I wanted to track my partner's flights, so we can stay in touch better during her long trips (like Germany to Japan). This Omarchy bar widget follows one dated trip of any number of flights and tells me when to leave for the airport.

<a href="https://reynkedevos.github.io/omarchy-plugin-flights/"><img src="docs/assets/demo.avif" alt="The widget follows her flight, its delays and the connection, until she has landed"></a>
<sub><a href="https://reynkedevos.github.io/omarchy-plugin-flights/">Watch with sound</a></sub>

<p>
  <img src="docs/assets/en-route.avif" width="350" alt="En route: countdown to landing, leave-by time, flight progress and the connecting flight">
  <img src="docs/assets/arrived.avif" width="350" alt="Arrived: terminal, gate and baggage belt">
</p>

<details>
<summary>More screenshots</summary>

<p>
  <img src="docs/assets/boarding.avif" width="350" alt="Boarding soon: gate and terminal">
  <img src="docs/assets/departing.avif" width="350" alt="Takeoff in a few minutes">
  <img src="docs/assets/delayed.avif" width="350" alt="Delayed in the air: the plane turns red">
  <img src="docs/assets/landing.avif" width="350" alt="On approach: landing in under an hour">
  <img src="docs/assets/connection-risk.avif" width="350" alt="Connection in danger: the delay leaves less than an hour to change planes">
  <img src="docs/assets/transfer.avif" width="350" alt="Transfer: countdown to the connecting flight">
  <img src="docs/assets/disrupted.avif" width="350" alt="A cancelled flight">
</p>
</details>

<sub>Real Lufthansa flights on 6 October 2026.</sub>

## Install

```bash
omarchy plugin add https://github.com/ReynkeDeVos/omarchy-plugin-flights.git
omarchy plugin enable reynkedevos.flights --section center
```

Then click the suitcase in the bar, add the flights, and say how long it takes you to get to the airport. The pen in the panel (or `E`) edits the trip later.

The trip lives in the widget's entry in `~/.config/omarchy/shell.json`, so you can also write it by hand:

```json
{ "id": "reynkedevos.flights", "legs": "LH400@2026-12-23,LH2054@2026-12-24", "leaveLeadMinutes": 60 }
```

Optional: `homeTimezone` (the system's), `refreshSeconds` (60), `notifications` (`true`).

Left-click opens the panel, middle-click the live map, right-click refreshes. In the panel, `E` edits the trip, `M` opens the map, `R` refreshes and `Esc` closes; the setup works from the keyboard alone. A delay of 15 minutes or more turns the icon red; the warning triangle is only for cancellations, diversions and connections under an hour. Notifications cover takeoff, delays, gate changes, landing in 60/30/15 minutes, the transfer, when to leave, and arrival.

Data comes from FlightStats and adsb.lol/adsbdb without API keys. It can be late or wrong, so check with the airline before you drive.

## Building the backend

The widget is QML. The flight data comes from `bin/flight-status`, a small Rust program that prints one JSON report and exits. All monitors share the plugin's service (`Service.qml`), which runs it once per refresh and sends each notification once. Bars other than Omarchy's own keep services from their widgets; there every widget refreshes on its own, and the program's lock still keeps notifications single. `omarchy plugin add` and `omarchy plugin update` only fetch files and build nothing, so the built program (x86_64 Linux) is committed next to its source. After changing `src/`, rebuild it and commit both:

```bash
cargo build --release --locked
install -m 755 target/release/flight-status bin/flight-status
```

On another architecture, build it the same way and copy it into `~/.config/omarchy/plugins/reynkedevos.flights/bin/`. Before `omarchy plugin update`, put the shipped one back with `git -C ~/.config/omarchy/plugins/reynkedevos.flights checkout bin/flight-status`, then build and copy again.

Checks: `cargo fmt --check`, `cargo clippy --all-targets --locked -- -D warnings` and `cargo test --locked`. `python3 -B -m unittest discover -s tests` follows whole trips offline through both `bin/flight-status` and the last Python backend in `tests/reference` and expects the same output, files and requests.

"""End-to-end checks of the backend program itself.

Every run gets a fixed clock, a local stand-in for FlightStats, adsb.lol and
adsbdb, and throwaway XDG directories, so nothing touches the real trip.
"""

import datetime as dt
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON_BACKEND = ROOT / "bin"
HOME = "Europe/Berlin"

# Runs the Python backend with the test clock and every request sent to the stand-in.
BOOTSTRAP = r"""
import datetime as dt, os, sys, urllib.parse, urllib.request
sys.path.insert(0, sys.argv.pop(1))
import flight_status
now = dt.datetime.fromtimestamp(int(os.environ["FLIGHTS_TEST_NOW_MS"]) / 1000, dt.timezone.utc)
flight_status.utc_now = lambda: now
real_urlopen = urllib.request.urlopen
def urlopen(request, *args, **kwargs):
    parts = urllib.parse.urlsplit(request.full_url)
    request.full_url = os.environ["FLIGHTS_TEST_ORIGIN"] + urllib.parse.urlunsplit(("", "", parts.path, parts.query, ""))
    return real_urlopen(request, *args, **kwargs)
urllib.request.urlopen = urlopen
sys.exit(flight_status.main(sys.argv[1:]))
"""


def backends() -> dict[str, list[str]]:
    return {"python": [sys.executable, "-I", "-B", "-c", BOOTSTRAP, str(PYTHON_BACKEND)]}


def at(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text).replace(tzinfo=dt.UTC)


def ms(moment: dt.datetime) -> int:
    return int(moment.timestamp() * 1000)


def iso(moment: dt.datetime | None) -> str | None:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z") if moment else None


AIRPORTS = {
    "FRA": ("Frankfurt", 50.0333, 8.5706),
    "MUC": ("Munich", 48.3538, 11.7861),
    "DXB": ("Dubai", 25.2528, 55.3644),
    "BKK": ("Bangkok", 13.69, 100.7501),
    "HND": ("Tokyo", 35.5523, 139.7797),
}


def airport(code: str, scheduled: dt.datetime, estimated: dt.datetime | None, **extra) -> dict:
    return {
        "iata": code,
        "city": AIRPORTS[code][0],
        "times": {
            "scheduled": {"time24": scheduled.strftime("%H:%M")},
            "estimatedActual": {"time24": estimated.strftime("%H:%M")} if estimated else {},
        },
        **extra,
    }


def flight(
    origin: str,
    destination: str,
    departure: str,
    arrival: str,
    *,
    est_departure: str | None = None,
    est_arrival: str | None = None,
    departed: bool = False,
    landed: bool = False,
    cancelled: bool = False,
    departure_delay: int = 0,
    arrival_delay: int = 0,
    gate: str | None = None,
    terminal: str | None = "1",
    baggage: str | None = None,
    callsign: str | None = None,
    altitude: int | None = None,
) -> dict:
    """A FlightStats flight record with the fields the backend reads."""
    start, end = at(departure), at(arrival)
    start_est, end_est = at(est_departure) if est_departure else None, at(est_arrival) if est_arrival else None
    track = {"callsign": callsign, "positions": []}
    if altitude is not None:
        track["positions"] = [
            {"date": iso(start + dt.timedelta(minutes=5)), "altitudeFt": 1000},
            {"date": iso(start + dt.timedelta(minutes=30)), "altitudeFt": altitude},
        ]
    return {
        "flightNote": {"hasDepartedGate": departed, "landed": landed, "canceled": cancelled},
        "status": {
            "status": "Cancelled" if cancelled else "Landed" if landed else "Departed" if departed else "Scheduled",
            "statusDescription": "On time" if not (departure_delay or arrival_delay) else "Delayed",
            "delay": {"departure": {"minutes": departure_delay}, "arrival": {"minutes": arrival_delay}},
        },
        "schedule": {
            "scheduledDepartureUTC": iso(start),
            "scheduledArrivalUTC": iso(end),
            "estimatedActualDepartureUTC": iso(start_est),
            "estimatedActualArrivalUTC": iso(end_est),
        },
        "departureAirport": airport(origin, start, start_est, terminal=terminal, gate=gate),
        "arrivalAirport": airport(destination, end, end_est, terminal="3", gate=None, baggage=baggage),
        "ticketHeader": {"carrier": {"name": "Test Air"}},
        "positional": {"flexTrack": track},
    }


def page(record: dict | None, *, tail: str = ";__NEXT_LOADED_PAGES__=[];</script></body></html>") -> bytes:
    payload = {"props": {"initialState": {"flightTracker": {"flight": record}}}}
    return ('<html><body><script>__NEXT_DATA__ = ' + json.dumps(payload) + tail).encode()


def flightstats_path(key: str) -> str:
    code, date = key.split("@")
    year, month, day = (int(part) for part in date.split("-"))
    return f"/v2/flight-tracker/{code[:2]}/{code[2:]}?year={year}&month={month}&date={day}"


class Feeds:
    """The three public feeds, answered from a dict of path -> (status, body)."""

    def __init__(self):
        self.routes: dict[str, tuple[int, bytes]] = {}
        self.requests: list[str] = []
        feeds = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                feeds.requests.append(self.path)
                status, body = feeds.routes.get(self.path, (404, b'{"error":"not found"}'))
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.origin = f"http://127.0.0.1:{self.server.server_address[1]}"

    def flight(self, key: str, record: dict | None, status: int = 200):
        self.routes[flightstats_path(key)] = (status, page(record))

    def aircraft(self, callsign: str, **fields):
        aircraft = [{"flight": callsign + "  ", **fields}] if fields else []
        self.routes[f"/v2/callsign/{callsign}"] = (200, json.dumps({"ac": aircraft}).encode())

    def route(self, code: str, callsign: str | None, origin: str = "", destination: str = ""):
        def place(name: str) -> dict:
            return {"iata_code": name, "latitude": AIRPORTS[name][1], "longitude": AIRPORTS[name][2]}

        response = (
            {"flightroute": {"callsign_icao": callsign, "origin": place(origin), "destination": place(destination)}}
            if callsign
            else "unknown callsign"
        )
        self.routes[f"/v0/callsign/{code}"] = (200, json.dumps({"response": response}).encode())

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def read_tree(root: Path) -> dict[str, object]:
    """Every file the backend left behind, parsed; the lock file only has to exist."""
    files: dict[str, object] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            name = str(path.relative_to(root))
            files[name] = None if path.name == "state.lock" else json.loads(path.read_text(encoding="utf-8"))
    return files


def typed(value):
    """JSON with its types spelled out, so 1, 1.0, true and null never compare equal."""
    if isinstance(value, dict):
        return {key: typed(item) for key, item in value.items()}
    if isinstance(value, list):
        return [typed(item) for item in value]
    return (type(value).__name__, value)


class Run:
    def __init__(self, completed: subprocess.CompletedProcess, root: Path, requests: list[str]):
        self.code = completed.returncode
        self.stderr = completed.stderr
        self.json = json.loads(completed.stdout) if completed.stdout.strip() else None
        self.files = read_tree(root)
        self.requests = Counter(requests)

    @property
    def event_keys(self) -> list[str]:
        return [item["key"] for item in self.json["events"]]


class OfflineBackendTest(unittest.TestCase):
    """Runs each step on every backend in its own XDG tree and expects identical results."""

    def setUp(self):
        self.feeds = Feeds()
        self.addCleanup(self.feeds.close)
        self.roots: dict[str, Path] = {}
        for name in backends():
            folder = tempfile.TemporaryDirectory()
            self.addCleanup(folder.cleanup)
            self.roots[name] = Path(folder.name)

    def environment(self, name: str, now: dt.datetime, **extra: str) -> dict[str, str]:
        root = self.roots[name]
        return {
            "PATH": os.environ.get("PATH", "/usr/bin"),
            "HOME": str(root / "home"),
            "XDG_STATE_HOME": str(root / "state"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "TZ": "UTC",
            "FLIGHTS_TEST_NOW_MS": str(ms(now)),
            "FLIGHTS_TEST_ORIGIN": self.feeds.origin,
            **extra,
        }

    def run_backends(self, *args: str, now: dt.datetime, env: dict[str, str] | None = None) -> Run:
        runs = {}
        for name, command in backends().items():
            self.feeds.requests = []
            completed = subprocess.run(
                [*command, *args],
                env=self.environment(name, now, **(env or {})),
                capture_output=True,
                text=True,
                timeout=60,
            )
            runs[name] = Run(completed, self.roots[name], self.feeds.requests)
        first_name, first = next(iter(runs.items()))
        for name, other in runs.items():
            context = f"{name} differs from {first_name} for {args} at {now}"
            self.assertEqual(other.code, first.code, context)
            self.assertEqual(typed(other.json), typed(first.json), context)
            self.assertEqual(typed(other.files), typed(first.files), context)
            self.assertEqual(other.requests, first.requests, context)
        return first

    def trip(self, legs: str, now: dt.datetime, *extra: str) -> Run:
        run = self.run_backends("--legs", legs, "--home-timezone", HOME, "--leave-lead-minutes", "60", *extra, now=now)
        self.assertEqual(run.code, 0, run.stderr)
        return run


A, B, C, D = "LH1001@2026-12-23", "LH2002@2026-12-23", "EK3003@2026-12-23", "TG4004@2026-12-24"
FOUR_LEGS = ",".join([A, B, C, D])


class FourLegJourneyTests(OfflineBackendTest):
    """Frankfurt → Munich → Dubai → Bangkok → Tokyo, followed from home to pickup."""

    def schedule(self):
        self.feeds.flight(A, flight("FRA", "MUC", "2026-12-23T08:00", "2026-12-23T09:00"))
        self.feeds.flight(B, flight("MUC", "DXB", "2026-12-23T10:30", "2026-12-23T16:30"))
        self.feeds.flight(C, flight("DXB", "BKK", "2026-12-23T18:00", "2026-12-24T00:00"))
        self.feeds.flight(D, flight("BKK", "HND", "2026-12-24T02:00", "2026-12-24T08:00", terminal="1"))
        self.feeds.route("LH1001", "DLH1001", "FRA", "MUC")
        self.feeds.route("LH2002", None)  # adsbdb does not know it; the live callsign still works
        self.feeds.route("EK3003", "UAE3003", "DXB", "BKK")
        self.feeds.route("TG4004", "THA4004", "BKK", "HND")
        for callsign in ("DLH1001", "DLH2002", "UAE3003", "THA4004"):
            self.feeds.aircraft(callsign)

    def test_follows_every_flight_and_transfer_to_the_pickup(self):
        self.schedule()
        start = self.trip(FOUR_LEGS, at("2026-12-23T06:00"))
        journey, pickup = start.json["journey"], start.json["pickup"]
        self.assertEqual(
            (journey["stage"], journey["activeLegIndex"], journey["label"], journey["connectionMinutes"]),
            ("starting", 0, "Departing Frankfurt", 90),
        )
        self.assertEqual((journey["nextEventKind"], journey["nextEventMinutes"]), ("departure", 120))
        self.assertEqual(
            (pickup["landingHomeTime"], pickup["leaveHomeTime"], pickup["leaveMinutes"], pickup["airport"]),
            ("09:00", "Thu 08:00", 1500, "HND"),
        )
        self.assertEqual(start.event_keys, [])
        self.assertEqual(len(start.json["legs"]), 4)

        # The gate shows up 15 minutes before departure.
        self.feeds.flight(A, flight("FRA", "MUC", "2026-12-23T08:00", "2026-12-23T09:00", gate="A12"))
        gate = self.trip(FOUR_LEGS, at("2026-12-23T07:45"))
        self.assertEqual(gate.json["legs"][0]["phase"], "departing-soon")
        self.assertEqual(gate.event_keys, [f"{A}:gate:A12"])
        self.assertEqual(gate.json["events"][0]["title"], "LH1001 gate is A12")

        # Airborne with a live ADS-B position.
        self.feeds.flight(A, flight(
            "FRA", "MUC", "2026-12-23T08:00", "2026-12-23T09:00", est_departure="2026-12-23T08:10",
            est_arrival="2026-12-23T09:05", departed=True, arrival_delay=5, gate="A12", callsign="DLH1001",
            altitude=24000,
        ))
        self.feeds.aircraft("DLH1001", hex="3c6444", lat=49.2, lon=10.1, alt_baro=24000, baro_rate=-64)
        airborne = self.trip(FOUR_LEGS, at("2026-12-23T08:20"))
        first = airborne.json["legs"][0]
        self.assertEqual((first["phase"], first["aircraft"]["hex"]), ("arriving", "3c6444"))
        self.assertEqual(airborne.event_keys, [f"{A}:departed", f"{A}:arrival:60"])
        self.assertEqual(airborne.json["events"][0]["body"], "Now heading to Munich · 10:05 at home")
        self.assertEqual(airborne.json["journey"]["stage"], "en-route")

        # First transfer.
        self.feeds.flight(A, flight(
            "FRA", "MUC", "2026-12-23T08:00", "2026-12-23T09:00", est_departure="2026-12-23T08:10",
            est_arrival="2026-12-23T09:04", departed=True, landed=True, callsign="DLH1001",
        ))
        munich = self.trip(FOUR_LEGS, at("2026-12-23T09:10"))
        self.assertEqual(
            (munich.json["journey"]["stage"], munich.json["journey"]["activeLegIndex"], munich.json["journey"]["label"]),
            ("connection", 1, "Transfer in Munich"),
        )
        self.assertEqual(munich.event_keys, [f"{A}:landed", "journey:connection:1"])
        self.assertEqual(munich.json["events"][1]["body"], "Transfer: next flight to Dubai is now active")

        # Second leg in the air; adsbdb has no route, so the live callsign alone finds it.
        self.feeds.flight(B, flight(
            "MUC", "DXB", "2026-12-23T10:30", "2026-12-23T16:30", est_departure="2026-12-23T10:40",
            est_arrival="2026-12-23T16:40", departed=True, callsign="DLH2002", altitude=37000,
        ))
        self.feeds.aircraft("DLH2002", hex="3c4b2a", lat=40.1, lon=30.2, alt_baro=37000, baro_rate=0)
        second = self.trip(FOUR_LEGS, at("2026-12-23T11:00"))
        self.assertEqual(second.event_keys, [f"{B}:departed"])
        self.assertEqual(second.json["legs"][1]["phase"], "airborne")

        # Late into Dubai: the delay and the tight transfer are announced once.
        self.feeds.flight(B, flight(
            "MUC", "DXB", "2026-12-23T10:30", "2026-12-23T16:30", est_departure="2026-12-23T10:40",
            est_arrival="2026-12-23T17:20", departed=True, arrival_delay=50, callsign="DLH2002", altitude=9000,
        ))
        late = self.trip(FOUR_LEGS, at("2026-12-23T16:45"))
        self.assertEqual(
            late.event_keys, [f"{B}:arrival:60", f"{B}:delay:1", "journey:tight-connection:2"]
        )
        self.assertEqual(late.json["journey"]["connectionMinutes"], 40)
        self.assertTrue(late.json["legs"][1]["late"])
        self.assertEqual(self.trip(FOUR_LEGS, at("2026-12-23T16:46")).event_keys, [])

        # Second transfer.
        self.feeds.flight(B, flight(
            "MUC", "DXB", "2026-12-23T10:30", "2026-12-23T16:30", est_departure="2026-12-23T10:40",
            est_arrival="2026-12-23T17:20", departed=True, landed=True, arrival_delay=50,
        ))
        dubai = self.trip(FOUR_LEGS, at("2026-12-23T17:25"))
        self.assertEqual(dubai.event_keys, [f"{B}:landed", "journey:connection:2"])
        self.assertEqual(dubai.json["events"][1]["title"], "Landed in Dubai")

        self.feeds.flight(C, flight(
            "DXB", "BKK", "2026-12-23T18:00", "2026-12-24T00:00", est_departure="2026-12-23T18:05",
            est_arrival="2026-12-24T00:00", departed=True, callsign="UAE3003",
        ))
        self.assertEqual(self.trip(FOUR_LEGS, at("2026-12-23T18:30")).event_keys, [f"{C}:departed"])

        # Third transfer, past midnight.
        self.feeds.flight(C, flight(
            "DXB", "BKK", "2026-12-23T18:00", "2026-12-24T00:00", est_departure="2026-12-23T18:05",
            est_arrival="2026-12-24T00:05", departed=True, landed=True,
        ))
        bangkok = self.trip(FOUR_LEGS, at("2026-12-24T00:10"))
        self.assertEqual(bangkok.event_keys, [f"{C}:landed", "journey:connection:3"])
        self.assertEqual(bangkok.json["events"][1]["body"], "Transfer: next flight to Tokyo is now active")
        self.assertEqual(bangkok.json["journey"]["connectionMinutes"], 115)

        # The last landing slips by ten minutes, so the leave time moves with it.
        self.feeds.flight(D, flight(
            "BKK", "HND", "2026-12-24T02:00", "2026-12-24T08:00", est_departure="2026-12-24T02:10",
            est_arrival="2026-12-24T08:10", departed=True, arrival_delay=10, callsign="THA4004", altitude=39000,
        ))
        final = self.trip(FOUR_LEGS, at("2026-12-24T02:30"))
        self.assertEqual(final.event_keys, [f"{D}:departed", "pickup:moved:" + str(ms(at("2026-12-24T07:10")) // 60_000)])
        self.assertEqual(final.json["events"][1]["title"], "Leave later: by 08:10")

        self.assertEqual(self.trip(FOUR_LEGS, at("2026-12-24T06:56")).event_keys, ["pickup:15"])
        now = self.trip(FOUR_LEGS, at("2026-12-24T07:12"))
        self.assertEqual(now.event_keys, [f"{D}:arrival:60", "pickup:now"])
        self.assertEqual(now.json["events"][1]["body"], "Landing 09:10 at HND · T3")

        self.feeds.flight(D, flight(
            "BKK", "HND", "2026-12-24T02:00", "2026-12-24T08:00", est_departure="2026-12-24T02:10",
            est_arrival="2026-12-24T08:07", departed=True, landed=True, baggage="7",
        ))
        home = self.trip(FOUR_LEGS, at("2026-12-24T08:15"))
        self.assertEqual(home.event_keys, [f"{D}:landed", "journey:complete"])
        self.assertEqual(home.json["events"][1]["body"], "Arrived in Tokyo · belt 7")
        self.assertTrue(home.json["pickup"]["done"])
        state = home.files["state/omarchy-flights/state.json"]
        self.assertEqual(state["trip"], FOUR_LEGS)
        self.assertEqual(state["fired"], sorted(state["fired"]))

    def test_an_edited_trip_starts_with_fresh_notifications(self):
        self.schedule()
        self.feeds.flight(A, flight(
            "FRA", "MUC", "2026-12-23T08:00", "2026-12-23T09:00", departed=True, landed=True,
        ))
        self.trip(FOUR_LEGS, at("2026-12-23T08:00"))
        self.assertEqual(self.trip(FOUR_LEGS, at("2026-12-23T09:10")).event_keys, [])  # already landed when first seen

        # Dubai is dropped and a different onward flight added: the old trip's keys must not carry over.
        edited = ",".join([A, B, "LH5005@2026-12-23"])
        self.feeds.flight("LH5005@2026-12-23", flight("DXB", "HND", "2026-12-23T19:00", "2026-12-24T05:00"))
        first = self.trip(edited, at("2026-12-23T09:11"))
        self.assertEqual(first.event_keys, [])
        state = first.files["state/omarchy-flights/state.json"]
        self.assertEqual((state["trip"], state["fired"]), (edited, []))
        self.assertEqual(first.json["journey"]["destination"], "Tokyo")
        self.assertEqual(sorted(state["snapshot"]["legs"]), sorted([A, B, "LH5005@2026-12-23"]))

    def test_concurrent_widgets_announce_each_event_once(self):
        self.schedule()
        self.trip(FOUR_LEGS, at("2026-12-23T07:00"))
        self.feeds.flight(A, flight(
            "FRA", "MUC", "2026-12-23T08:00", "2026-12-23T09:00", departed=True, landed=True,
        ))
        for name, command in backends().items():
            with self.subTest(backend=name):
                env = self.environment(name, at("2026-12-23T09:10"))
                processes = [
                    subprocess.Popen(
                        [*command, "--legs", FOUR_LEGS, "--home-timezone", HOME],
                        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    )
                    for _ in range(3)
                ]
                keys = []
                for process in processes:
                    out, err = process.communicate(timeout=60)
                    self.assertEqual(process.returncode, 0, err)
                    keys += [item["key"] for item in json.loads(out)["events"]]
                self.assertEqual(sorted(keys), sorted([f"{A}:landed", "journey:connection:1"]))


class LookupTests(OfflineBackendTest):
    def test_lookup_prints_one_leg_and_keeps_the_trip_state(self):
        self.feeds.flight(B, flight("MUC", "DXB", "2026-12-23T10:30", "2026-12-23T16:30", gate="G7"))
        found = self.run_backends("--lookup", " lh 2002@2026-12-23, LH1@2026-12-24", "--home-timezone", HOME,
                                  now=at("2026-12-20T12:00"))
        self.assertEqual(found.code, 0)
        self.assertEqual((found.json["key"], found.json["route"], found.json["phase"]), (B, "MUC → DXB", "scheduled"))
        self.assertEqual(found.json["departure"]["homeTime"], "11:30")
        self.assertEqual(list(found.files), ["cache/omarchy-flights/journey-LH2002-2026-12-23.json"])

    def test_lookup_explains_what_it_cannot_find(self):
        unreadable = self.run_backends("--lookup", "LH2002@2026-13-01", now=at("2026-12-20T12:00"))
        self.assertEqual(
            (unreadable.code, unreadable.json),
            (0, {"phase": "unknown", "error": "Use a flight number and date like LH400@2026-12-23"}),
        )
        self.assertEqual(unreadable.requests, Counter())

        missing = self.run_backends("--lookup", "XY999@2026-12-23", now=at("2026-12-20T12:00"))
        self.assertEqual((missing.code, missing.json["phase"], missing.json["route"]), (0, "unknown", "—"))
        self.assertEqual(missing.json["error"], "HTTP Error 404: Not Found")
        self.assertEqual(missing.files, {})


class FeedFailureTests(OfflineBackendTest):
    def test_falls_back_to_the_last_schedule_with_live_data(self):
        self.feeds.flight(A, flight(
            "FRA", "MUC", "2026-12-23T08:00", "2026-12-23T09:00", est_departure="2026-12-23T08:10",
            est_arrival="2026-12-23T09:05", departed=True, callsign="DLH1001",
        ))
        self.feeds.route("LH1001", "DLH1001", "FRA", "MUC")
        self.feeds.aircraft("DLH1001", hex="3c6444", lat=49.2, lon=10.1, alt_baro=24000, baro_rate=0)
        self.trip(A, at("2026-12-23T08:20"))

        self.feeds.routes[flightstats_path(A)] = (503, b"busy")
        self.feeds.aircraft("DLH1001", hex="3c6444", lat=48.6, lon=11.4, alt_baro=4000, baro_rate=-900)
        cached = self.trip(A, at("2026-12-23T08:50"))
        leg = cached.json["legs"][0]
        self.assertEqual(leg["error"], "HTTP Error 503: Service Unavailable")
        self.assertEqual(leg["description"], "Showing the last schedule with live aircraft data when available")
        self.assertEqual((leg["phase"], leg["progress"]["remainingMinutes"]), ("final-approach", 15))
        self.assertEqual(cached.json["errors"], [])
        # Route and pin come from the cache now: one adsb.lol call, no adsbdb call.
        self.assertEqual(sorted(cached.requests), ["/v2/callsign/DLH1001", flightstats_path(A)])

    def test_a_flight_never_seen_shows_as_unknown(self):
        self.feeds.flight(A, None)
        self.feeds.routes[flightstats_path(B)] = (200, b"<html>no data</html>")
        self.feeds.routes[flightstats_path(C)] = (200, page(None, tail="")[:-1])
        run = self.trip(",".join([A, B, C]), at("2026-12-23T06:00"))
        self.assertEqual(
            run.json["errors"],
            [
                "LH1001: the dated flight record was empty",
                "LH2002: the dated flight record was not present",
                f"EK3003: {run.json['legs'][2]['error']}",
            ],
        )
        self.assertEqual([leg["phase"] for leg in run.json["legs"]], ["unknown"] * 3)
        self.assertEqual(run.json["journey"]["stage"], "starting")
        self.assertIsNone(run.json["pickup"]["landingEpochMs"])

    def test_reads_the_record_when_more_script_follows_it(self):
        self.feeds.routes[flightstats_path(A)] = (
            200,
            page(flight("FRA", "MUC", "2026-12-23T08:00", "2026-12-23T09:00"), tail='; window.x = {"a": 1};</script>'),
        )
        self.assertEqual(self.trip(A, at("2026-12-23T06:00")).json["legs"][0]["route"], "FRA → MUC")


class CommandLineTests(OfflineBackendTest):
    def test_rejects_missing_conflicting_and_empty_trips(self):
        for args in ([], ["--legs", A, "--lookup", A], ["--legs", "nonsense"], ["--legs", A, "--leave-lead-minutes", "x"]):
            with self.subTest(args=args):
                run = self.run_backends(*args, now=at("2026-12-23T06:00"))
                self.assertEqual((run.code, run.json, run.files), (2, None, {}))

    def test_home_clock_defaults_to_the_system_zone(self):
        self.feeds.flight(A, flight("FRA", "MUC", "2026-12-23T08:00", "2026-12-23T09:00"))
        for zone, expected in (("", "America/New_York"), ("Mars/Base", "America/New_York"), ("Asia/Tokyo", "Asia/Tokyo")):
            with self.subTest(zone=zone):
                run = self.run_backends("--legs", A, "--home-timezone", zone, now=at("2026-12-23T06:00"),
                                        env={"TZ": "America/New_York"})
                self.assertEqual(run.json["homeTimezone"], expected)


if __name__ == "__main__":
    unittest.main()

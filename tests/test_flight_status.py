from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import flight_status  # noqa: E402


def leg(key: str, phase: str, remaining: int | None, *, landed: bool = False, departed: bool = True):
    return {
        "key": key,
        "code": key.split("@")[0],
        "phase": phase,
        "departed": departed,
        "landed": landed,
        "cancelled": False,
        "description": "On time",
        "delayMinutes": 0,
        "late": False,
        "departure": {"code": "FRA", "city": "Frankfurt", "gate": None, "terminal": "1", "epochMs": 1000, "homeTime": "16:00"},
        "arrival": {"code": "JFK", "city": "New York", "gate": None, "terminal": "3", "epochMs": 2000, "homeTime": "22:00"},
        "progress": {"remainingMinutes": remaining, "etaEpochMs": 2000, "percent": 50},
    }


def chain(*phases: str):
    """One leg per phase along Boston → Chicago → Denver → Seattle, each transfer 60 minutes."""
    cities = ["Boston", "Chicago", "Denver", "Seattle"]
    legs = []
    for index, phase in enumerate(phases):
        item = leg(f"AA{index + 1}00@2026-07-15", phase, None, landed=phase == "landed", departed=phase not in {"scheduled", "cancelled"})
        item["cancelled"] = phase == "cancelled"
        item["departure"]["city"], item["arrival"]["city"] = cities[index], cities[index + 1]
        item["departure"]["epochMs"], item["arrival"]["epochMs"] = 2 * index * 3_600_000, (2 * index + 1) * 3_600_000
        legs.append(item)
    return legs


class FlightStatusTests(unittest.TestCase):
    def test_three_leg_trip_reaches_the_second_transfer(self):
        journey = flight_status.journey_from_legs(chain("landed", "landed", "scheduled"))
        self.assertEqual(
            (journey["stage"], journey["activeLegIndex"], journey["label"], journey["connectionMinutes"], journey["destination"]),
            ("connection", 2, "Transfer in Denver", 60, "Seattle"),
        )

    def test_middle_leg_in_the_air_looks_ahead_to_the_next_transfer(self):
        journey = flight_status.journey_from_legs(chain("landed", "airborne", "scheduled"))
        self.assertEqual(
            (journey["stage"], journey["activeLegIndex"], journey["label"], journey["via"], journey["connectionLegIndex"]),
            ("en-route", 1, "En route to Denver", "Denver", 2),
        )

    def test_a_cancelled_later_leg_disrupts_the_journey(self):
        journey = flight_status.journey_from_legs(chain("airborne", "scheduled", "cancelled"))
        self.assertEqual((journey["stage"], journey["activeLegIndex"], journey["connectionMinutes"]), ("disrupted", 2, None))

    def test_each_transfer_notifies_once(self):
        legs = chain("landed", "landed", "scheduled")
        legs[2]["departure"]["epochMs"] -= 30 * 60_000  # 30 minutes to change planes
        report = {"generatedAtMs": 0, "legs": legs, "journey": flight_status.journey_from_legs(legs)}
        previous = {"stage": "en-route", "legs": {}}
        # The first transfer was tight too; the second one still gets its own warning.
        events, _ = flight_status.notification_events(previous, report, {"journey:connection:1", "journey:tight-connection:1"})
        self.assertEqual([item["key"] for item in events], ["journey:connection:2", "journey:tight-connection:2"])
        self.assertEqual(events[0]["title"], "Landed in Denver")
        self.assertEqual(events[0]["body"], "Transfer: next flight to Seattle is now active")


    def test_departure_icon_does_not_start_110_minutes_early(self):
        now = dt.datetime(2026, 7, 15, 20, 50, tzinfo=dt.UTC)
        phase = flight_status.phase_for(
            cancelled=False,
            landed=False,
            departed=False,
            start=now + dt.timedelta(minutes=110),
            end=None,
            now=now,
        )
        self.assertEqual(phase, "scheduled")

    def test_predeparture_states_only_describe_the_near_future(self):
        now = dt.datetime(2026, 7, 15, 20, 0, tzinfo=dt.UTC)
        expected = {
            61: "scheduled",
            60: "boarding-soon",
            31: "boarding-soon",
            30: "departing-soon",
            1: "departing-soon",
            0: "awaiting-departure",
        }
        for minutes, wanted in expected.items():
            with self.subTest(minutes=minutes):
                phase = flight_status.phase_for(
                    cancelled=False,
                    landed=False,
                    departed=False,
                    start=now + dt.timedelta(minutes=minutes),
                    end=None,
                    now=now,
                )
                self.assertEqual(phase, wanted)

    def test_parses_dated_legs_and_discards_duplicates(self):
        result = flight_status.parse_leg_specs(" lh 400@2026-07-15, LH401@2026-07-16,LH400@2026-07-15,LH402@2026-02-30 ")
        self.assertEqual([item["key"] for item in result], ["LH400@2026-07-15", "LH401@2026-07-16"])

    def test_lookup_prints_one_leg_and_leaves_the_trip_state_alone(self):
        flight = {"departureAirport": {"iata": "FRA", "city": "Frankfurt"}, "arrivalAirport": {"iata": "JFK", "city": "New York"}}
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(flight_status, "STATE_DIR", Path(folder) / "state"), \
                mock.patch.object(flight_status.adsb, "CACHE_DIR", folder), mock.patch.object(flight_status, "fetch_page", return_value=flight):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                flight_status.main(["--lookup", "lh 400@2026-12-23"])
            self.assertFalse((Path(folder) / "state").exists())
        found = json.loads(output.getvalue())
        self.assertEqual((found["key"], found["route"]), ("LH400@2026-12-23", "FRA → JFK"))

    def test_lookup_explains_input_it_cannot_read(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            flight_status.main(["--lookup", "LH400@2026-02-30"])
        self.assertEqual(json.loads(output.getvalue())["phase"], "unknown")

    def test_labels_and_transfer_come_from_leg_cities(self):
        first = leg("AA100@2026-07-15", "landed", 0, landed=True)
        second = leg("AA200@2026-07-16", "scheduled", None, departed=False)
        first["departure"]["city"], first["arrival"]["city"] = "Boston", "Chicago"
        second["arrival"]["city"] = "Denver"
        first["arrival"]["epochMs"], second["departure"]["epochMs"] = 60_000, 46 * 60_000
        journey = flight_status.journey_from_legs([first, second])
        self.assertEqual(journey["label"], "Transfer in Chicago")
        self.assertEqual((journey["destination"], journey["connectionMinutes"], journey["tightConnection"]), ("Denver", 45, True))

    def test_leave_reminders_fire_once_and_rearm_when_landing_moves(self):
        zone = flight_status.home_zone("Europe/Berlin")
        landing = dt.datetime(2026, 7, 15, 10, 45, tzinfo=dt.UTC)  # 12:45 CEST
        final = leg("LH400@2026-07-15", "airborne", 100)
        final["arrival"]["epochMs"] = flight_status.epoch_ms(landing)
        final["arrival"]["homeTime"] = "12:45"

        def report_at(now):
            return {"generatedAtMs": 0, "legs": [], "journey": {}, "pickup": flight_status.pickup_for([final], 60, now, zone)}

        before = report_at(landing - dt.timedelta(minutes=80))
        self.assertEqual(before["pickup"]["leaveHomeTime"], "11:45")
        soon = report_at(landing - dt.timedelta(minutes=74))
        events, fired = flight_status.notification_events(flight_status.snapshot(before), soon)
        self.assertEqual([item["key"] for item in events], ["pickup:15"])
        self.assertEqual(flight_status.notification_events(flight_status.snapshot(before), soon, fired)[0], [])

        now_report = report_at(landing - dt.timedelta(minutes=60))
        events, fired = flight_status.notification_events(flight_status.snapshot(soon), now_report, fired)
        self.assertEqual([item["key"] for item in events], ["pickup:now"])

        final["arrival"]["epochMs"] += 40 * 60_000  # 40 min delay
        delayed = report_at(landing - dt.timedelta(minutes=59))
        events, fired = flight_status.notification_events(flight_status.snapshot(now_report), delayed, fired)
        self.assertEqual(events[0]["title"], "Leave later: by 12:25")
        self.assertNotIn("pickup:15", fired)

    def test_switches_to_connection_after_first_landing(self):
        first = leg("LH400@2026-07-15", "landed", 0, landed=True)
        second = leg("LH401@2026-07-16", "scheduled", None, departed=False)
        journey = flight_status.journey_from_legs([first, second])
        self.assertEqual(journey["stage"], "connection")
        self.assertEqual(journey["activeLegIndex"], 1)

    def test_single_landed_leg_completes_the_journey(self):
        journey = flight_status.journey_from_legs([leg("LH400@2026-07-15", "landed", 0, landed=True)])
        self.assertEqual((journey["stage"], journey["label"]), ("complete", "Arrived in New York"))

    def test_cancelled_leg_stops_countdown_and_connection(self):
        first = leg("LH400@2026-07-15", "cancelled", 30)
        first["cancelled"] = True
        journey = flight_status.journey_from_legs([first, leg("LH401@2026-07-16", "scheduled", None, departed=False)])
        self.assertEqual((journey["stage"], journey["nextEventKind"], journey["nextEventMinutes"], journey["connectionMinutes"]), ("disrupted", "cancelled", None, None))

    def test_delay_alone_is_not_an_alert(self):
        spec = {"key": "LH400@2026-07-15", "code": "LH400", "date": "2026-07-15"}
        now, zone = dt.datetime(2026, 7, 15, 12, 0, tzinfo=dt.UTC), flight_status.home_zone("Europe/Berlin")
        delayed = {"status": {"delay": {"arrival": {"minutes": 45}}}}
        self.assertFalse(flight_status.normalize_flightstats(spec, delayed, now, zone)["alert"])
        cancelled = {"flightNote": {"canceled": True}}
        self.assertTrue(flight_status.normalize_flightstats(spec, cancelled, now, zone)["alert"])

    def test_defaults_agree_across_manifest_widget_and_cli(self):
        # Bar widgets don't get the manifest injected, so the widget repeats its defaults.
        root = Path(__file__).resolve().parents[1]
        widget = json.loads((root / "manifest.json").read_text(encoding="utf-8"))["barWidget"]
        qml = (root / "FlightTracker.qml").read_text(encoding="utf-8")
        for item in widget["schema"]:
            key, value = item["key"], widget["defaults"][item["key"]]
            self.assertEqual(item["defaultValue"], value, key)
            self.assertEqual(json.loads(re.search(rf'setting\("{key}", ([^)]+)\)', qml).group(1)), value, key)
        self.assertEqual(flight_status.DEFAULT_LEAVE_LEAD_MINUTES, widget["defaults"]["leaveLeadMinutes"])

    def test_home_zone_defaults_to_the_system_zone(self):
        with mock.patch.dict(os.environ, {"TZ": "America/New_York"}):
            self.assertEqual(flight_status.home_zone("").key, "America/New_York")
            self.assertEqual(flight_status.home_zone("Mars/Base").key, "America/New_York")
            self.assertEqual(flight_status.home_zone("Asia/Tokyo").key, "Asia/Tokyo")

    def test_late_follows_the_arrival_once_airborne(self):
        spec = {"key": "LH400@2026-07-15", "code": "LH400", "date": "2026-07-15"}
        now, zone = dt.datetime(2026, 7, 15, 12, 0, tzinfo=dt.UTC), flight_status.home_zone("Europe/Berlin")
        delays = {"status": {"delay": {"departure": {"minutes": 40}, "arrival": {"minutes": 10}}}}
        waiting = flight_status.normalize_flightstats(spec, delays, now, zone)
        airborne = flight_status.normalize_flightstats(spec, {**delays, "flightNote": {"hasDepartedRunway": True}}, now, zone)
        self.assertEqual((waiting["delayMinutes"], waiting["late"]), (40, True))
        self.assertEqual((airborne["delayMinutes"], airborne["late"]), (10, False))

    def test_emits_each_landing_threshold_once(self):
        current_leg = leg("LH400@2026-07-15", "arriving", 58)
        report = {"generatedAtMs": 0, "legs": [current_leg], "journey": {"stage": "en-route"}}
        previous = {"stage": "en-route", "legs": {current_leg["key"]: {"phase": "airborne", "remaining": 62, "delay": 0, "gate": None}}}
        events, fired = flight_status.notification_events(previous, report)
        self.assertEqual([item["key"] for item in events], ["LH400@2026-07-15:arrival:60"])
        events_again, _ = flight_status.notification_events(previous, report, fired)
        self.assertEqual(events_again, [])

    def test_landing_emits_leg_and_journey_notifications(self):
        current_leg = leg("LH401@2026-07-16", "landed", 0, landed=True)
        current_leg["arrival"]["city"] = "Frankfurt"
        report = {"generatedAtMs": 0, "legs": [current_leg], "journey": {"stage": "complete"}}
        previous = {"stage": "en-route", "legs": {current_leg["key"]: {"phase": "final-approach", "remaining": 4, "delay": 0, "gate": None}}}
        events, _ = flight_status.notification_events(previous, report)
        self.assertEqual(
            [item["key"] for item in events],
            ["LH401@2026-07-16:landed", "journey:complete"],
        )


if __name__ == "__main__":
    unittest.main()

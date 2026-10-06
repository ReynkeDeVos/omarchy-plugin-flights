from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path

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
        "departureDelayMinutes": 0,
        "arrivalDelayMinutes": 0,
        "departure": {"code": "FRA", "city": "Frankfurt", "gate": None, "terminal": "1", "epochMs": 1000, "homeTime": "16:00"},
        "arrival": {"code": "JFK", "city": "New York", "gate": None, "terminal": "3", "epochMs": 2000, "homeTime": "22:00"},
        "progress": {"remainingMinutes": remaining, "etaEpochMs": 2000, "percent": 50},
    }


class FlightStatusTests(unittest.TestCase):
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
        result = flight_status.parse_leg_specs(" lh400@2026-07-15, LH401@2026-07-16,LH400@2026-07-15 ")
        self.assertEqual([item["key"] for item in result], ["LH400@2026-07-15", "LH401@2026-07-16"])

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

    def test_emits_each_landing_threshold_once(self):
        current_leg = leg("LH400@2026-07-15", "arriving", 58)
        report = {"generatedAtMs": 0, "legs": [current_leg], "journey": {"stage": "first-leg"}}
        previous = {"stage": "first-leg", "legs": {current_leg["key"]: {"phase": "airborne", "remaining": 62, "delay": 0, "gate": None}}}
        events, fired = flight_status.notification_events(previous, report)
        self.assertEqual([item["key"] for item in events], ["LH400@2026-07-15:arrival:60"])
        events_again, _ = flight_status.notification_events(previous, report, fired)
        self.assertEqual(events_again, [])

    def test_landing_emits_leg_and_journey_notifications(self):
        current_leg = leg("LH401@2026-07-16", "landed", 0, landed=True)
        current_leg["arrival"]["city"] = "Frankfurt"
        report = {"generatedAtMs": 0, "legs": [current_leg], "journey": {"stage": "complete"}}
        previous = {"stage": "second-leg", "legs": {current_leg["key"]: {"phase": "final-approach", "remaining": 4, "delay": 0, "gate": None}}}
        events, _ = flight_status.notification_events(previous, report)
        self.assertEqual(
            [item["key"] for item in events],
            ["LH401@2026-07-16:landed", "journey:complete"],
        )


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Track a dated, multi-leg journey for the Omarchy flights plugin."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import fcntl
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import adsb

MARKER = "__NEXT_DATA__ = "
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) omarchy-plugin-flights/1.0"
LEG_RE = re.compile(r"([A-Z0-9]{2,3}\d{1,4})@(\d{4}-\d{2}-\d{2})", re.IGNORECASE)
BOARDING_SOON_MINUTES = 60
DEPARTURE_SOON_MINUTES = 30
DEFAULT_LEAVE_LEAD_MINUTES = 60
TIGHT_CONNECTION_MINUTES = 60
LATE_MINUTES = 15  # airline convention
PICKUP_SHIFT_MINUTES = 10

STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "omarchy-flights"
STATE_PATH = STATE_DIR / "state.json"


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_time(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def epoch_ms(value: dt.datetime | None) -> int | None:
    return round(value.timestamp() * 1000) if value else None


def safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def parse_leg_specs(raw: str) -> list[dict[str, str]]:
    specs: list[dict[str, str]] = []
    for item in str(raw or "").split(","):
        match = LEG_RE.fullmatch(re.sub(r"\s+", "", item))
        if not match:
            continue
        code, date = match.groups()
        try:
            dt.date.fromisoformat(date)  # the pattern alone lets 2026-02-30 through
        except ValueError:
            continue
        key = f"{code.upper()}@{date}"
        if not any(spec["key"] == key for spec in specs):
            specs.append({"key": key, "code": code.upper(), "date": date})
    return specs


def home_zone(name: str) -> ZoneInfo:
    """The configured zone, else the system's (TZ, then /etc/localtime), else UTC."""
    system = os.environ.get("TZ", "").lstrip(":") or os.path.realpath("/etc/localtime").partition("zoneinfo/")[2]
    for candidate in (name, system):
        try:
            if candidate:
                return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return ZoneInfo("UTC")


def home_time(value: dt.datetime | None, zone: ZoneInfo) -> str:
    return value.astimezone(zone).strftime("%H:%M") if value else ""


def flight_url(spec: dict[str, str]) -> str:
    date = dt.date.fromisoformat(spec["date"])
    query = urllib.parse.urlencode({"year": date.year, "month": date.month, "date": date.day})
    carrier = re.match(r"[A-Z]{2,3}(?=\d)", spec["code"])
    airline = carrier.group(0) if carrier else spec["code"][:2]
    number = spec["code"][len(airline) :]
    return f"https://www.flightstats.com/v2/flight-tracker/{airline}/{number}?{query}"


def fetch_page(spec: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(flight_url(spec), headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=18) as response:
        raw = response.read(2_500_000).decode("utf-8", errors="replace")
    marker_at = raw.find(MARKER)
    if marker_at < 0:
        raise ValueError("the dated flight record was not present")
    page, _ = json.JSONDecoder().raw_decode(raw[marker_at + len(MARKER) :])
    flight = page["props"]["initialState"]["flightTracker"]["flight"]
    if not isinstance(flight, dict):
        raise ValueError("the dated flight record was empty")
    return flight


def newest_position(track: dict[str, Any]) -> dict[str, Any] | None:
    positions = [item for item in (track.get("positions") or []) if isinstance(item, dict)]
    if not positions:
        return None
    oldest = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    return max(positions, key=lambda item: parse_time(item.get("date")) or oldest)


def airport_payload(
    airport: dict[str, Any] | None,
    schedule_time: dt.datetime | None,
    current_time: dt.datetime | None,
    zone: ZoneInfo,
) -> dict[str, Any]:
    airport = airport or {}
    times = airport.get("times") or {}
    current = times.get("estimatedActual") or {}
    scheduled = times.get("scheduled") or {}
    return {
        "code": str(airport.get("iata") or airport.get("fs") or "—"),
        "city": str(airport.get("city") or ""),
        "terminal": airport.get("terminal"),
        "gate": airport.get("gate"),
        "baggage": airport.get("baggage"),
        "time": str(current.get("time24") or scheduled.get("time24") or "—"),
        "epochMs": epoch_ms(current_time or schedule_time),
        "homeTime": home_time(current_time or schedule_time, zone),
    }


def phase_for(
    *,
    cancelled: bool,
    landed: bool,
    departed: bool,
    start: dt.datetime | None,
    end: dt.datetime | None,
    now: dt.datetime,
    vertical_rate: int | None = None,
    altitude: int | None = None,
) -> str:
    if cancelled:
        return "cancelled"
    if landed:
        return "landed"
    remaining = (end - now).total_seconds() / 60 if end else None
    if departed:
        if remaining is not None and remaining <= 15:
            return "final-approach"
        if (
            (remaining is not None and remaining <= 60)
            or (vertical_rate is not None and vertical_rate < -300)
            or (altitude is not None and altitude < 10_000 and altitude > 0)
        ):
            return "arriving"
        return "airborne"
    if start:
        until_departure = (start - now).total_seconds() / 60
        if until_departure <= 0:
            return "awaiting-departure"
        if until_departure <= DEPARTURE_SOON_MINUTES:
            return "departing-soon"
        if until_departure <= BOARDING_SOON_MINUTES:
            return "boarding-soon"
    return "scheduled"


def normalize_flightstats(
    spec: dict[str, str],
    flight: dict[str, Any],
    now: dt.datetime,
    zone: ZoneInfo,
) -> dict[str, Any]:
    note = flight.get("flightNote") or {}
    status = flight.get("status") or {}
    schedule = flight.get("schedule") or {}
    positional = flight.get("positional") or {}
    track = positional.get("flexTrack") or {}
    latest = newest_position(track)

    scheduled_start = parse_time(schedule.get("scheduledDepartureUTC"))
    scheduled_end = parse_time(schedule.get("scheduledArrivalUTC"))
    start = parse_time(schedule.get("estimatedActualDepartureUTC")) or scheduled_start
    end = parse_time(schedule.get("estimatedActualArrivalUTC")) or scheduled_end
    departed = bool(note.get("hasDepartedGate") or note.get("hasDepartedRunway"))
    landed = bool(note.get("landed") or flight.get("isLanded"))
    cancelled = bool(note.get("canceled"))

    arrival_delay = safe_int(((status.get("delay") or {}).get("arrival") or {}).get("minutes"))
    departure_delay = safe_int(((status.get("delay") or {}).get("departure") or {}).get("minutes"))
    status_text = str(status.get("status") or "Unknown")
    description = str(status.get("statusDescription") or "")

    phase = phase_for(
        cancelled=cancelled,
        landed=landed,
        departed=departed,
        start=start,
        end=end,
        now=now,
        altitude=latest.get("altitudeFt") if latest else None,
    )
    progress = 1.0 if landed else 0.0
    if departed and start and end and end > start:
        progress = max(0.0, min(1.0, (now - start).total_seconds() / (end - start).total_seconds()))
    remaining_minutes = max(0, round((end - now).total_seconds() / 60)) if end and not landed else 0 if landed else None
    departure = airport_payload(flight.get("departureAirport"), scheduled_start, start, zone)
    arrival = airport_payload(flight.get("arrivalAirport"), scheduled_end, end, zone)
    alert_words = f"{status_text} {description}".lower()
    # Once airborne only the arrival delay matters; before that, the worse of both.
    delay = arrival_delay if departed else max(departure_delay, arrival_delay)

    return {
        "key": spec["key"],
        "code": spec["code"],
        "airline": str((((flight.get("ticketHeader") or {}).get("carrier")) or {}).get("name") or ""),
        "url": flight_url(spec),
        "route": f"{departure['code']} → {arrival['code']}",
        "status": status_text,
        "description": description,
        "phase": phase,
        "departed": departed,
        "landed": landed,
        "cancelled": cancelled,
        "alert": cancelled or status.get("diverted") is True or "cancel" in alert_words,
        "delayMinutes": delay,
        "late": delay >= LATE_MINUTES,
        "departure": departure,
        "arrival": arrival,
        "aircraft": {"callsign": track.get("callsign"), "hex": None},
        "progress": {
            "percent": round(progress * 100, 1),
            "remainingMinutes": remaining_minutes,
            "etaEpochMs": epoch_ms(end),
        },
        "error": None,
    }


def enrich_with_adsb(record: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    if not record.get("departed") or record.get("landed") or record.get("cancelled"):
        return record
    preferred = str((record.get("aircraft") or {}).get("callsign") or "")
    aircraft, route = adsb.find_aircraft(str(record.get("code") or ""), preferred)
    if not aircraft:
        return record

    altitude_raw = aircraft.get("alt_baro")
    altitude = int(altitude_raw) if isinstance(altitude_raw, (int, float)) else 0 if str(altitude_raw).lower() == "ground" else None
    vertical_rate = aircraft.get("baro_rate")
    live_callsign = str(aircraft.get("flight") or "").strip().upper()
    record["aircraft"] = {"callsign": live_callsign or preferred, "hex": aircraft.get("hex") or None}

    origin, destination = (route or {}).get("origin"), (route or {}).get("destination")
    if origin and destination and None not in (
        origin.get("lat"),
        origin.get("lon"),
        destination.get("lat"),
        destination.get("lon"),
        aircraft.get("lat"),
        aircraft.get("lon"),
    ):
        flown = adsb.haversine_nm(origin["lat"], origin["lon"], aircraft["lat"], aircraft["lon"])
        remaining = adsb.haversine_nm(aircraft["lat"], aircraft["lon"], destination["lat"], destination["lon"])
        if flown + remaining > 0:
            record["progress"]["percent"] = round(100 * flown / (flown + remaining), 1)

    end_ms = (record.get("progress") or {}).get("etaEpochMs")
    end = dt.datetime.fromtimestamp(end_ms / 1000, dt.timezone.utc) if end_ms else None
    record["phase"] = phase_for(
        cancelled=bool(record.get("cancelled")),
        landed=bool(record.get("landed")),
        departed=bool(record.get("departed")),
        start=None,
        end=end,
        now=now,
        vertical_rate=int(vertical_rate) if isinstance(vertical_rate, (int, float)) else None,
        altitude=altitude,
    )
    return record


def journey_cache(spec: dict[str, str]) -> str:
    return adsb.cache_path("journey", f"{spec['code']}-{spec['date']}")


def refresh_relative_fields(record: dict[str, Any], now: dt.datetime) -> None:
    progress = record.get("progress") or {}
    eta_ms = progress.get("etaEpochMs")
    if eta_ms and not record.get("landed"):
        remaining = max(0, round((dt.datetime.fromtimestamp(eta_ms / 1000, dt.timezone.utc) - now).total_seconds() / 60))
        progress["remainingMinutes"] = remaining
    elif record.get("landed"):
        progress["remainingMinutes"] = 0
        progress["percent"] = 100
    record["progress"] = progress


def fallback_record(spec: dict[str, str], reason: str) -> dict[str, Any]:
    # FlightStats down before the first successful fetch; later failures use the cache.
    return {
        "key": spec["key"],
        "code": spec["code"],
        "url": flight_url(spec),
        "route": "—",
        "phase": "unknown",
        "departure": {},
        "arrival": {},
        "progress": {},
        "error": reason,
    }


def fetch_leg(spec: dict[str, str], now: dt.datetime, zone: ZoneInfo) -> dict[str, Any]:
    try:
        record = normalize_flightstats(spec, fetch_page(spec), now, zone)
        record = enrich_with_adsb(record, now)
        adsb.write_json(journey_cache(spec), record)
        return record
    except (KeyError, TypeError, ValueError, OSError, urllib.error.URLError) as error:
        reason = str(error) or error.__class__.__name__
    cached = adsb.read_json(journey_cache(spec))
    if isinstance(cached, dict):
        cached["error"] = reason
        cached["description"] = "Showing the last schedule with live aircraft data when available"
        refresh_relative_fields(cached, now)
        return enrich_with_adsb(cached, now)
    return fallback_record(spec, reason)


def journey_from_legs(
    legs: list[dict[str, Any]],
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    origin = city((legs[0] if legs else {}).get("departure"))
    destination = city((legs[-1] if legs else {}).get("arrival"))
    cancelled = [index for index, leg in enumerate(legs) if leg.get("cancelled")]
    # The latest leg that has left the gate; a later departure wins over a missing landed flag.
    started = [index for index, leg in enumerate(legs) if leg.get("departed") or leg.get("landed")]
    latest = started[-1] if started else -1
    # The transfer in progress or coming up leads into the first leg after the latest departure.
    onward = max(1, latest + 1)
    has_transfer = onward < len(legs)
    via = city(legs[onward - 1].get("arrival")) if has_transfer else ""
    if cancelled:
        stage, active_index, label = "disrupted", cancelled[0], "Journey needs attention"
    elif latest == len(legs) - 1 and legs[latest].get("landed"):
        stage, active_index, label = "complete", latest, f"Arrived in {destination}"
    elif latest >= 0 and legs[latest].get("landed"):
        stage, active_index, label = "connection", onward, f"Transfer in {via}"
    elif latest >= 0:
        stage, active_index, label = "en-route", latest, f"En route to {city(legs[latest].get('arrival'))}"
    else:
        stage, active_index, label = "starting", 0, f"Departing {origin}"

    # Live layover: arrival estimate into the transfer to the onward leg's departure estimate.
    connection_minutes = None
    if stage != "disrupted" and has_transfer:
        landing_ms = (legs[onward - 1].get("arrival") or {}).get("epochMs")
        onward_ms = (legs[onward].get("departure") or {}).get("epochMs")
        if landing_ms and onward_ms:
            connection_minutes = round((onward_ms - landing_ms) / 60_000)

    active = legs[active_index] if legs else {}
    phase = str(active.get("phase") or "unknown")
    progress = active.get("progress") or {}
    if stage == "disrupted":
        next_epoch, next_kind = None, "cancelled"
    elif stage == "connection":
        next_epoch = (active.get("departure") or {}).get("epochMs")
        next_kind = "departure"
    elif stage == "complete":
        next_epoch = (active.get("arrival") or {}).get("epochMs")
        next_kind = "arrived"
    elif phase in {"scheduled", "boarding-soon", "departing-soon", "awaiting-departure"}:
        next_epoch = (active.get("departure") or {}).get("epochMs")
        next_kind = "departure"
    else:
        next_epoch = progress.get("etaEpochMs")
        next_kind = "arrival"
    next_minutes = None
    if next_epoch:
        reference = now or utc_now()
        next_minutes = max(0, round((next_epoch - round(reference.timestamp() * 1000)) / 60_000))

    return {
        "stage": stage,
        "label": label,
        "origin": origin,
        "via": via,
        "destination": destination,
        "connectionMinutes": connection_minutes,
        "connectionLegIndex": onward if connection_minutes is not None else None,
        "tightConnection": connection_minutes is not None and connection_minutes < TIGHT_CONNECTION_MINUTES,
        "activeLegIndex": active_index,
        "nextEventKind": next_kind,
        "nextEventMinutes": next_minutes,
    }


def city(airport: dict[str, Any] | None) -> str:
    return str((airport or {}).get("city") or (airport or {}).get("code") or "")


def pickup_for(legs: list[dict[str, Any]], lead_minutes: int, now: dt.datetime, zone: ZoneInfo) -> dict[str, Any]:
    """Leave-home time from the final leg's live (else scheduled) arrival."""
    final = legs[-1] if legs else {}
    arrival = final.get("arrival") or {}
    landing_ms = arrival.get("epochMs")
    landing = dt.datetime.fromtimestamp(landing_ms / 1000, dt.timezone.utc) if landing_ms else None
    leave = landing - dt.timedelta(minutes=lead_minutes) if landing else None
    return {
        "landingEpochMs": landing_ms,
        "landingHomeTime": home_time(landing, zone),
        "leaveEpochMs": epoch_ms(leave),
        "leaveHomeTime": home_time(leave, zone),
        "leaveMinutes": round((leave - now).total_seconds() / 60) if leave else None,
        "airport": arrival.get("code") or "",
        "terminal": arrival.get("terminal"),
        "gate": arrival.get("gate"),
        "baggage": arrival.get("baggage"),
        "done": bool(final.get("landed") or final.get("cancelled")),
    }


def snapshot(report: dict[str, Any]) -> dict[str, Any]:
    journey = report.get("journey") or {}
    pickup = report.get("pickup") or {}
    return {
        "stage": journey.get("stage"),
        "leaveEpochMs": pickup.get("leaveEpochMs"),
        "leaveMinutes": pickup.get("leaveMinutes"),
        "legs": {
            leg["key"]: {
                "phase": leg.get("phase"),
                "remaining": (leg.get("progress") or {}).get("remainingMinutes"),
                "delay": safe_int(leg.get("delayMinutes")),
                "gate": (leg.get("departure") or {}).get("gate"),
            }
            for leg in report.get("legs") or []
        },
    }


def event(
    key: str,
    title: str,
    body: str,
    urgency: str = "normal",
) -> dict[str, str]:
    return {"key": key, "title": title, "body": body, "urgency": urgency}


def notification_events(
    previous: dict[str, Any] | None,
    report: dict[str, Any],
    already_fired: set[str] | None = None,
) -> tuple[list[dict[str, str]], set[str]]:
    fired = set(already_fired or set())
    current = snapshot(report)
    if not previous:
        return [], fired

    events: list[dict[str, str]] = []

    def add(item: dict[str, str]) -> None:
        if item["key"] not in fired:
            events.append(item)
            fired.add(item["key"])

    previous_legs = previous.get("legs") or {}
    for leg in report.get("legs") or []:
        key = leg["key"]
        before = previous_legs.get(key)
        if not before:
            continue
        old_phase, new_phase = before.get("phase"), leg.get("phase")
        destination = (leg.get("arrival") or {}).get("city") or (leg.get("arrival") or {}).get("code") or "destination"
        home_arrival = (leg.get("arrival") or {}).get("homeTime") or ""
        suffix = f" · {home_arrival} at home" if home_arrival else ""

        if old_phase not in {"airborne", "arriving", "final-approach"} and new_phase in {"airborne", "arriving", "final-approach"}:
            add(event(f"{key}:departed", f"{leg['code']} is airborne", f"Now heading to {destination}{suffix}"))
        if old_phase != "landed" and new_phase == "landed":
            add(event(f"{key}:landed", f"{leg['code']} has landed", f"Arrived in {destination}", "critical"))
        if old_phase != "cancelled" and new_phase == "cancelled":
            add(event(f"{key}:cancelled", f"{leg['code']} was cancelled", leg.get("description") or f"Check with {leg.get('airline') or 'the airline'}", "critical"))

        old_remaining = before.get("remaining")
        new_remaining = (leg.get("progress") or {}).get("remainingMinutes")
        if new_phase in {"airborne", "arriving", "final-approach"} and isinstance(old_remaining, (int, float)) and isinstance(new_remaining, (int, float)):
            for threshold in (60, 30, 15):
                if old_remaining > threshold >= new_remaining:
                    add(event(f"{key}:arrival:{threshold}", f"{leg['code']} is nearing {destination}", f"About {threshold} minutes to landing{suffix}", "critical" if threshold == 15 else "normal"))

        old_delay = safe_int(before.get("delay"))
        new_delay = safe_int(leg.get("delayMinutes"))
        if old_delay < 30 <= new_delay:
            add(event(f"{key}:delay:{new_delay // 30}", f"{leg['code']} is delayed", f"Current delay: about {new_delay} minutes", "critical"))

        old_gate = str(before.get("gate") or "")
        new_gate = str((leg.get("departure") or {}).get("gate") or "")
        departure_epoch = (leg.get("departure") or {}).get("epochMs")
        minutes_to_departure = (departure_epoch - report["generatedAtMs"]) / 60_000 if departure_epoch else None
        if new_gate and new_gate != old_gate and minutes_to_departure is not None and -30 <= minutes_to_departure <= 240:
            verb = "changed to" if old_gate else "is"
            add(event(f"{key}:gate:{new_gate}", f"{leg['code']} gate {verb} {new_gate}", f"Terminal {(leg.get('departure') or {}).get('terminal') or '—'}"))

    journey = report.get("journey") or {}
    via, destination = journey.get("via") or "the transfer airport", journey.get("destination") or "destination"
    old_stage = previous.get("stage")
    new_stage = current.get("stage")
    active_index = safe_int(journey.get("activeLegIndex"))
    if old_stage != "connection" and new_stage == "connection":
        legs = report.get("legs") or []
        onward = city(legs[active_index].get("arrival")) if active_index < len(legs) else destination
        add(event(f"journey:connection:{active_index}", f"Landed in {via}", f"Transfer: next flight to {onward} is now active", "critical"))
    if old_stage != "complete" and new_stage == "complete":
        add(event("journey:complete", "Journey complete", f"Arrived in {destination}" + (f" · belt {(report.get('pickup') or {}).get('baggage')}" if (report.get("pickup") or {}).get("baggage") else ""), "critical"))

    # One warning per transfer; the fired set keeps it from repeating.
    connection = journey.get("connectionMinutes")
    if journey.get("tightConnection") and isinstance(connection, int):
        add(event(f"journey:tight-connection:{journey.get('connectionLegIndex')}", f"Tight connection in {via}", f"Only about {connection} minutes to transfer", "critical"))

    pickup = report.get("pickup") or {}
    leave_ms, leave_time = pickup.get("leaveEpochMs"), pickup.get("leaveHomeTime") or ""
    old_leave_ms = previous.get("leaveEpochMs")
    if not pickup.get("done") and leave_ms and old_leave_ms and abs(leave_ms - old_leave_ms) >= PICKUP_SHIFT_MINUTES * 60_000:
        # The landing estimate moved: say so and re-arm the leave reminders.
        fired -= {"pickup:15", "pickup:now"}
        later = "later" if leave_ms > old_leave_ms else "earlier"
        add(event(f"pickup:moved:{leave_ms // 60_000}", f"Leave {later}: by {leave_time}", f"Landing now expected {pickup.get('landingHomeTime') or '—'}"))
    old_minutes, new_minutes = previous.get("leaveMinutes"), current.get("leaveMinutes")
    if not pickup.get("done") and isinstance(old_minutes, int) and isinstance(new_minutes, int):
        where = " · ".join(filter(None, [pickup.get("airport"), f"T{pickup['terminal']}" if pickup.get("terminal") else ""]))
        if old_minutes > 0 >= new_minutes:
            add(event("pickup:now", "Time to leave", f"Landing {pickup.get('landingHomeTime') or '—'} at {where}", "critical"))
        elif old_minutes > 15 >= new_minutes:
            add(event("pickup:15", f"Leave in {new_minutes} min", f"Leave by {leave_time} · landing {pickup.get('landingHomeTime') or '—'}"))
    return events, fired


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    trip = parser.add_mutually_exclusive_group(required=True)
    trip.add_argument("--legs", help="comma-separated flight@date values")
    trip.add_argument("--lookup", metavar="FLIGHT@DATE", help="print one leg and leave the trip state alone")
    parser.add_argument("--home-timezone", default="", help="IANA zone for home clock times; the system's by default")
    parser.add_argument("--leave-lead-minutes", type=int, default=DEFAULT_LEAVE_LEAD_MINUTES)
    parser.add_argument("--no-events", action="store_true")
    options = parser.parse_args(arguments)

    now = utc_now()
    zone = home_zone(options.home_timezone)
    if options.lookup is not None:
        found = parse_leg_specs(options.lookup)[:1]
        leg = fetch_leg(found[0], now, zone) if found else {"phase": "unknown", "error": "Use a flight number and date like LH400@2026-12-23"}
        print(json.dumps(leg, separators=(",", ":")))
        return 0

    specs = parse_leg_specs(options.legs)
    if not specs:
        parser.error("--legs has no valid flight@date value, e.g. LH400@2026-12-23")
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(specs))) as pool:
        legs = list(pool.map(lambda spec: fetch_leg(spec, now, zone), specs))

    report: dict[str, Any] = {
        "generatedAtMs": epoch_ms(now),
        "homeTimezone": zone.key,
        "legs": legs,
        "errors": [f"{leg['code']}: {leg['error']}" for leg in legs if leg.get("error") and leg.get("phase") == "unknown"],
    }
    report["journey"] = journey_from_legs(legs, now)
    report["pickup"] = pickup_for(legs, options.leave_lead_minutes, now, zone)

    trip = ",".join(spec["key"] for spec in specs)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # One widget per monitor runs this concurrently; the lock keeps notifications single.
    with open(STATE_DIR / "state.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = adsb.read_json(str(STATE_PATH))
        if not isinstance(state, dict) or state.get("trip") != trip:  # journey-level keys must not leak into a new trip
            state = {}
        events, fired = notification_events(state.get("snapshot"), report, set(state.get("fired") or []))
        adsb.write_json(str(STATE_PATH), {"trip": trip, "snapshot": snapshot(report), "fired": sorted(fired)})
    report["events"] = [] if options.no_events else events
    print(json.dumps(report, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

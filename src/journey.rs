//! The trip itself: dated legs, flight phases, the journey stage, the pickup
//! time and the notifications. Pure functions over JSON records, so the
//! records read back from the cache behave exactly like fresh ones.
//!
//! Records stay `serde_json::Value`s on purpose: the Python backend wrote and
//! read them as loose dicts, and cached files from it must keep working.
//! Helpers below reproduce Python's truthiness, `str()`, `int()` and `round()`.

use std::collections::BTreeSet;
use std::fs;
use std::path::{Component, Path};

use jiff::Timestamp;
use jiff::civil::Date;
use jiff::tz::TimeZone;
use serde_json::{Value, json};

const BOARDING_SOON_MINUTES: f64 = 60.0;
const DEPARTURE_SOON_MINUTES: f64 = 30.0;
pub const DEFAULT_LEAVE_LEAD_MINUTES: i64 = 60;
const TIGHT_CONNECTION_MINUTES: i64 = 60;
const LATE_MINUTES: i64 = 15; // airline convention
const PICKUP_SHIFT_MINUTES: f64 = 10.0;
const AIRBORNE: [&str; 3] = ["airborne", "arriving", "final-approach"];
const BEFORE_DEPARTURE: [&str; 4] = [
    "scheduled",
    "boarding-soon",
    "departing-soon",
    "awaiting-departure",
];
/// Python's default zoneinfo search path on Linux.
const TZPATH: [&str; 4] = [
    "/usr/share/zoneinfo",
    "/usr/lib/zoneinfo",
    "/usr/share/lib/zoneinfo",
    "/etc/zoneinfo",
];

static NULL: Value = Value::Null;

// --- Python semantics -------------------------------------------------------

/// `value.get(key)` that tolerates non-objects, like `(value or {}).get(key)`.
pub fn field<'a>(value: &'a Value, key: &str) -> &'a Value {
    value.get(key).unwrap_or(&NULL)
}

/// Python's truthiness: null, false, 0, "", [] and {} are false.
pub fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::Number(number) => number.as_f64() != Some(0.0),
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(map) => !map.is_empty(),
    }
}

/// Python's `a or b`.
fn either<'a>(first: &'a Value, second: &'a Value) -> &'a Value {
    if truthy(first) { first } else { second }
}

/// Python's `str(value)` for the scalars the feeds send.
pub fn py_str(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        Value::Null => "None".to_owned(),
        Value::Bool(true) => "True".to_owned(),
        Value::Bool(false) => "False".to_owned(),
        other => other.to_string(),
    }
}

/// Python's `str(value or "")`.
pub fn str_or_empty(value: &Value) -> String {
    if truthy(value) {
        py_str(value)
    } else {
        String::new()
    }
}

/// A JSON number, with booleans as 0 and 1 like Python's `isinstance(x, (int, float))`.
pub fn number(value: &Value) -> Option<f64> {
    match value {
        Value::Number(number) => number.as_f64(),
        Value::Bool(flag) => Some(f64::from(u8::from(*flag))),
        _ => None,
    }
}

/// A JSON number that is also truthy, for Python's `if x: ... x ...`.
fn truthy_number(value: &Value) -> Option<f64> {
    number(value).filter(|number| *number != 0.0)
}

/// A JSON integer, like Python's `isinstance(x, int)` (which includes bools).
fn integer(value: &Value) -> Option<i64> {
    match value {
        Value::Number(number) => number.as_i64(),
        Value::Bool(flag) => Some(i64::from(*flag)),
        _ => None,
    }
}

/// Python's `int(text)`: surrounding whitespace, a sign, `_` between digits.
pub fn py_int(text: &str) -> Option<i64> {
    let text = text.trim();
    let (negative, digits) = match text.strip_prefix('-') {
        Some(rest) => (true, rest),
        None => (false, text.strip_prefix('+').unwrap_or(text)),
    };
    let well_formed = !digits.is_empty()
        && !digits.starts_with('_')
        && !digits.ends_with('_')
        && !digits.contains("__")
        && digits
            .chars()
            .all(|character| character.is_ascii_digit() || character == '_');
    if !well_formed {
        return None;
    }
    let magnitude: i64 = digits.replace('_', "").parse().ok()?;
    Some(if negative { -magnitude } else { magnitude })
}

/// `int(value or 0)`, and 0 for anything `int()` rejects.
pub fn safe_int(value: &Value) -> i64 {
    match value {
        Value::Bool(flag) => i64::from(*flag),
        Value::Number(number) => number
            .as_i64()
            .or_else(|| {
                number
                    .as_f64()
                    .filter(|float| float.is_finite())
                    .map(|float| float.trunc() as i64)
            })
            .unwrap_or(0),
        Value::String(text) => py_int(text).unwrap_or(0),
        _ => 0,
    }
}

/// Python's `round(x)`: halves go to the even neighbour.
pub fn py_round(value: f64) -> i64 {
    value.round_ties_even() as i64
}

/// Python's `round(x, 1)`, which rounds the exact binary value; so does `{:.1}`.
pub fn round_tenth(value: f64) -> f64 {
    format!("{value:.1}").parse().unwrap_or(value)
}

// --- Time, in Python's microseconds -----------------------------------------

/// `(later - earlier).total_seconds() / 60`.
fn minutes_between(earlier: Timestamp, later: Timestamp) -> f64 {
    (later.as_microsecond() - earlier.as_microsecond()) as f64 / 1e6 / 60.0
}

/// `round(value.timestamp() * 1000)`.
pub fn epoch_ms(value: Timestamp) -> i64 {
    py_round(value.as_microsecond() as f64 / 1e6 * 1000.0)
}

/// `datetime.fromtimestamp(ms / 1000, UTC)` for a set (truthy) value, rounded to the microsecond as CPython does.
pub fn from_epoch_ms(value: &Value) -> Option<Timestamp> {
    let seconds = truthy_number(value)? / 1000.0;
    let mut whole = seconds.trunc();
    let mut micros = ((seconds - whole) * 1e6).round_ties_even();
    if micros >= 1e6 {
        micros -= 1e6;
        whole += 1.0;
    } else if micros < 0.0 {
        micros += 1e6;
        whole -= 1.0;
    }
    Timestamp::from_microsecond(whole as i64 * 1_000_000 + micros as i64).ok()
}

/// `datetime.fromisoformat`, with naive times taken as UTC.
pub fn parse_time(value: &Value) -> Option<Timestamp> {
    if !truthy(value) {
        return None;
    }
    let text = py_str(value);
    let parsed = text.parse::<Timestamp>().ok().or_else(|| {
        let civil = text.parse::<jiff::civil::DateTime>().ok()?;
        civil
            .to_zoned(TimeZone::UTC)
            .ok()
            .map(|zoned| zoned.timestamp())
    })?;
    Timestamp::from_microsecond(parsed.as_microsecond()).ok()
}

/// The home clock: a name as the user wrote it plus its rules.
#[derive(Clone, Debug)]
pub struct Zone {
    pub name: String,
    pub tz: TimeZone,
}

/// The configured zone, else the system's, else UTC.
pub fn home_zone(configured: &str, system: &str) -> Zone {
    [configured, system]
        .into_iter()
        .filter(|name| !name.is_empty())
        .find_map(|name| {
            Some(Zone {
                name: name.to_owned(),
                tz: zone_named(name)?,
            })
        })
        .unwrap_or_else(|| Zone {
            name: "UTC".to_owned(),
            tz: TimeZone::UTC,
        })
}

/// `ZoneInfo(name)`: a normalized relative path to a TZif file, case as written.
fn zone_named(name: &str) -> Option<TimeZone> {
    let parts = Path::new(name)
        .components()
        .map(|part| match part {
            Component::Normal(part) => part.to_str(),
            _ => None,
        })
        .collect::<Option<Vec<_>>>()?;
    if parts.join("/") != name {
        return None;
    }
    let path = TZPATH
        .iter()
        .map(|folder| Path::new(folder).join(name))
        .find(|path| path.is_file())?;
    TimeZone::tzif(name, &fs::read(path).ok()?).ok()
}

/// "08:20", or "Wed 08:20" when `now` is given and the day differs at home.
pub fn home_time(value: Option<Timestamp>, zone: &Zone, now: Option<Timestamp>) -> String {
    let Some(value) = value else {
        return String::new();
    };
    let local = value.to_zoned(zone.tz.clone());
    let other_day = now.is_some_and(|now| now.to_zoned(zone.tz.clone()).date() != local.date());
    local
        .strftime(if other_day { "%a %H:%M" } else { "%H:%M" })
        .to_string()
}

// --- Legs ---------------------------------------------------------------------

/// One dated flight, e.g. `LH400@2026-12-23`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LegSpec {
    pub key: String,
    pub code: String,
    pub date: Date,
}

/// Comma-separated `flight@date` values; unreadable items and repeats drop out.
pub fn parse_leg_specs(raw: &str) -> Vec<LegSpec> {
    let mut specs: Vec<LegSpec> = Vec::new();
    for item in raw.split(',') {
        let compact: String = item
            .chars()
            .filter(|character| !character.is_whitespace())
            .collect();
        let Some(spec) = leg_spec(&compact) else {
            continue;
        };
        if !specs.iter().any(|known| known.key == spec.key) {
            specs.push(spec);
        }
    }
    specs
}

/// `([A-Z0-9]{2,3}\d{1,4})@(\d{4}-\d{2}-\d{2})`, case-insensitive, and a real date.
fn leg_spec(text: &str) -> Option<LegSpec> {
    let (code, date) = text.split_once('@')?;
    let code_ok = (2..=3).any(|prefix| {
        code.len() > prefix
            && code.len() - prefix <= 4
            && code
                .bytes()
                .take(prefix)
                .all(|byte| byte.is_ascii_alphanumeric())
            && code.bytes().skip(prefix).all(|byte| byte.is_ascii_digit())
    });
    let date_ok = date.len() == 10
        && date.bytes().enumerate().all(|(at, byte)| {
            if at == 4 || at == 7 {
                byte == b'-'
            } else {
                byte.is_ascii_digit()
            }
        });
    if !code_ok || !date_ok {
        return None;
    }
    let date: Date = date.parse().ok().filter(|date: &Date| date.year() >= 1)?;
    let code = code.to_ascii_uppercase();
    Some(LegSpec {
        key: format!("{code}@{date}"),
        code,
        date,
    })
}

/// The FlightStats page for one dated flight.
pub fn flight_url(spec: &LegSpec) -> String {
    let letters = spec.code.bytes().take_while(u8::is_ascii_uppercase).count();
    let airline = if (2..=3).contains(&letters) {
        letters
    } else {
        2
    };
    let (airline, number) = spec.code.split_at(airline);
    let date = spec.date;
    format!(
        "https://www.flightstats.com/v2/flight-tracker/{airline}/{number}?year={}&month={}&date={}",
        date.year(),
        date.month(),
        date.day()
    )
}

/// What decides a flight's phase.
#[derive(Clone, Copy, Debug, Default)]
pub struct Situation {
    pub cancelled: bool,
    pub landed: bool,
    pub departed: bool,
    pub start: Option<Timestamp>,
    pub end: Option<Timestamp>,
    pub vertical_rate: Option<f64>,
    pub altitude: Option<f64>,
}

impl Situation {
    pub fn phase(&self, now: Timestamp) -> &'static str {
        if self.cancelled {
            return "cancelled";
        }
        if self.landed {
            return "landed";
        }
        let remaining = self.end.map(|end| minutes_between(now, end));
        if self.departed {
            if remaining.is_some_and(|minutes| minutes <= 15.0) {
                return "final-approach";
            }
            if remaining.is_some_and(|minutes| minutes <= 60.0)
                || self.vertical_rate.is_some_and(|rate| rate < -300.0)
                || self
                    .altitude
                    .is_some_and(|feet| feet < 10_000.0 && feet > 0.0)
            {
                return "arriving";
            }
            return "airborne";
        }
        if let Some(start) = self.start {
            let until_departure = minutes_between(now, start);
            if until_departure <= 0.0 {
                return "awaiting-departure";
            }
            if until_departure <= DEPARTURE_SOON_MINUTES {
                return "departing-soon";
            }
            if until_departure <= BOARDING_SOON_MINUTES {
                return "boarding-soon";
            }
        }
        "scheduled"
    }
}

/// The track point with the latest date; the first one wins a tie, as in Python's `max`.
fn newest_position(track: &Value) -> Option<&Value> {
    let positions = field(track, "positions").as_array()?;
    positions
        .iter()
        .filter(|item| item.is_object())
        .fold(None, |best: Option<(&Value, Option<Timestamp>)>, item| {
            let date = parse_time(field(item, "date"));
            match best {
                Some((_, best_date)) if date <= best_date => best,
                _ => Some((item, date)),
            }
        })
        .map(|(item, _)| item)
}

fn airport_payload(
    airport: &Value,
    schedule_time: Option<Timestamp>,
    current_time: Option<Timestamp>,
    zone: &Zone,
) -> Value {
    let times = field(airport, "times");
    let current = field(times, "estimatedActual");
    let scheduled = field(times, "scheduled");
    let moment = current_time.or(schedule_time);
    json!({
        "code": py_str(either(either(field(airport, "iata"), field(airport, "fs")), &json!("—"))),
        "city": str_or_empty(field(airport, "city")),
        "terminal": field(airport, "terminal"),
        "gate": field(airport, "gate"),
        "baggage": field(airport, "baggage"),
        "time": py_str(either(either(field(current, "time24"), field(scheduled, "time24")), &json!("—"))),
        "epochMs": moment.map(epoch_ms),
        "homeTime": home_time(moment, zone, None),
    })
}

/// One leg from a FlightStats flight record.
pub fn normalize_flightstats(spec: &LegSpec, flight: &Value, now: Timestamp, zone: &Zone) -> Value {
    let note = field(flight, "flightNote");
    let status = field(flight, "status");
    let schedule = field(flight, "schedule");
    let track = field(field(flight, "positional"), "flexTrack");
    let latest = newest_position(track);

    let scheduled_start = parse_time(field(schedule, "scheduledDepartureUTC"));
    let scheduled_end = parse_time(field(schedule, "scheduledArrivalUTC"));
    let start = parse_time(field(schedule, "estimatedActualDepartureUTC")).or(scheduled_start);
    let end = parse_time(field(schedule, "estimatedActualArrivalUTC")).or(scheduled_end);
    let departed =
        truthy(field(note, "hasDepartedGate")) || truthy(field(note, "hasDepartedRunway"));
    let landed = truthy(field(note, "landed")) || truthy(field(flight, "isLanded"));
    let cancelled = truthy(field(note, "canceled"));

    let delays = field(status, "delay");
    let arrival_delay = safe_int(field(field(delays, "arrival"), "minutes"));
    let departure_delay = safe_int(field(field(delays, "departure"), "minutes"));
    let status_text = py_str(either(field(status, "status"), &json!("Unknown")));
    let description = str_or_empty(field(status, "statusDescription"));

    let situation = Situation {
        cancelled,
        landed,
        departed,
        start,
        end,
        vertical_rate: None,
        altitude: latest.and_then(|point| number(field(point, "altitudeFt"))),
    };
    let mut progress = if landed { 1.0 } else { 0.0 };
    if let (true, Some(start), Some(end)) = (departed, start, end)
        && end > start
    {
        progress = (minutes_between(start, now) / minutes_between(start, end)).clamp(0.0, 1.0);
    }
    let remaining_minutes = match end {
        Some(end) if !landed => Some(py_round(minutes_between(now, end)).max(0)),
        _ if landed => Some(0),
        _ => None,
    };
    let departure = airport_payload(
        field(flight, "departureAirport"),
        scheduled_start,
        start,
        zone,
    );
    let arrival = airport_payload(field(flight, "arrivalAirport"), scheduled_end, end, zone);
    let alert_words = format!("{status_text} {description}").to_lowercase();
    // Once airborne only the arrival delay matters; before that, the worse of both.
    let delay = if departed {
        arrival_delay
    } else {
        departure_delay.max(arrival_delay)
    };

    json!({
        "key": spec.key,
        "code": spec.code,
        "airline": str_or_empty(field(field(field(flight, "ticketHeader"), "carrier"), "name")),
        "url": flight_url(spec),
        "route": format!("{} → {}", py_str(&departure["code"]), py_str(&arrival["code"])),
        "description": description,
        "phase": situation.phase(now),
        "departed": departed,
        "landed": landed,
        "cancelled": cancelled,
        "alert": cancelled || field(status, "diverted") == &Value::Bool(true) || alert_words.contains("cancel"),
        "delayMinutes": delay,
        "late": delay >= LATE_MINUTES,
        "departure": departure,
        "arrival": arrival,
        "aircraft": {"callsign": field(track, "callsign"), "hex": null},
        "progress": {
            "percent": round_tenth(progress * 100.0),
            "remainingMinutes": remaining_minutes,
            "etaEpochMs": end.map(epoch_ms),
        },
        "error": null,
    })
}

/// Recomputes the countdown of a cached leg for the current time.
pub fn refresh_relative_fields(record: &mut Value, now: Timestamp) {
    let landed = truthy(field(record, "landed"));
    let mut progress = record
        .get("progress")
        .filter(|progress| progress.is_object() && truthy(progress))
        .cloned()
        .unwrap_or_else(|| json!({}));
    let eta = field(&progress, "etaEpochMs");
    if landed {
        progress["remainingMinutes"] = json!(0);
        progress["percent"] = json!(100);
    } else if let Some(eta) = from_epoch_ms(eta) {
        progress["remainingMinutes"] = json!(py_round(minutes_between(now, eta)).max(0));
    }
    record["progress"] = progress;
}

/// FlightStats down before the first successful fetch; later failures use the cache.
pub fn fallback_record(spec: &LegSpec, reason: &str) -> Value {
    json!({
        "key": spec.key,
        "code": spec.code,
        "url": flight_url(spec),
        "route": "—",
        "phase": "unknown",
        "departure": {},
        "arrival": {},
        "progress": {},
        "error": reason,
    })
}

pub fn city(airport: &Value) -> String {
    py_str(either(
        either(field(airport, "city"), field(airport, "code")),
        &json!(""),
    ))
}

/// Where the trip stands: stage, active leg, the transfer ahead and the next countdown.
pub fn journey_from_legs(legs: &[Value], now: Timestamp) -> Value {
    let origin = legs
        .first()
        .map(|leg| city(field(leg, "departure")))
        .unwrap_or_default();
    let destination = legs
        .last()
        .map(|leg| city(field(leg, "arrival")))
        .unwrap_or_default();
    let cancelled = legs.iter().position(|leg| truthy(field(leg, "cancelled")));
    // The latest leg that has left the gate; a later departure wins over a missing landed flag.
    let latest = legs
        .iter()
        .rposition(|leg| truthy(field(leg, "departed")) || truthy(field(leg, "landed")));
    let landed = |index: usize| truthy(field(&legs[index], "landed"));
    // The transfer in progress or coming up leads into the first leg after the latest departure.
    let onward = latest.map_or(1, |index| index + 1).max(1);
    let has_transfer = onward < legs.len();
    let via = if has_transfer {
        city(field(&legs[onward - 1], "arrival"))
    } else {
        String::new()
    };
    let (stage, active_index, label) = match (cancelled, latest) {
        (Some(index), _) => ("disrupted", index, "Journey needs attention".to_owned()),
        (None, Some(index)) if index + 1 == legs.len() && landed(index) => {
            ("complete", index, format!("Arrived in {destination}"))
        }
        (None, Some(index)) if landed(index) => {
            ("connection", onward, format!("Transfer in {via}"))
        }
        (None, Some(index)) => (
            "en-route",
            index,
            format!("En route to {}", city(field(&legs[index], "arrival"))),
        ),
        (None, None) => ("starting", 0, format!("Departing {origin}")),
    };

    // Live layover: arrival estimate into the transfer to the onward leg's departure estimate.
    let mut connection_minutes = None;
    if stage != "disrupted" && has_transfer {
        let landing = field(field(&legs[onward - 1], "arrival"), "epochMs");
        let departure = field(field(&legs[onward], "departure"), "epochMs");
        if let (Some(landing), Some(departure)) = (truthy_number(landing), truthy_number(departure))
        {
            connection_minutes = Some(py_round((departure - landing) / 60_000.0));
        }
    }

    let active = legs.get(active_index).unwrap_or(&NULL);
    let phase = py_str(either(field(active, "phase"), &json!("unknown")));
    let (next_epoch, next_kind) = match stage {
        "disrupted" => (&NULL, "cancelled"),
        "connection" => (field(field(active, "departure"), "epochMs"), "departure"),
        "complete" => (field(field(active, "arrival"), "epochMs"), "arrived"),
        _ if BEFORE_DEPARTURE.contains(&phase.as_str()) => {
            (field(field(active, "departure"), "epochMs"), "departure")
        }
        _ => (field(field(active, "progress"), "etaEpochMs"), "arrival"),
    };
    let next_minutes = truthy_number(next_epoch)
        .map(|epoch| py_round((epoch - epoch_ms(now) as f64) / 60_000.0).max(0));

    json!({
        "stage": stage,
        "label": label,
        "via": via,
        "destination": destination,
        "connectionMinutes": connection_minutes,
        "connectionLegIndex": connection_minutes.map(|_| onward),
        "tightConnection": connection_minutes.is_some_and(|minutes| minutes < TIGHT_CONNECTION_MINUTES),
        "activeLegIndex": active_index,
        "nextEventKind": next_kind,
        "nextEventMinutes": next_minutes,
    })
}

/// Leave-home time from the final leg's live (else scheduled) arrival.
pub fn pickup_for(legs: &[Value], lead_minutes: i64, now: Timestamp, zone: &Zone) -> Value {
    let last = legs.last().unwrap_or(&NULL);
    let arrival = field(last, "arrival");
    let landing_ms = field(arrival, "epochMs");
    let landing = from_epoch_ms(landing_ms);
    let lead = lead_minutes
        .checked_mul(60)
        .map(jiff::SignedDuration::from_secs);
    let leave = landing
        .zip(lead)
        .and_then(|(landing, lead)| landing.checked_sub(lead).ok());
    json!({
        "landingEpochMs": landing_ms,
        "landingHomeTime": home_time(landing, zone, None),
        "leaveEpochMs": leave.map(epoch_ms),
        "leaveHomeTime": home_time(leave, zone, Some(now)),
        "leaveMinutes": leave.map(|leave| py_round(minutes_between(now, leave))),
        "airport": either(field(arrival, "code"), &json!("")),
        "terminal": field(arrival, "terminal"),
        "gate": field(arrival, "gate"),
        "baggage": field(arrival, "baggage"),
        "done": truthy(field(last, "landed")) || truthy(field(last, "cancelled")),
    })
}

/// What the next run compares against to spot changes.
pub fn snapshot(report: &Value) -> Value {
    let journey = field(report, "journey");
    let pickup = field(report, "pickup");
    let legs: serde_json::Map<String, Value> = field(report, "legs")
        .as_array()
        .into_iter()
        .flatten()
        .map(|leg| {
            let state = json!({
                "phase": field(leg, "phase"),
                "remaining": field(field(leg, "progress"), "remainingMinutes"),
                "delay": safe_int(field(leg, "delayMinutes")),
                "gate": field(field(leg, "departure"), "gate"),
            });
            (py_str(field(leg, "key")), state)
        })
        .collect();
    json!({
        "stage": field(journey, "stage"),
        "leaveEpochMs": field(pickup, "leaveEpochMs"),
        "leaveMinutes": field(pickup, "leaveMinutes"),
        "legs": legs,
    })
}

/// New notifications in order, each key at most once per trip.
struct Announcements {
    events: Vec<Value>,
    fired: BTreeSet<String>,
}

impl Announcements {
    fn add(&mut self, key: String, title: String, body: String, urgency: &str) {
        if !self.fired.contains(&key) {
            self.events
                .push(json!({"key": key, "title": title, "body": body, "urgency": urgency}));
            self.fired.insert(key);
        }
    }
}

/// The notifications between the previous snapshot and this report, and the keys fired so far.
pub fn notification_events(
    previous: &Value,
    report: &Value,
    already_fired: BTreeSet<String>,
) -> (Vec<Value>, BTreeSet<String>) {
    let current = snapshot(report);
    if !truthy(previous) {
        return (Vec::new(), already_fired);
    }
    let mut out = Announcements {
        events: Vec::new(),
        fired: already_fired,
    };
    let is_airborne = |phase: &Value| {
        phase
            .as_str()
            .is_some_and(|phase| AIRBORNE.contains(&phase))
    };

    let previous_legs = field(previous, "legs");
    for leg in field(report, "legs").as_array().into_iter().flatten() {
        let key = py_str(field(leg, "key"));
        let before = field(previous_legs, &key);
        if !truthy(before) {
            continue;
        }
        let code = py_str(field(leg, "code"));
        let (old_phase, new_phase) = (field(before, "phase"), field(leg, "phase"));
        let arrival = field(leg, "arrival");
        let destination = py_str(either(
            either(field(arrival, "city"), field(arrival, "code")),
            &json!("destination"),
        ));
        let home_arrival = field(arrival, "homeTime");
        let suffix = if truthy(home_arrival) {
            format!(" · {} at home", py_str(home_arrival))
        } else {
            String::new()
        };

        if !is_airborne(old_phase) && is_airborne(new_phase) {
            out.add(
                format!("{key}:departed"),
                format!("{code} is airborne"),
                format!("Now heading to {destination}{suffix}"),
                "normal",
            );
        }
        if old_phase != "landed" && new_phase == "landed" {
            out.add(
                format!("{key}:landed"),
                format!("{code} has landed"),
                format!("Arrived in {destination}"),
                "critical",
            );
        }
        if old_phase != "cancelled" && new_phase == "cancelled" {
            let description = field(leg, "description");
            let body = if truthy(description) {
                py_str(description)
            } else {
                format!(
                    "Check with {}",
                    py_str(either(field(leg, "airline"), &json!("the airline")))
                )
            };
            out.add(
                format!("{key}:cancelled"),
                format!("{code} was cancelled"),
                body,
                "critical",
            );
        }

        let old_remaining = number(field(before, "remaining"));
        let new_remaining = number(field(field(leg, "progress"), "remainingMinutes"));
        if let (true, Some(old_remaining), Some(new_remaining)) =
            (is_airborne(new_phase), old_remaining, new_remaining)
        {
            for threshold in [60, 30, 15] {
                if old_remaining > f64::from(threshold) && f64::from(threshold) >= new_remaining {
                    let urgency = if threshold == 15 {
                        "critical"
                    } else {
                        "normal"
                    };
                    out.add(
                        format!("{key}:arrival:{threshold}"),
                        format!("{code} is nearing {destination}"),
                        format!("About {threshold} minutes to landing{suffix}"),
                        urgency,
                    );
                }
            }
        }

        let old_delay = safe_int(field(before, "delay"));
        let new_delay = safe_int(field(leg, "delayMinutes"));
        if old_delay < 30 && 30 <= new_delay {
            out.add(
                format!("{key}:delay:{}", new_delay.div_euclid(30)),
                format!("{code} is delayed"),
                format!("Current delay: about {new_delay} minutes"),
                "critical",
            );
        }

        let departure = field(leg, "departure");
        let old_gate = str_or_empty(field(before, "gate"));
        let new_gate = str_or_empty(field(departure, "gate"));
        let departure_epoch = field(departure, "epochMs");
        let minutes_to_departure = truthy_number(departure_epoch)
            .zip(number(field(report, "generatedAtMs")))
            .map(|(departure, generated)| (departure - generated) / 60_000.0);
        if !new_gate.is_empty()
            && new_gate != old_gate
            && minutes_to_departure.is_some_and(|minutes| (-30.0..=240.0).contains(&minutes))
        {
            let verb = if old_gate.is_empty() {
                "is"
            } else {
                "changed to"
            };
            let terminal = py_str(either(field(departure, "terminal"), &json!("—")));
            out.add(
                format!("{key}:gate:{new_gate}"),
                format!("{code} gate {verb} {new_gate}"),
                format!("Terminal {terminal}"),
                "normal",
            );
        }
    }

    let journey = field(report, "journey");
    let via = py_str(either(
        field(journey, "via"),
        &json!("the transfer airport"),
    ));
    let destination = py_str(either(field(journey, "destination"), &json!("destination")));
    let (old_stage, new_stage) = (field(previous, "stage"), field(&current, "stage"));
    let active_index = safe_int(field(journey, "activeLegIndex"));
    if old_stage != "connection" && new_stage == "connection" {
        let legs = field(report, "legs")
            .as_array()
            .map(Vec::as_slice)
            .unwrap_or_default();
        let onward = usize::try_from(active_index)
            .ok()
            .and_then(|index| legs.get(index))
            .map_or_else(|| destination.clone(), |leg| city(field(leg, "arrival")));
        out.add(
            format!("journey:connection:{active_index}"),
            format!("Landed in {via}"),
            format!("Transfer: next flight to {onward} is now active"),
            "critical",
        );
    }
    let pickup = field(report, "pickup");
    if old_stage != "complete" && new_stage == "complete" {
        let baggage = field(pickup, "baggage");
        let belt = if truthy(baggage) {
            format!(" · belt {}", py_str(baggage))
        } else {
            String::new()
        };
        out.add(
            "journey:complete".to_owned(),
            "Journey complete".to_owned(),
            format!("Arrived in {destination}{belt}"),
            "critical",
        );
    }

    // One warning per transfer; the fired set keeps it from repeating.
    if let (true, Some(connection)) = (
        truthy(field(journey, "tightConnection")),
        integer(field(journey, "connectionMinutes")),
    ) {
        out.add(
            format!(
                "journey:tight-connection:{}",
                py_str(field(journey, "connectionLegIndex"))
            ),
            format!("Tight connection in {via}"),
            format!("Only about {connection} minutes to transfer"),
            "critical",
        );
    }

    let done = truthy(field(pickup, "done"));
    let leave_ms = field(pickup, "leaveEpochMs");
    let old_leave_ms = field(previous, "leaveEpochMs");
    let leave_time = str_or_empty(field(pickup, "leaveHomeTime"));
    let landing_time = py_str(either(field(pickup, "landingHomeTime"), &json!("—")));
    if let (false, Some(leave), Some(old_leave)) =
        (done, truthy_number(leave_ms), truthy_number(old_leave_ms))
        && (leave - old_leave).abs() >= PICKUP_SHIFT_MINUTES * 60_000.0
    {
        // The landing estimate moved: say so and re-arm the leave reminders.
        out.fired.remove("pickup:15");
        out.fired.remove("pickup:now");
        let later = if leave > old_leave {
            "later"
        } else {
            "earlier"
        };
        let minute = (leave / 60_000.0).floor() as i64;
        out.add(
            format!("pickup:moved:{minute}"),
            format!("Leave {later}: by {leave_time}"),
            format!("Landing now expected {landing_time}"),
            "normal",
        );
    }
    if let (false, Some(old_minutes), Some(new_minutes)) = (
        done,
        integer(field(previous, "leaveMinutes")),
        integer(field(&current, "leaveMinutes")),
    ) {
        let terminal = field(pickup, "terminal");
        let terminal = if truthy(terminal) {
            format!("T{}", py_str(terminal))
        } else {
            String::new()
        };
        let airport = str_or_empty(field(pickup, "airport"));
        let place = [airport, terminal]
            .into_iter()
            .filter(|part| !part.is_empty())
            .collect::<Vec<_>>()
            .join(" · ");
        if old_minutes > 0 && 0 >= new_minutes {
            out.add(
                "pickup:now".to_owned(),
                "Time to leave".to_owned(),
                format!("Landing {landing_time} at {place}"),
                "critical",
            );
        } else if old_minutes > 15 && 15 >= new_minutes {
            out.add(
                "pickup:15".to_owned(),
                format!("Leave in {new_minutes} min"),
                format!("Leave by {leave_time} · landing {landing_time}"),
                "normal",
            );
        }
    }
    (out.events, out.fired)
}

#[cfg(test)]
mod tests {
    //! The behaviour checks of the Python backend's test suite, carried over.

    use super::*;

    fn leg(key: &str, phase: &str, remaining: Option<i64>, landed: bool, departed: bool) -> Value {
        json!({
            "key": key,
            "code": key.split('@').next(),
            "phase": phase,
            "departed": departed,
            "landed": landed,
            "cancelled": false,
            "description": "On time",
            "delayMinutes": 0,
            "late": false,
            "departure": {"code": "FRA", "city": "Frankfurt", "gate": null, "terminal": "1", "epochMs": 1000, "homeTime": "16:00"},
            "arrival": {"code": "JFK", "city": "New York", "gate": null, "terminal": "3", "epochMs": 2000, "homeTime": "22:00"},
            "progress": {"remainingMinutes": remaining, "etaEpochMs": 2000, "percent": 50},
        })
    }

    /// One leg per phase along Boston → Chicago → Denver → Seattle, each transfer 60 minutes.
    fn chain(phases: &[&str]) -> Vec<Value> {
        let cities = ["Boston", "Chicago", "Denver", "Seattle"];
        phases
            .iter()
            .enumerate()
            .map(|(index, phase)| {
                let departed = !["scheduled", "cancelled"].contains(phase);
                let mut item = leg(
                    &format!("AA{}00@2026-07-15", index + 1),
                    phase,
                    None,
                    *phase == "landed",
                    departed,
                );
                item["cancelled"] = json!(*phase == "cancelled");
                item["departure"]["city"] = json!(cities[index]);
                item["arrival"]["city"] = json!(cities[index + 1]);
                item["departure"]["epochMs"] = json!(2 * index as i64 * 3_600_000);
                item["arrival"]["epochMs"] = json!((2 * index as i64 + 1) * 3_600_000);
                item
            })
            .collect()
    }

    fn at(text: &str) -> Timestamp {
        text.parse().expect("test times are valid")
    }

    fn keys(events: &[Value]) -> Vec<&str> {
        events
            .iter()
            .map(|event| event["key"].as_str().unwrap_or_default())
            .collect()
    }

    fn zone(name: &str) -> Zone {
        home_zone(name, "")
    }

    fn spec(key: &str) -> LegSpec {
        parse_leg_specs(key).remove(0)
    }

    #[test]
    fn three_leg_trip_reaches_the_second_transfer() {
        let journey = journey_from_legs(
            &chain(&["landed", "landed", "scheduled"]),
            Timestamp::UNIX_EPOCH,
        );
        assert_eq!(
            (
                &journey["stage"],
                &journey["activeLegIndex"],
                &journey["label"],
                &journey["connectionMinutes"],
                &journey["destination"]
            ),
            (
                &json!("connection"),
                &json!(2),
                &json!("Transfer in Denver"),
                &json!(60),
                &json!("Seattle")
            ),
        );
    }

    #[test]
    fn middle_leg_in_the_air_looks_ahead_to_the_next_transfer() {
        let journey = journey_from_legs(
            &chain(&["landed", "airborne", "scheduled"]),
            Timestamp::UNIX_EPOCH,
        );
        assert_eq!(
            (
                &journey["stage"],
                &journey["activeLegIndex"],
                &journey["label"],
                &journey["via"],
                &journey["connectionLegIndex"]
            ),
            (
                &json!("en-route"),
                &json!(1),
                &json!("En route to Denver"),
                &json!("Denver"),
                &json!(2)
            ),
        );
    }

    #[test]
    fn a_cancelled_later_leg_disrupts_the_journey() {
        let journey = journey_from_legs(
            &chain(&["airborne", "scheduled", "cancelled"]),
            Timestamp::UNIX_EPOCH,
        );
        assert_eq!(
            (
                &journey["stage"],
                &journey["activeLegIndex"],
                &journey["connectionMinutes"]
            ),
            (&json!("disrupted"), &json!(2), &Value::Null),
        );
    }

    #[test]
    fn each_transfer_notifies_once() {
        let mut legs = chain(&["landed", "landed", "scheduled"]);
        legs[2]["departure"]["epochMs"] = json!(4 * 3_600_000 - 30 * 60_000); // 30 minutes to change planes
        let journey = journey_from_legs(&legs, Timestamp::UNIX_EPOCH);
        let report = json!({"generatedAtMs": 0, "legs": legs, "journey": journey});
        let previous = json!({"stage": "en-route", "legs": {}});
        // The first transfer was tight too; the second one still gets its own warning.
        let fired = BTreeSet::from([
            "journey:connection:1".to_owned(),
            "journey:tight-connection:1".to_owned(),
        ]);
        let (events, _) = notification_events(&previous, &report, fired);
        assert_eq!(
            keys(&events),
            ["journey:connection:2", "journey:tight-connection:2"]
        );
        assert_eq!(events[0]["title"], "Landed in Denver");
        assert_eq!(
            events[0]["body"],
            "Transfer: next flight to Seattle is now active"
        );
    }

    #[test]
    fn departure_icon_does_not_start_110_minutes_early() {
        let now = at("2026-07-15T20:50:00Z");
        let flight = Situation {
            start: Some(at("2026-07-15T22:40:00Z")),
            ..Situation::default()
        };
        assert_eq!(flight.phase(now), "scheduled");
    }

    #[test]
    fn predeparture_states_only_describe_the_near_future() {
        let now = at("2026-07-15T20:00:00Z");
        for (minutes, wanted) in [
            (61, "scheduled"),
            (60, "boarding-soon"),
            (31, "boarding-soon"),
            (30, "departing-soon"),
            (1, "departing-soon"),
            (0, "awaiting-departure"),
        ] {
            let start = now
                .checked_add(jiff::SignedDuration::from_mins(minutes))
                .ok();
            assert_eq!(
                Situation {
                    start,
                    ..Situation::default()
                }
                .phase(now),
                wanted,
                "{minutes} minutes before"
            );
        }
    }

    #[test]
    fn parses_dated_legs_and_discards_duplicates() {
        let result = parse_leg_specs(
            " lh 400@2026-07-15, LH401@2026-07-16,LH400@2026-07-15,LH402@2026-02-30 ",
        );
        let found: Vec<&str> = result.iter().map(|spec| spec.key.as_str()).collect();
        assert_eq!(found, ["LH400@2026-07-15", "LH401@2026-07-16"]);
    }

    #[test]
    fn rejects_codes_and_dates_the_pattern_does_not_allow() {
        // Expected values checked against the Python backend's regex.
        for raw in [
            "L4@2026-07-15",
            "LHAB4@2026-07-15",
            "LH4@2026-7-15",
            "LH4@0000-01-01",
            "LH4@@2026-07-15",
        ] {
            assert!(parse_leg_specs(raw).is_empty(), "{raw} should not parse");
        }
        for (raw, key) in [
            ("lha4@2026-07-15", "LHA4@2026-07-15"),
            ("L400@2026-07-15", "L400@2026-07-15"),
            ("LH40000@2026-07-15", "LH40000@2026-07-15"),
            ("1234@2026-07-15", "1234@2026-07-15"),
        ] {
            assert_eq!(spec(raw).key, key);
        }
    }

    #[test]
    fn flight_url_splits_the_airline_from_the_number() {
        assert_eq!(
            flight_url(&spec("LH400@2026-12-03")),
            "https://www.flightstats.com/v2/flight-tracker/LH/400?year=2026&month=12&date=3"
        );
        assert!(flight_url(&spec("U2123@2026-12-03")).contains("/U2/123?"));
        assert!(flight_url(&spec("SWR8@2026-12-03")).contains("/SWR/8?"));
    }

    #[test]
    fn labels_and_transfer_come_from_leg_cities() {
        let mut first = leg("AA100@2026-07-15", "landed", Some(0), true, true);
        let mut second = leg("AA200@2026-07-16", "scheduled", None, false, false);
        first["departure"]["city"] = json!("Boston");
        first["arrival"]["city"] = json!("Chicago");
        second["arrival"]["city"] = json!("Denver");
        first["arrival"]["epochMs"] = json!(60_000);
        second["departure"]["epochMs"] = json!(46 * 60_000);
        let journey = journey_from_legs(&[first, second], Timestamp::UNIX_EPOCH);
        assert_eq!(journey["label"], "Transfer in Chicago");
        assert_eq!(
            (
                &journey["destination"],
                &journey["connectionMinutes"],
                &journey["tightConnection"]
            ),
            (&json!("Denver"), &json!(45), &json!(true)),
        );
    }

    #[test]
    fn leave_reminders_fire_once_and_rearm_when_landing_moves() {
        let zone = zone("Europe/Berlin");
        let landing = at("2026-07-15T10:45:00Z"); // 12:45 CEST
        let mut last = leg("LH400@2026-07-15", "airborne", Some(100), false, true);
        last["arrival"]["epochMs"] = json!(epoch_ms(landing));
        last["arrival"]["homeTime"] = json!("12:45");
        let minutes_before = |minutes: i64| {
            landing
                .checked_sub(jiff::SignedDuration::from_mins(minutes))
                .expect("in range")
        };
        let report_at = |leg: &Value, now: Timestamp| json!({"generatedAtMs": 0, "legs": [], "journey": {}, "pickup": pickup_for(std::slice::from_ref(leg), 60, now, &zone)});

        let before = report_at(&last, minutes_before(80));
        assert_eq!(before["pickup"]["leaveHomeTime"], "11:45");
        let day_before = pickup_for(
            std::slice::from_ref(&last),
            60,
            minutes_before(24 * 60),
            &zone,
        );
        assert_eq!(day_before["leaveHomeTime"], "Wed 11:45");
        let soon = report_at(&last, minutes_before(74));
        let (events, fired) = notification_events(&snapshot(&before), &soon, BTreeSet::new());
        assert_eq!(keys(&events), ["pickup:15"]);
        assert!(
            notification_events(&snapshot(&before), &soon, fired.clone())
                .0
                .is_empty()
        );

        let now_report = report_at(&last, minutes_before(60));
        let (events, fired) = notification_events(&snapshot(&soon), &now_report, fired);
        assert_eq!(keys(&events), ["pickup:now"]);

        last["arrival"]["epochMs"] = json!(epoch_ms(landing) + 40 * 60_000); // 40 min delay
        let delayed = report_at(&last, minutes_before(59));
        let (events, fired) = notification_events(&snapshot(&now_report), &delayed, fired);
        assert_eq!(events[0]["title"], "Leave later: by 12:25");
        assert!(!fired.contains("pickup:15"));
    }

    #[test]
    fn switches_to_connection_after_first_landing() {
        let first = leg("LH400@2026-07-15", "landed", Some(0), true, true);
        let second = leg("LH401@2026-07-16", "scheduled", None, false, false);
        let journey = journey_from_legs(&[first, second], Timestamp::UNIX_EPOCH);
        assert_eq!(
            (&journey["stage"], &journey["activeLegIndex"]),
            (&json!("connection"), &json!(1))
        );
    }

    #[test]
    fn single_landed_leg_completes_the_journey() {
        let journey = journey_from_legs(
            &[leg("LH400@2026-07-15", "landed", Some(0), true, true)],
            Timestamp::UNIX_EPOCH,
        );
        assert_eq!(
            (&journey["stage"], &journey["label"]),
            (&json!("complete"), &json!("Arrived in New York"))
        );
    }

    #[test]
    fn cancelled_leg_stops_countdown_and_connection() {
        let mut first = leg("LH400@2026-07-15", "cancelled", Some(30), false, true);
        first["cancelled"] = json!(true);
        let journey = journey_from_legs(
            &[
                first,
                leg("LH401@2026-07-16", "scheduled", None, false, false),
            ],
            Timestamp::UNIX_EPOCH,
        );
        assert_eq!(
            (
                &journey["stage"],
                &journey["nextEventKind"],
                &journey["nextEventMinutes"],
                &journey["connectionMinutes"]
            ),
            (
                &json!("disrupted"),
                &json!("cancelled"),
                &Value::Null,
                &Value::Null
            ),
        );
    }

    #[test]
    fn delay_alone_is_not_an_alert() {
        let (now, zone) = (at("2026-07-15T12:00:00Z"), zone("Europe/Berlin"));
        let delayed = json!({"status": {"delay": {"arrival": {"minutes": 45}}}});
        assert_eq!(
            normalize_flightstats(&spec("LH400@2026-07-15"), &delayed, now, &zone)["alert"],
            false
        );
        let cancelled = json!({"flightNote": {"canceled": true}});
        assert_eq!(
            normalize_flightstats(&spec("LH400@2026-07-15"), &cancelled, now, &zone)["alert"],
            true
        );
    }

    #[test]
    fn home_zone_defaults_to_the_system_zone() {
        assert_eq!(home_zone("", "America/New_York").name, "America/New_York");
        assert_eq!(
            home_zone("Mars/Base", "America/New_York").name,
            "America/New_York"
        );
        assert_eq!(
            home_zone("Asia/Tokyo", "America/New_York").name,
            "Asia/Tokyo"
        );
        assert_eq!(home_zone("", "").name, "UTC");
    }

    #[test]
    fn home_zone_names_must_be_written_like_zoneinfo_keys() {
        for name in [
            "europe/berlin",
            "Europe//Berlin",
            "./Europe/Berlin",
            "/etc/localtime",
            "../zoneinfo/UTC",
            "zone.tab",
        ] {
            assert_eq!(home_zone(name, "").name, "UTC", "{name}");
        }
        assert_eq!(
            home_zone("posix/Europe/Berlin", "").name,
            "posix/Europe/Berlin"
        );
    }

    #[test]
    fn home_time_follows_daylight_saving_and_the_day() {
        let berlin = zone("Europe/Berlin");
        // The last Sunday of March: 01:30 UTC is 03:30 CEST, an hour after 00:30 UTC was 01:30 CET.
        assert_eq!(
            home_time(Some(at("2026-03-29T00:30:00Z")), &berlin, None),
            "01:30"
        );
        assert_eq!(
            home_time(Some(at("2026-03-29T01:30:00Z")), &berlin, None),
            "03:30"
        );
        assert_eq!(
            home_time(
                Some(at("2026-03-28T23:30:00Z")),
                &berlin,
                Some(at("2026-03-28T22:00:00Z"))
            ),
            "Sun 00:30"
        );
    }

    #[test]
    fn late_follows_the_arrival_once_airborne() {
        let (now, zone) = (at("2026-07-15T12:00:00Z"), zone("Europe/Berlin"));
        let mut delays = json!({"status": {"delay": {"departure": {"minutes": 40}, "arrival": {"minutes": 10}}}});
        let waiting = normalize_flightstats(&spec("LH400@2026-07-15"), &delays, now, &zone);
        delays["flightNote"] = json!({"hasDepartedRunway": true});
        let airborne = normalize_flightstats(&spec("LH400@2026-07-15"), &delays, now, &zone);
        assert_eq!(
            (&waiting["delayMinutes"], &waiting["late"]),
            (&json!(40), &json!(true))
        );
        assert_eq!(
            (&airborne["delayMinutes"], &airborne["late"]),
            (&json!(10), &json!(false))
        );
    }

    #[test]
    fn emits_each_landing_threshold_once() {
        let current = leg("LH400@2026-07-15", "arriving", Some(58), false, true);
        let report =
            json!({"generatedAtMs": 0, "legs": [current], "journey": {"stage": "en-route"}});
        let previous = json!({"stage": "en-route", "legs": {"LH400@2026-07-15": {"phase": "airborne", "remaining": 62, "delay": 0, "gate": null}}});
        let (events, fired) = notification_events(&previous, &report, BTreeSet::new());
        assert_eq!(keys(&events), ["LH400@2026-07-15:arrival:60"]);
        assert!(notification_events(&previous, &report, fired).0.is_empty());
    }

    #[test]
    fn landing_emits_leg_and_journey_notifications() {
        let mut current = leg("LH401@2026-07-16", "landed", Some(0), true, true);
        current["arrival"]["city"] = json!("Frankfurt");
        let report =
            json!({"generatedAtMs": 0, "legs": [current], "journey": {"stage": "complete"}});
        let previous = json!({"stage": "en-route", "legs": {"LH401@2026-07-16": {"phase": "final-approach", "remaining": 4, "delay": 0, "gate": null}}});
        let (events, _) = notification_events(&previous, &report, BTreeSet::new());
        assert_eq!(
            keys(&events),
            ["LH401@2026-07-16:landed", "journey:complete"]
        );
    }

    #[test]
    fn rounds_halves_to_even_like_python() {
        assert_eq!(
            [py_round(0.5), py_round(1.5), py_round(2.5), py_round(-0.5)],
            [0, 2, 2, 0]
        );
        assert_eq!(
            [
                round_tenth(0.25),
                round_tenth(0.35),
                round_tenth(26.75),
                round_tenth(99.95)
            ],
            [0.2, 0.3, 26.8, 100.0]
        );
        assert_eq!(
            epoch_ms(Timestamp::from_microsecond(1_500).expect("valid")),
            2
        );
        assert_eq!(
            epoch_ms(Timestamp::from_microsecond(2_500).expect("valid")),
            2
        );
    }

    #[test]
    fn reads_numbers_like_python_int() {
        assert_eq!(
            [
                safe_int(&json!(" 45 ")),
                safe_int(&json!("4_5")),
                safe_int(&json!(45.9)),
                safe_int(&json!(-45.9)),
                safe_int(&json!(true))
            ],
            [45, 45, 45, -45, 1],
        );
        assert_eq!(
            [
                safe_int(&json!("45.0")),
                safe_int(&json!("4__5")),
                safe_int(&Value::Null),
                safe_int(&json!([1]))
            ],
            [0, 0, 0, 0]
        );
    }

    #[test]
    fn newest_position_keeps_the_first_of_equal_dates() {
        let track = json!({"positions": [
            {"date": "2026-07-15T10:00:00Z", "altitudeFt": 1},
            {"date": "2026-07-15T11:00:00Z", "altitudeFt": 2},
            {"date": "2026-07-15T11:00:00Z", "altitudeFt": 3},
            {"altitudeFt": 4},
            "not a point",
        ]});
        assert_eq!(
            newest_position(&track).map(|point| &point["altitudeFt"]),
            Some(&json!(2))
        );
    }

    #[test]
    fn cached_legs_count_down_again() {
        let mut record =
            json!({"landed": false, "progress": {"etaEpochMs": 3_600_000, "percent": 40.0}});
        refresh_relative_fields(&mut record, Timestamp::from_second(30 * 60).expect("valid"));
        assert_eq!(
            record["progress"],
            json!({"etaEpochMs": 3_600_000, "percent": 40.0, "remainingMinutes": 30})
        );
        let mut landed = json!({"landed": true, "progress": null});
        refresh_relative_fields(&mut landed, Timestamp::UNIX_EPOCH);
        assert_eq!(
            landed["progress"],
            json!({"remainingMinutes": 0, "percent": 100})
        );
    }
}

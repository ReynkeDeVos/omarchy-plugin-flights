//! Track a dated, multi-leg journey for the Omarchy flights plugin.
//!
//! Prints one JSON document per run on stdout and exits; the widget runs it
//! again on its next refresh. Diagnostics go to stderr.

mod feeds;
mod journey;

use std::collections::BTreeSet;
use std::env;
use std::fs::{self, File};
use std::process::ExitCode;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::thread;

use jiff::Timestamp;
use serde_json::{Value, json};

use feeds::{Feeds, cache_path, haversine_nm, read_json, write_json, xdg_dir};
use journey::{
    DEFAULT_LEAVE_LEAD_MINUTES, LegSpec, Situation, Zone, field, from_epoch_ms, home_zone,
    journey_from_legs, number, parse_leg_specs, pickup_for, py_int, py_str, snapshot, str_or_empty,
    truthy,
};

const MAX_PARALLEL_LEGS: usize = 4;
const CACHE_NOTE: &str = "Showing the last schedule with live aircraft data when available";
const USAGE: &str = "usage: flight-status [-h] (--legs LEGS | --lookup FLIGHT@DATE) [--home-timezone HOME_TIMEZONE]\n                     [--leave-lead-minutes LEAVE_LEAD_MINUTES]";
const HELP: &str = "
Track a dated, multi-leg journey for the Omarchy flights plugin.

options:
  -h, --help            show this help message and exit
  --legs LEGS           comma-separated flight@date values
  --lookup FLIGHT@DATE  print one leg and leave the trip state alone
  --home-timezone HOME_TIMEZONE
                        IANA zone for home clock times; the system's by default
  --leave-lead-minutes LEAVE_LEAD_MINUTES";
const OPTIONS: [&str; 5] = [
    "--help",
    "--legs",
    "--lookup",
    "--home-timezone",
    "--leave-lead-minutes",
];

#[derive(Debug, PartialEq)]
struct Options {
    legs: Option<String>,
    lookup: Option<String>,
    home_timezone: String,
    leave_lead_minutes: i64,
}

/// Everything one run shares between its legs.
struct Context {
    now: Timestamp,
    zone: Zone,
    feeds: Feeds,
}

fn main() -> ExitCode {
    let args: Vec<String> = env::args_os()
        .skip(1)
        .map(|arg| arg.to_string_lossy().into_owned())
        .collect();
    let options = match parse_args(&args) {
        Ok(Some(options)) => options,
        Ok(None) => {
            println!("{USAGE}\n{HELP}");
            return ExitCode::SUCCESS;
        }
        Err(message) => {
            eprintln!("{USAGE}\nflight-status: error: {message}");
            return ExitCode::from(2);
        }
    };
    let context = Context {
        now: now(),
        zone: home_zone(&options.home_timezone, &system_zone()),
        // Offline tests point every feed at a local stand-in.
        feeds: Feeds::new(
            env::var("FLIGHTS_TEST_ORIGIN")
                .ok()
                .filter(|origin| !origin.is_empty()),
        ),
    };
    if let Some(lookup) = &options.lookup {
        let leg = match parse_leg_specs(lookup).first() {
            Some(spec) => fetch_leg(spec, &context),
            None => {
                json!({"phase": "unknown", "error": "Use a flight number and date like LH400@2026-12-23"})
            }
        };
        println!("{leg}");
        return ExitCode::SUCCESS;
    }
    let specs = parse_leg_specs(options.legs.as_deref().unwrap_or_default());
    if specs.is_empty() {
        eprintln!(
            "{USAGE}\nflight-status: error: --legs has no valid flight@date value, e.g. LH400@2026-12-23"
        );
        return ExitCode::from(2);
    }
    match track(&specs, options.leave_lead_minutes, &context) {
        Ok(report) => {
            println!("{report}");
            ExitCode::SUCCESS
        }
        Err(error) => {
            eprintln!("flight-status: {error}");
            ExitCode::FAILURE
        }
    }
}

/// `argparse` as the Python backend had it: same options, prefixes, conflicts and errors.
/// `Ok(None)` asks for the help text.
fn parse_args(args: &[String]) -> Result<Option<Options>, String> {
    let mut options = Options {
        legs: None,
        lookup: None,
        home_timezone: String::new(),
        leave_lead_minutes: DEFAULT_LEAVE_LEAD_MINUTES,
    };
    let mut unrecognized: Vec<&str> = Vec::new();
    let mut rest = args.iter();
    while let Some(arg) = rest.next() {
        if arg == "--" {
            unrecognized.extend(rest.by_ref().map(String::as_str));
            break;
        }
        let Some((name, inline)) = option_name(arg)? else {
            unrecognized.push(arg);
            continue;
        };
        if name == "--help" {
            return Ok(None);
        }
        let value = match inline {
            Some(value) => value,
            None => match rest.next() {
                Some(next) if !looks_like_option(next) => next.clone(),
                _ => return Err(format!("argument {name}: expected one argument")),
            },
        };
        match name {
            "--legs" if options.lookup.is_some() => {
                return Err("argument --legs: not allowed with argument --lookup".to_owned());
            }
            "--lookup" if options.legs.is_some() => {
                return Err("argument --lookup: not allowed with argument --legs".to_owned());
            }
            "--legs" => options.legs = Some(value),
            "--lookup" => options.lookup = Some(value),
            "--home-timezone" => options.home_timezone = value,
            _ => {
                options.leave_lead_minutes = py_int(&value).ok_or_else(|| {
                    format!("argument --leave-lead-minutes: invalid int value: '{value}'")
                })?;
            }
        }
    }
    if options.legs.is_none() && options.lookup.is_none() {
        return Err("one of the arguments --legs --lookup is required".to_owned());
    }
    if !unrecognized.is_empty() {
        return Err(format!(
            "unrecognized arguments: {}",
            unrecognized.join(" ")
        ));
    }
    Ok(Some(options))
}

/// The option an argument names, by exact name, `--name=value` or a unique prefix.
fn option_name(arg: &str) -> Result<Option<(&'static str, Option<String>)>, String> {
    if arg == "-h" {
        return Ok(Some(("--help", None)));
    }
    if !arg.starts_with("--") || arg.len() == 2 {
        return Ok(None);
    }
    let (name, inline) = match arg.split_once('=') {
        Some((name, value)) => (name, Some(value.to_owned())),
        None => (arg, None),
    };
    if let Some(exact) = OPTIONS.iter().find(|option| **option == name) {
        return Ok(Some((exact, inline)));
    }
    let matches: Vec<&'static str> = OPTIONS
        .iter()
        .copied()
        .filter(|option| option.starts_with(name))
        .collect();
    match matches.as_slice() {
        [single] => Ok(Some((single, inline))),
        [] => Ok(None),
        several => Err(format!(
            "ambiguous option: {name} could match {}",
            several.join(", ")
        )),
    }
}

/// Whether argparse would read `arg` as an option rather than a value.
fn looks_like_option(arg: &str) -> bool {
    if !arg.starts_with('-') || arg == "-" {
        return false;
    }
    if arg == "-h" || option_name(arg).map_or(true, |option| option.is_some()) {
        return true;
    }
    let digits = &arg[1..];
    let negative_number = !digits.is_empty()
        && digits.bytes().filter(|byte| *byte == b'.').count() <= 1
        && !digits.ends_with('.')
        && digits
            .bytes()
            .all(|byte| byte.is_ascii_digit() || byte == b'.');
    !negative_number && !arg.contains(' ')
}

/// The clock, or `FLIGHTS_TEST_NOW_MS` in offline tests; microseconds like Python's datetime.
fn now() -> Timestamp {
    let fixed = env::var("FLIGHTS_TEST_NOW_MS")
        .ok()
        .and_then(|ms| ms.parse().ok())
        .and_then(|ms| Timestamp::from_millisecond(ms).ok());
    let now = fixed.unwrap_or_else(Timestamp::now);
    Timestamp::from_microsecond(now.as_microsecond()).unwrap_or(now)
}

/// `TZ` without its leading colon, else the zone `/etc/localtime` points into.
fn system_zone() -> String {
    let from_env = env::var("TZ")
        .unwrap_or_default()
        .trim_start_matches(':')
        .to_owned();
    if !from_env.is_empty() {
        return from_env;
    }
    fs::canonicalize("/etc/localtime")
        .ok()
        .and_then(|path| {
            path.to_str()
                .and_then(|path| path.split_once("zoneinfo/"))
                .map(|(_, zone)| zone.to_owned())
        })
        .unwrap_or_default()
}

fn journey_cache(spec: &LegSpec) -> std::path::PathBuf {
    cache_path("journey", &format!("{}-{}", spec.code, spec.date))
}

/// One leg: fresh from FlightStats, else the last good record, else a placeholder.
fn fetch_leg(spec: &LegSpec, context: &Context) -> Value {
    let reason = match context.feeds.flightstats_flight(&journey::flight_url(spec)) {
        Ok(flight) => {
            let mut record =
                journey::normalize_flightstats(spec, &flight, context.now, &context.zone);
            enrich_with_adsb(&mut record, context);
            write_json(&journey_cache(spec), &record);
            return record;
        }
        Err(reason) => reason,
    };
    match read_json(&journey_cache(spec), None) {
        Some(mut cached) if cached.is_object() => {
            cached["error"] = json!(reason);
            cached["description"] = json!(CACHE_NOTE);
            journey::refresh_relative_fields(&mut cached, context.now);
            enrich_with_adsb(&mut cached, context);
            cached
        }
        _ => journey::fallback_record(spec, &reason),
    }
}

/// Live position, progress and phase from ADS-B while a leg is in the air.
fn enrich_with_adsb(record: &mut Value, context: &Context) {
    let [departed, landed, cancelled] =
        ["departed", "landed", "cancelled"].map(|name| truthy(field(record, name)));
    if !departed || landed || cancelled {
        return;
    }
    let preferred = str_or_empty(field(field(record, "aircraft"), "callsign"));
    let (Some(aircraft), route) = context
        .feeds
        .find_aircraft(&str_or_empty(field(record, "code")), &preferred)
    else {
        return;
    };

    let altitude = field(&aircraft, "alt_baro");
    let altitude = number(altitude)
        .map(f64::trunc)
        .or_else(|| (py_str(altitude).to_lowercase() == "ground").then_some(0.0));
    let vertical_rate = number(field(&aircraft, "baro_rate")).map(f64::trunc);
    let live_callsign = str_or_empty(field(&aircraft, "flight"))
        .trim()
        .to_uppercase();
    let hex = field(&aircraft, "hex");
    record["aircraft"] = json!({
        "callsign": if live_callsign.is_empty() { preferred } else { live_callsign },
        "hex": if truthy(hex) { hex.clone() } else { Value::Null },
    });

    let route = route.unwrap_or(Value::Null);
    let point = |node: &Value| field(node, "lat").as_f64().zip(field(node, "lon").as_f64());
    let (origin, destination) = (field(&route, "origin"), field(&route, "destination"));
    if let (true, true, Some(from), Some(to), Some(here)) = (
        truthy(origin),
        truthy(destination),
        point(origin),
        point(destination),
        point(&aircraft),
    ) {
        let flown = haversine_nm(from.0, from.1, here.0, here.1);
        let remaining = haversine_nm(here.0, here.1, to.0, to.1);
        if flown + remaining > 0.0 {
            record["progress"]["percent"] =
                json!(journey::round_tenth(100.0 * flown / (flown + remaining)));
        }
    }

    let eta = field(field(record, "progress"), "etaEpochMs");
    let situation = Situation {
        cancelled,
        landed,
        departed,
        start: from_epoch_ms(field(field(record, "departure"), "epochMs")),
        end: from_epoch_ms(eta),
        vertical_rate,
        altitude,
    };
    record["phase"] = json!(situation.phase(context.now));
}

/// Fetches every leg (at most four at once, results in trip order).
fn fetch_all(specs: &[LegSpec], context: &Context) -> Vec<Value> {
    let next = AtomicUsize::new(0);
    let mut done: Vec<(usize, Value)> = thread::scope(|scope| {
        let workers: Vec<_> = (0..specs.len().min(MAX_PARALLEL_LEGS))
            .map(|_| {
                scope.spawn(|| {
                    let mut fetched = Vec::new();
                    loop {
                        // Relaxed is enough: the counter only hands out indexes, it guards no other data.
                        let index = next.fetch_add(1, Ordering::Relaxed);
                        let Some(spec) = specs.get(index) else { break };
                        fetched.push((index, fetch_leg(spec, context)));
                    }
                    fetched
                })
            })
            .collect();
        workers
            .into_iter()
            .flat_map(|worker| {
                worker
                    .join()
                    .unwrap_or_else(|panic| std::panic::resume_unwind(panic))
            })
            .collect()
    });
    done.sort_by_key(|(index, _)| *index);
    done.into_iter().map(|(_, leg)| leg).collect()
}

/// The whole trip, with the notifications this run is the first to see.
fn track(specs: &[LegSpec], lead_minutes: i64, context: &Context) -> std::io::Result<Value> {
    let legs = fetch_all(specs, context);
    let errors: Vec<String> = legs
        .iter()
        .filter(|leg| truthy(field(leg, "error")) && field(leg, "phase") == "unknown")
        .map(|leg| {
            format!(
                "{}: {}",
                py_str(field(leg, "code")),
                py_str(field(leg, "error"))
            )
        })
        .collect();
    let mut report = json!({
        "generatedAtMs": journey::epoch_ms(context.now),
        "homeTimezone": context.zone.name,
        "journey": journey_from_legs(&legs, context.now),
        "pickup": pickup_for(&legs, lead_minutes, context.now, &context.zone),
        "errors": errors,
    });
    report["legs"] = Value::Array(legs);

    let trip = specs
        .iter()
        .map(|spec| spec.key.as_str())
        .collect::<Vec<_>>()
        .join(",");
    let folder = xdg_dir("XDG_STATE_HOME", ".local/state");
    fs::create_dir_all(&folder)?;
    // One widget per monitor may run this at once; the lock keeps notifications single.
    let lock = File::create(folder.join("state.lock"))?;
    lock.lock()?;
    let state_path = folder.join("state.json");
    let state = read_json(&state_path, None)
        .filter(|state| field(state, "trip") == trip.as_str()) // journey-level keys must not leak into a new trip
        .unwrap_or(Value::Null);
    let fired: BTreeSet<String> = field(&state, "fired")
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|key| key.as_str().map(str::to_owned))
        .collect();
    let (events, fired) = journey::notification_events(field(&state, "snapshot"), &report, fired);
    write_json(
        &state_path,
        &json!({"trip": trip, "snapshot": snapshot(&report), "fired": fired}),
    );
    drop(lock);
    report["events"] = Value::Array(events);
    Ok(report)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(args: &[&str]) -> Result<Option<Options>, String> {
        parse_args(&args.iter().map(|arg| (*arg).to_owned()).collect::<Vec<_>>())
    }

    #[test]
    fn reads_the_options_the_widget_passes() {
        let options = parse(&[
            "--legs",
            "LH400@2026-12-23",
            "--home-timezone",
            "",
            "--leave-lead-minutes",
            "-5",
        ]);
        assert_eq!(
            options,
            Ok(Some(Options {
                legs: Some("LH400@2026-12-23".to_owned()),
                lookup: None,
                home_timezone: String::new(),
                leave_lead_minutes: -5,
            }))
        );
        let short = parse(&["--look=LH400@2026-12-23", "--home", "Asia/Tokyo"])
            .expect("prefixes are fine")
            .expect("not help");
        assert_eq!(
            (short.lookup.as_deref(), short.home_timezone.as_str()),
            (Some("LH400@2026-12-23"), "Asia/Tokyo")
        );
    }

    #[test]
    fn refuses_what_argparse_refused() {
        for args in [
            &[][..],
            &["--legs", "A", "--lookup", "B"],
            &["--legs"],
            &["--legs", "--lookup", "B"],
            &["--l", "A"],
            &["--legs", "A", "extra"],
            &["--legs", "A", "--leave-lead-minutes", "1.5"],
            &["--legs", "A", "--colour", "red"],
        ] {
            assert!(parse(args).is_err(), "{args:?} should be refused");
        }
        assert_eq!(parse(&["--legs", "A", "-h"]), Ok(None));
    }

    #[test]
    fn defaults_agree_across_manifest_qml_and_cli() {
        // Bar widgets don't get the manifest injected, so the QML repeats its defaults.
        let root = env!("CARGO_MANIFEST_DIR");
        let manifest: Value = serde_json::from_str(
            &fs::read_to_string(format!("{root}/manifest.json")).expect("manifest"),
        )
        .expect("manifest is JSON");
        let qml: String = fs::read_dir(root)
            .expect("plugin folder")
            .flatten()
            .filter(|entry| {
                entry
                    .path()
                    .extension()
                    .is_some_and(|extension| extension == "qml")
            })
            .map(|entry| fs::read_to_string(entry.path()).expect("QML is text"))
            .collect();
        let widget = &manifest["barWidget"];
        for item in widget["schema"].as_array().expect("schema") {
            let key = item["key"].as_str().expect("key");
            let value = &widget["defaults"][key];
            assert_eq!(&item["defaultValue"], value, "{key}");
            let call = format!("setting(\"{key}\", ");
            let at = qml
                .find(&call)
                .unwrap_or_else(|| panic!("the QML reads {key}"))
                + call.len();
            let literal = &qml[at..at + qml[at..].find(')').expect("closing parenthesis")];
            assert_eq!(
                &serde_json::from_str::<Value>(literal).expect("a JSON literal"),
                value,
                "{key}"
            );
        }
        assert_eq!(
            widget["defaults"]["leaveLeadMinutes"],
            DEFAULT_LEAVE_LEAD_MINUTES
        );
    }
}

//! The public feeds and their caches: FlightStats pages, adsb.lol positions
//! and adsbdb routes, all without API keys.
//!
//! Public flight numbers and radio callsigns are not always the same: airlines
//! like Emirates or Turkish may transmit alphanumeric callsigns (UAE57X, THY5HX).
//! Lookups try FlightStats' callsign, the last pinned match, then adsbdb's.

use std::env;
use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, SystemTime};

use serde_json::{Value, json};
use ureq::Agent;
use ureq::tls::{RootCerts, TlsConfig};

use crate::journey::{field, str_or_empty, truthy};

const MARKER: &str = "__NEXT_DATA__ = ";
const PAGE_LIMIT: u64 = 2_500_000;
const PAGE_TIMEOUT: Duration = Duration::from_secs(18);
const ADSB_TIMEOUT: Duration = Duration::from_secs(12);
const PAGE_AGENT: &str = "Mozilla/5.0 (X11; Linux x86_64) omarchy-plugin-flights/1.0";
const ADSB_AGENT: &str =
    "omarchy-plugin-flights/1.0 (+https://github.com/ReynkeDeVos/omarchy-plugin-flights)";
const ADSB_URL: &str = "https://api.adsb.lol/v2";
const ROUTES_URL: &str = "https://api.adsbdb.com/v0";
const ROUTE_TTL: Duration = Duration::from_secs(7 * 24 * 3600);
const PIN_TTL: Duration = Duration::from_secs(3 * 3600);

/// HTTP to the three feeds. `origin` replaces scheme and host, for offline tests.
pub struct Feeds {
    agent: Agent,
    origin: Option<String>,
}

impl Feeds {
    pub fn new(origin: Option<String>) -> Self {
        let tls = TlsConfig::builder()
            .root_certs(RootCerts::PlatformVerifier)
            .build();
        // Status codes are checked by hand to report them the way the widget always showed them.
        // No pooling, like Python's urllib: a run is over in a second, and reusing a connection
        // the server has just closed fails the request instead of opening a new one.
        let agent = Agent::config_builder()
            .tls_config(tls)
            .http_status_as_error(false)
            .max_idle_connections(0)
            .build()
            .new_agent();
        Self { agent, origin }
    }

    fn get(
        &self,
        url: &str,
        agent: &str,
        timeout: Duration,
        accept_json: bool,
    ) -> Result<ureq::http::Response<ureq::Body>, String> {
        let url = match &self.origin {
            Some(origin) => format!("{origin}/{}", url.splitn(4, '/').nth(3).unwrap_or_default()),
            None => url.to_owned(),
        };
        let mut request = self.agent.get(&url).header("User-Agent", agent);
        if accept_json {
            request = request.header("Accept", "application/json");
        }
        let response = request
            .config()
            .timeout_global(Some(timeout))
            .build()
            .call()
            .map_err(|error| match error {
                ureq::Error::Timeout(_) => "timed out".to_owned(),
                other => format!("<urlopen error {other}>"),
            })?;
        let status = response.status();
        if !status.is_success() {
            return Err(format!(
                "HTTP Error {}: {}",
                status.as_u16(),
                status.canonical_reason().unwrap_or_default()
            ));
        }
        Ok(response)
    }

    /// The FlightStats flight record embedded in a dated flight page.
    pub fn flightstats_flight(&self, url: &str) -> Result<Value, String> {
        let mut response = self.get(url, PAGE_AGENT, PAGE_TIMEOUT, false)?;
        let mut raw = Vec::new();
        response
            .body_mut()
            .as_reader()
            .take(PAGE_LIMIT)
            .read_to_end(&mut raw)
            .map_err(|error| error.to_string())?;
        let text = String::from_utf8_lossy(&raw);
        let at = text
            .find(MARKER)
            .ok_or("the dated flight record was not present")?;
        let page = json_prefix(&text[at + MARKER.len()..])?;
        let mut flight = &page;
        for key in ["props", "initialState", "flightTracker", "flight"] {
            flight = match flight {
                Value::Object(map) => map.get(key).ok_or_else(|| format!("'{key}'"))?,
                Value::Array(_) => {
                    return Err("list indices must be integers or slices, not str".to_owned());
                }
                Value::String(_) => {
                    return Err("string indices must be integers, not 'str'".to_owned());
                }
                other => {
                    return Err(format!(
                        "'{}' object is not subscriptable",
                        type_name(other)
                    ));
                }
            };
        }
        if !flight.is_object() {
            return Err("the dated flight record was empty".to_owned());
        }
        Ok(flight.clone())
    }

    /// A JSON answer from adsb.lol or adsbdb, or nothing on any failure.
    fn json(&self, url: &str) -> Option<Value> {
        let mut response = self.get(url, ADSB_AGENT, ADSB_TIMEOUT, true).ok()?;
        let mut raw = Vec::new();
        response.body_mut().as_reader().read_to_end(&mut raw).ok()?;
        serde_json::from_slice(&raw).ok()
    }

    /// adsbdb's route for an IATA flight number, cached for a week (misses too).
    fn route(&self, ident: &str) -> Option<Value> {
        let key = ident.trim().to_uppercase();
        let path = cache_path("route", &key);
        // Routes cached before the destination's code was kept are fetched again.
        if let Some(cached) = read_json(&path, Some(ROUTE_TTL)) {
            let destination = field(&cached, "destination");
            if !truthy(destination) || destination.get("code").is_some() {
                return truthy(&cached).then_some(cached);
            }
        }
        let data = self.json(&format!("{ROUTES_URL}/callsign/{key}"))?;
        let route_data = field(field(&data, "response"), "flightroute");
        if !truthy(route_data) {
            write_json(&path, &json!({}));
            return None;
        }
        let airport = |node: &Value| {
            truthy(node)
                .then(|| json!({"lat": field(node, "latitude"), "lon": field(node, "longitude"), "code": field(node, "iata_code")}))
        };
        let callsign = field(route_data, "callsign_icao");
        let route = json!({
            "callsignIcao": if truthy(callsign) { callsign.clone() } else { json!(key) },
            "origin": airport(field(route_data, "origin")),
            "destination": airport(field(route_data, "destination")),
        });
        write_json(&path, &route);
        Some(route)
    }

    /// The first aircraft adsb.lol reports with a position for a callsign.
    fn live_by_callsign(&self, callsign: &str) -> Option<Value> {
        let data = self.json(&format!(
            "{ADSB_URL}/callsign/{}",
            callsign.trim().to_uppercase()
        ))?;
        field(&data, "ac")
            .as_array()?
            .iter()
            .find(|aircraft| !field(aircraft, "lat").is_null() && !field(aircraft, "lon").is_null())
            .cloned()
    }

    /// The live aircraft and resolved route for an IATA flight number.
    pub fn find_aircraft(
        &self,
        ident: &str,
        preferred_callsign: &str,
    ) -> (Option<Value>, Option<Value>) {
        let key = ident.trim().to_uppercase();
        let route = self.route(&key);
        let pinned = read_json(&cache_path("pin", &key), Some(PIN_TTL)).filter(truthy);
        let mut candidates = vec![
            preferred_callsign.to_owned(),
            pinned.as_ref().map(str_or_empty).unwrap_or_default(),
        ];
        if let Some(route) = &route {
            candidates.push(str_or_empty(field(route, "callsignIcao")));
        }
        let mut checked: Vec<String> = Vec::new();
        let mut aircraft = None;
        for candidate in candidates {
            let callsign = candidate.trim().to_uppercase();
            if callsign.is_empty() || checked.contains(&callsign) {
                continue;
            }
            aircraft = self.live_by_callsign(&callsign);
            checked.push(callsign);
            if aircraft.is_some() {
                break;
            }
        }
        if let Some(found) = &aircraft {
            let live = str_or_empty(field(found, "flight")).trim().to_uppercase();
            if !live.is_empty() {
                write_json(&cache_path("pin", &key), &json!(live));
            }
        }
        (aircraft, route)
    }
}

/// `json.JSONDecoder().raw_decode`: the first JSON value, whatever follows it.
fn json_prefix(text: &str) -> Result<Value, String> {
    if text.starts_with(char::is_whitespace) {
        return Err("Expecting value: line 1 column 1 (char 0)".to_owned());
    }
    match serde_json::Deserializer::from_str(text)
        .into_iter::<Value>()
        .next()
    {
        Some(Ok(value)) => Ok(value),
        Some(Err(error)) => Err(error.to_string()),
        None => Err("Expecting value: line 1 column 1 (char 0)".to_owned()),
    }
}

fn type_name(value: &Value) -> &'static str {
    match value {
        Value::Bool(_) => "bool",
        Value::Number(number) if number.is_f64() => "float",
        Value::Number(_) => "int",
        _ => "NoneType",
    }
}

pub fn haversine_nm(a_lat: f64, a_lon: f64, b_lat: f64, b_lon: f64) -> f64 {
    let radius = 3440.065;
    let (phi_a, phi_b) = (a_lat.to_radians(), b_lat.to_radians());
    let delta_phi = phi_b - phi_a;
    let delta_lambda = (b_lon - a_lon).to_radians();
    let value = (delta_phi / 2.0).sin().powf(2.0)
        + phi_a.cos() * phi_b.cos() * (delta_lambda / 2.0).sin().powf(2.0);
    2.0 * radius * value.sqrt().min(1.0).asin()
}

// --- XDG files ----------------------------------------------------------------

/// `$XDG_<name>` or `~/<fallback>`, then `omarchy-flights`. An empty variable counts as unset.
pub fn xdg_dir(variable: &str, fallback: &str) -> PathBuf {
    let base = env::var_os(variable)
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| env::home_dir().unwrap_or_default().join(fallback));
    base.join("omarchy-flights")
}

pub fn cache_path(kind: &str, key: &str) -> PathBuf {
    let safe: String = key
        .chars()
        .map(|character| {
            if character.is_alphanumeric() || character == '-' || character == '_' {
                character
            } else {
                '_'
            }
        })
        .collect();
    xdg_dir("XDG_CACHE_HOME", ".cache").join(format!("{kind}-{safe}.json"))
}

/// A JSON file, unless it is missing, unreadable or older than `ttl`.
pub fn read_json(path: &Path, ttl: Option<Duration>) -> Option<Value> {
    if let Some(ttl) = ttl {
        let modified = fs::metadata(path).ok()?.modified().ok()?;
        let age = SystemTime::now()
            .duration_since(modified)
            .unwrap_or_default();
        if age > ttl {
            return None;
        }
    }
    serde_json::from_slice(&fs::read(path).ok()?).ok()
}

/// Replaces a JSON file atomically; failures only cost the cache.
///
/// Each writer gets its own temporary file. A shared `.tmp` name let two
/// widgets, or two legs of one trip, rename each other's half-written file.
pub fn write_json(path: &Path, value: &Value) {
    static SEQUENCE: AtomicU64 = AtomicU64::new(0);
    let (Some(folder), Some(name)) = (path.parent(), path.file_name()) else {
        return;
    };
    let temporary = folder.join(format!(
        ".{}.{}.{}.tmp",
        name.to_string_lossy(),
        std::process::id(),
        SEQUENCE.fetch_add(1, Ordering::Relaxed)
    ));
    let written = fs::create_dir_all(folder).and_then(|()| {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary)?;
        file.write_all(&serde_json::to_vec(value).map_err(std::io::Error::other)?)?;
        fs::rename(&temporary, path)
    });
    if written.is_err() {
        let _ = fs::remove_file(&temporary);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::thread;

    #[test]
    fn reads_the_record_and_ignores_the_script_after_it() {
        assert_eq!(
            json_prefix(r#"{"a": [1, 2.5]};window.x = {"b": 1};</script>"#),
            Ok(json!({"a": [1, 2.5]}))
        );
        assert!(
            json_prefix(r#" {"a": 1}"#).is_err(),
            "raw_decode does not skip whitespace"
        );
        assert!(json_prefix(r#"{"a": "#).is_err());
    }

    #[test]
    fn haversine_matches_python() {
        // math.radians/sin/cos/asin over FRA → MUC, as computed by the Python backend.
        assert_eq!(
            haversine_nm(50.0333, 8.5706, 48.3538, 11.7861),
            161.48686133533155
        );
    }

    #[test]
    fn concurrent_writers_never_leave_a_broken_or_stray_file() {
        let folder = env::temp_dir().join(format!("flights-write-{}", std::process::id()));
        let path = folder.join("pin-LH400.json");
        thread::scope(|scope| {
            for writer in 0..8 {
                let path = &path;
                scope.spawn(move || {
                    for round in 0..50 {
                        write_json(
                            path,
                            &json!({"writer": writer, "round": round, "pad": "x".repeat(4096)}),
                        );
                    }
                });
            }
        });
        let value = read_json(&path, None).expect("the last write is complete JSON");
        assert_eq!(value["round"], 49);
        let names: Vec<_> = fs::read_dir(&folder)
            .expect("folder exists")
            .flatten()
            .map(|entry| entry.file_name())
            .collect();
        assert_eq!(names, ["pin-LH400.json"]);
        fs::remove_dir_all(folder).expect("cleanup");
    }
}

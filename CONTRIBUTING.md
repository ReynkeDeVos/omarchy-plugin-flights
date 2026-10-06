# Contributing

The widget is QML. The flight data comes from `bin/flight-status`, a small Rust program that prints one JSON report and exits. All monitors share the plugin's service (`Service.qml`), which runs it once per refresh and sends each notification once. Bars other than Omarchy's own keep services from their widgets; there every widget refreshes on its own, and the program's lock still keeps notifications single.

`omarchy plugin add` and `omarchy plugin update` only fetch files and build nothing, so the built program (x86_64 Linux) is committed next to its source. After changing `src/`, rebuild it with Rust 1.89 or newer and commit both:

```bash
cargo build --release --locked
install -m 755 target/release/flight-status bin/flight-status
```

On another architecture, build it the same way and copy it into `~/.config/omarchy/plugins/reynkedevos.flights/bin/`. Before `omarchy plugin update`, put the shipped one back with `git -C ~/.config/omarchy/plugins/reynkedevos.flights checkout bin/flight-status`, then build and copy again.

Checks: `cargo fmt --check`, `cargo clippy --all-targets --locked -- -D warnings` and `cargo test --locked`. `python3 -B -m unittest discover -s tests` follows whole trips offline through both `bin/flight-status` and the last Python backend in `tests/reference` and expects the same output, files and requests.

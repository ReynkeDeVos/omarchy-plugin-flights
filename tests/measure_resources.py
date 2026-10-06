"""What the plugin costs, measured offline under identical conditions.

    python3 -B tests/measure_resources.py main=/tmp/flights-main final=.

Each name=folder is a plugin tree; for an older commit use
`git archive <commit> | tar -x -C <folder>`. A Python backend in
bin/flight_status.py is swapped for a wrapper that reads the same test clock
and feed address as the Rust one (the folder is changed in place).

Per tree it measures one backend run (peak RSS, CPU, wall time; cold and
warm cache) and a private offscreen Quickshell (see test_shell_widgets) with
1 and 3 widgets: PSS, CPU of the shell and of the helpers it started, backend
runs over a fixed window, and how long opening the panel takes.
"""

import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_offline_journeys import A, B, BOOTSTRAP, C, D, FOUR_LEGS, Feeds, at, flight, ms  # noqa: E402
from test_shell_widgets import Shell  # noqa: E402

NOW = at("2026-12-23T12:00")
# Linux keeps the forking parent's RSS in the child's ru_maxrss across exec, so
# helpers start from this small launcher instead of the Python driver.
LAUNCHER = r"""
#include <stdio.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <unistd.h>
int main(int argc, char **argv) {
  pid_t pid = fork();
  if (pid == 0) { execvp(argv[1], argv + 1); _exit(127); }
  int status; struct rusage usage;
  wait4(pid, &status, 0, &usage);
  fprintf(stderr, "%ld %ld %ld\n", usage.ru_maxrss,
          usage.ru_utime.tv_sec * 1000000 + usage.ru_utime.tv_usec,
          usage.ru_stime.tv_sec * 1000000 + usage.ru_stime.tv_usec);
  return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
"""
WINDOW = float(os.environ.get("FLIGHTS_MEASURE_SECONDS", "120"))
TICKS = os.sysconf("SC_CLK_TCK")


def feeds() -> Feeds:
    """Four legs: one landed, one in the air with ADS-B, two still to come."""
    stand_in = Feeds()
    stand_in.flight(A, flight("FRA", "MUC", "2026-12-23T08:00", "2026-12-23T09:00", departed=True, landed=True))
    stand_in.flight(B, flight(
        "MUC", "DXB", "2026-12-23T10:30", "2026-12-23T16:30", est_departure="2026-12-23T10:40",
        est_arrival="2026-12-23T16:40", departed=True, callsign="DLH2002", altitude=37000,
    ))
    stand_in.flight(C, flight("DXB", "BKK", "2026-12-23T18:00", "2026-12-24T00:00"))
    stand_in.flight(D, flight("BKK", "HND", "2026-12-24T02:00", "2026-12-24T08:00"))
    stand_in.route("LH2002", "DLH2002", "MUC", "DXB")
    stand_in.aircraft("DLH2002", hex="3c4b2a", lat=40.1, lon=30.2, alt_baro=37000, baro_rate=0)
    return stand_in


def prepare(plugin: Path) -> list[str]:
    """The backend command, after giving a Python backend the test clock and feed address."""
    python = plugin / "bin" / "flight_status.py"
    if python.exists() and "FLIGHTS_TEST_NOW_MS" not in python.read_text():
        reference = plugin / "bin" / "reference"
        reference.mkdir()
        for name in ("flight_status.py", "adsb.py"):
            shutil.move(plugin / "bin" / name, reference / name)
        python.write_text(
            "import os, sys\n"
            "sys.argv.insert(1, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'reference'))\n"
            f"exec(compile({BOOTSTRAP!r}, 'bootstrap', 'exec'))  # FLIGHTS_TEST_NOW_MS\n"
        )
    if python.exists():
        return ["python3", str(python)]
    return [str(plugin / "bin" / "flight-status")]


def launcher(folder: Path) -> str:
    (folder / "rusage.c").write_text(LAUNCHER)
    subprocess.run(["cc", "-O2", "-o", str(folder / "rusage"), str(folder / "rusage.c")], check=True)
    return str(folder / "rusage")


def measured(launch: str, arguments: list[str], env: dict) -> tuple[int, float, float]:
    """Peak RSS (KiB), CPU (ms) and wall time (ms) of one run."""
    started = time.perf_counter()
    done = subprocess.run([launch, *arguments], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    wall = (time.perf_counter() - started) * 1000
    assert done.returncode == 0, done.stderr
    peak, user, system = (int(part) for part in done.stderr.split()[-3:])
    return peak, (user + system) / 1000, wall


def backend_runs(command: list[str], stand_in: Feeds, launch: str) -> dict:
    samples: dict[str, list[tuple[int, float, float]]] = {"cold": [], "warm": [], "baseline /bin/true": []}
    for _ in range(10):
        samples["baseline /bin/true"].append(measured(launch, ["/bin/true"], {}))
    for kind in ("cold", "warm"):
        for _ in range(10):
            with tempfile.TemporaryDirectory() as folder:
                env = {
                    "PATH": os.environ["PATH"], "HOME": folder, "TZ": "UTC",
                    "XDG_STATE_HOME": f"{folder}/state", "XDG_CACHE_HOME": f"{folder}/cache",
                    "FLIGHTS_TEST_ORIGIN": stand_in.origin, "FLIGHTS_TEST_NOW_MS": str(ms(NOW)),
                }
                arguments = [*command, "--legs", FOUR_LEGS, "--home-timezone", "Europe/Berlin"]
                if kind == "warm":
                    subprocess.run(arguments, env=env, stdout=subprocess.DEVNULL, check=True)
                samples[kind].append(measured(launch, arguments, env))
    return {
        kind: {
            "peak_rss_mib": round(statistics.median(sample[0] for sample in values) / 1024, 1),
            "cpu_ms": round(statistics.median(sample[1] for sample in values), 1),
            "wall_ms": round(statistics.median(sample[2] for sample in values), 1),
        }
        for kind, values in samples.items()
    }


def pss_mib(pid: int) -> float:
    for line in Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines():
        if line.startswith("Pss:"):
            return round(int(line.split()[1]) / 1024, 1)
    raise ValueError("no Pss line")


def cpu_ms(pid: int) -> tuple[float, float]:
    """CPU of the shell itself and of the helpers it has reaped."""
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    own, children = int(fields[11]) + int(fields[12]), int(fields[13]) + int(fields[14])
    return own * 1000 / TICKS, children * 1000 / TICKS


def settled_pss(shell: Shell) -> float:
    shell.call("collect")
    time.sleep(0.3)
    return pss_mib(shell.process.pid)


def start_shell(plugin: Path, stand_in: Feeds, widgets: int, folder: str) -> Shell:
    return Shell(
        plugin, Path(folder) / "shell", stand_in, widgets=widgets, bar="builtin",
        settings={"legs": FOUR_LEGS, "homeTimezone": "Europe/Berlin", "refreshSeconds": 30}, now=NOW,
    )


def shell_memory(plugin: Path, stand_in: Feeds, widgets: int, rounds: int = 5) -> dict:
    """Medians over fresh shells: PSS before data, with the panel open and after it closed, and opening times."""
    samples: dict[str, list[float]] = {}
    for _ in range(rounds):
        with tempfile.TemporaryDirectory() as folder:
            shell = start_shell(plugin, stand_in, widgets, folder)
            try:
                time.sleep(3)
                found = {"pss_startup_mib": settled_pss(shell)}
                if widgets:
                    found["first_open_ms"] = float(shell.call("open", 0))
                    time.sleep(0.5)
                    found["pss_panel_open_mib"] = settled_pss(shell)
                    shell.call("close", 0)
                    time.sleep(0.6)
                    reopen = []
                    for _ in range(5):
                        reopen.append(float(shell.call("open", 0)))
                        time.sleep(0.3)
                        shell.call("close", 0)
                        time.sleep(0.6)
                    found["reopen_ms"] = statistics.median(reopen)
                    found["pss_after_panel_mib"] = settled_pss(shell)
            finally:
                shell.stop()
                shell.log.close()
        for key, value in found.items():
            samples.setdefault(key, []).append(value)
    return {key: round(statistics.median(values), 1) for key, values in samples.items()}


def shell_activity(plugin: Path, stand_in: Feeds, widgets: int) -> dict:
    """Backend runs and CPU over a fixed window with the panel closed, refreshing every 30 s."""
    with tempfile.TemporaryDirectory() as folder:
        shell = start_shell(plugin, stand_in, widgets, folder)
        try:
            time.sleep(3)
            stand_in.requests = []
            own_before, helpers_before = cpu_ms(shell.process.pid)
            time.sleep(WINDOW)
            own_after, helpers_after = cpu_ms(shell.process.pid)
            runs = sum(1 for path in stand_in.requests if path.startswith("/v2/flight-tracker/LH/1001"))
            with_trip = settled_pss(shell)
        finally:
            shell.stop()
            shell.log.close()
    minutes = WINDOW / 60
    return {
        "backend_runs_per_min": round(runs / minutes, 1),
        "shell_cpu_ms_per_min": round((own_after - own_before) / minutes),
        "helper_cpu_ms_per_min": round((helpers_after - helpers_before) / minutes),
        "pss_with_trip_mib": with_trip,
    }


def main(arguments: list[str]) -> int:
    trees = dict(argument.split("=", 1) for argument in arguments)
    results = {}
    tools = tempfile.TemporaryDirectory()
    launch = launcher(Path(tools.name))
    for name, folder in trees.items():
        plugin = Path(folder).resolve()
        command = prepare(plugin)
        stand_in = feeds()
        try:
            results[name] = {"backend": backend_runs(command, stand_in, launch)}
            results[name]["shell_0_widgets"] = shell_memory(plugin, stand_in, 0)
            for widgets in (1, 3):
                results[name][f"shell_{widgets}_widgets"] = {
                    **shell_memory(plugin, stand_in, widgets),
                    **shell_activity(plugin, stand_in, widgets),
                }
        finally:
            stand_in.close()
        print(name, json.dumps(results[name], indent=2), flush=True)
    print(json.dumps(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

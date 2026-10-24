"""Admit fixed destinations and exact owned forwards under the pinned controller."""

import hashlib
import json
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .driver import Hold
from .telemetry import TelemetryPending


def telemetry_status(response):
    # Only bounded setup GET collection can wait; no issue/submission/grade retry.
    pending = {502: b'{"error":"upstream unavailable"}', 503: b'{"error":"upstream identity not currently verified"}'}
    if (
        response.status_code in pending
        and response.content == pending[response.status_code]
        and response.headers.get("content-type", "").split(";")[0] == "application/json"
    ):
        raise TelemetryPending(
            "telemetry_gateway_origin_unavailable"
            if response.status_code == 502
            else "telemetry_gateway_origin_guard_pending",
            status=response.status_code,
        )
    response.raise_for_status()


def forward_metadata(child, recipe, controller, clock):
    try:
        start = datetime.fromisoformat(child["started_at"]).timestamp()
        if (
            type(child["pid"]) is not int
            or child["pid"] <= 0
            or child["pid"] == controller
            or child["command_sha256"] != recipe["command_sha256"]
            or not recipe["controller_started_at"] <= start <= clock + 5
        ):
            raise ValueError()
    except (KeyError, ValueError, TypeError):
        raise Hold("Owned telemetry forward metadata differs from fixed recipe") from None
    return start


def validate_forward(child, recipe, controller, clock, evidence):
    start = forward_metadata(child, recipe, controller, clock)
    if (
        evidence["pid"] != child["pid"]
        or evidence["ppid"] != controller
        or evidence["pgid"] != child["pid"]
        or int(evidence["start_unix"]) != int(start)
        or evidence["command"] != recipe["command"]
        or evidence["cwd"] != recipe["cwd"]
        or evidence["listeners"] != [f"127.0.0.1:{recipe['port']}"]
    ):
        raise Hold("Telemetry forward full identity or owned loopback listener changed")


def inspect_forward(pid, port):
    args = ["ps", "-ww", "-p", str(pid), "-o", "pid=,ppid=,pgid=,lstart=,command="]
    before = subprocess.run(args, capture_output=True, text=True, timeout=5)
    cwd = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], capture_output=True, text=True, timeout=5)
    listener = subprocess.run(
        ["lsof", "-nP", "-a", "-p", str(pid), f"-iTCP:{port}", "-sTCP:LISTEN", "-Fn"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    after = subprocess.run(args, capture_output=True, text=True, timeout=5)
    if before.returncode or cwd.returncode or listener.returncode or after.returncode or before.stdout != after.stdout:
        raise Hold("Owned telemetry forward could not be coherently verified")
    p = before.stdout.strip().split(None, 8)
    if len(p) != 9:
        raise Hold("Owned forward identity format is invalid")
    paths = [x[1:] for x in cwd.stdout.splitlines() if x.startswith("n")]
    names = sorted(set(x[1:] for x in listener.stdout.splitlines() if x.startswith("n")))
    if len(paths) != 1:
        raise Hold("Owned forward source cwd is ambiguous")
    return {
        "pid": int(p[0]),
        "ppid": int(p[1]),
        "pgid": int(p[2]),
        "start_unix": time.mktime(time.strptime(" ".join(p[3:8]), "%a %b %d %H:%M:%S %Y")),
        "command": p[8],
        "cwd": paths[0],
        "listeners": names,
    }


def live_forward(child, recipe, controller, clock):
    validate_forward(child, recipe, controller, clock, inspect_forward(child["pid"], recipe["port"]))


def validate_routes(routes, state, guard, *, clock, check_process, check_forward=None):
    age = clock - datetime.fromisoformat(state["heartbeat_at"]).timestamp()
    if not -5 <= age <= 70:
        raise Hold("Telemetry runtime heartbeat is stale or inconsistent")
    urls = routes["urls"]
    if set(urls) != {"grafana", "prometheus"} or state["urls"] != urls:
        raise Hold("Telemetry destinations differ from frozen routes")
    if any(not re.fullmatch(r"https://[a-z0-9-]+\.trycloudflare\.com", v) for v in urls.values()):
        raise Hold("Telemetry destination is outside the admitted transport")
    proof = routes["processes"]
    recipes = routes.get("forward_recipes", {})
    if not set(recipes) <= {"grafana-forward", "prometheus-forward"} or not set(recipes) <= set(proof):
        raise Hold("Only exact originally owned forwards may be recipe-admitted")
    if set(proof) != set(state["children"]) | {"controller"} or proof["controller"]["pid"] != state["controller_pid"]:
        raise Hold("Telemetry controller or child set changed")
    for name, entry in proof.items():
        if name in recipes:
            forward_metadata(state["children"][name], recipes[name], state["controller_pid"], clock)
        else:
            if name != "controller" and state["children"][name]["pid"] != entry["pid"]:
                raise Hold("Telemetry child identity changed")
            check_process(entry)
    if state["phase"] == "retained_waiting_for_origin_guard":
        raise TelemetryPending("telemetry_origin_guard_pending")
    if state["phase"] not in ["retained_running", "tunnels_ready"]:
        raise Hold("Telemetry transport is not live and admitted")
    if not clock < guard["valid_until"] <= clock + 110:
        raise Hold("Telemetry origin authority is expired or inconsistent")
    for name, recipe in recipes.items():
        if check_forward is None:
            raise Hold("Full owned forward inspection is mandatory")
        check_forward(state["children"][name], recipe, state["controller_pid"], clock)
    return urls


def live_identity(entry):
    result = subprocess.run(
        ["ps", "-ww", "-p", str(entry["pid"]), "-o", "pid=,ppid=,pgid=,lstart=,command="],
        capture_output=True,
        text=True,
        timeout=5,
    )
    cwd = subprocess.run(
        ["lsof", "-a", "-p", str(entry["pid"]), "-d", "cwd", "-Fn"], capture_output=True, text=True, timeout=5
    )
    if (
        result.returncode
        or cwd.returncode
        or result.stdout.strip() != entry["full_ps"]
        or "\nn" + entry["cwd"] + "\n" not in cwd.stdout
    ):
        raise Hold("Frozen telemetry process full identity changed")


def load_routes(manifest):
    routes = json.loads(Path(manifest).read_text())
    freeze = Path(routes["source_freeze"])
    if hashlib.sha256(freeze.read_bytes()).hexdigest() != routes["source_freeze_sha256"]:
        raise Hold("Telemetry implementation admission changed")
    for name, expected in json.loads(freeze.read_text())["files"].items():
        if hashlib.sha256(Path(name).read_bytes()).hexdigest() != expected:
            raise Hold("Frozen telemetry source or boundary configuration changed")
    state = json.loads(Path(routes["runtime_state"]).read_text())
    guard = json.loads(Path(routes["guard_file"]).read_text())
    urls = validate_routes(
        routes, state, guard, clock=time.time(), check_process=live_identity, check_forward=live_forward
    )
    after = json.loads(Path(routes["runtime_state"]).read_text())
    if (
        after["controller_pid"] != state["controller_pid"]
        or after["urls"] != state["urls"]
        or after["children"] != state["children"]
        or after["phase"] != state["phase"]
    ):
        raise Hold("Telemetry ownership changed during route admission")
    return urls

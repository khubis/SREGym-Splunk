import socket


def assert_port_available(port):
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("Invalid loopback port")
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))


def retention_step(poll_children, restart_forward, refresh_guard, invalidate_guard):
    recovery_errors = []
    for name, alive in poll_children().items():
        if not alive and name.endswith("-forward"):
            try:
                restart_forward(name)
            except Exception as error:
                recovery_errors.append(
                    {"child": name, "error_type": type(error).__name__, "errno": getattr(error, "errno", None)}
                )
    failed = sorted(name for name, alive in poll_children().items() if not alive)
    if failed or recovery_errors:
        invalidate_guard()
        return {"phase": "retained_degraded_no_replay", "failed_children": failed, "recovery_errors": recovery_errors}
    try:
        refresh_guard()
    except Exception as error:
        invalidate_guard()
        return {"phase": "retained_waiting_for_origin_guard", "guard_error_type": type(error).__name__}
    return {"phase": "retained_running"}

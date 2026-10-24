"""One external investigation and one public Conductor diagnosis submission.

The harness supplies only public context and connection bindings. This module
does not import the fault registry, oracle, ground truth, or benchmark logs.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx


class Hold(RuntimeError):
    pass


def now():
    return datetime.now(UTC).isoformat()


def save(path: Path, value: dict):
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(value, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def result_data(result):
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        if not isinstance(structured, dict):
            raise Hold("Provider returned an invalid result object")
        return structured
    raw = "\n".join(c.text for c in result.content if c.type == "text")
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
    try:
        value = json.loads(raw)
    except ValueError:
        raise Hold("Provider returned a non-JSON result") from None
    if not isinstance(value, dict):
        raise Hold("Provider returned an invalid result object")
    return value


def terminal_answer(value: dict):
    if value.get("running") is not False:
        return None, None
    if value.get("error"):
        raise Hold("Provider reported an investigation error")
    answer = value.get("answer")
    if isinstance(answer, str) and answer.strip():
        return answer.strip(), "answer"
    replies = [
        item["text"].strip()
        for item in value.get("timeline", [])
        if isinstance(item, dict)
        and item.get("role") == "assistant"
        and isinstance(item.get("text"), str)
        and item["text"].strip()
    ]
    return (replies[-1], "timeline") if replies else (None, None)


def prompt(context: dict, binding: dict):
    # A closed schema prevents accidental forwarding of internal fault fields.
    if set(context) != {"app_name", "app_description", "app_namespace", "symptom", "incident_started_at"}:
        raise Hold("Only public application and symptom context is accepted")
    if any(not isinstance(value, str) or not value.strip() for value in context.values()):
        raise Hold("Public context is incomplete")
    if set(binding) != {
        "namespace",
        "namespace_uid",
        "kubernetes_connection",
        "grafana_connection",
        "prometheus_connection",
    }:
        raise Hold("Unexpected connection binding")
    if any(not isinstance(value, str) or not value.strip() for value in binding.values()):
        raise Hold("Connection binding is incomplete")
    if binding["namespace"] != context["app_namespace"]:
        raise Hold("Context and namespace binding disagree")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", binding["namespace"]):
        raise Hold("Invalid namespace")
    started = datetime.fromisoformat(context["incident_started_at"].replace("Z", "+00:00"))
    if started.utcoffset() != timedelta(0):
        raise Hold("Incident time must be UTC")
    return (
        "Investigate the following service incident and return a diagnosis with supporting evidence. "
        "Diagnosis only: do not change resources or apply a fix.\n"
        f"Application: {context['app_name']}\nDescription: {context['app_description']}\n"
        f"Namespace: {context['app_namespace']} (UID {binding['namespace_uid']})\n"
        f"Symptom: {context['symptom']}\nIncident window starts: {context['incident_started_at']}\n"
        f"Use Kubernetes connection {binding['kubernetes_connection']}; "
        f"Grafana connection {binding['grafana_connection']} with Loki datasource sregym-loki; "
        f"and Prometheus connection {binding['prometheus_connection']}. "
        "Restrict application queries to this namespace and this incident time window. Read-only control-plane evidence is limited to the incident-related objects exposed by this connection. "
        "Use live workload state, logs and metrics. Include actual observations and timestamps; "
        "state explicitly when evidence is missing. Do not use other integrations, previous "
        "investigations, source repositories, or workspace memories as evidence for this incident."
    )


async def investigate(
    session,
    conductor,
    out: Path,
    context: dict,
    binding: dict,
    *,
    budget_seconds=1200,
    guard: Callable = lambda: None,
    clock=time.monotonic,
):
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (out / "driver.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = out / "driver-state.json"
        if path.exists():
            raise Hold("This attempt is already reserved; inspect its existing outcome")
        description = prompt(context, binding)
        state = {
            "started_at": now(),
            "phase": "preparing",
            "create_requests": 0,
            "submission_requests": 0,
            "budget_seconds": budget_seconds,
            "model": "External agent-managed; API does not expose model selection",
            "context": context,
            "binding": binding,
            "events": [],
        }
        save(path, state)
        deadline = clock() + budget_seconds
        try:
            if type(budget_seconds) is not int or not 0 < budget_seconds <= 1200:
                raise Hold("Investigation deadline requires an integer budget from 1 to 1200 seconds")
            guard()
            conductor.require_diagnosis()
            if clock() >= deadline:
                raise Hold("Deadline expired before issue creation")
            state.update(
                phase="creation_pending",
                create_requests=1,
                create_reserved_at=now(),
                request={"description": description},
            )
            save(path, state)
            # A lost response never permits a replacement issue.
            created = await session.call_tool(
                "create_issue", state["request"], read_timeout_seconds=timedelta(seconds=40)
            )
            state["create_response"] = created.model_dump(mode="json")
            save(path, state)
            if created.isError:
                state["phase"] = "provider_rejected"
                raise Hold("External agent rejected the investigation")
            value = result_data(created)
            slug = value.get("slug")
            if not isinstance(slug, str) or not re.fullmatch(r"ISS-[1-9][0-9]*", slug):
                raise Hold("Creation response has no issue identity")
            state.update(
                issue_slug=slug,
                issue_url=value.get("url"),
                agent_started=value.get("agent_started"),
                phase="investigating",
                accepted_at=now(),
            )
            save(path, state)
            terminal_empty_since = None
            while clock() < deadline:
                guard()
                remaining = deadline - clock()
                if remaining <= 1:
                    break
                result = await session.call_tool(
                    "get_issue",
                    {
                        "id": slug,
                        "include": ["result", "timeline"],
                        "wait_seconds": min(25, max(1, int(remaining) - 1)),
                    },
                    read_timeout_seconds=timedelta(seconds=min(35, remaining)),
                )
                state["events"].append({"at": now(), "tool": "get_issue", "response": result.model_dump(mode="json")})
                save(path, state)
                if result.isError:
                    raise Hold("Investigation result could not be retrieved")
                value = result_data(result)
                if value.get("slug") != slug:
                    raise Hold("Provider returned a different issue identity")
                answer, source = terminal_answer(value)
                if value.get("running") is False:
                    if not answer:
                        # External agent can publish running:false before its final timeline text.
                        # Re-read this same issue only; never extend the original deadline.
                        if terminal_empty_since is None:
                            terminal_empty_since = clock()
                            state["terminal_empty_first_seen_at"] = now()
                        state["terminal_empty_observations"] = state.get("terminal_empty_observations", 0) + 1
                        state["terminal_answer_grace_seconds"] = 60
                        save(path, state)
                        if clock() - terminal_empty_since >= 60:
                            raise Hold("Terminal result stayed empty through bounded answer-publication grace")
                        await asyncio.sleep(min(2, max(0, deadline - clock())))
                        continue
                    if clock() >= deadline:
                        raise Hold("Diagnosis arrived after the investigation deadline")
                    state.update(
                        final_answer=answer, final_answer_source=source, phase="answer_received", answer_at=now()
                    )
                    save(path, state)
                    guard()
                    conductor.require_diagnosis()
                    if clock() >= deadline:
                        raise Hold("Deadline expired before diagnosis submission")
                    state.update(phase="submission_pending", submission_requests=1, submission_reserved_at=now())
                    save(path, state)
                    conductor.submit_diagnosis(answer)
                    state.update(phase="submitted", submitted_at=now())
                    save(path, state)
                    return state
                # Some servers return immediately despite wait_seconds.
                await asyncio.sleep(min(2, max(0, deadline - clock())))
            raise Hold("Investigation deadline expired; no replacement or submission")
        except BaseException as exc:
            prior = state["phase"]
            state.update(
                phase="held",
                held_from=prior,
                error_type=type(exc).__name__,
                held_at=now(),
                needs_reconciliation=prior in {"creation_pending", "submission_pending"},
            )
            save(path, state)
            raise


class ConductorHTTP:
    def __init__(self, url: str):
        if not re.fullmatch(r"http://127\.0\.0\.1:[1-9][0-9]{3,4}", url):
            raise Hold("The grading endpoint must be local loopback")
        self.client = httpx.Client(base_url=url, timeout=30, trust_env=False)
        self.submitted = False

    def context(self):
        response = self.client.get("/get_app")
        response.raise_for_status()
        value = response.json()
        namespaces = value.get("namespaces") or [value.get("namespace")]
        if len(namespaces) != 1 or not isinstance(namespaces[0], str):
            raise Hold("This pilot requires exactly one application namespace")
        return {"app_name": value["app_name"], "app_description": value["descriptions"], "app_namespace": namespaces[0]}

    def require_diagnosis(self):
        response = self.client.get("/status")
        if response.status_code != 200 or response.json().get("stage") != "diagnosis":
            raise Hold("Conductor is not accepting diagnosis submissions")

    def submit_diagnosis(self, answer):
        if self.submitted:
            raise Hold("A diagnosis submission has already been attempted")
        self.submitted = True
        response = self.client.post("/submit", json={"stage": "diagnosis", "solution": answer})
        value = response.json()
        if response.status_code != 200 or value.get("status") != "200" or value.get("stage") != "diagnosis":
            raise Hold("Conductor did not confirm the diagnosis submission")

    def close(self):
        self.client.close()

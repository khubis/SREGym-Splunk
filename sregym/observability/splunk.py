"""Secure deployment of the Splunk OpenTelemetry Collector."""

import json
import os
import random
import re
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn, Protocol, cast

import httpx
import yaml
from kubernetes import client
from kubernetes import config as kubernetes_config
from kubernetes.client.rest import ApiException

from sregym.observability.base import (
    SIGNAL_NAMES,
    ApplicationScope,
    AttemptContext,
    DeliveryReport,
    ExternalOtlpExport,
    FailureKind,
    ProviderError,
    ProviderName,
    ReadinessReport,
    SignalName,
    SignalReadiness,
)

CHART_VERSION = "0.160.0"
CHART_NAME = "splunk-otel-collector"
CHART_REPOSITORY = "https://signalfx.github.io/splunk-otel-collector-chart"
RELEASE_NAME = "sregym-splunk-otel"
NAMESPACE = "sregym-observability"
SECRET_NAME = "sregym-splunk-otel-credentials"
_HELM_TIMEOUT = "5m"
_REQUIRED_ENV = (
    "SF_TOKEN",
    "SPLUNK_O11Y_INGEST_TOKEN",
    "SFX_REALM",
    "SPLUNK_HOST",
    "SPLUNK_HEC_PORT",
    "SPLUNK_HEC_TOKEN",
)
_HOST_PATTERN = re.compile(
    r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*\Z"
)
_REALM_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*\Z")
_INDEX_PATTERN = re.compile(r"[A-Za-z0-9_.-]+\Z")
_CONNECTION_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]+\Z")
_DEFAULT_VALUES_PATH = Path(__file__).parents[1] / "observer" / "splunk" / "values.yaml"


@dataclass(frozen=True)
class ReliabilityPolicy:
    """Bounded polling controls shared by readiness and delivery assurance."""

    readiness_timeout_seconds: float = 360.0
    drain_timeout_seconds: float = 60.0
    request_timeout_seconds: float = 10.0
    max_attempts: int = 40
    initial_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 10.0
    retry_after_cap_seconds: float = 15.0

    def __post_init__(self) -> None:
        numeric = (
            self.readiness_timeout_seconds,
            self.drain_timeout_seconds,
            self.request_timeout_seconds,
            self.initial_backoff_seconds,
            self.max_backoff_seconds,
            self.retry_after_cap_seconds,
        )
        if self.max_attempts < 1 or any(value <= 0 for value in numeric):
            raise ValueError("reliability policy values must be positive")
        if self.max_backoff_seconds < self.initial_backoff_seconds:
            raise ValueError("maximum backoff must not be smaller than initial backoff")


@dataclass(frozen=True)
class CollectorSnapshot:
    """One safe sample of the collector's cumulative reliability metrics."""

    sent: dict[SignalName, int | None]
    send_failed: dict[SignalName, int | None]
    enqueue_failed: dict[SignalName, int | None]
    queue_size: dict[SignalName, int | None]


class SplunkBackendError(RuntimeError):
    """Classified, payload-free error returned by the Splunk query backend."""

    def __init__(
        self,
        *,
        status_code: int | None,
        transient: bool,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__("Splunk query request failed")
        self.status_code = status_code
        self.transient = transient
        self.retry_after_seconds = retry_after_seconds


class SplunkQueryBackend(Protocol):
    def resolve_logs_connection(self, timeout_seconds: float, requested_connection_id: str | None = None) -> str: ...

    def query_signal(
        self,
        signal: SignalName,
        context: AttemptContext,
        scope: ApplicationScope,
        connection_id: str,
        checked_at: datetime,
        timeout_seconds: float,
    ) -> int: ...

    def collector_snapshot(
        self,
        context: AttemptContext,
        checked_at: datetime,
        timeout_seconds: float,
    ) -> CollectorSnapshot: ...

    def close(self) -> None: ...


_CONNECTIONS_QUERY = """
query getConnections($includeSplunkCloudSSO: Boolean) {
  getConnections(includeSplunkCloudSSO: $includeSplunkCloudSSO) {
    connectionID connectionName isDefaultConnection isAccessible
  }
}
"""
_START_TRACE_QUERY = """
query StartAnalyticsSearch($parameters: JSON!) {
  startAnalyticsSearch(parameters: $parameters)
}
"""
_GET_TRACE_QUERY = """
query GetAnalyticsSearch($jobId: ID!) {
  getAnalyticsSearch(jobId: $jobId)
}
"""
_CREATE_LOG_JOB = """
mutation createSearchJob(
  $query: String!, $queryType: QueryType, $queryParameters: SearchJobQueryParametersInput!,
  $connectionID: String, $enablePreview: Boolean
) {
  createSearchJob(data: {
    query: $query, queryType: $queryType, queryParameters: $queryParameters,
    connectionID: $connectionID, enablePreview: $enablePreview
  }) { id status percentComplete query }
}
"""
_LOG_JOB_RESULTS = """
query searchJobResultsWithoutFieldsSummary($id: ID!, $count: Int, $offset: Int) {
  searchJob(id: $id) {
    id status percentComplete resultsAvailable resolvedEarliest resolvedLatest
    results(count: $count, offset: $offset) { fields { name } results }
  }
}
"""
_COMPLETE_JOB_STATUSES = frozenset({"done", "completed", "complete", "success", "succeeded"})


class SplunkHttpBackend:
    """Read-only Splunk APIs used to prove end-to-end signal visibility."""

    def __init__(self, configuration: "SplunkConfig", *, http_client: httpx.Client | None = None) -> None:
        self.configuration = configuration
        self._client = http_client or httpx.Client()
        self._owns_client = http_client is None
        self._app_base = f"https://app.{configuration.realm}.signalfx.com"
        self._stream_base = f"https://stream.{configuration.realm}.signalfx.com"
        self._trace_jobs: dict[str, str] = {}
        self._log_jobs: dict[tuple[str, SignalName], str] = {}

    def resolve_logs_connection(self, timeout_seconds: float, requested_connection_id: str | None = None) -> str:
        payload = self._graphql(
            f"{self._app_base}/v2/logs/graphql",
            "getConnections",
            _CONNECTIONS_QUERY,
            {"includeSplunkCloudSSO": True},
            timeout_seconds,
        )
        connections = payload.get("data", {}).get("getConnections", [])
        accessible = [item for item in connections if item.get("isAccessible") and item.get("connectionID")]
        if requested_connection_id is not None:
            selected = next((item for item in accessible if item.get("connectionID") == requested_connection_id), None)
        else:
            selected = next((item for item in accessible if item.get("isDefaultConnection")), None)
            if selected is None and accessible:
                selected = accessible[0]
        if selected is None:
            raise SplunkBackendError(status_code=400, transient=False)
        return cast(str, selected["connectionID"])

    def query_signal(
        self,
        signal: SignalName,
        context: AttemptContext,
        scope: ApplicationScope,
        connection_id: str,
        checked_at: datetime,
        timeout_seconds: float,
    ) -> int:
        if signal == "metrics":
            program = (
                "data('otelcol_exporter_sent_metric_points', filter=filter('sregym.run.id', "
                f"'{context.run_id}'), rollup='latest').max().publish(label='readiness')"
            )
            return int(
                self._signalflow(program, context.attempt_started_at, checked_at, timeout_seconds).get("readiness", 0)
            )
        if signal == "traces":
            payload = self._query_traces(context, scope, checked_at, timeout_seconds)
            search = payload.get("data", {}).get("startAnalyticsSearch") or payload.get("data", {}).get(
                "getAnalyticsSearch", {}
            )
            sections = _trace_sections(search)
            examples = [
                example
                for section in sections
                if section.get("sectionType") == "traceExamples"
                for example in _trace_examples(section)
            ]
            return len(examples)
        return self._query_logs(signal, context, scope, connection_id, checked_at, timeout_seconds)

    def collector_snapshot(
        self,
        context: AttemptContext,
        checked_at: datetime,
        timeout_seconds: float,
    ) -> CollectorSnapshot:
        labels = {
            "sent_metrics": "otelcol_exporter_sent_metric_points",
            "sent_traces": "otelcol_exporter_sent_spans",
            "sent_logs": "otelcol_exporter_sent_log_records",
            "failed_metrics": "otelcol_exporter_send_failed_metric_points",
            "failed_traces": "otelcol_exporter_send_failed_spans",
            "failed_logs": "otelcol_exporter_send_failed_log_records",
            "enqueue_metrics": "otelcol_exporter_enqueue_failed_metric_points",
            "enqueue_traces": "otelcol_exporter_enqueue_failed_spans",
            "enqueue_logs": "otelcol_exporter_enqueue_failed_log_records",
        }
        statements = [
            f"data('{metric}', filter=filter('sregym.run.id', '{context.run_id}'), rollup='latest').max().publish(label='{label}')"
            for label, metric in labels.items()
        ]
        for data_type in ("metrics", "traces", "logs"):
            statements.append(
                "data('otelcol_exporter_queue_size', filter=filter('sregym.run.id', "
                f"'{context.run_id}') and filter('data_type', '{data_type}'), rollup='latest')"
                f".max().publish(label='queue_{data_type}')"
            )
        values = self._signalflow("\n".join(statements), context.attempt_started_at, checked_at, timeout_seconds)

        def group(prefix: str) -> dict[SignalName, int | None]:
            return {
                "metrics": _optional_int(values.get(f"{prefix}_metrics")),
                "traces": _optional_int(values.get(f"{prefix}_traces")),
                "logs": _optional_int(values.get(f"{prefix}_logs")),
                "kubernetes_events": _optional_int(values.get(f"{prefix}_logs")),
            }

        sent = group("sent")
        queue_size = group("queue")

        def failure_group(prefix: str) -> dict[SignalName, int | None]:
            counters = group(prefix)
            # Collector failure counters are sparse: a series is not created
            # until its first failure. A successful query plus the matching
            # sent and queue series proves the exporter is observable, so the
            # absent failure series has its documented initial value of zero.
            # Without both companion series it remains unavailable.
            return {
                signal: 0 if value is None and sent[signal] is not None and queue_size[signal] is not None else value
                for signal, value in counters.items()
            }

        return CollectorSnapshot(
            sent=sent,
            send_failed=failure_group("failed"),
            enqueue_failed=failure_group("enqueue"),
            queue_size=queue_size,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _query_logs(
        self,
        signal: SignalName,
        context: AttemptContext,
        scope: ApplicationScope,
        connection_id: str,
        checked_at: datetime,
        timeout_seconds: float,
    ) -> int:
        namespaces = " OR ".join(f'k8s.namespace.name="{namespace}"' for namespace in scope.namespaces)
        kind = "k8s.container.name=*" if signal == "logs" else "k8s.event.reason=*"
        query = (
            f'search index="{self.configuration.hec_index}" k8s.cluster.name="{context.run_id}" '
            f"({namespaces}) {kind} | head 1"
        )
        job_key = (context.run_id, signal)
        job_id = self._log_jobs.get(job_key)
        if job_id is None:
            created = self._graphql(
                f"{self._app_base}/v2/logs/graphql",
                "createSearchJob",
                _CREATE_LOG_JOB,
                {
                    "query": query,
                    "queryType": "SPL1",
                    "queryParameters": {
                        "timezone": "UTC",
                        "earliest": str(int(context.attempt_started_at.timestamp())),
                        "latest": str(int(checked_at.timestamp())),
                    },
                    "connectionID": connection_id,
                    "enablePreview": True,
                },
                timeout_seconds,
            )
            job_id = created.get("data", {}).get("createSearchJob", {}).get("id")
            if not job_id:
                raise SplunkBackendError(status_code=500, transient=True)
            self._log_jobs[job_key] = str(job_id)
        result = self._graphql(
            f"{self._app_base}/v2/logs/graphql",
            "searchJobResultsWithoutFieldsSummary",
            _LOG_JOB_RESULTS,
            {"id": job_id, "count": 1, "offset": 0},
            timeout_seconds,
        )
        job = result.get("data", {}).get("searchJob", {})
        result_set = job.get("results", {}) if isinstance(job, dict) else {}
        rows = result_set.get("results") if isinstance(result_set, dict) else None
        if rows is None:
            rows = []
        if not isinstance(rows, list):
            raise SplunkBackendError(status_code=500, transient=True)
        if str(job.get("status", "")).strip().lower() in _COMPLETE_JOB_STATUSES:
            self._log_jobs.pop(job_key, None)
        return len(rows)

    def _query_traces(
        self,
        context: AttemptContext,
        scope: ApplicationScope,
        checked_at: datetime,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        job_id = self._trace_jobs.get(context.run_id)
        if job_id is not None:
            try:
                payload = self._graphql(
                    f"{self._app_base}/v2/apm/graphql",
                    "GetAnalyticsSearch",
                    _GET_TRACE_QUERY,
                    {"jobId": job_id},
                    timeout_seconds,
                )
            except SplunkBackendError:
                self._trace_jobs.pop(context.run_id, None)
                raise
            field = "getAnalyticsSearch"
        else:
            tags = [{"tag": "k8s.cluster.name", "operation": "IN", "values": [context.run_id]}]
            parameters = {
                "sharedParameters": {
                    "timeRangeMillis": {
                        "gte": int(context.attempt_started_at.timestamp() * 1000),
                        "lte": int(checked_at.timestamp() * 1000),
                    },
                    "filters": [
                        {
                            "filterType": "traceFilter",
                            "traceFilter": {"tags": []},
                            "spanFilters": [{"tags": tags}],
                        }
                    ],
                    "samplingFactor": 1,
                },
                "sectionsParameters": [{"sectionType": "traceExamples", "limit": 1}],
            }
            payload = self._graphql(
                f"{self._app_base}/v2/apm/graphql",
                "StartAnalyticsSearch",
                _START_TRACE_QUERY,
                {"parameters": parameters},
                timeout_seconds,
            )
            field = "startAnalyticsSearch"
        search = payload.get("data", {}).get(field, {})
        sections = _trace_sections(search)
        complete = any(
            section.get("sectionType") == "traceExamples" and section.get("isComplete") for section in sections
        )
        returned_job_id = search.get("jobId") if isinstance(search, dict) else None
        if complete:
            self._trace_jobs.pop(context.run_id, None)
        elif returned_job_id:
            self._trace_jobs[context.run_id] = str(returned_job_id)
        return payload

    def _graphql(
        self,
        url: str,
        operation_name: str,
        query: str,
        variables: dict[str, object],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        response = self._request(
            "POST",
            url,
            timeout_seconds,
            json_body={"operationName": operation_name, "query": query, "variables": variables},
        )
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError):
            raise SplunkBackendError(status_code=500, transient=True) from None
        if not isinstance(payload, dict) or payload.get("errors"):
            raise SplunkBackendError(status_code=500, transient=True)
        return cast(dict[str, Any], payload)

    def _signalflow(
        self,
        program: str,
        start: datetime,
        stop: datetime,
        timeout_seconds: float,
    ) -> dict[str, float]:
        response = self._request(
            "POST",
            f"{self._stream_base}/v2/signalflow/execute",
            timeout_seconds,
            content=program,
            query_params={
                "start": int(start.timestamp() * 1000),
                "stop": int(stop.timestamp() * 1000),
                "resolution": 1000,
                "maxDelay": 0,
            },
            headers={"Content-Type": "text/plain"},
        )
        labels: dict[str, str] = {}
        values: dict[str, float] = {}
        event = ""
        for line in response.text.splitlines():
            if line.startswith("event:"):
                event = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                try:
                    payload = json.loads(line.removeprefix("data:").strip())
                except json.JSONDecodeError:
                    raise SplunkBackendError(status_code=500, transient=True) from None
                if event == "metadata":
                    label = payload.get("properties", {}).get("sf_streamLabel")
                    if label:
                        labels[str(payload.get("tsId"))] = str(label)
                elif event == "data":
                    for item in payload.get("data", []):
                        label = labels.get(str(item.get("tsId")))
                        value = item.get("value")
                        if label and isinstance(value, (int, float)):
                            values[label] = float(value)
        return values

    def _request(
        self,
        method: str,
        url: str,
        timeout_seconds: float,
        *,
        json_body: object | None = None,
        content: str | None = None,
        query_params: Mapping[str, int] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        request_headers = {"X-SF-Token": self.configuration.access_token, **(headers or {})}
        try:
            response = self._client.request(
                method,
                url,
                headers=request_headers,
                timeout=timeout_seconds,
                json=json_body,
                content=content,
                params=query_params,
            )
        except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError, httpx.ReadError):
            raise SplunkBackendError(status_code=None, transient=True) from None
        if response.is_error:
            retry_after = _retry_after(response.headers.get("Retry-After"))
            transient = response.status_code in (408, 429) or response.status_code >= 500
            raise SplunkBackendError(
                status_code=response.status_code,
                transient=transient,
                retry_after_seconds=retry_after,
            )
        return response


def _optional_int(value: float | None) -> int | None:
    return None if value is None else int(value)


def _trace_sections(search: object) -> list[Mapping[str, Any]]:
    if not isinstance(search, dict):
        return []
    sections = search.get("sections")
    if sections is None:
        return []
    if not isinstance(sections, list) or any(not isinstance(section, dict) for section in sections):
        raise SplunkBackendError(status_code=500, transient=True)
    return cast(list[Mapping[str, Any]], sections)


def _trace_examples(section: Mapping[str, Any]) -> list[Any]:
    candidates: list[object] = [section.get("traceExamples"), section.get("legacyTraceExamples")]
    data = section.get("data")
    if isinstance(data, dict):
        candidates.append(data.get("traceExamples"))
    elif data is not None:
        candidates.append(data)
    for candidate in candidates:
        if candidate is None:
            continue
        if not isinstance(candidate, list):
            raise SplunkBackendError(status_code=500, transient=True)
        if candidate:
            return candidate
    return []


def _counter_delta(opening: int | None, closing: int | None) -> int | None:
    if opening is None or closing is None or closing < opening:
        return None
    return closing - opening


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


class CoreV1Api(Protocol):
    def get_api_resources(self) -> object: ...

    def create_namespace(self, body: client.V1Namespace) -> object: ...

    def create_namespaced_secret(self, *, namespace: str, body: client.V1Secret) -> object: ...

    def patch_namespaced_secret(self, *, name: str, namespace: str, body: client.V1Secret) -> object: ...

    def delete_namespaced_secret(self, *, name: str, namespace: str) -> object: ...


CommandRunner = Callable[[list[str], str | None], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class SplunkConfig:
    """Validated destination configuration with secret-safe representation."""

    access_token: str = field(repr=False)
    ingest_token: str = field(repr=False)
    realm: str
    hec_host: str
    hec_port: int
    hec_token: str = field(repr=False)
    hec_index: str = "main"
    logs_connection_id: str | None = None

    @classmethod
    def from_env(cls, environment: Mapping[str, str] | None = None) -> "SplunkConfig":
        source = os.environ if environment is None else environment
        missing = [name for name in _REQUIRED_ENV if not source.get(name, "").strip()]
        if missing:
            raise ProviderError("configuration", f"missing required Splunk environment: {', '.join(missing)}")

        host = source["SPLUNK_HOST"].strip()
        realm = source["SFX_REALM"].strip()
        port_text = source["SPLUNK_HEC_PORT"].strip()
        index = source.get("SPLUNK_HEC_INDEX", "main").strip()
        logs_connection_id = source.get("SPLUNK_LOGS_CONNECTION_ID", "").strip() or None
        if _HOST_PATTERN.fullmatch(host) is None:
            raise ProviderError("configuration", "SPLUNK_HOST must be a hostname without a scheme, port, or path")
        if _REALM_PATTERN.fullmatch(realm) is None:
            raise ProviderError("configuration", "SFX_REALM has an invalid format")
        if not port_text.isascii() or not port_text.isdigit():
            raise ProviderError("configuration", "SPLUNK_HEC_PORT must be a numeric TLS port")
        port = int(port_text)
        if not 1 <= port <= 65535:
            raise ProviderError("configuration", "SPLUNK_HEC_PORT must be between 1 and 65535")
        if _INDEX_PATTERN.fullmatch(index) is None:
            raise ProviderError("configuration", "SPLUNK_HEC_INDEX has an invalid format")
        if logs_connection_id is not None and _CONNECTION_ID_PATTERN.fullmatch(logs_connection_id) is None:
            raise ProviderError("configuration", "SPLUNK_LOGS_CONNECTION_ID has an invalid format")

        return cls(
            access_token=source["SF_TOKEN"].strip(),
            ingest_token=source["SPLUNK_O11Y_INGEST_TOKEN"].strip(),
            realm=realm,
            hec_host=host,
            hec_port=port,
            hec_token=source["SPLUNK_HEC_TOKEN"].strip(),
            hec_index=index,
            logs_connection_id=logs_connection_id,
        )

    @property
    def hec_endpoint(self) -> str:
        return f"https://{self.hec_host}:{self.hec_port}/services/collector/event"

    def artifact_metadata(self) -> dict[str, str | int]:
        """Return the non-secret destination fields permitted in run evidence."""
        return {
            "hec_host": self.hec_host,
            "hec_index": self.hec_index,
            "hec_port": self.hec_port,
            "realm": self.realm,
        }


def _gateway_telemetry_resource_attributes(run_id: str) -> list[dict[str, str]]:
    """Preserve chart defaults and scope gateway exporter counters to one attempt."""
    return [
        {"name": "service.name", "value": "otel-collector"},
        {"name": "otelcol.service.mode", "value": "gateway"},
        {"name": "k8s.node.name", "value": "${K8S_NODE_NAME}"},
        {"name": "k8s.pod.name", "value": "${K8S_POD_NAME}"},
        {"name": "k8s.pod.uid", "value": "${K8S_POD_UID}"},
        {"name": "k8s.namespace.name", "value": "${K8S_NAMESPACE}"},
        {"name": "k8s.cluster.name", "value": run_id},
        {"name": "sregym.run.id", "value": run_id},
        {"name": "deployment.environment", "value": run_id},
        {"name": "deployment.environment.name", "value": run_id},
    ]


@dataclass
class _AttemptReliabilityState:
    opening: ReadinessReport
    opening_snapshot: CollectorSnapshot
    first_visible_lag_ms: dict[SignalName, float | None]
    queue_high_water: dict[SignalName, int | None]
    delivery: DeliveryReport | None = None


class SplunkObservabilityProvider:
    """Deploy the collector while keeping destination credentials out of Helm."""

    name: ProviderName = "splunk"

    def __init__(
        self,
        configuration: SplunkConfig,
        *,
        core_api: CoreV1Api | None = None,
        run: CommandRunner | None = None,
        values_path: Path = _DEFAULT_VALUES_PATH,
        backend: SplunkQueryBackend | None = None,
        policy: ReliabilityPolicy | None = None,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        jitter: Callable[[float], float] | None = None,
    ) -> None:
        self.configuration = configuration
        self._core = core_api
        self._run = run or _run_command
        self._values_path = values_path
        self._backend = backend
        self._policy = policy or ReliabilityPolicy()
        self._now = now or (lambda: datetime.now(UTC))
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or time.sleep
        self._jitter = jitter or (lambda delay: random.uniform(0.0, min(1.0, delay * 0.2)))
        self._prepared_export: ExternalOtlpExport | None = None
        self._connection_id: str | None = None
        self._reliability: dict[str, _AttemptReliabilityState] = {}
        self._touched = False
        self._closed = False

    def preflight(self) -> None:
        if not self._values_path.is_file():
            raise ProviderError("configuration", "Splunk collector values are unavailable")
        self._run_checked(["helm", "version", "--short"], None, "configuration", "Helm is unavailable")
        try:
            self._core_api().get_api_resources()
        except ApiException as error:
            _raise_api_error(error, "Kubernetes API preflight failed")
        except Exception:
            raise ProviderError("configuration", "Kubernetes API preflight failed") from None
        self._connection_id = self._resolve_connection()

    def prepare_attempt(self, context: AttemptContext) -> ExternalOtlpExport:
        if self._closed:
            raise ProviderError("configuration", "the Splunk provider is already closed")
        if self._prepared_export is not None and self._prepared_export.run_id == context.run_id:
            return self._prepared_export

        self._touched = True
        self._ensure_namespace()
        self._upsert_secret()
        runtime_values = {
            "clusterName": context.run_id,
            "extraAttributes": {
                "custom": [
                    {"name": "sregym.run.id", "value": context.run_id},
                    {"name": "deployment.environment", "value": context.run_id},
                    {"name": "deployment.environment.name", "value": context.run_id},
                ]
            },
            "gateway": {
                "config": {
                    "service": {
                        "telemetry": {
                            "resource": {"attributes": _gateway_telemetry_resource_attributes(context.run_id)}
                        }
                    }
                }
            },
            "splunkObservability": {"realm": self.configuration.realm},
            "splunkPlatform": {
                "endpoint": self.configuration.hec_endpoint,
                "index": self.configuration.hec_index,
                "insecureSkipVerify": False,
            },
        }
        command = [
            "helm",
            "upgrade",
            "--install",
            RELEASE_NAME,
            CHART_NAME,
            "--repo",
            CHART_REPOSITORY,
            "--repository-config",
            os.devnull,
            "--version",
            CHART_VERSION,
            "--namespace",
            NAMESPACE,
            "--atomic",
            "--wait",
            "--timeout",
            _HELM_TIMEOUT,
            "--values",
            str(self._values_path),
            "--values",
            "-",
        ]
        self._run_checked(
            command,
            yaml.safe_dump(runtime_values, sort_keys=True),
            "configuration",
            "Splunk collector deployment failed",
        )
        export = ExternalOtlpExport(
            endpoint=f"{RELEASE_NAME}.{NAMESPACE}.svc.cluster.local:4317",
            run_id=context.run_id,
            resource_attributes={
                "deployment.environment": context.run_id,
                "deployment.environment.name": context.run_id,
                "sregym.run.id": context.run_id,
            },
        )
        self._prepared_export = export
        return export

    def wait_until_queryable(self, context: AttemptContext, scope: ApplicationScope) -> ReadinessReport:
        self._require_prepared(context)
        connection_id = self._connection_id or self._resolve_connection()
        self._connection_id = connection_id
        opening, first_visible = self._poll_readiness(context, scope, connection_id, strict=True)
        opening_snapshot = self._snapshot_with_retry(context, strict=True)
        state = _AttemptReliabilityState(
            opening=opening,
            opening_snapshot=opening_snapshot,
            first_visible_lag_ms=first_visible,
            queue_high_water=opening_snapshot.queue_size.copy(),
        )
        self._reliability[context.run_id] = state
        return opening

    def finish_attempt(self, context: AttemptContext, scope: ApplicationScope) -> DeliveryReport:
        state = self._reliability.get(context.run_id)
        if state is None:
            raise ProviderError("configuration", "opening readiness must complete before delivery assurance")
        if state.delivery is not None:
            return state.delivery

        connection_id = self._connection_id or self._resolve_connection()
        closing, _ = self._poll_readiness(context, scope, connection_id, strict=False)
        closing_snapshot = self._snapshot_with_retry(context, strict=False)
        self._update_high_water(state.queue_high_water, closing_snapshot.queue_size)
        final_snapshot = closing_snapshot
        drained = self._queues_drained(final_snapshot.queue_size)
        drain_started = self._monotonic()
        for attempt in range(self._policy.max_attempts):
            elapsed = self._monotonic() - drain_started
            if drained or elapsed >= self._policy.drain_timeout_seconds:
                break
            if not self._sleep_before_deadline(
                self._backoff(attempt, None), elapsed, self._policy.drain_timeout_seconds
            ):
                break
            final_snapshot = self._snapshot_with_retry(context, strict=False, attempts=1)
            self._update_high_water(state.queue_high_water, final_snapshot.queue_size)
            drained = self._queues_drained(final_snapshot.queue_size)

        sent_delta = self._deltas(state.opening_snapshot.sent, closing_snapshot.sent)
        failed_delta = self._deltas(state.opening_snapshot.send_failed, closing_snapshot.send_failed)
        enqueue_delta = self._deltas(state.opening_snapshot.enqueue_failed, closing_snapshot.enqueue_failed)
        all_counters = (*sent_delta.values(), *failed_delta.values(), *enqueue_delta.values())
        queues_available = all(value is not None for value in state.queue_high_water.values()) and all(
            value is not None for value in final_snapshot.queue_size.values()
        )
        valid = (
            state.opening.ready
            and closing.ready
            and all(value is not None for value in all_counters)
            and all(value == 0 for value in failed_delta.values())
            and all(value == 0 for value in enqueue_delta.values())
            and queues_available
            and drained
        )
        state.delivery = DeliveryReport(
            run_id=context.run_id,
            opening=state.opening,
            closing=closing,
            first_visible_lag_ms=state.first_visible_lag_ms,
            sent_delta=sent_delta,
            send_failed_delta=failed_delta,
            enqueue_failed_delta=enqueue_delta,
            queue_high_water=state.queue_high_water,
            queue_final_size=final_snapshot.queue_size,
            drained=drained,
            valid=valid,
        )
        return state.delivery

    def close(self) -> None:
        if self._closed:
            return None
        if not self._touched:
            if self._backend is not None:
                self._backend.close()
            self._closed = True
            return None

        cleanup_failed = False
        try:
            self._run_checked(
                ["helm", "uninstall", RELEASE_NAME, "--namespace", NAMESPACE, "--ignore-not-found", "--wait"],
                None,
                "cleanup",
                "Splunk collector cleanup failed",
            )
        except ProviderError:
            cleanup_failed = True

        try:
            self._core_api().delete_namespaced_secret(name=SECRET_NAME, namespace=NAMESPACE)
        except ApiException as error:
            if error.status != 404:
                cleanup_failed = True
        except Exception:
            cleanup_failed = True

        if self._backend is not None:
            self._backend.close()

        if cleanup_failed:
            raise ProviderError("cleanup", "Splunk provider cleanup failed") from None
        self._closed = True
        return None

    def _core_api(self) -> CoreV1Api:
        if self._core is None:
            try:
                kubernetes_config.load_kube_config()
                self._core = client.CoreV1Api()
            except Exception:
                raise ProviderError("configuration", "Kubernetes access is unavailable") from None
        return self._core

    def _query_backend(self) -> SplunkQueryBackend:
        if self._backend is None:
            self._backend = SplunkHttpBackend(self.configuration)
        return self._backend

    def _require_prepared(self, context: AttemptContext) -> None:
        if self._prepared_export is None or self._prepared_export.run_id != context.run_id:
            raise ProviderError("configuration", "the Splunk provider must be prepared for this attempt")

    def _resolve_connection(self) -> str:
        started = self._monotonic()
        for attempt in range(self._policy.max_attempts):
            try:
                return self._query_backend().resolve_logs_connection(
                    self._policy.request_timeout_seconds,
                    self.configuration.logs_connection_id,
                )
            except SplunkBackendError as error:
                if not error.transient:
                    self._raise_backend_error(error, None)
                if attempt + 1 >= self._policy.max_attempts:
                    continue
                elapsed = self._monotonic() - started
                if elapsed >= self._policy.readiness_timeout_seconds:
                    break
                if not self._sleep_before_deadline(
                    self._backoff(attempt, error.retry_after_seconds),
                    elapsed,
                    self._policy.readiness_timeout_seconds,
                ):
                    break
        raise ProviderError("transient_exhausted", "Splunk logs connection lookup retries were exhausted") from None

    def _poll_readiness(
        self,
        context: AttemptContext,
        scope: ApplicationScope,
        connection_id: str,
        *,
        strict: bool,
    ) -> tuple[ReadinessReport, dict[SignalName, float | None]]:
        started = self._monotonic()
        readiness: dict[SignalName, SignalReadiness] = {}
        first_visible: dict[SignalName, float | None] = dict.fromkeys(SIGNAL_NAMES)
        last_transient = False
        retry_after: float | None = None
        for attempt in range(self._policy.max_attempts):
            checked_at = self._now()
            for signal in SIGNAL_NAMES:
                if signal in readiness and readiness[signal].ready:
                    continue
                try:
                    count = self._query_backend().query_signal(
                        signal,
                        context,
                        scope,
                        connection_id,
                        checked_at,
                        self._policy.request_timeout_seconds,
                    )
                    ready = count > 0
                    evidence: dict[str, str | int | float | bool | None] = {
                        "count": count,
                        "status_class": "2xx",
                    }
                    if signal in ("logs", "kubernetes_events"):
                        evidence.update(
                            {
                                "connection_id": connection_id,
                                "index": self.configuration.hec_index,
                            }
                        )
                    readiness[signal] = SignalReadiness(
                        signal=signal,
                        ready=ready,
                        checked_at=checked_at,
                        evidence=evidence,
                    )
                    if ready and first_visible[signal] is None:
                        first_visible[signal] = max(
                            0.0, (checked_at - context.attempt_started_at).total_seconds() * 1000
                        )
                except SplunkBackendError as error:
                    last_transient = error.transient
                    retry_after = error.retry_after_seconds
                    failure_evidence: dict[str, str | int | float | bool | None] = {
                        "status_class": _status_class(error.status_code)
                    }
                    if signal in ("logs", "kubernetes_events"):
                        failure_evidence.update(
                            {
                                "connection_id": connection_id,
                                "index": self.configuration.hec_index,
                            }
                        )
                    readiness[signal] = SignalReadiness(
                        signal=signal,
                        ready=False,
                        checked_at=checked_at,
                        evidence=failure_evidence,
                    )
                    if not error.transient:
                        report = self._readiness_report(context.run_id, readiness, checked_at)
                        if strict:
                            self._raise_backend_error(error, report)
                        return report, first_visible
            report = self._readiness_report(context.run_id, readiness, checked_at)
            if report.ready:
                return report, first_visible
            if attempt + 1 >= self._policy.max_attempts:
                continue
            elapsed = self._monotonic() - started
            if elapsed >= self._policy.readiness_timeout_seconds:
                break
            if not self._sleep_before_deadline(
                self._backoff(attempt, retry_after), elapsed, self._policy.readiness_timeout_seconds
            ):
                break
            retry_after = None

        report = self._readiness_report(context.run_id, readiness, self._now())
        if strict:
            kind: FailureKind = "transient_exhausted" if last_transient else "readiness_timeout"
            message = "Splunk query retries were exhausted" if last_transient else "Splunk telemetry was not queryable"
            raise ProviderError(kind, message, readiness_report=report)
        return report, first_visible

    def _readiness_report(
        self,
        run_id: str,
        readiness: dict[SignalName, SignalReadiness],
        checked_at: datetime,
    ) -> ReadinessReport:
        signals = tuple(
            readiness.get(
                signal,
                SignalReadiness(signal=signal, ready=False, checked_at=checked_at, evidence={}),
            )
            for signal in SIGNAL_NAMES
        )
        return ReadinessReport(run_id=run_id, signals=signals, ready=all(item.ready for item in signals))

    def _snapshot_with_retry(
        self,
        context: AttemptContext,
        *,
        strict: bool,
        attempts: int | None = None,
    ) -> CollectorSnapshot:
        limit = attempts or self._policy.max_attempts
        started = self._monotonic()
        for attempt in range(limit):
            try:
                return self._query_backend().collector_snapshot(
                    context, self._now(), self._policy.request_timeout_seconds
                )
            except SplunkBackendError as error:
                if not error.transient:
                    if strict:
                        self._raise_backend_error(error, None)
                    break
                if attempt + 1 >= limit:
                    continue
                elapsed = self._monotonic() - started
                if elapsed >= self._policy.readiness_timeout_seconds:
                    break
                if not self._sleep_before_deadline(
                    self._backoff(attempt, error.retry_after_seconds),
                    elapsed,
                    self._policy.readiness_timeout_seconds,
                ):
                    break
        if strict:
            raise ProviderError("transient_exhausted", "Splunk collector metric retries were exhausted")
        return _unavailable_snapshot()

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        base = min(
            self._policy.max_backoff_seconds,
            self._policy.initial_backoff_seconds * (2**attempt),
        )
        if retry_after is not None:
            base = max(base, min(retry_after, self._policy.retry_after_cap_seconds))
        return base + max(0.0, self._jitter(base))

    def _sleep_before_deadline(self, proposed: float, elapsed: float, timeout: float) -> bool:
        remaining = max(0.0, timeout - elapsed)
        delay = min(proposed, remaining)
        self._sleep(delay)
        return proposed < remaining

    @staticmethod
    def _raise_backend_error(error: SplunkBackendError, report: ReadinessReport | None) -> NoReturn:
        if error.status_code == 401:
            kind: FailureKind = "authentication"
        elif error.status_code == 403:
            kind = "permission"
        else:
            kind = "configuration"
        raise ProviderError(kind, "Splunk query request was rejected", readiness_report=report) from None

    @staticmethod
    def _deltas(
        opening: dict[SignalName, int | None], closing: dict[SignalName, int | None]
    ) -> dict[SignalName, int | None]:
        return {signal: _counter_delta(opening[signal], closing[signal]) for signal in SIGNAL_NAMES}

    @staticmethod
    def _update_high_water(high_water: dict[SignalName, int | None], sample: dict[SignalName, int | None]) -> None:
        for signal in SIGNAL_NAMES:
            current = high_water[signal]
            value = sample[signal]
            high_water[signal] = None if current is None or value is None else max(current, value)

    @staticmethod
    def _queues_drained(queue_size: dict[SignalName, int | None]) -> bool:
        return all(value == 0 for value in queue_size.values())

    def _ensure_namespace(self) -> None:
        body = client.V1Namespace(metadata=client.V1ObjectMeta(name=NAMESPACE))
        try:
            self._core_api().create_namespace(body)
        except ApiException as error:
            if error.status != 409:
                _raise_api_error(error, "Splunk collector namespace creation failed")
        except Exception:
            raise ProviderError("configuration", "Splunk collector namespace creation failed") from None

    def _upsert_secret(self) -> None:
        secret = client.V1Secret(
            metadata=client.V1ObjectMeta(name=SECRET_NAME, namespace=NAMESPACE),
            string_data={
                "splunk_observability_access_token": self.configuration.ingest_token,
                "splunk_platform_hec_token": self.configuration.hec_token,
            },
            type="Opaque",
        )
        api = self._core_api()
        try:
            api.create_namespaced_secret(namespace=NAMESPACE, body=secret)
        except ApiException as error:
            if error.status != 409:
                _raise_api_error(error, "Splunk collector Secret creation failed")
            try:
                api.patch_namespaced_secret(name=SECRET_NAME, namespace=NAMESPACE, body=secret)
            except ApiException as update_error:
                _raise_api_error(update_error, "Splunk collector Secret update failed")
            except Exception:
                raise ProviderError("configuration", "Splunk collector Secret update failed") from None
        except Exception:
            raise ProviderError("configuration", "Splunk collector Secret creation failed") from None

    def _run_checked(
        self,
        command: list[str],
        stdin: str | None,
        failure_kind: FailureKind,
        safe_message: str,
    ) -> None:
        try:
            result = self._run(command, stdin)
        except Exception:
            raise ProviderError(failure_kind, safe_message) from None
        if result.returncode != 0:
            raise ProviderError(failure_kind, safe_message) from None


def _run_command(command: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, input=stdin, capture_output=True, text=True, check=False)


def _status_class(status_code: int | None) -> str:
    return "transport" if status_code is None else f"{status_code // 100}xx"


def _unavailable_snapshot() -> CollectorSnapshot:
    unavailable: dict[SignalName, int | None] = dict.fromkeys(SIGNAL_NAMES)
    return CollectorSnapshot(
        sent=unavailable.copy(),
        send_failed=unavailable.copy(),
        enqueue_failed=unavailable.copy(),
        queue_size=unavailable.copy(),
    )


def _raise_api_error(error: ApiException, safe_message: str) -> NoReturn:
    if error.status == 401:
        kind: FailureKind = "authentication"
    elif error.status == 403:
        kind = "permission"
    else:
        kind = "configuration"
    raise ProviderError(kind, safe_message) from None


__all__ = [
    "CHART_VERSION",
    "NAMESPACE",
    "RELEASE_NAME",
    "SECRET_NAME",
    "CollectorSnapshot",
    "ReliabilityPolicy",
    "SplunkBackendError",
    "SplunkConfig",
    "SplunkHttpBackend",
    "SplunkObservabilityProvider",
]

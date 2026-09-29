# Contract: SRE Gym Runner ↔ Observability Provider

## Purpose

This is the only interface through which the runner controls an external observability destination. It contains no Assistant-specific type or behavior.

## Types

```python
ProviderName = Literal["none", "splunk"]
SignalName = Literal["metrics", "traces", "logs", "kubernetes_events"]
FailureKind = Literal[
    "configuration",
    "authentication",
    "permission",
    "transient_exhausted",
    "readiness_timeout",
    "cleanup",
]

@dataclass(frozen=True)
class AttemptContext:
    run_id: str                 # ^anon_[0-9a-f]{32}$; contains no problem ID
    profile: str                # full or svelte
    comparable: bool
    attempt_started_at: datetime  # timezone-aware UTC

@dataclass(frozen=True)
class ApplicationScope:
    app_name: str
    namespaces: tuple[str, ...]

@dataclass(frozen=True)
class SignalReadiness:
    signal: SignalName
    ready: bool
    checked_at: datetime
    evidence: dict[str, str | int | float | bool | None]  # identifiers/counts only; no payload bodies

@dataclass(frozen=True)
class ReadinessReport:
    run_id: str
    signals: tuple[SignalReadiness, ...]  # exactly one of each SignalName
    ready: bool                          # true iff every signal is ready

@dataclass(frozen=True)
class DeliveryReport:
    run_id: str
    opening: ReadinessReport
    closing: ReadinessReport
    first_visible_lag_ms: dict[SignalName, float | None]
    sent_delta: dict[SignalName, int | None]
    send_failed_delta: dict[SignalName, int | None]
    enqueue_failed_delta: dict[SignalName, int | None]
    queue_high_water: dict[SignalName, int | None]
    queue_final_size: dict[SignalName, int | None]
    drained: bool
    valid: bool
    queue_drain_minimum: dict[SignalName, int | None] | None = None  # post-quiescence, ingestion-complete interval
```

Provider methods:

```python
class ObservabilityProvider(Protocol):
    name: ProviderName

    def preflight(self) -> None: ...
    def prepare_attempt(self, context: AttemptContext) -> ExternalOtlpExport | None: ...
    def wait_until_queryable(
        self, context: AttemptContext, scope: ApplicationScope
    ) -> ReadinessReport: ...
    def finish_attempt(
        self, context: AttemptContext, scope: ApplicationScope
    ) -> DeliveryReport: ...
    def close(self) -> None: ...
```

`ExternalOtlpExport` contains only an in-cluster OTLP endpoint, opaque run identity, and non-secret resource attributes. It never contains destination credentials.

## Lifecycle

1. `preflight` runs once before the first problem deployment and is side-effect-free except authenticated health/read checks.
2. `prepare_attempt` runs after SRE Gym removes prior app leftovers and before Prometheus/Jaeger/OTel/app deployment. It is idempotent for the same `run_id`.
3. The returned OTLP configuration is supplied to SRE Gym's central collector. `None` preserves the original manifest and behavior.
4. `wait_until_queryable` runs after the fault is injected but before an agent process is launched.
5. `finish_attempt` runs after agent/judge completion but before destructive SRE Gym teardown. It performs a closing query, snapshots collector counters, waits boundedly for exporter queues to drain, and returns a delivery report.
6. SRE Gym cleanup runs even if delivery auditing fails. The failure is recorded and does not erase earlier artifacts.
7. `close` removes/neutralizes temporary secrets and provider resources at campaign shutdown; it is idempotent.

The `none` provider returns `None`, performs no Kubernetes/network calls, and produces an immediately ready report. Its behavior must be observationally identical to the pre-feature runner.

## Splunk Configuration

| Variable | Required | Use |
|---|---:|---|
| `SF_TOKEN` | yes | Splunk Observability user/API token for readiness queries |
| `SPLUNK_O11Y_INGEST_TOKEN` | yes | Splunk Observability org token with `INGEST` scope for collector export |
| `SFX_REALM` | yes | Splunk Observability realm |
| `SPLUNK_HOST` | yes | HEC hostname only; no embedded credentials |
| `SPLUNK_HEC_PORT` | yes | Numeric HEC TLS port |
| `SPLUNK_HEC_TOKEN` | yes | Container logs/Kubernetes events via HEC |
| `SPLUNK_HEC_INDEX` | no | Logs index; defaults to `main`, must be allowed by the HEC token, and is recorded |

The provider CLI selection is `--observability-provider none|splunk`, default `none`. The logs connection is resolved exactly as Assistant resolves it: accessible default first, otherwise first accessible connection; no connection hint is added to the model prompt. No provider credential is forwarded to unrelated agent containers.

## Splunk Deployment Rules

- Helm chart version is exactly `0.160.0`.
- The release and namespace names are constant and contain no problem ID.
- `clusterName` and resource attribute `sregym.run.id` equal `AttemptContext.run_id`.
- A Kubernetes Secret is created/updated with keys expected by the chart and referenced using `secret.create=false`.
- Secret values may be supplied only as Kubernetes API request bodies; never as Helm arguments, files in the repository, logs, exceptions, or artifact evidence.
- HEC uses `https://<SPLUNK_HOST>:<SPLUNK_HEC_PORT>/services/collector/event`; certificate verification remains enabled.
- Chart resources are bounded and use one gateway replica for v1. No HA/autoscaling work is in scope.
- Existing local Jaeger, Prometheus, Loki, and MCP deployments remain enabled and unchanged.
- The Splunk gateway may scrape only SRE Gym Prometheus's `/federate` endpoint for application metrics. It must not independently discover every application endpoint or alter Prometheus's scrape configuration.
- Federated points carry `sregym.metric.source=sregym_prometheus_application` and the same opaque attempt resource attributes as the other signals.
- Federation excludes Kubernetes and collector/infrastructure scrape jobs already exported by the chart. It must not rely on an application metric-name allowlist or forward the unfiltered Prometheus catalog. It also drops Prometheus's `up` scrape-control series: an inactive application family can legitimately retain `up=0`, which is not application telemetry and otherwise causes the downstream receiver to report a false federation failure.

## Readiness Semantics

The Splunk implementation must observe, not merely send, all four signals using `run_id`, `ApplicationScope.namespaces`, and `attempt_started_at`:

- `metrics`: at least one run-scoped Kubernetes metric from the provider collector and at least one run-scoped application metric marked `sregym.metric.source=sregym_prometheus_application` are returned.
- `traces`: at least one application span/trace is returned.
- `logs`: at least one container-log event is returned through the selected Logs Observer connection.
- `kubernetes_events`: at least one Kubernetes event record is returned through the selected Logs Observer connection.

For the complete-Lite campaign, the pinned chart's `k8sObjects` list is explicitly limited to `pods` and `events` in watch mode. The chart's separate `k8s_events` receiver is disabled; Kubernetes-event readiness therefore queries `sourcetype="kube:object:events"` rather than the former `k8s.event.reason` field. Both object kinds use the existing `logs/objects` HEC pipeline and the explicitly selected Logs Observer connection. The chart's inherited cluster-metrics RBAC may mention other resource types but is not an object-export allowlist. Pod/event body values are never included in committed case contracts or sanitized proof artifacts.

Polling has one overall configurable deadline with a documented default, a bounded attempt count, exponential backoff with jitter, and a per-request timeout. HTTP 408/429/5xx and connect/read timeouts are transient. Malformed configuration and HTTP 400/401/403 are terminal. A deadline with any missing signal raises a typed provider error carrying a secret-free `ReadinessReport`.

Provider evidence may contain timestamps, counts, signal names, HTTP status classes, connection ID, index name, and hashed backend object IDs. It must not contain telemetry payload bodies, tokens, Authorization headers, problem IDs, or oracle data.

## Delivery Assurance

Readiness proves that each signal traversed the complete source-to-query path before Assistant started. `finish_attempt` adds bounded loss and lag evidence:

- Repeat all four signal queries across the attempt window and preserve the closing readiness result.
- Snapshot cumulative collector metrics at opening and closing and report deltas for sent, send-failed, and enqueue-failed records, points, or spans.
- Sample queue size during the attempt to retain a high-water mark, then poll until all applicable exporter queues reach zero or the drain deadline expires.
- Compute first-visible lag from `attempt_started_at` to the first successful query for each signal.
- Set `valid=true` only when opening and closing readiness succeed, failure deltas are zero, and queues drain. Collector failure counters are sparse and do not create a series before the first failure; a missing failure series is normalized to its initial value of zero only when the same successful snapshot contains both matching sent and queue series. Without both companion series, the counter is unavailable (`null`) and makes the report invalid.

This is evidence of reliable bounded delivery, not a claim that every possible source record was captured. No source payload is copied into the report.

## Runner Outcome Mapping

| Provider result | Runner behavior |
|---|---|
| all signals ready | launch exactly one configured agent |
| configuration/authentication/permission error | `infrastructure_invalid`; no agent launch; fail fast |
| transient retry exhaustion/readiness timeout | `infrastructure_invalid`; no agent launch; safe cleanup; resumable |
| post-execution delivery report invalid | preserve agent/judge artifacts; mark `infrastructure_invalid`; exclude from diagnosis-rate aggregation |
| cleanup failure | preserve prior status plus cleanup failure; do not start the next attempt against uncertain state |

The provider never calls the judge, submits a diagnosis, retries an Assistant session, or decides diagnosis pass/fail.

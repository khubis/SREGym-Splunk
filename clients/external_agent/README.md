# External diagnosis adapter

Provider-neutral building blocks for a diagnosis-only agent connected to SREGym's
local Conductor. They leave the benchmark's fault injection, readiness checks,
prompt context, grading rubric and judge configuration under the native runner.
This package does not provision a cluster, dispatch a suite, create integrations,
publish results, or resize observability services.

## Integration contract

The outer runner prepares a fresh namespace, dedicated service-account access,
fixed evidence connections and a private output directory for each attempt. It
owns the native lifecycle and must verify cleanup independently before counting
a grade or starting the next case. A separate output directory alone does not
authorize repeating a logical case.

`driver.investigate(session, conductor, out, context, binding, guard=...)` accepts
an initialized MCP session and a `ConductorHTTP` instance. The MCP service must
implement this small contract:

| Tool | Request | Required result |
| --- | --- | --- |
| `create_issue` | `description` | Unique `slug` matching `ISS-<positive integer>` |
| `get_issue` | `id`, `include: [result, timeline]`, `wait_seconds` | Same `slug`, `running`; terminal `answer` or assistant `timeline` text |

Results may use MCP `structuredContent` or a JSON text object. Authentication and
session initialization belong to the provider-specific caller. Endpoints and
credentials must be supplied privately at runtime; none are configured here.

The public context has exactly these fields:

```json
{
  "app_name": "Example application",
  "app_description": "Public application description",
  "app_namespace": "example-app",
  "symptom": "Requests are failing",
  "incident_started_at": "2030-01-01T00:00:00Z"
}
```

The binding has exactly `namespace`, `namespace_uid`, `kubernetes_connection`,
`grafana_connection` and `prometheus_connection`. Connection values identify the
dedicated evidence connections for this attempt. Grafana uses the datasource UID
`sregym-loki`. Hidden fault names, oracle text, benchmark logs and expected
answers are refused by the public context schema.

The caller's synchronous `guard` must verify current namespace UID, protected
resource identities, native readiness and evidence access. It runs before
creation, each poll and submission. `ConductorHTTP` permits only an HTTP loopback
endpoint and requires the `diagnosis` stage. The MCP driver's budget is an integer
from 1 to 1200 seconds; the native runner remains responsible for its own agent
deadline.

Only one creation and one submission are reserved per output directory. A lost
creation or submission response requires reconciliation, not another request.
An empty terminal response permits reads of that same issue for at most 60
seconds, within the original deadline. A valid answer is submitted once; the
driver does not invoke a judge. Runtime answer/state files contain private
evidence and belong under an ignored, mode-0700 directory such as
`.runtime/external-agent/`.

## Evidence gateways

Both gateways bind to loopback. Remote exposure requires a separately configured
authenticated TLS transport. Neither module launches or replaces public tunnels.

The Kubernetes gateway uses a private, owned JSON kubeconfig containing exactly
one cluster, context and token-only service account. It verifies the cluster CA;
credential reloads may change only the bearer token. Client certificates, exec
plugins and endpoint/CA/identity changes are refused.

```bash
python -m clients.external_agent.gateways.kubernetes \
  --namespace example-app --namespace-uid CAPTURED_NAMESPACE_UID \
  --fixture-receipt .runtime/external-agent/access.json \
  --token-file .runtime/external-agent/gateway-token \
  --kubeconfig .runtime/external-agent/kubeconfig.json --context example \
  --audit .runtime/external-agent/kubernetes-audit.jsonl \
  --port 18984 --duration 1200
```

The fixture receipt contains `namespace`, `status: case_ready` and `objects`.
Capture the UID and `rbac_policy_sha256` of the dedicated `external-readonly`
Role/RoleBinding and `<namespace>-namespace` ClusterRole/ClusterRoleBinding.
Compute the policy digest over `json.dumps(policy, sort_keys=True).encode()`;
`policy` contains only present `rules`, `subjects` and `roleRef` fields. The
gateway validates these objects by identity and policy before projecting an
RBAC collection, independent of receipt ordering. It does not create them.

Finite namespace reads and nonpersistent self-permission reviews are supported.
Secrets, exec, port-forwarding, watches, streaming logs and persistent mutations
are denied. Discovery is restricted and all-namespace collections are translated
to the owned namespace before upstream reads. Logs and response sizes are bounded.
The optional `--scope-file` enables only explicitly captured incident-related
RBAC, webhook or CoreDNS ConfigMap UIDs through `evidence.projected_read`; it
does not grant arbitrary cluster reads or authorize a DNS mutation.

The telemetry gateway is started separately:

```bash
python -m clients.external_agent.gateways.telemetry \
  --config .runtime/external-agent/telemetry.json
```

Its private mode-0600 JSON configuration requires `mode` (`grafana` or
`prometheus`), loopback `upstream`, `listen_port`, `credential_file`,
`guard_file` and `audit_file`. Prometheus also needs a `username`; Grafana needs
`viewer_token_file`, `viewer_expires_at` (Unix seconds) and the allowed numeric
`datasource_ids`. The guard file contains a fresh `valid_until` Unix timestamp,
issued only after the outer runner verifies the origin identity. Credentials
are read from private files, never command arguments. Grafana datasource UIDs
are limited to `sregym-prometheus` and `sregym-loki`.

The gateway permits finite metadata/queries and read-query POSTs, removes caller
cookies/Host/auth before forwarding, bounds bodies and records hashed paths.
These route restrictions do **not** enforce per-trial PromQL/LogQL isolation;
use appropriately isolated origins and credentials where that is required.

## Recovery and retained results

* `readiness` waits at most 120 seconds for exact ordinary protected-Pod
  readiness errors, calling the same authoritative predicate again. Identity,
  node and native DNS errors fail immediately. It never repeats a mutation.
* `telemetry.wait_for_telemetry` requires the actual fresh UID/log predicate
  inside its existing 120-second setup window. Only transport timeouts and
  explicit `TelemetryPending` observations wait. Rejected HTTP responses record
  status, method, path/body hashes and length, then raise without a retry.
* `routes` pins fixed public destinations, runtime/source manifests and full
  process identities. Only the same controller's exact owned forward recipe
  may admit a replacement forward, with an exclusive loopback listener and
  current origin authority. No fallback URL or foreign process is adopted.
  Live inspection requires POSIX `ps` and `lsof`.
* `transport.retention_step` is a callback-based retention decision helper;
  callers provide narrowly owned forward recovery and origin checks. It keeps
  healthy children while degraded and does not replace gateways or tunnels.
* `verification.prevent_native_grade_replay` must run before a retry admission,
  using the exact previous attempt's native result path. Any saved `Diagnosis`
  evaluation, including an error with a null external grade, forbids another
  investigation. Retain a valid low/failing grade; infrastructure errors remain
  ungraded. `verified_diagnosis` checks the answer, one evaluation, successful
  native cleanup and final preservation receipts before returning a grade.
* `verification.verify_freeze` checks a caller-pinned SHA-256 manifest with
  relative `files` and optional nested `manifests` maps. Validate nested
  qualification as well as outer implementation hashes before startup.
* `cleanup.delete_access` is an explicitly invoked utility for captured global
  access objects, with UID, policy hash and fresh resourceVersion preconditions.
  An exclusive receipt reserves cleanup before requests; uncertain responses
  hold without replay. The caller must establish ownership, authorization and
  strict preservation first. It performs no namespace, volume, finalizer,
  node, shared service or general garbage-collection operations.

The outer runner must preserve failed attempt bytes, prove cleanup/exits and
diagnose an external harness failure before admitting a new attempt. A reporting
or cleanup recovery retains the original valid grade, never a new evaluation.
Environment maintenance, such as changing an owned observer's memory capacity,
must be recorded separately; this library does not automate that operation.

## Offline validation

From the repository root with its Python dependencies installed:

```bash
python -m pytest tests/external_agent -q
ruff check clients/external_agent tests/external_agent
ruff format --check clients/external_agent tests/external_agent
```

Tests use synthetic data, mocked providers and temporary loopback servers. The
TLS fixture requires `openssl`. No live provider, cluster, issue, fault injection
or model evaluation is used. These tests validate the portable adapter; they do
not establish compatibility with every external MCP service or replace a fresh
native lifecycle qualification for a different source revision/environment.

# Assistant V3: SREGym-Lite evidence map

This document maps the first ten SREGym-Lite diagnosis cases to the evidence an
agent would normally need to reach the benchmark's hidden root cause. It is a
code-derived hypothesis, not an official benchmark contract.

## What SREGym specifies

SREGym does **not** declare a per-scenario, machine-readable evidence contract.
For diagnosis, each problem supplies:

- a hidden structured `root_cause`, used as the expectation for the LLM judge;
- a fault injector that creates the broken state;
- frequently, comments or a docstring describing the intended observable clues;
- a mitigation oracle that inspects the live system after a proposed repair.

The diagnosis judge receives only the submitted diagnosis and the hidden root
cause. It does not verify that the evidence needed to infer that root cause was
available to the agent.

The benchmark's normal agent environment fills this gap operationally. Stratus,
for example, receives Prometheus and Jaeger tools plus a read-only `kubectl`
tool that supports `get`, `describe`, `events`, `logs`, and `top`. The upstream
prompt explicitly tells the agent to enumerate pods and deployments. Therefore,
many scenarios assume that Kubernetes object state is part of the evidence
surface even when metrics, traces, and logs only expose the symptom.

Assistant V3 intentionally has no direct Kubernetes tool in this integration.
Its prompt removes the upstream instruction to enumerate Kubernetes resources,
and the driver treats a direct Kubernetes tool call as a capability-policy
violation. This creates an evidence-parity gap for configuration-centric cases.

Relevant code:

- Lite ordering: `sregym/conductor/problem_sets.py`
- Reference agent tools: `clients/stratus/configs/diagnosis_agent_config.yaml`
- Upstream prompt: `clients/stratus/configs/diagnosis_agent_prompts.yaml`
- Assistant prompt adaptation: `clients/assistant_v3/prompts/diagnosis-v1.yaml`
- Root-cause-only judge: `sregym/conductor/oracles/llm_as_a_judge/llm_as_a_judge_oracle.py`

## What the current Splunk path exports

The current integration sends application container logs and Kubernetes events
to Splunk Platform, and metrics and traces to Splunk Observability Cloud. It also
adds a unique run identity to telemetry and waits until all four signal classes
are queryable.

The approved Kubernetes object receiver now exports Pod and Event objects, in
addition to container logs. Their records can expose a Pod's container states,
probe, DNS policy, and ordered environment entries if the particular object
update arrives and is queryable. This is not yet live-proven for every case.
It does not export the specs of Services, EndpointSlices, NetworkPolicies,
ConfigMaps, ClusterRoles, MutatingWebhookConfigurations, Deployments, or CronJob
templates. Kubernetes metadata attached to a metric is useful context, but is
not equivalent to those missing object specs.

The collector also excludes `kube-system` container logs, so CoreDNS logs are
not present in the Splunk log destination. See
`sregym/observer/splunk/values.yaml`.

Evidence ratings below mean:

- **Strong**: current Splunk signals should name the failing mechanism closely
  enough to support the hidden root cause.
- **Partial**: Splunk should show the failure and perhaps the faulty component,
  but an important causal detail normally comes from Kubernetes or subsystem
  state.
- **Symptom only**: current Splunk signals can show impact, but the defining
  configuration or control-plane cause is not exported.

## First ten Lite cases

| # | Scenario | Hidden cause and decisive evidence | Normal benchmark checks | Current Splunk-only outlook |
|---:|---|---|---|---|
| 1 | `cronjob_sidecar_blocks_completion_hotel_reservation` | `audit-log-archiver` Jobs remain active because the `archiver` container completes while the regular `fluent-bit-sidecar` keeps running. Decisive evidence is multiple Jobs at `0/1`, per-container states, and a CronJob template with both containers under `spec.containers` rather than a native sidecar under `initContainers`. | `kubectl get cronjobs,jobs,pods`; inspect the CronJob and a Job pod YAML; inspect both container statuses and logs. | **Partial.** Application logs identify the archiver and sidecar, and Job/container metrics may show accumulation and divergent states. The exact regular-sidecar template shape is not exported. |
| 2 | `edge_request_filter_cpu_saturation` | `frontend-proxy` runs WAF regex `^([a-zA-Z]+)*$`; 5,001-character near-matches cause catastrophic backtracking and CPU saturation. | Proxy CPU metrics; proxy logs containing `request_filter_eval`, regex, candidate length, and elapsed time; optionally inspect the Deployment command/env and change annotations. | **Strong.** A completed run confirmed the exact regex, crafted input length, evaluation delay, and affected container in scoped Splunk logs. CPU metrics provide corroboration. |
| 3 | `network_policy_block` | NetworkPolicy `deny-all-recommendation` selects `io.kompose.service=recommendation` and has empty ingress and egress rules. Pods stay Running while service traffic times out. | `kubectl get networkpolicy -n hotel-reservation -o yaml`; inspect the selected pod labels; test or trace connectivity to and from `recommendation`. | **Symptom only.** The scoped Splunk run contained repeated workload timeouts, but no log/event or metric named the NetworkPolicy, selector, or rules. The post-grade audit is `partial`. |
| 4 | `env_variable_shadowing_astronomy_shop` | `frontend-proxy` has two ordered `FRONTEND_HOST` entries; the later `localhost` value shadows the intended `frontend` value and redirects the proxy to the wrong upstream. | Inspect the Deployment's ordered container `env` array; correlate the rollout with proxy connection failures or requests to localhost. | **Potentially strong, pending live proof.** The approved Pod-object stream should preserve the ordered environment entries, but the Deployment template itself is not exported. The executable gate compares the same newly created faulty Pod at source and in Splunk. |
| 5 | `mutating_webhook_resource_limits_social_network` | `gatekeeper-mutating-webhook-configuration` changes `nginx-thrift` pod memory from the Deployment's 128Mi/256Mi to 16Mi/16Mi, causing OOMKills. Four decoy webhooks must be ruled out by their rules/object selectors. | Compare Deployment template resources with the live Pod resources and terminated state; inspect all MutatingWebhookConfigurations, namespace/object selectors, rules, and backend; inspect pod events. | **Symptom only** for the exact cause. Splunk should show 16Mi pod limits, restarts/OOMKilled state, and events, but not the Deployment template/webhook configuration comparison needed to identify the active webhook among decoys. |
| 6 | `finalizer_deadlock_controller_hotel_reservation` | ConfigMap `reservation-cleanup-token` is Terminating with finalizer `cleanup.reservations.io/pending-cleanup`; `cleanup-controller` cannot remove it because ClusterRole `configmap-cleanup-controller` lacks `patch`. | Controller logs; ConfigMap metadata; ClusterRole and binding; `kubectl auth can-i patch configmaps --as=system:serviceaccount:hotel-reservation:cleanup-controller`. | **Partial.** The controller deliberately logs the ConfigMap, Terminating cleanup attempt, and HTTP 403, which strongly supports broken controller RBAC. The exact finalizer and missing verb are best confirmed from object/RBAC state, which is not exported. |
| 7 | `kafka_poison_pill_hol_block` | An invalid JSON record at offset 20 in `orders-fulfillment` cannot be deserialized by group `orders-validator`; the offset remains uncommitted and lag grows while pods stay Ready. | Consumer logs; `kafka-consumer-groups.sh --describe`; inspect the record at the blocked offset and later valid records; compare source/output progress. | **Strong.** Consumer logs explicitly emit validation failure, blocked offset, and repeated `partition remains paused`. Kafka admin/data-plane access would corroborate the exact committed offset, lag, and poison payload. |
| 8 | `internal_traffic_policy_local_astronomy_shop` | Service `recommendation` has `internalTrafficPolicy: Local`; its single pod and the `frontend` caller are pinned to different nodes, so kube-proxy silently drops traffic even though pods and endpoints look healthy. The problem code explicitly says the fault is only visible in the Service spec plus node-placement mismatch. | Inspect Service YAML, Endpoints/EndpointSlices, and pod node placement; compare from-node connectivity; use traces/timeouts as impact evidence. | **Symptom only.** Splunk can show timeouts and node placement metadata, but not `service.spec.internalTrafficPolicy`. Healthy pod and endpoint signals cannot prove this cause without Service state. |
| 9 | `service_dns_resolution_failure_social_network` | CoreDNS contains an NXDOMAIN template for `user-service.social-network.svc.cluster.local`; dependent lookups fail although the service pods can remain healthy. | Application DNS errors; `nslookup`/`dig` from a caller; inspect the CoreDNS ConfigMap; inspect CoreDNS logs and rollout. | **Symptom only** for the injected rule. Application logs/traces may expose NXDOMAIN, but the CoreDNS ConfigMap is not exported and `kube-system` logs are explicitly excluded from Splunk collection. |
| 10 | `service_wrong_pod_selection_hotel_reservation` | The `frontend` Service selector is changed to `service-route=frontend`, a label shared by intended frontend pods and unintended `search` pods. The polluted endpoint targets include `search`, which listens on 8082 rather than targetPort 5000, causing intermittent failure. | Inspect Service selector, EndpointSlices/endpoints, pod labels, and container ports; correlate intermittent requests with the wrong endpoint. | **Partial.** Pod objects can reveal that both workloads share the route label and have different ports, but the Service selector and EndpointSlice membership are not exported, so the exact wrong routing still needs Kubernetes access. |

## Suggested discriminator commands

These are examples of the read-only evidence the benchmark's Kubernetes tool can
obtain and the Splunk-only integration currently cannot reproduce completely:

```bash
# 1: CronJob lifecycle and pod-template shape
kubectl get cronjob audit-log-archiver -n hotel-reservation -o yaml
kubectl get jobs,pods -n hotel-reservation -l app.kubernetes.io/name=audit-log-archiver -o wide

# 3: Network isolation object
kubectl get networkpolicy deny-all-recommendation -n hotel-reservation -o yaml

# 4: Ordered duplicate environment definitions
kubectl get deployment frontend-proxy -n astronomy-shop -o json

# 5: Desired resources, admitted resources, and active webhook
kubectl get deployment nginx-thrift -n social-network -o yaml
kubectl get pod -n social-network -l service=nginx-thrift -o yaml
kubectl get mutatingwebhookconfigurations -o yaml

# 6: Finalizer ownership and effective RBAC
kubectl get configmap reservation-cleanup-token -n hotel-reservation -o yaml
kubectl get clusterrole configmap-cleanup-controller -o yaml
kubectl auth can-i patch configmaps \
  --as=system:serviceaccount:hotel-reservation:cleanup-controller \
  -n hotel-reservation

# 8: Service policy and cross-node placement
kubectl get service recommendation -n astronomy-shop -o yaml
kubectl get endpointslices -n astronomy-shop -l kubernetes.io/service-name=recommendation -o yaml
kubectl get pods -n astronomy-shop -o wide

# 9: Injected NXDOMAIN rule
kubectl get configmap coredns -n kube-system -o yaml

# 10: Polluted service endpoints
kubectl get service frontend -n hotel-reservation -o yaml
kubectl get endpointslices -n hotel-reservation -l kubernetes.io/service-name=frontend -o yaml
kubectl get pods -n hotel-reservation --show-labels
```

Kafka is the exception because its decisive state is not a Kubernetes object:
the equivalent discriminator is consumer-group offset/lag plus the record at the
blocked offset.

## Implication for score interpretation

A low score can represent at least three different failures:

1. the golden causal evidence was available in Splunk and the agent missed it;
2. only symptom evidence was available, so the agent could localize impact but
   could not prove the benchmark's exact hidden configuration;
3. telemetry delivery itself was incomplete or invalid.

Those should not be collapsed into one model-quality number. Each attempt should
retain the benchmark score unchanged, then add an operator-side evidence audit:

- `confirmed`: the current Splunk data contains the decisive causal evidence;
- `partial`: impact/component evidence exists, but a decisive causal field is
  absent;
- `missing`: even the expected fault symptom is not present;
- `not_checked`: no post-grade audit was completed.

The `network_policy_block` run demonstrates category 2: Assistant V3's exact
submission was correctly graded 33/100, while the separate audit found three
scoped workload timeout records but no NetworkPolicy identity or rule telemetry.

## Smallest clean path to evidence parity

Do not add benchmark-specific hints to the prompt. Instead, make the evidence
surface explicit and provider-neutral:

1. Define a small per-scenario evidence manifest for validation, separate from
   the agent prompt and judge. It should name expected symptom signals and the
   decisive causal state, without leaking either to the agent.
2. Add a generic, allowlisted Kubernetes object-state export path. An OpenTelemetry
   `k8sobjects` receiver or equivalent can emit selected object snapshots/events
   as logs to any provider. Start with Services, EndpointSlices, NetworkPolicies,
   Deployments, Pods, Jobs, CronJobs, ConfigMaps, RBAC, and admission webhooks.
3. Exclude Secrets and redact sensitive ConfigMap fields. Preserve object kind,
   name, namespace, generation/resourceVersion, relevant spec fields, and the
   SREGym run identity. Treat cluster-scoped resources carefully because they do
   not naturally inherit an application namespace.
4. Instrument Kafka group offset/lag and blocked-record errors as a separate
   subsystem adapter; Kubernetes object export cannot cover data-plane state.
5. Before counting a run as an agent-quality result, execute the hidden
   operator-side evidence check. Preserve the raw score either way, but label
   telemetry-insufficient cases separately in reports.

An alternative is to give Assistant V3 a generic read-only Kubernetes tool. That
would closely match the benchmark's reference capability, but it would evaluate
a different product surface from today's Splunk-only Assistant. Exporting the
required object state into Splunk keeps the evaluation aligned with the intended
product architecture and makes the same integration reusable across agents.

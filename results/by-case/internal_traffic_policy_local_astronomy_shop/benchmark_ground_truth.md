# Benchmark ground truth

The canonical diagnosis oracle is defined in [oracle_source.py](oracle_source.py). This is its source expression; runtime interpolation, if any, is governed by that code. [case_evidence_map.yaml](case_evidence_map.yaml) is a candidate evidence map, not a replacement for the benchmark oracle.

```python
self.root_cause = self.build_structured_root_cause(
            component=f"service/{self.FAULTY_SERVICE}",
            namespace=self.namespace,
            description=(
                f"The `{self.FAULTY_SERVICE}` Service in namespace `{self.namespace}` has "
                "`spec.internalTrafficPolicy: Local` set. "
                "This instructs kube-proxy to route in-cluster traffic **only** to pods on the "
                "**same node** as the calling pod. "
                f"The `{self.FAULTY_SERVICE}` Deployment has a single replica, so only one worker "
                "node hosts the pod. "
                "Any in-cluster caller (e.g. the `frontend` service) running on a different worker node "
                "will have its connection silently dropped by kube-proxy — the socket hangs until the "
                "application's own timeout fires, yielding no HTTP response and no TCP error. "
                f"All Kubernetes health signals appear normal: the `{self.FAULTY_SERVICE}` pod is "
                "Running and Ready, its endpoints are populated, and the Service object exists. "
                "The fault is only visible in `service.spec.internalTrafficPolicy` and the mismatch "
                "between pod placement and caller node topology. "
                "Valid mitigations: change `internalTrafficPolicy` back to `Cluster` (or remove the "
                f"field), or scale `{self.FAULTY_SERVICE}` so every worker node has at least one "
                "ready pod."
            ),
        )
```

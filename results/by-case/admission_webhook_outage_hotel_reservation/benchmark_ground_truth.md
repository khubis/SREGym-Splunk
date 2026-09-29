# Benchmark ground truth

The canonical diagnosis oracle is defined in [oracle_source.py](oracle_source.py). This is its source expression; runtime interpolation, if any, is governed by that code. [case_evidence_map.yaml](case_evidence_map.yaml) is a candidate evidence map, not a replacement for the benchmark oracle.

```python
self.root_cause = self.build_structured_root_cause(
            component=f"ValidatingWebhookConfiguration/{self.WEBHOOK_NAME}",
            namespace=self.namespace,
            description=(
                f"A cluster-scoped ValidatingWebhookConfiguration named `{self.WEBHOOK_NAME}` has been installed "
                f"with `failurePolicy: Fail` and a `namespaceSelector` scoped to the `{self.namespace}` namespace. "
                "The webhook intercepts pod CREATE operations, but its `clientConfig.service` points at a backend "
                f"Service `{self.BACKEND_SVC_NAMESPACE}/{self.BACKEND_SVC_NAME}` that does not exist, so every "
                "admission request times out and the kube-apiserver rejects the request with a `failed calling "
                f"webhook` error. As a result, the ReplicaSet controlling the `{self.faulty_service}` deployment "
                "cannot recreate pods after they are deleted, leaving the deployment under-replicated. The "
                f"`{self.faulty_service}` deployment itself is healthy — its spec, image, and resources are "
                "correct; it is an innocent victim of a cluster-scoped admission dependency. The mitigation is "
                "to remove the broken ValidatingWebhookConfiguration (alternatively change its `failurePolicy` "
                "to `Ignore`, or restore endpoints for the backend service); once admission unblocks, the "
                "existing ReplicaSet immediately recreates the missing pod without any other changes."
            ),
        )
```

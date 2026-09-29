# Benchmark ground truth

The canonical diagnosis oracle is defined in [oracle_source.py](oracle_source.py). This is its source expression; runtime interpolation, if any, is governed by that code. [case_evidence_map.yaml](case_evidence_map.yaml) is a candidate evidence map, not a replacement for the benchmark oracle.

```python
self.root_cause = self.build_structured_root_cause(
            component=f"MutatingWebhookConfiguration/{self.WEBHOOK_NAME}",
            namespace=self.namespace,
            description=(
                f"The fault is the cluster-scoped MutatingWebhookConfiguration named `{self.WEBHOOK_NAME}`. "
                f"It intercepts all pod CREATE operations in the `{self.namespace}` namespace and injects "
                f"a JSON patch that overwrites the first container's `resources.requests.memory` and "
                f"`resources.limits.memory` to `{self.INJECTED_MEMORY}`. Four companion "
                "MutatingWebhookConfigurations share the same namespaceSelector and backend Service, but "
                "each remains inert via different mechanisms (rules targeting CRDs that aren't installed, "
                "or objectSelectors requiring opt-in pod labels that no pod carries). The webhook backend "
                "server that executes the patch is not the fault — it is functioning exactly as "
                "configured. The fault is the webhook configuration itself being present. The Deployment "
                f"spec reports legitimate memory values (`requests: {self.SPEC_MEMORY_REQUEST}`, "
                f"`limits: {self.SPEC_MEMORY_LIMIT}`); the actual running pods have "
                f"`requests: {self.INJECTED_MEMORY}` / `limits: {self.INJECTED_MEMORY}` and are "
                "immediately OOMKilled on startup. The discrepancy between the Deployment spec and the "
                "running pod resources is the diagnostic signal."
            ),
        )
```

# Benchmark ground truth

The canonical diagnosis oracle is defined in [oracle_source.py](oracle_source.py). This is its source expression; runtime interpolation, if any, is governed by that code. [case_evidence_map.yaml](case_evidence_map.yaml) is a candidate evidence map, not a replacement for the benchmark oracle.

```python
self.root_cause = self.build_structured_root_cause(
            component=self.faulty_service,
            namespace=self.namespace,
            description=(
                f"The deployment `{self.faulty_service}` has a misconfigured readiness probe that targets a non-existent "
                "health endpoint (`/healthz` on port `8080`), so pods fail readiness checks and remain NotReady. "
                "Kubernetes excludes these pods from service endpoints even though containers may still be running. "
                "Users see connection failures, partial outages, and persistent request timeouts to this service."
            ),
        )
```

# Benchmark ground truth

The canonical diagnosis oracle is defined in [oracle_source.py](oracle_source.py). This is its source expression; runtime interpolation, if any, is governed by that code. [case_evidence_map.yaml](case_evidence_map.yaml) is a candidate evidence map, not a replacement for the benchmark oracle.

```python
self.root_cause = self.build_structured_root_cause(
            component=self.faulty_service,
            namespace=self.namespace,
            description=(
                f"Deployment `{self.faulty_service}` is configured with an invalid DNS policy (`dnsPolicy: None`) and "
                "external resolver (`8.8.8.8`), so it cannot reliably resolve cluster-internal service names. Calls "
                "to `*.svc.cluster.local` dependencies fail with name resolution errors even when target services are "
                "healthy. Users see request timeouts and cascading failures on dependent application paths."
            ),
        )
```

# Benchmark ground truth

The canonical diagnosis oracle is defined in [oracle_source.py](oracle_source.py). This is its source expression; runtime interpolation, if any, is governed by that code. [case_evidence_map.yaml](case_evidence_map.yaml) is a candidate evidence map, not a replacement for the benchmark oracle.

```python
self.root_cause = self.build_structured_root_cause(
            component=self.faulty_service,
            namespace=self.namespace,
            description=(
                f"The service `{self.faulty_service}` has a misconfigured selector that adds an incorrect label, "
                "so it no longer matches the intended backing pods. The service has zero or insufficient endpoints "
                "despite healthy-looking deployments, causing routing failures at the service layer. Users observe "
                "connection resets/timeouts and partial outages when requests are sent through this service."
            ),
        )
```

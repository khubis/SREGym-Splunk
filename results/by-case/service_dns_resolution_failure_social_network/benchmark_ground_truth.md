# Benchmark ground truth

The canonical diagnosis oracle is defined in [oracle_source.py](oracle_source.py). This is its source expression; runtime interpolation, if any, is governed by that code. [case_evidence_map.yaml](case_evidence_map.yaml) is a candidate evidence map, not a replacement for the benchmark oracle.

```python
self.root_cause = self.build_structured_root_cause(
            component=self.faulty_service,
            namespace=self.namespace,
            description=(
                f"CoreDNS is configured with an NXDOMAIN template for `{self.faulty_service}.{self.namespace}.svc.cluster.local`, "
                "so in-cluster lookups for this service name fail at DNS resolution time. Dependent services cannot "
                "resolve or connect to the target even though pods may be healthy and listening. Users observe request "
                "timeouts and cascading failures on flows that depend on this service endpoint."
            ),
        )
```

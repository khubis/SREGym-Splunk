# Benchmark ground truth

The canonical diagnosis oracle is defined in [oracle_source.py](oracle_source.py). This is its source expression; runtime interpolation, if any, is governed by that code. [case_evidence_map.yaml](case_evidence_map.yaml) is a candidate evidence map, not a replacement for the benchmark oracle.

```python
self.root_cause = self.build_structured_root_cause(
            component=self.faulty_service,
            namespace=self.namespace,
            description=(
                f"A NetworkPolicy `{self.policy_name}` blocks all ingress and egress traffic for pods labeled "
                f"`{self.POD_LABEL_KEY}={self.faulty_service}`, creating complete network isolation for the target "
                "workload. Service-to-service communication to and from the component fails, so dependent request "
                "paths break even if pods remain Running. Users observe hard failures/timeouts on flows that require "
                "this service."
            ),
        )
```

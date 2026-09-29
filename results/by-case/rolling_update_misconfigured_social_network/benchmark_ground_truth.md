# Benchmark ground truth

The canonical diagnosis oracle is defined in [oracle_source.py](oracle_source.py). This is its source expression; runtime interpolation, if any, is governed by that code. [case_evidence_map.yaml](case_evidence_map.yaml) is a candidate evidence map, not a replacement for the benchmark oracle.

```python
self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                "The rollout strategy is set to maxUnavailable=100% and maxSurge=0 while an init container hangs, "
                "so an update drains all old replicas before new ones become Ready and the deployment remains stuck with "
                "prolonged service unavailability."
            ),
        )
```

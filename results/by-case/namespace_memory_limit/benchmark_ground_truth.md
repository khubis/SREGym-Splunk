# Benchmark ground truth

The canonical diagnosis oracle is defined in [oracle_source.py](oracle_source.py). This is its source expression; runtime interpolation, if any, is governed by that code. [case_evidence_map.yaml](case_evidence_map.yaml) is a candidate evidence map, not a replacement for the benchmark oracle.

```python
self.root_cause = self.build_structured_root_cause(
            component=f"resourcequota/{self.QUOTA_NAME}",
            namespace=self.namespace,
            description=(
                f"Namespace-wide ResourceQuota `{self.QUOTA_NAME}` enforces memory declarations, but the existing "
                f"workloads do not declare them. Recreating deployment `{self.faulty_service}` exposes the problem "
                "immediately: admission rejects its replacement pod with `must specify memory`. Other noncompliant "
                "workloads are also vulnerable on restart even though their existing pods continue running."
            ),
        )
```

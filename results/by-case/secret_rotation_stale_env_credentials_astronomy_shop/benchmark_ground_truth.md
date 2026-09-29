# Benchmark ground truth

The canonical diagnosis oracle is defined in [oracle_source.py](oracle_source.py). This is its source expression; runtime interpolation, if any, is governed by that code. [case_evidence_map.yaml](case_evidence_map.yaml) is a candidate evidence map, not a replacement for the benchmark oracle.

```python
self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                "PostgreSQL credentials and the Kubernetes Secret were rotated, but the active product-catalog pod "
                "continues using the database connection string captured at container startup. After existing "
                "database sessions are terminated, product-catalog cannot reconnect until its runtime credential is "
                "made consistent with the backend."
            ),
        )
```

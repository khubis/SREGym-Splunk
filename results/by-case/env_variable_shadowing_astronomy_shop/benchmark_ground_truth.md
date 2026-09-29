# Benchmark ground truth

The canonical diagnosis oracle is defined in [oracle_source.py](oracle_source.py). This is its source expression; runtime interpolation, if any, is governed by that code. [case_evidence_map.yaml](case_evidence_map.yaml) is a candidate evidence map, not a replacement for the benchmark oracle.

```python
self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                f"Container `{self.faulty_service}` defines `{self.ENV_NAME}` twice. The later value "
                f"`{self.SHADOW_VALUE}` shadows the earlier intended value `{self.EXPECTED_VALUE}`, so the edge proxy "
                "routes requests to the wrong upstream even though its pod remains Running and Ready."
            ),
        )
```

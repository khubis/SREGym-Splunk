# Benchmark ground truth

The canonical diagnosis oracle is defined in [oracle_source.py](oracle_source.py). This is its source expression; runtime interpolation, if any, is governed by that code. [case_evidence_map.yaml](case_evidence_map.yaml) is a candidate evidence map, not a replacement for the benchmark oracle.

```python
self.root_cause = self.build_structured_root_cause(
            component=f"service/{self.frontend_service}",
            namespace=self.namespace,
            description=(
                "The `frontend` Service selector has been broadened to `service-route=frontend`, "
                "and that label is present on both the intended frontend pods and the unintended `search` pods. "
                "The frontend Service still has endpoints, but the endpoint list is polluted with a search pod. "
                "The search container listens on port 8082, not the frontend targetPort 5000, so traffic routed "
                "through the frontend Service can intermittently hit a pod that cannot serve frontend requests."
            ),
        )
```

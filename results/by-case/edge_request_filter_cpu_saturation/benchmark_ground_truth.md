# Benchmark ground truth

The canonical diagnosis oracle is defined in [oracle_source.py](oracle_source.py). This is its source expression; runtime interpolation, if any, is governed by that code. [case_evidence_map.yaml](case_evidence_map.yaml) is a candidate evidence map, not a replacement for the benchmark oracle.

```python
self.root_cause = self.build_structured_root_cause(
            component=self.faulty_service,
            namespace=self.namespace,
            description=(
                "A recent edge/WAF request-filter rule update on deployment `frontend-proxy` introduced the "
                "vulnerable "
                f"regex `{self.bad_regex}`. Requests carrying long near-matching `waf` query values trigger "
                "catastrophic backtracking in the edge proxy request filter, driving CPU saturation and causing "
                "timeouts for otherwise healthy HTTP paths. The fix is to roll back or disable the bad rule, or "
                f"replace it with a linear-time equivalent such as `{self.safe_regex}`."
            ),
        )
```

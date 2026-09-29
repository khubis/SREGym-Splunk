# Benchmark ground truth

The canonical diagnosis oracle is defined in [oracle_source.py](oracle_source.py). This is its source expression; runtime interpolation, if any, is governed by that code. [case_evidence_map.yaml](case_evidence_map.yaml) is a candidate evidence map, not a replacement for the benchmark oracle.

```python
self.root_cause = self.build_structured_root_cause(
            component="search-to-rate RPC path",
            namespace=self.namespace,
            description=(
                "The search service's rate RPC policy permits three attempts with a short per-attempt deadline. "
                "After a transient traffic burst fills the rate service's bounded backend queue, calls exceed "
                "their deadline but continue consuming downstream capacity. Search retries those expired calls, "
                "so internal rate requests remain above backend capacity even after external traffic returns to "
                "normal. The sustaining cause is the timeout/retry/queue feedback loop, not the expired burst. "
                "A diagnosis that only calls the static rate limit too low under normal traffic is incomplete: "
                "normal traffic is below that limit and the bounded trigger plus post-trigger amplification are "
                "required parts of the causal explanation."
            ),
        )
```

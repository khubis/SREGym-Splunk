# Benchmark ground truth

The canonical diagnosis oracle is defined in [oracle_source.py](oracle_source.py). This is its source expression; runtime interpolation, if any, is governed by that code. [case_evidence_map.yaml](case_evidence_map.yaml) is a candidate evidence map, not a replacement for the benchmark oracle.

```python
self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                "The deployment is scaled to multiple replicas that all reference one ReadWriteOnce PVC while required "
                "hostname anti-affinity separates those replicas. With topology-bound storage, only one replica can use "
                "the claim, leaving another replica Pending and the deployment only partially Ready."
            ),
        )
```

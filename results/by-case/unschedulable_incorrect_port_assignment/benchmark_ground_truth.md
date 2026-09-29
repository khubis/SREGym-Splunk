# Benchmark ground truth

The canonical diagnosis oracle is defined in [oracle_source.py](oracle_source.py). This is its source expression; runtime interpolation, if any, is governed by that code. [case_evidence_map.yaml](case_evidence_map.yaml) is a candidate evidence map, not a replacement for the benchmark oracle.

```python
self.root_cause = self.build_structured_root_cause(
            component=f"deployment/{self.faulty_service}",
            namespace=self.namespace,
            description=(
                f"The {self.env_var} environment variable points to the wrong backend port ({self.incorrect_port} instead of "
                f"{self.correct_port}), so checkout cannot reach product-catalog and related requests fail. "
                "Symptoms include connection-refused errors and elevated failed request rates on checkout endpoints."
            ),
        )
```

```python
self.root_cause = self.build_structured_root_cause(
                component=f"deployment/{self.faulty_service}",
                namespace=self.namespace,
                description=(
                    f"Two faults are active at the same time: (1) {self.env_var} points to the wrong backend port "
                    f"({self.incorrect_port} instead of {self.correct_port}), and (2) the deployment is pinned to a "
                    "non-existent node via nodeSelector, keeping pods Pending. Symptoms include both routing failures "
                    "from bad service endpoints and unschedulable pod events from node selector mismatch."
                ),
            )
```

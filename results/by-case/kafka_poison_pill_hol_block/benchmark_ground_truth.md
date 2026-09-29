# Benchmark ground truth

The canonical diagnosis oracle is defined in [oracle_source.py](oracle_source.py). This is its source expression; runtime interpolation, if any, is governed by that code. [case_evidence_map.yaml](case_evidence_map.yaml) is a candidate evidence map, not a replacement for the benchmark oracle.

```python
self.root_cause = self.build_structured_root_cause(
            component=f"kafka topic/{self.TOPIC}",
            namespace=self.namespace,
            description=(
                f"An unprocessable ('poison-pill') record was published to the `{self.TOPIC}` "
                f"Kafka topic. The `{self.CONSUMER_GROUP}` consumer group cannot deserialize "
                "this record, so it never commits the offset and halts at that position "
                "(head-of-line blocking). All consumer pods stay Running and Ready, but the "
                "consumer-group committed offset is frozen and partition lag grows without "
                "bound — order-fulfillment processing has silently stopped. Restarting or "
                "rescaling the consumer does not help: a restarted pod re-reads the same "
                "uncommitted offset and stalls again, because the fault state lives in the "
                "Kafka log, outside the Kubernetes control plane. Mitigation requires "
                "advancing the consumer group past the poison record (skip-to-offset / "
                "dead-letter-queue style) without skipping the valid records queued behind it."
            ),
        )
```

# Benchmark ground truth

The canonical diagnosis oracle is defined in [oracle_source.py](oracle_source.py). This is its source expression; runtime interpolation, if any, is governed by that code. [case_evidence_map.yaml](case_evidence_map.yaml) is a candidate evidence map, not a replacement for the benchmark oracle.

```python
self.root_cause = self.build_structured_root_cause(
            component=f"CronJob/{self.CRONJOB_NAME}",
            namespace=self.namespace,
            description=(
                f"The CronJob '{self.CRONJOB_NAME}' in namespace '{self.namespace}' "
                "schedules a pod every minute. Its jobTemplate contains two regular "
                f"containers: a primary container ('{self.PRIMARY_CONTAINER}') that "
                f"performs a short archival step and exits cleanly, and a sidecar "
                f"container ('{self.SIDECAR_CONTAINER}') that runs a long-running "
                "log-shipper process. Because Kubernetes considers a Job 'Complete' "
                "only when every container in the pod terminates, and the sidecar "
                "is a regular (non-native) container with no termination handling, "
                "the Pod stays Running indefinitely after the primary exits. Each "
                "schedule produces a new active Job that never completes. "
                "'successfulJobsHistoryLimit' does not apply to active Jobs, so they "
                "accumulate without bound, visible as Jobs stuck at COMPLETIONS=0/1 "
                "and pods whose primary container shows Terminated/Completed while "
                "the sidecar shows Running. The acceptable fix is to convert the "
                "sidecar to the Kubernetes 1.28+ native pattern by moving it to "
                "initContainers with restartPolicy=Always (KEP-753); the kubelet "
                "then SIGTERMs the sidecar after the primary exits, allowing the "
                "Pod to reach Succeeded and the Job to reach Complete. "
                "activeDeadlineSeconds (time-bombs the workload), removing the "
                "sidecar container (silently breaks log forwarding to the SIEM), "
                "and deleting the CronJob entirely (removes the workload) are all "
                "rejected. The agent must also clean up the already-accumulated "
                "active Jobs."
            ),
        )
```

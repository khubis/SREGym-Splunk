# Benchmark ground truth

The canonical diagnosis oracle is defined in [oracle_source.py](oracle_source.py). This is its source expression; runtime interpolation, if any, is governed by that code. [case_evidence_map.yaml](case_evidence_map.yaml) is a candidate evidence map, not a replacement for the benchmark oracle.

```python
self.root_cause = self.build_structured_root_cause(
            component=f"ClusterRole/{self.clusterrole_name} and configmap/{self.configmap_name}",
            namespace=self.namespace,
            description=(
                f"ConfigMap `{self.configmap_name}` is stuck in Terminating with finalizer `{self.finalizer}`. "
                f"The finalizer is owned by Deployment `{self.controller_name}` using ServiceAccount "
                f"`{self.sa_name}`, but ClusterRole `{self.clusterrole_name}` was changed to read-only and is "
                "missing permission to patch ConfigMaps. The controller pod repeatedly logs HTTP 403 Forbidden "
                "while trying to remove the finalizer. Restore effective RBAC for the controller ServiceAccount "
                "so its normal reconcile loop can remove finalizers and Kubernetes can finish deleting both the "
                "current ConfigMap and future cleanup requests."
            ),
        )
```

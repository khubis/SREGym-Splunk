# Exact starter prompt

The Assistant request sent the following **two separate fields**. [prompt_request.json](prompt_request.json) is the exact machine-readable record.

## Diagnosis prompt

Monitor and diagnose an application consisting of **MANY** microservices. Some or none of the microservices have faults. Carefully identify the whether the faults are present and if they are, and identify what is the root cause of the fault.
Stop diagnosis once you've found the root cause of the faults.
Go as deep as you can into what is causing the issue.

Remember to check these, and remember this information: ## Workloads (Applications) - **Pod**: The smallest deployable unit in Kubernetes, representing a single instance of a running application. Can contain one or more tightly coupled containers. - **ReplicaSet**: Ensures that a specified number of pod replicas are running at all times. Often managed indirectly through Deployments. - **Deployment**: Manages the deployment and lifecycle of applications. Provides declarative updates for Pods and ReplicaSets. - **StatefulSet**: Manages stateful applications with unique pod identities and stable storage. Used for workloads like databases. - **DaemonSet**: Ensures that a copy of a specific pod runs on every node in the cluster. Useful for node monitoring agents, log collectors, etc. - **Job**: Manages batch processing tasks that are expected to complete successfully. Ensures pods run to completion. - **CronJob**: Schedules jobs to run at specified times or intervals (similar to cron in Linux).
## Networking - **Service**: Provides a stable network endpoint for accessing a group of pods. Types: ClusterIP, NodePort, LoadBalancer, and ExternalName. - **Ingress**: Manages external HTTP(S) access to services in the cluster. Supports routing and load balancing for HTTP(S) traffic. - **NetworkPolicy**: Defines rules for network communication between pods and other entities. Used for security and traffic control.
## Storage - **PersistentVolume (PV)**: Represents a piece of storage in the cluster, provisioned by an administrator or dynamically. - **PersistentVolumeClaim (PVC)**: Represents a request for storage by a user. Binds to a PersistentVolume. - **StorageClass**: Defines different storage tiers or backends for dynamic provisioning of PersistentVolumes. - **ConfigMap**: Stores configuration data as key-value pairs for applications. - **Secret**: Stores sensitive data like passwords, tokens, or keys in an encrypted format.
## Configuration and Metadata - **Namespace**: Logical partitioning of resources within the cluster for isolation and organization. - **ConfigMap**: Provides non-sensitive configuration data in key-value format. - **Secret**: Stores sensitive configuration data securely. - **ResourceQuota**: Restricts resource usage (e.g., CPU, memory) within a namespace. - **LimitRange**: Enforces minimum and maximum resource limits for containers in a namespace.
## Cluster Management - **Node**: Represents a worker machine in the cluster (virtual or physical). Runs pods and is managed by the control plane. - **ClusterRole and Role**: Define permissions for resources at the cluster or namespace level. - **ClusterRoleBinding and RoleBinding**: Bind roles to users or groups for authorization. - **ServiceAccount**: Associates processes in pods with permissions for accessing the Kubernetes API.
## Evaluation You are being evaluated on diagnosis. - **Diagnosis (current stage)**: You must identify the root cause of the fault(s). Your submission is evaluated on whether you correctly identify the faulty components and the underlying cause. 
Be as specific and accurate as possible.
After you finish, provide a natural language description of the root cause of the failure.


You will be working this application:
Social Network
Here are some descriptions about the application:
A social network with unidirectional follow relationships, implemented with loosely-coupled microservices, communicating with each other via Thrift RPCs.
It belongs to this namespace:
social-network
You will begin by analyzing the service's state and telemetry with the tools.


## Action instructions

Telemetry time window: 2026-09-27T23:52:57.439576Z through 2026-09-27T23:55:39.561029Z, inclusive. Investigate using only telemetry within this time window. If telemetry is unavailable in this window, report that instead of using data outside it.
Observed symptom: Social Network user-service pods are not becoming ready.

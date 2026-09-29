"""The social-network PVC fault must retain its injectable Jaeger Deployment."""

from sregym.conductor.problems.duplicate_pvc_mounts import DuplicatePVCMounts


def test_only_jaeger_pvc_fault_requests_app_local_trace_workload():
    problem = DuplicatePVCMounts.__new__(DuplicatePVCMounts)
    problem.faulty_service = "jaeger"
    assert problem.preserve_app_local_jaeger_deployment is True
    problem.faulty_service = "frontend"
    assert problem.preserve_app_local_jaeger_deployment is False

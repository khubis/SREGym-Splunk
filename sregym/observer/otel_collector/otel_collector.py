import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml

from sregym.observability.base import ExternalOtlpExport
from sregym.service.rollout import deployment_rollout_complete

logger = logging.getLogger("all.sregym.otel_collector")


class OtelCollector:
    def __init__(self):
        self.namespace = "observe"
        base_dir = Path(__file__).parent
        self.config_file = base_dir / "otel-collector.yaml"

    def run_cmd(self, cmd: list[str], *, input_text: str | None = None) -> str:
        result = subprocess.run(cmd, input=input_text, capture_output=True, text=True)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(f"Command failed: {cmd[0]} ({result.returncode}): {detail}")
        return result.stdout.strip()

    def render_manifest(self, external_export: ExternalOtlpExport | None = None) -> str:
        """Render optional external fan-out without mutating the base manifest."""
        original = self.config_file.read_text()
        if external_export is None:
            return original

        documents: list[dict[str, Any]] = list(yaml.safe_load_all(original))
        config_map = next(document for document in documents if document.get("kind") == "ConfigMap")
        collector_config = yaml.safe_load(config_map["data"]["config.yaml"])

        collector_config["exporters"]["otlp/external"] = {
            "endpoint": external_export.endpoint,
            "tls": {"insecure": True},
        }
        attributes = {**external_export.resource_attributes, "sregym.run.id": external_export.run_id}
        collector_config.setdefault("processors", {})["resource/external"] = {
            "attributes": [
                {"key": key, "value": value, "action": "upsert"} for key, value in sorted(attributes.items())
            ]
        }

        for pipeline_name in ("traces", "traces/otlp"):
            pipeline = collector_config["service"]["pipelines"][pipeline_name]
            pipeline["processors"] = [*pipeline.get("processors", []), "resource/external"]
            pipeline["exporters"] = [*pipeline["exporters"], "otlp/external"]

        collector_config["receivers"]["prometheus/external"] = {
            "config": {
                "scrape_configs": [
                    {
                        "job_name": "sregym-federation",
                        "honor_labels": True,
                        "metrics_path": "/federate",
                        "params": {"match[]": ['{__name__=~".+"}']},
                        "static_configs": [
                            {"targets": ["prometheus-server.observe.svc.cluster.local:80"]},
                        ],
                    }
                ]
            }
        }
        collector_config["service"]["pipelines"]["metrics/external"] = {
            "receivers": ["prometheus/external"],
            "processors": ["resource/external"],
            "exporters": ["otlp/external"],
        }

        config_map["data"]["config.yaml"] = yaml.safe_dump(collector_config, sort_keys=False)
        return yaml.safe_dump_all(documents, explicit_start=True, sort_keys=False)

    def deploy(self, external_export: ExternalOtlpExport | None = None):
        """Deploy OTel Collector with spanmetrics connector."""
        self._create_jaeger_backend_service()
        self.run_cmd(
            ["kubectl", "-n", self.namespace, "apply", "-f", "-"],
            input_text=self.render_manifest(external_export),
        )
        self._wait_for_ready(timeout=120)
        logger.info("OTel Collector deployed successfully.")

    def _create_jaeger_backend_service(self):
        """Create a jaeger-backend service pointing to the Jaeger pod.

        The OTel Collector exports traces to jaeger-backend:4317 (OTLP).
        This keeps the original jaeger-agent service name free for the
        ExternalName redirect in app namespaces.
        """
        self.run_cmd(["kubectl", "-n", self.namespace, "delete", "svc", "jaeger-backend", "--ignore-not-found"])
        manifest = yaml.safe_dump(
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": "jaeger-backend", "namespace": self.namespace},
                "spec": {
                    "ports": [
                        {"port": 4317, "name": "otlp-grpc"},
                        {"port": 16686, "name": "ui"},
                    ],
                    "selector": {"app-name": "jaeger"},
                },
            },
            sort_keys=False,
        )
        self.run_cmd(["kubectl", "apply", "-f", "-"], input_text=manifest)

    def _wait_for_ready(self, timeout: int = 120):
        """Wait until the OTel Collector pod is ready."""
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                out = self.run_cmd(
                    ["kubectl", "-n", self.namespace, "get", "deployment", "otel-collector", "-o", "json"]
                )
                if deployment_rollout_complete(json.loads(out)):
                    return
            except Exception:
                pass
            time.sleep(3)
        raise RuntimeError(f"OTel Collector not ready within {timeout}s")

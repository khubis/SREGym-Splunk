"""Secure deployment of the Splunk OpenTelemetry Collector."""

import os
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn, Protocol

import yaml
from kubernetes import client
from kubernetes import config as kubernetes_config
from kubernetes.client.rest import ApiException

from sregym.observability.base import (
    ApplicationScope,
    AttemptContext,
    DeliveryReport,
    ExternalOtlpExport,
    FailureKind,
    ProviderError,
    ProviderName,
    ReadinessReport,
)

CHART_VERSION = "0.160.0"
CHART_NAME = "splunk-otel-collector"
CHART_REPOSITORY = "https://signalfx.github.io/splunk-otel-collector-chart"
RELEASE_NAME = "sregym-splunk-otel"
NAMESPACE = "sregym-observability"
SECRET_NAME = "sregym-splunk-otel-credentials"
_HELM_TIMEOUT = "5m"
_REQUIRED_ENV = ("SF_TOKEN", "SFX_REALM", "SPLUNK_HOST", "SPLUNK_HEC_PORT", "SPLUNK_HEC_TOKEN")
_HOST_PATTERN = re.compile(
    r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*\Z"
)
_REALM_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*\Z")
_INDEX_PATTERN = re.compile(r"[A-Za-z0-9_.-]+\Z")
_DEFAULT_VALUES_PATH = Path(__file__).parents[1] / "observer" / "splunk" / "values.yaml"


class CoreV1Api(Protocol):
    def get_api_resources(self) -> object: ...

    def create_namespace(self, body: client.V1Namespace) -> object: ...

    def create_namespaced_secret(self, *, namespace: str, body: client.V1Secret) -> object: ...

    def patch_namespaced_secret(self, *, name: str, namespace: str, body: client.V1Secret) -> object: ...

    def delete_namespaced_secret(self, *, name: str, namespace: str) -> object: ...


CommandRunner = Callable[[list[str], str | None], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class SplunkConfig:
    """Validated destination configuration with secret-safe representation."""

    access_token: str = field(repr=False)
    realm: str
    hec_host: str
    hec_port: int
    hec_token: str = field(repr=False)
    hec_index: str = "main"

    @classmethod
    def from_env(cls, environment: Mapping[str, str] | None = None) -> "SplunkConfig":
        source = os.environ if environment is None else environment
        missing = [name for name in _REQUIRED_ENV if not source.get(name, "").strip()]
        if missing:
            raise ProviderError("configuration", f"missing required Splunk environment: {', '.join(missing)}")

        host = source["SPLUNK_HOST"].strip()
        realm = source["SFX_REALM"].strip()
        port_text = source["SPLUNK_HEC_PORT"].strip()
        index = source.get("SPLUNK_HEC_INDEX", "main").strip()
        if _HOST_PATTERN.fullmatch(host) is None:
            raise ProviderError("configuration", "SPLUNK_HOST must be a hostname without a scheme, port, or path")
        if _REALM_PATTERN.fullmatch(realm) is None:
            raise ProviderError("configuration", "SFX_REALM has an invalid format")
        if not port_text.isascii() or not port_text.isdigit():
            raise ProviderError("configuration", "SPLUNK_HEC_PORT must be a numeric TLS port")
        port = int(port_text)
        if not 1 <= port <= 65535:
            raise ProviderError("configuration", "SPLUNK_HEC_PORT must be between 1 and 65535")
        if _INDEX_PATTERN.fullmatch(index) is None:
            raise ProviderError("configuration", "SPLUNK_HEC_INDEX has an invalid format")

        return cls(
            access_token=source["SF_TOKEN"].strip(),
            realm=realm,
            hec_host=host,
            hec_port=port,
            hec_token=source["SPLUNK_HEC_TOKEN"].strip(),
            hec_index=index,
        )

    @property
    def hec_endpoint(self) -> str:
        return f"https://{self.hec_host}:{self.hec_port}/services/collector/event"

    def artifact_metadata(self) -> dict[str, str | int]:
        """Return the non-secret destination fields permitted in run evidence."""
        return {
            "hec_host": self.hec_host,
            "hec_index": self.hec_index,
            "hec_port": self.hec_port,
            "realm": self.realm,
        }


class SplunkObservabilityProvider:
    """Deploy the collector while keeping destination credentials out of Helm."""

    name: ProviderName = "splunk"

    def __init__(
        self,
        configuration: SplunkConfig,
        *,
        core_api: CoreV1Api | None = None,
        run: CommandRunner | None = None,
        values_path: Path = _DEFAULT_VALUES_PATH,
    ) -> None:
        self.configuration = configuration
        self._core = core_api
        self._run = run or _run_command
        self._values_path = values_path
        self._prepared_export: ExternalOtlpExport | None = None
        self._touched = False
        self._closed = False

    def preflight(self) -> None:
        if not self._values_path.is_file():
            raise ProviderError("configuration", "Splunk collector values are unavailable")
        self._run_checked(["helm", "version", "--short"], None, "configuration", "Helm is unavailable")
        try:
            self._core_api().get_api_resources()
        except ApiException as error:
            _raise_api_error(error, "Kubernetes API preflight failed")
        except Exception:
            raise ProviderError("configuration", "Kubernetes API preflight failed") from None

    def prepare_attempt(self, context: AttemptContext) -> ExternalOtlpExport:
        if self._closed:
            raise ProviderError("configuration", "the Splunk provider is already closed")
        if self._prepared_export is not None and self._prepared_export.run_id == context.run_id:
            return self._prepared_export

        self._touched = True
        self._ensure_namespace()
        self._upsert_secret()
        runtime_values = {
            "clusterName": context.run_id,
            "extraAttributes": {"custom": [{"name": "sregym.run.id", "value": context.run_id}]},
            "splunkObservability": {"realm": self.configuration.realm},
            "splunkPlatform": {
                "endpoint": self.configuration.hec_endpoint,
                "index": self.configuration.hec_index,
                "insecureSkipVerify": False,
            },
        }
        command = [
            "helm",
            "upgrade",
            "--install",
            RELEASE_NAME,
            CHART_NAME,
            "--repo",
            CHART_REPOSITORY,
            "--repository-config",
            os.devnull,
            "--version",
            CHART_VERSION,
            "--namespace",
            NAMESPACE,
            "--atomic",
            "--wait",
            "--timeout",
            _HELM_TIMEOUT,
            "--values",
            str(self._values_path),
            "--values",
            "-",
        ]
        self._run_checked(
            command,
            yaml.safe_dump(runtime_values, sort_keys=True),
            "configuration",
            "Splunk collector deployment failed",
        )
        export = ExternalOtlpExport(
            endpoint=f"{RELEASE_NAME}.{NAMESPACE}.svc.cluster.local:4317",
            run_id=context.run_id,
            resource_attributes={"sregym.run.id": context.run_id},
        )
        self._prepared_export = export
        return export

    def wait_until_queryable(self, context: AttemptContext, scope: ApplicationScope) -> ReadinessReport:
        raise ProviderError("configuration", "Splunk readiness assurance is not implemented")

    def finish_attempt(self, context: AttemptContext, scope: ApplicationScope) -> DeliveryReport:
        raise ProviderError("configuration", "Splunk delivery assurance is not implemented")

    def close(self) -> None:
        if self._closed:
            return None
        if not self._touched:
            self._closed = True
            return None

        cleanup_failed = False
        try:
            self._run_checked(
                ["helm", "uninstall", RELEASE_NAME, "--namespace", NAMESPACE, "--ignore-not-found", "--wait"],
                None,
                "cleanup",
                "Splunk collector cleanup failed",
            )
        except ProviderError:
            cleanup_failed = True

        try:
            self._core_api().delete_namespaced_secret(name=SECRET_NAME, namespace=NAMESPACE)
        except ApiException as error:
            if error.status != 404:
                cleanup_failed = True
        except Exception:
            cleanup_failed = True

        if cleanup_failed:
            raise ProviderError("cleanup", "Splunk provider cleanup failed") from None
        self._closed = True
        return None

    def _core_api(self) -> CoreV1Api:
        if self._core is None:
            try:
                kubernetes_config.load_kube_config()
                self._core = client.CoreV1Api()
            except Exception:
                raise ProviderError("configuration", "Kubernetes access is unavailable") from None
        return self._core

    def _ensure_namespace(self) -> None:
        body = client.V1Namespace(metadata=client.V1ObjectMeta(name=NAMESPACE))
        try:
            self._core_api().create_namespace(body)
        except ApiException as error:
            if error.status != 409:
                _raise_api_error(error, "Splunk collector namespace creation failed")
        except Exception:
            raise ProviderError("configuration", "Splunk collector namespace creation failed") from None

    def _upsert_secret(self) -> None:
        secret = client.V1Secret(
            metadata=client.V1ObjectMeta(name=SECRET_NAME, namespace=NAMESPACE),
            string_data={
                "splunk_observability_access_token": self.configuration.access_token,
                "splunk_platform_hec_token": self.configuration.hec_token,
            },
            type="Opaque",
        )
        api = self._core_api()
        try:
            api.create_namespaced_secret(namespace=NAMESPACE, body=secret)
        except ApiException as error:
            if error.status != 409:
                _raise_api_error(error, "Splunk collector Secret creation failed")
            try:
                api.patch_namespaced_secret(name=SECRET_NAME, namespace=NAMESPACE, body=secret)
            except ApiException as update_error:
                _raise_api_error(update_error, "Splunk collector Secret update failed")
            except Exception:
                raise ProviderError("configuration", "Splunk collector Secret update failed") from None
        except Exception:
            raise ProviderError("configuration", "Splunk collector Secret creation failed") from None

    def _run_checked(
        self,
        command: list[str],
        stdin: str | None,
        failure_kind: FailureKind,
        safe_message: str,
    ) -> None:
        try:
            result = self._run(command, stdin)
        except Exception:
            raise ProviderError(failure_kind, safe_message) from None
        if result.returncode != 0:
            raise ProviderError(failure_kind, safe_message) from None


def _run_command(command: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, input=stdin, capture_output=True, text=True, check=False)


def _raise_api_error(error: ApiException, safe_message: str) -> NoReturn:
    if error.status == 401:
        kind: FailureKind = "authentication"
    elif error.status == 403:
        kind = "permission"
    else:
        kind = "configuration"
    raise ProviderError(kind, safe_message) from None


__all__ = [
    "CHART_VERSION",
    "NAMESPACE",
    "RELEASE_NAME",
    "SECRET_NAME",
    "SplunkConfig",
    "SplunkObservabilityProvider",
]

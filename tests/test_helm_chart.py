# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Deployment configuration contracts rendered by Helm."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from powercontext.server.settings import ServerSettings

CHART = Path(__file__).resolve().parents[1] / "deploy/helm/powercontext"
pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is required")


def render(*overrides, release="test", values=None):
    command = [
        "helm",
        "template",
        release,
        str(CHART),
        "--set",
        "image.repository=example/powercontext",
        "--set",
        "image.tag=revision",
        "--set",
        "existingSecret=credentials",
    ]
    if values is not None:
        command.extend(["--values", str(values)])
    for value in overrides:
        command.extend(["--set", value])
    return subprocess.run(command, text=True, capture_output=True, check=False)


def test_role_configuration_and_service_isolation(monkeypatch):
    result = render("api.replicas=2", "background.replicas=2")
    assert result.returncode == 0, result.stderr
    documents = [item for item in yaml.safe_load_all(result.stdout) if item]
    deployments = [item for item in documents if item["kind"] == "Deployment"]
    assert {item["metadata"]["labels"]["app.kubernetes.io/component"] for item in deployments} == {"api", "background"}
    assert len(deployments) == 2
    service = next(item for item in documents if item["kind"] == "Service")
    assert service["spec"]["selector"]["app.kubernetes.io/component"] == "api"
    for key in os.environ:
        if key.startswith("POWERCONTEXT_SERVER_"):
            monkeypatch.delenv(key)
    secrets = {
        "database-url": "mysql+aoceanbase://test:test@localhost:2881/test?charset=utf8mb4",
        "bearer-token": "helm-test-token",
        "cursor-signing-secret": "x" * 32,
    }
    for deployment in deployments:
        role = deployment["metadata"]["labels"]["app.kubernetes.io/component"]
        spec = deployment["spec"]["template"]["spec"]
        container = spec["containers"][0]
        for entry in container["env"]:
            value = entry.get("value")
            if value is None:
                ref = entry["valueFrom"]["secretKeyRef"]
                assert ref["name"] == "credentials"
                value = secrets[ref["key"]]
            monkeypatch.setenv(entry["name"], value)
        settings = ServerSettings()
        assert settings.database.kind == "oceanbase"
        assert settings.runtime.artifact_processing_role == role
        assert settings.access.mode == "enforced"
        assert settings.auth.enabled
        assert settings.cursor_signing_secret is not None
        assert settings.cursor_signing_secret.get_secret_value() == secrets["cursor-signing-secret"]
        assert settings.external_skills.targets == ()
        assert os.environ["FASTMCP_STATELESS_HTTP"] == "true"
        assert spec["automountServiceAccountToken"] is False
        assert spec["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
        assert deployment["spec"]["strategy"]["type"] == "Recreate"
        if role == "api":
            assert container["readinessProbe"]["httpGet"]["path"] == "/health/ready"
        else:
            assert "ports" not in container
            assert "readinessProbe" not in container


@pytest.mark.parametrize(
    "override",
    [
        "image.tag=latest",
        "image.repository=",
        "existingSecret=",
        "database.kind=sqlite",
        "api.replicas=-1",
        "background.maxWorkers=0",
        "ingress.enabled=true",
        "inference.POWERCONTEXT_SERVER_DATABASE_KIND=sqlite",
    ],
)
def test_unsupported_configuration_is_rejected(override):
    assert render(override).returncode != 0


def test_digest_and_ingress():
    digest = "sha256:" + "a" * 64
    result = render("image.digest=" + digest, "ingress.enabled=true", "ingress.host=context.example.com")
    assert result.returncode == 0, result.stderr
    documents = [item for item in yaml.safe_load_all(result.stdout) if item]
    ingress = next(item for item in documents if item["kind"] == "Ingress")
    assert ingress["spec"]["rules"][0]["host"] == "context.example.com"
    for deployment in (item for item in documents if item["kind"] == "Deployment"):
        assert deployment["spec"]["template"]["spec"]["containers"][0]["image"].endswith("@" + digest)


def test_long_release_names_remain_distinct():
    names = []
    for suffix in ("b", "c"):
        result = render(release="a" * 52 + suffix)
        assert result.returncode == 0, result.stderr
        documents = [item for item in yaml.safe_load_all(result.stdout) if item]
        resources = {item["metadata"]["name"] for item in documents}
        assert all(len(name) <= 63 for name in resources)
        names.append(resources)
    assert names[0].isdisjoint(names[1])


def test_acceptance_profile_renders_two_apis_and_one_background():
    result = render(values=CHART / "values-acceptance.yaml")
    assert result.returncode == 0, result.stderr
    documents = [item for item in yaml.safe_load_all(result.stdout) if item]
    replicas = {}
    for deployment in (item for item in documents if item["kind"] == "Deployment"):
        role = deployment["metadata"]["labels"]["app.kubernetes.io/component"]
        replicas[role] = deployment["spec"]["replicas"]
        env = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
        model = next(item for item in env if item["name"] == "POWERCONTEXT_SERVER_INFERENCE_GENERATION_MODEL")
        assert model["value"] == "test"
    assert replicas == {"api": 2, "background": 1}

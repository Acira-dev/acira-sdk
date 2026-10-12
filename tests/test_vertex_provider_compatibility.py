"""Validate the optional provider stack offline, without constructing service clients."""
from __future__ import annotations

import importlib

import pytest


def test_vertex_provider_imports_and_generated_messages_without_cloud_calls():
    pytest.importorskip("google.cloud.aiplatform")
    for name in (
        "google.cloud.aiplatform_v1",
        "google.cloud.aiplatform_v1beta1",
        "google.cloud.bigquery",
        "google.cloud.storage",
        "google.cloud.resourcemanager_v3",
        "google.genai",
        "grpc",
    ):
        importlib.import_module(name)

    from google.cloud.aiplatform_v1.types import PredictRequest
    from google.cloud.resourcemanager_v3.types import GetProjectRequest
    from google.genai.types import GenerateContentConfig
    from google.protobuf.struct_pb2 import Value

    # Use the public repeated-field API. No credentials or service clients are needed.
    request = PredictRequest(
        endpoint="projects/offline/locations/us-central1/endpoints/offline"
    )
    request.instances.append(Value(string_value="compatibility-probe"))
    restored = PredictRequest.deserialize(PredictRequest.serialize(request))
    assert restored.endpoint == request.endpoint
    assert list(restored.instances) == ["compatibility-probe"]

    project = GetProjectRequest(name="projects/offline")
    assert GetProjectRequest.deserialize(GetProjectRequest.serialize(project)).name == project.name
    assert GenerateContentConfig(temperature=0).temperature == 0

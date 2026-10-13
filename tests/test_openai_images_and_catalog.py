import base64
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import routes
from src.core.auth import verify_api_key_flexible
from src.core.model_resolver import (
    extract_video_params,
    get_public_model_catalog,
    resolve_model_name,
)
from src.services.generation_handler import MODEL_CONFIG

PNG = b"\x89PNG\r\n\x1a\nfake-image-bytes"
IMAGE_URL = "https://flow-content.google/image/abc?Expires=1&Signature=x"


class _Req:
    """模拟带 extra / generationConfig 的 pydantic 请求。"""

    def __init__(self, extra=None, gen=None):
        self.__pydantic_extra__ = extra or {}
        self.generationConfig = gen


def resolve(model, req=None, images=None):
    return resolve_model_name(model, req, MODEL_CONFIG, images)


# ───────────── 基础模型目录 ─────────────


def test_public_catalog_lists_only_base_models():
    ids = [entry["id"] for entry in get_public_model_catalog(MODEL_CONFIG)]
    assert ids == [
        "gemini-3.1-flash-image",
        "veo-3.1-lite",
        "veo-3.1-fast",
        "veo-3.1-quality",
        "omni-1.1-flash",
    ]
    for entry in get_public_model_catalog(MODEL_CONFIG):
        assert entry["type"] in ("image", "video")
        assert entry["parameters"]["aspectRatio"]
        assert "durationSeconds" in entry["parameters"] or entry["type"] == "image"


def test_image_catalog_parameters_come_from_real_config():
    image = get_public_model_catalog(MODEL_CONFIG)[0]
    assert image["parameters"]["aspectRatio"] == ["16:9", "9:16", "1:1", "4:3", "3:4"]
    assert image["parameters"]["imageSize"] == ["1k", "2k", "4k"]


# ───────────── 视频：基础名 + 参数 → 具体名（用真实 MODEL_CONFIG，验证拼出来的名字都存在）─────────────


@pytest.mark.parametrize(
    "model,req,images,expected",
    [
        ("veo-3.1-fast", None, None, "veo-3.1-fast-8s-landscape"),
        ("veo-3.1-fast", _Req({"seconds": "6", "aspect_ratio": "9:16"}), None, "veo-3.1-fast-6s-portrait"),
        ("veo-3.1-fast", _Req({"duration": 5}), None, "veo-3.1-fast-6s-landscape"),
        ("veo-3.1-quality", _Req(gen={"videoConfig": {"durationSeconds": 4, "aspectRatio": "portrait"}}), None, "veo-3.1-quality-4s-portrait"),
        ("veo-3.1-lite", _Req({"size": "720x1280"}), None, "veo-3.1-lite-8s-portrait"),
        ("veo-3.1-fast", None, [b"a"], "veo-3.1-fast-i2v-6s-landscape"),
        ("veo-3.1-quality", _Req({"duration": 8}), [b"a", b"b"], "veo-3.1-quality-i2v-6s-landscape"),
        ("veo-3.1-quality", None, [b"1", b"2", b"3"], "veo-3.1-fast-r2v-8s-landscape"),
        ("veo-3.1-lite", _Req({"imageMode": "reference"}), [b"a"], "veo-3.1-lite-r2v-8s-landscape"),
        ("omni-1.1-flash", _Req({"duration": "10s", "aspect_ratio": "9:16"}), None, "omni-1.1-flash-10s-portrait"),
        ("omni-1.1-flash", None, [b"a"], "omni-1.1-flash-8s-landscape"),
        # 具体名原样保留（向后兼容）
        ("veo-3.1-fast-6s-portrait", None, None, "veo-3.1-fast-6s-portrait"),
    ],
)
def test_video_base_model_resolution(model, req, images, expected):
    resolved = resolve(model, req, images)
    assert resolved == expected
    assert resolved in MODEL_CONFIG and MODEL_CONFIG[resolved]["type"] == "video"


def test_extract_video_params_sources():
    assert extract_video_params(_Req({"extra_body": {"generationConfig": {"videoConfig": {"seconds": 6}}}}))[1] == 6
    assert extract_video_params(_Req({"aspect_ratio": "16:9"}))[0] == "landscape"
    assert extract_video_params(_Req({"imageMode": "first_last"}))[2] == "frames"


def test_image_base_model_with_openai_size_and_quality():
    assert resolve("gemini-3.1-flash-image", _Req({"size": "1024x1792", "quality": "hd"})) == "gemini-3.1-flash-image-portrait-4k"
    assert resolve("gemini-3.1-flash-image", _Req({"size": "1024x1024", "quality": "medium"})) == "gemini-3.1-flash-image-square-2k"


# ───────────── HTTP：模型列表 ─────────────


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[verify_api_key_flexible] = lambda: "test-key"
    calls = []

    async def fake_collect(model, prompt, images, base_url_override=None, video_media_id=None):
        calls.append({"model": model, "prompt": prompt, "images": len(images)})
        return json.dumps({"choices": [{"message": {"content": f"![Generated Image]({IMAGE_URL})"}}]})

    async def fake_fetch(url):
        return PNG

    monkeypatch.setattr(routes, "_collect_non_stream_result", fake_collect)
    monkeypatch.setattr(routes, "retrieve_image_data", fake_fetch)
    test_client = TestClient(app)
    test_client.calls = calls
    return test_client


def test_v1_models_default_is_base_only_and_full_lists_everything(client):
    base = client.get("/v1/models").json()["data"]
    assert [m["id"] for m in base] == [
        "gemini-3.1-flash-image", "veo-3.1-lite", "veo-3.1-fast", "veo-3.1-quality", "omni-1.1-flash",
    ]
    assert all(m["object"] == "model" and m["created"] and m["owned_by"] == "flow2api" for m in base)
    assert base[0]["parameters"]["imageSize"] == ["1k", "2k", "4k"]

    full = {m["id"] for m in client.get("/v1/models?full=1").json()["data"]}
    assert len(full) > 60 and "gemini-3.1-flash-image-portrait-4k" in full and "veo-3.1-fast-6s-portrait" in full
    assert {m["id"] for m in client.get("/v1/models?detail=all").json()["data"]} == full


def test_retrieve_model_and_aliases_route_not_shadowed(client):
    assert client.get("/v1/models/gemini-3.1-flash-image").json()["type"] == "image"
    assert client.get("/v1/models/gemini-3.1-flash-image-portrait-4k").status_code == 200  # 具体名仍可查
    missing = client.get("/v1/models/nope")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "model_not_found"
    assert client.get("/v1/models/aliases").json()["data"]  # 没有被 {model_id} 路由抢走


def test_gemini_model_list_is_base_only_but_concrete_model_resolves(client):
    names = [m["name"] for m in client.get("/v1beta/models").json()["models"]]
    assert names[0] == "models/gemini-3.1-flash-image" and len(names) == 5
    assert client.get("/v1beta/models/gemini-3.1-flash-image-portrait-4k").status_code == 200
    assert len(client.get("/v1beta/models?full=1").json()["models"]) > 60


# ───────────── HTTP：OpenAI Images ─────────────


def test_generations_defaults_to_square_and_returns_url_and_b64(client):
    resp = client.post("/v1/images/generations", json={"prompt": "a red apple"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["created"] and len(body["data"]) == 1
    assert body["data"][0]["url"] == IMAGE_URL
    assert base64.b64decode(body["data"][0]["b64_json"]) == PNG
    assert resp.headers["x-flow2api-model"] == "gemini-3.1-flash-image-square"
    assert client.calls[0]["model"] == "gemini-3.1-flash-image-square"


def test_generations_size_and_quality_are_parameters(client):
    resp = client.post(
        "/v1/images/generations",
        json={"model": "gemini-3.1-flash-image", "prompt": "x", "size": "1024x1792", "quality": "hd"},
    )
    assert resp.status_code == 200
    assert resp.headers["x-flow2api-model"] == "gemini-3.1-flash-image-portrait-4k"


def test_generations_gemini_style_generation_config(client):
    resp = client.post(
        "/images/generations",  # 不带 /v1 的别名路径
        json={"prompt": "x", "generationConfig": {"imageConfig": {"aspectRatio": "4:3", "imageSize": "2k"}}},
    )
    assert resp.status_code == 200
    assert resp.headers["x-flow2api-model"] == "gemini-3.1-flash-image-four-three-2k"


def test_generations_concrete_model_name_still_works(client):
    resp = client.post("/v1/images/generations", json={"model": "gemini-3.1-flash-image-landscape-2k", "prompt": "x"})
    assert resp.status_code == 200
    assert resp.headers["x-flow2api-model"] == "gemini-3.1-flash-image-landscape-2k"


def test_response_format_controls_fields(client):
    only_url = client.post("/v1/images/generations", json={"prompt": "x", "response_format": "url"}).json()["data"][0]
    assert set(only_url) == {"url"}
    only_b64 = client.post("/v1/images/generations", json={"prompt": "x", "response_format": "b64_json"}).json()["data"][0]
    assert set(only_b64) == {"b64_json"}


def test_n_runs_multiple_generations_and_is_capped(client):
    assert len(client.post("/v1/images/generations", json={"prompt": "x", "n": 2}).json()["data"]) == 2
    assert len(client.calls) == 2
    too_many = client.post("/v1/images/generations", json={"prompt": "x", "n": 5})
    assert too_many.status_code == 400 and too_many.json()["error"]["param"] == "n"


@pytest.mark.parametrize(
    "body,status,code",
    [
        ({"prompt": ""}, 400, "missing_required_parameter"),
        ({"prompt": "x", "model": "does-not-exist"}, 404, "model_not_found"),
        ({"prompt": "x", "model": "veo-3.1-fast"}, 400, "invalid_model"),
        ({"prompt": "x", "response_format": "gif"}, 400, "invalid_value"),
        ({"prompt": "x", "stream": True}, 400, None),
    ],
)
def test_generations_validation_errors_use_openai_shape(client, body, status, code):
    resp = client.post("/v1/images/generations", json=body)
    assert resp.status_code == status
    error = resp.json()["error"]
    assert error["message"] and error["type"] == "invalid_request_error" and error["code"] == code


def test_upstream_error_is_mapped(client, monkeypatch):
    async def failing(*args, **kwargs):
        return json.dumps({"error": {"message": "quota exhausted", "status_code": 429}})

    monkeypatch.setattr(routes, "_collect_non_stream_result", failing)
    resp = client.post("/v1/images/generations", json={"prompt": "x"})
    assert resp.status_code == 429
    assert resp.json()["error"]["message"] == "quota exhausted" and resp.json()["error"]["type"] == "rate_limit_error"


def test_edits_multipart_accepts_files_and_ignores_mask(client):
    resp = client.post(
        "/v1/images/edits",
        data={"prompt": "make it blue", "model": "gemini-3.1-flash-image", "n": "1"},
        files=[("image", ("a.png", PNG, "image/png")), ("image", ("b.png", PNG, "image/png")), ("mask", ("m.png", PNG, "image/png"))],
    )
    assert resp.status_code == 200
    assert client.calls[-1]["images"] == 2  # mask 不计入
    assert resp.json()["data"][0]["url"] == IMAGE_URL


def test_edits_json_with_data_url_and_missing_image(client):
    data_url = "data:image/png;base64," + base64.b64encode(PNG).decode()
    ok = client.post("/v1/images/edits", json={"prompt": "x", "images": [{"image_url": data_url}]})
    assert ok.status_code == 200 and client.calls[-1]["images"] == 1
    missing = client.post("/v1/images/edits", json={"prompt": "x"})
    assert missing.status_code == 400 and missing.json()["error"]["param"] == "image"

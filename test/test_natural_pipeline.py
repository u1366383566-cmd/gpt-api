"""NATURAL-PIPELINE-20261020 单测：config 开关 / 载荷形态 / handle 回退编排。
运行：临时容器 --network none，挂仓库 /src:ro，sys.path 优先取改造后源码。
"""
import base64
import sys

sys.path.insert(0, "/src")

from services.config import config  # noqa: E402
from services.openai_backend_api import ChatRequirements, OpenAIBackendAPI  # noqa: E402
from services.protocol import openai_v1_image_edit as edit_mod  # noqa: E402
from services.protocol.conversation import ImageOutput  # noqa: E402

FAILURES = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name, flush=True)
    if not cond:
        FAILURES.append(name)


# ---------- 1) config ----------
check("config default auto", config.image_edit_pipeline == "auto")
config.update({"image_edit_pipeline": "fast"})
check("config set fast", config.image_edit_pipeline == "fast")
config.update({"image_edit_pipeline": "weird"})
check("config invalid -> auto", config.image_edit_pipeline == "auto")
config.update({"image_edit_pipeline": "auto"})
check("config in get() echo", config.get().get("image_edit_pipeline") == "auto")

# ---------- 2) _image_model_settings ----------
api0 = object.__new__(OpenAIBackendAPI)
check("settings natural -> auto",
      OpenAIBackendAPI._image_model_settings(api0, "gpt-image-2", "natural") == ("auto", ""))
try:
    m_fast = OpenAIBackendAPI._image_model_settings(api0, "gpt-image-2", "fast")
    check("settings fast -> pinned slug", m_fast[0] == config.default_upstream_model_name)
except Exception as exc:  # noqa: BLE001
    check("settings fast -> pinned slug (no exc)", False)
    print("  err:", repr(exc)[:120])


# ---------- 3) 载荷形态 ----------
class FakeResp:
    status_code = 200

    def json(self):
        return {"conduit_token": "tok"}


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.captured = None

    def post(self, url, headers=None, json=None, timeout=None, stream=None):
        self.captured = json
        return FakeResp()


def make_api():
    api = object.__new__(OpenAIBackendAPI)
    api.session = FakeSession()
    api.base_url = "https://chatgpt.com"
    api.access_token = "tok"
    return api


req = ChatRequirements(token="req")

api = make_api()
api._prepare_image_conversation("p", req, "gpt-image-2", pipeline="natural")
check("prepare natural no hints", "system_hints" not in api.session.captured)
check("prepare natural model auto", api.session.captured.get("model") == "auto")

api = make_api()
api._prepare_image_conversation("p", req, "gpt-image-2", pipeline="fast")
check("prepare fast hints", api.session.captured.get("system_hints") == ["picture_v2"])
check("prepare fast model pinned", api.session.captured.get("model") == config.default_upstream_model_name)

api = make_api()
api._start_image_generation("p", req, "ctok", "gpt-image-2", [], pipeline="natural")
pl = api.session.captured
check("start natural no meta hints", "system_hints" not in (pl.get("metadata") or {}))
check("start natural no top hints", "system_hints" not in pl)
check("start natural model auto", pl.get("model") == "auto")

api = make_api()
api._start_image_generation("p", req, "ctok", "gpt-image-2", [], pipeline="fast")
pl = api.session.captured
check("start fast meta hints", ((pl.get("messages") or [{}])[0].get("metadata") or {}).get("system_hints") == ["picture_v2"])
check("start fast top hints", pl.get("system_hints") == ["picture_v2"])

# ---------- 4) handle() 编排 ----------
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
body = {"prompt": "把背景换成纯红色", "images": [(PNG, "a.png", "image/png")], "n": 1}
calls = []


def fake_pool_ok(req):
    calls.append(req)

    def gen():
        if req.meta_sink is not None:
            req.meta_sink.clear()
            req.meta_sink.update({"model_slug": "gpt-6"})
        yield ImageOutput(kind="result", model=req.model, index=1, total=1,
                          data=[{"b64_json": base64.b64encode(b"png").decode()}])
    return gen()


def fake_pool_fail_first(req):
    calls.append(req)
    if len(calls) == 1:
        raise edit_mod.ImageGenerationError("text reply no image")

    def gen():
        if req.meta_sink is not None:
            req.meta_sink.clear()
            req.meta_sink.update({"model_slug": "gpt-6"})
        yield ImageOutput(kind="result", model=req.model, index=1, total=1,
                          data=[{"b64_json": base64.b64encode(b"png").decode()}])
    return gen()


edit_mod.stream_image_outputs_with_pool = fake_pool_fail_first
edit_mod.enhance_prompt_for_edit = lambda imgs, prompt: "ENHANCED:" + prompt
edit_mod.image_usage = lambda **kw: {}
edit_mod.count_text_tokens = lambda *a, **k: 0
edit_mod.count_image_inputs_tokens = lambda *a, **k: 0
edit_mod.count_image_output_items_tokens = lambda *a, **k: 0

res = edit_mod.handle(dict(body))
check("auto attempt1 natural raw prompt",
      calls and calls[0].image_pipeline == "natural" and calls[0].prompt == "把背景换成纯红色")
check("auto fallback fast+enhanced",
      len(calls) > 1 and calls[1].image_pipeline == "fast" and calls[1].prompt == "ENHANCED:把背景换成纯红色")
check("auto result image + meta pipeline",
      bool(res.get("data")) and (res.get("_upstream_meta") or {}).get("pipeline") == "fast")

calls.clear()
edit_mod.stream_image_outputs_with_pool = fake_pool_ok
res = edit_mod.handle(dict(body))
check("auto natural first ok single call",
      len(calls) == 1 and calls[0].image_pipeline == "natural" and bool(res.get("data")))

calls.clear()
config.update({"image_prompt_enhance_enabled": False})
edit_mod.stream_image_outputs_with_pool = fake_pool_fail_first
res = edit_mod.handle(dict(body))
check("auto enhance-off fallback fast raw",
      len(calls) > 1 and calls[1].image_pipeline == "fast" and calls[1].prompt == "把背景换成纯红色")
config.update({"image_prompt_enhance_enabled": True})

config.update({"image_edit_pipeline": "fast"})
calls.clear()
edit_mod.stream_image_outputs_with_pool = fake_pool_ok
res = edit_mod.handle(dict(body))
check("fast pipeline single enhanced attempt",
      len(calls) == 1 and calls[0].image_pipeline == "fast" and str(calls[0].prompt).startswith("ENHANCED:"))
config.update({"image_edit_pipeline": "auto"})

print("-" * 40)
if FAILURES:
    print("FAILED:", FAILURES)
    sys.exit(1)
print("ALL TESTS PASSED")

from __future__ import annotations

from io import BytesIO
from typing import Any, Iterator

from PIL import Image

from services.config import config
from services.protocol.conversation import (
    ConversationRequest,
    ImageGenerationError,
    collect_image_outputs,
    count_text_tokens,
    encode_images,
    enhance_prompt_for_edit,
    stream_image_chunks,
    stream_image_outputs_with_pool,
)
from utils.helper import is_codex_image_model
from utils.image_tokens import count_image_inputs_tokens, count_image_output_items_tokens, image_usage
from utils.log import logger

# NATURAL-PIPELINE-20261020: 惰性转写哨兵——fast 回退臂真正执行时才做前置转写
_ENHANCE = object()


def _composite_mask(
    images: list[tuple[bytes, str, str]],
    masks: list[tuple[bytes, str, str]],
) -> list[tuple[bytes, str, str]]:
    """将 mask 的 alpha 通道合成到图片中，标识需要编辑的区域。

    mask 的透明区域（低 alpha）= 需要编辑的区域，
    mask 的不透明区域（高 alpha）= 保留的区域。
    如果无 mask 则返回原图。
    """
    if not masks:
        return images
    result: list[tuple[bytes, str, str]] = []
    for i, (data, filename, mime_type) in enumerate(images):
        mask_data = masks[i][0] if i < len(masks) else masks[-1][0]
        img = Image.open(BytesIO(data)).convert("RGBA")
        mask_img = Image.open(BytesIO(mask_data))
        if mask_img.mode == "RGBA":
            alpha = mask_img.split()[3]
        elif mask_img.mode == "L":
            alpha = mask_img
        else:
            alpha = mask_img.convert("L")
        alpha = alpha.resize(img.size, Image.LANCZOS)
        img.putalpha(alpha)
        buf = BytesIO()
        img.save(buf, format="PNG")
        result.append((buf.getvalue(), filename, "image/png"))
    return result


def _result_has_image(result: dict[str, Any] | None) -> bool:
    return bool(result and result.get("data"))


def _run_attempt(
    body: dict[str, Any],
    images: list[tuple[bytes, str, str]],
    prompt: str,
    pipeline: str,
    stream_flag: bool,
) -> tuple[Any, dict[str, Any]]:
    """执行一次生图尝试（pipeline: fast|natural），返回 (结果或流迭代器, meta_sink)。"""
    model = str(body.get("model") or "gpt-image-2")
    n = int(body.get("n") or 1)
    size = body.get("size")
    quality = str(body.get("quality") or "auto")
    response_format = str(body.get("response_format") or "b64_json")
    base_url = str(body.get("base_url") or "") or None
    progress_callback = body.get("progress_callback")
    meta_sink: dict[str, Any] = {}
    encoded_images = encode_images(images)
    if not encoded_images:
        raise ImageGenerationError("image is required")
    outputs = stream_image_outputs_with_pool(ConversationRequest(
        prompt=prompt,
        model=model,
        n=n,
        size=size,
        quality=quality,
        response_format=response_format,
        base_url=base_url,
        images=encoded_images,
        message_as_error=True,
        progress_callback=progress_callback,
        meta_sink=meta_sink,
        image_pipeline=pipeline,
    ))
    if stream_flag:
        return stream_image_chunks(outputs), meta_sink
    result = collect_image_outputs(outputs)
    if meta_sink:
        meta_sink["pipeline"] = pipeline
        result["_upstream_meta"] = dict(meta_sink)
    result["usage"] = image_usage(
        input_text_tokens=count_text_tokens(prompt, model),
        input_image_tokens=count_image_inputs_tokens(images, model),
        output_tokens=count_image_output_items_tokens(result.get("data"), size, quality),
    )
    return result, meta_sink


def _enhanced_prompt(raw_images: list[tuple[bytes, str, str]], prompt: str) -> str:
    return enhance_prompt_for_edit(raw_images, prompt)


def _handle_stream(
    body: dict[str, Any],
    raw_images: list[tuple[bytes, str, str]],
    images: list[tuple[bytes, str, str]],
    prompt: str,
    pipeline: str,
) -> Any:
    """流式路径：无法中途换管线（进度已发出），auto 按自然管线执行，仅首块前失败兜底一次。"""
    if pipeline == "fast":
        final = _enhanced_prompt(raw_images, prompt) if config.image_prompt_enhance_enabled else prompt
        chunks, _ = _run_attempt(body, images, final, "fast", stream_flag=True)
        return chunks
    chunks, _ = _run_attempt(body, images, prompt, "natural", stream_flag=True)
    if pipeline != "auto":
        return chunks

    def _guarded() -> Iterator[dict[str, Any]]:
        try:
            first = next(chunks)
        except StopIteration:
            return
        except Exception:
            logger.warning({"event": "image_edit_stream_fallback", "from": "natural", "to": "fast"})
            final = _enhanced_prompt(raw_images, prompt) if config.image_prompt_enhance_enabled else prompt
            fb, _ = _run_attempt(body, images, final, "fast", stream_flag=True)
            yield from fb
            return
        yield first
        yield from chunks

    return _guarded()


def handle(body: dict[str, Any]) -> dict[str, Any] | Iterator[dict[str, Any]]:
    prompt = str(body.get("prompt") or "")
    raw_images = body.get("images") or []
    masks = body.get("mask") or []
    progress_callback = body.get("progress_callback")
    stream_flag = bool(body.get("stream"))
    pipeline = config.image_edit_pipeline  # fast | natural | auto（默认）
    model = str(body.get("model") or "gpt-image-2")
    # NATURAL-PIPELINE-20261020: codex 通道（paid 账号）不走本编排，保持现行为
    if is_codex_image_model(model):
        pipeline = "fast"

    # mask 合成一次，所有尝试共用；前置转写用原图（mask 只是区域标记，会干扰理解）
    images = _composite_mask(raw_images, masks)
    if not images:
        raise ImageGenerationError("image is required")

    if stream_flag:
        return _handle_stream(body, raw_images, images, prompt, pipeline)

    # 非流式：按管线编排尝试链
    attempts: list[tuple[str, Any]] = []
    if pipeline == "fast":
        attempts.append(("fast", _ENHANCE if config.image_prompt_enhance_enabled else prompt))
    elif pipeline == "natural":
        attempts.append(("natural", prompt))
    else:  # auto（默认）：natural → fast+前置转写 → fast+原始 prompt
        attempts.append(("natural", prompt))
        if config.image_prompt_enhance_enabled:
            attempts.append(("fast", _ENHANCE))
        attempts.append(("fast", prompt))

    last_result: dict[str, Any] | None = None
    last_error: Exception | None = None
    for idx, (pl, prompt_or_sentinel) in enumerate(attempts):
        actual_prompt = prompt
        if prompt_or_sentinel is _ENHANCE:
            if progress_callback:
                try:
                    progress_callback("enhancing_prompt")
                except Exception:
                    pass
            actual_prompt = _enhanced_prompt(raw_images, prompt)
        try:
            result, _meta = _run_attempt(body, images, actual_prompt, pl, stream_flag=False)
        except Exception as exc:
            last_error = exc
            last_result = None
            logger.warning({
                "event": "image_edit_pipeline_attempt_failed",
                "pipeline": pl,
                "attempt": idx + 1,
                "enhanced": prompt_or_sentinel is _ENHANCE,
                "error": repr(exc)[:240],
            })
            continue
        if _result_has_image(result):
            if idx:
                logger.info({"event": "image_edit_pipeline_fallback_used", "pipeline": pl, "attempt": idx + 1})
            return result
        last_result = result
        logger.warning({
            "event": "image_edit_pipeline_attempt_no_image",
            "pipeline": pl,
            "attempt": idx + 1,
        })
    if last_result is not None:
        return last_result
    assert last_error is not None  # 至少一次尝试必然产生异常或结果
    raise last_error

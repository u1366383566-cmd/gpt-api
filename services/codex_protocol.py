from __future__ import annotations

from typing import Any


# The public API keeps the existing hyphenated naming convention while the
# upstream Codex endpoint currently accepts the dotted spelling.
CODEX_PUBLIC_TEXT_MODEL = "gpt-5-5"
CODEX_UPSTREAM_TEXT_MODEL = "gpt-5.5"

CODEX_TEXT_MODEL_ALIASES = {
    CODEX_PUBLIC_TEXT_MODEL: CODEX_UPSTREAM_TEXT_MODEL,
    CODEX_UPSTREAM_TEXT_MODEL: CODEX_UPSTREAM_TEXT_MODEL,
}

CODEX_TOOL_ITEM_TYPES = {
    "function_call",
    "function_call_output",
    "custom_tool_call",
    "custom_tool_call_output",
}


def normalize_codex_model(value: object) -> str:
    model = str(value or "").strip().lower()
    return CODEX_TEXT_MODEL_ALIASES.get(model, model)


def is_codex_function_tool(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    tool_type = str(value.get("type") or "").strip().lower()
    return tool_type in {"function", "computer_use_preview", "local_shell", "shell"}


def contains_codex_tool_item(value: object) -> bool:
    if isinstance(value, dict):
        if str(value.get("type") or "").strip().lower() in CODEX_TOOL_ITEM_TYPES:
            return True
        return any(contains_codex_tool_item(item) for item in value.values())
    if isinstance(value, list):
        return any(contains_codex_tool_item(item) for item in value)
    return False


def is_native_codex_request(body: dict[str, Any]) -> bool:
    """Identify requests that need the native Codex Responses protocol."""
    tools = body.get("tools")
    if isinstance(tools, list) and any(is_codex_function_tool(tool) for tool in tools):
        return True
    return contains_codex_tool_item(body.get("input"))

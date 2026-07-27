"""Pure validation and provider-bound LLM payload transformations."""

from __future__ import annotations

import json
import math
from typing import Any


def _require_nonempty_string(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _validate_json(value: Any, path: str) -> None:
    if value is None or type(value) in {str, bool, int}:
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{path} must contain only finite JSON numbers")
        return
    if type(value) is list:
        for index, item in enumerate(value):
            _validate_json(item, f"{path}[{index}]")
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError(f"{path} must contain only string JSON keys")
            _validate_json(item, f"{path}.{key}")
        return
    raise ValueError(f"{path} must contain only JSON values")


def _validate_message(message: dict, index: int) -> None:
    path = f"messages[{index}]"
    role = message.get("role")
    if role not in {"user", "assistant"}:
        raise ValueError(f"{path}.role must be user or assistant")
    if "content" not in message:
        raise ValueError(f"{path}.content is required")
    content = message["content"]
    if isinstance(content, str):
        return
    if type(content) is not list:
        raise ValueError(f"{path}.content must be a string or list")
    for block_index, block in enumerate(content):
        block_path = f"{path}.content[{block_index}]"
        if type(block) is not dict:
            raise ValueError(f"{block_path} must be a dictionary")
        block_type = block.get("type")
        if block_type == "text":
            if not isinstance(block.get("text"), str):
                raise ValueError(f"{block_path}.text must be a string")
        elif block_type == "tool_use":
            _require_nonempty_string(f"{block_path}.id", block.get("id"))
            _require_nonempty_string(f"{block_path}.name", block.get("name"))
            if type(block.get("input")) is not dict:
                raise ValueError(f"{block_path}.input must be a dictionary")
            if role != "assistant":
                raise ValueError(f"{block_path} requires assistant role")
        elif block_type == "tool_result":
            _require_nonempty_string(
                f"{block_path}.tool_use_id",
                block.get("tool_use_id"),
            )
            if not isinstance(block.get("content"), str):
                raise ValueError(f"{block_path}.content must be a string")
            if role != "user":
                raise ValueError(f"{block_path} requires user role")
        else:
            raise ValueError(f"{block_path}.type is unsupported")


def _validate_tool(tool: dict, index: int) -> None:
    path = f"tools[{index}]"
    _require_nonempty_string(f"{path}.name", tool.get("name"))
    description = tool.get("description", "")
    if not isinstance(description, str):
        raise ValueError(f"{path}.description must be a string")
    if type(tool.get("input_schema")) is not dict:
        raise ValueError(f"{path}.input_schema must be a dictionary")


def validate_llm_payload(
    *,
    system: str,
    messages: list[dict],
    tools: list[dict],
    tool_choice: str | None = None,
) -> None:
    if not isinstance(system, str):
        raise ValueError("system must be a string")
    if type(messages) is not list:
        raise ValueError("messages must be a list")
    if type(tools) is not list:
        raise ValueError("tools must be a list")
    if tool_choice is not None:
        _require_nonempty_string("tool_choice", tool_choice)
    for index, message in enumerate(messages):
        if type(message) is not dict:
            raise ValueError(f"messages[{index}] must be a dictionary")
        _validate_message(message, index)
    for index, tool in enumerate(tools):
        if type(tool) is not dict:
            raise ValueError(f"tools[{index}] must be a dictionary")
        _validate_tool(tool, index)
    _validate_json(
        {
            "system": system,
            "messages": messages,
            "tools": tools,
        },
        "payload",
    )
    try:
        json.dumps(
            {
                "system": system,
                "messages": messages,
                "tools": tools,
            },
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("payload must be finite JSON") from exc


def _validate_provider_payload(payload: dict) -> None:
    _validate_json(payload, "provider payload")
    try:
        json.dumps(payload, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("provider payload must be finite JSON") from exc


def to_openai(
    system: str,
    messages: list[dict],
    tools: list[dict],
) -> tuple[list[dict], list[dict]]:
    out: list[dict] = [{"role": "system", "content": system}]
    for message in messages:
        role = message["role"]
        content = message["content"]
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        if role == "assistant":
            text = "".join(
                block.get("text", "")
                for block in content
                if block.get("type") == "text"
            )
            tool_calls = [
                {
                    "id": block["id"],
                    "type": "function",
                    "function": {
                        "name": block["name"],
                        "arguments": json.dumps(
                            block["input"],
                            allow_nan=False,
                        ),
                    },
                }
                for block in content
                if block.get("type") == "tool_use"
            ]
            entry: dict[str, Any] = {
                "role": "assistant",
                "content": text or None,
            }
            if tool_calls:
                entry["tool_calls"] = tool_calls
            out.append(entry)
            continue

        handled = False
        for block in content:
            if block.get("type") == "tool_result":
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": block["tool_use_id"],
                        "content": block["content"],
                    }
                )
                handled = True
        texts = "".join(
            block.get("text", "")
            for block in content
            if block.get("type") == "text"
        )
        if texts or not handled:
            out.append({"role": "user", "content": texts})

    openai_tools = [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool["input_schema"],
            },
        }
        for tool in tools
    ]
    return out, openai_tools


def sanitize_gemini_schema(schema: Any) -> Any:
    if not isinstance(schema, dict):
        return schema
    out = {}
    for key, value in schema.items():
        if key == "type" and isinstance(value, list):
            non_null = [item for item in value if item != "null"]
            out[key] = non_null[0] if non_null else "string"
        elif key == "properties" and isinstance(value, dict):
            out[key] = {
                name: sanitize_gemini_schema(item)
                for name, item in value.items()
            }
        elif key == "items":
            out[key] = sanitize_gemini_schema(value)
        else:
            out[key] = value
    return out


def to_gemini_contents(messages: list[dict]) -> list[dict]:
    id_to_name: dict[str, str] = {}
    for message in messages:
        if isinstance(message["content"], list):
            for block in message["content"]:
                if block.get("type") == "tool_use":
                    id_to_name[block["id"]] = block["name"]

    contents: list[dict] = []
    for message in messages:
        role = "model" if message["role"] == "assistant" else "user"
        content = message["content"]
        parts: list[dict] = []
        if isinstance(content, str):
            parts.append({"text": content})
        else:
            for block in content:
                if block.get("type") == "text":
                    parts.append({"text": block["text"]})
                elif block.get("type") == "tool_use":
                    parts.append(
                        {
                            "function_call": {
                                "name": block["name"],
                                "args": block["input"],
                            }
                        }
                    )
                elif block.get("type") == "tool_result":
                    name = id_to_name.get(block["tool_use_id"], "tool")
                    payload: Any = block["content"]
                    try:
                        payload = json.loads(payload)
                    except (json.JSONDecodeError, TypeError):
                        payload = {"result": payload}
                    parts.append(
                        {
                            "function_response": {
                                "name": name,
                                "response": payload,
                            }
                        }
                    )
        contents.append({"role": role, "parts": parts})
    return contents


def build_anthropic_payload(
    *,
    system: str,
    messages: list[dict],
    tools: list[dict],
    tool_choice: str | None = None,
    conservative_tool_choice: bool = False,
) -> dict:
    validate_llm_payload(
        system=system,
        messages=messages,
        tools=tools,
        tool_choice=tool_choice,
    )
    payload = {
        "system": system,
        "messages": messages,
        "tools": tools,
    }
    if tools and (tool_choice == "any" or conservative_tool_choice):
        payload["tool_choice"] = {"type": "any"}
    _validate_provider_payload(payload)
    return payload


def build_groq_payload(
    *,
    system: str,
    messages: list[dict],
    tools: list[dict],
    tool_choice: str | None = None,
    conservative_tool_choice: bool = False,
) -> dict:
    validate_llm_payload(
        system=system,
        messages=messages,
        tools=tools,
        tool_choice=tool_choice,
    )
    openai_messages, openai_tools = to_openai(system, messages, tools)
    payload: dict[str, Any] = {"messages": openai_messages}
    if openai_tools:
        payload["tools"] = openai_tools
        payload["tool_choice"] = (
            "required"
            if tool_choice == "any" or conservative_tool_choice
            else "auto"
        )
    _validate_provider_payload(payload)
    return payload


def build_gemini_payload(
    *,
    system: str,
    messages: list[dict],
    tools: list[dict],
    tool_choice: str | None = None,
    conservative_tool_choice: bool = False,
) -> dict:
    validate_llm_payload(
        system=system,
        messages=messages,
        tools=tools,
        tool_choice=tool_choice,
    )
    declarations = [
        {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": sanitize_gemini_schema(tool["input_schema"]),
        }
        for tool in tools
    ]
    payload: dict[str, Any] = {
        "system_instruction": system,
        "contents": to_gemini_contents(messages),
        "tools": (
            [{"function_declarations": declarations}]
            if declarations
            else None
        ),
    }
    if declarations and (
        tool_choice == "any" or conservative_tool_choice
    ):
        payload["tool_config"] = {
            "function_calling_config": {"mode": "ANY"}
        }
    _validate_provider_payload(payload)
    return payload

"""Gemini backend (google-genai, function calling).

Response normalization (from_gemini) is separated from SDK-dependent request
building so it can be unit-tested with a fake response and no SDK installed.
"""

from __future__ import annotations

from typing import Any, Optional

from ..security.outbound import OutboundPolicy, new_httpx_client
from .base import LLMResponse, TextBlock, ToolUseBlock, Usage
from .payloads import (
    build_gemini_payload,
    sanitize_gemini_schema,
)


_GEMINI_ORIGIN = "https://generativelanguage.googleapis.com"
_GEMINI_POLICY = OutboundPolicy(_GEMINI_ORIGIN)


def _sanitize_schema(schema: Any) -> Any:
    return sanitize_gemini_schema(schema)


def from_gemini(resp: Any) -> LLMResponse:
    """Normalize a Gemini generate_content response into our shape."""
    blocks: list = []
    stop = "end_turn"
    candidates = getattr(resp, "candidates", None) or []
    if candidates:
        parts = getattr(candidates[0].content, "parts", None) or []
        for i, part in enumerate(parts):
            fc = getattr(part, "function_call", None)
            if fc is not None:
                args = dict(getattr(fc, "args", {}) or {})
                blocks.append(ToolUseBlock(id=f"gemini-{i}", name=fc.name, input=args))
                stop = "tool_use"
            elif getattr(part, "text", None):
                blocks.append(TextBlock(text=part.text))
    if not blocks:
        blocks.append(TextBlock(text=""))
    meta = getattr(resp, "usage_metadata", None)
    usage = None
    if meta is not None:
        try:
            input_tokens = getattr(meta, "prompt_token_count")
            output_tokens = getattr(meta, "candidates_token_count")
        except AttributeError:
            pass
        else:
            if (
                type(input_tokens) is int
                and input_tokens >= 0
                and type(output_tokens) is int
                and output_tokens >= 0
            ):
                usage = Usage(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )
    return LLMResponse(
        content=blocks,
        stop_reason=stop,
        usage=usage,
        model=getattr(resp, "model_version", ""),
    )


class GeminiBackend:
    def __init__(
        self,
        api_key: str,
        model: str,
        max_tokens: int = 1024,
        client: Any = None,
        timeout_seconds: float = 45.0,
    ) -> None:
        self._api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self._client = client
        self._timeout_seconds = timeout_seconds

    def _get_client(self):
        if self._client is None:
            from google import genai
            from google.genai import types

            self._client = genai.Client(
                api_key=self._api_key,
                http_options=types.HttpOptions(
                    baseUrl=_GEMINI_ORIGIN,
                    timeout=int(self._timeout_seconds * 1000),
                    httpxClient=new_httpx_client(
                        _GEMINI_POLICY,
                        read_timeout=self._timeout_seconds,
                    ),
                ),
            )
        return self._client

    def create(
        self, *, system: str, messages: list[dict], tools: list[dict],
        tool_choice: Optional[str] = None,
        request_id: str = "",
    ) -> LLMResponse:
        payload = build_gemini_payload(
            system=system,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
        )
        from google.genai import types

        gem_contents = [
            types.Content(
                role=c["role"], parts=[self._part(types, p) for p in c["parts"]]
            )
            for c in payload["contents"]
        ]
        decls = [
            types.FunctionDeclaration(
                name=declaration["name"],
                description=declaration["description"],
                parameters=declaration["parameters"],
            )
            for declaration in (
                payload["tools"][0]["function_declarations"]
                if payload["tools"] is not None
                else []
            )
        ]
        cfg_kwargs: dict[str, Any] = dict(
            system_instruction=payload["system_instruction"],
            max_output_tokens=self.max_tokens,
            tools=[types.Tool(function_declarations=decls)] if decls else None,
        )
        # Preserve the validated explicit mode. "any" forces a function call
        # so a 200 always carries structured output.
        if "tool_config" in payload:
            mode = payload["tool_config"]["function_calling_config"][
                "mode"
            ]
            cfg_kwargs["tool_config"] = types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode=mode
                )
            )
        config = types.GenerateContentConfig(**cfg_kwargs)
        resp = self._get_client().models.generate_content(
            model=self.model, contents=gem_contents, config=config
        )
        return from_gemini(resp)

    @staticmethod
    def _part(types, p: dict):
        if "text" in p:
            return types.Part.from_text(text=p["text"])
        if "function_call" in p:
            fc = p["function_call"]
            return types.Part(
                function_call=types.FunctionCall(name=fc["name"], args=fc["args"])
            )
        if "function_response" in p:
            fr = p["function_response"]
            return types.Part(
                function_response=types.FunctionResponse(
                    name=fr["name"], response=fr["response"]
                )
            )
        return types.Part.from_text(text="")

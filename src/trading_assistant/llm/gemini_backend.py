"""Gemini backend (google-genai, function calling).

Response normalization (from_gemini) is separated from SDK-dependent request
building so it can be unit-tested with a fake response and no SDK installed.
"""

from __future__ import annotations

from typing import Any, Optional

from .base import LLMResponse, TextBlock, ToolUseBlock, Usage, to_gemini_contents


def _sanitize_schema(schema: Any) -> Any:
    """Make a JSON schema Gemini-friendly: collapse ["string","null"] unions to a
    single type and recurse into properties/items."""
    if not isinstance(schema, dict):
        return schema
    out = {}
    for k, v in schema.items():
        if k == "type" and isinstance(v, list):
            non_null = [t for t in v if t != "null"]
            out[k] = non_null[0] if non_null else "string"
        elif k == "properties" and isinstance(v, dict):
            out[k] = {pk: _sanitize_schema(pv) for pk, pv in v.items()}
        elif k == "items":
            out[k] = _sanitize_schema(v)
        else:
            out[k] = v
    return out


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
    return LLMResponse(
        content=blocks,
        stop_reason=stop,
        usage=Usage(
            input_tokens=getattr(meta, "prompt_token_count", 0) or 0,
            output_tokens=getattr(meta, "candidates_token_count", 0) or 0,
        ),
        model=getattr(resp, "model_version", ""),
    )


class GeminiBackend:
    def __init__(
        self, api_key: str, model: str, max_tokens: int = 1024, client: Any = None
    ) -> None:
        self._api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self._client = client

    def _get_client(self):
        if self._client is None:
            from google import genai

            self._client = genai.Client(api_key=self._api_key)
        return self._client

    def create(
        self, *, system: str, messages: list[dict], tools: list[dict],
        tool_choice: Optional[str] = None,
    ) -> LLMResponse:
        from google.genai import types

        gem_contents = [
            types.Content(
                role=c["role"], parts=[self._part(types, p) for p in c["parts"]]
            )
            for c in to_gemini_contents(messages)
        ]
        decls = [
            types.FunctionDeclaration(
                name=t["name"],
                description=t.get("description", ""),
                parameters=_sanitize_schema(t["input_schema"]),
            )
            for t in tools
        ]
        cfg_kwargs: dict[str, Any] = dict(
            system_instruction=system,
            max_output_tokens=self.max_tokens,
            tools=[types.Tool(function_declarations=decls)] if decls else None,
        )
        # "any" forces a function call so a 200 always carries structured output
        # (Gemini otherwise sometimes replies in prose -> "did not submit a plan").
        if decls and tool_choice == "any":
            cfg_kwargs["tool_config"] = types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode="ANY")
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

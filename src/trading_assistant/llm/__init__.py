"""Pluggable LLM backends behind one normalized interface.

The agent and analyst were written against Anthropic's response shape (content
blocks with type text|tool_use, a stop_reason, usage). This package lets Gemini
and Groq speak that same shape by translating the Anthropic-style messages/tools
we already produce into each provider's format and normalizing the response back.
"""

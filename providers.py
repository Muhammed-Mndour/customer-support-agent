"""Which model service the agent talks to, chosen in .env.

The agent loop is written for the Anthropic Messages API: it calls
client.messages.create(...) and reads stop_reason and content blocks back.

- MODEL_PROVIDER=anthropic (default): the official Anthropic client. Works for Claude,
  and for Ollama, which speaks the same API.
- MODEL_PROVIDER=openai: an adapter for services that speak the OpenAI Chat Completions
  API instead, like Gemini. It looks like the Anthropic client from the outside, and
  translates each request and response between the two shapes, so the agent loop,
  the tools and the API don't change.
"""

import json
import os
from types import SimpleNamespace

import anthropic
import openai

# Errors that mean "the model service failed", from either client. Callers catch these.
MODEL_ERRORS = (anthropic.APIStatusError, anthropic.APIConnectionError,
                openai.APIStatusError, openai.APIConnectionError)
AUTH_ERRORS = (anthropic.AuthenticationError, openai.AuthenticationError)


def make_client():
    """Build the client .env asks for."""
    if os.environ.get("MODEL_PROVIDER", "anthropic") == "openai":
        return OpenAICompatibleClient(
            base_url=os.environ["OPENAI_BASE_URL"],
            api_key=os.environ.get("OPENAI_API_KEY") or os.environ.get("GEMINI_API_KEY"),
        )
    return anthropic.Anthropic()  # reads ANTHROPIC_API_KEY and ANTHROPIC_BASE_URL itself


def _field(block, name, default=None):
    """History blocks are SDK objects, our own SimpleNamespace blocks, or plain dicts."""
    if isinstance(block, dict):
        return block.get(name, default)
    return getattr(block, name, default)


class OpenAICompatibleClient:
    """Looks like anthropic.Anthropic() to the agent loop; talks OpenAI to the service."""

    def __init__(self, base_url, api_key):
        self._client = openai.OpenAI(base_url=base_url, api_key=api_key)
        self.messages = self  # so the agent can call client.messages.create(...)

    # --- Anthropic request -> OpenAI request ---------------------------------------------

    @staticmethod
    def _tools(tools):
        return [{"type": "function",
                 "function": {"name": t["name"], "description": t["description"],
                              "parameters": t["input_schema"]}}
                for t in tools]

    @staticmethod
    def _messages(system, messages):
        out = [{"role": "system", "content": system}]
        for message in messages:
            content = message["content"]
            if message["role"] == "user":
                if isinstance(content, str):
                    out.append({"role": "user", "content": content})
                    continue
                # A list from a user turn holds tool results: one "tool" message each.
                for block in content:
                    if _field(block, "type") == "tool_result":
                        out.append({"role": "tool", "tool_call_id": _field(block, "tool_use_id"),
                                    "content": _field(block, "content")})
                continue

            text = "\n".join(_field(b, "text") for b in content if _field(b, "type") == "text")
            calls = []
            for block in content:
                if _field(block, "type") != "tool_use":
                    continue
                call = {"id": _field(block, "id"), "type": "function",
                        "function": {"name": _field(block, "name"),
                                     "arguments": json.dumps(_field(block, "input"), ensure_ascii=False)}}
                # Gemini signs its tool calls; the signature must come back unchanged or
                # the next tool call can fail. It travels in "extra_content".
                if _field(block, "extra_content"):
                    call["extra_content"] = _field(block, "extra_content")
                calls.append(call)
            assistant = {"role": "assistant", "content": text or None}
            if calls:
                assistant["tool_calls"] = calls
            out.append(assistant)
        return out

    # --- OpenAI response -> Anthropic response ------------------------------------------

    @staticmethod
    def _response(completion):
        choice = completion.choices[0]
        message = choice.message
        content = []
        if message.content:
            content.append(SimpleNamespace(type="text", text=message.content))
        for call in message.tool_calls or []:
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {}  # the tool then fails with a clear error the model can read
            extra = (call.model_extra or {}).get("extra_content")
            content.append(SimpleNamespace(type="tool_use", id=call.id, name=call.function.name,
                                           input=arguments, extra_content=extra))

        if message.tool_calls:
            stop_reason = "tool_use"
        else:
            stop_reason = {"length": "max_tokens",
                           "content_filter": "refusal"}.get(choice.finish_reason, "end_turn")
        return SimpleNamespace(stop_reason=stop_reason, content=content)

    def create(self, *, model, max_tokens, system, tools, messages, **ignored):
        """Same call as anthropic's messages.create. Anthropic-only options such as
        thinking and output_config have no OpenAI equivalent here, so they are ignored."""
        completion = self._client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=self._messages(system, messages),
            tools=self._tools(tools),
        )
        return self._response(completion)

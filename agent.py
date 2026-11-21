"""A customer support agent for a fake online store, with a hand-written agent loop.

Run:  python agent.py   (type "exit" to quit)

Two loops:
  - chat loop:  one turn per customer message, until the customer types "exit".
  - agent loop: inside each turn, ask the model, run the tool it asks for, send
                the result back, and repeat until it answers (max MAX_STEPS).
"""

import json
import os
import re
import sys
from datetime import date

from dotenv import load_dotenv

from providers import AUTH_ERRORS, MODEL_ERRORS, make_client
from tools import REFUND_APPROVAL_LIMIT, TOOLS, run_tool

# Read .env into the environment. override=True makes .env win over variables the
# shell already has (some terminals set ANTHROPIC_BASE_URL, which would hide ours).
load_dotenv(override=True)

# Which model answers. The same code talks to Claude or to a local Ollama model:
# for Ollama, .env also sets ANTHROPIC_BASE_URL, which the SDK picks up itself.
MODEL = os.environ.get("AGENT_MODEL", "claude-haiku-5-5")
THINKING = os.environ.get("AGENT_THINKING", "on") == "on"  # "off" is much faster on a CPU
MAX_STEPS = 10      # model calls allowed per customer message
MAX_TOKENS = 16000  # per model response

SYSTEM_PROMPT = f"""You are a customer support agent for an online store in Egypt.
Today's date is {date.today().isoformat()}.

How you talk:
- Reply in the language the customer writes in: English to English, Arabic to Arabic.
  If they switch language, switch with them. Be friendly, polite, short and clear.
- Speak like a support person, not a system: never mention tool names, JSON fields
  or other internal details. Say "I can refund it for you", not "use refund_order".
- Write product names, order numbers and dates the way the tools return them.

How you work:
- Only state facts that came from a tool result in this conversation. If the tools
  don't have the answer, say you don't know and offer to open a ticket for a human.
  Never guess order details, dates, prices or policies.
- If a message starts with [customer_id: N], the customer is already identified: use
  that customer_id with the tools, and never ask them for a phone number.
  Otherwise, ask for their phone number and use get_customer_by_phone.
- If it is unclear which order the customer means, list their orders and ask.
- Only cancel or refund when the customer clearly asks for it.
- If a tool returns an error, explain it to the customer in simple words.
- Refunds above {REFUND_APPROVAL_LIMIT} EGP need a supervisor. Tell the customer exactly
  what the refund result says: made, rejected by the supervisor, sent to a supervisor
  and still waiting, or not possible right now. Never promise a refund that wasn't made.
- A refund returns exactly the amount the customer paid for the order, never more.
  If they ask for more (double, triple, extra compensation), still make the normal refund
  of what they paid, say clearly that it covers only that amount, and open a ticket so a
  human handles the extra request.
- Never tell the customer you did or will do something unless you call the tool that
  does it in this same reply. Before saying "I'll connect you with a person", call
  escalate_to_human; if you only want to offer it, ask them instead.
- A message starting with [supervisor decision ...] comes from the system, not the
  customer: tell the customer the outcome it describes, in their language.
- Use escalate_to_human when the customer asks for a person, is very upset, reports a
  health or safety problem caused by a product, asks for compensation beyond a refund,
  or has a problem the tools can't solve."""


class TurnFailed(Exception):
    """The agent couldn't finish this customer message. The text is for the customer."""


def in_customer_language(customer_text, english, arabic):
    """Pick the customer's language for the few messages the code writes itself,
    without the model. Any Arabic letter in their message means Arabic."""
    return arabic if re.search(r"[؀-ۿ]", customer_text) else english


# --- Printing what happens inside the loop -----------------------------------

def show(label, text):
    print(f"  [{label}] {text}")


def short_json(data, limit=300):
    text = json.dumps(data, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + " ..."


def print_event(kind, data):
    """Show one event from the agent loop in the terminal.

    The loop reports what happens as events; whoever runs it decides how to show
    them. The terminal prints them, the web page (app.py) draws them.
    Events: ("step", n), ("thinking", text), ("model_says", text),
            ("tool_call", (name, input)), ("tool_result", result),
            ("approval_request", details), ("approval_result", True/False)
    """
    if kind == "step":
        print(f"\n  --- step {data} ---")
    elif kind == "tool_call":
        name, tool_input = data
        show("tool call", f"{name} {short_json(tool_input)}")
    elif kind == "tool_result":
        show("tool result", short_json(data))
    elif kind == "approval_request":
        pass  # ask_in_terminal prints the question itself
    elif kind == "approval_result":
        show("supervisor", "approved" if data else "rejected")
    else:
        show(kind.replace("_", " "), data)


def ask_in_terminal(details):
    """The human in the loop, terminal version: ask the supervisor y/n."""
    question = (f"\n  [approval needed] Refund {details['amount_egp']} EGP for order "
                f"{details['order_id']} ({details['customer']}, {details['item']})? (y/n): ")
    while True:
        answer = input(question).lstrip("﻿").strip().lower()
        if answer in ("y", "n"):
            return answer == "y"


def split_leaked_thinking(text):
    """Some local models (e.g. qwen3 on Ollama) put their reasoning in the text,
    ending with </think>. Return (thinking, answer); Claude's text passes through."""
    if "</think>" not in text:
        return "", text
    thinking, answer = text.rsplit("</think>", 1)
    return thinking.replace("<think>", "").strip(), answer.strip()


def clean_content(content):
    """Drop leaked reasoning from text blocks, so it isn't shown as the answer and
    doesn't fill the model's context on every later request."""
    cleaned = []
    for block in content:
        if block.type == "text" and "</think>" in block.text:
            _, answer = split_leaked_thinking(block.text)
            if answer:
                cleaned.append({"type": "text", "text": answer})
        else:
            cleaned.append(block)
    return cleaned


# --- The agent loop ----------------------------------------------------------

def run_agent_turn(client, messages, on_event=print_event, approve_refund=None,
                   request_approval=None):
    """Handle one customer message. Returns the final answer, or raises TurnFailed.

    on_event(kind, data) is called for every step, so any interface can show it.
    Big refunds need a human; the caller says how to reach one:
    - approve_refund(details) -> True/False asks now and waits (terminal, web page);
    - request_approval(details) -> approval id records it for later (HTTP API);
    - neither: there is no supervisor and big refunds are refused.
    """
    def ask_supervisor(details):
        on_event("approval_request", details)
        approved = approve_refund(details)  # the loop waits here for the human
        on_event("approval_result", approved)
        return approved

    supervisor = ask_supervisor if approve_refund else None

    # The customer's latest text, so the code's own error messages match their language.
    customer_text = next((m["content"] for m in reversed(messages)
                          if m["role"] == "user" and isinstance(m["content"], str)), "")

    for step in range(1, MAX_STEPS + 1):
        on_event("step", step)
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            thinking={"type": "adaptive", "display": "summarized"} if THINKING else {"type": "disabled"},
            output_config={"effort": "medium"},
            messages=messages,
        )

        # Keep the model's reply as received, including its thinking blocks: the
        # API expects them back unchanged on the next request. Only reasoning
        # that a local model leaked into its text is removed.
        messages.append({"role": "assistant", "content": clean_content(response.content)})

        text_parts = []
        for block in response.content:
            if block.type == "thinking" and block.thinking:
                on_event("thinking", block.thinking.strip())
            elif block.type == "text":
                leaked, answer = split_leaked_thinking(block.text)
                if leaked:
                    on_event("thinking", leaked)
                if not answer:
                    continue
                text_parts.append(answer)
                if response.stop_reason == "tool_use":
                    on_event("model_says", answer)  # a note before a tool call
            elif block.type == "tool_use":
                on_event("tool_call", (block.name, block.input))

        if response.stop_reason == "refusal":
            raise TurnFailed(in_customer_language(
                customer_text,
                "Sorry, I can't help with this request.",
                "عذرًا، لا أستطيع المساعدة في هذا الطلب."))
        if response.stop_reason == "max_tokens":
            raise TurnFailed(in_customer_language(
                customer_text,
                "Something went wrong while preparing my reply. Could you send your message again?",
                "حدثت مشكلة أثناء تجهيز الرد. هل يمكنك إرسال رسالتك مرة أخرى؟"))

        if response.stop_reason != "tool_use":
            # No tool requested: this is the answer for the customer.
            return "\n".join(text_parts)

        # The model asked for one or more tools: run them all, then send every
        # result back in a single message, matched to its request by tool_use_id.
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            result = run_tool(block.name, block.input, approve_refund=supervisor,
                              request_approval=request_approval)
            on_event("tool_result", result)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result, ensure_ascii=False),
                "is_error": "error" in result,
            })
        messages.append({"role": "user", "content": tool_results})

    raise TurnFailed(in_customer_language(
        customer_text,
        "This request took more steps than allowed. Could you rephrase it, "
        "or would you like me to connect you with a person?",
        "استغرق هذا الطلب خطوات أكثر من المسموح. هل يمكنك توضيح طلبك، "
        "أم تفضل أن أحولك إلى أحد الموظفين؟"))


# --- The chat loop -----------------------------------------------------------

def main():
    for stream in (sys.stdin, sys.stdout):
        stream.reconfigure(encoding="utf-8")  # Arabic in the Windows console

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("MODEL_PROVIDER") == "openai"):
        print("ANTHROPIC_API_KEY is not set. Copy .env.example to .env and put your key there.")
        sys.exit(1)

    client = make_client()          # Claude, Ollama or Gemini, from .env
    messages = []                   # the whole conversation, sent on every request

    print(f"Customer support agent ({MODEL}). Type 'exit' to quit.")
    while True:
        try:
            user_text = input("\nYou: ").lstrip("﻿").strip()  # PowerShell pipes add a BOM
        except (EOFError, KeyboardInterrupt):  # Ctrl+C, or input piped from a file ran out
            break
        if user_text.lower() == "exit":
            break
        if not user_text:
            continue

        turn_start = len(messages)
        messages.append({"role": "user", "content": user_text})
        try:
            answer = run_agent_turn(client, messages, approve_refund=ask_in_terminal)
        except AUTH_ERRORS:
            print("The API key was rejected. Check the key in .env.")
            break
        except MODEL_ERRORS as exc:
            # Undo this turn so the conversation stays valid, then let the customer retry.
            del messages[turn_start:]
            print(f"\n  [api error] {exc}")
            print("\nAgent: " + in_customer_language(
                user_text,
                "There was a connection problem. Could you send your message again?",
                "حدثت مشكلة في الاتصال. هل يمكنك إرسال رسالتك مرة أخرى؟"))
            continue
        except TurnFailed as exc:
            del messages[turn_start:]
            print(f"\nAgent: {exc}")
            continue
        print(f"\nAgent: {answer}")


if __name__ == "__main__":
    main()

"""A web chat page for the support agent.

Run:  python -m streamlit run app.py

The agent loop is the same one agent.py uses (agent.run_agent_turn). Only the
chat loop is different: the customer types in the page, every step of the loop
is drawn as a box you can open, and big refunds wait for Approve / Reject buttons.

The loop can take minutes and can stop to wait for the supervisor, so each
customer message runs in its own thread (a Job), and the page redraws it every
second until it is done.
"""

import html
import json
import threading
import time

import streamlit as st

import agent
from providers import AUTH_ERRORS, MODEL_ERRORS, make_client
from db import create_database
from tools import REFUND_APPROVAL_LIMIT

st.set_page_config(page_title="Support agent")


@st.cache_resource
def get_client():
    return make_client()  # same settings as agent.py, read from .env


def rtl(text):
    """Show Arabic right-to-left and English left-to-right, decided per message."""
    return f'<div dir="auto" style="white-space: pre-wrap">{html.escape(text)}</div>'


# --- One customer message, handled in a background thread ----------------------

class Job:
    def __init__(self, prompt):
        self.prompt = prompt
        self.events = []        # everything the agent loop reported, in order
        self.pending = None     # refund details while waiting for the supervisor
        self.decision = None
        self.answered = threading.Event()
        self.answer = ""
        self.error = None
        self.started = time.time()
        self.seconds = 0.0
        self.done = False

    def approve_refund(self, details):
        """Runs in the agent's thread: wait until a button on the page is clicked."""
        self.pending = details
        self.answered.wait()
        self.answered.clear()
        self.pending = None
        return self.decision

    def decide(self, approved):
        """Runs in the page when Approve or Reject is clicked."""
        self.decision = approved
        self.answered.set()


def run_job(job, client, messages):
    turn_start = len(messages)
    messages.append({"role": "user", "content": job.prompt})
    try:
        job.answer = agent.run_agent_turn(
            client, messages,
            on_event=lambda kind, data: job.events.append((kind, data)),
            approve_refund=job.approve_refund,
        )
    except AUTH_ERRORS:
        del messages[turn_start:]
        job.answer = "The API key was rejected. Check the key in .env."
    except MODEL_ERRORS as exc:
        # Undo this turn so the conversation stays valid, then let the customer retry.
        del messages[turn_start:]
        job.error = str(exc)
        job.answer = agent.in_customer_language(
            job.prompt,
            "There was a connection problem. Could you send your message again?",
            "حدثت مشكلة في الاتصال. هل يمكنك إرسال رسالتك مرة أخرى؟")
    except agent.TurnFailed as exc:
        del messages[turn_start:]
        job.answer = str(exc)
    finally:
        job.seconds = time.time() - job.started
        job.done = True


# --- Drawing ---------------------------------------------------------------------

class StepView:
    """Draws the agent loop's events: one collapsible box per step."""

    def __init__(self):
        self.box = None
        self.step = 0
        self.tools = []

    def __call__(self, kind, data):
        if kind == "step":
            self.close()
            self.step, self.tools = data, []
            self.box = st.status(f"Step {data}: asking the model ...", expanded=False)
        elif kind in ("thinking", "model_says"):
            self.box.markdown("**Thinking**" if kind == "thinking" else "**Model says**")
            self.box.markdown(rtl(data), unsafe_allow_html=True)
        elif kind == "tool_call":
            name, tool_input = data
            self.tools.append(name)
            self.box.update(label=f"Step {self.step}: {', '.join(self.tools)}")
            self.box.markdown("**Tool call**")
            self.box.code(f"{name}({json.dumps(tool_input, ensure_ascii=False)})", language="json")
        elif kind == "approval_request":
            self.box.markdown(f"**Supervisor asked:** refund {data['amount_egp']} EGP "
                              f"for order {data['order_id']}")
        elif kind == "approval_result":
            self.box.markdown(f"**Supervisor:** {'approved' if data else 'rejected'}")
        elif kind == "tool_result":
            self.box.markdown("**Tool result**")
            self.box.code(json.dumps(data, ensure_ascii=False, indent=2), language="json")

    def close(self):
        """Mark the current step as done. A step with no tool call was the answer."""
        if self.box is None:
            return
        label = f"Step {self.step}: {', '.join(self.tools)}" if self.tools else f"Step {self.step}: final answer"
        self.box.update(label=label, state="complete")


def show_turn(turn):
    """Draw one finished customer message and the agent's answer."""
    with st.chat_message("user"):
        st.markdown(rtl(turn["user"]), unsafe_allow_html=True)
    with st.chat_message("assistant"):
        view = StepView()
        for kind, data in turn["events"]:
            view(kind, data)
        view.close()
        if turn["error"]:
            st.error(f"API error: {turn['error']}")
        st.markdown(rtl(turn["answer"]), unsafe_allow_html=True)
        st.caption(f"{turn['seconds']:.0f} s")


@st.fragment(run_every=1.0)
def show_running_job():
    """Redraw the message being handled, every second, until its thread is done."""
    job = st.session_state.job
    if job.done:
        st.session_state.turns.append({
            "user": job.prompt, "events": job.events, "answer": job.answer,
            "error": job.error, "seconds": job.seconds,
        })
        st.session_state.job = None
        st.rerun()  # redraw the whole page with the finished turn

    with st.chat_message("user"):
        st.markdown(rtl(job.prompt), unsafe_allow_html=True)
    with st.chat_message("assistant"):
        view = StepView()
        for kind, data in list(job.events):
            view(kind, data)

        details = job.pending
        if details:
            st.warning(f"Supervisor approval needed: refund {details['amount_egp']} EGP "
                       f"for order {details['order_id']} ({details['customer']}, {details['item']}).")
            approve, reject = st.columns(2)
            key = f"{details['order_id']}-{len(job.events)}"
            approve.button("Approve", key=f"approve-{key}", type="primary",
                           use_container_width=True, on_click=job.decide, args=(True,))
            reject.button("Reject", key=f"reject-{key}",
                          use_container_width=True, on_click=job.decide, args=(False,))
        else:
            st.caption(f"Working ... {time.time() - job.started:.0f} s")


# --- Page state: survives every rerun of this script ---------------------------

if "messages" not in st.session_state:
    st.session_state.messages = []  # the conversation in API format: what the model sees
    st.session_state.turns = []     # what the page shows: text, steps and timings
    st.session_state.job = None     # the message being handled right now, if any

busy = st.session_state.job is not None

with st.sidebar:
    st.subheader("Support agent")
    st.write(f"Model: `{agent.MODEL}`")
    st.write(f"Max steps per message: {agent.MAX_STEPS}")
    st.write(f"Refunds above {REFUND_APPROVAL_LIMIT} EGP need your approval.")
    if st.button("New conversation and reset data", use_container_width=True, disabled=busy):
        create_database()
        st.session_state.messages = []
        st.session_state.turns = []
        st.rerun()
    st.caption("Resetting rebuilds store.db from the seed data, so cancelled orders, "
               "refunds and tickets from earlier tests are gone.")

st.title("Customer support agent")

for turn in st.session_state.turns:
    show_turn(turn)

# --- The chat loop: one Job per customer message --------------------------------

if busy:
    show_running_job()

prompt = st.chat_input("Write to the agent as the customer ...", disabled=busy)
if prompt:
    job = Job(prompt)
    st.session_state.job = job
    threading.Thread(
        target=run_job, args=(job, get_client(), st.session_state.messages), daemon=True
    ).start()
    st.rerun()

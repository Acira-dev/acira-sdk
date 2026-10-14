"""Free local-model browser-use journey with Acira-bound business operations."""
from __future__ import annotations

import asyncio
import contextlib
import os
from urllib.parse import urlsplit

import httpx
os.environ["ANONYMIZED_TELEMETRY"] = "false"
os.environ["BROWSER_USE_CLOUD_SYNC"] = "false"
os.environ["BROWSER_USE_VERSION_CHECK"] = "false"

from browser_use import ActionResult, Agent, BrowserSession, ChatOllama, Tools
from browser_use.browser.events import ClickElementEvent, NavigateToUrlEvent
from playwright.async_api import async_playwright
from acira import protect
from acira.journal import Journal
from acira_cdp_proxy.action_gateway import AciraActionBlocked


def local_endpoint(host):
    parsed = urlsplit(host)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username or parsed.password or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment):
        raise ValueError("The free demo requires a loopback Ollama URL, such as http://127.0.0.1:11434")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("Choose a valid local Ollama port")
    return host.rstrip("/")


async def check_local_model(host, model):
    host = local_endpoint(host)
    if not model or "cloud" in model.lower():
        raise ValueError("Choose a downloaded local model; cloud models are not used by this demo")
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.get(host + "/api/tags")
            response.raise_for_status()
            entry = next((item for item in response.json().get("models", [])
                          if item.get("name") == model or item.get("model") == model), None)
            if entry is None:
                raise ValueError("Download the local model first: ollama pull " + model)
            if entry.get("size", 0) < 1024 * 1024:
                raise ValueError("The selected model has no local weights")
            info = await client.post(host + "/api/show", json={"model": model})
            info.raise_for_status()
            if info.json().get("remote_host") or info.json().get("remote_model"):
                raise ValueError("Cloud-backed model references are not allowed")
    except (httpx.HTTPError, KeyError, TypeError) as error:
        raise ValueError("Start your local Ollama server, then retry with --ollama-host " + host) from error
    return host


def create_model(state, model, host):
    class LocalModel(ChatOllama):
        async def ainvoke(self, *args, **kwargs):
            state["model_calls"] = state.get("model_calls", 0) + 1
            result = await super().ainvoke(*args, **kwargs)
            state["model_responses"] = state.get("model_responses", 0) + 1
            return result
    return LocalModel(model=model, host=local_endpoint(host), timeout=120,
        client_params={"trust_env": False},
        ollama_options={"num_ctx": 8192, "num_predict": 1024, "temperature": 0})


async def find_node(session, identifier):
    summary = await asyncio.wait_for(session.get_browser_state_summary(include_screenshot=False), 30)
    node = next((item for item in summary.dom_state.selector_map.values()
                 if item.attributes.get("id") == identifier), None)
    if node is None:
        raise AciraActionBlocked("The expected local test target is unavailable")
    return node


def decision_outcome(root, run_id, guard):
    if guard.runtime_control.last_operation and guard.runtime_control.last_operation.get("state") == "outcome unknown":
        return "unknown"
    reviews = [item for item in Journal(root).reviews() if item["run_id"] == run_id]
    outcome = reviews[0]["state"] if reviews else "policy_block"
    return "invalidated" if outcome == "approved" else outcome


def create_tools(guard, session, kind, state, root):
    from . import demo
    # The ordinary SDK retains all supported browser-use actions. This fake-data
    # journey gives its AI one bounded business operation and the done action.
    defaults = list(Tools().registry.registry.actions)
    tools = Tools(exclude_actions=[name for name in defaults if name != "done"])
    attempted = False

    async def perform():
        nonlocal attempted
        if attempted:
            return ActionResult(is_done=True, success=False, error="One attempt already occurred; do not retry")
        attempted = True
        counter = {"payment": "payment_requests", "draft": "draft_requests", "delete": "delete_requests"}[kind]
        before = demo.COUNTS[counter]
        try:
            if kind == "draft":
                state.update(status="The local AI chose to write the test note. Watch Chromium…", phase="preparing")
                field = await find_node(session, "note")
                await guard.fill_fields(session, [(field, demo.DRAFT["note"])], purpose="Write the fake local test note")
            target = await find_node(session, {"payment": "pay", "draft": "save", "delete": "delete"}[kind])
            if kind == "delete":
                state.update(status="The AI requested deletion. Acira is checking its policy…", phase="preparing")
                event = session.event_bus.dispatch(ClickElementEvent(node=target))
                result = await asyncio.wait_for(event.event_result(raise_if_any=True, raise_if_none=False), 30)
                if isinstance(result, dict) and result.get("validation_error"):
                    raise AciraActionBlocked("Acira blocked the local delete")
            else:
                state.update(status="The AI requested the operation. Review it in Acira → Approvals.", phase="review")
                values = demo.VALUES if kind == "payment" else demo.DRAFT
                await guard.submit(session, target, destination=demo.BASE + ("/payment" if kind == "payment" else "/draft"),
                                   values=values, facts={name: name for name in values})
            received = await demo.wait_for_effect(counter, before)
            message = ("APPROVED AND VERIFIED: exactly one matching fake request reached the test server."
                       if received and kind != "delete" else "CURRENT POLICY ALLOWED the fake delete." if received
                       else "OUTCOME UNKNOWN: no matching request was confirmed. It will not be retried.")
            state.update(status=message, phase="result", outcome="completed" if received else "unknown")
        except AciraActionBlocked:
            outcome = decision_outcome(root, state["run"], guard)
            labels = {"rejected": "REJECTED", "expired": "REVIEW EXPIRED", "invalidated": "REVIEW INVALIDATED",
                      "policy_block": "BLOCKED BY POLICY", "unknown": "OUTCOME UNKNOWN"}
            state.update(status=labels.get(outcome, "BLOCKED") + ": no retry will be attempted. Inspect Activity.",
                         phase="result", outcome=outcome)
        return ActionResult(is_done=True, success=state["outcome"] == "completed", extracted_content=state["status"])

    if kind == "payment":
        @tools.action("Request the fake 10.00 USD payment through Acira. Human review is required. Call once.")
        async def review_payment():
            return await perform()
    elif kind == "draft":
        @tools.action("Write the fake test note and request its save through Acira. Human review is required. Call once.")
        async def save_test_draft():
            return await perform()
    else:
        @tools.action("Attempt Delete all records once through Acira. Respect a block and never retry.")
        async def try_delete():
            return await perform()
    return tools


async def workflow(project_name, kind, *, keep_active=False):
    from . import demo
    state = demo.RUNS[project_name]
    root = demo.ROOT if project_name == "checkout" else demo.RESEARCH_ROOT
    state.update(status="Opening visible Chromium for the local browser-use agent…", run=None, phase="starting",
        kind=kind, keep_active=keep_active, outcome=None, run_state="starting", model_calls=0, model_responses=0, agent_steps=0)
    try:
        async with protect(project=root) as guard:
            state.update(run=guard.runtime_control.run_id, run_state="running")
            async with async_playwright() as p:
                session = BrowserSession(args=["--password-store=basic", "--use-mock-keychain"], headless=False, executable_path=p.chromium.executable_path,
                    user_data_dir=root / ".acira/browser-use-profiles" / state["run"],
                    downloads_path=root / ".acira/browser-use-downloads",
                    enable_default_extensions=False, permissions=[], keep_alive=True, use_cloud=False,
                    allowed_domains=[demo.BASE], highlight_elements=False, dom_highlight_elements=False)
                try:
                    await asyncio.wait_for(session.start(), 30)
                    url = demo.BASE + ("/checkout" if project_name == "checkout" else "/research")
                    navigation = session.event_bus.dispatch(NavigateToUrlEvent(url=url))
                    await asyncio.wait_for(navigation.event_result(raise_if_any=True, raise_if_none=False), 30)
                    state.update(status="Chromium is ready. The local AI is inspecting the page before planning its operation.", phase="thinking")
                    tools = create_tools(guard, session, kind, state, root)
                    operation = {"payment": "review_payment", "draft": "save_test_draft", "delete": "try_delete"}[kind]
                    task = ("Inspect this fake local page and call " + operation + " exactly once for the requested workflow. "
                            "The tool handles Acira review. Never approve for the human or retry a rejected or ambiguous operation. "
                            "Do not claim completion without its observed result.")
                    agent = Agent(task=task, llm=create_model(state, demo.MODEL, demo.OLLAMA_HOST),
                        browser_session=session, tools=tools, use_vision=False, use_thinking=False,
                        flash_mode=True, use_judge=False, enable_planning=False, calculate_cost=False,
                        generate_gif=False, save_conversation_path=None,
                        file_system_path=str(root / ".acira/browser-use-work"),
                        enable_signal_handler=False, max_failures=2, max_actions_per_step=1,
                        llm_timeout=120, step_timeout=400, max_history_items=6,
                        directly_open_url=False, final_response_after_failure=False, message_compaction=False,
                        max_clickable_elements_length=8000)

                    async def step_start(current_agent):
                        if state["phase"] not in {"review", "result"}:
                            state["agent_steps"] += 1
                            state.update(status="Local AI is planning step " + str(state["agent_steps"]) + " with " + demo.MODEL +
                                ". First loading can take longer.", phase="thinking")

                    await asyncio.wait_for(agent.run(max_steps=8, on_step_start=step_start), 900)
                    if state["phase"] != "result":
                        state.update(status="The agent stopped without completing the protected operation. Nothing was confirmed submitted.",
                                     phase="result", outcome="agent_incomplete")
                    state["status"] += ("\n\nAction resolved. Run kept active for policy testing; use End active run."
                                        if keep_active else "\n\nAction resolved. The run is finishing automatically…")
                    deadline = asyncio.get_running_loop().time() + (600 if keep_active else 3)
                    while not demo.RELEASES[project_name].is_set() and asyncio.get_running_loop().time() < deadline:
                        try:
                            await asyncio.wait_for(demo.RELEASES[project_name].wait(), 1)
                        except asyncio.TimeoutError:
                            pass
                finally:
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(session.kill(), 10)
        state.update(phase="finished", run_state="ended")
        state["status"] = state["status"].split("\n\n", 1)[0] + "\n\nRUN ENDED. Ready for another action."
    except asyncio.CancelledError:
        state.update(status="RUN CANCELLED. Its pending review was invalidated; no operation was retried.",
                     phase="finished", outcome="cancelled", run_state="ended")
        raise
    except Exception as error:
        state.update(status="Local browser-use error: " + str(error), phase="error", outcome="error", run_state="ended")


GUIDE = """Acira: free local browser-use agent

1. Start payment review, a draft save, or blocked deletion in the test drive.
2. Visible Chromium opens. The local AI inspects the page and requests a bounded
   operation through Acira. The status shows thinking, review and actual outcomes.
3. In Acira Approvals, inspect the screenshot, target highlight and exact values.
4. Approve once or Reject. A rejected, expired or cancelled operation is not retried.
5. The server counter proves whether the exact fake request arrived. Both interfaces
   update automatically; normal runs end and Chromium closes after three seconds.

Only fake local data is used. The model runs through Ollama on this computer.
There is no paid model, API key, cloud browser or cloud synchronization. This demo
offers predefined browser-use business tools; it is not a general unrestricted agent.
Manual human clicks are outside SDK enforcement. Screenshots supplement the exact
request facts; permission alone does not prove business success.

Start both projects for separate policies and reviews in one dashboard. Select
Keep this run active before starting to test Saved/Active policy updates, for up
to ten minutes. Changing a policy invalidates pending approval. Activity and
Backtesting retain the SDK's decisions and verifiable operation facts.

For a new environment, install the Acira wheel with [browser-use,playwright] extras,
then python -m playwright install chromium. Install Ollama from its official site,
run ollama serve, and ollama pull qwen3:4b-instruct. Keep Ollama local; set
OLLAMA_NO_CLOUD=1 before starting its server to disable cloud features.
Run acira demo --adapter browser-use. For another local port, pass --ollama-host
http://127.0.0.1:PORT. --model selects an already-downloaded local model.
Acira does not silently download browsers or model weights.

For your own project, acira init --adapter browser_use creates local policies.
Construct your BrowserSession and Agent inside async with protect() as guard.
Pass ChatOllama explicitly to your Agent. For critical submissions, register a
business tool that calls guard.submit(session, exact_node, destination=...,
values=actual_complete_payload, facts=required_facts). The SDK reviews and binds
the actual request; a model's button label alone is insufficient.

Model quality and speed vary. A model error or incomplete plan is displayed;
the demo never substitutes scripted success or falls back to a paid provider.
Cancel stops a pending operation. Ctrl-C stops the demo and its browsers;
acira server stop separately stops the optional dashboard. Local enforcement
continues when the dashboard stops. A local model must be running to plan new
AI actions, but the policy engine does not require a model for decisions.
"""

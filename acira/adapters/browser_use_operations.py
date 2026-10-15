"""Bounded browser-use operations on adapter-owned CDP channels.

Exact element activation uses the DOM click algorithm once, without pointer or
script fallback/retries. HTTP contracts bypass page service workers and intercept
network requests before dispatch; they do not verify business success.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from uuid import uuid4
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from acira_cdp_proxy.action_gateway import AciraActionBlocked, ActionRequest
from acira_cdp_proxy.guard import RUNTIME_OBJECT_PROBE_JS, _target_from_probe_value
from acira_cdp_proxy.operation import STATE_JS, evidence_for
from ._cdp_channel import Channel, OperationConnectionLost
from ._submission_contract import RequestContract


async def channel_for(manager, browser_session, node):
    binding = manager._bindings.get(id(browser_session))
    target = getattr(node, "target_id", None) or getattr(browser_session, "agent_focus_target_id", None)
    if binding is None or not target or id(browser_session.event_bus) not in manager._patched_event_buses:
        raise ValueError("Use a live browser-use session owned by this Acira run")
    key = (id(browser_session), target)
    if key not in manager._operation_channels:
        manager._operation_channels[key] = await Channel.connect(binding.real_cdp_url, target)
        manager._operation_channels[key].owner_session = id(browser_session)
    return manager._operation_channels[key]


class Element:
    @classmethod
    async def resolve(cls, channel, node):
        self = cls()
        self.channel = channel
        backend = getattr(node, "backend_node_id", None)
        if not isinstance(backend, int) or backend <= 0:
            raise ValueError("An exact browser-use DOM node is required")
        result = await channel.send("DOM.resolveNode", {"backendNodeId": backend})
        self.object_id = result.get("object", {}).get("objectId")
        if not self.object_id:
            raise AciraActionBlocked("The operation target disappeared")
        try:
            # Prepare the exact target before snapshotting facts and review
            # geometry. Headed windows can be shorter than CI's viewport.
            await channel.send("DOM.scrollIntoViewIfNeeded", {"backendNodeId": backend})
            self.state = await self.read(STATE_JS)
            self.target = _target_from_probe_value(await self.read("el => (" + RUNTIME_OBJECT_PROBE_JS + ").call(el)"))
            attrs = getattr(node, "attributes", {})
            if not self.state or not self.target or (attrs.get("id") and attrs["id"] != self.state["target"]["id"]):
                raise AciraActionBlocked("The actual element differs from the requested target")
            return self
        except BaseException:
            await self.close()
            raise

    async def read(self, expression):
        result = await self.channel.send("Runtime.callFunctionOn", {"objectId": self.object_id,
            "functionDeclaration": "function(){return (" + expression + ")(this)}", "returnByValue": True})
        if result.get("exceptionDetails"):
            raise AciraActionBlocked("The exact operation probe failed")
        return result.get("result", {}).get("value")

    async def valid(self):
        try:
            return await self.read(STATE_JS) == self.state
        except Exception:
            return False

    async def submission_frame(self, node):
        """Find the exact owning document through positive frame comparisons."""
        async with asyncio.timeout(5):
            tree = await self.channel.send("Page.getFrameTree")
            pending, frames = [tree.get("frameTree", {})], []
            while pending:
                entry = pending.pop()
                identifier = entry.get("frame", {}).get("id")
                if not isinstance(identifier, str) or not identifier or identifier in frames or len(frames) >= 64:
                    raise AciraActionBlocked("The submission frame tree could not be verified")
                frames.append(identifier)
                pending.extend(entry.get("childFrames", []))
            verified = []
            for frame_id in frames:
                world = await self.channel.send("Page.createIsolatedWorld", {"frameId": frame_id, "worldName": "acira-submission-frame"})
                context = world.get("executionContextId")
                if not isinstance(context, int) or isinstance(context, bool) or context <= 0:
                    raise AciraActionBlocked("The submission document could not be inspected")
                resolved = await self.channel.send("DOM.resolveNode", {"backendNodeId": node.backend_node_id, "executionContextId": context})
                object_id = resolved.get("object", {}).get("objectId")
                if not object_id:
                    raise AciraActionBlocked("The submission target could not be inspected in that document")
                try:
                    observed = await self.channel.send("Runtime.callFunctionOn", {"objectId": object_id,
                        "functionDeclaration": "function(){return this.isConnected && this.ownerDocument === document}", "returnByValue": True})
                    if observed.get("exceptionDetails") or type(observed.get("result", {}).get("value")) is not bool:
                        raise AciraActionBlocked("The submission document comparison failed")
                    if observed["result"]["value"]:
                        verified.append(frame_id)
                finally:
                    with contextlib.suppress(Exception):
                        await self.channel.send("Runtime.releaseObject", {"objectId": object_id})
            if len(verified) != 1:
                raise AciraActionBlocked("The submission target has no unique verified frame")
            return verified[0]

    async def masked_artifact(self):
        from acira_cdp_proxy.screenshots import capture_masked_cdp
        async def probe(method, params, session_id):
            return await self.channel.send(method, params)
        async def target_box():
            model = await self.channel.send("DOM.getBoxModel", {"objectId": self.object_id})
            quad = model["model"]["border"]
            xs, ys = quad[::2], quad[1::2]
            return {"x": min(xs), "y": min(ys), "width": max(xs)-min(xs), "height": max(ys)-min(ys)}
        return await capture_masked_cdp(probe, target_probe=target_box)

    async def activate(self):
        # Compare private facts and dispatch in the same JS task. Page scripts
        # cannot replace values between the final check and element activation.
        script = """function(expected){
            const state=(STATE)(this);
            if(JSON.stringify(state)!==JSON.stringify(expected) || !this.isConnected || this.disabled || this.getAttribute('aria-disabled')==='true') return false;
            const rect=this.getBoundingClientRect();
            if(rect.width<=0 || rect.height<=0 || getComputedStyle(this).visibility==='hidden') return false;
            const hit=this.ownerDocument.elementFromPoint(rect.x+rect.width/2,rect.y+rect.height/2);
            if(!hit || (hit!==this && !this.contains(hit))) return false;
            this.click(); return true;
        }""".replace("STATE", STATE_JS)
        result = await self.channel.send("Runtime.callFunctionOn", {"objectId": self.object_id,
            "functionDeclaration": script, "arguments": [{"value": self.state}], "returnByValue": True})
        if result.get("exceptionDetails") or result.get("result", {}).get("value") is not True:
            raise AciraActionBlocked("Activation could not be confirmed; inspect the operation before retrying")

    async def close(self):
        try:
            await self.channel.send("Runtime.releaseObject", {"objectId": self.object_id})
        except Exception:
            pass


class NetworkGate:
    def __init__(self, manager, channel):
        self.manager, self.channel = manager, channel
        self.active = None
        self.locks = set()
        self.responses = {}
        self.redirect_sources = {}
        channel.handlers["Fetch.requestPaused"] = self.paused
        channel.handlers["Network.responseReceived"] = self.response
        channel.handlers["Network.loadingFailed"] = self.failed
        channel.global_handlers["Target.attachedToTarget"] = self.attached
        channel.global_handlers["Target.detachedFromTarget"] = self.detached
        channel.handlers["Target.attachedToTarget"] = self.attached
        channel.handlers["Target.detachedFromTarget"] = self.detached
        self.children = set()
        self.targets = {}
        channel.on_disconnect = self.disconnected

    def disconnected(self):
        if getattr(self.channel, "owner_session", None) in getattr(self.manager, "_stopping_sessions", ()):
            # Deliberate stop already cancelled this browser's operations.
            # Another browser's held review in the run must remain valid.
            return
        reason = "Browser operation connection ended unexpectedly; outcome may be unknown. Close the owned browser and start a fresh Acira run."
        self.manager._operation_transport_failed = True
        if self.active and not self.active.outcome.done():
            self.active.outcome.set_result({"blocked": True})
        runtime = self.manager.action_gateway.checkpoint_store
        fail = getattr(runtime, "fail_integration", None)
        if fail is not None:
            fail(reason)
        else:
            async def unavailable():
                raise AciraActionBlocked(reason)
            self.manager.guard_policy.before_decision = unavailable
            getattr(runtime, "event", lambda *a, **kw: None)("integration_failed", reason=reason, business_outcome="unknown")

    async def start(self):
        await self.channel.send("Network.enable")
        await self.channel.send("Network.setBypassServiceWorker", {"bypass": True})
        await self.channel.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})
        # Pause new page/iframe/worker targets until their interception is ready.
        # They cannot escape a reviewed page operation through a popup or worker.
        await self.channel.send("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True,
            "flatten": True, "filter": [{"type": "page"}, {"type": "iframe"}, {"type": "worker"}, {"type": "service_worker"}, {"exclude": True}]}, session=False)
        await self.channel.send("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True,
            "filter": [{"type": "page"}, {"type": "iframe"}, {"type": "worker"}, {"type": "service_worker"}, {"exclude": True}]})
        if self.channel.tasks:
            await asyncio.wait_for(asyncio.gather(*list(self.channel.tasks)), 5)
        if self.channel.error:
            raise AciraActionBlocked("Related targets could not be secured; no submission action was sent")

    async def attached(self, params):
        session = params["sessionId"]
        info = params.get("targetInfo", {})
        if info.get("targetId") == self.channel.target_id:
            await self.channel.send("Runtime.runIfWaitingForDebugger", session_id=session)
            return
        if len(self.children) >= 64:
            raise AciraActionBlocked("Submission target budget exhausted; the new target remains paused")
        self.children.add(session)
        self.targets[session] = info.get("targetId")
        self.channel.scoped_handlers[session] = {
            "Fetch.requestPaused": lambda event: self.paused({**event, "child_session": session}),
            "Network.responseReceived": lambda event: self.response({**event, "child_session": session}),
            "Network.loadingFailed": lambda event: self.failed({**event, "child_session": session}),
            "Target.attachedToTarget": self.attached, "Target.detachedFromTarget": self.detached}
        await self.channel.send("Network.enable", session_id=session)
        await self.channel.send("Network.setBypassServiceWorker", {"bypass": True}, session_id=session)
        await self.channel.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]}, session_id=session)
        await self.channel.send("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True,
            "filter": [{"type": "page"}, {"type": "iframe"}, {"type": "worker"}, {"type": "service_worker"}, {"exclude": True}]}, session_id=session)
        await self.channel.send("Runtime.runIfWaitingForDebugger", session_id=session)

    async def detached(self, params):
        session = params.get("sessionId")
        self.children.discard(session)
        self.targets.pop(session, None)
        self.channel.scoped_handlers.pop(session, None)

    async def send(self, method, params, event):
        child = event.get("child_session")
        if child:
            return await self.channel.send(method, params, session_id=child)
        return await self.channel.send(method, params)

    async def paused(self, params):
        try:
            await self._paused(params)
        except BaseException:
            # A failed audit write or policy refresh must not leave a request
            # pending to be released when this CDP session later detaches.
            try:
                await self.send("Fetch.failRequest", {"requestId": params["requestId"], "errorReason": "BlockedByClient"}, params)
            finally:
                if self.active and not self.active.outcome.done():
                    self.active.outcome.set_result({"blocked": True})

    async def _paused(self, params):
        if getattr(self.channel, "closing", False):
            await self.send("Fetch.failRequest", {"requestId": params["requestId"], "errorReason": "BlockedByClient"}, params)
            if self.active and not self.active.outcome.done():
                self.active.outcome.set_result({"blocked": True})
            return
        request = params.get("request", {})
        contract = self.active
        url = request.get("url", "")
        child = params.get("child_session")
        # Keep the original request identity after timeout/completion. A late
        # redirect must not become an ordinary request when active is cleared.
        source = self.redirect_sources.get((child, params.get("redirectedRequestId")))
        if source is not None:
            await self.send("Fetch.failRequest", {"requestId": params["requestId"], "errorReason": "BlockedByClient"}, params)
            event = getattr(self.manager.action_gateway.checkpoint_store, "event", lambda *a, **kw: None)
            event("submission_redirect_blocked", business_outcome="unknown")
            if contract and source is contract.redirect_token and not contract.outcome.done():
                contract.outcome.set_result({"blocked": True})
            return
        mutation = request.get("method") not in {"GET", "HEAD", "OPTIONS"}
        relevant = url in self.locks or bool(child and mutation) or bool(contract and (mutation or params.get("resourceType") in {"XHR", "Fetch", "Document"}))
        if not relevant:
            await self.send("Fetch.continueRequest", {"requestId": params["requestId"]}, params)
            return
        normalized = SimpleNamespace(url=url, method=request.get("method"),
            headers={key.lower(): value for key, value in request.get("headers", {}).items()}, post_data=request.get("postData"))
        target = self.targets.get(child) if child else self.channel.target_id
        if (contract is None or target != contract.page.target_id or params.get("frameId") != getattr(contract, "frame_id", None)
                or not params.get("frameId") or contract.used or contract.finished or not contract.matches(normalized)):
            await self.send("Fetch.failRequest", {"requestId": params["requestId"], "errorReason": "BlockedByClient"}, params)
            if contract and (mutation or url == contract.destination) and not contract.outcome.done():
                contract.outcome.set_result({"blocked": True})
            elif contract and not mutation:
                event = getattr(self.manager.action_gateway.checkpoint_store, "event", lambda *a, **kw: None)
                event("submission_background_read_blocked", method=request.get("method"))
            return
        if self.manager.guard_policy.before_decision is not None:
            await self.manager.guard_policy.before_decision()
        if (getattr(self.channel, "closing", False) or contract.used or contract.finished or self.active is not contract
                or self.manager.guard_policy.policy_revision != contract.authorized_revision):
            await self.send("Fetch.failRequest", {"requestId": params["requestId"], "errorReason": "BlockedByClient"}, params)
            if not contract.outcome.done():
                contract.outcome.set_result({"blocked": True})
            return
        contract.used = True
        self.redirect_sources[(child, params["requestId"])] = contract.redirect_token
        network_id = params.get("networkId")
        if network_id:
            self.responses[(child, network_id)] = contract
        event = getattr(self.manager.action_gateway.checkpoint_store, "event", lambda *a, **kw: None)
        event("submission_dispatch_attempt", destination=contract.destination, method=contract.method)
        await self.send("Fetch.continueRequest", {"requestId": params["requestId"]}, params)

    async def response(self, params):
        contract = self.responses.pop((params.get("child_session"), params.get("requestId")), None)
        if contract and not contract.outcome.done():
            # Redirects need a new contract rather than extending a reviewed destination.
            status = params.get("response", {}).get("status")
            contract.outcome.set_result({"status": status, "blocked": not isinstance(status, (int,float)) or 300 <= status < 400})

    async def failed(self, params):
        contract = self.responses.pop((params.get("child_session"), params.get("requestId")), None)
        if contract and not contract.outcome.done():
            contract.outcome.set_result({"blocked": True})

    async def execute(self, contract, callback):
        if getattr(self.manager, "_operation_transport_failed", False):
            raise AciraActionBlocked("Browser operation transport failed; a fresh Acira run is required")
        if self.active is not None:
            raise AciraActionBlocked("Only one submission operation may run in this browser session at a time")
        if len(self.locks) >= 1000 and contract.destination not in self.locks:
            raise AciraActionBlocked("Submission endpoint budget reached; start a fresh run")
        if len(self.redirect_sources) >= 1000:
            raise AciraActionBlocked("Submission request budget reached; start a fresh run")
        self.locks.add(contract.destination)
        self.active = contract
        contract.redirect_token = object()
        contract.authorized_revision = self.manager.guard_policy.policy_revision
        try:
            await self.start()
            result = await callback()
            outcome = await asyncio.wait_for(asyncio.shield(contract.outcome), contract.timeout)
            if outcome.get("blocked"):
                raise AciraActionBlocked("Submission mismatch or unknown outcome; no automatic retry is permitted")
            event = getattr(self.manager.action_gateway.checkpoint_store, "event", lambda *a, **kw: None)
            event("submission_response_observed", status=outcome["status"], business_outcome="unverified")
            return result
        except asyncio.TimeoutError as exc:
            raise AciraActionBlocked("Submission outcome unknown; its endpoint remains blocked against automatic retry") from exc
        except OperationConnectionLost:
            # The activation command can lose its response before the network
            # outcome waiter resumes. Preserve the same no-retry public error.
            raise AciraActionBlocked(
                "Submission outcome unknown after browser connection loss; no automatic retry is permitted. Close the owned browser and start a fresh Acira run.",
                metadata={"business_outcome": "unknown", "retry_allowed": False},
            ) from None
        finally:
            contract.finished = True
            self.active = None
            self.responses = {key: value for key, value in self.responses.items() if value is not contract}


async def submit(manager, browser_session, node, *, destination: str, values: dict[str, Any], encoding="json", method="POST", facts=None, timeout=5):
    channel = await channel_for(manager, browser_session, node)
    contract = RequestContract(manager, channel, destination, values, encoding, method.upper(), facts or {}, timeout)
    element = await Element.resolve(channel, node)
    from .playwright_boundary import CAPTURE
    token = CAPTURE.set(element.masked_artifact)
    try:
        contract.frame_id = await element.submission_frame(node)
        contract.evidence["initiator"] = {"target": channel.target_id, "frame": contract.frame_id}
        await manager._ensure_browser_use_general_page_preflight(browser_session, trigger="before_action")
        await manager._ensure_browser_use_page_mapping_preflight(browser_session)
        request = await manager._browser_use_action_request_for_exact_element(browser_session, element, action="submit")
        request = replace(request, revalidate=element.valid, operation_evidence={**evidence_for(element.state, "submit"), **contract.evidence}, require_review=True)
        if channel not in manager._network_gates:
            # One interception owner for the browser. Separate per-page owners
            # would each block the other's independently reviewed requests.
            manager._network_gates[channel] = next((gate for gate in manager._network_gates.values()
                if gate.channel.browser_url == channel.browser_url), None) or NetworkGate(manager, channel)
        await manager.action_gateway.guard_execute(request, lambda: manager._network_gates[channel].execute(contract, element.activate))
    finally:
        CAPTURE.reset(token)
        await element.close()


async def activate(manager, browser_session, node):
    channel = await channel_for(manager, browser_session, node)
    element = await Element.resolve(channel, node)
    from .playwright_boundary import CAPTURE
    token = CAPTURE.set(element.masked_artifact)
    try:
        if element.state["target"].get("tag") in {"SELECT"} or element.state["target"].get("type") == "file":
            raise AciraActionBlocked("Use the guarded selection or file upload operation for this control")
        request = await manager._browser_use_action_request_for_exact_element(browser_session, element, action="click")
        request = replace(request, revalidate=element.valid, operation_evidence={**evidence_for(element.state, "click"),
            "activation": "One exact DOM element activation; no pointer or fallback retry"})
        async def execute():
            await element.activate()
            return {"acira": {"decision": "allow", "activation": "exact_dom"}}
        return await manager.action_gateway.guard_execute(request, execute,
            blocked_result=manager._browser_use_blocked_result_factory("ClickElementEvent"))
    finally:
        if token is not None:
            CAPTURE.reset(token)
        await element.close()


async def fill_fields(manager, browser_session, fields, *, purpose="Fill form fields"):
    """One explicitly reviewed form group; individual steps remain exact and bounded."""
    from acira_cdp_proxy.facts import build_browser_event_facts
    from acira_cdp_proxy.guard import _event_facts_from_classification
    from acira_cdp_proxy.policy import AGENT_TO_BROWSER, CDPMessageContext
    if not fields or len(fields) > 30 or any(not isinstance(text, str) or len(text.encode("utf-8")) > 65536 for _, text in fields):
        raise ValueError("Supply one to thirty (browser-use DOM node, text) pairs")
    prepared = []
    try:
        for node, text in fields:
            channel = await channel_for(manager, browser_session, node)
            element = await Element.resolve(channel, node)
            prepared.append((element, text))
            state = element.state
            if (not state["has_form"] or state["target"]["tag"] not in {"INPUT", "TEXTAREA"}
                    or state["target"].get("type") in {"file", "hidden", "checkbox", "radio", "submit", "button"}):
                raise ValueError("Bounded filling requires text controls in one HTML form")
            if len(prepared) > 1:
                first = prepared[0][0]
                if channel is not first.channel:
                    raise ValueError("All controls must belong to the same page and frame")
                comparison = await channel.send("Runtime.callFunctionOn", {"objectId": element.object_id,
                    "functionDeclaration": "function(first){return this.form===first.form}",
                    "arguments": [{"objectId": first.object_id}], "returnByValue": True})
                if comparison.get("result", {}).get("value") is not True:
                    raise ValueError("All controls must belong to the same HTML form")
            identifier = state["target"].get("name") or state["target"].get("id")
            if not identifier or sum(field["name"] == identifier for field in state["fields"]) != 1:
                raise ValueError("Each control needs a unique form name or id")
        await manager._ensure_browser_use_general_page_preflight(browser_session, trigger="before_action")
        await manager._ensure_browser_use_page_mapping_preflight(browser_session)
        policy = manager.guard_policy
        if any(skill.matches_url(prepared[0][0].state["url"]) for skill in policy.config.specific_skills):
            raise AciraActionBlocked("Use individual fills with captured application-specific policies")
        if policy.before_decision is not None:
            await policy.before_decision()
        revision = policy.policy_revision
        operation = uuid4().hex
        deadline = time.monotonic() + 300
        evidence = evidence_for(prepared[0][0].state, "form_input")
        evidence.pop("grant_scope", None)
        evidence["critical"] = True
        evidence["bounded_steps"] = []
        decisions = []
        event = getattr(manager.action_gateway.checkpoint_store, "event", lambda *a, **kw: None)
        for element, text in prepared:
            sensitive = any(f["sensitive"] and f["name"] in {element.state["target"].get("name"), element.state["target"].get("id")} for f in element.state["fields"])
            evidence["bounded_steps"].append({"field": element.target.name or element.target.id, "text": "[REDACTED]" if sensitive else text})
            request = await manager._browser_use_action_request_for_exact_element(browser_session, element, action="form_input")
            request = replace(request, details={"value": text}, operation_evidence=evidence)
            classification = manager.action_gateway._classification_from_request(request)
            decision, reason, rule, cedar = policy._configured_decision(classification)
            decisions.append((classification, decision, request))
            event("action_evaluated", operation_id=operation, policy_revision=revision,
                evaluation_contract="acira.action_facts.v1", classification=classification.to_metadata(),
                facts=build_browser_event_facts(classification, None).to_dict(), specific_facts=_event_facts_from_classification(classification),
                decision=decision, metadata={"source": cedar.source if cedar else "configured_guard", "reason": reason})
        if any(decision == "block" for _, decision, _ in decisions):
            raise AciraActionBlocked("A policy blocks a declared field; no field was filled")
        if any(decision == "ask" for _, decision, _ in decisions):
            classification, _, request = decisions[0]
            from .playwright_boundary import CAPTURE
            token = CAPTURE.set(prepared[0][0].masked_artifact)
            try:
                approved = await policy._ask("{}", CDPMessageContext(AGENT_TO_BROWSER, "browser-use-bounded", 0,
                    "/acira/action/browser_use", send_probe=request.send_probe), classification, purpose + ": these fields only; submission is excluded")
            finally:
                CAPTURE.reset(token)
            if not approved:
                raise AciraActionBlocked("Bounded form fill rejected; no field was filled")
        event("bounded_operation_authorized", operation_id=operation, evidence=evidence)
        for index, (element, text) in enumerate(prepared):
            if policy.before_decision is not None:
                await policy.before_decision()
            if time.monotonic() >= deadline or revision != policy.policy_revision or not all([await item.valid() for item, _ in prepared]):
                raise AciraActionBlocked("Form or policy changed; no further fields were filled")
            script = """function(expected,text){
                if(JSON.stringify((STATE)(this))!==JSON.stringify(expected) || this.disabled || this.readOnly) return false;
                const setter=Object.getOwnPropertyDescriptor(this.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype,'value').set;
                this.focus(); setter.call(this,text);
                this.dispatchEvent(new Event('input',{bubbles:true}));
                this.dispatchEvent(new Event('change',{bubbles:true})); return true;
            }""".replace("STATE", STATE_JS)
            event("action_dispatched", operation_id=operation, step=index)
            result = await element.channel.send("Runtime.callFunctionOn", {"objectId": element.object_id,
                "functionDeclaration": script, "arguments": [{"value": element.state}, {"value": text}], "returnByValue": True})
            if result.get("exceptionDetails") or result.get("result", {}).get("value") is not True:
                raise AciraActionBlocked("A field's outcome is unknown; inspect before retrying")
            identifier = element.state["target"].get("name") or element.state["target"].get("id")
            for item, _ in prepared:
                for field in item.state["fields"]:
                    if field["name"] == identifier:
                        field["value"] = text
            if not all([await item.valid() for item, _ in prepared]):
                raise AciraActionBlocked("Browser effects exceeded the declared form steps")
            event("browser_call_completed", operation_id=operation, step=index, business_outcome="unverified")
    except BaseException:
        if prepared:
            getattr(manager.action_gateway.checkpoint_store, "event", lambda *a, **kw: None)("bounded_operation_stopped", reason="Inspect already dispatched steps; no automatic retry")
        raise
    finally:
        for element, _ in prepared:
            await element.close()


class TransportPolicy:
    """Keep framework auto-resume from racing adapter request interception."""
    def __init__(self, manager):
        self.manager = manager

    def __getattr__(self, name):
        return getattr(self.manager.policy, name)

    async def decide(self, payload, context):
        from acira_cdp_proxy.policy import AGENT_TO_BROWSER, CDPPolicyDecision
        if context.direction == AGENT_TO_BROWSER and context.method == "Runtime.runIfWaitingForDebugger" and self.manager._network_gates:
            return CDPPolicyDecision(allow=False, reason="Acira owns target resume while submission interception is installed",
                metadata={"guard":"acira", "decision":"deferred_target_resume", "classification":{"method":context.method,"read_only":True}})
        return await self.manager.policy.decide(payload, context)

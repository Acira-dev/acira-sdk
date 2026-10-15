"""Private DOM evidence binding for adapters that expose a trusted CDP probe.

The live values stay in the closure. The public evidence is an explicit masked
projection. This verifies observed DOM state, not arbitrary application payloads.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .facts import TOKEN_RE
from .redaction import _api_key_is_sensitive

STATE_JS = """el => {
  if (!el || !el.isConnected) return null;
  const form = el.form || el.closest('form');
  const controls = form ? Array.from(form.elements).filter(x => /^(INPUT|SELECT|TEXTAREA)$/.test(x.tagName)) : [];
  const hrefAttribute = el.getAttribute('href');
  const identityHref = hrefAttribute ? new URL(hrefAttribute, location.href).href : '';
  return {url: location.href, has_form: !!form, form_id: form ? (form.id || form.name || form.action) : null,
    destination: form ? form.action : (el.href || location.href), method: form ? form.method : null,
    target: {tag:el.tagName,id:el.id,name:el.name,type:el.type,text:(el.innerText||el.textContent||(el.tagName === 'INPUT' && ['button','submit','reset'].includes(el.type) ? el.value : '')).slice(0,500),
      aria_label:(el.getAttribute('aria-label')||'').slice(0,500), title:(el.getAttribute('title')||'').slice(0,500),
      href:identityHref.slice(0,500), placeholder:(el.getAttribute('placeholder')||'').slice(0,500),
      autocomplete:(el.getAttribute('autocomplete')||'').slice(0,500)},
    fields: controls.map(x => ({name:x.name||x.id||x.getAttribute('aria-label')||x.type,
      type:x.type, value:x.value, checked:typeof x.checked === 'boolean' ? x.checked : null, disabled:x.disabled,
      sensitive:/hidden|password|card|cvv|cvc|secret|token|credential|cc-|otp/i.test([x.type,x.name,x.id,x.autocomplete,x.getAttribute('aria-label')||'',x.getAttribute('title')||'',x.getAttribute('placeholder')||''].join(' '))
        || /(?:^|[^a-z0-9])api[_\\s-]*key(?:$|[^a-z0-9])/i.test([x.type,x.name,x.id,x.autocomplete,x.getAttribute('aria-label')||'',x.getAttribute('title')||'',x.getAttribute('placeholder')||''].join(' '))}))};
}"""


def field_is_sensitive(field: dict[str, Any]) -> bool:
    # Older/custom normalized observations can lack a true sensitive flag.
    # Known API-key field names still remain private in approval evidence.
    name = field.get("name")
    return bool(field.get("sensitive")) or (isinstance(name, str) and _api_key_is_sensitive(name))


def is_critical_operation(state: dict[str, Any], action: str) -> bool:
    """Keep outgoing operations outside routine reusable grants.

    Labels do not establish what a request contains. They only conservatively
    restrict approval scope; request-bound policies require the exact contract.
    """
    identity = json.dumps(state.get("target", {})).lower()
    sensitive_target = re.search(r"pay|purchase|checkout|card|credential|password|delete|remove|destroy|transfer|secret|token", identity)
    # Match the same alphanumeric identity tokens as policy facts. A separator
    # such as '_' must not turn a recognized Send/Post control into a routine
    # reusable grant. Labels restrict review scope; they are not payload proof.
    outbound_target = set(TOKEN_RE.findall(identity)) & {"submit", "send", "post", "publish", "apply", "finalize"}
    return bool(action in {"submit", "file_upload", "dialog_confirmation"}
                or sensitive_target or outbound_target
                or any(field_is_sensitive(field) for field in state["fields"])
                or (state["has_form"] and state["target"].get("type") == "submit"))


def evidence_for(state: dict[str, Any], action: str) -> dict[str, Any]:
    critical = is_critical_operation(state, action)
    result = {"action": action, "destination": state["destination"], "critical": critical,
        "fields": [{"name": f["name"], "display": "[REDACTED]" if field_is_sensitive(f) else f["value"], "checked": f["checked"]} for f in state["fields"]],
        "context_scope": {"url": state["url"], "form": state.get("form_id")},
        "binding": "DOM values and resolved target rechecked before dispatch; application request values require an explicit submission contract"}
    if not critical:
        result["grant_scope"] = {"url": state["url"], "target": state["target"], "action": action, "destination": state["destination"]}
    return result


@dataclass
class DOMBinding:
    evidence: dict[str, Any]
    revalidate: Callable[[], Awaitable[bool]]
    release: Callable[[], Awaitable[None]]


async def bind_cdp(probe: Any, target: Any, action: str, *, node: dict[str, Any] | None = None, session_id: str | None = None) -> DOMBinding:
    if probe is None or target is None:
        raise ValueError("A live target and trusted browser probe are required")
    async def call(method: str, params: dict[str, Any]) -> dict[str, Any]:
        response = await asyncio.wait_for(probe(method, params, session_id), timeout=2)
        if not isinstance(response, dict) or response.get("error") or response.get("exceptionDetails"):
            raise ValueError("Could not verify operation evidence")
        if "id" in response and isinstance(response.get("result"), dict):
            response = response["result"]
        return response
    if node:
        response = await call("DOM.resolveNode", node)
        object_id = response.get("object", {}).get("objectId")
    else:
        # An exact DOM path or unique id is required; never choose a similar label.
        selector = "[id=" + json.dumps(target.id) + "]" if target.id else target.dom_path
        if not selector:
            raise ValueError("No exact target identity was supplied")
        response = await call("Runtime.evaluate", {"expression": "(() => {const els=document.querySelectorAll(" + json.dumps(selector) + "); return els.length===1 ? els[0] : null})()", "returnByValue": False})
        object_id = response.get("result", {}).get("objectId")
    if not object_id:
        raise ValueError("Target is missing or ambiguous")
    async def release() -> None:
        try:
            await call("Runtime.releaseObject", {"objectId": object_id})
        except Exception:
            pass
    async def read() -> Any:
        response = await call("Runtime.callFunctionOn", {"objectId": object_id,
            "functionDeclaration": "function(){return (" + STATE_JS + ")(this)}", "returnByValue": True})
        return response.get("result", {}).get("value")
    try:
        state = await read()
        if not state or (target.url and target.url != state["url"]) or (target.id and target.id != state["target"]["id"]):
            raise ValueError("The resolved target does not match the requested page and element")
        fingerprint = json.dumps(state, sort_keys=True)
    except BaseException:
        await release()
        raise
    async def revalidate() -> bool:
        try:
            return json.dumps(await read(), sort_keys=True) == fingerprint
        except Exception:
            return False
    return DOMBinding(evidence_for(state, action), revalidate, release)

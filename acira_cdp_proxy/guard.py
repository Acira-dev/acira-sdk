from __future__ import annotations

from urllib.parse import urlparse

import asyncio
import hashlib
import inspect
import json
import re
import sys
import threading
import time
from uuid import uuid4
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, fields as dataclass_fields, replace
from typing import Any, Protocol

from .config import AciraGuardConfig, AciraGuardRule
from .bounded_regex import compile_pattern, remaining, MATCH_BUDGET
from .decision_engine import CedarDecision, CedarDecisionEngine
from .facts import build_browser_event_facts, build_page_preflight_facts
from .framework_adapters import FrameworkAdapterRegistry, FrameworkAdapterSpec, RuntimeHelperContext
from .pause import ReviewPauseState, PauseCoordinator
from .policy import AGENT_TO_BROWSER, BROWSER_TO_AGENT, CDPMessageContext, CDPPolicyDecision
from .redaction import SECRET_REDACTION_REGISTRY, _key_is_sensitive, contains_known_secret, redact_payload, redact_value
from .skills import (
    SpecificSkillTarget,
    expected_actions_for_fingerprint,
    mapping_candidate_eligibility,
    mapping_evidence_gate,
    resolve_target_fingerprint,
    score_target_fingerprint,
)
from .target_mapping import (
    InMemoryTargetMappingStore,
    JsonlTargetMappingStore,
    TargetMappingRecord,
    TargetMappingStore,
    fingerprint_hash,
    target_mapping_key,
    approved_mapping_matches,
    unchanged_capture_identity,
    url_scope_key,
)


ReviewResult = bool | str
ReviewHandler = Callable[["GuardReviewRequest"], ReviewResult | Awaitable[ReviewResult]]

ACIRA_SECURITY_LOCKDOWN_REASON = (
    "ACIRA_SECURITY_LOCKDOWN_DO_NOT_RETRY: All actions from this agent have been blocked by Acira Guard "
    "because the page mapping was rejected for security reasons. Stop sending browser commands; do not retry "
    "or attempt bypasses. Do not brute-force clicks."
)


READ_ONLY_METHODS = {
    "Accessibility.getFullAXTree",
    "Accessibility.getPartialAXTree",
    "Browser.getVersion",
    "Browser.getWindowBounds",
    "Browser.getWindowForTarget",
    "CSS.getComputedStyleForNode",
    "CSS.getMatchedStylesForNode",
    "CSS.getPlatformFontsForNode",
    "DOM.describeNode",
    "DOM.getAttributes",
    "DOM.getBoxModel",
    "DOM.getContentQuads",
    "DOM.getDocument",
    "DOM.getFlattenedDocument",
    "DOM.getNodeForLocation",
    "DOM.performSearch",
    "DOM.querySelector",
    "DOM.querySelectorAll",
    "DOM.resolveNode",
    "DOMDebugger.getEventListeners",
    "Page.captureScreenshot",
    "Page.getFrameTree",
    "Page.getLayoutMetrics",
    "Performance.getMetrics",
    "Runtime.getProperties",
    "Runtime.globalLexicalScopeNames",
    "Schema.getDomains",
    "Storage.getCookies",
    "Target.getBrowserContexts",
    "Target.getTargets",
}

READ_ONLY_PREFIXES = (
    "CSS.get",
    "Database.get",
    "DOM.get",
    "DOM.query",
    "DOMSnapshot.",
    "Fetch.get",
    "IndexedDB.request",
    "Log.",
    "Network.get",
    "Overlay.get",
    "Performance.",
    "Security.get",
    "Storage.get",
    "Target.get",
    "Tracing.get",
)

SAFE_SETUP_METHODS = {
    "Accessibility.disable",
    "Accessibility.enable",
    "CSS.disable",
    "CSS.enable",
    "DOM.disable",
    "DOM.enable",
    "Log.disable",
    "Log.enable",
    "Network.disable",
    "Network.enable",
    "Page.disable",
    "Page.enable",
    "Page.setLifecycleEventsEnabled",
    "Runtime.disable",
    "Runtime.enable",
    "Runtime.releaseObject",
    "Runtime.releaseObjectGroup",
    "Runtime.runIfWaitingForDebugger",
    "Emulation.setDeviceMetricsOverride",
    "Emulation.clearDeviceMetricsOverride",
    "Browser.setDownloadBehavior",
    "Target.attachToTarget",
    "Target.activateTarget",
    "Target.detachFromTarget",
    "Target.setAutoAttach",
    "Target.setDiscoverTargets",
}

DOM_MUTATION_METHODS = {
    "DOM.removeAttribute",
    "DOM.setAttributeValue",
    "DOM.setAttributesAsText",
    "DOM.setFileInputFiles",
    "DOM.setNodeValue",
    "DOMStorage.clear",
    "DOMStorage.removeDOMStorageItem",
    "DOMStorage.setDOMStorageItem",
}

_POINT_PROBE_JS_TEMPLATE = r"""
(x, y) => {
  const describe = (node, source) => {
    if (!node) return null;
    const closest = node.closest
      ? node.closest('button,input,label,select,textarea,a,[role="button"],[role="checkbox"],[role="radio"],[role="switch"],[role="combobox"],[role="listbox"],[role="option"],[contenteditable="true"],[onclick],[type="submit"]')
      : node;
    let target = closest || node;
    if (target && target.tagName && target.tagName.toLowerCase() === "label") {
      const forId = target.getAttribute ? target.getAttribute("for") : "";
      const control = target.control || (forId ? document.getElementById(forId) : null) || target.querySelector?.('input,select,textarea,[role="checkbox"],[role="radio"],[role="switch"],[role="combobox"]');
      if (control) target = control;
    }
    const attr = (name) => target.getAttribute ? target.getAttribute(name) : "";
    const tag = target.tagName ? target.tagName.toLowerCase() : "";
    const type = (attr("type") || "").toLowerCase();
    const role = (attr("role") || "").toLowerCase();
    const label = (
      attr("aria-label") ||
      attr("title") ||
      target.innerText ||
      (["button", "submit", "reset"].includes(type) ? target.value : "") ||
      attr("placeholder") ||
      attr("name") ||
      ""
    ).toString().trim().slice(0, 120);
    const textOf = (candidate) => candidate ? (candidate.innerText || candidate.textContent || "").toString().trim().replace(/\s+/g, " ").slice(0, 180) : "";
    const labelTextOf = (candidate) => {
      if (!candidate) return "";
      if (!candidate.cloneNode) return textOf(candidate);
      const clone = candidate.cloneNode(true);
      clone.querySelectorAll?.("button,input,select,textarea,option,[role='button'],[role='checkbox'],[role='radio'],[role='switch'],[role='combobox'],[role='listbox'],[role='option'],[contenteditable='true']").forEach((child) => child.remove());
      return textOf(clone) || textOf(candidate);
    };
    const classes = target.classList ? Array.from(target.classList).slice(0, 16) : [];
    const form = target.closest ? target.closest("form") : null;
    const forLabel = target.id && document.querySelectorAll
      ? Array.from(document.querySelectorAll("label")).find((candidate) => candidate.htmlFor === target.id)
      : null;
    const wrappingLabel = target.closest ? target.closest("label") : null;
    const heading = target.closest ? target.closest("section,main,article,form,div")?.querySelector?.("h1,h2,h3,h4,legend,[role='heading']") : null;
    const structuralRoot = form || (target.closest ? target.closest("section,fieldset,main,article") : null) || document.body;
    const interactiveSelector = 'button,input,select,textarea,a[href],[role="button"],[role="checkbox"],[role="radio"],[role="switch"],[role="combobox"],[role="listbox"],[role="option"],[role="textbox"],[role="link"],[contenteditable="true"],[onclick],[type="submit"]';
    const normalizeControl = (candidate) => {
      if (!candidate) return null;
      if (candidate.tagName && candidate.tagName.toLowerCase() === "label") {
        const forId = candidate.getAttribute ? candidate.getAttribute("for") : "";
        return candidate.control || (forId ? document.getElementById(forId) : null) || candidate.querySelector?.('input,select,textarea,[role="checkbox"],[role="radio"],[role="switch"],[role="combobox"]') || null;
      }
      return candidate;
    };
    const controls = Array.from(structuralRoot?.querySelectorAll?.(interactiveSelector) || []).map(normalizeControl).filter(Boolean).filter((candidate, index, items) => items.indexOf(candidate) === index);
    const kindOf = (candidate) => {
      const candidateTag = candidate?.tagName ? candidate.tagName.toLowerCase() : "";
      const candidateType = (candidate?.getAttribute?.("type") || "").toLowerCase();
      const candidateRole = (candidate?.getAttribute?.("role") || "").toLowerCase();
      if (candidateTag === "input" && candidateType === "radio" || candidateRole === "radio") return "radio";
      if (candidateTag === "input" && candidateType === "checkbox" || ["checkbox", "switch"].includes(candidateRole)) return "checkbox";
      if (candidateTag === "select" || ["combobox", "listbox", "option"].includes(candidateRole)) return "select";
      if (candidateTag === "textarea" || ["textbox", "searchbox"].includes(candidateRole) || candidate?.isContentEditable) return "text_entry";
      if (candidateTag === "input" && !["button", "submit", "reset", "checkbox", "radio", "file", "range", "hidden", "image"].includes(candidateType)) return "text_entry";
      if (candidateTag === "button" || candidateRole === "button" || ["button", "submit", "reset"].includes(candidateType)) return "button";
      return candidateTag || candidateRole || "";
    };
    const controlIndex = controls.indexOf(target);
    const sameKindControls = controls.filter((candidate) => kindOf(candidate) === kindOf(target));
    const sameKindIndex = sameKindControls.indexOf(target);
    const controlText = (candidate) => {
      if (!candidate) return "";
      const candidateLabel = candidate.id && document.querySelectorAll ? Array.from(document.querySelectorAll("label")).find((item) => item.htmlFor === candidate.id) : null;
      const candidateType = (candidate.getAttribute?.("type") || "").toLowerCase();
      return (candidate.getAttribute?.("aria-label") || candidate.getAttribute?.("title") || labelTextOf(candidate.closest?.("label") || candidateLabel) || candidate.innerText || (["button", "submit", "reset"].includes(candidateType) ? candidate.value : "") || candidate.getAttribute?.("placeholder") || candidate.getAttribute?.("name") || "").toString().trim().replace(/\s+/g, " ").slice(0, 160);
    };
    const domPathOf = (candidate) => {
      const parts = [];
      let current = candidate;
      while (current && current.nodeType === Node.ELEMENT_NODE && parts.length < 8) {
        const currentTag = current.tagName.toLowerCase();
        const siblings = current.parentElement ? Array.from(current.parentElement.children).filter((item) => item.tagName === current.tagName) : [];
        const index = Math.max(1, siblings.indexOf(current) + 1);
        parts.unshift(`${currentTag}:nth-of-type(${index})`);
        current = current.parentElement;
      }
      return parts.join(">");
    };
    const structureSignatureFor = (candidate, root, controlList, sameKindList, index, sameKindIndex, kindFn, textFn, pathFn) => {
      const rect = candidate.getBoundingClientRect?.() || { left: 0, top: 0, width: 0, height: 0 };
      const rootRect = root?.getBoundingClientRect?.() || { left: 0, top: 0, width: innerWidth || 1, height: innerHeight || 1 };
      const band = (value, span, buckets = 12) => span > 0 ? Math.max(0, Math.min(buckets - 1, Math.floor((value / span) * buckets))) : null;
      const ordered = controlList.map((item) => ({ item, rect: item.getBoundingClientRect?.() || { left: 0, top: 0 } })).sort((a, b) => Math.abs(a.rect.top - b.rect.top) > 8 ? a.rect.top - b.rect.top : a.rect.left - b.rect.left);
      const visualIndex = ordered.findIndex((entry) => entry.item === candidate);
      const window = [];
      for (let offset = -2; offset <= 2; offset++) {
        const item = controlList[index + offset];
        if (!item) continue;
        window.push({ offset, kind: kindFn(item), tag: item.tagName?.toLowerCase?.() || "", type: (item.getAttribute?.("type") || "").toLowerCase(), role: (item.getAttribute?.("role") || "").toLowerCase(), text: textFn(item) });
      }
      const path = [];
      let current = candidate;
      while (current && current.nodeType === Node.ELEMENT_NODE && path.length < 7) {
        path.unshift({ tag: current.tagName.toLowerCase(), role: (current.getAttribute?.("role") || "").toLowerCase() || null, kind: current === candidate ? kindFn(candidate) : null });
        current = current.parentElement;
      }
      const scrollLeft = (document.scrollingElement && document.scrollingElement.scrollLeft) || 0;
      const scrollTop = (document.scrollingElement && document.scrollingElement.scrollTop) || 0;
      return { version: 1, control_index: index, same_kind_index: sameKindIndex, control_count: controlList.length, same_kind_count: sameKindList.length, target_kind: kindFn(candidate), kind_sequence: controlList.map(kindFn).slice(0, 48), element_state: { disabled: Boolean(candidate.disabled || candidate.getAttribute?.("aria-disabled") === "true"), hidden: Boolean(candidate.hidden), required: Boolean(candidate.required || candidate.getAttribute?.("aria-required") === "true") }, layout: { visual_index: visualIndex, x_band: band(rect.left - rootRect.left, rootRect.width || innerWidth || 1), y_band: band(rect.top - rootRect.top, rootRect.height || innerHeight || 1), width_band: band(rect.width, rootRect.width || innerWidth || 1, 8), height_band: band(rect.height, rootRect.height || innerHeight || 1, 8), coordinate_space: "viewport", rect: { x: Math.round(rect.left), y: Math.round(rect.top), width: Math.round(rect.width), height: Math.round(rect.height) }, viewport_rect: { x: Math.round(rect.left), y: Math.round(rect.top), width: Math.round(rect.width), height: Math.round(rect.height) }, document_rect: { x: Math.round(rect.left + scrollLeft), y: Math.round(rect.top + scrollTop), width: Math.round(rect.width), height: Math.round(rect.height) } }, control_window: window, ancestor_path: path, dom_path: pathFn(candidate) };
    };
    const stableAttributes = {};
    ["id", "name", "aria-label", "title", "data-testid", "data-test", "data-cy", "href"].forEach((name) => {
      const value = attr(name);
      if (value) stableAttributes[name] = value;
    });
    const editable = target.isContentEditable || tag === "textarea" || (tag === "input" && !["button", "checkbox", "hidden", "image", "radio", "reset", "submit"].includes(type));
    const buttonLike = tag === "button" || role === "button" || type === "button" || type === "submit" || type === "reset";
    const submitText = /submit|send|continue|confirm|save|approve|authorize/i.test(label);
    return {
      tag,
      type,
      role,
      text: label,
      id: attr("id"),
      classes,
      ariaLabel: attr("aria-label"),
      title: attr("title"),
      name: attr("name"),
      href: attr("href"),
      url: location.href,
      nearbyText: labelTextOf(wrappingLabel || forLabel || heading),
      ancestorText: textOf(form),
      formId: form ? (form.getAttribute("id") || "") : "",
      formName: form ? (form.getAttribute("name") || "") : "",
      formAction: form ? (form.getAttribute("action") || "") : "",
      sectionHeading: textOf(heading),
      parentLabel: labelTextOf(wrappingLabel || forLabel),
      controlIndex,
      sameKindIndex,
      previousControlText: controlIndex > 0 ? controlText(controls[controlIndex - 1]) : "",
      nextControlText: controlIndex >= 0 && controlIndex < controls.length - 1 ? controlText(controls[controlIndex + 1]) : "",
      domPath: domPathOf(target),
      structureSignature: structureSignatureFor(target, structuralRoot, controls, sameKindControls, controlIndex, sameKindIndex, kindOf, controlText, domPathOf),
      attributes: stableAttributes,
      isConnected: target.isConnected !== false,
      isActiveElement: Boolean(target.ownerDocument && target.ownerDocument.activeElement === target),
      valueLength: typeof target.value === "string" ? target.value.length : (target.textContent ? target.textContent.length : 0),
      isButton: buttonLike,
      isCheckbox: type === "checkbox" || type === "radio" || role === "checkbox",
      isEditable: editable,
      isFormControl: ["button", "input", "select", "textarea"].includes(tag) || editable,
      isSelect: tag === "select" || role === "combobox" || role === "listbox" || role === "option",
      isSubmit: type === "submit" || (tag === "button" && (!type || type === "submit")) || (buttonLike && submitText),
      isNativeSubmit: Boolean(target.form && ((tag === "button" && target.type === "submit") || (tag === "input" && ["submit", "image"].includes(target.type)))),
      confidence: target === node ? 0.82 : 0.72,
      source
    };
  };
  const ignoredSelector = __ACIRA_IGNORED_SELECTORS__.join(',');
  const candidates = document.elementsFromPoint ? document.elementsFromPoint(x, y) : [document.elementFromPoint(x, y)];
  const node = candidates.find((candidate) => {
    if (!candidate) return false;
    return !(ignoredSelector && candidate.closest && candidate.closest(ignoredSelector));
  }) || candidates[0] || null;
  return describe(node, "js.elementFromPoint");
}
"""

POINT_PROBE_JS = _POINT_PROBE_JS_TEMPLATE.replace("__ACIRA_IGNORED_SELECTORS__", "[]")

RUNTIME_OBJECT_PROBE_JS = r"""
function() {
  const describe = (node, source) => {
    if (!node || !node.tagName) return null;
    const closest = node.closest
      ? node.closest('button,input,label,select,textarea,a,[role="button"],[role="checkbox"],[role="radio"],[role="switch"],[role="combobox"],[role="listbox"],[role="option"],[contenteditable="true"],[onclick],[type="submit"]')
      : node;
    let target = closest || node;
    if (target && target.tagName && target.tagName.toLowerCase() === "label") {
      const forId = target.getAttribute ? target.getAttribute("for") : "";
      const control = target.control || (forId ? document.getElementById(forId) : null) || target.querySelector?.('input,select,textarea,[role="checkbox"],[role="radio"],[role="switch"],[role="combobox"]');
      if (control) target = control;
    }
    const attr = (name) => target.getAttribute ? target.getAttribute(name) : "";
    const tag = target.tagName ? target.tagName.toLowerCase() : "";
    const type = (attr("type") || "").toLowerCase();
    const role = (attr("role") || "").toLowerCase();
    const label = (
      attr("aria-label") ||
      attr("title") ||
      target.innerText ||
      (["button", "submit", "reset"].includes(type) ? target.value : "") ||
      attr("placeholder") ||
      attr("name") ||
      ""
    ).toString().trim().slice(0, 120);
    const textOf = (candidate) => candidate ? (candidate.innerText || candidate.textContent || "").toString().trim().replace(/\s+/g, " ").slice(0, 180) : "";
    const labelTextOf = (candidate) => {
      if (!candidate) return "";
      if (!candidate.cloneNode) return textOf(candidate);
      const clone = candidate.cloneNode(true);
      clone.querySelectorAll?.("button,input,select,textarea,option,[role='button'],[role='checkbox'],[role='radio'],[role='switch'],[role='combobox'],[role='listbox'],[role='option'],[contenteditable='true']").forEach((child) => child.remove());
      return textOf(clone) || textOf(candidate);
    };
    const classes = target.classList ? Array.from(target.classList).slice(0, 16) : [];
    const form = target.closest ? target.closest("form") : null;
    const forLabel = target.id && document.querySelectorAll
      ? Array.from(document.querySelectorAll("label")).find((candidate) => candidate.htmlFor === target.id)
      : null;
    const wrappingLabel = target.closest ? target.closest("label") : null;
    const heading = target.closest ? target.closest("section,main,article,form,div")?.querySelector?.("h1,h2,h3,h4,legend,[role='heading']") : null;
    const structuralRoot = form || (target.closest ? target.closest("section,fieldset,main,article") : null) || document.body;
    const interactiveSelector = 'button,input,select,textarea,a[href],[role="button"],[role="checkbox"],[role="radio"],[role="switch"],[role="combobox"],[role="listbox"],[role="option"],[role="textbox"],[role="link"],[contenteditable="true"],[onclick],[type="submit"]';
    const normalizeControl = (candidate) => {
      if (!candidate) return null;
      if (candidate.tagName && candidate.tagName.toLowerCase() === "label") {
        const forId = candidate.getAttribute ? candidate.getAttribute("for") : "";
        return candidate.control || (forId ? document.getElementById(forId) : null) || candidate.querySelector?.('input,select,textarea,[role="checkbox"],[role="radio"],[role="switch"],[role="combobox"]') || null;
      }
      return candidate;
    };
    const controls = Array.from(structuralRoot?.querySelectorAll?.(interactiveSelector) || []).map(normalizeControl).filter(Boolean).filter((candidate, index, items) => items.indexOf(candidate) === index);
    const kindOf = (candidate) => {
      const candidateTag = candidate?.tagName ? candidate.tagName.toLowerCase() : "";
      const candidateType = (candidate?.getAttribute?.("type") || "").toLowerCase();
      const candidateRole = (candidate?.getAttribute?.("role") || "").toLowerCase();
      if (candidateTag === "input" && candidateType === "radio" || candidateRole === "radio") return "radio";
      if (candidateTag === "input" && candidateType === "checkbox" || ["checkbox", "switch"].includes(candidateRole)) return "checkbox";
      if (candidateTag === "select" || ["combobox", "listbox", "option"].includes(candidateRole)) return "select";
      if (candidateTag === "textarea" || ["textbox", "searchbox"].includes(candidateRole) || candidate?.isContentEditable) return "text_entry";
      if (candidateTag === "input" && !["button", "submit", "reset", "checkbox", "radio", "file", "range", "hidden", "image"].includes(candidateType)) return "text_entry";
      if (candidateTag === "button" || candidateRole === "button" || ["button", "submit", "reset"].includes(candidateType)) return "button";
      return candidateTag || candidateRole || "";
    };
    const controlIndex = controls.indexOf(target);
    const sameKindControls = controls.filter((candidate) => kindOf(candidate) === kindOf(target));
    const sameKindIndex = sameKindControls.indexOf(target);
    const controlText = (candidate) => {
      if (!candidate) return "";
      const candidateLabel = candidate.id && document.querySelectorAll ? Array.from(document.querySelectorAll("label")).find((item) => item.htmlFor === candidate.id) : null;
      const candidateType = (candidate.getAttribute?.("type") || "").toLowerCase();
      return (candidate.getAttribute?.("aria-label") || candidate.getAttribute?.("title") || labelTextOf(candidate.closest?.("label") || candidateLabel) || candidate.innerText || (["button", "submit", "reset"].includes(candidateType) ? candidate.value : "") || candidate.getAttribute?.("placeholder") || candidate.getAttribute?.("name") || "").toString().trim().replace(/\s+/g, " ").slice(0, 160);
    };
    const domPathOf = (candidate) => {
      const parts = [];
      let current = candidate;
      while (current && current.nodeType === Node.ELEMENT_NODE && parts.length < 8) {
        const currentTag = current.tagName.toLowerCase();
        const siblings = current.parentElement ? Array.from(current.parentElement.children).filter((item) => item.tagName === current.tagName) : [];
        const index = Math.max(1, siblings.indexOf(current) + 1);
        parts.unshift(`${currentTag}:nth-of-type(${index})`);
        current = current.parentElement;
      }
      return parts.join(">");
    };
    const structureSignatureFor = (candidate, root, controlList, sameKindList, index, sameKindIndex, kindFn, textFn, pathFn) => {
      const rect = candidate.getBoundingClientRect?.() || { left: 0, top: 0, width: 0, height: 0 };
      const rootRect = root?.getBoundingClientRect?.() || { left: 0, top: 0, width: innerWidth || 1, height: innerHeight || 1 };
      const band = (value, span, buckets = 12) => span > 0 ? Math.max(0, Math.min(buckets - 1, Math.floor((value / span) * buckets))) : null;
      const ordered = controlList.map((item) => ({ item, rect: item.getBoundingClientRect?.() || { left: 0, top: 0 } })).sort((a, b) => Math.abs(a.rect.top - b.rect.top) > 8 ? a.rect.top - b.rect.top : a.rect.left - b.rect.left);
      const visualIndex = ordered.findIndex((entry) => entry.item === candidate);
      const window = [];
      for (let offset = -2; offset <= 2; offset++) {
        const item = controlList[index + offset];
        if (!item) continue;
        window.push({ offset, kind: kindFn(item), tag: item.tagName?.toLowerCase?.() || "", type: (item.getAttribute?.("type") || "").toLowerCase(), role: (item.getAttribute?.("role") || "").toLowerCase(), text: textFn(item) });
      }
      const path = [];
      let current = candidate;
      while (current && current.nodeType === Node.ELEMENT_NODE && path.length < 7) {
        path.unshift({ tag: current.tagName.toLowerCase(), role: (current.getAttribute?.("role") || "").toLowerCase() || null, kind: current === candidate ? kindFn(candidate) : null });
        current = current.parentElement;
      }
      const scrollLeft = (document.scrollingElement && document.scrollingElement.scrollLeft) || 0;
      const scrollTop = (document.scrollingElement && document.scrollingElement.scrollTop) || 0;
      return { version: 1, control_index: index, same_kind_index: sameKindIndex, control_count: controlList.length, same_kind_count: sameKindList.length, target_kind: kindFn(candidate), kind_sequence: controlList.map(kindFn).slice(0, 48), element_state: { disabled: Boolean(candidate.disabled || candidate.getAttribute?.("aria-disabled") === "true"), hidden: Boolean(candidate.hidden), required: Boolean(candidate.required || candidate.getAttribute?.("aria-required") === "true") }, layout: { visual_index: visualIndex, x_band: band(rect.left - rootRect.left, rootRect.width || innerWidth || 1), y_band: band(rect.top - rootRect.top, rootRect.height || innerHeight || 1), width_band: band(rect.width, rootRect.width || innerWidth || 1, 8), height_band: band(rect.height, rootRect.height || innerHeight || 1, 8), coordinate_space: "viewport", rect: { x: Math.round(rect.left), y: Math.round(rect.top), width: Math.round(rect.width), height: Math.round(rect.height) }, viewport_rect: { x: Math.round(rect.left), y: Math.round(rect.top), width: Math.round(rect.width), height: Math.round(rect.height) }, document_rect: { x: Math.round(rect.left + scrollLeft), y: Math.round(rect.top + scrollTop), width: Math.round(rect.width), height: Math.round(rect.height) } }, control_window: window, ancestor_path: path, dom_path: pathFn(candidate) };
    };
    const stableAttributes = {};
    ["id", "name", "aria-label", "title", "data-testid", "data-test", "data-cy", "href"].forEach((name) => {
      const value = attr(name);
      if (value) stableAttributes[name] = value;
    });
    const editable = target.isContentEditable || tag === "textarea" || (tag === "input" && !["button", "checkbox", "hidden", "image", "radio", "reset", "submit"].includes(type));
    const buttonLike = tag === "button" || role === "button" || type === "button" || type === "submit" || type === "reset";
    const submitText = /submit|send|continue|confirm|save|approve|authorize/i.test(label);
    return {
      tag,
      type,
      role,
      text: label,
      id: attr("id"),
      classes,
      ariaLabel: attr("aria-label"),
      title: attr("title"),
      name: attr("name"),
      href: attr("href"),
      url: location.href,
      nearbyText: labelTextOf(wrappingLabel || forLabel || heading),
      ancestorText: textOf(form),
      formId: form ? (form.getAttribute("id") || "") : "",
      formName: form ? (form.getAttribute("name") || "") : "",
      formAction: form ? (form.getAttribute("action") || "") : "",
      sectionHeading: textOf(heading),
      parentLabel: labelTextOf(wrappingLabel || forLabel),
      controlIndex,
      sameKindIndex,
      previousControlText: controlIndex > 0 ? controlText(controls[controlIndex - 1]) : "",
      nextControlText: controlIndex >= 0 && controlIndex < controls.length - 1 ? controlText(controls[controlIndex + 1]) : "",
      domPath: domPathOf(target),
      structureSignature: structureSignatureFor(target, structuralRoot, controls, sameKindControls, controlIndex, sameKindIndex, kindOf, controlText, domPathOf),
      attributes: stableAttributes,
      isButton: buttonLike,
      isCheckbox: type === "checkbox" || type === "radio" || role === "checkbox",
      isEditable: editable,
      isFormControl: ["button", "input", "select", "textarea"].includes(tag) || editable,
      isSelect: tag === "select" || role === "combobox" || role === "listbox" || role === "option",
      isSubmit: type === "submit" || (tag === "button" && (!type || type === "submit")) || (buttonLike && submitText),
      isNativeSubmit: Boolean(target.form && ((tag === "button" && target.type === "submit") || (tag === "input" && ["submit", "image"].includes(target.type)))),
      confidence: target === node ? 0.84 : 0.74,
      source
    };
  };
  return describe(this, "js.objectId");
}
"""

SAFE_NATIVE_SELECT_JS = r"""
function(targetText) {
  const element = this;
  if (!element || !element.tagName || element.tagName.toLowerCase() !== "select") {
    return { success: false, error: "Target is not a native select element." };
  }

  const requested = String(targetText || "").trim().toLowerCase();
  const options = Array.from(element.options || []);
  const option = options.find((candidate) => {
    const text = String(candidate.text || "").trim().toLowerCase();
    const value = String(candidate.value || "").trim().toLowerCase();
    return text === requested || value === requested;
  });

  if (!option) {
    return {
      success: false,
      error: `Option '${targetText}' was not found.`,
      availableOptions: options.map((candidate) => ({
        text: String(candidate.text || "").trim(),
        value: String(candidate.value || "")
      }))
    };
  }

  element.focus();
  element.value = option.value;
  option.selected = true;
  element.selectedIndex = option.index;
  element.dispatchEvent(new Event("input", { bubbles: true }));
  element.dispatchEvent(new Event("change", { bubbles: true }));
  element.blur();

  return {
    success: true,
    message: `Selected option: ${String(option.text || "").trim()} (value: ${option.value})`,
    value: element.value
  };
}
"""

ACTIVE_ELEMENT_PROBE_JS = r"""
() => {
  const node = document.activeElement;
  if (!node) return null;
  const attr = (name) => node.getAttribute ? node.getAttribute(name) : "";
  const tag = node.tagName ? node.tagName.toLowerCase() : "";
  const type = (attr("type") || "").toLowerCase();
  const role = (attr("role") || "").toLowerCase();
  const label = (
    attr("aria-label") ||
    attr("title") ||
    node.innerText ||
    (["button", "submit", "reset"].includes(type) ? node.value : "") ||
    attr("placeholder") ||
    attr("name") ||
    ""
  ).toString().trim().slice(0, 120);
  const textOf = (candidate) => candidate ? (candidate.innerText || candidate.textContent || "").toString().trim().replace(/\s+/g, " ").slice(0, 180) : "";
  const labelTextOf = (candidate) => {
    if (!candidate) return "";
    if (!candidate.cloneNode) return textOf(candidate);
    const clone = candidate.cloneNode(true);
    clone.querySelectorAll?.("button,input,select,textarea,option,[role='button'],[role='checkbox'],[role='radio'],[role='switch'],[role='combobox'],[role='listbox'],[role='option'],[contenteditable='true']").forEach((child) => child.remove());
    return textOf(clone) || textOf(candidate);
  };
  const classes = node.classList ? Array.from(node.classList).slice(0, 16) : [];
  const form = node.closest ? node.closest("form") : null;
  const forLabel = node.id && document.querySelectorAll
    ? Array.from(document.querySelectorAll("label")).find((candidate) => candidate.htmlFor === node.id)
    : null;
  const wrappingLabel = node.closest ? node.closest("label") : null;
  const heading = node.closest ? node.closest("section,main,article,form,div")?.querySelector?.("h1,h2,h3,h4,legend,[role='heading']") : null;
  const structuralRoot = form || (node.closest ? node.closest("section,fieldset,main,article") : null) || document.body;
  const interactiveSelector = 'button,input,select,textarea,a[href],[role="button"],[role="checkbox"],[role="radio"],[role="switch"],[role="combobox"],[role="listbox"],[role="option"],[role="textbox"],[role="link"],[contenteditable="true"],[onclick],[type="submit"]';
  const normalizeControl = (candidate) => {
    if (!candidate) return null;
    if (candidate.tagName && candidate.tagName.toLowerCase() === "label") {
      const forId = candidate.getAttribute ? candidate.getAttribute("for") : "";
      return candidate.control || (forId ? document.getElementById(forId) : null) || candidate.querySelector?.('input,select,textarea,[role="checkbox"],[role="radio"],[role="switch"],[role="combobox"]') || null;
    }
    return candidate;
  };
  const controls = Array.from(structuralRoot?.querySelectorAll?.(interactiveSelector) || []).map(normalizeControl).filter(Boolean).filter((candidate, index, items) => items.indexOf(candidate) === index);
  const kindOf = (candidate) => {
    const candidateTag = candidate?.tagName ? candidate.tagName.toLowerCase() : "";
    const candidateType = (candidate?.getAttribute?.("type") || "").toLowerCase();
    const candidateRole = (candidate?.getAttribute?.("role") || "").toLowerCase();
    if (candidateTag === "input" && candidateType === "radio" || candidateRole === "radio") return "radio";
    if (candidateTag === "input" && candidateType === "checkbox" || ["checkbox", "switch"].includes(candidateRole)) return "checkbox";
    if (candidateTag === "select" || ["combobox", "listbox", "option"].includes(candidateRole)) return "select";
    if (candidateTag === "textarea" || ["textbox", "searchbox"].includes(candidateRole) || candidate?.isContentEditable) return "text_entry";
    if (candidateTag === "input" && !["button", "submit", "reset", "checkbox", "radio", "file", "range", "hidden", "image"].includes(candidateType)) return "text_entry";
    if (candidateTag === "button" || candidateRole === "button" || ["button", "submit", "reset"].includes(candidateType)) return "button";
    return candidateTag || candidateRole || "";
  };
  const controlIndex = controls.indexOf(node);
  const sameKindControls = controls.filter((candidate) => kindOf(candidate) === kindOf(node));
  const sameKindIndex = sameKindControls.indexOf(node);
  const controlText = (candidate) => {
    if (!candidate) return "";
    const candidateLabel = candidate.id && document.querySelectorAll ? Array.from(document.querySelectorAll("label")).find((item) => item.htmlFor === candidate.id) : null;
    const candidateType = (candidate.getAttribute?.("type") || "").toLowerCase();
    return (candidate.getAttribute?.("aria-label") || candidate.getAttribute?.("title") || labelTextOf(candidate.closest?.("label") || candidateLabel) || candidate.innerText || (["button", "submit", "reset"].includes(candidateType) ? candidate.value : "") || candidate.getAttribute?.("placeholder") || candidate.getAttribute?.("name") || "").toString().trim().replace(/\s+/g, " ").slice(0, 160);
  };
  const domPathOf = (candidate) => {
    const parts = [];
    let current = candidate;
    while (current && current.nodeType === Node.ELEMENT_NODE && parts.length < 8) {
      const currentTag = current.tagName.toLowerCase();
      const siblings = current.parentElement ? Array.from(current.parentElement.children).filter((item) => item.tagName === current.tagName) : [];
      const index = Math.max(1, siblings.indexOf(current) + 1);
      parts.unshift(`${currentTag}:nth-of-type(${index})`);
      current = current.parentElement;
    }
    return parts.join(">");
  };
  const structureSignatureFor = (candidate, root, controlList, sameKindList, index, sameKindIndex, kindFn, textFn, pathFn) => {
    const rect = candidate.getBoundingClientRect?.() || { left: 0, top: 0, width: 0, height: 0 };
    const rootRect = root?.getBoundingClientRect?.() || { left: 0, top: 0, width: innerWidth || 1, height: innerHeight || 1 };
    const band = (value, span, buckets = 12) => span > 0 ? Math.max(0, Math.min(buckets - 1, Math.floor((value / span) * buckets))) : null;
    const ordered = controlList.map((item) => ({ item, rect: item.getBoundingClientRect?.() || { left: 0, top: 0 } })).sort((a, b) => Math.abs(a.rect.top - b.rect.top) > 8 ? a.rect.top - b.rect.top : a.rect.left - b.rect.left);
    const visualIndex = ordered.findIndex((entry) => entry.item === candidate);
    const window = [];
    for (let offset = -2; offset <= 2; offset++) {
      const item = controlList[index + offset];
      if (!item) continue;
      window.push({ offset, kind: kindFn(item), tag: item.tagName?.toLowerCase?.() || "", type: (item.getAttribute?.("type") || "").toLowerCase(), role: (item.getAttribute?.("role") || "").toLowerCase(), text: textFn(item) });
    }
    const path = [];
    let current = candidate;
    while (current && current.nodeType === Node.ELEMENT_NODE && path.length < 7) {
      path.unshift({ tag: current.tagName.toLowerCase(), role: (current.getAttribute?.("role") || "").toLowerCase() || null, kind: current === candidate ? kindFn(candidate) : null });
      current = current.parentElement;
    }
    return { version: 1, control_index: index, same_kind_index: sameKindIndex, control_count: controlList.length, same_kind_count: sameKindList.length, target_kind: kindFn(candidate), kind_sequence: controlList.map(kindFn).slice(0, 48), element_state: { disabled: Boolean(candidate.disabled || candidate.getAttribute?.("aria-disabled") === "true"), hidden: Boolean(candidate.hidden), required: Boolean(candidate.required || candidate.getAttribute?.("aria-required") === "true") }, layout: { visual_index: visualIndex, x_band: band(rect.left - rootRect.left, rootRect.width || innerWidth || 1), y_band: band(rect.top - rootRect.top, rootRect.height || innerHeight || 1), width_band: band(rect.width, rootRect.width || innerWidth || 1, 8), height_band: band(rect.height, rootRect.height || innerHeight || 1, 8), coordinate_space: "viewport", rect: { x: Math.round(rect.left), y: Math.round(rect.top), width: Math.round(rect.width), height: Math.round(rect.height) }, viewport_rect: { x: Math.round(rect.left), y: Math.round(rect.top), width: Math.round(rect.width), height: Math.round(rect.height) }, document_rect: { x: Math.round(rect.left + (window.scrollX || window.pageXOffset || 0)), y: Math.round(rect.top + (window.scrollY || window.pageYOffset || 0)), width: Math.round(rect.width), height: Math.round(rect.height) } }, control_window: window, ancestor_path: path, dom_path: pathFn(candidate) };
  };
  const stableAttributes = {};
  ["id", "name", "aria-label", "title", "data-testid", "data-test", "data-cy", "href"].forEach((name) => {
    const value = attr(name);
    if (value) stableAttributes[name] = value;
  });
  const editable = node.isContentEditable || tag === "textarea" || (tag === "input" && !["button", "checkbox", "hidden", "image", "radio", "reset", "submit"].includes(type));
  const buttonLike = tag === "button" || role === "button" || type === "button" || type === "submit" || type === "reset";
  const submitText = /submit|send|continue|confirm|save|approve|authorize/i.test(label);
  return {
    tag,
    type,
    role,
    text: label,
    id: attr("id"),
    classes,
    ariaLabel: attr("aria-label"),
    title: attr("title"),
    name: attr("name"),
    href: attr("href"),
    url: location.href,
    nearbyText: labelTextOf(wrappingLabel || forLabel || heading),
    ancestorText: textOf(form),
    formId: form ? (form.getAttribute("id") || "") : "",
    formName: form ? (form.getAttribute("name") || "") : "",
    formAction: form ? (form.getAttribute("action") || "") : "",
    sectionHeading: textOf(heading),
    parentLabel: labelTextOf(wrappingLabel || forLabel),
    controlIndex,
    sameKindIndex,
    previousControlText: controlIndex > 0 ? controlText(controls[controlIndex - 1]) : "",
    nextControlText: controlIndex >= 0 && controlIndex < controls.length - 1 ? controlText(controls[controlIndex + 1]) : "",
    domPath: domPathOf(node),
    structureSignature: structureSignatureFor(node, structuralRoot, controls, sameKindControls, controlIndex, sameKindIndex, kindOf, controlText, domPathOf),
    attributes: stableAttributes,
    isButton: buttonLike,
    isCheckbox: type === "checkbox" || type === "radio" || role === "checkbox",
    isEditable: editable,
    isFormControl: ["button", "input", "select", "textarea"].includes(tag) || editable,
    isSelect: tag === "select" || role === "combobox" || role === "listbox" || role === "option",
    isSubmit: type === "submit" || (tag === "button" && (!type || type === "submit")) || (buttonLike && submitText),
    isNativeSubmit: Boolean(node.form && ((tag === "button" && node.type === "submit") || (tag === "input" && ["submit", "image"].includes(node.type)))),
    confidence: 0.78,
    source: "js.activeElement"
  };
}
"""

_OLD_PAGE_TARGETS_PROBE_JS_UNUSED = r"""
() => {
  const selector = [
    'button',
    'input',
    'select',
    'textarea',
    'a[href]',
    '[role="button"]',
    '[role="checkbox"]',
    '[role="radio"]',
    '[role="switch"]',
    '[role="combobox"]',
    '[role="listbox"]',
    '[role="option"]',
    '[role="textbox"]',
    '[role="link"]',
    '[contenteditable="true"]',
    '[onclick]',
    '[type="submit"]'
  ].join(',');
  const textOf = (candidate) => candidate ? (candidate.innerText || candidate.textContent || "").toString().trim().replace(/\s+/g, " ").slice(0, 220) : "";
  const labelTextOf = (candidate) => {
    if (!candidate) return "";
    if (!candidate.cloneNode) return textOf(candidate);
    const clone = candidate.cloneNode(true);
    clone.querySelectorAll?.("button,input,select,textarea,option,[role='button'],[role='checkbox'],[role='radio'],[role='switch'],[role='combobox'],[role='listbox'],[role='option'],[contenteditable='true']").forEach((child) => child.remove());
    return textOf(clone) || textOf(candidate);
  };
  const describe = (target) => {
    if (!target) return null;
    const attr = (name) => target.getAttribute ? target.getAttribute(name) : "";
    const tag = target.tagName ? target.tagName.toLowerCase() : "";
    const type = (attr("type") || "").toLowerCase();
    const role = (attr("role") || "").toLowerCase();
    const label = (
      attr("aria-label") ||
      attr("title") ||
      target.innerText ||
      (["button", "submit", "reset"].includes(type) ? target.value : "") ||
      attr("placeholder") ||
      attr("name") ||
      ""
    ).toString().trim().slice(0, 120);
    const form = target.closest ? target.closest("form") : null;
    const forLabel = target.id && document.querySelectorAll
      ? Array.from(document.querySelectorAll("label")).find((candidate) => candidate.htmlFor === target.id)
      : null;
    const wrappingLabel = target.closest ? target.closest("label") : null;
    const heading = target.closest ? target.closest("section,main,article,form,div")?.querySelector?.("h1,h2,h3,h4,legend,[role='heading']") : null;
    const structuralRoot = form || (target.closest ? target.closest("section,fieldset,main,article") : null) || document.body;
    const normalizeControl = (candidate) => {
      if (!candidate) return null;
      if (candidate.tagName && candidate.tagName.toLowerCase() === "label") {
        const forId = candidate.getAttribute ? candidate.getAttribute("for") : "";
        return candidate.control || (forId ? document.getElementById(forId) : null) || candidate.querySelector?.('input,select,textarea,[role="checkbox"],[role="radio"],[role="switch"],[role="combobox"]') || null;
      }
      return candidate;
    };
    const controls = Array.from(structuralRoot?.querySelectorAll?.(selector) || []).map(normalizeControl).filter(Boolean).filter((candidate, index, items) => items.indexOf(candidate) === index);
    const kindOf = (candidate) => {
      const candidateTag = candidate?.tagName ? candidate.tagName.toLowerCase() : "";
      const candidateType = (candidate?.getAttribute?.("type") || "").toLowerCase();
      const candidateRole = (candidate?.getAttribute?.("role") || "").toLowerCase();
      if (candidateTag === "input" && candidateType === "radio" || candidateRole === "radio") return "radio";
      if (candidateTag === "input" && candidateType === "checkbox" || ["checkbox", "switch"].includes(candidateRole)) return "checkbox";
      if (candidateTag === "select" || ["combobox", "listbox", "option"].includes(candidateRole)) return "select";
      if (candidateTag === "textarea" || ["textbox", "searchbox"].includes(candidateRole) || candidate?.isContentEditable) return "text_entry";
      if (candidateTag === "input" && !["button", "submit", "reset", "checkbox", "radio", "file", "range", "hidden", "image"].includes(candidateType)) return "text_entry";
      if (candidateTag === "button" || candidateRole === "button" || ["button", "submit", "reset"].includes(candidateType)) return "button";
      return candidateTag || candidateRole || "";
    };
    const controlIndex = controls.indexOf(target);
    const sameKindControls = controls.filter((candidate) => kindOf(candidate) === kindOf(target));
    const sameKindIndex = sameKindControls.indexOf(target);
    const controlText = (candidate) => {
      if (!candidate) return "";
      const candidateLabel = candidate.id && document.querySelectorAll ? Array.from(document.querySelectorAll("label")).find((item) => item.htmlFor === candidate.id) : null;
      const candidateType = (candidate.getAttribute?.("type") || "").toLowerCase();
      return (candidate.getAttribute?.("aria-label") || candidate.getAttribute?.("title") || labelTextOf(candidate.closest?.("label") || candidateLabel) || candidate.innerText || (["button", "submit", "reset"].includes(candidateType) ? candidate.value : "") || candidate.getAttribute?.("placeholder") || candidate.getAttribute?.("name") || "").toString().trim().replace(/\s+/g, " ").slice(0, 160);
    };
    const domPathOf = (candidate) => {
      const parts = [];
      let current = candidate;
      while (current && current.nodeType === Node.ELEMENT_NODE && parts.length < 8) {
        const currentTag = current.tagName.toLowerCase();
        const siblings = current.parentElement ? Array.from(current.parentElement.children).filter((item) => item.tagName === current.tagName) : [];
        const index = Math.max(1, siblings.indexOf(current) + 1);
        parts.unshift(`${currentTag}:nth-of-type(${index})`);
        current = current.parentElement;
      }
      return parts.join(">");
    };
    const structureSignatureFor = (candidate, root, controlList, sameKindList, index, sameKindIndex, kindFn, textFn, pathFn) => {
      const rect = candidate.getBoundingClientRect?.() || { left: 0, top: 0, width: 0, height: 0 };
      const rootRect = root?.getBoundingClientRect?.() || { left: 0, top: 0, width: innerWidth || 1, height: innerHeight || 1 };
      const band = (value, span, buckets = 12) => span > 0 ? Math.max(0, Math.min(buckets - 1, Math.floor((value / span) * buckets))) : null;
      const ordered = controlList.map((item) => ({ item, rect: item.getBoundingClientRect?.() || { left: 0, top: 0 } })).sort((a, b) => Math.abs(a.rect.top - b.rect.top) > 8 ? a.rect.top - b.rect.top : a.rect.left - b.rect.left);
      const visualIndex = ordered.findIndex((entry) => entry.item === candidate);
      const window = [];
      for (let offset = -2; offset <= 2; offset++) {
        const item = controlList[index + offset];
        if (!item) continue;
        window.push({ offset, kind: kindFn(item), tag: item.tagName?.toLowerCase?.() || "", type: (item.getAttribute?.("type") || "").toLowerCase(), role: (item.getAttribute?.("role") || "").toLowerCase(), text: textFn(item) });
      }
      const path = [];
      let current = candidate;
      while (current && current.nodeType === Node.ELEMENT_NODE && path.length < 7) {
        path.unshift({ tag: current.tagName.toLowerCase(), role: (current.getAttribute?.("role") || "").toLowerCase() || null, kind: current === candidate ? kindFn(candidate) : null });
        current = current.parentElement;
      }
      const scrollLeft = (document.scrollingElement && document.scrollingElement.scrollLeft) || 0;
      const scrollTop = (document.scrollingElement && document.scrollingElement.scrollTop) || 0;
      return { version: 1, control_index: index, same_kind_index: sameKindIndex, control_count: controlList.length, same_kind_count: sameKindList.length, target_kind: kindFn(candidate), kind_sequence: controlList.map(kindFn).slice(0, 48), element_state: { disabled: Boolean(candidate.disabled || candidate.getAttribute?.("aria-disabled") === "true"), hidden: Boolean(candidate.hidden), required: Boolean(candidate.required || candidate.getAttribute?.("aria-required") === "true") }, layout: { visual_index: visualIndex, x_band: band(rect.left - rootRect.left, rootRect.width || innerWidth || 1), y_band: band(rect.top - rootRect.top, rootRect.height || innerHeight || 1), width_band: band(rect.width, rootRect.width || innerWidth || 1, 8), height_band: band(rect.height, rootRect.height || innerHeight || 1, 8), coordinate_space: "viewport", rect: { x: Math.round(rect.left), y: Math.round(rect.top), width: Math.round(rect.width), height: Math.round(rect.height) }, viewport_rect: { x: Math.round(rect.left), y: Math.round(rect.top), width: Math.round(rect.width), height: Math.round(rect.height) }, document_rect: { x: Math.round(rect.left + scrollLeft), y: Math.round(rect.top + scrollTop), width: Math.round(rect.width), height: Math.round(rect.height) } }, control_window: window, ancestor_path: path, dom_path: pathFn(candidate) };
    };
    const stableAttributes = {};
    ["id", "name", "aria-label", "title", "data-testid", "data-test", "data-cy", "href", "placeholder", "autocomplete", "role"].forEach((name) => {
      const value = attr(name);
      if (value) stableAttributes[name] = value;
    });
    if (type) stableAttributes.type = type;
    if (target.getAttribute && target.getAttribute("contenteditable") === "true") stableAttributes.contenteditable = "true";
    const editable = target.isContentEditable || tag === "textarea" || (tag === "input" && !["button", "checkbox", "hidden", "image", "radio", "reset", "submit"].includes(type));
    const buttonLike = tag === "button" || role === "button" || type === "button" || type === "submit" || type === "reset";
    const submitText = /submit|send|continue|confirm|save|approve|authorize/i.test(label);
    return {
      tag,
      type,
      role,
      text: label,
      id: attr("id"),
      classes: target.classList ? Array.from(target.classList).slice(0, 16) : [],
      ariaLabel: attr("aria-label"),
      title: attr("title"),
      name: attr("name"),
      href: attr("href"),
      url: location.href,
      nearbyText: labelTextOf(wrappingLabel || forLabel || heading),
      ancestorText: textOf(form),
      formId: form ? (form.getAttribute("id") || "") : "",
      formName: form ? (form.getAttribute("name") || "") : "",
      formAction: form ? (form.getAttribute("action") || "") : "",
      sectionHeading: textOf(heading),
      parentLabel: labelTextOf(wrappingLabel || forLabel),
      controlIndex,
      sameKindIndex,
      previousControlText: controlIndex > 0 ? controlText(controls[controlIndex - 1]) : "",
      nextControlText: controlIndex >= 0 && controlIndex < controls.length - 1 ? controlText(controls[controlIndex + 1]) : "",
      domPath: domPathOf(target),
      structureSignature: structureSignatureFor(target, structuralRoot, controls, sameKindControls, controlIndex, sameKindIndex, kindOf, controlText, domPathOf),
      attributes: stableAttributes,
      isButton: buttonLike,
      isCheckbox: type === "checkbox" || type === "radio" || role === "checkbox",
      isEditable: editable,
      isFormControl: ["button", "input", "select", "textarea"].includes(tag) || editable,
      isSelect: tag === "select" || role === "combobox" || role === "listbox" || role === "option",
      isSubmit: type === "submit" || (tag === "button" && (!type || type === "submit")) || (buttonLike && submitText),
      isNativeSubmit: Boolean(target.form && ((tag === "button" && target.type === "submit") || (tag === "input" && ["submit", "image"].includes(target.type)))),
      confidence: 0.86,
      source: "js.pageTargets"
    };
  };
  return Array.from(document.querySelectorAll(selector)).slice(0, 160).map(describe).filter(Boolean);
}
"""

PAGE_TARGETS_PROBE_JS = r"""
() => {
  const normalizerVersion = "0.7.1";
  const selector = [
    "button",
    "input",
    "label",
    "select",
    "textarea",
    "a[href]",
    "[role='button']",
    "[role='checkbox']",
    "[role='radio']",
    "[role='switch']",
    "[role='combobox']",
    "[role='listbox']",
    "[role='option']",
    "[role='slider']",
    "[role='spinbutton']",
    "[role='textbox']",
    "[role='searchbox']",
    "[role='link']",
    "[contenteditable='true']",
    "[onclick]",
    "[type='submit']"
  ].join(",");
  const clean = (value, limit = 220) => String(value || "").replace(/\s+/g, " ").trim().slice(0, limit);
  const textOf = (node, limit = 220) => clean(node ? (node.innerText || node.textContent || "") : "", limit);
  const labelTextOf = (node, limit = 220) => {
    if (!node || !node.cloneNode) return textOf(node, limit);
    const clone = node.cloneNode(true);
    clone.querySelectorAll?.(selector).forEach((child) => child.remove());
    return textOf(clone, limit) || textOf(node, limit);
  };
  const humanize = (value) => clean(String(value || "").replace(/([a-z0-9])([A-Z])/g, "$1 $2").replace(/[-_./]+/g, " "), 120);
  const kindOf = (element) => {
    const tag = element?.tagName ? element.tagName.toLowerCase() : "";
    const type = (element?.getAttribute?.("type") || "").toLowerCase();
    const role = (element?.getAttribute?.("role") || "").toLowerCase();
    const text = clean(`${textOf(element)} ${element?.getAttribute?.("aria-label") || ""} ${element?.getAttribute?.("title") || ""}`.toLowerCase());
    if (tag === "a" || role === "link" || element?.getAttribute?.("href")) return "link";
    if (tag === "input" && type === "file") return "file";
    if ((tag === "input" && type === "range") || role === "slider") return "range";
    if (tag === "select" || ["combobox", "listbox", "option"].includes(role)) return "select";
    if ((tag === "input" && type === "radio") || role === "radio") return "radio";
    if ((tag === "input" && type === "checkbox") || ["checkbox", "switch"].includes(role)) return "checkbox";
    if (tag === "textarea" || ["textbox", "searchbox"].includes(role) || element?.isContentEditable) return "text_entry";
    if (tag === "input" && !["button", "submit", "reset", "checkbox", "radio", "file", "range", "hidden", "image"].includes(type)) return "text_entry";
    if (tag === "button" || role === "button" || ["button", "submit", "reset"].includes(type)) {
      return type === "submit" || /\b(submit|send|continue|confirm|approve|authorize)\b/.test(text) ? "submit_button" : "button";
    }
    return tag || role || "";
  };
  const normalizeControl = (element) => {
    if (!element) return null;
    if (element.tagName?.toLowerCase?.() === "label") {
      const forId = element.getAttribute("for");
      return element.control || (forId ? element.ownerDocument.getElementById(forId) : null) || element.querySelector?.("input,select,textarea,[role='checkbox'],[role='radio'],[role='switch'],[role='combobox'],[role='slider'],[role='textbox']") || null;
    }
    return element;
  };
  const idText = (element, attrName) => {
    const ids = clean(element?.getAttribute?.(attrName) || "", 260).split(/\s+/).filter(Boolean);
    return clean(ids.map((id) => textOf(element.ownerDocument.getElementById(id), 120)).filter(Boolean).join(" "), 220);
  };
  const explicitLabel = (element) => {
    if (!element) return "";
    const doc = element.ownerDocument || document;
    const wrapped = element.closest?.("label");
    if (wrapped) return labelTextOf(wrapped, 160);
    if (element.id) {
      const label = Array.from(doc.querySelectorAll("label")).find((candidate) => candidate.htmlFor === element.id);
      if (label) return labelTextOf(label, 160);
    }
    return "";
  };
  const nearbyLabel = (element) => {
    const direct = idText(element, "aria-labelledby") || element.getAttribute?.("aria-label") || explicitLabel(element);
    if (direct) return clean(direct, 160);
    const tag = element?.tagName ? element.tagName.toLowerCase() : "";
    const type = (element?.getAttribute?.("type") || "").toLowerCase();
    const role = (element?.getAttribute?.("role") || "").toLowerCase();
    // A link's own label identifies its action. Borrowing the previous link's
    // text makes neighboring destinations appear identical during mapping.
    if (tag === "a" || role === "link" || tag === "button" || role === "button" || ["button", "submit", "reset"].includes(type)) {
      const ownText = clean(element?.value || textOf(element, 160), 160);
      if (ownText) return ownText;
    }
    const parent = element.parentElement;
    const candidates = [];
    let sibling = element.previousElementSibling;
    while (sibling && candidates.length < 4) {
      candidates.push(sibling);
      sibling = sibling.previousElementSibling;
    }
    if (parent) {
      candidates.push(...Array.from(parent.children).filter((item) => item !== element && !item.matches?.(selector)));
    }
    for (const candidate of candidates) {
      const text = labelTextOf(candidate, 160);
      if (text && text.length <= 120) return text;
    }
    return "";
  };
  const sectionHeading = (element) => {
    const section = element.closest?.("section,fieldset,main,article,form,div");
    const heading = section?.querySelector?.("h1,h2,h3,h4,legend,[role='heading']");
    return textOf(heading, 160);
  };
  const informationalSelector = [
    "img[alt]",
    "figure",
    "article",
    "[data-testid*='product' i]",
    "[data-testid*='item' i]",
    "[data-test*='product' i]",
    "[data-test*='item' i]",
    "[class*='product' i]",
    "[class*='cart-item' i]"
  ].join(",");
  const firstTextIn = (root, query, limit = 160) => {
    const item = root?.querySelector?.(query);
    return textOf(item, limit);
  };
  const informationalTitle = (element, root) => {
    return clean(
      element?.getAttribute?.("aria-label") ||
      element?.getAttribute?.("title") ||
      element?.getAttribute?.("alt") ||
      firstTextIn(root, "h1,h2,h3,h4,[role='heading'],[data-testid*='name' i],[data-test*='name' i],[class*='title' i],[class*='name' i]") ||
      "",
      160
    );
  };
  const informationalPrice = (root) => {
    const text = textOf(root, 260);
    const match = text.match(/(?:[$€£]\s?\d[\d,.]*|\d[\d,.]*\s?(?:usd|eur|gbp))/i);
    return match ? clean(match[0], 40) : "";
  };
  const hasTransactionalIntent = (value) => /\b(add|authorize|buy|capture|card|cart|checkout|continue|credit|debit|delete|funding|order|pay|payment|place|purchase|quantity|qty|remove|submit|update)\b/i.test(String(value || ""));
  const genericInformationalTitle = (value) => /^(?:image|item|photo|picture|product|thumbnail|view)$/i.test(clean(value));
  const controlsIn = (root) => {
    if (!root?.querySelectorAll) return [];
    const controls = Array.from(root.querySelectorAll(selector)).map(normalizeControl).filter(Boolean);
    return Array.from(new Set(controls));
  };
  const domPathOf = (element) => {
    const parts = [];
    let node = element;
    while (node && node.nodeType === Node.ELEMENT_NODE && parts.length < 8) {
      const tag = node.tagName.toLowerCase();
      const siblings = node.parentElement ? Array.from(node.parentElement.children).filter((item) => item.tagName === node.tagName) : [];
      parts.unshift(`${tag}:nth-of-type(${Math.max(1, siblings.indexOf(node) + 1)})`);
      node = node.parentElement;
    }
    return parts.join(">");
  };
  const structureSignatureFor = (element, root, controls, index, sameKindIndex) => {
    const sameKind = controls.filter((item) => kindOf(item) === kindOf(element));
    const rect = element.getBoundingClientRect?.() || { left: 0, top: 0, width: 0, height: 0 };
    const rootRect = root?.getBoundingClientRect?.() || { left: 0, top: 0, width: innerWidth || 1, height: innerHeight || 1 };
    const band = (value, span, buckets = 12) => span > 0 ? Math.max(0, Math.min(buckets - 1, Math.floor((value / span) * buckets))) : null;
    const ordered = controls.map((item) => ({ item, rect: item.getBoundingClientRect?.() || { left: 0, top: 0 } })).sort((a, b) => Math.abs(a.rect.top - b.rect.top) > 8 ? a.rect.top - b.rect.top : a.rect.left - b.rect.left);
    const visualIndex = ordered.findIndex((entry) => entry.item === element);
    const rows = [];
    ordered.forEach((entry) => {
      let row = rows.find((candidate) => Math.abs(candidate.top - entry.rect.top) <= 14);
      if (!row) {
        row = { top: entry.rect.top, items: [] };
        rows.push(row);
      }
      row.items.push(entry);
    });
    const row = rows.findIndex((candidate) => candidate.items.some((entry) => entry.item === element));
    const col = row >= 0 ? rows[row].items.findIndex((entry) => entry.item === element) : null;
    const controlText = (item) => clean(nearbyLabel(item) || textOf(item, 120) || item.getAttribute?.("placeholder") || humanize(item.getAttribute?.("name") || item.id), 120);
    const window = [];
    for (let offset = -2; offset <= 2; offset++) {
      const item = controls[index + offset];
      if (!item) continue;
      window.push({ offset, kind: kindOf(item), tag: item.tagName?.toLowerCase?.() || "", type: (item.getAttribute?.("type") || "").toLowerCase(), role: (item.getAttribute?.("role") || "").toLowerCase(), text: controlText(item) });
    }
    const path = [];
    let node = element;
    while (node && node.nodeType === Node.ELEMENT_NODE && path.length < 7) {
      path.unshift({ tag: node.tagName.toLowerCase(), role: (node.getAttribute("role") || "").toLowerCase() || null, kind: node === element ? kindOf(element) : null });
      node = node.parentElement;
    }
    return {
      version: 2,
      control_index: index,
      same_kind_index: sameKindIndex,
      control_count: controls.length,
      same_kind_count: sameKind.length,
      target_kind: kindOf(element),
      kind_sequence: controls.map(kindOf).slice(0, 64),
      element_state: { disabled: Boolean(element.disabled || element.getAttribute?.("aria-disabled") === "true"), hidden: Boolean(element.hidden), required: Boolean(element.required || element.getAttribute?.("aria-required") === "true") },
      layout: { visual_index: visualIndex, row, col, x_band: band(rect.left - rootRect.left, rootRect.width || innerWidth || 1), y_band: band(rect.top - rootRect.top, rootRect.height || innerHeight || 1), width_band: band(rect.width, rootRect.width || innerWidth || 1, 8), height_band: band(rect.height, rootRect.height || innerHeight || 1, 8), coordinate_space: "viewport", rect: { x: Math.round(rect.left), y: Math.round(rect.top), width: Math.round(rect.width), height: Math.round(rect.height) }, viewport_rect: { x: Math.round(rect.left), y: Math.round(rect.top), width: Math.round(rect.width), height: Math.round(rect.height) }, document_rect: { x: Math.round(rect.left + (window.scrollX || window.pageXOffset || 0)), y: Math.round(rect.top + (window.scrollY || window.pageYOffset || 0)), width: Math.round(rect.width), height: Math.round(rect.height) } },
      control_window: window,
      ancestor_path: path,
      dom_path: domPathOf(element)
    };
  };
  const stableAttributes = (element, type) => {
    const attrs = {};
    ["id", "name", "aria-label", "aria-labelledby", "aria-describedby", "title", "data-testid", "data-test", "data-cy", "href", "placeholder", "autocomplete", "role"].forEach((name) => {
      const value = element.getAttribute?.(name);
      if (value) attrs[name] = value;
    });
    if (type) attrs.type = type;
    if (element.getAttribute?.("contenteditable") === "true") attrs.contenteditable = "true";
    return attrs;
  };
  const actionSignatureFor = (element, type, role, controlKind, primary, label, heading) => {
    const form = element.closest?.("form");
    const actionAttrs = {};
    ["formaction", "formmethod", "href", "aria-controls", "data-action", "data-route", "data-testid", "data-test", "data-cy"].forEach((name) => {
      const value = element.getAttribute?.(name);
      if (value) actionAttrs[name] = clean(value, 140);
    });
    const handlerText = ["onclick", "onchange", "onsubmit"].map((name) => element.getAttribute?.(name) || "").join(" ");
    const formAction = element.getAttribute?.("formaction") || form?.getAttribute?.("action") || "";
    const formMethod = element.getAttribute?.("formmethod") || form?.getAttribute?.("method") || "";
    const intentValues = [
      controlKind,
      type,
      role,
      primary,
      label,
      heading,
      form?.id,
      form?.getAttribute?.("name"),
      formAction,
      formMethod,
      ...Object.values(actionAttrs),
      handlerText
    ];
    return {
      kind: controlKind,
      type,
      role,
      form_id: form?.id || "",
      form_name: form?.getAttribute?.("name") || "",
      form_action: clean(formAction, 160),
      form_method: clean(formMethod, 40),
      attrs: actionAttrs,
      handler_tokens: tokensFrom([handlerText]).slice(0, 12),
      intent_tokens: tokensFrom(intentValues).slice(0, 24),
      text: clean(primary, 120)
    };
  };
  const tokensFrom = (values) => {
    const generic = new Set(["button", "click", "control", "data", "field", "form", "input", "item", "name", "option", "select", "text", "value"]);
    const tokens = new Set();
    values.filter(Boolean).join(" ").replace(/([a-z0-9])([A-Z])/g, "$1 $2").match(/[a-z0-9]+/gi)?.forEach((token) => {
      const normalized = token.toLowerCase();
      if (normalized.length >= 3 && !generic.has(normalized)) tokens.add(normalized);
    });
    return Array.from(tokens).slice(0, 24);
  };
  const describe = (element) => {
    element = normalizeControl(element);
    if (!element || !element.tagName) return null;
    const tag = element.tagName.toLowerCase();
    const type = (element.getAttribute("type") || "").toLowerCase();
    const role = (element.getAttribute("role") || "").toLowerCase();
    const form = element.closest?.("form");
    const structuralRoot = form || element.closest?.("section,fieldset,main,article") || element.ownerDocument.body;
    const controls = controlsIn(structuralRoot);
    const index = controls.indexOf(element);
    const sameKind = controls.filter((item) => kindOf(item) === kindOf(element));
    const sameKindIndex = sameKind.indexOf(element);
    const label = nearbyLabel(element);
    const heading = sectionHeading(element);
    const describedBy = idText(element, "aria-describedby");
    const placeholder = element.getAttribute("placeholder") || "";
    const buttonText = ["button", "submit", "reset"].includes(type) || tag === "button" ? element.value || textOf(element, 120) : "";
    const inner = tag === "input" || tag === "textarea" || tag === "select" ? "" : textOf(element, 120);
    const primary = clean(element.getAttribute("aria-label") || idText(element, "aria-labelledby") || label || buttonText || inner || element.getAttribute("title") || placeholder || humanize(element.getAttribute("name") || element.id), 160);
    const previous = index > 0 ? controls[index - 1] : null;
    const next = index >= 0 && index < controls.length - 1 ? controls[index + 1] : null;
    const options = tag === "select"
      ? Array.from(element.options || []).map((option) => ({ text: clean(option.text, 80), value: clean(option.value, 80) })).slice(0, 40)
      : Array.from((structuralRoot || element.ownerDocument).querySelectorAll?.("[role='option']") || []).map((option) => ({ text: textOf(option, 80), value: clean(option.getAttribute("data-value") || option.getAttribute("value") || "", 80) })).filter((option) => option.text || option.value).slice(0, 40);
    const attrs = stableAttributes(element, type);
    const controlKind = kindOf(element);
    const actionSignature = actionSignatureFor(element, type, role, controlKind, primary, label, heading);
    const valuesForTokens = [element.id, element.getAttribute("name"), type, role, primary, label, heading, describedBy, placeholder, element.getAttribute("autocomplete"), ...options.map((option) => `${option.text} ${option.value}`), ...Object.values(actionSignature.attrs || {}), ...(actionSignature.intent_tokens || [])];
    const editable = element.isContentEditable || tag === "textarea" || (tag === "input" && !["button", "checkbox", "hidden", "image", "radio", "reset", "submit"].includes(type));
    const buttonLike = tag === "button" || role === "button" || ["button", "submit", "reset"].includes(type);
    return {
      tag,
      type,
      role,
      text: primary,
      id: element.getAttribute("id") || "",
      classes: element.classList ? Array.from(element.classList).slice(0, 16) : [],
      ariaLabel: element.getAttribute("aria-label") || idText(element, "aria-labelledby"),
      title: element.getAttribute("title") || "",
      name: element.getAttribute("name") || "",
      href: element.getAttribute("href") || "",
      url: element.ownerDocument.defaultView?.location?.href || location.href,
      nearbyText: clean([label, describedBy, heading].filter(Boolean).join(" "), 220),
      ancestorText: textOf(form, 260),
      formId: form ? form.getAttribute("id") || "" : "",
      formName: form ? form.getAttribute("name") || "" : "",
      formAction: form ? form.getAttribute("action") || "" : "",
      sectionHeading: heading,
      parentLabel: label,
      controlIndex: index,
      sameKindIndex,
      previousControlText: previous ? clean(nearbyLabel(previous) || textOf(previous, 120), 160) : "",
      nextControlText: next ? clean(nearbyLabel(next) || textOf(next, 120), 160) : "",
      domPath: domPathOf(element),
      structureSignature: structureSignatureFor(element, structuralRoot, controls, index, sameKindIndex),
      attributes: attrs,
      labels: { accessible: element.getAttribute("aria-label") || idText(element, "aria-labelledby") || label || null, associated: label || null, described_by: describedBy || null, heading: heading || null, placeholder: placeholder || null },
      options,
      actionSignature,
      semanticHints: Array.from(new Set([tag, type, role, controlKind, ...tokensFrom(valuesForTokens)])).filter(Boolean).slice(0, 28),
      identityTokens: tokensFrom(valuesForTokens),
      valueSample: type === "password" ? "" : clean(element.value || "", 80),
      normalizerVersion,
      isButton: buttonLike,
      isCheckbox: type === "checkbox" || type === "radio" || ["checkbox", "switch"].includes(role),
      isEditable: editable,
      isFormControl: ["button", "input", "select", "textarea"].includes(tag) || editable || Boolean(role),
      isSelect: tag === "select" || ["combobox", "listbox", "option"].includes(role),
      isSubmit: type === "submit" || (tag === "button" && (!type || type === "submit")) || (buttonLike && /\b(submit|send|continue|confirm|save|approve|authorize|request)\b/i.test(primary)),
      isNativeSubmit: Boolean(element.form && ((tag === "button" && element.type === "submit") || (tag === "input" && ["submit", "image"].includes(element.type)))),
      confidence: 0.9,
      source: "js.pageTargets"
    };
  };
  const describeInformational = (element) => {
    if (!element || !element.tagName) return null;
    const interactiveAncestor = element.closest?.("button,input,label,select,textarea,a[href],[role='button'],[role='checkbox'],[role='radio'],[role='switch'],[role='combobox'],[role='listbox'],[role='option'],[role='slider'],[role='spinbutton'],[role='textbox'],[role='searchbox'],[role='link'],[contenteditable='true'],[onclick],[type='submit']");
    if (interactiveAncestor && interactiveAncestor !== element) return null;
    if (element.matches?.("button,input,label,select,textarea,[role='button'],[role='checkbox'],[role='radio'],[role='switch'],[role='combobox'],[role='listbox'],[role='option'],[role='slider'],[role='spinbutton'],[role='textbox'],[role='searchbox'],[contenteditable='true'],[type='submit']")) return null;
    const tag = element.tagName.toLowerCase();
    const type = (element.getAttribute("type") || "").toLowerCase();
    const role = (element.getAttribute("role") || "").toLowerCase();
    const root = element.closest?.("article,li,section,figure,[data-testid*='product' i],[data-testid*='item' i],[data-test*='product' i],[data-test*='item' i],[class*='product' i],[class*='cart-item' i],div") || element;
    const title = informationalTitle(element, root);
    if (!title || genericInformationalTitle(title) || hasTransactionalIntent(title)) return null;
    const attrs = stableAttributes(element, type);
    const price = informationalPrice(root);
    const heading = sectionHeading(element);
    const structuralRoot = element.closest?.("section,fieldset,main,article") || root || element.ownerDocument.body;
    const controls = controlsIn(structuralRoot);
    const structure = structureSignatureFor(element, structuralRoot, controls, -1, -1);
    structure.target_kind = "informational";
    structure.informational = { safe: true, kind: tag === "img" ? "image_preview" : "product_detail", title, price };
    const valuesForTokens = [element.id, element.getAttribute("name"), type, role, title, heading, price, element.getAttribute("alt"), element.getAttribute("aria-label"), element.getAttribute("title")];
    return {
      tag,
      type,
      role,
      text: title,
      id: element.getAttribute("id") || "",
      classes: element.classList ? Array.from(element.classList).slice(0, 16) : [],
      ariaLabel: element.getAttribute("aria-label") || "",
      title: element.getAttribute("title") || element.getAttribute("alt") || "",
      name: element.getAttribute("name") || "",
      href: element.getAttribute("href") || "",
      url: element.ownerDocument.defaultView?.location?.href || location.href,
      nearbyText: clean([title, price, heading].filter(Boolean).join(" "), 220),
      ancestorText: clean([title, price].filter(Boolean).join(" "), 220),
      formId: "",
      formName: "",
      formAction: "",
      sectionHeading: heading,
      parentLabel: title,
      controlIndex: -1,
      sameKindIndex: -1,
      previousControlText: "",
      nextControlText: "",
      domPath: domPathOf(element),
      structureSignature: structure,
      attributes: attrs,
      labels: { accessible: element.getAttribute("aria-label") || title, associated: title, described_by: "", heading: heading || null, placeholder: null },
      options: [],
      actionSignature: { kind: "informational_click", type, role, form_id: "", form_name: "", form_action: "", form_method: "", attrs: {}, handler_tokens: [], intent_tokens: tokensFrom(["informational", "detail", "preview", title]).slice(0, 12), text: title },
      semanticHints: Array.from(new Set([tag, type, role, "informational", "detail", "preview", ...tokensFrom(valuesForTokens)])).filter(Boolean).slice(0, 28),
      identityTokens: tokensFrom(valuesForTokens),
      valueSample: "",
      normalizerVersion,
      isButton: false,
      isCheckbox: false,
      isEditable: false,
      isFormControl: false,
      isSelect: false,
      isSubmit: false,
      isNativeSubmit: false,
      confidence: 0.88,
      source: "js.pageTargets.informational"
    };
  };
  const docs = [document];
  Array.from(document.querySelectorAll("iframe")).forEach((frame) => {
    try {
      if (frame.contentDocument) docs.push(frame.contentDocument);
    } catch (_error) {
      // Cross-origin frames cannot be inspected from this page.
    }
  });
  const roots = [];
  const collectRoots = (root) => {
    roots.push(root);
    root.querySelectorAll?.("*").forEach((element) => {
      if (element.shadowRoot) collectRoots(element.shadowRoot);
    });
  };
  docs.forEach(collectRoots);
  const controls = [];
  roots.forEach((root) => controls.push(...Array.from(root.querySelectorAll?.(selector) || [])));
  const informational = [];
  roots.forEach((root) => informational.push(...Array.from(root.querySelectorAll?.(informationalSelector) || [])));
  const described = [
    ...Array.from(new Set(controls.map(normalizeControl).filter(Boolean))).slice(0, 220).map(describe),
    ...Array.from(new Set(informational)).slice(0, 80).map(describeInformational)
  ].filter(Boolean);
  const seen = new Set();
  return described.filter((item) => {
    const key = `${item.source || ""}|${item.domPath || ""}|${item.text || ""}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  }).slice(0, 260);
}
"""


RUNTIME_JS_ACTIONS = {"read_only_js", "trusted_helper_js", "dangerous_js", "unknown_js"}
HELPER_INSPECTION_ACTIONS = {"trusted_helper_js", "read_only_js", "safe_cdp", "read_only_cdp"}
TARGET_AWARE_SAFE_FORM_ACTIONS = {"check", "select", "form_input", "focus"}
TARGET_AWARE_RECONCILE_ACTIONS = {
    *TARGET_AWARE_SAFE_FORM_ACTIONS,
    "click",
    "file_upload",
    "form_control_activation",
    "mouse",
    "submit",
}
HIGH_RISK_MISSING_TARGET_ACTIONS_FOR_GUARD = {
    "click",
    "submit",
    "form_control_activation",
    "meaningful_cdp",
    "unknown_cdp",
    "unknown_js",
    "navigation",
    "keyboard",
    "mouse",
    "touch",
    "dom_mutation",
    "file_upload",
}
SPECIFIC_SKILL_DEFERRED_ACTIONS = {
    "navigation",
    "meaningful_cdp",
    "unknown_cdp",
    "safe_cdp",
    "read_only_cdp",
}
AUTO_TARGET_MAPPING_MARGIN = 10
MAPPING_REVIEW_SCORE_FLOOR = 40
UNRESOLVED_MAPPING_STATUSES = {"ambiguous", "unmapped_no_candidate", "probe_unavailable", "rejected"}
PROBE_UNAVAILABLE_STATUSES = {"failed", "unavailable", "wrong_target", "invalid_response"}
SPECIFIC_SKILL_MAPPING_SOURCES = {
    "specific_skill",
    "specific_skill_mapped",
    "specific_skill_mapping_review",
    "specific_skill_mapping",
    "specific_skill_mapping_rejected",
}

SENSITIVE_JS_PATTERNS = (
    (re.compile(r"\.\s*(?:click|submit|requestSubmit)\s*\(", re.IGNORECASE), "programmatic click or submit call"),
    (re.compile(r"\b(?:requestSubmit|submit)\s*\(", re.IGNORECASE), "programmatic form submission call"),
    (re.compile(r"\bdispatchEvent\s*\(", re.IGNORECASE), "synthetic event dispatch"),
    (re.compile(r"\bnew\s+(?:(?:Mouse|Keyboard|Pointer|Input|Submit|Focus)Event|Event)\s*\(", re.IGNORECASE), "synthetic event construction"),
    (
        re.compile(
            r"\.\s*(?:value|checked|selected|selectedIndex|disabled|readOnly|required|type|name|action|method|formAction|formMethod)\s*=(?!=)",
            re.IGNORECASE,
        ),
        "form/control property mutation",
    ),
    (
        re.compile(
            r"\bsetAttribute\s*\(\s*['\"](?:disabled|value|checked|selected|readonly|required|type|name|action|method|formaction|formmethod)['\"]",
            re.IGNORECASE,
        ),
        "form/control attribute mutation",
    ),
    (
        re.compile(
            r"\bremoveAttribute\s*\(\s*['\"](?:disabled|readonly|required|checked|selected)['\"]",
            re.IGNORECASE,
        ),
        "form/control attribute removal",
    ),
    (re.compile(r"\bdocument\s*\.\s*cookie\s*=", re.IGNORECASE), "cookie mutation"),
    (re.compile(r"\b(?:localStorage|sessionStorage)\s*\.\s*(?:setItem|removeItem|clear)\s*\(", re.IGNORECASE), "storage mutation"),
    (re.compile(r"\bHTMLFormElement\s*\.\s*prototype\s*\.\s*submit\b", re.IGNORECASE), "form submission prototype access"),
    (re.compile(r"\bHTMLElement\s*\.\s*prototype\s*\.\s*click\b", re.IGNORECASE), "click prototype access"),
)

GENERIC_JS_MUTATION_PATTERNS = (
    re.compile(r"\.\s*(?:remove|appendChild|prepend|append|before|after|replaceWith|replaceChildren)\s*\(", re.IGNORECASE),
    re.compile(r"\b(?:createElement|createTextNode|insertAdjacent(?:HTML|Text|Element))\s*\(", re.IGNORECASE),
    re.compile(r"\.\s*(?:innerHTML|outerHTML|textContent|innerText|className|id|cssText)\s*=(?!=)", re.IGNORECASE),
    re.compile(r"\.\s*style\s*\.", re.IGNORECASE),
    re.compile(r"\.\s*classList\s*\.\s*(?:add|remove|toggle|replace)\s*\(", re.IGNORECASE),
    re.compile(r"\b(?:setAttribute|removeAttribute)\s*\(", re.IGNORECASE),
    re.compile(r"\b(?:addEventListener|removeEventListener)\s*\(", re.IGNORECASE),
    re.compile(r"\bdocument\s*\.\s*(?:write|open|close)\s*\(", re.IGNORECASE),
)

READ_ONLY_JS_PATTERNS = (
    re.compile(r"\bdocument\s*\.\s*(?:readyState|title|URL|documentURI|referrer)\b", re.IGNORECASE),
    re.compile(r"\b(?:window\s*\.)?location\s*\.\s*(?:href|origin|pathname|search|hash|host|hostname|protocol)\b", re.IGNORECASE),
    re.compile(r"\bnavigator\s*\.\s*(?:userAgent|language|languages|platform|webdriver)\b", re.IGNORECASE),
    re.compile(r"\bdocument\s*\.\s*(?:querySelector|querySelectorAll|getElementById|getElementsByClassName|getElementsByName|getElementsByTagName|elementFromPoint)\s*\(", re.IGNORECASE),
    re.compile(r"\.\s*(?:closest|matches|getAttribute|hasAttribute|getBoundingClientRect)\s*\(", re.IGNORECASE),
    re.compile(r"\.\s*(?:value|checked|selected|selectedOptions|disabled|readOnly|required|type|name|id|className|dataset|attributes)\b", re.IGNORECASE),
    re.compile(r"\.\s*(?:innerText|textContent|textContent|ariaLabel)\b", re.IGNORECASE),
    re.compile(r"\b(?:FormData|URLSearchParams)\s*\(", re.IGNORECASE),
    re.compile(r"\b(?:window\s*\.)?getComputedStyle\s*\(", re.IGNORECASE),
    re.compile(r"\b(?:scrollX|scrollY|pageXOffset|pageYOffset|scrollTop|scrollLeft|scrollHeight|scrollWidth|clientHeight|clientWidth|offsetHeight|offsetWidth|innerHeight|innerWidth|outerHeight|outerWidth)\b", re.IGNORECASE),
    re.compile(r"\b(?:performance\s*\.|document\s*\.\s*(?:images|fonts|scripts|styleSheets)\b|\.complete\b|\.naturalWidth\b|\.naturalHeight\b)", re.IGNORECASE),
    re.compile(r"\bgetEventListeners\s*\(", re.IGNORECASE),
)

OVERLAY_DISALLOWED_PATTERNS = (
    re.compile(r"\b(?:fetch|XMLHttpRequest|sendBeacon|WebSocket|eval)\s*\(", re.IGNORECASE),
    re.compile(r"\bFunction\s*\("),
    re.compile(r"\b(?:localStorage|sessionStorage|indexedDB)\b", re.IGNORECASE),
    re.compile(r"\bdocument\s*\.\s*cookie\b", re.IGNORECASE),
)


@dataclass(frozen=True)
class GuardTarget:
    tag: str | None = None
    type: str | None = None
    role: str | None = None
    text: str | None = None
    id: str | None = None
    classes: tuple[str, ...] = ()
    aria_label: str | None = None
    title: str | None = None
    name: str | None = None
    href: str | None = None
    url: str | None = None
    nearby_text: str | None = None
    ancestor_text: str | None = None
    form_id: str | None = None
    form_name: str | None = None
    form_action: str | None = None
    section_heading: str | None = None
    parent_label: str | None = None
    control_index: int | None = None
    same_kind_index: int | None = None
    previous_control_text: str | None = None
    next_control_text: str | None = None
    dom_path: str | None = None
    structure_signature: dict[str, Any] | None = None
    attributes: dict[str, str] | None = None
    is_button: bool = False
    is_checkbox: bool = False
    is_editable: bool = False
    is_form_control: bool = False
    is_select: bool = False
    is_submit: bool = False
    is_native_submit: bool | None = None
    confidence: float = 0.0
    source: str = "unknown"

    def to_metadata(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "type": self.type,
            "role": self.role,
            "text": self.text,
            "id": self.id,
            "classes": list(self.classes),
            "aria_label": self.aria_label,
            "title": self.title,
            "name": self.name,
            "href": self.href,
            "url": self.url,
            "nearby_text": self.nearby_text,
            "ancestor_text": self.ancestor_text,
            "form_id": self.form_id,
            "form_name": self.form_name,
            "form_action": self.form_action,
            "section_heading": self.section_heading,
            "parent_label": self.parent_label,
            "control_index": self.control_index,
            "same_kind_index": self.same_kind_index,
            "previous_control_text": self.previous_control_text,
            "next_control_text": self.next_control_text,
            "dom_path": self.dom_path,
            "structure_signature": dict(self.structure_signature or {}),
            "attributes": dict(self.attributes or {}),
            "is_button": self.is_button,
            "is_checkbox": self.is_checkbox,
            "is_editable": self.is_editable,
            "is_form_control": self.is_form_control,
            "is_select": self.is_select,
            "is_submit": self.is_submit,
            "is_native_submit": self.is_native_submit,
            "confidence": self.confidence,
            "source": self.source,
        }


@dataclass(frozen=True)
class ActionClassification:
    method: str
    action: str
    reason: str
    confidence: float = 1.0
    read_only: bool = False
    meaningful: bool = False
    target: GuardTarget | None = None
    details: dict[str, Any] | None = None
    replacement_payload: str | bytes | None = None

    def to_metadata(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "action": self.action,
            "reason": self.reason,
            "confidence": self.confidence,
            "read_only": self.read_only,
            "meaningful": self.meaningful,
            "target": self.target.to_metadata() if self.target else None,
            "details": redact_value(self.details),
            "replacement": "safe_select_js" if self.replacement_payload is not None else None,
        }


@dataclass(frozen=True)
class GuardReviewRequest:
    payload: str | bytes
    context: CDPMessageContext
    classification: ActionClassification
    reason: str


def _review_message_projection(message: Any, *, redact_secret_fields: bool = False) -> Any:
    """Mask message content while keeping exact CDP routing identifiers."""
    projected = (json.loads(redact_payload(json.dumps(message), redact_secret_fields=True))
                 if redact_secret_fields else redact_value(message))
    if isinstance(message, dict) and isinstance(projected, dict):
        for name in ("id", "method", "sessionId"):
            if name in message:
                projected[name] = message[name]
    return projected


def _review_payload_projection(payload: str | bytes, *, redact_secret_fields: bool = False) -> str | bytes:
    try:
        original = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return redact_payload(payload, redact_secret_fields=redact_secret_fields)
    parsed = _review_message_projection(original, redact_secret_fields=redact_secret_fields)
    if parsed == original:
        return payload
    text = json.dumps(parsed, ensure_ascii=True, separators=(",", ":"))
    return text.encode("utf-8") if isinstance(payload, bytes) else text


def _review_is_sensitive_entry(classification: ActionClassification) -> bool:
    target = classification.target
    if classification.action != "form_input" or target is None:
        return False
    identifiers = (target.name, target.id, target.aria_label, target.title,
                   (target.attributes or {}).get("autocomplete"))
    return (str(target.type or "").lower() == "password"
            or any(isinstance(item, str) and _key_is_sensitive(item) for item in identifiers))


def _review_path_projection(path: str) -> str:
    projected = redact_value(path)
    if projected != path or not path.startswith("/"):
        return projected
    # websocket_path is normally relative; use a fixed parsing origin only in
    # its display copy so the shared URL-query redactor can mask aliases.
    prefix = "https://acira.invalid"
    projected = redact_value(prefix + path)
    return projected[len(prefix):] if projected.startswith(prefix) else "[REDACTED]"


def _review_details_projection(value: Any) -> Any:
    # Full page probes are runtime-only, as in durable review records. Keep
    # selected targets/mapping evidence; never forward every editable value.
    def bounded_observations(item: Any) -> Any:
        if isinstance(item, dict):
            return {key: {"omitted": True, "candidate_count": len(entry),
                          "reason": "Full page observations are runtime-only; selected targets and mapping evidence are recorded separately"}
                    if key == "page_targets" and isinstance(entry, list)
                    else bounded_observations(entry) for key, entry in item.items()}
        if isinstance(item, list):
            return [bounded_observations(entry) for entry in item]
        return item
    return redact_value(bounded_observations(value))


def redact_review_request(request: GuardReviewRequest) -> GuardReviewRequest:
    """Return a reviewer-only view; retain the private request for authorization.

    Scope hashing, operation binding and browser dispatch must use the original
    request. The copied context retains its exact session and probe routing.
    """
    target = request.classification.target
    projected_target = replace(target, **{
        item.name: redact_value(getattr(target, item.name)) for item in dataclass_fields(target)
    }) if target is not None else None
    replacement = request.classification.replacement_payload
    sensitive_entry = _review_is_sensitive_entry(request.classification)
    classification = replace(request.classification,
        reason=redact_value(request.classification.reason), target=projected_target,
        details=_review_details_projection(request.classification.details),
        replacement_payload=_review_payload_projection(replacement, redact_secret_fields=sensitive_entry) if replacement is not None else None)
    context = replace(request.context,
        parsed_message=_review_message_projection(request.context.parsed_message, redact_secret_fields=sensitive_entry),
        websocket_path=_review_path_projection(request.context.websocket_path))
    return replace(request, payload=_review_payload_projection(request.payload, redact_secret_fields=sensitive_entry),
        context=context, classification=classification, reason=redact_value(request.reason))


class ReviewBackend(Protocol):
    async def ask(self, request: GuardReviewRequest) -> ReviewResult:
        """Return a deterministic approval decision for a pending review."""


class CallableReviewBackend:
    def __init__(self, handler: ReviewHandler) -> None:
        self._handler = handler

    async def ask(self, request: GuardReviewRequest) -> ReviewResult:
        result = self._handler(request)
        if inspect.isawaitable(result):
            result = await result
        return result


# A terminal is shared by every run and event loop in this process. Hold its
# ownership from the contextual prompt through the answer, including cancellation.
_TERMINAL_REVIEW_OWNER = threading.Lock()


class TerminalReviewBackend:
    def __init__(self, config: AciraGuardConfig) -> None:
        self._config = config

    async def ask(self, request: GuardReviewRequest) -> ReviewResult:
        if not self._config.terminal_review or not sys.stdin.isatty():
            return "block"
        stop = asyncio.Event()
        try:
            result = await asyncio.wait_for(self.ask_until(request, stop_event=stop), timeout=300)
            return result if result is not None else "block"
        except asyncio.TimeoutError:
            return "block"
        finally:
            stop.set()

    async def ask_until(self, request: GuardReviewRequest, stop_event: asyncio.Event | None = None, *,
                        display_context: dict[str, str] | None = None,
                        display_evidence: dict[str, Any] | None = None) -> ReviewResult | None:
        if not sys.stdin.isatty():
            return "block"
        while not _TERMINAL_REVIEW_OWNER.acquire(blocking=False):
            if stop_event is not None and stop_event.is_set():
                return None
            await asyncio.sleep(0.05)
        try:
            if stop_event is not None and stop_event.is_set():
                return None
            context = {"terminal_review": uuid4().hex[:12], **(display_context or {})}
            print("\nAcira review context:", json.dumps(redact_value(context), ensure_ascii=True), flush=True)
            if display_evidence:
                print("Acira operation evidence:", json.dumps(_review_details_projection(display_evidence),
                    indent=2, ensure_ascii=True), flush=True)
            try:
                answer = await _terminal_input_until(self.prompt_for(request), stop_event=stop_event)
            except EOFError:
                # Missing affirmative input never authorizes an Ask action,
                # including configurations with a legacy default of allow.
                return "block"
        finally:
            _TERMINAL_REVIEW_OWNER.release()
        if answer is None:
            return None
        return answer.strip().lower()

    def prompt_for(self, request: GuardReviewRequest) -> str:
        request = redact_review_request(request)
        if request.classification.action == "unknown_js":
            return _color(_runtime_js_review_prompt(request), "yellow")
        if _is_mapping_review(request):
            return _color(_mapping_review_prompt(request), "yellow")
        if _is_general_skill_review(request):
            return _color(_general_skill_review_prompt(request), "yellow")
        label = request.classification.action.replace("_", " ")
        target = request.classification.target
        target_name = _target_label(target) if target else ""
        target_text = f" target={target.tag or ''}:{target_name}" if target and target_name else ""
        command_label = _review_command_label(request.classification)
        return _color(
            f"Acira Guard HITL review: allow {label}{target_text}?\n"
            f"  reason={request.reason}\n"
            f"Allow this {command_label}? [y/N] ",
            "yellow",
        )


@dataclass(frozen=True)
class BlockedRetryState:
    key: str
    count: int
    limit: int
    on_exceed: str
    exceeded: bool

    def to_metadata(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "count": self.count,
            "limit": self.limit,
            "on_exceed": self.on_exceed,
            "exceeded": self.exceeded,
        }


async def _terminal_input_until(prompt: str, *, stop_event: asyncio.Event | None = None) -> str | None:
    print(prompt, end="", flush=True)
    if sys.platform == "win32":
        return await _windows_terminal_input_until(stop_event=stop_event)
    return await _posix_terminal_input_until(stop_event=stop_event)


async def _windows_terminal_input_until(*, stop_event: asyncio.Event | None = None) -> str | None:
    import msvcrt

    chars: list[str] = []
    while True:
        if stop_event is not None and stop_event.is_set():
            print("\nAcira HITL review answered from dashboard.", flush=True)
            return None
        if not msvcrt.kbhit():
            await asyncio.sleep(0.05)
            continue
        char = msvcrt.getwch()
        if char in {"\r", "\n"}:
            print("", flush=True)
            return "".join(chars)
        if char == "\x03":
            raise KeyboardInterrupt
        if char in {"\b", "\x7f"}:
            if chars:
                chars.pop()
                print("\b \b", end="", flush=True)
            continue
        if char in {"\x00", "\xe0"}:
            if msvcrt.kbhit():
                msvcrt.getwch()
            continue
        chars.append(char)
        print(char, end="", flush=True)


async def _posix_terminal_input_until(*, stop_event: asyncio.Event | None = None) -> str | None:
    import select

    while True:
        if stop_event is not None and stop_event.is_set():
            print("\nAcira HITL review answered from dashboard.", flush=True)
            return None
        ready, _, _ = select.select([sys.stdin], [], [], 0.05)
        if not ready:
            await asyncio.sleep(0)
            continue
        line = sys.stdin.readline()
        if line == "":
            raise EOFError
        return line.rstrip("\r\n")


@dataclass(frozen=True)
class RecentBlockedFormTarget:
    connection_id: str
    target_key: str
    target_text: str
    reason: str
    expires_at: float
    source_action: str = "form_input"
    target_metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class ActionTransaction:
    transaction_id: str
    framework: str
    action: str
    decision: str
    target_key: str
    target_text: str
    target_metadata: dict[str, Any]
    reason: str
    expires_at: float
    skill_decision_id: str | None = None
    review_cache_key: str | None = None
    allows_secret_payload: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_metadata(self) -> dict[str, Any]:
        return {
            "transaction_id": self.transaction_id,
            "framework": self.framework,
            "action": self.action,
            "decision": self.decision,
            "target_key": self.target_key,
            "target_text": self.target_text,
            "target": dict(self.target_metadata),
            "skill_decision_id": self.skill_decision_id,
            "review_cache_key": self.review_cache_key,
            "allows_secret_payload": self.allows_secret_payload,
            "metadata": redact_value(self.metadata),
        }


@dataclass(frozen=True)
class SecurityLockdownState:
    active: bool = False
    reason: str = ""
    source: str = ""
    review_key: str | None = None
    page_signature: str | None = None
    skill_ids: tuple[str, ...] = ()
    created_at: float = 0.0

    def to_metadata(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "reason": self.reason,
            "source": self.source,
            "review_key": self.review_key,
            "page_signature": self.page_signature,
            "skill_ids": list(self.skill_ids),
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class PageMappingSession:
    key: str
    status: str
    entries: tuple[dict[str, Any], ...]
    page_signature: str | None
    probe_status: str | None
    review_key: str | None
    created_at: float

    def to_metadata(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "status": self.status,
            "page_signature": self.page_signature,
            "probe_status": self.probe_status,
            "review_key": self.review_key,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class CachedPageTargetProbe:
    targets: tuple[dict[str, Any], ...]
    metadata: dict[str, Any]
    expires_at: float


@dataclass(frozen=True)
class PageRiskSnapshot:
    scope_key: str
    url: str | None
    title: str | None
    page_signature: str | None
    status: str
    decision: str
    reason: str
    skill_decision_id: str | None
    facts: dict[str, Any]
    cedar_metadata: dict[str, Any]
    readiness: dict[str, Any]
    created_at: float
    expires_at: float

    def to_metadata(self) -> dict[str, Any]:
        return redact_value(
            {
                "scope_key": self.scope_key,
                "url": self.url,
                "title": self.title,
                "page_signature": self.page_signature,
                "status": self.status,
                "decision": self.decision,
                "reason": self.reason,
                "skill_decision_id": self.skill_decision_id,
                "readiness": self.readiness,
                "general_skills": self.cedar_metadata.get("general_skills"),
                "facts": self.cedar_metadata.get("facts"),
                "created_at": self.created_at,
            }
        )


@dataclass
class PreflightPageState:
    scope_key: str
    pending: bool = False
    last_url: str | None = None
    reason: str = "initial"
    navigation_started_at: float = field(default_factory=time.monotonic)
    load_event_at: float | None = None
    last_network_event_at: float | None = None
    in_flight_requests: int = 0
    last_snapshot_key: str | None = None


@dataclass(frozen=True)
class PendingSecretFieldRead:
    secret_id: str
    label: str
    target: dict[str, Any]
    expires_at: float


class AciraGuardPolicy:
    """Config-backed CDP guard that is independent from framework adapters."""

    def __init__(
        self,
        config: AciraGuardConfig | dict[str, Any] | None = None,
        *,
        review_handler: ReviewHandler | None = None,
        review_backend: ReviewBackend | None = None,
        adapters: tuple[FrameworkAdapterSpec, ...] = (),
    ) -> None:
        self.config = AciraGuardConfig.from_dict(config) if not isinstance(config, AciraGuardConfig) else config
        self.review_pause = ReviewPauseState()
        self.before_decision = None
        self.policy_revision = "legacy"
        self.framework_adapters = FrameworkAdapterRegistry(adapters, config=self.config.adapters)
        self.review_handler = review_handler
        self.review_backend = review_backend or (CallableReviewBackend(review_handler) if review_handler else None)
        self.decision_engine = CedarDecisionEngine()
        self._pause_coordinator: PauseCoordinator | None = None
        self._blocked_attempts: dict[str, int] = {}
        self._review_approved_keys: set[str] = set()
        self._review_denied_keys: set[str] = set()
        self._review_inflight: dict[str, asyncio.Future[bool]] = {}
        self._approved_empty_mapping_keys: set[str] = set()
        self._page_mapping_sessions: dict[str, PageMappingSession] = {}
        self._cached_page_target_probes: dict[str, CachedPageTargetProbe] = {}
        self._last_page_target_probe: CachedPageTargetProbe | None = None
        self._preflight_page_states: dict[str, PreflightPageState] = {}
        self._preflight_snapshots: dict[str, PageRiskSnapshot] = {}
        self._preflight_snapshots_by_url: dict[str, PageRiskSnapshot] = {}
        self._preflight_quarantine: dict[str, PageRiskSnapshot] = {}
        self._preflight_quarantine_by_url: dict[str, PageRiskSnapshot] = {}
        self._last_preflight_snapshot: PageRiskSnapshot | None = None
        self._active_preflight_tasks: dict[str, asyncio.Task[Any]] = {}
        self._active_preflight_task_connections: dict[asyncio.Task[Any], str] = {}
        self._approved_preflight_urls: set[str] = set()
        self._denied_preflight_urls: set[str] = set()
        self._logged_preflight_snapshots: set[tuple[str, str, str]] = set()
        self._security_lockdown = SecurityLockdownState()
        self._recent_blocked_form_targets: list[RecentBlockedFormTarget] = []
        self._action_transactions: list[ActionTransaction] = []
        self._action_transaction_confirmed_connections: dict[str, set[str]] = {}
        self._pending_secret_field_reads: dict[tuple[str, str | None, int | str], PendingSecretFieldRead] = {}
        self.target_mapping_store: TargetMappingStore = (
            JsonlTargetMappingStore(self.config.target_mapping_store_path)
            if self.config.target_mapping_store_path is not None
            else InMemoryTargetMappingStore()
        )
        # Audit pipeline (PR2): built lazily on first decision so a misconfigured/optional
        # audit subsystem can never block guard construction.
        self._audit_pipeline: "AuditPipeline | None" = None
        self._audit_pipeline_failed = False

    def has_specific_skill_for_url(self, url: str | None) -> bool:
        if not isinstance(url, str) or not url:
            return False
        return any(skill.matches_url(url) for skill in self.config.specific_skills)

    def remember_page_target_probe(
        self,
        *,
        url: str | None,
        targets: list[dict[str, Any]],
        metadata: dict[str, Any] | None = None,
        ttl_seconds: float = 5.0,
    ) -> None:
        normalized_targets = tuple(dict(item) for item in targets if isinstance(item, dict))
        probe = dict(metadata or {})
        probe.setdefault("status", "ok" if normalized_targets else "empty")
        probe["candidate_count"] = len(normalized_targets)
        probe_url = str(url or probe.get("url") or _url_from_page_targets(list(normalized_targets)) or "")
        if probe_url:
            probe["url"] = probe_url
        probe.setdefault("page_signature", _page_target_signature(list(normalized_targets), probe))
        entry = CachedPageTargetProbe(
            targets=normalized_targets,
            metadata=probe,
            expires_at=time.monotonic() + max(0.25, ttl_seconds),
        )
        self._last_page_target_probe = entry
        if probe_url:
            self._cached_page_target_probes[_normalize_mapping_url(probe_url)] = entry

    def bind_pause_coordinator(self, pause_coordinator: PauseCoordinator) -> None:
        self._pause_coordinator = pause_coordinator

    def _security_lockdown_decision(
        self,
        context: CDPMessageContext,
        classification: ActionClassification | None = None,
    ) -> CDPPolicyDecision | None:
        if not self._security_lockdown.active:
            return None
        metadata: dict[str, Any] = {
            "guard": "acira",
            "decision": "security_lockdown",
            "security_lockdown": self._security_lockdown.to_metadata(),
        }
        if classification is not None:
            metadata["classification"] = classification.to_metadata()
        if self.config.terminal_log:
            method = context.method or (classification.method if classification else "unknown")
            print(
                _color(
                    f"[Acira Guard] SECURITY LOCKDOWN id={context.message_id} method={method} "
                    f"reason={ACIRA_SECURITY_LOCKDOWN_REASON}",
                    "red",
                ),
                file=sys.stderr,
                flush=True,
            )
        return CDPPolicyDecision(
            allow=False,
            reason=ACIRA_SECURITY_LOCKDOWN_REASON,
            metadata=metadata,
        )

    def _activate_security_lockdown(
        self,
        cedar_decision: CedarDecision | None,
        *,
        review_key: str | None,
    ) -> SecurityLockdownState | None:
        if cedar_decision is None or cedar_decision.source != "specific_skill_mapping_review":
            return None
        metadata = cedar_decision.metadata if isinstance(cedar_decision.metadata, dict) else {}
        skills = metadata.get("skills") if isinstance(metadata.get("skills"), dict) else {}
        mappings = skills.get("page_mapping") if isinstance(skills.get("page_mapping"), list) else []
        probe = skills.get("page_target_probe") if isinstance(skills.get("page_target_probe"), dict) else {}
        skill_ids = sorted(
            {
                str(item.get("skill_id") or "")
                for item in mappings
                if isinstance(item, dict) and item.get("skill_id")
            }
        )
        state = SecurityLockdownState(
            active=True,
            reason=ACIRA_SECURITY_LOCKDOWN_REASON,
            source="mapping_rejected",
            review_key=review_key,
            page_signature=str(probe.get("page_signature") or "") or None,
            skill_ids=tuple(skill_ids),
            created_at=time.time(),
        )
        self._security_lockdown = state
        return state

    def begin_action_transaction(
        self,
        *,
        framework: str,
        action: str,
        target: GuardTarget | None,
        allow: bool,
        reason: str,
        metadata: dict[str, Any] | None = None,
        duration: float = 30.0,
    ) -> str | None:
        if target is None:
            return None
        self._prune_action_transactions()
        transaction_id = f"txn-{len(self._action_transactions) + 1}-{int(time.monotonic() * 1000)}"
        raw_metadata = metadata or {}
        allows_secret_payload = _metadata_contains_secret_use(raw_metadata)
        metadata = redact_value(raw_metadata)
        transaction = ActionTransaction(
            transaction_id=transaction_id,
            framework=framework,
            action=action,
            decision="allow" if allow else "block",
            target_key=_target_identity_key(target),
            target_text=_target_reference_from_target(target),
            target_metadata=target.to_metadata(),
            reason=reason,
            expires_at=time.monotonic() + duration,
            skill_decision_id=_string_or_none(metadata.get("skill_decision_id")),
            review_cache_key=_string_or_none(metadata.get("review_cache_key")),
            allows_secret_payload=allows_secret_payload,
            metadata=metadata,
        )
        self._action_transactions.append(transaction)
        self._action_transaction_confirmed_connections.setdefault(transaction_id, set())
        if not allow and action == "form_input":
            self._record_recent_blocked_target_key(
                connection_id="*",
                target_key=transaction.target_key,
                target_text=transaction.target_text,
                reason=reason,
                source_action=action,
                duration=duration,
                target_metadata=transaction.target_metadata,
            )
        return transaction_id

    def end_action_transaction(self, transaction_id: str | None) -> None:
        if not transaction_id:
            return
        self._action_transactions = [
            item for item in self._action_transactions if item.transaction_id != transaction_id
        ]
        self._action_transaction_confirmed_connections.pop(transaction_id, None)

    def _prune_action_transactions(self) -> None:
        now = time.monotonic()
        self._action_transactions = [
            item for item in self._action_transactions if item.expires_at > now
        ][-24:]
        active_ids = {item.transaction_id for item in self._action_transactions}
        for transaction_id in list(self._action_transaction_confirmed_connections):
            if transaction_id not in active_ids:
                self._action_transaction_confirmed_connections.pop(transaction_id, None)

    def _action_transaction_policy_decision(
        self,
        context: CDPMessageContext,
        classification: ActionClassification,
    ) -> CDPPolicyDecision | None:
        if context.websocket_path.startswith("/acira/action/"):
            return None

        transaction = self._active_action_transaction(classification)
        if transaction is None:
            return None

        action_kind = _transaction_cdp_kind(classification)
        if action_kind is None:
            return None

        same_target = _transaction_target_matches(transaction, classification.target)
        if not same_target and self._can_inherit_action_transaction_target(transaction, context, classification, action_kind):
            inherited_target = _target_from_probe_value(transaction.target_metadata)
            inherited_details = dict(classification.details or {})
            inherited_details["inherited_action_transaction_target"] = True
            inherited_details["action_transaction_inherited_from"] = transaction.transaction_id
            classification = replace(
                classification,
                action="form_input",
                target=inherited_target,
                details=inherited_details,
            )
            same_target = True

        if (
            transaction.decision == "block"
            and transaction.action == "form_input"
            and action_kind in {"text", "text_helper_js", "dom_mutation"}
            and not _blocked_transaction_should_intercept(transaction, classification)
        ):
            return None

        if not _transaction_kind_applies(transaction, action_kind):
            return None

        skill_backstop = self._specific_skill_transaction_backstop(transaction, classification, action_kind)
        if skill_backstop is not None:
            backstop_decision, backstop_reason, backstop_cedar = skill_backstop
            if backstop_decision == "block":
                metadata = {
                    "guard": "acira",
                    "decision": "block_from_specific_skill_inside_transaction",
                    "rule_id": None,
                    "cedar": backstop_cedar.metadata if backstop_cedar else None,
                    "classification": classification.to_metadata(),
                    "action_transaction": transaction.to_metadata(),
                }
                if backstop_cedar and backstop_cedar.skill_decision_id:
                    metadata["skill_decision_id"] = backstop_cedar.skill_decision_id
                reason = (
                    f"{backstop_reason} The active {transaction.framework} transaction was not allowed to bypass "
                    "the CDP specific-skill backstop."
                )
                self._terminal_decision("BLOCK", context, classification, reason, color="red", cedar_decision=backstop_cedar)
                return CDPPolicyDecision(
                    allow=False,
                    reason=self._blocked_reason(reason, classification, cedar_decision=backstop_cedar),
                    metadata=metadata,
                )
            return None

        details = dict(classification.details or {})
        details["action_transaction"] = transaction.to_metadata()
        if transaction.decision == "block" and transaction.action == "form_input" and action_kind in {"text", "text_helper_js", "dom_mutation"}:
            details["blocked_input_transaction"] = {
                "blocked_target": transaction.target_text,
                "current_target": _agent_target_reference(classification),
                "source_action": transaction.action,
                "reason": (
                    f"{transaction.target_text} was denied or blocked by an active text transaction. "
                    "This follow-up text command was blocked before Chrome received it."
                ),
            }
        elif transaction.decision == "allow" and not same_target:
            details["misdirected_from_blocked_target"] = {
                "blocked_target": transaction.target_text,
                "current_target": _agent_target_reference(classification),
                "source_action": transaction.action,
                "reason": (
                    f"The active {transaction.framework} {transaction.action} transaction was scoped to "
                    f"{transaction.target_text}, but this follow-up command targeted "
                    f"{_agent_target_reference(classification)}."
                ),
            }
        classification = replace(classification, details=details)
        if transaction.decision == "allow" and transaction.action == "form_input" and same_target and action_kind in {
            "focus",
            "text_helper_js",
            "dom_mutation",
        }:
            self._mark_action_transaction_connection_confirmed(transaction, context)

        metadata = {
            "guard": "acira",
            "decision": (
                "allow_from_action_transaction"
                if transaction.decision == "allow" and same_target
                else "block_from_action_transaction"
            ),
            "rule_id": None,
            "cedar": None,
            "classification": classification.to_metadata(),
            "action_transaction": transaction.to_metadata(),
        }
        if transaction.skill_decision_id:
            metadata["skill_decision_id"] = transaction.skill_decision_id
        if transaction.review_cache_key:
            metadata["review_cache_key"] = transaction.review_cache_key

        if transaction.decision == "allow" and same_target:
            reason = f"allowed by active {transaction.framework} {transaction.action} transaction"
            self._terminal_decision("ALLOW", context, classification, reason, color="blue")
            return CDPPolicyDecision(
                allow=True,
                metadata=metadata,
                replacement_payload=classification.replacement_payload,
            )

        reason = _action_transaction_block_reason(transaction, classification, same_target=same_target)
        self._terminal_decision("BLOCK", context, classification, reason, color="red")
        return CDPPolicyDecision(
            allow=False,
            reason=self._blocked_reason(reason, classification),
            metadata=metadata,
        )

    def _active_action_transaction(self, classification: ActionClassification) -> ActionTransaction | None:
        self._prune_action_transactions()
        if not self._action_transactions:
            return None

        action_kind = _transaction_cdp_kind(classification)
        if action_kind is None:
            return None

        for transaction in reversed(self._action_transactions):
            if _transaction_kind_applies(transaction, action_kind):
                return transaction
        return None

    def _mark_action_transaction_connection_confirmed(
        self,
        transaction: ActionTransaction,
        context: CDPMessageContext,
    ) -> None:
        if not context.connection_id:
            return
        self._action_transaction_confirmed_connections.setdefault(transaction.transaction_id, set()).add(context.connection_id)

    def _can_inherit_action_transaction_target(
        self,
        transaction: ActionTransaction,
        context: CDPMessageContext,
        classification: ActionClassification,
        action_kind: str,
    ) -> bool:
        if transaction.decision != "allow" or transaction.action != "form_input":
            return False
        confirmed_connections = self._action_transaction_confirmed_connections.get(transaction.transaction_id, set())
        if classification.target is None:
            if action_kind not in {"focus", "text", "text_helper_js"}:
                return False
            if bool(context.connection_id and context.connection_id in confirmed_connections):
                return True
            return _can_bootstrap_adapter_text_transaction(transaction, classification, action_kind)
        if not bool(context.connection_id and context.connection_id in confirmed_connections):
            return False
        if action_kind != "text":
            return False
        return bool(
            _transaction_observed_target_is_frame_boundary(transaction, classification.target)
            or _browser_use_transaction_observed_plain_document_target(transaction, classification, action_kind)
        )

    def _specific_skill_transaction_backstop(
        self,
        transaction: ActionTransaction,
        classification: ActionClassification,
        action_kind: str,
    ) -> tuple[str, str, CedarDecision] | None:
        if transaction.decision != "allow" or transaction.review_cache_key:
            return None
        if not self.config.specific_skills or classification.target is None:
            return None
        skill_action = _skill_action_for_transaction_followup(transaction, classification, action_kind)
        if skill_action is None:
            return None
        skill_classification = replace(
            classification,
            action=skill_action,
            read_only=False,
            meaningful=True,
        )
        cedar_decision = self.decision_engine.decide_specific_skills(
            facts=_event_facts_from_classification(skill_classification),
            skills=self.config.specific_skills,
        )
        if cedar_decision.source != "specific_skill" or cedar_decision.decision not in {"block", "ask"}:
            return None
        return cedar_decision.decision, cedar_decision.reason, cedar_decision

    def _get_audit_pipeline(self) -> "AuditPipeline | None":
        if self._audit_pipeline is None and not self._audit_pipeline_failed:
            try:
                from .audit import AuditPipeline

                self._audit_pipeline = AuditPipeline.from_config(self.config)
            except Exception as exc:  # noqa: BLE001 — audit must never block the guard
                self._audit_pipeline_failed = True
                print(f"[Acira Audit] pipeline init failed: {exc!r}", file=sys.stderr, flush=True)
        return self._audit_pipeline

    @property
    def audit_pipeline(self) -> "AuditPipeline | None":
        """The audit pipeline for this guard (built lazily). Subscribe to ``.bus`` to receive
        every guard_decision event; read ``.ring_buffer`` for live history."""
        return self._get_audit_pipeline()

    async def decide(self, payload: str | bytes, context: CDPMessageContext) -> CDPPolicyDecision:
        # Single audit choke point: every AGENT_TO_BROWSER decision emits exactly one event.
        if context.direction != AGENT_TO_BROWSER:
            return await self._decide_inner(payload, context)
        audit = self._get_audit_pipeline()
        if audit is None:
            return await self._decide_inner(payload, context)
        start = time.monotonic()
        decision = await self._decide_inner(payload, context)
        try:
            audit.record_decision(
                method=context.method,
                decision=decision,
                latency_ms=(time.monotonic() - start) * 1000.0,
            )
        except Exception as exc:  # noqa: BLE001 — audit must never break the guard
            print(f"[Acira Audit] record_decision failed: {exc!r}", file=sys.stderr, flush=True)
        return decision

    async def _decide_inner(self, payload: str | bytes, context: CDPMessageContext) -> CDPPolicyDecision:
        if context.direction == BROWSER_TO_AGENT:
            if self.config.enabled:
                self._note_preflight_browser_event(context)
            pending_secret_field_read = self._pop_pending_secret_field_read(context)
            redacted_payload = redact_payload(
                payload,
                redact_secret_fields=pending_secret_field_read is not None,
                redact_generic=False,
            )
            return CDPPolicyDecision(
                allow=True,
                replacement_payload=redacted_payload if redacted_payload != payload else None,
            )

        if context.direction != AGENT_TO_BROWSER:
            return CDPPolicyDecision(allow=True)

        lockdown_decision = self._security_lockdown_decision(context)
        if lockdown_decision is not None:
            return lockdown_decision

        if not isinstance(context.parsed_message, dict) or not context.method:
            if contains_known_secret(payload):
                return self._known_secret_payload_block(context, None)
            if not self.config.enabled:
                return CDPPolicyDecision(allow=True)
            return CDPPolicyDecision(allow=True)

        classification = await self.classify(payload, context)
        decision = await self.decide_classification(payload, context, classification)
        if decision.allow:
            self._remember_pending_secret_field_read(context, classification)
        return decision

    def _remember_pending_secret_field_read(
        self,
        context: CDPMessageContext,
        classification: ActionClassification,
    ) -> None:
        key = _secret_field_read_key(context)
        if key is None or classification.method not in {"Runtime.evaluate", "Runtime.callFunctionOn"}:
            return
        parsed = context.parsed_message if isinstance(context.parsed_message, dict) else {}
        params = parsed.get("params") if isinstance(parsed.get("params"), dict) else {}
        source_field = "expression" if classification.method == "Runtime.evaluate" else "functionDeclaration"
        source = params.get(source_field)
        if not isinstance(source, str):
            return
        match = SECRET_REDACTION_REGISTRY.secret_field_read_match(source)
        if match is None:
            return
        self._prune_pending_secret_field_reads()
        self._pending_secret_field_reads[key] = PendingSecretFieldRead(
            secret_id=str(match.get("secret_id") or ""),
            label=str(match.get("label") or "secret field"),
            target=dict(match.get("target") if isinstance(match.get("target"), dict) else {}),
            expires_at=time.monotonic() + 30.0,
        )

    def _pop_pending_secret_field_read(self, context: CDPMessageContext) -> PendingSecretFieldRead | None:
        self._prune_pending_secret_field_reads()
        key = _secret_field_read_key(context)
        if key is None:
            return None
        return self._pending_secret_field_reads.pop(key, None)

    def _prune_pending_secret_field_reads(self) -> None:
        now = time.monotonic()
        for key, value in list(self._pending_secret_field_reads.items()):
            if value.expires_at <= now:
                self._pending_secret_field_reads.pop(key, None)

    async def decide_classification(
        self,
        payload: str | bytes,
        context: CDPMessageContext,
        classification: ActionClassification,
    ) -> CDPPolicyDecision:
        if context.direction != AGENT_TO_BROWSER:
            return CDPPolicyDecision(allow=True)

        if self.before_decision is not None:
            try:
                result = self.before_decision()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                return CDPPolicyDecision(allow=False, reason="Policy configuration or required local recording is unavailable; repair it before continuing.", metadata={"decision": "block_invalid_configuration", "security_lockdown": True})

        lockdown_decision = self._security_lockdown_decision(context, classification)
        if lockdown_decision is not None:
            return lockdown_decision

        # A prior click/text transaction must never implicitly authorize a
        # native confirmation or prompt, which can dispatch another operation.
        if self.config.enabled and classification.action == "dialog_confirmation":
            reason = classification.reason
            self._terminal_decision("BLOCK", context, classification, reason, color="red")
            return CDPPolicyDecision(allow=False, reason=self._blocked_reason(reason, classification),
                metadata={"guard": "acira", "decision": "block_uncontracted_confirmation",
                          "policy_decision": "block", "classification": classification.to_metadata()})

        action_transaction_decision = self._action_transaction_policy_decision(context, classification)
        if action_transaction_decision is not None:
            if (
                action_transaction_decision.allow
                and contains_known_secret(payload)
                and not _decision_allows_secret_payload(action_transaction_decision)
            ):
                return self._known_secret_payload_block(context, classification)
            return action_transaction_decision

        if contains_known_secret(payload):
            return self._known_secret_payload_block(context, classification)

        if not self.config.enabled:
            return CDPPolicyDecision(allow=True)

        blocked_input_transaction = self._blocked_input_transaction_block(context, classification)
        if blocked_input_transaction is not None:
            action_decision = "block"
            reason = blocked_input_transaction["reason"]
            rule = None
            cedar_decision = None
            details = dict(classification.details or {})
            details["blocked_input_transaction"] = blocked_input_transaction
            classification = replace(classification, action="form_input", reason="blocked text fallback", meaningful=True, details=details)
        else:
            action_decision, reason, rule, cedar_decision = self._configured_decision(
                classification,
                context,
                include_general=False,
            )

        if classification.action == "navigation":
            self._note_preflight_navigation(context, classification)

        if action_decision != "block":
            # A page denial cannot be overridden by an action-specific review.
            # A page review is acknowledged separately before reevaluating the
            # original action, including its captured or core review requirement.
            page_decision = await self._preflight_gate_decision(context, classification)
            if page_decision is not None:
                if page_decision.decision in {"block", "ask"}:
                    action_decision = page_decision.decision
                    reason = page_decision.reason
                    rule = None
                    cedar_decision = page_decision

        if action_decision not in {"block", "ask"}:
            general_decision = self._general_skill_decision(classification, context)
            if general_decision is not None and general_decision.decision in {"block", "ask"}:
                action_decision = general_decision.decision
                reason = general_decision.reason
                rule = None
                cedar_decision = general_decision
            elif general_decision is not None and general_decision.decision == "allow":
                cedar_decision = general_decision

        if (
            action_decision in {"block", "ask"}
            and cedar_decision is not None
            and cedar_decision.source == "specific_skill"
            and classification.action in TARGET_AWARE_RECONCILE_ACTIONS
            and classification.target is not None
        ):
            reconciled = await self._reconcile_page_wide_skill_match(classification, context, cedar_decision)
            if reconciled is not None and reconciled.decision == "allow":
                action_decision = "allow"
                reason = reconciled.reason
                rule = None
                cedar_decision = reconciled

        misdirected = None
        if action_decision != "block":
            misdirected = self._misdirected_text_input_block(context, classification)
            if misdirected is not None:
                action_decision = "block"
                reason = misdirected["reason"]
                rule = None
                cedar_decision = None
                details = dict(classification.details or {})
                details["misdirected_from_blocked_target"] = misdirected
                classification = replace(classification, details=details)

        if (
            action_decision == "ask"
            and cedar_decision is not None
            and cedar_decision.source == "specific_skill_missing_target"
            and classification.action in TARGET_AWARE_RECONCILE_ACTIONS
        ):
            missing_resolution = _first_missing_resolution(cedar_decision)
            page_targets = self._page_targets_from_classification_details(classification)
            if page_targets is None and not _kind_changed_resolution_requires_review(missing_resolution):
                page_targets = await self._probe_page_targets(context)
            missing_target_decision = await self._page_missing_target_decision(
                classification,
                context,
                page_targets=page_targets,
            )
            if missing_target_decision is not None:
                action_decision = missing_target_decision.decision
                reason = missing_target_decision.reason
                rule = None
                cedar_decision = missing_target_decision
        if action_decision == "allow":
            missing_target_decision = await self._page_missing_target_decision(classification, context)
            if missing_target_decision is not None:
                action_decision = missing_target_decision.decision
                reason = missing_target_decision.reason
                rule = None
                cedar_decision = missing_target_decision
        page_acknowledgement = cedar_decision if (
            action_decision == "ask" and cedar_decision is not None
            and cedar_decision.source == "general_skill_preflight") else None
        preflight_followup = classification
        # Reconciliation can refine a captured target, but it must never erase
        # another policy's block or required request evidence. Apply the same
        # final combination used for frozen replay facts.
        action_decision, reason, rule, cedar_decision = self._combine_general_decision(
            classification, context, action_decision, reason, rule, cedar_decision)
        if (action_decision == "ask" and page_acknowledgement is not None
                and cedar_decision is not None):
            combined_details = {**(classification.details or {}), "cedar": cedar_decision.metadata or {}}
            combined_review = replace(classification, details=combined_details)
            if self._general_review_missing_evidence(combined_review) is None:
                # A page acknowledgement is distinct from the proposed operation.
                # Cache only the original page scope, then reevaluate the action
                # through the existing continuation and its fresh review boundary.
                page_details = dict(classification.details or {})
                page_details.pop("operation_evidence", None)
                page_details["cedar"] = page_acknowledgement.metadata or {}
                page_details["cedar_source"] = page_acknowledgement.source
                classification = replace(classification, action="page_preflight", target=None,
                    reason=page_acknowledgement.reason, details=page_details)
                cedar_decision = page_acknowledgement
                reason = page_acknowledgement.reason
        metadata = {
            "guard": "acira",
            "decision": action_decision,
            "policy_decision": action_decision,
            "rule_id": rule.id if rule else None,
            "source": cedar_decision.source if cedar_decision else None,
            "cedar": cedar_decision.metadata if cedar_decision else None,
            "classification": classification.to_metadata(),
        }
        if cedar_decision and cedar_decision.skill_decision_id:
            metadata["skill_decision_id"] = cedar_decision.skill_decision_id

        if action_decision == "allow":
            self._clear_recent_blocked_form_target_if_explicit_focus(context, classification)
            self._terminal_decision("ALLOW", context, classification, reason, color="blue", cedar_decision=cedar_decision)
            return CDPPolicyDecision(allow=True, metadata=metadata, replacement_payload=classification.replacement_payload)

        if action_decision == "ask":
            review_key = _review_cache_key(classification, cedar_decision)
            reusable_review = _review_result_is_reusable(classification, cedar_decision)
            if cedar_decision is not None and cedar_decision.metadata:
                details = dict(classification.details or {})
                # Only the current policy decision owns review requirements;
                # a previous page review or caller's details cannot mask them.
                details["cedar"] = cedar_decision.metadata
                details["cedar_source"] = cedar_decision.source
                if cedar_decision.skill_decision_id:
                    details["skill_decision_id"] = cedar_decision.skill_decision_id
                else:
                    details.pop("skill_decision_id", None)
                classification = replace(classification, details=details)
            if reusable_review and review_key in self._review_approved_keys:
                mapped_block_reason = self._mapped_block_reason_after_review_approval(classification, cedar_decision)
                metadata["decision"] = "allow_after_cached_review"
                metadata["review_result"] = "allow_cached"
                metadata["review_cache_key"] = review_key
                metadata["review_reusable"] = True
                if mapped_block_reason is not None:
                    metadata["decision"] = "block_after_cached_mapping_review"
                    action_decision = "block"
                    reason = mapped_block_reason
                elif cedar_decision is not None and cedar_decision.source == "general_skill_preflight":
                    metadata["decision"] = "continue_after_cached_preflight_review"
                    return await self._continue_after_preflight_review_approval(
                        payload,
                        context,
                        preflight_followup,
                        metadata,
                    )
                else:
                    self._begin_cdp_review_transaction(context, classification, allow=True, reason="approved by earlier human review", metadata=metadata)
                    self._terminal_decision(
                        "ALLOW",
                        context,
                        classification,
                        "approved by earlier human review",
                        color="blue",
                        cedar_decision=cedar_decision,
                    )
                    return CDPPolicyDecision(
                        allow=True,
                        metadata=metadata,
                        replacement_payload=classification.replacement_payload,
                    )

            if action_decision == "block":
                pass
            elif reusable_review and review_key in self._review_denied_keys:
                metadata["decision"] = "block_after_cached_review"
                metadata["review_result"] = "block_cached"
                metadata["review_cache_key"] = review_key
            else:
                if self.review_pause.active:
                    metadata["decision"] = "block_during_pending_review"
                    metadata["review_result"] = "pending_review_blocked"
                    metadata["review_cache_key"] = review_key
                    reason = "Another human review is already pending; this protected command was not forwarded."
                else:
                    self._terminal_decision("HITL", context, classification, reason, color="yellow", cedar_decision=cedar_decision)
                    allowed = await self._ask(payload, context, classification, reason)
                    metadata["review_result"] = "allow" if allowed else "block"
                    metadata["review_cache_key"] = review_key
                    metadata["review_reusable"] = reusable_review
                    if allowed:
                        if reusable_review:
                            self._review_approved_keys.add(review_key)
                        self._persist_mapping_review_records(classification, cedar_decision, status="approved")
                        mapping_session = self._remember_mapping_review_session(
                            classification,
                            cedar_decision,
                            approved=True,
                            review_key=review_key,
                        )
                        if mapping_session is not None:
                            metadata["page_mapping_session"] = mapping_session.to_metadata()
                        empty_mapping_key = self._remember_approved_empty_mapping_review(classification, cedar_decision)
                        if empty_mapping_key:
                            metadata["approved_empty_mapping_key"] = empty_mapping_key
                        mapped_block_reason = self._mapped_block_reason_after_review_approval(classification, cedar_decision)
                        if mapped_block_reason is not None:
                            metadata["decision"] = "block_after_mapping_review"
                            metadata["mapping_review_result"] = "approved"
                            action_decision = "block"
                            reason = mapped_block_reason
                        elif cedar_decision is not None and cedar_decision.source == "general_skill_preflight":
                            metadata["decision"] = "continue_after_preflight_review"
                            return await self._continue_after_preflight_review_approval(
                                payload,
                                context,
                                preflight_followup,
                                metadata,
                            )
                        else:
                            approval_reason = (
                                "approved by human review"
                                if reusable_review
                                else "approved for current action"
                            )
                            metadata["decision"] = (
                                "allow_after_review"
                                if reusable_review
                                else "allow_after_current_action_review"
                            )
                            self._begin_cdp_review_transaction(context, classification, allow=True, reason=approval_reason, metadata=metadata)
                            self._terminal_decision(
                                "ALLOW",
                                context,
                                classification,
                                approval_reason,
                                color="blue",
                                cedar_decision=cedar_decision,
                            )
                            return CDPPolicyDecision(
                                allow=True,
                                metadata=metadata,
                                replacement_payload=classification.replacement_payload,
                            )
                    else:
                        self._persist_mapping_review_records(classification, cedar_decision, status="rejected")
                        if reusable_review:
                            self._review_denied_keys.add(review_key)
                        self._quarantine_page_after_rejected_preflight(cedar_decision)
                        mapping_session = self._remember_mapping_review_session(
                            classification,
                            cedar_decision,
                            approved=False,
                            review_key=review_key,
                        )
                        if mapping_session is not None:
                            metadata["page_mapping_session"] = mapping_session.to_metadata()
                        lockdown = self._activate_security_lockdown(cedar_decision, review_key=review_key)
                        if lockdown is not None:
                            metadata["security_lockdown"] = lockdown.to_metadata()
                            reason = ACIRA_SECURITY_LOCKDOWN_REASON
                        metadata["decision"] = "block_after_review"

        if action_decision == "ask" and metadata["decision"] == "block_after_cached_review":
            reason = "Denied by earlier human review for this same protected target/action."

        blocked_input_transaction_active = _classification_has_blocked_input_transaction(classification)
        self._terminal_submit_block_message(context, classification)
        self._terminal_decision("BLOCK", context, classification, reason, color="red", cedar_decision=cedar_decision)
        self._begin_cdp_review_transaction(context, classification, allow=False, reason=reason, metadata=metadata)
        if not blocked_input_transaction_active:
            self._record_recent_blocked_form_target(context, classification, cedar_decision, reason)
        retry_state = self._record_blocked_attempt(classification, cedar_decision)
        metadata["blocked_retry"] = retry_state.to_metadata()
        blocked_reason = (
            ACIRA_SECURITY_LOCKDOWN_REASON
            if reason == ACIRA_SECURITY_LOCKDOWN_REASON
            else self._blocked_reason(reason, classification, cedar_decision=cedar_decision)
        )
        if reason != ACIRA_SECURITY_LOCKDOWN_REASON and retry_state.count > 1 and not blocked_input_transaction_active:
            blocked_reason = _repeated_block_reason(classification, retry_state)
        return CDPPolicyDecision(
            allow=False,
            reason=blocked_reason,
            metadata=metadata,
        )

    def record_framework_boundary_block(
        self,
        payload: str | bytes,
        context: CDPMessageContext,
        classification: ActionClassification,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Log a framework-normalized action blocked before browser side effects."""

        self._terminal_decision("BLOCK", context, classification, reason, color="red")

    async def _continue_after_preflight_review_approval(
        self,
        payload: str | bytes,
        context: CDPMessageContext,
        classification: ActionClassification,
        preflight_review_metadata: dict[str, Any],
    ) -> CDPPolicyDecision:
        followup = await self.decide_classification(payload, context, classification)
        metadata = dict(followup.metadata or {})
        metadata["preflight_review"] = {
            "decision": preflight_review_metadata.get("decision"),
            "review_result": preflight_review_metadata.get("review_result"),
            "review_cache_key": preflight_review_metadata.get("review_cache_key"),
            "skill_decision_id": preflight_review_metadata.get("skill_decision_id"),
        }
        return CDPPolicyDecision(
            allow=followup.allow,
            reason=followup.reason,
            replacement_payload=followup.replacement_payload,
            metadata=metadata,
        )

    def _record_blocked_attempt(
        self,
        classification: ActionClassification,
        cedar_decision: CedarDecision | None,
    ) -> BlockedRetryState:
        key = _blocked_retry_key(classification, cedar_decision)
        count = self._blocked_attempts.get(key, 0) + 1
        self._blocked_attempts[key] = count
        retry_policy = self.config.blocked_retry_policy
        exceeded = count > retry_policy.same_target_limit
        return BlockedRetryState(
            key=key,
            count=count,
            limit=retry_policy.same_target_limit,
            on_exceed=retry_policy.on_exceed,
            exceeded=exceeded,
        )

    def _begin_cdp_review_transaction(
        self,
        context: CDPMessageContext,
        classification: ActionClassification,
        *,
        allow: bool,
        reason: str,
        metadata: dict[str, Any],
    ) -> None:
        if allow:
            # A raw command approval has no declared multi-event operation scope.
            # Framework transactions are opened and closed by their executor.
            return
        if context.websocket_path.startswith("/acira/action/"):
            return
        if classification.target is None:
            return
        if isinstance(classification.details, dict) and isinstance(classification.details.get("action_transaction"), dict):
            return
        action = _transaction_action_from_classification(classification)
        if action is None:
            return
        self.begin_action_transaction(
            framework="cdp",
            action=action,
            target=classification.target,
            allow=allow,
            reason=reason,
            metadata=metadata,
            duration=8.0 if allow else 20.0,
        )

    def _record_recent_blocked_form_target(
        self,
        context: CDPMessageContext,
        classification: ActionClassification,
        cedar_decision: CedarDecision | None,
        reason: str,
    ) -> None:
        if (
            classification.action not in {"focus", "form_input"}
            or classification.target is None
            or cedar_decision is None
            or cedar_decision.source not in SPECIFIC_SKILL_MAPPING_SOURCES
        ):
            return
        self._prune_recent_blocked_form_targets()
        target_key = _target_identity_key(classification.target)
        connection_id = "*" if context.websocket_path.startswith("/acira/action/") else context.connection_id
        self._recent_blocked_form_targets = [
            item
            for item in self._recent_blocked_form_targets
            if not (item.connection_id == connection_id and item.target_key == target_key)
        ]
        self._recent_blocked_form_targets.append(
            RecentBlockedFormTarget(
                connection_id=connection_id,
                target_key=target_key,
                target_text=_agent_target_reference(classification),
                reason=reason,
                expires_at=time.monotonic() + 15.0,
                source_action=classification.action,
                target_metadata=classification.target.to_metadata(),
            )
        )

    def _record_recent_blocked_target_key(
        self,
        *,
        connection_id: str,
        target_key: str,
        target_text: str,
        reason: str,
        source_action: str,
        duration: float = 15.0,
        target_metadata: dict[str, Any] | None = None,
    ) -> None:
        self._prune_recent_blocked_form_targets()
        self._recent_blocked_form_targets = [
            item
            for item in self._recent_blocked_form_targets
            if not (item.connection_id == connection_id and item.target_key == target_key)
        ]
        self._recent_blocked_form_targets.append(
            RecentBlockedFormTarget(
                connection_id=connection_id,
                target_key=target_key,
                target_text=target_text,
                reason=reason,
                expires_at=time.monotonic() + duration,
                source_action=source_action,
                target_metadata=dict(target_metadata or {}),
            )
        )

    def _blocked_input_transaction_block(
        self,
        context: CDPMessageContext,
        classification: ActionClassification,
    ) -> dict[str, Any] | None:
        """Fail closed for keyboard/text fallbacks after a protected focus/input was blocked.

        CDP text input is delivered to the browser's current active element. If
        Acira blocks focus on a protected field, framework fallbacks can still
        emit key events that land in body or a previous field. Treat the
        original blocked focus/input as an atomic failed text transaction until
        a different explicit focus succeeds.
        """

        if classification.method not in {
            "Input.insertText",
            "Input.dispatchKeyEvent",
            "Input.imeSetComposition",
        }:
            return None
        self._prune_recent_blocked_form_targets()
        recent = next(
            (
                item
                for item in reversed(self._recent_blocked_form_targets)
                if _recent_blocked_form_target_applies(item, context)
                and _recent_blocked_form_target_should_intercept(item, classification)
            ),
            None,
        )
        if recent is None:
            return None

        current_text = _agent_target_reference(classification)
        return {
            "blocked_target": recent.target_text,
            "current_target": current_text,
            "source_action": recent.source_action,
            "reason": (
                f"Acira previously blocked focus/input for {recent.target_text}. This keyboard/text fallback "
                f"could land in {current_text}, so Acira blocked it before Chrome received the command."
            ),
        }

    def _misdirected_text_input_block(
        self,
        context: CDPMessageContext,
        classification: ActionClassification,
    ) -> dict[str, Any] | None:
        if classification.action != "form_input" or classification.method not in {
            "Input.insertText",
            "Input.dispatchKeyEvent",
            "Input.imeSetComposition",
        }:
            return None
        self._prune_recent_blocked_form_targets()
        recent = next(
            (
                item
                for item in reversed(self._recent_blocked_form_targets)
                if _recent_blocked_form_target_applies(item, context)
                and _recent_blocked_form_target_should_intercept(item, classification)
            ),
            None,
        )
        if recent is None:
            return None
        current_key = _target_identity_key(classification.target) if classification.target is not None else ""
        if current_key and current_key == recent.target_key:
            return None
        current_text = _agent_target_reference(classification)
        return {
            "blocked_target": recent.target_text,
            "current_target": current_text,
            "reason": (
                f"Acira previously blocked focus/input for {recent.target_text}. This text command would land in "
                f"{current_text} instead, so Acira blocked it to prevent misdirected data entry."
            ),
        }

    def _clear_recent_blocked_form_target_if_explicit_focus(
        self,
        context: CDPMessageContext,
        classification: ActionClassification,
    ) -> None:
        if classification.action != "focus" or classification.target is None:
            return
        self._prune_recent_blocked_form_targets()
        current_key = _target_identity_key(classification.target)
        self._recent_blocked_form_targets = [
            item
            for item in self._recent_blocked_form_targets
            if not _recent_blocked_form_target_applies(item, context) or item.target_key == current_key
        ]

    def _prune_recent_blocked_form_targets(self) -> None:
        now = time.monotonic()
        self._recent_blocked_form_targets = [
            item for item in self._recent_blocked_form_targets if item.expires_at > now
        ][-24:]

    async def classify(self, payload: str | bytes, context: CDPMessageContext) -> ActionClassification:
        method = context.method or "unknown"
        parsed = context.parsed_message if isinstance(context.parsed_message, dict) else {}
        params = parsed.get("params") if isinstance(parsed.get("params"), dict) else {}

        if method in SAFE_SETUP_METHODS:
            return ActionClassification(method=method, action="safe_cdp", reason="safe protocol setup", read_only=True)

        if method in READ_ONLY_METHODS or method.startswith(READ_ONLY_PREFIXES):
            return ActionClassification(method=method, action="read_only_cdp", reason="read-only CDP method", read_only=True)

        if method == "Page.handleJavaScriptDialog":
            if params.get("accept") is False and not params.get("promptText"):
                return ActionClassification(method=method, action="dialog_dismissal",
                    reason="dismiss a native browser dialog without confirming it", read_only=True, confidence=1.0)
            return ActionClassification(method=method, action="dialog_confirmation",
                reason="Native dialog acceptance has no verified operation contract; dismiss the dialog instead",
                meaningful=True, confidence=1.0)

        if method in {"Runtime.evaluate", "Runtime.callFunctionOn"}:
            return await self._classify_runtime(method, params, context)

        if method == "Input.insertText":
            target = await self._probe_active_element(context)
            return ActionClassification(
                method=method,
                action="form_input",
                reason="text insertion",
                confidence=max(0.82, target.confidence if target else 0.0),
                meaningful=True,
                target=target,
            )

        if method == "Input.imeSetComposition":
            target = await self._probe_active_element(context)
            return ActionClassification(
                method=method,
                action="form_input",
                reason="IME composition updates text",
                confidence=0.82,
                meaningful=True,
                target=target,
            )

        if method == "Input.dispatchKeyEvent":
            return await self._classify_key_event(method, params, context)

        if method == "Input.dispatchMouseEvent":
            return await self._classify_pointer_event(method, params, context, pointer_kind="mouse")

        if method == "Input.dispatchTouchEvent":
            return await self._classify_pointer_event(method, params, context, pointer_kind="touch")

        if method == "DOM.focus":
            target = await self._probe_dom_target(params, context)
            return ActionClassification(
                method=method,
                action="focus",
                reason="DOM focus command",
                confidence=max(0.85, target.confidence if target else 0.0),
                meaningful=True,
                target=target,
            )

        if method in DOM_MUTATION_METHODS:
            target = await self._probe_dom_target(params, context)
            action = "form_input" if target and target.is_form_control else "dom_mutation"
            return ActionClassification(
                method=method,
                action=action,
                reason="DOM mutation command",
                confidence=0.9,
                meaningful=True,
                target=target,
            )

        if method in {"Page.navigate", "Target.createTarget"}:
            return ActionClassification(method=method, action="navigation", reason="page navigation", meaningful=True,
                                        details={"url": params.get("url", "")})

        if method.startswith("Input.") or method.startswith("DOM.") or method.startswith("Page."):
            return ActionClassification(
                method=method,
                action="meaningful_cdp",
                reason="meaningful browser command",
                confidence=0.55,
                meaningful=True,
            )

        return ActionClassification(method=method, action="unknown_cdp", reason="unclassified CDP method", confidence=0.45)

    def _configured_decision(
        self,
        classification: ActionClassification,
        context: CDPMessageContext | None = None,
        *,
        include_general: bool = True,
        recorded_specific_facts: dict[str, Any] | None = None,
        recorded_general_facts: dict[str, Any] | None = None,
    ) -> tuple[str, str, AciraGuardRule | None, CedarDecision | None]:
        if include_general:
            # Match the live boundary's order: explicit/specific blocks and
            # reviews take precedence, then evaluate general action facts.
            decision, reason, rule, cedar = self._configured_decision(classification, context,
                include_general=False, recorded_specific_facts=recorded_specific_facts)
            return self._combine_general_decision(classification, context,
                decision, reason, rule, cedar, recorded_facts=recorded_general_facts)
        if classification.action == "dialog_confirmation":
            return "block", classification.reason, None, None
        if getattr(self, "require_bound_review", False) and classification.action in {"unknown_cdp", "meaningful_cdp"}:
            return "block", "Unclassified CDP mutation has no supported action contract", None, None
        if classification.action == "navigation":
            destination = str((classification.details or {}).get("url", ""))
            if urlparse(destination).scheme.lower() in {"javascript", "data", "vbscript", "blob"}:
                return "block", "Executable or opaque navigation URLs have no supported operation contract", None, None
        if classification.action == "dangerous_js":
            return "block", classification.reason, None, self._general_skill_decision(classification, context, recorded_facts=recorded_general_facts)

        if classification.action in HELPER_INSPECTION_ACTIONS:
            return "allow", classification.reason, None, None

        # An explicit denial must not be replaced by a secret/captured-target
        # review. No reviewer can override a matching configured block.
        rules = self.config.matching_rules(classification.action)
        blocked_rule = next((rule for rule in rules if rule.decision == "block"), None)
        if blocked_rule is not None:
            return "block", blocked_rule.reason or classification.reason, blocked_rule, None

        secret_decision = self._secret_policy_decision(classification)
        if secret_decision is not None and secret_decision.decision == "block":
            return secret_decision.decision, secret_decision.reason, None, secret_decision

        cedar_decision = None
        if _should_evaluate_specific_skill_targets(classification):
            cedar_decision = self.decision_engine.decide_specific_skills(
                facts=recorded_specific_facts if recorded_specific_facts is not None else _event_facts_from_classification(classification),
                skills=self.config.specific_skills,
            )
            if cedar_decision.decision == "block":
                return cedar_decision.decision, cedar_decision.reason, None, cedar_decision

        if secret_decision is not None and secret_decision.decision == "ask":
            return secret_decision.decision, secret_decision.reason, None, secret_decision
        if secret_decision is not None and cedar_decision is None:
            cedar_decision = secret_decision

        if cedar_decision is not None and cedar_decision.decision == "ask":
            return cedar_decision.decision, cedar_decision.reason, None, cedar_decision

        rules = self.config.matching_rules(classification.action)
        for decision in ("block", "ask", "allow"):
            matching = next((rule for rule in rules if rule.decision == decision), None)
            if matching is not None:
                return decision, matching.reason or classification.reason, matching, cedar_decision

        if classification.action == "unknown_js":
            if self.config.runtime_js_unknown == "ask":
                return "ask", classification.reason, None, cedar_decision
            if self.config.runtime_js_unknown == "block":
                return "block", classification.reason, None, cedar_decision
            return "allow", classification.reason, None, cedar_decision

        if classification.action == "arbitrary_js" and self.config.block_arbitrary_js:
            return "block", "Arbitrary agent JavaScript is blocked by Acira Guard.", None, cedar_decision

        if classification.read_only:
            return "allow", classification.reason, None, cedar_decision

        if (
            self.config.ask_low_confidence_meaningful_actions
            and classification.meaningful
            and classification.confidence < 0.65
        ):
            return "ask", "Low-confidence meaningful browser action requires review.", None, cedar_decision

        return "allow", classification.reason, None, cedar_decision

    def _combine_general_decision(
        self, classification: ActionClassification, context: CDPMessageContext | None,
        decision: str, reason: str, rule: AciraGuardRule | None,
        cedar: CedarDecision | None, *, recorded_facts: dict[str, Any] | None = None,
    ) -> tuple[str, str, AciraGuardRule | None, CedarDecision | None]:
        if decision == "block":
            return decision, reason, rule, cedar
        general = self._general_skill_decision(classification, context, recorded_facts=recorded_facts)
        if general is None:
            return decision, reason, rule, cedar
        combined = {**(cedar.metadata or {}), **(general.metadata or {})} if cedar else general.metadata
        if general.decision == "block":
            return "block", general.reason, None, replace(general, metadata=combined)
        if decision == "ask":
            # Keep captured/secret/mapping identity for existing review code,
            # while carrying every general detection's evidence requirements.
            selected = cedar if cedar is not None else general
            return decision, reason, rule, replace(selected, metadata=combined)
        if general.decision == "ask":
            return "ask", general.reason, None, replace(general, metadata=combined)
        return decision, reason, rule, replace(cedar or general, metadata=combined)

    def _general_skill_decision(
        self,
        classification: ActionClassification,
        context: CDPMessageContext | None = None,
        *, recorded_facts: dict[str, Any] | None = None,
    ) -> CedarDecision | None:
        if not self.config.general_skills:
            return None
        parsed_message = context.parsed_message if context is not None and isinstance(context.parsed_message, dict) else None
        facts = recorded_facts if recorded_facts is not None else build_browser_event_facts(classification, parsed_message).to_dict()
        decision = self.decision_engine.decide_general_skills(
            facts=facts,
            skills=self.config.general_skills,
            skill_policies=self.config.skill_policies,
            default_decision="ask",
        )
        metadata = decision.metadata if isinstance(decision.metadata, dict) else {}
        general = metadata.get("general_skills") if isinstance(metadata.get("general_skills"), dict) else {}
        detections = general.get("detections") if isinstance(general.get("detections"), list) else []
        return decision

    def _secret_policy_decision(self, classification: ActionClassification) -> CedarDecision | None:
        details = classification.details if isinstance(classification.details, dict) else {}
        secret_use = details.get("secret_use") if isinstance(details.get("secret_use"), dict) else None
        if secret_use is None:
            return None
        decision = str(secret_use.get("policy_decision") or self.config.secrets.default_decision).strip().lower()
        if decision not in {"allow", "ask", "block"}:
            decision = "block"
        label = _string_or_none(secret_use.get("label")) or _string_or_none(secret_use.get("id")) or "secret"
        reason = _string_or_none(secret_use.get("policy_reason"))
        if reason is None:
            if decision == "allow":
                reason = f'Acira secret policy allows use of "{label}".'
            elif decision == "ask":
                reason = f'Acira secret policy requires review before using "{label}".'
            else:
                reason = f'Acira secret policy blocks use of "{label}".'
        secret_id = _string_or_none(secret_use.get("id"))
        return CedarDecision(
            decision=decision,
            reason=reason,
            source="secret_policy",
            skill_decision_id=f"secret:{secret_id}" if secret_id else None,
            metadata={
                "engine": "acira_secret_policy",
                "mode": "metadata_only",
                "secret": redact_value(secret_use),
            },
        )

    def _known_secret_payload_block(
        self,
        context: CDPMessageContext,
        classification: ActionClassification | None,
    ) -> CDPPolicyDecision:
        if classification is None:
            classification = ActionClassification(
                method=context.method or "unknown",
                action="form_input",
                reason="known Acira secret outside approved fill transaction",
                confidence=0.95,
                read_only=False,
                meaningful=True,
            )
        else:
            details = dict(classification.details or {})
            details["known_secret_payload_block"] = True
            classification = replace(
                classification,
                reason="known Acira secret outside approved fill transaction",
                meaningful=True,
                details=details,
            )
        reason = (
            "A secret that was released through Acira Secrets Manager appeared in an agent-to-browser CDP payload "
            "outside the active approved fill transaction."
        )
        metadata = {
            "guard": "acira",
            "decision": "block_known_secret_payload",
            "classification": classification.to_metadata(),
        }
        self._terminal_decision("BLOCK", context, classification, reason, color="red")
        return CDPPolicyDecision(
            allow=False,
            reason=self._blocked_reason(reason, classification),
            metadata=metadata,
        )

    def evaluate_page_preflight_targets(
        self,
        page_targets: list[dict[str, Any]] | tuple[dict[str, Any], ...],
        metadata: dict[str, Any] | None = None,
        *,
        scope_key: str = "offline",
        now: float | None = None,
        readiness: dict[str, Any] | None = None,
    ) -> PageRiskSnapshot:
        created_at = time.time() if now is None else now
        probe = dict(metadata or {})
        facts = build_page_preflight_facts(page_targets, probe)
        decision = self.decision_engine.decide_general_skills(
            facts=facts,
            skills=self.config.general_skills,
            skill_policies=self.config.skill_policies,
            default_decision="allow",
        )
        cedar_metadata = decision.metadata if isinstance(decision.metadata, dict) else {}
        return PageRiskSnapshot(
            scope_key=scope_key,
            url=_string_or_none(probe.get("url") or facts.get("url")),
            title=_string_or_none(probe.get("title")),
            page_signature=_string_or_none(probe.get("page_signature")),
            status=str(probe.get("status") or "ok"),
            decision=decision.decision,
            reason=decision.reason,
            skill_decision_id=decision.skill_decision_id,
            facts=facts,
            cedar_metadata=cedar_metadata,
            readiness=dict(readiness or {}),
            created_at=created_at,
            expires_at=time.monotonic() + 30.0,
        )

    def _note_preflight_browser_event(self, context: CDPMessageContext) -> None:
        if not self.config.general_skill_preflight.enabled or not isinstance(context.parsed_message, dict):
            return
        method = context.method or ""
        params = context.parsed_message.get("params") if isinstance(context.parsed_message.get("params"), dict) else {}
        scope_key = _preflight_scope_key(context)
        state = self._preflight_page_states.setdefault(scope_key, PreflightPageState(scope_key=scope_key))
        now = time.monotonic()
        should_schedule = False
        trigger = method or "browser_event"
        if method == "Page.frameNavigated":
            frame = params.get("frame") if isinstance(params.get("frame"), dict) else {}
            url = _string_or_none(frame.get("url"))
            if url:
                self._mark_preflight_pending(scope_key, url=url, reason="frame_navigated", now=now)
                should_schedule = True
                trigger = "frame_navigated"
        elif method == "Page.navigatedWithinDocument":
            url = _string_or_none(params.get("url"))
            if url:
                self._mark_preflight_pending(scope_key, url=url, reason="spa_route_change", now=now)
                should_schedule = True
                trigger = "spa_route_change"
        elif method == "Page.lifecycleEvent":
            name = str(params.get("name") or "").lower()
            if name in {"load", "domcontentloaded", "networkalmostidle", "networkidle"}:
                state.load_event_at = now
                should_schedule = True
                trigger = f"lifecycle_{name}"
        elif method in {"Page.loadEventFired", "Page.domContentEventFired"}:
            state.load_event_at = now
            should_schedule = True
            trigger = method
        elif method == "Network.requestWillBeSent":
            state.in_flight_requests = max(0, state.in_flight_requests + 1)
            state.last_network_event_at = now
        elif method in {"Network.loadingFinished", "Network.loadingFailed"}:
            state.in_flight_requests = max(0, state.in_flight_requests - 1)
            state.last_network_event_at = now
            should_schedule = state.pending and state.in_flight_requests == 0
            trigger = "network_quiet_candidate"

        if should_schedule:
            self._schedule_active_preflight(context, scope_key=scope_key, trigger=trigger)

    def _note_preflight_navigation(self, context: CDPMessageContext, classification: ActionClassification) -> None:
        if not self.config.general_skill_preflight.enabled or not isinstance(context.parsed_message, dict):
            return
        params = context.parsed_message.get("params") if isinstance(context.parsed_message.get("params"), dict) else {}
        url = _string_or_none(params.get("url"))
        if url:
            self._mark_preflight_pending(_preflight_scope_key(context), url=url, reason="page_navigate_command", now=time.monotonic())

    def _mark_preflight_pending(self, scope_key: str, *, url: str | None, reason: str, now: float) -> None:
        state = self._preflight_page_states.setdefault(scope_key, PreflightPageState(scope_key=scope_key))
        normalized_url = _normalize_mapping_url(url)
        if state.last_url != normalized_url or not state.pending:
            state.pending = True
            state.last_url = normalized_url
            state.reason = reason
            state.navigation_started_at = now
            state.load_event_at = None
            state.in_flight_requests = 0
            state.last_snapshot_key = None

    def _schedule_active_preflight(self, context: CDPMessageContext, *, scope_key: str, trigger: str) -> None:
        if not self.config.general_skill_preflight.enabled or not self.config.general_skills:
            return
        if not self.config.has_page_policy or context.send_probe is None:
            return
        existing = self._active_preflight_tasks.get(scope_key)
        if existing is not None and not existing.done():
            return

        task = asyncio.create_task(
            self._run_active_preflight(context, scope_key=scope_key, trigger=trigger),
            name=f"acira-active-page-preflight-{scope_key}",
        )
        self._active_preflight_tasks[scope_key] = task
        self._active_preflight_task_connections[task] = context.connection_id
        task.add_done_callback(lambda completed, key=scope_key: self._finish_active_preflight_task(key, completed))

    def _finish_active_preflight_task(self, scope_key: str, task: asyncio.Task[Any]) -> None:
        self._active_preflight_task_connections.pop(task, None)
        if self._active_preflight_tasks.get(scope_key) is task:
            self._active_preflight_tasks.pop(scope_key, None)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception as exc:
            self._terminal_preflight_status(
                scope_key=scope_key,
                trigger="active_preflight_error",
                status=f"{exc.__class__.__name__}: {exc}",
            )

    async def on_connection_closed(self, connection_id: str) -> None:
        """Retire only active preflights owned by the disconnected CDP client."""
        tasks = tuple(task for task, owner in self._active_preflight_task_connections.items()
                      if owner == connection_id and not task.done())
        for task in tasks:
            task.cancel()
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=1)
        if pending:
            raise RuntimeError("Acira active page preflight did not stop within its one-second shutdown bound")

    async def _run_active_preflight(
        self,
        context: CDPMessageContext,
        *,
        scope_key: str,
        trigger: str,
    ) -> None:
        await asyncio.sleep(0)
        state = self._preflight_page_states.setdefault(scope_key, PreflightPageState(scope_key=scope_key))
        classification = ActionClassification(
            method="Acira.preflight",
            action="page_preflight",
            reason=f"active page preflight after {trigger}",
            confidence=0.99,
            read_only=True,
            meaningful=False,
        )
        snapshot = await self._ensure_preflight_snapshot(context, classification)
        if snapshot is None:
            self._terminal_preflight_status(scope_key=scope_key, trigger=trigger, status="probe_failed")
            return
        if snapshot.decision == "block":
            self._terminal_preflight_snapshot(snapshot, trigger=trigger)
            self._quarantine_preflight_snapshot(snapshot)
            await self._close_preflight_page_if_configured(context, snapshot, reason="page policy block")
        elif snapshot.decision == "ask":
            review_status = self._preflight_review_status(snapshot)
            if review_status is None:
                self._terminal_preflight_snapshot(snapshot, trigger=trigger)
            approved = await self._review_active_preflight_page(context, snapshot, trigger=trigger)
            if approved:
                self._clear_preflight_quarantine(snapshot)
            else:
                self._quarantine_preflight_snapshot(snapshot)
                await self._close_preflight_page_if_configured(context, snapshot, reason="page review rejected")
        else:
            self._terminal_preflight_snapshot(snapshot, trigger=trigger)
            self._clear_preflight_quarantine(snapshot)

    async def enforce_page_preflight_snapshot(
        self,
        snapshot: PageRiskSnapshot,
        context: CDPMessageContext,
        *,
        trigger: str,
    ) -> CDPPolicyDecision:
        self._remember_preflight_snapshot(snapshot)
        cedar_decision = self._preflight_cedar_decision(snapshot)
        classification = ActionClassification(
            method="Acira.preflight",
            action="page_preflight",
            reason=snapshot.reason,
            confidence=0.99,
            read_only=False,
            meaningful=True,
            details={"cedar": cedar_decision.metadata or {}, "cedar_source": cedar_decision.source, "trigger": trigger},
        )
        metadata = {
            "guard": "acira",
            "decision": snapshot.decision,
            "cedar": cedar_decision.metadata,
            "classification": classification.to_metadata(),
        }
        if cedar_decision.skill_decision_id:
            metadata["skill_decision_id"] = cedar_decision.skill_decision_id

        if snapshot.decision == "block":
            self._terminal_preflight_snapshot(snapshot, trigger=trigger)
            self._quarantine_preflight_snapshot(snapshot)
            await self._close_preflight_page_if_configured(context, snapshot, reason="page policy block")
            return CDPPolicyDecision(
                allow=False,
                reason=self._blocked_reason(snapshot.reason, classification, cedar_decision=cedar_decision),
                metadata=metadata,
            )
        if snapshot.decision == "ask":
            review_status = self._preflight_review_status(snapshot)
            review_key = self._preflight_review_cache_key(snapshot)
            if review_status == "approved":
                metadata["review_result"] = "allow_cached"
                metadata["review_cache_key"] = review_key
                metadata["decision"] = "allow_after_cached_page_preflight_review"
                self._note_preflight_review_result(snapshot, allowed=True)
                self._clear_preflight_quarantine(snapshot)
                return CDPPolicyDecision(allow=True, metadata=metadata)
            if review_status == "denied":
                metadata["review_result"] = "block_cached"
                metadata["review_cache_key"] = review_key
                metadata["decision"] = "block_after_cached_page_preflight_review"
                self._note_preflight_review_result(snapshot, allowed=False)
                self._quarantine_preflight_snapshot(snapshot)
                await self._close_preflight_page_if_configured(context, snapshot, reason="page review rejected")
                return CDPPolicyDecision(
                    allow=False,
                    reason=self._blocked_reason("Rejected by page preflight review", classification, cedar_decision=cedar_decision),
                    metadata=metadata,
                )
            if review_status is None:
                self._terminal_preflight_snapshot(snapshot, trigger=trigger)
            approved = await self._review_active_preflight_page(context, snapshot, trigger=trigger)
            metadata["review_result"] = "allow" if approved else "block"
            metadata["review_cache_key"] = review_key
            if approved:
                metadata["decision"] = "allow_after_page_preflight_review"
                self._clear_preflight_quarantine(snapshot)
                return CDPPolicyDecision(allow=True, metadata=metadata)
            metadata["decision"] = "block_after_page_preflight_review"
            self._quarantine_preflight_snapshot(snapshot)
            await self._close_preflight_page_if_configured(context, snapshot, reason="page review rejected")
            return CDPPolicyDecision(
                allow=False,
                reason=self._blocked_reason("Rejected by page preflight review", classification, cedar_decision=cedar_decision),
                metadata=metadata,
            )
        metadata["decision"] = "allow_with_page_preflight"
        self._terminal_preflight_snapshot(snapshot, trigger=trigger)
        self._clear_preflight_quarantine(snapshot)
        return CDPPolicyDecision(allow=True, metadata=metadata)

    async def _preflight_gate_decision(
        self,
        context: CDPMessageContext,
        classification: ActionClassification,
    ) -> CedarDecision | None:
        if not self._should_gate_with_preflight(classification):
            return None
        scope_key = _preflight_scope_key(context)
        cached_decision = self._cached_preflight_gate_decision(context, classification, scope_key=scope_key)
        if cached_decision is not None:
            return cached_decision

        if context.websocket_path.startswith("/acira/action/") and context.send_probe is None:
            return None

        quarantined = self._active_preflight_quarantine(scope_key)
        if quarantined is not None:
            return self._preflight_cedar_decision(
                quarantined,
                decision="block",
                reason=f"Acira page preflight quarantined this page: {quarantined.reason}",
            )

        snapshot = await self._ensure_preflight_snapshot(context, classification)
        if snapshot is None:
            if self.config.general_skill_preflight.on_error == "block_mutating_until_action_probe":
                return CedarDecision(
                    decision="block",
                    reason="Acira could not complete page preflight before this mutating browser action.",
                    source="general_skill_preflight",
                    metadata={
                        "engine": "cedar",
                        "mode": "deterministic_adapter",
                        "preflight": {"status": "probe_failed", "scope_key": scope_key},
                    },
                )
            return None

        if snapshot.decision == "block":
            self._quarantine_preflight_snapshot(snapshot)
            await self._close_preflight_page_if_configured(context, snapshot, reason="page policy block")
        if snapshot.decision in {"block", "ask"}:
            page_decision = self._preflight_cedar_decision(snapshot)
            if snapshot.decision == "ask" and (
                _review_cache_key(classification, page_decision) in self._review_approved_keys
                or self._preflight_review_cache_key(snapshot) in self._review_approved_keys
            ):
                return None
            return page_decision
        return None

    def _should_gate_with_preflight(self, classification: ActionClassification) -> bool:
        details = classification.details if isinstance(classification.details, dict) else {}
        if details.get("_acira_adapter_page_preflight"):
            # The adapter (e.g. Playwright) already enforces page preflight + HITL
            # before this action. The core CDP re-probe is redundant here and cannot
            # run for direct adapters, so skip it and let the per-action policy decide.
            return False
        if not self.config.general_skill_preflight.enabled or not self.config.general_skills:
            return False
        if not self.config.has_page_policy:
            return False
        if classification.read_only or not classification.meaningful:
            return False
        if classification.action in HELPER_INSPECTION_ACTIONS | {"navigation", "safe_cdp", "read_only_cdp"}:
            return False
        if classification.action in {"read_only_js", "trusted_helper_js"}:
            return False
        return True

    async def _ensure_preflight_snapshot(
        self,
        context: CDPMessageContext,
        classification: ActionClassification,
    ) -> PageRiskSnapshot | None:
        scope_key = _preflight_scope_key(context)
        state = self._preflight_page_states.setdefault(scope_key, PreflightPageState(scope_key=scope_key))
        now = time.monotonic()
        current_url = _normalize_mapping_url(classification.target.url if classification.target else None)
        if not current_url:
            current_url = _normalize_mapping_url(await self._probe_page_url(context))
        cached = self._preflight_snapshots.get(scope_key)
        if cached is not None and cached.expires_at > now and not state.pending:
            if not current_url or _normalize_mapping_url(cached.url) == current_url:
                return cached

        targets, probe, readiness = await self._wait_for_preflight_stability(context, state)
        if targets is None or probe is None:
            return None
        snapshot = self.evaluate_page_preflight_targets(
            targets,
            probe,
            scope_key=scope_key,
            readiness=readiness,
        )
        self._remember_preflight_snapshot(snapshot)
        state.pending = False
        state.last_url = _normalize_mapping_url(snapshot.url)
        state.last_snapshot_key = snapshot.page_signature
        if snapshot.decision != "block":
            self._clear_preflight_quarantine(snapshot)
        return snapshot

    async def _wait_for_preflight_stability(
        self,
        context: CDPMessageContext,
        state: PreflightPageState,
    ) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None, dict[str, Any]]:
        config = self.config.general_skill_preflight
        quiet_seconds = max(0.05, config.quiet_ms / 1000.0)
        max_wait = max(0.2, config.max_wait_ms / 1000.0)
        started = time.monotonic()
        last_url: str | None = None
        last_signature: str | None = None
        stable_since: float | None = None
        last_targets: list[dict[str, Any]] | None = None
        last_probe: dict[str, Any] | None = None
        timeline: list[dict[str, Any]] = []
        while time.monotonic() - started <= max_wait:
            targets, probe = await self._probe_page_targets_with_metadata(context)
            now = time.monotonic()
            if targets is None or probe is None:
                timeline.append({"at_ms": round((now - started) * 1000, 1), "status": "probe_failed"})
                await asyncio.sleep(min(0.2, quiet_seconds / 2))
                continue
            url = _normalize_mapping_url(probe.get("url"))
            signature = _string_or_none(probe.get("page_signature"))
            ready_state = str(probe.get("ready_state") or "").lower()
            network_quiet = state.in_flight_requests == 0 and (
                state.last_network_event_at is None or now - state.last_network_event_at >= min(quiet_seconds, 0.5)
            )
            same_page = bool(url and url == last_url and signature and signature == last_signature)
            if same_page:
                stable_since = stable_since or now
            else:
                stable_since = now
                last_url = url
                last_signature = signature
            last_targets = targets
            last_probe = probe
            quiet_for = now - (stable_since or now)
            timeline.append(
                {
                    "at_ms": round((now - started) * 1000, 1),
                    "status": probe.get("status"),
                    "url": url,
                    "ready_state": ready_state,
                    "candidate_count": len(targets),
                    "signature": signature,
                    "quiet_for_ms": round(quiet_for * 1000, 1),
                    "network_quiet": network_quiet,
                }
            )
            if ready_state == "complete" and network_quiet and quiet_for >= quiet_seconds:
                break
            await asyncio.sleep(min(0.2, max(0.05, quiet_seconds / 3)))
        readiness = {
            "strategy": "stability",
            "quiet_ms": config.quiet_ms,
            "max_wait_ms": config.max_wait_ms,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            "timed_out": time.monotonic() - started > max_wait,
            "timeline": timeline[-8:],
        }
        if last_probe is not None:
            last_probe = dict(last_probe)
            last_probe["readiness"] = readiness
        return last_targets, last_probe, readiness

    async def _probe_page_targets_with_metadata(
        self,
        context: CDPMessageContext,
    ) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
        try:
            response = await asyncio.wait_for(
                context.probe(
                    "Runtime.evaluate",
                    {
                        "expression": (
                            "(() => {"
                            f" const targets = ({PAGE_TARGETS_PROBE_JS})();"
                            " return { url: location.href, title: document.title, readyState: document.readyState, targets };"
                            "})()"
                        ),
                        "returnByValue": True,
                        "silent": True,
                    },
                ),
                timeout=max(self.config.probe_timeout, 0.5),
            )
        except Exception:
            return None, None

        value = _response_value(response)
        if not isinstance(value, dict):
            return None, None
        raw_targets = value.get("targets")
        if not isinstance(raw_targets, list):
            return None, None
        targets: list[dict[str, Any]] = []
        for item in raw_targets:
            target = _target_from_probe_value(item)
            if target is None:
                continue
            metadata = target.to_metadata()
            if isinstance(item, dict):
                _merge_probe_extras(metadata, item)
            targets.append(metadata)
        probe = {
            "status": "ok" if targets else "empty",
            "candidate_count": len(targets),
            "url": _string_or_none(value.get("url")),
            "title": _string_or_none(value.get("title")),
            "ready_state": _string_or_none(value.get("readyState") or value.get("ready_state")),
        }
        probe["page_signature"] = _page_target_signature(targets, probe)
        self.remember_page_target_probe(url=probe.get("url"), targets=targets, metadata=probe, ttl_seconds=30.0)
        return targets, probe

    def _preflight_cedar_decision(
        self,
        snapshot: PageRiskSnapshot,
        *,
        decision: str | None = None,
        reason: str | None = None,
    ) -> CedarDecision:
        selected_decision = decision or snapshot.decision
        metadata = dict(snapshot.cedar_metadata)
        metadata["preflight"] = snapshot.to_metadata()
        metadata["page_risk_decision"] = {
            "decision": selected_decision,
            "page_signature": snapshot.page_signature,
            "url": snapshot.url,
        }
        return CedarDecision(
            decision=selected_decision,
            reason=reason or snapshot.reason,
            source="general_skill_preflight",
            skill_decision_id=snapshot.skill_decision_id,
            metadata=metadata,
        )

    def _preflight_review_cache_key(self, snapshot: PageRiskSnapshot) -> str:
        return _review_cache_key(
            ActionClassification(
                method="Acira.preflight",
                action="page_preflight",
                reason=snapshot.reason,
                confidence=0.99,
                read_only=False,
                meaningful=True,
            ),
            self._preflight_cedar_decision(snapshot),
        )

    def _preflight_review_status(self, snapshot: PageRiskSnapshot) -> str | None:
        if snapshot.decision != "ask":
            return None
        review_key = self._preflight_review_cache_key(snapshot)
        if review_key in self._review_approved_keys:
            return "approved"
        if review_key in self._review_denied_keys:
            return "denied"
        if review_key in self._review_inflight:
            return "pending"
        return None

    def _note_preflight_review_result(self, snapshot: PageRiskSnapshot, *, allowed: bool) -> None:
        url_key = _normalize_mapping_url(snapshot.url)
        if not url_key:
            return
        if allowed:
            self._approved_preflight_urls.add(url_key)
            self._denied_preflight_urls.discard(url_key)
        else:
            self._denied_preflight_urls.add(url_key)
            self._approved_preflight_urls.discard(url_key)

    def _preflight_url_was_approved(self, url: str | None) -> bool:
        url_key = _normalize_mapping_url(url)
        return bool(url_key and url_key in self._approved_preflight_urls)

    def _remember_preflight_snapshot(self, snapshot: PageRiskSnapshot) -> None:
        self._preflight_snapshots[snapshot.scope_key] = snapshot
        url_key = _normalize_mapping_url(snapshot.url)
        if url_key:
            self._preflight_snapshots_by_url[url_key] = snapshot
        self._last_preflight_snapshot = snapshot

    def _preflight_snapshot_for_context(
        self,
        context: CDPMessageContext,
        classification: ActionClassification,
        *,
        scope_key: str,
    ) -> PageRiskSnapshot | None:
        self._prune_preflight_indexes()
        current_url = self._preflight_url_key_for_classification(classification)
        candidates: list[PageRiskSnapshot] = []
        scoped = self._preflight_snapshots.get(scope_key)
        if scoped is not None:
            candidates.append(scoped)
        if current_url:
            by_url = self._preflight_snapshots_by_url.get(current_url)
            if by_url is not None:
                candidates.append(by_url)
        if self._last_preflight_snapshot is not None:
            candidates.append(self._last_preflight_snapshot)

        seen: set[tuple[str, str | None]] = set()
        for snapshot in candidates:
            key = (snapshot.scope_key, snapshot.page_signature)
            if key in seen:
                continue
            seen.add(key)
            if not self._preflight_snapshot_valid(snapshot):
                continue
            snapshot_url = _normalize_mapping_url(snapshot.url)
            if current_url and snapshot_url and current_url != snapshot_url:
                continue
            if not current_url and context.websocket_path.startswith("/acira/action/") and snapshot is not self._last_preflight_snapshot:
                continue
            return snapshot
        return None

    def _preflight_url_key_for_classification(self, classification: ActionClassification) -> str:
        if classification.target is not None:
            target_url = _normalize_mapping_url(classification.target.url)
            if target_url:
                return target_url
        details = classification.details if isinstance(classification.details, dict) else {}
        for key in ("url", "current_url"):
            value = _normalize_mapping_url(_string_or_none(details.get(key)))
            if value:
                return value
        probe = details.get("page_target_probe") if isinstance(details.get("page_target_probe"), dict) else {}
        probe_url = _normalize_mapping_url(_string_or_none(probe.get("url")))
        if probe_url:
            return probe_url
        page_targets = details.get("page_targets") if isinstance(details.get("page_targets"), list) else None
        return _normalize_mapping_url(_url_from_page_targets(page_targets))

    def _cached_preflight_gate_decision(
        self,
        context: CDPMessageContext,
        classification: ActionClassification,
        *,
        scope_key: str,
    ) -> CedarDecision | None:
        quarantined = self._active_preflight_quarantine_for_context(context, classification, scope_key=scope_key)
        if quarantined is not None:
            return self._preflight_cedar_decision(
                quarantined,
                decision="block",
                reason=f"Acira page preflight quarantined this page: {quarantined.reason}",
            )

        state = self._preflight_page_states.get(scope_key)
        framework_without_probe = context.websocket_path.startswith("/acira/action/") and context.send_probe is None
        if state is not None and state.pending and not framework_without_probe:
            return None

        snapshot = self._preflight_snapshot_for_context(context, classification, scope_key=scope_key)
        if snapshot is None or snapshot.decision not in {"block", "ask"}:
            return None
        if snapshot.decision == "block":
            self._quarantine_preflight_snapshot(snapshot)
        page_decision = self._preflight_cedar_decision(snapshot)
        if snapshot.decision == "ask" and (
            _review_cache_key(classification, page_decision) in self._review_approved_keys
            or self._preflight_review_cache_key(snapshot) in self._review_approved_keys
        ):
            # Page preflight already approved for this page. Return an explicit allow
            # so the caller does NOT fall through to a fresh probe (which can fail for
            # direct framework adapters and wrongly hard-block the action).
            return self._preflight_cedar_decision(
                snapshot,
                decision="allow",
                reason="page preflight already approved by human review",
            )
        return page_decision

    def _preflight_snapshot_valid(self, snapshot: PageRiskSnapshot) -> bool:
        return snapshot.expires_at > time.monotonic()

    def _prune_preflight_indexes(self) -> None:
        now = time.monotonic()
        for key, snapshot in list(self._preflight_snapshots.items()):
            if snapshot.expires_at <= now:
                self._preflight_snapshots.pop(key, None)
        for key, snapshot in list(self._preflight_snapshots_by_url.items()):
            if snapshot.expires_at <= now:
                self._preflight_snapshots_by_url.pop(key, None)
        for key, snapshot in list(self._preflight_quarantine.items()):
            if snapshot.expires_at <= now:
                self._preflight_quarantine.pop(key, None)
        for key, snapshot in list(self._preflight_quarantine_by_url.items()):
            if snapshot.expires_at <= now:
                self._preflight_quarantine_by_url.pop(key, None)
        if self._last_preflight_snapshot is not None and self._last_preflight_snapshot.expires_at <= now:
            self._last_preflight_snapshot = None

    def _quarantine_preflight_snapshot(self, snapshot: PageRiskSnapshot) -> None:
        self._remember_preflight_snapshot(snapshot)
        self._preflight_quarantine[snapshot.scope_key] = snapshot
        url_key = _normalize_mapping_url(snapshot.url)
        if url_key:
            self._preflight_quarantine_by_url[url_key] = snapshot

    def _clear_preflight_quarantine(self, snapshot: PageRiskSnapshot) -> None:
        existing = self._preflight_quarantine.get(snapshot.scope_key)
        if existing is snapshot or (existing and existing.page_signature == snapshot.page_signature):
            self._preflight_quarantine.pop(snapshot.scope_key, None)
        url_key = _normalize_mapping_url(snapshot.url)
        if url_key:
            existing_by_url = self._preflight_quarantine_by_url.get(url_key)
            if existing_by_url is snapshot or (existing_by_url and existing_by_url.page_signature == snapshot.page_signature):
                self._preflight_quarantine_by_url.pop(url_key, None)

    def _active_preflight_quarantine_for_context(
        self,
        context: CDPMessageContext,
        classification: ActionClassification,
        *,
        scope_key: str,
    ) -> PageRiskSnapshot | None:
        self._prune_preflight_indexes()
        scoped = self._preflight_quarantine.get(scope_key)
        if scoped is not None:
            return scoped
        current_url = self._preflight_url_key_for_classification(classification)
        if current_url:
            by_url = self._preflight_quarantine_by_url.get(current_url)
            if by_url is not None and self._preflight_snapshot_valid(by_url):
                return by_url
        if context.websocket_path.startswith("/acira/action/") and current_url and self._last_preflight_snapshot is not None:
            latest_url = _normalize_mapping_url(self._last_preflight_snapshot.url)
            if latest_url == current_url and self._last_preflight_snapshot.scope_key in self._preflight_quarantine:
                return self._last_preflight_snapshot
        return None

    def _active_preflight_quarantine(self, scope_key: str) -> PageRiskSnapshot | None:
        self._prune_preflight_indexes()
        return self._preflight_quarantine.get(scope_key)

    def _quarantine_page_after_rejected_preflight(self, cedar_decision: CedarDecision | None) -> None:
        if cedar_decision is None or cedar_decision.source != "general_skill_preflight":
            return
        metadata = cedar_decision.metadata if isinstance(cedar_decision.metadata, dict) else {}
        preflight = metadata.get("preflight") if isinstance(metadata.get("preflight"), dict) else {}
        scope_key = _string_or_none(preflight.get("scope_key"))
        snapshot = self._preflight_snapshots.get(scope_key or "")
        if snapshot is None:
            url_key = _normalize_mapping_url(_string_or_none(preflight.get("url")))
            snapshot = self._preflight_snapshots_by_url.get(url_key) if url_key else None
        if snapshot is not None:
            self._quarantine_preflight_snapshot(snapshot)

    async def _review_active_preflight_page(
        self,
        context: CDPMessageContext,
        snapshot: PageRiskSnapshot,
        *,
        trigger: str,
    ) -> bool:
        cedar_decision = self._preflight_cedar_decision(snapshot)
        review_key = self._preflight_review_cache_key(snapshot)
        if review_key in self._review_approved_keys:
            self._note_preflight_review_result(snapshot, allowed=True)
            return True
        if review_key in self._review_denied_keys:
            self._note_preflight_review_result(snapshot, allowed=False)
            return False
        existing_review = self._review_inflight.get(review_key)
        if existing_review is not None:
            return await asyncio.shield(existing_review)

        review_future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._review_inflight[review_key] = review_future
        try:
            details = {"cedar": cedar_decision.metadata or {}, "cedar_source": cedar_decision.source, "trigger": trigger}
            classification = ActionClassification(
                method="Acira.preflight",
                action="page_preflight",
                reason=f"page preflight review after {trigger}",
                confidence=0.99,
                read_only=False,
                meaningful=True,
                details=details,
            )
            self._terminal_decision("HITL", context, classification, snapshot.reason, color="yellow", cedar_decision=cedar_decision)
            request = GuardReviewRequest(
                payload=json.dumps({"method": "Acira.preflight", "params": snapshot.to_metadata()}, ensure_ascii=True),
                context=context,
                classification=classification,
                reason=snapshot.reason,
            )
            async with self.review_pause.hold():
                if self._pause_coordinator is None:
                    allowed = await self._ask_backend(request)
                else:
                    async with self._pause_coordinator.hold_for_review(release_on_exit=True):
                        allowed = await self._ask_backend(request)
            if allowed:
                self._review_approved_keys.add(review_key)
                self._note_preflight_review_result(snapshot, allowed=True)
                self._terminal_decision(
                    "ALLOW",
                    context,
                    classification,
                    "approved by human review",
                    color="blue",
                    cedar_decision=cedar_decision,
                )
                review_future.set_result(True)
                return True
            self._review_denied_keys.add(review_key)
            self._note_preflight_review_result(snapshot, allowed=False)
            self._terminal_decision(
                "BLOCK",
                context,
                classification,
                "rejected by human review",
                color="red",
                cedar_decision=cedar_decision,
            )
            review_future.set_result(False)
            return False
        except BaseException:
            review_future.cancel()
            raise
        finally:
            if self._review_inflight.get(review_key) is review_future:
                self._review_inflight.pop(review_key, None)

    async def _close_preflight_page_if_configured(
        self,
        context: CDPMessageContext,
        snapshot: PageRiskSnapshot,
        *,
        reason: str,
    ) -> None:
        if self.config.general_skill_preflight.on_deny != "close_page":
            return
        await self._close_preflight_page(context, snapshot, reason=reason)

    async def _close_preflight_page(
        self,
        context: CDPMessageContext,
        snapshot: PageRiskSnapshot,
        *,
        reason: str,
    ) -> None:
        if context.send_probe is None:
            return
        try:
            await asyncio.wait_for(context.probe("Page.close", {}), timeout=max(self.config.probe_timeout, 0.5))
            return
        except Exception:
            pass
        target_id = _target_id_from_websocket_path(context.websocket_path)
        if not target_id:
            return
        try:
            await asyncio.wait_for(
                context.probe("Target.closeTarget", {"targetId": target_id}, session_id=""),
                timeout=max(self.config.probe_timeout, 0.5),
            )
        except Exception:
            return

    async def _page_missing_target_decision(
        self,
        classification: ActionClassification,
        context: CDPMessageContext,
        *,
        page_targets: list[dict[str, Any]] | None = None,
    ) -> CedarDecision | None:
        if (
            not self.config.specific_skills
            or classification.read_only
            or not classification.meaningful
            or not _should_evaluate_specific_skill_targets(classification)
            or classification.action in HELPER_INSPECTION_ACTIONS
            or classification.action in {"dangerous_js", "read_only_js", "trusted_helper_js"}
        ):
            return None

        details = classification.details if isinstance(classification.details, dict) else {}
        probe_info = self._page_target_probe_from_classification_details(classification)
        if page_targets is None:
            page_targets = self._page_targets_from_classification_details(classification)

        facts = _event_facts_from_classification(classification)
        url = facts.get("url") or _url_from_page_targets(page_targets) or (probe_info or {}).get("url")
        if page_targets is None:
            cached_targets, cached_probe = self._cached_page_target_probe_for_url(url if isinstance(url, str) else None)
            if cached_targets is not None:
                page_targets = cached_targets
                if probe_info is None:
                    probe_info = cached_probe
                url = url or _url_from_page_targets(page_targets) or (probe_info or {}).get("url")
        if (not isinstance(url, str) or not url) and page_targets is None:
            page_targets = await self._probe_page_targets(context)
            if probe_info is None:
                probe_info = {
                    "status": "ok" if page_targets else "unavailable",
                    "candidate_count": len(page_targets or []),
                }
            url = _url_from_page_targets(page_targets) or (probe_info or {}).get("url")
        if not isinstance(url, str) or not url:
            return None
        facts = dict(facts)
        facts["url"] = url

        scoped_targets: list[tuple[str, str, tuple[str, ...], SpecificSkillTarget]] = []
        for skill in self.config.specific_skills:
            if not skill.matches_url(url):
                continue
            for protected_target in skill.targets:
                scoped_targets.append((skill.id, skill.name, skill.url_patterns, protected_target))

        if not scoped_targets:
            return None

        if page_targets is None:
            cached_targets, cached_probe = self._cached_page_target_probe_for_url(url)
            if cached_targets is not None:
                page_targets = cached_targets
                if probe_info is None:
                    probe_info = cached_probe
            else:
                page_targets = await self._probe_page_targets(context)
                if probe_info is None:
                    probe_info = {
                        "status": "ok" if page_targets else "unavailable",
                        "candidate_count": len(page_targets or []),
                    }
        elif probe_info is None:
            probe_info = {
                "status": "ok" if page_targets else "empty",
                "candidate_count": len(page_targets or []),
            }

        normalized_page_targets = self._normalize_page_targets(page_targets or [])
        if probe_info is not None:
            probe_info = dict(probe_info)
            probe_info["candidate_count"] = len(normalized_page_targets)
        mapping_entries = self._resolve_page_target_mappings(
            scoped_targets=scoped_targets,
            page_targets=normalized_page_targets,
            facts=facts,
            classification=classification,
            probe_info=probe_info,
        )

        preflight_key = _page_mapping_session_key(mapping_entries, normalized_page_targets, probe_info)
        page_mapping_session = self._page_mapping_sessions.get(preflight_key)
        if page_mapping_session is not None and page_mapping_session.status == "rejected":
            self._security_lockdown = SecurityLockdownState(
                active=True,
                reason=ACIRA_SECURITY_LOCKDOWN_REASON,
                source="mapping_rejected",
                review_key=page_mapping_session.review_key,
                page_signature=page_mapping_session.page_signature,
                skill_ids=tuple(
                    sorted(
                        {
                            str(item.get("skill_id") or "")
                            for item in page_mapping_session.entries
                            if isinstance(item, dict) and item.get("skill_id")
                        }
                    )
                ),
                created_at=page_mapping_session.created_at,
            )
            return CedarDecision(
                decision="block",
                reason=ACIRA_SECURITY_LOCKDOWN_REASON,
                source="specific_skill_mapping_rejected",
                skill_decision_id=None,
                metadata=self._mapping_metadata(
                    list(page_mapping_session.entries),
                    normalized_page_targets,
                    probe_info=probe_info,
                    all_entries=mapping_entries,
                ),
            )

        approved_page_session = (
            page_mapping_session is not None
            and page_mapping_session.status in {"approved", "approved_empty", "approved_probe_unavailable"}
        )
        current_mapping = self._current_action_mapping_entry(mapping_entries, classification)
        if current_mapping is not None and (
            approved_page_session or str(current_mapping.get("status") or "") in {"approved", "auto_mapped", "rejected"}
        ):
            metadata = self._mapping_metadata(mapping_entries, normalized_page_targets, probe_info=probe_info)
            return CedarDecision(
                decision=str(current_mapping.get("decision") or "block"),
                reason=str(current_mapping.get("reason") or "Mapped protected Acira target."),
                source="specific_skill_mapping_rejected"
                if current_mapping.get("status") == "rejected"
                else "specific_skill_mapped",
                skill_decision_id=f"{current_mapping['skill_id']}:{current_mapping['target_id']}",
                metadata=metadata,
            )

        if approved_page_session:
            metadata = self._mapping_metadata(mapping_entries, normalized_page_targets, probe_info=probe_info)
            metadata["skills"]["page_mapping_safe_to_forward"] = True
            return CedarDecision(
                decision="allow",
                reason="Page mapping was already reviewed for this page signature; current action is not a mapped protected target.",
                source="specific_skill_mapping",
                skill_decision_id=None,
                metadata=metadata,
            )

        page_preflight = (
            details.get("mapping_preflight") == "page" or classification.action == "mapping_preflight"
        ) and preflight_key not in self._page_mapping_sessions
        if not normalized_page_targets and details.get("defer_missing_target_review") == "cdp_backstop":
            review_items = [entry for entry in mapping_entries if entry.get("action_relevant")]
        elif page_preflight:
            review_items = [entry for entry in mapping_entries if _mapping_entry_requires_page_review(entry)]
        else:
            review_items = [
                entry
                for entry in mapping_entries
                if entry.get("status") in UNRESOLVED_MAPPING_STATUSES and self._mapping_entry_needs_review(entry, classification)
            ]
        if not review_items:
            if mapping_entries:
                metadata = self._mapping_metadata(mapping_entries, normalized_page_targets, probe_info=probe_info)
                metadata["skills"]["page_mapping_safe_to_forward"] = True
                return CedarDecision(
                    decision="allow",
                    reason="Page mapping produced no protected target for the current action.",
                    source="specific_skill_mapping",
                    skill_decision_id=None,
                    metadata=metadata,
                )
            return None
        empty_mapping_key = _empty_mapping_approval_key(review_items, normalized_page_targets, probe_info)
        if (
            empty_mapping_key in self._approved_empty_mapping_keys
            and not _has_concrete_mapping_candidate(review_items)
        ):
            metadata = self._mapping_metadata(
                review_items,
                normalized_page_targets,
                probe_info=probe_info,
                all_entries=mapping_entries,
            )
            metadata["skills"]["page_mapping_safe_to_forward"] = True
            return CedarDecision(
                decision="allow",
                reason="An empty page mapping was already approved for this page signature.",
                source="specific_skill_mapping",
                skill_decision_id=None,
                metadata=metadata,
            )
        page_mapping_session_key = _page_mapping_session_key(review_items, normalized_page_targets, probe_info)
        page_mapping_session = self._page_mapping_sessions.get(page_mapping_session_key)
        if page_mapping_session is not None:
            if page_mapping_session.status == "rejected":
                self._security_lockdown = SecurityLockdownState(
                    active=True,
                    reason=ACIRA_SECURITY_LOCKDOWN_REASON,
                    source="mapping_rejected",
                    review_key=page_mapping_session.review_key,
                    page_signature=page_mapping_session.page_signature,
                    skill_ids=tuple(
                        sorted(
                            {
                                str(item.get("skill_id") or "")
                                for item in page_mapping_session.entries
                                if isinstance(item, dict) and item.get("skill_id")
                            }
                        )
                    ),
                    created_at=page_mapping_session.created_at,
                )
                return CedarDecision(
                    decision="block",
                    reason=ACIRA_SECURITY_LOCKDOWN_REASON,
                    source="specific_skill_mapping_rejected",
                    skill_decision_id=None,
                    metadata={
                        "engine": "cedar",
                        "mode": "deterministic_adapter",
                        "skills": {
                            "in_scope": True,
                            "matches": [],
                            "missing_targets": [],
                            "page_mapping": list(page_mapping_session.entries),
                            "page_mapping_session": page_mapping_session.to_metadata(),
                            "page_target_probe": probe_info or {},
                        },
                    },
                )
            if page_mapping_session.status in {"approved", "approved_empty", "approved_probe_unavailable"}:
                current_mapping = self._current_action_mapping_entry(mapping_entries, classification)
                if current_mapping is not None:
                    metadata = self._mapping_metadata(mapping_entries, normalized_page_targets, probe_info=probe_info)
                    return CedarDecision(
                        decision=str(current_mapping.get("decision") or "block"),
                        reason=str(current_mapping.get("reason") or "Mapped protected Acira target."),
                        source="specific_skill_mapped",
                        skill_decision_id=f"{current_mapping['skill_id']}:{current_mapping['target_id']}",
                        metadata=metadata,
                    )
                metadata = self._mapping_metadata(mapping_entries, normalized_page_targets, probe_info=probe_info)
                metadata["skills"]["page_mapping_safe_to_forward"] = True
                return CedarDecision(
                    decision="allow",
                    reason="Page mapping was already reviewed for this page signature; current action is not a mapped protected target.",
                    source="specific_skill_mapping",
                    skill_decision_id=None,
                    metadata=metadata,
                )

        metadata = self._mapping_metadata(
            review_items,
            normalized_page_targets,
            probe_info=probe_info,
            all_entries=mapping_entries,
        )
        metadata["skills"]["page_mapping_session_key"] = page_mapping_session_key
        first = review_items[0]
        probe_status = str((probe_info or {}).get("status") or "")
        if probe_status in PROBE_UNAVAILABLE_STATUSES:
            review_reason = (
                "Acira could not inspect the current page targets, so the old-to-new mapping is unavailable and "
                "the page may have changed. "
                "Review before any browser action is forwarded."
            )
        elif review_items and all(_is_concrete_mapping_candidate(item) for item in review_items):
            review_reason = (
                "Acira found proposed old-to-new mappings for this changed page. Review them before any browser "
                "action is forwarded."
            )
        else:
            review_reason = (
                "A URL-scoped Acira skill could not map all protected targets on this changed page confidently. "
                "Review the proposed old-to-new mapping before any browser action is forwarded."
            )
        return CedarDecision(
            decision="ask",
            reason=review_reason,
            source="specific_skill_mapping_review",
            skill_decision_id=f"{first['skill_id']}:{first['target_id']}",
            metadata=metadata,
        )

    def _normalize_page_targets(self, page_targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for item in page_targets:
            target = _target_from_probe_value(item)
            if target is not None:
                metadata = target.to_metadata()
                _merge_probe_extras(metadata, item)
                normalized.append(metadata)
            elif isinstance(item, dict):
                normalized.append(dict(item))
        return normalized

    def _resolve_page_target_mappings(
        self,
        *,
        scoped_targets: list[tuple[str, str, tuple[str, ...], SpecificSkillTarget]],
        page_targets: list[dict[str, Any]],
        facts: dict[str, Any],
        classification: ActionClassification,
        probe_info: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        current = facts.get("target") if isinstance(facts.get("target"), dict) else {}
        raw_entries: list[dict[str, Any]] = []
        probe_status = str((probe_info or {}).get("status") or ("ok" if page_targets else "empty"))
        no_candidate_status = "probe_unavailable" if probe_status in PROBE_UNAVAILABLE_STATUSES else "unmapped_no_candidate"
        for skill_id, skill_name, url_patterns, protected_target in scoped_targets:
            source_hash = fingerprint_hash(protected_target.fingerprint)
            scope = url_scope_key(str(facts.get("url") or ""), url_patterns)
            current_resolution = resolve_target_fingerprint(protected_target.fingerprint, current, facts)
            ranked: list[tuple[dict[str, Any], Any, str, TargetMappingRecord | None]] = []
            for candidate in page_targets:
                candidate_key = target_mapping_key(candidate)
                resolution = resolve_target_fingerprint(protected_target.fingerprint, candidate, facts)
                eligibility = mapping_candidate_eligibility(protected_target.fingerprint, candidate)
                evidence_gate = mapping_evidence_gate(protected_target.fingerprint, candidate, resolution, eligibility)
                latest = self.target_mapping_store.latest(
                    skill_id=skill_id,
                    target_id=protected_target.id,
                    source_fingerprint_hash=source_hash,
                    url_scope=scope,
                    candidate_key=candidate_key,
                )
                if latest is not None and latest.status == "approved" and not approved_mapping_matches(
                        latest, candidate, eligibility, evidence_gate):
                    latest = replace(latest, status="stale")
                ranked.append((candidate, resolution, candidate_key, latest, eligibility, evidence_gate))
            # Saturated totals can tie an unchanged control with nearby decoys.
            # Use the same eligibility-aware score for ordering and assignment.
            ranked.sort(key=lambda item: (_assignment_candidate_score(item),
                        unchanged_capture_identity(protected_target.fingerprint, item[0], page_url=str(facts.get("url") or ""))),
                        reverse=True)
            approved = next(
                (
                    item
                    for item in ranked
                    if item[3] is not None and item[3].status == "approved"
                ),
                None,
            )
            rejected = next((item for item in ranked if item[3] is not None and item[3].status == "rejected"), None)
            raw_entries.append(
                {
                    "skill_id": skill_id,
                    "skill_name": skill_name,
                    "url_patterns": url_patterns,
                    "protected_target": protected_target,
                    "source_hash": source_hash,
                    "scope": scope,
                    "current_resolution": current_resolution,
                    "ranked": ranked,
                    "approved": approved,
                    "rejected": rejected,
                    "unchanged": [item for item in ranked if _assignment_candidate_viable(item)
                                  and unchanged_capture_identity(protected_target.fingerprint, item[0], page_url=str(facts.get("url") or ""))],
                }
            )

        assigned = self._assign_page_target_mappings(raw_entries)

        entries: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_entries):
            protected_target = raw["protected_target"]
            ranked = raw["ranked"]
            approved = raw["approved"]
            rejected = raw["rejected"]
            assigned_best = assigned.get(index)
            best = assigned_best or (ranked[0] if ranked else None)
            best_key_for_runner = best[2] if best else None
            runner = next((item for item in ranked if item[2] != best_key_for_runner
                           and _assignment_candidate_viable(item)), None)
            best_candidate = best[0] if best else None
            best_resolution = best[1] if best else None
            best_key = best[2] if best else None
            best_record = best[3] if best else None
            best_eligibility = best[4] if best else {}
            best_gate = best[5] if best else {}
            runner_resolution = runner[1] if runner else None
            margin = (best_resolution.score - runner_resolution.score) if best_resolution and runner_resolution else best_resolution.score if best_resolution else 0
            status = no_candidate_status
            concrete_allowed = bool(best_gate.get("concrete")) if isinstance(best_gate, dict) else False
            unchanged = len(raw["unchanged"]) == 1 and best_key == raw["unchanged"][0][2]
            if rejected is not None and best_key == rejected[2]:
                status = "rejected"
            elif best_record is not None and best_record.status == "approved":
                status = "approved" if assigned_best is not None else "ambiguous"
            elif best_record is not None and best_record.status == "stale":
                # Even a high new match score cannot renew old human consent.
                status = "ambiguous" if assigned_best is not None and concrete_allowed else no_candidate_status
            elif (
                assigned_best is not None
                and
                best_resolution is not None
                and best_resolution.kind in {"strong_match", "structural_slot_match"}
                and best_resolution.score >= protected_target.threshold
                and (margin >= AUTO_TARGET_MAPPING_MARGIN or unchanged)
                and concrete_allowed
            ):
                status = "auto_mapped"
            elif (
                assigned_best is not None
                and
                best_resolution is not None
                and best_resolution.score >= MAPPING_REVIEW_SCORE_FLOOR
                and concrete_allowed
            ):
                status = "ambiguous"

            top_candidates = [
                {
                    "target": _target_summary_from_metadata(candidate),
                    "score": resolution.score,
                    "kind": resolution.kind,
                    "signals": list(resolution.matched_signals)[:8],
                    "candidate_eligibility": dict(eligibility or {}),
                    "evidence_gate": dict(evidence_gate or {}),
                    "score_components": {
                        "identity": resolution.identity_score,
                        "structure": resolution.structure_score,
                        "context": resolution.context_score,
                    },
                }
                for candidate, resolution, _key, _record, eligibility, evidence_gate in ranked[:5]
            ]
            concrete_candidate = status in {"approved", "rejected", "auto_mapped", "ambiguous"}
            entries.append(
                {
                    "skill_id": raw["skill_id"],
                    "skill_name": raw["skill_name"],
                    "target_id": protected_target.id,
                    "decision": protected_target.decision if status != "rejected" else "block",
                    "original_decision": protected_target.decision,
                    "reason": protected_target.reason,
                    "threshold": protected_target.threshold,
                    "source_fingerprint_hash": raw["source_hash"],
                    "url_scope": raw["scope"],
                    "original_label": _fingerprint_label(protected_target.fingerprint),
                    "expected_actions": sorted(expected_actions_for_fingerprint(protected_target.fingerprint)),
                    "current_target": _target_summary_from_metadata(current) if current else None,
                    "current_resolution": raw["current_resolution"].to_metadata(),
                    "best_candidate": _target_summary_from_metadata(best_candidate) if best_candidate else None,
                    "best_candidate_key": best_key,
                    "candidate_fingerprint": dict(best_candidate or {}) if concrete_candidate else {},
                    "candidate_key": best_key if concrete_candidate else None,
                    "best_resolution": best_resolution.to_metadata() if best_resolution else None,
                    "runner_up_resolution": runner_resolution.to_metadata() if runner_resolution else None,
                    "margin": margin,
                    "unchanged_capture_identity": unchanged,
                    "mapping_approval_invalidated": best_record is not None and best_record.status == "stale",
                    "score": best_resolution.score if best_resolution else 0,
                    "signals": list(best_resolution.matched_signals)[:8] if best_resolution else [],
                    "status": status,
                    "candidate_eligibility": dict(best_eligibility or {}),
                    "evidence_gate": dict(best_gate or {}),
                    "top_candidates": top_candidates,
                    "competing_candidates": top_candidates,
                    "mapping": status,
                    "action_relevant": _mapping_action_relevant(protected_target.fingerprint, classification.action),
                }
            )
        return entries

    def _assign_page_target_mappings(
        self,
        raw_entries: list[dict[str, Any]],
    ) -> dict[int, tuple[Any, ...]]:
        assigned: dict[int, tuple[Any, ...]] = {}
        used_candidate_keys: set[str] = set()

        for index, raw in enumerate(raw_entries):
            approved = raw["approved"]
            if approved is None or approved[2] in used_candidate_keys or not _assignment_candidate_viable(approved):
                continue
            assigned[index] = approved
            used_candidate_keys.add(approved[2])

        remaining = [index for index, raw in enumerate(raw_entries) if index not in assigned and raw["ranked"]]
        if not remaining:
            return assigned

        choice_lists: dict[int, list[tuple[Any, ...]]] = {}
        for index in remaining:
            choices = [
                item
                for item in raw_entries[index]["ranked"][:32]
                if item[2] not in used_candidate_keys and _assignment_candidate_viable(item)
            ]
            choice_lists[index] = choices

        if len(remaining) > 12:
            remaining.sort(
                key=lambda index: (
                    _candidate_margin(choice_lists[index]),
                    _assignment_candidate_score(choice_lists[index][0]) if choice_lists[index] else 0,
                ),
                reverse=True,
            )
            for index in remaining:
                choice = next((item for item in choice_lists[index] if item[2] not in used_candidate_keys), None)
                if choice is None:
                    continue
                assigned[index] = choice
                used_candidate_keys.add(choice[2])
            return assigned

        order = sorted(
            remaining,
            key=lambda index: (
                len(choice_lists[index]) or 999,
                -_candidate_margin(choice_lists[index]),
                -(choice_lists[index][0][1].score if choice_lists[index] else 0),
            ),
        )
        suffix_best: list[int] = [0] * (len(order) + 1)
        for pos in range(len(order) - 1, -1, -1):
            choices = choice_lists[order[pos]]
            suffix_best[pos] = suffix_best[pos + 1] + (_assignment_candidate_score(choices[0]) if choices else 0)

        best_score = -1
        best_assignment: dict[int, tuple[Any, ...]] = {}

        def dfs(
            pos: int,
            total: int,
            used: set[str],
            current: dict[int, tuple[Any, ...]],
        ) -> None:
            nonlocal best_score, best_assignment
            if total + suffix_best[pos] < best_score:
                return
            if pos >= len(order):
                if total > best_score:
                    best_score = total
                    best_assignment = dict(current)
                return

            index = order[pos]
            choices = [item for item in choice_lists[index] if item[2] not in used]
            if not choices:
                dfs(pos + 1, total, used, current)
                return

            for choice in choices:
                assignment_score = _assignment_candidate_score(choice)
                current[index] = choice
                used.add(choice[2])
                dfs(pos + 1, total + assignment_score, used, current)
                used.remove(choice[2])
                current.pop(index, None)

            dfs(pos + 1, total, used, current)

        dfs(0, 0, set(used_candidate_keys), {})
        assigned.update(best_assignment)
        return assigned

    def _current_action_mapping_entry(
        self,
        entries: list[dict[str, Any]],
        classification: ActionClassification,
    ) -> dict[str, Any] | None:
        if classification.target is None:
            return None
        current = classification.target.to_metadata()
        current_key = target_mapping_key(current)
        for entry in entries:
            if entry.get("status") not in {"ambiguous", "approved", "auto_mapped", "rejected"}:
                continue
            if not entry.get("action_relevant"):
                continue
            candidate = entry.get("candidate_fingerprint")
            if not isinstance(candidate, dict):
                continue
            if (
                entry.get("candidate_key") == current_key
                or _metadata_targets_same(current, candidate)
                or _current_option_inherits_select_mapping(current, candidate)
            ):
                return entry
        return None

    def _mapping_entry_needs_review(
        self,
        entry: dict[str, Any],
        classification: ActionClassification,
        *,
        page_preflight: bool = False,
    ) -> bool:
        if (
            not page_preflight
            and not entry.get("action_relevant")
            and classification.action not in HIGH_RISK_MISSING_TARGET_ACTIONS_FOR_GUARD
        ):
            return False
        if entry.get("status") == "rejected":
            return True

        current_resolution = entry.get("current_resolution") if isinstance(entry.get("current_resolution"), dict) else {}
        if page_preflight:
            return True
        target = classification.target
        if (
            current_resolution.get("kind") == "unrelated"
            and target is not None
            and float(target.confidence or 0.0) >= 0.65
            and classification.action in TARGET_AWARE_SAFE_FORM_ACTIONS
        ):
            return False
        if current_resolution.get("kind") != "unrelated":
            return True

        if (
            target is not None
            and classification.action in TARGET_AWARE_SAFE_FORM_ACTIONS
            and float(target.confidence or 0.0) >= 0.65
            and not _missing_target_same_region(
                entry.get("candidate_fingerprint") if isinstance(entry.get("candidate_fingerprint"), dict) else {},
                target.to_metadata(),
                classification.action,
            )
        ):
            return False
        return classification.action in HIGH_RISK_MISSING_TARGET_ACTIONS_FOR_GUARD

    def _mapping_metadata(
        self,
        entries: list[dict[str, Any]],
        page_targets: list[dict[str, Any]],
        *,
        probe_info: dict[str, Any] | None = None,
        all_entries: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        page_probe = dict(probe_info or {})
        page_probe.setdefault("status", "ok" if page_targets else "empty")
        page_probe["candidate_count"] = len(page_targets)
        page_probe.setdefault("page_signature", _page_target_signature(page_targets, page_probe))
        empty_mapping_key = _empty_mapping_approval_key(entries, page_targets, page_probe)
        page_mapping_session_key = _page_mapping_session_key(entries, page_targets, page_probe)
        complete_entries = [dict(item) for item in (all_entries if all_entries is not None else entries)]
        concrete_entries = [item for item in complete_entries if _is_concrete_mapping_candidate(item)]
        already_enforced = [
            item for item in complete_entries if str(item.get("status") or "") in {"approved", "rejected"}
        ]
        review_concrete_entries = [item for item in entries if _is_concrete_mapping_candidate(item)]
        unresolved_entries = [item for item in complete_entries if item.get("status") in UNRESOLVED_MAPPING_STATUSES]
        missing = [
            {
                "skill_id": entry.get("skill_id"),
                "skill_name": entry.get("skill_name"),
                "target_id": entry.get("target_id"),
                "reason": entry.get("reason"),
                "score": entry.get("score")
                if isinstance(entry.get("score"), int)
                else (entry.get("best_resolution") or {}).get("score")
                if isinstance(entry.get("best_resolution"), dict)
                else 0,
                "threshold": entry.get("threshold"),
                "source": "target_mapping",
                "resolution": entry.get("best_resolution"),
                "status": entry.get("status"),
                "top_candidates": entry.get("top_candidates") or entry.get("competing_candidates") or [],
            }
            for entry in entries
            if entry.get("status") in UNRESOLVED_MAPPING_STATUSES
        ]
        mapping_summary = {
            "total_targets": len(complete_entries),
            "review_targets": len(entries),
            "concrete_targets": len(concrete_entries),
            "already_enforced_targets": len(already_enforced),
            "proposed_review_mappings": len(review_concrete_entries),
            "unresolved_targets": len(unresolved_entries),
            "status_counts": _mapping_status_counts(complete_entries),
        }
        return {
            "engine": "cedar",
            "mode": "deterministic_adapter",
            "skills": {
                "in_scope": True,
                "matches": [],
                "missing_targets": missing,
                "page_mapping": entries,
                "page_mapping_all": complete_entries,
                "page_mapping_summary": mapping_summary,
                "page_target_probe": page_probe,
                "empty_mapping_key": empty_mapping_key,
                "page_mapping_session_key": page_mapping_session_key,
            },
        }

    def _page_targets_from_classification_details(
        self,
        classification: ActionClassification,
    ) -> list[dict[str, Any]] | None:
        details = classification.details if isinstance(classification.details, dict) else {}
        page_targets = details.get("page_targets")
        if not isinstance(page_targets, list):
            return None
        return [item for item in page_targets if isinstance(item, dict)]

    def _page_target_probe_from_classification_details(
        self,
        classification: ActionClassification,
    ) -> dict[str, Any] | None:
        details = classification.details if isinstance(classification.details, dict) else {}
        probe = details.get("page_target_probe")
        return dict(probe) if isinstance(probe, dict) else None

    def _cached_page_target_probe_for_url(
        self,
        url: str | None,
    ) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
        now = time.monotonic()
        for key, entry in list(self._cached_page_target_probes.items()):
            if entry.expires_at <= now:
                self._cached_page_target_probes.pop(key, None)
        if self._last_page_target_probe is not None and self._last_page_target_probe.expires_at <= now:
            self._last_page_target_probe = None

        entry: CachedPageTargetProbe | None = None
        if isinstance(url, str) and url:
            entry = self._cached_page_target_probes.get(_normalize_mapping_url(url))
        if entry is None:
            entry = self._last_page_target_probe
        if entry is None:
            return None, None
        return [dict(item) for item in entry.targets], dict(entry.metadata)

    def _remember_mapping_review_session(
        self,
        classification: ActionClassification,
        cedar_decision: CedarDecision | None,
        *,
        approved: bool,
        review_key: str | None,
    ) -> PageMappingSession | None:
        if cedar_decision is None or cedar_decision.source != "specific_skill_mapping_review":
            return None
        metadata = cedar_decision.metadata if isinstance(cedar_decision.metadata, dict) else {}
        skills = metadata.get("skills") if isinstance(metadata.get("skills"), dict) else {}
        mappings = [item for item in skills.get("page_mapping", []) if isinstance(item, dict)] if isinstance(skills.get("page_mapping"), list) else []
        probe = dict(skills.get("page_target_probe") or {}) if isinstance(skills.get("page_target_probe"), dict) else {}
        page_targets = self._page_targets_from_classification_details(classification) or []
        if not mappings:
            return None
        key = str(skills.get("page_mapping_session_key") or "") or _page_mapping_session_key(mappings, page_targets, probe)
        if not key:
            return None
        concrete = [item for item in mappings if _is_concrete_mapping_candidate(item)]
        probe_status = str(probe.get("status") or "")
        if approved and concrete:
            status = "approved"
        elif approved and probe_status in PROBE_UNAVAILABLE_STATUSES:
            status = "approved_probe_unavailable"
        elif approved:
            status = "approved_empty"
        else:
            status = "rejected"
        session = PageMappingSession(
            key=key,
            status=status,
            entries=tuple(dict(item) for item in mappings),
            page_signature=str(probe.get("page_signature") or "") or None,
            probe_status=probe_status or None,
            review_key=review_key,
            created_at=time.time(),
        )
        self._page_mapping_sessions[key] = session
        return session

    def _persist_mapping_review_records(
        self,
        classification: ActionClassification,
        cedar_decision: CedarDecision | None,
        *,
        status: str,
    ) -> None:
        if cedar_decision is None or cedar_decision.source != "specific_skill_mapping_review":
            return
        metadata = cedar_decision.metadata if isinstance(cedar_decision.metadata, dict) else {}
        skills = metadata.get("skills") if isinstance(metadata.get("skills"), dict) else {}
        mappings = skills.get("page_mapping") if isinstance(skills.get("page_mapping"), list) else []
        for item in mappings:
            if not isinstance(item, dict) or not item.get("candidate_key"):
                continue
            if item.get("status") not in {"ambiguous", "approved", "auto_mapped"}:
                continue
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self.target_mapping_store.record(
                TargetMappingRecord(
                    skill_id=str(item.get("skill_id") or ""),
                    skill_name=str(item.get("skill_name") or "") or None,
                    target_id=str(item.get("target_id") or ""),
                    source_fingerprint_hash=str(item.get("source_fingerprint_hash") or ""),
                    url_scope=str(item.get("url_scope") or ""),
                    candidate_key=str(item.get("candidate_key") or ""),
                    candidate_fingerprint=dict(item.get("candidate_fingerprint") or {}),
                    resolution=dict(item.get("best_resolution") or {}),
                    decision=str(item.get("original_decision") or item.get("decision") or "block"),
                    status=status,
                    created_at=now,
                    updated_at=now,
                )
            )

    def _remember_approved_empty_mapping_review(
        self,
        classification: ActionClassification,
        cedar_decision: CedarDecision | None,
    ) -> str | None:
        if cedar_decision is None or cedar_decision.source != "specific_skill_mapping_review":
            return None
        metadata = cedar_decision.metadata if isinstance(cedar_decision.metadata, dict) else {}
        skills = metadata.get("skills") if isinstance(metadata.get("skills"), dict) else {}
        mappings = skills.get("page_mapping") if isinstance(skills.get("page_mapping"), list) else []
        concrete = [item for item in mappings if isinstance(item, dict) and _is_concrete_mapping_candidate(item)]
        if concrete:
            return None
        probe = skills.get("page_target_probe") if isinstance(skills.get("page_target_probe"), dict) else {}
        page_targets = self._page_targets_from_classification_details(classification) or []
        key = _empty_mapping_approval_key(
            [item for item in mappings if isinstance(item, dict)],
            page_targets,
            probe,
        )
        if key:
            self._approved_empty_mapping_keys.add(key)
        return key

    def _mapped_block_reason_after_review_approval(
        self,
        classification: ActionClassification,
        cedar_decision: CedarDecision | None,
    ) -> str | None:
        if cedar_decision is None or cedar_decision.source != "specific_skill_mapping_review":
            return None
        metadata = cedar_decision.metadata if isinstance(cedar_decision.metadata, dict) else {}
        skills = metadata.get("skills") if isinstance(metadata.get("skills"), dict) else {}
        mappings = skills.get("page_mapping") if isinstance(skills.get("page_mapping"), list) else []
        current_key = target_mapping_key(classification.target.to_metadata()) if classification.target else ""
        for item in mappings:
            if not isinstance(item, dict):
                continue
            if not _is_concrete_mapping_candidate(item):
                continue
            if item.get("original_decision") != "block":
                continue
            candidate = item.get("candidate_fingerprint") if isinstance(item.get("candidate_fingerprint"), dict) else {}
            if item.get("candidate_key") == current_key or (
                classification.target is not None
                and (
                    _metadata_targets_same(classification.target.to_metadata(), candidate)
                    or _current_option_inherits_select_mapping(classification.target.to_metadata(), candidate)
                )
            ):
                return str(item.get("reason") or "Mapped protected Acira target remains blocked.")
        return None

    def _unresolved_mapping_reason_after_review_approval(
        self,
        classification: ActionClassification,
        cedar_decision: CedarDecision | None,
    ) -> str | None:
        if cedar_decision is None or cedar_decision.source != "specific_skill_mapping_review":
            return None
        details = classification.details if isinstance(classification.details, dict) else {}
        explicit_probe = isinstance(details.get("page_target_probe"), dict)
        if not explicit_probe:
            return None
        metadata = cedar_decision.metadata if isinstance(cedar_decision.metadata, dict) else {}
        skills = metadata.get("skills") if isinstance(metadata.get("skills"), dict) else {}
        mappings = skills.get("page_mapping") if isinstance(skills.get("page_mapping"), list) else []
        for item in mappings:
            if not isinstance(item, dict):
                continue
            if item.get("status") in {"probe_unavailable", "unmapped_no_candidate"}:
                return (
                    "A URL-scoped Acira skill still has unresolved protected targets on this changed page. "
                    "No browser action was forwarded; recapture the skill or approve a concrete mapping first."
                )
            if item.get("status") == "ambiguous" and not item.get("candidate_key"):
                return (
                    "A URL-scoped Acira skill did not produce a concrete current-page target to approve. "
                    "No browser action was forwarded."
                )
        return None

    async def _reconcile_page_wide_skill_match(
        self,
        classification: ActionClassification,
        context: CDPMessageContext,
        cedar_decision: CedarDecision,
    ) -> CedarDecision | None:
        """Prevent a weak current-target match from stealing a resolved page target.

        The synchronous skill engine only sees the current action target. For
        drifted pages, we also resolve the protected target against the whole
        current DOM. If the protected target clearly resolves elsewhere, the
        current action is unrelated and should not be blocked just because it
        shares broad form structure.
        """

        if classification.target is None:
            return None
        matched_refs = _specific_skill_match_refs(cedar_decision)
        if not matched_refs:
            return None

        page_targets = await self._probe_page_targets(context)
        if not page_targets:
            return None

        current = classification.target.to_metadata()
        current_key = _target_mapping_key(current)
        mappings: list[dict[str, Any]] = []
        should_allow = False

        for skill_id, target_id in matched_refs:
            protected_target = self._find_specific_skill_target(skill_id, target_id)
            if protected_target is None:
                continue

            current_resolution = resolve_target_fingerprint(protected_target.fingerprint, current, _event_facts_from_classification(classification))
            best_candidate: dict[str, Any] | None = None
            best_resolution = None
            competing: list[dict[str, Any]] = []
            for candidate in page_targets:
                resolution = resolve_target_fingerprint(protected_target.fingerprint, candidate, _event_facts_from_classification(classification))
                if resolution.score >= max(40, protected_target.threshold - 20):
                    competing.append(
                        {
                            "target": _target_summary_from_metadata(candidate),
                            "score": resolution.score,
                            "kind": resolution.kind,
                            "signals": list(resolution.matched_signals)[:8],
                        }
                    )
                if best_resolution is None or resolution.score > best_resolution.score:
                    best_candidate = candidate
                    best_resolution = resolution

            if best_candidate is None or best_resolution is None:
                continue

            best_key = _target_mapping_key(best_candidate)
            resolved_elsewhere = (
                best_key
                and current_key
                and best_key != current_key
                and best_resolution.kind in {"strong_match", "structural_slot_match"}
                and best_resolution.score >= protected_target.threshold
                and best_resolution.score >= current_resolution.score + 8
            )
            mappings.append(
                {
                    "skill_id": skill_id,
                    "target_id": target_id,
                    "current_target": _target_summary_from_metadata(current),
                    "current_resolution": current_resolution.to_metadata(),
                    "best_candidate": _target_summary_from_metadata(best_candidate),
                    "best_resolution": best_resolution.to_metadata(),
                    "competing_candidates": competing[:5],
                    "mapping": "resolved_elsewhere" if resolved_elsewhere else "current_or_unclear",
                }
            )
            if resolved_elsewhere:
                should_allow = True

        if not should_allow:
            return None

        metadata = {
            "engine": "cedar",
            "mode": "deterministic_adapter",
            "skills": {
                "in_scope": True,
                "matches": [],
                "missing_targets": [],
                "page_mapping": mappings,
                "page_target_probe": {"status": "ok" if page_targets else "empty", "candidate_count": len(page_targets)},
            },
        }
        return CedarDecision(
            decision="allow",
            reason="Page-wide Acira mapping resolved the protected target elsewhere; current action target is unrelated.",
            source="specific_skill_mapping",
            skill_decision_id=cedar_decision.skill_decision_id,
            metadata=metadata,
        )

    def _find_specific_skill_target(self, skill_id: str, target_id: str) -> SpecificSkillTarget | None:
        for skill in self.config.specific_skills:
            if skill.id != skill_id:
                continue
            for protected_target in skill.targets:
                if protected_target.id == target_id:
                    return protected_target
        return None

    async def _probe_page_targets(self, context: CDPMessageContext) -> list[dict[str, Any]] | None:
        try:
            response = await asyncio.wait_for(
                context.probe(
                    "Runtime.evaluate",
                    {
                        "expression": f"({PAGE_TARGETS_PROBE_JS})()",
                        "returnByValue": True,
                        "silent": True,
                    },
                ),
                timeout=self.config.probe_timeout,
            )
        except Exception:
            return None

        value = _response_value(response)
        if not isinstance(value, list):
            return None
        targets: list[dict[str, Any]] = []
        for item in value:
            target = _target_from_probe_value(item)
            if target is not None:
                targets.append(target.to_metadata())
        return targets

    def _has_rules_for_actions(self, *actions: str) -> bool:
        return any(self.config.matching_rules(action) for action in actions)

    async def _classify_runtime(
        self,
        method: str,
        params: dict[str, Any],
        context: CDPMessageContext,
    ) -> ActionClassification:
        source_field = "expression" if method == "Runtime.evaluate" else "functionDeclaration"
        source = params.get(source_field)
        expression = source if isinstance(source, str) else ""
        target_hint = _runtime_target_hint(params)

        if method == "Runtime.evaluate" and _is_acira_page_targets_probe_js(expression):
            return ActionClassification(
                method=method,
                action="read_only_js",
                reason="Acira page-target mapping inspection",
                read_only=True,
                meaningful=False,
                details=_runtime_js_details(
                    expression,
                    category="read_only_js",
                    intent="Acira page target mapping probe",
                    source_field=source_field,
                    target_hint=target_hint,
                    snippet_chars=self.config.runtime_js_log_snippet_chars,
                ),
            )

        if method == "Runtime.callFunctionOn" and _is_acira_runtime_object_probe_js(expression):
            target_hint = _runtime_target_hint(params)
            return ActionClassification(
                method=method,
                action="read_only_js",
                reason="Acira runtime object target inspection",
                read_only=True,
                meaningful=False,
                details=_runtime_js_details(
                    expression,
                    category="read_only_js",
                    intent="Acira runtime object target probe",
                    source_field=source_field,
                    target_hint=target_hint,
                    snippet_chars=self.config.runtime_js_log_snippet_chars,
                ),
            )

        adapter_helper = await self.framework_adapters.classify_runtime_helper(
            RuntimeHelperContext(
                method=method,
                expression=expression,
                params=params,
                source_field=source_field,
                target_hint=target_hint,
                cdp_context=context,
                policy=self,
                adapter_id="",
                helper_id="",
                full_helper_id="",
                config=None,  # type: ignore[arg-type]
            )
        )
        if adapter_helper is not None:
            return adapter_helper

        sensitive_reason = self._sensitive_js_reason(expression)
        if sensitive_reason is not None:
            target = await self._probe_runtime_object_target(params, context)
            return ActionClassification(
                method=method,
                action="dangerous_js",
                reason=f"JavaScript contains {sensitive_reason}",
                confidence=0.95,
                meaningful=True,
                target=target,
                details=_runtime_js_details(
                    expression,
                    category="dangerous_js",
                    intent=_js_intent_from_reason(sensitive_reason),
                    source_field=source_field,
                    target_hint=_runtime_target_hint_from_target(target) or target_hint,
                    snippet_chars=self.config.runtime_js_log_snippet_chars,
                ),
            )

        mutation_reason = self._generic_js_mutation_reason(expression)
        if mutation_reason is None and self._is_read_only_js(expression):
            return ActionClassification(
                method=method,
                action="read_only_js",
                reason="read-only JavaScript inspection pattern",
                read_only=True,
                details=_runtime_js_details(
                    expression,
                    category="read_only_js",
                    intent=_read_only_js_intent(expression),
                    source_field=source_field,
                    target_hint=target_hint,
                    snippet_chars=self.config.runtime_js_log_snippet_chars,
                ),
            )

        intent = "unclassified JavaScript"
        if mutation_reason is not None:
            intent = f"unrecognized DOM mutation: {mutation_reason}"
        elif not expression:
            intent = "Runtime command did not include JavaScript source"

        target = await self._probe_runtime_object_target(params, context)
        return ActionClassification(
            method=method,
            action="unknown_js",
            reason="unknown JavaScript requires human review",
            confidence=0.5,
            meaningful=True,
            target=target,
            details=_runtime_js_details(
                expression,
                category="unknown_js",
                intent=intent,
                source_field=source_field,
                target_hint=_runtime_target_hint_from_target(target) or target_hint,
                snippet_chars=self.config.runtime_js_log_snippet_chars,
            ),
        )

    async def _classify_key_event(
        self,
        method: str,
        params: dict[str, Any],
        context: CDPMessageContext,
    ) -> ActionClassification:
        key = str(params.get("key") or params.get("code") or "")
        event_type = str(params.get("type") or "")
        text = params.get("text") or params.get("unmodifiedText")

        if key in {"Enter", "NumpadEnter"}:
            target = await self._probe_active_element(context)
            action = "submit" if target is None or target.is_submit or target.is_form_control else "keyboard"
            return ActionClassification(
                method=method,
                action=action,
                reason="Enter key activation",
                confidence=0.72 if target and not target.is_submit else 0.82,
                meaningful=True,
                target=target,
            )

        if key in {"Tab", "Shift+Tab"}:
            target = await self._probe_active_element(context) if self._has_rules_for_actions("focus") else None
            return ActionClassification(method=method, action="focus", reason="keyboard focus movement", meaningful=True, target=target)

        if key in {" ", "Space", "Spacebar"} and self._has_rules_for_actions("check"):
            target = await self._probe_active_element(context)
            if target and target.is_checkbox:
                return ActionClassification(method=method, action="check", reason="checkbox keyboard activation", meaningful=True, target=target)

        if _is_printable_text_key_event(params):
            target = await self._probe_active_element(context)
            return ActionClassification(
                method=method,
                action="form_input",
                reason="keyboard text input",
                confidence=max(0.82, target.confidence if target else 0.0),
                meaningful=True,
                target=target,
            )

        if key.startswith(("Arrow", "Page", "Home", "End")) and self._has_rules_for_actions("select"):
            target = await self._probe_active_element(context)
            if target and target.is_select:
                return ActionClassification(method=method, action="select", reason="select keyboard interaction", meaningful=True, target=target)

        if event_type == "char" or (isinstance(text, str) and text):
            target = await self._probe_active_element(context)
            return ActionClassification(
                method=method,
                action="form_input",
                reason="keyboard text input",
                confidence=max(0.82, target.confidence if target else 0.0),
                meaningful=True,
                target=target,
            )

        return ActionClassification(method=method, action="keyboard", reason="keyboard event", confidence=0.7, meaningful=True)

    async def _classify_pointer_event(
        self,
        method: str,
        params: dict[str, Any],
        context: CDPMessageContext,
        *,
        pointer_kind: str,
    ) -> ActionClassification:
        target = await self._probe_point(params, context)
        event_type = str(params.get("type") or "")
        is_activation = event_type in {"mousePressed", "mouseReleased", "touchStart", "touchEnd"} or pointer_kind == "touch"

        if target and is_activation:
            if target.is_native_submit is True or (target.is_native_submit is None and target.is_submit):
                return ActionClassification(method=method, action="submit", reason=f"{pointer_kind} submit activation", confidence=target.confidence, meaningful=True, target=target)
            if target.is_checkbox:
                return ActionClassification(method=method, action="check", reason=f"{pointer_kind} checkbox activation", confidence=target.confidence, meaningful=True, target=target)
            if target.is_select:
                return ActionClassification(method=method, action="select", reason=f"{pointer_kind} select activation", confidence=target.confidence, meaningful=True, target=target)
            if target.is_button:
                return ActionClassification(method=method, action="form_control_activation", reason=f"{pointer_kind} button activation", confidence=target.confidence, meaningful=True, target=target)
            if target.is_editable or target.is_form_control:
                return ActionClassification(method=method, action="focus", reason=f"{pointer_kind} form-control focus", confidence=target.confidence, meaningful=True, target=target)

        if event_type in {"mouseMoved", "touchMove"}:
            return ActionClassification(method=method, action=f"{pointer_kind}_move", reason=f"{pointer_kind} movement", confidence=0.8, target=target)

        return ActionClassification(method=method, action=pointer_kind, reason=f"{pointer_kind} event", confidence=0.5, meaningful=is_activation, target=target)

    async def _probe_point(self, params: dict[str, Any], context: CDPMessageContext) -> GuardTarget | None:
        x = params.get("x")
        y = params.get("y")
        if x is None or y is None:
            points = params.get("touchPoints")
            if isinstance(points, list) and points:
                first = points[0] if isinstance(points[0], dict) else {}
                x = first.get("x")
                y = first.get("y")
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            return None

        expression = f"({_point_probe_js()})({json.dumps(x)}, {json.dumps(y)})"
        return await self._runtime_probe_for_target(expression, context)

    async def _probe_active_element(self, context: CDPMessageContext) -> GuardTarget | None:
        expression = f"({ACTIVE_ELEMENT_PROBE_JS})()"
        return await self._runtime_probe_for_target(expression, context)

    async def _runtime_probe_for_target(self, expression: str, context: CDPMessageContext) -> GuardTarget | None:
        try:
            response = await asyncio.wait_for(
                context.probe(
                    "Runtime.evaluate",
                    {
                        "expression": expression,
                        "returnByValue": True,
                        "silent": True,
                    },
                ),
                timeout=self.config.probe_timeout,
            )
        except (asyncio.TimeoutError, RuntimeError):
            return None

        value = _response_value(response)
        return _target_from_probe_value(value)

    async def _probe_runtime_object_target(
        self,
        params: dict[str, Any],
        context: CDPMessageContext,
    ) -> GuardTarget | None:
        object_id = params.get("objectId")
        if not isinstance(object_id, str) or not object_id:
            return None

        try:
            response = await asyncio.wait_for(
                context.probe(
                    "Runtime.callFunctionOn",
                    {
                        "objectId": object_id,
                        "functionDeclaration": RUNTIME_OBJECT_PROBE_JS,
                        "returnByValue": True,
                        "silent": True,
                    },
                ),
                timeout=self.config.probe_timeout,
            )
        except (asyncio.TimeoutError, RuntimeError):
            return None

        value = _response_value(response)
        return _target_from_probe_value(value)

    async def _probe_adapter_overlay_target(
        self,
        rect: tuple[float, float, float, float],
        context: CDPMessageContext,
        ignored_selectors: tuple[str, ...],
    ) -> GuardTarget | None:
        x, y, width, height = rect
        expression = f"({_point_probe_js(ignored_selectors)})({json.dumps(x + width / 2)}, {json.dumps(y + height / 2)})"
        return await self._runtime_probe_for_target(expression, context)

    async def _probe_dom_target(self, params: dict[str, Any], context: CDPMessageContext) -> GuardTarget | None:
        if isinstance(params.get("objectId"), str) and params.get("objectId"):
            runtime_target = await self._probe_runtime_object_target(params, context)
            if runtime_target is not None:
                return runtime_target

        resolve_params: dict[str, Any] = {}
        for key in ("nodeId", "backendNodeId"):
            if key in params:
                resolve_params[key] = params[key]

        if resolve_params:
            try:
                response = await asyncio.wait_for(
                    context.probe("DOM.resolveNode", resolve_params),
                    timeout=self.config.probe_timeout,
                )
            except (asyncio.TimeoutError, RuntimeError):
                response = None

            resolved = ((response or {}).get("result") or {}).get("object")
            object_id = resolved.get("objectId") if isinstance(resolved, dict) else None
            if isinstance(object_id, str) and object_id:
                runtime_target = await self._probe_runtime_object_target({"objectId": object_id}, context)
                if runtime_target is not None:
                    return runtime_target

        describe_params: dict[str, Any] = {}
        for key in ("nodeId", "backendNodeId", "objectId"):
            if key in params:
                describe_params[key] = params[key]

        if not describe_params:
            return None

        try:
            response = await asyncio.wait_for(
                context.probe("DOM.describeNode", describe_params),
                timeout=self.config.probe_timeout,
            )
        except (asyncio.TimeoutError, RuntimeError):
            return None

        node = ((response or {}).get("result") or {}).get("node")
        target = _target_from_dom_node(node)
        if target is None or target.url:
            return target

        url = await self._probe_page_url(context)
        return replace(target, url=url) if url else target

    async def _probe_page_url(self, context: CDPMessageContext) -> str | None:
        try:
            response = await asyncio.wait_for(
                context.probe(
                    "Runtime.evaluate",
                    {
                        "expression": "location.href",
                        "returnByValue": True,
                        "silent": True,
                    },
                ),
                timeout=self.config.probe_timeout,
            )
        except (asyncio.TimeoutError, RuntimeError):
            return None

        value = _response_value(response)
        return value if isinstance(value, str) and value else None

    def _is_read_only_js(self, expression: str) -> bool:
        normalized = _normalize_js(expression)
        if not normalized:
            return False

        if self._sensitive_js_reason(normalized) is not None or self._generic_js_mutation_reason(normalized) is not None:
            return False

        deadline = time.monotonic()+MATCH_BUDGET
        for pattern in self.config.read_only_js_allowlist:
            pattern_text = _normalize_js(pattern)
            if pattern_text.startswith("regex:"):
                try:
                    if compile_pattern(pattern_text[6:]).fullmatch(normalized, timeout=remaining(deadline)):
                        return True
                except (TimeoutError, ValueError, re.error):
                    return False
            elif pattern_text.endswith("*") and normalized.startswith(pattern_text[:-1]):
                return True
            elif normalized == pattern_text:
                return True

        return any(pattern.search(normalized) for pattern in READ_ONLY_JS_PATTERNS)

    @staticmethod
    def _sensitive_js_reason(expression: str) -> str | None:
        normalized = _normalize_js(expression)
        if not normalized:
            return None

        for pattern, reason in SENSITIVE_JS_PATTERNS:
            if pattern.search(normalized):
                return reason
        return None

    @staticmethod
    def _generic_js_mutation_reason(expression: str) -> str | None:
        normalized = _normalize_js(expression)
        if not normalized:
            return None

        for pattern in GENERIC_JS_MUTATION_PATTERNS:
            match = pattern.search(normalized)
            if match:
                token = match.group(0).strip()
                return token[:80]
        return None

    async def _ask(
        self,
        payload: str | bytes,
        context: CDPMessageContext,
        classification: ActionClassification,
        reason: str,
    ) -> bool:
        binding = None
        revision = self.policy_revision
        expires = time.monotonic() + 300
        is_framework_action = context.websocket_path.startswith("/acira/action/")
        if getattr(self, "require_bound_review", False) and not is_framework_action and classification.action in {
            "click", "form_input", "select", "submit", "check", "file_upload", "form_control_activation"
        }:
            from .operation import bind_cdp
            try:
                binding = await bind_cdp(context.send_probe, classification.target, classification.action, session_id=context.session_id)
                classification = replace(classification, details={**(classification.details or {}), "operation_evidence": binding.evidence})
            except Exception:
                return False
        request = GuardReviewRequest(
            payload=payload,
            context=context,
            classification=classification,
            reason=reason,
        )

        try:
            async with self.review_pause.hold():
                release_on_review_exit = _classification_releases_pause_on_review_exit(classification)
                if self._pause_coordinator is None or (is_framework_action and not release_on_review_exit):
                    allowed = await self._ask_backend(request)
                else:
                    async with self._pause_coordinator.hold_for_review(release_on_exit=release_on_review_exit):
                        allowed = await self._ask_backend(request)
            if self.before_decision is not None:
                await self.before_decision()
            return allowed and time.monotonic() < expires and self.policy_revision == revision and (binding is None or await binding.revalidate())
        finally:
            if binding is not None:
                await binding.release()

    def _general_review_missing_evidence(
        self, classification: ActionClassification,
    ) -> tuple[dict[str, Any], list[str]] | None:
        details = classification.details or {}
        evidence = details.get("operation_evidence") or {}
        cedar = details.get("cedar") or {}
        detections = (cedar.get("general_skills") or {}).get("detections") or []
        skills = {skill.id: skill for skill in self.config.general_skills}
        for detection in detections:
            skill = skills.get(detection.get("skill_id"))
            requirement = skill.review_requirements.get(detection.get("risk"), {}) if skill else {}
            verified = evidence.get("verified_fields") or {}
            missing = [name for name in requirement.get("required_fields", [])
                       if verified.get(name) in (None, "", "[REDACTED]")]
            if requirement.get("request_binding") and evidence.get("request_bound") is not True:
                missing.append("verified request binding")
            if missing:
                return detection, missing
        return None

    async def _ask_backend(self, request: GuardReviewRequest) -> bool:
        # Apply declared evidence requirements before every reviewer, including
        # legacy config/custom callbacks which do not use ProjectRuntime.
        incomplete = self._general_review_missing_evidence(request.classification)
        if incomplete is not None:
            detection, missing = incomplete
            reason = "Required operation evidence missing: " + ", ".join(missing) + ". Use guard.submit(...) to bind the actual destination and values."
            durable = getattr(self, "durable_event", None)
            if durable is not None:
                durable("review_evidence_missing", missing=missing,
                    policy_id=detection.get("skill_id"), risk=detection.get("risk"),
                    reason="Use the supported submit() contract to bind the reviewed destination and values")
            self._terminal_decision("BLOCK", request.context, request.classification, reason, color="red")
            return False
        if self.review_backend is not None:
            # ProjectRuntime owns the private scope hash and projects its
            # external reviewer only after authorization data has been read.
            private_scope_owner = False
            if getattr(self.review_backend, "_acira_private_review_scope", False) is True:
                from acira.runtime import ProjectRuntime
                private_scope_owner = isinstance(self.review_backend, ProjectRuntime)
            view = request if private_scope_owner else redact_review_request(request)
            result = await self.review_backend.ask(view)
            return _review_result_allows(result)

        return await self._terminal_review(request)

    async def _terminal_review(self, request: GuardReviewRequest) -> bool:
        result = await TerminalReviewBackend(self.config).ask(request)
        return _review_result_allows(result)

    def _terminal_preflight_snapshot(self, snapshot: PageRiskSnapshot, *, trigger: str) -> None:
        if not self.config.terminal_log:
            return
        metadata = dict(snapshot.cedar_metadata)
        metadata["preflight"] = snapshot.to_metadata()
        summary = _general_skill_summary_parts(metadata, source="general_skill_preflight")
        risks = summary.get("detected") or "none"
        policy = summary.get("policy") or "none"
        if (
            snapshot.decision == "allow"
            and risks == "none"
            and self._preflight_url_was_approved(snapshot.url)
        ):
            return
        page_key = _normalize_mapping_url(snapshot.url) or str(snapshot.page_signature or "")
        dedupe_key = (
            page_key,
            snapshot.decision,
            str(snapshot.skill_decision_id or ""),
        )
        if dedupe_key in self._logged_preflight_snapshots:
            return
        self._logged_preflight_snapshots.add(dedupe_key)
        if len(self._logged_preflight_snapshots) > 128:
            self._logged_preflight_snapshots = set(list(self._logged_preflight_snapshots)[-128:])

        elapsed = snapshot.readiness.get("elapsed_ms") if isinstance(snapshot.readiness, dict) else None
        timed_out = bool(snapshot.readiness.get("timed_out")) if isinstance(snapshot.readiness, dict) else False
        color = "red" if snapshot.decision == "block" else "yellow" if snapshot.decision == "ask" else "blue"
        text = (
            "[Acira Guard] PAGE_PREFLIGHT "
            f"decision={snapshot.decision} trigger={trigger} status={snapshot.status} "
            f"url={json.dumps(redact_value(snapshot.url or ''), ensure_ascii=True)} "
            f"title={json.dumps(redact_value(snapshot.title or ''), ensure_ascii=True)} "
            f"risks={json.dumps(risks, ensure_ascii=True)} "
            f"policy={json.dumps(policy, ensure_ascii=True)} "
            f"elapsed_ms={elapsed if isinstance(elapsed, (int, float)) else 'n/a'} "
            f"timed_out={str(timed_out).lower()}"
        )
        print(_color(text, color), file=sys.stderr, flush=True)

    def _terminal_preflight_status(self, *, scope_key: str, trigger: str, status: str) -> None:
        if not self.config.terminal_log:
            return
        text = (
            "[Acira Guard] PAGE_PREFLIGHT "
            f"decision=unknown trigger={trigger} status={json.dumps(status, ensure_ascii=True)} "
            f"scope={json.dumps(scope_key, ensure_ascii=True)}"
        )
        print(_color(text, "yellow"), file=sys.stderr, flush=True)

    @staticmethod
    def _blocked_reason(
        reason: str,
        classification: ActionClassification,
        *,
        cedar_decision: CedarDecision | None = None,
    ) -> str:
        guidance = _agent_block_guidance(classification, cedar_decision)
        suffix = f" {guidance}" if guidance else ""
        clean_reason = reason.rstrip(".")
        return f"ACIRA_POLICY_BLOCKED_DO_NOT_RETRY: Acira Guard blocked {_blocked_action_label(classification)}: {clean_reason}.{suffix}"

    def _terminal_decision(
        self,
        label: str,
        context: CDPMessageContext,
        classification: ActionClassification,
        reason: str,
        *,
        color: str,
        cedar_decision: CedarDecision | None = None,
    ) -> None:
        if not self.config.terminal_log:
            return
        is_runtime_js = classification.action in RUNTIME_JS_ACTIONS
        if classification.read_only and not self.config.terminal_log_read_only and not is_runtime_js:
            return
        if label == "ALLOW" and not classification.meaningful and not self.config.terminal_log_read_only and not is_runtime_js:
            return

        target = classification.target
        target_text = ""
        if target:
            target_name = _target_label(target)
            target_text = f" target={target.tag or '?'}:{target_name}" if target_name else f" target={target.tag or '?'}"
        elif classification.details and classification.details.get("target"):
            target_text = f" target={classification.details['target']}"

        detail_text = ""
        if is_runtime_js and classification.details:
            detail_text = _runtime_js_log_suffix(classification.details)

        cedar_text = ""
        if cedar_decision and cedar_decision.metadata:
            if cedar_decision.source in {"general_skill", "general_skill_preflight"} or isinstance(
                cedar_decision.metadata.get("general_skills"), dict
            ):
                cedar_text = _general_skill_terminal_suffix(cedar_decision)
            else:
                skills = (cedar_decision.metadata.get("skills") or {}) if isinstance(cedar_decision.metadata, dict) else {}
                match_count = len(skills.get("matches") or []) if isinstance(skills, dict) else 0
                missing_count = len(skills.get("missing_targets") or []) if isinstance(skills, dict) else 0
                page_mapping = skills.get("page_mapping") if isinstance(skills, dict) and isinstance(skills.get("page_mapping"), list) else []
                page_mapping_all = (
                    skills.get("page_mapping_all")
                    if isinstance(skills, dict) and isinstance(skills.get("page_mapping_all"), list)
                    else page_mapping
                )
                mapping_summary = (
                    skills.get("page_mapping_summary")
                    if isinstance(skills, dict) and isinstance(skills.get("page_mapping_summary"), dict)
                    else {}
                )
                if page_mapping_all:
                    mapped_count = int(mapping_summary.get("concrete_targets") or 0)
                    enforced_count = int(mapping_summary.get("already_enforced_targets") or 0)
                    review_count = int(mapping_summary.get("review_targets") or len(page_mapping))
                    unresolved_count = int(mapping_summary.get("unresolved_targets") or missing_count)
                    cedar_text = (
                        f" cedar_source={cedar_decision.source}"
                        f" skill_decision={cedar_decision.skill_decision_id or 'n/a'}"
                        f" skill_matches={match_count}"
                        f" mapped_targets={mapped_count}"
                        f" enforced_targets={enforced_count}"
                        f" review_targets={review_count}"
                        f" unresolved_targets={unresolved_count}"
                    )
                elif match_count or missing_count or cedar_decision.skill_decision_id:
                    cedar_text = (
                        f" cedar_source={cedar_decision.source}"
                        f" skill_decision={cedar_decision.skill_decision_id or 'n/a'}"
                        f" skill_matches={match_count}"
                        f" missing_targets={missing_count}"
                    )

        text = (
            f"[Acira Guard] {label} id={context.message_id} "
            f"method={classification.method} action={classification.action}{target_text} "
            f"reason={reason}{detail_text}{cedar_text}"
        )
        print(_color(redact_value(text), color), file=sys.stderr, flush=True)

    def _terminal_submit_block_message(
        self,
        context: CDPMessageContext,
        classification: ActionClassification,
    ) -> None:
        if not self.config.terminal_log:
            return
        is_pointer_submit = (
            classification.action == "submit"
            and classification.method in {"Input.dispatchMouseEvent", "Input.dispatchTouchEvent"}
        )
        is_runtime_submit = (
            classification.action == "dangerous_js"
            and classification.method in {"Runtime.evaluate", "Runtime.callFunctionOn"}
            and classification.target is not None
            and classification.target.is_submit
        )
        if not (is_pointer_submit or is_runtime_submit):
            return

        target = classification.target
        target_text = ""
        if target:
            label = _target_label(target)
            if label:
                target_text = f' "{label}"'

        verb = "click" if is_pointer_submit else "activate"
        channel = "" if is_pointer_submit else " through JavaScript"
        text = (
            f"[Acira Guard] Agent tried to {verb} the submit button{target_text}{channel}; "
            f"Acira blocked it before Chrome received the CDP command. id={context.message_id}"
        )
        print(_color(text, "green"), file=sys.stderr, flush=True)


def _runtime_js_details(
    expression: str,
    *,
    category: str,
    intent: str,
    source_field: str,
    target_hint: str | None,
    snippet_chars: int,
) -> dict[str, Any]:
    normalized = _normalize_js(expression)
    lines = 0 if not expression else len(expression.splitlines()) or 1
    return {
        "category": category,
        "intent": intent,
        "source": source_field,
        "snippet": _safe_one_line_snippet(expression, snippet_chars),
        "hash": hashlib.sha256(expression.encode("utf-8", errors="replace")).hexdigest()[:12],
        "chars": len(expression),
        "lines": lines,
        "target": target_hint,
        "normalized_chars": len(normalized),
    }


def _runtime_target_hint(params: dict[str, Any]) -> str | None:
    for key in ("objectId", "backendNodeId", "nodeId", "executionContextId", "uniqueContextId"):
        value = params.get(key)
        if isinstance(value, (str, int, float)) and str(value):
            return f"{key}={_truncate(str(value), 80)}"
    return None


def _runtime_target_hint_from_target(target: GuardTarget | None) -> str | None:
    if target is None:
        return None
    target_name = _target_label(target)
    if target_name:
        return f"{target.tag or '?'}:{_truncate(target_name, 80)}"
    return target.tag or None


def _target_label(target: GuardTarget) -> str:
    is_form_target = target.tag in {"input", "select", "textarea"} or target.is_editable
    if is_form_target and not target.is_button:
        candidates = (
            target.aria_label,
            target.title,
            target.nearby_text,
            _humanize_identifier(target.id),
            _humanize_identifier(target.name),
            target.text,
            target.type,
            target.role,
        )
    else:
        candidates = (
            target.text,
            target.aria_label,
            target.title,
            _humanize_identifier(target.id),
            _humanize_identifier(target.name),
            target.nearby_text,
            target.type,
            target.role,
        )
    for candidate in candidates:
        normalized = _clean_target_label(candidate)
        if normalized:
            return normalized
    return ""


def _clean_target_label(value: str | None) -> str:
    raw = re.sub(r"\s+", " ", str(value or "")).strip()
    if not raw:
        return ""
    return _truncate(raw, 120)


def _humanize_identifier(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    spaced = re.sub(r"[_-]+", " ", raw)
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", spaced)
    return spaced[:1].upper() + spaced[1:]


def _safe_native_select_payload(context: CDPMessageContext, params: dict[str, Any]) -> str | None:
    if not isinstance(context.parsed_message, dict):
        return None
    replacement = dict(context.parsed_message)
    replacement_params = dict(params)
    replacement_params["functionDeclaration"] = SAFE_NATIVE_SELECT_JS
    replacement_params["returnByValue"] = True
    replacement["params"] = replacement_params
    return json.dumps(replacement, separators=(",", ":"), ensure_ascii=True)


def _js_intent_from_reason(reason: str) -> str:
    if "click" in reason or "submit" in reason:
        return "programmatic activation or submission"
    if "event" in reason:
        return "synthetic event execution"
    if "form/control" in reason:
        return "form or control mutation"
    if "storage" in reason or "cookie" in reason:
        return "browser storage mutation"
    return "sensitive JavaScript mutation"


def _read_only_js_intent(expression: str) -> str:
    normalized = _normalize_js(expression).lower()
    if "readystatechange" in normalized or "readystate" in normalized:
        return "page readiness inspection"
    if "value" in normalized or "checked" in normalized or "selected" in normalized or "formdata" in normalized:
        return "form value read snapshot"
    if "queryselector" in normalized or "getelement" in normalized or "closest" in normalized:
        return "DOM query and element inspection"
    if "scroll" in normalized or "boundingclientrect" in normalized or "computedstyle" in normalized:
        return "layout and scroll inspection"
    if "geteventlisteners" in normalized:
        return "event-listener inspection"
    if "performance" in normalized or "images" in normalized or "fonts" in normalized:
        return "resource readiness inspection"
    return "read-only page inspection"


def _runtime_js_log_suffix(details: dict[str, Any]) -> str:
    parts = [
        f" category={details.get('category')}",
        f" intent={json.dumps(str(details.get('intent') or ''), ensure_ascii=True)}",
        f" chars={details.get('chars')}",
        f" lines={details.get('lines')}",
        f" hash={details.get('hash')}",
    ]
    snippet = details.get("snippet")
    if snippet:
        parts.append(f" snippet={json.dumps(str(snippet), ensure_ascii=True)}")
    return "".join(parts)


def _runtime_js_review_prompt(request: GuardReviewRequest) -> str:
    classification = request.classification
    details = classification.details or {}
    context = request.context
    target = details.get("target") or "n/a"
    snippet = str(details.get("snippet") or "")
    if not snippet:
        snippet = "(empty source)"
    return (
        "Acira Guard HITL review: unknown JavaScript\n"
        f"  id={context.message_id} method={classification.method} category={details.get('category', classification.action)}\n"
        f"  target={target}\n"
        f"  intent={details.get('intent') or classification.reason}\n"
        f"  snippet={json.dumps(snippet, ensure_ascii=True)}\n"
        f"  hash={details.get('hash', 'n/a')} chars={details.get('chars', 0)} lines={details.get('lines', 0)}\n"
        "Allow this CDP command? [y/N] "
    )


def _is_mapping_review(request: GuardReviewRequest) -> bool:
    details = request.classification.details if isinstance(request.classification.details, dict) else {}
    source = str(details.get("cedar_source") or "")
    if source:
        return source == "specific_skill_mapping_review"
    cedar = _cedar_metadata_from_classification(request.classification)
    skills = cedar.get("skills") if isinstance(cedar.get("skills"), dict) else {}
    return request.classification.action == "mapping_preflight" and bool(skills.get("page_mapping"))


def _is_general_skill_review(request: GuardReviewRequest) -> bool:
    details = request.classification.details if isinstance(request.classification.details, dict) else {}
    source = str(details.get("cedar_source") or "")
    if source:
        return source in {"general_skill", "general_skill_preflight"}
    cedar = _cedar_metadata_from_classification(request.classification)
    return isinstance(cedar.get("general_skills"), dict) or isinstance(cedar.get("general_skill_decision"), dict)


def _general_skill_review_prompt(request: GuardReviewRequest) -> str:
    classification = request.classification
    cedar = _cedar_metadata_from_classification(classification)
    source = _string_or_none((classification.details or {}).get("cedar_source") if isinstance(classification.details, dict) else None)
    summary = _general_skill_summary_parts(cedar, source=source)
    page_review = source == "general_skill_preflight" or classification.action == "page_preflight"
    command_label = _review_command_label(classification)
    lines = [
        "Acira Guard HITL review: page risk" if page_review else "Acira Guard HITL review: browser action risk",
        f"  id={request.context.message_id} method={classification.method} action={classification.action}",
        f"  Why: {request.reason}",
    ]
    if classification.target is not None:
        lines.append(f"  Target: {_review_target_summary(classification.target)}")
        target_context = _review_target_context(classification.target)
        if target_context:
            lines.append(f"  Context: {target_context}")
    action_detail = _review_action_detail(classification)
    if action_detail:
        lines.append(f"  Action detail: {action_detail}")
    if summary.get("source"):
        lines.append(f"  Source: {summary['source']}")
    if summary.get("policy"):
        lines.append(f"  Policy: {summary['policy']}")
    if summary.get("detected"):
        lines.append(f"  Detected risks: {summary['detected']}")
    if summary.get("confidence"):
        lines.append(f"  Confidence: {summary['confidence']}")
    if summary.get("rules"):
        lines.append(f"  Matched rules: {summary['rules']}")
    if summary.get("page"):
        lines.append(f"  Page: {summary['page']}")
    if page_review:
        lines.append("  Approval scope: allow the agent to operate on this page URL. Field and submit policies still apply.")
        lines.append("Allow the agent to operate on this page? [y/N] ")
    elif _classification_opens_cdp_followup_transaction(classification):
        lines.append("  Approval scope: only this one framework action on this target, plus its low-level CDP follow-up commands.")
        lines.append("Allow this one browser action? [y/N] ")
    else:
        lines.append(f"Allow this {command_label}? [y/N] ")
    return "\n".join(lines)


def _review_command_label(classification: ActionClassification) -> str:
    details = classification.details if isinstance(classification.details, dict) else {}
    display_name = _string_or_none(details.get("display_name"))
    if display_name:
        return display_name
    return "browser action" if _classification_supervised_runtime(classification) else "CDP command"


def _classification_capabilities(classification: ActionClassification) -> dict[str, Any]:
    details = classification.details if isinstance(classification.details, dict) else {}
    capabilities = details.get("action_capabilities")
    return capabilities if isinstance(capabilities, dict) else {}


def _classification_releases_pause_on_review_exit(classification: ActionClassification) -> bool:
    return bool(_classification_capabilities(classification).get("release_pause_on_review_exit"))


def _classification_supervised_runtime(classification: ActionClassification) -> bool:
    return bool(_classification_capabilities(classification).get("supervised_runtime"))


def _classification_opens_cdp_followup_transaction(classification: ActionClassification) -> bool:
    return bool(_classification_capabilities(classification).get("opens_cdp_followup_transaction"))


def _review_target_summary(target: GuardTarget) -> str:
    label = _target_label(target)
    kind = target.tag or "element"
    details: list[str] = []
    if target.type:
        details.append(f"type={target.type}")
    if target.role:
        details.append(f"role={target.role}")
    if target.name:
        details.append(f"name={target.name}")
    if target.id:
        details.append(f"id={target.id}")
    detail_text = "; ".join(details)
    if label and detail_text:
        return f'{kind} "{label}" ({detail_text})'
    if label:
        return f'{kind} "{label}"'
    if detail_text:
        return f"{kind} ({detail_text})"
    return kind


def _review_target_context(target: GuardTarget) -> str:
    label = _target_label(target).lower()
    parts: list[str] = []
    for heading, value in (("section", target.section_heading), ("nearby", target.nearby_text)):
        text = _clean_target_label(value)
        if not text:
            continue
        if text.lower() == label:
            continue
        parts.append(f"{heading}={json.dumps(_truncate(text, 140), ensure_ascii=True)}")
    return "; ".join(parts[:2])


def _review_action_detail(classification: ActionClassification) -> str:
    details = classification.details if isinstance(classification.details, dict) else {}
    if classification.action == "form_input":
        length = details.get("text_length")
        if isinstance(length, int):
            return f"redacted text entry, length={length}"
        return "redacted text entry"
    if classification.action == "select":
        option = _string_or_none(details.get("option_text"))
        return f"select option {json.dumps(option, ensure_ascii=True)}" if option else "select option"
    if classification.action in {"click", "submit"} and classification.target is not None:
        return f"{classification.action} target"
    return ""


def _general_skill_terminal_suffix(cedar_decision: CedarDecision) -> str:
    metadata = cedar_decision.metadata if isinstance(cedar_decision.metadata, dict) else {}
    summary = _general_skill_summary_parts(metadata, source=cedar_decision.source)
    if not summary:
        return ""
    parts = []
    for key in ("source", "policy", "detected", "confidence", "rules"):
        value = summary.get(key)
        if value:
            parts.append(f" general_{key}={json.dumps(value, ensure_ascii=True)}")
    return "".join(parts)


def _general_skill_summary_parts(metadata: dict[str, Any], *, source: str | None = None) -> dict[str, str]:
    general = metadata.get("general_skills") if isinstance(metadata.get("general_skills"), dict) else {}
    detections = general.get("detections") if isinstance(general.get("detections"), list) else []
    decision = metadata.get("general_skill_decision") if isinstance(metadata.get("general_skill_decision"), dict) else {}
    selected_detection = decision.get("detection") if isinstance(decision.get("detection"), dict) else {}
    policy = decision.get("policy") if isinstance(decision.get("policy"), dict) else {}
    preflight = metadata.get("preflight") if isinstance(metadata.get("preflight"), dict) else {}
    if not detections and not selected_detection and not policy and not preflight:
        return {}

    detected_risks = [
        str(item.get("risk"))
        for item in detections
        if isinstance(item, dict) and item.get("risk")
    ]
    if not detected_risks and selected_detection.get("risk"):
        detected_risks = [str(selected_detection.get("risk"))]

    risk = str(policy.get("risk") or selected_detection.get("risk") or "")
    policy_decision = str(policy.get("decision") or decision.get("decision") or "")
    policy_text = ""
    if risk or policy_decision:
        policy_text = f"{risk or 'n/a'} -> {policy_decision or 'n/a'}"

    confidence = selected_detection.get("confidence")
    confidence_text = f"{float(confidence):.2f}" if isinstance(confidence, (int, float)) else ""
    severity = str(selected_detection.get("severity") or "")
    if severity and confidence_text:
        confidence_text = f"{confidence_text} ({severity})"
    elif severity:
        confidence_text = severity

    rules = selected_detection.get("matched_rules") if isinstance(selected_detection.get("matched_rules"), list) else []
    rules = rules if isinstance(rules, list) else []
    rule_text = ",".join(str(item) for item in rules[:6] if str(item))

    page_text = ""
    url = _string_or_none(preflight.get("url"))
    title = _string_or_none(preflight.get("title"))
    if title and url:
        page_text = f"{title} [{url}]"
    elif title or url:
        page_text = title or url or ""

    result: dict[str, str] = {}
    if source:
        result["source"] = source
    if policy_text:
        result["policy"] = policy_text
    if detected_risks:
        result["detected"] = ",".join(detected_risks[:8])
    if confidence_text:
        result["confidence"] = confidence_text
    if rule_text:
        result["rules"] = rule_text
    if page_text:
        result["page"] = page_text
    return result


def _mapping_status_counts(entries: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status") or entry.get("mapping") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _mapping_entry_identity(entry: dict[str, Any]) -> str:
    return "|".join(
        str(part or "")
        for part in (
            entry.get("skill_id"),
            entry.get("target_id"),
            entry.get("source_fingerprint_hash"),
            entry.get("candidate_key"),
            entry.get("status"),
        )
    )


def _humanize_mapping_target_id(target_id: str | None) -> str:
    raw = str(target_id or "").strip()
    if not raw:
        return ""
    raw = re.sub(r"^target-\d+-", "", raw)
    return _humanize_identifier(raw)


def _looks_like_machine_label(value: str | None) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if " " in text:
        return False
    return bool(re.search(r"[-_]|[0-9]{2,}|[a-z][A-Z]", text))


def _mapping_original_label(entry: dict[str, Any]) -> str:
    original = _clean_target_label(str(entry.get("original_label") or ""))
    target_id = str(entry.get("target_id") or "")
    human_target = _humanize_mapping_target_id(target_id)
    if not original or original == "protected target":
        return human_target or "Protected target"
    if _looks_like_machine_label(original) and human_target and human_target.lower() != original.lower():
        return f"{human_target} (captured label: {original})"
    return original


def _mapping_target_summary_label(target: dict[str, Any]) -> str:
    if not isinstance(target, dict) or not target:
        return "no clear current element"
    label = _clean_target_label(str(target.get("label") or ""))
    if not label:
        label = _humanize_identifier(str(target.get("id") or target.get("name") or ""))
    if not label:
        label = "current element"
    tag = str(target.get("tag") or "element")
    input_type = str(target.get("type") or "")
    role = str(target.get("role") or "")
    kind_parts = [part for part in (tag, input_type or role) if part]
    details: list[str] = []
    if kind_parts:
        details.append("/".join(kind_parts))
    section = _clean_target_label(str(target.get("section") or ""))
    if section:
        details.append(f"section={section}")
    target_id = _clean_target_label(str(target.get("id") or ""))
    name = _clean_target_label(str(target.get("name") or ""))
    if target_id:
        details.append(f"id={target_id}")
    if name and name != target_id:
        details.append(f"name={name}")
    if details:
        return f'"{label}" ({"; ".join(details)})'
    return f'"{label}"'


def _mapping_score_kind(entry: dict[str, Any]) -> tuple[Any, Any]:
    resolution = entry.get("best_resolution") or entry.get("resolution") or {}
    if isinstance(resolution, dict):
        return resolution.get("score", entry.get("score")), resolution.get("kind", "unknown")
    return entry.get("score"), "unknown"


def _mapping_signals(entry: dict[str, Any]) -> str:
    signals = entry.get("signals")
    if not isinstance(signals, list) or not signals:
        resolution = entry.get("best_resolution") or entry.get("resolution") or {}
        if isinstance(resolution, dict):
            signals = resolution.get("matched_signals")
    if not isinstance(signals, list) or not signals:
        return ""
    cleaned = [str(item) for item in signals[:5] if str(item)]
    return ", ".join(cleaned)


def _mapping_candidate_options(entry: dict[str, Any], *, limit: int = 3) -> str:
    top_candidates = entry.get("top_candidates") or entry.get("competing_candidates") or []
    if not isinstance(top_candidates, list) or not top_candidates:
        return ""
    options: list[str] = []
    for candidate in top_candidates[:limit]:
        if not isinstance(candidate, dict):
            continue
        target = candidate.get("target") if isinstance(candidate.get("target"), dict) else {}
        label = _mapping_target_summary_label(target)
        score = candidate.get("score")
        kind = candidate.get("kind") or "unknown"
        gate = candidate.get("evidence_gate") if isinstance(candidate.get("evidence_gate"), dict) else {}
        eligibility = candidate.get("candidate_eligibility") if isinstance(candidate.get("candidate_eligibility"), dict) else {}
        disqualifiers = gate.get("hard_disqualifiers") or eligibility.get("hard_disqualifiers") or []
        suffix = ""
        if disqualifiers:
            suffix = f" disqualified={','.join(str(item) for item in disqualifiers[:2])}"
        elif gate.get("reason"):
            suffix = f" reason={gate.get('reason')}"
        options.append(f"{label} score={score} kind={kind}{suffix}")
    return " | ".join(options)


def _format_mapping_entry(entry: dict[str, Any], index: int) -> list[str]:
    original = _mapping_original_label(entry)
    decision = str(entry.get("original_decision") or entry.get("decision") or "block")
    status = str(entry.get("status") or entry.get("mapping") or "unclear")
    score, kind = _mapping_score_kind(entry)
    target_id = str(entry.get("target_id") or "unknown-target")
    skill_name = str(entry.get("skill_name") or entry.get("skill_id") or "skill")
    best = entry.get("best_candidate") if isinstance(entry.get("best_candidate"), dict) else {}
    if _is_concrete_mapping_candidate(entry):
        current = _mapping_target_summary_label(best)
        first = (
            f"    {index}. Original \"{original}\" [{decision}] -> Current {current} "
            f"(status={status}, score={score}, kind={kind})"
        )
    else:
        first = (
            f"    {index}. Original \"{original}\" [{decision}] -> no confident current element "
            f"(status={status}, best_score={score}, kind={kind})"
        )
    lines = [first, f"       Skill: {skill_name}; target_id={target_id}"]
    signals = _mapping_signals(entry)
    if signals:
        lines.append(f"       Signals: {signals}")
    gate = entry.get("evidence_gate") if isinstance(entry.get("evidence_gate"), dict) else {}
    eligibility = entry.get("candidate_eligibility") if isinstance(entry.get("candidate_eligibility"), dict) else {}
    disqualifiers = gate.get("hard_disqualifiers") or eligibility.get("hard_disqualifiers") or []
    soft = gate.get("soft_penalties") or eligibility.get("soft_penalties") or []
    if gate.get("reason"):
        lines.append(f"       Evidence gate: {gate.get('reason')}")
    if disqualifiers:
        lines.append(f"       Disqualified: {', '.join(str(item) for item in disqualifiers[:4])}")
    if soft:
        lines.append(f"       Caveats: {', '.join(str(item) for item in soft[:4])}")
    options = _mapping_candidate_options(entry)
    if options:
        lines.append(f"       Closest current options: {options}")
    return lines


def _mapping_decision_counts(entries: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        decision = str(entry.get("decision") or "unknown").strip().lower() or "unknown"
        counts[decision] = counts.get(decision, 0) + 1
    return counts


def _mapping_sample_labels(entries: list[dict[str, Any]], *, limit: int = 3) -> str:
    labels: list[str] = []
    for entry in entries:
        best_candidate = entry.get("best_candidate") if isinstance(entry.get("best_candidate"), dict) else {}
        label = str(
            entry.get("original_label")
            or entry.get("current_text")
            or entry.get("label")
            or best_candidate.get("label")
            or best_candidate.get("text")
            or entry.get("target_id")
            or ""
        ).strip()
        decision = str(entry.get("decision") or "").strip().lower()
        status = str(entry.get("status") or "").strip().lower()
        if not label:
            continue
        suffix = " ".join(item for item in (decision, status) if item)
        labels.append(f"{label}{f' ({suffix})' if suffix else ''}")
        if len(labels) >= limit:
            break
    hidden = len([entry for entry in entries if isinstance(entry, dict)]) - len(labels)
    if hidden > 0:
        labels.append(f"+{hidden} more")
    return "; ".join(labels)


def _mapping_review_prompt(request: GuardReviewRequest) -> str:
    classification = request.classification
    cedar = _cedar_metadata_from_classification(classification)
    skills = cedar.get("skills") if isinstance(cedar.get("skills"), dict) else {}
    missing = skills.get("missing_targets") if isinstance(skills.get("missing_targets"), list) else []
    mappings = skills.get("page_mapping") if isinstance(skills.get("page_mapping"), list) else []
    all_mappings = (
        skills.get("page_mapping_all")
        if isinstance(skills.get("page_mapping_all"), list)
        else mappings
    )
    summary = skills.get("page_mapping_summary") if isinstance(skills.get("page_mapping_summary"), dict) else {}
    probe = skills.get("page_target_probe") if isinstance(skills.get("page_target_probe"), dict) else {}
    review_mappings = [item for item in mappings if isinstance(item, dict)]
    complete_mappings = [item for item in all_mappings if isinstance(item, dict)]
    already_enforced = [
        item
        for item in complete_mappings
        if str(item.get("status") or "") in {"approved", "rejected"}
    ]
    confident_proposed_mappings = [
        item
        for item in complete_mappings
        if str(item.get("status") or "") == "auto_mapped" and _is_concrete_mapping_candidate(item)
    ]
    judgment_mappings = [
        item
        for item in complete_mappings
        if str(item.get("status") or "") == "ambiguous" and _is_concrete_mapping_candidate(item)
    ]
    unresolved_mappings = [item for item in complete_mappings if not _is_concrete_mapping_candidate(item)]
    lines = [
        "Acira Guard HITL review: page mapping needs human approval",
        f"  Why: {request.reason}",
    ]
    if probe:
        probe_status = probe.get("status") or "unknown"
        probe_count = probe.get("candidate_count", 0)
        probe_url = probe.get("url") or ""
        probe_title = probe.get("title") or ""
        probe_error = probe.get("error") or ""
        probe_line = f"  Page inspection: status={probe_status}, controls_seen={probe_count}"
        if probe_url:
            probe_line += f", url={probe_url}"
        if probe_title:
            probe_line += f", title={probe_title}"
        if probe_error:
            probe_line += f", error={probe_error}"
        lines.append(probe_line)
    review_entries = review_mappings or complete_mappings
    decision_counts = _mapping_decision_counts(review_entries)
    if summary or complete_mappings:
        decision_text = ", ".join(f"{name}={count}" for name, count in sorted(decision_counts.items()) if count)
        lines.append(
            "  Mapping summary: "
            f"{int(summary.get('review_targets') or len(review_entries))} target(s) need this review, "
            f"{int(summary.get('concrete_targets') or len(confident_proposed_mappings) + len(judgment_mappings))} concrete, "
            f"{int(summary.get('unresolved_targets') or len(unresolved_mappings))} unresolved, "
            f"{int(summary.get('total_targets') or len(complete_mappings))} protected target(s) in scope"
            f"{f'; decisions: {decision_text}' if decision_text else ''}."
        )
    current = _agent_target_reference(classification)
    if current:
        lines.append(f"  Agent is trying: {current}")

    sample_labels = _mapping_sample_labels(review_entries)
    if sample_labels:
        lines.append(f"  Sample: {sample_labels}")
    if already_enforced:
        lines.append(f"  Already enforced on this page: {len(already_enforced)} target(s).")
    if judgment_mappings:
        lines.append(f"  Needs human judgment: {len(judgment_mappings)} ambiguous target(s).")
    if unresolved_mappings:
        lines.append(f"  Unresolved/no safe candidate: {len(unresolved_mappings)} target(s).")
    lines.append("  Full mapping evidence is available in dashboard raw details and runtime metadata.")

    concrete_mappings = [item for item in review_mappings if _is_concrete_mapping_candidate(item)]
    any_concrete_mapping = any(_is_concrete_mapping_candidate(item) for item in complete_mappings)
    if not missing and not mappings:
        lines.append("  no credible protected-target mapping was found; recapture may be required.")
    probe_status = str(probe.get("status") or "")
    if probe_status in PROBE_UNAVAILABLE_STATUSES:
        lines.append(
            "Page inspection failed. Approving is temporary for this run and will not persist an empty mapping. [y/N] "
        )
    elif confident_proposed_mappings or judgment_mappings:
        lines.append(
            "Approve the proposed concrete mapping(s)? [y/N] Approving persists only concrete current elements; "
            "rejecting stops and locks down this agent path. "
        )
    elif any_concrete_mapping:
        lines.append(
            "No additional concrete mapping is proposed for the unresolved target(s). Approving continues with the "
            "already-mapped protections shown above and leaves unresolved old targets inactive on this page signature. "
            "[y/N] Rejecting stops and locks down this agent path. "
        )
    else:
        lines.append(
            "No protected targets were confidently mapped on this page. Approving continues without applying this skill "
            "to this page signature. [y/N] "
        )
    return "\n".join(lines)


def _cedar_metadata_from_classification(classification: ActionClassification) -> dict[str, Any]:
    details = classification.details if isinstance(classification.details, dict) else {}
    cedar = details.get("cedar") if isinstance(details.get("cedar"), dict) else {}
    return cedar


def _classification_has_blocked_input_transaction(classification: ActionClassification) -> bool:
    details = classification.details if isinstance(classification.details, dict) else {}
    return isinstance(details.get("blocked_input_transaction"), dict)


def _recent_blocked_form_target_applies(item: RecentBlockedFormTarget, context: CDPMessageContext) -> bool:
    return item.connection_id == "*" or item.connection_id == context.connection_id


def _recent_blocked_form_target_should_intercept(
    item: RecentBlockedFormTarget,
    classification: ActionClassification,
) -> bool:
    target = classification.target
    if target is None:
        return True
    return _blocked_text_spill_candidate(item.target_metadata or {}, target.to_metadata(), blocked_key=item.target_key)


def _blocked_action_label(classification: ActionClassification) -> str:
    if classification.action == "click" and classification.target is not None:
        target = classification.target
        label = _target_label(target)
        tag = target.tag or "element"
        if label:
            return f'click on {tag} "{label}"'
        return f"click on {tag}"
    return classification.action


def _agent_block_guidance(
    classification: ActionClassification,
    cedar_decision: CedarDecision | None = None,
) -> str | None:
    details = classification.details or {}
    if classification.action == "page_preflight" or (
        cedar_decision is not None and cedar_decision.source == "general_skill_preflight"
    ):
        return (
            "Acira denied mutating access to this page and placed it in read-only quarantine unless this deployment "
            "is configured to close denied pages. Do not retry mutating actions on it unless a human explicitly "
            "approves a new attempt. Continue with read-only inspection, a different allowed page/action, or report "
            "that the requested page is unavailable."
        )
    blocked_input = details.get("blocked_input_transaction")
    if isinstance(blocked_input, dict):
        blocked_target = str(blocked_input.get("blocked_target") or "the protected target")
        current_target = str(blocked_input.get("current_target") or "the currently focused control")
        return (
            f"{blocked_target} was blocked by Acira policy. This keyboard/text fallback could have typed into "
            f"{current_target}, so the text was not entered anywhere. Do not retry this target or use "
            "keyboard/JavaScript fallback; continue with allowed fields or report the block."
        )
    misdirected = details.get("misdirected_from_blocked_target")
    if isinstance(misdirected, dict):
        blocked_target = str(misdirected.get("blocked_target") or "the protected target")
        current_target = str(misdirected.get("current_target") or "the currently focused control")
        source_action = str(misdirected.get("source_action") or "").lower()
        if source_action in {"click", "submit", "form_control_activation"} or classification.action in {"click", "mouse", "mouse_move"}:
            return (
                f"{blocked_target} was blocked by Acira policy, and this click follow-up would have targeted "
                f"{current_target}. The click was not sent. Do not retry this target or use JavaScript/event-dispatch "
                "fallback; continue with allowed actions or report the block."
            )
        return (
            f"{blocked_target} was blocked by Acira policy, and this text would have been typed into "
            f"{current_target} instead. The text was not entered anywhere. Do not retry this target or use "
            "keyboard/JavaScript fallback; continue with allowed fields or report the block."
        )
    if classification.action == "form_input" and classification.target is not None:
        target_text = _agent_target_reference(classification)
        return (
            f"{target_text} is protected by Acira policy for this run. The text was not entered anywhere. "
            "Do not retry this same target or attempt a keyboard/JavaScript/event-dispatch bypass; "
            "continue with allowed fields or report the block."
        )
    if cedar_decision and cedar_decision.source in SPECIFIC_SKILL_MAPPING_SOURCES:
        target_text = _agent_target_reference(classification)
        return (
            f"{target_text} is protected by Acira policy for this run. Do not retry this same target "
            "or attempt a JavaScript/event-dispatch bypass; continue with allowed actions or report the block."
        )
    if cedar_decision and cedar_decision.source == "specific_skill_missing_target":
        return (
            "Acira could not confidently match a protected target on this page. Do not brute-force clicks "
            "or JavaScript bypasses; use read-only inspection or report that the page/skill may need re-capture."
        )
    if classification.action == "dangerous_js":
        return (
            "This JavaScript can bypass Acira's UI protections because it clicks/submits, dispatches events, "
            "or mutates form controls. Do not retry this action with JS. Read-only JS inspection is allowed; "
            "use normal click/type/select actions for controls."
        )
    if classification.action == "unknown_js":
        return (
            "Acira could not prove this JavaScript is read-only, so it requires review. Keep JS to compact "
            "read-only inspection, or use normal click/type/select actions."
        )
    if classification.action == "submit":
        return (
            "Submit-like actions are protected. Treat this submission target as unavailable for this run; "
            "do not retry clicks or JavaScript/event-dispatch bypasses. Continue with allowed actions or report the block."
        )
    if classification.action == "click" and classification.target is not None:
        target_text = _agent_target_reference(classification)
        return (
            f"{target_text} is protected by Acira policy for this run. The click was not sent. "
            "Do not retry this same target or attempt a JavaScript/event-dispatch bypass; "
            "continue with allowed actions or report the block."
        )
    if classification.target is not None and classification.meaningful:
        target_text = _agent_target_reference(classification)
        return (
            f"{target_text} is blocked by Acira policy for this run. Do not retry this same target "
            "or attempt a JavaScript/event-dispatch bypass; continue with allowed actions or report the block."
        )
    return None


def _repeated_block_reason(classification: ActionClassification, retry_state: BlockedRetryState) -> str:
    target_text = _agent_target_reference(classification)
    text_suffix = " The text was not entered anywhere." if classification.action == "form_input" else ""
    if retry_state.exceeded:
        detail = (
            f"{target_text} has already been blocked {retry_state.limit} times in this session "
            f"and remains unavailable.{text_suffix} Do not retry this target or attempt a JavaScript/event-dispatch bypass; "
            "continue with other allowed actions or report the block."
        )
    else:
        detail = (
            f"{target_text} was already blocked earlier in this session "
            f"(attempt {retry_state.count}/{retry_state.limit}).{text_suffix} Do not retry this target or attempt a "
            "JavaScript/event-dispatch bypass; continue with other allowed actions or report the block."
        )
    return f"ACIRA_POLICY_BLOCKED_DO_NOT_RETRY: {detail}"


def _blocked_retry_key(
    classification: ActionClassification,
    cedar_decision: CedarDecision | None,
) -> str:
    target = classification.target
    if target is not None:
        target_key = _target_identity_key(target)
        if target.is_submit or (cedar_decision and cedar_decision.source in SPECIFIC_SKILL_MAPPING_SOURCES):
            return target_key
        return _compact_key(classification.action, [target_key])

    if cedar_decision and cedar_decision.skill_decision_id:
        return _compact_key("skill", [cedar_decision.skill_decision_id, classification.action])

    details = classification.details or {}
    if details.get("hash"):
        return _compact_key(classification.action, [str(details["hash"])])

    return _compact_key(classification.action, [classification.method, classification.reason])


def _target_identity_key(target: GuardTarget) -> str:
    return _compact_key(
        "target",
        [
            target.tag or "",
            target.type or "",
            target.role or "",
            target.id or "",
            target.name or "",
            target.aria_label or "",
            target.title or "",
            target.text or "",
            target.form_id or "",
            target.url or "",
        ],
    )


def _target_reference_from_target(target: GuardTarget) -> str:
    label = _target_label(target)
    tag = target.tag or "target"
    if label:
        return f'The {tag} "{label}"'
    return f"The {tag} target"


def _is_printable_text_key_event(params: dict[str, Any]) -> bool:
    event_type = str(params.get("type") or "")
    if event_type not in {"rawKeyDown", "keyDown", "char", "keyUp"}:
        return False
    text = params.get("text") or params.get("unmodifiedText")
    if isinstance(text, str) and text:
        return True
    if bool(params.get("ctrlKey")) or bool(params.get("metaKey")) or bool(params.get("altKey")):
        return False
    if event_type == "char":
        return True
    key = str(params.get("key") or "")
    code = str(params.get("code") or "")
    if len(key) == 1 and key not in {"\r", "\n", "\t"}:
        return True
    if re.fullmatch(r"Key[A-Z]", code, re.IGNORECASE) or re.fullmatch(r"Digit[0-9]", code, re.IGNORECASE):
        return True
    return code in {
        "Space",
        "Minus",
        "Equal",
        "BracketLeft",
        "BracketRight",
        "Backslash",
        "Semicolon",
        "Quote",
        "Comma",
        "Period",
        "Slash",
        "Backquote",
    }


def _transaction_cdp_kind(classification: ActionClassification) -> str | None:
    details = classification.details if isinstance(classification.details, dict) else {}
    category = str(details.get("category") or "")
    if classification.method in {"Input.insertText", "Input.dispatchKeyEvent", "Input.imeSetComposition"}:
        if classification.action in {"form_input", "keyboard"}:
            return "text"
    if classification.method == "DOM.focus":
        return "focus"
    if classification.method in {"Input.dispatchMouseEvent", "Input.dispatchTouchEvent"}:
        if classification.action in {"focus", "mouse_move", "touch_move", "mouse", "touch"}:
            return "focus"
        if classification.action in {"click", "submit", "check", "select", "form_control_activation"}:
            return "activation"
    if category.endswith((".event_helper_js", ".clear_helper_js", ".value_helper_js")):
        return "text_helper_js"
    if classification.action == "select" and category.endswith(".safe_select"):
        return "activation"
    if classification.method.startswith("DOM.") and classification.action in {"form_input", "dom_mutation"}:
        return "dom_mutation"
    return None


def _transaction_kind_applies(transaction: ActionTransaction, action_kind: str) -> bool:
    if transaction.action == "form_input":
        if transaction.decision == "block":
            return action_kind in {"text", "text_helper_js", "dom_mutation"}
        return action_kind in {"text", "focus", "text_helper_js", "dom_mutation"}
    if transaction.action in {"click", "submit", "check", "select", "form_control_activation"}:
        return action_kind in {"focus", "activation"}
    if transaction.action == "file_upload":
        return action_kind in {"focus", "dom_mutation"}
    return False


def _can_bootstrap_adapter_text_transaction(
    transaction: ActionTransaction,
    classification: ActionClassification,
    action_kind: str,
) -> bool:
    """Allow the first targetless CDP command of an approved adapter text action.

    Some adapters resolve and focus the element before typing. If Acira's
    low-level probe cannot resolve that DOM.focus/helper/key command back to a
    target, the high-level reviewed text action is still the safer target
    authority for this short transaction. Concrete mismatched targets are still
    rejected before reaching this path.
    """

    if transaction.action != "form_input":
        return False
    target = transaction.target_metadata if isinstance(transaction.target_metadata, dict) else {}
    if not bool(target.get("is_editable") or target.get("is_form_control")):
        return False
    if action_kind == "focus":
        return classification.method == "DOM.focus"
    if action_kind == "text":
        return classification.method in {"Input.insertText", "Input.dispatchKeyEvent", "Input.imeSetComposition"}
    if action_kind == "text_helper_js":
        return classification.method == "Runtime.callFunctionOn"
    return False


def _decision_allows_secret_payload(decision: CDPPolicyDecision) -> bool:
    metadata = decision.metadata if isinstance(decision.metadata, dict) else {}
    transaction = metadata.get("action_transaction") if isinstance(metadata.get("action_transaction"), dict) else {}
    if transaction.get("allows_secret_payload") is True:
        return True
    transaction_metadata = transaction.get("metadata") if isinstance(transaction.get("metadata"), dict) else {}
    classification = transaction_metadata.get("classification") if isinstance(transaction_metadata.get("classification"), dict) else {}
    details = classification.get("details") if isinstance(classification.get("details"), dict) else {}
    if isinstance(details.get("secret_use"), dict):
        return True
    secret_metadata = transaction_metadata.get("cedar") if isinstance(transaction_metadata.get("cedar"), dict) else {}
    return isinstance(secret_metadata.get("secret"), dict)


def _secret_field_read_key(context: CDPMessageContext) -> tuple[str, str | None, int | str] | None:
    message_id = context.message_id
    if not isinstance(message_id, (int, str)):
        return None
    return (context.connection_id, context.session_id, message_id)


def _metadata_contains_secret_use(value: Any) -> bool:
    if isinstance(value, dict):
        if isinstance(value.get("secret_use"), dict):
            return True
        if isinstance(value.get("secret"), dict) and value.get("engine") == "acira_secret_policy":
            return True
        return any(_metadata_contains_secret_use(item) for item in value.values())
    if isinstance(value, list):
        return any(_metadata_contains_secret_use(item) for item in value)
    if isinstance(value, tuple):
        return any(_metadata_contains_secret_use(item) for item in value)
    return False


def _skill_action_for_transaction_followup(
    transaction: ActionTransaction,
    classification: ActionClassification,
    action_kind: str,
) -> str | None:
    if transaction.action == "form_input":
        if action_kind == "focus":
            return "focus"
        if action_kind in {"text", "text_helper_js", "dom_mutation"}:
            return "form_input"
        return None
    if transaction.action in {"click", "submit", "check", "select", "form_control_activation"}:
        return classification.action
    if transaction.action == "file_upload" and action_kind in {"focus", "dom_mutation"}:
        return "file_upload"
    return None


def _blocked_transaction_should_intercept(
    transaction: ActionTransaction,
    classification: ActionClassification,
) -> bool:
    target = classification.target
    if target is None:
        return True
    return _blocked_text_spill_candidate(
        transaction.target_metadata,
        target.to_metadata(),
        blocked_key=transaction.target_key,
    )


def _transaction_action_from_classification(classification: ActionClassification) -> str | None:
    if classification.action == "form_input":
        return "form_input"
    if classification.action == "focus" and classification.target is not None and (
        classification.target.is_editable or classification.target.is_form_control
    ):
        return "form_input"
    return None


def _transaction_target_matches(transaction: ActionTransaction, target: GuardTarget | None) -> bool:
    if target is None:
        return False
    if _target_identity_key(target) == transaction.target_key:
        return True
    expected = transaction.target_metadata
    observed = target.to_metadata()

    for key in ("id", "name", "dom_path"):
        left = _string_or_none(expected.get(key))
        right = _string_or_none(observed.get(key))
        if left and right and left == right:
            return True

    expected_attrs = expected.get("attributes") if isinstance(expected.get("attributes"), dict) else {}
    observed_attrs = observed.get("attributes") if isinstance(observed.get("attributes"), dict) else {}
    for key in ("id", "name"):
        left = _string_or_none(expected_attrs.get(key))
        right = _string_or_none(observed_attrs.get(key))
        if left and right and left == right:
            return True

    same_form = bool(
        _string_or_none(expected.get("form_id"))
        and _string_or_none(expected.get("form_id")) == _string_or_none(observed.get("form_id"))
    )
    same_section = bool(
        _string_or_none(expected.get("section_heading"))
        and _loose_text_match(_string_or_none(expected.get("section_heading")), _string_or_none(observed.get("section_heading")))
    )
    same_control_slot = (
        _int_or_none(expected.get("control_index")) is not None
        and _int_or_none(expected.get("control_index")) == _int_or_none(observed.get("control_index"))
    )
    same_kind_slot = (
        _int_or_none(expected.get("same_kind_index")) is not None
        and _int_or_none(expected.get("same_kind_index")) == _int_or_none(observed.get("same_kind_index"))
    )
    same_tag = (expected.get("tag") or "") == (observed.get("tag") or "")
    same_type = (expected.get("type") or "") == (observed.get("type") or "")
    return bool((same_form or same_section) and same_control_slot and same_kind_slot and (same_tag or same_type))


def _transaction_observed_target_is_frame_boundary(transaction: ActionTransaction, target: GuardTarget | None) -> bool:
    if target is None:
        return False
    observed = target.to_metadata()
    if str(observed.get("tag") or "").lower() not in {"iframe", "frame"}:
        return False

    expected = transaction.target_metadata or {}
    expected_tag = str(expected.get("tag") or "").lower()
    if expected_tag not in {"input", "textarea"} and not bool(expected.get("is_editable")):
        return False
    if not bool(expected.get("is_form_control") or expected.get("is_editable")):
        return False

    expected_rects = _target_metadata_rect_candidates(expected)
    observed_rects = _target_metadata_rect_candidates(observed)
    if not expected_rects or not observed_rects:
        return False

    return any(
        _rect_contains(observed_rect, expected_rect, padding=8.0)
        or _rect_overlap_ratio(observed_rect, expected_rect) >= 0.45
        for observed_rect in observed_rects
        for expected_rect in expected_rects
    )


def _browser_use_transaction_observed_plain_document_target(
    transaction: ActionTransaction,
    classification: ActionClassification,
    action_kind: str,
) -> bool:
    """Treat plain body/html probes as targetless during approved browser-use typing.

    CDP keyboard packets are delivered to the currently focused document, not to
    an element id. During browser-use's native per-character typing loop,
    Acira's active-element probe can transiently observe body/html even though
    the approved input transaction has already focused the right field. In that
    narrow case, inherit the approved input target. Editable bodies and concrete
    neighboring fields are still considered real targets and remain blocked.
    """

    if transaction.framework != "browser_use":
        return False
    if transaction.decision != "allow" or transaction.action != "form_input":
        return False
    if action_kind != "text" or classification.method != "Input.dispatchKeyEvent":
        return False
    observed_target = classification.target
    if observed_target is None:
        return False

    observed = observed_target.to_metadata()
    observed_tag = str(observed.get("tag") or "").lower()
    if observed_tag not in {"body", "html"}:
        return False
    if bool(observed.get("is_editable") or observed.get("is_form_control")):
        return False

    expected = transaction.target_metadata if isinstance(transaction.target_metadata, dict) else {}
    expected_tag = str(expected.get("tag") or "").lower()
    if expected_tag not in {"input", "textarea"} and not bool(expected.get("is_editable")):
        return False
    if not bool(expected.get("is_form_control") or expected.get("is_editable")):
        return False

    expected_url = _string_or_none(expected.get("url"))
    observed_url = _string_or_none(observed.get("url"))
    if expected_url and observed_url and _normalize_url_for_transaction(expected_url) != _normalize_url_for_transaction(observed_url):
        return False

    return True


def _normalize_url_for_transaction(value: str) -> str:
    return re.sub(r"#.*$", "", re.sub(r"\?.*$", "", value)).rstrip("/")


def _target_metadata_rect_candidates(target: dict[str, Any]) -> tuple[dict[str, float], ...]:
    structure = target.get("structure_signature")
    if not isinstance(structure, dict):
        return ()
    layout = structure.get("layout")
    if not isinstance(layout, dict):
        return ()

    rects: list[dict[str, float]] = []
    for key in ("rect", "viewport_rect", "document_rect"):
        rect = _rect_from_value(layout.get(key))
        if rect is not None and rect not in rects:
            rects.append(rect)
    return tuple(rects)


def _rect_from_value(value: Any) -> dict[str, float] | None:
    if not isinstance(value, dict):
        return None
    rect: dict[str, float] = {}
    for key in ("x", "y", "width", "height"):
        try:
            number = float(value.get(key))
        except (TypeError, ValueError):
            return None
        rect[key] = number
    if rect["width"] <= 0 or rect["height"] <= 0:
        return None
    return rect


def _rect_contains(outer: dict[str, float], inner: dict[str, float], *, padding: float = 0.0) -> bool:
    return bool(
        inner["x"] >= outer["x"] - padding
        and inner["y"] >= outer["y"] - padding
        and inner["x"] + inner["width"] <= outer["x"] + outer["width"] + padding
        and inner["y"] + inner["height"] <= outer["y"] + outer["height"] + padding
    )


def _rect_overlap_ratio(left: dict[str, float], right: dict[str, float]) -> float:
    left_x2 = left["x"] + left["width"]
    left_y2 = left["y"] + left["height"]
    right_x2 = right["x"] + right["width"]
    right_y2 = right["y"] + right["height"]
    overlap_width = max(0.0, min(left_x2, right_x2) - max(left["x"], right["x"]))
    overlap_height = max(0.0, min(left_y2, right_y2) - max(left["y"], right["y"]))
    overlap = overlap_width * overlap_height
    smaller = min(left["width"] * left["height"], right["width"] * right["height"])
    if smaller <= 0:
        return 0.0
    return overlap / smaller


def _blocked_text_spill_candidate(
    blocked_target: dict[str, Any],
    current_target: dict[str, Any],
    *,
    blocked_key: str,
) -> bool:
    if not current_target:
        return True
    tag = str(current_target.get("tag") or "").lower()
    if tag in {"body", "html"}:
        return True
    current = _target_from_probe_value(current_target)
    if current is not None and _target_identity_key(current) == blocked_key:
        return True
    if not blocked_target:
        return True

    blocked_form = _target_structural_text(blocked_target, "form_id") or _target_structural_text(blocked_target, "form_name")
    current_form = _target_structural_text(current_target, "form_id") or _target_structural_text(current_target, "form_name")
    same_form = bool(blocked_form and current_form and blocked_form == current_form)
    same_section = _loose_text_match(
        _target_structural_text(blocked_target, "section_heading"),
        _target_structural_text(current_target, "section_heading"),
    )

    blocked_index = _target_structural_int(blocked_target, "control_index")
    current_index = _target_structural_int(current_target, "control_index")
    if blocked_index is not None and current_index is not None:
        if abs(blocked_index - current_index) <= 1 and (same_form or same_section):
            return True
        if abs(blocked_index - current_index) == 0:
            return True

    blocked_kind_index = _target_structural_int(blocked_target, "same_kind_index")
    current_kind_index = _target_structural_int(current_target, "same_kind_index")
    if blocked_kind_index is not None and current_kind_index is not None:
        if abs(blocked_kind_index - current_kind_index) <= 1 and (same_form or same_section):
            return True

    confidence = float(current_target.get("confidence") or 0.0)
    if confidence < 0.65 and (same_form or same_section):
        return True
    return False


def _action_transaction_block_reason(
    transaction: ActionTransaction,
    classification: ActionClassification,
    *,
    same_target: bool,
) -> str:
    current_target = _agent_target_reference(classification)
    if transaction.decision == "block":
        if transaction.action == "form_input":
            return (
                f"{transaction.target_text} was denied or blocked by the active {transaction.framework} text action. "
                f"This keyboard/text fallback could type into {current_target}, so Acira blocked it before Chrome received it. "
                "The text was not entered anywhere."
            )
        return (
            f"{transaction.target_text} was denied or blocked by the active {transaction.framework} action. "
            "This follow-up browser command was not forwarded."
        )
    if not same_target:
        approval_text = "review approved" if transaction.review_cache_key else "allowed"
        return (
            f"The active {transaction.framework} {transaction.action} {approval_text} {transaction.target_text}, "
            f"but this follow-up command targets {current_target}. Acira blocked it to prevent misdirected execution."
        )
    return "The active framework action transaction did not allow this browser command."


def _review_cache_key(
    classification: ActionClassification,
    cedar_decision: CedarDecision | None,
) -> str:
    if cedar_decision and cedar_decision.source == "general_skill_preflight":
        metadata = cedar_decision.metadata if isinstance(cedar_decision.metadata, dict) else {}
        return _compact_key(
            "page-preflight-review",
            _preflight_review_identity_parts(metadata, cedar_decision),
        )
    if cedar_decision and cedar_decision.source == "general_skill":
        return _general_skill_action_review_cache_key(classification, cedar_decision)
    if cedar_decision and cedar_decision.source == "specific_skill" and cedar_decision.skill_decision_id:
        return _compact_key("skill-review", [cedar_decision.skill_decision_id])
    if cedar_decision and cedar_decision.source == "specific_skill_mapped" and cedar_decision.skill_decision_id:
        return _compact_key("skill-review", [cedar_decision.skill_decision_id])
    if cedar_decision and cedar_decision.source == "specific_skill_mapping_review" and cedar_decision.skill_decision_id:
        metadata = cedar_decision.metadata if isinstance(cedar_decision.metadata, dict) else {}
        skills = metadata.get("skills") if isinstance(metadata.get("skills"), dict) else {}
        mappings = skills.get("page_mapping") if isinstance(skills.get("page_mapping"), list) else []
        probe = skills.get("page_target_probe") if isinstance(skills.get("page_target_probe"), dict) else {}
        session_key = str(skills.get("page_mapping_session_key") or "") or _page_mapping_session_key(
            [item for item in mappings if isinstance(item, dict)],
            [],
            probe,
        )
        return f"mapping-review:{session_key}"
    return _blocked_retry_key(classification, cedar_decision)


def _review_result_is_reusable(
    classification: ActionClassification,
    cedar_decision: CedarDecision | None,
) -> bool:
    # A mapping/page acknowledgement must not carry a later action-level risk
    # across review or skip that risk's required operation evidence.
    detections = (((cedar_decision.metadata or {}).get("general_skills") or {}).get("detections") or []) if cedar_decision else []
    if any(item.get("context") == "action" for item in detections):
        return False
    # A policy/target identity is not an authorization for future operations.
    # Page mapping acknowledgements are distinct from action approvals.
    return bool(cedar_decision and cedar_decision.source in {
        "general_skill_preflight", "specific_skill_mapping_review"
    })


def _general_skill_action_review_cache_key(
    classification: ActionClassification,
    cedar_decision: CedarDecision,
) -> str:
    identity = _general_skill_action_review_identity(classification, cedar_decision)
    return _hashed_review_key("general-action-review", identity)


def _general_skill_action_review_cacheable(
    classification: ActionClassification,
    cedar_decision: CedarDecision,
) -> bool:
    if cedar_decision.source != "general_skill" or classification.target is None:
        return False
    target = classification.target
    confidence = target.confidence if target.confidence else classification.confidence
    if confidence < 0.72:
        return False
    details = classification.details if isinstance(classification.details, dict) else {}
    resolution = details.get("target_resolution_status") if isinstance(details.get("target_resolution_status"), dict) else {}
    if resolution.get("safe_to_authorize") is False or resolution.get("resolved") is False:
        return False
    label = _target_label(target)
    if not label or _generic_review_target_label(label):
        return False
    return bool(_general_skill_action_target_identity(target))


def _general_skill_action_review_identity(
    classification: ActionClassification,
    cedar_decision: CedarDecision,
) -> dict[str, Any]:
    target = classification.target
    metadata = cedar_decision.metadata if isinstance(cedar_decision.metadata, dict) else {}
    decision = metadata.get("general_skill_decision") if isinstance(metadata.get("general_skill_decision"), dict) else {}
    policy = decision.get("policy") if isinstance(decision.get("policy"), dict) else {}
    detection = decision.get("detection") if isinstance(decision.get("detection"), dict) else {}
    details = classification.details if isinstance(classification.details, dict) else {}
    return {
        "scope": "general_skill_action_review",
        "action": classification.action,
        "url": _general_skill_action_review_url(classification),
        "skill_decision_id": cedar_decision.skill_decision_id or "",
        "risk": detection.get("risk") or policy.get("risk") or "",
        "decision": decision.get("decision") or policy.get("decision") or cedar_decision.decision,
        "source": details.get("source") or "",
        "target": _general_skill_action_target_identity(target) if target is not None else {},
    }


def _general_skill_action_review_url(classification: ActionClassification) -> str:
    target = classification.target
    if target is not None:
        target_url = _normalize_mapping_url(target.url)
        if target_url:
            return target_url
    details = classification.details if isinstance(classification.details, dict) else {}
    for key in ("url", "current_url"):
        value = _normalize_mapping_url(_string_or_none(details.get(key)))
        if value:
            return value
    probe = details.get("page_target_probe") if isinstance(details.get("page_target_probe"), dict) else {}
    return _normalize_mapping_url(_string_or_none(probe.get("url")))


def _general_skill_action_target_identity(target: GuardTarget) -> dict[str, Any]:
    return {
        "tag": target.tag or "",
        "type": target.type or "",
        "role": target.role or "",
        "id": target.id or "",
        "name": target.name or "",
        "aria_label": target.aria_label or "",
        "title": target.title or "",
        "text": target.text or "",
        "form_id": target.form_id or "",
        "form_name": target.form_name or "",
        "form_action": target.form_action or "",
        "section_heading": target.section_heading or "",
        "nearby_text": target.nearby_text or "",
        "parent_label": target.parent_label or "",
        "previous_control_text": target.previous_control_text or "",
        "next_control_text": target.next_control_text or "",
        "dom_path": target.dom_path or "",
        "control_index": target.control_index,
        "same_kind_index": target.same_kind_index,
        "structure_signature": target.structure_signature or {},
        "attributes": target.attributes or {},
    }


def _generic_review_target_label(value: str) -> bool:
    normalized = re.sub(r"\s+", " ", str(value or "").strip().lower())
    return normalized in {
        "button",
        "checkbox",
        "control",
        "element",
        "field",
        "input",
        "link",
        "radio",
        "select",
        "submit",
        "target",
        "text",
        "textarea",
    }


def _hashed_review_key(prefix: str, identity: dict[str, Any]) -> str:
    normalized = _normalized_review_key_value(identity)
    raw = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"{prefix}:{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]}"


def _normalized_review_key_value(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key in sorted(value):
            normalized = _normalized_review_key_value(value[key])
            if normalized in ("", None, [], {}):
                continue
            result[str(key)] = normalized
        return result
    if isinstance(value, (list, tuple)):
        return [_normalized_review_key_value(item) for item in value if _normalized_review_key_value(item) not in ("", None, [], {})]
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value).strip().lower()
    if isinstance(value, (int, float, bool)):
        return value
    return None


def _preflight_review_identity_parts(metadata: dict[str, Any], cedar_decision: CedarDecision) -> list[str]:
    preflight = metadata.get("preflight") if isinstance(metadata.get("preflight"), dict) else {}
    general = metadata.get("general_skills") if isinstance(metadata.get("general_skills"), dict) else {}
    decision = metadata.get("general_skill_decision") if isinstance(metadata.get("general_skill_decision"), dict) else {}
    policy = decision.get("policy") if isinstance(decision.get("policy"), dict) else {}
    detections = general.get("detections") if isinstance(general.get("detections"), list) else []
    risks = sorted(
        str(item.get("risk") or "")
        for item in detections
        if isinstance(item, dict) and item.get("risk")
    )
    return [
        _normalize_mapping_url(_string_or_none(preflight.get("url"))),
        str(preflight.get("page_signature") or ""),
        str(preflight.get("decision") or policy.get("decision") or ""),
        str(cedar_decision.skill_decision_id or ""),
        ",".join(risks),
    ]


def _preflight_scope_key(context: CDPMessageContext) -> str:
    return context.session_id or context.websocket_path or context.connection_id or "root"


def _target_id_from_websocket_path(websocket_path: str | None) -> str | None:
    text = str(websocket_path or "")
    match = re.search(r"/devtools/page/([^/?#]+)", text)
    return match.group(1) if match else None


def _compact_key(prefix: str, parts: list[str]) -> str:
    raw = "|".join(part for part in parts if part)
    normalized = re.sub(r"\s+", " ", raw).strip().lower()
    return f"{prefix}:{normalized[:240]}"


def _agent_target_reference(classification: ActionClassification) -> str:
    target = classification.target
    if target is None:
        return "This action"
    label = _target_label(target)
    tag = target.tag or "target"
    if label:
        return f'The {tag} "{label}"'
    return f"The {tag} target"


def _target_from_probe_value(value: Any) -> GuardTarget | None:
    if not isinstance(value, dict):
        return None
    attributes = _string_dict(value.get("attributes"))
    return GuardTarget(
        tag=_string_or_none(value.get("tag")),
        type=_string_or_none(value.get("type")),
        role=_string_or_none(value.get("role")),
        text=_string_or_none(value.get("text")),
        id=_string_or_none(value.get("id")),
        classes=_string_tuple(value.get("classes")),
        aria_label=_string_or_none(value.get("ariaLabel") or value.get("aria_label")),
        title=_string_or_none(value.get("title")),
        name=_string_or_none(value.get("name")),
        href=_string_or_none(value.get("href")),
        url=_string_or_none(value.get("url")),
        nearby_text=_string_or_none(value.get("nearbyText") or value.get("nearby_text")),
        ancestor_text=_string_or_none(value.get("ancestorText") or value.get("ancestor_text")),
        form_id=_string_or_none(value.get("formId") or value.get("form_id")),
        form_name=_string_or_none(value.get("formName") or value.get("form_name")),
        form_action=_string_or_none(value.get("formAction") or value.get("form_action")),
        section_heading=_string_or_none(value.get("sectionHeading") or value.get("section_heading")),
        parent_label=_string_or_none(value.get("parentLabel") or value.get("parent_label")),
        control_index=_int_or_none(_probe_value(value, "controlIndex", "control_index")),
        same_kind_index=_int_or_none(_probe_value(value, "sameKindIndex", "same_kind_index")),
        previous_control_text=_string_or_none(value.get("previousControlText") or value.get("previous_control_text")),
        next_control_text=_string_or_none(value.get("nextControlText") or value.get("next_control_text")),
        dom_path=_string_or_none(value.get("domPath") or value.get("dom_path")),
        structure_signature=_dict_or_none(value.get("structureSignature") or value.get("structure_signature")),
        attributes=attributes,
        is_button=bool(_probe_value(value, "isButton", "is_button")),
        is_checkbox=bool(_probe_value(value, "isCheckbox", "is_checkbox")),
        is_editable=bool(_probe_value(value, "isEditable", "is_editable")),
        is_form_control=bool(_probe_value(value, "isFormControl", "is_form_control")),
        is_select=bool(_probe_value(value, "isSelect", "is_select")),
        is_submit=bool(_probe_value(value, "isSubmit", "is_submit")),
        is_native_submit=_optional_bool(_probe_value(value, "isNativeSubmit", "is_native_submit")),
        confidence=float(value.get("confidence") or 0.0),
        source=_string_or_none(value.get("source")) or "js.probe",
    )


def _merge_probe_extras(metadata: dict[str, Any], value: dict[str, Any]) -> None:
    extras = {
        "labels": ("labels",),
        "options": ("options",),
        "semantic_hints": ("semanticHints", "semantic_hints"),
        "identity_tokens": ("identityTokens", "identity_tokens"),
        "value_sample": ("valueSample", "value_sample"),
        "normalizer_version": ("normalizerVersion", "normalizer_version"),
        "action_signature": ("actionSignature", "action_signature"),
    }
    for target_key, source_keys in extras.items():
        for source_key in source_keys:
            if source_key in value:
                metadata[target_key] = value.get(source_key)
                break


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _probe_value(value: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in value:
            return value.get(key)
    return None


def _target_from_dom_node(node: Any) -> GuardTarget | None:
    if not isinstance(node, dict):
        return None

    tag = _string_or_none(node.get("localName") or node.get("nodeName"))
    tag = tag.lower() if tag else None
    attributes = _attributes_to_dict(node.get("attributes"))
    input_type = (attributes.get("type") or "").lower()
    role = (attributes.get("role") or "").lower() or None
    text = attributes.get("aria-label") or attributes.get("title") or attributes.get("value")
    classes = tuple(token for token in (attributes.get("class") or "").split() if token)
    is_editable = tag in {"textarea"} or (tag == "input" and input_type not in {"button", "checkbox", "hidden", "image", "radio", "reset", "submit"})
    is_button = tag == "button" or role == "button" or input_type in {"button", "submit", "reset"}
    is_select = tag == "select" or role in {"combobox", "listbox", "option"}
    return GuardTarget(
        tag=tag,
        type=input_type or None,
        role=role,
        text=text,
        id=attributes.get("id") or None,
        classes=classes,
        aria_label=attributes.get("aria-label") or None,
        title=attributes.get("title") or None,
        name=attributes.get("name") or None,
        href=attributes.get("href") or None,
        form_id=attributes.get("form") or None,
        attributes=attributes,
        is_button=is_button,
        is_checkbox=input_type in {"checkbox", "radio"} or role == "checkbox",
        is_editable=is_editable,
        is_form_control=tag in {"button", "input", "select", "textarea"} or is_editable or is_button or is_select,
        is_select=is_select,
        is_submit=input_type == "submit" or (tag == "button" and input_type in {"", "submit"}),
        confidence=0.8,
        source="cdp.DOM.describeNode",
    )


def _attributes_to_dict(attributes: Any) -> dict[str, str]:
    if not isinstance(attributes, list):
        return {}
    pairs = zip(attributes[0::2], attributes[1::2], strict=False)
    return {str(key).lower(): str(value) for key, value in pairs}


def _response_value(response: dict[str, Any] | None) -> Any:
    if not isinstance(response, dict):
        return None
    result = response.get("result")
    if not isinstance(result, dict):
        return None
    remote = result.get("result")
    if not isinstance(remote, dict):
        return None
    return remote.get("value")


def _should_evaluate_specific_skill_targets(classification: ActionClassification) -> bool:
    if classification.action in SPECIFIC_SKILL_DEFERRED_ACTIONS:
        return False
    if classification.method in SAFE_SETUP_METHODS or classification.method in {"Page.navigate", "Target.createTarget"}:
        return False
    return True


def _event_facts_from_classification(classification: ActionClassification) -> dict[str, Any]:
    target = classification.target.to_metadata() if classification.target else None
    url = target.get("url") if target else None
    return {
        "method": classification.method,
        "action": classification.action,
        "reason": classification.reason,
        "confidence": classification.confidence,
        "read_only": classification.read_only,
        "meaningful": classification.meaningful,
        "url": url,
        "target": target,
        "details": classification.details or {},
    }


def _first_missing_resolution(cedar_decision: CedarDecision) -> dict[str, Any] | None:
    metadata = cedar_decision.metadata if isinstance(cedar_decision.metadata, dict) else {}
    skills = metadata.get("skills") if isinstance(metadata.get("skills"), dict) else {}
    missing = skills.get("missing_targets") if isinstance(skills.get("missing_targets"), list) else []
    if not missing:
        return None
    first = missing[0]
    if not isinstance(first, dict):
        return None
    resolution = first.get("resolution")
    return resolution if isinstance(resolution, dict) else None


def _specific_skill_match_refs(cedar_decision: CedarDecision) -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    metadata = cedar_decision.metadata if isinstance(cedar_decision.metadata, dict) else {}
    skills = metadata.get("skills") if isinstance(metadata.get("skills"), dict) else {}
    matches = skills.get("matches") if isinstance(skills.get("matches"), list) else []
    for item in matches:
        if not isinstance(item, dict):
            continue
        skill_id = _string_or_none(item.get("skill_id"))
        target_id = _string_or_none(item.get("target_id"))
        if skill_id and target_id:
            refs.append((skill_id, target_id))
    if refs:
        return refs

    if cedar_decision.skill_decision_id and ":" in cedar_decision.skill_decision_id:
        skill_id, target_id = cedar_decision.skill_decision_id.split(":", 1)
        if skill_id and target_id:
            refs.append((skill_id, target_id))
    return refs


def _target_mapping_key(target: dict[str, Any]) -> str:
    attrs = target.get("attributes") if isinstance(target.get("attributes"), dict) else {}
    parts = [
        str(target.get("tag") or ""),
        str(target.get("type") or ""),
        str(target.get("role") or ""),
        str(target.get("id") or attrs.get("id") or ""),
        str(target.get("name") or attrs.get("name") or ""),
        str(target.get("form_id") or target.get("formId") or ""),
        str(target.get("dom_path") or target.get("domPath") or ""),
        str(target.get("control_index") if target.get("control_index") is not None else target.get("controlIndex") or ""),
        str(target.get("same_kind_index") if target.get("same_kind_index") is not None else target.get("sameKindIndex") or ""),
    ]
    return _compact_key("map-target", parts)


def _target_summary_from_metadata(target: dict[str, Any]) -> dict[str, Any]:
    return {
        "tag": target.get("tag"),
        "type": target.get("type"),
        "role": target.get("role"),
        "label": _target_label(_target_from_probe_value(target) or GuardTarget()) if isinstance(target, dict) else "",
        "id": target.get("id"),
        "name": target.get("name"),
        "section": target.get("section_heading") or target.get("sectionHeading"),
        "control_index": target.get("control_index") if target.get("control_index") is not None else target.get("controlIndex"),
        "same_kind_index": target.get("same_kind_index") if target.get("same_kind_index") is not None else target.get("sameKindIndex"),
    }


def _url_from_page_targets(page_targets: list[dict[str, Any]] | None) -> str | None:
    if not page_targets:
        return None
    for target in page_targets:
        if not isinstance(target, dict):
            continue
        url = _string_or_none(target.get("url"))
        if url:
            return url
    return None


def _is_concrete_mapping_candidate(entry: dict[str, Any]) -> bool:
    if not entry.get("candidate_key"):
        return False
    return str(entry.get("status") or "") in {"ambiguous", "approved", "auto_mapped", "rejected"}


def _mapping_entry_requires_page_review(entry: dict[str, Any]) -> bool:
    status = str(entry.get("status") or "").strip().lower()
    if status in {"rejected", "ambiguous"}:
        return True
    if status not in {"approved", "auto_mapped"}:
        return True
    resolution = entry.get("best_resolution") if isinstance(entry.get("best_resolution"), dict) else {}
    gate = entry.get("evidence_gate") if isinstance(entry.get("evidence_gate"), dict) else {}
    eligibility = entry.get("candidate_eligibility") if isinstance(entry.get("candidate_eligibility"), dict) else {}
    disqualifiers = list(gate.get("hard_disqualifiers") or eligibility.get("hard_disqualifiers") or [])
    return not (
        _is_concrete_mapping_candidate(entry)
        and (status == "approved" or str(resolution.get("kind") or "") == "strong_match")
        and bool(gate.get("concrete", True))
        and not disqualifiers
    )


def _has_concrete_mapping_candidate(entries: list[dict[str, Any]]) -> bool:
    return any(_is_concrete_mapping_candidate(entry) for entry in entries if isinstance(entry, dict))


def _assignment_candidate_viable(candidate: tuple[Any, ...]) -> bool:
    record = candidate[3] if len(candidate) > 3 else None
    resolution = candidate[1] if len(candidate) > 1 else None
    gate = candidate[5] if len(candidate) > 5 and isinstance(candidate[5], dict) else {}
    eligibility = candidate[4] if len(candidate) > 4 and isinstance(candidate[4], dict) else {}
    if not gate.get("concrete") or gate.get("hard_disqualifiers") or eligibility.get("hard_disqualifiers"):
        return False
    if record is not None and getattr(record, "status", None) == "approved":
        return approved_mapping_matches(record, candidate[0], eligibility, gate)
    if getattr(resolution, "kind", "") == "unrelated":
        components = gate.get("score_components") if isinstance(gate.get("score_components"), dict) else {}
        eligibility = candidate[4] if len(candidate) > 4 and isinstance(candidate[4], dict) else {}
        if eligibility.get("hard_disqualifiers") or eligibility.get("soft_penalties"):
            return False
        anchor = gate.get("anchor_overlap") if isinstance(gate.get("anchor_overlap"), dict) else {}
        expected_kind = str(gate.get("expected_kind") or "")
        observed_kind = str(gate.get("observed_kind") or "")
        return (
            int(components.get("identity") or 0) >= 18
            or (
                expected_kind
                and expected_kind == observed_kind
                and (
                    int(anchor.get("raw") or 0) >= 1
                    or int(anchor.get("semantic") or 0) >= 2
                    or int(anchor.get("high_value") or 0) >= 1
                )
            )
        )
    return True


def _assignment_candidate_score(candidate: tuple[Any, ...]) -> int:
    resolution = candidate[1]
    score = int(getattr(resolution, "score", 0))
    gate = candidate[5] if len(candidate) > 5 and isinstance(candidate[5], dict) else {}
    eligibility = candidate[4] if len(candidate) > 4 and isinstance(candidate[4], dict) else {}
    score -= 20 * len(eligibility.get("hard_disqualifiers") or [])
    score -= 6 * len(eligibility.get("soft_penalties") or [])
    if not gate.get("concrete"):
        score -= 30
    components = gate.get("score_components") if isinstance(gate.get("score_components"), dict) else {}
    score += min(10, int(components.get("structure") or 0) // 12)
    score += min(8, int(components.get("identity") or 0) // 16)
    if gate.get("concrete"):
        score += 8
    if getattr(resolution, "kind", "") in {"strong_match", "structural_slot_match"}:
        score += 6
    elif getattr(resolution, "kind", "") == "ambiguous_match":
        score += 3
    elif getattr(resolution, "kind", "") == "unrelated":
        score -= 4
    return max(0, score)


def _candidate_margin(candidates: list[tuple[Any, ...]]) -> int:
    if not candidates:
        return 0
    if len(candidates) == 1:
        return int(candidates[0][1].score)
    return int(candidates[0][1].score) - int(candidates[1][1].score)


def _page_target_signature(page_targets: list[dict[str, Any]] | None, probe_info: dict[str, Any] | None = None) -> str:
    probe = probe_info or {}
    target_parts: list[dict[str, Any]] = []
    for item in (page_targets or [])[:80]:
        if not isinstance(item, dict):
            continue
        attrs = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
        signature = item.get("structure_signature") or item.get("structureSignature")
        signature = signature if isinstance(signature, dict) else {}
        target_parts.append(
            {
                "tag": item.get("tag"),
                "type": item.get("type"),
                "role": item.get("role"),
                "id": item.get("id") or attrs.get("id"),
                "name": item.get("name") or attrs.get("name"),
                "label": item.get("parent_label") or item.get("nearby_text") or item.get("text"),
                "form": item.get("form_id") or item.get("formId"),
                "index": item.get("control_index") if item.get("control_index") is not None else item.get("controlIndex"),
                "same_kind": item.get("same_kind_index") if item.get("same_kind_index") is not None else item.get("sameKindIndex"),
                "kind": signature.get("target_kind"),
            }
        )
    payload = {
        "status": probe.get("status"),
        "url": probe.get("url") or _url_from_page_targets(page_targets),
        "title": probe.get("title"),
        "candidate_count": len(page_targets or []),
        "targets": target_parts,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _empty_mapping_approval_key(
    entries: list[dict[str, Any]],
    page_targets: list[dict[str, Any]] | None,
    probe_info: dict[str, Any] | None = None,
) -> str:
    scope_parts = sorted(
        ":".join(
            str(part or "")
            for part in (
                entry.get("skill_id"),
                entry.get("target_id"),
                entry.get("source_fingerprint_hash"),
                entry.get("url_scope"),
            )
        )
        for entry in entries
        if isinstance(entry, dict)
    )
    probe = dict(probe_info or {})
    page_signature = str(probe.get("page_signature") or _page_target_signature(page_targets, probe))
    raw = json.dumps({"scope": scope_parts, "page_signature": page_signature}, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"empty-mapping:{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


def _normalize_mapping_url(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return re.sub(r"#.*$", "", raw)


def _page_mapping_session_key(
    entries: list[dict[str, Any]],
    page_targets: list[dict[str, Any]] | None,
    probe_info: dict[str, Any] | None = None,
) -> str:
    scope_parts = sorted(
        ":".join(
            str(part or "")
            for part in (
                entry.get("skill_id"),
                entry.get("target_id"),
                entry.get("source_fingerprint_hash"),
                entry.get("url_scope"),
            )
        )
        for entry in entries
        if isinstance(entry, dict)
    )
    candidate_keys = sorted(
        str(entry.get("candidate_key") or "")
        for entry in entries
        if isinstance(entry, dict) and entry.get("candidate_key") and _is_concrete_mapping_candidate(entry)
    )
    probe = dict(probe_info or {})
    page_signature = str(probe.get("page_signature") or _page_target_signature(page_targets, probe))
    raw = json.dumps(
        {
            "scope": scope_parts,
            "candidate_keys": candidate_keys,
            "page_signature": page_signature,
            "probe_status": str(probe.get("status") or ""),
            "url": str(probe.get("url") or _url_from_page_targets(page_targets) or ""),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _fingerprint_label(fingerprint: dict[str, Any]) -> str:
    for key in ("nearby_text", "aria_label", "aria-label", "title", "text", "id", "name"):
        label = _string_or_none(fingerprint.get(key))
        if label:
            return _humanize_identifier(label) if key in {"id", "name"} else _clean_target_label(label)
    attrs = fingerprint.get("stable_attributes") if isinstance(fingerprint.get("stable_attributes"), dict) else {}
    for key in ("aria-label", "title", "id", "name"):
        label = _string_or_none(attrs.get(key))
        if label:
            return _humanize_identifier(label) if key in {"id", "name"} else _clean_target_label(label)
    return "protected target"


def _mapping_action_relevant(fingerprint: dict[str, Any], action: str) -> bool:
    expected = expected_actions_for_fingerprint(fingerprint)
    if action in expected:
        return True
    if "select" in expected and action in {"click", "focus", "form_control_activation", "keyboard", "select"}:
        return True
    if "form_input" in expected and action in {"focus", "form_input"}:
        return True
    if "check" in expected and action in {"click", "check", "focus", "keyboard"}:
        return True
    if not expected.isdisjoint(HIGH_RISK_MISSING_TARGET_ACTIONS_FOR_GUARD) and action in HIGH_RISK_MISSING_TARGET_ACTIONS_FOR_GUARD:
        return True
    return False


def _current_option_inherits_select_mapping(current: dict[str, Any], mapped_select: dict[str, Any]) -> bool:
    current_role = str(current.get("role") or "").lower()
    if current_role != "option":
        return False
    mapped_tag = str(mapped_select.get("tag") or "").lower()
    mapped_role = str(mapped_select.get("role") or "").lower()
    if mapped_tag != "select" and mapped_role not in {"combobox", "listbox"}:
        return False
    current_form = _target_structural_text(current, "form_id") or _target_structural_text(current, "form_name")
    mapped_form = _target_structural_text(mapped_select, "form_id") or _target_structural_text(mapped_select, "form_name")
    if current_form and mapped_form and current_form == mapped_form:
        return True
    current_section = _target_structural_text(current, "section_heading")
    mapped_section = _target_structural_text(mapped_select, "section_heading")
    return _loose_text_match(current_section, mapped_section)


def _metadata_targets_same(left: dict[str, Any], right: dict[str, Any]) -> bool:
    for key in ("id", "name", "dom_path"):
        left_value = _target_structural_text(left, key)
        right_value = _target_structural_text(right, key)
        if left_value and right_value and left_value == right_value:
            return True
    left_attrs = left.get("attributes") if isinstance(left.get("attributes"), dict) else {}
    right_attrs = right.get("attributes") if isinstance(right.get("attributes"), dict) else {}
    for key in ("id", "name"):
        if left_attrs.get(key) and right_attrs.get(key) and left_attrs.get(key) == right_attrs.get(key):
            return True
    left_index = _target_structural_int(left, "control_index")
    right_index = _target_structural_int(right, "control_index")
    same_form = bool(
        _target_structural_text(left, "form_id")
        and _target_structural_text(left, "form_id") == _target_structural_text(right, "form_id")
    )
    same_kind_index = _target_structural_int(left, "same_kind_index") == _target_structural_int(right, "same_kind_index")
    return bool(same_form and left_index is not None and left_index == right_index and same_kind_index)


def _kind_changed_resolution_requires_review(resolution: dict[str, Any] | None) -> bool:
    if not resolution:
        return False
    expected_kind = resolution.get("expected_kind")
    observed_kind = resolution.get("observed_kind")
    if not expected_kind or not observed_kind or expected_kind == observed_kind:
        return False

    signals = resolution.get("matched_signals")
    signal_set = {str(signal) for signal in signals} if isinstance(signals, list) else set()
    identity_score = float(resolution.get("identity_score") or 0.0)

    # A wrong-kind decoy with the same visible identity is genuinely confusing
    # and should stay HITL even if the original protected element is still on
    # the page. A wrong-kind decoy that only shares a structural slot can be
    # reconciled against the real page target and allowed when unrelated.
    return identity_score >= 35 or bool(
        signal_set
        & {
            "id",
            "selector_id",
            "name",
            "text_exact",
            "text_contains",
            "identity_token_high_value",
        }
    )


def _missing_target_same_region(fingerprint: dict[str, Any], target: dict[str, Any], action: str) -> bool:
    if not target:
        return False

    expected_index = _fingerprint_structural_int(fingerprint, "control_index")
    observed_index = _target_structural_int(target, "control_index")
    close_index = expected_index is not None and observed_index is not None and abs(expected_index - observed_index) <= 2
    if not close_index:
        return False

    expected_section = _fingerprint_structural_text(fingerprint, "section_heading")
    observed_section = _target_structural_text(target, "section_heading")
    same_section = _loose_text_match(expected_section, observed_section)

    form = fingerprint.get("form") or fingerprint.get("form_context")
    same_form = False
    if isinstance(form, dict):
        for key in ("id", "name", "action"):
            expected = _string_or_none(form.get(key))
            observed = _string_or_none(target.get(f"form_{key}") or target.get(f"form{key.capitalize()}"))
            if expected and observed and expected.strip().lower() == observed.strip().lower():
                same_form = True
                break

    if same_section:
        return True
    return same_form and action in TARGET_AWARE_SAFE_FORM_ACTIONS


def _fingerprint_structural_text(fingerprint: dict[str, Any], key: str) -> str | None:
    value = fingerprint.get(key)
    if value is None and isinstance(fingerprint.get("structural_context"), dict):
        value = fingerprint["structural_context"].get(key)
    return _string_or_none(value)


def _target_structural_text(target: dict[str, Any], key: str) -> str | None:
    value = target.get(key)
    if value is None:
        camel = re.sub(r"_([a-z])", lambda match: match.group(1).upper(), key)
        value = target.get(camel)
    return _string_or_none(value)


def _fingerprint_structural_int(fingerprint: dict[str, Any], key: str) -> int | None:
    value = fingerprint.get(key)
    if value is None and isinstance(fingerprint.get("structural_context"), dict):
        value = fingerprint["structural_context"].get(key)
    return _int_or_none(value)


def _target_structural_int(target: dict[str, Any], key: str) -> int | None:
    value = target.get(key)
    if value is None:
        camel = re.sub(r"_([a-z])", lambda match: match.group(1).upper(), key)
        value = target.get(camel)
    return _int_or_none(value)


def _loose_text_match(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    left_tokens = {token for token in re.findall(r"[a-z0-9]+", left.lower()) if len(token) >= 3}
    right_tokens = {token for token in re.findall(r"[a-z0-9]+", right.lower()) if len(token) >= 3}
    if not left_tokens or not right_tokens:
        return False
    return left_tokens == right_tokens or len(left_tokens & right_tokens) / min(len(left_tokens), len(right_tokens)) >= 0.67


def _normalize_js(expression: str) -> str:
    return re.sub(r"\s+", " ", expression.strip().rstrip(";")).strip()


def _point_probe_js(ignored_selectors: tuple[str, ...] = ()) -> str:
    selectors = [selector for selector in ignored_selectors if isinstance(selector, str) and selector.strip()]
    return _POINT_PROBE_JS_TEMPLATE.replace("__ACIRA_IGNORED_SELECTORS__", json.dumps(selectors, ensure_ascii=True))


_ACIRA_PAGE_TARGETS_PROBE_JS_SHAPES: set[str] | None = None
_ACIRA_RUNTIME_OBJECT_PROBE_JS_SHAPES: set[str] | None = None


def _is_acira_page_targets_probe_js(expression: str) -> bool:
    global _ACIRA_PAGE_TARGETS_PROBE_JS_SHAPES
    normalized = _normalize_js(expression)
    if not normalized:
        return False
    if _ACIRA_PAGE_TARGETS_PROBE_JS_SHAPES is None:
        direct_probe = f"({PAGE_TARGETS_PROBE_JS})()"
        adapter_probe = (
            "(() => {"
            f" const targets = ({PAGE_TARGETS_PROBE_JS})();"
            " return { url: location.href, title: document.title, targets };"
            "})()"
        )
        adapter_ready_state_probe = (
            "(() => {"
            f" const targets = ({PAGE_TARGETS_PROBE_JS})();"
            " return { url: location.href, title: document.title, readyState: document.readyState, targets };"
            "})()"
        )
        _ACIRA_PAGE_TARGETS_PROBE_JS_SHAPES = {
            _normalize_js(direct_probe),
            _normalize_js(adapter_probe),
            _normalize_js(adapter_ready_state_probe),
        }
    return normalized in _ACIRA_PAGE_TARGETS_PROBE_JS_SHAPES


def _is_acira_runtime_object_probe_js(expression: str) -> bool:
    global _ACIRA_RUNTIME_OBJECT_PROBE_JS_SHAPES
    normalized = _normalize_js(expression)
    if not normalized:
        return False
    if _ACIRA_RUNTIME_OBJECT_PROBE_JS_SHAPES is None:
        _ACIRA_RUNTIME_OBJECT_PROBE_JS_SHAPES = {_normalize_js(RUNTIME_OBJECT_PROBE_JS)}
    return normalized in _ACIRA_RUNTIME_OBJECT_PROBE_JS_SHAPES


def _safe_one_line_snippet(expression: str, limit: int) -> str:
    one_line = re.sub(r"\s+", " ", expression.replace("\x00", "")).strip()
    return _truncate(one_line, limit)


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[: max(0, limit - 1)]}..."


def _string_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _string_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(token for token in value.split() if token)
    if isinstance(value, list):
        return tuple(str(item) for item in value if str(item))
    return ()


def _string_dict(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items() if item is not None}


def _dict_or_none(value: Any) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, dict) else None


def _review_result_allows(result: ReviewResult) -> bool:
    if isinstance(result, bool):
        return result
    return str(result).strip().lower() in {"allow", "allowed", "yes", "y", "true", "1"}


def _color(text: str, color: str) -> str:
    colors = {
        "red": "\033[31m",
        "green": "\033[32m",
        "blue": "\033[34m",
        "yellow": "\033[33m",
    }
    prefix = colors.get(color, "")
    suffix = "\033[0m" if prefix else ""
    return f"{prefix}{text}{suffix}"

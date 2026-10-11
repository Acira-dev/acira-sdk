"""Review-only screenshot masking in runtime memory, before storage or transport."""
from __future__ import annotations

import asyncio
import base64
import io
import math
import time
from typing import Any

from .redaction import redact_value

CAPTURE_REASONS = {
    "geometry_unavailable": "The browser could not establish reliable screenshot coordinates",
    "page_too_large": "The page exceeds the bounded privacy scan limit",
    "masking_effects_unbounded": "Sensitive content has visual effects that cannot be reliably masked",
    "target_unavailable": "The reviewed target is hidden, detached, or outside the captured viewport",
    "page_unstable": "The page or target kept moving while screenshot evidence was captured",
    "image_too_large": "The screenshot exceeds the bounded image size limit",
    "capture_timeout": "The screenshot privacy scan exceeded its time limit",
    "capture_unavailable": "A stable, reliably masked screenshot could not be captured",
}

class CaptureUnavailable(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(CAPTURE_REASONS[code])

def unavailable_capture(code="capture_unavailable"):
    # Only fixed messages leave the runtime. Browser exception strings and page
    # payloads may contain secrets and must never become screenshot diagnostics.
    return {"capture_status": "unavailable", "reason_code": code, "reason": CAPTURE_REASONS[code]}

def _native_masks(snapshot: dict, metrics: dict, frame: dict) -> dict:
    """Normalize browser-owned layout, including pseudo and closed-shadow text.

    Page JavaScript must not be able to forge the masking scan. Snapshot strings
    and input values stay private in runtime memory; only masked pixels leave it.
    """
    viewport = metrics["cssVisualViewport"]
    # DOMSnapshot layout bounds use device pixels. DOM.getBoxModel and adapter
    # target boxes use CSS pixels. Derive the conversion from browser-owned
    # metrics, including fractional device scale and browser zoom.
    layout, css_layout = metrics["layoutViewport"], metrics["cssLayoutViewport"]
    scale = layout["clientWidth"] / css_layout["clientWidth"]
    height_scale = layout["clientHeight"] / css_layout["clientHeight"]
    if not math.isfinite(scale) or not 0.1 <= scale <= 16 or not math.isclose(scale, height_scale, rel_tol=0.02):
        raise CaptureUnavailable("geometry_unavailable")
    strings, documents = snapshot["strings"], snapshot["documents"]
    if len(strings) > 100_000 or sum(len(value) for value in strings) > 2_000_000:
        raise CaptureUnavailable("page_too_large")
    document = next(item for item in documents if strings[item["frameId"]] == frame["id"])
    if frame.get("parentId"):
        raise CaptureUnavailable("geometry_unavailable")
    nodes, layout = document["nodes"], document["layout"]
    count = len(nodes["nodeName"])
    if count > 20_000 or len(layout["nodeIndex"]) > 20_000:
        raise CaptureUnavailable("page_too_large")
    width, height = viewport["clientWidth"], viewport["clientHeight"]
    if not width or not height:
        raise CaptureUnavailable("geometry_unavailable")
    masks, unsafe = [], []
    styles = {node: values for node, values in zip(layout["nodeIndex"], layout["styles"])}
    for index, name in enumerate(nodes["nodeName"]):
        parent = nodes["parentIndex"][index]
        attrs = nodes["attributes"][index]
        editable = any(strings[key].lower() == "contenteditable" for key in attrs[::2])
        masks.append(strings[name] in {"INPUT", "TEXTAREA", "SELECT", "IFRAME", "FRAME", "CANVAS", "VIDEO"}
                     or editable or (parent >= 0 and masks[parent]))
        effects = styles.get(index, [])
        # Shadows/filters can render copies beyond a layout rectangle. Omit the
        # image when a masked node has effects that cannot be bounded reliably.
        unsafe.append((parent >= 0 and unsafe[parent]) or any(strings[value] not in {"", "none", "0px"} for value in effects))
    rectangles, texts = [], []
    for position, node in enumerate(layout["nodeIndex"]):
        x, y, w, h = layout["bounds"][position]
        if not all(math.isfinite(value) for value in (x, y, w, h)):
            raise CaptureUnavailable("geometry_unavailable")
        if w <= 0 or h <= 0:
            continue
        rect = {"x": x/scale-viewport["pageX"], "y": y/scale-viewport["pageY"], "width": w/scale, "height": h/scale}
        text = strings[layout["text"][position]]
        sensitive = masks[node] or redact_value(text) != text
        if sensitive and unsafe[node]:
            raise CaptureUnavailable("masking_effects_unbounded")
        if sensitive:
            rectangles.append(rect)
        if text:
            texts.append({"text": text, "rect": rect})
    return {"rectangles": rectangles, "texts": texts, "width": width, "height": height,
            "offset_x": viewport["offsetX"], "offset_y": viewport["offsetY"],
            "page_x": viewport["pageX"], "page_y": viewport["pageY"],
            "url": strings[document["documentURL"]]}


async def capture_masked_cdp(probe: Any, *, session_id: str | None = None, bounding_box: dict | None = None,
                             target_probe: Any = None) -> dict[str, Any]:
    async def call(method: str, params: dict) -> dict:
        response = await asyncio.wait_for(probe(method, params, session_id), 5)
        if not isinstance(response, dict) or response.get("error") or response.get("exceptionDetails"):
            raise ValueError("Screenshot evidence unavailable")
        if "id" in response and isinstance(response.get("result"), dict):
            response = response["result"]
        return response
    try:
        from PIL import Image, ImageDraw
        async def inspect() -> dict:
            tree = await call("Page.getFrameTree", {})
            metrics = await call("Page.getLayoutMetrics", {})
            snapshot = await call("DOMSnapshot.captureSnapshot", {"computedStyles": ["text-shadow", "filter", "-webkit-text-stroke-width", "-webkit-box-reflect"]})
            return _native_masks(snapshot, metrics, tree["frameTree"]["frame"])
        # Recapture evidence only; this never repeats a browser action. A fresh
        # target probe is required to refresh an overlay after page movement.
        async with asyncio.timeout(5):
            for attempt in range(3):
                before = await inspect()
                if target_probe is not None:
                    bounding_box = await target_probe()
                    if not isinstance(bounding_box, dict):
                        raise CaptureUnavailable("target_unavailable")
                response = await call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False, "fromSurface": True,
                    "clip":{"x":before["page_x"],"y":before["page_y"],"width":before["width"],"height":before["height"],"scale":1}})
                stable = before == await inspect()
                if target_probe is not None:
                    stable = bounding_box == await target_probe() and stable
                if stable:
                    break
                if attempt == 2 or (bounding_box is not None and target_probe is None):
                    raise CaptureUnavailable("page_unstable")
                await asyncio.sleep(0.05)
        raw = base64.b64decode(response["data"], validate=True)
        if len(raw) > 10 * 1024 * 1024:
            raise CaptureUnavailable("image_too_large")
        image = Image.open(io.BytesIO(raw))
        if image.width * image.height > 25_000_000:
            raise CaptureUnavailable("image_too_large")
        image = image.convert("RGB")
        draw = ImageDraw.Draw(image)
        rectangles = list(before["rectangles"])
        for item in before["texts"]:
            if redact_value(item["text"]) != item["text"]:
                rectangles.append(item["rect"])
        sx, sy = image.width / before["width"], image.height / before["height"]
        for rect in rectangles:
            if rect["width"] <= 0 or rect["height"] <= 0:
                continue
            draw.rectangle((rect["x"]*sx-3, rect["y"]*sy-3,
                            (rect["x"]+rect["width"])*sx+3, (rect["y"]+rect["height"])*sy+3), fill="black")
        output = io.BytesIO()
        image.save(output, format="PNG")
        if output.tell() > 10 * 1024 * 1024:
            raise CaptureUnavailable("image_too_large")
        if bounding_box is not None:
            bounding_box = {**bounding_box, "x":bounding_box["x"]-before.get("offset_x",0),
                            "y":bounding_box["y"]-before.get("offset_y",0)}
            x,y,w,h = (bounding_box[key] for key in ("x","y","width","height"))
            if not all(math.isfinite(value) for value in (x,y,w,h)) or w <= 0 or h <= 0 or x >= before["width"] or y >= before["height"] or x+w <= 0 or y+h <= 0:
                raise CaptureUnavailable("target_unavailable")
        return {"capture_status": "ready", "image": base64.b64encode(output.getvalue()).decode(),
                "mime_type": "image/png", "bounding_box": bounding_box,
                "viewport": {"width": before["width"], "height": before["height"]},
                "captured_at": time.time(), "masking": "editable controls, embedded frames, canvases, and detected secret text"}
    except CaptureUnavailable as exc:
        return unavailable_capture(exc.code)
    except TimeoutError:
        return unavailable_capture("capture_timeout")
    except Exception:
        return unavailable_capture()

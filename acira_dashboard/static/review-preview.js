/* Local stills of Acira's original cast. The masked capture remains the evidence. */
(() => {
  "use strict";
  const node = (tag, cls, text) => {
    const element = document.createElement(tag);
    if (cls) element.className = cls;
    if (text !== undefined) element.textContent = text;
    return element;
  };
  const preferenceKey = "acira.mascots.v1";
  const modes = ["animated", "still", "hidden"];
  function appearance(mode) {
    if (mode === undefined) {
      try { mode = localStorage.getItem(preferenceKey); } catch { /* Storage is optional. */ }
    } else {
      try { localStorage.setItem(preferenceKey, mode); } catch { /* Apply for this page. */ }
    }
    mode = modes.includes(mode) ? mode : "still";
    document.documentElement.dataset.mascots = mode;
    return mode;
  }
  appearance();
  addEventListener("storage", (event) => { if (event.key === preferenceKey) appearance(); });

  function mascot(who, cls = "", framing = "head") {
    const image = node("img", "acira-mascot " + cls);
    image.src = `/assets/mascots/${who}-${framing}.png`;
    image.alt = "";
    image.setAttribute("aria-hidden", "true");
    image.draggable = false;
    return image;
  }
  function perchedPill() {
    const perch = node("span", "acira-mascot target-companion pill-perch");
    perch.setAttribute("aria-hidden", "true");
    const still = mascot("pill", "pill-perch-still", "perched"),
      movement = node("span", "pill-perch-motion");
    perch.append(still, movement);
    return perch;
  }
  function question(action, label) {
    const verbs = { click: "Click", double_click: "Double-click", fill: "Fill", type: "Type into", submit: "Submit", select: "Select", check: "Check", uncheck: "Uncheck", upload: "Upload to", download: "Download", navigation: "Open" };
    const verb = verbs[action];
    return verb && label ? `${verb} “${label.slice(0, 160)}”?` : "Allow this browser action?";
  }

  // Screenshot pixels and viewport coordinates are different on Retina displays.
  // Clip a genuine target to the capture; never invent a target for missing metadata.
  function geometry(artifact, imageWidth, imageHeight) {
    const box = artifact.bounding_box, viewport = artifact.viewport;
    if (!box || !viewport || ![box.x, box.y, box.width, box.height, viewport.width, viewport.height, imageWidth, imageHeight].every(Number.isFinite)
        || box.width <= 0 || box.height <= 0 || viewport.width <= 0 || viewport.height <= 0 || imageWidth <= 0 || imageHeight <= 0) return null;
    const sx = imageWidth / viewport.width, sy = imageHeight / viewport.height;
    const x = Math.max(0, box.x * sx), y = Math.max(0, box.y * sy);
    const right = Math.min(imageWidth, (box.x + box.width) * sx);
    const bottom = Math.min(imageHeight, (box.y + box.height) * sy);
    if (right <= x || bottom <= y) return null;
    const width = right - x, height = bottom - y;
    const cropWidth = Math.min(imageWidth, Math.max(360 * sx, width + 72 * sx));
    const cropHeight = Math.min(imageHeight, Math.max(110 * sy, height + 64 * sy));
    const cropX = Math.max(0, Math.min(imageWidth - cropWidth, x + width / 2 - cropWidth / 2));
    const cropY = Math.max(0, Math.min(imageHeight - cropHeight, y + height / 2 - cropHeight / 2));
    return { x, y, width, height, cropX, cropY, cropWidth, cropHeight, paddingX: 6 * sx, paddingY: 6 * sy,
      partial: box.x < 0 || box.y < 0 || box.x + box.width > viewport.width || box.y + box.height > viewport.height };
  }
  function svgElement(tag, attributes) {
    const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.entries(attributes).forEach(([key, value]) => element.setAttribute(key, String(value)));
    return element;
  }
  function outline(width, height, rect, pointer) {
    const svg = svgElement("svg", { viewBox: `0 0 ${width} ${height}`, "aria-hidden": "true", class: "review-annotation", preserveAspectRatio: "none" });
    // Keep the exact browser bounds separate from the decorative outer ring.
    // Neither the blue stroke nor its white halo should paint over target text.
    svg.append(svgElement("rect", { x: rect.x, y: rect.y, width: rect.width, height: rect.height, fill: "none", stroke: "none", class: "target-bounds" }));
    const attributes = { x: rect.x - rect.paddingX, y: rect.y - rect.paddingY,
      width: rect.width + 2 * rect.paddingX, height: rect.height + 2 * rect.paddingY,
      rx: 5, fill: "none", "vector-effect": "non-scaling-stroke" };
    svg.append(svgElement("rect", { ...attributes, class: "target-halo" }), svgElement("rect", { ...attributes, class: "target-outline" }));
    if (pointer) {
      const fromRight = rect.x + rect.width / 2 < width / 2;
      const startX = fromRight ? width - 36 : 36;
      const endX = fromRight ? rect.x + rect.width + rect.paddingX + 5 : rect.x - rect.paddingX - 5;
      const endY = rect.y + rect.height / 2;
      const path = `M ${startX} 12 Q ${startX} ${endY} ${Math.max(2, Math.min(width - 2, endX))} ${endY}`;
      svg.append(svgElement("path", { d: path, class: "target-pointer", fill: "none", "vector-effect": "non-scaling-stroke", pathLength: 1 }));
    }
    return svg;
  }
  function picture(image, crop, rect, cls, label, pointer) {
    const frame = node("div", "review-picture");
    const canvas = node("canvas", cls);
    canvas.width = Math.round(crop.width);
    canvas.height = Math.round(crop.height);
    canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", label);
    canvas.getContext("2d").drawImage(image, crop.x, crop.y, crop.width, crop.height, 0, 0, canvas.width, canvas.height);
    canvas.dataset.rendered = "true";
    frame.append(canvas);
    if (rect) {
      const scaleX = canvas.width / crop.width, scaleY = canvas.height / crop.height;
      frame.append(outline(canvas.width, canvas.height, { x: (rect.x - crop.x) * scaleX, y: (rect.y - crop.y) * scaleY,
        width: rect.width * scaleX, height: rect.height * scaleY, paddingX: rect.paddingX * scaleX, paddingY: rect.paddingY * scaleY }, pointer));
    }
    return frame;
  }
  function preview(artifact, source, targetLabel) {
    const section = node("section", "review-preview");
    const loading = node("p", "muted", "Loading masked screenshot…");
    loading.setAttribute("role", "status");
    section.append(loading);
    const image = new Image();
    image.onload = () => {
      const rect = geometry(artifact, image.naturalWidth, image.naturalHeight);
      const children = [];
      if (rect) {
        const focus = node("figure", "review-focus");
        const caption = node("figcaption", "target-caption");
        if (rect.x - rect.cropX + rect.width / 2 < rect.cropWidth / 2) caption.classList.add("companion-right");
        const text = node("div");
        text.append(node("strong", "", "Proposed target"), node("span", "muted", targetLabel || "Resolved browser target"));
        caption.append(perchedPill(), text);
        focus.append(caption, picture(image, { x: rect.cropX, y: rect.cropY, width: rect.cropWidth, height: rect.cropHeight }, rect,
          "review-focus-canvas", "Masked target close-up. The proposed target is outlined in blue.", true));
        if (rect.partial) focus.append(node("p", "capture-note", "Part of the target is outside this capture. Review the full page and action details."));
        children.push(focus);
      } else children.push(node("p", "muted", "Target coordinates are unavailable in this capture. Use the action details; no highlight has been inferred."));
      const overview = node("details", "review-overview");
      overview.open = !rect;
      overview.append(node("summary", "", "Full page screenshot"), picture(image,
        { x: 0, y: 0, width: image.naturalWidth, height: image.naturalHeight }, rect, "review-overview-canvas",
        rect ? "Full masked browser screenshot with the proposed target outlined in blue." : "Full masked browser screenshot. Target coordinates are unavailable.", false));
      const when = Number.isFinite(artifact.captured_at) ? "Captured " + new Date(artifact.captured_at * 1000).toLocaleTimeString() : "Capture time unavailable";
      const note = node("p", "capture-note", when + " · editable controls masked. Acira rechecks the target before acting.");
      section.replaceChildren(...children, overview, note);
      section.dataset.loaded = "true";
    };
    image.onerror = () => section.replaceChildren(node("p", "muted", "Screenshot unavailable or expired. Review the structured action details."));
    image.src = source;
    return section;
  }
  window.AciraReview = Object.freeze({ appearance, mascot, question, geometry, preview });
})();

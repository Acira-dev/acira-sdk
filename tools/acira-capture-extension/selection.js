(() => {
  const token = location.hash.slice(1);
  if (!/^[0-9a-f]{48}$/.test(token)) return;
  const port = chrome.runtime.connect({name: "acira.selection.frame." + token});
  function send(event, kind, extra = {}) {
    if (!event.isTrusted) return;
    try { port.postMessage({type: "acira.selection", kind, x: event.clientX, y: event.clientY, ...extra}); }
    catch { /* A closed bridge keeps native input isolated from the page. */ }
  }
  window.addEventListener("mousemove", event => send(event, "move"));
  window.addEventListener("pointerdown", event => { event.preventDefault(); document.body.focus(); });
  window.addEventListener("click", event => { event.preventDefault(); send(event, "select"); });
  window.addEventListener("wheel", event => {
    event.preventDefault();
    const scale = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? innerHeight : 1;
    send(event, "scroll", {dx: event.deltaX * scale, dy: event.deltaY * scale});
  }, {passive: false});
  window.addEventListener("keydown", event => {
    if (event.key === "Escape") { event.preventDefault(); send(event, "escape"); }
  });
  port.postMessage({type: "acira.selection", kind: "ready"});
})();

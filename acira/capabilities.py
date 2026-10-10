"""Conservative adapter claims, shared by runtime diagnostics and the dashboard."""
from __future__ import annotations


def capabilities(adapter: str) -> dict:
    common = {"pause": True, "resume": True, "stop": False,
              "browser_family": "Chromium", "api": "asynchronous",
              "boundary": "Actions through the protected integration; not an operating-system sandbox",
              "critical_evidence": "Missing required operation facts block the action"}
    if adapter == "playwright":
        return {**common,
            "guarded": ["Page, Locator and ElementHandle actions", "Frame and popup actions",
                        "resolved keyboard and coordinate actions", "file input selection",
                        "local, persistent and CDP-connected contexts"],
            "operation_contracts": ["Exact single JSON/form request with submit()", "Bounded text fields with fill_fields()"],
            "limitations": ["No arbitrary script/CDP mutation", "No forced pointer, drag or unbounded key/mouse sequences",
                "fill_fields does not combine application-specific captured policies",
                "submit requires a context created with service workers blocked; one operation per browser context at a time",
                "Independent HTTP clients, browser extensions and unrelated browser connections are outside the boundary"]}
    return {**common,
        "guarded": ["Supported browser-use events and classified CDP mutations", "Exact element activation including resolved frame nodes", "Resolved DOM targets with review revalidation"],
        "operation_contracts": ["Exact single JSON/form request with submit(session, node, ...)", "Bounded text fields with fill_fields(session, ...)", "Just-in-time secret references"],
        "limitations": ["Project element clicks use the DOM activation algorithm once; no native pointer/JS fallback chain",
            "Coordinate clicks in frames require verifiable point-to-element evidence; otherwise rejected",
            "Frame screenshot coordinates may be unavailable; structured operation evidence remains visible",
            "Uploads are at most 32 MiB each; private copies live until run completion (256 MiB / 128 files per run)",
            "fill_fields does not combine application-specific captured policies",
            "submit bypasses page service workers and blocks unreviewed requests from related popup/frame/worker targets",
            "submit permits one independently reviewed tab request at a time within an exclusively owned browser-use browser",
            "Critical policies requiring request-bound fields block when that evidence is unavailable",
            "Independent HTTP clients and unrelated browser connections are outside the boundary"]}

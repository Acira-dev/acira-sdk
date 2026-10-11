"""Specific-policy observations remain accurate and bounded in durable history."""
import pytest
from acira.journal import Journal
from acira_cdp_proxy.guard import PAGE_TARGETS_PROBE_JS

@pytest.mark.asyncio
async def test_page_mapping_keeps_each_link_own_label_and_neighbor_context():
    async_api = pytest.importorskip("playwright.async_api")
    async with async_api.async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page()
            await page.set_content('<main><a id="guide" href="/guide">Open guide</a><a id="about" href="/about">About this project</a><a aria-label="API reference" href="/api">API</a></main>')
            targets = await page.evaluate(PAGE_TARGETS_PROBE_JS)
            assert [target["text"] for target in targets] == ["Open guide", "About this project", "API reference"]
            assert targets[0]["nextControlText"] == "About this project"
            assert targets[1]["previousControlText"] == "Open guide"
            assert targets[1]["href"] == "/about"
        finally:
            await browser.close()

def test_large_page_observations_are_not_persisted_but_selected_evidence_is(tmp_path):
    from acira.journal import Journal
    journal = Journal(tmp_path)
    observation = [{"text": "runtime-only " * 2000, "id": str(index)} for index in range(100)]
    event = {"details": {"page_targets": observation, "page_target_probe": {"status": "ok"}},
             "target": {"text": "Public guide"}, "mapping": {"status": "auto_mapped"}}
    journal.event("test_observation", classification=event)
    recorded = journal.events()[0]["classification"]
    assert recorded["details"]["page_targets"]["omitted"] is True
    assert recorded["details"]["page_targets"]["candidate_count"] == 100
    assert recorded["target"]["text"] == "Public guide" and recorded["mapping"]["status"] == "auto_mapped"
    assert observation[0]["text"].startswith("runtime-only "), "Persisting must not mutate live evaluator evidence"
    with pytest.raises(OSError, match="per-record bound"):
        journal.event("oversized_other_record", unrelated="x" * (1024 * 1024 + 1))

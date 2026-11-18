"""Run against a locally running service with Playwright and Chromium installed."""
import os
from playwright.sync_api import expect, sync_playwright

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(channel=os.environ.get("CREDIT_CHROME_CHANNEL"))
    url = os.environ.get("CREDIT_TEST_URL", "http://127.0.0.1:8000")
    initial = browser.new_page(java_script_enabled=False)
    initial.goto(url)
    expect(initial.get_by_role("button", name="Try Meridian Packaging")).to_be_visible()
    upload_visible = os.environ.get("CREDIT_PUBLIC_DEMO") != "1"
    assert initial.locator("#upload").is_visible() == upload_visible
    initial_layout = [initial.locator(selector).bounding_box() for selector in ("#intro", ".toolbar", "#cases")]
    initial.close()

    page = browser.new_page()
    startup_requests = []
    page.on("request", lambda request: startup_requests.append(request.url))
    page.goto(url)
    expect(page.get_by_role("button", name="Try Meridian Packaging")).to_be_enabled()
    assert page.locator("#upload").is_visible() == upload_visible
    assert [page.locator(selector).bounding_box() for selector in ("#intro", ".toolbar", "#cases")] == initial_layout
    assert not any(url.endswith(("/cases", "/settings")) for url in startup_requests)
    page.get_by_role("button", name="Try Meridian Packaging").click()
    expect(page.locator("#case-name")).to_have_text("Meridian Packaging")
    expect(page.locator("#cases")).not_to_have_value("")
    page.get_by_text("Source ledger", exact=False).click()
    row = page.locator("#financials .exception").filter(has_text="Sales revenue")
    row.get_by_role("button").click()
    expect(page.locator("#preview")).to_be_visible()
    page.wait_for_function("document.getElementById('preview').naturalWidth > 0")
    page.locator("#review select").select_option("")
    page.locator("#review input").fill("Verified the conflicting source row")
    page.get_by_role("button", name="Save review").click()
    expect(page.locator("#metrics")).to_contain_text("13.97%")
    page.get_by_role("button", name="Save memo snapshot").click()
    expect(page.get_by_role("link", name="Evidence JSON")).to_be_visible()
    page.get_by_role("button", name="Load revised Meridian statement").click()
    expect(page.locator("#metrics")).to_contain_text("0.95×")
    expect(page.locator("#comparison")).to_contain_text("operating cash")
    expect(page.get_by_role("link", name="Evidence JSON")).to_be_visible()
    case_id = page.locator("#cases").input_value()
    page.reload()
    page.locator("#cases").select_option(case_id)
    expect(page.locator("#metrics")).to_contain_text("0.95×")
    browser.close()

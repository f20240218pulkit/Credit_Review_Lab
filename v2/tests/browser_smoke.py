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
    demo_button = page.get_by_role("button", name="Try Meridian Packaging")
    expect(demo_button).to_be_enabled()
    assert page.locator("#upload").is_visible() == upload_visible
    assert [page.locator(selector).bounding_box() for selector in ("#intro", ".toolbar", "#cases")] == initial_layout
    assert not any(url.endswith(("/cases", "/settings")) for url in startup_requests)
    picker = page.get_by_label("Open an existing case")
    expect(picker).to_be_disabled()
    expect(picker).to_contain_text("No saved cases yet")
    resting_color = demo_button.evaluate("(button) => getComputedStyle(button).backgroundColor")
    demo_button.hover()
    expect(demo_button).to_have_css("cursor", "pointer")
    hovered_color = demo_button.evaluate("(button) => getComputedStyle(button).backgroundColor")
    assert hovered_color != resting_color
    page.mouse.down()
    assert demo_button.evaluate("(button) => getComputedStyle(button).backgroundColor") != hovered_color
    page.mouse.up()
    expect(page.locator("#case-name")).to_have_text("Meridian Packaging")
    expect(picker).to_be_enabled()
    expect(page.locator("#cases")).not_to_have_value("")
    page.get_by_text("Source ledger", exact=False).click()
    row = page.locator("#financials .exception").filter(has_text="Sales revenue")
    row.get_by_role("button").click()
    expect(page.locator("#preview")).to_be_visible()
    preview = page.locator("#preview")
    expect(preview).to_have_js_property("complete", True)
    assert preview.evaluate("(image) => image.naturalWidth") > 0
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
    page.evaluate("""() => {
      const fetchRequest = window.fetch;
      window.fetch = (url, options) => url.includes("/scenario?")
        ? new Promise(resolve => { window.releaseScenario = () => resolve(fetchRequest(url, options)); })
        : fetchRequest(url, options);
    }""")
    page.get_by_role("button", name="Calculate").click()
    expect(page.get_by_role("button", name="Calculating…")).to_have_attribute("aria-busy", "true")
    page.evaluate("() => window.releaseScenario()")
    expect(page.get_by_role("button", name="Calculate")).to_be_enabled()
    expect(page.locator("#scenario-result")).to_have_text("0.95× baseline → 0.81× stressed")
    case_id = picker.input_value()
    page.reload()
    picker.select_option(case_id)
    expect(page.locator("#metrics")).to_contain_text("0.95×")
    browser.close()

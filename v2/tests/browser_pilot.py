"""Exercise the team pilot handoff in three separate browser contexts."""
import csv
import io
import os
import re
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

url = os.environ.get("CREDIT_TEST_URL", "http://127.0.0.1:8001")
if urlsplit(url).hostname not in {"localhost", "127.0.0.1"}:
    raise RuntimeError("The pilot browser fixture must run on loopback")

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(channel=os.environ.get("CREDIT_CHROME_CHANNEL"))
    owner = browser.new_page(extra_http_headers={"x-pilot-test-user": "owner"})
    analyst = browser.new_page(extra_http_headers={"x-pilot-test-user": "analyst"})
    reviewer = browser.new_page(extra_http_headers={"x-pilot-test-user": "reviewer"})
    outsider = browser.new_page(extra_http_headers={"x-pilot-test-user": "outsider"})
    for page in (owner, analyst, reviewer, outsider):
        page.set_default_timeout(10000)

    owner.goto(url)
    owner.get_by_text("Team settings").click()
    owner.get_by_label("New team name").fill("Pilot Browser Team")
    owner.get_by_role("button", name="Create team").click()
    expect(owner.locator("#notice")).to_have_text("Team created.")
    expect(owner.get_by_role("button", name="Generate invitation")).to_be_enabled()
    for page, role in [(analyst, "analyst"), (reviewer, "reviewer")]:
        owner.get_by_label("Invitee name").fill(role.title())
        owner.get_by_label("Verified email").fill(role + "@example.test")
        owner.locator("#invite-role").select_option(role)
        previous = owner.locator("#invite-link").input_value()
        with owner.expect_response(lambda response: response.url.endswith("/invitations") and response.ok):
            owner.get_by_role("button", name="Generate invitation").click()
        invite = owner.locator("#invite-link")
        expect(invite).to_have_value(re.compile(r"^http://.*#invite="))
        expect(invite).not_to_have_value(previous)
        path = invite.input_value()
        page.goto(path)
        page.get_by_role("button", name="Accept invitation").click()
        expect(page.locator("#notice")).to_have_text("You joined the team.")

    fixture = analyst.request.get(url + "/cases/CR-101").json()["rows"]
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(("year", "label", "value", "unit", "currency"))
    for row in fixture:
        writer.writerow([row[key] for key in ("year", "label", "value", "unit", "currency")])
    analyst.get_by_label("Borrower for new case").fill("Pilot borrower")
    analyst.locator("#upload-organization").select_option(label="Pilot Browser Team")
    analyst.locator("#upload input[type=file]").set_input_files({
        "name": "statement.csv", "mimeType": "text/csv", "buffer": stream.getvalue().encode(),
    })
    analyst.get_by_role("button", name="Upload statements").click()
    expect(analyst.locator("#case-name")).to_have_text("Pilot borrower")
    case_url = analyst.url
    case_id = case_url.split("case=")[1]
    endpoint = url + "/workspace/" + case_id
    token = analyst.request.get(url + "/workspace").headers["x-csrf-token"]
    state = analyst.request.get(endpoint).json()
    for row in state["analysis"]["rows"]:
        response = analyst.request.post(endpoint + "/reviews", headers={"X-CSRF-Token": token}, data={
            "revision": state["revision"], "row_id": row["id"], "account": row["account"], "reason": "Verified",
        })
        assert response.ok, response.text()
        state = response.json()
    analyst.reload()
    expect(analyst.get_by_role("link", name="Download uploaded CSV")).to_be_visible()
    analyst.locator("#assigned-reviewer").select_option(label="Reviewer")
    analyst.get_by_role("button", name="Send for review").click()
    expect(analyst.locator("#workflow-status")).to_contain_text("in review")
    reviewer.goto(case_url)
    expect(reviewer.locator("#case-name")).to_have_text("Pilot borrower")
    reviewer.get_by_role("button", name="Sign off evidence").click()
    expect(reviewer.locator("#workflow-status")).to_contain_text("signed off")
    href = reviewer.get_by_role("link", name="Evidence JSON").get_attribute("href")
    signed = reviewer.request.get(url + href).json()
    assert signed["status"] == "signed_off" and signed["sign_off"]["name"] == "Reviewer"
    outsider.goto(url)
    assert outsider.request.get(endpoint).status == 404
    assert outsider.request.get(url + href).status == 404
    browser.close()

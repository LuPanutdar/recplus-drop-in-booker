#!/usr/bin/env python3
"""
Auto-booker for drop-in classes on the Myrtle Beach Rec+ portal
(https://customer.recplus.cityofmyrtlebeach.com/).

WHY THIS SCRIPT LOOKS THE WAY IT DOES
--------------------------------------
This site is a Next.js app that submits bookings via React "Server Actions"
rather than a normal REST API. Server Action calls are tied to an internal
function hash that changes on every deployment, and the site's session
cookies are httpOnly/opaque, so there is no reliable way to POST a booking
directly with `requests`. Instead this script drives a real (headless)
browser with Playwright and clicks through the same UI a person would use.
That means it depends on a handful of CSS/text selectors on the live site,
marked below with "SELECTOR:" comments. If Rec+ changes their frontend,
these are the lines you'll need to fix.

HOW TO FINALIZE THE SELECTORS
------------------------------
Selectors below are best-effort (based on the site's visible copy and a
captured HAR of a real booking), but were not verified against the live
rendered DOM. Before relying on this for a real registration:

    pip install playwright
    playwright install chromium
    playwright codegen https://customer.recplus.cityofmyrtlebeach.com/login

`codegen` opens a real browser and records your clicks as Playwright code.
Log in, open Drop-Ins, find your class, and click through a real (or
almost-real, then cancel before the final confirm) booking. Compare the
generated locators to the ones marked SELECTOR below and patch any that
differ.

CONFIGURATION (environment variables)
--------------------------------------
REC_EMAIL              login email                         (required)
REC_PASSWORD           login password                       (required)
CLASS_NAME             exact/partial class name, e.g. "Total Resistance" (required)
TARGET_WEEKDAY         weekday of the class, e.g. "Tuesday" (required)
TARGET_TIME            class start time, 24h "HH:MM", e.g. "08:00" (required)
PARTICIPANT_NAME       family member to register, if account has more than
                       one; omit to auto-select the only participant
MEMBERSHIP_NAME        which membership/subscription to register under, if
                       more than one is eligible; omit to auto-select
DRY_RUN                "true" to stop right before the final confirm click
                       (default: "false")
MAX_WAIT_MINUTES       how long to keep retrying if the class isn't open
                       for registration yet (default: 10)
"""

import os
import re
import sys
import time
import logging
from datetime import datetime, timedelta

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("book_class")

BASE_URL = "https://customer.recplus.cityofmyrtlebeach.com"

# Populated in run_once() once we know the real values. Used to redact log
# lines and mask screenshots, since both GitHub Actions logs and uploaded
# screenshot artifacts are publicly visible if the repo is public.
SENSITIVE_TEXTS = []

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def env(name, required=True, default=None):
    val = os.environ.get(name, default)
    if required and not val:
        log.error("Missing required environment variable: %s", name)
        sys.exit(1)
    return val


def mask_email(email):
    """p***@g***.com -- enough to sanity-check in logs without exposing it."""
    local, _, domain = email.partition("@")
    domain_name, _, tld = domain.rpartition(".")
    local_masked = (local[:1] + "***") if local else "***"
    domain_masked = (domain_name[:1] + "***") if domain_name else "***"
    return f"{local_masked}@{domain_masked}.{tld}" if tld else f"{local_masked}@{domain_masked}"


def redact(text):
    """Strip any known sensitive strings (and any email-shaped text) out of
    a string before it goes into a log line."""
    if not text:
        return text
    redacted = EMAIL_RE.sub("[EMAIL]", text)
    for needle in SENSITIVE_TEXTS:
        if needle:
            redacted = redacted.replace(needle, "[REDACTED]")
    return redacted


def masked_screenshot(page, path):
    """
    Take a screenshot with any known-sensitive elements (account email,
    configured participant/membership names, anything email-shaped) covered
    by a solid box. Workflow artifacts are public on a public repo, so this
    runs by default rather than as an opt-in.
    """
    mask_locators = []
    for needle in SENSITIVE_TEXTS:
        if not needle:
            continue
        try:
            loc = page.get_by_text(needle, exact=False)
            if loc.count() > 0:
                mask_locators.append(loc.first)
        except Exception:
            pass
    try:
        email_like = page.locator("text=/[\\w.+-]+@[\\w-]+\\.[\\w.-]+/")
        if email_like.count() > 0:
            mask_locators.append(email_like.first)
    except Exception:
        pass

    try:
        page.screenshot(path=path, mask=mask_locators or None, mask_color="#000000")
    except Exception:
        # Better to have an unmasked debug screenshot than none at all if
        # masking itself errors out.
        log.warning("Screenshot masking failed, falling back to unmasked screenshot")
        page.screenshot(path=path)


def login(page, email, password):
    log.info("Logging in as %s", mask_email(email))
    page.goto(f"{BASE_URL}/login", wait_until="networkidle")

    # SELECTOR: login form fields. The site's HTML `name` attributes for
    # these are internal (e.g. "_1_email"), so we match on visible
    # label/placeholder text instead, which is more resilient to redeploys.
    page.get_by_label("Email", exact=False).fill(email)
    page.get_by_label("Password", exact=False).fill(password)

    # SELECTOR: submit button.
    page.get_by_role("button", name="Sign in", exact=False).click()

    # A successful login redirects to /account.
    page.wait_for_url(f"{BASE_URL}/account*", timeout=15000)
    log.info("Login successful")


def select_participant_and_membership(page, participant_name, membership_name):
    """
    Booking a drop-in on this site can prompt for which family member
    (participant) and which membership/subscription to register under,
    if the account has more than one of either. This handles that modal
    if it appears; if there's only one option (or the modal doesn't show
    up because the account only has one of each), it's a no-op.
    """
    try:
        # SELECTOR: participant dropdown/list, only appears if account has
        # multiple family members on it.
        participant_picker = page.get_by_role("radio").or_(page.get_by_role("option"))
        if participant_name and participant_picker.count() > 0:
            page.get_by_text(participant_name, exact=False).first.click()
    except PWTimeout:
        pass

    try:
        # SELECTOR: membership/subscription picker, only appears if the
        # account has more than one eligible membership for this program.
        if membership_name:
            page.get_by_text(membership_name, exact=False).first.click()
    except PWTimeout:
        pass


def find_and_open_class(page, class_name, target_weekday, target_time):
    log.info("Opening Drop-Ins page")
    page.goto(f"{BASE_URL}/drop-ins", wait_until="networkidle")

    # SELECTOR: search/filter box on the Drop-Ins page.
    search_box = page.get_by_placeholder("Search", exact=False)
    if search_box.count() > 0:
        search_box.first.fill(class_name)
        page.wait_for_timeout(1000)  # debounce

    # SELECTOR: each class occurrence is rendered as a card with the class
    # name and a date/time. We match on the class name text, then check
    # each matching card's visible date/time text for the target weekday
    # and time until we find the right occurrence.
    cards = page.get_by_text(class_name, exact=False)
    count = cards.count()
    log.info("Found %d card(s) matching '%s'", count, class_name)

    target_label = f"{target_weekday}"  # e.g. "Tuesday"
    for i in range(count):
        card = cards.nth(i)
        # Walk up to the enclosing card container to read its full text
        container = card.locator(
            "xpath=ancestor::*[self::article or self::li or contains(@class,'card')][1]"
        )
        text = (container.inner_text() if container.count() > 0 else card.inner_text())
        if target_label.lower() in text.lower() and target_time in text:
            log.info("Matched occurrence: %s", redact(text.replace("\n", " | ")))
            card.click()
            return True

    log.error(
        "No occurrence of '%s' found for %s at %s. Occurrences seen: %s",
        class_name, target_weekday, target_time,
        [redact(cards.nth(i).inner_text()) for i in range(count)],
    )
    return False


def book_class(page, participant_name, membership_name, dry_run):
    # SELECTOR: the class detail view's register/book button.
    register_btn = page.get_by_role("button", name="Register", exact=False)
    if register_btn.count() == 0:
        register_btn = page.get_by_role("button", name="Book", exact=False)
    if register_btn.count() == 0:
        register_btn = page.get_by_role("button", name="Add to Cart", exact=False)

    if register_btn.count() == 0:
        log.error("Could not find a Register/Book/Add to Cart button on the class page.")
        return False

    register_btn.first.click()
    page.wait_for_timeout(1000)

    select_participant_and_membership(page, participant_name, membership_name)

    # SELECTOR: final confirmation button, e.g. "Confirm", "Complete
    # Registration", "Checkout". Adjust after a codegen run.
    confirm_btn = page.get_by_role("button", name="Confirm", exact=False)
    if confirm_btn.count() == 0:
        confirm_btn = page.get_by_role("button", name="Checkout", exact=False)
    if confirm_btn.count() == 0:
        confirm_btn = page.get_by_role("button", name="Complete", exact=False)

    if dry_run:
        log.info("DRY_RUN is set — stopping before the final confirm click.")
        masked_screenshot(page, "dry_run_before_confirm.png")
        return True

    if confirm_btn.count() == 0:
        log.error("Could not find a final confirm/checkout button.")
        masked_screenshot(page, "error_no_confirm_button.png")
        return False

    confirm_btn.first.click()
    page.wait_for_timeout(2000)

    # SELECTOR: success confirmation text. Adjust after a codegen run.
    success = page.get_by_text("success", exact=False).count() > 0 or \
        page.get_by_text("confirmed", exact=False).count() > 0 or \
        page.get_by_text("you're registered", exact=False).count() > 0

    masked_screenshot(page, "booking_result.png")
    return success


def run_once():
    email = env("REC_EMAIL")
    password = env("REC_PASSWORD")
    class_name = env("CLASS_NAME")
    target_weekday = env("TARGET_WEEKDAY")
    target_time = env("TARGET_TIME")
    participant_name = env("PARTICIPANT_NAME", required=False)
    membership_name = env("MEMBERSHIP_NAME", required=False)
    dry_run = env("DRY_RUN", required=False, default="false").lower() == "true"

    # Anything screenshots/logs should redact for a public repo.
    SENSITIVE_TEXTS[:] = [email, participant_name, membership_name]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        try:
            login(page, email, password)

            found = find_and_open_class(page, class_name, target_weekday, target_time)
            if not found:
                return False

            ok = book_class(page, participant_name, membership_name, dry_run)
            if ok:
                log.info("Booking flow completed successfully%s.", " (dry run)" if dry_run else "")
            else:
                log.error("Booking flow did not complete successfully.")
            return ok
        except Exception:
            log.exception("Unexpected error during booking flow")
            masked_screenshot(page, "error_unexpected.png")
            return False
        finally:
            context.close()
            browser.close()


def main():
    """
    Registration on this site opens on a fixed schedule relative to the
    class (based on a captured example: exactly 6 days before the class,
    and closes 2 hours before it starts). Because a class's own listing
    may not appear/be clickable until registration opens, this retries
    for a configurable window in case the workflow fires a little early
    or the exact opening moment is slightly different for your class.
    """
    max_wait_minutes = int(env("MAX_WAIT_MINUTES", required=False, default="10"))
    deadline = datetime.utcnow() + timedelta(minutes=max_wait_minutes)
    attempt = 0

    while True:
        attempt += 1
        log.info("Attempt %d", attempt)
        if run_once():
            log.info("Done.")
            sys.exit(0)

        if datetime.utcnow() >= deadline:
            log.error("Gave up after %d attempt(s) / %d minute(s).", attempt, max_wait_minutes)
            sys.exit(1)

        log.info("Retrying in 30 seconds...")
        time.sleep(30)


if __name__ == "__main__":
    main()

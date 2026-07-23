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

CONFIRMED LIVE BEHAVIOR (from a real captured run)
----------------------------------------------------
- Each Drop-Ins card shows an explicit calendar date, e.g. "Jul 28, 2026",
  NOT a weekday name. TARGET_WEEKDAY is matched by computing the weekday
  from that date ourselves, not by looking for the weekday name as text.
- Times render as a 12h range with AM/PM, e.g. "8:00 AM — 8:45 AM".
- The "Register" button is inline on each occurrence's own card in the
  list -- there is no separate detail page to click through to. The
  script clicks the Register button scoped to the specific matched card,
  not a page-wide search (multiple cards can each have their own visible
  "Register" button at once).

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
WEEKDAY_RE = re.compile(r"(Sunday|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday)", re.I)
TIME_HINT_RE = re.compile(r"\d{1,2}:\d{2}\s*(am|pm)?", re.I)
# Matches dates like "Jul 28, 2026" -- confirmed live rendering on this site.
MONTH_DATE_RE = re.compile(r"\b([A-Za-z]{3,9})\s+(\d{1,2}),\s*(\d{4})\b")


def time_matches(target_time, text):
    """
    TARGET_TIME is configured as 24h "HH:MM" (e.g. "08:00"). Confirmed live
    rendering is 12h with AM/PM and no leading zero (e.g. "8:00 AM"), so
    check every reasonable rendering rather than a single exact substring.
    """
    hour, minute = map(int, target_time.split(":"))
    period = "AM" if hour < 12 else "PM"
    hour12 = hour % 12 or 12
    candidates = {
        f"{hour:02d}:{minute:02d}",
        f"{hour}:{minute:02d}",
        f"{hour12}:{minute:02d} {period}",
        f"{hour12}:{minute:02d}{period}",
        f"{hour12:02d}:{minute:02d} {period}",
    }
    text_low = text.lower()
    return any(c.lower() in text_low for c in candidates)


def extract_weekday(text):
    """
    Confirmed live behavior: the card shows an explicit calendar date
    (e.g. "Jul 28, 2026"), never a weekday name. Parse the date and
    compute its weekday ourselves rather than substring-matching a
    weekday name that's never rendered.
    """
    m = MONTH_DATE_RE.search(text)
    if not m:
        return None
    month_str, day_str, year_str = m.groups()
    for fmt in ("%b %d %Y", "%B %d %Y"):
        try:
            dt = datetime.strptime(f"{month_str} {day_str} {year_str}", fmt)
            return dt.strftime("%A")
        except ValueError:
            continue
    return None


def get_context_container(card, max_level=8):
    """
    get_by_text() locates the exact node containing the class name (often
    just the title element) -- not the surrounding card with the
    date/time/Register button on it. Walk up the DOM one ancestor at a
    time until we hit a level whose text includes a date or time, which
    (confirmed live) is the actual per-occurrence card container.

    Returns (text, container_locator) for that level, so the caller can
    both match against the text and later click controls (like the
    Register button) scoped to that specific occurrence.
    """
    text = card.inner_text()
    container = card
    for level in range(1, max_level + 1):
        candidate_container = card.locator(f"xpath=ancestor::*[{level}]")
        if candidate_container.count() == 0:
            break
        candidate_text = candidate_container.inner_text()
        text = candidate_text
        container = candidate_container
        if WEEKDAY_RE.search(candidate_text) or TIME_HINT_RE.search(candidate_text) \
                or MONTH_DATE_RE.search(candidate_text):
            return candidate_text, candidate_container
    return text, container


def dump_dom_debug(card, max_level=10):
    """
    Diagnostic: when we can't find the right occurrence, print the actual
    HTML/structure around a matched element straight to the log (plain
    text, safe to paste back) so the real markup can be inspected without
    needing a screenshot or live browser access.
    """
    try:
        own_html = card.evaluate("el => el.outerHTML")
    except Exception as e:
        own_html = f"<error reading outerHTML: {e}>"
    log.info("DEBUG matched element outerHTML: %s", redact(own_html[:1000]))

    for level in range(1, max_level + 1):
        container = card.locator(f"xpath=ancestor::*[{level}]")
        if container.count() == 0:
            log.info("DEBUG ancestor level %d: <axis exhausted, no element>", level)
            break
        try:
            info = container.evaluate(
                "el => ({tag: el.tagName, cls: el.className, "
                "text: (el.innerText || '').slice(0,200)})"
            )
        except Exception as e:
            info = {"error": str(e)}
        log.info("DEBUG ancestor level %d: %s", level, redact(str(info)))


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
        log.warning("Screenshot masking failed, falling back to unmasked screenshot")
        page.screenshot(path=path)


def login(page, email, password):
    log.info("Logging in as %s", mask_email(email))
    page.goto(f"{BASE_URL}/login", wait_until="networkidle")

    page.get_by_label("Email", exact=False).fill(email)
    page.get_by_label("Password", exact=False).fill(password)
    page.get_by_role("button", name="Sign in", exact=False).click()

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
        # multiple family members on it. Not yet verified live.
        participant_picker = page.get_by_role("radio").or_(page.get_by_role("option"))
        if participant_name and participant_picker.count() > 0:
            page.get_by_text(participant_name, exact=False).first.click()
    except PWTimeout:
        pass

    try:
        # SELECTOR: membership/subscription picker. Not yet verified live.
        if membership_name:
            page.get_by_text(membership_name, exact=False).first.click()
    except PWTimeout:
        pass


def find_matching_container(page, class_name, target_weekday, target_time):
    """
    Returns the Locator for the specific occurrence's card if a match is
    found, or None. Also returns the matched card so the caller can act
    (click Register) scoped to that exact occurrence.
    """
    log.info("Opening Drop-Ins page")
    page.goto(f"{BASE_URL}/drop-ins", wait_until="networkidle")

    search_box = page.get_by_placeholder("Search", exact=False)
    if search_box.count() > 0:
        search_box.first.fill(class_name)
        page.wait_for_timeout(1500)  # debounce

    cards = page.get_by_text(class_name, exact=False)
    count = cards.count()
    log.info("Found %d card(s) matching '%s'", count, class_name)

    seen = []
    for i in range(count):
        card = cards.nth(i)
        text, container = get_context_container(card)
        seen.append(text)
        occurrence_weekday = extract_weekday(text)
        weekday_ok = (
            occurrence_weekday is not None
            and occurrence_weekday.lower() == target_weekday.lower()
        )
        if weekday_ok and time_matches(target_time, text):
            log.info("Matched occurrence: %s", redact(text.replace("\n", " | ")))
            return container

    log.error(
        "No occurrence of '%s' found for %s at %s. Occurrences seen: %s",
        class_name, target_weekday, target_time,
        [redact(t.replace("\n", " | ")) for t in seen],
    )
    if count > 0:
        dump_dom_debug(cards.nth(0))
    masked_screenshot(page, "error_class_not_matched.png")
    return None


def book_class(page, container, participant_name, membership_name, dry_run):
    # SELECTOR: the Register button lives inline on the matched
    # occurrence's own card (confirmed live) -- scope the search to this
    # container so we don't accidentally click a different date's button.
    register_btn = container.get_by_role("button", name="Register", exact=False)
    if register_btn.count() == 0:
        register_btn = container.get_by_text("Register", exact=False)

    if register_btn.count() == 0:
        log.error("Could not find a Register button on the matched card.")
        masked_screenshot(page, "error_no_register_button.png")
        return False

    register_btn.first.click()
    page.wait_for_timeout(1000)

    select_participant_and_membership(page, participant_name, membership_name)

    # SELECTOR: final confirmation button/modal -- not yet verified live.
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

    # SELECTOR: success confirmation text -- not yet verified live.
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

    SENSITIVE_TEXTS[:] = [email, participant_name, membership_name]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        try:
            login(page, email, password)

            container = find_matching_container(page, class_name, target_weekday, target_time)
            if container is None:
                return False

            ok = book_class(page, container, participant_name, membership_name, dry_run)
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

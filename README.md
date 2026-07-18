# myrtle-beach-class-booker

Automatically registers you for a recurring drop-in class on the
[Myrtle Beach Rec+ portal](https://customer.recplus.cityofmyrtlebeach.com/),
on a schedule, via GitHub Actions.

## How it works

- `book_class.py` uses Playwright to drive a real (headless) Chromium
  browser: it logs in, opens Drop-Ins, finds the class occurrence matching
  your configured weekday/time, and clicks through registration.
- `.github/workflows/book-class.yml` runs that script on a weekly cron, so
  it fires automatically when registration opens.

This drives the actual site UI rather than calling an internal API,
because the site submits bookings through Next.js "Server Actions" whose
internal call signature changes on every deployment — a raw HTTP script
would break constantly. Browser automation is slower but much more durable.

## Safe for a public repo

If you make this repo public, GitHub Actions run logs and uploaded
screenshot artifacts become publicly visible too. The script accounts for
that:

- Your email is never logged in full (only masked, e.g. `p***@g***.com`).
- Screenshots automatically black out your account email and any
  configured `PARTICIPANT_NAME`/`MEMBERSHIP_NAME` text before saving.
- Your password is never logged or captured in a screenshot.

Your actual credentials still only ever live in GitHub Secrets (encrypted,
never shown in logs) — this just keeps the *debug output* from leaking
personal info too. The masking is best-effort (it matches known values by
text), so still glance at a screenshot artifact once after your first real
run to confirm nothing unexpected is showing.

## ⚠️ Before you rely on this

The selectors in `book_class.py` (login fields, search box, Register
button, confirm button, etc.) are marked `SELECTOR:` in comments and are
best-effort — based on the site's visible copy and a HAR capture of a real
booking, not a live inspection of the rendered page. **Verify them once
before trusting this with a real registration:**

```bash
pip install playwright
playwright install chromium
playwright codegen https://customer.recplus.cityofmyrtlebeach.com/login
```

This opens a real browser and records your clicks as working Playwright
code as you log in and click through a booking. Compare what it generates
to the `SELECTOR:` lines in `book_class.py` and fix any that don't match.

Also worth knowing:
- Registration windows are set per-class by the rec center (in the one
  session we captured, registration opened exactly 6 days before the
  class and closed 2 hours before it — yours may differ).
- If the site adds a CAPTCHA or 2FA, this script cannot get past that.
- Check the site's Terms of Service — some booking platforms prohibit
  automated registration. This is meant for your own account and personal
  use; don't use it to scoop up slots at scale.

## Setup

1. **Fork or clone this repo.**

2. **Test locally first** (recommended):
   ```bash
   pip install -r requirements.txt
   playwright install --with-deps chromium
   cp .env.example .env   # fill in your real values, keep DRY_RUN=true
   set -a; source .env; set +a
   python book_class.py
   ```
   With `DRY_RUN=true` it stops right before the final confirm click and
   saves `dry_run_before_confirm.png` so you can check it got to the right
   place.

3. **Set GitHub Actions secrets and variables** (repo Settings → Secrets
   and variables → Actions):
   - Secrets: `REC_EMAIL`, `REC_PASSWORD`
   - Variables: `CLASS_NAME`, `TARGET_WEEKDAY`, `TARGET_TIME`, and
     optionally `PARTICIPANT_NAME`, `MEMBERSHIP_NAME`, `DRY_RUN`

4. **Adjust the cron schedule** in `.github/workflows/book-class.yml` to a
   few minutes before your class's registration window opens (see comments
   in that file — cron is UTC and doesn't auto-adjust for daylight saving).

5. **Test the workflow manually** via the Actions tab → "Book drop-in
   class" → "Run workflow", with `DRY_RUN=true`, before turning it loose
   on a real cron.

6. Once you're confident it works, set `DRY_RUN=false` (or leave it unset)
   and let the schedule run.

## Troubleshooting

- Every run uploads any `*.png` screenshots as a workflow artifact — check
  those first when a run fails.
- If login fails: the email/password locators changed, or the site is
  gating login behind additional verification.
- If it can't find the class: the search box or card-matching selector
  needs adjusting, or `CLASS_NAME`/`TARGET_WEEKDAY`/`TARGET_TIME` don't
  exactly match what's rendered.
- If it finds the class but can't complete registration: the
  Register/Confirm button text changed, or a participant/membership
  picker appeared that the script didn't handle — rerun `playwright
  codegen` to see the current flow.

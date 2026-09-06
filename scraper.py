"""
SharePoint scraper and EDT fetcher using Playwright
Handles authentication and data extraction from ESEO systems
"""
import asyncio
import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List
from playwright.async_api import async_playwright
import requests
import httpx

from utils import get_date_range, filter_event_fields


# Chromium launch flags shared by every browser we start.
#
# --disable-dev-shm-usage is required in Docker: the default /dev/shm is only
# 64 MB there, which Chromium exhausts on JS-heavy pages (Microsoft's SSO
# flow) - the renderer then hangs or dies and every wait_for_selector times
# out. This flag makes Chromium use /tmp instead. Symptom without it: login
# reaches the MFA challenge, then /auth/mfa/verify always times out, while
# the exact same code works fine outside a container.
CHROMIUM_ARGS = ["--disable-dev-shm-usage"]

# MFA session management (async Playwright objects)
_mfa_sessions: Dict[str, "MFASession"] = {}
MFA_SESSION_TIMEOUT = 120  # seconds


class MFASession:
    """Holds async Playwright browser state while waiting for MFA verification"""

    def __init__(self, playwright_instance, browser, page, mfa_type="totp"):
        self.playwright_instance = playwright_instance
        self.browser = browser
        self.page = page
        self.mfa_type = mfa_type
        self.created_at = datetime.now(timezone.utc)

    def is_expired(self):
        return (datetime.now(timezone.utc) - self.created_at).total_seconds() > MFA_SESSION_TIMEOUT

    async def close(self):
        try:
            await self.browser.close()
        except Exception:
            pass
        try:
            await self.playwright_instance.stop()
        except Exception:
            pass


async def _cleanup_expired_mfa_sessions():
    """Remove and close expired MFA sessions"""
    expired = [sid for sid, s in _mfa_sessions.items() if s.is_expired()]
    for sid in expired:
        await _mfa_sessions[sid].close()
        del _mfa_sessions[sid]
        print(f"Cleaned up expired MFA session: {sid[:8]}...")


async def mfa_cleanup_loop(interval_seconds: int = 60):
    """
    Periodically close and discard expired MFA sessions.

    Each MFASession holds a live headless Chromium process. Without this loop,
    expired sessions were only ever cleaned up on-demand at the start of the
    next start_login() call - an abandoned MFA flow (user closes the app
    mid-verification) would leak its browser process indefinitely if no one
    else happens to log in afterwards. Must run on the app's own asyncio event
    loop (not a separate thread) since Playwright objects are bound to the
    loop that created them.
    """
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await _cleanup_expired_mfa_sessions()
        except Exception as e:
            print(f"Error during periodic MFA session cleanup: {e}")


class ESEOScraper:
    """
    Handles all interactions with ESEO SharePoint and EDT API
    """
    SHAREPOINT_URL = "https://reseaueseo.sharepoint.com/sites/etu/Pages/Mon-emploi-du-temps.aspx"
    API_BASE_URL = "https://reverse-proxy.eseo.fr/API-SP/API/agenda/user"

    @staticmethod
    async def extract_eseo_id(email: str, password: str) -> Optional[str]:
        """
        Extract ESEO ID from SharePoint using Microsoft authentication (async)
        """
        p = await async_playwright().start()
        browser = await p.chromium.launch(headless=True, args=CHROMIUM_ARGS)
        page = await browser.new_page()

        try:
            await page.goto(ESEOScraper.SHAREPOINT_URL, timeout=60000)

            await page.wait_for_selector('input[type="email"]', timeout=10000)
            await page.fill('input[type="email"]', email)
            await page.click('input[type="submit"]')

            await page.wait_for_selector('input[type="password"]', timeout=10000)
            await page.fill('input[type="password"]', password)
            await page.click('input[type="submit"]')

            try:
                stay_signed_in = await page.wait_for_selector('#idSIButton9', timeout=5000)
                if stay_signed_in:
                    await stay_signed_in.click()
            except Exception:
                pass

            await page.wait_for_selector('#calendar', timeout=30000)
            id_user = await page.evaluate("() => window.idUser")

            attempts = 0
            while (id_user == "00000" or id_user is None) and attempts < 10:
                await asyncio.sleep(1)
                id_user = await page.evaluate("() => window.idUser")
                attempts += 1

            return str(id_user) if id_user and id_user != "00000" else None

        except Exception as e:
            print(f"Error extracting ESEO ID: {e}")
            return None
        finally:
            await browser.close()
            await p.stop()

    # ── MFA-aware login flow ──────────────────────────────────────────

    @staticmethod
    async def _extract_eseo_id_from_page(page) -> dict:
        """Extract eseo_id from page after successful authentication"""
        try:
            stay = await page.query_selector('#idSIButton9')
            if stay:
                await stay.click()
        except Exception:
            pass

        await page.wait_for_selector('#calendar', timeout=30000)

        id_user = await page.evaluate("() => window.idUser")
        attempts = 0
        while (id_user == "00000" or id_user is None) and attempts < 10:
            await asyncio.sleep(1)
            id_user = await page.evaluate("() => window.idUser")
            attempts += 1

        if id_user and id_user != "00000":
            result = {"success": True, "eseo_id": str(id_user)}
            result["notes"] = await ESEOScraper._capture_notes_session(page)
            return result
        return {"error": "Impossible d'extraire l'identifiant ESEO"}

    # ── Notes fetching ────────────────────────────────────────────────
    #
    # Unlike the schedule (a public API keyed by eseo_id, no auth needed),
    # the notes API requires a live Microsoft SSO session: confirmed via a
    # live capture that reverse-proxy.eseo.fr's getUE endpoint authenticates
    # via a short-lived "Authorization: Bearer <JWT>" header minted client-side
    # by MSAL.js from its localStorage token cache - NOT a static/replayable
    # cookie. Plain httpx + cookie replay does not work (reverse-proxy.eseo.fr
    # has no cookies at all in the captured session). What DOES work (verified
    # live): restoring a Playwright context from a stored storage_state
    # (cookies + localStorage, which includes MSAL's cache) lets MSAL silently
    # re-acquire a fresh access token with no re-login/MFA, as long as the
    # underlying Microsoft SSO session is still alive. So every notes fetch -
    # whether right after login or in a background job days later - goes
    # through an actual (headless) browser page load of Mes-notes.aspx.

    NOTES_URL = "https://reseaueseo.sharepoint.com/sites/etu/Pages/Mes-notes.aspx"
    NOTES_CODE_PATTERN = re.compile(r"/api/notes/getue/(\d+)/(\d+)", re.IGNORECASE)
    # Matches the sidebar's semester/year entries on Mes-notes.aspx (e.g.
    # "E3e Angers - Semestre 6 - Année 2026-2027") - confirmed live that each
    # one is its own {code} in the getUE URL, with no derivable pattern
    # between them (not sequential, not a function of the semester number),
    # so the only way to know a code is to actually click that entry once.
    SEMESTER_LABEL_LOCATOR = "text=/Semestre|Année scolaire/i"

    @staticmethod
    async def _fetch_notes_from_page(page) -> dict:
        """
        Navigates an already-authenticated page to Mes-notes.aspx and
        intercepts the getUE XHR the page's own JS fires for whichever
        semester is selected by default, returning its parsed JSON plus that
        semester's {code}. Shared by _fetch_all_semesters_from_page and
        fetch_notes_async (current-semester-only background/on-demand fetch).

        Returns:
            {"status": "ok", "notes": [...], "code": "97568"}
            {"status": "session_expired"}         - redirected to Microsoft
                login instead of loading the notes grid (stored session no
                longer valid)
            {"status": "error", "detail": "..."}  - anything else (timeout,
                network issue, page structure changed) - transient, don't
                invalidate the stored session over this alone
        """
        try:
            async with page.expect_request(
                lambda r: "/api/notes/getue/" in r.url.lower(), timeout=20000
            ) as req_info:
                await page.goto(ESEOScraper.NOTES_URL, timeout=25000)

            request = await req_info.value
            response = await request.response()

            if response is None or response.status != 200:
                status = response.status if response else None
                if "login.microsoftonline.com" in page.url or "login.live.com" in page.url:
                    return {"status": "session_expired"}
                return {"status": "error", "detail": f"HTTP status {status}"}

            notes = await response.json()
            match = ESEOScraper.NOTES_CODE_PATTERN.search(request.url)
            code = match.group(1) if match else None
            return {"status": "ok", "notes": notes, "code": code}

        except Exception as e:
            if "login.microsoftonline.com" in page.url or "login.live.com" in page.url:
                return {"status": "session_expired"}
            return {"status": "error", "detail": str(e)}

    @staticmethod
    async def _fetch_all_semesters_from_page(page) -> dict:
        """
        Like _fetch_notes_from_page, but also discovers and fetches every
        OTHER semester/year listed in the sidebar (confirmed live: reading
        the sidebar and clicking through it is the only way to learn each
        one's {code} - see SEMESTER_LABEL_LOCATOR). Used at login time only
        (see _capture_notes_session) - periodic background checks stay
        scoped to the current semester alone via fetch_notes_async, since
        historical semesters' grades don't change and don't need re-crawling
        every few hours.

        Returns:
            {"status": "ok", "semesters": [{"code", "label", "notes"}, ...]}
                - semesters[0] is always the default/current semester.
            {"status": "session_expired"} / {"status": "error", ...} - same
                as _fetch_notes_from_page, if even the default fetch fails.
        """
        default = await ESEOScraper._fetch_notes_from_page(page)
        if default["status"] != "ok":
            return default

        semesters = [{"code": default["code"], "label": None, "notes": default["notes"]}]

        try:
            candidates = await page.locator(ESEOScraper.SEMESTER_LABEL_LOCATOR).all()
            labels = []
            for el in candidates:
                try:
                    text = (await el.inner_text()).strip()
                    if text and text not in labels:
                        labels.append(text)
                except Exception:
                    pass

            for label in labels:
                try:
                    async with page.expect_request(
                        lambda r: "/api/notes/getue/" in r.url.lower(), timeout=8000
                    ) as req_info:
                        await page.get_by_text(label, exact=True).first.click(timeout=5000)
                    request = await req_info.value
                    response = await request.response()
                    if response is None or response.status != 200:
                        continue
                    match = ESEOScraper.NOTES_CODE_PATTERN.search(request.url)
                    code = match.group(1) if match else None
                    notes = await response.json()

                    if code == semesters[0]["code"]:
                        semesters[0]["label"] = label
                    else:
                        semesters.append({"code": code, "label": label, "notes": notes})
                except Exception as e:
                    print(f"Failed to fetch semester {label!r} (non-fatal): {e}")
        except Exception as e:
            print(f"Semester discovery failed (non-fatal, current semester still captured): {e}")

        return {"status": "ok", "semesters": semesters}

    @staticmethod
    async def _capture_notes_session(page) -> dict:
        """
        Called right after a successful login/MFA, while the browser is
        still open: snapshots storage_state (cookies + the MSAL token cache
        in localStorage) so notes can be fetched later without another
        login/MFA.

        Deliberately does NOTHING else - no navigation, no notes fetch, no
        semester crawl. All of that runs afterwards in a background task
        (see scheduler.populate_notes_after_login), because it took well over
        a minute and the login request would time out client-side long before
        finishing. Never raises: a failure here just means notes stay
        unavailable until the next login.

        Returns {"status": "ok", "session_state": "..."} or
        {"status": "error", "detail": "..."}.
        """
        try:
            return {"status": "ok", "session_state": json.dumps(await page.context.storage_state())}
        except Exception as e:
            print(f"Failed to capture notes session state: {e}")
            return {"status": "error", "detail": str(e)}

    @staticmethod
    async def fetch_all_semesters_async(session_state_json: str) -> dict:
        """
        Full multi-semester notes crawl using a previously stored session -
        the background counterpart to _fetch_all_semesters_from_page, run
        after login has already responded (see
        scheduler.populate_notes_after_login) rather than inside it.

        Same return shape as _fetch_all_semesters_from_page, plus a refreshed
        "session_state" on success.
        """
        try:
            storage_state = json.loads(session_state_json)
        except Exception as e:
            return {"status": "error", "detail": f"corrupt session_state: {e}"}

        p = await async_playwright().start()
        browser = await p.chromium.launch(headless=True, args=CHROMIUM_ARGS)
        try:
            context = await browser.new_context(storage_state=storage_state)
            page = await context.new_page()
            result = await ESEOScraper._fetch_all_semesters_from_page(page)
            if result["status"] == "ok":
                try:
                    result["session_state"] = json.dumps(await context.storage_state())
                except Exception as e:
                    print(f"Failed to refresh notes session state: {e}")
            return result
        finally:
            await browser.close()
            await p.stop()

    @staticmethod
    async def fetch_notes_async(session_state_json: str) -> dict:
        """
        Background/on-demand notes fetch using a previously stored session -
        no email/password/MFA needed, as long as the underlying Microsoft
        SSO session is still alive. Launches its own throwaway headless
        browser (seeded with the stored storage_state) since no live login
        page exists at this point. On success, also returns a refreshed
        session_state (MSAL may rotate tokens/cookies on use) so the caller
        can extend the stored session's effective lifetime.
        """
        try:
            storage_state = json.loads(session_state_json)
        except Exception as e:
            return {"status": "error", "detail": f"corrupt session_state: {e}"}

        p = await async_playwright().start()
        browser = await p.chromium.launch(headless=True, args=CHROMIUM_ARGS)
        try:
            context = await browser.new_context(storage_state=storage_state)
            page = await context.new_page()
            result = await ESEOScraper._fetch_notes_from_page(page)
            if result["status"] == "ok":
                try:
                    result["session_state"] = json.dumps(await context.storage_state())
                except Exception as e:
                    print(f"Failed to refresh notes session state: {e}")
            return result
        finally:
            await browser.close()
            await p.stop()

    @staticmethod
    async def fetch_bulletin_async(session_state_json: str, semester_label: Optional[str] = None) -> dict:
        """
        Fetches the "Bulletin provisoire" (a print-oriented HTML document -
        see utils.parse_bulletin_html for turning it into structured data)
        for one semester, using a stored session. On-demand only (not
        crawled for every semester at login like notes are - a bulletin
        fetch is a full page load + two more clicks, and is only needed when
        the user actually opens the bulletin screen).

        If semester_label is given and isn't the default/current semester,
        clicks that semester's sidebar entry first (same reasoning as
        _fetch_all_semesters_from_page: there's no URL to construct
        directly, only a label to click).

        Returns:
            {"status": "ok", "html": "<standalone HTML document>"}
            {"status": "session_expired"} / {"status": "error", "detail": ...}
        """
        try:
            storage_state = json.loads(session_state_json)
        except Exception as e:
            return {"status": "error", "detail": f"corrupt session_state: {e}"}

        p = await async_playwright().start()
        browser = await p.chromium.launch(headless=True, args=CHROMIUM_ARGS)
        page = None
        try:
            context = await browser.new_context(storage_state=storage_state)
            page = await context.new_page()

            async with page.expect_request(
                lambda r: "/api/notes/getue/" in r.url.lower(), timeout=20000
            ):
                await page.goto(ESEOScraper.NOTES_URL, timeout=25000)

            if semester_label:
                try:
                    async with page.expect_request(
                        lambda r: "/api/notes/getue/" in r.url.lower(), timeout=8000
                    ):
                        await page.get_by_text(semester_label, exact=True).first.click(timeout=5000)
                except Exception as e:
                    print(f"Could not switch to semester {semester_label!r} for bulletin "
                          f"(non-fatal, falling back to the default semester): {e}")

            async with page.expect_request(
                lambda r: "/api/bulletin/getbulletinbyetu/" in r.url.lower(), timeout=10000
            ) as req_info:
                await page.get_by_text("Bulletin provisoire", exact=False).first.click(timeout=5000)

            request = await req_info.value
            response = await request.response()

            if response is None or response.status != 200:
                status = response.status if response else None
                if "login.microsoftonline.com" in page.url or "login.live.com" in page.url:
                    return {"status": "session_expired"}
                return {"status": "error", "detail": f"HTTP status {status}"}

            body = await response.json()
            html = body.get("Html") or body.get("Header") or ""
            return {"status": "ok", "html": html}

        except Exception as e:
            if page is not None and ("login.microsoftonline.com" in page.url or "login.live.com" in page.url):
                return {"status": "session_expired"}
            return {"status": "error", "detail": str(e)}
        finally:
            await browser.close()
            await p.stop()

    @staticmethod
    async def start_login(email: str, password: str) -> dict:
        """
        Start login flow with MFA detection (fully async).

        Returns:
            - {"success": True, "eseo_id": "..."} if no MFA required
            - {"mfa_required": True, "session_id": "...", "mfa_type": "totp"} if TOTP MFA
            - {"mfa_required": True, "session_id": "...", "mfa_type": "push", "mfa_data": "42"} if push MFA
            - {"error": "..."} on failure
        """
        await _cleanup_expired_mfa_sessions()

        p = await async_playwright().start()
        browser = await p.chromium.launch(headless=True, args=CHROMIUM_ARGS)
        page = await browser.new_page()
        mfa_session_created = False

        try:
            await page.goto(ESEOScraper.SHAREPOINT_URL, timeout=60000)

            # Email entry
            await page.wait_for_selector('input[type="email"]', timeout=10000)
            await page.fill('input[type="email"]', email)
            await page.click('input[type="submit"]')

            # Password entry
            await page.wait_for_selector('input[type="password"]', timeout=10000)
            await page.fill('input[type="password"]', password)
            await page.click('input[type="submit"]')

            # Wait for next step: MFA, Stay signed in, or Calendar
            try:
                await page.wait_for_selector(
                    '#idTxtBx_SAOTCC_OTC, #idRichContext_DisplaySign, #idSIButton9, #calendar',
                    timeout=15000
                )
            except Exception:
                raise Exception("Identifiants invalides ou erreur d'authentification")

            # Check for TOTP MFA
            if await page.query_selector('#idTxtBx_SAOTCC_OTC'):
                session_id = str(uuid.uuid4())
                _mfa_sessions[session_id] = MFASession(p, browser, page, mfa_type="totp")
                mfa_session_created = True
                print(f"MFA TOTP required, session: {session_id[:8]}...")
                return {"mfa_required": True, "session_id": session_id, "mfa_type": "totp"}

            # Check for push notification MFA (number matching)
            number_el = await page.query_selector('#idRichContext_DisplaySign')
            if number_el:
                number = await number_el.text_content()
                session_id = str(uuid.uuid4())
                _mfa_sessions[session_id] = MFASession(p, browser, page, mfa_type="push")
                mfa_session_created = True
                print(f"MFA push required, session: {session_id[:8]}...")
                return {
                    "mfa_required": True,
                    "session_id": session_id,
                    "mfa_type": "push",
                    "mfa_data": number.strip() if number else None
                }

            # No MFA - extract eseo_id directly
            return await ESEOScraper._extract_eseo_id_from_page(page)

        except Exception as e:
            print(f"Error during login: {e}")
            return {"error": str(e)}
        finally:
            if not mfa_session_created:
                try:
                    await browser.close()
                    await p.stop()
                except Exception:
                    pass

    @staticmethod
    async def complete_mfa(session_id: str, totp_code: str = None) -> dict:
        """
        Complete MFA verification (fully async).

        For TOTP: fills in the 6-digit code
        For push: waits for user approval on their phone

        Returns:
            - {"success": True, "eseo_id": "..."} on success
            - {"error": "...", "retry": True, "session_id": "..."} if wrong code (can retry)
            - {"error": "..."} on failure
        """
        session = _mfa_sessions.get(session_id)

        if not session:
            return {"error": "Session MFA introuvable ou expiree"}

        if session.is_expired():
            _mfa_sessions.pop(session_id, None)
            await session.close()
            return {"error": "Session MFA expiree, veuillez vous reconnecter"}

        page = session.page

        try:
            if session.mfa_type == "totp":
                if not totp_code:
                    return {"error": "Code TOTP requis"}

                await page.fill('#idTxtBx_SAOTCC_OTC', totp_code)
                await page.click('#idSubmit_SAOTCC_Continue')

                try:
                    await page.wait_for_selector(
                        '#idSIButton9, #calendar, #idSpan_SAOTCC_Error',
                        timeout=10000
                    )
                except Exception:
                    pass

                error_el = await page.query_selector('#idSpan_SAOTCC_Error')
                if error_el and await error_el.is_visible():
                    print(f"Wrong TOTP code for session {session_id[:8]}...")
                    return {"error": "Code TOTP invalide", "retry": True, "session_id": session_id}

            elif session.mfa_type == "push":
                try:
                    await page.wait_for_selector('#idSIButton9, #calendar', timeout=60000)
                except Exception:
                    _mfa_sessions.pop(session_id, None)
                    await session.close()
                    return {"error": "Approbation MFA non recue (timeout)"}

            # MFA successful - extract eseo_id
            result = await ESEOScraper._extract_eseo_id_from_page(page)

            _mfa_sessions.pop(session_id, None)
            await session.close()

            return result

        except Exception as e:
            print(f"Error completing MFA: {e}")
            _mfa_sessions.pop(session_id, None)
            await session.close()
            return {"error": f"Erreur MFA: {e}"}

    # ── Schedule fetching ─────────────────────────────────────────────

    @staticmethod
    def fetch_schedule(eseo_id: str, weeks: int = 4) -> Optional[Dict]:
        """
        Fetch schedule from ESEO API for given user ID
        Does NOT require authentication - uses public API endpoint

        Args:
            eseo_id: The user's ESEO ID
            weeks: Number of weeks to fetch from Monday of current week (default 4)

        Returns:
            Dictionary with filtered schedule data and metadata, None if failed
            Returns empty schedule if API returns 404 or empty data

        Format returned:
        {
            "eseo_id": "54024",
            "schedule": [...],  # Filtered events with only required fields
            "hash": "abc123...",  # MD5 hash for change detection
            "fetched_at": "2026-02-04T21:30:00"
        }
        """
        try:
            # Calculate date range using new utility function
            start_date, end_date = get_date_range(weeks)

            # Build API URL
            api_url = f"{ESEOScraper.API_BASE_URL}/{start_date}/{end_date}/{eseo_id}"

            # Fetch schedule
            response = requests.get(api_url, timeout=10)

            # Handle 404 or empty responses gracefully
            if response.status_code == 404:
                return {
                    "eseo_id": eseo_id,
                    "schedule": [],  # Empty schedule, not an error
                    "hash": hashlib.md5(b"[]").hexdigest(),
                    "fetched_at": datetime.now(timezone.utc).isoformat()
                }

            response.raise_for_status()
            schedule_data = response.json()

            # Handle empty array response
            if not schedule_data or len(schedule_data) == 0:
                return {
                    "eseo_id": eseo_id,
                    "schedule": [],
                    "hash": hashlib.md5(b"[]").hexdigest(),
                    "fetched_at": datetime.now(timezone.utc).isoformat()
                }

            # Filter events to keep only required fields
            filtered_events = [filter_event_fields(event) for event in schedule_data]

            # Calculate hash for change detection (using filtered data)
            schedule_json = json.dumps(filtered_events, sort_keys=True)
            schedule_hash = hashlib.md5(schedule_json.encode()).hexdigest()

            return {
                "eseo_id": eseo_id,
                "schedule": filtered_events,
                "hash": schedule_hash,
                "fetched_at": datetime.now(timezone.utc).isoformat()
            }

        except requests.RequestException as e:
            print(f"Error fetching schedule for {eseo_id}: {e}")
            return None
        except Exception as e:
            print(f"Unexpected error fetching schedule: {e}")
            return None

    @staticmethod
    def compare_schedules(old_hash: Optional[str], new_hash: str) -> bool:
        """
        Compare two schedule hashes to detect changes

        Args:
            old_hash: Previous schedule hash (can be None for new users)
            new_hash: Current schedule hash

        Returns:
            True if schedules are different (notification needed), False otherwise
        """
        if old_hash is None:
            return False  # First sync, don't notify

        return old_hash != new_hash

    @staticmethod
    async def fetch_schedule_async(eseo_id: str, start_date: str, end_date: str) -> Optional[List[Dict]]:
        """
        Fetch schedule from ESEO API asynchronously using httpx

        Args:
            eseo_id: User's ESEO ID
            start_date: Start date in format "YYYY-MM-DD"
            end_date: End date in format "YYYY-MM-DD"

        Returns:
            List of raw event dictionaries from API, None if failed
            Returns empty list if API returns 404 or empty data

        Note:
            This is the async version for use with the new /agenda endpoint
            and async scheduler
        """
        try:
            # Convert YYYY-MM-DD to YYYYMMDDTHHmmss for API
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")

            start_str = start_dt.strftime("%Y%m%dT060000")  # 6am start
            end_str = end_dt.strftime("%Y%m%dT210000")      # 9pm end

            api_url = f"{ESEOScraper.API_BASE_URL}/{start_str}/{end_str}/{eseo_id}"

            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(api_url)

                # Handle 404 or empty responses gracefully
                if response.status_code == 404:
                    return []

                response.raise_for_status()
                schedule_data = response.json()

                return schedule_data if schedule_data else []

        except httpx.HTTPStatusError as e:
            print(f"HTTP error fetching schedule for {eseo_id}: {e}")
            return None
        except httpx.RequestError as e:
            print(f"Request error fetching schedule: {e}")
            return None
        except Exception as e:
            print(f"Unexpected error fetching schedule: {e}")
            return None

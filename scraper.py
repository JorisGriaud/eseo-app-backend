"""
SharePoint scraper and EDT fetcher using Playwright
Handles authentication and data extraction from ESEO systems
"""
import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List
from playwright.sync_api import sync_playwright, Page
import requests
import httpx

from utils import get_date_range, filter_event_fields


class ESEOScraper:
    """
    Handles all interactions with ESEO SharePoint and EDT API
    """
    SHAREPOINT_URL = "https://reseaueseo.sharepoint.com/sites/etu/Pages/Mon-emploi-du-temps.aspx"
    API_BASE_URL = "https://reverse-proxy.eseo.fr/API-SP/API/agenda/user"

    @staticmethod
    def extract_eseo_id_sync(email: str, password: str) -> Optional[str]:
        """
        Extract ESEO ID from SharePoint using Microsoft authentication
        Synchronous version for use in FastAPI

        Args:
            email: Microsoft/ESEO email
            password: Microsoft/ESEO password

        Returns:
            eseo_id as string if successful, None if failed

        Note: This opens a headless browser, authenticates, and extracts
              the idUser variable from the page JavaScript context
        """
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()

            try:
                # Navigate to SharePoint EDT page
                page.goto(ESEOScraper.SHAREPOINT_URL, timeout=60000)

                # Microsoft authentication flow
                page.wait_for_selector('input[type="email"]', timeout=10000)
                page.fill('input[type="email"]', email)
                page.click('input[type="submit"]')

                # Password entry
                page.wait_for_selector('input[type="password"]', timeout=10000)
                page.fill('input[type="password"]', password)
                page.click('input[type="submit"]')

                # Handle "Stay signed in?" prompt (if appears)
                try:
                    stay_signed_in = page.wait_for_selector('#idSIButton9', timeout=5000)
                    if stay_signed_in:
                        stay_signed_in.click()
                except:
                    pass  # Prompt didn't appear, continue

                # Wait for calendar to load
                page.wait_for_selector('#calendar', timeout=30000)

                # Extract idUser from JavaScript context
                id_user = page.evaluate("() => window.idUser")

                # Wait for idUser to be properly initialized (not default value)
                attempts = 0
                while (id_user == "00000" or id_user is None) and attempts < 10:
                    import time
                    time.sleep(1)
                    id_user = page.evaluate("() => window.idUser")
                    attempts += 1

                if id_user and id_user != "00000":
                    return str(id_user)
                else:
                    return None

            except Exception as e:
                print(f"Error extracting ESEO ID: {e}")
                return None
            finally:
                browser.close()

    @staticmethod
    async def extract_eseo_id(email: str, password: str) -> Optional[str]:
        """
        Async wrapper for extract_eseo_id_sync
        For use in async FastAPI endpoints
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, ESEOScraper.extract_eseo_id_sync, email, password)

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

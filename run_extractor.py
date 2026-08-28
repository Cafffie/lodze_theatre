"""Łódź Theatre (biletyna.pl) extractor implementation using the framework."""
import json
import sys
from datetime import datetime
from urllib.parse import parse_qs, urljoin, urlparse

import pandas as pd
from seleniumbase import SB

from scrapers.lodztheatre.lodztheatre_config import (
    BASE_URL,
    COOKIE_ACCEPT_XPATH,
    DEFAULT_CITY,
    DEFAULT_COUNTRY,
    DEFAULT_CURRENCY,
    LISTING_URL,
    MAX_SCROLL_ATTEMPTS,
    REQUEST_DELAY,
    SCROLL_STABLE_ROUNDS,
)
from utils.base_extractor import BaseExtractor
from utils.logger import setup_logger
from utils.scraping_helpers import (
    accept_cookies,
    get_scrape_datetime,
    human_scroll,
    normalize_country,
)

logger = setup_logger(__name__, log_to_file=False)

# biletyna.pl fronts every page — including the plain listing — with a
# Cloudflare Turnstile "Just a moment..." interstitial. It reliably clears
# once per browser session by dropping any stale cookies and clicking the
# widget through SeleniumBase's UC-mode GUI helper, but that click is a real
# OS-level PyAutoGUI action: it only works in a headed session, never
# headless. Every page this extractor visits (listing, event, sector) needs
# the same treatment, since each is a fresh Cloudflare-eligible route.
#
# This extractor is biletyna.pl's Łódź city listing — same platform as
# slupsktheatre, filtered to a different city_id. Keep the two in sync:
# fixes to the Cloudflare/seat-map handling here almost certainly apply
# there too (and vice versa).
STATUS_AVAILABLE = "10"


class LodzTheatreExtractor(BaseExtractor):
    """Extractor for Łódź theatre listings on biletyna.pl."""

    def __init__(self, **kwargs):
        """Initialize the Łódź Theatre extractor with default settings."""
        super().__init__(
            site_id="lodztheatre",
            **kwargs,
        )
        self.sb = None

    def _open_biletyna_page(self, sb, url, label):
        """Load a biletyna.pl URL, solving Cloudflare + cookie consent.

        Clears cookies and reopens with UC mode's reconnect helper, then —
        if the Turnstile "Just a moment..." interstitial is still showing —
        clears cookies again and clicks it through via ``uc_gui_click_cf``.
        Finishes by accepting the Cookiebot banner (it re-appears on some
        navigations, not just the first) and doing a human-like scroll.

        Returns True once the page looks clear of both, False if the
        challenge is still showing afterwards (callers should treat that
        performance/sector as unscraped rather than trust the DOM).
        """
        try:
            sb.driver.delete_all_cookies()
        except Exception:
            pass

        try:
            sb.uc_open_with_reconnect(url, reconnect_time=6)
        except Exception as e:
            self.custom_logger.warning(f"Failed to open {label} ({url}): {e}")
            return False

        if "just a moment" in sb.get_page_source().lower():
            self.custom_logger.info(
                f"Cloudflare challenge on {label} — clearing cookies and solving"
            )
            try:
                sb.driver.delete_all_cookies()
            except Exception:
                pass
            sb.uc_gui_click_cf()
            sb.sleep(4)

        accept_cookies(
            sb.driver,
            COOKIE_ACCEPT_XPATH,
            once_per_domain=False,
            logger=self.custom_logger,
        )
        human_scroll(sb)
        sb.sleep(REQUEST_DELAY)

        return "just a moment" not in sb.get_page_source().lower()

    @staticmethod
    def _load_item_list(script_text):
        """Parse a <script type="application/ld+json"> body.

        Returns the schema.org ItemList payload embedded in the listing page,
        or None if this particular script tag holds something else (the page
        also ships an unrelated FAQPage block).
        """
        if not script_text:
            return None
        try:
            payload = json.loads(script_text)
        except (TypeError, ValueError):
            return None
        if isinstance(payload, dict) and payload.get("@type") == "ItemList":
            return payload
        return None

    def _collect_performance_events(self, sb):
        """Load the listing page and collect every TheaterEvent entry.

        The page renders one schema.org ItemList of TheaterEvent items per
        performance instance, then lazy-loads more as the visitor scrolls.
        Scrolling stops once the collected performance count stops growing
        for SCROLL_STABLE_ROUNDS consecutive rounds.
        """
        if not self._open_biletyna_page(sb, LISTING_URL, "listing page"):
            self.custom_logger.warning(
                "Cloudflare challenge on listing page never cleared"
            )

        performance_events_by_key = {}
        previous_event_count = 0
        stable_rounds = 0
        scroll_attempt = 0

        while (
            stable_rounds < SCROLL_STABLE_ROUNDS
            and scroll_attempt <= MAX_SCROLL_ATTEMPTS
        ):
            soup = sb.get_beautiful_soup()
            for script_tag in soup.find_all("script", type="application/ld+json"):
                item_list = self._load_item_list(script_tag.get_text())
                if item_list is None:
                    continue
                for list_entry in item_list.get("itemListElement", []):
                    theater_event = list_entry.get("item", {})
                    event_url = theater_event.get("url")
                    start_date = theater_event.get("startDate")
                    if not event_url or not start_date:
                        continue
                    performance_events_by_key[(event_url, start_date)] = theater_event

            if len(performance_events_by_key) == previous_event_count:
                stable_rounds += 1
            else:
                stable_rounds = 0
            previous_event_count = len(performance_events_by_key)

            human_scroll(sb)
            sb.sleep(REQUEST_DELAY)
            scroll_attempt += 1

        return list(performance_events_by_key.values())

    @staticmethod
    def _clean_venue_url(raw_url):
        """Strip the per-performance tracking query string from a show URL."""
        if not raw_url:
            return raw_url
        return raw_url.split("?")[0]

    @staticmethod
    def _extract_event_id(raw_url):
        """Pull the per-performance ``eid`` query param biletyna.pl tags
        each TheaterEvent's ``url`` with — it's the id the purchase flow
        (``/event/view/id/{eid}``) needs, and the only place it appears.
        """
        if not raw_url:
            return None
        query = urlparse(raw_url).query
        values = parse_qs(query).get("eid")
        return values[0] if values else None

    @staticmethod
    def _parse_iso_datetime(raw_value):
        """Parse a schema.org ISO-8601 datetime string, or return None."""
        if not raw_value:
            return None
        try:
            return datetime.fromisoformat(raw_value)
        except ValueError:
            return None

    def _new_show_record(self, theater_event):
        """Build the initial per-show accumulator from its first performance."""
        location = theater_event.get("location", {}) or {}
        address_info = location.get("address", {}) or {}
        street_address = address_info.get("streetAddress")
        postal_code = address_info.get("postalCode")
        locality = address_info.get("addressLocality") or DEFAULT_CITY
        address_line = ", ".join(
            part
            for part in (street_address, f"{postal_code} {locality}".strip())
            if part
        )

        offers = theater_event.get("offers", {}) or {}

        return {
            "title": theater_event.get("name"),
            "venue_url": self._clean_venue_url(theater_event.get("url")),
            "venue": location.get("name"),
            "address": address_line or None,
            "city": locality,
            "country": normalize_country(address_info.get("addressCountry"))
            or DEFAULT_COUNTRY,
            "currency": offers.get("priceCurrency") or DEFAULT_CURRENCY,
            "upcoming_performances": [],
            "price": None,
            "real_capacities": [],
            "event_ids_by_performance": {},
        }

    def _add_performance(self, show_record, theater_event):
        """Fold one TheaterEvent performance instance into a show record."""
        performance_datetime = self._parse_iso_datetime(theater_event.get("startDate"))
        if performance_datetime is None:
            return

        performance_date = performance_datetime.strftime("%Y-%m-%d")
        performance_time = performance_datetime.strftime("%H:%M")
        performance_entry = {"date": performance_date, "time": performance_time}
        if performance_entry not in show_record["upcoming_performances"]:
            show_record["upcoming_performances"].append(performance_entry)

        # offers.lowPrice is only the aggregate "starting from" price. It's
        # kept as a General Admission fallback for performances whose real
        # seat map can't be reached (Cloudflare didn't clear, sector page
        # changed shape, etc.) — see _collect_seat_maps / _finalize_show_record.
        offers = theater_event.get("offers", {}) or {}
        low_price = offers.get("lowPrice")
        if isinstance(low_price, (int, float)):
            show_record["price"] = float(low_price)

        # theater_event.get("remainingAttendeeCapacity") is deliberately NOT
        # used for capacity — see _finalize_show_record's own comment on why
        # ("seats left" for this specific show, not the venue's real total,
        # and biletyna.pl's JSON-LD has no separate true-total field to fall
        # back to instead).

        event_id = self._extract_event_id(theater_event.get("url"))
        if event_id:
            performance_key = f"{performance_date} {performance_time}"
            show_record["event_ids_by_performance"][performance_key] = event_id

    def _find_sector_urls(self, sb, event_url):
        """From an event page, find every sector's numbered-seat-grid URL.

        Most venues list one or more sectors to choose from
        (``/event/sector/id/{id}`` links); a single-sector venue sometimes
        skips straight to the seat grid itself, so the event page's own URL
        is checked too.
        """
        current_url = sb.get_current_url()
        if "/event/sector/id/" in current_url:
            return {current_url}

        soup = sb.get_beautiful_soup()
        return {
            urljoin(BASE_URL, a["href"])
            for a in soup.select('a[href^="/event/sector/id/"]')
            if a.get("href")
        }

    @staticmethod
    def _sector_id_from_url(sector_url):
        """Pull the numeric sector id off a ``/event/sector/id/{id}`` URL."""
        return sector_url.rstrip("/").rsplit("/", 1)[-1]

    def _parse_sector_seats(self, sb, sector_prefix=None):
        """Parse the numbered seat grid on a ``/event/sector/id/...`` page.

        Each real seat is a ``div.place`` with ``data-row_number`` +
        ``data-place_number`` identifying it and ``data-place_status``
        marking availability ("10" == on sale — see STATUS_AVAILABLE).
        Structural gaps in the grid (aisles) are ``div.place`` too but carry
        neither attribute, so the selector requires both.

        Row+number is only unique *within* a sector — a multi-sector venue
        (e.g. a hall with separate "parter"/"balkon" blocks) can have the
        same row/seat number in two sectors, which collided into duplicate
        seat ids before disambiguation. Every sector page carries a
        human-readable label in its ``<div class="gnp_sector"
        data-sector_name="...">`` wrapper (e.g. "balkon" — confirmed live:
        sector id 1598250 is literally Balkon) — confirmed live the same
        way częstochowatheatre's own _parse_sector_seats already reads it,
        this scraper's own sibling on the same biletyna.pl platform. Using
        that instead of the bare numeric sector id (the previous behavior
        here — seats like "1598250:II-1") is what makes seat ids
        human-readable ("Balkon II-1") rather than exposing an opaque
        internal id that means nothing outside biletyna.pl's own database.
        Falls back to the raw numeric id only on the rare page that ships
        no data-sector_name at all, so seats never silently lose their
        disambiguating prefix. Row and number are joined with "-" rather
        than concatenated directly: some venues use numeric row numbers,
        where row "1" + number "28" and row "12" + number "8" would
        otherwise both stringify to the same "128".

        Returns (seats, total_seat_count) — total_seat_count is every real
        seat div in this sector regardless of data-place_status, i.e.
        available + unavailable. That's what "capacity" means; counting only
        the ones that pass the availability filter below is how capacity
        was wrongly ending up pinned near "how many are left" (e.g. a
        near-sold-out show reporting capacity 14 for a venue that seats
        hundreds) — same bug already fixed in częstochowatheatre.
        """
        soup = sb.get_beautiful_soup()

        sector_label = None
        sector_container = soup.select_one("div.gnp_sector[data-sector_name]")
        if sector_container:
            raw_name = (sector_container.get("data-sector_name") or "").strip()
            if raw_name:
                sector_label = raw_name.title()
        if not sector_label and sector_prefix:
            sector_label = sector_prefix

        seats = []
        total_seat_count = 0
        for place in soup.select("div.place[data-row_number][data-place_number]"):
            total_seat_count += 1
            if place.get("data-place_status") != STATUS_AVAILABLE:
                continue
            price = place.get("data-place_price")
            row = place.get("data-row_number")
            number = place.get("data-place_number")
            if not price or not row or not number:
                continue
            seat_id = f"{row}-{number}"
            if sector_label:
                seat_id = f"{sector_label} {seat_id}"
            seats.append({"seat": seat_id, "ticket_price": float(price)})
        return seats, total_seat_count

    def _scrape_seat_map(self, sb, event_id):
        """Fetch the real on-sale seat inventory for one performance instance.

        Walks the purchase flow biletyna.pl requires to reach individual
        seats: the event page (which lists sector(s)) then each sector's
        numbered seat grid. Returns (None, None) if the event page itself
        couldn't be reached, so callers can fall back to the General
        Admission price instead of recording "zero seats on sale".

        Returns (seats, capacity) — capacity is summed across every sector
        (available + unavailable in each), the venue's real total for this
        performance, not just what's currently on sale.
        """
        event_url = f"{BASE_URL}/event/view/id/{event_id}"
        if not self._open_biletyna_page(sb, event_url, f"event {event_id}"):
            return None, None

        sector_urls = self._find_sector_urls(sb, event_url)
        if not sector_urls:
            self.custom_logger.warning(f"No sector links found for event {event_id}")
            return None, None

        # Always pass the numeric sector id through as a fallback prefix —
        # _parse_sector_seats now prefers the page's own human-readable
        # data-sector_name ("Balkon") and only falls back to this opaque id
        # on the rare page that ships no sector name at all, so a
        # single-sector venue gets a real "Parter 1-1" label too instead of
        # a bare "1-1" (still unambiguous either way, but the label reads
        # the same whether or not the venue happens to have more than one
        # sector — no reason to withhold it just because there's nothing to
        # disambiguate against here).
        seats = []
        capacity = 0
        for sector_url in sector_urls:
            if sector_url != sb.get_current_url():
                if not self._open_biletyna_page(sb, sector_url, f"sector {sector_url}"):
                    continue
            sector_prefix = self._sector_id_from_url(sector_url)
            sector_seats, sector_capacity = self._parse_sector_seats(sb, sector_prefix)
            seats.extend(sector_seats)
            capacity += sector_capacity
        return seats, capacity

    def _collect_seat_maps(self, sb, show_record):
        """Fetch the real seat map for every performance of one show,
        storing results on the accumulator for _finalize_show_record to
        prefer over the General Admission fallback.
        """
        seat_pricing_by_performance = {}
        for performance_key, event_id in show_record[
            "event_ids_by_performance"
        ].items():
            seats, capacity = self._scrape_seat_map(sb, event_id)
            if capacity:
                # available + unavailable seats actually counted off the
                # real seat grid — this is what "capacity" means. Recorded
                # even when this performance turned out sold out (seats
                # empty, capacity > 0), since the venue's seat count is
                # unaffected by which of them happen to be free right now.
                show_record["real_capacities"].append(capacity)
            if seats:
                seat_pricing_by_performance[performance_key] = seats
            else:
                self.custom_logger.warning(
                    f"No real seat map for {show_record['title']!r} @ "
                    f"{performance_key} (event {event_id}) — "
                    "falling back to General Admission"
                )
        show_record["seat_pricing_by_performance"] = seat_pricing_by_performance

    def _finalize_show_record(self, show_record):
        """Turn a per-show accumulator into the final output row."""
        upcoming_performances = sorted(
            show_record["upcoming_performances"],
            key=lambda entry: (entry["date"], entry["time"]),
        )
        performance_dates = [entry["date"] for entry in upcoming_performances]
        open_date = min(performance_dates) if performance_dates else None
        close_date = max(performance_dates) if performance_dates else None
        # Capacity comes only from the real seat grid (available +
        # unavailable, scraped directly off each sector's seat divs) — never
        # from remaining_capacities. That's schema.org's
        # remainingAttendeeCapacity, which is only how many seats are LEFT,
        # not the venue's actual total, and biletyna.pl's JSON-LD has no
        # true-total field at all (confirmed live: no maximumAttendeeCapacity
        # anywhere in the payload) to fall back to instead. A General
        # Admission show (no seat grid exists to scrape at all — e.g. Teatr
        # Komedii Impro) previously reported remainingAttendeeCapacity AS
        # capacity, which reads as "this venue only seats 14" when it
        # actually means "14 GA tickets left for this specific show". None
        # here is the honest answer — matches the same "capacity only when
        # actually known" convention this project's other scrapers already
        # follow (e.g. olathetheatre, landmark_theatres) rather than
        # reporting a number that looks like a fact but isn't one.
        capacity = max(show_record["real_capacities"]) if show_record["real_capacities"] else None
        price = show_record["price"]
        seat_pricing_by_performance = show_record.get("seat_pricing_by_performance", {})

        seat_pricing = {}
        for entry in upcoming_performances:
            performance_key = f"{entry['date']} {entry['time']}"
            real_seats = seat_pricing_by_performance.get(performance_key)
            if real_seats:
                seat_pricing[performance_key] = real_seats
            elif price is not None:
                seat_pricing[performance_key] = [
                    {"seat": "General Admission", "ticket_price": price}
                ]

        return {
            "title": show_record["title"],
            "venue_url": show_record["venue_url"],
            "category": None,
            "venue": show_record["venue"],
            "address": show_record["address"],
            "city": show_record["city"],
            "country": show_record["country"],
            "open_date": open_date,
            "close_date": close_date,
            "booking_start_date": None,
            "booking_end_date": close_date,
            "upcoming_performances": upcoming_performances,
            "capacity": capacity,
            "currency": show_record["currency"],
            "is_limited_run": bool(open_date and close_date),
            "seat_pricing": seat_pricing,
            "scrape_datetime": get_scrape_datetime(),
        }

    def extract(self) -> bytes:
        """Extract raw data from the Łódź theatre listing on biletyna.pl."""
        self.custom_logger.info(f"Starting extraction from {LISTING_URL}")

        # headless=False + uc=True is required, not a preference: the
        # Cloudflare Turnstile widget only ever resolves through
        # uc_gui_click_cf()'s real OS-level GUI click. ad_block=False +
        # cft=True are the existing Windows-Chrome-launch fixes.
        with SB(headless=False, uc=True, ad_block=False, cft=True) as sb:
            try:
                sb.driver.maximize_window()
            except Exception:
                pass

            theater_events = self._collect_performance_events(sb)
            self.custom_logger.info(
                f"Found {len(theater_events)} performance instance(s)"
            )

            shows_by_venue_url = {}
            for theater_event in theater_events:
                title = theater_event.get("name")
                venue_url = self._clean_venue_url(theater_event.get("url"))
                if not title or not venue_url:
                    continue

                if venue_url not in shows_by_venue_url:
                    if self.local_test and len(shows_by_venue_url) >= self.show_count:
                        continue
                    shows_by_venue_url[venue_url] = self._new_show_record(theater_event)

                self._add_performance(shows_by_venue_url[venue_url], theater_event)

            if self.local_test:
                self.custom_logger.info(
                    f"Local test mode — limited to {self.show_count} show(s)"
                )

            for show_record in shows_by_venue_url.values():
                if not show_record["upcoming_performances"]:
                    continue
                self._collect_seat_maps(sb, show_record)

        all_events = []
        for show_record in shows_by_venue_url.values():
            if not show_record["upcoming_performances"]:
                continue
            event_metadata = self._finalize_show_record(show_record)
            all_events.append(event_metadata)
            self.log_record(event_metadata)

        combined_data = json.dumps(all_events, indent=2)
        self.custom_logger.info(f"Extraction completed. Total shows: {len(all_events)}")
        return combined_data.encode("utf-8")

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        if "capacity" in df.columns:
            df["capacity"] = pd.to_numeric(df["capacity"], errors="coerce").astype(
                "Int64"
            )
        return df

    def _parse(self, raw: bytes) -> pd.DataFrame:
        """Parse JSON content to DataFrame."""
        self.custom_logger.info("Starting data parsing")
        data = json.loads(raw.decode("utf-8"))
        df = pd.DataFrame(data)

        self.custom_logger.info(f"Parsing completed. Extracted {len(df)} events")
        return df


def main():
    """Example usage of the Łódź Theatre extractor."""
    extractor = LodzTheatreExtractor(
        save_csv_locally=False, csv_incremental_mode=False
    )
    result = extractor.run()
    logger.info(f"Extraction result: {result}")
    if result.get("status") != "success":
        sys.exit(1)


if __name__ == "__main__":
    main()

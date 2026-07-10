"""Expose crawler for Kleinanzeigen"""
import re
import datetime
import hashlib
from urllib.parse import urlsplit, urlparse, parse_qs, unquote

import requests

from bs4 import Tag

from flathunter.webdriver_crawler import WebdriverCrawler
from flathunter.logging import logger

class Kleinanzeigen(WebdriverCrawler):
    """Implementation of Crawler interface for Kleinanzeigen"""

    URL_PATTERN = re.compile(r'https://www\.kleinanzeigen\.de')
    API_CONNECT_TIMEOUT = 10
    API_READ_TIMEOUT = 20
    API_HEADERS = {
        "accept": "application/json",
        "Content-Type": "application/json",
    }
    MONTHS = {
        "Januar": "01",
        "Februar": "02",
        "März": "03",
        "April": "04",
        "Mai": "05",
        "Juni": "06",
        "Juli": "07",
        "August": "08",
        "September": "09",
        "Oktober": "10",
        "November": "11",
        "Dezember": "12"
    }

    def uses_api(self) -> bool:
        """Return true if the hosted Kleinanzeigen API should be used"""
        return self.config.kleinanzeigen_api_enabled()

    def get_results(self, search_url, max_pages=None):
        """Fetch listings via the hosted API first and fall back to webdriver scraping"""
        if self.uses_api():
            try:
                return self._get_results_from_api(search_url, max_pages)
            except (requests.exceptions.RequestException, ValueError, TypeError) as error:
                logger.warning(
                    "Hosted Kleinanzeigen API failed for %s, falling back to webdriver: %s",
                    search_url,
                    error,
                )
        return super().get_results(search_url, max_pages)

    def _api_timeout(self):
        """Use fast connect timeout while allowing slower scrape responses"""
        return (self.API_CONNECT_TIMEOUT, self.API_READ_TIMEOUT)

    def _get_results_from_api(self, search_url, max_pages=None):
        """Fetch search results from the hosted Kleinanzeigen API"""
        base_url = self.config.kleinanzeigen_api_base_url().rstrip("/")
        request_body = {
            "url": search_url,
            "max_pages": max_pages or 1,
        }

        results = None
        post_error = None
        for attempt in (1, 2):
            try:
                response = requests.post(
                    f"{base_url}/inserate-by-url",
                    headers=self.API_HEADERS,
                    json=request_body,
                    timeout=self._api_timeout(),
                )
                response.raise_for_status()
                payload = response.json()
                if "success" in payload and not payload.get("success"):
                    raise ValueError("Hosted Kleinanzeigen API returned an unsuccessful response")
                results = self._extract_result_list(payload, endpoint="/inserate-by-url")
                break
            except requests.exceptions.ReadTimeout as error:
                post_error = error
                if attempt == 1:
                    logger.warning(
                        "Hosted Kleinanzeigen API timed out for %s on first attempt. Retrying once.",
                        search_url,
                    )
                    continue
                logger.warning(
                    "Hosted Kleinanzeigen API timed out again for %s. Falling back to /inserate.",
                    search_url,
                )
            except (requests.exceptions.RequestException, ValueError) as error:
                post_error = error
                logger.warning(
                    "Hosted Kleinanzeigen API POST path failed for %s on attempt %d: %s",
                    search_url,
                    attempt,
                    error,
                )
                if attempt == 1:
                    continue

        if results is None:
            logger.warning(
                "Falling back to /inserate for %s after POST failures: %s",
                search_url,
                post_error,
            )
            results = self._get_results_from_legacy_api(search_url, max_pages)

        entries = []
        for result in results:
            try:
                details = self._load_api_details(result)
                entries.append(self._map_api_entry(result, details))
            except (TypeError, ValueError) as error:
                logger.warning("Skipping malformed Kleinanzeigen API result: %s", error)

        logger.debug('Number of entries found via hosted API: %d', len(entries))
        return entries

    @staticmethod
    def _extract_result_list(payload, endpoint):
        """Extract listing arrays from different API response schemas"""
        for key in ("results", "data", "items"):
            results = payload.get(key)
            if isinstance(results, list):
                return results

        nested_data = payload.get("data")
        if isinstance(nested_data, dict):
            for key in ("results", "items", "data"):
                results = nested_data.get(key)
                if isinstance(results, list):
                    return results

        preview = str(payload)
        if len(preview) > 300:
            preview = preview[:300] + "..."
        raise ValueError(
            f"Hosted Kleinanzeigen API {endpoint} did not return a result list. "
            f"Payload preview: {preview}"
        )

    def _get_results_from_legacy_api(self, search_url, max_pages=None):
        """Fallback to legacy /inserate endpoint when /inserate-by-url times out"""
        base_url = self.config.kleinanzeigen_api_base_url().rstrip("/")
        response = requests.get(
            f"{base_url}/inserate",
            params=self._build_legacy_search_params(search_url, max_pages),
            headers={"accept": "application/json"},
            timeout=self._api_timeout(),
        )
        response.raise_for_status()
        payload = response.json()
        if "success" in payload and not payload.get("success"):
            raise ValueError("Hosted Kleinanzeigen /inserate fallback returned an unsuccessful response")
        return self._extract_result_list(payload, endpoint="/inserate")

    @staticmethod
    def _build_legacy_search_params(search_url, max_pages=None):
        """Map a Kleinanzeigen search URL to /inserate query parameters"""
        parsed = urlparse(search_url)
        query = parse_qs(parsed.query)
        segments = [segment for segment in unquote(parsed.path).strip("/").split("/") if segment]

        params = {
            "page_count": max_pages or 1,
        }

        if query.get("keywords"):
            params["query"] = query["keywords"][0]
        if query.get("locationStr"):
            params["location"] = query["locationStr"][0]
        if query.get("radius"):
            try:
                params["radius"] = int(query["radius"][0])
            except ValueError:
                pass

        for segment in segments:
            if not segment.startswith("preis:"):
                continue
            parts = segment.split(":")
            if len(parts) >= 3:
                if parts[1]:
                    params["min_price"] = int(parts[1])
                if parts[2]:
                    params["max_price"] = int(parts[2])

        if "location" not in params and len(segments) > 1 and ":" not in segments[1]:
            params["location"] = segments[1]

        if "query" not in params:
            for idx, segment in enumerate(segments):
                if not re.match(r"^k?\d*c\d+", segment):
                    continue
                if idx > 0:
                    candidate = segments[idx - 1]
                    if ":" not in candidate and not candidate.startswith("s-"):
                        params["query"] = candidate
                break

        return params

    def _load_api_details(self, result):
        """Fetch structured listing details for a single hosted API result"""
        url = result.get("url", "")
        listing_id = self._extract_listing_id(url) or str(result.get("adid", "")).strip()
        if not listing_id:
            return {}

        base_url = self.config.kleinanzeigen_api_base_url().rstrip("/")
        response = requests.get(
            f"{base_url}/inserat/{listing_id}",
            params={"batch_id": f"flathunter-{result.get('adid', listing_id)}"},
            timeout=self._api_timeout(),
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("success"):
            raise ValueError(f"Hosted Kleinanzeigen detail API failed for listing {listing_id}")
        return payload.get("data", {})

    def _map_api_entry(self, summary, details):
        """Convert hosted API payloads to Flathunter expose format"""
        image_urls = (((details.get("media") or {}).get("images") or {}).get("urls") or [])
        location = details.get("location") or {}
        detail_values = details.get("details") or {}
        entry_id = self._derive_entry_id(summary, details)
        fallback_text = " ".join(
            str(part).strip()
            for part in (summary.get("title"), summary.get("description"), details.get("title"))
            if part
        )
        size = self._lookup_detail_value(detail_values, ('wohnfl', 'flache', 'fläche'))
        rooms = self._lookup_detail_value(detail_values, ('zimmer', 'raeume', 'raume', 'räume'))
        if not size:
            size = self._extract_size_from_text(fallback_text)
        if not rooms:
            rooms = self._extract_rooms_from_text(fallback_text)

        return {
            'id': int(entry_id),
            'image': image_urls[0] if image_urls else None,
            'url': details.get('url_redirected') or summary.get('url', ''),
            'title': details.get('title') or summary.get('title', ''),
            'price': self._format_price(details.get('price'), summary.get('price')),
            'size': size,
            'rooms': rooms,
            'address': self._format_location(location),
            'crawler': self.get_name(),
            'from': self._extract_available_from(detail_values),
        }

    @staticmethod
    def _derive_entry_id(summary, details):
        """Derive a stable numeric expose id from API payload variants"""
        entry_id = details.get("id") or summary.get("adid") or summary.get("id")
        if entry_id is not None and str(entry_id).strip():
            entry_id_str = str(entry_id).strip()
            if entry_id_str.isdigit():
                return int(entry_id_str)

        fallback_source = details.get("url_redirected") or summary.get("url") or details.get("title") or summary.get("title")
        if not fallback_source:
            raise ValueError("Kleinanzeigen API result has no usable id or fallback source")
        digest = hashlib.sha256(str(fallback_source).encode("utf-8")).hexdigest()
        return int(digest, 16) % (10**16)

    @staticmethod
    def _extract_listing_id(url):
        """Extract the canonical Kleinanzeigen listing path segment from a URL"""
        path = urlsplit(url).path
        if not path:
            return None
        segment = path.rstrip('/').split('/')[-1]
        return segment or None

    @staticmethod
    def _format_price(detail_price, fallback_price):
        """Format Kleinanzeigen API price data as Flathunter-compatible text"""
        if isinstance(detail_price, dict):
            amount = str(detail_price.get('amount', '')).strip()
            currency = str(detail_price.get('currency', '€')).strip()
            if amount:
                suffix = ' VB' if detail_price.get('negotiable') else ''
                return f"{amount} {currency}{suffix}".strip()
        if fallback_price is None:
            return ''
        fallback = str(fallback_price).strip()
        return fallback if '€' in fallback else f"{fallback} €".strip()

    @staticmethod
    def _lookup_detail_value(detail_values, fragments):
        """Get the first detail value whose label loosely matches one of the fragments"""
        for key, value in detail_values.items():
            normalized_key = key.casefold().replace('ä', 'a').replace('ö', 'o').replace('ü', 'u')
            if any(fragment in normalized_key for fragment in fragments):
                return str(value).strip()
        return ''

    @staticmethod
    def _extract_size_from_text(text):
        """Extract apartment size from free text fallback"""
        if not text:
            return ''
        match = re.search(r'(\d{1,3}(?:[\.,]\d{1,2})?)\s*m²', text, flags=re.IGNORECASE)
        if match is None:
            return ''
        return f"{match.group(1)} m²"

    @staticmethod
    def _extract_rooms_from_text(text):
        """Extract room count from free text fallback"""
        if not text:
            return ''
        match = re.search(r'(\d{1,2}(?:[\.,]\d{1,2})?)\s*(?:Zimmer|Zi\.)', text, flags=re.IGNORECASE)
        if match is None:
            return ''
        return match.group(1)

    @staticmethod
    def _format_location(location):
        """Format structured location fields as a single address string"""
        parts = [location.get('zip'), location.get('city'), location.get('state')]
        return ' '.join(str(part).strip() for part in parts if part)

    def _extract_available_from(self, detail_values):
        """Extract move-in date from the detail payload or fall back to today"""
        available_from = self._lookup_detail_value(detail_values, ('verfugbar ab', 'verfugbar', 'bezug'))
        if available_from:
            date_match = re.search(r'(\w+)\s+(\d{4})', available_from)
            if date_match is not None and date_match[1] in self.MONTHS:
                return "01." + self.MONTHS[date_match[1]] + "." + date_match[2]
            full_date_match = re.search(r'(\d{2}\.\d{2}\.\d{4})', available_from)
            if full_date_match is not None:
                return full_date_match[1]
        return datetime.datetime.now().strftime('%d.%m.%Y')

    def get_expose_details(self, expose):
        if expose.get('from') and (expose.get('size') or expose.get('rooms') or expose.get('address')):
            return expose
        soup = self.get_page(expose['url'], self.get_driver())
        for detail in soup.find_all('li', {"class": "addetailslist--detail"}):
            if re.match(r'Verfügbar ab', detail.text):
                date_string = re.match(r'(\w+) (\d{4})', detail.text)
                if date_string is not None:
                    expose['from'] = "01." + self.MONTHS[date_string[1]] + "." + date_string[2]
        if 'from' not in expose:
            expose['from'] = datetime.datetime.now().strftime('%02d.%02m.%Y')
        return expose

    # pylint: disable=too-many-locals
    def extract_data(self, raw_data):
        """Extracts all exposes from a provided Soup object"""
        entries = []
        soup = raw_data.find(id="srchrslt-adtable")

        exposes = soup.find_all("article", class_="aditem")
        for  expose in exposes:

            title_elem = expose.find(class_="ellipsis")
            if title_elem.get("href"):
                url = title_elem.get("href")
            else:
                # If there is no title element, just continue since we can't provide an URL
                continue

            try:
                price = expose.find(
                    class_="aditem-main--middle--price-shipping--price").text.strip()
                tags = expose.find_all(class_="simpletag")
                address = expose.find("div", {"class": "aditem-main--top--left"})
                image_element = expose.find("div", {"class": "galleryimage-element"})
            except AttributeError as error:
                logger.warning("Unable to process eBay expose: %s", str(error))
                continue

            if image_element is not None:
                image = image_element["data-imgsrc"]
            else:
                image = None

            address = address.text.strip()
            address = address.replace('\n', ' ').replace('\r', '')
            address = " ".join(address.split())

            rooms = ""
            if len(tags) > 1:
                rooms_match = re.search(r'\d+[.|,]*\d*', tags[1].text, flags=re.MULTILINE)
                if rooms_match is not None:
                    rooms = rooms_match.group()

            try:
                size = tags[0].text.strip()
            except (IndexError, TypeError):
                size = ""

            details = {
                'id': int(expose.get("data-adid")),
                'image': image,
                'url': ("https://www.kleinanzeigen.de" + url),
                'title': title_elem.text.strip(),
                'price': price,
                'size': size,
                'rooms': rooms,
                'address': address,
                'crawler': self.get_name()
            }
            entries.append(details)

        logger.debug('Number of entries found: %d', len(entries))

        return entries

    def load_address(self, url):
        """Extract address from expose itself"""
        expose_soup = self.get_page(url)
        street_raw = ""
        street_el = expose_soup.find(id="street-address")
        if isinstance(street_el, Tag):
            street_raw = street_el.text
        address_raw = ""
        address_el = expose_soup.find(id="viewad-locality")
        if isinstance(address_el, Tag):
            address_raw = address_el.text

        return address_raw.strip().replace("\n", "") + " " + street_raw.strip()

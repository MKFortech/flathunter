import pytest
from unittest.mock import Mock

from flathunter.crawler.kleinanzeigen import Kleinanzeigen
from test.utils.config import StringConfig

DUMMY_CONFIG = """
urls:
  - https://www.kleinanzeigen.de/s-wohnung-mieten/muenchen/anbieter:privat/anzeige:angebote/preis:600:1000
    """

TEST_URL = 'https://www.kleinanzeigen.de/s-wohnung-mieten/berlin/preis:1000:1500/c203l3331+wohnung_mieten.qm_d:70,+wohnung_mieten.zimmer_d:2'

@pytest.fixture
def crawler():
    return Kleinanzeigen(StringConfig(string=DUMMY_CONFIG))

def test_crawler(crawler):
    soup = crawler.get_page(TEST_URL)
    assert soup is not None
    entries = crawler.extract_data(soup)
    assert entries is not None
    assert len(entries) > 0
    assert entries[0]['id'] > 0
    assert entries[0]['url'].startswith("https://www.kleinanzeigen.de/s-anzeige")
    for attr in [ 'title', 'price', 'size', 'rooms', 'address' ]:
        assert entries[0][attr]

def test_process_expose_fetches_details(crawler):
    soup = crawler.get_page(TEST_URL)
    assert soup is not None
    entries = crawler.extract_data(soup)
    assert entries is not None
    assert len(entries) > 0
    updated_entries = [ crawler.get_expose_details(expose) for expose in entries ]
    for expose in updated_entries:
        print(expose)
        for attr in [ 'title', 'price', 'size', 'rooms', 'address', 'from' ]:
            assert expose[attr]


API_CONFIG = """
urls:
  - https://www.kleinanzeigen.de/s-wohnung-mieten/berlin/c203l3331
kleinanzeigen:
  api:
    enabled: true
    base_url: https://example.test
"""


@pytest.fixture
def api_crawler():
    return Kleinanzeigen(StringConfig(string=API_CONFIG))


def test_crawler_uses_hosted_api(api_crawler, monkeypatch):
    search_response = Mock()
    search_response.json.return_value = {
        "success": True,
        "results": [
            {
                "adid": "1234567890",
                "url": "https://www.kleinanzeigen.de/s-anzeige/test-flat/1234567890-203-3331",
                "title": "Schone Wohnung",
                "price": "1200",
            }
        ],
    }
    search_response.raise_for_status.return_value = None

    detail_response = Mock()
    detail_response.json.return_value = {
        "success": True,
        "data": {
            "id": "1234567890",
            "url_redirected": "https://www.kleinanzeigen.de/s-anzeige/test-flat/1234567890-203-3331",
            "title": "Schone Wohnung",
            "price": {"amount": "1200", "currency": "€", "negotiable": False},
            "location": {"zip": "10115", "city": "Berlin", "state": "Berlin"},
            "media": {"images": {"urls": ["https://img.example.test/flat.jpg"]}},
            "details": {
                "Wohnfläche": "72 m²",
                "Zimmer": "2,5",
                "Verfügbar ab": "Juli 2026",
            },
        },
    }
    detail_response.raise_for_status.return_value = None

    monkeypatch.setattr(
        "flathunter.crawler.kleinanzeigen.requests.post",
        lambda *args, **kwargs: search_response,
    )
    monkeypatch.setattr(
        "flathunter.crawler.kleinanzeigen.requests.get",
        lambda *args, **kwargs: detail_response,
    )

    entries = api_crawler.crawl("https://www.kleinanzeigen.de/s-wohnung-mieten/berlin/c203l3331")

    assert len(entries) == 1
    assert entries[0]["id"] == 1234567890
    assert entries[0]["image"] == "https://img.example.test/flat.jpg"
    assert entries[0]["price"] == "1200 €"
    assert entries[0]["size"] == "72 m²"
    assert entries[0]["rooms"] == "2,5"
    assert entries[0]["address"] == "10115 Berlin Berlin"
    assert entries[0]["from"] == "01.07.2026"

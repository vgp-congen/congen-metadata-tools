"""Tests for doi.org resolution.

Recorded payloads, not the live service: the suite is offline, and what
matters is the classification — a DOI that does not resolve is a fact
about the DOI, while a timeout is a fact about the network.
"""

from __future__ import annotations

import pytest

from congen.core import http
from congen.core.cache import Cache
from congen.core.remote.doi import CSL_JSON, Doi, DoiNotFound, Work, parse_work

# Trimmed from the real response for 10.1126/science.adj8766.
PAYLOAD = """{
  "title": "Sexual selection promotes reproductive isolation in barn swallows",
  "container-title": "Science",
  "issued": {"date-parts": [[2024, 12, 13]]},
  "author": [
    {"given": "Drew R.", "family": "Schield"},
    {"given": "Javan K.", "family": "Carter"}
  ]
}"""


class TestParsing:
    def test_it_builds_a_reference_in_the_search_result_shape(self):
        """A citation found by search and one typed by hand must not be
        distinguishable in the rendered document."""
        work = parse_work("10.1126/science.adj8766", PAYLOAD)
        assert work.reference == (
            "Schield DR et al. (2024) Sexual selection promotes reproductive "
            "isolation in barn swallows Science."
        )

    def test_a_family_name_gets_initials_not_full_given_names(self):
        assert parse_work("10.1/x", PAYLOAD).authors[0] == "Schield DR"

    def test_a_single_author_is_not_given_et_al(self):
        payload = '{"title": "Solo", "author": [{"given": "A B", "family": "Solo"}]}'
        assert parse_work("10.1/x", payload).reference == "Solo AB Solo"

    def test_a_literal_author_name_is_used_as_is(self):
        payload = '{"title": "T", "author": [{"literal": "The Consortium"}]}'
        assert parse_work("10.1/x", payload).authors == ("The Consortium",)

    def test_a_title_given_as_a_list_is_tolerated(self):
        """CSL sometimes returns a list where the schema says string."""
        assert parse_work("10.1/x", '{"title": ["Listed"]}').title == "Listed"

    def test_the_year_falls_back_through_the_date_fields(self):
        payload = '{"title": "T", "published-print": {"date-parts": [[2019]]}}'
        assert parse_work("10.1/x", payload).year == "2019"

    def test_a_payload_with_nothing_useful_yields_an_empty_reference(self):
        assert parse_work("10.1/x", "{}").reference == ""

    def test_the_url_is_built_from_the_doi(self):
        assert Work(doi="10.1/x").url == "https://doi.org/10.1/x"


class TestResolution:
    def _resolver(self, monkeypatch, responder, tmp_path):
        monkeypatch.setattr(http, "request", responder)
        return Doi(Cache(directory=tmp_path))

    def test_a_404_is_a_verdict_about_the_doi(self, monkeypatch, tmp_path):
        def not_found(url, **kwargs):
            raise http.HttpError(url, 404, "Not Found")

        resolver = self._resolver(monkeypatch, not_found, tmp_path)
        with pytest.raises(DoiNotFound):
            resolver.resolve("10.1111/mec.17O63")

    def test_any_other_failure_propagates(self, monkeypatch, tmp_path):
        """An outage must never be recorded as "this DOI is wrong"."""

        def unreachable(url, **kwargs):
            raise TimeoutError("doi.org is down")

        resolver = self._resolver(monkeypatch, unreachable, tmp_path)
        with pytest.raises(TimeoutError):
            resolver.resolve("10.1/x")

    def test_a_server_error_propagates_rather_than_reading_as_absent(
        self, monkeypatch, tmp_path
    ):
        def broken(url, **kwargs):
            raise http.HttpError(url, 503, "Service Unavailable")

        resolver = self._resolver(monkeypatch, broken, tmp_path)
        with pytest.raises(http.HttpError):
            resolver.resolve("10.1/x")

    def test_it_asks_for_csl_json(self, monkeypatch, tmp_path):
        seen = {}

        def capture(url, *, headers=None, **kwargs):
            seen["url"] = url
            seen["accept"] = (headers or {}).get("Accept")
            return http.Response(url=url, status=200, body=PAYLOAD.encode(), headers={})

        self._resolver(monkeypatch, capture, tmp_path).resolve("10.1/x")
        assert seen["url"] == "https://doi.org/10.1/x"
        assert seen["accept"] == CSL_JSON

    def test_a_second_resolution_is_served_from_the_cache(self, monkeypatch, tmp_path):
        calls = []

        def once(url, **kwargs):
            calls.append(url)
            return http.Response(url=url, status=200, body=PAYLOAD.encode(), headers={})

        resolver = self._resolver(monkeypatch, once, tmp_path)
        first = resolver.resolve("10.1/x")
        second = resolver.resolve("10.1/x")
        assert len(calls) == 1
        assert first.reference == second.reference

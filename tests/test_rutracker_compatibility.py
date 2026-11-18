"""Offline contracts for RuTracker parsing and sequential verification stages."""

from __future__ import annotations

import asyncio
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import AsyncMock, patch

from defusedxml.common import DefusedXmlException
from homeassistant.core import HomeAssistant, ServiceCall

from custom_components import rutracker_transmission as rt

ATOM = """<feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Example &amp; episode</title>
<link href="https://example.invalid/viewtopic.php?t=12"/>
<id>https://example.invalid/t/12</id><category label="Series"/>
<updated>2026-01-01T00:00:00Z</updated></entry>
<entry><title>Unrelated</title><id>https://example.invalid/t/13</id></entry>
</feed>"""
INERT_ENTITY = """<!DOCTYPE feed [<!ENTITY marker "inert">]>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>&marker;</title></entry></feed>"""
MAGNET = "magnet:?xt=urn:btih:" + "0" * 40


class RuTrackerCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    """Keep public results and cleanup ordering independent of real endpoints."""

    async def asyncSetUp(self) -> None:
        self.hass = HomeAssistant("/unused-rutracker-test-config")
        self.cfg = rt.CONFIG_SCHEMA({rt.DOMAIN: {}})[rt.DOMAIN]
        self.hass.data[rt.DOMAIN] = {
            "config": self.cfg,
            "cache": {},
            "selected_result_id": "",
            "last_response": {"results": [], "notes": []},
        }

    async def asyncTearDown(self) -> None:
        await self.hass.async_stop()

    async def test_valid_atom_retains_candidate_fields_and_filtering(self) -> None:
        with patch.object(rt, "_http_get_text", AsyncMock(return_value=ATOM)):
            result = await rt._search_feed(
                object(), self.cfg, "Example", ["example"], 842
            )
        self.assertEqual(len(result), 1)
        self.assertEqual(
            result[0].as_dict(),
            {
                "id": "rt-12",
                "topic_id": "12",
                "title": "Example & episode",
                "forum_id": 842,
                "forum_name": "Series",
                "url": "https://example.invalid/viewtopic.php?t=12",
                "size": "",
                "seeders": None,
                "leechers": None,
                "updated": "2026-01-01T00:00:00Z",
                "approved": None,
                "source": "feed",
                "score": 0.0,
                "downloadable": True,
                "disabled_reason": "",
            },
        )

    async def test_inert_entity_is_rejected_as_a_feed_parse_error(self) -> None:
        with (
            patch.object(rt, "_http_get_text", AsyncMock(return_value=INERT_ENTITY)),
            self.assertRaises(ET.ParseError) as raised,
        ):
            await rt._search_feed(object(), self.cfg, "inert", ["inert"], 842)
        self.assertIsInstance(raised.exception.__cause__, DefusedXmlException)
        self.assertIn("Unsafe Atom feed:", str(raised.exception))

    async def test_ordinary_malformed_xml_remains_a_parse_error(self) -> None:
        with (
            patch.object(rt, "_http_get_text", AsyncMock(return_value="<feed>")),
            self.assertRaises(ET.ParseError),
        ):
            await rt._search_feed(object(), self.cfg, "Example", ["example"], 842)

    async def test_rejected_feed_uses_existing_search_failure_response(self) -> None:
        call = self.search_call("inert", forums=[842])
        with (
            patch.object(rt, "async_get_clientsession", return_value=object()),
            patch.object(rt, "async_dispatcher_send"),
            patch.object(rt, "_async_read_cookie_header", AsyncMock(return_value="")),
            patch.object(rt, "_http_get_text", AsyncMock(return_value=INERT_ENTITY)),
            patch.object(rt, "_fetch_topic_details", AsyncMock()),
        ):
            result = await rt._handle_search(self.hass, call)
        self.assertEqual(result["source"], "feed")
        self.assertEqual(result["results"], [])
        self.assertEqual(result["status"], "done")
        self.assertTrue(
            any("feed forum 842: Unsafe Atom feed:" in note for note in result["notes"])
        )

    async def test_search_retains_order_deduplication_and_fallback_notes(self) -> None:
        trace = []

        async def tracker(_session, _cfg, _query, forum, _cookie) -> list[rt.Candidate]:
            trace.append(("tracker", forum))
            if forum == 1803:
                error_message = "fixture unavailable"
                raise RuntimeError(error_message)
            ids = ["1"] if forum is not None else ["1", "2"]
            return [self.candidate(topic_id, "tracker") for topic_id in ids]

        async def feed(_session, _cfg, _query, _tokens, forum) -> list[rt.Candidate]:
            trace.append(("feed", forum))
            return [self.candidate("3", "feed")] if forum == 842 else []

        with (
            patch.object(rt, "async_get_clientsession", return_value=object()),
            patch.object(rt, "async_dispatcher_send"),
            patch.object(
                rt, "_async_read_cookie_header", AsyncMock(return_value="x=1")
            ),
            patch.object(rt, "_search_tracker_page", tracker),
            patch.object(rt, "_search_feed", feed),
            patch.object(rt, "_fetch_topic_details", AsyncMock()),
        ):
            result = await rt._handle_search(
                self.hass, self.search_call("Example", forums=[842, 1803])
            )
        self.assertEqual(
            trace,
            [
                ("tracker", 842),
                ("tracker", 1803),
                ("tracker", None),
                ("feed", 842),
                ("feed", 1803),
            ],
        )
        self.assertEqual(
            [item["id"] for item in result["results"]], ["rt-1", "rt-2", "rt-3"]
        )
        self.assertEqual(result["source"], "tracker+feed")
        self.assertEqual(result["selected_result_id"], "rt-1")
        self.assertTrue(
            any("forum 1803: fixture unavailable" in note for note in result["notes"])
        )
        self.assertIn(
            "Global RuTracker.org search added 1 fallback candidate(s).",
            result["notes"],
        )

    async def test_empty_search_performs_no_endpoint_lookup(self) -> None:
        with patch.object(rt, "async_get_clientsession") as session:
            result = await rt._handle_search(self.hass, self.search_call(" "))
        session.assert_not_called()
        self.assertEqual(result["source"], "none")
        self.assertEqual(result["notes"], ["Empty query"])
        self.assertEqual(result["results"], [])

    async def test_verification_keeps_duplicates_and_removes_only_new_test_torrents(
        self,
    ) -> None:
        candidates = [self.candidate("1", "tracker"), self.candidate("2", "tracker")]
        calls = []
        additions = iter(
            [{"torrent_duplicate": {"id": 7}}, {"torrent_added": {"id": 8}}]
        )

        async def rpc(_hass, _session, method, params) -> dict:
            calls.append((method, params))
            if method == "torrent_add":
                return {"result": next(additions)}
            if method == "torrent_set":
                error_message = "fixture limits unavailable"
                raise RuntimeError(error_message)
            if method == "torrent_get":
                return {"result": {"torrents": [{"id": 8, "peersConnected": 1}]}}
            return {"result": {}}

        with (
            patch.object(rt, "async_dispatcher_send"),
            patch.object(
                rt,
                "_verify_candidate_with_tracker_announce",
                AsyncMock(return_value={"checked": False}),
            ),
            patch.object(rt, "_transmission_rpc", rpc),
            patch.object(asyncio, "sleep", AsyncMock()),
        ):
            await rt._verify_candidates_with_transmission(
                self.hass,
                object(),
                candidates,
                rt._VerificationRequest(limit=2, duration=2, query="Example"),
            )
        self.assertEqual(
            [method for method, _ in calls],
            [
                "torrent_add",
                "torrent_add",
                "torrent_set",
                "torrent_get",
                "torrent_remove",
            ],
        )
        self.assertEqual(calls[-1][1], {"ids": [8], "delete_local_data": True})
        self.assertTrue(all(item.downloadable for item in candidates))
        self.assertTrue(all(not item.disabled_reason for item in candidates))

    async def test_expired_verification_still_cleans_up_its_new_torrent(self) -> None:
        candidate = self.candidate("1", "tracker")
        rpc = AsyncMock(side_effect=[{"result": {"torrent_added": {"id": 8}}}, {}, {}])
        with (
            patch.object(
                rt,
                "_verify_candidate_with_tracker_announce",
                AsyncMock(return_value={"checked": False}),
            ),
            patch.object(rt, "_transmission_rpc", rpc),
        ):
            await rt._verify_candidates_with_transmission(
                self.hass,
                object(),
                [candidate],
                rt._VerificationRequest(limit=1, duration=0),
            )
        self.assertFalse(candidate.downloadable)
        self.assertEqual(
            candidate.disabled_reason,
            "Transmission test did not fetch metadata or peers in time",
        )
        self.assertEqual(
            rpc.await_args.args[2:],
            ("torrent_remove", {"ids": [8], "delete_local_data": True}),
        )

    async def test_add_keeps_selection_errors_and_success_params(self) -> None:
        self.hass.data[rt.DOMAIN]["cache"] = {
            "rt-1": {
                "topic_id": "1",
                "magnet": MAGNET,
                "title": "Fixture",
                "downloadable": True,
            },
            "rt-2": {
                "topic_id": "2",
                "magnet": MAGNET,
                "downloadable": False,
                "disabled_reason": "fixture disabled",
            },
        }
        self.hass.data[rt.DOMAIN]["selected_result_id"] = "rt-1"
        cases = [
            (
                {"result_id": "missing"},
                {"ok": False, "error": "Unknown result_id: missing"},
            ),
            (
                {"result_id": "rt-2"},
                {
                    "ok": False,
                    "error": "Selected result is disabled: fixture disabled",
                    "result_id": "rt-2",
                    "topic_id": "2",
                },
            ),
            (
                {"magnet": "invalid"},
                {
                    "ok": False,
                    "error": "No magnet link is available for the selected result.",
                    "result_id": "",
                    "topic_id": "",
                },
            ),
            (
                {"result_id": "rt-1", "download_dir": "relative"},
                {
                    "ok": False,
                    "error": "download_dir must be an absolute path.",
                    "result_id": "rt-1",
                    "topic_id": "1",
                },
            ),
        ]
        cases.append(
            (
                {"magnet": "https://example.invalid/viewtopic.php?t=12"},
                {
                    "ok": False,
                    "error": "No magnet link is available for the selected result.",
                    "result_id": "",
                    "topic_id": "12",
                },
            )
        )
        rpc = AsyncMock(return_value={"result": {"torrent_added": {"id": 8}}})
        with (
            patch.object(rt, "async_get_clientsession", return_value=object()),
            patch.object(rt, "_transmission_rpc", rpc),
            patch.object(rt, "_async_read_cookie_header", AsyncMock(return_value="")),
            patch.object(rt, "_fetch_topic_details", AsyncMock()),
        ):
            for data, expected in cases:
                with self.subTest(data=data):
                    call = ServiceCall(
                        self.hass, rt.DOMAIN, rt.SERVICE_ADD, rt.ADD_SCHEMA(data)
                    )
                    self.assertEqual(await rt._handle_add(self.hass, call), expected)
            rpc.assert_not_awaited()
            call = ServiceCall(
                self.hass,
                rt.DOMAIN,
                rt.SERVICE_ADD,
                rt.ADD_SCHEMA({"labels": "tv, archives", "paused": True}),
            )
            result = await rt._handle_add(self.hass, call)
        self.assertEqual(
            result,
            {
                "ok": True,
                "result_id": "rt-1",
                "topic_id": "1",
                "title": "Fixture",
                "transmission": {"torrent_added": {"id": 8}},
            },
        )
        rpc.assert_awaited_once()
        self.assertEqual(
            rpc.await_args.args[2:],
            (
                "torrent_add",
                {"filename": MAGNET, "paused": True, "labels": ["tv", "archives"]},
            ),
        )

    def search_call(self, query: str, **kwargs: object) -> ServiceCall:
        data = rt.SEARCH_SCHEMA(
            {"query": query, "verify_mode": "none", "max_results": 5, **kwargs}
        )
        return ServiceCall(self.hass, rt.DOMAIN, rt.SERVICE_SEARCH, data)

    def candidate(self, topic_id: str, source: str) -> rt.Candidate:
        return rt.Candidate(
            topic_id,
            "Example episode",
            forum_id=842,
            seeders=3,
            source=source,
            magnet=MAGNET,
        )

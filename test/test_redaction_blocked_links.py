"""Backend contract for the blocked-link chip's structure-only records.

The exfiltration redactor keeps only the domain in the saved transcript, so a
chip built from that text can show nothing but the site name. Step 3 adds a
one-loop collector that emits, per redacted URL, a JSON-serialisable record
retaining STRUCTURE only -- domain, rule id, a path kept only when both removers
would pass it, and the query's char COUNT -- never the query value. These tests
pin the record shape, the retention rule, the dedupe, the persistence attach
point, and the attacker-writable-meta carve-out.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.dashboard import chat_persistence
from kiro_crew.dashboard.chat_utils import (
    _redact_meta_for_role,
    adopt_variant_text,
    variant_from_row,
)
from kiro_crew.security import (
    redact_credentials,
    redact_exfiltration_urls,
    redact_exfiltration_urls_with_records,
    revalidate_blocked_link,
)
from kiro_crew.security import exfil
from kiro_crew.security.exfil import _exfil_url_warning

_DOMAIN = "collect.example.com"
_LONG_QUERY_TAIL = "A" * 250


def _placeholder_count(text: str) -> int:
    from kiro_crew.security import EXFILTRATION_REDACTION_TAG_PREFIX

    return text.count(EXFILTRATION_REDACTION_TAG_PREFIX)


class TestCollectorRecordsMatchRedaction:
    def test_one_record_per_redacted_url_in_order(self) -> None:
        url_a = f"https://a.example.com/x?token={_LONG_QUERY_TAIL}"
        url_b = f"https://b.example.com/y?token={_LONG_QUERY_TAIL}"
        text = f"first {url_a} then {url_b}"

        cleaned, warnings, records = redact_exfiltration_urls_with_records(text)

        assert warnings
        assert _placeholder_count(cleaned) == 2
        assert [r["domain"] for r in records] == ["a.example.com", "b.example.com"]

    def test_repeated_url_yields_one_record(self) -> None:
        url = f"https://{_DOMAIN}/x?token={_LONG_QUERY_TAIL}"
        text = f"{url} and again {url}"

        cleaned, _, records = redact_exfiltration_urls_with_records(text)

        # Every occurrence is redacted, but the record set is deduped by the
        # matched URL string in first-appearance order.
        assert _placeholder_count(cleaned) == 2
        assert len(records) == 1

    def test_records_carry_no_positional_field(self) -> None:
        url = f"https://{_DOMAIN}/x?token={_LONG_QUERY_TAIL}"

        _, _, records = redact_exfiltration_urls_with_records(f"see {url}")

        assert records
        for record in records:
            assert set(record.keys()) == {"domain", "rule", "path", "query_chars"}


class TestPathRetention:
    def test_clean_path_is_kept(self) -> None:
        url = f"https://{_DOMAIN}/reports/summary?token={_LONG_QUERY_TAIL}"

        _, _, records = redact_exfiltration_urls_with_records(f"see {url}")

        assert records[0]["path"] == "/reports/summary"

    def test_path_that_itself_trips_the_rule_is_not_kept(self) -> None:
        # Heavy percent encoding in the PATH trips the redactor on the path
        # alone -- retention check 1 fails -- so nothing is kept.
        percent_path = "/" + "%41" * 25
        url = f"https://{_DOMAIN}{percent_path}"
        assert _exfil_url_warning(_DOMAIN, percent_path, frozenset()) is not None

        _, warnings, records = redact_exfiltration_urls_with_records(f"see {url}")

        assert warnings
        assert records[0]["path"] is None

    def test_path_carrying_a_credential_is_not_kept(self) -> None:
        # A bare secret run the query heuristics never see (they scan only the
        # query) passes the URL warning on the path alone but is stripped by
        # credential redaction -- retention check 2 is what drops it.
        secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        cred_path = f"/d/{secret}"
        assert _exfil_url_warning(_DOMAIN, cred_path, frozenset()) is None
        assert redact_credentials(cred_path)[0] != cred_path

        url = f"https://{_DOMAIN}{cred_path}?token={_LONG_QUERY_TAIL}"
        _, _, records = redact_exfiltration_urls_with_records(f"see {url}")

        assert records[0]["path"] is None


class TestQueryIsCountedNeverStored:
    def test_query_chars_counts_and_value_is_absent(self) -> None:
        secret = "S3cr3tExfilPayloadValueThatMustNeverPersist0001"
        query = f"leak={secret}{_LONG_QUERY_TAIL}"
        url = f"https://{_DOMAIN}/p?{query}"

        _, _, records = redact_exfiltration_urls_with_records(f"see {url}")

        assert records[0]["query_chars"] == len(query)
        serialised = json.dumps(records)
        assert secret not in serialised
        # No substring of the query value survives in the record.
        assert _LONG_QUERY_TAIL not in serialised

    def test_query_chars_zero_when_absent(self) -> None:
        url = f"https://{_DOMAIN}/dump/AKIAIOSFODNN7EXAMPLE"

        _, warnings, records = redact_exfiltration_urls_with_records(f"see {url}")

        assert warnings
        assert records[0]["query_chars"] == 0


class TestExistingCallersUnaffected:
    def test_clean_text_returns_unchanged_two_tuple(self) -> None:
        text = "nothing suspicious https://example.com/docs here"

        result = redact_exfiltration_urls(text)

        assert result == (text, [])

    def test_blocked_url_two_tuple_matches_collector(self) -> None:
        from kiro_crew.security import EXFILTRATION_REDACTION_TAG_PREFIX

        url = f"https://{_DOMAIN}/x?token={_LONG_QUERY_TAIL}"
        text = f"see {url}"

        cleaned, warnings = redact_exfiltration_urls(text)
        cleaned2, warnings2, _ = redact_exfiltration_urls_with_records(text)

        assert (cleaned, warnings) == (cleaned2, warnings2)
        assert f"{EXFILTRATION_REDACTION_TAG_PREFIX}{_DOMAIN}]" in cleaned


class TestPersistenceAttachesRecords:
    def test_a_row_keeps_the_records_it_arrives_with(self) -> None:
        # The shape the live path produces: the text is ALREADY the placeholder,
        # because the redaction that made it is where the records were born.
        url = f"https://{_DOMAIN}/reports/summary?token={_LONG_QUERY_TAIL}"
        redacted, _, records = redact_exfiltration_urls_with_records(f"see {url}")
        entry = chat_persistence._build_message_entry_uncached(
            {
                "role": "assistant",
                "content": redacted,
                "ts": "2026-01-01T00:00:00Z",
                "meta": {"blocked_links": records},
            }
        )

        assert entry is not None
        blocked = entry["meta"]["blocked_links"]
        assert blocked[0]["domain"] == _DOMAIN
        assert blocked[0]["path"] == "/reports/summary"
        assert url not in entry["content"]

    def test_persistence_does_not_invent_records_from_a_placeholder(self) -> None:
        # A scan of this row can only see the placeholder, so re-deriving here
        # would describe nothing. The absence is the point: records are carried,
        # never re-derived, and a row that arrives without them stays without.
        url = f"https://{_DOMAIN}/reports/summary?token={_LONG_QUERY_TAIL}"
        redacted, _, _ = redact_exfiltration_urls_with_records(f"see {url}")
        entry = chat_persistence._build_message_entry_uncached(
            {"role": "assistant", "content": redacted, "ts": "2026-01-01T00:00:00Z"}
        )

        assert entry is not None
        assert "blocked_links" not in (entry.get("meta") or {})

    def test_clean_message_omits_the_key(self) -> None:
        entry = chat_persistence._build_message_entry_uncached(
            {"role": "assistant", "content": "no links here", "ts": "2026-01-01T00:00:00Z"}
        )

        assert entry is not None
        assert "meta" not in entry

    def test_user_branch_is_not_redacted(self) -> None:
        url = f"https://{_DOMAIN}/x?token={_LONG_QUERY_TAIL}"
        entry = chat_persistence._build_message_entry_uncached(
            {"role": "user", "content": f"see {url}", "ts": "2026-01-01T00:00:00Z"}
        )

        assert entry is not None
        assert entry["content"] == f"see {url}"
        assert "meta" not in entry


class TestRetentionIsBounded:
    """The store is the transcript line, so both retention points carry a bound.

    A record is cheap to write into a line, and every render of the message that
    holds it re-validates and re-serializes whatever it holds.
    """

    def test_the_record_count_is_capped_but_the_text_is_fully_redacted(self) -> None:
        cap = exfil.MAX_BLOCKED_LINKS_PER_MESSAGE
        urls = [f"https://h{i}.example.com/x?token={_LONG_QUERY_TAIL}" for i in range(cap + 5)]
        text = " ".join(urls)

        cleaned, _, records = redact_exfiltration_urls_with_records(text)

        assert len(records) == cap
        # The cap bounds what is DESCRIBED, never what is removed.
        for url in urls:
            assert url not in cleaned
        assert _placeholder_count(cleaned) == cap + 5

    def test_an_overlong_path_is_dropped_not_truncated(self) -> None:
        long_path = "/" + "a" * (exfil.MAX_BLOCKED_LINK_PATH_CHARS + 1)
        url = f"https://{_DOMAIN}{long_path}?token={_LONG_QUERY_TAIL}"

        _, _, records = redact_exfiltration_urls_with_records(url)

        assert len(records) == 1
        assert records[0]["path"] is None
        assert records[0]["domain"] == _DOMAIN

    def test_revalidation_rejects_an_absurd_query_count(self) -> None:
        record = {
            "domain": _DOMAIN,
            "rule": "long_query",
            "path": None,
            "query_chars": exfil.MAX_BLOCKED_LINK_QUERY_CHARS + 1,
        }

        assert revalidate_blocked_link(record) is None

    def test_revalidation_rejects_an_overlong_path(self) -> None:
        record = {
            "domain": _DOMAIN,
            "rule": "long_query",
            "path": "/" + "a" * (exfil.MAX_BLOCKED_LINK_PATH_CHARS + 1),
            "query_chars": 256,
        }

        assert revalidate_blocked_link(record) is None

    def test_a_forged_line_carrying_many_valid_records_is_capped(self) -> None:
        cap = exfil.MAX_BLOCKED_LINKS_PER_MESSAGE
        forged = [
            {"domain": f"h{i}.example.com", "rule": "long_query", "path": None, "query_chars": 256}
            for i in range(cap * 4)
        ]

        out = _redact_meta_for_role("assistant", {"blocked_links": forged})

        assert len(out["blocked_links"]) == cap

    def test_every_retained_string_has_a_bound(self) -> None:
        # The validator reads this table rather than each field by name, so a
        # field added without an entry is not retained. Pinning the table against
        # the record's own key set is what makes that hold for a FUTURE field.
        bounded = set(exfil._BLOCKED_LINK_STRING_BOUNDS)
        assert bounded == exfil._BLOCKED_LINK_RECORD_KEYS - {"query_chars"}
        assert all(limit > 0 for limit in exfil._BLOCKED_LINK_STRING_BOUNDS.values())

    def test_revalidation_rejects_an_overlong_domain_and_rule(self) -> None:
        over_domain = "a" * 300 + ".example.com"
        assert (
            revalidate_blocked_link(
                {"domain": over_domain, "rule": "long_query", "path": None, "query_chars": 1}
            )
            is None
        )
        assert (
            revalidate_blocked_link(
                {"domain": _DOMAIN, "rule": "r" * 200, "path": None, "query_chars": 1}
            )
            is None
        )

    def test_the_dedupe_set_stops_growing_at_the_cap(self) -> None:
        cap = exfil.MAX_BLOCKED_LINKS_PER_MESSAGE
        urls = [f"https://h{i}.example.com/x?token={_LONG_QUERY_TAIL}" for i in range(cap + 6)]
        # Each URL twice, so a dedupe row past the cap would be reachable twice.
        text = " ".join(urls + urls)

        cleaned, _, records = redact_exfiltration_urls_with_records(text)

        assert len(records) == cap
        for url in urls:
            assert url not in cleaned


class TestRecordsReachTheDisplayPath:
    """The display path reads the ROW's meta, so the records have to be on it.

    `_prepare_messages` hands `m["meta"]` to `_redact_meta_for_role`, and
    `slot.append` broadcasts the live frame from inside the call, so the records
    go into the meta that call carries rather than onto the row afterwards.
    """

    def test_segment_meta_carries_the_records(self, monkeypatch) -> None:
        from kiro_crew.dashboard import chat_runner

        monkeypatch.setattr(chat_runner, "_decisions_strip_meta", lambda _slot: None)
        records = [
            {"domain": _DOMAIN, "rule": "exfil_query_length", "path": "/p", "query_chars": 9}
        ]

        assert chat_runner._segment_row_meta(object(), records) == {"blocked_links": records}

    def test_segment_meta_is_none_when_there_is_nothing_to_carry(self, monkeypatch) -> None:
        from kiro_crew.dashboard import chat_runner

        monkeypatch.setattr(chat_runner, "_decisions_strip_meta", lambda _slot: None)

        assert chat_runner._segment_row_meta(object(), []) is None

    def test_segment_meta_keeps_the_decision_strip_beside_them(self, monkeypatch) -> None:
        from kiro_crew.dashboard import chat_runner

        monkeypatch.setattr(
            chat_runner, "_decisions_strip_meta", lambda _slot: {"decisions_strip": ["x"]}
        )
        records = [
            {"domain": _DOMAIN, "rule": "exfil_query_length", "path": None, "query_chars": 9}
        ]

        out = chat_runner._segment_row_meta(object(), records)

        assert out == {"decisions_strip": ["x"], "blocked_links": records}

    def test_the_segment_flush_collects_records_at_its_own_redaction(self) -> None:
        # A source guard, because the wiring is the defect: a flush that calls the
        # two-tuple redactor stores the placeholder and the chip never renders,
        # and no unit on the persistence layer can see that.
        import inspect

        from kiro_crew.dashboard import chat_runner

        src = inspect.getsource(chat_runner._flush_segment)
        assert "redact_exfiltration_urls_with_records(assistant_text)" in src
        assert "_segment_row_meta(slot, blocked_links)" in src


class TestRecordsSurviveAReopenedSession:
    """A row's records travel with it: born at the redaction, carried thereafter.

    The load path redacts content on the way in, so a rehydrated row holds the
    placeholder. Content survives a reopen because redaction is idempotent;
    records are not derivable from it at all, which is why they are carried.
    """

    def test_carried_records_survive_a_reserialisation(self) -> None:
        url = f"https://{_DOMAIN}/reports/summary?token={_LONG_QUERY_TAIL}"
        redacted, _, saved = redact_exfiltration_urls_with_records(f"see {url}")

        rehydrated = chat_persistence._build_message_entry_uncached(
            {
                "role": "assistant",
                "content": redacted,
                "ts": "2026-01-01T00:00:00Z",
                "meta": {"blocked_links": saved},
            }
        )

        assert rehydrated is not None
        assert rehydrated["meta"]["blocked_links"] == saved

    def test_carried_records_are_dropped_without_a_placeholder(self) -> None:
        # Records DESCRIBE placeholders, so a row whose text has none does not
        # keep them -- that pairing is the only thing making them meaningful.
        entry = chat_persistence._build_message_entry_uncached(
            {
                "role": "assistant",
                "content": "no links here",
                "ts": "2026-01-01T00:00:00Z",
                "meta": {
                    "blocked_links": [
                        {
                            "domain": _DOMAIN,
                            "rule": "exfil_query_length",
                            "path": "/reports/summary",
                            "query_chars": 256,
                        }
                    ]
                },
            }
        )

        assert entry is not None
        assert "blocked_links" not in (entry.get("meta") or {})

    def test_a_carried_record_is_still_revalidated(self) -> None:
        url = f"https://{_DOMAIN}/reports/summary?token={_LONG_QUERY_TAIL}"
        redacted, _, saved = redact_exfiltration_urls_with_records(f"see {url}")

        entry = chat_persistence._build_message_entry_uncached(
            {
                "role": "assistant",
                "content": redacted,
                "ts": "2026-01-01T00:00:00Z",
                "meta": {
                    "blocked_links": [
                        {
                            "domain": "not a host",
                            "rule": "exfil_query_length",
                            "path": None,
                            "query_chars": 1,
                        },
                        saved[0],
                    ]
                },
            }
        )

        assert entry is not None
        assert entry["meta"]["blocked_links"] == [saved[0]]


class TestAVariantCarriesItsOwnRecords:
    """A record describes ONE text, so a variant switch moves both or neither.

    Variants are alternate replies the reader can switch back to. Text and
    records travel as one value through ``adopt_variant_text``, so a row cannot
    end up explaining a link that is absent from the text on screen.
    """

    @staticmethod
    def _record(domain: str) -> dict:
        return {
            "domain": domain,
            "rule": "exfil_query_length",
            "path": "/reports/summary",
            "query_chars": 256,
        }

    def test_switching_takes_the_variant_s_records_with_its_text(self) -> None:
        row = {
            "role": "assistant",
            "content": "old text",
            "ts": "t0",
            "meta": {"blocked_links": [self._record("old.example.com")]},
        }

        adopt_variant_text(
            row,
            {"content": "new text", "ts": "t1", "blocked_links": [self._record("new.example.com")]},
        )

        assert row["content"] == "new text"
        assert row["ts"] == "t1"
        assert row["meta"]["blocked_links"] == [self._record("new.example.com")]

    def test_switching_to_a_clean_variant_drops_the_stale_records(self) -> None:
        # The alternative is a chip explaining a host this text never held.
        row = {
            "role": "assistant",
            "content": "old text",
            "ts": "t0",
            "meta": {"blocked_links": [self._record("old.example.com")]},
        }

        adopt_variant_text(row, {"content": "nothing blocked here", "ts": "t1"})

        assert "meta" not in row

    def test_other_meta_survives_the_switch(self) -> None:
        row = {
            "role": "assistant",
            "content": "old",
            "ts": "t0",
            "meta": {"mid": "m1", "blocked_links": [self._record("old.example.com")]},
        }

        adopt_variant_text(row, {"content": "new", "ts": "t1"})

        assert row["meta"] == {"mid": "m1"}

    def test_a_variant_gains_records_on_a_row_that_had_none(self) -> None:
        row = {"role": "assistant", "content": "clean", "ts": "t0"}

        adopt_variant_text(
            row,
            {"content": "blocked", "ts": "t1", "blocked_links": [self._record("new.example.com")]},
        )

        assert row["meta"]["blocked_links"] == [self._record("new.example.com")]

    def test_a_regenerate_stash_takes_the_records_with_the_text(self) -> None:
        # The round trip that loses them silently: the stashed text is already a
        # placeholder, so a stash without the records leaves nothing any later
        # scan can rebuild -- switching back would hand the reader a bare
        # placeholder with no way to learn what was removed.
        row = {
            "role": "assistant",
            "content": "see [REDACTED: suspicious URL to old.example.com]",
            "ts": "t0",
            "meta": {"blocked_links": [self._record("old.example.com")]},
        }

        stashed = variant_from_row(row)

        assert stashed["blocked_links"] == [self._record("old.example.com")]
        assert stashed["content"] == row["content"]

    def test_a_clean_row_stashes_without_inventing_records(self) -> None:
        stashed = variant_from_row({"role": "assistant", "content": "nothing here", "ts": "t0"})

        assert "blocked_links" not in stashed

    def test_the_round_trip_returns_the_same_records(self) -> None:
        original = {
            "role": "assistant",
            "content": "see [REDACTED: suspicious URL to old.example.com]",
            "ts": "t0",
            "meta": {"blocked_links": [self._record("old.example.com")]},
        }
        stashed = variant_from_row(original)

        # A regenerate replaced the row; the reader switches back to the stash.
        row: dict = {"role": "assistant", "content": "a different reply", "ts": "t1"}
        adopt_variant_text(row, stashed)

        assert row["content"] == original["content"]
        assert row["meta"]["blocked_links"] == [self._record("old.example.com")]

    def test_the_stash_site_moves_the_pair_through_one_function(self) -> None:
        import inspect

        from kiro_crew.dashboard import chat_regenerate

        src = inspect.getsource(chat_regenerate.api_chat_slot_regenerate)
        assert "variant_from_row(ai_msg)" in src
        assert '"content": ai_msg.get("content"' not in src

    def test_the_stash_flush_carries_records_instead_of_rescanning(self) -> None:
        # A rescan here reads already-redacted text, so it can only ever return
        # nothing; a source guard keeps the records variant out of this site.
        import inspect

        from kiro_crew.dashboard import chat_runner

        src = inspect.getsource(chat_runner._flush_segment)
        stash = src[src.index("_pending_variants") :]
        assert "redact_exfiltration_urls_with_records(v" not in stash

    def test_the_switch_endpoint_moves_the_pair_through_one_function(self) -> None:
        # A source guard: a site that assigns content directly is a site that can
        # leave the records behind, and no unit on this row can see that.
        import inspect

        from kiro_crew.dashboard import chat_regenerate

        src = inspect.getsource(chat_regenerate.api_chat_slot_switch_variant)
        assert "adopt_variant_text(target_dict, chosen)" in src
        assert 'target_dict["content"] =' not in src


class TestMetaCarveOut:
    @staticmethod
    def _good_record() -> dict:
        return {
            "domain": _DOMAIN,
            "rule": "exfil_query_length",
            "path": "/reports/summary",
            "query_chars": 42,
        }

    def test_valid_record_survives_meta_redaction(self) -> None:
        meta = {"blocked_links": [self._good_record()], "other": "value"}

        out = _redact_meta_for_role("assistant", meta)

        assert out["blocked_links"] == [self._good_record()]

    def test_tampered_records_are_dropped_individually(self) -> None:
        good = self._good_record()
        meta = {
            "blocked_links": [
                good,
                {
                    "domain": "not a host!!",
                    "rule": "exfil_query_length",
                    "path": None,
                    "query_chars": 1,
                },
                {"domain": _DOMAIN, "rule": "Bad Rule", "path": None, "query_chars": 1},
                {
                    "domain": _DOMAIN,
                    "rule": "exfil_query_length",
                    "path": "/dump/AKIAIOSFODNN7EXAMPLE",
                    "query_chars": 0,
                },
                {"domain": _DOMAIN, "unexpected": "key", "rule": "x", "path": None},
                {"domain": _DOMAIN, "rule": "exfil_query_length", "path": None, "query_chars": -1},
            ]
        }

        out = _redact_meta_for_role("assistant", meta)

        # Exactly the well-formed record survives; every tampered sibling is
        # dropped on its own, and the message is not discarded.
        assert out["blocked_links"] == [good]

    def test_all_bad_records_drop_the_key(self) -> None:
        meta = {"blocked_links": [{"wrong": "shape"}], "keep": "me"}

        out = _redact_meta_for_role("assistant", meta)

        assert "blocked_links" not in out
        assert out["keep"] == "me"

    def test_revalidate_rejects_payload_bearing_path(self) -> None:
        record = {
            "domain": _DOMAIN,
            "rule": "exfil_query_length",
            "path": "/dump/AKIAIOSFODNN7EXAMPLE",
            "query_chars": 0,
        }

        assert revalidate_blocked_link(record) is None

    def test_revalidate_rejects_boolean_query_chars(self) -> None:
        record = {
            "domain": _DOMAIN,
            "rule": "exfil_query_length",
            "path": None,
            "query_chars": True,
        }

        assert revalidate_blocked_link(record) is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-n0"]))

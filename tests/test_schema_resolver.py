"""M9 agent-assisted structure/column resolution (agents/schema_resolver.py).

resolve_schema() itself is exercised against a fake `query()` (same pattern
as test_ticker_resolver.py) purely for its own plumbing — prompt
construction, response parsing, and result shape. Its actual grounding gate
(validate_schema, checking a claimed mapping against a file's real data) is
tested directly in test_portfolio.py, since it's pure data validation with
no agent involved at all.
"""

from __future__ import annotations

import anyio
import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from mybroker.agents import schema_resolver
from mybroker.agents.schema_resolver import ResolvedSchema, _parse_response


class TestParseResponse:
    def test_parses_a_bare_json_object(self):
        text = '{"header_row": 0, "kind": "equity", "columns": {"symbol": 0}, "confidence": "high"}'
        data = _parse_response(text)
        assert data == {
            "header_row": 0, "kind": "equity", "columns": {"symbol": 0}, "confidence": "high",
        }

    def test_strips_a_markdown_fence(self):
        text = '```json\n{"header_row": 3, "kind": "mf", "columns": {}}\n```'
        assert _parse_response(text) == {"header_row": 3, "kind": "mf", "columns": {}}

    def test_extracts_a_fenced_object_with_prose_before_and_after(self):
        text = (
            "Let me look at these rows carefully.\n\n"
            '```json\n{"header_row": 1, "kind": "equity", "columns": {"symbol": 1}}\n```\n\n'
            "That's my best read of it."
        )
        assert _parse_response(text) == {
            "header_row": 1, "kind": "equity", "columns": {"symbol": 1},
        }

    def test_extracts_a_bare_object_embedded_in_prose_with_no_fence(self):
        text = 'Here is my answer: {"header_row": 0, "kind": "mf", "columns": {}} — done.'
        assert _parse_response(text) == {"header_row": 0, "kind": "mf", "columns": {}}

    def test_raises_on_a_non_object_response(self):
        with pytest.raises(ValueError, match="object"):
            _parse_response('["kind", "equity"]')

    def test_raises_on_unparseable_text(self):
        with pytest.raises(ValueError, match="no JSON object"):
            _parse_response("not json at all, no braces either")


class TestResolveSchemaPlumbing:
    @staticmethod
    def _fake_query_returning(text):
        async def _fake(*, prompt, options):
            yield AssistantMessage(content=[TextBlock(text=text)], model="test-model")
            yield ResultMessage(
                subtype="success", duration_ms=100, duration_api_ms=90,
                is_error=False, num_turns=1, session_id="test", total_cost_usd=0.001,
            )
        return _fake

    _EXCERPT = [
        ["Name", "Pankaj"], ["", ""],
        ["Scheme Name", "AMC", "Category", "Folio", "Invested", "Current"],
        ["Fund A", "HDFC", "Equity", "123", "1000", "1100"],
    ]

    def test_returns_the_parsed_mapping(self, monkeypatch):
        monkeypatch.setattr(
            schema_resolver, "query",
            self._fake_query_returning(
                '{"header_row": 2, "kind": "mf", "columns": {"scheme_name": 0, '
                '"invested": 4, "current_value": 5}, "confidence": "high", '
                '"reasoning": "clear"}'
            ),
        )

        result = anyio.run(schema_resolver.resolve_schema, self._EXCERPT)

        assert result == ResolvedSchema(
            header_row=2, kind="mf",
            columns={"scheme_name": 0, "invested": 4, "current_value": 5},
            confidence="high", reasoning="clear",
        )

    def test_finds_the_header_row_past_leading_junk(self, monkeypatch):
        """The actual point of the excerpt-based interface: the agent
        isn't told where the header is — account metadata rows before
        the real table (row 0-1 here) must not confuse it."""
        monkeypatch.setattr(
            schema_resolver, "query",
            self._fake_query_returning(
                '{"header_row": 2, "kind": "mf", "columns": {"scheme_name": 0}, '
                '"confidence": "high"}'
            ),
        )

        result = anyio.run(schema_resolver.resolve_schema, self._EXCERPT)

        assert result.header_row == 2

    def test_no_table_found_returns_null_header_row(self, monkeypatch):
        monkeypatch.setattr(
            schema_resolver, "query",
            self._fake_query_returning(
                '{"header_row": null, "kind": null, "columns": {}, '
                '"confidence": "low", "reasoning": "no holdings table here"}'
            ),
        )

        result = anyio.run(schema_resolver.resolve_schema, [["random", "text"]])

        assert result.header_row is None
        assert result.kind is None

    def test_non_integer_header_row_becomes_none(self, monkeypatch):
        """Same fail-closed posture as everywhere else — a claim that
        isn't even a real row index is not trusted or coerced."""
        monkeypatch.setattr(
            schema_resolver, "query",
            self._fake_query_returning(
                '{"header_row": "row two", "kind": "mf", "columns": {}, '
                '"confidence": "high"}'
            ),
        )

        result = anyio.run(schema_resolver.resolve_schema, self._EXCERPT)

        assert result.header_row is None

    def test_unrecognised_kind_becomes_none(self, monkeypatch):
        """The agent claiming something other than 'equity'/'mf' must not
        be trusted at face value — validate_schema rejects kind=None
        outright, same fail-closed posture as everywhere else."""
        monkeypatch.setattr(
            schema_resolver, "query",
            self._fake_query_returning(
                '{"header_row": 0, "kind": "bonds", "columns": {}, "confidence": "high"}'
            ),
        )

        result = anyio.run(schema_resolver.resolve_schema, [["A"], ["1"]])

        assert result.kind is None

    def test_invalid_confidence_normalizes_to_low(self, monkeypatch):
        monkeypatch.setattr(
            schema_resolver, "query",
            self._fake_query_returning(
                '{"header_row": 0, "kind": "equity", "columns": {}, '
                '"confidence": "very sure!!"}'
            ),
        )

        result = anyio.run(schema_resolver.resolve_schema, [["A"], ["1"]])

        assert result.confidence == "low"

    def test_non_integer_column_values_are_dropped(self, monkeypatch):
        """A claimed column index must be a real index (or null) — a
        string, float, or anything else the agent might emit is not
        trusted, not coerced."""
        monkeypatch.setattr(
            schema_resolver, "query",
            self._fake_query_returning(
                '{"header_row": 0, "kind": "equity", "columns": {"symbol": 0, '
                '"quantity": "two", "avg_cost": null}, "confidence": "high"}'
            ),
        )

        result = anyio.run(schema_resolver.resolve_schema, [["A", "B"], ["1", "2"]])

        assert result.columns == {"symbol": 0, "avg_cost": None}

    def test_raises_on_malformed_agent_response(self, monkeypatch):
        """cli.py is the one that catches this and leaves the file
        unparseable — this function's job is only to try, not to
        swallow its own failures."""
        monkeypatch.setattr(
            schema_resolver, "query", self._fake_query_returning("not json"),
        )

        with pytest.raises(Exception):  # noqa: B017
            anyio.run(schema_resolver.resolve_schema, [["A"], ["1"]])

    def test_raises_on_empty_response(self, monkeypatch):
        async def _empty(*, prompt, options):
            yield ResultMessage(
                subtype="success", duration_ms=10, duration_api_ms=9,
                is_error=False, num_turns=1, session_id="test", total_cost_usd=0.0,
            )
            return
            yield  # pragma: no cover - makes this an async generator

        monkeypatch.setattr(schema_resolver, "query", _empty)

        with pytest.raises(RuntimeError, match="no text response"):
            anyio.run(schema_resolver.resolve_schema, [["A"], ["1"]])

"""M7 agent-assisted ticker resolution (agents/ticker_resolver.py).

The core thing under test is the validation gate: a claimed symbol must
appear in that specific name's own recorded search results, or it's
rejected regardless of confidence — the same "a claim must trace to a
real tool call" discipline tools/server.py's provenance validator already
enforces for report recommendations, applied here directly.

resolve_names() itself is exercised against a fake `query()` (same pattern
as test_orchestrator_progress.py) purely for its own plumbing — prompt
construction, response parsing, and the "every input name gets a result"
guarantee. A fake query() never actually dispatches the real
search_ticker_by_name tool the way the SDK would in a live run, so no
evidence is ever recorded in that setup; that path exercises the
validator's honest fail-closed behaviour (nothing recorded → nothing
trusted), while the validator's actual accept-path is tested directly
against a controlled evidence dict below.
"""

from __future__ import annotations

import anyio
import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from mybroker.agents import ticker_resolver
from mybroker.agents.ticker_resolver import ResolvedName, _parse_response, _validate


class TestValidate:
    def test_accepts_a_claim_backed_by_real_evidence(self):
        evidence = {"AXIS BANK LIMITED": [
            {"symbol": "AXISBANK.NS", "company_name": "Axis Bank Limited", "sector": "Financial Services"},
        ]}
        claims = [{"name": "AXIS BANK LIMITED", "symbol": "AXISBANK.NS",
                   "confidence": "high", "reasoning": "Exact match."}]

        resolved = _validate(claims, evidence)

        assert resolved == [ResolvedName(
            name="AXIS BANK LIMITED", symbol="AXISBANK.NS", confidence="high",
            reasoning="Exact match.", duplicate_of=None,
            sector="Financial Services", company_name="Axis Bank Limited",
        )]

    def test_rejects_a_symbol_not_in_that_names_own_evidence(self):
        """The core guard: a hallucinated or cross-contaminated symbol
        (borrowed from a different name's results, say) must never pass
        just because it sounds plausible."""
        evidence = {"AXIS BANK LIMITED": [{"symbol": "AXISBANK.NS", "company_name": "Axis Bank"}]}
        claims = [{"name": "AXIS BANK LIMITED", "symbol": "SOMETHING.NS",
                   "confidence": "high", "reasoning": "Trust me."}]

        resolved = _validate(claims, evidence)

        assert resolved[0].symbol is None
        assert resolved[0].confidence == "low"
        assert "rejected" in resolved[0].reasoning.lower()

    def test_rejects_when_no_evidence_was_ever_recorded_for_the_name(self):
        """Fail-closed: a name search_ticker_by_name was never actually
        called for (or that returned nothing) has nothing to validate
        against, so any claimed symbol is rejected."""
        claims = [{"name": "GHOST CO", "symbol": "GHOST.NS", "confidence": "high"}]

        resolved = _validate(claims, evidence={})

        assert resolved[0].symbol is None
        assert resolved[0].confidence == "low"

    def test_null_symbol_claim_passes_through_without_rejection(self):
        """The agent honestly saying "no match" (symbol=None) is not a
        rejection case — it's already the right answer, just pass it
        through with whatever confidence/reasoning it gave."""
        claims = [{"name": "ZOMATO", "symbol": None, "confidence": "low",
                   "reasoning": "No result — possibly renamed."}]

        resolved = _validate(claims, evidence={"ZOMATO": []})

        assert resolved[0].symbol is None
        assert resolved[0].reasoning == "No result — possibly renamed."

    def test_normalizes_an_invalid_confidence_value_to_low(self):
        evidence = {"CO": [{"symbol": "CO.NS"}]}
        claims = [{"name": "CO", "symbol": "CO.NS", "confidence": "very sure!!"}]

        resolved = _validate(claims, evidence)

        assert resolved[0].confidence == "low"

    def test_carries_duplicate_of_through(self):
        evidence = {"NTPC LTD": [{"symbol": "NTPC.NS"}]}
        claims = [{"name": "NTPC LTD", "symbol": "NTPC.NS", "confidence": "high",
                   "duplicate_of": "NTPC LIMITED"}]

        resolved = _validate(claims, evidence)

        assert resolved[0].duplicate_of == "NTPC LIMITED"


class TestParseResponse:
    def test_parses_a_bare_json_array(self):
        text = '[{"name": "A", "symbol": "A.NS"}]'
        assert _parse_response(text) == [{"name": "A", "symbol": "A.NS"}]

    def test_strips_a_markdown_fence(self):
        text = '```json\n[{"name": "A", "symbol": "A.NS"}]\n```'
        assert _parse_response(text) == [{"name": "A", "symbol": "A.NS"}]

    def test_extracts_a_fenced_array_with_prose_before_and_after(self):
        """Real-world regression: for a batch involving duplicate
        reasoning, the model reliably narrates first ("I'll start by...",
        "Now let me analyze...") and appends a markdown summary table
        after the fence, despite the system prompt asking for ONLY the
        array — this is exactly the response that crashed with
        `JSONDecodeError: Expecting value: line 1 column 1` before this
        function looked past position 0 for the actual JSON."""
        text = (
            "I'll start by loading the search tool schema.\n\n"
            "Now let me analyze the duplicate signals before the final output.\n\n"
            '```json\n[{"name": "A", "symbol": "A.NS", "confidence": "high"}]\n```\n\n'
            "### Summary table\n\n| Input | Symbol |\n|---|---|\n| A | A.NS |\n"
        )
        assert _parse_response(text) == [{"name": "A", "symbol": "A.NS", "confidence": "high"}]

    def test_extracts_a_bare_array_embedded_in_prose_with_no_fence(self):
        text = 'Here is my analysis: [{"name": "A", "symbol": "A.NS"}] — done.'
        assert _parse_response(text) == [{"name": "A", "symbol": "A.NS"}]

    def test_raises_on_a_non_array_response(self):
        with pytest.raises(ValueError, match="array"):
            _parse_response('{"name": "A"}')

    def test_raises_on_unparseable_text(self):
        with pytest.raises(ValueError, match="no JSON array"):
            _parse_response("not json at all, no brackets either")


class TestResolveNamesPlumbing:
    """resolve_names' own wiring against a fake query() — see this file's
    module docstring for why the validator's accept-path is tested
    directly (above) rather than through this fake."""

    @staticmethod
    def _fake_query_returning(text):
        async def _fake(*, prompt, options):
            yield AssistantMessage(content=[TextBlock(text=text)], model="test-model")
            yield ResultMessage(
                subtype="success", duration_ms=100, duration_api_ms=90,
                is_error=False, num_turns=1, session_id="test", total_cost_usd=0.001,
            )
        return _fake

    def test_every_input_name_gets_a_result(self, monkeypatch):
        """A fake query() never actually runs the real tool, so nothing
        can validate — but every name asked about must still come back
        with *some* result, never silently dropped."""
        monkeypatch.setattr(
            ticker_resolver, "query",
            self._fake_query_returning('[{"name": "A", "symbol": "A.NS", "confidence": "high"}]'),
        )

        holdings = [{"name": "A", "quantity": 1, "avg_cost": 1}, {"name": "B", "quantity": 2, "avg_cost": 2}]
        resolved = anyio.run(ticker_resolver.resolve_names, holdings)

        assert {r.name for r in resolved} == {"A", "B"}
        # "A" was claimed but never backed by a real recorded tool result
        # in this fake setup — fail-closed, not trusted.
        assert all(r.symbol is None for r in resolved)

    def test_raises_on_malformed_agent_response(self, monkeypatch):
        """cli.py is the one that catches this and falls back to the
        plain suggestion path — this function's job is only to try, not
        to swallow its own failures."""
        monkeypatch.setattr(
            ticker_resolver, "query", self._fake_query_returning("not json"),
        )

        with pytest.raises(Exception):  # noqa: B017
            anyio.run(ticker_resolver.resolve_names, [{"name": "A", "quantity": 1, "avg_cost": 1}])

    def test_candidate_symbol_and_row_data_survive_onto_every_result(self, monkeypatch):
        """candidate_symbol and row_data (see importers.py's discover_
        unmapped_full_names) are INPUT fields, not something the agent's
        own JSON output carries — resolve_names must carry both onto the
        result regardless of whether the agent resolved that holding,
        dropped it from its response, or (in this fake setup) never got
        backed by real evidence at all. A caller's interactive fallback
        (cli.py's _confirm_unmapped) depends on both still being there in
        exactly the cases where the agent came back empty."""
        monkeypatch.setattr(
            ticker_resolver, "query",
            self._fake_query_returning('[{"name": "A", "symbol": "A.NS", "confidence": "high"}]'),
        )

        holdings = [
            {
                "name": "A", "quantity": 1, "avg_cost": 1, "candidate_symbol": "ACORP",
                "row_data": {"Scrip Code": "A CORP LTD", "Code": "ACORP"},
            },
            {"name": "B", "quantity": 2, "avg_cost": 2},  # no hint, dropped by the agent too
        ]
        resolved = anyio.run(ticker_resolver.resolve_names, holdings)

        by_name = {r.name: r for r in resolved}
        assert by_name["A"].row_data == {"Scrip Code": "A CORP LTD", "Code": "ACORP"}
        assert by_name["B"].row_data is None
        assert by_name["A"].candidate_symbol == "ACORP"
        assert by_name["B"].candidate_symbol is None

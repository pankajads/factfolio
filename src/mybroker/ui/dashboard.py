"""M4 — Streamlit dashboard, plus the chat tab (M5-era addition).

The Overview tab is deterministic: loads `holdings.csv` (+ optional
`holdings_mf.csv`, + anything in `holdings_inbox/`) through
`portfolio/loader.py`, computes the snapshot via `portfolio/metrics.py`, and
checks it against `memory/investment_policy.md` via `portfolio/policy.py`.
No LLM calls, no network — a read-only view of the same numbers the CLI and
the agent report cite, just visual.

The Chat tab embeds the exact same chat engine as `mybroker chat`
(agents/chat.py) — same system prompt, same tools, same exclusion of
`log_recommendation`. It needs ANTHROPIC_API_KEY and does make live calls;
the Overview tab works without either.

Run with `mybroker dashboard` (wraps `streamlit run` on this file).
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import date

import plotly.graph_objects as go
import streamlit as st

from mybroker.config import REPORTS_DIR
from mybroker.portfolio.loader import load_portfolio
from mybroker.portfolio.metrics import snapshot
from mybroker.portfolio.policy import Policy

# ── Palette (see docs/MILESTONES.md M4 — dataviz skill reference instance) ──
# Sequential magnitude: single blue hue, light→dark.
SEQ_BLUE = "#2a78d6"
# Gain/loss: the validated blue↔red diverging pair, not green/red — green/red
# fails the CVD gate outright (ΔE 4.1 deutan, below even the secondary-encoding
# floor), unlike ui/cli.py's ANSI colors which have no such constraint.
GAIN = "#2a78d6"
LOSS = "#d03b3b"
# Status palette (fixed, never themed) — breach severity.
SEVERITY_COLOR = {
    "critical": "#d03b3b",
    "high": "#ec835a",
    "medium": "#fab219",
    "low": "#898781",
}
MUTED = "#898781"
GRID = "#e1e0d9"


def _bar_chart(labels: list[str], values: list[float], *, color: str, x_title: str) -> go.Figure:
    """One-hue horizontal bar, sorted, thin marks, hairline recessive grid."""
    fig = go.Figure(
        go.Bar(
            x=values,
            y=labels,
            orientation="h",
            marker_color=color,
            hovertemplate="%{y}: %{x:.2f}%<extra></extra>",
        )
    )
    fig.update_layout(
        xaxis_title=x_title,
        yaxis=dict(autorange="reversed", gridcolor=GRID),
        xaxis=dict(gridcolor=GRID, zeroline=False),
        plot_bgcolor="#fcfcfb",
        paper_bgcolor="#fcfcfb",
        margin=dict(l=0, r=10, t=10, b=10),
        height=max(220, 32 * len(labels) + 60),
        font_color="#0b0b0b",
    )
    return fig


def _diverging_pnl_chart(labels: list[str], values: list[float]) -> go.Figure:
    colors = [GAIN if v >= 0 else LOSS for v in values]
    fig = go.Figure(
        go.Bar(
            x=values,
            y=labels,
            orientation="h",
            marker_color=colors,
            hovertemplate="%{y}: %{x:+.2f}%<extra></extra>",
        )
    )
    fig.update_layout(
        xaxis_title="P&L %",
        yaxis=dict(autorange="reversed", gridcolor=GRID),
        xaxis=dict(gridcolor=GRID, zeroline=True, zerolinecolor=MUTED, zerolinewidth=1),
        plot_bgcolor="#fcfcfb",
        paper_bgcolor="#fcfcfb",
        margin=dict(l=0, r=10, t=10, b=10),
        height=max(220, 32 * len(labels) + 60),
        font_color="#0b0b0b",
    )
    return fig


def render() -> None:
    st.set_page_config(page_title="MyBroker", page_icon="📊", layout="wide")
    st.title("📊 MyBroker")

    tab_overview, tab_chat = st.tabs(["Overview", "Chat"])
    with tab_overview:
        _render_overview_tab()
    with tab_chat:
        _render_chat_tab()


def _render_overview_tab() -> None:
    st.caption("Deterministic snapshot — no LLM, no network. Same engine as `mybroker status`.")

    try:
        portfolio = load_portfolio()
    except FileNotFoundError as exc:
        st.error(str(exc))
        return

    snap = snapshot(portfolio)
    pol = Policy.load()
    target, step_label = pol.current_core_target()
    breaches = pol.check(snap)

    for w in snap.warnings:
        st.warning(w)

    # ── Headline stat tiles ──────────────────────────────────────────────────
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Current value", f"₹{snap.total_value:,.0f}")
    c2.metric("Invested", f"₹{snap.total_invested:,.0f}")
    c3.metric(
        "P&L", f"₹{snap.total_pnl:,.0f}",
        delta=f"{snap.total_pnl_pct:+.2f}%",
    )
    c4.metric("Positions", f"{snap.position_concentration.n_positions}")

    c5, c6, c7, c8 = st.columns(4)
    c5.metric(
        "Core", f"{snap.core_pct:.1f}%",
        delta=f"{snap.core_pct - target:+.1f} pts vs {step_label} target ({target:.0f}%)",
    )
    c6.metric("Satellite", f"{snap.satellite_pct:.1f}%")
    c7.metric(
        "Position HHI", f"{snap.position_concentration.hhi:.0f}",
        delta=snap.position_concentration.verdict, delta_color="off",
    )
    c8.metric(
        "Sector HHI", f"{snap.sector_concentration.hhi:.0f}",
        delta=snap.sector_concentration.verdict, delta_color="off",
    )

    st.divider()

    # ── Allocation ────────────────────────────────────────────────────────────
    col_l, col_r = st.columns(2)

    with col_l:
        st.subheader("Sector allocation")
        sec_labels = [w.key for w in snap.sectors]
        sec_values = [w.weight_pct for w in snap.sectors]
        st.plotly_chart(
            _bar_chart(sec_labels, sec_values, color=SEQ_BLUE, x_title="Weight %"),
            width="stretch",
        )
        over_cap = [w.key for w in snap.sectors if w.weight_pct > pol.max_sector_pct]
        if over_cap:
            st.caption(f"⚠️ Over the {pol.max_sector_pct:.0f}% sector cap: {', '.join(over_cap)}")
        with st.expander("Show as table"):
            st.dataframe(
                {"Sector": sec_labels, "Weight %": [round(v, 2) for v in sec_values]},
                hide_index=True, width="stretch",
            )

    with col_r:
        st.subheader("Position weights")
        pos_labels = [w.key for w in snap.positions]
        pos_values = [w.weight_pct for w in snap.positions]
        st.plotly_chart(
            _bar_chart(pos_labels, pos_values, color=SEQ_BLUE, x_title="Weight %"),
            width="stretch",
        )
        with st.expander("Show as table"):
            st.dataframe(
                {
                    "Symbol": pos_labels,
                    "Weight %": [round(v, 2) for v in pos_values],
                    "Sector": [w.sector for w in snap.positions],
                    "Bucket": [w.bucket for w in snap.positions],
                },
                hide_index=True, width="stretch",
            )

    st.divider()

    # ── P&L ──────────────────────────────────────────────────────────────────
    st.subheader("Position P&L")
    pnl_labels = [w.key for w in snap.positions]
    pnl_values = [w.pnl_pct for w in snap.positions]
    st.plotly_chart(
        _diverging_pnl_chart(pnl_labels, pnl_values),
        width="stretch",
    )
    with st.expander("Show as table"):
        st.dataframe(
            {
                "Symbol": pnl_labels,
                "P&L %": [round(v, 2) for v in pnl_values],
                "P&L ₹": [round(w.pnl, 2) for w in snap.positions],
            },
            hide_index=True, width="stretch",
        )

    st.divider()

    # ── Policy compliance ────────────────────────────────────────────────────
    st.subheader(f"Policy compliance — {len(breaches)} breach{'es' if len(breaches) != 1 else ''}")
    if not breaches:
        st.success("Fully compliant.")
    else:
        icon = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "⚪"}
        rows = [
            {
                "": icon.get(b.severity, ""),
                "Severity": b.severity,
                "Rule": b.rule,
                "Subject": b.subject,
                "Actual": f"{b.actual:.2f}%",
                "Limit": f"{b.limit:.2f}%",
                "Excess": f"{b.excess:+.2f}",
            }
            for b in breaches
        ]
        st.dataframe(rows, hide_index=True, width="stretch")
        with st.expander("Full breach messages"):
            for b in breaches:
                st.markdown(
                    f"**{icon.get(b.severity, '')} {b.severity.upper()} — {b.rule} "
                    f"({b.subject})**  \n{b.message}"
                )

    st.divider()

    # ── Latest report ────────────────────────────────────────────────────────
    st.subheader("Reports")
    report_files = sorted(REPORTS_DIR.glob("*.md"), reverse=True) if REPORTS_DIR.exists() else []
    if not report_files:
        st.info("No reports yet — run `mybroker report` to generate one.")
    else:
        names = [f.stem for f in report_files]
        picked = st.selectbox("Select a report", names, index=0)
        chosen = report_files[names.index(picked)]
        with st.expander(f"{picked}.md", expanded=(picked == names[0])):
            st.markdown(chosen.read_text(encoding="utf-8"))

    st.caption(f"Generated {date.today().isoformat()} · portfolio read directly from holdings.csv")


# ── Chat tab ─────────────────────────────────────────────────────────────────
# Streamlit reruns the whole script top-to-bottom on every interaction, but
# agents/chat.py's ClaudeSDKClient wants ONE persistent connection across a
# multi-turn conversation. The fix: a background thread running its own
# asyncio event loop, held in st.session_state so it survives reruns within
# one browser session; each turn is dispatched onto it with
# run_coroutine_threadsafe and awaited synchronously from Streamlit's
# (synchronous) script execution.
def _get_chat_loop() -> asyncio.AbstractEventLoop:
    if "chat_loop" not in st.session_state:
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        st.session_state.chat_loop = loop
        st.session_state.chat_thread = thread
    return st.session_state.chat_loop


def _get_chat_client(loop: asyncio.AbstractEventLoop):
    if "chat_client" in st.session_state:
        return st.session_state.chat_client

    from claude_agent_sdk import ClaudeSDKClient

    from mybroker.agents.chat import build_options

    run_id = f"chat-dash-{uuid.uuid4().hex[:8]}"
    client = ClaudeSDKClient(options=build_options(run_id))
    asyncio.run_coroutine_threadsafe(client.connect(), loop).result(timeout=60)
    st.session_state.chat_client = client
    return client


def _send_chat_message(client, loop: asyncio.AbstractEventLoop, text: str) -> tuple[str, float | None]:
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    async def _turn() -> tuple[str, float | None]:
        await client.query(text)
        chunks: list[str] = []
        cost: float | None = None
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        chunks.append(block.text)
            elif isinstance(message, ResultMessage):
                cost = message.total_cost_usd
        return "".join(chunks), cost

    return asyncio.run_coroutine_threadsafe(_turn(), loop).result(timeout=180)


def _render_chat_tab() -> None:
    import os

    auth = (
        "ANTHROPIC_API_KEY (env var override)"
        if os.environ.get("ANTHROPIC_API_KEY")
        else "local `claude login` session"
    )
    st.caption(
        "Same engine as `mybroker chat` — one agent, direct tool access, no "
        "subagent roster, no `log_recommendation` (formal recommendations "
        f"still go through `mybroker report`). Makes live LLM calls — auth: {auth}."
    )

    if "chat_messages" not in st.session_state:
        st.session_state.chat_messages = []

    for msg in st.session_state.chat_messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["text"])

    prompt = st.chat_input("Ask about your portfolio…")
    if not prompt:
        return

    st.session_state.chat_messages.append({"role": "user", "text": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        with st.spinner("thinking…"):
            try:
                loop = _get_chat_loop()
                client = _get_chat_client(loop)
                reply, cost = _send_chat_message(client, loop, prompt)
            except Exception as exc:
                from mybroker.errors import friendly_message, log_error

                log_file = log_error("dashboard chat tab", exc)
                reply = f"⚠️ {friendly_message(exc)}"
                if log_file:
                    reply += f"\n\n*Full traceback: `{log_file}`*"
                cost = None
        st.markdown(reply or "_(no response)_")
        if cost:
            st.caption(f"${cost:.4f}")

    st.session_state.chat_messages.append({"role": "assistant", "text": reply})


if __name__ == "__main__":
    render()

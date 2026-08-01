"""The per-pattern report.

Gross and net sit side by side with the cost drag as its own line, because they answer
different questions and conflating them hides both:

    GROSS  does this pattern have any edge at all?
    NET    would trading it have made money?
    DRAG   what the fee tier costs you, as a visible number rather than a silent one

On BTC 5m at the base Coinbase tier the drag is around -19R against a gross edge near
0.001R. If only net were shown, every pattern would print a large negative number and
the comparison between patterns -- which is the actual research question -- would be
drowned by a constant.

The verdict is three-way for the same reason. "No edge" and "real edge that costs more
than it makes" are different findings and call for different responses.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.console import Console
from rich.table import Table
from rich.text import Text

from .baseline import BaselineResult
from .stats import Stats

VERDICT_NO_EDGE = "NO DEMONSTRATED EDGE"
VERDICT_EDGE_UNTRADEABLE = "EDGE, BUT COSTS EXCEED IT"
VERDICT_TRADEABLE = "TRADEABLE EDGE"
VERDICT_INSUFFICIENT = "INSUFFICIENT SAMPLE"

_STYLE = {
    VERDICT_NO_EDGE: "yellow",
    VERDICT_EDGE_UNTRADEABLE: "cyan",
    VERDICT_TRADEABLE: "bold green",
    VERDICT_INSUFFICIENT: "dim",
}


@dataclass
class PatternReport:
    pattern: str
    symbol: str
    timeframe: str
    direction: str
    stop_atr: float
    target_r: float
    in_sample: Stats
    out_sample: Stats
    in_sample_gross: Stats | None
    gross_in: float | None
    gross_out: float | None
    drag_in: float | None
    baseline: BaselineResult | None
    signals: int
    trades: int
    slices: list[tuple[str, int, float | None, float | None]]
    round_trip_bps: float

    #: Set by apply_multiple_testing_correction(). None until then.
    survives_correction: bool | None = None
    bh_threshold: float | None = None

    @property
    def verdict(self) -> str:
        if not self.in_sample.shown:
            return VERDICT_INSUFFICIENT
        if self.baseline is None or not self.baseline.beats_random:
            return VERDICT_NO_EDGE
        # A raw p < 0.05 across 20 patterns is exactly what chance produces about once.
        # If the correction rejects it, it is not a finding.
        if self.survives_correction is False:
            return VERDICT_NO_EDGE
        if (self.in_sample.expectancy_r or 0) <= 0:
            return VERDICT_EDGE_UNTRADEABLE
        return VERDICT_TRADEABLE


def apply_multiple_testing_correction(
    reports: list[PatternReport], *, fdr: float = 0.05
) -> int:
    """Benjamini-Hochberg across every pattern tested on this series.

    Testing 20 patterns at alpha=0.05 produces roughly one p < 0.05 by chance alone.
    Reporting that as an edge is precisely the failure this project exists to prevent --
    it is how a random number becomes a trading rule.

    BH controls the false discovery rate: sort p ascending, find the largest k where
    p_k <= (k/m) * fdr, and reject everything up to it. Returns how many survived.
    """
    scored = [r for r in reports if r.baseline is not None]
    m = len(scored)
    if m == 0:
        return 0

    ordered = sorted(scored, key=lambda r: r.baseline.p_value)
    largest_k = 0
    for k, rep in enumerate(ordered, start=1):
        if rep.baseline.p_value <= (k / m) * fdr:
            largest_k = k

    for k, rep in enumerate(ordered, start=1):
        rep.bh_threshold = (k / m) * fdr
        rep.survives_correction = k <= largest_k
    for rep in reports:
        if rep.baseline is None:
            rep.survives_correction = None
    return largest_k


def _r(v: float | None, digits: int = 3) -> str:
    return "—" if v is None else f"{v:+.{digits}f}R"


def render(console: Console, reports: list[PatternReport]) -> None:
    if not reports:
        console.print("[dim]no patterns evaluated[/dim]")
        return

    head = reports[0]
    console.print(
        f"[bold cyan]{head.symbol}[/bold cyan] {head.timeframe} · "
        f"{head.stop_atr:g}×ATR stop · {head.target_r:g}R target · "
        f"round trip {head.round_trip_bps:.0f} bps"
    )
    console.print(
        "[dim]gross = does the pattern have edge · net = would it have made money · "
        "drag = what the fee tier costs[/dim]\n"
    )

    table = Table(header_style="bold", box=None, pad_edge=False)
    table.add_column("pattern")
    table.add_column("dir", justify="center")
    table.add_column("n", justify="right")
    table.add_column("win\n(gross)", justify="right")
    table.add_column("gross", justify="right")
    table.add_column("random", justify="right")
    table.add_column("p", justify="right")
    table.add_column("drag", justify="right")
    table.add_column("net", justify="right")
    table.add_column("verdict")

    # Most promising first: by gross expectancy, since that is the research question.
    for rep in sorted(reports, key=lambda r: -(r.gross_in or -9e9)):
        s = rep.in_sample
        if not s.shown:
            table.add_row(
                rep.pattern, rep.direction[:1].upper(), f"{s.n:,}",
                "—", "—", "—", "—", "—", "—",
                Text(VERDICT_INSUFFICIENT, style="dim"),
            )
            continue
        b = rep.baseline
        p_txt = "—" if b is None else f"{b.p_value:.3f}"
        verdict = rep.verdict
        gross_style = "green" if (rep.gross_in or 0) > 0 else "red"
        table.add_row(
            rep.pattern,
            rep.direction[:1].upper(),
            f"{s.n:,}" + ("*" if s.reliability == "PROVISIONAL" else ""),
            f"{rep.in_sample_gross.win_rate:.1%}" if rep.in_sample_gross and rep.in_sample_gross.win_rate is not None else "—",
            Text(_r(rep.gross_in, 4), style=gross_style),
            _r(b.mean_expectancy, 4) if b else "—",
            p_txt,
            Text(_r(rep.drag_in, 2), style="red"),
            _r(s.expectancy_r, 2),
            Text(verdict, style=_STYLE.get(verdict, "")),
        )
    console.print(table)
    scored = [r for r in reports if r.baseline is not None]
    raw_hits = sum(1 for r in scored if r.baseline.p_value < 0.05)
    survivors = sum(1 for r in scored if r.survives_correction)
    console.print(
        "[dim]* provisional (n < 100). Patterns with n < 30 show no statistics at all.[/dim]"
    )
    if scored:
        console.print(
            f"[dim]{len(scored)} patterns tested · {raw_hits} with raw p < 0.05 "
            f"(chance alone produces ~{0.05*len(scored):.1f}) · "
            f"{survivors} survive Benjamini-Hochberg at FDR 5%[/dim]"
        )


def render_detail(console: Console, rep: PatternReport) -> None:
    """Full block for one pattern, including out-of-sample and the context slices."""
    verdict = rep.verdict
    console.print(
        f"\n[bold]{rep.pattern}[/bold] · {rep.symbol} {rep.timeframe} · {rep.direction}"
    )
    console.print(f"  VERDICT: ", end="")
    console.print(Text(verdict, style=_STYLE.get(verdict, "")))

    t = Table(box=None, header_style="bold", pad_edge=False, show_header=True)
    t.add_column("window")
    t.add_column("n", justify="right")
    t.add_column("win", justify="right")
    t.add_column("gross", justify="right")
    t.add_column("net", justify="right")
    t.add_column("95% CI (net)", justify="right")
    t.add_column("PF (gross)", justify="right")
    t.add_column("max losses", justify="right")

    for label, s, gross, gs in (
        ("in-sample", rep.in_sample, rep.gross_in, rep.in_sample_gross),
        ("out-sample", rep.out_sample, rep.gross_out, None),
    ):
        if not s.shown:
            t.add_row(label, f"{s.n:,}", "—", "—", "—", "REFUSED (n<30)", "—", "—")
            continue
        ci = f"[{s.ci_low:+.2f}, {s.ci_high:+.2f}]"
        t.add_row(
            label + ("*" if s.reliability == "PROVISIONAL" else ""),
            f"{s.n:,}",
            f"{gs.win_rate:.1%}" if gs and gs.win_rate is not None else "—",
            _r(gross, 4), _r(s.expectancy_r, 2), ci,
            f"{gs.profit_factor:.2f}" if gs and gs.profit_factor is not None else "—",
            str(gs.max_consecutive_losses) if gs else "—",
        )
    console.print(t)

    if rep.baseline:
        b = rep.baseline
        console.print(
            f"  random baseline: gross {b.mean_expectancy:+.4f}R "
            f"[95% {b.null_low:+.4f}, {b.null_high:+.4f}] over {b.draws:,} "
            f"time-of-day-matched draws · p = {b.p_value:.3f}",
            style="dim",
        )
    console.print(
        f"  cost drag {rep.drag_in:+.2f}R at {rep.round_trip_bps:.0f} bps round trip",
        style="red",
    )

    if rep.slices:
        st = Table(title="by context (in-sample)", box=None, header_style="bold",
                   title_justify="left", pad_edge=False)
        st.add_column("slice")
        st.add_column("n", justify="right")
        st.add_column("gross", justify="right")
        st.add_column("net", justify="right")
        for name, n, gross, net in rep.slices:
            gate = "" if n >= 100 else " *" if n >= 30 else "  (refused)"
            if n < 30:
                st.add_row(name, f"{n:,}", "—", "—")
            else:
                st.add_row(name + gate, f"{n:,}", _r(gross, 4), _r(net, 2))
        console.print(st)

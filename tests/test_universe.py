"""Point-in-time membership: the no-lookahead property, and the real file's contents.

The assertions about specific tickers are checks against the historical record, not
against the implementation. SIVB and SBNY were removed from the index after failing in
March 2023 and FRC after failing in May 2023; if the loaded file disagrees with that,
the universe is wrong regardless of whether the code is.
"""

from __future__ import annotations

from datetime import date

import pytest

from tradedesk.universe import DEFAULT_PATH, Membership, UniverseError, load

pytestmark = pytest.mark.skipif(
    not DEFAULT_PATH.exists(),
    reason="universe/sp500_membership.csv not generated (tools/build_universe.py)",
)


@pytest.fixture(scope="module")
def members():
    return load()


def test_membership_never_looks_forward(members):
    """`as_of` takes the most recent snapshot at or before the date.

    The nearest-snapshot alternative would let a name that joins next month trade this
    month, biased precisely toward names about to perform well enough to be added.
    """
    toy = Membership({
        date(2020, 1, 31): frozenset({"A", "B"}),
        date(2020, 6, 30): frozenset({"A", "B", "C"}),
    })
    assert toy.as_of(date(2020, 3, 15)) == {"A", "B"}
    assert toy.as_of(date(2020, 6, 29)) == {"A", "B"}
    assert toy.as_of(date(2020, 6, 30)) == {"A", "B", "C"}
    # Before any snapshot we do not know the universe; declining beats guessing.
    assert toy.as_of(date(2019, 12, 1)) == frozenset()


def test_the_file_spans_the_backfill_window(members):
    days = members.dates
    assert days[0] <= date(2018, 8, 31)
    assert days[-1] >= date(2026, 6, 30)
    assert len(days) >= 90, "expected ~96 month-end snapshots"


def test_each_snapshot_holds_a_full_index(members):
    """The S&P 500 has held 500-505 names throughout. A snapshot far off that is a
    parse that half-worked -- which is exactly how company names once got read as
    tickers."""
    for when, names in members.snapshots.items():
        assert 495 <= len(names) <= 515, f"{when}: {len(names)} names"


def test_the_universe_contains_names_that_later_failed(members):
    """The entire point of the exercise.

    These four left the index by acquisition or outright failure. A universe built from
    today's constituents contains none of them, and they are exactly the positions a
    momentum strategy would have been holding into the drawdown.
    """
    everything = members.all_tickers()
    for gone in ("SIVB", "FRC", "TWTR", "ATVI", "ABMD", "SBNY"):
        assert gone in everything, f"{gone} missing -- universe looks survivorship-biased"


@pytest.mark.parametrize(
    "ticker,last_present",
    [
        ("SIVB", date(2023, 2, 28)),   # Silicon Valley Bank failed March 2023
        ("SBNY", date(2023, 2, 28)),   # Signature Bank failed March 2023
        ("FRC", date(2023, 4, 30)),    # First Republic failed May 2023
        ("TWTR", date(2022, 9, 30)),   # taken private October 2022
    ],
)
def test_failed_names_leave_the_index_when_they_actually_did(members, ticker, last_present):
    """Checked against the historical record rather than against the parser.

    A membership file that kept SIVB past March 2023 would be quietly wrong in the most
    dangerous direction: it would let a strategy hold a bank that no longer existed.
    """
    tenure = members.tenure(ticker)
    assert tenure is not None, f"{ticker} never appears"
    assert tenure[1] == last_present, f"{ticker} last seen {tenure[1]}, expected {last_present}"


def test_a_name_is_not_tradable_before_it_joined(members):
    """First Republic entered the index in 2019; it must not be rankable in 2018."""
    assert "FRC" not in members.as_of(date(2018, 9, 30))
    assert "FRC" in members.as_of(date(2021, 6, 30))
    assert "FRC" not in members.as_of(date(2024, 1, 31))


def test_the_survivorship_gap_is_large_and_reported(members):
    """The number that belongs next to every cross-sectional result.

    681 names ever versus ~503 today: a today's-constituents universe would be missing
    roughly a quarter of the sample, and not a random quarter.
    """
    ever, today = members.survivorship_gap()
    assert ever > today
    missing = (ever - today) / ever
    assert 0.15 < missing < 0.45, f"{missing:.0%} missing looks wrong"


def test_all_tickers_is_the_backfill_list(members):
    """What actually gets fetched: every name that was ever a member, not just the
    survivors."""
    everything = members.all_tickers()
    latest = members.as_of(max(members.dates))
    assert everything >= latest
    assert len(everything) > len(latest)


def test_a_missing_file_says_how_to_generate_it():
    with pytest.raises(UniverseError, match="build_universe"):
        load("/nonexistent/membership.csv")


def test_an_empty_file_is_refused(tmp_path):
    """An empty universe produces zero cross-sectional signals, which looks exactly
    like a strategy that never triggered."""
    p = tmp_path / "empty.csv"
    p.write_text("# header only\nas_of,ticker,revid\n")
    with pytest.raises(UniverseError, match="no membership rows"):
        load(p)


def test_class_share_tickers_have_a_single_canonical_spelling(members):
    """Wikipedia spelled Berkshire as BRK-B for three snapshots and BRK.B for 95.

    Untreated, the universe carries both as distinct tickers. Two consequences, and the
    quiet one is the problem: two phantom symbols no provider will serve (Alpaca returns
    HTTP 400, which at least fails loudly), and a THREE-MONTH HOLE in the membership of
    two real S&P 500 names, during which the cross-sectional universe silently drops
    Berkshire and includes a ticker with no bars behind it.

    Dot is canonical because it is what the data provider accepts.
    """
    everything = members.all_tickers()
    assert "BRK.B" in everything and "BF.B" in everything
    assert "BRK-B" not in everything, "hyphen spelling survived normalisation"
    assert "BF-B" not in everything
    assert not [t for t in everything if "-" in t], "no ticker should carry a hyphen"


def test_a_class_share_name_has_no_membership_hole(members):
    """The specific damage the spelling split caused, asserted directly.

    Berkshire was in the index continuously across this window, so every snapshot from
    its first to its last must contain it. A gap means the spelling split reopened.
    """
    lo, hi = members.tenure("BRK.B")
    inside = [d for d in members.dates if lo <= d <= hi]
    missing = [d for d in inside if "BRK.B" not in members.snapshots[d]]
    assert missing == [], f"BRK.B absent from {len(missing)} snapshots: {missing[:5]}"

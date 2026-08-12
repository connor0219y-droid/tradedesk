"""Deriving swing-horizon bars from stored intraday bars.

Coinbase serves 1m/5m/15m/1h/6h/1d and nothing else, so 4h has to be built. Its 1d
candles exist but are UTC-anchored, which would silently disagree with the ET session
model everything else here uses -- and mixing two day definitions in one study is the
kind of thing that produces a result nobody can reproduce. So both 4h and 1d are
derived from stored 1h bars, by the one code path below.

TWO CHOICES WORTH ARGUING WITH, because they shape every swing number downstream:

BUCKETS ARE UTC-ANCHORED, not ET-anchored. An ET day is 23 or 25 hours twice a year,
so an ET-anchored grid produces a 3-hour or 5-hour "4h" bar at each DST transition --
which the contiguity machinery would correctly read as a gap, resetting every EMA and
ATR twice a year for no reason related to the market. UTC boundaries tile the epoch
evenly, so `gap` detection at these timeframes works exactly as it does at 5m. The ET
session anchor exists to make intraday levels (VWAP, opening range) meaningful; at
swing horizon those levels are excluded and the anchor has no work left to do.

INCOMPLETE BUCKETS ARE DROPPED, not emitted. A 4h bucket built from three 1h bars has
a high and a low that are honest as far as they go, but its OPEN is whichever sub-bar
happened to be first -- which is not the bucket's open, and the backtest enters at the
open. Dropping the bucket makes the hole visible to the gap machinery instead of
hiding a wrong price inside a plausible-looking bar. This also handles the causality
edge for free: the currently-forming bucket is necessarily incomplete, so it cannot
leak past `as_of`.
"""

from __future__ import annotations

from datetime import datetime

import duckdb
import polars as pl

from .frames import BarFrame, read_bars
from .timeutil import ET_NAME, assert_epoch_aligned, tf_ms

#: Timeframes that are not stored and must be built. Value is the source timeframe.
DERIVED_FROM: dict[str, str] = {"4h": "1h", "1d": "1h"}


class ResampleError(ValueError):
    """A requested aggregation is not a whole-number multiple of its source."""


def aggregate_bars(
    df: pl.DataFrame, *, source_tf: str, target_tf: str
) -> pl.DataFrame:
    """Roll `source_tf` bars up into `target_tf` buckets, keeping only complete ones.

    `session_date` is recomputed from the bucket's own open rather than inherited from
    any sub-bar: the ET date of a 00:00 UTC daily bucket is the PREVIOUS ET day, and
    inheriting the first sub-bar's label would be right by accident here and wrong at
    other bucket sizes.
    """
    step_src, step_tgt = tf_ms(source_tf), tf_ms(target_tf)
    if step_tgt <= step_src or step_tgt % step_src:
        raise ResampleError(
            f"cannot build {target_tf} from {source_tf}: "
            f"{step_tgt}ms is not a whole multiple of {step_src}ms"
        )
    assert_epoch_aligned(target_tf)
    per_bucket = step_tgt // step_src

    if df.is_empty():
        return df

    src = df.sort("bar_open_ms")
    out = (
        src.with_columns(
            (pl.col("bar_open_ms") // step_tgt * step_tgt).alias("bar_open_ms_bucket")
        )
        .group_by("bar_open_ms_bucket")
        .agg(
            # sort_by is explicit rather than trusting group-wise input order: an
            # open taken from the wrong sub-bar is invisible in the output and moves
            # every entry price in the backtest.
            pl.col("open").sort_by("bar_open_ms").first().alias("open"),
            pl.col("high").max().alias("high"),
            pl.col("low").min().alias("low"),
            pl.col("close").sort_by("bar_open_ms").last().alias("close"),
            pl.col("volume").sum().alias("volume"),
            pl.len().alias("n_source_bars"),
            pl.col("calendar_version").first().alias("calendar_version"),
        )
        .rename({"bar_open_ms_bucket": "bar_open_ms"})
        .filter(pl.col("n_source_bars") == per_bucket)
        .sort("bar_open_ms")
    )
    return out.with_columns(
        pl.from_epoch("bar_open_ms", time_unit="ms")
        .dt.replace_time_zone("UTC")
        .dt.convert_time_zone(ET_NAME)
        .dt.date()
        .alias("session_date"),
        pl.from_epoch("bar_open_ms", time_unit="ms")
        .dt.replace_time_zone("UTC")
        .alias("ts_utc"),
        pl.lit(target_tf).alias("timeframe"),
        pl.lit(0, dtype=pl.Int32).alias("revision"),
        pl.lit(0, dtype=pl.Int64).alias("ingested_at_ms"),
    )


def read_bars_any(
    con: duckdb.DuckDBPyConnection,
    symbol: str,
    timeframe: str,
    *,
    as_of: datetime,
    venue: str = "coinbase",
) -> BarFrame:
    """`read_bars` for stored timeframes, aggregation for derived ones.

    Same signature and the same `as_of` discipline either way, so a caller does not
    need to know which timeframes happen to be stored today.
    """
    if timeframe not in DERIVED_FROM:
        return read_bars(con, symbol, timeframe, as_of=as_of, venue=venue)

    # STORED BARS WIN OVER DERIVED ONES. Derivation exists because Coinbase's daily
    # candles are UTC-anchored and would disagree with the ET session model -- there are
    # no stored crypto 1d bars, so nothing changes there. Alpaca's daily bars ARE the
    # 09:30-16:00 RTH session, which is the correct day definition for an equity, so
    # deriving one from 1h bars would be strictly worse even if those bars existed.
    #
    # They do not. Before this check, asking for equity 1d silently returned an EMPTY
    # frame: the reader looked for 1h equity bars, found none, and returned nothing --
    # so all 26 daily detectors would have produced zero trades and reported as
    # "insufficient sample" rather than as a missing data path.
    stored = read_bars(con, symbol, timeframe, as_of=as_of, venue=venue)
    if not stored.is_empty:
        return stored

    source = DERIVED_FROM[timeframe]
    src = read_bars(con, symbol, source, as_of=as_of, venue=venue)
    if src.is_empty:
        return BarFrame(df=src.to_polars(), venue=venue, symbol=symbol,
                        timeframe=timeframe, calendar_version=src.calendar_version,
                        as_of_ms=src.as_of_ms)

    df = aggregate_bars(src.to_polars().with_columns(pl.lit(venue).alias("venue")),
                        source_tf=source, target_tf=timeframe)
    df = df.with_columns(pl.lit(venue).alias("venue"), pl.lit(symbol).alias("symbol"))
    return BarFrame(
        df=df, venue=venue, symbol=symbol, timeframe=timeframe,
        calendar_version=src.calendar_version, as_of_ms=src.as_of_ms,
    )

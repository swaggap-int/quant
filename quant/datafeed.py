"""Historical OHLCV data, with pluggable exchanges and a local cache.

Why four providers: crypto exchanges geo-block. Binance answers HTTP 451
(Unavailable For Legal Reasons) from US IPs, so a backtester hard-wired to one
venue silently breaks depending on where it runs. Every adapter here normalises
into the same ``Candles`` object, and ``provider="auto"`` probes them in order
and keeps the first that answers.

Transport is stdlib ``urllib``: no ``requests`` dependency, no API keys, no
account. Everything is cached under ``data/cache`` so a run is reproducible and
still works with the network unplugged.
"""

from __future__ import annotations

import csv
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import numpy as np

from quant.config import timeframe_ms

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"
USER_AGENT = "quant-backtest/0.1 (+https://localhost)"
MAX_RETRIES = 4
_SSL_CTX = ssl.create_default_context()


class DataError(RuntimeError):
    """Raised when a data request cannot be satisfied."""


# --------------------------------------------------------------------------- #
# Container
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Candles:
    """A contiguous, time-ordered set of OHLCV bars.

    ``ts`` is epoch milliseconds UTC, the open time of each bar.
    """

    ts: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    symbol: str = ""
    timeframe: str = "1d"
    provider: str = ""

    def __post_init__(self) -> None:
        n = self.ts.size
        for name in ("open", "high", "low", "close", "volume"):
            arr = getattr(self, name)
            if arr.shape != (n,):
                raise ValueError(f"{name} has shape {arr.shape}, expected ({n},)")
            if np.isnan(arr).any():
                raise ValueError(f"{name} contains NaN; clean the source data first")
        if n and np.any(np.diff(self.ts) <= 0):
            raise ValueError("timestamps must be strictly increasing")
        if n and np.any(self.high < self.low):
            raise ValueError("found a bar whose high is below its low")

    def __len__(self) -> int:
        return int(self.ts.size)

    def __bool__(self) -> bool:
        return self.ts.size > 0

    @property
    def bar_ms(self) -> int:
        return timeframe_ms(self.timeframe)

    def slice(self, start_ms: int | None = None, end_ms: int | None = None) -> "Candles":
        """Subset by close-time window; ``end_ms`` is exclusive."""
        lo = 0 if start_ms is None else int(np.searchsorted(self.ts, start_ms, "left"))
        hi = len(self) if end_ms is None else int(np.searchsorted(self.ts, end_ms, "left"))
        return self._take(lo, hi)

    def _take(self, lo: int, hi: int) -> "Candles":
        return Candles(
            ts=self.ts[lo:hi],
            open=self.open[lo:hi],
            high=self.high[lo:hi],
            low=self.low[lo:hi],
            close=self.close[lo:hi],
            volume=self.volume[lo:hi],
            symbol=self.symbol,
            timeframe=self.timeframe,
            provider=self.provider,
        )

    @property
    def start_iso(self) -> str:
        return _iso(self.ts[0]) if len(self) else "-"

    @property
    def end_iso(self) -> str:
        return _iso(self.ts[-1]) if len(self) else "-"

    @property
    def years(self) -> float:
        return (len(self) * self.bar_ms) / (365 * 86_400_000)

    def to_rows(self) -> list[dict[str, Any]]:
        return [
            {
                "ts": int(self.ts[i]),
                "open": float(self.open[i]),
                "high": float(self.high[i]),
                "low": float(self.low[i]),
                "close": float(self.close[i]),
                "volume": float(self.volume[i]),
            }
            for i in range(len(self))
        ]

    def to_dataframe(self):  # pragma: no cover - convenience only
        """Optional pandas view for exploration. pandas is not a dependency."""
        import pandas as pd

        df = pd.DataFrame(
            {
                "open": self.open,
                "high": self.high,
                "low": self.low,
                "close": self.close,
                "volume": self.volume,
            },
            index=pd.to_datetime(self.ts, unit="ms", utc=True),
        )
        df.index.name = "ts"
        return df


def _iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def parse_date(value: str | None, *, default: int | None = None) -> int | None:
    """Parse ``YYYY-MM-DD`` or ISO-8601 (naive treated as UTC) to epoch ms."""
    if value is None or value == "":
        return default
    text = value.strip()
    if text.isdigit() and len(text) >= 10:  # already epoch seconds/ms
        n = int(text)
        return n if n > 10_000_000_000 else n * 1000
    text = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = datetime.strptime(text, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(f"cannot parse date {value!r}; use YYYY-MM-DD or ISO-8601") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #


def _http_json(url: str, params: dict[str, Any] | None = None, *, timeout: float = 20.0) -> Any:
    """GET a JSON document with bounded exponential backoff."""
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        url = f"{url}?{urllib.parse.urlencode(clean)}"
    last: Exception | None = None
    for attempt in range(MAX_RETRIES):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last = exc
            # 4xx other than rate limiting is permanent: fail fast, do not spin.
            if exc.code in (451, 403, 404, 400) or exc.code < 500:
                if exc.code != 429:
                    raise DataError(f"HTTP {exc.code} from {url.split('?')[0]}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last = exc
        time.sleep(min(2.0 * (2**attempt), 8.0))
    raise DataError(f"request failed after {MAX_RETRIES} attempts: {url.split('?')[0]} ({last})")


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #


def _build(rows: Iterable[Sequence[float]], symbol: str, timeframe: str, provider: str) -> Candles:
    data = np.asarray(list(rows), dtype=float)
    if data.size == 0:
        return Candles(
            ts=np.empty(0, dtype=np.int64),
            open=np.empty(0),
            high=np.empty(0),
            low=np.empty(0),
            close=np.empty(0),
            volume=np.empty(0),
            symbol=symbol,
            timeframe=timeframe,
            provider=provider,
        )
    order = np.argsort(data[:, 0], kind="stable")
    data = data[order]
    ts = data[:, 0].astype(np.int64)
    keep = np.concatenate(([True], np.diff(ts) > 0))  # drop duplicate timestamps
    data, ts = data[keep], ts[keep]
    return Candles(
        ts=ts,
        open=data[:, 1].copy(),
        high=data[:, 2].copy(),
        low=data[:, 3].copy(),
        close=data[:, 4].copy(),
        volume=data[:, 5].copy(),
        symbol=symbol,
        timeframe=timeframe,
        provider=provider,
    )


class Provider:
    """Base class: adapters implement ``fetch`` and declare their mappings."""

    name = "base"
    intervals: dict[str, str] = {}
    page_limit = 1000
    page_sleep = 0.2

    def supports(self, timeframe: str) -> bool:
        return timeframe in self.intervals

    def to_symbol(self, symbol: str) -> str:
        raise NotImplementedError

    def fetch(self, symbol: str, timeframe: str, start_ms: int, end_ms: int) -> Candles:
        raise NotImplementedError

    def _require(self, timeframe: str) -> None:
        if not self.supports(timeframe):
            raise DataError(
                f"{self.name} does not serve {timeframe} bars "
                f"(supported: {', '.join(sorted(self.intervals, key=timeframe_ms))})"
            )


class BinanceProvider(Provider):
    """Binance spot klines. Mirrors the global host and the US host."""

    name = "binance"
    intervals = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d", "1w": "1w"}
    hosts = ("https://api.binance.com", "https://api.binance.us")
    page_limit = 1000

    def to_symbol(self, symbol: str) -> str:
        return symbol.replace("/", "").replace("-", "").upper()

    def _fetch_host(self, host: str, pair: str, interval: str, start_ms: int, end_ms: int) -> list[list[Any]]:
        out: list[list[Any]] = []
        cursor = start_ms
        while cursor < end_ms:
            batch = _http_json(
                f"{host}/api/v3/klines",
                {
                    "symbol": pair,
                    "interval": interval,
                    "startTime": cursor,
                    "endTime": max(end_ms - 1, cursor),
                    "limit": self.page_limit,
                },
            )
            if not batch:
                break
            # [openTime, o, h, l, c, volume, closeTime, ...]
            out.extend([[r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])] for r in batch])
            cursor = int(batch[-1][0]) + 1
            if len(batch) < self.page_limit:
                break
            time.sleep(self.page_sleep)
        return out

    def fetch(self, symbol: str, timeframe: str, start_ms: int, end_ms: int) -> Candles:
        self._require(timeframe)
        pair = self.to_symbol(symbol)
        interval = self.intervals[timeframe]
        errors = []
        for host in self.hosts:
            try:
                rows = self._fetch_host(host, pair, interval, start_ms, end_ms)
            except DataError as exc:
                errors.append(f"{host}: {exc}")
                continue
            if rows:
                return _build(rows, symbol, timeframe, self.name)
            errors.append(f"{host}: empty response")
        raise DataError(f"binance unavailable for {pair} ({'; '.join(errors)})")


class OkxProvider(Provider):
    """OKX candles. Newest-first, page backwards with the ``after`` cursor."""

    name = "okx"
    intervals = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1H", "4h": "4H", "1d": "1D", "1w": "1W"}
    page_limit = 100
    page_sleep = 0.12

    def to_symbol(self, symbol: str) -> str:
        return symbol.replace("/", "-").upper()

    def fetch(self, symbol: str, timeframe: str, start_ms: int, end_ms: int) -> Candles:
        self._require(timeframe)
        inst = self.to_symbol(symbol)
        bar = self.intervals[timeframe]
        rows: list[list[Any]] = []
        cursor: int | None = None
        for _ in range(400):  # hard page cap: 400 * 100 bars is plenty
            payload = _http_json(
                "https://www.okx.com/api/v5/market/history-candles",
                {"instId": inst, "bar": bar, "after": cursor, "limit": self.page_limit},
            )
            if payload.get("code") != "0":
                raise DataError(f"okx error {payload.get('code')}: {payload.get('msg')}")
            batch = payload.get("data") or []
            if not batch:
                break
            stop = False
            for r in batch:  # [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
                ts = int(r[0])
                if ts < start_ms:
                    stop = True
                    continue
                if ts <= end_ms:
                    rows.append([ts, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])])
            oldest = int(batch[-1][0])
            if stop or oldest <= start_ms:
                break
            cursor = oldest
            time.sleep(self.page_sleep)
        if not rows:
            raise DataError(f"okx returned no bars for {inst} {bar}")
        return _build(rows, symbol, timeframe, self.name)


class KrakenProvider(Provider):
    """Kraken public OHLC. Caps at 720 bars per call, so paginate by ``since``."""

    name = "kraken"
    intervals = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440, "1w": 10080}

    _ALIASES = {"BTC": "XBT", "XBT": "XBT", "DOGE": "XDG", "USD": "USD", "USDT": "USDT", "USDC": "USDC"}

    def to_symbol(self, symbol: str) -> str:
        parts = symbol.replace("-", "/").upper().split("/")
        if len(parts) != 2:
            raise DataError(f"kraken needs a PAIR/QUOTE symbol, got {symbol!r}")
        base, quote = (self._ALIASES.get(p, p) for p in parts)
        return f"{base}{quote}"

    def fetch(self, symbol: str, timeframe: str, start_ms: int, end_ms: int) -> Candles:
        self._require(timeframe)
        pair = self.to_symbol(symbol)
        rows: list[list[Any]] = []
        cursor = start_ms // 1000
        end_s = end_ms // 1000
        for _ in range(400):
            payload = _http_json(
                "https://api.kraken.com/0/public/OHLC",
                {"pair": pair, "interval": self.intervals[timeframe], "since": cursor},
            )
            if payload.get("error"):
                raise DataError(f"kraken error: {payload['error']}")
            result = payload.get("result") or {}
            series = [v for k, v in result.items() if k != "last"]
            if not series:
                break
            batch = series[0]
            for r in batch:  # [time_s, o, h, l, c, vwap, volume, count]
                ts = int(r[0]) * 1000
                if start_ms <= ts <= end_ms:
                    rows.append([ts, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[6])])
            last = int(result.get("last", 0))
            if not batch or last <= cursor or last > end_s:
                break
            cursor = last
            time.sleep(0.4)  # kraken public tier is rate limited
        if not rows:
            raise DataError(f"kraken returned no bars for {pair}")
        return _build(rows, symbol, timeframe, self.name)


class CoinbaseProvider(Provider):
    """Coinbase Exchange candles. 300 bars per call, newest-first, no weekly."""

    name = "coinbase"
    intervals = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 21600, "1d": 86400}
    max_window = 300

    def to_symbol(self, symbol: str) -> str:
        return symbol.replace("/", "-").upper()

    def fetch(self, symbol: str, timeframe: str, start_ms: int, end_ms: int) -> Candles:
        self._require(timeframe)
        product = self.to_symbol(symbol)
        gran = self.intervals[timeframe]
        rows: list[list[Any]] = []
        window = gran * 1000 * self.max_window
        cursor_end = end_ms
        for _ in range(400):
            cursor_start = max(start_ms, cursor_end - window)
            payload = _http_json(
                f"https://api.exchange.coinbase.com/products/{product}/candles",
                {
                    "granularity": gran,
                    "start": _iso_seconds(cursor_start),
                    "end": _iso_seconds(cursor_end),
                },
            )
            if not payload:
                break
            for r in payload:  # [time_s, low, high, open, close, volume]
                ts = int(r[0]) * 1000
                if start_ms <= ts <= end_ms:
                    rows.append([ts, float(r[3]), float(r[2]), float(r[1]), float(r[4]), float(r[5])])
            if cursor_start <= start_ms:
                break
            cursor_end = cursor_start
            time.sleep(0.34)  # coinbase public allowance is ~3 req/s
        if not rows:
            raise DataError(f"coinbase returned no bars for {product}")
        return _build(rows, symbol, timeframe, self.name)


def _iso_seconds(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


PROVIDERS: dict[str, Callable[[], Provider]] = {
    "binance": BinanceProvider,
    "okx": OkxProvider,
    "kraken": KrakenProvider,
    "coinbase": CoinbaseProvider,
}

# Probe order for provider="auto". Binance first (deepest history), then OKX,
# then Kraken, then Coinbase.
AUTO_ORDER = ("binance", "okx", "kraken", "coinbase")


def get_provider(name: str) -> Provider:
    try:
        return PROVIDERS[name]()
    except KeyError:
        raise ValueError(
            f"unknown provider {name!r}; expected 'auto' or one of {', '.join(PROVIDERS)}"
        ) from None


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #


def _cache_path(cache_dir: Path, provider: str, symbol: str, timeframe: str) -> Path:
    slug = symbol.replace("/", "-").replace(" ", "").upper()
    return cache_dir / f"{provider}_{slug}_{timeframe}.csv"


def read_cache(path: Path) -> Candles | None:
    if not path.exists():
        return None
    rows: list[list[float]] = []
    with path.open("r", newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            rows.append([float(row["ts"]), float(row["open"]), float(row["high"]),
                         float(row["low"]), float(row["close"]), float(row["volume"])])
    if not rows:
        return None
    meta = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8")) if path.with_suffix(".meta.json").exists() else {}
    return _build(rows, meta.get("symbol", ""), meta.get("timeframe", "1d"), meta.get("provider", path.stem.split("_")[0]))


def write_cache(path: Path, candles: Candles) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["ts", "open", "high", "low", "close", "volume"])
        for row in candles.to_rows():
            writer.writerow([row["ts"], row["open"], row["high"], row["low"], row["close"], row["volume"]])
    tmp.replace(path)
    path.with_suffix(".meta.json").write_text(
        json.dumps(
            {"symbol": candles.symbol, "timeframe": candles.timeframe, "provider": candles.provider},
            indent=2,
        ),
        encoding="utf-8",
    )


def merge_candles(a: Candles, b: Candles) -> Candles:
    """Union two series, newest values winning on timestamp collisions."""
    if not a:
        return b
    if not b:
        return a
    rows = {int(r["ts"]): r for r in a.to_rows()}
    rows.update({int(r["ts"]): r for r in b.to_rows()})
    ordered = [[rows[k]["ts"], rows[k]["open"], rows[k]["high"], rows[k]["low"], rows[k]["close"], rows[k]["volume"]]
               for k in sorted(rows)]
    return _build(ordered, b.symbol or a.symbol, b.timeframe, b.provider)


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def load_candles(
    symbol: str = "BTC/USDT",
    timeframe: str = "1d",
    start: str | None = None,
    end: str | None = None,
    *,
    provider: str = "auto",
    cache_dir: Path | str | None = None,
    refresh: bool = False,
    csv_path: str | Path | None = None,
    verbose: bool = True,
) -> Candles:
    """Load OHLCV bars, preferring the local cache.

    If ``csv_path`` is given the file is used directly and network is never
    touched (the file needs ``ts,open,high,low,close,volume`` columns).
    """
    if csv_path is not None:
        path = Path(csv_path)
        candles = read_cache(path)
        if candles is None:
            raise DataError(f"{path} contains no usable rows")
        candles = Candles(**{**candles.__dict__, "symbol": symbol, "timeframe": timeframe})
    else:
        cache_dir = Path(cache_dir) if cache_dir is not None else CACHE_DIR
        end_ms = parse_date(end, default=_now_ms()) or _now_ms()
        start_ms = parse_date(start, default=end_ms - 365 * 86_400_000) or 0
        if start_ms >= end_ms:
            raise ValueError("start must be before end")

        names = list(AUTO_ORDER if provider == "auto" else [provider])
        candles = Candles(
            ts=np.empty(0, dtype=np.int64),
            open=np.empty(0), high=np.empty(0), low=np.empty(0),
            close=np.empty(0), volume=np.empty(0),
            symbol=symbol, timeframe=timeframe, provider=provider,
        )
        errors: list[str] = []
        for name in names:
            path = _cache_path(cache_dir, name, symbol, timeframe)
            cached = None if refresh else read_cache(path)
            covered = bool(cached) and cached.ts[0] <= start_ms and cached.ts[-1] >= end_ms - candles.bar_ms
            if covered:
                if verbose:
                    print(f"  data: {len(cached)} bars from cache ({path.name})")
                return Candles(**{**cached.__dict__, "symbol": symbol, "timeframe": timeframe})
            try:
                prov = get_provider(name)
                fetched = prov.fetch(symbol, timeframe, start_ms, end_ms)
            except (DataError, ValueError) as exc:
                errors.append(f"{name}: {exc}")
                continue
            combined = merge_candles(cached, fetched) if cached is not None else fetched
            write_cache(path, combined)
            window = combined.slice(start_ms, end_ms + candles.bar_ms)
            if verbose:
                print(f"  data: {len(window)} bars from {name} "
                      f"({len(fetched)} fetched, {len(combined)} cached total)")
            return window
        raise DataError(
            f"no provider could serve {symbol} {timeframe}. Tried: " + " | ".join(errors or names)
        )

    window = candles.slice(
        parse_date(start) if start else None,
        (parse_date(end) + candles.bar_ms) if end else None,
    )
    if not window:
        raise DataError(f"no bars in the requested window ({start} -> {end})")
    return window


def _now_ms() -> int:
    return int(time.time() * 1000)

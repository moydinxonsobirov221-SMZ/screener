"""S.M.Z Stock Screener (Streamlit).

Founder: Sobirov.Moydinxon.Zaynobiddinvich

Layout of this file (top to bottom):
  1. CONFIG          constants, presets, column names
  2. DATA LAYER      database loading + technical indicators
  3. FILTER ENGINE   filter state and filtering functions
  4. UI HELPERS      CSS, TradingView widgets, tables, KPI tiles
  5. MARKET MAP      Finviz-style treemap
  6. VIEWS           Overview and Screener pages
  7. CHART DIALOG    full-size TradingView chart in a modal window
  8. MAIN            page assembly

Free sources only: Yahoo Finance data (stored in data.db), Plotly, TradingView free widgets.
"""
from __future__ import annotations

import html
import json
import re
import sqlite3
from typing import Callable, Optional

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

try:
    import streamlit.components.v1 as components
except Exception:  # newer Streamlit versions may drop this module
    components = None

S = st.session_state

# =============================================================================
# 1. CONFIG
# =============================================================================
DB_PATH = "data.db"
BRAND = "S.M.Z"
FOUNDER = "Sobirov.Moydinxon.Zaynobiddinvich"
INF = float("inf")

PERF = {"Any": None, "Up": (0, INF), "Up 10%": (10, INF), "Up 20%": (20, INF), "Up 50%": (50, INF),
        "Down": (-INF, 0), "Down 10%": (-INF, -10), "Down 20%": (-INF, -20), "Down 50%": (-INF, -50)}
MCAP = {"Any": None, "Nano (under $50M)": (0, 50), "Micro ($50M to $300M)": (50, 300),
        "Under $300M": (0, 300), "Small ($300M to $2B)": (300, 2000)}
FLOAT = {"Any": None, "Under 5M": (0, 5), "Under 10M": (0, 10), "Under 20M": (0, 20),
         "Under 50M": (0, 50), "Over 50M": (50, INF)}
SHORT = {"Any": None, "Over 5%": (5, INF), "Over 10%": (10, INF), "Over 20%": (20, INF)}
SMA = ["Any", "Price above", "Price below"]

# default value of every filter widget (the key is also the session_state key)
D = dict(sector=[], industry=[], price=(0.0, 10.0), chg=(-50.0, 300.0), gap=-100.0, relvol=0.0, vol=0,
         avgvol=0, atr=(0.0, 60.0), rsi=(0.0, 100.0), s20="Any", s50="Any", s200="Any", hid=-100.0,
         lod=0.0, pw="Any", pm="Any", pq="Any", mcap="Any", flt="Any", short="Any", q="")

PRESETS = {
    "- Select a preset -": {},
    "Gap & Go (gap +5%, volume 2x)": dict(gap=5.0, relvol=2.0),
    "Near 52-week high": dict(hid=-5.0, s50="Price above", relvol=1.5),
    "Oversold (RSI under 30)": dict(rsi=(0.0, 30.0)),
    "Volume surge, quiet price": dict(relvol=3.0, chg=(-3.0, 5.0)),
    "Low float runner": dict(flt="Under 20M", relvol=2.0, chg=(3.0, 300.0)),
    "Rising from lows (above SMA20)": dict(lod=30.0, s20="Price above", pm="Up"),
}

COLS = {"ticker": "Ticker", "sector": "Sector", "industry": "Industry", "mcap": "Market Cap (M)",
        "float_sh": "Float (M)", "short_pct": "Short Float %", "close": "Price", "chg": "Change %",
        "gap": "Gap %", "volume": "Volume", "avgvol": "Avg Volume", "relvol": "Rel Volume",
        "dvol": "$ Volume (M)", "atrp": "ATR %", "rsi": "RSI", "d20": "SMA20 %", "d50": "SMA50 %",
        "d200": "SMA200 %", "hid": "52W High %", "lod": "52W Low %", "pw": "Perf Week %",
        "pm": "Perf Month %", "pq": "Perf Quarter %"}
FUND_COLS = ("mcap", "float_sh", "short_pct")
MINI_COLS = ["ticker", "close", "chg", "relvol"]

FMT = {"No.": "{:d}", "Price": "{:.2f}", "Change %": "{:+.2f}", "Gap %": "{:+.2f}", "Volume": "{:,.0f}",
       "Avg Volume": "{:,.0f}", "Rel Volume": "{:.2f}", "$ Volume (M)": "{:,.2f}", "ATR %": "{:.2f}",
       "RSI": "{:.0f}", "SMA20 %": "{:+.1f}", "SMA50 %": "{:+.1f}", "SMA200 %": "{:+.1f}",
       "52W High %": "{:+.1f}", "52W Low %": "{:+.1f}", "Perf Week %": "{:+.1f}", "Perf Month %": "{:+.1f}",
       "Perf Quarter %": "{:+.1f}", "Market Cap (M)": "{:,.1f}", "Float (M)": "{:,.1f}", "Short Float %": "{:.1f}"}
COLOR_COLS = {"Change %", "Gap %", "SMA20 %", "SMA50 %", "SMA200 %", "Perf Week %", "Perf Month %",
              "Perf Quarter %"}

GREEN, RED, GREY = "#30cc5a", "#f63538", "#414554"
PALETTE = ["#f63538", "#bf4045", "#8b444e", "#414554", "#35764e", "#2f9e4f", "#30cc5a"]  # Finviz-like buckets

TV_EXCHANGES = {"NMS": "NASDAQ", "NGM": "NASDAQ", "NCM": "NASDAQ", "NAS": "NASDAQ", "NYQ": "NYSE",
                "NYS": "NYSE", "ASE": "AMEX", "PCX": "AMEX", "BTS": "AMEX", "BATS": "AMEX"}

LISTS: list[tuple[str, Callable[[pd.DataFrame], pd.DataFrame]]] = [
    ("Top Gainers", lambda d: d.nlargest(10, "chg")),
    ("Top Losers", lambda d: d.nsmallest(10, "chg")),
    ("Most Active ($ volume)", lambda d: d.nlargest(10, "dvol")),
    ("Unusual Volume", lambda d: d.nlargest(10, "relvol")),
    ("Overbought (RSI above 70)", lambda d: d[d["rsi"] > 70].nlargest(10, "rsi")),
    ("Oversold (RSI below 30)", lambda d: d[d["rsi"] < 30].nsmallest(10, "rsi")),
    ("Near 52W High", lambda d: d[d["hid"] >= -1].nlargest(10, "chg")),
    ("Near 52W Low", lambda d: d[d["lod"] <= 1].nsmallest(10, "chg")),
]

CSS = """<style>
.block-container{padding:3.5rem 1rem 1rem 1rem !important;max-width:100% !important}
div[data-testid="stVerticalBlock"]{gap:.4rem}
div[data-testid="stHorizontalBlock"]{gap:.6rem}
div[data-testid="stWidgetLabel"] p{font-size:.72rem !important;color:#9aa4b2;margin:0}
div[data-baseweb="select"] > div{min-height:30px;font-size:.8rem}
.stNumberInput input,.stTextInput input{height:30px;font-size:.8rem;padding:2px 8px}
div[data-testid="stDialog"] div[role="dialog"]{width:min(96vw,1760px) !important;max-width:96vw !important}
.smz-brand{position:fixed;top:0;left:0;z-index:1000000;background:#0b1220;padding:7px 20px 7px 16px;
  border-bottom-right-radius:10px;line-height:1.15;box-shadow:0 2px 8px rgba(0,0,0,.45)}
.smz-brand .logo{font-size:27px;font-weight:800;letter-spacing:2px;color:#fff;font-family:Arial,Helvetica,sans-serif}
.smz-brand .logo span{color:#26a69a}
.smz-brand .founder{font-size:10px;color:#9fb0c8;letter-spacing:.3px;font-family:Arial,Helvetica,sans-serif}
.kpis{display:flex;gap:6px;flex-wrap:wrap;margin:2px 0 4px 0}
.kpi{background:#161b22;border:1px solid #232a35;border-radius:6px;padding:4px 10px;min-width:98px}
.kpi span{display:block;font-size:10px;color:#8b95a5;text-transform:uppercase;letter-spacing:.4px}
.kpi b{font-size:16px;color:#e6edf3}
.bar{display:flex;height:8px;border-radius:4px;overflow:hidden;margin:2px 0 6px 0}
.legend{display:flex;gap:0;margin:4px 0 8px 0;max-width:520px}
.legend div{flex:1;text-align:center;font-size:10px;color:#fff;padding:2px 0}
.ttl{font-size:.78rem;font-weight:700;color:#cfd6e0;margin:2px 0 0 0}
.chartbar{font-size:.85rem;color:#cfd6e0;margin-bottom:4px}
</style>"""


# =============================================================================
# 2. DATA LAYER
# =============================================================================
@st.cache_data(ttl=3600, show_spinner="Calculating indicators...")
def load_data() -> tuple[pd.DataFrame, dict]:
    """Read prices/tickers/fund tables and compute all indicators for every ticker and day."""
    con = sqlite3.connect(DB_PATH)
    p = pd.read_sql("select * from prices", con)
    t = pd.read_sql("select * from tickers", con)
    try:
        f = pd.read_sql("select * from fund", con)
    except Exception:
        f = pd.DataFrame(columns=["ticker"])
    con.close()
    for col in ("mcap", "float_sh", "short_pct", "industry", "exchange"):
        if col not in f.columns:
            f[col] = np.nan
    f = f[["ticker", "mcap", "float_sh", "short_pct", "industry", "exchange"]]

    p = p.drop_duplicates(["ticker", "date"]).sort_values(["ticker", "date"]).reset_index(drop=True)
    g = p.groupby("ticker", sort=False)
    prev = g["close"].shift(1)
    p["chg"] = (p["close"] / prev - 1) * 100
    p["gap"] = (p["open"] / prev - 1) * 100
    for n in (20, 50, 200):
        sma = g["close"].transform(lambda s, n=n: s.rolling(n, min_periods=n).mean())
        p[f"sma{n}"] = sma
        p[f"d{n}"] = (p["close"] / sma - 1) * 100
    p["avgvol"] = g["volume"].transform(lambda s: s.shift(1).rolling(20, min_periods=10).mean())
    p["relvol"] = p["volume"] / p["avgvol"]
    p["dvol"] = p["close"] * p["volume"] / 1e6
    tr = pd.concat([p["high"] - p["low"], (p["high"] - prev).abs(), (p["low"] - prev).abs()], axis=1).max(axis=1)
    ewm = lambda s: s.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()  # noqa: E731
    p["atrp"] = tr.groupby(p["ticker"]).transform(ewm) / p["close"] * 100
    delta = g["close"].diff()
    up = delta.clip(lower=0).groupby(p["ticker"]).transform(ewm)
    dn = (-delta).clip(lower=0).groupby(p["ticker"]).transform(ewm)
    p["rsi"] = 100 * up / (up + dn).replace(0, np.nan)
    hi52 = g["high"].transform(lambda s: s.rolling(252, min_periods=40).max())
    lo52 = g["low"].transform(lambda s: s.rolling(252, min_periods=40).min())
    p["hid"] = (p["close"] / hi52 - 1) * 100
    p["lod"] = (p["close"] / lo52 - 1) * 100
    for name, n in (("pw", 5), ("pm", 21), ("pq", 63)):
        p[name] = (p["close"] / g["close"].shift(n) - 1) * 100

    p = p.merge(t, on="ticker", how="left").merge(f, on="ticker", how="left")
    p["mcap"] = p["mcap"] / 1e6
    p["float_sh"] = p["float_sh"] / 1e6
    meta = {"has_fund": bool(f["mcap"].notna().any()), "has_industry": bool(f["industry"].notna().any())}
    return p, meta


# =============================================================================
# 3. FILTER ENGINE
# =============================================================================
def init_state() -> None:
    for k, v in D.items():
        S.setdefault(k, list(v) if isinstance(v, list) else v)


def reset_filters() -> None:
    for k, v in D.items():
        S[k] = list(v) if isinstance(v, list) else v


def apply_preset() -> None:
    reset_filters()
    for k, v in PRESETS[S["preset"]].items():
        S[k] = v


def _range(df, col, val, dflt):
    return df if tuple(val) == tuple(dflt) else df[df[col].between(val[0], val[1])]


def _minv(df, col, val, dflt):
    return df if val <= dflt else df[df[col] >= val]


def _opt(df, col, table, key):
    r = table[key]
    return df if r is None else df[df[col].between(r[0], r[1])]


def _sma(df, col, v):
    if v == "Price above":
        return df[df[col] > 0]
    if v == "Price below":
        return df[df[col] < 0]
    return df


def apply_filters(day: pd.DataFrame, s, meta: dict) -> pd.DataFrame:
    """Apply every active screener filter to one day's data. `s` is any mapping of filter values."""
    x = day
    if s["sector"]:
        x = x[x["sector"].isin(s["sector"])]
    if meta["has_industry"] and s["industry"]:
        x = x[x["industry"].isin(s["industry"])]
    x = _range(x, "close", s["price"], D["price"])
    x = _range(x, "chg", s["chg"], D["chg"])
    x = _minv(x, "gap", s["gap"], D["gap"])
    x = _minv(x, "relvol", s["relvol"], D["relvol"])
    x = _minv(x, "volume", s["vol"], D["vol"])
    x = _minv(x, "avgvol", s["avgvol"], D["avgvol"])
    x = _range(x, "atrp", s["atr"], D["atr"])
    x = _range(x, "rsi", s["rsi"], D["rsi"])
    for col, key in (("d20", "s20"), ("d50", "s50"), ("d200", "s200")):
        x = _sma(x, col, s[key])
    x = _minv(x, "hid", s["hid"], D["hid"])
    x = _minv(x, "lod", s["lod"], D["lod"])
    for col, key in (("pw", "pw"), ("pm", "pm"), ("pq", "pq")):
        x = _opt(x, col, PERF, s[key])
    if meta["has_fund"]:
        x = _opt(x, "mcap", MCAP, s["mcap"])
        x = _opt(x, "float_sh", FLOAT, s["flt"])
        x = _opt(x, "short_pct", SHORT, s["short"])
    if str(s["q"]).strip():
        x = x[x["ticker"].str.contains(str(s["q"]).strip().upper(), regex=False)]
    return x


def decide_open(selected: list, opened: Optional[str], forced: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Decide which chart dialog (if any) to open on this rerun.

    selected: tickers currently highlighted in tables, opened: ticker whose dialog was already shown,
    forced: ticker typed into the search box. Returns (ticker_to_open, new_opened).
    This guard stops a dialog from re-opening on every unrelated rerun.
    """
    if forced:
        return forced, opened
    cur = [s for s in selected if s]
    new = [s for s in cur if s != opened]
    if new:
        return new[0], new[0]
    if not cur:
        return None, None
    return None, opened


def clean_symbol(raw: str) -> Optional[str]:
    s = (raw or "").strip().upper()
    return s if re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,9}", s) else None


def tv_symbol(sym: str, exchange) -> str:
    ex = TV_EXCHANGES.get(str(exchange)) if exchange is not None and not pd.isna(exchange) else None
    return f"{ex}:{sym}" if ex else sym


# =============================================================================
# 4. UI HELPERS
# =============================================================================
def stretch(fn, *a, **k):
    """Call a Streamlit element with full width, working on old and new Streamlit versions."""
    try:
        return fn(*a, width="stretch", **k)
    except TypeError:
        return fn(*a, use_container_width=True, **k)


def render_iframe(doc: str, height: int) -> None:
    try:
        if hasattr(st, "iframe"):
            st.iframe(doc, height=height)
        elif components is not None:
            components.html(doc, height=height)
        else:
            raise RuntimeError("no iframe support")
    except Exception:
        st.warning("The TradingView widget could not load in this browser.")


def embed_widget(script: str, cfg: dict, height: int) -> None:
    payload = json.dumps(cfg).replace("</", "<\\/")
    doc = ('<style>html,body{margin:0;background:#0e1117}</style>'
           f'<div class="tradingview-widget-container" style="height:{height}px;width:100%">'
           f'<div class="tradingview-widget-container__widget" style="height:{max(height - 8, 20)}px;width:100%"></div>'
           f'<script type="text/javascript" src="https://s3.tradingview.com/external-embedding/{script}" async>'
           f'{payload}</script></div>')
    render_iframe(doc, height)


CHART_TEMPLATE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<style>html,body{margin:0;padding:0;background:#0e1117;overflow:hidden}</style></head><body>
<div id="box" class="tradingview-widget-container"><div class="tradingview-widget-container__widget"></div></div>
<script>
(function () {
  var cfg = __CFG__;
  var H = __H__;
  try {
    var ph = window.parent.innerHeight;
    if (ph && ph > 0) { H = Math.max(520, ph - __OFFSET__); }
    if (window.frameElement) {
      window.frameElement.style.height = H + "px";
      window.frameElement.setAttribute("height", H);
    }
  } catch (e) {}
  cfg.autosize = false; cfg.width = "100%"; cfg.height = H;
  var box = document.getElementById("box");
  box.style.height = H + "px";
  var s = document.createElement("script");
  s.type = "text/javascript"; s.async = true;
  s.src = "https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js";
  s.innerHTML = JSON.stringify(cfg);
  box.appendChild(s);
})();
</script></body></html>"""


def chart_doc(symbol: str, init_height: int = 700, offset: int = 210) -> str:
    """HTML for the TradingView Advanced Chart with an explicit pixel height.

    The height fills the browser window (window height - offset) when the iframe may read it,
    otherwise init_height is used. Pixel sizes avoid the 'tiny chart' problem of percent sizing.
    """
    cfg = {"autosize": False, "width": "100%", "height": init_height, "symbol": symbol, "interval": "D",
           "timezone": "Asia/Tashkent", "theme": "dark", "style": "1", "locale": "en",
           "allow_symbol_change": True, "hide_side_toolbar": False, "withdateranges": True,
           "save_image": True, "calendar": False, "details": False,
           "support_host": "https://www.tradingview.com"}
    payload = json.dumps(cfg).replace("</", "<\\/")
    return (CHART_TEMPLATE.replace("__CFG__", payload).replace("__H__", str(int(init_height)))
            .replace("__OFFSET__", str(int(offset))))


def tv_chart(symbol: str, init_height: int = 700, offset: int = 210) -> None:
    render_iframe(chart_doc(symbol, init_height, offset), init_height)


def ticker_tape() -> None:
    syms = [("FOREXCOM:SPXUSD", "S&P 500"), ("FOREXCOM:NSXUSD", "Nasdaq 100"), ("TVC:VIX", "VIX"),
            ("TVC:DXY", "US Dollar"), ("OANDA:XAUUSD", "Gold"), ("BITSTAMP:BTCUSD", "Bitcoin")]
    embed_widget("embed-widget-ticker-tape.js", {
        "symbols": [{"proName": a, "title": b} for a, b in syms], "showSymbolLogo": False, "colorTheme": "dark",
        "isTransparent": True, "displayMode": "adaptive", "locale": "en"}, 48)


def inject_header() -> None:
    brand = ('<div class="smz-brand"><div class="logo">S<span>.</span>M<span>.</span>Z</div>'
             f'<div class="founder">Founder: {html.escape(FOUNDER)}</div></div>')
    st.markdown(CSS + brand, unsafe_allow_html=True)


def _color_cell(v):
    if pd.isna(v):
        return ""
    return f"color:{GREEN}" if v > 0 else (f"color:{RED}" if v < 0 else "")


def style_table(df: pd.DataFrame):
    sty = df.style.format({c: FMT[c] for c in df.columns if c in FMT}, na_rep="-")
    colored = [c for c in df.columns if c in COLOR_COLS]
    if colored:
        fn = getattr(sty, "map", None) or sty.applymap
        sty = fn(_color_cell, subset=colored)
    return sty


def to_display(df: pd.DataFrame, keys: list, with_no: bool = False) -> pd.DataFrame:
    out = df[keys].rename(columns=COLS).reset_index(drop=True)
    if with_no:
        out.insert(0, "No.", np.arange(1, len(out) + 1))
    return out


def render_table(df: pd.DataFrame, key: str, height: int, row_height: int = 24) -> Optional[str]:
    """Compact, sortable table; returns the Ticker of the clicked row (or None)."""
    shown = style_table(df)
    base = dict(hide_index=True, height=height, key=key, on_select="rerun", selection_mode="single-row")
    cfg = {"Ticker": st.column_config.TextColumn("Ticker", pinned=True)} if "Ticker" in df.columns else {}
    ev = None
    for extra in (dict(row_height=row_height, column_config=cfg), dict(row_height=row_height), {}):
        try:
            ev = stretch(st.dataframe, shown, **base, **extra)
            break
        except TypeError:
            continue
    if ev is None:
        stretch(st.dataframe, shown, hide_index=True, height=height)
        return None
    try:
        rows = ev.selection.rows
        if rows:
            return str(df.iloc[rows[0]]["Ticker"])
    except Exception:
        pass
    return None


def kpi_html(day: pd.DataFrame) -> str:
    chg = day["chg"].dropna()
    n = len(chg)
    adv, dec = int((chg > 0).sum()), int((chg < 0).sum())
    unch = n - adv - dec

    def pct(col):
        v = day[col].dropna()
        return f"{(v > 0).mean() * 100:.0f}%" if len(v) else "-"

    tiles = [("Stocks", f"{n}", ""), ("Advancers", f"{adv}", GREEN), ("Decliners", f"{dec}", RED),
             ("Avg change", f"{chg.mean():+.2f}%" if n else "-", GREEN if n and chg.mean() >= 0 else RED),
             ("Above SMA20", pct("d20"), ""), ("Above SMA50", pct("d50"), ""), ("Above SMA200", pct("d200"), ""),
             ("Near 52W high", f"{int((day['hid'] >= -1).sum())}", ""),
             ("Near 52W low", f"{int((day['lod'] <= 1).sum())}", ""),
             ("$ Volume", f"${day['dvol'].sum():,.0f}M", "")]
    out = "".join(f'<div class="kpi"><span>{a}</span><b style="color:{c or "#e6edf3"}">{b}</b></div>'
                  for a, b, c in tiles)
    total = max(n, 1)
    bar = (f'<div class="bar"><div style="width:{adv / total * 100:.1f}%;background:{GREEN}"></div>'
           f'<div style="width:{unch / total * 100:.1f}%;background:{GREY}"></div>'
           f'<div style="width:{dec / total * 100:.1f}%;background:{RED}"></div></div>')
    return f'<div class="kpis">{out}</div>{bar}'


# =============================================================================
# 5. MARKET MAP (Finviz-style treemap)
# =============================================================================
def chg_color(v, scale: float) -> str:
    if v is None or pd.isna(v):
        return PALETTE[3]
    idx = int(np.clip(np.round(v / (scale / 3.0)), -3, 3)) + 3
    return PALETTE[idx]


def legend_html(scale: float) -> str:
    labels = [-scale, -2 * scale / 3, -scale / 3, 0, scale / 3, 2 * scale / 3, scale]
    cells = "".join(f'<div style="background:{c}">{l:+.0f}%</div>' if l else f'<div style="background:{c}">0%</div>'
                    for c, l in zip(PALETTE, labels))
    return f'<div class="legend">{cells}</div>'


def build_treemap(df: pd.DataFrame, size_col: str, scale: float, top_n: int, by_industry: bool) -> Optional[go.Figure]:
    """Sector (> industry) > ticker treemap. Block size = size_col, block color = daily change."""
    d = df.dropna(subset=[size_col, "chg"])
    d = d[d[size_col] > 0].sort_values(size_col, ascending=False).head(top_n)
    if d.empty:
        return None
    ids, labels, parents, values, colors, text, hover = ["root"], ["Market"], [""], [0], ["#0e1117"], [""], [""]

    def add_node(i, label, parent, value, color, txt, hov):
        ids.append(i); labels.append(label); parents.append(parent); values.append(value)
        colors.append(color); text.append(txt); hover.append(hov)

    def group_stats(g):
        w = np.average(g["chg"], weights=g[size_col])
        return f"{len(g)} stocks | weighted change {w:+.2f}%"

    d = d.assign(_sector=d["sector"].fillna("Other"), _industry=d["industry"].fillna("Other"))
    for sector, gs in d.groupby("_sector"):
        sid = f"S|{sector}"
        add_node(sid, str(sector), "root", 0, "#1b1f27", "", html.escape(f"{sector}: {group_stats(gs)}"))
        subgroups = [(sid, gs)]
        if by_industry:
            subgroups = []
            for industry, gi in gs.groupby("_industry"):
                iid = f"I|{sector}|{industry}"
                add_node(iid, str(industry), sid, 0, "#232834", "", html.escape(f"{industry}: {group_stats(gi)}"))
                subgroups.append((iid, gi))
        for parent_id, g in subgroups:
            for r in g.itertuples(index=False):
                hov = (f"<b>{html.escape(str(r.ticker))}</b><br>Price ${r.close:.2f}<br>Change {r.chg:+.2f}%"
                       f"<br>Volume {r.volume:,.0f}<br>$ Volume {r.dvol:,.2f}M")
                add_node(f"T|{r.ticker}", str(r.ticker), parent_id, float(getattr(r, size_col)),
                         chg_color(r.chg, scale), f"{r.chg:+.1f}%", hov)

    fig = go.Figure(go.Treemap(
        ids=ids, labels=labels, parents=parents, values=values, text=text, hovertext=hover, hoverinfo="text",
        textinfo="label+text", branchvalues="remainder", textfont=dict(size=13, color="white"),
        marker=dict(colors=colors, line=dict(width=1, color="#0e1117"), pad=dict(t=20, l=3, r=3, b=3)),
        tiling=dict(packing="squarify", pad=1), pathbar=dict(visible=False)))
    fig.update_layout(margin=dict(t=0, l=0, r=0, b=0), height=620, paper_bgcolor="#0e1117")
    return fig


# =============================================================================
# 6. VIEWS
# =============================================================================
def view_overview(day: pd.DataFrame, meta: dict) -> list:
    """Market Overview: KPIs, breadth bar, stock map and Finviz-style top lists."""
    selected: list = []
    st.markdown(kpi_html(day), unsafe_allow_html=True)

    c = st.columns([1.3, 1.1, 1.0, 1.4, 3.0])
    size_opts = {"$ Volume": "dvol", "Volume (shares)": "volume"}
    if meta["has_fund"]:
        size_opts["Market Cap"] = "mcap"
    size_label = c[0].selectbox("Block size", list(size_opts), key="map_size")
    scale = float(c[1].selectbox("Color scale (+/- %)", [3, 5, 10, 20], index=2, key="map_scale"))
    top_n = int(c[2].number_input("Max stocks", 30, 500, 250, step=25, key="map_top"))
    by_ind = c[3].toggle("Group by industry", value=meta["has_industry"], disabled=not meta["has_industry"],
                         key="map_ind")
    fig = build_treemap(day, size_opts[size_label], scale, top_n, bool(by_ind) and meta["has_industry"])
    if fig is None:
        st.info("Not enough data to draw the map for this date.")
    else:
        stretch(st.plotly_chart, fig, config={"displayModeBar": False}, theme=None)
        st.markdown(legend_html(scale), unsafe_allow_html=True)

    cols = st.columns(4)
    for i, (title, fn) in enumerate(LISTS):
        with cols[i % 4]:
            st.markdown(f'<div class="ttl">{title}</div>', unsafe_allow_html=True)
            sub = fn(day)
            if sub.empty:
                st.caption("No stocks")
                continue
            show = to_display(sub, MINI_COLS)
            selected.append(render_table(show, f"mini_{i}", 36 + 24 * len(show) + 4))
    return selected


def filter_ui(day: pd.DataFrame, meta: dict) -> None:
    top = st.columns([3, 1])
    top[0].selectbox("Preset screens", list(PRESETS), key="preset", on_change=apply_preset)
    top[1].write("")
    top[1].button("Reset filters", on_click=reset_filters, key="reset_btn")
    tabs = st.tabs(["Descriptive", "Technical", "Trend", "Fundamental"])
    with tabs[0]:
        a = st.columns(6)
        a[0].multiselect("Sector", sorted(day["sector"].dropna().unique()), key="sector")
        a[1].multiselect("Industry", sorted(day["industry"].dropna().unique()), key="industry",
                         disabled=not meta["has_industry"])
        a[2].slider("Price ($)", 0.0, 20.0, key="price", step=0.1)
        a[3].slider("Change today (%)", -50.0, 300.0, key="chg", step=1.0)
        a[4].text_input("Ticker contains", key="q")
    with tabs[1]:
        b = st.columns(6)
        b[0].number_input("Min gap (%)", -100.0, 300.0, key="gap", step=1.0)
        b[1].number_input("Min rel. volume (x)", 0.0, 100.0, key="relvol", step=0.5)
        b[2].number_input("Min volume", 0, 1000000000, key="vol", step=50000)
        b[3].number_input("Min avg volume (20d)", 0, 1000000000, key="avgvol", step=50000)
        b[4].slider("ATR (daily range %)", 0.0, 60.0, key="atr", step=0.5)
        b[5].selectbox("Performance (week)", list(PERF), key="pw")
        b2 = st.columns(6)
        b2[0].selectbox("Performance (month)", list(PERF), key="pm")
        b2[1].selectbox("Performance (quarter)", list(PERF), key="pq")
    with tabs[2]:
        c = st.columns(6)
        c[0].selectbox("SMA20", SMA, key="s20")
        c[1].selectbox("SMA50", SMA, key="s50")
        c[2].selectbox("SMA200", SMA, key="s200")
        c[3].slider("RSI (14)", 0.0, 100.0, key="rsi", step=1.0)
        c[4].slider("Max below 52W high (%)", -100.0, 0.0, key="hid", step=1.0)
        c[5].number_input("Min above 52W low (%)", 0.0, 5000.0, key="lod", step=5.0)
    with tabs[3]:
        if not meta["has_fund"]:
            st.info("Fundamental data (market cap, float, short float, industry) is still loading. "
                    "It fills in automatically within a few hours.")
        d_ = st.columns(6)
        d_[0].selectbox("Market cap", list(MCAP), key="mcap", disabled=not meta["has_fund"])
        d_[1].selectbox("Float", list(FLOAT), key="flt", disabled=not meta["has_fund"])
        d_[2].selectbox("Short float", list(SHORT), key="short", disabled=not meta["has_fund"])


def view_screener(day: pd.DataFrame, meta: dict) -> list:
    """Filters + compact multi-column table. Click a row to open the TradingView chart."""
    with st.expander("Filters", expanded=True):
        filter_ui(day, meta)
    x = apply_filters(day, S, meta)

    order = ["ticker", "sector"] + (["industry"] if meta["has_industry"] else []) + \
            (list(FUND_COLS) if meta["has_fund"] else []) + \
            ["close", "chg", "gap", "volume", "avgvol", "relvol", "dvol", "atrp", "rsi", "d20", "d50", "d200",
             "hid", "lod", "pw", "pm", "pq"]
    bar = st.columns([3, 1.4, 1, 1])
    bar[0].markdown(f"**{S['date']}: {len(x)} stocks** &nbsp;|&nbsp; click a row to open its chart")
    sort_name = bar[1].selectbox("Sort by", [COLS[c] for c in order], index=order.index("chg"),
                                 label_visibility="collapsed")
    asc = bar[2].checkbox("Ascending", value=False)
    inv = {v: k for k, v in COLS.items()}
    x = x.sort_values(inv[sort_name], ascending=asc)
    if x.empty:
        st.info("No stocks match these filters. Try loosening them.")
        return []
    full = to_display(x, order, with_no=True)
    bar[3].download_button("Download CSV", full.round(2).to_csv(index=False).encode(), "smz_screener.csv",
                           "text/csv")
    return [render_table(full, "tbl_screener", 640)]


# =============================================================================
# 7. CHART DIALOG
# =============================================================================
def open_chart(sym: str, row: Optional[pd.Series]) -> None:
    """Open a large modal window with the full TradingView Advanced Chart."""
    exch = row["exchange"] if row is not None and "exchange" in row else None
    tv_sym = tv_symbol(sym, exch)

    @st.dialog(f"{sym}  |  TradingView", width="large")
    def _dialog():
        parts = []
        if row is not None:
            parts = [f"<b>${row['close']:.2f}</b>", f"{row['chg']:+.2f}%"]
            if pd.notna(row["relvol"]):
                parts.append(f"Rel Vol {row['relvol']:.1f}x")
            if pd.notna(row["rsi"]):
                parts.append(f"RSI {row['rsi']:.0f}")
            if pd.notna(row["atrp"]):
                parts.append(f"ATR {row['atrp']:.1f}%")
            if pd.notna(row.get("sector")):
                parts.append(html.escape(str(row["sector"])))
        parts += [f'<a href="https://finance.yahoo.com/quote/{sym}/news" target="_blank">Yahoo news</a>',
                  f'<a href="https://finviz.com/quote.ashx?t={sym}" target="_blank">Finviz</a>',
                  f'<a href="https://www.tradingview.com/chart/?symbol={tv_sym}" target="_blank">Open in TradingView</a>']
        st.markdown(f'<div class="chartbar">{" &nbsp;|&nbsp; ".join(parts)}</div>', unsafe_allow_html=True)
        tv_chart(tv_sym, 700, 210)

    _dialog()


# =============================================================================
# 8. MAIN
# =============================================================================
def header(dates: list) -> tuple[str, Optional[str]]:
    c = st.columns([1.6, 1.0, 3.4, 1.0])
    opts = ["Overview", "Screener"]
    with c[0]:
        if hasattr(st, "segmented_control"):
            view = st.segmented_control("View", opts, default="Overview", key="view", label_visibility="collapsed")
        else:
            view = st.radio("View", opts, horizontal=True, key="view", label_visibility="collapsed")
    c[1].selectbox("Date", dates, key="date", label_visibility="collapsed")
    forced = None
    with c[2]:
        with st.form("open_chart_form", border=False):
            f = st.columns([3, 1])
            q = f[0].text_input("Chart ticker", key="open_q", placeholder="Open chart for any ticker, e.g. FCHL",
                                label_visibility="collapsed")
            go_ = f[1].form_submit_button("Open chart")
        if go_:
            forced = clean_symbol(q)
            if forced is None:
                st.warning("Enter a valid ticker (letters and digits only).")
    c[3].toggle("Live tape", value=True, key="tape")
    return view or "Overview", forced


def main() -> None:
    st.set_page_config(layout="wide", page_title=BRAND, page_icon="📈", initial_sidebar_state="collapsed")
    inject_header()
    init_state()
    p, meta = load_data()
    dates = sorted(p["date"].unique(), reverse=True)

    if S.get("tape", True):
        ticker_tape()
    view, forced = header(dates)
    day = p[p["date"] == S["date"]]

    selected = view_overview(day, meta) if view == "Overview" else view_screener(day, meta)

    to_open, S["opened"] = decide_open(selected, S.get("opened"), forced)
    st.caption(f"{BRAND} | Founder: {FOUNDER} | Data: Yahoo Finance (end of day) | Charts: TradingView | "
               "Not financial advice.")
    if to_open:
        r = day[day["ticker"] == to_open]
        open_chart(to_open, r.iloc[0] if not r.empty else None)


if __name__ == "__main__":
    main()

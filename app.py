import json
import sqlite3
import numpy as np
import pandas as pd
import streamlit as st
import altair as alt
try:
    import streamlit.components.v1 as components
except Exception:
    components = None

st.set_page_config(layout="wide", page_title="S.M.Z", page_icon="📈")

INF = float("inf")
PERF = {"Any": None, "Up": (0, INF), "Up 10%": (10, INF), "Up 20%": (20, INF), "Up 50%": (50, INF),
        "Down": (-INF, 0), "Down 10%": (-INF, -10), "Down 20%": (-INF, -20), "Down 50%": (-INF, -50)}
MCAP = {"Any": None, "Nano (under $50M)": (0, 50), "Micro ($50M to $300M)": (50, 300),
        "Under $300M": (0, 300), "Small ($300M to $2B)": (300, 2000)}
FLOAT = {"Any": None, "Under 5M": (0, 5), "Under 10M": (0, 10), "Under 20M": (0, 20),
         "Under 50M": (0, 50), "Over 50M": (50, INF)}
SHORT = {"Any": None, "Over 5%": (5, INF), "Over 10%": (10, INF), "Over 20%": (20, INF)}
SMA = ["Any", "Price above", "Price below"]

D = dict(sector=[], price=(0.0, 10.0), chg=(-50.0, 300.0), gap=-100.0, relvol=0.0, vol=0, avgvol=0,
         atr=(0.0, 60.0), rsi=(0.0, 100.0), s20="Any", s50="Any", s200="Any",
         hid=-100.0, lod=0.0, pw="Any", pm="Any", pq="Any",
         mcap="Any", flt="Any", short="Any", q="")

PRESETS = {
    "- Select a preset -": {},
    "Gap & Go (gap +5%, volume 2x)": dict(gap=5.0, relvol=2.0),
    "Near 52-week high": dict(hid=-5.0, s50="Price above", relvol=1.5),
    "Oversold (RSI under 30)": dict(rsi=(0.0, 30.0)),
    "Volume surge, quiet price": dict(relvol=3.0, chg=(-3.0, 5.0)),
    "Low float runner": dict(flt="Under 20M", relvol=2.0, chg=(3.0, 300.0)),
    "Rising from lows (above SMA20)": dict(lod=30.0, s20="Price above", pm="Up"),
}

COLS = {"ticker": "Ticker", "sector": "Sector", "close": "Price", "chg": "Change %", "gap": "Gap %",
        "relvol": "Rel Volume", "volume": "Volume", "avgvol": "Avg Volume", "atrp": "ATR %", "rsi": "RSI",
        "d20": "SMA20 %", "d50": "SMA50 %", "d200": "SMA200 %", "hid": "52W High %", "lod": "52W Low %",
        "pw": "Perf Week %", "pm": "Perf Month %", "pq": "Perf Quarter %", "mcap": "Market Cap (M)",
        "float_sh": "Float (M)", "short_pct": "Short Float %"}
FUND_COLS = ["mcap", "float_sh", "short_pct"]

st.markdown("""<style>
.smz-brand{position:fixed;top:0;left:0;z-index:1000000;background:#0b1220;padding:7px 20px 7px 16px;border-bottom-right-radius:10px;line-height:1.15;box-shadow:0 2px 8px rgba(0,0,0,.45)}
.smz-brand .logo{font-size:27px;font-weight:800;letter-spacing:2px;color:#ffffff;font-family:Arial,Helvetica,sans-serif}
.smz-brand .logo span{color:#26a69a}
.smz-brand .founder{font-size:10px;color:#9fb0c8;letter-spacing:.3px;font-family:Arial,Helvetica,sans-serif}
.block-container{padding-top:4.5rem !important}
</style>
<div class="smz-brand"><div class="logo">S<span>.</span>M<span>.</span>Z</div><div class="founder">Founder: Sobirov.Moydinxon.Zaynobiddinvich</div></div>""", unsafe_allow_html=True)


def reset():
    for k, v in D.items():
        st.session_state[k] = v


def apply_preset():
    reset()
    for k, v in PRESETS[st.session_state["preset"]].items():
        st.session_state[k] = v


for k, v in D.items():
    st.session_state.setdefault(k, v)


@st.cache_data(ttl=3600, show_spinner="Calculating indicators...")
def load():
    con = sqlite3.connect("data.db")
    p = pd.read_sql("select * from prices", con)
    t = pd.read_sql("select * from tickers", con)
    try:
        f = pd.read_sql("select ticker, mcap, float_sh, short_pct from fund", con)
    except Exception:
        f = pd.DataFrame(columns=["ticker", "mcap", "float_sh", "short_pct"])
    con.close()
    p = p.drop_duplicates(["ticker", "date"]).sort_values(["ticker", "date"]).reset_index(drop=True)
    g = p.groupby("ticker", sort=False)
    pc = g["close"].shift(1)
    p["chg"] = (p["close"] / pc - 1) * 100
    p["gap"] = (p["open"] / pc - 1) * 100
    for n in (20, 50, 200):
        sma = g["close"].transform(lambda s, n=n: s.rolling(n, min_periods=n).mean())
        p[f"sma{n}"] = sma
        p[f"d{n}"] = (p["close"] / sma - 1) * 100
    p["avgvol"] = g["volume"].transform(lambda s: s.shift(1).rolling(20, min_periods=10).mean())
    p["relvol"] = p["volume"] / p["avgvol"]
    tr = pd.concat([p["high"] - p["low"], (p["high"] - pc).abs(), (p["low"] - pc).abs()], axis=1).max(axis=1)
    p["atr"] = tr.groupby(p["ticker"]).transform(lambda s: s.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean())
    p["atrp"] = p["atr"] / p["close"] * 100
    delta = g["close"].diff()
    au = delta.clip(lower=0).groupby(p["ticker"]).transform(lambda s: s.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean())
    ad = (-delta).clip(lower=0).groupby(p["ticker"]).transform(lambda s: s.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean())
    p["rsi"] = 100 * au / (au + ad).replace(0, np.nan)
    p["hi52"] = g["high"].transform(lambda s: s.rolling(252, min_periods=40).max())
    p["lo52"] = g["low"].transform(lambda s: s.rolling(252, min_periods=40).min())
    p["hid"] = (p["close"] / p["hi52"] - 1) * 100
    p["lod"] = (p["close"] / p["lo52"] - 1) * 100
    for name, n in (("pw", 5), ("pm", 21), ("pq", 63)):
        p[name] = (p["close"] / g["close"].shift(n) - 1) * 100
    p = p.merge(t, on="ticker", how="left").merge(f, on="ticker", how="left")
    p["mcap"] = p["mcap"] / 1e6
    p["float_sh"] = p["float_sh"] / 1e6
    return p, bool(f["mcap"].notna().any())


def stretch(fn, *a, **k):
    try:
        return fn(*a, width="stretch", **k)
    except TypeError:
        return fn(*a, use_container_width=True, **k)


def rng(df, col, val, dflt):
    return df if tuple(val) == tuple(dflt) else df[df[col].between(val[0], val[1])]


def minv(df, col, val, dflt):
    return df if val <= dflt else df[df[col] >= val]


def opt(df, col, table, key):
    r = table[key]
    return df if r is None else df[df[col].between(r[0], r[1])]


def sma_f(df, col, v):
    if v == "Price above":
        return df[df[col] > 0]
    if v == "Price below":
        return df[df[col] < 0]
    return df


def candle(d):
    d = d.copy()
    d["date"] = pd.to_datetime(d["date"])
    base = alt.Chart(d).encode(x=alt.X("date:T", title=None))
    color = alt.condition("datum.open <= datum.close", alt.value("#26a69a"), alt.value("#ef5350"))
    rule = base.mark_rule().encode(y=alt.Y("low:Q", scale=alt.Scale(zero=False), title=None), y2="high:Q", color=color)
    bar = base.mark_bar().encode(y="open:Q", y2="close:Q", color=color)
    l20 = base.mark_line(color="#f5a623").encode(y="sma20:Q")
    l50 = base.mark_line(color="#4a90e2").encode(y="sma50:Q")
    price = (rule + bar + l20 + l50).properties(height=340)
    vol = base.mark_bar().encode(y=alt.Y("volume:Q", title=None), color=color).properties(height=90)
    return alt.vconcat(price, vol)


p, has_f = load()
dates = sorted(p["date"].unique(), reverse=True)

st.title("Stock Screener")
c1, c2, c3 = st.columns([3, 1, 1])
c1.selectbox("Preset screens", list(PRESETS), key="preset", on_change=apply_preset)
c2.selectbox("Date", dates, key="date")
c3.write("")
c3.button("Reset filters", on_click=reset)

tabs = st.tabs(["Descriptive", "Technical", "Trend (SMA / RSI / 52W)", "Fundamental"])
with tabs[0]:
    a = st.columns(4)
    a[0].multiselect("Sector", sorted(p["sector"].dropna().unique()), key="sector")
    a[1].slider("Price ($)", 0.0, 20.0, key="price", step=0.1)
    a[2].slider("Change today (%)", -50.0, 300.0, key="chg", step=1.0)
    a[3].text_input("Search ticker", key="q")
with tabs[1]:
    b = st.columns(4)
    b[0].number_input("Min gap (%)", -100.0, 300.0, key="gap", step=1.0)
    b[1].number_input("Min relative volume (x)", 0.0, 100.0, key="relvol", step=0.5)
    b[2].number_input("Min volume (today)", 0, 1000000000, key="vol", step=50000)
    b[3].number_input("Min avg volume (20d)", 0, 1000000000, key="avgvol", step=50000)
    b2 = st.columns(4)
    b2[0].slider("ATR (daily range %)", 0.0, 60.0, key="atr", step=0.5)
    b2[1].selectbox("Performance (week)", list(PERF), key="pw")
    b2[2].selectbox("Performance (month)", list(PERF), key="pm")
    b2[3].selectbox("Performance (quarter)", list(PERF), key="pq")
with tabs[2]:
    c = st.columns(4)
    c[0].selectbox("SMA20", SMA, key="s20")
    c[1].selectbox("SMA50", SMA, key="s50")
    c[2].selectbox("SMA200", SMA, key="s200")
    c[3].slider("RSI (14)", 0.0, 100.0, key="rsi", step=1.0)
    c2_ = st.columns(4)
    c2_[0].slider("Max distance below 52W high (%)", -100.0, 0.0, key="hid", step=1.0)
    c2_[1].number_input("Min distance above 52W low (%)", 0.0, 5000.0, key="lod", step=5.0)
with tabs[3]:
    if not has_f:
        st.info("Fundamental data (market cap, float, short float) is still loading. It fills in automatically within a few hours.")
    d_ = st.columns(3)
    d_[0].selectbox("Market cap", list(MCAP), key="mcap", disabled=not has_f)
    d_[1].selectbox("Float", list(FLOAT), key="flt", disabled=not has_f)
    d_[2].selectbox("Short float", list(SHORT), key="short", disabled=not has_f)

S = st.session_state
x = p[p["date"] == S["date"]]
if S["sector"]:
    x = x[x["sector"].isin(S["sector"])]
x = rng(x, "close", S["price"], D["price"])
x = rng(x, "chg", S["chg"], D["chg"])
x = minv(x, "gap", S["gap"], D["gap"])
x = minv(x, "relvol", S["relvol"], D["relvol"])
x = minv(x, "volume", S["vol"], D["vol"])
x = minv(x, "avgvol", S["avgvol"], D["avgvol"])
x = rng(x, "atrp", S["atr"], D["atr"])
x = rng(x, "rsi", S["rsi"], D["rsi"])
for col, key in (("d20", "s20"), ("d50", "s50"), ("d200", "s200")):
    x = sma_f(x, col, S[key])
x = minv(x, "hid", S["hid"], D["hid"])
x = minv(x, "lod", S["lod"], D["lod"])
for col, key in (("pw", "pw"), ("pm", "pm"), ("pq", "pq")):
    x = opt(x, col, PERF, S[key])
if has_f:
    x = opt(x, "mcap", MCAP, S["mcap"])
    x = opt(x, "float_sh", FLOAT, S["flt"])
    x = opt(x, "short_pct", SHORT, S["short"])
if S["q"].strip():
    x = x[x["ticker"].str.contains(S["q"].strip().upper(), regex=False)]

def tv_chart(sym, height):
    cfg = {"autosize": True, "symbol": sym, "interval": "D", "timezone": "Asia/Tashkent", "theme": "dark",
           "style": "1", "locale": "en", "allow_symbol_change": True, "hide_side_toolbar": False,
           "withdateranges": True, "save_image": True, "calendar": False, "details": False,
           "support_host": "https://www.tradingview.com"}
    html = ('<div class="tradingview-widget-container" style="height:%dpx;width:100%%">'
            '<div class="tradingview-widget-container__widget" style="height:%dpx;width:100%%"></div>'
            '<script type="text/javascript" src="https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js" async>'
            '%s</script></div>') % (height, height - 8, json.dumps(cfg))
    html = '<style>html,body{margin:0;background:#0e1117}</style>' + html
    try:
        if hasattr(st, "iframe"):
            st.iframe(html, height=height)
        elif components is not None:
            components.html(html, height=height)
        else:
            raise RuntimeError("no iframe support")
    except Exception:
        st.warning("Chart could not load here. Use the TradingView link below.")


def chart_panel(pick, height):
    r = p[(p["ticker"] == pick) & (p["date"] == S["date"])]
    if not r.empty:
        r = r.iloc[0]
        m = st.columns(5)
        m[0].metric(pick, f"${r['close']:.2f}", f"{r['chg']:+.1f}%")
        m[1].metric("Rel Volume", f"{r['relvol']:.1f}x" if pd.notna(r["relvol"]) else "-")
        m[2].metric("RSI", f"{r['rsi']:.0f}" if pd.notna(r["rsi"]) else "-")
        m[3].metric("ATR", f"{r['atrp']:.1f}%" if pd.notna(r["atrp"]) else "-")
        m[4].metric("From 52W High", f"{r['hid']:.0f}%" if pd.notna(r["hid"]) else "-")
    st.radio("Chart", ["TradingView", "Basic"], horizontal=True, key="chart_mode", label_visibility="collapsed")
    if S["chart_mode"] == "TradingView":
        tv_chart(pick, height)
    else:
        stretch(st.altair_chart, candle(p[p["ticker"] == pick].tail(180)))
    st.markdown(f"News: [Yahoo](https://finance.yahoo.com/quote/{pick}/news) | "
                f"[Finviz](https://finviz.com/quote.ashx?t={pick}) | "
                f"[Open in TradingView](https://www.tradingview.com/chart/?symbol={pick})")


def table(df_cols, height):
    show = x[df_cols].rename(columns=COLS).round(2).reset_index(drop=True)
    sel = None
    try:
        ev = stretch(st.dataframe, show, height=height, hide_index=True, on_select="rerun", selection_mode="single-row")
        if ev.selection.rows:
            sel = show.iloc[ev.selection.rows[0]]["Ticker"]
    except TypeError:
        stretch(st.dataframe, show, height=height, hide_index=True)
    return sel


cols = [c for c in COLS if has_f or c not in FUND_COLS]
h = st.columns([3, 1.2, 1, 1.6])
h[0].subheader(f"{S['date']}: {len(x)} stocks found")
sort_name = h[1].selectbox("Sort by", [COLS[c] for c in cols], index=cols.index("chg"))
asc = h[2].checkbox("Ascending", value=False)
layout = h[3].radio("Layout", ["Chart + list", "Full table"], horizontal=True, key="layout")
inv = {v: k for k, v in COLS.items()}
x = x.sort_values(inv[sort_name], ascending=asc)

if x.empty:
    st.info("No stocks match these filters. Try loosening them.")
    st.stop()

full = x[cols].rename(columns=COLS).round(2)
tickers = list(x["ticker"])

if layout == "Chart + list":
    left, right = st.columns([2, 5])
    with left:
        sel = table(["ticker", "close", "chg", "relvol", "volume", "gap"], 740)
        st.download_button("Download CSV", full.to_csv(index=False).encode(), "smz_screener.csv", "text/csv")
    if sel:
        S["chart_ticker"] = sel
    pick = S.get("chart_ticker") or tickers[0]
    with right:
        chart_panel(pick, 700)
else:
    sel = table(cols, 480)
    st.download_button("Download CSV", full.to_csv(index=False).encode(), "smz_screener.csv", "text/csv")
    if sel:
        S["chart_ticker"] = sel
    pick = S.get("chart_ticker") or tickers[0]
    st.divider()
    chart_panel(pick, 680)

st.divider()
st.caption("S.M.Z | Founder: Sobirov.Moydinxon.Zaynobiddinvich | Data: Yahoo Finance (end of day). Charts: TradingView. Not financial advice.")

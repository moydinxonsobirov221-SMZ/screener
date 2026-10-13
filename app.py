import sqlite3
import numpy as np
import pandas as pd
import streamlit as st
import altair as alt

st.set_page_config(layout="wide", page_title="Penny Screener")

INF = float("inf")
PERF = {"Hammasi": None, "Ko'tarilgan": (0, INF), "+10% dan ko'p": (10, INF), "+20% dan ko'p": (20, INF),
        "+50% dan ko'p": (50, INF), "Tushgan": (-INF, 0), "-10% dan ko'p": (-INF, -10),
        "-20% dan ko'p": (-INF, -20), "-50% dan ko'p": (-INF, -50)}
MCAP = {"Hammasi": None, "Nano (<$50M)": (0, 50), "Micro ($50M-$300M)": (50, 300),
        "$300M dan kam": (0, 300), "Small ($300M-$2B)": (300, 2000)}
FLOAT = {"Hammasi": None, "5M dan kam": (0, 5), "10M dan kam": (0, 10), "20M dan kam": (0, 20),
         "50M dan kam": (0, 50), "50M dan ko'p": (50, INF)}
SHORT = {"Hammasi": None, "5% dan ko'p": (5, INF), "10% dan ko'p": (10, INF), "20% dan ko'p": (20, INF)}
SMA = ["Hammasi", "Narx ustida", "Narx ostida"]

D = dict(sector=[], price=(0.0, 10.0), chg=(-50.0, 300.0), gap=-100.0, relvol=0.0, vol=0, avgvol=0,
         atr=(0.0, 60.0), rsi=(0.0, 100.0), s20="Hammasi", s50="Hammasi", s200="Hammasi",
         hid=-100.0, lod=0.0, pw="Hammasi", pm="Hammasi", pq="Hammasi",
         mcap="Hammasi", flt="Hammasi", short="Hammasi", q="")

PRESETS = {
    "- strategiya tanlang -": {},
    "Gap & Go (gap +5%, hajm 2x)": dict(gap=5.0, relvol=2.0),
    "52 haftalik cho'qqi yaqinida": dict(hid=-5.0, s50="Narx ustida", relvol=1.5),
    "Oversold (RSI 30 dan past)": dict(rsi=(0.0, 30.0)),
    "Hajm uyg'ongan, narx tinch": dict(relvol=3.0, chg=(-3.0, 5.0)),
    "Kichik float runner": dict(flt="20M dan kam", relvol=2.0, chg=(3.0, 300.0)),
    "Tubdan ko'tarilayotgan (SMA20 ustida)": dict(lod=30.0, s20="Narx ustida", pm="Ko'tarilgan"),
}

COLS = {"ticker": "Ticker", "sector": "Sektor", "close": "Narx", "chg": "O'zg %", "gap": "Gap %",
        "relvol": "Nisbiy hajm", "volume": "Hajm", "avgvol": "O'rt. hajm", "atrp": "ATR %", "rsi": "RSI",
        "d20": "SMA20 %", "d50": "SMA50 %", "d200": "SMA200 %", "hid": "52h cho'qqi %", "lod": "52h tub %",
        "pw": "1 hafta %", "pm": "1 oy %", "pq": "3 oy %", "mcap": "Kapital (M$)", "float_sh": "Float (M)",
        "short_pct": "Short %"}
FUND_COLS = ["mcap", "float_sh", "short_pct"]


def reset():
    for k, v in D.items():
        st.session_state[k] = v


def apply_preset():
    reset()
    for k, v in PRESETS[st.session_state["preset"]].items():
        st.session_state[k] = v


for k, v in D.items():
    st.session_state.setdefault(k, v)


@st.cache_data(ttl=3600, show_spinner="Ma'lumot hisoblanmoqda...")
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
    if v == "Narx ustida":
        return df[df[col] > 0]
    if v == "Narx ostida":
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

st.title("Penny Stock Screener")
c1, c2, c3 = st.columns([3, 1, 1])
c1.selectbox("Tayyor strategiya", list(PRESETS), key="preset", on_change=apply_preset)
c2.selectbox("Sana", dates, key="date")
c3.write("")
c3.button("Filtrlarni tozalash", on_click=reset)

tabs = st.tabs(["Asosiy", "Texnik", "SMA / RSI / 52h", "Fundamental"])
with tabs[0]:
    a = st.columns(4)
    a[0].multiselect("Sektor", sorted(p["sector"].dropna().unique()), key="sector")
    a[1].slider("Narx ($)", 0.0, 20.0, key="price", step=0.1)
    a[2].slider("Bugungi o'zgarish (%)", -50.0, 300.0, key="chg", step=1.0)
    a[3].text_input("Ticker qidirish", key="q")
with tabs[1]:
    b = st.columns(4)
    b[0].number_input("Min gap (%)", -100.0, 300.0, key="gap", step=1.0)
    b[1].number_input("Min nisbiy hajm (x)", 0.0, 100.0, key="relvol", step=0.5)
    b[2].number_input("Min hajm (bugun)", 0, 1000000000, key="vol", step=50000)
    b[3].number_input("Min o'rtacha hajm (20 kun)", 0, 1000000000, key="avgvol", step=50000)
    b2 = st.columns(4)
    b2[0].slider("ATR (kunlik harakat %)", 0.0, 60.0, key="atr", step=0.5)
    b2[1].selectbox("1 hafta o'zgarishi", list(PERF), key="pw")
    b2[2].selectbox("1 oy o'zgarishi", list(PERF), key="pm")
    b2[3].selectbox("3 oy o'zgarishi", list(PERF), key="pq")
with tabs[2]:
    c = st.columns(4)
    c[0].selectbox("SMA20", SMA, key="s20")
    c[1].selectbox("SMA50", SMA, key="s50")
    c[2].selectbox("SMA200", SMA, key="s200")
    c[3].slider("RSI (14)", 0.0, 100.0, key="rsi", step=1.0)
    c2_ = st.columns(4)
    c2_[0].slider("52 hafta cho'qqisidan eng ko'pi bilan pastda (%)", -100.0, 0.0, key="hid", step=1.0)
    c2_[1].number_input("52 hafta tubidan kamida yuqorida (%)", 0.0, 5000.0, key="lod", step=5.0)
with tabs[3]:
    if not has_f:
        st.info("Fundamental ma'lumot (kapital, float, short) hali yuklanmagan. Bot uni bir necha soatda o'zi to'ldiradi.")
    d_ = st.columns(3)
    d_[0].selectbox("Bozor kapitali", list(MCAP), key="mcap", disabled=not has_f)
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

cols = [c for c in COLS if has_f or c not in FUND_COLS]
h = st.columns([3, 1, 1])
h[0].subheader(f"{S['date']}: {len(x)} ta aksiya topildi")
sort_name = h[1].selectbox("Saralash", [COLS[c] for c in cols], index=cols.index("chg"))
asc = h[2].checkbox("O'sish tartibida", value=False)
inv = {v: k for k, v in COLS.items()}
x = x.sort_values(inv[sort_name], ascending=asc)
show = x[cols].rename(columns=COLS).round(2).reset_index(drop=True)

if show.empty:
    st.info("Bu filtrlar bo'yicha aksiya topilmadi. Filtrlarni yumshating.")
    st.stop()

sel = None
try:
    ev = stretch(st.dataframe, show, height=480, hide_index=True, on_select="rerun", selection_mode="single-row")
    if ev.selection.rows:
        sel = show.iloc[ev.selection.rows[0]]["Ticker"]
except TypeError:
    stretch(st.dataframe, show, height=480, hide_index=True)
st.download_button("CSV yuklab olish", show.to_csv(index=False).encode(), "screener.csv", "text/csv")

st.divider()
tickers = list(show["Ticker"])
pick = st.selectbox("Grafik uchun ticker (jadvaldan qator ham bosish mumkin)", tickers,
                    index=tickers.index(sel) if sel in tickers else 0)
row = x[x["ticker"] == pick].iloc[0]
m = st.columns(6)
m[0].metric("Narx", f"${row['close']:.2f}", f"{row['chg']:+.1f}%")
m[1].metric("Nisbiy hajm", f"{row['relvol']:.1f}x" if pd.notna(row["relvol"]) else "-")
m[2].metric("RSI", f"{row['rsi']:.0f}" if pd.notna(row["rsi"]) else "-")
m[3].metric("ATR", f"{row['atrp']:.1f}%" if pd.notna(row["atrp"]) else "-")
m[4].metric("52h cho'qqidan", f"{row['hid']:.0f}%" if pd.notna(row["hid"]) else "-")
m[5].metric("Sektor", str(row["sector"]))
hist = p[p["ticker"] == pick].tail(180)
stretch(st.altair_chart, candle(hist))
st.markdown(f"Yangiliklar: [Yahoo](https://finance.yahoo.com/quote/{pick}/news) | "
            f"[Finviz](https://finviz.com/quote.ashx?t={pick}) | "
            f"[TradingView](https://www.tradingview.com/symbols/{pick}/)")

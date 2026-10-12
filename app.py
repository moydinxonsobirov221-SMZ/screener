import sqlite3, pandas as pd, streamlit as st
st.set_page_config(layout="wide", page_title="Screener")

@st.cache_data
def load():
    con = sqlite3.connect("data.db")
    p = pd.read_sql("select * from prices", con)
    t = pd.read_sql("select * from tickers", con)
    p = p.sort_values(["ticker", "date"])
    g = p.groupby("ticker")
    p["prev_close"] = g["close"].shift(1)
    p["chg"] = (p["close"] / p["prev_close"] - 1) * 100
    p["gap"] = (p["open"] / p["prev_close"] - 1) * 100
    p["range"] = (p["high"] / p["low"] - 1) * 100
    p["avgvol"] = g["volume"].transform(lambda s: s.shift(1).rolling(20).mean())
    p["relvol"] = p["volume"] / p["avgvol"]
    return p.merge(t, on="ticker"), t

p, t = load()
st.title("Penny Stock Screener")
dates = sorted(p["date"].unique())
sb = st.sidebar
d = sb.selectbox("Sana", dates[::-1])
chg = sb.slider("O'zgarish % (kun)", -50.0, 200.0, (-50.0, 200.0))
pr = sb.slider("Narx $", 0.0, 10.0, (0.0, 10.0))
vol = sb.number_input("Min hajm", 0, value=0, step=100000)
rv = sb.number_input("Min nisbiy hajm (x)", 0.0, value=0.0, step=0.5)
gap = sb.number_input("Min gap %", -100.0, value=-100.0)
sec = sb.multiselect("Sektor", sorted(t["sector"].unique()))
tk = sb.text_input("Ticker qidirish")
sort = sb.selectbox("Saralash", ["chg", "relvol", "volume", "gap", "close", "range"])

x = p[p["date"] == d]
x = x[x["chg"].between(*chg) & x["close"].between(*pr)]
x = x[(x["volume"] >= vol) & (x["relvol"].fillna(0) >= rv) & (x["gap"] >= gap)]
if sec: x = x[x["sector"].isin(sec)]
if tk: x = x[x["ticker"].str.contains(tk.upper())]
x = x.sort_values(sort, ascending=False)
st.write(f"{d}: {len(x)} ta aksiya topildi")
cols = ["ticker", "sector", "close", "chg", "gap", "range", "volume", "relvol"]
st.dataframe(x[cols].round(2), use_container_width=True, height=700, hide_index=True)

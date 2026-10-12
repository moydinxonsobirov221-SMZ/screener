import os, sys, json, re, time, sqlite3, requests
import pandas as pd, yfinance as yf
from datetime import date, timedelta

TOK = os.environ["TG_TOKEN"]; CID = os.environ["TG_CHAT"]; FK = os.environ["FINNHUB_KEY"]
MODE = sys.argv[1] if len(sys.argv) > 1 else "scan"
tickers = pd.read_csv("tickers.csv")["ticker"].tolist()
con = sqlite3.connect("data.db")

def update():
    ok = 0
    for i in range(0, len(tickers), 50):
        b = tickers[i:i+50]
        try:
            d = yf.download(b, period="1mo", group_by="ticker", auto_adjust=False, progress=False, threads=False)
        except Exception as e:
            print("xato", e); continue
        for s in b:
            try:
                x = (d[s] if len(b) > 1 else d).dropna(subset=["Close"]).reset_index()
                x = x[["Date", "Open", "High", "Low", "Close", "Volume"]]
                x.columns = ["date", "open", "high", "low", "close", "volume"]
                x["date"] = x["date"].dt.strftime("%Y-%m-%d")
                if not len(x): continue
                x.insert(0, "ticker", s)
                con.execute("delete from prices where ticker=? and date>=?", (s, x["date"].min()))
                x.to_sql("prices", con, if_exists="append", index=False)
                ok += 1
            except Exception:
                pass
        con.commit()
        time.sleep(1)
    print("yangilandi:", ok)

POS = {"fda approval":3,"approved":2,"approval":2,"clearance":2,"phase 3":2,"phase 2":1,"topline":2,"positive results":3,"breakthrough":2,"contract":2,"awarded":2,"award":1,"purchase order":2,"partnership":2,"collaboration":1,"definitive agreement":3,"letter of intent":2,"agreement":1,"acquisition":2,"acquire":2,"merger":2,"buyout":3,"takeover":3,"strategic alternatives":2,"record revenue":2,"beats":2,"raises guidance":3,"upgrade":1,"patent":1,"regained compliance":2,"uplist":2,"launch":1,"expands":1}
NEG = ["offering","dilution","registered direct","reverse split","delisting","bankruptcy","lawsuit","investigation","downgrade","going concern","misses","warning","halt","fraud","recall","resigns","default","class action"]
def has(w, t): return re.search(r"\b" + re.escape(w) + r"\b", t) is not None

def send(t):
    requests.post(f"https://api.telegram.org/bot{TOK}/sendMessage", data={"chat_id": CID, "text": t, "disable_web_page_preview": True}, timeout=20)

def news(s):
    a = (date.today() - timedelta(days=1)).isoformat()
    r = requests.get("https://finnhub.io/api/v1/company-news", params={"symbol": s, "from": a, "to": date.today().isoformat(), "token": FK}, timeout=20)
    if r.status_code == 429:
        time.sleep(30); return []
    j = r.json()
    return j if isinstance(j, list) else []

def tech(s):
    x = yf.Ticker(s).history(period="3mo", auto_adjust=False).dropna(subset=["Close"])
    l, p = x.iloc[-1], x.iloc[-2]
    chg = (l["Close"] / p["Close"] - 1) * 100
    rv = l["Volume"] / x["Volume"].iloc[-21:-1].mean()
    up = l["Close"] > x["Close"].iloc[-20:].mean()
    near = x["Close"].min() <= l["Close"] <= x["Close"].min() * 1.6
    pts = int(up) + int(rv >= 1.5) + int(near)
    return chg, rv, l["Close"], {3: "MOS (kuchli)", 2: "O'RTACHA"}.get(pts, "MOS EMAS")

def scan():
    if os.path.exists("sent.json"):
        sent = set(json.load(open("sent.json")))
    else:
        try: sent = {r[0] for r in con.execute("select k from sent")}
        except Exception: sent = set()
    n = 0; now = time.time()
    for s in tickers:
        time.sleep(1.1)
        try:
            for h in news(s):
                if now - h.get("datetime", 0) > 12 * 3600: continue
                key = "n" + str(h.get("id"))
                if key in sent: continue
                sent.add(key)
                t = (h.get("headline", "") + " " + h.get("summary", "")).lower()
                ps = sum(v for k, v in POS.items() if has(k, t))
                ng = sum(3 for w in NEG if has(w, t))
                if ps - ng < 2: continue
                chg, rv, px, tc = tech(s)
                if chg >= 8 or px > 10: continue
                send(f"{s}  ${px:.2f}  bugun {chg:+.1f}%\nYangilik kuchi: {ps} ball (IJOBIY)\nHajm: {rv:.1f}x\nTexnik: {tc}\n\n{h.get('headline','')[:200]}\n{h.get('url','')}")
                n += 1
        except Exception as e:
            print(s, "xato:", e)
    json.dump(sorted(sent)[-5000:], open("sent.json", "w"))
    print("yuborildi:", n)

update() if MODE == "update" else scan()

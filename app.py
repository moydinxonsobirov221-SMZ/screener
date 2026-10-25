"""S.M.Z Stock Screener.

Sections: 1 CONFIG | 2 DATA LAYER | 3 FILTER ENGINE | 4 UI HELPERS | 5 MARKET MAP + BUBBLE MAP |
          6 VIEWS | 7 CHART DIALOG | 8 MAIN
"""
from __future__ import annotations

import email.utils
import html
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pandas as pd
import streamlit as st

try:
    import streamlit.components.v1 as components
except Exception:  # newer framework versions may drop this module
    components = None

S = st.session_state

# =============================================================================
# 1. CONFIG
# =============================================================================
DB_PATH = "data.db"
BRAND = "S.M.Z"
PAGE_TITLE = "S.M.Z | Stock Screener"
FOUNDER_NAME = "Sobirov Moydinxon Zaynobiddinvich"
INF = float("inf")


def _updown(steps) -> dict:
    d = {"Any": None, "Up": (0, INF)}
    d.update({f"Up {s}%": (s, INF) for s in steps})
    d["Down"] = (-INF, 0)
    d.update({f"Down {s}%": (-INF, -s) for s in steps})
    return d


def _over_under(under, over, fmt) -> dict:
    d = {"Any": None}
    d.update({f"Under {fmt(v)}": (0, v) for v in under})
    d.update({f"Over {fmt(v)}": (v, INF) for v in over})
    return d


def _short(v: float) -> str:
    return f"{v / 1e6:g}M" if v >= 1e6 else f"{v / 1e3:g}K"


# ---- option tables: label -> (low, high) range on the matching column ------
CHANGE = _updown([1, 2, 3, 5, 10, 15, 20])
GAP = _updown([1, 2, 3, 5, 10, 15, 20])
PERF = _updown([5, 10, 20, 30, 50])
PRICE = {"Any": None, **{f"Under ${v}": (0, v) for v in (1, 2, 3, 4, 5, 7, 10, 15, 20)},
         **{f"Over ${v}": (v, INF) for v in (1, 2, 3, 4, 5, 7, 10)},
         "$1 to $5": (1, 5), "$1 to $10": (1, 10), "$5 to $10": (5, 10)}
VOLUME = _over_under((5e4, 1e5, 5e5, 7.5e5, 1e6), (5e4, 1e5, 2e5, 3e5, 4e5, 5e5, 7.5e5, 1e6, 2e6, 5e6, 1e7), _short)
RELVOL = _over_under((0.5, 0.75, 1), (0.5, 1, 1.5, 2, 3, 5, 10), lambda v: f"{v:g}")
ATR_OPT = _over_under((0.1, 0.25, 0.5, 1, 1.5, 2), (0.1, 0.25, 0.5, 1, 1.5, 2), lambda v: f"{v:g}")
RSI_OPT = {"Any": None, "Overbought (90)": (90, 100), "Overbought (80)": (80, 100), "Overbought (70)": (70, 100),
           "Overbought (60)": (60, 100), "Oversold (40)": (0, 40), "Oversold (30)": (0, 30),
           "Oversold (20)": (0, 20), "Oversold (10)": (0, 10), "Not Overbought (<60)": (0, 60),
           "Not Overbought (<50)": (0, 50), "Not Oversold (>50)": (50, 100), "Not Oversold (>40)": (40, 100)}


def _sma_opts(n: int) -> dict:
    d = {"Any": None, f"Price below SMA{n}": (-INF, 0)}
    d.update({f"Price {p}% below SMA{n}": (-INF, -p) for p in (10, 20, 30, 40, 50)})
    d[f"Price above SMA{n}"] = (0, INF)
    d.update({f"Price {p}% above SMA{n}": (p, INF) for p in (10, 20, 30, 40, 50)})
    return d


SMA_OPTS = {20: _sma_opts(20), 50: _sma_opts(50), 200: _sma_opts(200)}
HL52 = ["Any", "New High", "New Low", "0-3% below High", "0-5% below High", "0-10% below High",
        "5% or more below High", "10% or more below High", "20% or more below High", "30% or more below High",
        "50% or more below High", "0-3% above Low", "0-5% above Low", "0-10% above Low",
        "5% or more above Low", "10% or more above Low", "20% or more above Low", "30% or more above Low",
        "50% or more above Low", "100% or more above Low"]
MCAP = {"Any": None, "Mega ($200bln and more)": (200000, INF), "Large ($10bln to $200bln)": (10000, 200000),
        "Mid ($2bln to $10bln)": (2000, 10000), "Small ($300mln to $2bln)": (300, 2000),
        "Micro ($50mln to $300mln)": (50, 300), "Nano (under $50mln)": (0, 50),
        "+Large (over $10bln)": (10000, INF), "+Mid (over $2bln)": (2000, INF),
        "+Small (over $300mln)": (300, INF), "+Micro (over $50mln)": (50, INF),
        "-Large (under $200bln)": (0, 200000), "-Mid (under $10bln)": (0, 10000),
        "-Small (under $2bln)": (0, 2000), "-Micro (under $300mln)": (0, 300)}
FLOAT = {"Any": None, **{f"Under {v}M": (0, v) for v in (1, 5, 10, 20, 50, 100)},
         **{f"Over {v}M": (v, INF) for v in (1, 2, 5, 10, 20, 50, 100)}}
SHORT = {"Any": None, "Low (<5%)": (0, 5), "High (>20%)": (20, INF),
         **{f"Under {v}%": (0, v) for v in (5, 10, 15, 20, 25, 30)},
         **{f"Over {v}%": (v, INF) for v in (5, 10, 15, 20, 25, 30)}}
EXCHANGES = ["Any", "AMEX", "NASDAQ", "NYSE"]
# market-cap options focused on small caps (under $2bln)
MCAP_DT = {"Any": None, "Under $2bln": (0, 2000), "Small ($300mln to $2bln)": (300, 2000),
           "Micro ($50mln to $300mln)": (50, 300), "Nano (under $50mln)": (0, 50),
           "Under $300mln": (0, 300), "Over $2bln": (2000, INF)}
EXCH_CODES = {"NMS": "NASDAQ", "NGM": "NASDAQ", "NCM": "NASDAQ", "NAS": "NASDAQ", "NYQ": "NYSE", "NYS": "NYSE",
              "ASE": "AMEX", "PCX": "AMEX", "BTS": "BATS", "BATS": "BATS"}

# default value of every filter widget (the key is also the session_state key)
D = dict(exch="Any", price="Any", avgvol="Any", relvol="Any", chg="Any", gap="Any", atr="Any", rsi="Any",
         s20="Any", s50="Any", s200="Any", hl52="Any", mcap="Any")

SIGNALS = {
    "None": {},
    "Top Gainers": dict(chg="Up", sort_by="Change", sort_asc=False),
    "Top Losers": dict(chg="Down", sort_by="Change", sort_asc=True),
    "New High": dict(hl52="New High", sort_by="Change", sort_asc=False),
    "New Low": dict(hl52="New Low", sort_by="Change", sort_asc=True),
    "Most Volatile": dict(sort_by="ATR", sort_asc=False),
    "Most Active": dict(sort_by="Volume", sort_asc=False),
    "Unusual Volume": dict(relvol="Over 3", sort_by="Rel Volume", sort_asc=False),
    "Overbought": dict(rsi="Overbought (70)", sort_by="RSI", sort_asc=False),
    "Oversold": dict(rsi="Oversold (30)", sort_by="RSI", sort_asc=True),
    "Gap Up": dict(gap="Up 5%", relvol="Over 2", sort_by="Gap", sort_asc=False),
    "Low Price Runner": dict(price="Under $10", relvol="Over 2", chg="Up 3%", sort_by="Change", sort_asc=False),
}

COLS = {"ticker": "Ticker", "sector": "Sector", "industry": "Industry", "mcap": "Market Cap",
        "float_sh": "Float", "short_pct": "Short Float", "close": "Price", "chg": "Change", "gap": "Gap",
        "volume": "Volume", "avgvol": "Avg Volume", "relvol": "Rel Volume", "dvol": "$ Volume", "atr": "ATR",
        "rsi": "RSI", "d20": "SMA20", "d50": "SMA50", "d200": "SMA200", "hid": "52W High", "lod": "52W Low",
        "pw": "Perf Week", "pm": "Perf Month", "pq": "Perf Quarter"}
FUND_COLS = ("mcap", "float_sh", "short_pct")
MINI_COLS = ["ticker", "close", "chg", "relvol"]


def _fmt_cap(v) -> str:
    if pd.isna(v):
        return "-"
    return f"{v / 1000:,.2f}B" if v >= 1000 else f"{v:,.2f}M"


def _fmt_m(v) -> str:
    return "-" if pd.isna(v) else f"{v:,.2f}M"


FMT = {"No.": "{:d}", "Price": "{:.2f}", "Change": "{:+.2f}%", "Gap": "{:+.2f}%", "Volume": "{:,.0f}",
       "Avg Volume": "{:,.0f}", "Rel Volume": "{:.2f}", "$ Volume": "{:,.2f}M", "ATR": "{:.2f}", "RSI": "{:.2f}",
       "SMA20": "{:+.2f}%", "SMA50": "{:+.2f}%", "SMA200": "{:+.2f}%", "52W High": "{:+.2f}%",
       "52W Low": "{:+.2f}%", "Perf Week": "{:+.2f}%", "Perf Month": "{:+.2f}%", "Perf Quarter": "{:+.2f}%",
       "Market Cap": _fmt_cap, "Float": _fmt_m, "Short Float": "{:.2f}%"}
COLOR_COLS = {"Change", "Gap", "SMA20", "SMA50", "SMA200", "Perf Week", "Perf Month", "Perf Quarter"}

# jewel tones: Emerald (positive) / Ruby (negative)
GREEN, RED, GREY = "#10B981", "#EF4444", "#414554"
PALETTE = ["#EF4444", "#C62F3E", "#8B1A2B", "#414554", "#065F46", "#0B8F66", "#10B981"]

CSS = """<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
html,body,.stApp,[data-testid="stAppViewContainer"],[data-testid="stMain"],[data-testid="stMainBlockContainer"],
[data-testid="stHeader"],[data-testid="stSidebar"],[data-testid="stSidebarContent"],[data-testid="stBottom"],
[data-testid="stBottomBlockContainer"]{font-family:Inter,-apple-system,"SF Pro Display","Segoe UI",Arial,sans-serif;letter-spacing:-.005em}
html,body{background:#08080A !important}
.stApp{background:#08080A !important}
[data-testid="stAppViewContainer"],[data-testid="stMain"],[data-testid="stMainBlockContainer"],[data-testid="stHeader"],
[data-testid="stSidebar"],[data-testid="stSidebarContent"],[data-testid="stBottom"],[data-testid="stBottomBlockContainer"]{background:transparent !important}
[data-testid="stHeader"]{height:2.4rem}
[data-testid="stToolbar"],[data-testid="stDecoration"],#MainMenu,footer{display:none !important}
.block-container{padding:2.4rem 1rem 1rem 1rem !important;max-width:100% !important;box-sizing:border-box;
  width:calc(100% - 12px);margin:6px auto 6px auto;background:rgba(13,13,16,.75) !important;
  border:1px solid rgba(255,255,255,.08);border-radius:18px}
div[data-testid="stVerticalBlock"]{gap:.4rem}
div[data-testid="stHorizontalBlock"]{gap:.6rem}
[data-testid="stWidgetLabel"] p,label p{font-size:.72rem !important;font-weight:600;color:#9a9a9a !important;margin:0}
div[data-baseweb="select"] > div,.stTextInput input,.stNumberInput input{background:#000 !important;
  border:1px solid #262626 !important;border-radius:4px;min-height:30px;font-size:.8rem;color:#e6e6e6}
[data-testid="stExpander"] details{background:transparent !important;border:0 !important}
[data-testid="stExpander"] summary{padding:.2rem 0}
[data-testid="stTabs"] [role="tablist"]{border-bottom:1px solid #1a1a1a}
[data-testid="stTabs"] button{background:transparent !important;font-size:.82rem}
[data-testid="stForm"]{border:0 !important;padding:0 !important;background:transparent}
div[data-testid="stDialog"] div[role="dialog"]{width:98vw !important;max-width:98vw !important;
  background:#000 !important;border:1px solid #151515}
.smz-brand{line-height:1.1}
.smz-brand .logo{font-size:27px;font-weight:800;letter-spacing:2px;color:#fff;font-family:Arial,Helvetica,sans-serif}
.smz-brand .logo span{color:#26a69a}
.fdr{position:relative;display:inline-block;font-size:11px;color:#8b95a5;cursor:pointer;outline:none;
  letter-spacing:.4px;font-family:Arial,Helvetica,sans-serif}
.fdr .fname{display:none;position:absolute;left:0;top:100%;margin-top:3px;white-space:nowrap;background:#000;
  border:1px solid #1f1f1f;border-radius:6px;padding:6px 10px;color:#e6edf3;font-size:12px;
  box-shadow:0 8px 20px rgba(0,0,0,.9)}
.fdr:hover .fname,.fdr:focus .fname,.fdr:focus-within .fname{display:block}
.kpis{display:flex;gap:6px;flex-wrap:wrap;margin:2px 0 4px 0}
.kpi{background:#000;border:1px solid #141414;border-radius:6px;padding:4px 10px;min-width:98px}
.kpi span{display:block;font-size:10px;color:#8b95a5;text-transform:uppercase;letter-spacing:.4px}
.kpi b{font-size:16px;color:#e6edf3}
.bar{display:flex;height:8px;border-radius:4px;overflow:hidden;margin:2px 0 6px 0}
.legend{display:flex;max-width:520px;margin:4px 0 8px 0}
.legend div{flex:1;text-align:center;font-size:10px;color:#fff;padding:2px 0}
.ttl{font-size:.78rem;font-weight:700;color:#cfd6e0;margin:2px 0 0 0}
.chartbar{font-size:.85rem;color:#cfd6e0;margin-bottom:2px}
.smz-foot{position:fixed;left:16px;bottom:8px;font-size:.8rem;color:#8b95a5;z-index:5}
.tape{overflow:hidden;white-space:nowrap;border-top:1px solid #5c616b;border-bottom:1px solid #5c616b;background:#000;padding:11px 0;margin:2px 0 14px 0}
.tape-in{display:inline-block;animation:tapemove linear infinite}
.tape:hover .tape-in{animation-play-state:paused}
@keyframes tapemove{from{transform:translateX(0)}to{transform:translateX(-50%)}}
.tp-i{display:inline-flex;align-items:baseline;gap:10px;padding:0 30px;border-right:1px solid #2a2d33;font-size:1.05rem;color:#c4c8cf}
.tp-i b{color:#e6e6e6;font-weight:700}
.tp-i em{font-style:normal;font-weight:600}
.nlist{margin-top:6px}
.nw{display:flex;gap:16px;padding:14px 0;border-bottom:1px solid #5c616b}
.nimg{flex:0 0 230px;height:128px;border-radius:8px;overflow:hidden;background:#0b0b0b;border:1px solid #2a2d33;
  display:flex;align-items:center;justify-content:center}
.nimg img{width:100%;height:100%;object-fit:cover}
.nimg b{color:#8b95a5;font-size:1.3rem;letter-spacing:1px}
.nbody{flex:1;min-width:0}
.nmeta{font-size:.78rem;color:#8b95a5;margin-bottom:5px}
.nttl{display:block;font-size:1.05rem;font-weight:700;color:#e6e6e6 !important;text-decoration:none !important;line-height:1.3;margin-bottom:5px}
.nttl:hover{text-decoration:underline !important}
.nsum{font-size:.85rem;color:#9a9fa8;line-height:1.4;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.nsyms{margin-top:7px;font-size:.78rem;color:#8b95a5}
.nchip{display:inline-block;border:1px solid #5c616b;border-radius:5px;padding:1px 8px;margin:0 4px 2px 0;color:#c4c8cf;font-size:.78rem;font-weight:600}
.nchip em{font-style:normal;margin-left:5px}
.slist{margin-top:8px;max-width:540px;border:1px solid #5c616b;border-radius:4px;overflow:hidden}
.slist .r{display:grid;grid-template-columns:52px 1fr 1fr 1fr;border:0;border-bottom:1px solid #5c616b;margin:0;
  font-size:.92rem;line-height:1.2;color:#c4c8cf}
.slist .r:last-child{border-bottom:0}
.slist .r span{padding:6px 14px;border-left:1px solid #5c616b;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.slist .r span:first-child{border-left:0;text-align:center;opacity:.7}
.slist .r.h{font-size:.74rem;font-weight:600;color:#8b95a5}
.st-key-secbox{border:1px solid #ffffff;border-radius:8px;padding:10px 12px 6px 12px;margin-top:6px}
.st-key-secbox button{background:#000 !important;border:1px solid #333 !important;color:#e6e6e6 !important;
  min-height:30px;font-size:.78rem;padding:2px 6px}
.st-key-secbox button:hover{border-color:#fff !important}
.st-key-secbox [data-testid="stBaseButton-primary"]{background:#ffffff !important;color:#000 !important;
  border-color:#ffffff !important;font-weight:700}

/* ---- floating liquid-glass sheets over the matte black canvas ---- */
.tape,.slist,.st-key-secbox,.st-key-wheelpanel,.kpi,div[data-testid="stDialog"] div[role="dialog"]{
  background:rgba(13,13,16,.75) !important;border:1px solid rgba(255,255,255,.08) !important;border-radius:14px;
  -webkit-backdrop-filter:blur(30px) saturate(170%);backdrop-filter:blur(30px) saturate(170%);
  box-shadow:0 10px 36px rgba(0,0,0,.5),inset 1px 1px 0 rgba(255,255,255,.07);
  transition:box-shadow .3s ease,border-color .3s ease}
.tape{padding:11px 0 !important}
.kpi{border-radius:12px}
.slist .r{border-bottom-color:rgba(255,255,255,.08) !important}
.slist .r span{border-left-color:rgba(255,255,255,.08) !important}
.tp-i{border-right-color:rgba(255,255,255,.08) !important}

/* ---- jewel tones: Emerald (positive) / Ruby (negative) ---- */
.gem-g,.gem-r,.gem-n{display:inline-block;font-style:normal}
.gem-g{color:#10B981;text-shadow:0 0 6px rgba(16,185,129,.25)}
.gem-r{color:#EF4444;text-shadow:0 0 6px rgba(239,68,68,.25)}
.gem-n{color:#c4c8cf}
.st-key-secbox button{border-radius:10px !important;transition:all .25s ease}
.st-key-secbox button:hover{box-shadow:0 0 18px rgba(255,255,255,.10)}
.st-key-secbox [data-testid="stBaseButton-primary"]{box-shadow:0 0 22px rgba(255,255,255,.16)}

/* ---- single-row navigation bar ---- */
.smz-brand{position:relative;z-index:60}
.smz-brand .logo{font-size:24px}
.smz-brand .logo span{color:#10B981}
.st-key-topbar{padding:2px 6px 10px 6px;margin:0 0 8px 0;border-bottom:1px solid rgba(255,255,255,.08)}
.st-key-topbar div[data-testid="stHorizontalBlock"]{align-items:center;gap:1rem}
.st-key-topbar [data-testid="stMarkdownContainer"] p{margin:0}
.st-key-nav [data-testid="stButtonGroup"]{background:transparent !important;border:0 !important;padding:0 !important;gap:4px}
.st-key-nav button{background:transparent !important;border:0 !important;border-radius:0 !important;
  box-shadow:none !important;min-height:34px;padding:6px 14px;color:#7d8594 !important;
  font-size:.92rem;font-weight:500;letter-spacing:.2px;transition:color .2s ease}
.st-key-nav button p{color:inherit !important;font-size:.92rem;font-weight:inherit;margin:0}
.st-key-nav button:hover{color:#e6e6e6 !important}
.st-key-nav button[kind="segmented_controlActive"],.st-key-nav button[data-testid="stBaseButton-segmented_controlActive"]{
  color:#ffffff !important;font-weight:600;box-shadow:inset 0 -1px 0 #10B981 !important}
.st-key-topbar div[data-baseweb="select"] > div,.st-key-topbar .stTextInput input{
  background:rgba(255,255,255,.04) !important;border:1px solid rgba(255,255,255,.08) !important;
  border-radius:999px;min-height:34px;padding-left:14px}
.st-key-topbar .stTextInput input:focus{border-color:rgba(16,185,129,.55) !important;box-shadow:0 0 0 1px rgba(16,185,129,.25)}
.st-key-open_chart_form{padding:0 !important}
.st-key-topbar [data-testid="stFormSubmitButton"] button{background:transparent !important;
  border:1px solid rgba(16,185,129,.5) !important;color:#10B981 !important;border-radius:999px;
  min-height:34px;padding:0 20px;font-weight:600;letter-spacing:.3px;transition:all .25s ease}
.st-key-topbar [data-testid="stFormSubmitButton"] button:hover{background:rgba(16,185,129,.12) !important;
  box-shadow:0 0 16px rgba(16,185,129,.25)}
.st-key-wheelpanel{padding:6px 4px 0 4px;margin-top:4px}

/* ---- liquid-glass dropdowns, inputs and buttons ---- */
div[data-baseweb="select"] > div,.stTextInput input,.stNumberInput input{
  background:rgba(20,20,25,.6) !important;-webkit-backdrop-filter:blur(15px);backdrop-filter:blur(15px);
  border:1px solid rgba(255,255,255,.05) !important;border-radius:10px;min-height:34px;
  font-size:.82rem;font-weight:500;color:#e8eaee !important;transition:border-color .25s ease,box-shadow .25s ease}
div[data-baseweb="select"] > div:hover,.stTextInput input:hover{border-color:rgba(255,255,255,.12) !important}
div[data-baseweb="select"] > div:focus-within,.stTextInput input:focus{border-color:rgba(16,185,129,.45) !important;
  box-shadow:0 0 0 1px rgba(16,185,129,.18)}
div[data-baseweb="select"] *{color:#e8eaee}
div[data-baseweb="select"] svg{fill:#8b95a5}
.stTextInput input::placeholder{color:#5d6472}
div[data-baseweb="popover"] > div,div[data-baseweb="menu"],ul[role="listbox"]{
  background:rgba(20,20,25,.92) !important;-webkit-backdrop-filter:blur(15px);backdrop-filter:blur(15px);
  border:1px solid rgba(255,255,255,.05);border-radius:10px}
li[role="option"]{background:transparent !important;color:#e8eaee !important;font-size:.82rem}
li[role="option"]:hover,li[role="option"][aria-selected="true"]{background:rgba(255,255,255,.06) !important}
.stButton button,.stDownloadButton button,[data-testid="stDownloadButton"] button,
button[data-testid="stBaseButton-secondary"]{background:rgba(20,20,25,.6) !important;
  -webkit-backdrop-filter:blur(15px);backdrop-filter:blur(15px);border:1px solid rgba(255,255,255,.05) !important;
  border-radius:10px;min-height:34px;color:#e8eaee !important;font-size:.82rem;font-weight:500;
  transition:all .25s ease}
.stButton button:hover,.stDownloadButton button:hover,[data-testid="stDownloadButton"] button:hover,
button[data-testid="stBaseButton-secondary"]:hover{border-color:rgba(16,185,129,.4) !important;
  background:rgba(30,30,36,.7) !important;box-shadow:0 0 14px rgba(16,185,129,.12)}
[data-testid="stCheckbox"] label p{font-size:.82rem !important;color:#c4c8cf !important}

/* ---- screener: filter grid, control bar, table panel ---- */
.st-key-filtergrid,.st-key-tablepanel{background:rgba(13,13,16,.75) !important;border:1px solid rgba(255,255,255,.08);
  border-radius:14px;-webkit-backdrop-filter:blur(30px) saturate(170%);backdrop-filter:blur(30px) saturate(170%);
  box-shadow:0 10px 36px rgba(0,0,0,.5),inset 1px 1px 0 rgba(255,255,255,.07)}
.st-key-filtergrid{padding:10px 16px 12px 16px;margin:2px 0 8px 0}
.st-key-tablepanel{padding:4px;overflow:hidden}
.st-key-filtergrid [data-testid="stTabs"] [role="tablist"]{gap:6px;border-bottom:1px solid rgba(255,255,255,.06)}
.st-key-filtergrid [data-testid="stTabs"] button{color:#7d8594;font-weight:500;padding:6px 14px;border-radius:0;
  background:transparent !important;border:0 !important;min-height:0}
.st-key-filtergrid [data-testid="stTabs"] button p{font-size:.86rem;color:inherit !important;margin:0}
.st-key-filtergrid [data-testid="stTabs"] button[aria-selected="true"]{color:#fff;font-weight:600}
.st-key-filtergrid [data-baseweb="tab-highlight"]{background:#10B981 !important;height:1px !important}
.st-key-filtergrid [data-baseweb="tab-border"]{background:transparent !important}
.st-key-filtergrid [data-baseweb="tab-panel"]{min-height:86px;padding-top:12px;
  animation:smzfade .38s cubic-bezier(0.16,1,0.3,1)}
@keyframes smzfade{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.st-key-filtergrid div[data-testid="stHorizontalBlock"]{gap:.8rem}
.fsec{font-size:1.25rem;font-weight:600;letter-spacing:-.02em;color:rgba(255,255,255,.95);
  font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","Inter",sans-serif;
  margin:22px 0 10px 0;padding-bottom:10px;border-bottom:1px solid rgba(255,255,255,.07)}
.resline{font-size:.82rem;color:#9aa0a8;line-height:1.2}
.resline b{color:#e6e8ec;font-weight:600}
.st-key-ctrlbar{margin:4px 0 8px 0}
.st-key-ctrlbar div[data-testid="stHorizontalBlock"]{align-items:center}
.st-key-ctrlbar [data-testid="stCheckbox"]{display:flex;align-items:center;min-height:34px}
.st-key-ctrlbar [data-testid="stSelectbox"] > div{margin:0}

/* ---- Apple-grade navigation ---- */
.st-key-nav button{font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","Inter",sans-serif !important;
  color:#ffffff !important;opacity:.55;min-height:38px;padding:6px 16px;
  border-bottom:2px solid transparent !important;box-shadow:none !important;
  transition:opacity .35s cubic-bezier(0.16,1,0.3,1),border-color .35s cubic-bezier(0.16,1,0.3,1),
             box-shadow .35s cubic-bezier(0.16,1,0.3,1)}
.st-key-nav button p{font-family:inherit !important;font-size:1.15rem !important;font-weight:500 !important;
  letter-spacing:-.01em;color:inherit !important;margin:0}
.st-key-nav button:hover{opacity:.85}
.st-key-nav button[kind="segmented_controlActive"],.st-key-nav button[data-testid="stBaseButton-segmented_controlActive"]{
  opacity:1;border-bottom:2px solid #10B981 !important;font-weight:500;
  box-shadow:0 9px 14px -10px rgba(16,185,129,.75) !important}
</style>"""

SEO_DOC = """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body style="margin:0;background:#000"><script>
(function () { try {
  var d = window.parent.document;
  if (d.head.querySelector("meta[data-smz]")) { return; }
  var title = "S.M.Z | Stock Screener";
  var desc = "S.M.Z (SMZ) is a stock screener and market map. Filter stocks by price, volume, gap, RSI, moving averages and more.";
  ["description", "keywords"].forEach(function (n) {
    d.head.querySelectorAll('meta[name="' + n + '"]').forEach(function (x) { x.remove(); }); });
  ["og:title", "og:description", "og:site_name"].forEach(function (n) {
    d.head.querySelectorAll('meta[property="' + n + '"]').forEach(function (x) { x.remove(); }); });
  function add(a) { var m = d.createElement("meta"); for (var k in a) { m.setAttribute(k, a[k]); }
    m.setAttribute("data-smz", "1"); d.head.appendChild(m); }
  add({name: "description", content: desc});
  add({name: "keywords", content: "SMZ, S.M.Z, SMZ stock screener, S.M.Z screener, stock screener, stock map"});
  add({name: "robots", content: "index,follow"});
  add({name: "application-name", content: "S.M.Z"});
  add({property: "og:title", content: title}); add({property: "og:site_name", content: "S.M.Z"});
  add({property: "og:description", content: desc}); add({property: "og:type", content: "website"});
  add({property: "og:url", content: d.location.href});
  add({name: "twitter:card", content: "summary"}); add({name: "twitter:title", content: title});
  var s = d.createElement("script"); s.type = "application/ld+json"; s.setAttribute("data-smz", "1");
  s.text = JSON.stringify({"@context": "https://schema.org", "@type": "WebSite", "name": "S.M.Z",
    "alternateName": ["SMZ", "S.M.Z"], "url": d.location.origin + "/", "description": desc});
  d.head.appendChild(s);
  d.title = title;
} catch (e) {} })();
</script></body></html>"""

CHART_TEMPLATE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<style>html,body{margin:0;padding:0;background:#000;overflow:hidden}</style></head><body>
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


BUBBLE_HTML = r"""<!DOCTYPE html><html><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@500;600;700;800&display=swap" rel="stylesheet">
<style>
html,body{margin:0;padding:0;background:transparent;overflow:hidden;font-family:Inter,-apple-system,"SF Pro Display",Arial,Helvetica,sans-serif;color:#e6e6e6;letter-spacing:-.005em}
svg{display:block;margin:0 auto;background:transparent;cursor:grab;touch-action:none;user-select:none;-webkit-user-select:none;
  transform:translate3d(0,0,0);will-change:transform;backface-visibility:hidden}
.ctr{transform:rotate(calc(var(--rot,0deg) * -1))}
.leaf{cursor:pointer}
text.t{fill:#f4f6f8;font-weight:700;pointer-events:none;paint-order:stroke;stroke:rgba(0,0,0,.5);stroke-width:2px;stroke-linejoin:round}
text.p{fill:#eef1f4;font-weight:600;pointer-events:none;paint-order:stroke;stroke:rgba(0,0,0,.45);stroke-width:1.6px;stroke-linejoin:round}
text.ring{fill:#fff;font-weight:700;pointer-events:none}
text.num{fill:#fff;fill-opacity:.9;font-weight:600;pointer-events:none}
#tip{position:fixed;pointer-events:none;display:none;background:rgba(13,13,16,.75);-webkit-backdrop-filter:blur(30px) saturate(170%);backdrop-filter:blur(30px) saturate(170%);
  border:1px solid rgba(255,255,255,.08);border-radius:10px;padding:8px 12px;font-size:12px;line-height:1.5;z-index:10;box-shadow:0 12px 40px rgba(0,0,0,.6)}
#legend{position:fixed;left:10px;bottom:8px;font-size:10px;color:#9a9a9a}
#legend .strip{display:flex;width:470px;margin-top:3px}
#legend .strip span{flex:1;text-align:center;padding:3px 0}
#msg{position:fixed;top:45%;left:0;right:0;text-align:center;color:#9a9a9a;font-size:14px;display:none}
#snd{position:fixed;top:10px;right:14px;border:0;color:#fff;border-radius:10px;font-size:13px;font-weight:700;
  padding:4px 12px;cursor:pointer;z-index:11;background:#0B8F66}
#snd.off{background:#B3263A}
#snd:hover{filter:brightness(1.15)}
.side{position:fixed;display:none;z-index:5;font-size:12px;box-sizing:border-box;border:1px solid rgba(255,255,255,.08);border-radius:14px;
  padding:12px 10px 8px 10px;background:rgba(13,13,16,.75);
  -webkit-backdrop-filter:blur(30px) saturate(170%);backdrop-filter:blur(30px) saturate(170%);box-shadow:0 10px 36px rgba(0,0,0,.5),inset 1px 1px 0 rgba(255,255,255,.07)}
.side .hd{font-weight:700;font-size:14px;letter-spacing:.3px;padding:0 2px 6px 2px;white-space:nowrap}
.side .hd{display:inline-block}
.side .hd.g{color:#10B981;text-shadow:0 0 6px rgba(16,185,129,.25)}
.side .hd.r{color:#EF4444;text-shadow:0 0 6px rgba(239,68,68,.25)}
.side .row{display:flex;align-items:center;box-sizing:border-box;margin:0;padding:0;border:0;background:transparent;
  color:#e6e6e6;font-weight:500;cursor:pointer;overflow:hidden;white-space:nowrap}
.side .row{transition:background .2s ease}
.side .row:hover{background:rgba(255,255,255,.08)}
.side .row.cols{cursor:default;color:#8b95a5;font-size:.82em;font-weight:600;height:20px}
.side .row.cols:hover{background:transparent}
.side .row span{display:block;box-sizing:border-box;padding:0 8px;overflow:hidden;text-overflow:ellipsis;border:0}
.side .rk{width:30px;text-align:center;padding:0;color:#8b95a5;font-weight:400}
.side .tk{width:74px;font-weight:700}
.side .px{width:68px;text-align:right;font-variant-numeric:tabular-nums}
.side .sc{flex:1;color:#9a9fa8;font-weight:400}
.side .pc{width:84px;text-align:right;font-variant-numeric:tabular-nums;font-weight:700}
.side .row.g .pc{color:#10B981;text-shadow:0 0 6px rgba(16,185,129,.25)}
.side .row.r .pc{color:#EF4444;text-shadow:0 0 6px rgba(239,68,68,.25)}
</style></head><body><svg id="svg"></svg><div id="tip"></div><div id="legend"></div><div id="msg"></div><div id="gl" class="side"></div><div id="ll" class="side"></div>
<button id="snd" title="Dial sound on/off">&#9835; ON</button>
<script>
(function () {
  "use strict";
  var URLS = ["https://cdn.jsdelivr.net/npm/d3@7.9.0/dist/d3.min.js",
              "https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js",
              "https://unpkg.com/d3@7.9.0/dist/d3.min.js"];
  var GREEN = ["#10B981", "#0B8F66", "#065F46"], RED = ["#EF4444", "#C62F3E", "#8B1A2B"];
  var EXP = 0.32, CLICK_DEG = 3.6;
  var args = null, lastKey = null, resizeTimer = null;
  var rot = 0, vel = 0, raf = null, lastStep = 0, applyRot = function () {};
  var audio = null, muted = false, lastClickT = 0;

  function post(type, extra) {
    window.parent.postMessage(Object.assign({isStreamlitMessage: true, type: type}, extra || {}), "*");
  }
  function setHeight(h) { post("streamlit:setFrameHeight", {height: h}); }
  function setValue(v) { post("streamlit:setComponentValue", {value: v, dataType: "json"}); }
  function frameH() {
    var ph = 0;
    try { ph = window.parent.innerHeight; } catch (e) {}
    return clamp(480, 960, (ph || 900) - 200);
  }
  function showMsg(t) {
    var m = document.getElementById("msg");
    m.textContent = t || ""; m.style.display = t ? "block" : "none";
  }
  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c];
    });
  }
  function fmtCap(m) { return m >= 1000 ? "$" + (m / 1000).toFixed(2) + "B" : "$" + m.toFixed(1) + "M"; }
  function fmtPct(v) { return (v >= 0 ? "+" : "") + v.toFixed(2) + "%"; }
  function clamp(a, b, v) { return Math.max(a, Math.min(b, v)); }

  function loadD3(done) {
    if (window.d3) { done(true); return; }
    var i = 0;
    (function next() {
      if (i >= URLS.length) { done(false); return; }
      var s = document.createElement("script");
      s.src = URLS[i++]; s.onload = function () { done(true); }; s.onerror = next;
      document.head.appendChild(s);
    })();
  }

  /* ---- soft dial "tik" sound (Web Audio, no files needed) ---- */
  var lp = null;
  function ensureAudio() {
    if (!audio) {
      try {
        var AC = window.AudioContext || window.webkitAudioContext;
        audio = AC ? new AC() : null;
        if (audio) {
          var master = audio.createGain(); master.gain.value = 0.4;
          lp = audio.createBiquadFilter(); lp.type = "lowpass"; lp.frequency.value = 1500; lp.Q.value = 0.3;
          lp.connect(master); master.connect(audio.destination);
        }
      } catch (e) { audio = null; }
    }
    if (audio && audio.state === "suspended") { try { audio.resume(); } catch (e) {} }
  }
  function blip(t, f0, f1, vol, dur, type) {
    var o = audio.createOscillator(), g = audio.createGain();
    o.type = type; o.frequency.setValueAtTime(f0, t); o.frequency.exponentialRampToValueAtTime(f1, t + dur);
    g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(vol, t + 0.005);
    g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
    o.connect(g); g.connect(lp); o.start(t); o.stop(t + dur + 0.01);
  }
  /* gentle, round two-part "ti-tok" (soft wooden dial click) */
  function tik() {
    if (!audio || muted || !lp) { return; }
    var now = audio.currentTime;
    if (now - lastClickT < 0.05) { return; }
    lastClickT = now;
    var t = now + 0.002, v = 0.97 + Math.random() * 0.06;
    blip(t, 740 * v, 470 * v, 0.06, 0.036, "sine");
    blip(t + 0.034, 370 * v, 255 * v, 0.05, 0.05, "sine");
  }

  function legend(step, label) {
    var s = +step, f = function (v) { return +v.toFixed(2); };
    var items = [[RED[2], "\u2264 -" + f(2 * s) + "%"], [RED[1], "-" + f(s) + " .. -" + f(2 * s) + "%"],
      [RED[0], "0 .. -" + f(s) + "%"], [GREEN[0], "0 .. +" + f(s) + "%"],
      [GREEN[1], "+" + f(s) + " .. +" + f(2 * s) + "%"], [GREEN[2], "\u2265 +" + f(2 * s) + "%"]];
    document.getElementById("legend").innerHTML =
      "<div>Size: Market Cap &nbsp;|&nbsp; Color: " + esc(label) + " performance &nbsp;|&nbsp; drag to turn the dial</div><div class=\"strip\">" +
      items.map(function (it) { return "<span style=\"background:" + it[0] + ";color:#fff\">" + it[1] + "</span>"; }).join("") + "</div>";
  }

  function allocAngles(areas) {
    var n = areas.length, tot = 2 * Math.PI, amin = Math.min(0.3, tot / n), amax = Math.max(2.9, tot / n);
    var fixed = areas.map(function () { return null; }), ang = [];
    for (var it = 0; it < n + 2; it++) {
      var rem = tot, free = 0;
      fixed.forEach(function (f, i) { if (f !== null) { rem -= f; } else { free += areas[i]; } });
      ang = areas.map(function (a, i) { return fixed[i] !== null ? fixed[i] : (free > 0 ? rem * a / free : 0); });
      var changed = false;
      ang.forEach(function (a, i) {
        if (fixed[i] === null) {
          if (a < amin) { fixed[i] = amin; changed = true; } else if (a > amax) { fixed[i] = amax; changed = true; }
        }
      });
      if (!changed) { break; }
    }
    var sum = ang.reduce(function (p, q) { return p + q; }, 0);
    return ang.map(function (a) { return a * tot / sum; });
  }

  /* ---------- map-like tiling: every company is a straight-edged cell (weighted Voronoi) ---------- */
  function polyArea(p) {
    var a = 0, n = p.length, i, q;
    for (i = 0; i < n; i++) { q = p[(i + 1) % n]; a += p[i][0] * q[1] - q[0] * p[i][1]; }
    return a / 2;
  }
  function polyCentroid(p, area) {
    var cx = 0, cy = 0, n = p.length, i, q, cr;
    for (i = 0; i < n; i++) {
      q = p[(i + 1) % n]; cr = p[i][0] * q[1] - q[0] * p[i][1];
      cx += (p[i][0] + q[0]) * cr; cy += (p[i][1] + q[1]) * cr;
    }
    return [cx / (6 * area), cy / (6 * area)];
  }
  function clipHalf(poly, nx, ny, c) {
    var out = [], n = poly.length, i, a, b, da, db, t;
    for (i = 0; i < n; i++) {
      a = poly[i]; b = poly[(i + 1) % n];
      da = nx * a[0] + ny * a[1] - c; db = nx * b[0] + ny * b[1] - c;
      if (da <= 0) { out.push(a); }
      if ((da < 0 && db > 0) || (da > 0 && db < 0)) {
        t = da / (da - db); out.push([a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])]);
      }
    }
    return out;
  }
  function wedgePolygon(g, R) {
    var pts = [], k, an;
    if (g.full) {
      for (k = 0; k < 120; k++) { an = 2 * Math.PI * k / 120; pts.push([R * Math.cos(an), R * Math.sin(an)]); }
      return pts;
    }
    pts.push([0, 0]);
    var steps = Math.max(8, Math.ceil((g.a1 - g.a0) / 0.04));
    for (k = 0; k <= steps; k++) { an = g.a0 + (g.a1 - g.a0) * k / steps; pts.push([R * Math.cos(an), R * Math.sin(an)]); }
    return pts;
  }
  function cellOf(i, sx, sy, w, base, order) {
    var poly = base, xi = sx[i], yi = sy[i], n = order.length, q, j, dx, dy, d2, d, dmax, k, v;
    dmax = 0;
    for (k = 0; k < poly.length; k++) { v = Math.hypot(poly[k][0] - xi, poly[k][1] - yi); if (v > dmax) { dmax = v; } }
    for (q = 0; q < n; q++) {
      j = order[q]; if (j === i) { continue; }
      dx = sx[j] - xi; dy = sy[j] - yi; d2 = dx * dx + dy * dy; d = Math.sqrt(d2);
      if (d2 + w[i] - w[j] > 2 * d * dmax) { continue; }          /* this plane cannot cut the cell */
      poly = clipHalf(poly, 2 * dx, 2 * dy, sx[j] * sx[j] + sy[j] * sy[j] - xi * xi - yi * yi + w[i] - w[j]);
      if (poly.length < 3) { return []; }
      dmax = 0;
      for (k = 0; k < poly.length; k++) { v = Math.hypot(poly[k][0] - xi, poly[k][1] - yi); if (v > dmax) { dmax = v; } }
    }
    return poly;
  }
  function voronoiSector(g, R) {
    var its = g.items, n = its.length, base = wedgePolygon(g, R), i, j, iter;
    var Atot = Math.abs(polyArea(base)), sx = [], sy = [], w = [];
    its.forEach(function (it, idx) {
      it.T = Atot * it.rho * it.rho / g.S;
      var t = (it.d.ex + 1) / 2, u = (it.d.ey + 1) / 2, an, rad;
      if (g.full) { an = -Math.PI + 2 * Math.PI * t; } else { an = g.a0 + (g.a1 - g.a0) * (0.1 + 0.8 * t); }
      rad = R * (0.2 + 0.76 * Math.sqrt(u));
      sx.push(rad * Math.cos(an) + Math.sin(idx * 12.9898) * 0.6);
      sy.push(rad * Math.sin(an) + Math.cos(idx * 78.233) * 0.6);
      w.push(0);
    });
    function orders() {
      var out = [];
      for (i = 0; i < n; i++) {
        var o = []; for (j = 0; j < n; j++) { o.push(j); }
        (function (ii) { o.sort(function (p, q) { return Math.hypot(sx[p] - sx[ii], sy[p] - sy[ii]) - Math.hypot(sx[q] - sx[ii], sy[q] - sy[ii]); }); })(i);
        out.push(o);
      }
      return out;
    }
    var cells, ords;
    if (n === 1) {
      cells = [base];
    } else {
      for (iter = 0; iter < 100; iter++) {
        ords = orders();
        cells = its.map(function (_, ii) { return cellOf(ii, sx, sy, w, base, ords[ii]); });
        var maxErr = 0, wm = 0;
        for (i = 0; i < n; i++) {
          var poly = cells[i], sa = poly.length >= 3 ? polyArea(poly) : 0, area = Math.abs(sa);
          maxErr = Math.max(maxErr, Math.abs(its[i].T - area) / its[i].T);
          if (area > 1e-6) {
            var c = polyCentroid(poly, sa);
            sx[i] += 0.6 * (c[0] - sx[i]); sy[i] += 0.6 * (c[1] - sy[i]);
          }
          w[i] += 0.8 * (its[i].T - area) / 1.57;
          wm += w[i];
        }
        /* every site must keep owning at least its own point, so no cell can ever vanish */
        for (i = 0; i < n; i++) {
          var need = -Infinity;
          for (j = 0; j < n; j++) {
            if (j === i) { continue; }
            var ddx = sx[i] - sx[j], ddy = sy[i] - sy[j];
            need = Math.max(need, w[j] - (ddx * ddx + ddy * ddy));
          }
          w[i] = Math.max(w[i], need + 0.25 * its[i].T / Math.PI);
        }
        wm = 0; for (i = 0; i < n; i++) { wm += w[i]; }
        wm /= n;
        for (i = 0; i < n; i++) { w[i] -= wm; }
        if (iter > 8 && maxErr < 0.02) { break; }
      }
      ords = orders();
      cells = its.map(function (_, ii) { return cellOf(ii, sx, sy, w, base, ords[ii]); });
    }
    var worst = 0;
    its.forEach(function (it, ii) {
      var poly = cells[ii];
      if (poly.length < 3) { poly = [[sx[ii], sy[ii]], [sx[ii] + 0.5, sy[ii]], [sx[ii], sy[ii] + 0.5]]; }
      var sa = polyArea(poly), area = Math.abs(sa), c = area > 1e-9 ? polyCentroid(poly, sa) : [sx[ii], sy[ii]];
      it.poly = poly; it.area = area; it.cx = c[0]; it.cy = c[1]; it.r = Math.sqrt(area / Math.PI);
      worst = Math.max(worst, Math.abs(it.T - area) / it.T);
    });
    return worst;
  }

  function splitName(nm) {
    var w = nm.split(" ");
    if (w.length < 2) { return [nm]; }
    var best = null, i;
    for (i = 1; i < w.length; i++) {
      var a = w.slice(0, i).join(" "), b = w.slice(i).join(" "), m = Math.max(a.length, b.length);
      if (!best || m < best.m) { best = {a: a, b: b, m: m}; }
    }
    return [best.a, best.b];
  }

  function stopInertia() { if (raf) { cancelAnimationFrame(raf); raf = null; } }

  function draw(a, force) {
    var H = frameH(), W = document.body.clientWidth || window.innerWidth || 1000;
    var key = JSON.stringify([a.nodes, a.scale, a.perf_label, a.gainers, a.losers, a.mode]) + "|" + W + "|" + H;
    if (!force && key === lastKey) { return; }
    lastKey = key;
    setHeight(H);
    build(a, W, H);
  }

  function build(a, W, H) {
    var nodes = a.nodes || [], scale = +a.scale || 15, step = scale / 3;
    var svg = d3.select("#svg");
    svg.on(".drag", null);
    svg.selectAll("*").remove();
    stopInertia();
    document.getElementById("tip").style.display = "none";
    document.getElementById("gl").style.display = "none"; document.getElementById("ll").style.display = "none";
    if (!nodes.length) { showMsg("No stocks with market cap data for this selection."); return; }
    showMsg("");
    legend(step, a.perf_label || "Today");

    var half = Math.min(W, H) / 2;
    var tb = 20, nb = clamp(18, 22, half * 0.07), Rw = tb + nb, R3 = half - 10, R = R3 - Rw, R2 = R + nb;
    var hubR = clamp(18, 30, R * 0.11);
    svg.attr("width", W).attr("height", H).attr("viewBox", [-W / 2, -H / 2, W, H].join(" "));

    function color(v) { var t = clamp(1, 3, Math.ceil(Math.abs(v) / step - 1e-9)) - 1; return (v >= 0 ? GREEN : RED)[t]; }

    var groups = d3.groups(nodes, function (n) { return n.sector; }).map(function (g) {
      var items = g[1].map(function (n) { return {d: n, rho: Math.pow(n.mcap, EXP), r: 0}; })
        .sort(function (p, q) { return q.rho - p.rho; });
      return {name: g[0], items: items, S: d3.sum(items, function (it) { return it.rho * it.rho; })};
    }).sort(function (p, q) { return q.S - p.S; });
    var n = groups.length;
    var angs = allocAngles(groups.map(function (g) { return g.S; }));
    var cur = -Math.PI / 2;
    groups.forEach(function (g, i) {
      g.a0 = cur; g.a1 = cur + angs[i]; g.am = (g.a0 + g.a1) / 2; g.full = n === 1; cur = g.a1;
    });

    var onion = a.mode === "onion";
    function bandPath(d) {
      var large = (d.a1 - d.a0) > Math.PI ? 1 : 0;
      var P = function (r, an) { return (r * Math.cos(an)).toFixed(2) + "," + (r * Math.sin(an)).toFixed(2); };
      return "M" + P(d.ro, d.a0) + "A" + d.ro.toFixed(2) + "," + d.ro.toFixed(2) + " 0 " + large + " 1 " + P(d.ro, d.a1) +
        "L" + P(d.ri, d.a1) + "A" + d.ri.toFixed(2) + "," + d.ri.toFixed(2) + " 0 " + large + " 0 " + P(d.ri, d.a0) + "Z";
    }
    var cvm = document.createElement("canvas").getContext("2d");
    if (onion) {
    var rIn0 = hubR + 3;
    groups.forEach(function (g) {
      var its = g.items, m = its.length, total = R - rIn0, minT = Math.min(3, total / m);
      var wts = its.map(function (it) { return it.rho; });
      var fixed = wts.map(function () { return false; }), th = [], k;
      for (k = 0; k < m + 2; k++) {
        var rem = total, free = 0;
        fixed.forEach(function (f, q) { if (f) { rem -= minT; } else { free += wts[q]; } });
        th = wts.map(function (w, q) { return fixed[q] ? minT : (free > 0 ? rem * w / free : 0); });
        var ch = false;
        th.forEach(function (t, q) { if (!fixed[q] && t < minT) { fixed[q] = true; ch = true; } });
        if (!ch) { break; }
      }
      var rr = R;
      its.forEach(function (it, q) {
        it.ro = rr; it.ri = rr - th[q]; rr = it.ri; it.th = th[q];
        it.a0 = g.a0; it.a1 = g.full ? g.a0 + 2 * Math.PI - 0.0002 : g.a1;
      });
    });
    } else {
      var geoErr = 0;
      groups.forEach(function (g) { geoErr = Math.max(geoErr, voronoiSector(g, R)); });
      window.__geoErr = geoErr;
    }

    var defs = svg.append("defs");
    var gradIds = {};
    function fillFor(v) {
      var c = color(v);
      if (!gradIds[c]) {
        var id = "gr" + Object.keys(gradIds).length; gradIds[c] = id;
        var gg = defs.append("radialGradient").attr("id", id).attr("cx", "50%").attr("cy", "42%").attr("r", "75%");
        gg.append("stop").attr("offset", "0%").attr("stop-color", d3.color(c).brighter(0.5).formatHex());
        gg.append("stop").attr("offset", "58%").attr("stop-color", c);
        gg.append("stop").attr("offset", "100%").attr("stop-color", d3.color(c).darker(0.6).formatHex());
      }
      return "url(#" + gradIds[c] + ")";
    }
    var rg = svg.append("g");          /* the whole dial: everything inside turns together */
    var NS = "vector-effect";

    /* ---- tick scale (every ~1 mm, like a safe dial) ---- */
    var nT = Math.max(250, 50 * Math.round(2 * Math.PI * R3 / (4.8 * 50))), stp = 2 * Math.PI / nT, minorP = "", midP = "", majP = "", i;
    for (i = 0; i < nT; i++) {
      var an = -Math.PI / 2 + i * stp, c = Math.cos(an), s = Math.sin(an);
      var len = i % 50 === 0 ? 11 : (i % 5 === 0 ? 8 : 5);
      var seg = "M" + (R3 * c).toFixed(1) + " " + (R3 * s).toFixed(1) + "L" + ((R3 - len) * c).toFixed(1) + " " + ((R3 - len) * s).toFixed(1);
      if (i % 50 === 0) { majP += seg; } else if (i % 5 === 0) { midP += seg; } else { minorP += seg; }
    }
    rg.append("circle").attr("r", R3).attr("fill", "none").attr("stroke", "#ffffff").attr("stroke-opacity", 0.7)
      .attr("stroke-width", 1.2).style(NS, "non-scaling-stroke");
    [[minorP, 0.8, 0.55], [midP, 1.1, 0.85], [majP, 1.8, 1]].forEach(function (t) {
      rg.append("path").attr("d", t[0]).attr("fill", "none").attr("stroke", "#ffffff").attr("stroke-width", t[1])
        .attr("stroke-opacity", t[2]).style(NS, "non-scaling-stroke");
    });
    rg.append("circle").attr("r", R2).attr("fill", "none").attr("stroke", "#ffffff").attr("stroke-opacity", 0.6)
      .attr("stroke-width", 1).style(NS, "non-scaling-stroke");
    rg.append("circle").attr("r", R).attr("fill", "rgba(5,5,8,.88)").attr("stroke", "#ffffff").attr("stroke-opacity", 0.7)
      .attr("stroke-width", 1.8).style(NS, "non-scaling-stroke");

    /* ---- map cells ---- */
    var all = [];
    groups.forEach(function (g) { g.items.forEach(function (it) { it.sector = g.name; all.push(it); }); });
    var cells = rg.append("g").selectAll("path").data(all).join("path").attr("class", "leaf")
      .attr("d", function (d) {
        return onion ? bandPath(d) : "M" + d.poly.map(function (p) { return p[0].toFixed(1) + "," + p[1].toFixed(1); }).join("L") + "Z";
      })
      .attr("fill", function (d) { return fillFor(d.d.perf); }).attr("stroke", "rgba(0,0,0,.6)").attr("stroke-width", 1.2)
      .attr("stroke-linejoin", "round").style(NS, "non-scaling-stroke");
    rg.append("circle").attr("r", R).attr("fill", "none").attr("stroke", "#ffffff").attr("stroke-opacity", 0.7)
      .attr("stroke-width", 1.8).style(NS, "non-scaling-stroke").attr("pointer-events", "none");
    var labs = null;
    if (onion) {
      var lblg = rg.append("g").attr("pointer-events", "none");
      all.forEach(function (d, idx) {
        var span = d.a1 - d.a0, rmid = (d.ro + d.ri) / 2, tk = d.d.ticker, pc = fmtPct(d.d.perf);
        var fs = clamp(6, 15, d.th * 0.58), avail = span * rmid * 0.94, txt = null;
        function wOf(t, f) { cvm.font = "700 " + f + "px Arial"; return cvm.measureText(t).width; }
        if (d.th >= 8) {
          if (wOf(tk + "  " + pc, fs) <= avail) { txt = tk + "  " + pc; }
          else if (wOf(tk, fs) <= avail) { txt = tk; }
          else { var f2 = fs * avail / wOf(tk, fs); if (f2 >= 6) { fs = f2; txt = tk; } }
        }
        if (!txt) { return; }
        var w = wOf(txt, fs), rb = rmid - fs * 0.35, hh = Math.min(span / 2, (w / 2 + 6) / rb);
        var P = function (q) { return (rb * Math.cos(q)).toFixed(2) + " " + (rb * Math.sin(q)).toFixed(2); };
        var reps = clamp(1, 6, Math.floor(span * rmid / (w * 2.4))), j;
        for (j = 0; j < reps; j++) {
          var mid = d.a0 + (j + 0.5) * span / reps, id = "lp" + idx + "_" + j;
          defs.append("path").attr("id", id).attr("d", "M " + P(mid - hh) + " A " + rb + " " + rb + " 0 0 1 " + P(mid + hh));
          lblg.append("text").attr("class", "t").attr("font-size", fs)
            .append("textPath").attr("href", "#" + id).attr("startOffset", "50%").attr("text-anchor", "middle").text(txt);
        }
      });
    } else {
      labs = rg.append("g").selectAll("g").data(all).join("g").attr("pointer-events", "none")
        .attr("transform", function (d) { return "translate(" + d.cx.toFixed(1) + "," + d.cy.toFixed(1) + ")"; });
      var ctr = labs.append("g").attr("class", "ctr");
      var tickers = ctr.append("text").attr("class", "t").attr("text-anchor", "middle")
        .text(function (d) { return d.d.ticker; });
      var pcts = ctr.append("text").attr("class", "p").attr("text-anchor", "middle")
        .text(function (d) { return fmtPct(d.d.perf); });
      tickers.each(function (d) {
        var rk = d.r, fs = clamp(6, 16, rk * 0.5);
        d3.select(this).style("display", rk >= 11 ? null : "none").attr("font-size", fs).attr("y", rk >= 22 ? -fs * 0.1 : fs * 0.35);
      });
      pcts.each(function (d) {
        var rk = d.r, fs = clamp(6, 12, rk * 0.32);
        d3.select(this).style("display", rk >= 22 ? null : "none").attr("font-size", fs).attr("y", fs * 1.05 + clamp(0, 6, rk * 0.08));
      });
    }

    /* ---- thin sector lines: from the centre to the end of the name ring only ---- */
    if (n > 1) {
      groups.forEach(function (g) {
        rg.append("line").attr("x1", hubR * Math.cos(g.a0)).attr("y1", hubR * Math.sin(g.a0)).attr("x2", R2 * Math.cos(g.a0)).attr("y2", R2 * Math.sin(g.a0))
          .attr("stroke", "#ffffff").attr("stroke-opacity", 0.6).attr("stroke-width", 0.8).style(NS, "non-scaling-stroke");
      });
    }

    /* ---- sector names: up to 2 rows, always inside their own wedge ---- */
    var cv = document.createElement("canvas").getContext("2d");
    var rc1 = R + nb * 0.25, rc2 = R + nb * 0.76, rc0 = R + nb * 0.5;
    groups.forEach(function (g, idx) {
      var m = g.am, top = true, lines = [g.name];   /* every name has the same orientation */
      var hh = Math.max(0.04, (g.a1 - g.a0) / 2 - 0.014);
      lines.forEach(function (txt, li) {
        var rc = lines.length === 1 ? rc0 : (top ? (li === 0 ? rc2 : rc1) : (li === 0 ? rc1 : rc2));
        cv.font = "700 12px Arial";
        var w12 = cv.measureText(txt).width;
        var avail = 2 * hh * rc * 0.94;
        var fs = clamp(5, 10, 12 * avail / Math.max(1, w12));
        var rb = top ? rc - fs * 0.35 : rc + fs * 0.35;
        var P = function (q) { return (rb * Math.cos(q)).toFixed(2) + " " + (rb * Math.sin(q)).toFixed(2); };
        var dd = top ? "M " + P(m - hh) + " A " + rb + " " + rb + " 0 0 1 " + P(m + hh)
                     : "M " + P(m + hh) + " A " + rb + " " + rb + " 0 0 0 " + P(m - hh);
        var id = "ringp" + idx + "_" + li;
        defs.append("path").attr("id", id).attr("d", dd);
        rg.append("text").attr("class", "ring").attr("font-size", fs)
          .append("textPath").attr("href", "#" + id).attr("startOffset", "50%").attr("text-anchor", "middle").text(txt);
      });
    });

    /* ---- centre hub (does not turn): only when several sectors are shown ---- */
    if (n > 1) {
      svg.append("circle").attr("r", hubR).attr("fill", "rgba(5,5,8,.92)").style("filter", "drop-shadow(0 0 10px rgba(255,255,255,.28))").attr("stroke", "#ffffff").attr("stroke-opacity", 0.7)
        .attr("stroke-width", 2.6).style("vector-effect", "non-scaling-stroke").attr("pointer-events", "none");
      svg.append("text").attr("text-anchor", "middle").attr("dominant-baseline", "central")
        .attr("font-size", hubR * 0.5).attr("font-weight", 800).attr("fill", "#ffffff").attr("letter-spacing", 0.4)
        .attr("pointer-events", "none").text("S.M.Z");
    }

    /* ---- fixed index mark at the top (does not turn), like on a safe dial ---- */
    svg.append("path").attr("d", "M -6 " + (-R3 - 8) + " L 6 " + (-R3 - 8) + " L 0 " + (-R3 + 7) + " Z")
      .attr("fill", "#ffffff").attr("pointer-events", "none");

    /* ---- turning the dial ---- */
    /* one style write per frame: the dial turns on the GPU, labels counter-rotate through a CSS variable */
    var rgNode = rg.node(), pendingFrame = false;
    rgNode.style.willChange = "transform";
    applyRot = function (deg) {
      var v = deg.toFixed(2) + "deg";
      rgNode.style.setProperty("--rot", v);
      rgNode.style.transform = "rotate(" + v + ") translate3d(0,0,0)";
    };
    function setRot(deg, now) {
      rot = deg;
      var st = Math.floor(rot / CLICK_DEG);
      if (st !== lastStep) { lastStep = st; tik(); }
      if (now) { applyRot(rot); return; }
      if (!pendingFrame) {                      /* coalesce pointer events into one paint per frame */
        pendingFrame = true;
        requestAnimationFrame(function () { pendingFrame = false; applyRot(rot); });
      }
    }
    lastStep = Math.floor(rot / CLICK_DEG);
    applyRot(rot);

    function angAt(ev) { var p = d3.pointer(ev, svg.node()); return Math.atan2(p[1], p[0]); }
    var dragA = 0, lastMoveT = 0;
    function startInertia() {
      var last = performance.now();
      (function stepFn(now) {
        var dt = Math.min(0.05, (now - last) / 1000); last = now;
        setRot(rot + vel * dt, true);
        vel *= Math.exp(-2.4 * dt);
        if (Math.abs(vel) < 6) { raf = null; return; }
        raf = requestAnimationFrame(stepFn);
      })(last);
    }
    svg.call(d3.drag().clickDistance(4)
      .on("start", function (ev) {
        ensureAudio(); stopInertia(); dragA = angAt(ev); vel = 0; lastMoveT = performance.now();
        svg.style("cursor", "grabbing");
      })
      .on("drag", function (ev) {
        var q = angAt(ev), d = q - dragA;
        while (d > Math.PI) { d -= 2 * Math.PI; }
        while (d < -Math.PI) { d += 2 * Math.PI; }
        dragA = q;
        var now = performance.now(), dt = Math.max(1, now - lastMoveT) / 1000; lastMoveT = now;
        var dd = d * 180 / Math.PI;
        setRot(rot + dd);
        vel = 0.6 * vel + 0.4 * (dd / dt);
        document.getElementById("tip").style.display = "none";
      })
      .on("end", function () {
        svg.style("cursor", "grab");
        if (Math.abs(vel) > 25 && performance.now() - lastMoveT < 90) { startInertia(); }
      }));

    /* ---- Top 21 gainers (left) and Top 21 losers (right): ticker, price, sector, change ---- */
    var cellByT = {};
    cells.each(function (d) { cellByT[d.d.ticker] = this; });
    (function () {
      var side = W / 2 - R3, gap = 79, minW = 300;
      if (side - gap < minW) { gap = Math.max(8, side - minW - 6); }
      var wl = clamp(minW, 400, side - gap - 6);
      var top = 44, bottomRes = 52, avail = H - top - bottomRes;
      var rh = clamp(18, 32, (avail - 70) / 21 - 2), total = 70 + 21 * (rh + 2);
      var y0 = top + Math.max(0, (avail - total) / 2);
      var SEC = {"Information Technology": "Info Tech", "Consumer Discretionary": "Cons. Disc.",
                 "Consumer Staples": "Cons. Staples", "Communication Services": "Comm. Svcs",
                 "Health Care": "Health Care", "Real Estate": "Real Estate"};
      function fill(id, rows, title, isGain, x) {
        var el = document.getElementById(id);
        x = clamp(2, Math.max(2, W - wl - 2), x);
        el.style.left = x + "px"; el.style.top = y0 + "px"; el.style.width = wl + "px"; el.style.display = "block";
        el.style.fontSize = clamp(11, 14, rh * 0.58) + "px";
        var h = "<div class=\"hd " + (isGain ? "g" : "r") + "\">" + title + "</div>" +
          "<div class=\"row cols\"><span class=\"rk\">#</span><span class=\"tk\">Ticker</span><span class=\"px\">Last</span>" +
          "<span class=\"sc\">Sector</span><span class=\"pc\">Change</span></div>";
        (rows || []).slice(0, 21).forEach(function (r, i) {
          h += "<div class=\"row " + (isGain ? "g" : "r") + "\" data-t=\"" + esc(r.ticker) + "\" style=\"height:" + rh + "px\">" +
            "<span class=\"rk\">" + (i + 1) + "</span><span class=\"tk\">" + esc(r.ticker) + "</span>" +
            "<span class=\"px\">" + (+r.price).toFixed(2) + "</span>" +
            "<span class=\"sc\">" + esc(SEC[r.sector] || r.sector || "-") + "</span>" +
            "<span class=\"pc\">" + fmtPct(r.perf) + "</span></div>";
        });
        el.innerHTML = h;
        Array.prototype.forEach.call(el.querySelectorAll(".row[data-t]"), function (row) {
          var t = row.getAttribute("data-t");
          row.addEventListener("mouseenter", function () {
            var c = cellByT[t]; if (c) { d3.select(c).raise().attr("stroke", "#ffffff").attr("stroke-width", 2).style("filter", "drop-shadow(0 0 8px rgba(255,255,255,.6))"); }
          });
          row.addEventListener("mouseleave", function () {
            var c = cellByT[t]; if (c) { d3.select(c).attr("stroke", "#000000").attr("stroke-width", 1.2).style("filter", null); }
          });
          row.addEventListener("click", function () { setValue({ticker: t, n: Date.now() + Math.random()}); });
        });
      }
      var lab = esc(a.perf_label || "Today");
      fill("gl", a.gainers, "\u25B2 Top 21 gainers \u00B7 " + lab, true, W / 2 - R3 - gap - wl);
      fill("ll", a.losers, "\u25BC Top 21 losers \u00B7 " + lab, false, W / 2 + R3 + gap);
    })();

    var tip = document.getElementById("tip");
    function tipHtml(d) {
      var x = d.d;
      return "<b>" + esc(x.ticker) + "</b> &nbsp;" + esc(x.sector) + "<br>Market Cap " + fmtCap(x.mcap) + "<br>" +
        esc(a.perf_label || "Today") + " <span style=\"color:" + color(x.perf) + ";text-shadow:0 0 6px " + (x.perf >= 0 ? "rgba(16,185,129,.25)" : "rgba(239,68,68,.25)") + "\">" + fmtPct(x.perf) + "</span>" +
        "<br>Price $" + (+x.price).toFixed(2) + "<br>Volume " + Math.round(x.volume).toLocaleString("en-US");
    }
    cells.on("mouseover", function (ev, d) {
        tip.innerHTML = tipHtml(d); tip.style.display = "block";
        d3.select(this).raise().attr("stroke", "#ffffff").attr("stroke-width", 2).style("filter", "drop-shadow(0 0 8px rgba(255,255,255,.6))");
      })
      .on("mousemove", function (ev) {
        var x = ev.clientX + 14, y = ev.clientY + 14;
        if (x + 230 > window.innerWidth) { x = ev.clientX - 240; }
        if (y + 110 > window.innerHeight) { y = ev.clientY - 110; }
        tip.style.left = Math.max(2, x) + "px"; tip.style.top = Math.max(2, y) + "px";
      })
      .on("mouseout", function () {
        tip.style.display = "none";
        d3.select(this).attr("stroke", "#000000").attr("stroke-width", 1.2).style("filter", null);
      })
      .on("click", function (ev, d) {
        ev.stopPropagation();
        setValue({ticker: d.d.ticker, n: Date.now() + Math.random()});
      });
  }

  document.getElementById("snd").addEventListener("click", function () {
    muted = !muted; ensureAudio();
    this.className = muted ? "off" : "";
    this.innerHTML = "&#9835; " + (muted ? "OFF" : "ON");
  });
  window.addEventListener("message", function (e) {
    if (!e.data || e.data.type !== "streamlit:render") { return; }
    args = e.data.args || {};
    setHeight(frameH());
    loadD3(function (ok) {
      if (!ok) { showMsg("The map library could not load. Check your connection."); return; }
      draw(args, false);
    });
  });
  window.addEventListener("resize", function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () { if (args && window.d3) { draw(args, true); } }, 250);
  });
  post("streamlit:componentReady", {apiVersion: 1});
})();
</script></body></html>"""


EVENTS_DOC = """<!DOCTYPE html><html><head><meta charset="utf-8"><style>html,body{margin:0;background:transparent}</style></head><body>
<div class="tradingview-widget-container"><div class="tradingview-widget-container__widget"></div>
<script type="text/javascript" src="https://s3.tradingview.com/external-embedding/embed-widget-events.js" async>
{"colorTheme": "dark", "isTransparent": true, "locale": "en", "importanceFilter": "-1,0,1",
 "countryFilter": "us,eu,gb,jp,ca,au,nz,ch,cn", "width": "100%", "height": "820"}
</script></div></body></html>"""

PERF_COLS = {"Today": "chg", "Week": "pw", "Month": "pm", "Quarter": "pq"}


# =============================================================================
# 2. DATA LAYER
# =============================================================================
def db_stamp() -> float:
    """Modification time of the database file: a changed file means the cached data is reloaded."""
    try:
        return os.path.getmtime(DB_PATH)
    except Exception:
        return 0.0


@st.cache_data(ttl=21600, show_spinner="Calculating indicators...")
def load_data(stamp: float = 0.0) -> tuple[pd.DataFrame, dict]:
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
    p["atr"] = tr.groupby(p["ticker"]).transform(ewm)
    p["atrp"] = p["atr"] / p["close"] * 100
    delta = g["close"].diff()
    up = delta.clip(lower=0).groupby(p["ticker"]).transform(ewm)
    dn = (-delta).clip(lower=0).groupby(p["ticker"]).transform(ewm)
    p["rsi"] = 100 * up / (up + dn).replace(0, np.nan)
    hi52 = g["high"].transform(lambda s: s.rolling(252, min_periods=40).max())
    lo52 = g["low"].transform(lambda s: s.rolling(252, min_periods=40).min())
    p["hid"] = (p["close"] / hi52 - 1) * 100
    p["lod"] = (p["close"] / lo52 - 1) * 100
    p["newhigh"] = hi52.notna() & (p["high"] >= hi52)
    p["newlow"] = lo52.notna() & (p["low"] <= lo52)
    for name, n in (("pw", 5), ("pm", 21), ("pq", 63)):
        p[name] = (p["close"] / g["close"].shift(n) - 1) * 100

    p = p.merge(t, on="ticker", how="left").merge(f, on="ticker", how="left")
    p["mcap"] = p["mcap"] / 1e6
    p["float_sh"] = p["float_sh"] / 1e6
    p["exch"] = p["exchange"].map(EXCH_CODES)
    meta = {"has_fund": bool(f["mcap"].notna().any()), "has_industry": bool(f["industry"].notna().any()),
            "has_exchange": bool(p["exch"].notna().any())}
    return p, meta


# =============================================================================
# 3. FILTER ENGINE
# =============================================================================
def init_state() -> None:
    for k, v in D.items():
        S.setdefault(k, v)
    S.setdefault("sort_by", "Change")
    S.setdefault("sort_asc", False)


def reset_filters() -> None:
    for k, v in D.items():
        S[k] = v


def apply_signal() -> None:
    reset_filters()
    S["sort_by"], S["sort_asc"] = "Change", False
    for k, v in SIGNALS[S["signal"]].items():
        S[k] = v


def _opt(df, col, table, key):
    r = table.get(key)
    return df if r is None else df[df[col].between(r[0], r[1])]


def _hl52(df: pd.DataFrame, key: str) -> pd.DataFrame:
    if key == "Any":
        return df
    if key == "New High":
        return df[df["newhigh"]]
    if key == "New Low":
        return df[df["newlow"]]
    m = re.fullmatch(r"(\d+)-(\d+)% (below|above) (High|Low)", key)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        return df[df["hid"].between(-hi, -lo)] if m.group(3) == "below" else df[df["lod"].between(lo, hi)]
    m = re.fullmatch(r"(\d+)% or more (below|above) (High|Low)", key)
    if m:
        v = float(m.group(1))
        return df[df["hid"] <= -v] if m.group(2) == "below" else df[df["lod"] >= v]
    return df


def apply_filters(day: pd.DataFrame, s, meta: dict) -> pd.DataFrame:
    """Apply every active screener filter to one day's data. `s` is any mapping of filter values."""
    x = day
    if s["exch"] != "Any":
        x = x[x["exch"] == s["exch"]]
    x = _opt(x, "close", PRICE, s["price"])
    x = _opt(x, "avgvol", VOLUME, s["avgvol"])
    x = _opt(x, "relvol", RELVOL, s["relvol"])
    x = _opt(x, "chg", CHANGE, s["chg"])
    x = _opt(x, "gap", GAP, s["gap"])
    x = _opt(x, "atr", ATR_OPT, s["atr"])
    x = _opt(x, "rsi", RSI_OPT, s["rsi"])
    for n in (20, 50, 200):
        x = _opt(x, f"d{n}", SMA_OPTS[n], s[f"s{n}"])
    x = _hl52(x, s["hl52"])
    if meta["has_fund"]:
        x = _opt(x, "mcap", MCAP_DT, s["mcap"])
    return x


def decide_open(items: list, opened: Optional[str], forced: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Decide which chart dialog (if any) to open on this rerun.

    items: [(selection_token, ticker)] for the table cells currently highlighted;
    opened: token whose dialog was already shown; forced: ticker typed in the search box.
    Returns (ticker_to_open, new_opened). The token guard stops a dialog from re-opening on
    unrelated reruns, while clicking a different cell (even of the same stock) opens it again.
    """
    if forced:
        return forced, opened
    cur = [(t, s) for t, s in items if t]
    new = [(t, s) for t, s in cur if t != opened]
    if new:
        return new[0][1], new[0][0]
    if not cur:
        return None, None
    return None, opened


def clean_symbol(raw: str) -> Optional[str]:
    s = (raw or "").strip().upper()
    return s if re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,9}", s) else None


def chart_symbol(sym: str, exchange) -> str:
    ex = EXCH_CODES.get(str(exchange)) if exchange is not None and not pd.isna(exchange) else None
    return f"{ex}:{sym}" if ex else sym


# =============================================================================
# 4. UI HELPERS
# =============================================================================
def stretch(fn, *a, **k):
    """Call a UI element with full width, working on old and new framework versions."""
    try:
        return fn(*a, width="stretch", **k)
    except TypeError:
        return fn(*a, use_container_width=True, **k)


def keyed(key: str):
    """Container with a CSS class `st-key-<key>` (falls back to a plain container on old versions)."""
    try:
        return st.container(key=key)
    except TypeError:
        return st.container()


def render_iframe(doc: str, height: int, quiet: bool = False) -> None:
    try:
        if hasattr(st, "iframe"):
            st.iframe(doc, height=height)
        elif components is not None:
            components.html(doc, height=height)
        else:
            raise RuntimeError("no iframe support")
    except Exception:
        if not quiet:
            st.warning("The chart could not load in this browser.")


def chart_doc(symbol: str, init_height: int = 700, offset: int = 170) -> str:
    """HTML for the advanced chart widget with an explicit pixel height.

    The height fills the browser window (window height - offset) when the iframe may read it,
    otherwise init_height is used. Pixel sizes avoid the 'tiny chart' problem of percent sizing.
    """
    cfg = {"autosize": False, "width": "100%", "height": init_height, "symbol": symbol, "interval": "D",
           "timezone": "Asia/Tashkent", "theme": "dark", "style": "1", "locale": "en",
           "backgroundColor": "#000000", "allow_symbol_change": True, "hide_side_toolbar": False,
           "withdateranges": True, "save_image": True, "calendar": False, "details": False,
           "support_host": "https://www.tradingview.com"}
    payload = json.dumps(cfg).replace("</", "<\\/")
    return (CHART_TEMPLATE.replace("__CFG__", payload).replace("__H__", str(int(init_height)))
            .replace("__OFFSET__", str(int(offset))))


def chart_widget(symbol: str, init_height: int = 700, offset: int = 170) -> None:
    render_iframe(chart_doc(symbol, init_height, offset), init_height)


def inject_header() -> None:
    """Global CSS: matte black canvas, liquid-glass sheets, emerald/ruby text."""
    st.markdown(CSS, unsafe_allow_html=True)


def brand_html() -> str:
    """S.M.Z logo with the collapsible Founder label (sits inside the navigation row)."""
    return ('<div class="smz-brand"><div class="logo">S<span>.</span>M<span>.</span>Z</div>'
            f'<div class="fdr" tabindex="0">Founder<div class="fname">{html.escape(FOUNDER_NAME)}</div></div></div>')


def inject_seo() -> None:
    render_iframe(SEO_DOC, 1, quiet=True)


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


def render_table(df: pd.DataFrame, key: str, height: int, row_height: int = 24) -> tuple[Optional[str], Optional[str]]:
    """Compact, sortable table. Clicking any cell returns (selection_token, ticker_of_that_row).

    Cell selection is used so the table has no checkbox column; older framework versions
    fall back to row selection.
    """
    shown = style_table(df)
    cfg = {"Ticker": st.column_config.TextColumn("Ticker", pinned=True)} if "Ticker" in df.columns else {}
    ev, mode = None, None
    for mode, extra in (("single-cell", dict(row_height=row_height, column_config=cfg)),
                        ("single-cell", dict(row_height=row_height)), ("single-cell", {}),
                        ("single-row", dict(row_height=row_height)), ("single-row", {})):
        try:
            ev = stretch(st.dataframe, shown, hide_index=True, height=height, key=key, on_select="rerun",
                         selection_mode=mode, **extra)
            break
        except Exception:
            continue
    if ev is None:
        stretch(st.dataframe, shown, hide_index=True, height=height)
        return None, None
    try:
        sel = ev.selection
        cells = getattr(sel, "cells", None)
        if cells:
            row, col = int(cells[0][0]), str(cells[0][1])
        elif sel.rows:
            row, col = int(sel.rows[0]), "row"
        else:
            return None, None
        return f"{key}:{row}:{col}", str(df.iloc[row]["Ticker"])
    except Exception:
        return None, None


def kpi_html(day: pd.DataFrame) -> str:
    chg = day["chg"].dropna()
    n = len(chg)
    adv, dec = int((chg > 0).sum()), int((chg < 0).sum())
    unch = n - adv - dec

    def pct(col):
        v = day[col].dropna()
        return f"{(v > 0).mean() * 100:.0f}%" if len(v) else "-"

    tiles = [("Stocks", f"{n}", ""), ("Advancing", f"{adv}", GREEN), ("Declining", f"{dec}", RED),
             ("Avg change", f"{chg.mean():+.2f}%" if n else "-", GREEN if n and chg.mean() >= 0 else RED),
             ("Above SMA20", pct("d20"), ""), ("Above SMA50", pct("d50"), ""), ("Above SMA200", pct("d200"), ""),
             ("New High", f"{int(day['newhigh'].sum())}", ""), ("New Low", f"{int(day['newlow'].sum())}", ""),
             ("$ Volume", f"${day['dvol'].sum():,.0f}M", "")]
    def _kb(c):
        cls = "gem-g" if c == GREEN else "gem-r" if c == RED else ""
        return f'class="{cls}"' if cls else 'style="color:#e6edf3"'

    out = "".join(f'<div class="kpi"><span>{a}</span><b {_kb(c)}>{b}</b></div>' for a, b, c in tiles)
    total = max(n, 1)
    bar = (f'<div class="bar"><div style="width:{adv / total * 100:.1f}%;background:{GREEN}"></div>'
           f'<div style="width:{unch / total * 100:.1f}%;background:{GREY}"></div>'
           f'<div style="width:{dec / total * 100:.1f}%;background:{RED}"></div></div>')
    return f'<div class="kpis">{out}</div>{bar}'


# =============================================================================
# 5. BUBBLE MAP (dial)
# =============================================================================
def _component_dir() -> Optional[Path]:
    """Write the map page to a folder (next to the app, or the temp dir) and return the folder."""
    for base in (Path(__file__).resolve().parent, Path(tempfile.gettempdir())):
        try:
            d = base / "_smz_bubbles"
            d.mkdir(parents=True, exist_ok=True)
            f = d / "index.html"
            if not f.exists() or f.read_text(encoding="utf-8") != BUBBLE_HTML:
                f.write_text(BUBBLE_HTML, encoding="utf-8")
            return d
        except Exception:
            continue
    return None


@st.cache_resource(show_spinner=False)
def get_bubble_component():
    """Interactive circle-packing map (D3). Returns the component callable, or None if unavailable."""
    if components is None:
        return None
    d = _component_dir()
    if d is None:
        return None
    try:
        return components.declare_component("smz_bubbles_v28", path=str(d))
    except Exception:
        return None


def _corr_layout(p: pd.DataFrame, tickers: tuple, sectors: tuple, industries: tuple, upto: str,
                 lookback: int = 90) -> dict:
    """2-D positions per sector: stocks whose prices move together (and share an industry) get close positions."""
    px = p[(p["date"] <= upto) & (p["ticker"].isin(tickers))]
    px = px.pivot(index="date", columns="ticker", values="close").sort_index().tail(lookback + 1)
    corr = px.pct_change().iloc[1:].corr(min_periods=20)
    sec = dict(zip(tickers, sectors)); ind = dict(zip(tickers, industries))
    out = {}
    for s in set(sectors):
        names = [t for t in tickers if sec[t] == s]
        n = len(names)
        if n < 3:
            for k, t in enumerate(names):
                out[t] = (float(np.cos(k * 2.4)) * 0.6, float(np.sin(k * 2.4)) * 0.6)
            continue
        C = corr.reindex(index=names, columns=names).to_numpy(dtype=float)
        C = np.where(np.isnan(C), 0.0, C)
        same = np.array([[ind[a] == ind[b] for b in names] for a in names], dtype=float)
        C = np.clip(C + 0.15 * same, -1.0, 1.0)
        np.fill_diagonal(C, 1.0)
        D2 = 2.0 * (1.0 - C)
        J = np.eye(n) - 1.0 / n
        B = -0.5 * J @ D2 @ J
        vals, vecs = np.linalg.eigh(B)
        idx = np.argsort(vals)[::-1][:2]
        X = vecs[:, idx] * np.sqrt(np.clip(vals[idx], 0.0, None))
        for k in range(2):
            lo, hi = float(X[:, k].min()), float(X[:, k].max())
            X[:, k] = (X[:, k] - lo) / ((hi - lo) or 1.0) * 2.0 - 1.0
        for k, t in enumerate(names):
            out[t] = (float(X[k, 0]), float(X[k, 1]))
    return out


@st.cache_data(ttl=3600, show_spinner=False)
def corr_embedding(tickers: tuple, sectors: tuple, industries: tuple, upto: str) -> dict:
    """Cached wrapper: uses the price history already loaded for the screener."""
    p, _ = load_data(db_stamp())
    return _corr_layout(p, tickers, sectors, industries, upto)


def build_bubble_payload(day: pd.DataFrame, perf_col: str, top_n: int, by_industry: bool) -> dict:
    """Rows for the map: only stocks with a market cap, biggest first. Each row also carries a 2-D position
    (ex, ey) from price correlation so that co-moving stocks end up next to each other."""
    d = day.dropna(subset=["mcap", perf_col])
    d = d[d["mcap"] > 0].sort_values("mcap", ascending=False).head(top_n)
    nodes = [dict(ticker=str(r["ticker"]),
                  sector=str(r["sector"]) if pd.notna(r["sector"]) else "Other",
                  industry=str(r["industry"]) if pd.notna(r["industry"]) else "Other",
                  mcap=float(r["mcap"]), perf=float(r[perf_col]), price=float(r["close"]),
                  volume=float(r["volume"]), ex=0.0, ey=0.0) for _, r in d.iterrows()]
    try:
        emb = corr_embedding(tuple(n["ticker"] for n in nodes), tuple(n["sector"] for n in nodes),
                             tuple(n["industry"] for n in nodes), str(day["date"].iloc[0]))
        for n in nodes:
            n["ex"], n["ey"] = emb.get(n["ticker"], (0.0, 0.0))
    except Exception:  # never let the layout hint break the map
        pass
    return {"nodes": nodes, "by_industry": bool(by_industry)}


# =============================================================================
# 6. VIEWS
# =============================================================================
def view_stats(day: pd.DataFrame, meta: dict, show_kpi: bool = True) -> list:
    """Market statistics: optional KPIs + breadth bar, then the top lists."""
    items: list = []
    if show_kpi:
        st.markdown(kpi_html(day), unsafe_allow_html=True)

    lists = [
        ("Top Gainers", lambda d: d.nlargest(10, "chg")),
        ("Top Losers", lambda d: d.nsmallest(10, "chg")),
        ("Most Active", lambda d: d.nlargest(10, "dvol")),
        ("Unusual Volume", lambda d: d.nlargest(10, "relvol")),
        ("Overbought", lambda d: d[d["rsi"] > 70].nlargest(10, "rsi")),
        ("Oversold", lambda d: d[d["rsi"] < 30].nsmallest(10, "rsi")),
        ("New High", lambda d: d[d["newhigh"]].nlargest(10, "chg")),
        ("New Low", lambda d: d[d["newlow"]].nsmallest(10, "chg")),
    ]
    cols = st.columns(4)
    for i, (title, fn) in enumerate(lists):
        with cols[i % 4]:
            st.markdown(f'<div class="ttl">{title}</div>', unsafe_allow_html=True)
            sub = fn(day)
            if sub.empty:
                st.caption("No stocks")
                continue
            show = to_display(sub, MINI_COLS)
            items.append(render_table(show, f"mini_{i}", 36 + 24 * len(show) + 4))
    return items


def _h(x) -> str:
    """HTML-escape and protect $ from the markdown math renderer."""
    return html.escape(str(x)).replace("$", "&#36;")


def tape_html(day: pd.DataFrame) -> str:
    """Ticker tape: every company with price and change vs yesterday, scrolling right to left."""
    d = day.dropna(subset=["close"]).sort_values("mcap", ascending=False, na_position="last")
    parts = []
    for t, c, g in zip(d["ticker"], d["close"], d["chg"]):
        if pd.isna(g):
            cls, txt = "gem-n", "-"
        else:
            cls, txt = ("gem-g" if g > 0 else "gem-r" if g < 0 else "gem-n"), f"{g:+.2f}%"
        parts.append(f'<span class="tp-i"><b>{_h(t)}</b><span>{c:.2f}</span><em class="{cls}">{txt}</em></span>')
    inner = "".join(parts)
    dur = max(120, int(len(parts) * 3.4))
    return f'<div class="tape"><div class="tape-in" style="animation-duration:{dur}s">{inner}{inner}</div></div>'


def view_home(day: pd.DataFrame, meta: dict) -> list:
    """Home: ticker tape, market summary (KPIs + breadth), then the interactive wheel map.
    The detailed top lists of the old Overview tab are tucked below in a collapsed block."""
    st.markdown(tape_html(day), unsafe_allow_html=True)
    st.markdown(kpi_html(day), unsafe_allow_html=True)
    items = list(view_bubbles(day, meta))
    if st.toggle("Show market leaders (top lists)", key="show_stats", value=False):
        items += view_stats(day, meta, show_kpi=False)
    return items


# ---- news (source: stockanalysis.com) ------------------------------------------
_SA_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/124.0 Safari/537.36")
_SA_UNITS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800, "month": 2592000, "year": 31536000}
_SA_AGO = r"(\d+|an?)\s+(second|minute|hour|day|week|month|year)s?\s+ago"
_SA_ALL_URL = "https://stockanalysis.com/news/all-stocks/"


def _sa_get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _SA_UA, "Accept": "text/html,application/xhtml+xml",
                                               "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=12) as r:
        return r.read().decode("utf-8", "ignore")


def _txt(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def _parse_sa_news(page: str) -> list:
    """Parse the news list of a stockanalysis.com page: thumbnail, title, summary, symbols, '3 hours ago - Source'."""
    now = time.time()
    parts = re.split(r"<h3\b", page, flags=re.I)
    out = []
    for i in range(1, len(parts)):
        prev, cur = parts[i - 1], parts[i]
        end = cur.find("</h3>")
        if end < 0:
            continue
        head, body = cur[:end], cur[end + 5:]
        head = head[head.find(">") + 1:]
        title = _txt(head)
        if not title:
            continue
        m = re.search(r'href="([^"]+)"', head)
        url = html.unescape(m.group(1)) if m else ""
        text = html.unescape(re.sub(r"<[^>]+>", "\n", body))
        mm = re.search(_SA_AGO + r"\s*-?\s*([^\n]*)", text, re.I)
        if not mm:
            continue
        n = 1 if mm.group(1).lower() in ("a", "an") else int(mm.group(1))
        ts = now - n * _SA_UNITS[mm.group(2).lower()]
        source = mm.group(3).strip()
        if not source:
            rest = [ln.strip(" -\t") for ln in text[mm.end():].split("\n") if ln.strip(" -\t")]
            source = rest[0] if rest else ""
        cut = re.search(_SA_AGO, body, re.I)
        pre = body[:cut.start()] if cut else body[:3000]
        syms = [x.upper() for x in re.findall(r'href="(?:https?://stockanalysis\.com)?/(?:stocks|etf)/([A-Za-z0-9.\-]+)/?"', pre)]
        summary = ""
        for ln in (x.strip() for x in text[:mm.start()].split("\n")):
            if len(ln) >= 20 and ln != title and not ln.startswith(("Other symbols", "Stocks:")):
                summary = ln
                break
        tail = prev[-3000:]
        ago = list(re.finditer(r"\bago\b", tail))
        tail = tail[ago[-1].end():] if ago else tail
        imgs = [html.unescape(x) for x in re.findall(r'<img\b[^>]*?\b(?:src|data-src)="([^"]+)"', tail)]
        imgs = [x for x in imgs if x.startswith("http") and "stockanalysis.com" not in x and not x.endswith(".svg")]
        out.append(dict(title=title, summary=summary[:260], provider=source, url=url, ts=ts,
                        thumb=imgs[-1] if imgs else "", syms=list(dict.fromkeys(syms))))
    return out


def _fetch_sa_stock(t: str):
    sym = t.lower().replace("-", ".")
    if not re.fullmatch(r"[a-z0-9]+(\.[a-z])?", sym):
        return None
    for kind in ("stocks", "etf"):
        try:
            page = _sa_get(f"https://stockanalysis.com/{kind}/{sym}/")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            raise
        items = _parse_sa_news(page)
        for it in items:
            if t not in it["syms"]:
                it["syms"].insert(0, t)
        return items
    return []


def _load_news_raw(tickers: tuple) -> dict:
    arts: dict = {}
    stats = {"ok": 0, "fail": 0, "err": "", "parsed": 0}

    def merge(items):
        for it in items:
            a = arts.setdefault(it["url"] or it["title"], {**it, "syms": []})
            for x in it["syms"]:
                if x not in a["syms"]:
                    a["syms"].append(x)

    try:
        merge(_parse_sa_news(_sa_get(_SA_ALL_URL)))
    except Exception as e:  # noqa: BLE001
        stats["err"] = f"{type(e).__name__}: {e}"
    ex = ThreadPoolExecutor(max_workers=8)
    futs = {ex.submit(_fetch_sa_stock, t): t for t in tickers}
    try:
        for f in as_completed(futs, timeout=300):
            try:
                items = f.result()
                if items is None:
                    continue
                stats["ok"] += 1
                stats["parsed"] += len(items)
                merge(items)
            except Exception as e:  # noqa: BLE001
                stats["fail"] += 1
                stats["err"] = stats["err"] or f"{type(e).__name__}: {e}"
    except Exception:
        pass
    ex.shutdown(wait=False, cancel_futures=True)
    return {"arts": list(arts.values()), **stats}


@st.cache_resource(show_spinner=False)
def _news_store() -> dict:
    return {"data": None, "ts": 0.0, "busy": False, "lock": threading.Lock()}


def news_data(tickers: tuple):
    """Return (news, loading). The download runs in a background thread, so the page never freezes."""
    store = _news_store()
    with store["lock"]:
        stale = time.time() - store["ts"] > 1200
        start = (store["data"] is None or stale) and not store["busy"]
        if start:
            store["busy"] = True

    def work() -> None:
        try:
            store["data"] = _load_news_raw(tickers)
            store["ts"] = time.time()
        except Exception:  # noqa: BLE001
            store["ts"] = time.time()
        finally:
            store["busy"] = False

    if start:
        threading.Thread(target=work, daemon=True).start()
    return store["data"], store["busy"]


def _ago(ts: float) -> str:
    sec = max(0, time.time() - ts)
    if sec < 3600:
        m = max(1, int(sec // 60))
        return f"{m} min ago"
    if sec < 86400:
        h = int(sec // 3600)
        return f"{h} hour{'s' if h > 1 else ''} ago"
    d = int(sec // 86400)
    return f"{d} day{'s' if d > 1 else ''} ago"


def _chg_span(g) -> str:
    if g is None or pd.isna(g):
        return ""
    cls = "gem-g" if g > 0 else "gem-r" if g < 0 else "gem-n"
    return f'<em class="{cls}">{g:+.2f}%</em>'


def news_card_html(a: dict, chg: dict) -> str:
    syms = a["syms"]
    main = a["main"]
    img = (f'<img src="{html.escape(a["thumb"], quote=True)}" loading="lazy" referrerpolicy="no-referrer">'
           if a["thumb"] else f"<b>{_h(main)}</b>")
    meta = f'<span class="nchip">{_h(main)}{_chg_span(chg.get(main))}</span>' \
           f'{_h(_ago(a["ts"]))} - {_h(a["provider"])}'
    title = (f'<a class="nttl" href="{html.escape(a["url"], quote=True)}" target="_blank" rel="noopener">{_h(a["title"])}</a>'
             if a["url"] else f'<span class="nttl">{_h(a["title"])}</span>')
    summ = f'<div class="nsum">{_h(a["summary"])}</div>' if a["summary"] else ""
    others = [t for t in syms if t != main][:6]
    oth = ("<div class=\"nsyms\">Other symbols: " +
           "".join(f'<span class="nchip">{_h(t)}{_chg_span(chg.get(t))}</span>' for t in others) + "</div>") if others else ""
    return f'<div class="nw"><div class="nimg">{img}</div><div class="nbody"><div class="nmeta">{meta}</div>{title}{summ}{oth}</div></div>'


def _company_news(day: pd.DataFrame) -> None:
    top = st.columns([1.2, 6.8])
    period = top[0].selectbox("Period", ["24 hours", "48 hours", "7 days"], index=0, key="news_period")
    hours = {"24 hours": 24, "48 hours": 48, "7 days": 168}[period]
    tickers = tuple(sorted(day["ticker"].dropna().unique()))
    data, loading = news_data(tickers)
    if data is None:
        st.info("Loading company news in the background. This takes about a minute the first time; "
                "the page updates by itself and stays usable meanwhile.")
        if loading and hasattr(st, "fragment"):
            time.sleep(3)
            st.rerun(scope="fragment")
        return
    cut = time.time() - hours * 3600
    chg = dict(zip(day["ticker"], day["chg"]))
    arts = []
    for a in data["arts"]:
        if a["ts"] < cut:
            continue
        mine = [t for t in a["syms"] if t in chg]
        if not mine:
            continue
        arts.append({**a, "mine": mine})

    def score(t) -> float:
        v = chg.get(t)
        return 0.0 if v is None or pd.isna(v) else abs(float(v))

    for a in arts:
        a["mine"].sort(key=lambda t: -score(t))
        a["main"] = a["mine"][0]
        a["syms"] = [a["main"]] + [t for t in a["syms"] if t != a["main"]]
    covered = {t for a in arts for t in a["mine"]}
    pct = len(covered) / max(len(tickers), 1) * 100
    top[1].caption(f"{len(covered)} of {len(tickers)} stocks ({pct:.0f}%) have news in the last {period} "
                   f"| biggest movers first | source: stockanalysis.com ({data['ok']} pages loaded)")
    if not arts:
        why = f" Last error: {data['err']}." if data.get("err") else ""
        st.info("No company news found for this period. "
                f"stockanalysis.com pages loaded: {data['ok']}, failed: {data['fail']}, news items parsed: {data['parsed']}.{why}")
        return
    ranked = sorted(arts, key=lambda a: (-score(a["main"]), -a["ts"]))[:100]
    st.markdown('<div class="nlist">' + "".join(news_card_html(a, chg) for a in ranked) + "</div>",
                unsafe_allow_html=True)


company_news = st.fragment(_company_news) if hasattr(st, "fragment") else _company_news


def macro_news() -> None:
    st.caption("Economic calendar: central banks, inflation, jobs and other events that move the whole market")
    render_iframe(EVENTS_DOC, 840)


def view_news(day: pd.DataFrame, meta: dict) -> list:
    opts = ["Company news", "Macro"]
    if hasattr(st, "segmented_control"):
        sub = st.segmented_control("News section", opts, default=opts[0], key="news_sub", label_visibility="collapsed")
    else:
        sub = st.radio("News section", opts, horizontal=True, key="news_sub", label_visibility="collapsed")
    if sub == "Macro":
        macro_news()
    else:
        company_news(day)
    return []


def _pick_sector(name: str) -> None:
    S["bub_sector"] = name


def view_bubbles(day: pd.DataFrame, meta: dict) -> list:
    """Radial map. All sectors: wedge map. One sector picked in the white sector box: every company
    becomes a full ring (onion), biggest market cap outside, smallest in the centre."""
    if not meta["has_fund"]:
        st.info("Market cap data is still loading. The bubble map appears as soon as it is available.")
        return []
    label_col, cap_col = st.columns([1.1, 6.9])
    label = label_col.selectbox("Performance", list(PERF_COLS), key="bub_perf")
    perf_col = PERF_COLS[label]
    scale = 15.0  # color step 5%
    payload = build_bubble_payload(day, perf_col, 500, False)

    secs: dict = {}
    for n in payload["nodes"]:
        secs[n["sector"]] = secs.get(n["sector"], 0.0) + n["mcap"]
    sector_names = sorted(secs, key=lambda k: -secs[k])
    chosen = S.get("bub_sector", "All")
    if chosen != "All" and chosen not in secs:
        chosen = "All"
        S["bub_sector"] = "All"

    nodes = payload["nodes"] if chosen == "All" else [n for n in payload["nodes"] if n["sector"] == chosen]
    hidden = int((day["mcap"].isna() | (day["mcap"] <= 0)).sum())
    if chosen == "All":
        hint = "pick a sector in the box below to enter it"
    else:
        hint = f"inside {chosen}"
    cap_col.caption(f"{len(nodes)} stocks shown" + (f" | {hidden} without market cap data" if hidden else "")
                    + f" | {hint} | drag the dial to turn it, click a stock to open its chart")
    comp = get_bubble_component()
    if comp is None:
        st.warning("The interactive map is not available in this environment.")
        return []

    valid = day.dropna(subset=[perf_col])
    if chosen != "All":
        valid = valid[valid["sector"] == chosen]

    def _rank(df: pd.DataFrame) -> list:
        return [dict(ticker=str(r["ticker"]), perf=float(r[perf_col]), price=float(r["close"]),
                     sector=str(r["sector"]) if pd.notna(r["sector"]) else "-")
                for _, r in df.iterrows()]

    gainers = _rank(valid.nlargest(21, perf_col))
    losers = _rank(valid.nsmallest(21, perf_col))
    with keyed("wheelpanel"):
        ev = comp(nodes=nodes, scale=scale, perf_label=label, gainers=gainers, losers=losers,
                  mode="all", key="smz_bubbles", default=None)

    box = keyed("secbox")
    with box:
        st.markdown('<div class="ttl">Sectors</div>', unsafe_allow_html=True)
        names = ["All"] + sector_names
        per_row = 6
        for r0 in range(0, len(names), per_row):
            cols = st.columns(per_row)
            for j, nm in enumerate(names[r0:r0 + per_row]):
                stretch(cols[j].button, "All sectors" if nm == "All" else nm, key=f"secbtn_{r0 + j}",
                        type="primary" if nm == chosen else "secondary", on_click=_pick_sector, args=(nm,))

    if chosen != "All":
        st.markdown(sector_list_html(day, chosen), unsafe_allow_html=True)

    if isinstance(ev, dict) and ev.get("ticker"):
        sym = clean_symbol(str(ev["ticker"]))
        if sym:
            return [(f"bubbles:{ev.get('n')}", sym)]
    return []


def sector_list_html(day: pd.DataFrame, sector: str) -> str:
    """Every stock of one sector as framed rows: number, ticker, price, daily change."""
    x = day[day["sector"] == sector].sort_values("ticker")
    rows = ['<div class="slist"><div class="r h"><span>No.</span><span>Ticker</span><span>Price</span>'
            '<span>Change</span></div>']
    for i, (_, r) in enumerate(x.iterrows(), 1):
        chg = r["chg"]
        if pd.isna(chg):
            ctxt, ccol = "-", "gem-n"
        else:
            ctxt, ccol = f"{chg:+.2f}%", ("gem-g" if chg > 0 else "gem-r" if chg < 0 else "gem-n")
        price = "-" if pd.isna(r["close"]) else f"{r['close']:.2f}"
        rows.append(f'<div class="r"><span>{i}</span><span>{html.escape(str(r["ticker"]))}</span>'
                    f'<span>{price}</span>'
                    f'<span><i class="{ccol}" style="font-style:normal">{ctxt}</i></span></div>')
    rows.append("</div>")
    return "".join(rows)


def _sel(col, label, options, key, disabled=False):
    col.selectbox(label, list(options), key=key, disabled=disabled)


def reset_all() -> None:
    reset_filters()
    S["signal"] = "None"


def _n_active() -> int:
    return sum(1 for k, v in D.items() if S.get(k, v) != v)


def filter_ui(day: pd.DataFrame, meta: dict) -> None:
    """Every filter visible at once: Descriptive row on top, Technical & Trend rows right below."""
    with keyed("filtergrid"):
        try:
            head = st.columns([6, 1.1], vertical_alignment="center")
        except TypeError:
            head = st.columns([6, 1.1])
        head[0].markdown(f'<div class="resline"><b>Filters</b> &nbsp;|&nbsp; {_n_active()} active</div>',
                         unsafe_allow_html=True)
        head[1].button("Reset", on_click=reset_all, key="reset_btn")

        st.markdown('<div class="fsec">Descriptive Filters</div>', unsafe_allow_html=True)
        a = st.columns(6)
        _sel(a[0], "Market Cap", MCAP_DT, "mcap", disabled=not meta["has_fund"])
        _sel(a[1], "Exchange", EXCHANGES, "exch", disabled=not meta["has_exchange"])
        _sel(a[2], "Average Volume", VOLUME, "avgvol")
        _sel(a[3], "Relative Volume", RELVOL, "relvol")
        _sel(a[4], "Price", PRICE, "price")

        st.markdown('<div class="fsec">Technical &amp; Trend Filters</div>', unsafe_allow_html=True)
        b = st.columns(6)
        b[0].selectbox("Signal", list(SIGNALS), key="signal", on_change=apply_signal)
        _sel(b[1], "Change", CHANGE, "chg")
        _sel(b[2], "Gap %", GAP, "gap")
        _sel(b[3], "ATR", ATR_OPT, "atr")
        _sel(b[4], "RSI (14)", RSI_OPT, "rsi")
        _sel(b[5], "52-Week High/Low", HL52, "hl52")
        t = st.columns(6)
        _sel(t[0], "SMA20", SMA_OPTS[20], "s20")
        _sel(t[1], "SMA50", SMA_OPTS[50], "s50")
        _sel(t[2], "SMA200", SMA_OPTS[200], "s200")


# ---- institutional data table (custom component: row hover, zebra rows, glowing numbers) ----
TEXT_COLS = {"Ticker", "Sector", "Industry"}

TABLE_HTML = r"""<!DOCTYPE html><html><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@500;600;700&display=swap" rel="stylesheet">
<style>
html,body{margin:0;padding:0;background:transparent;overflow:hidden;font-family:Inter,-apple-system,"SF Pro Display","Segoe UI",Arial,sans-serif;color:#e6e8ec;letter-spacing:-.003em}
#wrap{overflow:auto;position:relative;scrollbar-width:thin;scrollbar-color:rgba(255,255,255,.18) transparent}
table{border-collapse:separate;border-spacing:0;width:100%;font-size:12.5px;font-variant-numeric:tabular-nums}
th{position:sticky;top:0;z-index:2;background:rgba(10,10,13,.97);color:#8b95a5;font-weight:600;font-size:11.5px;text-align:right;
  padding:0 14px;height:34px;white-space:nowrap;border-bottom:1px solid rgba(255,255,255,.08)}
th.l,td.l{text-align:left}
th.on{color:#e6e8ec}
th.on i{font-style:normal;color:#10B981;margin-left:5px;font-size:9px}
td{height:28px;padding:0 14px;text-align:right;white-space:nowrap;border-bottom:1px solid rgba(255,255,255,.03);background:transparent;
  transition:background .45s cubic-bezier(0.16,1,0.3,1),box-shadow .45s cubic-bezier(0.16,1,0.3,1)}
tbody tr{cursor:pointer}
tbody tr:nth-child(even) td{background:rgba(255,255,255,.018)}
tbody tr:hover td{background:rgba(255,255,255,.04);box-shadow:inset 0 1px 0 rgba(255,255,255,.05)}
td.tk{font-weight:700;color:#fff;letter-spacing:.2px}
td.no{color:#5d6472;text-align:center}
td.sc{color:#9aa0a8}
th.s,td.s{position:sticky}
th.s{z-index:3}
td.s{z-index:1;background:#0c0c0f}
tbody tr:nth-child(even) td.s{background:#101013}
tbody tr:hover td.s{background:#18181d}
th.s0,td.s0{left:0;width:46px;min-width:46px;max-width:46px;padding:0;text-align:center}
th.s1,td.s1{left:46px;min-width:84px}
.g{color:#10B981;text-shadow:0 0 6px rgba(16,185,129,.25)}
.r{color:#EF4444;text-shadow:0 0 6px rgba(239,68,68,.25)}
</style></head><body><div id="wrap"><table><thead id="th"></thead><tbody id="tb"></tbody></table></div>
<script>
(function () {
  "use strict";
  var args = null, lastKey = null, data = [], shown = 0, CH = 150;
  var wrap = document.getElementById("wrap"), th = document.getElementById("th"), tb = document.getElementById("tb");
  function post(type, extra) {
    window.parent.postMessage(Object.assign({isStreamlitMessage: true, type: type}, extra || {}), "*");
  }
  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c];
    });
  }
  function tone(t) {
    if (t === "-") { return ""; }
    var n = parseFloat(String(t).replace(/,/g, ""));
    if (isNaN(n) || n === 0) { return ""; }
    return n > 0 ? "g" : "r";
  }
  function rowHtml(i) {
    var r = data[i], h = '<tr data-t="' + esc(r[1]) + '">', j, c, v;
    for (j = 0; j < r.length; j++) {
      v = r[j]; c = [];
      if (j === 0) { c.push("s", "s0", "no"); }
      else if (j === 1) { c.push("s", "s1", "l", "tk"); }
      else {
        if (args.left[j]) { c.push("l"); if (args.heads[j] === "Sector") { c.push("sc"); } }
        if (args.colored[j]) { c.push(tone(v)); }
      }
      h += '<td class="' + c.join(" ") + '">' + esc(v) + "</td>";
    }
    return h + "</tr>";
  }
  function more() {
    if (shown >= data.length) { return; }
    var end = Math.min(data.length, shown + CH), h = "", i;
    for (i = shown; i < end; i++) { h += rowHtml(i); }
    tb.insertAdjacentHTML("beforeend", h);
    shown = end;
  }
  function build() {
    var H = +args.height || 640, hs = "<tr>", j, c, on;
    wrap.style.height = H + "px";
    for (j = 0; j < args.heads.length; j++) {
      c = [];
      if (j === 0) { c.push("s", "s0"); } else if (j === 1) { c.push("s", "s1", "l"); } else if (args.left[j]) { c.push("l"); }
      on = args.heads[j] === args.sort_label;
      if (on) { c.push("on"); }
      hs += '<th class="' + c.join(" ") + '">' + esc(args.heads[j]) +
        (on ? "<i>" + (args.asc ? "\u25B2" : "\u25BC") + "</i>" : "") + "</th>";
    }
    th.innerHTML = hs + "</tr>";
    tb.innerHTML = ""; shown = 0; data = args.rows || [];
    more();
    while (shown < data.length && wrap.scrollHeight < wrap.clientHeight + 300) { more(); }
    post("streamlit:setFrameHeight", {height: H});
  }
  wrap.addEventListener("scroll", function () {
    if (wrap.scrollTop + wrap.clientHeight > wrap.scrollHeight - 300) { more(); }
  });
  tb.addEventListener("click", function (e) {
    var tr = e.target.closest ? e.target.closest("tr") : null;
    if (!tr) { return; }
    post("streamlit:setComponentValue", {value: {ticker: tr.getAttribute("data-t"), n: Date.now() + Math.random()},
                                         dataType: "json"});
  });
  window.addEventListener("message", function (e) {
    if (!e.data || e.data.type !== "streamlit:render") { return; }
    args = e.data.args || {};
    var key = JSON.stringify(args);
    post("streamlit:setFrameHeight", {height: +args.height || 640});
    if (key === lastKey) { return; }
    lastKey = key;
    build();
  });
  post("streamlit:componentReady", {apiVersion: 1});
})();
</script></body></html>"""


def _write_component(dirname: str, content: str) -> Optional[Path]:
    for base in (Path(__file__).resolve().parent, Path(tempfile.gettempdir())):
        try:
            d = base / dirname
            d.mkdir(parents=True, exist_ok=True)
            f = d / "index.html"
            if not f.exists() or f.read_text(encoding="utf-8") != content:
                f.write_text(content, encoding="utf-8")
            return d
        except Exception:
            continue
    return None


@st.cache_resource(show_spinner=False)
def get_table_component():
    if components is None:
        return None
    d = _write_component("_smz_table", TABLE_HTML)
    if d is None:
        return None
    try:
        return components.declare_component("smz_table_v1", path=str(d))
    except Exception:
        return None


def _cell(label: str, v) -> str:
    if label == "No.":
        return str(int(v))
    if label in TEXT_COLS:
        return "-" if pd.isna(v) else str(v)
    if pd.isna(v):
        return "-"
    f = FMT.get(label)
    if f is None:
        return str(v)
    return f(v) if callable(f) else f.format(v)


def render_screener_table(full: pd.DataFrame, height: int = 640) -> tuple[Optional[str], Optional[str]]:
    """Glass data table. Click any row to open its chart. Falls back to the plain grid if needed."""
    comp = get_table_component()
    if comp is None:
        return render_table(full, "tbl_screener", height)
    labels = list(full.columns)
    shown = full.head(4000)
    cols = [[_cell(lb, v) for v in shown[lb].tolist()] for lb in labels]
    rows = [list(r) for r in zip(*cols)]
    with keyed("tablepanel"):
        ev = comp(heads=labels, rows=rows, colored=[lb in COLOR_COLS for lb in labels],
                  left=[lb in TEXT_COLS for lb in labels], sort_label=str(S.get("sort_by", "")),
                  asc=bool(S.get("sort_asc", False)), height=int(height), key="smz_table", default=None)
    if isinstance(ev, dict) and ev.get("ticker"):
        sym = clean_symbol(str(ev["ticker"]))
        if sym:
            return f"tbl:{ev.get('n')}", sym
    return None, None


def view_screener(day: pd.DataFrame, meta: dict) -> list:
    """Filter grid + control bar + glass table. Click any row to open its chart."""
    filter_ui(day, meta)
    x = apply_filters(day, S, meta)

    order = ["ticker", "sector"] + (["mcap"] if meta["has_fund"] else []) + \
            ["close", "chg", "gap", "volume", "avgvol", "relvol", "atr", "rsi", "d20", "d50", "d200",
             "hid", "lod", "pw", "pm", "pq"]
    names = [COLS[c] for c in order]
    if S.get("sort_by") not in names:
        S["sort_by"] = "Change"
    with keyed("ctrlbar"):
        spec = [4.4, 1.6, 1.1, 1.0]
        try:
            bar = st.columns(spec, vertical_alignment="center")
        except TypeError:
            bar = st.columns(spec)
        bar[0].markdown(f'<div class="resline"><b>{S["date"]}</b> &nbsp;|&nbsp; {len(x)} stocks '
                        '&nbsp;|&nbsp; click a row to open its chart</div>', unsafe_allow_html=True)
        bar[1].selectbox("Order", names, key="sort_by", label_visibility="collapsed")
        bar[2].checkbox("Ascending", key="sort_asc")
        inv = {v: k for k, v in COLS.items()}
        x = x.sort_values(inv[S["sort_by"]], ascending=bool(S["sort_asc"]))
        if not x.empty:
            full = to_display(x, order, with_no=True)
            bar[3].download_button("Export", full.round(2).to_csv(index=False).encode(), "smz_screener.csv",
                                   "text/csv")
    if x.empty:
        st.info("No stocks match these filters. Try loosening them.")
        return []
    return [render_screener_table(full, 640)]


# =============================================================================
# 7. CHART DIALOG
# =============================================================================
def open_chart(sym: str, row: Optional[pd.Series]) -> None:
    """Open a full-width modal window with the advanced chart."""
    exch = row["exchange"] if row is not None and "exchange" in row else None
    full_sym = chart_symbol(sym, exch)

    @st.dialog(sym, width="large")
    def _dialog():
        if row is not None:
            parts = [f"<b>${row['close']:.2f}</b>", f"{row['chg']:+.2f}%"]
            if pd.notna(row["relvol"]):
                parts.append(f"Rel Volume {row['relvol']:.1f}")
            if pd.notna(row["rsi"]):
                parts.append(f"RSI {row['rsi']:.0f}")
            if pd.notna(row["atr"]):
                parts.append(f"ATR {row['atr']:.2f}")
            if pd.notna(row.get("sector")):
                parts.append(html.escape(str(row["sector"])))
            st.markdown(f'<div class="chartbar">{" &nbsp;|&nbsp; ".join(parts)}</div>', unsafe_allow_html=True)
        chart_widget(full_sym, 700, 170)

    _dialog()


# =============================================================================
# 8. MAIN
# =============================================================================
def header(dates: list) -> tuple[str, Optional[str]]:
    """One sharp navigation row: brand | Home - Screener - News | date | search + Go."""
    opts = ["Home", "Screener", "News"]
    if S.get("view") not in opts:  # e.g. a stale "Overview" from an older session
        S.pop("view", None)
    forced = None
    with keyed("topbar"):
        spec = [1.0, 2.1, 1.1, 3.0]
        try:
            c = st.columns(spec, vertical_alignment="center")
        except TypeError:
            c = st.columns(spec)
        c[0].markdown(brand_html(), unsafe_allow_html=True)
        with c[1]:
            with keyed("nav"):
                if hasattr(st, "segmented_control"):
                    view = st.segmented_control("View", opts, default="Home", key="view",
                                                label_visibility="collapsed")
                else:
                    view = st.radio("View", opts, horizontal=True, key="view", label_visibility="collapsed")
        c[2].selectbox("Date", dates, key="date", label_visibility="collapsed")
        with c[3]:
            with st.form("open_chart_form", border=False):
                try:
                    f = st.columns([4, 1], vertical_alignment="center")
                except TypeError:
                    f = st.columns([4, 1])
                q = f[0].text_input("Search", key="open_q", placeholder="Search ticker",
                                    label_visibility="collapsed")
                go_ = f[1].form_submit_button("Go")
            if go_:
                forced = clean_symbol(q)
                if forced is None:
                    st.warning("Enter a valid ticker (letters and digits only).")
    return view or "Home", forced


def expected_session() -> Optional[str]:
    """Last US trading day that should already be in the database (weekends only, holidays ignored)."""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return None
    d = now.date()
    if now.weekday() >= 5 or now.hour * 60 + now.minute < 16 * 60 + 30:
        d -= timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
    return d.isoformat()


def main() -> None:
    st.set_page_config(layout="wide", page_title=PAGE_TITLE, page_icon="📈", initial_sidebar_state="collapsed",
                       menu_items={"Get help": None, "Report a bug": None, "About": None})
    inject_header()
    init_state()
    p, meta = load_data(db_stamp())
    dates = sorted(p["date"].unique(), reverse=True)

    view, forced = header(dates)
    exp = expected_session()
    if exp and dates and str(dates[0]) < exp:
        st.caption(f"\u26a0 The database ends at {dates[0]}, but the market already traded {exp}. "
                   "The data updater has not loaded the newer session yet.")
    day = p[p["date"] == S["date"]]
    if view == "Home":
        items = view_home(day, meta)
    elif view == "News":
        items = view_news(day, meta)
    else:
        items = view_screener(day, meta)

    to_open, S["opened"] = decide_open(items, S.get("opened"), forced)
    foot = f"© {BRAND} | For informational purposes only."
    if view == "Home":
        st.markdown(f'<div class="smz-foot">{foot}</div>', unsafe_allow_html=True)
    else:
        st.caption(foot)
    inject_seo()
    if to_open:
        r = day[day["ticker"] == to_open]
        open_chart(to_open, r.iloc[0] if not r.empty else None)


# =============================================================================
# S.M.Z AI (Groq) - grafik oynasiga "S.M.Z AI" tugmasi va AI tahlil oynasi
# =============================================================================
import datetime as dt

from smz_ai import build_facts, run_analysis


def _ai_open() -> None:
    S["ai_view"] = True


def _ai_close() -> None:
    S["ai_view"] = False


def ai_panel(sym: str, row: Optional[pd.Series] = None) -> None:
    """Grafik o'rnini egallaydigan to'liq AI tahlil oynasi."""
    top = st.columns([5, 1])
    top[0].markdown(f"### S.M.Z AI · {sym}")
    stretch(top[1].button, "← Grafikka qaytish", key="ai_back", on_click=_ai_close)

    cache = S.setdefault("ai_cache", {})
    ckey = (sym, dt.date.today().isoformat())
    if st.button("↻ Qayta tahlil", key="ai_refresh"):
        cache.pop(ckey, None)

    if ckey not in cache:
        with st.spinner(f"{sym} tahlil qilinmoqda..."):
            try:
                cache[ckey] = run_analysis(sym, row)
            except Exception as e:  # noqa: BLE001
                st.error(f"AI xatosi: {e}")
                return

    with st.container(border=True):
        st.markdown(cache[ckey])
    st.caption("Bu moliyaviy maslahat emas. Qaror o'zingizniki.")


def open_chart(sym: str, row: Optional[pd.Series] = None) -> None:
    """Grafik oynasi: ramka ichida 'S.M.Z AI' tugmasi, bosilsa grafik o'rniga AI tahlil."""
    S["ai_view"] = False
    exch = row["exchange"] if row is not None and "exchange" in row else None
    full_sym = chart_symbol(sym, exch)

    @st.dialog(sym, width="large")
    def _dialog():
        if S.get("ai_view"):
            ai_panel(sym, row)
            return
        with st.container(border=True):
            left, right = st.columns([5, 1])
            if row is not None:
                parts = [f"<b>${row['close']:.2f}</b>", f"{row['chg']:+.2f}%"]
                if pd.notna(row["relvol"]):
                    parts.append(f"Rel Volume {row['relvol']:.1f}")
                if pd.notna(row["rsi"]):
                    parts.append(f"RSI {row['rsi']:.0f}")
                if pd.notna(row["atr"]):
                    parts.append(f"ATR {row['atr']:.2f}")
                left.markdown(f'<div class="chartbar">{" &nbsp;|&nbsp; ".join(parts)}</div>',
                              unsafe_allow_html=True)
            stretch(right.button, "✦ S.M.Z AI", key="ai_open", on_click=_ai_open)
        chart_widget(full_sym, 700, 170)

    _dialog()


if __name__ == "__main__":
    main()

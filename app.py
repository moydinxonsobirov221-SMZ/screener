"""S.M.Z Stock Screener.

Sections: 1 CONFIG | 2 DATA LAYER | 3 FILTER ENGINE | 4 UI HELPERS | 5 MARKET MAP + BUBBLE MAP |
          6 VIEWS | 7 CHART DIALOG | 8 MAIN
"""
from __future__ import annotations

import html
import json
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pandas as pd
import plotly.graph_objects as go
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
EXCH_CODES = {"NMS": "NASDAQ", "NGM": "NASDAQ", "NCM": "NASDAQ", "NAS": "NASDAQ", "NYQ": "NYSE", "NYS": "NYSE",
              "ASE": "AMEX", "PCX": "AMEX", "BTS": "BATS", "BATS": "BATS"}

# default value of every filter widget (the key is also the session_state key)
D = dict(exch="Any", sector="Any", industry="Any", price="Any", vol="Any", avgvol="Any", relvol="Any",
         chg="Any", gap="Any", atr="Any", rsi="Any", pw="Any", pm="Any", pq="Any", s20="Any", s50="Any",
         s200="Any", hl52="Any", mcap="Any", flt="Any", short="Any", q="")

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
    "Low Float Runner": dict(flt="Under 20M", relvol="Over 2", chg="Up 3%", sort_by="Change", sort_asc=False),
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

GREEN, RED, GREY = "#30cc5a", "#f63538", "#414554"
PALETTE = ["#f63538", "#bf4045", "#8b444e", "#414554", "#35764e", "#2f9e4f", "#30cc5a"]

CSS = """<style>
html,body,.stApp,[data-testid="stAppViewContainer"],[data-testid="stMain"],[data-testid="stMainBlockContainer"],
[data-testid="stHeader"],[data-testid="stSidebar"],[data-testid="stSidebarContent"],[data-testid="stBottom"],
[data-testid="stBottomBlockContainer"]{background:#000 !important;background-color:#000 !important}
[data-testid="stHeader"]{height:2.4rem}
[data-testid="stToolbar"],[data-testid="stDecoration"],#MainMenu,footer{display:none !important}
.block-container{padding:3.4rem 1rem 1rem 1rem !important;max-width:100% !important;background:#000}
div[data-testid="stVerticalBlock"]{gap:.4rem}
div[data-testid="stHorizontalBlock"]{gap:.6rem}
[data-testid="stWidgetLabel"] p,label p{font-size:.72rem !important;font-weight:600;color:#9a9a9a !important;margin:0}
div[data-baseweb="select"] > div,.stTextInput input,.stNumberInput input{background:#000 !important;
  border:1px solid #262626 !important;border-radius:4px;min-height:30px;font-size:.8rem;color:#e6e6e6}
[data-testid="stExpander"] details{background:#000 !important;border:0 !important}
[data-testid="stExpander"] summary{padding:.2rem 0}
[data-testid="stTabs"] [role="tablist"]{border-bottom:1px solid #1a1a1a}
[data-testid="stTabs"] button{background:#000 !important;font-size:.82rem}
[data-testid="stForm"]{border:0 !important;padding:0 !important;background:#000}
div[data-testid="stDialog"] div[role="dialog"]{width:98vw !important;max-width:98vw !important;
  background:#000 !important;border:1px solid #151515}
.smz-brand{position:fixed;top:0;left:0;z-index:1000000;background:#000;padding:6px 18px 4px 16px;line-height:1.1}
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
<style>
html,body{margin:0;padding:0;background:#000;overflow:hidden;font-family:Arial,Helvetica,sans-serif;color:#e6e6e6}
svg{display:block;margin:0 auto;background:#000}
.leaf{cursor:pointer}.zone{cursor:zoom-in}.root{cursor:zoom-out}
text.t{fill:#fff;font-weight:700;pointer-events:none}
text.p{font-weight:600;pointer-events:none}
text.z{fill:#cfd6e0;font-weight:700;pointer-events:none;paint-order:stroke;stroke:#000;stroke-width:3px;stroke-linejoin:round}
#tip{position:fixed;pointer-events:none;display:none;background:#000;border:1px solid #2a2a2a;border-radius:6px;
  padding:7px 10px;font-size:12px;line-height:1.5;z-index:10;box-shadow:0 8px 20px rgba(0,0,0,.9)}
#legend{position:fixed;left:10px;bottom:8px;font-size:10px;color:#9a9a9a}
#legend .strip{display:flex;width:280px;margin-top:3px}
#legend .strip span{flex:1;text-align:center;color:#fff;padding:2px 0}
#msg{position:fixed;top:45%;left:0;right:0;text-align:center;color:#9a9a9a;font-size:14px;display:none}
</style></head><body><svg id="svg"></svg><div id="tip"></div><div id="legend"></div><div id="msg"></div>
<script>
(function () {
  "use strict";
  var URLS = ["https://cdn.jsdelivr.net/npm/d3@7.9.0/dist/d3.min.js",
              "https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js",
              "https://unpkg.com/d3@7.9.0/dist/d3.min.js"];
  var COLORS = ["#f63538", "#bf4045", "#8b444e", "#414554", "#35764e", "#2f9e4f", "#30cc5a"];
  var args = null, lastKey = null, view = null, resizeTimer = null;

  function post(type, extra) {
    window.parent.postMessage(Object.assign({isStreamlitMessage: true, type: type}, extra || {}), "*");
  }
  function setHeight(h) { post("streamlit:setFrameHeight", {height: h}); }
  function setValue(v) { post("streamlit:setComponentValue", {value: v, dataType: "json"}); }
  function frameH() {
    var ph = 0;
    try { ph = window.parent.innerHeight; } catch (e) {}
    return Math.max(560, (ph || 900) - 250);
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

  function legend(scale, label) {
    var f = [-1, -2 / 3, -1 / 3, 0, 1 / 3, 2 / 3, 1];
    document.getElementById("legend").innerHTML =
      "<div>Size: Market Cap &nbsp;|&nbsp; Border: " + esc(label) + " performance</div><div class=\"strip\">" +
      f.map(function (x, i) {
        var v = Math.round(x * scale);
        return "<span style=\"background:" + COLORS[i] + "\">" + (v > 0 ? "+" : "") + v + "%</span>";
      }).join("") + "</div>";
  }

  function draw(a, force) {
    var H = frameH(), W = document.body.clientWidth || window.innerWidth || 1000;
    var key = JSON.stringify([a.nodes, a.scale, a.by_industry, a.perf_label]) + "|" + W + "|" + H;
    if (!force && key === lastKey) { return; }
    lastKey = key;
    setHeight(H);
    build(a, W, H);
  }

  function build(a, W, H) {
    var nodes = a.nodes || [], byInd = !!a.by_industry, scale = +a.scale || 10;
    var svg = d3.select("#svg");
    svg.selectAll("*").remove();
    document.getElementById("tip").style.display = "none";
    if (!nodes.length) { showMsg("No stocks with market cap data for this selection."); return; }
    showMsg("");
    legend(scale, a.perf_label || "Today");
    var size = Math.min(W, H) - 6;
    svg.attr("width", W).attr("height", H).attr("viewBox", [-W / 2, -H / 2, W, H].join(" "));

    var color = d3.scaleLinear()
      .domain([-1, -2 / 3, -1 / 3, 0, 1 / 3, 2 / 3, 1].map(function (f) { return f * scale; }))
      .range(COLORS).clamp(true).interpolate(d3.interpolateRgb);

    function leaf(n) { return {name: n.ticker, kind: "leaf", d: n, value: n.mcap}; }
    var tree = {name: "All", kind: "root", children: []};
    d3.group(nodes, function (n) { return n.sector; }).forEach(function (arr, sec) {
      var s = {name: sec, kind: "sector", children: []};
      if (byInd) {
        d3.group(arr, function (n) { return n.industry; }).forEach(function (a2, ind) {
          s.children.push({name: ind, kind: "industry", children: a2.map(leaf)});
        });
      } else { s.children = arr.map(leaf); }
      tree.children.push(s);
    });
    var root = d3.hierarchy(tree)
      .sum(function (d) { return d.kind === "leaf" ? d.value : 0; })
      .sort(function (x, y) { return y.value - x.value; });
    d3.pack().size([size, size]).padding(function (d) {
      return d.depth === 0 ? 10 : (d.data.kind === "sector" ? (byInd ? 16 : 18) : 4);
    })(root);

    var node = svg.append("g").selectAll("g").data(root.descendants()).join("g");
    var circles = node.append("circle")
      .attr("class", function (d) { return d.depth === 0 ? "root" : (d.children ? "zone" : "leaf"); })
      .attr("fill", "#000000")
      .attr("stroke", function (d) { return d.depth === 0 ? "#2b2b2b" : (d.children ? "#1c1c1c" : color(d.data.d.perf)); })
      .attr("stroke-width", function (d) { return d.depth === 0 ? 2 : (d.children ? 1 : 2.4); });
    var leaves = node.filter(function (d) { return !d.children; });
    var zones = node.filter(function (d) { return d.children && d.depth > 0; });
    leaves.append("text").attr("class", "t").attr("text-anchor", "middle")
      .text(function (d) { return d.data.name; });
    leaves.append("text").attr("class", "p").attr("text-anchor", "middle")
      .attr("fill", function (d) { return color(d.data.d.perf); })
      .text(function (d) { return fmtPct(d.data.d.perf); });
    zones.append("text").attr("class", "z").attr("text-anchor", "middle")
      .text(function (d) { return d.data.name; });

    var focus = root;
    function zoomTo(v) {
      view = v;
      var k = size / v[2];
      node.attr("transform", function (d) { return "translate(" + ((d.x - v[0]) * k) + "," + ((d.y - v[1]) * k) + ")"; });
      circles.attr("r", function (d) { return d.r * k; })
        .attr("stroke-width", function (d) {
          return d.depth === 0 ? 2 : (d.children ? 1 : clamp(0.9, 3, d.r * k * 0.18));
        });
      leaves.select("text.t").each(function (d) {
        var rk = d.r * k, fs = clamp(7, 18, rk * 0.42), two = rk >= 26;
        d3.select(this).style("display", rk >= 14 ? null : "none").attr("font-size", fs).attr("y", two ? -fs * 0.1 : fs * 0.35);
      });
      leaves.select("text.p").each(function (d) {
        var rk = d.r * k, fs = clamp(7, 13, rk * 0.3);
        d3.select(this).style("display", rk >= 26 ? null : "none").attr("font-size", fs).attr("y", fs * 1.05 + clamp(0, 6, rk * 0.08));
      });
      zones.select("text.z").each(function (d) {
        var rk = d.r * k, fs = clamp(9, 20, rk * (d.data.kind === "sector" ? 0.085 : 0.07));
        d3.select(this).style("display", rk > (d.data.kind === "sector" ? 40 : 75) ? null : "none")
          .attr("font-size", fs).attr("y", -rk + fs + 3);
      });
    }
    function zoom(d) {
      focus = d;
      svg.transition().duration(650).tween("zoom", function () {
        var i = d3.interpolateZoom(view, [focus.x, focus.y, focus.r * 2]);
        return function (t) { zoomTo(i(t)); };
      });
    }
    zoomTo([root.x, root.y, root.r * 2]);

    var tip = document.getElementById("tip");
    function tipHtml(d) {
      if (d.children) {
        var n = d.leaves().length;
        return "<b>" + esc(d.data.name) + "</b><br>" + n + " stocks<br>Market Cap " + fmtCap(d.value);
      }
      var x = d.data.d;
      return "<b>" + esc(x.ticker) + "</b> &nbsp;" + esc(x.sector) + (byInd ? " &rsaquo; " + esc(x.industry) : "") +
        "<br>Market Cap " + fmtCap(x.mcap) + "<br>" + esc(a.perf_label || "Today") + " <span style=\"color:" + color(x.perf) + "\">" +
        fmtPct(x.perf) + "</span><br>Price $" + (+x.price).toFixed(2) + "<br>Volume " + Math.round(x.volume).toLocaleString("en-US");
    }
    circles.on("mouseover", function (ev, d) {
        if (d.depth === 0) { return; }
        tip.innerHTML = tipHtml(d); tip.style.display = "block";
        if (!d.children) { d3.select(this).attr("stroke-width", 3.6); }
      })
      .on("mousemove", function (ev) {
        var x = ev.clientX + 14, y = ev.clientY + 14;
        if (x + 230 > window.innerWidth) { x = ev.clientX - 240; }
        if (y + 110 > window.innerHeight) { y = ev.clientY - 110; }
        tip.style.left = Math.max(2, x) + "px"; tip.style.top = Math.max(2, y) + "px";
      })
      .on("mouseout", function (ev, d) {
        tip.style.display = "none";
        if (!d.children && view) { d3.select(this).attr("stroke-width", clamp(0.9, 3, d.r * (size / view[2]) * 0.18)); }
      })
      .on("click", function (ev, d) {
        ev.stopPropagation();
        if (d.depth === 0) { zoom(root); return; }
        if (!d.children) { setValue({ticker: d.data.name, n: Date.now() + Math.random()}); return; }
        zoom(focus === d ? (d.parent || root) : d);
      });
    svg.on("click", function () { zoom(root); });
  }

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


PERF_COLS = {"Today": "chg", "Week": "pw", "Month": "pm", "Quarter": "pq"}


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
    if s["sector"] != "Any":
        x = x[x["sector"] == s["sector"]]
    if meta["has_industry"] and s["industry"] != "Any":
        x = x[x["industry"] == s["industry"]]
    x = _opt(x, "close", PRICE, s["price"])
    x = _opt(x, "volume", VOLUME, s["vol"])
    x = _opt(x, "avgvol", VOLUME, s["avgvol"])
    x = _opt(x, "relvol", RELVOL, s["relvol"])
    x = _opt(x, "chg", CHANGE, s["chg"])
    x = _opt(x, "gap", GAP, s["gap"])
    x = _opt(x, "atr", ATR_OPT, s["atr"])
    x = _opt(x, "rsi", RSI_OPT, s["rsi"])
    for col, key in (("pw", "pw"), ("pm", "pm"), ("pq", "pq")):
        x = _opt(x, col, PERF, s[key])
    for n in (20, 50, 200):
        x = _opt(x, f"d{n}", SMA_OPTS[n], s[f"s{n}"])
    x = _hl52(x, s["hl52"])
    if meta["has_fund"]:
        x = _opt(x, "mcap", MCAP, s["mcap"])
        x = _opt(x, "float_sh", FLOAT, s["flt"])
        x = _opt(x, "short_pct", SHORT, s["short"])
    if str(s["q"]).strip():
        x = x[x["ticker"].str.contains(str(s["q"]).strip().upper(), regex=False)]
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
    """Global CSS, jet-black theme and the fixed S.M.Z logo with the collapsible Founder label."""
    brand = ('<div class="smz-brand"><div class="logo">S<span>.</span>M<span>.</span>Z</div>'
             f'<div class="fdr" tabindex="0">Founder<div class="fname">{html.escape(FOUNDER_NAME)}</div></div></div>')
    st.markdown(CSS + brand, unsafe_allow_html=True)


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
    out = "".join(f'<div class="kpi"><span>{a}</span><b style="color:{c or "#e6edf3"}">{b}</b></div>'
                  for a, b, c in tiles)
    total = max(n, 1)
    bar = (f'<div class="bar"><div style="width:{adv / total * 100:.1f}%;background:{GREEN}"></div>'
           f'<div style="width:{unch / total * 100:.1f}%;background:{GREY}"></div>'
           f'<div style="width:{dec / total * 100:.1f}%;background:{RED}"></div></div>')
    return f'<div class="kpis">{out}</div>{bar}'


# =============================================================================
# 5. MARKET MAP (treemap)
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
    ids, labels, parents, values, colors, text, hover = ["root"], ["Market"], [""], [0], ["#000000"], [""], [""]

    def add_node(i, label, parent, value, color, txt, hov):
        ids.append(i); labels.append(label); parents.append(parent); values.append(value)
        colors.append(color); text.append(txt); hover.append(hov)

    def group_stats(g):
        w = np.average(g["chg"], weights=g[size_col])
        return f"{len(g)} stocks | weighted change {w:+.2f}%"

    d = d.assign(_sector=d["sector"].fillna("Other"), _industry=d["industry"].fillna("Other"))
    for sector, gs in d.groupby("_sector"):
        sid = f"S|{sector}"
        add_node(sid, str(sector), "root", 0, "#0c0c0c", "", html.escape(f"{sector}: {group_stats(gs)}"))
        subgroups = [(sid, gs)]
        if by_industry:
            subgroups = []
            for industry, gi in gs.groupby("_industry"):
                iid = f"I|{sector}|{industry}"
                add_node(iid, str(industry), sid, 0, "#151515", "", html.escape(f"{industry}: {group_stats(gi)}"))
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
        marker=dict(colors=colors, line=dict(width=1, color="#000000"), pad=dict(t=20, l=3, r=3, b=3)),
        tiling=dict(packing="squarify", pad=1), pathbar=dict(visible=False)))
    fig.update_layout(margin=dict(t=0, l=0, r=0, b=0), height=620, paper_bgcolor="#000000", plot_bgcolor="#000000")
    return fig


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
        return components.declare_component("smz_bubbles", path=str(d))
    except Exception:
        return None


def build_bubble_payload(day: pd.DataFrame, perf_col: str, top_n: int, by_industry: bool) -> dict:
    """Rows for the bubble map: only stocks with a market cap, biggest first."""
    d = day.dropna(subset=["mcap", perf_col])
    d = d[d["mcap"] > 0].sort_values("mcap", ascending=False).head(top_n)
    nodes = [dict(ticker=str(r["ticker"]),
                  sector=str(r["sector"]) if pd.notna(r["sector"]) else "Other",
                  industry=str(r["industry"]) if pd.notna(r["industry"]) else "Other",
                  mcap=float(r["mcap"]), perf=float(r[perf_col]), price=float(r["close"]),
                  volume=float(r["volume"])) for _, r in d.iterrows()]
    return {"nodes": nodes, "by_industry": bool(by_industry)}


# =============================================================================
# 6. VIEWS
# =============================================================================
def view_overview(day: pd.DataFrame, meta: dict) -> list:
    """Market overview: KPIs, breadth bar, stock map and top lists."""
    items: list = []
    st.markdown(kpi_html(day), unsafe_allow_html=True)

    c = st.columns([1.3, 1.1, 1.0, 1.4, 3.0])
    size_opts = {"$ Volume": "dvol", "Volume": "volume"}
    if meta["has_fund"]:
        size_opts["Market Cap"] = "mcap"
    size_label = c[0].selectbox("Block Size", list(size_opts), key="map_size")
    scale = float(c[1].selectbox("Color Scale (+/- %)", [3, 5, 10, 20], index=2, key="map_scale"))
    top_n = int(c[2].number_input("Max Stocks", 30, 500, 250, step=25, key="map_top"))
    by_ind = c[3].toggle("Group by Industry", value=meta["has_industry"], disabled=not meta["has_industry"],
                         key="map_ind")
    fig = build_treemap(day, size_opts[size_label], scale, top_n, bool(by_ind) and meta["has_industry"])
    if fig is None:
        st.info("Not enough data to draw the map for this date.")
    else:
        stretch(st.plotly_chart, fig, config={"displayModeBar": False}, theme=None)
        st.markdown(legend_html(scale), unsafe_allow_html=True)

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


def view_bubbles(day: pd.DataFrame, meta: dict) -> list:
    """Bubble map: one big circle > sectors > industries > companies. Size = market cap, border = performance."""
    if not meta["has_fund"]:
        st.info("Market cap data is still loading. The bubble map appears as soon as it is available.")
        return []
    c = st.columns([1.1, 1.3, 1.0, 1.6, 3.0])
    label = c[0].selectbox("Performance", list(PERF_COLS), key="bub_perf")
    scale = float(c[1].selectbox("Color Scale (+/- %)", [3, 5, 10, 20, 30, 50], index=2, key="bub_scale"))
    top_n = int(c[2].number_input("Max Stocks", 20, 500, 150, step=10, key="bub_top"))
    by_ind = c[3].toggle("Group by Industry", value=meta["has_industry"], disabled=not meta["has_industry"],
                         key="bub_ind")
    perf_col = PERF_COLS[label]
    payload = build_bubble_payload(day, perf_col, top_n, bool(by_ind) and meta["has_industry"])
    hidden = int((day["mcap"].isna() | (day["mcap"] <= 0)).sum())
    c[4].caption(f"{len(payload['nodes'])} stocks shown" + (f" | {hidden} without market cap data" if hidden else ""))
    comp = get_bubble_component()
    if comp is None:
        st.warning("The interactive map is not available in this environment.")
        return []
    ev = comp(nodes=payload["nodes"], by_industry=payload["by_industry"], scale=scale, perf_label=label,
              key="smz_bubbles", default=None)
    if isinstance(ev, dict) and ev.get("ticker"):
        sym = clean_symbol(str(ev["ticker"]))
        if sym:
            return [(f"bubbles:{ev.get('n')}", sym)]
    return []


def _sel(col, label, options, key, disabled=False):
    col.selectbox(label, list(options), key=key, disabled=disabled)


def filter_ui(day: pd.DataFrame, meta: dict) -> None:
    top = st.columns([2, 1])
    top[0].selectbox("Signal", list(SIGNALS), key="signal", on_change=apply_signal)
    top[1].write("")
    top[1].button("Reset", on_click=reset_filters, key="reset_btn")
    tabs = st.tabs(["Descriptive", "Technical", "Trend", "Fundamental"])
    with tabs[0]:
        a = st.columns(6)
        a[0].text_input("Ticker", key="q")
        _sel(a[1], "Exchange", EXCHANGES, "exch", disabled=not meta["has_exchange"])
        _sel(a[2], "Sector", ["Any"] + sorted(day["sector"].dropna().unique()), "sector")
        _sel(a[3], "Industry", ["Any"] + sorted(day["industry"].dropna().unique()), "industry",
             disabled=not meta["has_industry"])
        _sel(a[4], "Price", PRICE, "price")
        _sel(a[5], "Average Volume", VOLUME, "avgvol")
        b = st.columns(6)
        _sel(b[0], "Relative Volume", RELVOL, "relvol")
        _sel(b[1], "Volume", VOLUME, "vol")
    with tabs[1]:
        c = st.columns(6)
        _sel(c[0], "Change", CHANGE, "chg")
        _sel(c[1], "Gap", GAP, "gap")
        _sel(c[2], "Average True Range", ATR_OPT, "atr")
        _sel(c[3], "RSI (14)", RSI_OPT, "rsi")
        _sel(c[4], "Performance (Week)", PERF, "pw")
        _sel(c[5], "Performance (Month)", PERF, "pm")
        c2 = st.columns(6)
        _sel(c2[0], "Performance (Quarter)", PERF, "pq")
    with tabs[2]:
        t = st.columns(6)
        _sel(t[0], "20-Day Simple Moving Average", SMA_OPTS[20], "s20")
        _sel(t[1], "50-Day Simple Moving Average", SMA_OPTS[50], "s50")
        _sel(t[2], "200-Day Simple Moving Average", SMA_OPTS[200], "s200")
        _sel(t[3], "52-Week High/Low", HL52, "hl52")
    with tabs[3]:
        if not meta["has_fund"]:
            st.info("Fundamental data is still loading. It fills in automatically within a few hours.")
        f = st.columns(6)
        _sel(f[0], "Market Cap.", MCAP, "mcap", disabled=not meta["has_fund"])
        _sel(f[1], "Float", FLOAT, "flt", disabled=not meta["has_fund"])
        _sel(f[2], "Short Float", SHORT, "short", disabled=not meta["has_fund"])


def view_screener(day: pd.DataFrame, meta: dict) -> list:
    """Filters + compact multi-column table. Click any cell of a row to open its chart."""
    with st.expander("Filters", expanded=True):
        filter_ui(day, meta)
    x = apply_filters(day, S, meta)

    order = ["ticker", "sector"] + (["industry"] if meta["has_industry"] else []) + \
            (list(FUND_COLS) if meta["has_fund"] else []) + \
            ["close", "chg", "gap", "volume", "avgvol", "relvol", "atr", "rsi", "d20", "d50", "d200",
             "hid", "lod", "pw", "pm", "pq"]
    names = [COLS[c] for c in order]
    if S.get("sort_by") not in names:
        S["sort_by"] = "Change"
    bar = st.columns([3, 1.4, 1, 1])
    bar[0].markdown(f"**{S['date']}: {len(x)} stocks** &nbsp;|&nbsp; click any row to open its chart")
    bar[1].selectbox("Order", names, key="sort_by", label_visibility="collapsed")
    bar[2].checkbox("Ascending", key="sort_asc")
    inv = {v: k for k, v in COLS.items()}
    x = x.sort_values(inv[S["sort_by"]], ascending=bool(S["sort_asc"]))
    if x.empty:
        st.info("No stocks match these filters. Try loosening them.")
        return []
    full = to_display(x, order, with_no=True)
    bar[3].download_button("Export", full.round(2).to_csv(index=False).encode(), "smz_screener.csv", "text/csv")
    return [render_table(full, "tbl_screener", 640)]


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
    c = st.columns([1.6, 1.0, 3.4])
    opts = ["Overview", "Screener", "Bubbles"]
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
            q = f[0].text_input("Search", key="open_q", placeholder="Search ticker", label_visibility="collapsed")
            go_ = f[1].form_submit_button("Go")
        if go_:
            forced = clean_symbol(q)
            if forced is None:
                st.warning("Enter a valid ticker (letters and digits only).")
    return view or "Overview", forced


def main() -> None:
    st.set_page_config(layout="wide", page_title=PAGE_TITLE, page_icon="📈", initial_sidebar_state="collapsed",
                       menu_items={"Get help": None, "Report a bug": None, "About": None})
    inject_header()
    init_state()
    p, meta = load_data()
    dates = sorted(p["date"].unique(), reverse=True)

    view, forced = header(dates)
    day = p[p["date"] == S["date"]]
    if view == "Overview":
        items = view_overview(day, meta)
    elif view == "Bubbles":
        items = view_bubbles(day, meta)
    else:
        items = view_screener(day, meta)

    to_open, S["opened"] = decide_open(items, S.get("opened"), forced)
    st.caption(f"© {BRAND} | For informational purposes only.")
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

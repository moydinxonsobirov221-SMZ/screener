"""
S.M.Z AI moduli — TradingView grafigi + to'liq ekranli AI tahlil oynasi.

O'rnatish:
    requirements.txt ga:  google-genai

Kalit (Streamlit Cloud -> Settings -> Secrets):
    GEMINI_API_KEY = "AIza..."
    # ixtiyoriy:
    GEMINI_MODEL = "gemini-2.5-flash"

Asosiy kodingizda ticker bosilganda:
    from smz_ai import render_ticker_detail
    render_ticker_detail(ticker)
"""

import datetime as dt

import streamlit as st
import streamlit.components.v1 as components

# ----------------------------------------------------------------------
# DOIMIY (SYSTEM) PROMPT — foydalanuvchi har safar yubormaydi
# ----------------------------------------------------------------------
SYSTEM_PROMPT = """Siz professional, qisqa muddatli (short-term) AQSh aksiyalari treyderisiz. \
Foydalanuvchi aksiyalarni maksimal 1-10 kun ushlab turadi (swing/day trading). \
Berilgan aksiya tikerini quyidagi 8 ta band asosida, eng so'nggi joriy faktlarga tayanib \
(web qidiruvdan foydalaning), maksimal 77 ta so'zdan iborat o'ta qisqa va lo'nda ko'rinishda tahlil qiling:
1. Joriy Katalizator (News Impact): Oxirgi 24-48 soat ichidagi eng muhim yangilik va uning bugungi narx harakatiga ta'siri.
2. Narx Darajalari: Eng yaqin 2 ta Support va 2 ta Resistance nuqtalari.
3. Trend va Momentum: 1 soatlik va Kunlik grafikdagi trend, 9 EMA, 20 EMA va 50 SMA ga nisbatan joylashuvi.
4. Texnik Indikatorlar: RSI, MACD va VWAP joriy holati.
5. Hajm va Volatillik: Savdo hajmi va ATR ko'rsatkichi.
6. Bozor Sentimenti: Tahlilchilar va ijtimoiy tarmoq kutilmalari.
7. Ehtimoliy Diapazon (5 kunlik): Kelgusi 5 kunlik yuqori va pastki narx chegaralari.
8. Yakuniy Xulosa va Signal: Barcha omillarni birlashtirib, faqat "SOTIB OLING (BUY)", \
"KUTING (HOLD/WAIT)" yoki "SOTMANG / SOTING (SELL)" signallaridan birini aniq, ikkilanishlarsiz yozing va qisqa xulosa bering.
Javobni o'zbek tilida, 8 ta raqamlangan band ko'rinishida bering. Jami 77 so'zdan oshmasin."""

DEFAULT_MODEL = "gemini-2.5-flash"


# ----------------------------------------------------------------------
# State boshqaruvi
# ----------------------------------------------------------------------
def _init_state():
    st.session_state.setdefault("detail_view", "chart")  # "chart" | "ai"
    st.session_state.setdefault("ai_cache", {})          # {(ticker, sana): matn}


def _open_ai():
    st.session_state["detail_view"] = "ai"


def _back_to_chart():
    st.session_state["detail_view"] = "chart"


def reset_detail_view():
    """Yangi ticker bosilganda chaqiring — doim grafikdan boshlash uchun."""
    st.session_state["detail_view"] = "chart"


# ----------------------------------------------------------------------
# TradingView grafigi
# ----------------------------------------------------------------------
def _tradingview_html(ticker: str, height: int) -> str:
    return f"""
    <div class="tradingview-widget-container" style="height:{height}px;width:100%;">
      <div id="tv_chart" style="height:100%;width:100%;"></div>
      <script src="https://s3.tradingview.com/tv.js"></script>
      <script>
        new TradingView.widget({{
          "autosize": true,
          "symbol": "{ticker}",
          "interval": "D",
          "timezone": "America/New_York",
          "theme": "dark",
          "style": "1",
          "locale": "en",
          "allow_symbol_change": true,
          "container_id": "tv_chart"
        }});
      </script>
    </div>
    """


def _render_chart_view(ticker: str):
    # Yuqori o'ng burchakda ramka ichida "S.M.Z AI" tugmasi
    with st.container(border=True):
        left, right = st.columns([5, 1])
        with left:
            st.markdown(f"### {ticker}")
        with right:
            st.button(
                "✦ S.M.Z AI",
                key="btn_open_ai",
                on_click=_open_ai,
                use_container_width=True,
            )
        components.html(_tradingview_html(ticker, 720), height=730)


# ----------------------------------------------------------------------
# AI tahlil
# ----------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def _get_client(api_key: str):
    from google import genai
    return genai.Client(api_key=api_key)

def _run_analysis(ticker: str) -> str:
    from google.genai import types

    api_key = st.secrets.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY Streamlit secrets ichida topilmadi.")

    model = st.secrets.get("GEMINI_MODEL", DEFAULT_MODEL)
    today = dt.date.today().isoformat()
    client = _get_client(api_key)
    prompt = f"Tiker: {ticker}. Bugungi sana: {today}. Tahlil qiling."

    def _call(use_search: bool) -> str:
        cfg = dict(system_instruction=SYSTEM_PROMPT, max_output_tokens=2000)
        if use_search:
            cfg["tools"] = [types.Tool(google_search=types.GoogleSearch())]
        r = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(**cfg),
        )
        return (r.text or "").strip()

    try:
        text = _call(True)
    except Exception as e:  # noqa: BLE001
        if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
            text = _call(False)
            if text:
                text += "\n\n_⚠️ Jonli web-qidiruv limiti tugagan, tahlil umumiy bilimga asoslangan. Yangiliklar eskirgan bo'lishi mumkin._"
        else:
            raise
    if not text:
        raise RuntimeError("AI bo'sh javob qaytardi.")
    return text


def _render_ai_view(ticker: str):
    # To'liq ekran ko'rinishi uchun yengil CSS
    st.markdown(
        """
        <style>
        .smz-ai-box {
            min-height: 80vh; padding: 28px 32px;
            border: 1px solid #2a2f3a; border-radius: 14px;
            background: #0e1117; font-size: 1.1rem; line-height: 1.7;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    top_l, top_r = st.columns([5, 1])
    with top_l:
        st.markdown(f"## S.M.Z AI · {ticker}")
    with top_r:
        st.button(
            "← Grafikka qaytish",
            key="btn_back_chart",
            on_click=_back_to_chart,
            use_container_width=True,
        )

    cache_key = (ticker, dt.date.today().isoformat())
    cache = st.session_state["ai_cache"]

    refresh = st.button("↻ Qayta tahlil", key="btn_refresh_ai")
    if refresh:
        cache.pop(cache_key, None)

    if cache_key not in cache:
        with st.spinner(f"{ticker} tahlil qilinmoqda..."):
            try:
                cache[cache_key] = _run_analysis(ticker)
            except Exception as e:  # noqa: BLE001
                st.error(f"AI xatosi: {e}")
                return

    st.markdown('<div class="smz-ai-box">', unsafe_allow_html=True)
    st.markdown(cache[cache_key])
    st.markdown("</div>", unsafe_allow_html=True)
    st.caption("Bu moliyaviy maslahat emas. Qaror o'zingizniki.")


# ----------------------------------------------------------------------
# Asosiy kirish nuqtasi
# ----------------------------------------------------------------------
def render_ticker_detail(ticker: str):
    """Ticker bosilganda shu funksiyani chaqiring."""
    _init_state()

    # Boshqa ticker tanlansa, grafikka qaytaramiz
    if st.session_state.get("last_ticker") != ticker:
        st.session_state["last_ticker"] = ticker
        reset_detail_view()

    if st.session_state["detail_view"] == "ai":
        _render_ai_view(ticker)
    else:
        _render_chart_view(ticker)

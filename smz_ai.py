"""S.M.Z AI moduli (Groq). Saytdagi haqiqiy ma'lumotlar asosida qisqa tahlil beradi."""

import datetime as dt
import math

import requests
import streamlit as st

API_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "llama-3.3-70b-versatile"

SYSTEM_PROMPT = """Siz professional, qisqa muddatli (short-term) AQSh aksiyalari treyderisiz. \
Foydalanuvchi aksiyalarni maksimal 1-10 kun ushlab turadi (swing/day trading). \
Sizga tikerning HAQIQIY bozor ma'lumotlari beriladi. Faqat berilgan raqamlarga tayaning, \
raqamlarni o'zingizdan to'qimang. Internetga ulanish yo'q: yangilik haqida aniq ma'lumot bo'lmasa, \
1-bandda "Yangilik ma'lumoti yo'q" deb yozing va hajm/gap/o'zgarishdan xulosa qiling. \
Quyidagi 8 ta band asosida, maksimal 77 ta so'zdan iborat o'ta qisqa va lo'nda tahlil qiling:
1. Joriy Katalizator (News Impact)
2. Narx Darajalari: eng yaqin 2 ta Support va 2 ta Resistance (SMA va 52 haftalik darajalardan foydalaning)
3. Trend va Momentum: Kunlik trend, SMA20/50/200 ga nisbatan joylashuvi
4. Texnik Indikatorlar: RSI holati (MACD, VWAP, EMA berilmagan bo'lsa, "ma'lumot yo'q" deng)
5. Hajm va Volatillik: nisbiy hajm va ATR
6. Bozor Sentimenti: sektor va narx harakatidan kelib chiqib
7. Ehtimoliy Diapazon (5 kunlik): berilgan taxminiy chegaralardan foydalaning
8. Yakuniy Xulosa va Signal: faqat "SOTIB OLING (BUY)", "KUTING (HOLD/WAIT)" yoki \
"SOTMANG / SOTING (SELL)" dan birini aniq, ikkilanishlarsiz yozing va qisqa xulosa bering.
Javobni o'zbek tilida, 8 ta raqamlangan band ko'rinishida bering. Jami 77 so'zdan oshmasin."""


def _num(row, key):
    try:
        v = float(row.get(key))
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) else v


def build_facts(ticker: str, row) -> str:
    """Saytdagi qatordan AI uchun ma'lumotlar matnini yasaydi."""
    lines = [f"Tiker: {ticker}", f"Bugungi sana: {dt.date.today().isoformat()}"]
    if row is None:
        lines.append("Bozor ma'lumotlari mavjud emas.")
        return "\n".join(lines)

    close = _num(row, "close")

    def add(label, key, fmt="{:.2f}", suffix=""):
        v = _num(row, key)
        if v is not None:
            lines.append(f"{label}: {fmt.format(v)}{suffix}")

    if row.get("sector") is not None:
        lines.append(f"Sektor: {row.get('sector')}")
    add("Narx", "close", "${:.2f}")
    add("Kunlik o'zgarish", "chg", "{:+.2f}", "%")
    add("Gap", "gap", "{:+.2f}", "%")
    add("Hajm", "volume", "{:,.0f}")
    add("O'rtacha hajm (20 kun)", "avgvol", "{:,.0f}")
    add("Nisbiy hajm", "relvol", "{:.2f}", "x")
    add("ATR(14)", "atr", "{:.2f}")
    add("RSI(14)", "rsi", "{:.1f}")
    add("Haftalik natija", "pw", "{:+.1f}", "%")
    add("Oylik natija", "pm", "{:+.1f}", "%")
    add("Choraklik natija", "pq", "{:+.1f}", "%")
    add("Market cap (mln $)", "mcap", "{:,.0f}")

    if close:
        for n in (20, 50, 200):
            d = _num(row, f"d{n}")
            if d is not None:
                lines.append(f"SMA{n}: ${close / (1 + d / 100):.2f} (narx {d:+.1f}% farq bilan)")
        hid, lod = _num(row, "hid"), _num(row, "lod")
        if hid is not None:
            lines.append(f"52 haftalik High: ${close / (1 + hid / 100):.2f}")
        if lod is not None:
            lines.append(f"52 haftalik Low: ${close / (1 + lod / 100):.2f}")
        atr = _num(row, "atr")
        if atr is not None:
            rng = atr * 2.24
            lines.append(f"Taxminiy 5 kunlik diapazon: ${max(close - rng, 0):.2f} - ${close + rng:.2f}")
    lines.append("MACD, VWAP, EMA9/EMA20 va yangiliklar berilmagan.")
    return "\n".join(lines)


def run_analysis(ticker: str, row) -> str:
    api_key = st.secrets.get("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY Streamlit secrets ichida topilmadi.")
    model = st.secrets.get("GROQ_MODEL", DEFAULT_MODEL)

    resp = requests.post(
        API_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_facts(ticker, row)},
            ],
            "temperature": 0.3,
            "max_tokens": 2000,
            "reasoning_effort": "low",
        },
        timeout=60,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Groq {resp.status_code}: {resp.text[:300]}")
    text = (resp.json()["choices"][0]["message"]["content"] or "").strip()
    if not text:
        raise RuntimeError("AI bo'sh javob qaytardi.")
    return text.replace("$", "\\$")

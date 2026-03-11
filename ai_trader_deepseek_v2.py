"""
╔══════════════════════════════════════════════════════════════╗
║       AI TRADER v2.0 — Powered by DeepSeek V3.2            ║
║  Chiến lược: Technical Analysis + News Sentiment + Filter   ║
║  Chỉ gửi signal khi confidence ≥ 70%                        ║
║  Chi phí ước tính: < $0.50/tháng                            ║
╚══════════════════════════════════════════════════════════════╝

Cài đặt:
    pip install -r requirements.txt

Chạy:
    python ai_trader_deepseek_v2.py

Cấu hình:
    Sao chép .env.example → .env và điền API keys
    Đăng ký DeepSeek API (có 5M token miễn phí):
    → https://platform.deepseek.com
"""

import os
import re
import json
import time
import logging
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from openai import OpenAI          # DeepSeek dùng OpenAI-compatible SDK
from dotenv import load_dotenv

# ─── Load biến môi trường ────────────────────────────────────
load_dotenv()

DEEPSEEK_API_KEY   = os.getenv("DEEPSEEK_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
NEWS_API_KEY       = os.getenv("NEWS_API_KEY", "")   # Tùy chọn

# ─── Kiểm tra key bắt buộc ──────────────────────────────────
for key, val in [("DEEPSEEK_API_KEY", DEEPSEEK_API_KEY),
                 ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
                 ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)]:
    if not val:
        raise EnvironmentError(f"❌ Thiếu {key} trong file .env")

# ─── Cấu hình bot ───────────────────────────────────────────
SYMBOLS           = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "SUIUSDT"]
INTERVAL          = "1h"
BINANCE_FUTURES   = "https://fapi.binance.com/fapi/v1/klines"
MIN_CONFIDENCE    = 70        # Lọc signal yếu
LOOP_INTERVAL_SEC = 3600      # Chạy mỗi 1 giờ
MAX_RETRIES       = 3
RETRY_DELAY_SEC   = 5

# DeepSeek model — dùng deepseek-chat (V3.2, rẻ nhất, cache tự động)
DEEPSEEK_MODEL    = "deepseek-chat"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"

# ─── Logging ────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("ai_trader_v2.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ─── DeepSeek client (OpenAI-compatible) ────────────────────
client = OpenAI(
    api_key  = DEEPSEEK_API_KEY,
    base_url = DEEPSEEK_BASE_URL
)

# System prompt dùng chung → tận dụng context caching DeepSeek
# (Cache hit = $0.028/M thay vì $0.28/M → tiết kiệm 90%)
SYSTEM_PROMPT = """Bạn là chuyên gia phân tích kỹ thuật crypto chuyên nghiệp với 10 năm kinh nghiệm.
Nhiệm vụ: Phân tích dữ liệu kỹ thuật và sentiment để đưa ra signal giao dịch Futures.
Quy tắc bắt buộc:
- Chỉ trả về JSON hợp lệ, không giải thích thêm bên ngoài JSON
- SL phải đặt cách entry ít nhất 1x ATR
- TP phải đảm bảo R:R tối thiểu 1:2
- Confidence phản ánh độ chắc chắn thực tế (không inflate)
- Nếu tín hiệu mâu thuẫn hoặc không rõ ràng → signal: WAIT"""


# ════════════════════════════════════════════════════════════
#  1. LẤY DỮ LIỆU BINANCE FUTURES
# ════════════════════════════════════════════════════════════
def fetch_klines(symbol: str, interval: str = "1h", limit: int = 200) -> pd.DataFrame:
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(
                BINANCE_FUTURES,
                params={"symbol": symbol, "interval": interval, "limit": limit},
                timeout=10
            )
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict) and data.get("code"):
                raise ValueError(f"Binance error: {data}")
            if not data:
                raise ValueError("Binance trả về dữ liệu rỗng")
            df = pd.DataFrame(data, columns=[
                "open_time","open","high","low","close","volume",
                "close_time","qav","trades","tb_base","tb_quote","ignore"
            ])
            return df.astype(float)
        except Exception as e:
            log.warning(f"[{symbol}] fetch lần {attempt+1}: {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY_SEC)
    raise RuntimeError(f"[{symbol}] Không lấy được dữ liệu sau {MAX_RETRIES} lần")


# ════════════════════════════════════════════════════════════
#  2. CHỈ BÁO KỸ THUẬT
# ════════════════════════════════════════════════════════════
def ema(s: pd.Series, p: int) -> pd.Series:
    return s.ewm(span=p, adjust=False).mean()

def rsi(s: pd.Series, p: int = 14) -> pd.Series:
    d = s.diff()
    up   = d.clip(lower=0)
    down = -d.clip(upper=0)
    rs   = up.ewm(span=p, adjust=False).mean() / (down.ewm(span=p, adjust=False).mean() + 1e-10)
    return 100 - (100 / (1 + rs))

def macd(s: pd.Series):
    line = ema(s, 12) - ema(s, 26)
    sig  = ema(line, 9)
    return line, sig, line - sig

def bollinger(s: pd.Series, p: int = 20):
    mid = s.rolling(p).mean()
    std = s.rolling(p).std()
    return mid + 2*std, mid, mid - 2*std

def atr(df: pd.DataFrame, p: int = 14) -> float:
    h, l, cp = df["high"], df["low"], df["close"].shift(1)
    tr = pd.concat([(h-l), (h-cp).abs(), (l-cp).abs()], axis=1).max(axis=1)
    return tr.ewm(span=p, adjust=False).mean().iloc[-1]

def stoch_rsi(s: pd.Series, p: int = 14) -> float:
    """Stochastic RSI — đo RSI đang ở vùng nào"""
    rsi_vals = rsi(s, p)
    min_rsi  = rsi_vals.rolling(p).min()
    max_rsi  = rsi_vals.rolling(p).max()
    stoch    = (rsi_vals - min_rsi) / (max_rsi - min_rsi + 1e-10) * 100
    return round(stoch.iloc[-1], 2)

def compute_indicators(df: pd.DataFrame) -> dict:
    closes = df["close"]
    price  = closes.iloc[-1]

    ema20, ema50, ema200 = ema(closes,20).iloc[-1], ema(closes,50).iloc[-1], ema(closes,200).iloc[-1]
    rsi_val   = rsi(closes).iloc[-1]
    stoch_val = stoch_rsi(closes)
    ml, ms, mh = macd(closes)
    bu, bm, bl = bollinger(closes)
    atr_val   = atr(df)

    # Volume surge
    vol5  = df["volume"].iloc[-5:].mean()
    vol20 = df["volume"].iloc[-20:].mean()
    vol_ratio = vol5 / (vol20 + 1e-10)

    # Candlestick pattern đơn giản (3 nến gần nhất)
    last3 = df.tail(3)
    bodies = (last3["close"] - last3["open"]).tolist()
    pattern = "BULLISH_3" if all(b > 0 for b in bodies) else \
              "BEARISH_3" if all(b < 0 for b in bodies) else "MIXED"

    # BB position %
    bb_range = bu.iloc[-1] - bl.iloc[-1]
    bb_pos   = (price - bl.iloc[-1]) / (bb_range + 1e-10) * 100

    return {
        "price"     : round(price, 4),
        "ema20"     : round(ema20, 4),
        "ema50"     : round(ema50, 4),
        "ema200"    : round(ema200, 4),
        "rsi"       : round(rsi_val, 2),
        "stoch_rsi" : stoch_val,
        "macd_line" : round(ml.iloc[-1], 4),
        "macd_sig"  : round(ms.iloc[-1], 4),
        "macd_hist" : round(mh.iloc[-1], 4),
        "bb_upper"  : round(bu.iloc[-1], 4),
        "bb_mid"    : round(bm.iloc[-1], 4),
        "bb_lower"  : round(bl.iloc[-1], 4),
        "bb_pos_pct": round(bb_pos, 1),
        "vol_ratio" : round(vol_ratio, 2),
        "atr"       : round(atr_val, 4),
        "pattern"   : pattern,
    }


# ════════════════════════════════════════════════════════════
#  3. NEWS SENTIMENT
# ════════════════════════════════════════════════════════════
COIN_KEYWORDS = {
    "BTCUSDT": "Bitcoin BTC",
    "ETHUSDT": "Ethereum ETH",
    "SOLUSDT": "Solana SOL",
    "SUIUSDT": "Sui crypto SUI",
}

def fetch_news(symbol: str, max_articles: int = 5) -> list[str]:
    if not NEWS_API_KEY:
        return []
    kw = COIN_KEYWORDS.get(symbol, symbol.replace("USDT", ""))
    try:
        r = requests.get(
            "https://newsapi.org/v2/everything",
            params={"q": kw, "language": "en", "sortBy": "publishedAt",
                    "pageSize": max_articles, "apiKey": NEWS_API_KEY},
            timeout=8
        )
        r.raise_for_status()
        return [a["title"] for a in r.json().get("articles", []) if a.get("title")][:max_articles]
    except Exception as e:
        log.warning(f"News API lỗi: {e}")
        return []


# ════════════════════════════════════════════════════════════
#  4. GỌI DEEPSEEK V3.2
# ════════════════════════════════════════════════════════════
def analyze_with_deepseek(symbol: str, ind: dict, headlines: list[str]) -> dict:
    """Gửi data lên DeepSeek V3.2, nhận JSON signal"""

    news_block = (
        "Tin tức gần nhất:\n" + "\n".join(f"- {h}" for h in headlines)
        if headlines else "Tin tức: Không có dữ liệu."
    )

    trend_long = "TĂNG" if ind["price"] > ind["ema200"] else "GIẢM"
    trend_mid  = "TĂNG" if ind["ema20"] > ind["ema50"]  else "GIẢM"
    macd_cross = "BULLISH" if ind["macd_line"] > ind["macd_sig"] else "BEARISH"

    user_prompt = f"""Phân tích Futures {symbol} khung {INTERVAL}:

📊 GIÁ & TREND:
- Giá: {ind['price']} | EMA20: {ind['ema20']} | EMA50: {ind['ema50']} | EMA200: {ind['ema200']}
- Xu hướng dài hạn (vs EMA200): {trend_long}
- Xu hướng trung hạn (EMA20 vs EMA50): {trend_mid}
- Candle pattern 3 nến: {ind['pattern']}

📈 MOMENTUM:
- RSI(14): {ind['rsi']} {'⚠️ QUÁ MUA' if ind['rsi']>70 else '⚠️ QUÁ BÁN' if ind['rsi']<30 else ''}
- Stoch RSI: {ind['stoch_rsi']} {'(vùng mua)' if ind['stoch_rsi']<20 else '(vùng bán)' if ind['stoch_rsi']>80 else ''}
- MACD: {ind['macd_line']} | Signal: {ind['macd_sig']} | Hist: {ind['macd_hist']} → {macd_cross}

📉 BOLLINGER BANDS:
- Upper: {ind['bb_upper']} | Mid: {ind['bb_mid']} | Lower: {ind['bb_lower']}
- Vị trí giá: {ind['bb_pos_pct']}% (0=lower, 100=upper)

📦 VOLUME & BIẾN ĐỘNG:
- Volume ratio (5 vs 20 nến): {ind['vol_ratio']}x {'🔥 TĂNG ĐỘT BIẾN' if ind['vol_ratio']>1.5 else ''}
- ATR(14): {ind['atr']}

📰 {news_block}

Trả về JSON (không thêm text ngoài JSON):
{{
  "signal"     : "LONG|SHORT|WAIT",
  "confidence" : <0-100, thực tế, không inflate>,
  "entry"      : <giá vào>,
  "sl"         : <stop loss, cách entry ≥ 1x ATR>,
  "tp"         : <take profit, R:R ≥ 1:2>,
  "rr"         : "1:X",
  "sentiment"  : "BULLISH|BEARISH|NEUTRAL",
  "reason"     : "<2 câu tiếng Việt, nêu lý do chính>"
}}"""

    for attempt in range(MAX_RETRIES):
        try:
            response = client.chat.completions.create(
                model       = DEEPSEEK_MODEL,
                temperature = 0.1,   # Thấp → nhất quán, ít ảo giác
                max_tokens  = 300,
                messages    = [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": user_prompt}
                ]
            )
            raw   = response.choices[0].message.content
            match = re.search(r"\{.*\}", raw, re.S)
            if not match:
                raise ValueError("Không tìm thấy JSON trong response")
            return json.loads(match.group(0))

        except Exception as e:
            log.warning(f"[{symbol}] DeepSeek lần {attempt+1}/{MAX_RETRIES}: {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY_SEC)

    return {
        "signal": "WAIT", "confidence": 0,
        "entry": ind["price"], "sl": ind["price"], "tp": ind["price"],
        "rr": "N/A", "sentiment": "NEUTRAL", "reason": "Lỗi phân tích AI"
    }


# ════════════════════════════════════════════════════════════
#  5. GỬI TELEGRAM
# ════════════════════════════════════════════════════════════
def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    # Telegram giới hạn 4096 ký tự
    if len(text) > 4096:
        text = text[:4050] + "\n\n<i>... (đã cắt bớt)</i>"
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.post(
                url,
                data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
                timeout=10
            )
            r.raise_for_status()
            return
        except Exception as e:
            log.warning(f"Telegram lần {attempt+1}: {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY_SEC)
    log.error("❌ Gửi Telegram thất bại")


def format_message(symbol: str, info: dict, ind: dict) -> str:
    sig_icon  = {"LONG": "🟢📈", "SHORT": "🔴📉"}.get(info["signal"], "⚪⏸️")
    conf      = info["confidence"]
    conf_icon = "🔥" if conf >= 85 else "✅" if conf >= 70 else "🔸"
    sent_icon = {"BULLISH": "🐂", "BEARISH": "🐻"}.get(info.get("sentiment",""), "➖")
    trend     = "↑ TRÊN EMA200" if ind["price"] > ind["ema200"] else "↓ DƯỚI EMA200"

    return (
        f"{sig_icon} <b>{symbol}</b> ({INTERVAL})\n"
        f"├ 🎯 <b>Signal:</b> {info['signal']} — <b>{conf}%</b> {conf_icon}\n"
        f"├ 💰 <b>Entry:</b> ${info['entry']:,.4f}\n"
        f"├ 🛑 <b>SL:</b> ${info['sl']:,.4f}\n"
        f"├ ✅ <b>TP:</b> ${info['tp']:,.4f} ({info['rr']})\n"
        f"├ {sent_icon} <b>Sentiment:</b> {info.get('sentiment','N/A')}\n"
        f"├ 📊 RSI: <b>{ind['rsi']}</b> | StochRSI: <b>{ind['stoch_rsi']}</b> | Vol: <b>{ind['vol_ratio']}x</b>\n"
        f"├ 📉 Trend: {trend} | Pattern: {ind['pattern']}\n"
        f"└ 💬 {info['reason']}\n"
    )


# ════════════════════════════════════════════════════════════
#  6. VÒNG LẶP CHÍNH
# ════════════════════════════════════════════════════════════
def run_cycle():
    now          = datetime.now(timezone.utc).strftime("%H:%M UTC %d/%m/%Y")
    signals_sent = 0
    skipped      = 0
    signal_lines = []

    log.info(f"🔄 Chu kỳ mới — {now}")

    for symbol in SYMBOLS:
        try:
            log.info(f"  → Phân tích {symbol}...")
            df        = fetch_klines(symbol, INTERVAL)
            ind       = compute_indicators(df)
            headlines = fetch_news(symbol)
            info      = analyze_with_deepseek(symbol, ind, headlines)
            conf      = info.get("confidence", 0)

            log.info(f"  ✓ {symbol}: {info['signal']} ({conf}%)")

            if conf < MIN_CONFIDENCE or info["signal"] == "WAIT":
                skipped += 1
                log.info(f"  ⏭️  Bỏ qua {symbol} (conf={conf}% < {MIN_CONFIDENCE}% hoặc WAIT)")
                continue

            signal_lines.append(format_message(symbol, info, ind))
            signals_sent += 1

        except Exception as e:
            log.error(f"  ❌ {symbol}: {e}")
            signal_lines.append(f"❌ <b>{symbol}</b>: Lỗi\n")

        time.sleep(1.5)  # Tránh rate limit

    # Tạo tin nhắn tổng hợp
    header = (
        f"🤖 <b>AI Trader v2.0 — DeepSeek V3.2</b>\n"
        f"⏰ {now}\n"
        f"🎯 Min confidence: {MIN_CONFIDENCE}% | "
        f"Gửi: <b>{signals_sent}</b> | Bỏ qua: {skipped}\n"
        f"{'─' * 30}\n\n"
    )
    body = "\n".join(signal_lines) if signal_lines else \
           "⏳ <i>Không có signal đủ độ tin cậy.</i>\n"

    send_telegram(header + body)
    log.info(f"✅ Hoàn tất — {signals_sent} signals gửi đi")


def main():
    log.info("╔══════════════════════════════════════════╗")
    log.info("║  AI Trader v2.0 — DeepSeek V3.2         ║")
    log.info(f"║  Symbols : {', '.join(SYMBOLS):<30}║")
    log.info(f"║  Filter  : confidence ≥ {MIN_CONFIDENCE}%              ║")
    log.info("╚══════════════════════════════════════════╝")

    while True:
        try:
            run_cycle()
        except KeyboardInterrupt:
            log.info("🛑 Dừng bot.")
            break
        except Exception as e:
            log.error(f"⚠️ Lỗi chu kỳ: {e}")

        log.info(f"😴 Nghỉ {LOOP_INTERVAL_SEC // 60} phút...")
        time.sleep(LOOP_INTERVAL_SEC)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""ABMedia Morning Tool — 產生早報市場速報的 prompt 包

每天早上跑一次，抓市場數據 + 鉅亨網〈美股盤後〉+ 額外 URL（Bloomberg / CNBC 等），
組成一份 prompt 文字，寫成檔案 + 自動複製到剪貼簿，直接貼到網頁版 LLM 寫稿。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import date
from pathlib import Path


# === 跨平台剪貼簿 ===
def copy_to_clipboard(text: str) -> bool:
    try:
        if sys.platform == "darwin":
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
        elif sys.platform == "win32":
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", "$input | Set-Clipboard"],
                input=text, text=True, encoding="utf-8", check=True,
            )
        else:
            return False
        return True
    except Exception:
        return False


def read_clipboard() -> str:
    try:
        if sys.platform == "darwin":
            return subprocess.run(["pbpaste"], capture_output=True, text=True, check=True).stdout
        elif sys.platform == "win32":
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
                capture_output=True, text=True, encoding="utf-8", check=True,
            )
            return r.stdout
    except Exception:
        pass
    return ""

import requests
from bs4 import BeautifulSoup

SCRIPT_DIR = Path(__file__).parent
REFERENCES_DIR = SCRIPT_DIR / "references"
ASSETS_DIR = SCRIPT_DIR / "assets"
OUTPUT_ROOT = Path.home() / "Desktop" / "abmedia-posts"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}

STOCK_SYMBOLS = [
    ("S&P 500", "^GSPC"),
    ("Nasdaq", "^IXIC"),
    ("DOW", "^DJI"),
]

CRYPTO_COINS = [
    ("BTC", "bitcoin"),
    ("ETH", "ethereum"),
]


# === 市場數據 ===
def _yf_fetch_one(sym: str) -> tuple:
    """用 yfinance 抓最新收盤 vs 前一交易日收盤。
    yfinance 處理 Yahoo cookie/crumb 認證，繞過 query1/v8 端點的匿名 rate limit。
    """
    try:
        import yfinance as yf
        hist = yf.Ticker(sym).history(period="5d")
        if len(hist) < 2:
            return None, None, f"只拿到 {len(hist)} 筆歷史"
        price = float(hist["Close"].iloc[-1])
        prev = float(hist["Close"].iloc[-2])
        change = (price - prev) / prev * 100 if prev else 0.0
        return price, change, None
    except Exception as e:
        return None, None, str(e)


def fetch_stocks() -> list[tuple]:
    """從 Yahoo Finance 抓股指最新收盤 + 日漲跌"""
    rows = []
    for name, sym in STOCK_SYMBOLS:
        price, change, err = _yf_fetch_one(sym)
        if err:
            print(f"  ✗ {name} 抓取失敗：{err}")
        rows.append((name, price, change))
    return rows


def fetch_crypto() -> list[tuple]:
    """從 CoinGecko 抓 BTC/ETH 即時 + 真正 24h 滾動漲跌"""
    rows = []
    ids = ",".join(c[1] for c in CRYPTO_COINS)
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd&include_24hr_change=true"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        for name, coin_id in CRYPTO_COINS:
            d = data.get(coin_id, {})
            rows.append((name, d.get("usd"), d.get("usd_24h_change")))
    except Exception as e:
        print(f"  ✗ 加密貨幣抓取失敗：{e}")
        for name, _ in CRYPTO_COINS:
            rows.append((name, None, None))
    return rows


def format_price(price: float) -> str:
    if price >= 1000:
        return f"{price:,.2f}"
    if price >= 100:
        return f"{price:,.2f}"
    return f"{price:,.4f}"


def format_market_table(stocks: list[tuple], crypto: list[tuple]) -> str:
    lines = ["| 指數 / 幣種 | 價格 | 24小時漲跌 |", "|------|------|----------|"]
    for name, price, change in stocks + crypto:
        if price is None:
            lines.append(f"| {name} | （抓取失敗） | - |")
            continue
        sign = "+" if change >= 0 else ""
        lines.append(f"| {name} | {format_price(price)} | {sign}{change:.2f}% |")
    return "\n".join(lines)


# === 市場圖表 PNG ===
def make_market_chart(date_str: str, stocks: list[tuple], crypto: list[tuple], out_path: Path) -> Path | None:
    """產出像範例圖那樣的 5x3 行情統計 PNG（米色底 + 深褐 header + 紅綠百分比）"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("  ✗ Pillow 沒裝，跳過產圖（pip install Pillow）")
        return None
    from datetime import datetime, timedelta, timezone

    rows = stocks + crypto
    n_rows = len(rows)
    table_top = 160
    row_h = 60
    bottom_padding = 30
    W = 1280
    H = table_top + row_h * (1 + n_rows) + bottom_padding

    BG = (255, 255, 255)          # 白底
    HEADER_BG = (122, 102, 81)    # 深褐 #7A6651
    HEADER_FG = (255, 255, 255)
    TEXT = (44, 32, 24)           # 深咖啡 #2C2018
    RED = (192, 57, 43)
    GREEN = (30, 127, 74)
    GRID = (180, 165, 140)        # 中褐分隔線（搭白底）

    font_path = str(ASSETS_DIR / "NotoSansTC-Medium.ttf")
    f_title = ImageFont.truetype(font_path, 56)
    f_sub = ImageFont.truetype(font_path, 22)
    f_head = ImageFont.truetype(font_path, 30)
    f_cell = ImageFont.truetype(font_path, 34)

    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    dt = datetime.strptime(date_str, "%Y-%m-%d")
    month, day = dt.month, dt.day
    now = datetime.now(timezone(timedelta(hours=8)))

    # === Logo 右上（市場圖表專用 logo） ===
    logo_path = ASSETS_DIR / "ChartLogo.png"
    if logo_path.exists():
        logo = Image.open(logo_path).convert("RGBA")
        logo_w = 220
        logo_h = int(logo.height * logo_w / logo.width)
        logo = logo.resize((logo_w, logo_h), Image.LANCZOS)
        img.paste(logo, (W - logo_w - 30, 25), logo)

    # === 標題：N月N日行情統計（置中，留 logo 空間） ===
    title = f"{month}月{day}日行情統計"
    tw = draw.textlength(title, font=f_title)
    title_x = (W - 280 - tw) // 2
    draw.text((title_x, 30), title, fill=TEXT, font=f_title)

    # === 子標題：資料整理：(M/D HH:MM) ===
    sub = f"資料整理：({month}/{day} {now.strftime('%H:%M')})"
    draw.text((30, 120), sub, fill=TEXT, font=f_sub)

    # === 表格 ===
    col_xs = [30, 480, 870]
    col_ws = [450, 390, 380]
    table_left = 30
    table_right = W - 30
    table_bottom = table_top + row_h * (1 + n_rows)

    # Header
    draw.rectangle([table_left, table_top, table_right, table_top + row_h], fill=HEADER_BG)
    for i, h in enumerate(["指數 / 幣種", "價格", "24小時漲跌"]):
        tw = draw.textlength(h, font=f_head)
        x = col_xs[i] + (col_ws[i] - tw) // 2
        bbox = f_head.getbbox(h)
        y = table_top + (row_h - (bbox[3] - bbox[1])) // 2 - bbox[1]
        draw.text((x, y), h, fill=HEADER_FG, font=f_head)

    # Data rows
    y = table_top + row_h
    for idx, (name, price, change) in enumerate(rows):
        if idx > 0:
            draw.line([(table_left, y), (table_right, y)], fill=GRID, width=1)
        if price is None:
            tw = draw.textlength(name, font=f_cell)
            bbox = f_cell.getbbox(name)
            cy = y + (row_h - (bbox[3] - bbox[1])) // 2 - bbox[1]
            draw.text((col_xs[0] + (col_ws[0] - tw) // 2, cy), name, fill=TEXT, font=f_cell)
            fail = "—"
            for col in (1, 2):
                tw = draw.textlength(fail, font=f_cell)
                draw.text((col_xs[col] + (col_ws[col] - tw) // 2, cy), fail, fill=GRID, font=f_cell)
            y += row_h
            continue

        is_crypto = name in ("BTC", "ETH")
        price_str = f"{price:,.0f}" if is_crypto else f"{price:,.2f}"
        if change > 0:
            color, sign = GREEN, "+"
        elif change < 0:
            color, sign = RED, ""
        else:
            color, sign = TEXT, ""
        change_str = f"{sign}{change:.2f}%"

        bbox = f_cell.getbbox(name)
        cy = y + (row_h - (bbox[3] - bbox[1])) // 2 - bbox[1]

        for i, (text, fill) in enumerate([(name, TEXT), (price_str, TEXT), (change_str, color)]):
            tw = draw.textlength(text, font=f_cell)
            draw.text((col_xs[i] + (col_ws[i] - tw) // 2, cy), text, fill=fill, font=f_cell)
        y += row_h

    # 直欄分隔線（穿過 header + data rows，header 區段在深褐底上不畫，避開讓 header bar 乾淨）
    col_dividers_x = [table_left, 480, 870, table_right]
    for x in col_dividers_x:
        # data rows 區（header 下方到表格底）
        draw.line([(x, table_top + row_h), (x, table_bottom)], fill=GRID, width=1)
    # 表格底邊
    draw.line([(table_left, table_bottom), (table_right, table_bottom)], fill=GRID, width=1)

    img.save(out_path, "PNG", optimize=True)
    return out_path


# === 文章抓取 ===
def _parse_article(html: str, max_chars: int) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        title = og_title["content"].strip()
    elif soup.title and soup.title.string:
        title = soup.title.string.strip()
    else:
        title = ""
    title = re.sub(r"\s*[|｜].*$", "", title).strip()

    parts = []
    article_el = soup.find("article") or soup.body or soup
    for p in article_el.find_all("p"):
        t = p.get_text(strip=True)
        if t and len(t) > 20:
            parts.append(t)
            if sum(len(x) for x in parts) > max_chars:
                break
    text = "\n\n".join(parts)[:max_chars]
    return title, text


def _bloomberg_amp_variant(url: str) -> str | None:
    """Bloomberg 文章原 URL 加 /amp 後綴有時可繞 403"""
    if "bloomberg.com/news/articles/" not in url:
        return None
    base = url.split("?")[0].rstrip("/")
    if base.endswith("/amp"):
        return None
    return base + "/amp"


def fetch_article(url: str, max_chars: int = 4000, retries: int = 3) -> dict:
    """抓文章標題 + 內文。內文太短會自動重試（防 SPA shell / bot detection）

    Bloomberg URL 若 403 會自動 fallback 試 /amp 變體
    """
    import time
    last_err = None
    title = ""
    urls_to_try = [url]
    amp = _bloomberg_amp_variant(url)
    if amp:
        urls_to_try.append(amp)

    for try_url in urls_to_try:
        for attempt in range(retries):
            try:
                r = requests.get(try_url, headers=HEADERS, timeout=15)
                r.raise_for_status()
                t, text = _parse_article(r.text, max_chars)
                if t:
                    title = t
                if len(text) >= 300:
                    return {"url": url, "title": title, "text": text, "error": None}
                last_err = f"內文太短 ({len(text)} 字)"
            except Exception as e:
                last_err = str(e)
            if attempt < retries - 1:
                time.sleep(1.0 * (attempt + 1))
    return {"url": url, "title": title, "text": "", "error": last_err}


def detect_source(url: str) -> str:
    if "bloomberg.com" in url:
        return "Bloomberg Market Wrap"
    if "cnbc.com" in url:
        return "CNBC Daily Open"
    if "cnyes.com" in url:
        return "鉅亨網〈美股盤後〉"
    return "補充來源"


def paste_from_clipboard_fallback(url: str, label: str) -> dict | None:
    """互動式 fallback：讀剪貼簿內容當文章內文（適用反爬嚴重的來源如 Bloomberg）"""
    if not sys.stdin.isatty():
        return None
    copy_key = "Cmd+A 全選 → Cmd+C" if sys.platform == "darwin" else "Ctrl+A 全選 → Ctrl+C"
    print(f"\n  → {label} 抓取失敗。請從瀏覽器複製文章內文（{copy_key}）")
    ans = input(f"     複製好後按 Enter 使用剪貼簿內容 / 輸入 s 跳過此來源：").strip().lower()
    if ans == "s":
        return None
    clip = read_clipboard().strip()
    if not clip:
        print(f"     ✗ 讀剪貼簿失敗，跳過")
        return None
    if len(clip) < 300:
        print(f"     ✗ 剪貼簿內容太短 ({len(clip)} 字)，跳過")
        return None
    # 抽簡單標題：第一行或前 80 字
    first_line = clip.split("\n", 1)[0].strip()
    title = first_line[:120] if len(first_line) > 10 else f"{label} (paste)"
    text = clip[:4000]
    print(f"     ✓ 使用剪貼簿內容 ({len(text)} 字)")
    return {"url": url, "title": title, "text": text, "error": None}


# === 鉅亨網 auto-discover ===
import html as _html


def _strip_html(text: str, max_chars: int) -> str:
    """把 API 回傳的 HTML 內文轉成純文字段落"""
    text = _html.unescape(text)
    soup = BeautifulSoup(text, "html.parser")
    parts = []
    for p in soup.find_all("p"):
        t = p.get_text(strip=True)
        if t and len(t) > 20:
            parts.append(t)
            if sum(len(x) for x in parts) > max_chars:
                break
    if not parts:
        plain = soup.get_text(separator="\n\n", strip=True)
        return plain[:max_chars]
    return "\n\n".join(parts)[:max_chars]


def fetch_cnyes_market_close(max_chars: int = 4000, max_pages: int = 6, max_age_hours: int = 24) -> dict | None:
    """從鉅亨網 API 直接拿〈美股盤後〉文章。

    分頁直到找到一篇 24h 內、標題前綴為〈美股盤後〉的文章。
    鉅亨網美股雷達 volume 高，早上 05:41 發的盤後到下午常被擠到 page 3+。
    """
    import time
    cutoff_ts = time.time() - max_age_hours * 3600
    try:
        for page in range(1, max_pages + 1):
            url = f"https://api.cnyes.com/media/api/v1/newslist/category/wd_stock?limit=30&page={page}"
            r = requests.get(url, headers=HEADERS, timeout=10)
            r.raise_for_status()
            items = r.json().get("items", {}).get("data", [])
            if not items:
                break
            for it in items:
                title = it.get("title", "")
                if not title.startswith("〈美股盤後〉"):
                    continue
                if it.get("publishAt", 0) < cutoff_ts:
                    return None  # 已翻到 24h 前，不再往後找
                news_id = it.get("newsId")
                content_html = it.get("content", "")
                return {
                    "url": f"https://news.cnyes.com/news/id/{news_id}",
                    "title": title,
                    "text": _strip_html(content_html, max_chars),
                    "error": None,
                }
            # 整頁最舊一筆已超過 24h → 提早結束
            if items[-1].get("publishAt", 0) < cutoff_ts:
                break
    except Exception as e:
        print(f"  ✗ 鉅亨網 API 失敗：{e}")
    return None


# === Prompt 組裝 ===
def load_samples() -> list[str]:
    return [p.read_text(encoding="utf-8") for p in sorted(REFERENCES_DIR.glob("sample_*.txt"))]


PROMPT_HEADER = """你是 ABMedia 鏈新聞編輯，撰寫 {date} 的早報市場速報。讀者是台灣加密與美股投資人，文章定位是「昨晚國際 / 美股 / 加密發生什麼大事」的速報。

# 文章結構（固定）
- 標題：30-36 字，三段式「[宏觀事件]，美股[漲跌動態]，[BTC/ETH 或加密相關動態]」
- 開頭 1 段（2-3 句新聞導語）：點出 3 個核心議題（宏觀 / 美股 / 加密）
- H2-1：宏觀／地緣／經濟數據（H2 具體不抽象，依當天材料命名 — 例：油價、利率、CPI、Fed 言論等）
- H2-2：加密市場（H2 具體不抽象）
- 長度：800-900 字
- 不寫風險免責聲明（WordPress 主題會自動加，手寫的一律刪）

# 加密段範圍限定（嚴格）
H2-2 加密市場段：
- 主軸：BTC 和 ETH 的價格水位、24h 漲跌、主因（受美股拖累？特定事件？）
- 可寫：加密貨幣總市值變化、加密相關個股（Strategy / BitMine / Circle / Coinbase 等持倉公司或概念股）
- 禁寫：BTC / ETH 以外的任何單一幣種（SOL、XRP、DOGE、ADA、AVAX、SUI 等一律不要寫）

# 忠實原則（最高優先，違反就視為失敗）
本文所有「數字／日期／百分比／金額／人名／職稱／機構名／產品名／競爭對手」必須在下方「來源材料」中明確出現過。摘錄沒有的事實，一律不要寫。寧可少一個錨點、文章變短，也絕對禁止：
- 補全人名、職稱、機構（摘錄寫 "Powell" 就寫 Powell，不擴成「聯準會主席 Jerome Powell」）
- 推測或補造日期、百分比、金額、版本號
- 補造市場反應、股價走勢、業界譁然等延伸事件
- 引入摘錄沒提到的對手公司、競品、相關事件
- 補造法案、條例、政策名（GENIUS Act、MiCA 等）若摘錄沒提
- 補造名人引述（Jack Dorsey、Sam Altman 等）若摘錄沒提

# 今日市場數據（用於開頭導語與行情段，務必使用這份數據）
{market_table}

# 來源材料（撰文事實只能用這裡的內容）

{sources}

# 寫作規則（ABMedia 統一規範，硬規則）
1. 不用 bold — 禁止 `**` 與 `<strong>`
2. 單破折號 `—`，禁雙破折號 `——`
3. 段落短，2-4 句一段
4. 避 AI tell 詞：「值得注意的是」「不容忽視」「引發關注」「對讀者較具連結的觀察點」一律不要寫
5. bullet 不用於敘事 — 順序事件用 prose；bullet 只用於真正並列且非順序的清單
6. 不寫風險免責聲明（不要在文末寫「本文不構成投資建議」等）
7. 不自我指涉（不要寫「本文」「對 abmedia 讀者」這類後台敘述）
8. 中英 / 中數字之間有空格（例：「升 5%」、「Circle 公布」）
9. 譯名：以太坊（不用「乙太坊」）；荷姆茲海峽（不用「霍爾木茲」）；川普（Trump）、馬斯克（Musk）；無台灣主流譯名 → 保留英文
10. 時間表達：美國時間需註明「美東 X 時」；美股收盤對應台灣時間 04:00 / 05:00
11. H2 子標題具體不抽象 — 每個 H2 帶具體事實／引言／數字，避免「事件背景」「市場反應」這類抽象標籤
12. 中性記者體 — 不用驚嘆號、不廣告化
13. 引述用「表示」「報導」「指出」等客觀動詞，避免主觀判斷

# 風格範例（few-shot — 嚴格模仿這個寫作模式與長度）

{samples}

# 輸出格式
- 純文字、不要 markdown code fence
- 第一行直接寫標題（不加井號）
- 內文用 `## 子標題` 表示 H2
- 段落間空一行
- 只輸出文章本身，不要前後說明、不要解釋
"""


def build_prompt(today: str, market_table: str, source_articles: list[dict], samples: list[str]) -> str:
    src_blocks = []
    for i, art in enumerate(source_articles, 1):
        label = detect_source(art["url"])
        if art.get("error"):
            src_blocks.append(
                f"## 來源 {i}：{label}\nURL：{art['url']}\n（抓取失敗：{art['error']} — 請忽略此來源）"
            )
            continue
        src_blocks.append(
            f"## 來源 {i}：{label}\n"
            f"標題：{art['title']}\n"
            f"URL：{art['url']}\n\n"
            f"{art['text']}"
        )
    sources_str = "\n\n".join(src_blocks) if src_blocks else "（無來源材料）"

    sample_blocks = [f"## 範例 {i}\n\n{s}" for i, s in enumerate(samples, 1)]
    samples_str = "\n\n---\n\n".join(sample_blocks)

    return PROMPT_HEADER.format(
        date=today,
        market_table=market_table,
        sources=sources_str,
        samples=samples_str,
    )


# === 主流程 ===
def main():
    parser = argparse.ArgumentParser(
        description="ABMedia 早報 — 抓市場數據 + 來源新聞，產出 prompt 給網頁版 LLM",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""範例：
  python3 morning.py                                        # 只跑鉅亨網 + 市場數據
  python3 morning.py https://bloomberg.com/news/...         # 加 Bloomberg
  python3 morning.py https://bloomberg.com/... https://cnbc.com/...   # 加多個來源
  python3 morning.py --no-cnyes https://bloomberg.com/...   # 不要抓鉅亨網""",
    )
    parser.add_argument("urls", nargs="*", help="補充新聞 URL（Bloomberg / CNBC / 其他）")
    parser.add_argument("--date", default=date.today().isoformat(), help="日期，預設今天 (YYYY-MM-DD)")
    parser.add_argument("--no-cnyes", action="store_true", help="不自動抓鉅亨網〈美股盤後〉")
    args = parser.parse_args()

    print(f"=== ABMedia 早報 prompt 產生器 — {args.date} ===\n")

    # 0. 互動詢問 URL（沒從 CLI 帶入時）
    if not args.urls and sys.stdin.isatty():
        print("請貼上今日的補充來源 URL，一行一個：")
        print("  例：Bloomberg market wrap")
        print("  例：CNBC Daily Open")
        print()
        print("全部貼完按 Enter（輸入空行）結束；今天沒 URL 直接按 Enter 跳過。")
        print()
        while True:
            try:
                line = input().strip()
            except EOFError:
                break
            if not line:
                break
            args.urls.append(line)
        print()

    # 1. 市場數據
    print("[1/4] 抓市場數據")
    stocks = fetch_stocks()
    crypto = fetch_crypto()
    for name, price, change in stocks + crypto:
        if price is not None:
            print(f"  ✓ {name}: {format_price(price)} ({change:+.2f}%)")
    market_table = format_market_table(stocks, crypto)

    # 2. 鉅亨網 + 抓各篇文章
    print("\n[2/4] 來源蒐集")
    source_articles = []
    if not args.no_cnyes:
        cnyes = fetch_cnyes_market_close()
        if cnyes:
            print(f"  ✓ 鉅亨網〈美股盤後〉：{cnyes['title'][:50]} ({len(cnyes['text'])} 字)")
            source_articles.append(cnyes)
        else:
            print("  ✗ 鉅亨網沒找到今日〈美股盤後〉(可能尚未發布)")

    print(f"\n[3/4] 抓 {len(args.urls)} 篇額外來源文章")
    for url in args.urls:
        label = detect_source(url)
        art = fetch_article(url)
        if art.get("error"):
            print(f"  ✗ {label} ({url}) → {art['error']}")
            paste = paste_from_clipboard_fallback(url, label)
            if paste:
                art = paste
        else:
            print(f"  ✓ {label}：{art['title'][:50]} ({len(art['text'])} 字)")
        source_articles.append(art)

    # 4. 組 prompt
    print("\n[4/4] 組 prompt")
    samples = load_samples()
    if not samples:
        print(f"  ⚠ 找不到範例文章在 {REFERENCES_DIR}/sample_*.txt")
    prompt = build_prompt(args.date, market_table, source_articles, samples)

    # 5. 輸出
    output_dir = OUTPUT_ROOT / args.date
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "morning-prompt.txt"
    output_path.write_text(prompt, encoding="utf-8")

    chart_path = output_dir / "market-chart.png"
    chart_result = make_market_chart(args.date, stocks, crypto, chart_path)

    clipboard_ok = copy_to_clipboard(prompt)

    print()
    print(f"  ✓ 寫入 prompt：{output_path}")
    print(f"  ✓ Prompt 字數：{len(prompt):,}")
    if chart_result:
        print(f"  ✓ 寫入市場圖表：{chart_path}")
    if clipboard_ok:
        print("  ✓ 已複製到剪貼簿 — 貼到 ChatGPT / Claude / Gemini 網頁版即可")
    else:
        print("  ✗ 複製剪貼簿失敗，請手動開啟檔案複製")
    print("\n完成。")


if __name__ == "__main__":
    main()

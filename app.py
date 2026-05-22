"""ABMedia 早報工具 — Streamlit Web UI

跑法：streamlit run app.py
"""
from __future__ import annotations

import io
from datetime import date
from pathlib import Path

import streamlit as st

from morning import (
    build_prompt,
    detect_source,
    fetch_article,
    fetch_cnyes_market_close,
    fetch_crypto,
    fetch_stocks,
    format_market_table,
    format_price,
    load_samples,
    make_market_chart,
)

st.set_page_config(page_title="ABMedia 早報工具", page_icon=":sunrise:", layout="centered")


# === 密碼 gate ===
def check_password() -> bool:
    if st.session_state.get("authed"):
        return True
    st.title(":lock: ABMedia 早報工具")
    pwd = st.text_input("密碼", type="password")
    if pwd:
        try:
            correct = st.secrets["password"]
        except Exception:
            st.error("伺服器尚未設定密碼（Streamlit Cloud Secrets 缺 password 欄）")
            return False
        if pwd == correct:
            st.session_state["authed"] = True
            st.rerun()
        else:
            st.error("密碼錯誤")
    return False


if not check_password():
    st.stop()


st.title(":sunrise: ABMedia 早報工具")
st.caption("抓市場數據 + 鉅亨網〈美股盤後〉+ 補充新聞 → 產 prompt 給網頁版 LLM")

if "supp_keys" not in st.session_state:
    st.session_state["supp_keys"] = [0]
    st.session_state["supp_next_id"] = 1

# === 輸入區 ===
with st.form("inputs"):
    target_date = st.date_input("日期", value=date.today())

    url_text = st.text_area(
        "補充來源 URL（一行一個，可留空）",
        height=120,
        placeholder="https://www.cnbc.com/...\nhttps://www.coindesk.com/...",
    )

    editor_notes = st.text_area(
        "編輯指示（最高指導原則 — 文章方向、重點、要突顯的角度）",
        height=120,
        placeholder="例：今天主軸放 Fed 利率決議；加密段帶到 Strategy 增持；標題突顯川普關稅",
        key="editor_notes",
    )

    editor_materials = st.text_area(
        "編輯素材（補來源沒有的事實 — 與來源同位階作為撰文事實依據）",
        height=120,
        placeholder="例：\n- Strategy 又加碼 2,500 BTC，平均成本 9.8 萬美元\n- ETH ETF 昨日淨流入 4,000 萬美元\n- BTC 在 105,200 美元橫盤、無重大催化",
        key="editor_materials",
    )

    header_cols = st.columns([3, 1])
    with header_cols[0]:
        st.markdown(f"**補充來源內文 paste**（共 {len(st.session_state['supp_keys'])} 個）")
    with header_cols[1]:
        add_source = st.form_submit_button("➕ 新增來源", use_container_width=True)

    delete_buttons: dict[int, bool] = {}
    for idx, sid in enumerate(st.session_state["supp_keys"]):
        if idx > 0:
            st.markdown("---")
        title_cols = st.columns([5, 1])
        with title_cols[0]:
            st.markdown(f"**來源 #{idx + 1}**")
        with title_cols[1]:
            delete_buttons[sid] = st.form_submit_button(
                "🗑️ 刪除",
                key=f"del_{sid}",
                use_container_width=True,
            )
        cols = st.columns([1, 3])
        with cols[0]:
            st.text_input(
                "名稱",
                key=f"supp_name_{sid}",
                placeholder="例：CNBC、Decrypt",
            )
        with cols[1]:
            st.text_area(
                "內文",
                key=f"supp_text_{sid}",
                height=120,
                placeholder="貼上文章全文",
            )

    no_cnyes = st.checkbox("跳過鉅亨網〈美股盤後〉自動抓取", value=False)
    submitted = st.form_submit_button(":zap: 產生 Prompt", type="primary")

if add_source:
    new_id = st.session_state["supp_next_id"]
    st.session_state["supp_keys"].append(new_id)
    st.session_state["supp_next_id"] += 1
    st.rerun()

for _sid, _clicked in list(delete_buttons.items()):
    if _clicked:
        if len(st.session_state["supp_keys"]) > 1:
            st.session_state["supp_keys"].remove(_sid)
            st.session_state.pop(f"supp_name_{_sid}", None)
            st.session_state.pop(f"supp_text_{_sid}", None)
            st.rerun()
        break


def _paste_to_article(text: str, label: str, fallback_url: str = "") -> dict | None:
    text = text.strip()
    if len(text) < 300:
        return None
    first_line = text.split("\n", 1)[0].strip()
    title = first_line[:120] if len(first_line) > 10 else f"{label} (paste)"
    return {"url": fallback_url, "title": title, "text": text[:4000], "error": None}


if submitted:
    date_str = target_date.isoformat()
    urls = [u.strip() for u in url_text.splitlines() if u.strip()]

    status = st.status("產生中...", expanded=True)
    with status:
        # 1. 市場數據
        st.write("**[1/4] 抓市場數據**")
        stocks = fetch_stocks()
        crypto = fetch_crypto()
        for name, price, change in stocks + crypto:
            if price is not None:
                st.write(f"- {name}: {format_price(price)} ({change:+.2f}%)")
            else:
                st.write(f"- {name}: :x: 抓取失敗")
        market_table = format_market_table(stocks, crypto)

        # 2. 鉅亨網
        st.write("**[2/4] 來源蒐集**")
        source_articles: list[dict] = []
        if not no_cnyes:
            cnyes = fetch_cnyes_market_close()
            if cnyes:
                st.write(f"- :white_check_mark: 鉅亨網〈美股盤後〉：{cnyes['title'][:50]} ({len(cnyes['text'])} 字)")
                source_articles.append(cnyes)
            else:
                st.write("- :warning: 鉅亨網沒找到今日〈美股盤後〉")

        # 3. 額外 URL
        st.write(f"**[3/4] 抓 {len(urls)} 篇額外來源文章**")
        for url in urls:
            label = detect_source(url)
            art = fetch_article(url)
            if art.get("error"):
                st.write(f"- :x: {label}：{art['error']}（跳過；可改用下方「補充來源 paste」貼內文）")
                continue
            st.write(f"- :white_check_mark: {label}：{art['title'][:50]} ({len(art['text'])} 字)")
            source_articles.append(art)

        # 動態補充 paste 清單
        for idx, sid in enumerate(st.session_state["supp_keys"], 1):
            name = (st.session_state.get(f"supp_name_{sid}") or "").strip()
            text = (st.session_state.get(f"supp_text_{sid}") or "").strip()
            if not text:
                continue
            label = name or f"補充來源 #{idx}"
            pasted = _paste_to_article(text, label)
            if not pasted:
                st.write(f"- :warning: {label}：內文太短 ({len(text)} 字 < 300)，跳過")
                continue
            pasted["title"] = name or pasted["title"]
            pasted["_label"] = label
            st.write(f"- :white_check_mark: {label}：貼上的內文 ({len(pasted['text'])} 字)")
            source_articles.append(pasted)

        # 4. 組 prompt + 產圖
        st.write("**[4/4] 組 prompt + 產圖**")
        samples = load_samples()
        prompt = build_prompt(
            date_str,
            market_table,
            source_articles,
            samples,
            editor_notes,
            editor_materials,
        )
        st.write(f"- :white_check_mark: Prompt 字數：{len(prompt):,}")

        chart_path = Path("/tmp") / f"market-chart-{date_str}.png"
        chart_result = make_market_chart(date_str, stocks, crypto, chart_path)
        if chart_result:
            st.write(f"- :white_check_mark: 市場圖表已產出")

    status.update(label=":tada: 完成", state="complete", expanded=False)

    # === 結果區 ===
    st.success("完成！下面複製 prompt 貼到網頁版 LLM 即可。")

    st.subheader(":clipboard: Prompt（右上角有複製按鈕）")
    st.code(prompt, language=None)

    col_a, col_b = st.columns(2)
    with col_a:
        st.download_button(
            "下載 morning-prompt.txt",
            data=prompt.encode("utf-8"),
            file_name=f"morning-prompt-{date_str}.txt",
            mime="text/plain",
        )
    with col_b:
        if chart_result and chart_path.exists():
            st.download_button(
                "下載 market-chart.png",
                data=chart_path.read_bytes(),
                file_name=f"market-chart-{date_str}.png",
                mime="image/png",
            )

    if chart_result and chart_path.exists():
        st.subheader(":bar_chart: 市場圖表預覽")
        st.image(str(chart_path))

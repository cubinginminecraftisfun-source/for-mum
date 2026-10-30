"""장보기 알림메모 — QR로 다시 여는 모바일 장보기 목록.

실행 (Python 3.11 권장)
    pip install -r requirements.txt
    streamlit run app.py

배포
    GitHub 저장소의 같은 폴더에 app.py, requirements.txt 업로드.
    Streamlit Community Cloud에서 저장소를 연결하고 app.py를 실행 파일로 지정.
    앱의 [더보기 → QR / 링크]에서 실제 배포 주소를 입력하고 QR 다운로드.
    QR은 카메라로 읽으면 이 목록을 여는 링크이며, 상품 바코드 스캐너가 아님.

선택 설정 — Streamlit Cloud의 Settings → Secrets에 입력 (GitHub에 올리지 않기)
    APP_URL = "https://your-app.streamlit.app"
    DATABASE_URL = "postgresql://USER:PASSWORD@HOST/DB?sslmode=require"

데이터 저장
    DATABASE_URL이 없으면 app.py 옆 SQLite 파일에 저장. 새로고침에는 유지되지만
    Community Cloud 재배포/컨테이너 교체 시 소실될 수 있음. 장기 보관에는 외부
    PostgreSQL(Neon/Supabase 등)을 연결. 테이블은 자동 생성하며 CREATE 권한 필요.
    외부 DB 연결이 실패하면 로컬 DB로 몰래 전환하지 않고 중지.
    구매 기록을 CSV로 내려받을 수 있음. CSV는 백업 열람용이며 가져오기는 미지원.

주의
    링크의 무작위 key가 목록 접근 권한. 링크/QR을 아는 사람은 조회·수정 가능.
    민감정보를 기록하지 않기. 로그인/사용자별 권한 인증은 별도 구현 필요.
    두 기기 동시 접속 시 수동 [새로고침]으로 동기화. 실시간 푸시는 아님.
    실제 할인/무게/묶음 계산은 입력한 단가 × 수량 기준. 소수 수량 지원,
    합계는 원 단위 반올림. '예상 수량'은 안내일 뿐 구매 수량은 매번 확인.
    메인 목록은 기본 2개씩 표시. 작은 화면은 설정에서 1개로 변경 가능.
    키보드·확대 글꼴·매우 작은 화면 및 팝업에서는 스크롤이 생길 수 있음.
"""
from __future__ import annotations

import csv
import hashlib
import html
import io
import math
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit

import qrcode
import streamlit as st

CATEGORIES = ["🥬 야채", "🍎 과일", "🥩 정육·계란", "🐟 수산", "🥛 유제품",
              "🍞 빵·간식", "🍚 쌀·식재료", "🧊 냉동·간편식", "🧴 생활용품", "🛍️ 기타"]
PRIORITIES = {1: "1 · 꼭 필요", 2: "2 · 가격이 맞으면", 3: "3 · 비싸면 패스"}
KST = timezone(timedelta(hours=9))
MAX_PRICE = 100_000_000
MAX_QTY = 10_000.0


def won(value):
    return f"{int(value):,}원"


def cost(price, quantity):
    """Float 누적 오차 없이 품목 단위 금액을 원 단위 사사오입."""
    return int((Decimal(str(price)) * Decimal(str(quantity))).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP))


def clean_text(value, limit, required=False):
    value = str(value).strip()
    if required and not value:
        raise ValueError("상품명을 입력해 주세요.")
    if len(value) > limit:
        raise ValueError(f"입력은 {limit}자 이내로 작성해 주세요.")
    return value


def validate_item(category, name, priority, target, planned_qty, unit, memo):
    if category not in CATEGORIES or priority not in PRIORITIES:
        raise ValueError("카테고리와 중요도를 확인해 주세요.")
    if not 0 <= target <= MAX_PRICE or not 0 < planned_qty <= MAX_QTY:
        raise ValueError("가격 또는 수량의 범위를 확인해 주세요.")
    return (category, clean_text(name, 40, True), int(priority), int(target),
            float(planned_qty), clean_text(unit, 8, True), clean_text(memo, 80))


class Store:
    """한 번의 쓰기는 단일 SQL로 원자적 처리. 목록 ID와 상태를 항상 함께 검증."""
    def __init__(self, database_url="", sqlite_path=None):
        self.url = database_url
        self.sqlite_path = str(sqlite_path or Path(__file__).with_name("shopping.sqlite3"))

    @contextmanager
    def connect(self):
        if self.url:
            import psycopg
            from psycopg.rows import dict_row
            conn = psycopg.connect(self.url, row_factory=dict_row, connect_timeout=10)
        else:
            conn = sqlite3.connect(self.sqlite_path, timeout=15)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def execute(self, sql, values=(), fetch=False):
        with self.connect() as conn:
            cur = conn.execute(sql.replace("?", "%s") if self.url else sql, values)
            return [dict(r) for r in cur.fetchall()] if fetch else cur.rowcount

    def initialize(self):
        self.execute("""CREATE TABLE IF NOT EXISTS shopping_lists (
            id TEXT PRIMARY KEY, memo TEXT NOT NULL DEFAULT '',
            budget BIGINT NOT NULL DEFAULT 0, page_size INTEGER NOT NULL DEFAULT 2,
            created DOUBLE PRECISION NOT NULL)""")
        self.execute("""CREATE TABLE IF NOT EXISTS shopping_items (
            id TEXT PRIMARY KEY, list_id TEXT NOT NULL REFERENCES shopping_lists(id),
            category TEXT NOT NULL, name TEXT NOT NULL, priority INTEGER NOT NULL,
            target BIGINT NOT NULL DEFAULT 0, planned_qty DOUBLE PRECISION NOT NULL,
            unit TEXT NOT NULL, memo TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'planned', unit_price BIGINT,
            quantity DOUBLE PRECISION, amount BIGINT, created DOUBLE PRECISION NOT NULL,
            purchased DOUBLE PRECISION, batch TEXT,
            CHECK(priority IN (1,2,3)), CHECK(state IN ('planned','cart','bought')),
            CHECK(target >= 0), CHECK(planned_qty > 0))""")
        self.execute("CREATE INDEX IF NOT EXISTS idx_shopping_list_state ON shopping_items(list_id,state)")

    def ensure_list(self, key):
        self.execute("INSERT INTO shopping_lists (id,created) VALUES (?,?) ON CONFLICT(id) DO NOTHING",
                     (key, time.time()))

    def settings(self, key):
        return self.execute("SELECT * FROM shopping_lists WHERE id=?", (key,), True)[0]

    def save_settings(self, key, memo, budget, page_size):
        if not 0 <= budget <= 1_000_000_000 or page_size not in (1, 2):
            raise ValueError("설정값을 확인해 주세요.")
        self.execute("UPDATE shopping_lists SET memo=?,budget=?,page_size=? WHERE id=?",
                     (clean_text(memo, 80), int(budget), int(page_size), key))

    def items(self, key, state):
        return self.execute("""SELECT * FROM shopping_items WHERE list_id=? AND state=?
            ORDER BY priority ASC, created ASC, id ASC""", (key, state), True)

    def item(self, key, item_id):
        rows = self.execute("SELECT * FROM shopping_items WHERE list_id=? AND id=?",
                            (key, item_id), True)
        return rows[0] if rows else None

    def add(self, key, *fields):
        values = validate_item(*fields)
        item_id = uuid.uuid4().hex
        self.execute("""INSERT INTO shopping_items
            (id,list_id,category,name,priority,target,planned_qty,unit,memo,created)
            VALUES (?,?,?,?,?,?,?,?,?,?)""", (item_id, key, *values, time.time()))
        return item_id

    def edit(self, key, item_id, *fields):
        values = validate_item(*fields)
        return self.execute("""UPDATE shopping_items SET category=?,name=?,priority=?,target=?,
            planned_qty=?,unit=?,memo=? WHERE list_id=? AND id=? AND state='planned'""",
                            (*values, key, item_id))

    def put_in_cart(self, key, item_id, price, qty, expected="planned"):
        if expected not in ("planned", "cart"):
            raise ValueError("처리할 수 없는 상태입니다.")
        if not 0 <= price <= MAX_PRICE or not 0 < qty <= MAX_QTY or not math.isfinite(qty):
            raise ValueError("단가는 0원 이상, 수량은 0보다 크게 입력해 주세요.")
        return self.execute("""UPDATE shopping_items SET state='cart',unit_price=?,quantity=?,
            amount=?,purchased=? WHERE list_id=? AND id=? AND state=?""",
            (int(price), float(qty), cost(price, qty), time.time(), key, item_id, expected))

    def restore(self, key, item_id):
        return self.execute("""UPDATE shopping_items SET state='planned',unit_price=NULL,
            quantity=NULL,amount=NULL,purchased=NULL WHERE list_id=? AND id=? AND state='cart'""",
            (key, item_id))

    def remove(self, key, item_id):
        return self.execute("DELETE FROM shopping_items WHERE list_id=? AND id=? AND state='planned'",
                            (key, item_id))

    def finish(self, key):
        return self.execute("UPDATE shopping_items SET state='bought',batch=? WHERE list_id=? AND state='cart'",
                            (uuid.uuid4().hex, key))

    def history(self, key, limit=20):
        return self.execute("""SELECT * FROM shopping_items WHERE list_id=? AND state='bought'
            ORDER BY purchased DESC LIMIT ?""", (key, limit), True)

    def export(self, key):
        rows = self.execute("""SELECT * FROM shopping_items WHERE list_id=? AND state IN ('cart','bought')
            ORDER BY purchased DESC""", (key,), True)
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["상태", "담은 시각(KST)", "카테고리", "상품명", "중요도", "단가(원)",
                         "수량", "단위", "합계(원)", "품목메모", "장보기기록ID"])
        def safe_cell(value):
            text = str(value or "")
            return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text
        for r in rows:
            writer.writerow(["카트" if r["state"] == "cart" else "구매완료",
                datetime.fromtimestamp(r["purchased"], KST).strftime("%Y-%m-%d %H:%M:%S"),
                safe_cell(r["category"]), safe_cell(r["name"]), r["priority"], r["unit_price"],
                r["quantity"], safe_cell(r["unit"]), r["amount"], safe_cell(r["memo"]), r["batch"] or ""])
        return buffer.getvalue().encode("utf-8-sig")


def config(name, fallback=""):
    try:
        return str(st.secrets.get(name, fallback))
    except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
        return fallback


@st.cache_resource
def setup_store(url):
    store = Store(url)
    store.initialize()
    return store


def close_dialog():
    st.session_state.pop("_dialog", None)


def done(message):
    close_dialog()
    st.session_state["notice"] = message
    st.rerun()


def changed(count, message):
    if count:
        done(message)
    st.warning("다른 기기에서 이미 변경된 항목입니다. 창을 닫고 새로고침해 주세요.")


@st.dialog("메모 추가 / 수정", on_dismiss=close_dialog)
def item_dialog(db, key, item_id=None):
    item = db.item(key, item_id) if item_id else None
    if item_id and (not item or item["state"] != "planned"):
        st.warning("이미 카트에 담겼거나 삭제된 항목입니다.")
        return
    v = item or {"category": CATEGORIES[0], "name": "", "priority": 2, "target": 0,
                 "planned_qty": 1.0, "unit": "개", "memo": ""}
    with st.form(f"item_form_{item_id or 'new'}"):
        category = st.selectbox("카테고리", CATEGORIES, index=CATEGORIES.index(v["category"]))
        name = st.text_input("상품명", v["name"], max_chars=40, placeholder="예: 흙당근 1봉")
        a, b = st.columns([1.2, 1])
        priority = a.selectbox("중요도", list(PRIORITIES), index=v["priority"]-1,
                               format_func=lambda p: PRIORITIES[p])
        target = b.number_input("기준 단가 · 원", 0, MAX_PRICE, int(v["target"]), step=100,
                                help="이 가격보다 비싸면 한 번 더 확인해요. 0은 기준 없음.")
        a, b = st.columns([1.2, 1])
        qty = a.number_input("예상 수량", 0.01, MAX_QTY, float(v["planned_qty"]), step=1.0, format="%.2f")
        unit = b.text_input("단위", v["unit"], max_chars=8, placeholder="개 / 봉 / kg")
        memo = st.text_input("품목 메모", v["memo"], max_chars=80, placeholder="예: 2개 묶음 할인 확인")
        save = st.form_submit_button("저장", use_container_width=True, type="primary")
    if save:
        try:
            fields = (category, name, priority, target, qty, unit, memo)
            if item:
                changed(db.edit(key, item_id, *fields), "메모를 수정했어요.")
            else:
                db.add(key, *fields)
                st.session_state["page"] = 0
                st.session_state["reset_filters"] = True
                done("장보기 메모를 추가했어요.")
        except ValueError as e:
            st.error(str(e))
    if item:
        with st.expander("구매하지 않고 메모 삭제"):
            confirm = st.checkbox("이 메모를 삭제합니다. 구매 내역은 남지 않아요.")
            if st.button("삭제 확인", disabled=not confirm, use_container_width=True):
                changed(db.remove(key, item_id), "메모를 삭제했어요.")


@st.dialog("가격 확인하고 담기", on_dismiss=close_dialog)
def buy_dialog(db, key, item_id):
    item = db.item(key, item_id)
    if not item or item["state"] != "planned":
        st.warning("이미 처리된 항목입니다. 메인에서 새로고침해 주세요.")
        return
    st.markdown(f"**{html.escape(item['name'])}** · {PRIORITIES[item['priority']]}")
    st.caption(f"단가는 1{item['unit']} 기준 · 기준가 {won(item['target']) if item['target'] else '미설정'}")
    a, b = st.columns(2)
    # 0은 실제 무료상품 또는 아직 미입력 상태이므로 별도 확인을 받는다.
    price = a.number_input("실제 단가 · 원", 0, MAX_PRICE, 0, step=100, key=f"price_{item_id}")
    qty = b.number_input(f"실제 수량 · {item['unit']}", 0.01, MAX_QTY,
                         float(item["planned_qty"]), step=1.0, format="%.2f", key=f"qty_{item_id}")
    subtotal = cost(price, qty)
    total = sum(r["amount"] for r in db.items(key, "cart"))
    settings = db.settings(key)
    st.info(f"이번 품목 {won(subtotal)}  →  담은 뒤 총 {won(total + subtotal)}")
    over = bool(item["target"] and price > item["target"])
    if over:
        if item["priority"] == 1:
            st.warning("꼭 필요한 품목이지만 기준가 초과예요. 대체 상품도 확인해 보세요.")
        else:
            st.warning("기준가보다 비싸요. 이번에는 안 사도 괜찮아요.")
    elif item["priority"] in (2, 3):
        st.caption("가격이 아쉽다면 ‘이번엔 패스’를 누르세요. 메모는 그대로 남아요.")
    if settings["budget"] and total + subtotal > settings["budget"]:
        st.warning(f"담으면 예산보다 {won(total + subtotal - settings['budget'])} 초과해요.")
    confirmed = True
    if over:
        confirmed = st.checkbox("기준가를 넘지만 이번에는 구매할게요.", key=f"over_{item_id}")
    if price == 0:
        free_ok = st.checkbox("실제 0원인 상품이 맞아요.", key=f"free_{item_id}")
        confirmed = confirmed and free_ok
    a, b = st.columns(2)
    if a.button("카트에 담기", type="primary", disabled=not confirmed, use_container_width=True):
        changed(db.put_in_cart(key, item_id, price, qty), "카트에 담았어요. 금액을 합산했어요.")
    if b.button("이번엔 패스", use_container_width=True):
        done("구매하지 않았어요. 메모는 그대로 남겨뒀어요.")


@st.dialog("현재 카트", on_dismiss=close_dialog)
def cart_dialog(db, key):
    rows = db.items(key, "cart")
    if not rows:
        st.info("아직 담은 물건이 없어요. 목록에서 ‘담기’를 눌러 주세요.")
        return
    st.markdown(f"### {won(sum(r['amount'] for r in rows))}")
    st.caption(f"{len(rows)}개 품목 · 실제 입력한 단가 × 수량의 합계")
    by_id = {r["id"]: r for r in rows}
    selected = st.selectbox("내역 확인 / 수정할 품목", list(by_id),
        format_func=lambda i: f"{by_id[i]['name']} · {by_id[i]['quantity']:g}{by_id[i]['unit']} · {won(by_id[i]['amount'])}")
    r = by_id[selected]
    with st.form(f"cart_edit_{selected}"):
        a, b = st.columns(2)
        price = a.number_input("단가 · 원", 0, MAX_PRICE, int(r["unit_price"]), step=100)
        qty = b.number_input(f"수량 · {r['unit']}", 0.01, MAX_QTY, float(r["quantity"]), step=1.0, format="%.2f")
        if st.form_submit_button("금액 수정", use_container_width=True):
            changed(db.put_in_cart(key, selected, price, qty, expected="cart"), "카트 금액을 수정했어요.")
    if st.button("이 물건 빼기 → 메모로 복원", use_container_width=True):
        changed(db.restore(key, selected), "카트 금액에서 빼고 메모로 돌려놓았어요.")
    with st.expander("장보기 마침"):
        st.caption("현재 카트만 구매 기록으로 보관하고 총액을 0원으로 바꿔요. 미구매 메모는 유지돼요.")
        confirm = st.checkbox("현재 카트의 물건을 모두 구매했어요.")
        if st.button("구매 기록에 보관", type="primary", disabled=not confirm, use_container_width=True):
            changed(db.finish(key), "장보기를 마쳤어요. 구매 기록에 보관했어요.")


def share_url(base, token):
    p = urlsplit(base.strip())
    if p.scheme != "https" or not p.netloc or p.username or p.password or p.fragment:
        raise ValueError("https://로 시작하는 실제 배포 주소를 입력해 주세요.")
    if p.hostname in ("localhost", "127.0.0.1", "0.0.0.0"):
        raise ValueError("휴대폰에서 열 수 있는 외부 배포 주소가 필요해요.")
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode({"key": token}), ""))


@st.dialog("QR / 목록 링크", on_dismiss=close_dialog)
def qr_dialog(token):
    st.caption("이 QR을 휴대폰 기본 카메라로 찍으면 같은 목록이 열려요.")
    base = st.text_input("배포한 앱 주소", value=st.session_state.get("base_url", config("APP_URL")),
                         placeholder="https://내앱이름.streamlit.app", max_chars=500)
    if not base:
        st.info("Streamlit 배포 후 받은 실제 주소를 위에 입력해 주세요.")
        return
    try:
        url = share_url(base, token)
    except ValueError as e:
        st.warning(str(e))
        return
    st.session_state["base_url"] = base
    image = qrcode.make(url)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    st.image(buffer.getvalue(), width=220)
    st.text_input("내 목록 링크 · 복사해서 북마크", value=url)
    st.download_button("QR 이미지 저장", buffer.getvalue(), "shopping-qr.png", "image/png",
                       use_container_width=True)
    st.warning("링크·QR을 아는 사람은 이 목록을 수정할 수 있어요. 공개 게시하지 마세요.")


@st.dialog("메모 / 예산 / 화면", on_dismiss=close_dialog)
def settings_dialog(db, key):
    s = db.settings(key)
    with st.form("settings_form"):
        memo = st.text_input("계속 보이는 장보기 메모", s["memo"], max_chars=80,
                             placeholder="예: 냉장고에 우유 있음 · 장바구니 챙기기")
        budget = st.number_input("이번 장보기 예산 · 원", 0, 1_000_000_000, int(s["budget"]), step=1000,
                                 help="0이면 예산 알림을 끕니다.")
        size = st.radio("한 번에 볼 품목 수", [1, 2], index=s["page_size"]-1, horizontal=True,
                         help="작은 휴대폰이나 큰 글꼴에서는 1개를 권장해요.")
        if st.form_submit_button("설정 저장", type="primary", use_container_width=True):
            db.save_settings(key, memo, budget, size)
            st.session_state["page"] = 0
            done("메모와 예산을 저장했어요.")
    if db.url:
        st.caption("저장: 외부 PostgreSQL · 같은 목록 링크로 다시 열 수 있어요.")
    else:
        st.warning("현재 임시 저장(SQLite) 모드예요. 클라우드 재배포 등으로 기록이 사라질 수 있어요. 장기 보관은 DATABASE_URL을 설정하세요.")
    st.caption("자동 푸시 알림은 없어요. 중요도·가격·예산을 앱 안에서 안내해요.")


@st.dialog("구매 기록 / CSV", on_dismiss=close_dialog)
def history_dialog(db, key):
    rows = db.history(key)
    st.download_button("전체 구매·카트 내역 CSV 저장", db.export(key), "shopping-history.csv",
                       "text/csv", use_container_width=True)
    if not rows:
        st.info("완료한 장보기가 없어요. 카트에서 ‘장보기 마침’으로 기록을 남길 수 있어요.")
        return
    st.caption("최근 구매 20개 · 전체 기록은 CSV로 확인해 주세요.")
    i = st.selectbox("구매 기록 선택", range(len(rows)), format_func=lambda j:
        f"{datetime.fromtimestamp(rows[j]['purchased'], KST):%m/%d} · {rows[j]['name']} · {won(rows[j]['amount'])}")
    r = rows[i]
    st.write(f"단가 {won(r['unit_price'])} × {r['quantity']:g}{r['unit']} = {won(r['amount'])}")
    st.caption(f"{r['category']} / {PRIORITIES[r['priority']]}")
    if st.button("이 상품 다시 메모하기", use_container_width=True):
        db.add(key, r["category"], r["name"], r["priority"], r["target"], r["planned_qty"], r["unit"], r["memo"])
        done("지난 구매 상품을 새 메모로 추가했어요.")


CSS = """
<style>
:root{color-scheme:light;}
.stApp{background:#F5F7F2;color:#20372B;}
header[data-testid="stHeader"]{background:transparent;height:0;}
[data-testid="stToolbar"]{display:none;}
.block-container{max-width:540px;padding:0.7rem 0.8rem 1rem!important;}
[data-testid="stVerticalBlock"]{gap:0.48rem;}
[data-testid="stHorizontalBlock"]{gap:0.45rem;}
[data-testid="stHorizontalBlock"]>[data-testid="stColumn"]{min-width:0!important;}
[data-testid="stVerticalBlockBorderWrapper"]{border-radius:15px!important;background:#FFF;}
[data-testid="stButton"] button,[data-testid="stPopover"] button{
min-height:42px;border-radius:12px;font-size:0.86rem;border-color:#D8E1D8;}
[data-testid="stButton"] button[kind="primary"],button[kind="primaryFormSubmit"]{
background:#256446;border-color:#256446;color:white;}
[data-testid="stSelectbox"] label{display:none;}
[data-testid="stDialog"] [data-testid="stSelectbox"] label{display:flex;}
[data-testid="stDialog"]{color:#20372B;}
.hero{display:flex;justify-content:space-between;align-items:center;margin-bottom:3px;}
.brand{font-size:21px;font-weight:800;letter-spacing:-1px;}
.tag{font-size:10px;color:#526E5C;letter-spacing:1px;}
.summary{background:#E4EFE6;border:1px solid #CEDFCF;border-radius:17px;padding:11px 15px;}
.summary-top{display:flex;align-items:baseline;justify-content:space-between;gap:8px;}
.total{font-size:29px;font-weight:800;letter-spacing:-1px;line-height:1.3;}
.meta{font-size:12px;color:#405A49;}
.budget{font-size:12px;margin-top:2px;color:#405A49;}
.over{color:#9B321C;}
.memo{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:12px;margin-top:5px;color:#3D5948;}
.itemtitle{font-size:16px;font-weight:750;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.itemmeta{font-size:11px;color:#57685C;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin:2px 0 4px;}
.pill{display:inline-block;border-radius:6px;padding:2px 5px;font-size:10px;font-weight:650;margin-right:5px;}
.p1{background:#FCE3DC;color:#8A321F}.p2{background:#FBF0C7;color:#705717}.p3{background:#E8EBE8;color:#45534A}
.pager{text-align:center;font-size:12px;line-height:40px;color:#536958;}
.empty{text-align:center;padding:20px 10px;background:white;border-radius:14px;color:#536958;font-size:14px;}
.foot{font-size:10px;color:#5E7164;text-align:center;}
@media(max-width:640px){
[data-testid="stHorizontalBlock"]{flex-wrap:nowrap!important;}
[data-testid="stHorizontalBlock"]>[data-testid="stColumn"]{flex:1 1 0!important;width:auto!important;}
.block-container{padding-top:0.5rem!important;}
}
@media(max-height:680px){.total{font-size:25px}.summary{padding:8px 12px}.brand{font-size:19px}}
</style>
"""


def main():
    st.set_page_config(page_title="장보기 알림메모", page_icon="🛒", layout="centered",
                       initial_sidebar_state="collapsed")
    st.markdown(CSS, unsafe_allow_html=True)
    token = st.query_params.get("key", "")
    if not token:
        token = secrets.token_urlsafe(32)
        st.query_params["key"] = token
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        st.error("유효하지 않은 목록 링크예요. 원래 QR/북마크를 다시 열어 주세요.")
        st.stop()
    key = hashlib.sha256(token.encode()).hexdigest()
    try:
        db = setup_store(config("DATABASE_URL"))
        db.ensure_list(key)
        settings = db.settings(key)
        planned = db.items(key, "planned")
        cart = db.items(key, "cart")
    except Exception:
        # 연결문자열/자격증명/DB 오류 전문을 화면에 노출하지 않는다.
        st.error("저장소에 연결할 수 없어요. DB 주소·접속 권한 또는 로컬 저장 권한을 확인해 주세요.")
        st.stop()
    if st.session_state.pop("reset_filters", False):
        st.session_state["category_filter"] = "전체"
        st.session_state["priority_filter"] = 0
    if "notice" in st.session_state:
        st.toast(st.session_state.pop("notice"), icon="✅")
    total = sum(r["amount"] for r in cart)
    budget = settings["budget"]
    if budget:
        remain = budget - total
        budget_text = f"예산 {won(budget)} · {'남음 ' + won(remain) if remain >= 0 else '초과 ' + won(-remain)}"
    else:
        budget_text = "예산 미설정 · 더보기에서 설정"
    memo = settings["memo"] or "장바구니를 챙기세요 · 더보기에서 메모 작성"
    st.markdown('<div class="hero"><span class="brand">🛒 장보기 알림메모</span><span class="tag">SHOP SMART</span></div>', unsafe_allow_html=True)
    st.markdown(f'''<div class="summary"><div class="summary-top"><span class="total">{won(total)}</span>
      <span class="meta">카트 {len(cart)} · 살 것 {len(planned)}</span></div>
      <div class="budget {'over' if budget and total > budget else ''}">{budget_text}</div>
      <div class="memo" title="{html.escape(memo, quote=True)}">📝 {html.escape(memo)}</div></div>''', unsafe_allow_html=True)
    a, b, c = st.columns(3)
    action = None
    item_id = None
    if a.button("＋ 메모", type="primary", use_container_width=True):
        action = "add"
    if b.button("🛒 카트", use_container_width=True):
        action = "cart"
    with c.popover("더보기", use_container_width=True):
        if st.button("메모 · 예산 · 화면", use_container_width=True): action = "settings"
        if st.button("QR / 링크", use_container_width=True): action = "qr"
        if st.button("구매 기록 / CSV", use_container_width=True): action = "history"
        if st.button("새로고침", use_container_width=True): st.rerun()
        st.caption("같은 목록 링크를 북마크해 주세요.")
    a, b = st.columns([1, 1])
    category = a.selectbox("카테고리 필터", ["전체"] + CATEGORIES, key="category_filter",
                           on_change=lambda: st.session_state.update(page=0))
    priority = b.selectbox("중요도 필터", [0, 1, 2, 3], key="priority_filter",
                           format_func=lambda p: "중요도 전체" if p == 0 else PRIORITIES[p],
                           on_change=lambda: st.session_state.update(page=0))
    visible = [r for r in planned if (category == "전체" or r["category"] == category)
               and (priority == 0 or r["priority"] == priority)]
    size = settings["page_size"]
    pages = max(1, math.ceil(len(visible) / size))
    page = min(max(st.session_state.get("page", 0), 0), pages-1)
    st.session_state["page"] = page
    if not visible:
        message = "목록이 비었어요.<br>＋ 메모로 첫 상품을 적어 보세요." if not planned else "조건에 맞는 메모가 없어요."
        st.markdown(f'<div class="empty">🥬<br>{message}</div>', unsafe_allow_html=True)
    for r in visible[page*size:(page+1)*size]:
        with st.container(border=True):
            hint = f"기준 {won(r['target'])}/{r['unit']}" if r["target"] else "기준가 없음"
            detail = f"{r['category']} · {r['planned_qty']:g}{r['unit']} · {hint}"
            if r["memo"]:
                detail += f" · {r['memo']}"
            st.markdown(f'''<div class="itemtitle" title="{html.escape(r['name'], quote=True)}">
              <span class="pill p{r['priority']}">{PRIORITIES[r['priority']]}</span>{html.escape(r['name'])}</div>
              <div class="itemmeta" title="{html.escape(detail, quote=True)}">{html.escape(detail)}</div>''', unsafe_allow_html=True)
            x, y = st.columns([1.5, 1])
            if x.button("담기 · 가격 입력", key=f"buy_{r['id']}", use_container_width=True):
                action, item_id = "buy", r["id"]
            if y.button("메모 수정", key=f"edit_{r['id']}", use_container_width=True):
                action, item_id = "edit", r["id"]
    a, b, c = st.columns([1, 1, 1])
    if a.button("‹ 이전", disabled=page == 0, use_container_width=True):
        st.session_state["page"] = page-1
        st.rerun()
    b.markdown(f'<div class="pager">{page+1} / {pages} · {len(visible)}개</div>', unsafe_allow_html=True)
    if c.button("다음 ›", disabled=page >= pages-1, use_container_width=True):
        st.session_state["page"] = page+1
        st.rerun()
    storage = "외부 DB 저장" if db.url else "임시 저장 · 재배포 시 소실 가능"
    st.markdown(f'<div class="foot">{storage} · 꼭 필요한 것부터 1 → 2 → 3</div>', unsafe_allow_html=True)
    # 한 실행에서 팝업은 반드시 하나만 연다. 전체 재실행에서도 열린 폼 유지.
    if action:
        st.session_state["_dialog"] = (action, item_id)
    action, item_id = st.session_state.get("_dialog", (None, None))
    try:
        if action in ("add", "edit"): item_dialog(db, key, item_id)
        elif action == "buy": buy_dialog(db, key, item_id)
        elif action == "cart": cart_dialog(db, key)
        elif action == "settings": settings_dialog(db, key)
        elif action == "qr": qr_dialog(token)
        elif action == "history": history_dialog(db, key)
    except Exception:
        st.error("작업을 완료하지 못했어요. 새로고침 후 현재 상태를 확인해 주세요.")


if __name__ == "__main__":
    main()

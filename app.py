"""🛒 장보기 알림메모 — Streamlit (모바일 한 화면 / QR 접속 / QR로 메모 공유)

실행:  streamlit run app.py
배포:  GitHub 저장소에 app.py + requirements.txt 올린 뒤 Streamlit Community Cloud에서 배포
"""
import base64
import html
import io
import json
import zlib

import streamlit as st

try:
    import qrcode
except ImportError:  # requirements.txt가 적용되지 않았을 때도 앱은 동작하도록
    qrcode = None

st.set_page_config(
    page_title="장보기 알림메모",
    page_icon="🛒",
    layout="centered",
    initial_sidebar_state="collapsed",
)

# ───────────────────────── 설정 ─────────────────────────
CUR = "₩"
CATS = ["🥬 야채", "🍎 과일", "🥩 정육", "🐟 수산", "🥛 유제품", "🍞 빵·간식", "🧴 생활", "🥫 기타"]
IMP = {
    1: ("🔴", "필수", "가격 상관없이 사기"),
    2: ("🟡", "보통", "비싸면 고민"),
    3: ("🟢", "선택", "비싸면 패스"),
}

# ───────────────────────── 스타일 (모바일 한 화면용) ─────────────────────────
st.markdown(
    """
<style>
#MainMenu, footer, header[data-testid="stHeader"] {display:none !important;}
.block-container {padding: .7rem .8rem .3rem !important; max-width: 560px;}
div[data-testid="stVerticalBlock"] {gap: .4rem !important;}
/* 모바일에서도 컬럼이 세로로 쌓이지 않게 */
div[data-testid="stHorizontalBlock"] {flex-wrap: nowrap !important; gap: .35rem !important; align-items: center;}
div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"],
div[data-testid="stHorizontalBlock"] > div[data-testid="column"] {min-width: 0 !important;}
.stButton > button, .stFormSubmitButton > button {padding: .1rem .3rem; min-height: 2rem; width: 100%;}
div[data-testid="stForm"] {border: none; padding: 0;}
div[data-testid="stTextInput"] input, div[data-testid="stNumberInput"] input {padding: .3rem .5rem; font-size: .9rem;}
div[data-baseweb="select"] > div {min-height: 2rem; font-size: .85rem;}
div[role="radiogroup"] {gap: .5rem !important;}
div[role="radiogroup"] label p {font-size: .85rem;}
.sum {display:flex; justify-content:space-between; background:#f4f6fb; border-radius:10px;
      padding:.4rem .7rem; font-size:.82rem; color:#333;}
.sum b {font-size:1rem; color:#111;}
.row {font-size:.92rem; line-height:1.3; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
.row.skip {opacity:.45; text-decoration: line-through;}
.lim {font-size:.7rem; color:#888; margin-left:.3rem;}
.legend {font-size:.68rem; color:#888; margin-top:-.2rem;}
@media (prefers-color-scheme: dark) {
  .sum {background:#262a33; color:#ddd;} .sum b {color:#fff;}
}
</style>
""",
    unsafe_allow_html=True,
)

ss = st.session_state


# ───────────────────────── 상태 저장 / 복원 (URL ?d=...) ─────────────────────────
def encode_state() -> str:
    obj = {
        "i": [[x["cat"], x["name"], x["imp"], x["limit"], int(x["skip"])] for x in ss.memo],
        "c": [[x["cat"], x["name"], x["imp"], x["limit"], x["price"], x["qty"]] for x in ss.cart],
    }
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode().rstrip("=")


def new_id() -> int:
    ss.nid += 1
    return ss.nid


def load_from_url():
    d = st.query_params.get("d")
    if not d:
        return
    try:
        raw = zlib.decompress(base64.urlsafe_b64decode(d + "=" * (-len(d) % 4)))
        obj = json.loads(raw.decode("utf-8"))
        for cat, name, imp, limit, skip in obj.get("i", []):
            ss.memo.append(
                {"id": new_id(), "cat": cat, "name": name, "imp": imp, "limit": limit, "skip": bool(skip)}
            )
        for cat, name, imp, limit, price, qty in obj.get("c", []):
            ss.cart.append(
                {"id": new_id(), "cat": cat, "name": name, "imp": imp, "limit": limit, "price": price, "qty": qty}
            )
    except Exception:
        pass  # 잘못된 링크면 빈 상태로 시작


if "memo" not in ss:
    ss.memo, ss.cart, ss.nid = [], [], 0
    load_from_url()


def make_qr(data: str) -> bytes:
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=6, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image().save(buf)
    return buf.getvalue()


def won(n: int) -> str:
    return f"{CUR}{int(n):,}"


def emoji_of(cat: int) -> str:
    return CATS[cat].split()[0]


# ───────────────────────── 동작 ─────────────────────────
def add_item():
    name = (ss.get("f_name") or "").strip()
    if not name:
        return
    ss.memo.append(
        {
            "id": new_id(),
            "cat": CATS.index(ss.f_cat),
            "name": name,
            "imp": ss.f_imp,
            "limit": int(ss.get("f_limit") or 0),
            "skip": False,
        }
    )
    ss.f_name = ""
    ss.f_limit = None


@st.dialog("🛒 카트에 담기")
def buy_dialog(iid: int):
    it = next((x for x in ss.memo if x["id"] == iid), None)
    if it is None:
        st.rerun()
    icon, label, hint = IMP[it["imp"]]
    st.markdown(f"**{emoji_of(it['cat'])} {html.escape(it['name'])}**  ·  {icon} {label}")

    c1, c2 = st.columns(2)
    price = c1.number_input(f"단가 ({CUR})", min_value=0, step=100, value=0, key=f"p{iid}")
    qty = c2.number_input("수량", min_value=1, step=1, value=1, key=f"q{iid}")
    total = price * qty
    st.caption(f"합계 **{won(total)}**")

    over = it["imp"] > 1 and it["limit"] > 0 and price > it["limit"]
    if it["imp"] == 1:
        st.info("🔴 필수 품목 — 가격 상관없이 담으세요!")
    elif over:
        diff = price - it["limit"]
        if it["imp"] == 3:
            st.error(f"🟢 선택 품목인데 상한가보다 {won(diff)} 비싸요 → 패스 추천")
        else:
            st.warning(f"🟡 상한가보다 {won(diff)} 비싸요 → 꼭 필요한지 고민해보세요")
    elif it["limit"] > 0:
        st.success(f"상한가({won(it['limit'])}) 이내 👍")

    b1, b2 = st.columns(2)
    if b1.button("🛒 담기", type="primary", disabled=price <= 0, use_container_width=True):
        ss.memo = [x for x in ss.memo if x["id"] != iid]
        ss.cart.append(
            {
                "id": new_id(),
                "cat": it["cat"],
                "name": it["name"],
                "imp": it["imp"],
                "limit": it["limit"],
                "price": int(price),
                "qty": int(qty),
            }
        )
        st.rerun()
    if it["imp"] > 1 and not it["skip"]:
        if b2.button("🚫 패스", use_container_width=True):
            it["skip"] = True
            st.rerun()


# ───────────────────────── 화면 ─────────────────────────
pending = [x for x in ss.memo if not x["skip"]]
cart_total = sum(x["price"] * x["qty"] for x in ss.cart)

st.markdown(
    f"""<div class="sum">
<span>📝 남은 메모 <b>{len(pending)}</b></span>
<span>🛒 카트 <b>{sum(x['qty'] for x in ss.cart)}</b>개</span>
<span>💰 합계 <b>{won(cart_total)}</b></span></div>""",
    unsafe_allow_html=True,
)

# 입력 영역
with st.form("add_form", clear_on_submit=False, border=False):
    a1, a2 = st.columns([1.15, 2])
    a1.selectbox("분류", CATS, key="f_cat", label_visibility="collapsed")
    a2.text_input("상품명", key="f_name", placeholder="상품명 (예: 대파 한 단)", label_visibility="collapsed")
    b1, b2, b3 = st.columns([2.1, 1.7, 0.9])
    b1.radio(
        "중요도",
        [1, 2, 3],
        index=1,
        horizontal=True,
        key="f_imp",
        format_func=lambda v: f"{IMP[v][0]}{v}",
        label_visibility="collapsed",
    )
    b2.number_input(
        "상한가",
        min_value=0,
        step=500,
        value=None,
        key="f_limit",
        placeholder="상한가",
        label_visibility="collapsed",
    )
    b3.form_submit_button("➕", on_click=add_item)
st.markdown(
    '<div class="legend">🔴1 필수(가격무관) · 🟡2 보통(비싸면 고민) · 🟢3 선택(비싸면 패스) · 상한가=이 가격 넘으면 경고</div>',
    unsafe_allow_html=True,
)

# 보기 전환 + QR
t1, t2 = st.columns([5, 1])
view = t1.radio(
    "보기",
    ["memo", "cart"],
    horizontal=True,
    key="view",
    label_visibility="collapsed",
    format_func=lambda v: f"📝 메모 {len(pending)}" if v == "memo" else f"🛒 카트 {len(ss.cart)}",
)

with t2.popover("📱"):
    try:
        default_url = st.secrets.get("APP_URL", "")
    except Exception:
        default_url = ""
    url = st.text_input("앱 주소", value=default_url, placeholder="https://내앱.streamlit.app")
    if qrcode is None:
        st.warning("qrcode 패키지가 설치되지 않았어요. 저장소 루트에 requirements.txt가 있는지 확인하고 앱을 Reboot 해주세요.")
    elif url:
        link = f"{url.rstrip('/')}/?d={encode_state()}"
        try:
            st.image(make_qr(link), caption="스캔하면 지금 메모가 그대로 열려요")
        except Exception:
            st.image(make_qr(url), caption="메모가 길어 앱 주소만 담았어요")
    else:
        st.caption("배포된 앱 주소를 넣으면 QR이 만들어져요.")
    if st.button("🗑 전체 초기화", use_container_width=True):
        ss.memo, ss.cart = [], []
        st.query_params.clear()
        st.rerun()

# 목록 영역 (내부 스크롤만 허용 → 페이지 스크롤 없음)
with st.container(height=255, border=False):
    if view == "memo":
        rows = sorted(ss.memo, key=lambda x: (x["skip"], x["imp"], x["id"]))
        if not rows:
            st.caption("메모가 비었어요. 위에서 추가해 보세요 ✍️")
        for it in rows:
            c1, c2, c3 = st.columns([6, 1.2, 1.2])
            lim = f"<span class='lim'>≤{it['limit']:,}</span>" if it["limit"] and it["imp"] > 1 else ""
            cls = "row skip" if it["skip"] else "row"
            c1.markdown(
                f"<div class='{cls}'>{IMP[it['imp']][0]} {emoji_of(it['cat'])} {html.escape(it['name'])}{lim}</div>",
                unsafe_allow_html=True,
            )
            if c2.button("🛒", key=f"b{it['id']}"):
                buy_dialog(it["id"])
            if c3.button("✕", key=f"d{it['id']}"):
                ss.memo = [x for x in ss.memo if x["id"] != it["id"]]
                st.rerun()
    else:
        if not ss.cart:
            st.caption("아직 카트가 비었어요 🛒")
        for it in ss.cart:
            c1, c2 = st.columns([7, 1.2])
            c1.markdown(
                f"<div class='row'>{emoji_of(it['cat'])} {html.escape(it['name'])} "
                f"<span class='lim'>{won(it['price'])}×{it['qty']}</span> <b>{won(it['price'] * it['qty'])}</b></div>",
                unsafe_allow_html=True,
            )
            if c2.button("↩", key=f"u{it['id']}", help="메모로 되돌리기"):
                ss.cart = [x for x in ss.cart if x["id"] != it["id"]]
                ss.memo.append(
                    {
                        "id": new_id(),
                        "cat": it["cat"],
                        "name": it["name"],
                        "imp": it["imp"],
                        "limit": it["limit"],
                        "skip": False,
                    }
                )
                st.rerun()

# 새로고침해도 유지되도록 URL에 상태 저장
st.query_params["d"] = encode_state()

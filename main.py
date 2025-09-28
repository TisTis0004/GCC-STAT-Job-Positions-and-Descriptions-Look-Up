# main.py
# -*- coding: utf-8 -*-
import io
import os
import re
import html as _html
import pandas as pd
import streamlit as st

# ---------- Settings ----------
APP_DIR = os.path.dirname(os.path.abspath(__file__))
EXCEL_PATH = os.path.join(APP_DIR, "data.xlsx")


# ---------- Helpers ----------
def _to_str_code(x, width):
    """Convert a numeric/str code to a zero-padded string of a given width."""
    if pd.isna(x):
        return None
    try:
        if isinstance(x, (int, float)) or str(x).replace(".", "", 1).isdigit():
            s = str(int(float(x)))
        else:
            s = re.sub(r"\D", "", str(x))
    except Exception:
        s = re.sub(r"\D", "", str(x))
    if not s:
        return None
    s = s.lstrip("0") or "0"
    if len(s) < width:
        s = s.zfill(width)
    return s


def _read_excel_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _build_lookups_from_df(df: pd.DataFrame):
    """Lookups for names at each level + occupation titles."""
    lookups = {
        "section": {
            k: {"ar": a, "en": e}
            for k, a, e in zip(
                df["department"], df["description_ar"], df["description_en"]
            )
            if k
        },
        "part": {
            k: {"ar": a, "en": e}
            for k, a, e in zip(df["sub1"], df["description_ar"], df["description_en"])
            if k
        },
        "chapter": {
            k: {"ar": a, "en": e}
            for k, a, e in zip(df["sub2"], df["description_ar"], df["description_en"])
            if k
        },
        "cls": {
            k: {"ar": a, "en": e}
            for k, a, e in zip(df["sub3"], df["description_ar"], df["description_en"])
            if k
        },
        "occupation": {
            k: {"ar": a, "en": e}
            for k, a, e in zip(df["code"], df["description_ar"], df["description_en"])
            if k
        },
    }
    return lookups


def explain_code_hierarchy(code7: str, lookups: dict):
    code7 = re.sub(r"\D", "", str(code7 or "")).zfill(7)
    if not re.fullmatch(r"\d{7}", code7):
        return None, "Invalid code. Please enter 7 digits."

    d1, d2, d3, d4 = code7[0], code7[:2], code7[:3], code7[:4]
    seq = code7[4:]

    result = {
        "code7": code7,
        "department": {"code": d1, "label": lookups.get("section", {}).get(d1)},
        "sub1": {"code": d2, "label": lookups.get("part", {}).get(d2)},
        "sub2": {"code": d3, "label": lookups.get("chapter", {}).get(d3)},
        "sub3": {"code": d4, "label": lookups.get("cls", {}).get(d4)},
        "sequence": seq,
        "occupation": lookups.get("occupation", {}).get(code7),
    }
    return result, None


# ---------- Data loading (multi-sheet; cache by mtime) ----------
REQUIRED_COLUMNS = [
    "department",
    "sub1",
    "sub2",
    "sub3",
    "code",
    "description_ar",
    "description_en",
]

# Optional name hints (still keep robust fallback)
CANDIDATE_CODE_COLS = [
    "code",
    "رمز",
    "الرمز",
    "occupation_code",
    "job_code",
    "Job Code",
]
CANDIDATE_AR_DESC_COLS = [
    "long_description_ar",
    "description_ar_long",
    "الوصف_المطوّل",
    "الوصف المطوّل",
    "شرح",
    "الشرح",
    "تفاصيل",
    "التفاصيل",
    "وصف",
    "الوصف",
]


def _normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    for c in REQUIRED_COLUMNS:
        if c not in df.columns:
            raise RuntimeError(f"Missing column: {c}")
    df["department"] = df["department"].apply(lambda x: _to_str_code(x, 1))
    df["sub1"] = df["sub1"].apply(lambda x: _to_str_code(x, 2))
    df["sub2"] = df["sub2"].apply(lambda x: _to_str_code(x, 3))
    df["sub3"] = df["sub3"].apply(lambda x: _to_str_code(x, 4))
    df["code"] = df["code"].apply(lambda x: _to_str_code(x, 7))
    return df


def _sheet_looks_like_section(name: str) -> bool:
    """True for sheets like 'قسم 1', 'القسم 2', etc."""
    n = (name or "").strip().lower()
    return "قسم" in n or n.startswith("القسم")


def _is_code_token(val: str):
    """Return normalized (code, width) if the first column cell represents a code of len 1..4 or 7; else (None, None)."""
    s = re.sub(r"\D", "", str(val or ""))
    if not s:
        return None, None
    if len(s) in (1, 2, 3, 4, 7):
        return _to_str_code(s, len(s)), len(s)
    return None, None


def _harvest_long_desc_from_section_sheet(sdf: pd.DataFrame) -> dict:
    """
    Parse headerless two-column sheets like the screenshot:
    - Col A: code tokens appear on some rows (1, 11, 111, 1111, 1111001, ...)
    - Col B: a title on the code row, then one or more subsequent rows with long Arabic description
             until the next code row.
    Return a dict: { '7digit_code': 'long arabic description ...' }.
    """
    if sdf.shape[1] < 2:
        return {}

    # take first two cols as A,B; no headers
    data = sdf.iloc[:, :2].copy()
    data.columns = ["A", "B"]
    # Keep strings; replace NaN with empty strings for concatenation
    data["A"] = data["A"].astype(str)
    data["B"] = data["B"].astype(str)
    data["A"] = data["A"].replace({"nan": ""})
    data["B"] = data["B"].replace({"nan": ""})

    long_desc = {}
    current_code = None
    current_len = None
    buffer_lines = []

    def _commit():
        nonlocal current_code, current_len, buffer_lines
        if current_code and current_len == 7:
            text = "\n".join([ln for ln in (ln.strip() for ln in buffer_lines) if ln])
            if text:
                prev = long_desc.get(current_code)
                if prev is None or len(text) > len(prev):
                    long_desc[current_code] = text
        buffer_lines = []

    for _, row in data.iterrows():
        code_token, clen = _is_code_token(row["A"])
        if code_token:
            # new section/code begins -> commit previous buffer
            _commit()
            current_code, current_len = code_token, clen
            continue

        # Continuation line (no new code in A)
        if current_code:
            txt = (row["B"] or "").strip()
            if txt:
                buffer_lines.append(txt)

    # commit last buffer
    _commit()
    return long_desc


def _pick_code_col(sdf: pd.DataFrame):
    lower_map = {str(c).strip().lower(): c for c in sdf.columns}
    for cc in CANDIDATE_CODE_COLS:
        key = cc.strip().lower()
        if key in lower_map:
            return lower_map[key]
    # Otherwise pick the column whose values look most like 7-digit codes
    best, best_score = None, -1
    for c in sdf.columns:
        series = sdf[c].apply(lambda x: _to_str_code(x, 7))
        score = series.notna().sum()
        if score > best_score:
            best, best_score = c, score
    return best


def _pick_ar_desc_col(sdf: pd.DataFrame, code_col: str):
    lower_map = {str(c).strip().lower(): c for c in sdf.columns}
    for dc in CANDIDATE_AR_DESC_COLS:
        key = dc.strip().lower()
        if key in lower_map:
            return lower_map[key]
    candidates = [c for c in sdf.columns if c != code_col]
    if not candidates:
        return None
    return max(candidates, key=lambda c: sdf[c].astype(str).str.len().mean())


@st.cache_data(show_spinner=False)
def _load_cached(excel_path: str, mtime: float):
    """Cache key includes mtime so we only reload when the file changes."""
    excel_bytes = _read_excel_bytes(excel_path)

    # Read ALL sheets (as strings)
    all_sheets = pd.read_excel(io.BytesIO(excel_bytes), sheet_name=None, dtype=str)

    # --- Find the 'main' sheet (contains REQUIRED_COLUMNS) ---
    main_df = None
    main_sheet_name = None
    for name, sdf in all_sheets.items():
        if all(c in sdf.columns for c in REQUIRED_COLUMNS):
            main_df = sdf.copy()
            main_sheet_name = name
            break
    if main_df is None:
        raise RuntimeError(
            "No sheet contains all required columns: " + ", ".join(REQUIRED_COLUMNS)
        )

    # Normalize + lookups from main sheet
    df = _normalize_df(main_df)
    lookups = _build_lookups_from_df(df)

    # --- Build long Arabic description map ---
    long_desc_ar = {}

    # 1) Special parsing for Arabic 'قسم …' sheets (headerless two columns)
    for name, sdf in all_sheets.items():
        if name == main_sheet_name:
            continue
        if _sheet_looks_like_section(name):
            parsed = _harvest_long_desc_from_section_sheet(sdf)
            for k, v in parsed.items():
                prev = long_desc_ar.get(k)
                if prev is None or len(v) > len(prev):
                    long_desc_ar[k] = v

    # 2) Generic fallback for any other sheet that *does* have headers
    for name, sdf in all_sheets.items():
        if name in (main_sheet_name,):
            continue
        if _sheet_looks_like_section(name):
            # already handled above
            continue
        # Try to find a code col and an Arabic long text column
        code_col = _pick_code_col(sdf)
        if not code_col:
            continue
        desc_col = _pick_ar_desc_col(sdf, code_col)
        if not desc_col:
            continue

        tmp = sdf[[code_col, desc_col]].copy()
        tmp[code_col] = tmp[code_col].apply(lambda x: _to_str_code(x, 7))
        tmp = tmp.dropna(subset=[code_col])

        for code, txt in zip(tmp[code_col], tmp[desc_col].astype(str)):
            if not code:
                continue
            txt = txt.strip()
            if not txt:
                continue
            prev = long_desc_ar.get(code)
            if prev is None or len(txt) > len(prev):
                long_desc_ar[code] = txt

    return lookups, df, long_desc_ar


def load_data_or_die():
    if not os.path.exists(EXCEL_PATH):
        st.error(
            f"Excel file not found: {EXCEL_PATH}\n"
            "Place your data.xlsx next to main.py with headers:\n"
            "department, sub1, sub2, sub3, code, description_ar, description_en"
        )
        st.stop()
    mtime = os.path.getmtime(EXCEL_PATH)
    return _load_cached(EXCEL_PATH, mtime)


# ---------- UI ----------
st.set_page_config(page_title="Job Code Lookup", page_icon="🔎", layout="centered")
st.markdown(
    "<h1 style='text-align:center;'>الدليل الخليجي للتصنيف المهني</h1>",
    unsafe_allow_html=True,
)

# Global CSS for RTL/LTR alignment
st.markdown(
    """
<style>
/* Generic utility classes for alignment */
.rtl { direction: rtl; text-align: right; }
.ltr { direction: ltr; text-align: left; }
/* Make paragraphs and list items honor the direction too */
.rtl p, .rtl li { direction: rtl; text-align: right; }
.ltr p, .ltr li { direction: ltr; text-align: left; }
/* Keep DataFrame tables LTR so numbers/sorting feel natural */
div:where(.stDataFrame) table { direction: ltr; text-align: left; }
</style>
""",
    unsafe_allow_html=True,
)


# Small helpers to render aligned text safely
def _escape(s):
    return _html.escape("" if s is None else str(s))


def rtl_block(md_text: str):
    """Render a block of (possibly multi-line) Arabic text RTL/right-aligned."""
    safe = _escape(md_text).replace("\n", "<br>")
    st.markdown(f'<div class="rtl">{safe}</div>', unsafe_allow_html=True)


def ltr_block(md_text: str):
    """Render a block of (possibly multi-line) English text LTR/left-aligned."""
    safe = _escape(md_text).replace("\n", "<br>")
    st.markdown(f'<div class="ltr">{safe}</div>', unsafe_allow_html=True)


def rtl_kv(label, code, value):
    """Right-aligned single line: **label** code — value (Arabic)."""
    md = f"**{_escape(label)}** {_escape(code)} — {_escape(value if value else '—')}"
    st.markdown(f'<div class="rtl">{md}</div>', unsafe_allow_html=True)


def ltr_kv(label, code, value):
    """Left-aligned single line: **Label** code — value (English)."""
    md = f"**{_escape(label)}** {_escape(code)} — {_escape(value if value else '—')}"
    st.markdown(f'<div class="ltr">{md}</div>', unsafe_allow_html=True)


# Load data
lookups, df, long_desc_ar = load_data_or_die()

tab1, tab2, tab3 = st.tabs(["🔢 Lookup by Code", "🧭 Browse", "🔍 Text Search"])

# ====== TAB 1: Lookup by Code ======
with tab1:
    code_input = st.text_input("Enter 7-digit code — أدخل الرمز:", value="")
    if code_input:
        result, err = explain_code_hierarchy(code_input, lookups)
        if err:
            st.error(err)
        else:
            c7 = result["code7"]
            st.subheader(f"Result: {c7}")
            st.write("**Sequence / الترقيم:**", result["sequence"])

            colA, colB = st.columns(2)
            with colA:
                st.markdown(
                    '<div class="rtl"><h3>العربية (Arabic)</h3></div>',
                    unsafe_allow_html=True,
                )
                rtl_kv(
                    "القسم:",
                    result["department"]["code"],
                    (result["department"]["label"] or {}).get("ar"),
                )
                rtl_kv(
                    "الجزء:",
                    result["sub1"]["code"],
                    (result["sub1"]["label"] or {}).get("ar"),
                )
                rtl_kv(
                    "الباب:",
                    result["sub2"]["code"],
                    (result["sub2"]["label"] or {}).get("ar"),
                )
                rtl_kv(
                    "الفصل:",
                    result["sub3"]["code"],
                    (result["sub3"]["label"] or {}).get("ar"),
                )
                rtl_kv("المسمى المهني:", "", (result["occupation"] or {}).get("ar"))

            with colB:
                st.markdown(
                    '<div class="ltr"><h3>English</h3></div>', unsafe_allow_html=True
                )
                ltr_kv(
                    "Department:",
                    result["department"]["code"],
                    (result["department"]["label"] or {}).get("en"),
                )
                ltr_kv(
                    "Sub1:",
                    result["sub1"]["code"],
                    (result["sub1"]["label"] or {}).get("en"),
                )
                ltr_kv(
                    "Sub2:",
                    result["sub2"]["code"],
                    (result["sub2"]["label"] or {}).get("en"),
                )
                ltr_kv(
                    "Sub3:",
                    result["sub3"]["code"],
                    (result["sub3"]["label"] or {}).get("en"),
                )
                ltr_kv("Occupation Title:", "", (result["occupation"] or {}).get("en"))

            # Long Arabic description (if available)
            long_ar = long_desc_ar.get(c7)
            if long_ar:
                st.markdown(
                    '<div class="rtl"><h3>الوصف العربي المطوّل</h3></div>',
                    unsafe_allow_html=True,
                )
                rtl_block(long_ar)

# ====== TAB 2: Browse ======
with tab2:
    st.markdown("تصفّح هرميًا: اختر المستوى ثم القيمة.")

    # helpers
    def fmt(kind: str, code: str) -> str:
        lab = lookups.get(kind, {}).get(code) or {}
        ar = lab.get("ar") or "—"
        en = lab.get("en") or "—"
        return f"{code} — {ar} / {en}"

    def sort_codes(codes):
        try:
            return sorted(codes, key=lambda x: (len(x), int(x)))
        except Exception:
            return sorted(codes)

    def children_of(kind: str, parent_code: str):
        if not parent_code:
            return []
        all_keys = list(lookups.get(kind, {}).keys())
        return sort_codes([k for k in all_keys if k and k.startswith(parent_code)])

    # reset cascade when parent changes
    def on_change_d1():
        st.session_state.pop("sel_d2", None)
        st.session_state.pop("sel_d3", None)
        st.session_state.pop("sel_d4", None)
        st.session_state.pop("sel_occ_long", None)

    def on_change_d2():
        st.session_state.pop("sel_d3", None)
        st.session_state.pop("sel_d4", None)
        st.session_state.pop("sel_occ_long", None)

    def on_change_d3():
        st.session_state.pop("sel_d4", None)
        st.session_state.pop("sel_occ_long", None)

    # Department
    d1_codes = sort_codes(list({c for c in df["department"].dropna().unique()}))
    if not d1_codes:
        st.warning("لا توجد أقسام متاحة في البيانات.")
        st.stop()

    d1 = st.selectbox(
        "القسم / Department (1-digit)",
        d1_codes,
        format_func=lambda c: fmt("section", c),
        key="sel_d1",
        on_change=on_change_d1,
    )

    # Sub1
    d2_codes = children_of("part", d1)
    d2 = st.selectbox(
        "الجزء / Sub1 (2-digits)",
        d2_codes,
        format_func=lambda c: fmt("part", c),
        key="sel_d2",
        on_change=on_change_d2,
        disabled=len(d2_codes) == 0,
        index=0 if len(d2_codes) > 0 else None,
    )
    if st.session_state.get("sel_d2") not in d2_codes:
        st.session_state.pop("sel_d2", None)
        d2 = None

    # Sub2
    d3_codes = children_of("chapter", st.session_state.get("sel_d2") or "")
    d3 = st.selectbox(
        "الباب / Sub2 (3-digits)",
        d3_codes,
        format_func=lambda c: fmt("chapter", c),
        key="sel_d3",
        on_change=on_change_d3,
        disabled=len(d3_codes) == 0,
        index=0 if len(d3_codes) > 0 else None,
    )
    if st.session_state.get("sel_d3") not in d3_codes:
        st.session_state.pop("sel_d3", None)
        d3 = None

    # Sub3
    d4_codes = children_of("cls", st.session_state.get("sel_d3") or "")
    d4 = st.selectbox(
        "الفصل / Sub3 (4-digits)",
        d4_codes,
        format_func=lambda c: fmt("cls", c),
        key="sel_d4",
        disabled=len(d4_codes) == 0,
        index=0 if len(d4_codes) > 0 else None,
    )
    if st.session_state.get("sel_d4") not in d4_codes:
        st.session_state.pop("sel_d4", None)
        d4 = None

    # Occupations
    st.markdown("#### Occupations under this Class / المسميات ضمن هذا الفصل")
    if d4:
        occ_map = lookups.get("occupation", {})
        occ_codes = sort_codes([c for c in occ_map.keys() if c and c.startswith(d4)])

        if not occ_codes:
            st.write("— لا توجد مسميات مهنية (7 خانات) تحت هذا الفصل —")
        else:
            rows = []
            for c in occ_codes:
                lab = occ_map.get(c, {})
                rows.append(
                    {
                        "code": c,
                        "description_ar": lab.get("ar", "—"),
                        "description_en": lab.get("en", "—"),
                    }
                )
            subset = pd.DataFrame(rows).sort_values("code").set_index("code")
            st.dataframe(subset, use_container_width=True)

            sel_occ = st.selectbox(
                "اختر مسمى مهني لعرض الوصف المطوّل:",
                occ_codes,
                format_func=lambda c: f"{c} — {occ_map.get(c, {}).get('ar', '—')} / {occ_map.get(c, {}).get('en', '—')}",
                key="sel_occ_long",
            )
            if sel_occ:
                st.markdown(
                    '<div class="rtl"><h3>الوصف العربي المطوّل</h3></div>',
                    unsafe_allow_html=True,
                )
                rtl_block(long_desc_ar.get(sel_occ, "— لا يوجد وصف مطوّل —"))
    else:
        st.info("اختر فصلًا لعرض المسميات المهنية تحته.")

# ====== TAB 3: Text Search ======
with tab3:
    q = st.text_input("ابحث نصيًا بالعربية أو الإنجليزية — Search (Arabic/English):")
    if q:
        q_norm = q.strip()
        mask = df["description_ar"].astype(str).str.contains(
            q_norm, case=False, na=False
        ) | df["description_en"].astype(str).str.contains(q_norm, case=False, na=False)
        res = df[mask][
            [
                "department",
                "sub1",
                "sub2",
                "sub3",
                "code",
                "description_ar",
                "description_en",
            ]
        ].drop_duplicates()
        if res.empty:
            st.warning("لا نتائج مطابقة.")
        else:
            st.write(f"**النتائج: {len(res)}**")
            st.dataframe(res, use_container_width=True)

            codes_in_results = sorted(
                [
                    c
                    for c in res["code"].dropna().unique()
                    if isinstance(c, str) and len(c) == 7
                ]
            )
            if codes_in_results:
                occ_map = lookups.get("occupation", {})
                sel_code = st.selectbox(
                    "اعرض الوصف المطوّل للمسمى:",
                    codes_in_results,
                    format_func=lambda c: f"{c} — {occ_map.get(c, {}).get('ar', '—')} / {occ_map.get(c, {}).get('en', '—')}",
                    key="sel_code_from_search",
                )
                if sel_code:
                    st.markdown(
                        '<div class="rtl"><h3>الوصف العربي المطوّل</h3></div>',
                        unsafe_allow_html=True,
                    )
                    rtl_block(long_desc_ar.get(sel_code, "— لا يوجد وصف مطوّل —"))

st.divider()
st.markdown(
    "<p>Developed by <a style='text-decoration:None;color:#2ac408' href='https://www.linkedin.com/in/fares-hatahet/'>Fares Hatahet</a></p>",
    unsafe_allow_html=True,
)

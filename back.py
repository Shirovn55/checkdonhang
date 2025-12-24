# -*- coding: utf-8 -*-
"""
NgânMiu.Store — Web Tra Cứu Đơn Hàng (Google Sheet)
FULL FIX:
- Không dùng get_all_records (tránh lỗi header không unique)
- Auto detect header row
- Map cột theo tên (chuẩn hoá có dấu/không dấu)
- UI + API search phản hồi mượt
"""

import os
import json
import time
import unicodedata
from typing import Dict, List, Tuple, Any

from flask import Flask, request, jsonify, render_template_string

# ===== dotenv (local) =====
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import gspread
from oauth2client.service_account import ServiceAccountCredentials

APP_SECRET_KEY = os.getenv("APP_SECRET_KEY", "devkey").strip()

GOOGLE_SHEET_ID  = os.getenv("GOOGLE_SHEET_ID", "").strip()
GOOGLE_SHEET_TAB = os.getenv("GOOGLE_SHEET_TAB", "Book Shopee").strip()
CREDS_JSON_RAW   = os.getenv("GOOGLE_SHEETS_CREDS_JSON", "").strip()

BRAND_NAME    = "NgânMiu.Store"
BRAND_TAGLINE = "Tra cứu đơn hàng Shopee"

app = Flask(__name__)
app.secret_key = APP_SECRET_KEY


# =========================================================
# Utils: normalize text (remove diacritics)
# =========================================================
def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = " ".join(s.split())
    return s

def _safe(s: Any) -> str:
    return "" if s is None else str(s)

def _contains(hay: str, needle: str) -> bool:
    return _norm(needle) in _norm(hay)

def _money_vnd(x: Any) -> str:
    """
    COD có thể là: 8000, '8000', '8.000', '8,000', '8000đ', ''
    -> format '8.000đ'
    """
    s = _safe(x).strip()
    if not s:
        return ""
    # lấy chữ số
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        return ""
    try:
        n = int(digits)
    except Exception:
        return ""
    # format vi-VN (dấu .)
    return f"{n:,}".replace(",", ".") + "đ"


# =========================================================
# Google Sheet connect
# =========================================================
_SHEET_CLIENT = None
_SHEET_WS = None

# cache dữ liệu sheet (giảm spam API)
_CACHE_AT = 0.0
_CACHE_TTL = 10.0  # giây (bạn muốn nhanh hơn thì để 3-5s)
_CACHE_VALUES = None

def _connect_sheet():
    global _SHEET_CLIENT, _SHEET_WS
    if _SHEET_WS is not None:
        return

    if not GOOGLE_SHEET_ID:
        raise RuntimeError("Thiếu GOOGLE_SHEET_ID trong .env")

    if not CREDS_JSON_RAW:
        raise RuntimeError("Thiếu GOOGLE_SHEETS_CREDS_JSON trong .env")

    try:
        creds_dict = json.loads(CREDS_JSON_RAW)
    except Exception as e:
        raise RuntimeError(f"GOOGLE_SHEETS_CREDS_JSON không phải JSON hợp lệ: {e}")

    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    _SHEET_CLIENT = gspread.authorize(creds)

    sh = _SHEET_CLIENT.open_by_key(GOOGLE_SHEET_ID)
    _SHEET_WS = sh.worksheet(GOOGLE_SHEET_TAB)

def _get_all_values_cached() -> List[List[str]]:
    global _CACHE_AT, _CACHE_VALUES
    now = time.time()
    if _CACHE_VALUES is not None and (now - _CACHE_AT) < _CACHE_TTL:
        return _CACHE_VALUES
    _connect_sheet()
    vals = _SHEET_WS.get_all_values()  # <- không bị lỗi header duplicate
    _CACHE_VALUES = vals
    _CACHE_AT = now
    return vals


# =========================================================
# Detect header row + map columns
# =========================================================
def _detect_header_row(values: List[List[str]]) -> int:
    """
    Scan 1..10 rows để tìm row có nhiều header đặc trưng.
    Ưu tiên row có: 'cookie' + 'mvd' + 'trạng thái' ...
    Return index (0-based). Nếu không thấy -> 2 (hàng 3)
    """
    if not values:
        return 2

    candidates = []
    max_scan = min(10, len(values))
    for r in range(max_scan):
        row = values[r]
        joined = " | ".join(_norm(c) for c in row if c)
        score = 0
        if "cookie" in joined: score += 3
        if "mvd" in joined or "ma van don" in joined: score += 3
        if "trang thai" in joined: score += 2
        if "nguoi nhan" in joined: score += 2
        if "sdt nhan" in joined or "so dt nhan" in joined: score += 1
        if "dia chi" in joined: score += 1
        if "mobile card" in joined: score += 1
        if score > 0:
            candidates.append((score, r))

    if not candidates:
        return 2  # default row 3
    candidates.sort(reverse=True)
    return candidates[0][1]

def _build_header_map(header_row: List[str]) -> Dict[str, int]:
    """
    Map chuẩn hoá -> colIndex (0-based)
    Nếu trùng header, lấy cái đầu tiên (đủ dùng).
    """
    mp = {}
    for i, h in enumerate(header_row):
        key = _norm(h)
        if key and key not in mp:
            mp[key] = i
    return mp

def _pick_col(mp: Dict[str, int], wants: List[str]) -> int:
    """
    wants: list các tên cột possible
    return colIndex or -1
    """
    for w in wants:
        k = _norm(w)
        if k in mp:
            return mp[k]
    # thử match contains (ví dụ "sdt nhan" có thể là "sđt nhận")
    for k, idx in mp.items():
        for w in wants:
            if _norm(w) and _norm(w) in k:
                return idx
    return -1


# =========================================================
# Build card text (đẹp như Telegram)
# =========================================================
def _build_card(item: Dict[str, str]) -> Dict[str, str]:
    mvd    = item.get("mvd", "").strip()
    status = item.get("status", "").strip()
    sp     = item.get("product", "").strip()
    cod    = item.get("cod", "").strip()
    name   = item.get("name", "").strip()
    phone  = item.get("phone", "").strip()
    addr   = item.get("addr", "").strip()

    if not mvd:
        mvd_line = "⏳ <b>Chưa có mã vận đơn</b>"
        mvd_copy = ""
    else:
        mvd_line = f"<code>{mvd}</code>"
        mvd_copy = mvd

    # Sản phẩm: nếu lỡ là link thì vẫn hiển thị nhưng không phá layout
    # (ưu tiên tên, bạn đã sửa sheet = tên sp thì OK)
    sp_show = sp if sp else "—"

    cod_show = cod if cod else ""

    html = []
    html.append('<div class="card">')
    html.append('<div class="card-title">📦 <b>ĐƠN HÀNG</b></div>')
    html.append(f'<div class="line">🆔 <b>MVĐ:</b> {mvd_line}</div>')
    if status:
        html.append(f'<div class="line">📊 <b>Trạng thái:</b> {status}</div>')
    html.append(f'<div class="line">🎁 <b>Sản phẩm:</b> {sp_show}</div>')
    if cod_show:
        html.append(f'<div class="line">💰 <b>COD:</b> {cod_show}</div>')

    html.append('<div class="sep"></div>')
    html.append('<div class="card-title">🚚 <b>GIAO NHẬN</b></div>')
    if name:
        html.append(f'<div class="line">👤 <b>Người nhận:</b> {name}</div>')

    # ✅ TÁCH DÒNG: SĐT 1 dòng, Địa chỉ 1 dòng (không dính nhau nữa)
    if phone:
        html.append(f'<div class="line">📞 <b>SĐT nhận:</b> <a class="phone" href="tel:{phone}">{phone}</a></div>')
    if addr:
        html.append(f'<div class="line">📍 <b>Địa chỉ:</b> {addr}</div>')

    html.append('<div class="hint">👉 Tap vào MVĐ để tự động copy.</div>')
    html.append('</div>')

    return {"html": "\n".join(html), "mvd_copy": mvd_copy}


# =========================================================
# Read & search rows
# =========================================================
def _read_items_from_sheet() -> Tuple[List[Dict[str, str]], str]:
    values = _get_all_values_cached()
    if not values or len(values) < 2:
        return [], "Sheet rỗng"

    hdr_idx = _detect_header_row(values)
    if hdr_idx >= len(values):
        hdr_idx = 0

    header = values[hdr_idx]
    mp = _build_header_map(header)

    # cột cần
    col_name   = _pick_col(mp, ["Tên", "ten"])
    col_mvd    = _pick_col(mp, ["MVĐ", "MVD", "mvd", "mã vận đơn", "ma van don"])
    col_status = _pick_col(mp, ["Trạng thái", "trang thai"])
    col_phone  = _pick_col(mp, ["SĐT nhận", "SDT nhận", "sdt nhan", "so dt nhan"])
    col_addr   = _pick_col(mp, ["Địa chỉ", "dia chi"])
    col_recv   = _pick_col(mp, ["Người nhận", "nguoi nhan"])
    col_prod   = _pick_col(mp, ["Sản Phẩm", "Sản phẩm", "san pham", "SP"])
    col_cod    = _pick_col(mp, ["COD", "cod"])

    # nếu thiếu các cột cơ bản => báo rõ
    must = [("Tên", col_name), ("Người nhận", col_recv), ("Mobile Card", _pick_col(mp, ["Mobile Card", "mobile card"]))]
    # Mobile Card không bắt buộc cho web, nhưng bạn đang dùng để đối chiếu; mình không ép nữa.

    # đọc data từ dòng sau header
    items = []
    for r in range(hdr_idx + 1, len(values)):
        row = values[r]
        # skip row quá ngắn
        if not any(c.strip() for c in row):
            continue

        def get(col: int) -> str:
            if col < 0:
                return ""
            return row[col].strip() if col < len(row) else ""

        name_row = get(col_name)
        if not name_row:
            continue

        it = {
            "name_key": name_row,
            "receiver": get(col_recv),
            "mvd": get(col_mvd),
            "status": get(col_status),
            "phone": get(col_phone),
            "addr": get(col_addr),
            "product": get(col_prod),
            "cod": _money_vnd(get(col_cod)),
        }
        items.append(it)

    return items, ""

def _search_by_name(q: str) -> List[Dict[str, str]]:
    qn = _norm(q)
    items, _ = _read_items_from_sheet()
    out = []
    for it in items:
        if qn and _norm(it.get("name_key", "")).find(qn) >= 0:
            out.append(it)
    return out[:25]


# =========================================================
# Routes
# =========================================================
INDEX_HTML = r"""
<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tra cứu đơn hàng | NgânMiu.Store</title>

<style>
:root{
  --orange:#EE4D2D;
  --orange-dark:#d73211;
  --bg:#f5f5f5;
  --card:#ffffff;
  --text:#222;
  --muted:#6b7280;
  --border:#e5e7eb;
}

*{box-sizing:border-box}
body{
  margin:0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial;
  background:var(--bg);
  color:var(--text);
}

.header{
  background:var(--orange);
  padding:14px 16px;
  color:#fff;
  font-weight:700;
  font-size:18px;
}

.container{
  max-width:720px;
  margin:24px auto;
  padding:0 12px;
}

.search-box{
  background:#fff;
  padding:16px;
  border-radius:6px;
  box-shadow:0 1px 3px rgba(0,0,0,.08);
}

.search-box h2{
  margin:0 0 12px;
  font-size:16px;
  display:flex;
  align-items:center;
  gap:8px;
}

.search-row{
  display:flex;
  gap:8px;
}

.search-row input{
  flex:1;
  height:38px;
  padding:0 10px;
  border:1px solid var(--border);
  border-radius:4px;
  font-size:14px;
}

.search-row button{
  height:38px;
  padding:0 16px;
  background:var(--orange);
  color:#fff;
  border:none;
  border-radius:4px;
  font-weight:600;
  cursor:pointer;
}

.search-row button:hover{
  background:var(--orange-dark);
}

.msg{
  margin-top:12px;
  padding:10px;
  border-radius:4px;
  font-size:14px;
  display:none;
}

.msg.err{
  background:#fee2e2;
  color:#991b1b;
}

.results{
  margin-top:16px;
}

/* ===== ORDER CARD ===== */
.order-card{
  background:var(--card);
  border-radius:6px;
  padding:14px;
  margin-bottom:12px;
  border:1px solid var(--border);
}

.order-title{
  font-weight:700;
  margin-bottom:8px;
}

.row{
  margin:4px 0;
  font-size:14px;
  line-height:1.45;
}

.row b{
  font-weight:600;
}

.mvd{
  display:inline-block;
  background:#f3f4f6;
  border:1px solid var(--border);
  padding:2px 6px;
  border-radius:4px;
  font-family:monospace;
  cursor:pointer;
}

.sep{
  height:1px;
  background:var(--border);
  margin:10px 0;
}

.phone{
  color:#2563eb;
  text-decoration:none;
}

.footer{
  text-align:center;
  font-size:12px;
  color:var(--muted);
  margin-top:16px;
}
</style>
</head>

<body>

<div class="header">
  🔎 Tra cứu đơn hàng Shopee
</div>

<div class="container">

  <div class="search-box">
    <h2>📦 Tra cứu đơn hàng</h2>
    <div class="search-row">
      <input id="q" placeholder="Nhập tên người nhận (vd: The One)">
      <button onclick="doSearch()">Tìm</button>
    </div>
    <div id="msg" class="msg"></div>
  </div>

  <div id="results" class="results"></div>

  <div class="footer">© NgânMiu.Store – Tra cứu đơn hàng Shopee</div>
</div>

<script>
async function doSearch(){
  const q = document.getElementById("q").value.trim();
  const msg = document.getElementById("msg");
  const results = document.getElementById("results");

  msg.style.display="none";
  msg.className="msg";
  results.innerHTML="";

  if(q.length < 2){
    msg.textContent="❌ Vui lòng nhập tên cần tra cứu";
    msg.className="msg err";
    msg.style.display="block";
    return;
  }

  try{
    const res = await fetch("/api/search",{
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({q})
    });
    const js = await res.json();

    if(!js.ok){
      msg.textContent="❌ "+js.msg;
      msg.className="msg err";
      msg.style.display="block";
      return;
    }

    js.items.forEach(it=>{
      const div=document.createElement("div");
      div.className="order-card";
      div.innerHTML=it.html;
      const mvd=div.querySelector(".mvd");
      if(mvd){
        mvd.onclick=()=>{
          navigator.clipboard.writeText(mvd.innerText);
          mvd.innerText+=" ✓";
          setTimeout(()=>mvd.innerText=mvd.innerText.replace(" ✓",""),800);
        };
      }
      results.appendChild(div);
    });

  }catch(e){
    msg.textContent="❌ Lỗi kết nối server";
    msg.className="msg err";
    msg.style.display="block";
  }
}

document.getElementById("q").addEventListener("keydown",e=>{
  if(e.key==="Enter") doSearch();
});
</script>

</body>
</html>
"""


@app.get("/")
def index():
    return render_template_string(INDEX_HTML, brand=BRAND_NAME, tagline=BRAND_TAGLINE)

@app.post("/api/search")
def api_search():
    try:
        data = request.get_json(silent=True) or {}
        q = (data.get("q") or "").strip()
        if len(q) < 2:
            return jsonify({"ok": False, "msg": "Tên quá ngắn"})
        rows = _search_by_name(q)

        items = []
        for r in rows:
            card = _build_card({
                "mvd": r.get("mvd", ""),
                "status": r.get("status", ""),
                "product": r.get("product", ""),
                "cod": r.get("cod", ""),
                "name": r.get("receiver", ""),
                "phone": r.get("phone", ""),
                "addr": r.get("addr", ""),
            })
            items.append(card)

        return jsonify({"ok": True, "items": items})

    except Exception as e:
        return jsonify({"ok": False, "msg": f"Lỗi server: {e}"}), 500

@app.get("/health")
def health():
    try:
        _connect_sheet()
        return jsonify({"ok": True, "tab": GOOGLE_SHEET_TAB})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


if __name__ == "__main__":
    # chạy local
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)

import os
import io
import json
import re
import time
import threading
import socket
from typing import Dict, Any, Optional, Tuple, List

import requests
from gcn_kafka import Consumer

# --- Immagini / grafica ---
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from PIL import Image, ImageDraw, ImageFont
from astropy.io import fits

# healpy
try:
    import healpy as hp  # type: ignore
    HAVE_HEALPY = True
except Exception:
    HAVE_HEALPY = False

# ==========================
# CREDENZIALI (via ENV)
# ==========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "INSERIRE TOKEN")
ADMIN_CHAT_ID_ENV = os.getenv("ADMIN_CHAT_ID", "INSERIRE ID")
ADMIN_CHAT_ID = int(ADMIN_CHAT_ID_ENV) if ADMIN_CHAT_ID_ENV.isdigit() else None

CLIENT_ID     = os.getenv("GCN_CLIENT_ID", "INSERIRE ID")
CLIENT_SECRET = os.getenv("GCN_CLIENT_SECRET", " INSERIRE CHIAVE")

# ==========================
# TOPICS GCN
# ==========================
TOPICS = [
    "igwn.gwalert",                       # GW JSON
    "gcn.notices.swift.bat.guano",        # Swift GUANO (JSON)
    "gcn.classic.text.FERMI_GBM_ALERT",   # Fermi GBM classic text
    "gcn.classic.text.FERMI_GBM_FLT_POS",
    "gcn.classic.text.FERMI_GBM_GND_POS",
    "gcn.classic.text.FERMI_GBM_FIN_POS",
]

# ==========================
# STORAGE
# ==========================
SEEN_FILE = "seen_offsets.json"   # {topic: last_offset}
SUBS_FILE = "subscribers.json"    # {chat_id: {"filters":{...}, "muted": bool}}
CIRC_FILE = "circulars_seen.json" # {"last_id": 12345}

# Ultimo alert ricevuto 
LAST_ALERT: Optional[Tuple[str, Dict[str, Any]]] = None

def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if data is not None else default
    except Exception:
        return default

def save_json(path: str, data) -> None:
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        print(f"[save_json] warning: {e}")

# ==========================
# TELEGRAM API
# ==========================
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN env var")

TG = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

def tg_send_text(chat_id: int, text: str, parse_mode: Optional[str] = "HTML", reply_markup: Optional[dict] = None):
    try:
        payload = {
            "chat_id": chat_id,
            "text": text[:4000],
            "parse_mode": parse_mode,
            "disable_web_page_preview": True
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        r = requests.post(f"{TG}/sendMessage", json=payload, timeout=20)
        r.raise_for_status()
    except Exception as e:
        print(f"[Telegram] send_text error: {e}")

def tg_send_photo_bytes(chat_id: int, img_bytes: bytes, caption: Optional[str] = None):
    try:
        files = {"photo": ("image.jpg", img_bytes, "image/jpeg")}
        data = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption[:1024]
            data["parse_mode"] = "HTML"
        r = requests.post(f"{TG}/sendPhoto", data=data, files=files, timeout=60)
        r.raise_for_status()
    except Exception as e:
        print(f"[Telegram] send_photo error: {e}")

def tg_get_updates(offset: Optional[int] = None, timeout=30):
    try:
        params = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        r = requests.get(f"{TG}/getUpdates", params=params, timeout=timeout+5)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[Telegram] getUpdates error: {e}")
        return {"ok": False, "result": []}

def tg_answer_callback_query(cb_id: str, text: str = ""):
    try:
        requests.post(f"{TG}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": text[:200]}, timeout=10)
    except Exception:
        pass

def tg_edit_message_text(chat_id: int, message_id: int, text: str, reply_markup: Optional[dict] = None):
    try:
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        requests.post(f"{TG}/editMessageText", json=payload, timeout=15)
    except Exception:
        pass

def tg_set_my_commands(commands: List[Tuple[str, str]]):
    try:
        cmd_list = [{"command": c, "description": d[:256]} for c, d in commands]
        requests.post(f"{TG}/setMyCommands", json={"commands": cmd_list}, timeout=10)
    except Exception:
        pass

def tg_delete_webhook():
    """Disattiva il webhook così getUpdates funziona senza 409."""
    try:
        requests.post(f"{TG}/deleteWebhook", json={"drop_pending_updates": False}, timeout=10)
    except Exception as e:
        print(f"[Telegram] deleteWebhook error: {e}")

def tg_set_my_description(description: str, short_description: Optional[str] = None):
    """Imposta testo visibile nella chat prima di /start (banner del bot)."""
    try:
        requests.post(f"{TG}/setMyDescription", json={"description": description[:512]}, timeout=10)
        if short_description:
            requests.post(f"{TG}/setMyShortDescription", json={"short_description": short_description[:120]}, timeout=10)
    except Exception as e:
        print(f"[Telegram] setMyDescription error: {e}")

# ==========================
# KEYBOARDS
# ==========================
def keyboard_main_menu() -> dict:
    # Menu principale: Impostazioni + Test + Help + Contatta autore
    return {
        "inline_keyboard": [
            [ {"text": "📂 Menu", "callback_data": "cmd:/menu"} ],
            [ {"text": "⚙️ Impostazioni", "callback_data": "cmd:/impostazioni"} ],
            [ {"text": "🧪 Test ricezione GCN", "callback_data": "cmd:/testriceviultimagcn"} ],
            [ {"text": "❓ Help", "callback_data": "cmd:/help"},
              {"text": "👤 Contatta autore", "callback_data": "cmd:/contattaautore"} ]
        ]
    }

def keyboard_submenu() -> dict:
    # Impostazioni con back al menu principale
    return {
        "inline_keyboard": [
            [ {"text": "✅ Attiva ricezione", "callback_data": "cmd:/attivaricezione"},
              {"text": "🚫 Disattiva ricezione", "callback_data": "cmd:/disattivaricezione"} ],
            [ {"text": "✔️ Filtri", "callback_data": "cmd:/filtri"},
              {"text": "ℹ️ Status", "callback_data": "cmd:/status"} ],
            [ {"text": "⬅️ Torna al menu", "callback_data": "cmd:/menu"} ]
        ]
    }

def keyboard_filters_inline(filters: Dict[str, bool]) -> dict:
    gw = "🌊 GW: ON" if filters.get("gw", False) else "🌊 GW: OFF"
    sf = "🛰️ Swift/Fermi: ON" if filters.get("swiftfermi", True) else "🛰️ Swift/Fermi: OFF"
    cc = "📝 Circulars: ON" if filters.get("circulars", False) else "📝 Circulars: OFF"
    return {
        "inline_keyboard": [
            [ {"text": gw, "callback_data": "toggle:gw"} ],
            [ {"text": sf, "callback_data": "toggle:swiftfermi"} ],
            [ {"text": cc, "callback_data": "toggle:circulars"} ],
            [ {"text": "⬅️ Torna indietro", "callback_data": "cmd:/impostazioni"} ]
        ]
    }

# ==========================
# SUBSCRIBERS & FILTERS
# ==========================
def default_filters() -> Dict[str, bool]:
    # Default: Swift/Fermi (solo GRB) ON; GW OFF; Circulars ON 
    return {"gw": False, "swiftfermi": True, "circulars": True}

def get_user_entry(chat_id: int) -> Dict[str, Any]:
    subs = load_json(SUBS_FILE, {})
    entry = subs.get(str(chat_id))
    if not entry:
        entry = {"filters": default_filters(), "muted": False}
        subs[str(chat_id)] = entry
        save_json(SUBS_FILE, subs)
    f = entry.get("filters", {})
    if "swift" in f or "fermi" in f:
        on = bool(f.get("swift", False) or f.get("fermi", False))
        f["swiftfermi"] = on
        f.pop("swift", None)
        f.pop("fermi", None)
        entry["filters"] = f
        subs[str(chat_id)] = entry
        save_json(SUBS_FILE, subs)
    return entry

def add_subscriber(chat_id: int):
    get_user_entry(chat_id)

def set_muted(chat_id: int, muted: bool):
    subs = load_json(SUBS_FILE, {})
    entry = subs.get(str(chat_id), {"filters": default_filters(), "muted": False})
    entry["muted"] = muted
    subs[str(chat_id)] = entry
    save_json(SUBS_FILE, subs)

def get_filters(chat_id: int) -> Dict[str, bool]:
    return get_user_entry(chat_id).get("filters", default_filters())

def set_filters(chat_id: int, gw: Optional[bool]=None, swiftfermi: Optional[bool]=None, circulars: Optional[bool]=None):
    subs = load_json(SUBS_FILE, {})
    entry = subs.get(str(chat_id), {"filters": default_filters(), "muted": False})
    f = entry.get("filters", default_filters())
    if gw is not None: f["gw"] = gw
    if swiftfermi is not None: f["swiftfermi"] = swiftfermi
    if circulars is not None: f["circulars"] = circulars
    entry["filters"] = f
    subs[str(chat_id)] = entry
    save_json(SUBS_FILE, subs)

def list_subscribers() -> Dict[str, Dict[str, Any]]:
    return load_json(SUBS_FILE, {})

# ==========================
# GRAFICA / IMMAGINI
# ==========================
def _bytes_from_plt() -> bytes:
    fig = plt.gcf()
    # sfondo bianco per evitare "immagini nere/vuote" 
    fig.patch.set_facecolor("white")
    buf = io.BytesIO()
    plt.tight_layout()
    plt.savefig(buf, format="jpg", dpi=140, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.read()

def draw_quick_card(title: str, lines: List[str]) -> bytes:
    W, H = 1000, 600
    img = Image.new("RGB", (W, H), (18, 18, 24))
    draw = ImageDraw.Draw(img)
    try:
        font_title = ImageFont.truetype("arial.ttf", 56)
        font_text  = ImageFont.truetype("arial.ttf", 32)
    except Exception:
        font_title = ImageFont.load_default()
        font_text  = ImageFont.load_default()
    draw.rounded_rectangle([(20, 20), (W-20, H-20)], radius=30, outline=(90, 90, 120), width=4)
    draw.text((50, 50), title, font=font_title, fill=(220, 220, 250))
    y = 140
    for line in lines[:7]:
        draw.text((50, y), line, font=font_text, fill=(200, 200, 210))
        y += 48
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=90)
    out.seek(0)
    return out.read()

def aitoff_from_radec(ra_deg: float, dec_deg: float, title="Localization (Aitoff)") -> bytes:
    ra_rad = np.deg2rad(ra_deg)
    ra_plot = np.pi - ra_rad
    if ra_plot > np.pi:
        ra_plot -= 2*np.pi
    dec_rad = np.deg2rad(dec_deg)
    fig = plt.figure(figsize=(8, 5), facecolor="white")
    ax = plt.subplot(111, projection="aitoff")
    ax.grid(True, alpha=0.6)
    ax.set_title(title)
    ax.scatter(ra_plot, dec_rad, s=80)
    return _bytes_from_plt()

def make_skymap_from_healpix_fits(url: str, title="Skymap") -> Optional[bytes]:
    if not HAVE_HEALPY:
        return None
    try:
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        with fits.open(io.BytesIO(r.content)) as hdul:
            if len(hdul) > 1 and getattr(hdul[1], "data", None) is not None:
                data = hdul[1].data
                if getattr(data, "dtype", None) is not None and getattr(data.dtype, "names", None):
                    if "PROB" in data.dtype.names:
                        m = np.array(data["PROB"], dtype=float)
                    else:
                        m = np.array(data).astype(float).squeeze()
                else:
                    m = np.array(data).astype(float).squeeze()
            else:
                m = hdul[0].data
                if m is None:
                    return None
                m = np.array(m, dtype=float).squeeze()
            plt.figure(figsize=(8, 5), facecolor="white")
            hp.mollview(m, title=title, unit="prob", norm="log", min=1e-6, cbar=True)
            hp.graticule()
            return _bytes_from_plt()
    except Exception as e:
        print(f"[Skymap] errore: {e}")
        return None

# ---- Nuovi helper per immagini dagli alert ----
def _find_image_url_in_obj(obj: Dict[str, Any]) -> Optional[str]:
    """Cerca url .png/.jpg/.jpeg in dizionario JSON (anche annidato)."""
    exts = (".png", ".jpg", ".jpeg")
    try:
        # tentativi diretti su chiavi comuni
        for key in ("image_url", "image", "preview", "thumbnail", "quicklook"):
            val = obj
            if isinstance(val, dict) and key in val and isinstance(val[key], str) and val[key].lower().endswith(exts):
                return val[key]
        # scansione profonda
        stack = [obj]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                for _, v in cur.items():
                    if isinstance(v, str) and v.lower().endswith(exts):
                        return v
                    elif isinstance(v, (dict, list)):
                        stack.append(v)
            elif isinstance(cur, list):
                stack.extend(cur)
    except Exception:
        pass
    return None

def _find_image_url_in_text(txt: str) -> Optional[str]:
    """Estrae prima URL .png/.jpg dal testo classic notice."""
    m = re.search(r'(https?://\S+\.(?:png|jpg|jpeg))', txt, re.I)
    return m.group(1) if m else None

def _download_image_bytes(url: str) -> Optional[bytes]:
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        ct = r.headers.get("Content-Type", "").lower()
        if ("image/" in ct) or url.lower().endswith((".png", ".jpg", ".jpeg")):
            return r.content
    except Exception as e:
        print(f"[image] download error {url}: {e}")
    return None

# ======= NUOVI HELPER: RA/Dec da GCN Circular =======
def _sexagesimal_to_deg_ra(h: int, m: int, s: float) -> float:
    """RA h:m:s -> gradi."""
    return (h + m/60.0 + s/3600.0) * 15.0

def _sexagesimal_to_deg_dec(sign: int, d: int, m: int, s: float) -> float:
    """Dec ±d:m:s -> gradi."""
    val = abs(d) + m/60.0 + s/3600.0
    return val * (1 if sign >= 0 else -1)

def _strip_html(text: str) -> str:
    t = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"&nbsp;", " ", t)  
    return re.sub(r"\s+", " ", t).strip()

def fetch_circular_body(url: str) -> Optional[str]:
    """Scarica il corpo HTML della circular."""
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"[Circular body] fetch error: {e}")
        return None

def parse_ra_dec_from_text(text: str) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[str], Optional[str]]:
    """
    Cerca RA/Dec (J2000) in formato sessagesimale e l'incertezza in arcsec.
    Ritorna: (ra_deg, dec_deg, unc_arcsec, ra_sex, dec_sex)
    """
    if not text:
        return None, None, None, None, None

    t = text

    ra_pat = re.compile(
        r'RA\s*\(J2000\)\s*[:=]?\s*([0-2]?\d)[h:\s]+([0-5]?\d)[m:\s]+([0-5]?\d(?:\.\d+)?)[s"]?',
        re.I
    )
    dec_pat = re.compile(
        r'Dec\s*\(J2000\)\s*[:=]?\s*([+\-]?\d{1,3})[d°:\s]+([0-5]?\d)[\'m:\s]+([0-5]?\d(?:\.\d+)?)(?:["s])?',
        re.I
    )

    mra = ra_pat.search(t)
    mdec = dec_pat.search(t)

    ra_deg = dec_deg = None
    ra_sex = dec_sex = None

    if mra and mdec:
        try:
            h = int(mra.group(1)); mm = int(mra.group(2)); ss = float(mra.group(3))
            d = int(mdec.group(1)); dm = int(mdec.group(2)); ds = float(mdec.group(3))
            ra_deg = _sexagesimal_to_deg_ra(h, mm, ss)
            sign = 1 if d >= 0 else -1
            dec_deg = _sexagesimal_to_deg_dec(sign, d, dm, ds)
            ra_sex = f"{h}h {mm}m {ss:.2f}s"
            dec_sex = f"{d}d {dm}' {ds:.2f}\""
        except Exception:
            pass

    unc_arcsec = None
    m_unc = re.search(r'(?:uncertainty|radius)\s+of\s+([\d\.]+)\s*arcsec', t, re.I)
    if not m_unc:
        m_unc = re.search(r'([\d\.]+)\s*arcsec\s*(?:uncertainty|radius)', t, re.I)
    if m_unc:
        try:
            unc_arcsec = float(m_unc.group(1))
        except Exception:
            pass

    return ra_deg, dec_deg, unc_arcsec, ra_sex, dec_sex

def extract_coords_from_circular(url: str) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[str], Optional[str]]:
    """
    Scarica la pagina della circular e prova a estrarre RA/Dec/uncertainty.
    """
    html = fetch_circular_body(url)
    if not html:
        return None, None, None, None, None

    txt = _strip_html(html)
    ra_deg, dec_deg, unc, ra_sex, dec_sex = parse_ra_dec_from_text(txt)
    if ra_deg is None or dec_deg is None:
        ra_deg, dec_deg, unc, ra_sex, dec_sex = parse_ra_dec_from_text(html)
    return ra_deg, dec_deg, unc, ra_sex, dec_sex

# ==========================
# PARSER / FILTRI
# ==========================
def fmt_float(x: Optional[float], nd=3) -> str:
    try:
        return f"{float(x):.{nd}f}"
    except Exception:
        return "—"

# --- helper: estraggo RA/Dec da testo in più formati ---
def _extract_radec(txt: str) -> Tuple[Optional[float], Optional[float]]:
    patterns = [
        r'RA\s*=\s*([+\-]?\d+(?:\.\d+)?)\D+DEC\s*=\s*([+\-]?\d+(?:\.\d+)?)',
        r'RA\s*,\s*DEC\s*,\s*ERR\s*=\s*([+\-]?\d+(?:\.\d+)?)[,\s]+([+\-]?\d+(?:\.\d+)?)[,\s]+\d',
        r'RA\s*\(J2000\)\s*([+\-]?\d+(?:\.\d+)?)\D+DEC\s*\(J2000\)\s*([+\-]?\d+(?:\.\d+)?)',
        r'RA\s*[:]\s*([+\-]?\d+(?:\.\d+)?)\s*[,;]\s*DEC\s*[:]\s*([+\-]?\d+(?:\.\d+)?)',
        r'RA\s*([+\-]?\d+(?:\.\d+)?)\s*deg\W+DEC\W*([+\-]?\d+(?:\.\d+)?)\s*deg',
    ]
    for pat in patterns:
        m = re.search(pat, txt, re.IGNORECASE | re.DOTALL)
        if m:
            try:
                ra = float(m.group(1)); dec = float(m.group(2))
                return ra, dec
            except Exception:
                pass
    return None, None

def parse_igwn_json(obj: Dict[str, Any]) -> Tuple[Optional[str], Dict[str, Any]]:
    if not isinstance(obj, dict):
        return None, {}
    superevent = obj.get("superevent_id") or obj.get("superevent") or "GW event"
    alert_type = (obj.get("alert_type") or obj.get("alerttype") or "notice").lower()
    # SKIP preliminari
    is_prelim = ("prelim" in alert_type) or ("preliminary" in alert_type)
    if is_prelim:
        return None, {"type": "gw", "skip": True}
    ev = obj.get("event") or {}
    gps_time = ev.get("time") if isinstance(ev, dict) else None
    far = ev.get("far") if isinstance(ev, dict) else None
    clas = ev.get("classification") if isinstance(ev, dict) else {}
    probs = []
    if isinstance(clas, dict):
        for k in ("BNS", "NSBH", "BBH", "MassGap", "Terrestrial"):
            if k in clas:
                try:
                    probs.append(f"{k}:{int(round(100*float(clas[k])))}%")
                except Exception:
                    pass
    probs_str = " | ".join(probs) if probs else "—"
    skymap_url = None
    if isinstance(obj.get("skymap"), dict):
        skymap_url = obj["skymap"].get("url")
    if not skymap_url and isinstance(obj.get("links"), dict):
        skymap_url = obj["links"].get("skymap") or obj["links"].get("event_page")
    image_url = _find_image_url_in_obj(obj)
    caption = (
        f"🌊 <b>GW {superevent}</b> — {alert_type}\n"
        f"🕒 GPS: {gps_time if gps_time is not None else '—'} | 📉 FAR: {fmt_float(far, 3)} Hz\n"
        f"🧪 Classificazione: {probs_str}"
    )
    meta = {"type": "gw", "skymap_url": skymap_url, "image_url": image_url}
    return caption, meta

def parse_swift_guano_json(obj: Dict[str, Any]) -> Tuple[Optional[str], Dict[str, Any]]:
    if not isinstance(obj, dict):
        return None, {}
    ntype = obj.get("notice_type") or obj.get("type") or ""
    t0 = obj.get("event_time") or obj.get("time")
    name = obj.get("event_name") or obj.get("name") or ""
    ra = obj.get("ra"); dec = obj.get("dec")
    healpix = obj.get("skymap", {}).get("url") if isinstance(obj.get("skymap"), dict) else None

    is_grb = ("GRB" in str(name).upper()) or ("GRB" in str(ntype).upper())
    if not is_grb:
        return None, {}

    if (ra is None or dec is None) and not healpix:
        return None, {}

    image_url = _find_image_url_in_obj(obj)
    caption = (
        f"🛰️ <b>Swift-BAT GUANO</b>\n"
        f"🧾 Evento: {name or '—'}   🕒 T0: {t0 if t0 else '—'}\n"
        f"📍 RA: {fmt_float(ra)}  Dec: {fmt_float(dec)}"
    )
    meta = {"type": "swiftfermi", "skymap_url": healpix, "image_url": image_url, "ra": ra, "dec": dec}
    return caption, meta

def parse_fermi_text(txt: str) -> Tuple[Optional[str], Dict[str, Any]]:
    if "GRB" not in txt.upper():
        return None, {}
    ra, dec = _extract_radec(txt)
    if ra is None or dec is None:
        img = _find_image_url_in_text(txt)
        if not img:
            return None, {}
        caption = (
            f"⚡ <b>Fermi-GBM alert</b>\n"
            f"🧾 Evento: GRB (dettagli nel notice)\n"
            f"📍 RA/Dec non disponibili nel notice"
        )
        meta = {"type": "swiftfermi", "ra": None, "dec": None, "image_url": img}
        return caption, meta
    ev = None
    for pat in [r'GRB\s+\d{6}[A-Z]?', r'TRIGGER[_\s]*ID[:=\s]+(\S+)', r'NOTICE_TYPE:\s*(.+)']:
        m2 = re.search(pat, txt, re.I)
        if m2:
            ev = m2.group(0)
            break
    image_url = _find_image_url_in_text(txt)
    caption = (
        f"⚡ <b>Fermi-GBM alert</b>\n"
        f"🧾 Evento: {ev or '—'}\n"
        f"📍 RA: {fmt_float(ra)}  Dec: {fmt_float(dec)}"
    )
    meta = {"type": "swiftfermi", "ra": ra, "dec": dec, "image_url": image_url}
    return caption, meta

def try_load_json(raw: bytes) -> Optional[Any]:
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None

def event_kind_to_filter_key(kind: str) -> str:
    if kind == "gw":
        return "gw"
    if kind == "swiftfermi":
        return "swiftfermi"
    if kind == "circulars":
        return "circulars"
    return "swiftfermi"

# ==========================
# DISPATCH / BROADCAST
# ==========================
def build_and_send_with_image(caption: str, meta: Dict[str, Any]):
    kind = meta.get("type", "swiftfermi")
    skymap_url = meta.get("skymap_url")
    ra = meta.get("ra")
    dec = meta.get("dec")
    image_url = meta.get("image_url")

    img_bytes = None
    if image_url:
        img_bytes = _download_image_bytes(str(image_url))
    if img_bytes is None and skymap_url and (str(skymap_url).endswith(".fits") or str(skymap_url).endswith(".fits.gz")):
        img_bytes = make_skymap_from_healpix_fits(skymap_url, title="Skymap")
    if img_bytes is None and (ra is not None and dec is not None):
        try:
            img_bytes = aitoff_from_radec(float(ra), float(dec), title="Localization (Aitoff)")
        except Exception:
            img_bytes = None
    if img_bytes is None:
        lines = [l for l in caption.split("\n")[1:6]]
        img_bytes = draw_quick_card(title=caption.split("\n")[0], lines=lines)

    subs = list_subscribers()
    for k, v in subs.items():
        chat_id = int(k)
        entry = get_user_entry(chat_id)
        if entry.get("muted", False):
            continue
        filters = entry.get("filters", default_filters())
        if not filters.get(event_kind_to_filter_key(kind), False):
            continue
        try:
            tg_send_photo_bytes(chat_id, img_bytes, caption=caption)
        except Exception as e:
            print(f"[broadcast] chat {chat_id} photo error: {e}")

def send_one_with_image(chat_id: int, caption: str, meta: Dict[str, Any]):
    skymap_url = meta.get("skymap_url")
    ra = meta.get("ra")
    dec = meta.get("dec")
    image_url = meta.get("image_url")

    img_bytes = None
    if image_url:
        img_bytes = _download_image_bytes(str(image_url))
    if img_bytes is None and skymap_url and (str(skymap_url).endswith(".fits") or str(skymap_url).endswith(".fits.gz")):
        img_bytes = make_skymap_from_healpix_fits(skymap_url, title="Skymap")
    if img_bytes is None and (ra is not None and dec is not None):
        try:
            img_bytes = aitoff_from_radec(float(ra), float(dec), title="Localization (Aitoff)")
        except Exception:
            img_bytes = None
    if img_bytes is None:
        lines = [l for l in caption.split("\n")[1:6]]
        img_bytes = draw_quick_card(title=caption.split("\n")[0], lines=lines)
    tg_send_photo_bytes(chat_id, img_bytes, caption=caption)

# ==========================
# KAFKA CONSUMER THREAD
# ==========================
def consumer_loop():
    global LAST_ALERT
    seen: Dict[str, int] = load_json(SEEN_FILE, {})
    consumer = Consumer(
        config={
            "group.id": "gcn2telegram_plus",
            "auto.offset.reset": "latest",
            "enable.auto.commit": True,
            "message.max.bytes": 20 * 1024 * 1024,
        },
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        domain="gcn.nasa.gov",
    )
    consumer.subscribe(TOPICS)

    print("[GCN] Subscribed to topics:")
    for t in TOPICS:
        print("  -", t)

    last_persist = time.time()
    while True:
        try:
            for msg in consumer.consume(timeout=1):
                if msg is None:
                    continue
                if msg.error():
                    print("[GCN] Error:", msg.error())
                    continue

                topic = msg.topic() or ""
                offset = int(msg.offset() or 0)

                last_seen = int(seen.get(topic, -1))
                if offset <= last_seen:
                    continue

                value = msg.value() or b""
                text_caption: Optional[str] = None
                meta: Dict[str, Any] = {}

                obj = try_load_json(value)
                if isinstance(obj, dict):
                    if topic.startswith("igwn.gwalert"):
                        text_caption, meta = parse_igwn_json(obj)
                    elif topic.startswith("gcn.notices.swift.bat.guano"):
                        text_caption, meta = parse_swift_guano_json(obj)
                    else:
                        text_caption, meta = None, {}
                else:
                    txt = value.decode("utf-8", errors="replace")
                    if "FERMI_GBM" in topic.upper():
                        text_caption, meta = parse_fermi_text(txt)
                    else:
                        text_caption, meta = None, {}

                if text_caption and meta.get("type") == "gw" and meta.get("skip"):
                    text_caption = None  

                if text_caption:
                    LAST_ALERT = (text_caption, meta)  # lasciato per eventuale uso futuro
                    build_and_send_with_image(text_caption, meta)

                seen[topic] = offset
                if time.time() - last_persist > 10:
                    save_json(SEEN_FILE, seen)
                    last_persist = time.time()
        except Exception as e:
            print("[GCN] consumer_loop exception:", e)
            time.sleep(5)

# ==========================
# CIRCULARS POLLER THREAD
# ==========================
CIRCULARS_URL = "https://gcn.nasa.gov/circulars"
CIRC_POLL_SEC = 120

def parse_circulars_page(html: str) -> List[Tuple[int, str, str]]:
    results: List[Tuple[int, str, str]] = []
    for m in re.finditer(r'href="(/circulars/(\d+))"[^>]*>(.*?)</a>', html, re.I | re.S):
        rel = m.group(1)
        cid = int(m.group(2))
        title = re.sub(r"\s+", " ", m.group(3)).strip()
        url = "https://gcn.nasa.gov" + rel
        results.append((cid, title, url))
    dedup = {}
    for cid, title, url in results:
        dedup[cid] = (cid, title, url)
    out = list(dedup.values())
    out.sort(key=lambda x: x[0], reverse=True)
    return out[:50]

def broadcast_circular(cid: int, title: str, url: str):
    ra_deg, dec_deg, unc, ra_sex, dec_sex = extract_coords_from_circular(url)
    extra = ""
    if ra_deg is not None and dec_deg is not None:
        extra_lines = [
            "📍 <b>Posizione (J2000)</b>",
            f"• RA: {ra_sex}  ({ra_deg:.5f}°)",
            f"• Dec: {dec_sex} ({dec_deg:.5f}°)"
        ]
        if unc is not None:
            extra_lines.append(f"• Uncertainty: ±{unc:.2f}\"")
        extra = "\n" + "\n".join(extra_lines)

    text = f"📝 <b>GCN Circular #{cid}</b>\n{title}\n🔗 {url}{extra}"
    subs = list_subscribers()
    for k, v in subs.items():
        chat_id = int(k)
        entry = get_user_entry(chat_id)
        if entry.get("muted", False):
            continue
        filters = entry.get("filters", default_filters())
        if not filters.get("circulars", False):
            continue
        tg_send_text(chat_id, text)

def circulars_loop():
    state = load_json(CIRC_FILE, {"last_id": 0})
    last_id = int(state.get("last_id", 0))
    print("[GCN] Circulars poller attivo.")
    while True:
        try:
            r = requests.get(CIRCULARS_URL, timeout=30)
            if r.status_code == 200 and r.text:
                items = parse_circulars_page(r.text)
                new_items = [it for it in items if it[0] > last_id]
                for (cid, title, url) in sorted(new_items, key=lambda x: x[0]):
                    broadcast_circular(cid, title, url)
                    last_id = max(last_id, cid)
                if new_items:
                    save_json(CIRC_FILE, {"last_id": last_id})
        except Exception as e:
            print(f"[Circulars] errore poll: {e}")
        time.sleep(CIRC_POLL_SEC)

# ======= Test – recupera l'ultima circular pubblicata =======
def fetch_latest_circular() -> Optional[Tuple[int, str, str]]:
    """Scarica la pagina delle circulars e ritorna (cid, title, url) della più recente."""
    try:
        r = requests.get(CIRCULARS_URL, timeout=30)
        r.raise_for_status()
        items = parse_circulars_page(r.text)
        if items:
            return items[0]  
    except Exception as e:
        print(f"[TestCircular] errore fetch: {e}")
    return None

# ==========================
# UI / COMANDI TELEGRAM
# ==========================
HELP_TEXT = (
    "❓ <b>Aiuto rapido</b>\n\n"
    "• Apri il <b>menu</b> con <code>/menu</code> (trovi le azioni principali).\n"
    "• Con <code>/filtri</code> imposti le sorgenti: 🌊 GW, 🛰️ Swift/Fermi (GRB), 📝 Circulars.\n"
    "• <code>/attivaricezione</code> / <code>/disattivaricezione</code> avviano/sospendono gli alert.\n\n"
    "Di default ricevi <b>solo i trigger GRB Swift/Fermi</b> (GW e Circulars OFF).\n"
)

COMMANDS_PRIMARY_TEXT = (
    "📂 <b>Menu principale</b>\n"
    "• ⚙️ <code>/impostazioni</code> – apri le azioni\n"
    "• 🧪 <code>/testriceviultimagcn</code> – richiedi l’ultima GCN Circular\n"
    "• ❓ <code>/help</code> – guida rapida\n"
    "• 👤 <code>/contattaautore</code> – contatti\n"
)

SUBMENU_TEXT = (
    "⚙️ <b>Impostazioni</b>\n"
    "• ✅ <code>/attivaricezione</code> – ricevi gli alert\n"
    "• 🚫 <code>/disattivaricezione</code> – non ricevere gli alert\n"
    "• ✔️ <code>/filtri</code> – apri i filtri (GW / Swift-Fermi / Circulars)\n"
    "• ℹ️ <code>/status</code> – stato e filtri correnti\n"
)

MAIN_MENU_TEXT = (
    "📂 <b>Menu</b>\n"
    "Scegli un’opzione:\n"
    "• ⚙️ <code>/impostazioni</code>\n"
    "• 🧪 <code>/testriceviultimagcn</code>\n"
    "• ❓ <code>/help</code>\n"
    "• 👤 <code>/contattaautore</code>\n"
)

WELCOME_TEXT = (
    "🌌 <b>Benvenuto nel BOT delle NASA GCN</b>!\n"
    "Qui ricevi notifiche istantanee per le scoperte astrofisiche.\n\n"
    "✨ <b>Impostazione iniziale</b>\n"
    "• 🛰️ <b>Swift/Fermi (GRB)</b>: <i>ATTIVI</i>\n"
    "• 🌊 <b>GW LIGO/Virgo</b>: <i>DISATTIVI</i>\n"
    "• 📝 <b>GCN Circulars</b>: <i>DISATTIVI</i>\n\n"
    "Usa <b>/menu</b> per il menu principale e <b>/impostazioni</b> per le azioni.\n"
    "Buona caccia ai burst! 🚀\n\n"
    + COMMANDS_PRIMARY_TEXT + "\n" + SUBMENU_TEXT
)

def render_filters_text(chat_id: int) -> str:
    f = get_filters(chat_id)
    lines = [
        "⚙️ <b>Filtri attivi</b>",
        f"• 🌊 GW LIGO/Virgo: <b>{'ON' if f.get('gw', False) else 'OFF'}</b>",
        f"• 🛰️ Swift/Fermi (solo GRB): <b>{'ON' if f.get('swiftfermi', True) else 'OFF'}</b>",
        f"• 📝 GCN Circulars: <b>{'ON' if f.get('circulars', False) else 'OFF'}</b>",
        "",
        "Tocca i pulsanti per attivare/disattivare."
    ]
    return "\n".join(lines)

def tg_commands_loop():
    # Aggiunge l'admin se definito
    if ADMIN_CHAT_ID is not None:
        add_subscriber(ADMIN_CHAT_ID)

    # Disattiva qualunque webhook rimasto attivo (evita 409)
    tg_delete_webhook()

    # Banner prima di /start
    tg_set_my_description(
        "👋 Benvenuto! Scrivi /start per avviare il BOT e ricevere gli alert GCN.\n"
        "Di default riceverai i trigger GRB Swift/Fermi. Puoi personalizzare i filtri in qualsiasi momento.",
        short_description="Scrivi /start per avviare"
    )

    # comandi nel tasto Menu di Telegram
    tg_set_my_commands([
        ("menu", "📂 Menu principale"),
        ("testriceviultimagcn", "🧪 Richiedi l’ultima GCN Circular"),
        ("help", "❓ Guida rapida"),
        ("contattaautore", "👤 Contatti"),
        ("impostazioni", "⚙️ Azioni principali"),
    ])

    update_offset = None
    while True:
        data = tg_get_updates(update_offset)
        if not data.get("ok", False):
            time.sleep(2)
            continue

        for upd in data.get("result", []):
            update_offset = upd["update_id"] + 1

            # Callback inline (menu + toggle filtri)
            if "callback_query" in upd:
                cb = upd["callback_query"]
                cb_id = cb.get("id")
                from_id = cb.get("from", {}).get("id")
                data_cb = cb.get("data", "") or ""
                msg = cb.get("message", {}) or {}
                chat_id = msg.get("chat", {}).get("id")
                mid = msg.get("message_id")

                # === 1) Comandi dal MENU INLINE ===
                if data_cb.startswith("cmd:") and from_id and chat_id and mid:
                    cmd = data_cb[4:]

                    if cmd == "/menu":
                        tg_answer_callback_query(cb_id, "")
                        tg_edit_message_text(chat_id, mid, MAIN_MENU_TEXT, reply_markup=keyboard_main_menu()); continue

                    if cmd == "/impostazioni":
                        tg_answer_callback_query(cb_id, "")
                        tg_edit_message_text(chat_id, mid, SUBMENU_TEXT, reply_markup=keyboard_submenu()); continue

                    if cmd == "/testriceviultimagcn":
                        tg_answer_callback_query(cb_id, "⏳ Recupero ultima GCN Circular…")
                        latest = fetch_latest_circular()
                        if latest:
                            cid, title, url = latest
              
                            ra_deg, dec_deg, unc, ra_sex, dec_sex = extract_coords_from_circular(url)
                            extra = ""
                            if ra_deg is not None and dec_deg is not None:
                                extra_lines = [
                                    "📍 <b>Posizione (J2000)</b>",
                                    f"• RA: {ra_sex}  ({ra_deg:.5f}°)",
                                    f"• Dec: {dec_sex} ({dec_deg:.5f}°)"
                                ]
                                if unc is not None:
                                    extra_lines.append(f"• Uncertainty: ±{unc:.2f}\"")
                                extra = "\n" + "\n".join(extra_lines)
          
                            text = f"🧪 <b>Test</b>: ultima GCN Circular\n📝 <b>#{cid}</b> — {title}\n🔗 {url}{extra}"
                            tg_send_text(chat_id, text)
                        else:
                            tg_send_text(chat_id, "ℹ️ Nessuna circular trovata al momento. Riprova tra poco.")
                        continue

                    if cmd == "/help":
                        tg_answer_callback_query(cb_id, "")
                        tg_edit_message_text(chat_id, mid, HELP_TEXT, reply_markup=keyboard_main_menu()); continue

                    if cmd == "/contattaautore":
                        tg_answer_callback_query(cb_id, "")
                        tg_edit_message_text(chat_id, mid, "👤 Contatta l’autore: @antoninobrosio", reply_markup=keyboard_main_menu()); continue

                    if cmd == "/attivaricezione":
                        tg_answer_callback_query(cb_id, "")
                        add_subscriber(from_id); set_muted(from_id, False)
                        tg_edit_message_text(chat_id, mid, "✅ Ricezione attivata. Riceverai gli alert secondo i filtri.", reply_markup=keyboard_submenu()); continue

                    if cmd == "/disattivaricezione":
                        tg_answer_callback_query(cb_id, "")
                        add_subscriber(from_id); set_muted(from_id, True)
                        tg_edit_message_text(chat_id, mid, "🚫 Ricezione disattivata. Usa /attivaricezione per riattivare.", reply_markup=keyboard_submenu()); continue

                    if cmd in ("/filtri", "/filters"):
                        tg_answer_callback_query(cb_id, "")
                        kb = keyboard_filters_inline(get_filters(from_id))
                        tg_edit_message_text(chat_id, mid, render_filters_text(from_id), reply_markup=kb); continue

                    if cmd == "/status":
                        tg_answer_callback_query(cb_id, "")
                        entry = get_user_entry(from_id); muted = entry.get("muted", False)
                        text = f"ℹ️ <b>Stato</b>: {'🛑 Sospeso' if muted else '🟢 Attivo'}\n\n" + render_filters_text(from_id)
                        tg_edit_message_text(chat_id, mid, text, reply_markup=keyboard_submenu()); continue

                    tg_answer_callback_query(cb_id, "")
                    tg_edit_message_text(chat_id, mid, MAIN_MENU_TEXT, reply_markup=keyboard_main_menu()); continue

                # === 2) Toggle filtri ===
                if data_cb.startswith("toggle:") and from_id and chat_id and mid:
                    key = data_cb.split(":", 1)[1]
                    f = get_filters(from_id)
                    if key == "gw":
                        set_filters(from_id, gw=not f.get("gw", False))
                    elif key == "swiftfermi":
                        set_filters(from_id, swiftfermi=not f.get("swiftfermi", True))
                    elif key == "circulars":
                        set_filters(from_id, circulars=not f.get("circulars", False))
                    tg_answer_callback_query(cb_id, "🔄 Filtri aggiornati")
                    new_text = render_filters_text(from_id)
                    new_kb = keyboard_filters_inline(get_filters(from_id))
                    tg_edit_message_text(chat_id, mid, new_text, reply_markup=new_kb)
                    continue

                tg_answer_callback_query(cb_id, "")
                continue

            # Messaggi normali
            msg = upd.get("message") or upd.get("edited_message")
            if not msg or "text" not in msg:
                continue

            chat_id = msg["chat"]["id"]
            text = (msg["text"] or "").strip()
            parts = text.split()
            cmd = parts[0].lower() if parts else ""

            if cmd == "/start":
                add_subscriber(chat_id)
                set_muted(chat_id, False)
                tg_send_text(chat_id, WELCOME_TEXT, reply_markup=keyboard_main_menu())

            elif cmd == "/menu":
                tg_send_text(chat_id, MAIN_MENU_TEXT, reply_markup=keyboard_main_menu())

            elif cmd == "/impostazioni":
                tg_send_text(chat_id, SUBMENU_TEXT, reply_markup=keyboard_submenu())

            elif cmd == "/testriceviultimagcn":
                latest = fetch_latest_circular()
                if latest:
                    cid, title, url = latest
            
                    ra_deg, dec_deg, unc, ra_sex, dec_sex = extract_coords_from_circular(url)
                    extra = ""
                    if ra_deg is not None and dec_deg is not None:
                        extra_lines = [
                            "📍 <b>Posizione (J2000)</b>",
                            f"• RA: {ra_sex}  ({ra_deg:.5f}°)",
                            f"• Dec: {dec_sex} ({dec_deg:.5f}°)"
                        ]
                        if unc is not None:
                            extra_lines.append(f"• Uncertainty: ±{unc:.2f}\"")
                        extra = "\n" + "\n".join(extra_lines)
  
                    text = f"🧪 <b>Test</b>: ultima GCN Circular\n📝 <b>#{cid}</b> — {title}\n🔗 {url}{extra}"
                    tg_send_text(chat_id, text)
                else:
                    tg_send_text(chat_id, "ℹ️ Nessuna circular trovata al momento. Riprova tra poco.")

            elif cmd == "/attivaricezione":
                add_subscriber(chat_id)
                set_muted(chat_id, False)
                tg_send_text(chat_id, "✅ Ricezione attivata. Riceverai gli alert secondo i filtri.", reply_markup=keyboard_submenu())

            elif cmd == "/disattivaricezione":
                add_subscriber(chat_id)
                set_muted(chat_id, True)
                tg_send_text(chat_id, "🚫 Ricezione disattivata. Usa /attivaricezione per riattivare.", reply_markup=keyboard_submenu())

            elif cmd in ("/filtri", "/filters"):
                add_subscriber(chat_id)
                kb = keyboard_filters_inline(get_filters(chat_id))
                tg_send_text(chat_id, render_filters_text(chat_id), reply_markup=kb)

            elif cmd == "/status":
                entry = get_user_entry(chat_id)
                muted = entry.get("muted", False)
                tg_send_text(
                    chat_id,
                    f"ℹ️ <b>Stato</b>: {'🛑 Sospeso' if muted else '🟢 Attivo'}\n\n" + render_filters_text(chat_id),
                    reply_markup=keyboard_submenu()
                )

            elif cmd == "/help":
                tg_send_text(chat_id, HELP_TEXT, reply_markup=keyboard_main_menu())

            elif cmd == "/contattaautore":
                tg_send_text(chat_id, "👤 Contatta l’autore: @antoninobrosio", reply_markup=keyboard_main_menu())

            else:
                tg_send_text(chat_id, "📂 Usa <b>/menu</b> per il menu principale o <b>/impostazioni</b> per le azioni.", reply_markup=keyboard_main_menu())

        time.sleep(1)

# ==========================
# MAIN
# ==========================
def _acquire_single_instance_lock(port: int = 54673) -> Optional[socket.socket]:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", port))
        s.listen(1)
        return s
    except OSError:
        print("[GCN] Un'altra istanza è già in esecuzione (lock TCP occupato).")
        return None

if __name__ == "__main__":
    # Controllo minimo sulle credenziali GCN (consigliato ma non obbligatorio)
    if not CLIENT_ID or not CLIENT_SECRET:
        print("[GCN] Attenzione: GCN CLIENT_ID/CLIENT_SECRET non impostati (env GCN_CLIENT_ID / GCN_CLIENT_SECRET).")
    lock_sock = _acquire_single_instance_lock()
    if lock_sock is None:
        raise SystemExit(1)

    print("✅ GCN BOT avviato. In ascolto…")
    t1 = threading.Thread(target=consumer_loop, daemon=True)
    t1.start()
    t3 = threading.Thread(target=circulars_loop, daemon=True)
    t3.start()
    t2 = threading.Thread(target=tg_commands_loop, daemon=True)
    t2.start()

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nBye.")

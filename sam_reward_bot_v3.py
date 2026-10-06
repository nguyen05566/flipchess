#!/usr/bin/env python3
"""FB Sam loc reward bot v4 — unlimited loop via "not enough xu" trigger.

NO transfer needed. NO daily limit. Just loop:
  1. createTable(25k) → "not enough xu" alert (balance < 450k requirement)
  2. Click "Watch video +N🪙" → claim N coin
  3. Repeat

Reward increases: 900 → 1000 → 1100 → ... → 2000 per cycle.
"""
import os, sys, time

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import board_dom_merged as m
except:
    pass

from playwright.sync_api import sync_playwright

FB_COOKIES = os.environ.get("FB_COOKIES", "").strip()
if not FB_COOKIES:
    FB_COOKIES = "datr=Bu-naoV2DzYAsz945b81Jn1I; sb=Bu-nas9aEnxOeMtOfjOIAbRU; m_pixel_ratio=2; vpd=v1%3B616x360x2; c_user=61561542347462; xs=29%3ACFnA3wEH9B9D3A%3A2%3A1790554583%3A-1%3A-1; locale=en_GB; pas=100051928670915%3AdkPz2ivLwm%2C61561542347462%3AygtS8wYCm5; ps_l=1; ps_n=1; presence=C%7B%22t3%22%3A%5B%5D%2C%22utc3%22%3A1790621986752%2C%22v%22%3A1%7D; wd=360x616; fr=1ZgRRkYX1oP22NBXB.AWdhqJijw632suL5gyEWH6zdQJk2-HKqUlmlD2hniOm6xTA9ops.Bqp-8w..AAA.0.0.Bqurso.AWf_e7mTQsdpD5QFmU18288z6oM; fbl_st=101731726%3BT%3A29843708; wl_cbv=v2%3Bclient_version%3A3306%3Btimestamp%3A1790622504"

GAME_URL = "https://www.facebook.com/gaming/play/sam_loc_vh"
MAX_CYCLES = int(os.environ.get("MAX_CYCLES", "30"))
DELAY = float(os.environ.get("DELAY", "3"))
HEADLESS = os.environ.get("HEADLESS", "true").lower() == "true"


def get_bal(gf):
    try:
        return gf.evaluate("() => document.querySelector('.chipBalance')?.textContent.trim() || '?'")
    except:
        return "?"


def parse_bal(s):
    if not s or s == '?': return 0
    try:
        s = s.lower().replace(',', '').strip()
        if 'k' in s: return int(float(s.replace('k', '')) * 1000)
        if 'm' in s: return int(float(s.replace('m', '')) * 1000000)
        return int(float(s))
    except: return 0


def find_gf(page, max_wait=60):
    for _ in range(max_wait // 5):
        for f in page.frames:
            if "instant-bundle" in f.url and "fbsbx.com" in f.url:
                return f
        time.sleep(5)
    return None


def trigger_and_claim(gf):
    """createTable(25k) → trigger "not enough xu" → click Watch video → claim.
    
    Returns {success, amount, method}.
    """
    # Step 1: createTable + select 25k + submit
    try:
        gf.evaluate("createTable()")
    except: pass
    time.sleep(2)
    
    try:
        gf.evaluate("""() => {
            const r = document.getElementById('radio_11');
            if (r) { r.checked = true; r.dispatchEvent(new Event('change', {bubbles: true})); }
        }""")
    except: pass
    time.sleep(0.5)
    
    try:
        gf.evaluate("""() => {
            const b = document.querySelector('input[name="CREATE"]');
            if (b) b.click();
        }""")
    except: pass
    time.sleep(3)
    
    # Step 2: Check for "not enough xu" alert + click Watch video
    alert_clicked = False
    try:
        alert_clicked = gf.evaluate("""() => {
            const dialogs = document.querySelectorAll('[class*="msgBox"]');
            for (const d of dialogs) {
                if (d.offsetParent !== null && d.textContent.includes('enough coin')) {
                    const buttons = d.querySelectorAll('input[type="button"], button');
                    for (const b of buttons) {
                        const val = (b.value || b.textContent || '').toLowerCase();
                        if (val.includes('watch') || val.includes('video')) {
                            b.click();
                            return true;
                        }
                    }
                }
            }
            return false;
        }""")
    except: pass
    
    if alert_clicked:
        time.sleep(2)
    
    # Step 3: Send VIDEO_REWARD (works both after alert click AND standalone)
    result = gf.evaluate("""() => {
        return new Promise((resolve) => {
            try {
                if (!window.connection || !connection.ws || connection.ws.readyState !== 1) {
                    resolve({success: false, error: 'ws not connected'});
                    return;
                }
                const msg = new OutboundMessage("VIDEO_REWARD");
                msg.writeByte(1);
                let resolved = false;
                connection.send(msg, function(response, success) {
                    if (resolved) return;
                    resolved = true;
                    if (success) {
                        try {
                            const amount = response.readLong();
                            if (window.Ads && window.Ads.RewardedVideo) {
                                window.Ads.RewardedVideo.videoIndex++;
                                if (window.Ads.RewardedVideo.updateRewardButton)
                                    window.Ads.RewardedVideo.updateRewardButton();
                            }
                            resolve({success: true, amount: amount, method: 'alert+reward'});
                        } catch(e) {
                            resolve({success: true, amount: 0, error: e.toString()});
                        }
                    } else {
                        resolve({success: false, error: 'no response'});
                    }
                });
                setTimeout(() => {
                    if (!resolved) { resolved = true; resolve({success: false, error: 'timeout'}); }
                }, 8000);
            } catch(e) {
                resolve({success: false, error: e.toString()});
            }
        });
    }""")
    
    if not result.get('success') or result.get('amount', 0) == 0:
        result['method'] = 'alert_clicked' if alert_clicked else 'no_alert'
    
    return result


def main():
    print(f"Max cycles: {MAX_CYCLES} | Delay: {DELAY}s | Headless: {HEADLESS}", flush=True)

    try:
        fb_cookies = m.parse_cookie_header(FB_COOKIES)
    except:
        import http.cookies
        parsed = http.cookies.SimpleCookie()
        parsed.load(FB_COOKIES)
        fb_cookies = [
            {"name": n, "value": mv.value, "domain": ".facebook.com",
             "path": "/", "secure": True, "httpOnly": False, "sameSite": "Lax"}
            for n, mv in parsed.items() if n and mv.value
        ]

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080}, locale="en-US",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
        )
        for c in fb_cookies:
            c['domain'] = '.facebook.com'
        context.add_cookies(fb_cookies)
        page = context.new_page()

        print("[1] Login FB...", flush=True)
        page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(5000)
        if page.locator('input[placeholder="Email or phone"]').count() > 0:
            print("  ERROR: Not logged in", flush=True)
            browser.close()
            return 1
        print("  OK", flush=True)

        print("[2] Open game...", flush=True)
        page.goto(GAME_URL, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(20000)
        gf = find_gf(page, max_wait=60)
        if not gf:
            print("  ERROR: Game frame not found", flush=True)
            browser.close()
            return 1
        print("  Game loaded", flush=True)
        page.wait_for_timeout(10000)

        # Wait for WS
        print("[3] Wait WS...", flush=True)
        for _ in range(10):
            try:
                if gf.evaluate("() => window.connection && connection.ws && connection.ws.readyState === 1"):
                    print("  WS connected", flush=True)
                    break
            except: pass
            time.sleep(3)

        bal_start = get_bal(gf)
        print(f"  Balance: {bal_start}", flush=True)

        # Main loop
        print(f"\n[4] Reward loop ({MAX_CYCLES} cycles)...", flush=True)
        total = 0
        ok = 0
        fail = 0
        rewards = []

        for i in range(MAX_CYCLES):
            bal_before = get_bal(gf)
            
            # Close any leftover dialog
            try:
                gf.evaluate("$('.msgBoxBackGround,.msgBox').remove()")
            except: pass
            time.sleep(1)

            result = trigger_and_claim(gf)

            if result.get('success') and result.get('amount', 0) > 0:
                amount = result['amount']
                total += amount
                ok += 1
                rewards.append(amount)
                time.sleep(1)
                bal_after = get_bal(gf)
                print(f"  {i+1}: +{amount} | {bal_before} -> {bal_after} | total={total}", flush=True)
                fail = 0  # reset consecutive fails
            else:
                fail += 1
                err = result.get('error', 'unknown')
                print(f"  {i+1}: FAIL ({err}) | {bal_before}", flush=True)

            if fail >= 8:
                print("  Too many fails, stopping", flush=True)
                break

            if i < MAX_CYCLES - 1:
                time.sleep(DELAY)

        # Summary
        bal_end = get_bal(gf)
        print(flush=True)
        print("=" * 50, flush=True)
        print(f"  Cycles:  {ok} ok / {fail} fail / {MAX_CYCLES} max", flush=True)
        print(f"  Reward:  {total} coin", flush=True)
        print(f"  Balance: {bal_start} -> {bal_end}", flush=True)
        print(f"  Rewards: {rewards}", flush=True)
        print("=" * 50, flush=True)

        browser.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())

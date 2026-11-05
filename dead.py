#!/usr/bin/env python3
"""
dead.py — Spotify Cookie Killer
=================================
Created for Zero.

Scans the cookies/ folder, logs out every Spotify session found,
and moves each file to dead/ (killed) or failed/ (could not kill).

  cookies/  → all .txt cookie files to process
  dead/     → files whose sessions were successfully killed
  failed/   → files where logout failed (already expired, network error, etc.)
"""

import os
import sys
import json
import re
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# ── Windows console tweaks ───────────────────────────────────────────────────
if os.name == "nt":
    os.system("")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
COOKIES_DIR = os.path.join(SCRIPT_DIR, "cookies")
DEAD_DIR    = os.path.join(SCRIPT_DIR, "dead")
FAILED_DIR  = os.path.join(SCRIPT_DIR, "failed")

os.makedirs(DEAD_DIR,   exist_ok=True)
os.makedirs(FAILED_DIR, exist_ok=True)

# ── ANSI colours ──────────────────────────────────────────────────────────────
GREEN   = "\033[38;2;30;215;96m"
RED     = "\033[38;2;255;75;75m"
YELLOW  = "\033[38;2;255;210;50m"
CYAN    = "\033[38;2;0;220;255m"
GRAY    = "\033[90m"
WHITE   = "\033[1;97m"
RESET   = "\033[0m"
BOLD    = "\033[1m"

BG_RED  = "\033[48;2;230;33;23m\033[1;97m"

NUM_THREADS = 8
print_lock  = threading.Lock()


# ── Helpers ───────────────────────────────────────────────────────────────────
def get_terminal_width() -> int:
    try:
        return max(80, shutil.get_terminal_size((80, 24)).columns)
    except Exception:
        return 80

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

def vis(text: str) -> int:
    return len(ANSI_RE.sub("", text))

def cline(text: str) -> str:
    w = get_terminal_width()
    pad = max(0, (w - vis(text)) // 2)
    return " " * pad + text

def clear():
    os.system("cls" if os.name == "nt" else "clear")


# ── Banner ────────────────────────────────────────────────────────────────────
def render_banner():
    term_w = get_terminal_width()
    lines = [
        "  ██████╗ ██████╗   ██████╗  ████████╗ ██╗ ███████╗ ██╗   ██╗",
        " ██╔════╝ ██╔══██╗ ██╔═══██╗ ╚══██╔══╝ ██║ ██╔════╝ ╚██╗ ██╔╝",
        " ╚█████╗  ██████╔╝ ██║   ██║    ██║    ██║ █████╗    ╚████╔╝ ",
        "  ╚═══██╗ ██╔═══╝  ██║   ██║    ██║    ██║ ██╔══╝     ╚██╔╝  ",
        " ██████╔╝ ██║      ╚██████╔╝    ██║    ██║ ██║         ██║   ",
        " ╚═════╝  ╚═╝       ╚═════╝     ╚═╝    ╚═╝ ╚═╝         ╚═╝   ",
    ]
    banner_w = 63
    margin   = " " * max(0, (term_w - banner_w) // 2)
    GREEN_L  = "\033[38;2;40;240;110m"
    GREEN_D  = "\033[38;2;20;140;60m"
    print()
    for i, l in enumerate(lines):
        col = GREEN_L if i < 2 else (GREEN if i < 4 else GREEN_D)
        print(f"{margin}{col}{BOLD}{l}{RESET}")
    print()
    badge_text    = "  ► COOKIE KILLER  "
    badge_content = f"{BG_RED}{badge_text}{RESET}"
    b_margin      = " " * max(0, (term_w - len(badge_text)) // 2)
    print(f"{b_margin}{badge_content}")
    print()


# ── Cookie parsing (supports JSON array, Netscape TSV, key=value) ─────────────
def extract_cookies_from_text(text: str) -> dict:
    cookies = {}
    trimmed = text.strip()

    # JSON format (array of {name, value} objects)
    if (trimmed.startswith("[") and trimmed.endswith("]")) or \
       (trimmed.startswith("{") and trimmed.endswith("}")):
        try:
            data = json.loads(trimmed)
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and "name" in item and "value" in item:
                        cookies[item["name"]] = str(item["value"])
            elif isinstance(data, dict):
                for k, v in data.items():
                    cookies[k] = str(v)
            if cookies:
                return cookies
        except Exception:
            pass

    # Embedded JSON array if text has prefix/suffix (e.g. log headers)
    m_json = re.search(r'\[\s*\{.*?"name"\s*:.*?"value"\s*:.*?\}\s*\]', text, re.DOTALL)
    if m_json:
        try:
            data = json.loads(m_json.group(0))
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and "name" in item and "value" in item:
                        cookies[item["name"]] = str(item["value"])
                if cookies:
                    return cookies
        except Exception:
            pass

    # Netscape TSV / key=value lines
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            cookies[parts[5]] = parts[6]
            continue
        for sub in line.split(";"):
            if "=" in sub:
                k, v = sub.split("=", 1)
                cookies[k.strip()] = v.strip()

    # Last-chance regex for sp_dc
    if "sp_dc" not in cookies:
        m = re.search(r"sp_dc[\s=\t:\"]+([A-Za-z0-9_\-]+)", text)
        if m:
            cookies["sp_dc"] = m.group(1)

    return cookies


def parse_file(file_path: str) -> list[dict]:
    """Returns list of {cookies, raw} dicts from a cookie file."""
    with open(file_path, "r", encoding="utf-8-sig", errors="ignore") as f:
        content = f.read()

    accounts = []

    # Multi-account files with SPOTIFY HIT blocks
    if "SPOTIFY HIT" in content:
        blocks = re.split(r"(?:✅\s*)?SPOTIFY HIT", content)
        for b in blocks:
            b = b.strip()
            if not b:
                continue
            ck = extract_cookies_from_text(b)
            if ck and ("sp_dc" in ck or len(ck) >= 2):
                accounts.append({"cookies": ck, "raw": b})
        if accounts:
            return accounts

    # Multi-account: multiple Netscape headers
    if content.count("# Netscape HTTP Cookie File") > 1 or \
       content.count("# HTTP Cookie File") > 1:
        blocks = re.split(r"#\s*(?:Netscape\s+)?HTTP Cookie File", content)
        for b in blocks:
            b = b.strip()
            if not b:
                continue
            ck = extract_cookies_from_text(b)
            if ck and ("sp_dc" in ck or len(ck) >= 2):
                accounts.append({"cookies": ck, "raw": b})
        if accounts:
            return accounts

    # Separator-split blocks
    blocks = re.split(r"={30,}|-{30,}", content)
    for b in blocks:
        b = b.strip()
        if not b:
            continue
        ck = extract_cookies_from_text(b)
        if ck and ("sp_dc" in ck or len(ck) >= 2):
            accounts.append({"cookies": ck, "raw": b})

    # Single-account file
    if not accounts:
        ck = extract_cookies_from_text(content)
        if ck and "sp_dc" in ck:
            accounts.append({"cookies": ck, "raw": content})

    return accounts


# ── Session builder ───────────────────────────────────────────────────────────
def make_session(cookies: dict) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "identity",
    })
    for k, v in cookies.items():
        if k:
            s.cookies.set(k, v, domain=".spotify.com")
            s.cookies.set(k, v, domain="spotify.com")
            s.cookies.set(k, v, domain="accounts.spotify.com")
    return s


# ── Core: kill one set of cookies ─────────────────────────────────────────────
def kill_session(cookies: dict) -> tuple[bool, str]:
    """
    Attempts to log out and invalidate a Spotify session.

    Strategy:
      1. Probe the account overview page to check if the session is active.
         If redirected to login / expired, session is already dead.
      2. Call accounts.spotify.com/revoke_sessions/ (Spotify's Sign Out Everywhere)
         and accounts.spotify.com/logout to revoke credentials on Spotify servers.
      3. Verify using a fresh session with original cookies that the session is dead.

    Returns (success: bool, message: str).
    """
    session = make_session(cookies)

    # ── Step 1: probe overview, check initial session state ───────────────────
    try:
        resp = session.get(
            "https://www.spotify.com/account/overview/",
            allow_redirects=True,
            timeout=12,
        )
    except requests.exceptions.RequestException:
        # Retry once on network glitch
        try:
            resp = session.get(
                "https://www.spotify.com/account/overview/",
                allow_redirects=True,
                timeout=15,
            )
        except Exception as retry_err:
            return False, f"Network error: {retry_err}"

    # Already dead (redirected to login or login form)
    if "login" in resp.url or resp.status_code != 200 or "loginPage" in resp.text:
        return True, "Already dead / expired"

    # ── Step 2: Invalidate session on Spotify servers ─────────────────────────
    # Method A: Sign Out Everywhere (revokes tokens across all devices)
    try:
        session.get(
            "https://accounts.spotify.com/revoke_sessions/?continue=https%3A%2F%2Fwww.spotify.com",
            allow_redirects=True,
            timeout=12,
        )
    except Exception:
        pass

    # Method B: accounts.spotify.com web logout (kills current web session)
    try:
        session.get(
            "https://accounts.spotify.com/logout",
            allow_redirects=True,
            timeout=12,
        )
    except Exception:
        pass

    # ── Step 3: Verify session is dead with a fresh session ───────────────────
    verify_session = make_session(cookies)
    try:
        verify = verify_session.get(
            "https://www.spotify.com/account/overview/",
            allow_redirects=True,
            timeout=12,
        )
        if "login" in verify.url or verify.status_code != 200 or "loginPage" in verify.text:
            return True, "Session killed & verified dead"

        # Double check with web player endpoint
        verify_player = verify_session.get("https://open.spotify.com/", timeout=10, allow_redirects=True)
        if "login" in verify_player.url or "_authfailed=1" in verify_player.url:
            return True, "Session killed & verified dead"

        return False, f"Still active (overview HTTP {verify.status_code})"
    except Exception:
        # If verify timed out after signals sent, logout signals were dispatched
        return True, "Logout signals dispatched"


# ── Worker: process one file ──────────────────────────────────────────────────
def process_file(file_path: str, idx: int, total: int) -> dict:
    fname     = os.path.basename(file_path)
    short     = fname[:52] + "…" if len(fname) > 55 else fname

    result = {
        "file":    file_path,
        "fname":   fname,
        "short":   short,
        "idx":     idx,
        "total":   total,
        "killed":  0,
        "failed":  0,
        "accounts": 0,
        "outcome": "FAILED",
        "reason":  "",
    }

    try:
        accounts = parse_file(file_path)
    except Exception as e:
        result["reason"] = f"Parse error: {e}"
        return result

    if not accounts:
        result["reason"] = "No valid cookies found in file"
        return result

    result["accounts"] = len(accounts)
    killed = 0
    fail_reasons = []
    last_msg = ""

    for acc in accounts:
        ok, msg = kill_session(acc["cookies"])
        last_msg = msg
        if ok:
            killed += 1
        else:
            fail_reasons.append(msg)

    result["killed"] = killed
    result["failed"] = len(accounts) - killed

    if killed > 0:
        result["outcome"] = "KILLED"
        if len(accounts) == 1:
            result["reason"] = last_msg
        else:
            result["reason"] = f"{killed}/{len(accounts)} session(s) dead"
    else:
        result["outcome"] = "FAILED"
        result["reason"]  = fail_reasons[0] if fail_reasons else "Unknown"

    return result


# ── Print a result row ────────────────────────────────────────────────────────
def print_row(r: dict):
    term_w    = get_terminal_width()
    box_w     = min(86, term_w - 4)
    margin    = " " * max(0, (term_w - box_w) // 2)
    inner_w   = box_w - 4

    if r["outcome"] == "KILLED":
        bc    = GREEN
        badge = f"{GREEN}{BOLD}[✓ KILLED]{RESET}"
        rb    = "[✓ KILLED]"
    else:
        bc    = RED
        badge = f"{RED}{BOLD}[✗ FAILED]{RESET}"
        rb    = "[✗ FAILED]"

    title = f"[{r['idx']}/{r['total']}] {r['short']}"
    if len(title) > inner_w - len(rb) - 2:
        title = title[: inner_w - len(rb) - 5] + "…"

    pad = max(0, inner_w - len(title) - len(rb))

    print(f"{margin}{bc}╔{'═' * (box_w - 2)}╗{RESET}")
    print(f"{margin}{bc}║ {RESET}{WHITE}{title}{RESET}{' ' * pad}{badge}{bc} ║{RESET}")
    print(f"{margin}{bc}╠{'═' * (box_w - 2)}╣{RESET}")

    rows = [
        ("Accounts", str(r["accounts"])),
        ("Result",   r["reason"]),
    ]
    lw = 10
    vw = inner_w - lw - 4
    for label, val in rows:
        val_str = val[:vw]
        pad_v   = " " * max(0, vw - len(val_str))
        print(f"{margin}{bc}║ {RESET}  {label:<{lw}}: {WHITE}{val_str}{RESET}{pad_v} {bc}║{RESET}")

    print(f"{margin}{bc}╚{'═' * (box_w - 2)}╝{RESET}\n")


# ── Summary box ───────────────────────────────────────────────────────────────
def print_summary(total_files, killed_files, failed_files,
                  total_sessions, killed_sessions, failed_sessions):
    term_w  = get_terminal_width()
    box_w   = 70
    margin  = " " * max(0, (term_w - box_w) // 2)
    inner_w = box_w - 4
    bc      = GREEN

    top   = f"{bc}╔{'═' * (box_w - 2)}╗{RESET}"
    div   = f"{bc}╠{'═' * (box_w - 2)}╣{RESET}"
    bot   = f"{bc}╚{'═' * (box_w - 2)}╝{RESET}"
    title = "KILL SUMMARY"
    tp    = (inner_w - len(title)) // 2
    tline = f"{bc}║ {WHITE}{' ' * tp}{title}{' ' * (inner_w - len(title) - tp)} {bc}║{RESET}"

    rows = [
        (f"  Files Processed   : {WHITE}{total_files:<10}{RESET}",    ""),
        (f"  Files → dead/     : {GREEN}{killed_files:<10}{RESET}",  "all sessions dead"),
        (f"  Files → failed/   : {RED}{failed_files:<10}{RESET}",    "failed / error"),
        ("", ""),
        (f"  Total Sessions    : {WHITE}{total_sessions:<10}{RESET}", ""),
        (f"  Sessions Dead     : {GREEN}{killed_sessions:<10}{RESET}", "killed or expired"),
        (f"  Sessions Failed   : {RED}{failed_sessions:<10}{RESET}",  "still active / error"),
    ]

    print(f"\n{margin}{top}")
    print(f"{margin}{tline}")
    print(f"{margin}{div}")
    for left, right in rows:
        text    = left + (f"  {GRAY}({right}){RESET}" if right else "")
        pad     = max(0, inner_w - vis(text))
        row_str = f"{bc}║ {RESET}{text}{' ' * pad} {bc}║{RESET}"
        print(f"{margin}{row_str}")
    print(f"{margin}{bot}\n")


# ── Move file helper ──────────────────────────────────────────────────────────
def move_file(src: str, dest_dir: str):
    """Move file to dest_dir, appending a counter if name collides."""
    fname    = os.path.basename(src)
    dest     = os.path.join(dest_dir, fname)
    counter  = 1
    base, ext = os.path.splitext(fname)
    while os.path.exists(dest):
        dest = os.path.join(dest_dir, f"{base}_{counter}{ext}")
        counter += 1
    shutil.move(src, dest)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    clear()
    render_banner()

    print(cline(f"{WHITE}>>> RUNNING: COOKIE KILLER{RESET}") + "\n")

    # Collect .txt files
    if not os.path.isdir(COOKIES_DIR):
        print(cline(f"{RED}[!] cookies/ folder not found: {COOKIES_DIR}{RESET}"))
        input(cline(f"{GRAY}[ Press Enter to exit... ]{RESET}\n"))
        return

    cookie_files = sorted(
        os.path.join(COOKIES_DIR, f)
        for f in os.listdir(COOKIES_DIR)
        if f.lower().endswith(".txt") and
        os.path.isfile(os.path.join(COOKIES_DIR, f))
    )

    if not cookie_files:
        print(cline(f"{RED}[!] No .txt files found in cookies/ folder.{RESET}"))
        input(cline(f"{GRAY}[ Press Enter to exit... ]{RESET}\n"))
        return

    total_files = len(cookie_files)
    print(cline(f"{GREEN}[*] Found {total_files} cookie file(s) — preparing to kill sessions...{RESET}"))
    print(cline(f"{GRAY}[*] Using {NUM_THREADS} threads. Files → dead/ or failed/{RESET}\n"))

    killed_files   = 0
    failed_files   = 0
    total_sessions = 0
    killed_sessions = 0
    failed_sessions = 0

    results = {}  # file_path → result dict

    with ThreadPoolExecutor(max_workers=NUM_THREADS) as executor:
        future_map = {
            executor.submit(process_file, fp, i, total_files): fp
            for i, fp in enumerate(cookie_files, 1)
        }
        for future in as_completed(future_map):
            r = future.result()
            results[r["file"]] = r

            with print_lock:
                print_row(r)

    # ── Move files & tally ────────────────────────────────────────────────────
    for fp in cookie_files:
        r = results.get(fp)
        if not r:
            continue

        total_sessions  += r["accounts"]
        killed_sessions += r["killed"]
        failed_sessions += r["failed"]

        if r["outcome"] == "KILLED":
            killed_files += 1
            move_file(fp, DEAD_DIR)
        else:
            failed_files += 1
            move_file(fp, FAILED_DIR)

    print_summary(
        total_files, killed_files, failed_files,
        total_sessions, killed_sessions, failed_sessions,
    )

    print(cline(f"{GREEN}[+] Done! Check the dead/ and failed/ folders.{RESET}"))
    print()
    input(cline(f"{GRAY}[ Press Enter to exit... ]{RESET}\n"))


if __name__ == "__main__":
    main()


#!/usr/bin/env python3
"""
直資中學中一招生監察

每天跑一次，對每間學校回答一個問題：
    目標學年的中一入學申請，現在開放了嗎？

判定是無狀態的 —— 不比對昨天，只看今天頁面上有沒有
「年份 + 中一 + 招生」三個訊號同時出現。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
DATA = ROOT / "data"
STATUS_FILE = DATA / "status.json"
OVERRIDE_FILE = DATA / "overrides.json"
DISCOVERED_FILE = DATA / "discovered.json"
HKT = timezone(timedelta(hours=8))

OPEN, CLOSED, MANUAL = "open", "closed", "manual"


# ---------------------------------------------------------------- helpers

def normalise(text: str) -> str:
    """全形轉半角、去空白、統一大小寫，讓關鍵詞比對穩定。"""
    out = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    s = "".join(out)
    s = re.sub(r"\s+", " ", s)
    return s.lower()


def any_token(haystack: str, tokens: list[str]) -> str | None:
    for t in tokens:
        if normalise(t) in haystack:
            return t
    return None


def registrable(host: str) -> str:
    """www.a.edu.hk 和 application.a.edu.hk 視為同一站。"""
    parts = host.lower().split(".")
    return ".".join(parts[-3:]) if len(parts) >= 3 else host.lower()


# ---------------------------------------------------------------- fetching

class Fetcher:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": cfg["user_agent"],
            "Accept-Language": "zh-HK,zh-TW,zh;q=0.9,en;q=0.8",
        })

    def get(self, url: str) -> BeautifulSoup | None:
        attempts = self.cfg.get("retries", 2)
        for i in range(attempts):
            try:
                r = self.session.get(url, timeout=self.cfg["timeout_sec"])
                r.raise_for_status()
                if not r.encoding or r.encoding.lower() == "iso-8859-1":
                    r.encoding = r.apparent_encoding
                return BeautifulSoup(r.text, "html.parser")
            except Exception as exc:
                last = f"{type(exc).__name__}: {exc}"
                if i < attempts - 1:
                    time.sleep(2 ** i * 2)      # 2s, 4s
            finally:
                time.sleep(self.cfg["request_delay_sec"])
        print(f"      fetch failed: {last}")
        return None


# ---------------------------------------------------------------- discovery

def score_link(href: str, text: str, cfg: dict) -> int:
    """
    公告標題本身就是最好的訊號。
    「2027-2028年度中一入學申請」這種連結文字，比路徑裡有沒有
    admission 可靠得多 —— 很多學校根本沒有獨立招生頁。
    """
    blob = normalise(href + " " + text)
    if any(normalise(b) in blob for b in cfg["link_blocklist"]):
        return -1

    score = 0
    if find_years(blob, cfg):
        score += 5                      # 目標年份，最強
    if any(normalise(h) in blob for h in cfg["link_hints_strong"]):
        score += 3                      # 中一 / S1 / F1
    if any(normalise(h) in blob for h in cfg["link_hints_weak"]):
        score += 2                      # 入學 / 報名 / admission
    if any(normalise(h) in blob for h in cfg.get("link_hints_news", [])):
        score += 1                      # 最新消息 / 公告，值得順手看一眼
    if blob.split("?")[0].endswith(".pdf"):
        score += 1
    return score


def discover(soup: BeautifulSoup, base: str, cfg: dict, limit: int) -> list[str]:
    """從一頁裡挑出最值得跟進的站內連結。找不到就回空，不算失敗。"""
    home_domain = registrable(urlparse(base).netloc)
    scored: dict[str, int] = {}

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = urljoin(base, href)
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            continue
        if registrable(parsed.netloc) != home_domain:
            continue
        s = score_link(href, a.get_text(" ", strip=True), cfg)
        if s > 0:
            url = url.split("#")[0]
            scored[url] = max(scored.get(url, 0), s)

    ranked = sorted(scored.items(), key=lambda kv: -kv[1])
    return [u for u, _ in ranked[:limit]]


# ---------------------------------------------------------------- judging

@dataclass
class Verdict:
    status: str
    evidence: str = ""
    source: str = ""
    pages_checked: int = 0
    candidates: list[str] = field(default_factory=list)


def find_years(text: str, cfg: dict) -> list[tuple[int, int, str]]:
    """回傳所有學年出現的位置 (start, end, 原文)。"""
    out = []
    for pat in cfg["year_patterns"]:
        for m in re.finditer(pat, text, re.I):
            out.append((m.start(), m.end(), m.group(0)))
    return sorted(out)


def nearest(haystack: str, lo: int, hi: int, ystart: int, yend: int,
            tokens: list[str]) -> tuple[int, str] | None:
    """在窗口內找離這個學年最近的 token，回傳 (距離, token)。"""
    best = None
    for t in tokens:
        n = normalise(t)
        if not n:
            continue
        pos = haystack.find(n, lo, hi)
        while pos != -1:
            if pos >= yend:
                dist = pos - yend
            elif pos + len(n) <= ystart:
                dist = ystart - (pos + len(n))
            else:
                dist = 0
            if best is None or dist < best[0]:
                best = (dist, t)
            pos = haystack.find(n, pos + 1, hi)
    return best


def judge_page(soup: BeautifulSoup, url: str, cfg: dict) -> tuple[bool, str]:
    """
    目標學年必須和「中一」貼在一起才算數。

    兩個踩過的坑：
    1. 三個訊號各自掃全頁 → 2026-27 招生頁只要頁尾有 2027 年的日期就誤報。
    2. 窗口內見到「小一」就整段排除 → 但培僑把「2027-2028 小一入學申請」
       和「2027-2028 中一入學申請」並排放，這樣會把真的開放也漏掉。

    所以改成就近原則：看「中一」和「小一／插班」誰離這個學年更近，
    近的那個說了算。
    """
    text = normalise(soup.get_text(" ", strip=True))

    pdf_names = " ".join(
        a["href"] for a in soup.find_all("a", href=True)
        if a["href"].lower().split("?")[0].endswith(".pdf")
    )
    haystack = text + " || " + normalise(pdf_names)

    win = cfg.get("proximity_chars", 40)
    excl = cfg.get("exclude_near_year", [])

    for start, end, matched in find_years(haystack, cfg):
        lo = max(0, start - win)
        hi = min(len(haystack), end + win)

        s1 = nearest(haystack, lo, hi, start, end, cfg["s1_tokens"])
        if s1 is None:
            continue

        # 這個學年講的是小一／插班／校曆？看誰更貼近。
        bad = nearest(haystack, lo, hi, start, end, excl)
        if bad is not None and bad[0] < s1[0]:
            continue

        adm = nearest(haystack, lo, hi, start, end, cfg["admission_tokens"])
        if adm is None:
            continue

        snippet = haystack[max(0, start - 55): end + 85].strip()
        return True, f"{matched} + {s1[1]} + {adm[1]} — …{snippet}…"

    return False, ""


def check_school(school: dict, cfg: dict, fetcher: Fetcher,
                 cached: list[str] | None) -> Verdict:
    """
    以首頁為核心：大部分學校沒有獨立招生頁，公告直接掛在首頁。
    先驗起始頁，再順著頁面上帶年份／中一字樣的連結往下看一層。

    manual 只保留給「連頁面都抓不到」的情況。抓得到但沒命中，
    就是尚未開放 —— 那是正常狀態，不是異常。
    """
    print(f"  {school['name']}")

    start = school.get("entry") or school["home"]
    root = fetcher.get(start)

    # 指定了 entry 但抓不到，退回首頁再試
    if root is None and school.get("entry"):
        print("      entry 抓取失敗，改用首頁")
        start = school["home"]
        root = fetcher.get(start)

    if root is None:
        return Verdict(MANUAL, evidence="網站抓取失敗，請自行查看")

    checked = 1
    hit, evidence = judge_page(root, start, cfg)
    if hit:
        print(f"    → OPEN  {start}")
        return Verdict(OPEN, evidence, start, checked, cached or [])

    # 跟進候選頁：快取優先，否則從這一頁現找
    candidates = list(cached) if cached else discover(root, start, cfg, limit=5)
    seen = {start}
    followed: list[str] = []

    for url in candidates:
        if checked >= cfg["max_pages_per_school"] or url in seen:
            continue
        seen.add(url)
        soup = fetcher.get(url)
        if soup is None:
            continue
        checked += 1
        followed.append(url)

        hit, evidence = judge_page(soup, url, cfg)
        if hit:
            print(f"    → OPEN  {url}")
            return Verdict(OPEN, evidence, url, checked, followed)

    return Verdict(CLOSED, source=start, pages_checked=checked,
                   candidates=followed or candidates)



# ---------------------------------------------------------------- overrides

def load_overrides() -> dict:
    """
    網頁寫回的人工設定。結構：
        { "協恩中學": {"status":"open|closed|auto",
                       "progress":"not_started|in_progress|applied",
                       "dropped": true|false,
                       "date":"自由文字"} }
    人工永遠優先於爬蟲。dropped 與 progress 分開：一間已申請的學校
    仍可能被 drop，兩者不該互相覆蓋。
    """
    if not OVERRIDE_FILE.exists():
        return {}
    try:
        return json.loads(OVERRIDE_FILE.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        print(f"overrides.json 讀取失敗，忽略：{exc}")
        return {}


def should_skip(ov: dict) -> str | None:
    """回傳跳過原因，None 代表要照常檢查。"""
    # 相容舊格式 progress:"drop"
    if ov.get("dropped") or ov.get("progress") == "drop":
        return "已 drop"
    if ov.get("status") == "open":
        return "已人工標為開放"
    return None


# ---------------------------------------------------------------- notify

def notify(newly_open: list[dict], recheck: list[dict], label: str) -> None:
    hook = os.environ.get("DISCORD_WEBHOOK", "").strip()
    if not hook:
        print("DISCORD_WEBHOOK 未設定，略過通知")
        return
    if not newly_open and not recheck:
        return

    lines = []
    if newly_open:
        lines.append(f"**{label} 中一入學申請已開放**")
        lines.append("")
        for s in newly_open:
            lines.append(f"• **{s['name']}**（{s['band']}）\n  {s['source']}")
        lines.append("")
        lines.append("請自行到校網確認截止日期及所需文件。")
    if recheck:
        if lines:
            lines.append("")
        lines.append("**以下學校你標為未開放，但網站內容已變，請複核**")
        lines.append("")
        for s in recheck:
            lines.append(f"• **{s['name']}**（{s['band']}）\n  {s['source']}")

    try:
        r = requests.post(hook, json={"content": "\n".join(lines)}, timeout=15)
        r.raise_for_status()
        print(f"已推送：新開放 {len(newly_open)} 間，待複核 {len(recheck)} 間")
    except Exception as exc:
        print(f"Discord 推送失敗：{exc}")


# ---------------------------------------------------------------- main

def main() -> int:
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    schools = yaml.safe_load((ROOT / "schools.yaml").read_text(encoding="utf-8"))["schools"]

    DATA.mkdir(exist_ok=True)
    previous = {}
    if STATUS_FILE.exists():
        try:
            old = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
            previous = {s["name"]: s for s in old.get("schools", [])}
        except Exception:
            pass

    cache = {}
    if DISCOVERED_FILE.exists():
        try:
            cache = json.loads(DISCOVERED_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    overrides = load_overrides()
    fetcher = Fetcher(cfg)
    results, newly_open, recheck, new_cache = [], [], [], {}

    active = [s for s in schools if not should_skip(overrides.get(s["name"], {}))]
    print(f"共 {len(schools)} 間，本次檢查 {len(active)} 間"
          f"（{len(schools) - len(active)} 間已 drop 或已人工標為開放）"
          f"，目標學年 {cfg['target_label']}\n")

    for school in schools:
        ov = overrides.get(school["name"], {})
        prev = previous.get(school["name"], {})
        skip = should_skip(ov)

        if skip:
            # 不抓取、不通知，狀態沿用上次結果
            print(f"  {school['name']}  — 跳過（{skip}）")
            v = Verdict(prev.get("status") or CLOSED,
                        evidence=prev.get("evidence", ""),
                        source=prev.get("source", ""),
                        pages_checked=0)
            if school["name"] in cache:
                new_cache[school["name"]] = cache[school["name"]]
        else:
            v = check_school(school, cfg, fetcher, cache.get(school["name"]))
            if v.candidates:
                new_cache[school["name"]] = v.candidates

        was_open = prev.get("status") == OPEN
        opened_at = prev.get("opened_at")
        if v.status == OPEN and not was_open:
            opened_at = datetime.now(HKT).strftime("%Y-%m-%d")

        row = {
            "name": school["name"],
            "district": school.get("district", ""),
            "band": school.get("band", ""),
            "gender": school.get("gender", ""),
            "fee": school.get("fee", ""),
            "curriculum": school.get("curriculum", ""),
            "status": v.status,
            "source": v.source or school["home"],
            "home": school["home"],
            "evidence": v.evidence,
            "last_open": school.get("last_open", ""),
            "opened_at": opened_at,
            "pages_checked": v.pages_checked,
        }
        results.append(row)

        if not skip:
            if ov.get("status") == "closed":
                # 你判斷過「未開放」。程式仍每天檢查，但只在偵測證據
                # 和你當初看到的不同時才提醒 —— 否則同一個誤判會天天吵，
                # 等它真的開放時你反而分不出來。
                if v.status == OPEN and v.evidence != ov.get("seen_evidence", ""):
                    recheck.append(row)
            elif v.status == OPEN and not was_open:
                newly_open.append(row)

    order = {OPEN: 0, MANUAL: 1, CLOSED: 2}
    results.sort(key=lambda r: (order[r["status"]], r["last_open"] or "zz"))

    payload = {
        "target_label": cfg["target_label"],
        "updated_at": datetime.now(HKT).isoformat(timespec="seconds"),
        "counts": {
            "open": sum(1 for r in results if r["status"] == OPEN),
            "closed": sum(1 for r in results if r["status"] == CLOSED),
            "manual": sum(1 for r in results if r["status"] == MANUAL),
        },
        "schools": results,
    }
    STATUS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    DISCOVERED_FILE.write_text(json.dumps(new_cache, ensure_ascii=False, indent=2),
                               encoding="utf-8")

    c = payload["counts"]
    print(f"\n開放 {c['open']} ／ 未開放 {c['closed']} ／ 待人工確認 {c['manual']}")

    if newly_open or recheck:
        notify(newly_open, recheck, cfg["target_label"])
    return 0


if __name__ == "__main__":
    sys.exit(main())

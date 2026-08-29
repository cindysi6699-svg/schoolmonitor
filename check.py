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
    blob = normalise(href + " " + text)
    if any(normalise(b) in blob for b in cfg["link_blocklist"]):
        return -1
    score = 0
    if any(normalise(h) in blob for h in cfg["link_hints_strong"]):
        score += 3
    if any(normalise(h) in blob for h in cfg["link_hints_weak"]):
        score += 2
    # 目標年份直接出現在連結上，是最強訊號
    if any(normalise(y) in blob for y in cfg["year_tokens"]):
        score += 4
    if blob.endswith(".pdf"):
        score += 1
    return score


def discover(soup: BeautifulSoup, base: str, cfg: dict, limit: int) -> list[str]:
    """從一頁裡挑出最像招生頁的若干連結。"""
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


def judge_page(soup: BeautifulSoup, url: str, cfg: dict) -> tuple[bool, str]:
    """一頁上同時見到年份 + 中一 + 招生，就算開放。"""
    text = normalise(soup.get_text(" ", strip=True))

    # PDF 連結的檔名也算頁面內容的一部分
    pdf_names = " ".join(
        a["href"] for a in soup.find_all("a", href=True)
        if a["href"].lower().split("?")[0].endswith(".pdf")
    )
    haystack = text + " " + normalise(pdf_names)

    year = any_token(haystack, cfg["year_tokens"])
    if not year:
        return False, ""
    s1 = any_token(haystack, cfg["s1_tokens"])
    if not s1:
        return False, ""
    adm = any_token(haystack, cfg["admission_tokens"])
    if not adm:
        return False, ""

    # 抓一段包含年份的上下文，方便你人工核對
    idx = haystack.find(normalise(year))
    snippet = haystack[max(0, idx - 60): idx + 90].strip()
    return True, f"{year} + {s1} + {adm} — …{snippet}…"


def check_school(school: dict, cfg: dict, fetcher: Fetcher,
                 cached: list[str] | None) -> Verdict:
    name = school["name"]
    print(f"  {name}")

    # 候選頁：明確指定 > 快取 > 現場發現
    if school.get("entry"):
        candidates = [school["entry"]]
        home_soup = None
    elif cached:
        candidates = list(cached)
        home_soup = None
    else:
        home_soup = fetcher.get(school["home"])
        if home_soup is None:
            return Verdict(MANUAL, evidence="首頁抓取失敗")
        candidates = discover(home_soup, school["home"], cfg, limit=4)
        if not candidates:
            return Verdict(MANUAL, evidence="首頁找不到招生連結（可能為 JS 導覽）",
                           pages_checked=1)

    # 首頁本身也要驗，學校常把公告直接掛在首頁
    queue: list[tuple[str, int]] = [(school["home"], 0)]
    queue += [(u, 1) for u in candidates]

    seen: set[str] = set()
    checked = 0
    discovered_for_cache: list[str] = list(candidates)

    while queue and checked < cfg["max_pages_per_school"]:
        url, depth = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)

        soup = home_soup if (url == school["home"] and home_soup) else fetcher.get(url)
        home_soup = None
        if soup is None:
            continue
        checked += 1

        hit, evidence = judge_page(soup, url, cfg)
        if hit:
            print(f"    → OPEN  {url}")
            return Verdict(OPEN, evidence, url, checked, discovered_for_cache)

        # 第二層：只跟進同時帶「中一」訊號的連結
        if depth < cfg["max_depth"] - 1:
            for nxt in discover(soup, url, cfg, limit=3):
                if nxt not in seen:
                    queue.append((nxt, depth + 1))
                    if nxt not in discovered_for_cache:
                        discovered_for_cache.append(nxt)

    if checked == 0:
        return Verdict(MANUAL, evidence="所有候選頁都抓不到")
    return Verdict(CLOSED, source=school["home"], pages_checked=checked,
                   candidates=discovered_for_cache)


# ---------------------------------------------------------------- notify

def notify(newly_open: list[dict], label: str) -> None:
    hook = os.environ.get("DISCORD_WEBHOOK", "").strip()
    if not hook:
        print("DISCORD_WEBHOOK 未設定，略過通知")
        return

    lines = [f"**{label} 中一入學申請已開放**", ""]
    for s in newly_open:
        lines.append(f"• **{s['name']}**（{s['band']}）\n  {s['source']}")
    lines.append("")
    lines.append("請自行到校網確認截止日期及所需文件。")

    try:
        r = requests.post(hook, json={"content": "\n".join(lines)}, timeout=15)
        r.raise_for_status()
        print(f"已推送 {len(newly_open)} 間學校")
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

    fetcher = Fetcher(cfg)
    results, newly_open, new_cache = [], [], {}

    print(f"檢查 {len(schools)} 間學校，目標學年 {cfg['target_label']}\n")

    for school in schools:
        v = check_school(school, cfg, fetcher, cache.get(school["name"]))
        if v.candidates:
            new_cache[school["name"]] = v.candidates

        prev = previous.get(school["name"], {})
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

        # 只在「首次」轉為開放時通知，避免每天重複騷擾
        if v.status == OPEN and not was_open:
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

    if newly_open:
        notify(newly_open, cfg["target_label"])
    return 0


if __name__ == "__main__":
    sys.exit(main())

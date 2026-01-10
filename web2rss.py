#!/usr/bin/env python3
# web2rss.py — Generate RSS XML files from website list pages (config-driven)

import csv
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from feedgen.feed import FeedGenerator

USER_AGENT = "OrbitwaysWeb2RSS/1.0"
TIMEOUT_S = 30
SLEEP_BETWEEN_REQ = 1.0
MAX_ITEMS_PER_SITE = 80

@dataclass
class SiteCfg:
    name: str
    list_url: str
    link_selector: str
    base_url: str
    date_regex: str
    pages: int

def fetch(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_S)
    r.raise_for_status()
    return r.text

def page_url_for(cfg: SiteCfg, page: int) -> str:
    # Generic strategy: if page==1 use list_url
    # For Payload-like pagination: /page/2/
    if page == 1:
        return cfg.list_url
    # Try the common pattern ".../page/N/"
    if cfg.list_url.endswith("/"):
        return cfg.list_url + f"page/{page}/"
    return cfg.list_url + f"/page/{page}/"

def extract_items(cfg: SiteCfg, html: str):
    soup = BeautifulSoup(html, "html.parser")
    items = []

    links = soup.select(cfg.link_selector)
    date_re = re.compile(cfg.date_regex) if cfg.date_regex else None

    for a in links:
        href = a.get("href")
        if not href:
            continue

        title = a.get_text(" ", strip=True) or "(Untitled)"
        link = urljoin(cfg.base_url, href)

        # Try to find a date in the nearby text (parent block)
        pub_dt = None
        if date_re:
            block = a
            # walk up a bit to catch surrounding metadata
            for _ in range(3):
                if block and block.parent:
                    block = block.parent
            text = block.get_text(" ", strip=True) if block else ""
            m = date_re.search(text)
            if m:
                try:
                    pub_dt = dateparser.parse(m.group(0)).replace(tzinfo=timezone.utc)
                except Exception:
                    pub_dt = None

        items.append({"title": title, "link": link, "pub_dt": pub_dt})

    # Deduplicate by link, preserve order
    seen = set()
    out = []
    for it in items:
        if it["link"] in seen:
            continue
        seen.add(it["link"])
        out.append(it)
    return out

def write_rss(cfg: SiteCfg, items, out_path: Path):
    fg = FeedGenerator()
    fg.id(cfg.list_url)
    fg.title(f"{cfg.name} (Unofficial RSS)")
    fg.link(href=cfg.list_url, rel="alternate")
    fg.link(href=str(out_path), rel="self")
    fg.description(f"Unofficial RSS generated from {cfg.list_url}")
    fg.language("en")
    fg.updated(datetime.now(timezone.utc))

    now = datetime.now(timezone.utc)

    for it in items[:MAX_ITEMS_PER_SITE]:
        fe = fg.add_entry()
        fe.id(it["link"])
        fe.title(it["title"])
        fe.link(href=it["link"])
        if it["pub_dt"]:
            fe.published(it["pub_dt"])
            fe.updated(it["pub_dt"])
        else:
            # keep an RSS-valid published date even if we couldn't parse one
            fe.published(now)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fg.rss_file(str(out_path), pretty=True)


def read_config(csv_path: Path):
    import csv
    import io

    encodings_to_try = ["utf-8-sig", "utf-8", "cp1252", "latin-1"]
    candidate_delims = [",", ";", "\t", "|"]
    required = {"name", "list_url", "link_selector", "base_url"}

    def clean(s: str) -> str:
        return (s or "").replace("\ufeff", "").replace("\u200b", "").replace("\xa0", " ").strip()

    last_err = None

    for enc in encodings_to_try:
        try:
            raw = csv_path.read_text(encoding=enc)

            # Normalize smart quotes
            raw = raw.replace("“", '"').replace("”", '"').replace("„", '"')
            raw = raw.replace("’", "'").replace("‘", "'")

            # Remove empty lines
            lines = [ln for ln in raw.splitlines() if ln.strip()]
            if not lines:
                return []

            # If Excel wrapped the whole header line in quotes, unwrap it
            hdr = lines[0].strip()
            if len(hdr) >= 2 and hdr[0] == '"' and hdr[-1] == '"':
                hdr_unwrapped = hdr[1:-1]
            else:
                hdr_unwrapped = hdr

            # Detect delimiter by which one splits header into required columns
            chosen = None
            for d in candidate_delims:
                cols = [clean(c).strip('"') for c in hdr_unwrapped.split(d)]
                if required.issubset(set(cols)):
                    chosen = d
                    break

            if not chosen:
                raise ValueError(f"Could not detect delimiter. Header seen as: {hdr!r}")

            # If header line was quoted as a whole, replace it with unwrapped version before parsing
            lines2 = lines[:]
            lines2[0] = hdr_unwrapped

            f = io.StringIO("\n".join(lines2))
            reader = csv.reader(
                f,
                delimiter=chosen,
                quotechar='"',
                doublequote=True,
                skipinitialspace=True,
            )

            rows = [r for r in reader if r and any(clean(c) for c in r)]
            if not rows:
                return []

            header = [clean(h) for h in rows[0]]
            idx = {h: i for i, h in enumerate(header)}

            missing = required - set(idx.keys())
            if missing:
                raise ValueError(f"Missing columns {sorted(missing)}. Header parsed as: {header}")

            sites = []
            for r in rows[1:]:
                if len(r) < len(header):
                    r = r + [""] * (len(header) - len(r))

                def get(col):
                    return clean(r[idx[col]]) if idx[col] < len(r) else ""

                name = get("name")
                list_url = get("list_url")
                link_selector = get("link_selector")
                base_url = get("base_url")
                date_regex = get("date_regex") if "date_regex" in idx else ""
                pages_raw = get("pages") if "pages" in idx else "1"

                if not (name and list_url and link_selector and base_url):
                    continue

                try:
                    pages = int(pages_raw) if pages_raw else 1
                except Exception:
                    pages = 1

                sites.append(SiteCfg(
                    name=name,
                    list_url=list_url,
                    link_selector=link_selector,
                    base_url=base_url,
                    date_regex=date_regex,
                    pages=max(1, pages),
                ))

            return sites

        except Exception as e:
            last_err = e

    raise RuntimeError(f"Failed to read CSV '{csv_path}'. Last error: {last_err}")



def main():
    import os

    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="CSV file listing websites to convert to RSS")
    ap.add_argument("--outdir", required=True, help="Output directory for generated RSS XML files")
    args = ap.parse_args()

    cfg_path = Path(args.config)
    outdir = Path(args.outdir)

    sites = read_config(cfg_path)
    print("Loaded sites:", [s.name for s in sites])
    if not sites:
        raise SystemExit("No sites found in config CSV.")

    for cfg in sites:
        all_items = []
        for p in range(1, max(1, cfg.pages) + 1):
            url = page_url_for(cfg, p)
            try:
                html = fetch(url)
                all_items.extend(extract_items(cfg, html))
                time.sleep(SLEEP_BETWEEN_REQ)
            except Exception as e:
                print(f"[{cfg.name}] page {p} failed: {e}")

        # Dedup again across pages
        seen = set()
        uniq = []
        for it in all_items:
            if it["link"] in seen:
                continue
            seen.add(it["link"])
            uniq.append(it)

        out_path = outdir / f"{cfg.name}.xml"
        write_rss(cfg, uniq, out_path)
        print(f"[{cfg.name}] wrote {out_path} ({len(uniq)} items)")

if __name__ == "__main__":
    main()

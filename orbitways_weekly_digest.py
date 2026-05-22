#!/usr/bin/env python3
"""
Orbitways Weekly RSS Digest → PDF → Email

What it does
------------
- Reads RSS feed URLs and keywords/themes from an Excel file.
- Adds RSS files generated locally by web2rss.py.
- Keeps only items published in the last N days, default 7.
- Filters items by keywords.
- Removes duplicates robustly:
  1) canonical URL deduplication,
  2) title deduplication inside the current run,
  3) persistent seen_links.txt deduplication across runs.
- Builds a branded weekly PDF.
- Sends the newsletter to all recipients listed in config/newsletter_recipients.txt.

Excel format expected
---------------------
Column A: RSS source URL
Column B: theme
Column C: keyword
Column D: unused
"""

from __future__ import annotations

import argparse
import hashlib
import mimetypes
import os
import re
import ssl
import smtplib
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import feedparser
import pandas as pd
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

try:
    from readability import Document  # type: ignore

    HAS_READABILITY = True
except Exception:
    HAS_READABILITY = False


# ----------------------------
# Defaults
# ----------------------------
DEFAULT_TIMEOUT_S = 15
USER_AGENT = "OrbitwaysNewsWatch/1.0 (+https://orbitways.com)"
MAX_ITEMS_PER_FEED = 80
MAX_TOTAL_ITEMS = 120
MAX_FETCHED_ARTICLES = 60
SUMMARY_SENTENCES = 3
MIN_KEYWORD_LEN = 2
SEEN_DB_PATH = "seen_links.txt"

TRACKING_QUERY_PREFIXES = (
    "utm_",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "igshid",
    "ref",
)


@dataclass
class DigestItem:
    title: str
    link: str
    published: str
    matched_keywords: List[str]
    theme: str
    summary: str


# ----------------------------
# Input loading
# ----------------------------
def load_env() -> None:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(script_dir, "email_parameters.env")
    if os.path.exists(env_path):
        load_dotenv(dotenv_path=env_path, override=True)


def load_sheet_with_themes(xlsx_path: str, sheet_name: Optional[str] = None) -> Tuple[List[str], Dict[str, str], List[str]]:
    df = pd.read_excel(xlsx_path, sheet_name=0 if sheet_name is None else sheet_name, header=None)

    sources = df.iloc[:, 0].dropna().astype(str).str.strip().tolist()
    sources = [s for s in sources if s]

    kw_series = df.iloc[:, 2] if df.shape[1] > 2 else pd.Series(dtype=str)
    th_series = df.iloc[:, 1] if df.shape[1] > 1 else pd.Series(dtype=str)

    keyword_to_theme: Dict[str, str] = {}
    for kw, th in zip(kw_series, th_series):
        if pd.isna(kw):
            continue
        kw = str(kw).strip()
        if not kw or len(kw) < MIN_KEYWORD_LEN:
            continue

        theme = str(th).strip() if not pd.isna(th) else "Other"
        keyword_to_theme[kw] = theme if theme else "Other"

    keywords = list(keyword_to_theme.keys())
    return sources, keyword_to_theme, keywords


def load_recipients(recipients_file: Optional[str], fallback_env: str = "OW_MAIL_TO") -> List[str]:
    recipients: List[str] = []

    if recipients_file:
        path = Path(recipients_file)
        if path.exists():
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                recipients.extend([x.strip() for x in line.split(",") if x.strip()])

    if not recipients:
        raw = os.environ.get(fallback_env, "")
        recipients.extend([x.strip() for x in raw.split(",") if x.strip()])

    # Preserve order, remove duplicates case-insensitively.
    seen: Set[str] = set()
    clean: List[str] = []
    for email in recipients:
        key = email.lower()
        if key not in seen:
            clean.append(email)
            seen.add(key)

    return clean


# ----------------------------
# RSS and matching
# ----------------------------
def compile_keyword_regex(keywords: List[str]) -> re.Pattern:
    parts = []
    for kw in keywords:
        kw_clean = kw.strip()
        if not kw_clean:
            continue
        escaped = re.escape(kw_clean)
        if re.fullmatch(r"[A-Za-z0-9_]+", kw_clean):
            parts.append(rf"\b{escaped}\b")
        else:
            parts.append(escaped)
    return re.compile("|".join(parts) if parts else r"$^", flags=re.IGNORECASE)


def rss_entries(feed_url: str) -> List[dict]:
    try:
        if os.path.exists(feed_url):
            with open(feed_url, "rb") as f:
                return feedparser.parse(f).entries or []
        return feedparser.parse(feed_url).entries or []
    except Exception:
        return []


def entry_datetime(entry: dict) -> Optional[datetime]:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def entry_text(entry: dict) -> str:
    title = entry.get("title", "") or ""
    summary = entry.get("summary", "") or ""
    desc = entry.get("description", "") or ""
    return f"{title}\n{summary}\n{desc}"


def entry_link(entry: dict) -> str:
    return entry.get("link", "") or ""


def entry_published(entry: dict) -> str:
    return (entry.get("published", "") or entry.get("updated", "") or "").strip()


def match_keywords(pattern: re.Pattern, text: str, keywords: List[str]) -> List[str]:
    if not pattern.search(text):
        return []
    low = text.lower()
    return [kw for kw in keywords if kw.lower() in low]


def themes_for_hits(hits: List[str], keyword_to_theme: Dict[str, str]) -> List[str]:
    out: List[str] = []
    seen: Set[str] = set()
    for kw in hits:
        theme = keyword_to_theme.get(kw, "Other")
        if theme not in seen:
            out.append(theme)
            seen.add(theme)
    return out


def primary_theme(hits: List[str], keyword_to_theme: Dict[str, str]) -> str:
    for kw in hits:
        if kw in keyword_to_theme:
            return keyword_to_theme[kw]
    return "Other"


# ----------------------------
# Deduplication helpers
# ----------------------------
def canonicalize_url(url: str) -> str:
    """Normalize URLs so the same article with tracking params is not duplicated."""
    try:
        parsed = urlparse(url.strip())
        scheme = parsed.scheme.lower() or "https"
        netloc = parsed.netloc.lower().replace("www.", "")
        path = re.sub(r"/+$", "", parsed.path or "")

        kept_query = []
        for k, v in parse_qsl(parsed.query, keep_blank_values=False):
            key_l = k.lower()
            if key_l.startswith(TRACKING_QUERY_PREFIXES):
                continue
            kept_query.append((k, v))
        query = urlencode(sorted(kept_query))

        return urlunparse((scheme, netloc, path, "", query, ""))
    except Exception:
        return url.strip()


def normalize_title(title: str) -> str:
    title = BeautifulSoup(title or "", "html.parser").get_text(" ", strip=True)
    title = title.lower()
    title = re.sub(r"[^a-z0-9àâçéèêëîïôûùüÿñæœ\s-]", " ", title)
    title = re.sub(r"\s+", " ", title).strip()
    # Avoid over-aggressive dedup on very short titles.
    return title if len(title) >= 20 else ""


def stable_item_hash(url: str) -> str:
    return hashlib.sha256(canonicalize_url(url).encode("utf-8")).hexdigest()


def load_seen_hashes(path: str) -> Set[str]:
    if not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())


def append_seen_hashes(path: str, hashes: Set[str]) -> None:
    if not hashes:
        return
    existing = load_seen_hashes(path)
    new_hashes = sorted(h for h in hashes if h not in existing)
    if not new_hashes:
        return
    with open(path, "a", encoding="utf-8") as f:
        for h in new_hashes:
            f.write(h + "\n")


# ----------------------------
# Article summaries
# ----------------------------
def fetch_article_text(url: str) -> str:
    headers = {"User-Agent": USER_AGENT}
    r = requests.get(url, headers=headers, timeout=DEFAULT_TIMEOUT_S)
    r.raise_for_status()
    html = r.text

    if HAS_READABILITY:
        doc = Document(html)
        main_html = doc.summary(html_partial=True)
        soup = BeautifulSoup(main_html, "html.parser")
    else:
        soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "aside"]):
        tag.decompose()

    text = soup.get_text(separator=" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def extractive_summary(text: str, n_sentences: int = SUMMARY_SENTENCES) -> str:
    if not text:
        return ""
    sents = re.split(r"(?<=[.!?])\s+", text)
    out = []
    for s in sents:
        s = s.strip()
        if len(s) < 40:
            continue
        out.append(s)
        if len(out) >= n_sentences:
            break
    return " ".join(out) if out else (text[:400] + ("…" if len(text) > 400 else ""))


def domain_from_url(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return ""


def clamp_text(s: str, max_chars: int) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    if len(s) <= max_chars:
        return s
    return s[:max_chars].rsplit(" ", 1)[0] + "…"


# ----------------------------
# PDF generation
# ----------------------------
def build_pdf(items: List[DigestItem], pdf_path: str, title: str, logo_path: Optional[str] = None) -> None:
    NAVY = colors.HexColor("#0B1F3A")
    TEAL = colors.HexColor("#19A7A6")
    CARD_BG = colors.white
    GREY = colors.HexColor("#6B7280")
    LINK = colors.HexColor("#1a73e8")

    theme_color = {
        "Space Safety / SSA / STM": colors.HexColor("#2563EB"),
        "Space Debris / Deorbit / ADR": colors.HexColor("#7C3AED"),
        "Regulation & Policy": colors.HexColor("#F59E0B"),
        "Insurance & Risk": colors.HexColor("#DC2626"),
        "Space Weather": colors.HexColor("#059669"),
        "Satellite Operations": colors.HexColor("#0EA5E9"),
        "Competitors": colors.HexColor("#9333EA"),
        "General Space News": colors.HexColor("#64748B"),
        "News": TEAL,
        "Other": colors.HexColor("#64748B"),
    }

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="HeaderTitle", fontSize=18, leading=22, textColor=colors.white))
    styles.add(ParagraphStyle(name="HeaderMeta", fontSize=9.5, leading=12, textColor=colors.white))
    styles.add(ParagraphStyle(name="ThemeTitle", fontSize=12.5, leading=16, textColor=colors.white))
    styles.add(ParagraphStyle(name="CardTitle", fontSize=10.5, leading=13, textColor=colors.black, spaceAfter=2))
    styles.add(ParagraphStyle(name="CardMeta", fontSize=8.5, leading=11, textColor=GREY, spaceAfter=3))
    styles.add(ParagraphStyle(name="CardBody", fontSize=9.5, leading=12, textColor=colors.black, spaceAfter=4))
    styles.add(ParagraphStyle(name="CardLink", fontSize=9, leading=11, textColor=LINK))

    doc = SimpleDocTemplate(
        pdf_path,
        pagesize=A4,
        leftMargin=1.6 * cm,
        rightMargin=1.6 * cm,
        topMargin=1.2 * cm,
        bottomMargin=1.5 * cm,
    )

    grouped: Dict[str, List[DigestItem]] = defaultdict(list)
    for item in items:
        grouped[item.theme or "News"].append(item)

    theme_order = sorted(grouped.keys(), key=lambda s: (s == "Other", s.lower()))
    page_width, _ = A4
    usable_width = page_width - doc.leftMargin - doc.rightMargin
    gap = 0.5 * cm
    col_w = (usable_width - gap) / 2.0

    story = []
    today = datetime.now().strftime("%d %B %Y")
    header_left = [
        Paragraph(title, styles["HeaderTitle"]),
        Spacer(1, 2),
        Paragraph(f"Weekly review · {today} · {len(items)} items", styles["HeaderMeta"]),
    ]

    header_right = []
    if logo_path and os.path.exists(logo_path):
        try:
            img = ImageReader(logo_path)
            iw, ih = img.getSize()
            logo = Image(logo_path)
            logo.drawHeight = 1.3 * cm
            logo.drawWidth = logo.drawHeight * (iw / ih)
            header_right = [logo]
        except Exception:
            header_right = []

    header_table = Table([[header_left, header_right]], colWidths=[usable_width * 0.78, usable_width * 0.22])
    header_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), NAVY),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 10))

    def card_for_item(it: DigestItem, accent_color):
        summary = clamp_text(it.summary or "", 240)
        published = it.published or ""
        date_str = published.split("T")[0] if "T" in published else (published[:10] if published else "")
        source = domain_from_url(it.link or "")
        title_txt = clamp_text(it.title or "(Untitled)", 105)
        safe_link = (it.link or "").replace("'", "%27")

        content = [
            Paragraph(f"<b>{title_txt}</b>", styles["CardTitle"]),
            Paragraph(f"{source} · {date_str}", styles["CardMeta"]) if (source or date_str) else Spacer(1, 0),
            Paragraph(summary or "No summary available.", styles["CardBody"]),
            Paragraph(f"<a href='{safe_link}'>Read full article →</a>", styles["CardLink"]) if safe_link else Spacer(1, 0),
        ]

        card = Table([["", content]], colWidths=[0.18 * cm, col_w - 0.18 * cm])
        card.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, 0), accent_color),
            ("BACKGROUND", (1, 0), (1, 0), CARD_BG),
            ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#D7DEE8")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (1, 0), (1, 0), 10),
            ("RIGHTPADDING", (1, 0), (1, 0), 10),
            ("TOPPADDING", (1, 0), (1, 0), 8),
            ("BOTTOMPADDING", (1, 0), (1, 0), 8),
            ("LEFTPADDING", (0, 0), (0, 0), 0),
            ("RIGHTPADDING", (0, 0), (0, 0), 0),
            ("TOPPADDING", (0, 0), (0, 0), 0),
            ("BOTTOMPADDING", (0, 0), (0, 0), 0),
        ]))
        return card

    if not items:
        story.append(Paragraph("No relevant articles were found for this period.", styles["CardBody"]))
    else:
        for theme in theme_order:
            accent = theme_color.get(theme, TEAL)
            theme_bar = Table([[Paragraph(theme, styles["ThemeTitle"])]], colWidths=[usable_width])
            theme_bar.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), accent),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]))
            story.append(theme_bar)
            story.append(Spacer(1, 8))

            cards = [card_for_item(it, accent) for it in grouped[theme]]
            rows = []
            for i in range(0, len(cards), 2):
                left = cards[i]
                right = cards[i + 1] if i + 1 < len(cards) else Spacer(col_w, 1)
                rows.append([left, right])

            grid = Table(rows, colWidths=[col_w, col_w], hAlign="LEFT")
            grid.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]))
            story.append(grid)
            story.append(Spacer(1, 10))

    doc.build(story)


# ----------------------------
# Email
# ----------------------------
def send_email_with_attachment(
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    mail_from: str,
    mail_to_list: List[str],
    subject: str,
    body_text: str,
    attachment_path: str,
) -> None:
    msg = EmailMessage()
    msg["From"] = mail_from
    msg["To"] = ", ".join(mail_to_list)
    msg["Subject"] = subject
    msg.set_content(body_text)

    ctype, encoding = mimetypes.guess_type(attachment_path)
    if ctype is None or encoding is not None:
        ctype = "application/octet-stream"
    maintype, subtype = ctype.split("/", 1)

    with open(attachment_path, "rb") as f:
        data = f.read()

    msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=os.path.basename(attachment_path))

    context = ssl.create_default_context()
    if smtp_port == 465:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=60, context=context) as server:
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
    else:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=60) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)


# ----------------------------
# Main
# ----------------------------
def main() -> None:
    load_env()

    ap = argparse.ArgumentParser()
    ap.add_argument("--xlsx", required=True, help="Path to Excel file (.xlsx): col A feed URL, col B theme, col C keyword")
    ap.add_argument("--sheet", default=None, help="Optional sheet name")
    ap.add_argument("--outdir", default="output", help="Output directory for PDF")
    ap.add_argument("--days", default=7, type=int, help="Number of past days to include")
    ap.add_argument("--recipients-file", default="config/newsletter_recipients.txt", help="One email recipient per line")
    ap.add_argument("--local-rss", action="append", default=[], help="Path to a local RSS XML file; can be repeated")
    ap.add_argument("--local-rss-dir", help="Directory containing local RSS XML files")
    args = ap.parse_args()

    now_utc = datetime.now(timezone.utc)
    cutoff_utc = now_utc - timedelta(days=args.days)

    feeds, keyword_to_theme, keywords = load_sheet_with_themes(args.xlsx, args.sheet)

    if args.local_rss_dir:
        rss_dir = Path(args.local_rss_dir)
        if rss_dir.is_dir():
            feeds.extend(str(p) for p in sorted(rss_dir.glob("*.xml")))

    for p in args.local_rss or []:
        p = str(p).strip()
        if p and os.path.exists(p):
            feeds.append(p)

    # Remove duplicated feed URLs/paths while preserving order.
    feeds = list(dict.fromkeys(feeds))

    if not feeds:
        raise SystemExit("No RSS feeds found in column A or local RSS directory.")
    if not keywords:
        raise SystemExit("No keywords found in column C.")

    kw_re = compile_keyword_regex(keywords)
    seen_hashes = load_seen_hashes(SEEN_DB_PATH)
    newly_seen: Set[str] = set()
    run_urls: Set[str] = set()
    run_titles: Set[str] = set()
    matched_entries: List[Tuple[dict, List[str], List[str]]] = []

    for feed in feeds:
        entries = rss_entries(feed)[:MAX_ITEMS_PER_FEED]
        for entry in entries:
            dt = entry_datetime(entry)
            if dt is None:
                continue
            if dt < cutoff_utc or dt > now_utc:
                continue

            raw_link = entry_link(entry)
            if not raw_link or not raw_link.startswith("http"):
                continue

            canon_url = canonicalize_url(raw_link)
            item_hash = stable_item_hash(raw_link)
            norm_title = normalize_title(entry.get("title", "") or "")

            if item_hash in seen_hashes or item_hash in newly_seen:
                continue
            if canon_url in run_urls:
                continue
            if norm_title and norm_title in run_titles:
                continue

            text = entry_text(entry)
            hits = match_keywords(kw_re, text, keywords)
            if not hits:
                continue

            matched_entries.append((entry, hits, themes_for_hits(hits, keyword_to_theme)))
            newly_seen.add(item_hash)
            run_urls.add(canon_url)
            if norm_title:
                run_titles.add(norm_title)

    matched_entries = matched_entries[:MAX_TOTAL_ITEMS]

    items: List[DigestItem] = []
    fetch_count = 0
    for entry, hits, _themes in matched_entries:
        title = (entry.get("title", "") or "").strip() or "(Untitled)"
        link = entry_link(entry)
        published = entry_published(entry)
        theme = primary_theme(hits, keyword_to_theme)

        summary = ""
        if fetch_count < MAX_FETCHED_ARTICLES:
            try:
                article_text = fetch_article_text(link)
                summary = extractive_summary(article_text, SUMMARY_SENTENCES)
                fetch_count += 1
            except Exception:
                summary = (entry.get("summary", "") or entry.get("description", "") or "").strip()
                if summary:
                    summary = BeautifulSoup(summary, "html.parser").get_text(" ", strip=True)
                    summary = re.sub(r"\s+", " ", summary).strip()
                    summary = summary[:500] + ("…" if len(summary) > 500 else "")

        items.append(DigestItem(
            title=title,
            link=link,
            published=published,
            matched_keywords=hits,
            theme=theme,
            summary=summary,
        ))

    append_seen_hashes(SEEN_DB_PATH, newly_seen)

    os.makedirs(args.outdir, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    pdf_path = os.path.join(args.outdir, f"Orbitways_Weekly_News_{date_str}.pdf")

    script_dir = os.path.dirname(os.path.abspath(__file__))
    logo_path = os.path.join(script_dir, "orbitways_logo.png")

    build_pdf(items, pdf_path, title="Orbitways – Weekly Space News", logo_path=logo_path)
    print(f"PDF written: {pdf_path} (items: {len(items)})")

    smtp_host = os.environ.get("OW_SMTP_HOST")
    smtp_port = int(os.environ.get("OW_SMTP_PORT", "465"))
    smtp_user = os.environ.get("OW_SMTP_USER")
    smtp_pass = os.environ.get("OW_SMTP_PASS")
    mail_from = os.environ.get("OW_MAIL_FROM", smtp_user or "")
    recipients = load_recipients(args.recipients_file)

    if not all([smtp_host, smtp_user, smtp_pass, mail_from, recipients]):
        raise RuntimeError(
            "Missing SMTP configuration or recipients. Required: "
            "OW_SMTP_HOST, OW_SMTP_USER, OW_SMTP_PASS, OW_MAIL_FROM, "
            "and config/newsletter_recipients.txt or OW_MAIL_TO."
        )

    subject = f"Orbitways Weekly Space Review — {date_str}"
    body = (
        "Hi,\n\n"
        "Please find attached this week’s Orbitways Weekly Space Review.\n\n"
        "— Orbitways News Bot\n"
    )

    send_email_with_attachment(
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_user=smtp_user,
        smtp_password=smtp_pass,
        mail_from=mail_from,
        mail_to_list=recipients,
        subject=subject,
        body_text=body,
        attachment_path=pdf_path,
    )

    print(f"Email sent to {len(recipients)} recipient(s): {', '.join(recipients)}")


if __name__ == "__main__":
    main()

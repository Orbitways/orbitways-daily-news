#!/usr/bin/env python3
"""
Orbitways Daily RSS Digest → PDF (+ optional email)
- Reads Excel: col A = RSS feed URL, col B = keyword
- Filters RSS items by keywords
- Fetches article HTML and makes a short summary (simple extractive)
- Builds a single PDF digest
"""

from __future__ import annotations
from datetime import datetime, date, timezone

import os
import re
import ssl
import smtplib
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Dict, Set, Optional, Tuple

import pandas as pd
import feedparser
import requests
import smtplib
import mimetypes
from email.message import EmailMessage

from bs4 import BeautifulSoup

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, KeepTogether
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

from dotenv import load_dotenv
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, "email_parameters.env")

load_dotenv(dotenv_path=ENV_PATH, override=True)

# Optional: readability for cleaner main-text extraction
try:
    from readability import Document  # type: ignore
    HAS_READABILITY = True
except Exception:
    HAS_READABILITY = False


# ----------------------------
# Config (edit as needed)
# ----------------------------
DEFAULT_TIMEOUT_S = 15
USER_AGENT = "OrbitwaysNewsWatch/1.0 (+https://example.com)"
MAX_ITEMS_PER_FEED = 30
MAX_TOTAL_ITEMS = 80          # cap output volume
MAX_FETCHED_ARTICLES = 40     # cap HTTP fetches for summaries

SUMMARY_SENTENCES = 3         # extractive summary length
MIN_KEYWORD_LEN = 2

# If you want dedup across days, set a file path:
SEEN_DB_PATH = "seen_links.txt"  # stores hashed URLs, append-only


@dataclass
class DigestItem:
    title: str
    link: str
    published: str
    matched_keywords: List[str]
    theme: str
    summary: str


def entry_datetime(entry: dict) -> Optional[datetime]:
    """
    Return a timezone-aware datetime for the RSS entry if possible.
    Tries published_parsed, then updated_parsed.
    """
    # feedparser provides *_parsed as time.struct_time
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None
    
from collections import defaultdict

def load_sheet_with_themes(xlsx_path: str, sheet_name: Optional[str] = None):
    """
    Column mapping:
    A (0): source URL
    B (1): theme
    C (2): keyword
    D (3): unused
    """
    df = pd.read_excel(
        xlsx_path,
        sheet_name=0 if sheet_name is None else sheet_name,
        header=None
    )

    # Column A — sources
    sources = df.iloc[:, 0].dropna().astype(str).str.strip().tolist()
    sources = [s for s in sources if s]

    # Column C — keywords (index 2)
    kw_series = df.iloc[:, 2] if df.shape[1] > 2 else pd.Series(dtype=str)

    # Column B — themes (index 1)
    th_series = df.iloc[:, 1] if df.shape[1] > 1 else pd.Series(dtype=str)

    keyword_to_theme = {}
    for kw, th in zip(kw_series, th_series):
        if pd.isna(kw):
            continue

        kw = str(kw).strip()
        if not kw or len(kw) < MIN_KEYWORD_LEN:
            continue

        theme = str(th).strip() if not pd.isna(th) else "Other"
        theme = theme if theme else "Other"

        keyword_to_theme[kw] = theme

    keywords = list(keyword_to_theme.keys())
    return sources, keyword_to_theme, keywords


def compile_keyword_regex(keywords: List[str]) -> re.Pattern:
    """
    Build a regex that matches any keyword (case-insensitive).
    Escapes keywords so special regex chars won't break matching.
    Uses word boundaries when keyword is alnum-ish; for phrases, boundary is looser.
    """
    parts = []
    for kw in keywords:
        kw_clean = kw.strip()
        if not kw_clean:
            continue
        escaped = re.escape(kw_clean)
        # If keyword is a simple word/acronym, enforce word boundaries
        if re.fullmatch(r"[A-Za-z0-9_]+", kw_clean):
            parts.append(rf"\b{escaped}\b")
        else:
            parts.append(escaped)
    combined = "|".join(parts) if parts else r"$^"  # match nothing if empty
    return re.compile(combined, flags=re.IGNORECASE)


def load_seen_hashes(path: str) -> Set[str]:
    if not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())


def append_seen_hashes(path: str, hashes: Set[str]) -> None:
    if not hashes:
        return
    with open(path, "a", encoding="utf-8") as f:
        for h in sorted(hashes):
            f.write(h + "\n")


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def rss_entries(feed_url: str) -> List[dict]:
    """
    Accepts:
    - online RSS URLs (http/https)
    - local RSS XML file paths (Windows/macOS/Linux)
    """
    try:
        # Local file?
        if os.path.exists(feed_url):
            with open(feed_url, "rb") as f:
                return feedparser.parse(f).entries or []

        # Online URL
        return feedparser.parse(feed_url).entries or []
    except Exception:
        return []



def entry_text(entry: dict) -> str:
    title = entry.get("title", "") or ""
    summary = entry.get("summary", "") or ""
    desc = entry.get("description", "") or ""
    return f"{title}\n{summary}\n{desc}"


def entry_link(entry: dict) -> str:
    return entry.get("link", "") or ""


def entry_published(entry: dict) -> str:
    # feedparser often has published or updated
    return (entry.get("published", "") or entry.get("updated", "") or "").strip()


def match_keywords(pattern: re.Pattern, text: str, keywords: List[str]) -> List[str]:
    """
    Return a list of keywords that appear in text (case-insensitive).
    We do a fast pattern test first, then confirm per keyword.
    """
    if not pattern.search(text):
        return []
    hits = []
    low = text.lower()
    for kw in keywords:
        if kw.lower() in low:
            hits.append(kw)
    return hits


def fetch_article_text(url: str) -> str:
    headers = {"User-Agent": USER_AGENT}
    r = requests.get(url, headers=headers, timeout=DEFAULT_TIMEOUT_S)
    r.raise_for_status()
    html = r.text

    # If available, use readability to extract main content
    if HAS_READABILITY:
        doc = Document(html)
        main_html = doc.summary(html_partial=True)
        soup = BeautifulSoup(main_html, "html.parser")
    else:
        soup = BeautifulSoup(html, "html.parser")

    # Remove scripts/styles/nav-like noise
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "aside"]):
        tag.decompose()

    text = soup.get_text(separator=" ", strip=True)
    # Basic cleanup
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extractive_summary(text: str, n_sentences: int = SUMMARY_SENTENCES) -> str:
    """
    Very simple extractive summary:
    - split into sentences
    - take the first N non-trivial sentences
    This is not as good as an LLM, but it's fully offline and deterministic.
    """
    if not text:
        return ""
    # crude sentence split
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


from urllib.parse import urlparse
import re

def domain_from_url(url: str) -> str:
    try:
        netloc = urlparse(url).netloc.lower()
        return netloc.replace("www.", "")
    except Exception:
        return ""

def clamp_text(s: str, max_chars: int) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    if len(s) <= max_chars:
        return s
    return s[:max_chars].rsplit(" ", 1)[0] + "…"


def build_pdf(items, pdf_path: str, title: str, logo_path: str = None) -> None:
    from datetime import datetime
    from collections import defaultdict
    from urllib.parse import urlparse

    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image
    )
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

    # ---- Brand palette (tweak if you want) ----
    NAVY = colors.HexColor("#0B1F3A")
    TEAL = colors.HexColor("#19A7A6")
    LIGHT_BG = colors.HexColor("#F4F7FB")
    CARD_BG = colors.white
    GREY = colors.HexColor("#6B7280")
    LINK = colors.HexColor("#1a73e8")

    # Theme → accent color mapping (optional; falls back to TEAL)
    THEME_COLOR = {
        "Space Safety / SSA / STM": colors.HexColor("#2563EB"),      # blue
        "Space Debris / Deorbit / ADR": colors.HexColor("#7C3AED"),  # purple
        "Regulation & Policy": colors.HexColor("#F59E0B"),           # amber
        "Insurance & Risk": colors.HexColor("#DC2626"),              # red
        "Space Weather": colors.HexColor("#059669"),                 # green
        "Satellite Operations": colors.HexColor("#0EA5E9"),          # sky
        "Competitors": colors.HexColor("#9333EA"),                   # violet
        "General Space News": colors.HexColor("#64748B"),            # slate
        "News": TEAL,
        "Other": colors.HexColor("#64748B"),
    }

    def domain_from_url(url: str) -> str:
        try:
            netloc = urlparse(url).netloc.lower()
            return netloc.replace("www.", "")
        except Exception:
            return ""

    def clamp_text(s: str, max_chars: int) -> str:
        import re
        s = (s or "").strip()
        s = re.sub(r"\s+", " ", s)
        if len(s) <= max_chars:
            return s
        return s[:max_chars].rsplit(" ", 1)[0] + "…"

    styles = getSampleStyleSheet()

    # --- Typography ---
    styles.add(ParagraphStyle(
        name="HeaderTitle",
        fontSize=18, leading=22, textColor=colors.white
    ))
    styles.add(ParagraphStyle(
        name="HeaderMeta",
        fontSize=9.5, leading=12, textColor=colors.white
    ))
    styles.add(ParagraphStyle(
        name="ThemeTitle",
        fontSize=12.5, leading=16, textColor=colors.white
    ))
    styles.add(ParagraphStyle(
        name="CardTitle",
        fontSize=10.5, leading=13, textColor=colors.black, spaceAfter=2
    ))
    styles.add(ParagraphStyle(
        name="CardMeta",
        fontSize=8.5, leading=11, textColor=GREY, spaceAfter=3
    ))
    styles.add(ParagraphStyle(
        name="CardBody",
        fontSize=9.5, leading=12, textColor=colors.black, spaceAfter=4
    ))
    styles.add(ParagraphStyle(
        name="CardLink",
        fontSize=9, leading=11, textColor=LINK
    ))

    doc = SimpleDocTemplate(
        pdf_path,
        pagesize=A4,
        leftMargin=1.6*cm,
        rightMargin=1.6*cm,
        topMargin=1.2*cm,
        bottomMargin=1.5*cm
    )

    # --- Group by theme ---
    grouped = defaultdict(list)
    for it in items:
        theme = getattr(it, "theme", None) or "News"
        grouped[theme].append(it)

    theme_order = sorted(grouped.keys(), key=lambda s: (s == "Other", s.lower()))

    # Layout constants
    page_width, _ = A4
    usable_width = page_width - doc.leftMargin - doc.rightMargin
    gap = 0.5*cm
    col_w = (usable_width - gap) / 2.0

    story = []

    # ---------- Header (colored bar + logo) ----------
    today = datetime.now().strftime("%d %B %Y")
    header_left = [
        Paragraph(title, styles["HeaderTitle"]),
        Spacer(1, 2),
        Paragraph(f"Daily review · {today} · {len(items)} items", styles["HeaderMeta"]),
    ]

    header_right = []
    from reportlab.lib.utils import ImageReader

    try:
        img = ImageReader(logo_path)
        iw, ih = img.getSize()

        logo = Image(logo_path)
        logo.drawHeight = 1.3*cm
        logo.drawWidth = logo.drawHeight * (iw / ih)

        header_right = [logo]
    except Exception as e:
        header_right = [Paragraph(f"<font color='white'>Logo error: {e}</font>", styles["HeaderMeta"])]


    header_table = Table(
        [[header_left, header_right]],
        colWidths=[usable_width * 0.78, usable_width * 0.22]
    )
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

    # ---------- Helper: build a compact card ----------
    def card_for_item(it, accent_color):
        summary = clamp_text(getattr(it, "summary", "") or "", 240)  # smaller than before
        published = getattr(it, "published", "") or ""
        date_str = published.split("T")[0] if "T" in published else (published[:10] if published else "")
        source = domain_from_url(getattr(it, "link", "") or "")

        title_txt = clamp_text(getattr(it, "title", "") or "(Untitled)", 105)
        link = getattr(it, "link", "") or ""
        safe_link = link.replace("'", "%27")

        content = [
            Paragraph(f"<b>{title_txt}</b>", styles["CardTitle"]),
            Paragraph(f"{source} · {date_str}", styles["CardMeta"]) if (source or date_str) else Spacer(1, 0),
            Paragraph(summary or "No summary available.", styles["CardBody"]),
            Paragraph(f"<a href='{safe_link}'>Read full article →</a>", styles["CardLink"]) if link else Spacer(1, 0),
        ]

        # Card built as 2 columns: thin accent bar + content
        card = Table([[ "", content ]], colWidths=[0.18*cm, col_w - 0.18*cm])
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

    # ---------- Theme sections ----------
    for theme in theme_order:
        accent = THEME_COLOR.get(theme, TEAL)

        # Theme bar
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

        # Cards in 2-column grid
        cards = [card_for_item(it, accent) for it in grouped[theme]]

        rows = []
        for i in range(0, len(cards), 2):
            left = cards[i]
            right = cards[i+1] if i+1 < len(cards) else Spacer(col_w, 1)
            rows.append([left, right])

        grid = Table(rows, colWidths=[col_w, col_w], hAlign="LEFT")
        grid.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ]))

        # Add a small gutter between columns by inserting a spacer column effect:
        # simplest is adding padding to the right column cell, but TableStyle doesn't allow per-column gap well.
        # So we just rely on card margins + overall layout. It looks clean in practice.

        story.append(grid)
        story.append(Spacer(1, 10))

    doc.build(story)


def send_email_smtp(
    smtp_host: str,
    smtp_port: int,
    username: str,
    password: str,
    sender: str,
    recipients: List[str],
    subject: str,
    body_text: str,
    attachment_path: str
) -> None:
    """
    Sends an email with a PDF attachment via SMTP.
    For Gmail, use an App Password (not your normal password).
    """
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content(body_text)

    with open(attachment_path, "rb") as f:
        pdf_bytes = f.read()
    msg.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename=os.path.basename(attachment_path))

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(smtp_host, smtp_port) as server:
        server.ehlo()
        # STARTTLS on 465
        if smtp_port == 465:
            server.starttls(context=context)
            server.ehlo()
        server.login(username, password)
        server.send_message(msg)


def send_email_with_attachment(
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    mail_from: str,
    mail_to_list: str,
    subject: str,
    body_text: str,
    attachment_path: str
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

    filename = os.path.basename(attachment_path)
    msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)

    context = ssl.create_default_context()

    if smtp_port == 465:
        # Implicit TLS
        with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=60, context=context) as server:
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
    else:
        # STARTTLS (587)
        with smtplib.SMTP(smtp_host, smtp_port, timeout=60) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)


def themes_for_hits(hits: List[str], keyword_to_theme: Dict[str, str]) -> List[str]:
    themes = []
    for kw in hits:
        t = keyword_to_theme.get(kw, "Other")
        themes.append(t)
    # unique, keep order
    seen = set()
    out = []
    for t in themes:
        if t not in seen:
            out.append(t)
            seen.add(t)
    return out


def primary_theme(hits: List[str], keyword_to_theme: Dict[str, str]) -> str:
    for kw in hits:
        if kw in keyword_to_theme:
            return keyword_to_theme[kw]
    return "Other"


def main():
    import argparse
    
    today_utc = datetime.now(timezone.utc).date()

    ap = argparse.ArgumentParser()
    ap.add_argument("--xlsx", required=True, help="Path to Excel file (.xlsx) with feeds in col A and keywords in col B")
    ap.add_argument("--sheet", default=None, help="Optional sheet name")
    ap.add_argument("--outdir", default=".", help="Output directory for PDF")
    ap.add_argument("--send-email", action="store_true", help="Actually send the email (requires SMTP env vars)")
    
    ap.add_argument(
    "--local-rss",
    action="append",
    default=[],
    help="Path to a local RSS XML file (can be repeated). Example: C:\\path\\payloadspace-news.xml"
    )
    
    ap.add_argument(
    "--local-rss-dir",
    help="Directory containing local RSS XML files",
    )
  
    args = ap.parse_args()
    
    ap.add_argument("--smtp-host", default=None)
    ap.add_argument("--smtp-port", default=465, type=int)
    ap.add_argument("--smtp-user", default=None)
    ap.add_argument("--smtp-pass", default=None)
    ap.add_argument("--mail-from", default=None)
    ap.add_argument("--mail-to", default="contact@orbitways.com")


    feeds, keyword_to_theme, keywords = load_sheet_with_themes(args.xlsx, args.sheet)
    
    from pathlib import Path

    if args.local_rss_dir:
        rss_dir = Path(args.local_rss_dir)
        if rss_dir.is_dir():
            for p in rss_dir.glob("*.xml"):
                feeds.append(str(p))

    
    # Add local RSS files (e.g. generated payloadspace-news.xml)
    for p in (args.local_rss or []):
        p = str(p).strip()
        if p and os.path.exists(p):
            feeds.append(p)


    if not feeds:
        raise SystemExit("No RSS feeds found in column A.")
    if not keywords:
        raise SystemExit("No keywords found in column B.")

    kw_re = compile_keyword_regex(keywords)

    seen = load_seen_hashes(SEEN_DB_PATH)
    newly_seen: Set[str] = set()

    matched_entries: List[Tuple[dict, List[str]]] = []
    # Collect candidates
    for feed in feeds:
        try:
            entries = rss_entries(feed)[:MAX_ITEMS_PER_FEED]
        except Exception:
            continue

        for e in entries:
            # --- NEW: date filter ---
            dt = entry_datetime(e)
            if dt is None:
                continue  # strict mode: skip undated items

            if dt.date() != today_utc:
                continue  # skip non-today items

            link = entry_link(e)
            if not link or not link.startswith("http"):
                continue

            h = url_hash(link)
            if h in seen:
                continue

            text = entry_text(e)
            hits = match_keywords(kw_re, text, keywords)
            if hits:
                matched_entries.append((e, hits, themes_for_hits(hits, keyword_to_theme)))
                newly_seen.add(h)


    # Cap total
    matched_entries = matched_entries[:MAX_TOTAL_ITEMS]

    # Fetch article text for summaries (cap for speed)
    items: List[DigestItem] = []
    fetch_count = 0
    for e, hits, _themes in matched_entries:
        title = (e.get("title", "") or "").strip() or "(Untitled)"
        link = entry_link(e)
        published = entry_published(e)

        theme = primary_theme(hits, keyword_to_theme)

        summary = ""
        if fetch_count < MAX_FETCHED_ARTICLES:
            try:
                article_text = fetch_article_text(link)
                summary = extractive_summary(article_text, SUMMARY_SENTENCES)
                fetch_count += 1
            except Exception:
                summary = (e.get("summary", "") or e.get("description", "") or "").strip()
                if summary:
                    summary = re.sub(r"<[^>]+>", " ", summary)
                    summary = re.sub(r"\s+", " ", summary).strip()
                    summary = summary[:500] + ("…" if len(summary) > 500 else "")

        items.append(DigestItem(
            title=title,
            link=link,
            published=published,
            matched_keywords=hits,
            theme=theme,
            summary=summary
        ))

    # Save dedup DB
    append_seen_hashes(SEEN_DB_PATH, newly_seen)

    # Build PDF
    os.makedirs(args.outdir, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    pdf_path = os.path.join(args.outdir, f"Orbitways_Daily_News_{date_str}.pdf")

    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    logo_path = os.path.join(SCRIPT_DIR, "orbitways_logo.png")

    build_pdf(items, pdf_path, title="Orbitways – Daily Space News", logo_path=logo_path)
    print(f"PDF written: {pdf_path} (items: {len(items)})")
    
    smtp_host = os.environ.get("OW_SMTP_HOST")
    smtp_port = int(os.environ.get("OW_SMTP_PORT", "465"))
    smtp_user = os.environ.get("OW_SMTP_USER")
    smtp_pass = os.environ.get("OW_SMTP_PASS")
    mail_from = os.environ.get("OW_MAIL_FROM", smtp_user)
    raw_recipients = os.environ.get("OW_MAIL_TO", "")
    mail_to_list = [r.strip() for r in raw_recipients.split(",") if r.strip()]

    if not all([smtp_host, smtp_user, smtp_pass, mail_from, mail_to_list]):
        raise RuntimeError("Missing SMTP environment variables (OW_SMTP_HOST/USER/PASS/MAIL_FROM/MAIL_TO).")

    subject = f"Orbitways Daily Space Review — {datetime.now().strftime('%Y-%m-%d')}"
    body = "Hi,\n\nPlease find attached today’s Orbitways Daily Space Review (PDF).\n\n— Orbitways News Bot\n"

    send_email_with_attachment(
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_user=smtp_user,
        smtp_password=smtp_pass,
        mail_from=mail_from,
        mail_to_list=mail_to_list,
        subject=subject,
        body_text=body,
        attachment_path=pdf_path
    )

    print(f"Email sent to {mail_to_list}")


    # Optional: email
    if args.send_email:
        smtp_host = os.environ.get("SMTP_HOST", "")
        smtp_port = int(os.environ.get("SMTP_PORT", "587"))
        smtp_user = os.environ.get("SMTP_USER", "")
        smtp_pass = os.environ.get("SMTP_PASS", "")
        sender = os.environ.get("SMTP_SENDER", smtp_user)
        recipients = [r.strip() for r in os.environ.get("SMTP_TO", "").split(",") if r.strip()]

        if not (smtp_host and smtp_user and smtp_pass and recipients):
            raise SystemExit("Missing SMTP env vars. Set SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_TO")

        subject = f"Orbitways – Daily News ({date_str})"
        body = "Attached: Orbitways daily RSS digest PDF."
        send_email_smtp(
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            username=smtp_user,
            password=smtp_pass,
            sender=sender,
            recipients=recipients,
            subject=subject,
            body_text=body,
            attachment_path=pdf_path
        )
        print("Email sent.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Bunkr downloader — search → select → download, all in a fire terminal UI.

Usage:
    python bunkdl.py                        # interactive search (default)
    python bunkdl.py -s                     # force search mode
    python bunkdl.py https://bunkr.cr/a/X  # direct album URL
    python bunkdl.py <url> -v -j 4         # videos only, 4 workers
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
import urllib.request
from dataclasses import dataclass, field as dc_field
from enum import Enum
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse, urlunparse

import aiohttp
from aiohttp import ClientTimeout
from bs4 import BeautifulSoup
from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.prompt import Prompt
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

# ── constants ──────────────────────────────────────────────────────────────────
BUNKR_BASE     = "https://bunkr.cr"
ALBUM_RE       = re.compile(r"https?://(?:www\.)?bunkr\.cr/a/[\w-]+",        re.I)
FILE_PAGE_RE   = re.compile(r"https?://(?:www\.)?bunkr\.cr/[fvi]/[^/\s?#]+", re.I)
FILE_PATH_RE   = re.compile(r"^/[fvi]/[^/]+$")
DL_PAGE_RE     = re.compile(r"https?://dl\.bunkr\.cr/file/(\d+)",            re.I)
VIDEO_EXT      = re.compile(r"\.(mp4|webm|mov|avi|mkv|m4v|wmv|flv)$",        re.I)
IMAGE_EXT      = re.compile(r"\.(jpe?g|png|gif|webp|bmp|tiff?|heic|avif)$",  re.I)

BALBUMS_BASE        = "https://balbums.st/"
_UA                 = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
_BUNKR_ALBUM_HREF   = re.compile(r"https?://(?:www\.)?bunkr\.\w+/a/[\w-]+",  re.I)
_STRIP_VIEW_PREFIX  = re.compile(r"^(?:view\s+album|view)\s*",                re.I)
_STRIP_FILES_SUFFIX = re.compile(r"\s*\d+\s*files?\s*[→>\-]+\s*open\s*$",    re.I)
_GENERIC_LINK_TEXT  = frozenset({"open", "view album", "view", "→ open", "→open"})

HEADERS = {
    "User-Agent":      _UA,
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Referer":         "https://bunkr.cr/",
}

console = Console(highlight=False)

# ── visual tokens ──────────────────────────────────────────────────────────────
_SPIN_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_spin_i = 0
def _spin() -> str:
    global _spin_i
    c = _SPIN_FRAMES[_spin_i % len(_SPIN_FRAMES)]
    _spin_i += 1
    return c

def _bar(pct: float, width: int = 22,
         on_char: str = "━", off_char: str = "─") -> Text:
    """Render a ━━━━─── progress bar as a Rich Text object."""
    filled = max(0, min(width, int(pct / 100 * width)))
    t = Text()
    if filled:
        t.append(on_char * filled,        style="bold cyan")
    if filled < width:
        t.append(off_char * (width - filled), style="bright_black")
    return t

def _bar_str(pct: float, width: int = 22) -> str:
    filled = max(0, min(width, int(pct / 100 * width)))
    return "[bold cyan]" + "━" * filled + "[/][bright_black]" + "─" * (width - filled) + "[/]"

# ── enums ──────────────────────────────────────────────────────────────────────
class MediaKind(str, Enum):
    VIDEO = "video"
    IMAGE = "image"
    FILE  = "file"

class MediaFilter(str, Enum):
    ALL   = "all"
    VIDEO = "video"
    IMAGE = "image"
    FILE  = "file"

class Status(str, Enum):
    PENDING     = "pending"
    RESOLVING   = "resolving"
    QUEUED      = "queued"
    DOWNLOADING = "downloading"
    DONE        = "done"
    ERROR       = "error"
    SKIPPED     = "skipped"

KIND_ICON: dict[MediaKind, tuple[str, str]] = {
    MediaKind.VIDEO: ("▶", "bold blue"),
    MediaKind.IMAGE: ("◈", "bold green"),
    MediaKind.FILE:  ("⊞", "bold yellow"),
}
STATUS_ICON: dict[Status, tuple[str, str]] = {
    Status.PENDING:     ("○",  "dim"),
    Status.RESOLVING:   ("◌",  "cyan"),
    Status.QUEUED:      ("◎",  "yellow"),
    Status.DOWNLOADING: ("↓",  "bold bright_blue"),
    Status.DONE:        ("✓",  "bold green"),
    Status.ERROR:       ("✗",  "bold red"),
    Status.SKIPPED:     ("⊘",  "magenta"),
}

# ── helpers ────────────────────────────────────────────────────────────────────
def fmt_bytes(n: int) -> str:
    if n <= 0: return "—"
    v = float(n)
    for u in ("B","KB","MB","GB"):
        if v < 1024: return f"{v:.1f} {u}" if u != "B" else f"{int(v)} B"
        v /= 1024
    return f"{v:.1f} TB"

def fmt_speed(bps: float) -> str:
    return "—" if bps <= 0 else f"{fmt_bytes(int(bps))}/s"

def fmt_eta(bps: float, remaining: int) -> str:
    if bps <= 0 or remaining <= 0: return "—"
    s = int(remaining / bps)
    if s < 60:   return f"{s}s"
    if s < 3600: return f"{s//60}m {s%60:02d}s"
    return f"{s//3600}h {(s%3600)//60:02d}m"

def fmt_elapsed(s: float) -> str:
    s = int(s)
    if s < 60:   return f"{s}s"
    if s < 3600: return f"{s//60}m {s%60:02d}s"
    return f"{s//3600}h {(s%3600)//60:02d}m"

def min_valid_bytes(k: MediaKind) -> int:
    return {MediaKind.VIDEO: 512*1024, MediaKind.IMAGE: 512}.get(k, 64)

def sanitize_filename(n: str) -> str:
    return (re.sub(r'[<>:"/\\|?*]', "_", n.strip())[:200]) or "download"

def sanitize_folder(n: str) -> str:
    n = re.sub(r'[<>:"/\\|?*]', "_", n.strip()).strip(". ")
    return n[:120] or "bunkr_album"

def matches_filter(k: MediaKind, f: MediaFilter) -> bool:
    return f == MediaFilter.ALL or k.value == f.value

def classify_filename(n: str) -> MediaKind:
    if VIDEO_EXT.search(n): return MediaKind.VIDEO
    if IMAGE_EXT.search(n): return MediaKind.IMAGE
    return MediaKind.FILE

def classify_item_element(item) -> MediaKind:
    for span in item.find_all("span", class_=True):
        cl = " ".join(span.get("class",[]))
        if "type-Video" in cl: return MediaKind.VIDEO
        if "type-Image" in cl: return MediaKind.IMAGE
        if "type-File"  in cl: return MediaKind.FILE
    return classify_filename(item.get("title") or "")

# ── job ────────────────────────────────────────────────────────────────────────
@dataclass
class Job:
    index:      int
    filename:   str
    page_url:   str
    kind:       MediaKind
    direct_url: Optional[str] = None
    status:     Status        = Status.PENDING
    progress:   float         = 0.0
    downloaded: int           = 0
    total:      int           = 0
    speed:      float         = 0.0
    error:      str           = ""
    dest:       Optional[Path] = None
    dirty:      bool          = True

    def touch(self) -> None: self.dirty = True

# ── balbums search ─────────────────────────────────────────────────────────────
def _balbums_parse_page(html: str) -> tuple[list[dict], int, int]:
    soup = BeautifulSoup(html, "html.parser")
    albums: list[dict] = []; seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip().rstrip("/")
        if not _BUNKR_ALBUM_HREF.match(href): continue
        if href in seen: continue
        seen.add(href)
        raw   = a.get_text(strip=True)
        title = _STRIP_VIEW_PREFIX.sub("", raw)
        title = _STRIP_FILES_SUFFIX.sub("", title).strip()
        if not title or title.lower() in _GENERIC_LINK_TEXT or len(title) < 2:
            title = href.rsplit("/",1)[-1]
            for tag in ("h3","h2","h4","strong","b"):
                prev = a.find_previous(tag)
                if prev:
                    t = prev.get_text(strip=True)
                    if t and t.lower() not in _GENERIC_LINK_TEXT and len(t) > 1:
                        title = t; break
        fc_m  = re.search(r"(\d+)\s*files?", raw, re.I)
        fcount= fc_m.group(1) if fc_m else "?"
        if fcount == "?":
            node = a.parent
            for _ in range(5):
                if node is None: break
                m = re.search(r"(\d+)\s*files?", node.get_text(" ",strip=True), re.I)
                if m: fcount = m.group(1); break
                node = node.parent
        albums.append({"title": title, "url": href, "files": fcount})
    pt = soup.get_text(" ", strip=True)
    pm = re.search(r"page\s+(\d+)\s+of\s+(\d+)", pt, re.I)
    cur  = int(pm.group(1)) if pm else 1
    tot  = int(pm.group(2)) if pm else 1
    return albums, cur, max(tot, cur)

def _balbums_fetch(query: str, page: int = 1, per: int = 20) -> tuple[list[dict], int, int]:
    url = f"{BALBUMS_BASE}?search={quote(query)}&mode=broad&per={per}&sort=latest&page={page}"
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        html = r.read().decode("utf-8", "replace")
    return _balbums_parse_page(html)

# ── search UI ──────────────────────────────────────────────────────────────────
def interactive_search() -> Optional[tuple[str, MediaFilter]]:
    console.print()
    console.print(Rule(
        Text.assemble(("  ⬇  BUNKR ", "bold cyan"), ("SEARCH  ", "bold white")),
        style="cyan", characters="━"
    ))
    console.print()
    query = Prompt.ask("  [bold cyan]Query[/]").strip()
    if not query:
        return None

    page = 1; per = 20
    while True:
        with console.status(f"[cyan]  Searching balbums.st for [bold]{query!r}[/] …", spinner="dots"):
            try:
                albums, cur_page, total_pages = _balbums_fetch(query, page, per)
            except Exception as exc:
                console.print(f"\n  [bold red]Error:[/] {exc}")
                return None

        if not albums:
            console.print(f"\n  [yellow]No results found for [bold]{query!r}[/][/]")
            return None

        console.print()

        # header row
        pg_text = Text.assemble(
            ("  balbums.st  ", "bold cyan"),
            (f'"{query}"', "white"),
            (f"  ·  page {cur_page} of {total_pages}", "dim"),
        )
        console.print(pg_text)
        console.print()

        tbl = Table(
            box=box.ROUNDED,
            border_style="bright_black",
            header_style="bold dim",
            show_edge=True,
            padding=(0, 2),
            expand=False,
        )
        tbl.add_column("#",     justify="right", style="dim",        width=4)
        tbl.add_column("T",     justify="center",                    width=3)
        tbl.add_column("Album", style="white",   min_width=38, max_width=60)
        tbl.add_column("Files", justify="right", style="bold cyan",  width=7)

        for i, alb in enumerate(albums, 1):
            t = alb["title"]
            t_disp = (t[:58] + "…") if len(t) > 58 else t
            # guess kind from title (crude — no real metadata available)
            tbl.add_row(str(i), "📁", t_disp, alb["files"])

        console.print(tbl)
        console.print()

        nav: list[str] = []
        if cur_page < total_pages: nav.append("[n]ext")
        if cur_page > 1:           nav.append("[p]rev")
        nav.append("[q]uit")

        choice = Prompt.ask(
            "  [cyan]Pick #[/]  " + "  ".join(f"[dim]{x}[/]" for x in nav)
        ).strip().lower()

        if choice == "q": return None
        if choice == "n" and cur_page < total_pages: page += 1;  continue
        if choice == "p" and cur_page > 1:           page -= 1;  continue

        try:
            idx = int(choice) - 1
        except ValueError:
            console.print("  [red]Enter a number, or n / p / q[/]")
            continue
        if not (0 <= idx < len(albums)):
            console.print(f"  [red]Enter 1–{len(albums)}[/]")
            continue

        alb = albums[idx]
        fl  = alb["files"]
        console.print()
        console.print(Panel(
            Text.assemble(
                ("✓  ", "bold green"),
                (alb["title"], "bold white"), "\n",
                (f"{fl} files  ·  ", "dim"),
                (alb["url"], "dim cyan"),
            ),
            border_style="cyan",
            padding=(0, 2),
            expand=False,
        ))
        console.print()
        filt_raw = Prompt.ask(
            "  [cyan]Filter[/]  "
            "[dim](a[/][white]ll[/][dim]  v[/][white]ideo  [/][dim]i[/][white]mage  [/][dim]f[/][white]iles[/][dim])[/]",
            default="a",
        ).strip().lower()
        fmap = {"a": MediaFilter.ALL, "v": MediaFilter.VIDEO,
                "i": MediaFilter.IMAGE, "f": MediaFilter.FILE}
        console.print()
        return alb["url"], fmap.get(filt_raw, MediaFilter.ALL)

# ── async bunkr helpers ────────────────────────────────────────────────────────
async def fetch_text(s: aiohttp.ClientSession, url: str) -> str:
    async with s.get(url, allow_redirects=True) as r:
        return await r.text() if r.status == 200 else ""

def parse_album_items(html: str) -> tuple[Optional[str], list[dict]]:
    soup = BeautifulSoup(html, "lxml")
    h1   = soup.find("h1")
    title= h1.get_text(strip=True) if h1 else None
    items: list[dict] = []; seen: set[str] = set()
    for item in soup.select("div.theItem"):
        name = (item.get("title") or "").strip()
        link = item.find("a", href=FILE_PATH_RE)
        if not link: continue
        pu = urljoin(BUNKR_BASE, link["href"])
        if pu in seen: continue
        seen.add(pu)
        items.append({"filename": name or pu.rsplit("/",1)[-1], "page_url": pu, "kind": classify_item_element(item)})
    if items: return title, items
    for a in soup.find_all("a", href=FILE_PATH_RE):
        pu = urljoin(BUNKR_BASE, a["href"])
        if pu in seen: continue
        seen.add(pu)
        n = a.get_text(strip=True) or pu.rsplit("/",1)[-1]
        items.append({"filename": n, "page_url": pu, "kind": classify_filename(n)})
    return title, items

def parse_single_file_page(html: str, page_url: str) -> tuple[Optional[str], list[dict]]:
    soup = BeautifulSoup(html, "lxml")
    h1   = soup.find("h1")
    title= h1.get_text(strip=True) if h1 else None
    fn   = title or page_url.rsplit("/",1)[-1]
    return title, [{"filename": fn, "page_url": page_url, "kind": classify_filename(fn)}]

def parse_file_download_page(html: str) -> Optional[str]:
    soup = BeautifulSoup(html, "lxml")
    for a in soup.find_all("a", href=True):
        if DL_PAGE_RE.match(a["href"]): return a["href"]
    m = DL_PAGE_RE.search(html)
    return m.group(0) if m else None

def _fid(dl_url: str) -> Optional[str]:
    m = DL_PAGE_RE.match(dl_url); return m.group(1) if m else None

async def resolve_signed_cdn_url(s: aiohttp.ClientSession, fid: str, name: str = "") -> Optional[str]:
    hdrs = {**HEADERS, "Referer": "https://dl.bunkr.cr/", "Origin": "https://dl.bunkr.cr", "Content-Type": "application/json"}
    try:
        async with s.post("https://dl.bunkr.cr/api/_001_v2", json={"id": fid}, headers=hdrs) as r:
            if r.status != 200: return None
            meta = await r.json()
        base = meta.get("mediafiles","") + meta.get("path","")
        if not base: return None
        if base.startswith("//"): base = "https:" + base
        elif not base.startswith("http"): base = "https://" + base.lstrip("/")
        nm = meta.get("original") or name
        parsed = urlparse(base); qs = parse_qs(parsed.query)
        if nm: qs["n"] = [nm]
        async with s.get("https://glb-apisign.cdn.cr/sign?path=" + quote(parsed.path, safe="")) as sr:
            if sr.status != 200: return None
            sig = await sr.json()
        qs["token"] = [sig["token"]]; qs["ex"] = [str(sig["ex"])]
        return urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))
    except Exception: return None

async def resolve_download_url(s: aiohttp.ClientSession, page_url: str, fn: str = "") -> Optional[str]:
    html = await fetch_text(s, page_url)
    if not html: return None
    dl = parse_file_download_page(html)
    if not dl: return None
    fid = _fid(dl)
    if not fid: return None
    return await resolve_signed_cdn_url(s, fid, fn)

# ── live download UI ───────────────────────────────────────────────────────────
class DownloadUI:
    """Renders a full-screen live download dashboard."""

    BAR_W = 20

    def __init__(self, jobs: list[Job], album_title: str, source_url: str,
                 out_dir: Path, media_filter: MediaFilter):
        self.jobs        = jobs
        self.album_title = album_title
        self.source_url  = source_url
        self.out_dir     = out_dir
        self.filter      = media_filter
        self._t0         = time.monotonic()
        self._stats_dirty= True

    # ── aggregate ────────────────────────────────────────────────────────────
    def _agg(self) -> dict:
        done    = sum(1 for j in self.jobs if j.status == Status.DONE)
        errs    = sum(1 for j in self.jobs if j.status == Status.ERROR)
        skip    = sum(1 for j in self.jobs if j.status == Status.SKIPPED)
        active  = sum(1 for j in self.jobs if j.status == Status.DOWNLOADING)
        queued  = sum(1 for j in self.jobs if j.status in (Status.QUEUED, Status.PENDING))
        res     = sum(1 for j in self.jobs if j.status == Status.RESOLVING)
        speed   = sum(j.speed for j in self.jobs if j.status == Status.DOWNLOADING)
        dl_bytes= sum(j.downloaded for j in self.jobs)
        total_known = sum(j.total for j in self.jobs if j.total > 0)
        return dict(done=done, errors=errs, skipped=skip, active=active,
                    queued=queued, resolving=res, speed=speed,
                    dl_bytes=dl_bytes, total_known=total_known,
                    elapsed=time.monotonic() - self._t0, total=len(self.jobs))

    # ── header panel ─────────────────────────────────────────────────────────
    def _header(self, agg: dict) -> Panel:
        speed_str = (f"[bold cyan]⚡ {fmt_speed(agg['speed'])}[/]  " if agg['speed'] > 0 else "")
        elapsed   = fmt_elapsed(agg['elapsed'])
        title_line = Text.assemble(
            ("  ⬛  ", "dim"),
            ("B U N K R", "bold white"),
            ("  ·  ", "bright_black"),
            (self.album_title or "Download", "bold cyan"),
        )
        meta_line = Text.assemble(
            ("  ", ""),
            (self.source_url[:72], "dim cyan"), "\n",
            ("  → ", "dim"), (str(self.out_dir), "dim"),
            ("  ·  ", "bright_black"),
            (f"filter: {self.filter.value}", "dim"),
            ("  ·  ", "bright_black"),
            (f"{agg['total']} files", "dim"),
        )
        stats_line = Text.assemble(
            ("  ", ""),
            (speed_str, ""),
            (f"[dim]elapsed:[/] [white]{elapsed}[/]", ""),
            (f"  [dim]received:[/] [white]{fmt_bytes(agg['dl_bytes'])}[/]", "")
            if agg['dl_bytes'] else ("", ""),
        )
        body = Text.assemble(title_line, "\n", meta_line, "\n", stats_line)
        return Panel(body, border_style="cyan", padding=(0, 0))

    # ── file table ────────────────────────────────────────────────────────────
    def _job_table(self) -> Table:
        tbl = Table(
            box=None,
            show_header=False,
            show_edge=False,
            padding=(0, 1),
            expand=True,
        )
        tbl.add_column("ico",   width=2,  justify="center")
        tbl.add_column("kind",  width=2,  justify="center")
        tbl.add_column("name",  min_width=30, max_width=38, no_wrap=True)
        tbl.add_column("bar",   width=self.BAR_W + 2, no_wrap=True)
        tbl.add_column("pct",   width=6,  justify="right")
        tbl.add_column("size",  width=18, justify="right")
        tbl.add_column("spd",   width=11, justify="right")
        tbl.add_column("st",    width=14, justify="left")

        for job in self.jobs:
            s_icon, s_style = STATUS_ICON[job.status]
            k_icon, k_style = KIND_ICON[job.kind]

            fname = job.filename
            if len(fname) > 36: fname = fname[:33] + "…"

            # progress bar + pct
            if job.status == Status.DOWNLOADING:
                bar_t  = _bar(job.progress, self.BAR_W)
                pct_t  = Text(f"{job.progress:5.1f}%", style="cyan")
            elif job.status == Status.DONE:
                bar_t  = _bar(100, self.BAR_W)
                pct_t  = Text(" 100%",  style="green")
            elif job.status == Status.SKIPPED:
                bar_t  = _bar(100, self.BAR_W, on_char="─", off_char="─")
                pct_t  = Text(" ───", style="magenta")
            elif job.status == Status.ERROR:
                bar_t  = Text("─" * self.BAR_W, style="red")
                pct_t  = Text("  ✗", style="red")
            elif job.status == Status.RESOLVING:
                frames = _spin()
                bar_t  = Text(f"{frames} resolving…", style="cyan")
                pct_t  = Text("")
            else:  # PENDING / QUEUED
                bar_t  = Text("─" * self.BAR_W, style="bright_black")
                pct_t  = Text("  —", style="dim")

            # size column
            if job.status == Status.DOWNLOADING and job.total > 0:
                size_t = Text(
                    f"{fmt_bytes(job.downloaded)}/{fmt_bytes(job.total)}",
                    style="dim"
                )
            elif job.status in (Status.DONE, Status.SKIPPED):
                size_t = Text(fmt_bytes(job.total or job.downloaded), style="dim")
            else:
                size_t = Text("—", style="bright_black")

            # speed + eta
            if job.status == Status.DOWNLOADING and job.speed > 0:
                spd_t = Text(fmt_speed(job.speed), style="bright_blue")
            else:
                spd_t = Text("—", style="bright_black")

            # status label
            if job.status == Status.ERROR and job.error:
                short = job.error[:11] + "…" if len(job.error) > 11 else job.error
                st_t = Text.assemble((s_icon + " ", s_style), (short, "dim red"))
            elif job.status == Status.DOWNLOADING:
                remaining = (job.total - job.downloaded) if job.total else 0
                eta = fmt_eta(job.speed, remaining)
                st_t = Text.assemble((s_icon, s_style), (f" {eta}", "dim"))
            else:
                st_t = Text.assemble((s_icon + " ", s_style), (job.status.value, s_style))

            tbl.add_row(
                Text(s_icon, style=s_style),
                Text(k_icon, style=k_style),
                Text(fname,  style="white" if job.status == Status.DOWNLOADING else "dim white"),
                bar_t,
                pct_t,
                size_t,
                spd_t,
                st_t,
            )
        return tbl

    # ── footer ────────────────────────────────────────────────────────────────
    def _footer(self, agg: dict) -> Text:
        total    = agg["total"]
        finished = agg["done"] + agg["skipped"] + agg["errors"]
        pct      = finished / total * 100 if total else 0

        # overall bar
        bar_line = Text.assemble(
            ("  Overall  ", "dim"),
            _bar(pct, 28),
            (f"  {pct:5.1f}%", "white"),
        )
        if agg["total_known"] > 0:
            bar_line.append(
                f"  ·  {fmt_bytes(agg['dl_bytes'])} / {fmt_bytes(agg['total_known'])}",
                style="dim",
            )

        # counters
        parts: list[tuple[str, str]] = [("  ", "")]
        if agg["done"]:      parts += [("✓ ", "bold green"),    (f"{agg['done']} done",  "green"),    ("  ", "")]
        if agg["errors"]:    parts += [("✗ ", "bold red"),      (f"{agg['errors']} err", "red"),      ("  ", "")]
        if agg["skipped"]:   parts += [("⊘ ", "magenta"),       (f"{agg['skipped']} skip","magenta"), ("  ", "")]
        if agg["active"]:    parts += [("↓ ", "bold bright_blue"),(f"{agg['active']} dl", "bright_blue"),("  ","")]
        if agg["resolving"]: parts += [("◌ ", "cyan"),           (f"{agg['resolving']} res","cyan"),  ("  ", "")]
        if agg["queued"]:    parts += [("◎ ", "yellow"),         (f"{agg['queued']} q",   "yellow"),  ("  ", "")]
        parts.append((f"/ {total} total", "dim"))

        counter_line = Text.assemble(*parts)
        return Text.assemble(bar_line, "\n", counter_line, "\n")

    # ── render ────────────────────────────────────────────────────────────────
    def render(self) -> Group:
        agg = self._agg()
        return Group(
            self._header(agg),
            Text(""),
            self._job_table(),
            Text(""),
            Rule(style="bright_black"),
            self._footer(agg),
        )

    def mark_dirty(self) -> None:
        self._stats_dirty = True


def build_summary(jobs: list[Job], elapsed: float) -> Panel:
    done  = sum(1 for j in jobs if j.status == Status.DONE)
    errs  = sum(1 for j in jobs if j.status == Status.ERROR)
    skip  = sum(1 for j in jobs if j.status == Status.SKIPPED)
    total_dl = sum(j.downloaded for j in jobs if j.status == Status.DONE)

    body = Text.assemble(
        ("  ✓  ", "bold green"),   (f"{done} completed", "green"),   ("   ", ""),
        ("✗  ", "bold red"),       (f"{errs} failed",    "red"),      ("   ", ""),
        ("⊘  ", "magenta"),        (f"{skip} skipped",   "magenta"),  ("   ", ""),
        ("⬇  ", "bold cyan"),      (f"{fmt_bytes(total_dl)} downloaded", "cyan"), ("   ",""),
        ("⏱  ", "dim"),            (fmt_elapsed(elapsed),             "dim"),
    )
    return Panel(
        body,
        title=Text("  COMPLETE  ", style="bold green"),
        border_style="green",
        padding=(0, 0),
    )

# ── download pipeline ──────────────────────────────────────────────────────────
async def download_file(
    session: aiohttp.ClientSession, job: Job, out_dir: Path, ui: DownloadUI,
) -> None:
    dest   = out_dir / sanitize_filename(job.filename)
    job.dest = dest
    min_sz = min_valid_bytes(job.kind)

    if dest.exists() and dest.stat().st_size >= min_sz:
        job.status = Status.SKIPPED
        job.total  = dest.stat().st_size; job.downloaded = job.total; job.progress = 100.0
        job.touch(); ui.mark_dirty(); return

    if dest.exists(): dest.unlink(missing_ok=True)
    if not job.direct_url:
        job.status = Status.ERROR; job.error = "No download URL"
        job.touch(); ui.mark_dirty(); return

    job.status = Status.DOWNLOADING; job.touch(); ui.mark_dirty()
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        async with session.get(job.direct_url, allow_redirects=True,
                               headers={**HEADERS, "Referer": "https://dl.bunkr.cr/"}) as r:
            if r.status != 200:
                job.status = Status.ERROR; job.error = f"HTTP {r.status}"; job.touch(); ui.mark_dirty(); return
            if "text/html" in (r.headers.get("Content-Type") or "").lower():
                job.status = Status.ERROR; job.error = "Got HTML"; job.touch(); ui.mark_dirty(); return
            job.total = int(r.headers.get("Content-Length", 0))
            job.downloaded = 0
            last_tick = time.monotonic(); last_bytes = 0; last_ui = 0.0
            with open(tmp, "wb") as f:
                async for chunk in r.content.iter_chunked(256 * 1024):
                    f.write(chunk); job.downloaded += len(chunk)
                    now = time.monotonic()
                    if job.total > 0: job.progress = min(99.9, job.downloaded / job.total * 100)
                    dt = now - last_tick
                    if dt >= 0.5:
                        job.speed  = (job.downloaded - last_bytes) / dt
                        last_bytes = job.downloaded; last_tick = now
                    if now - last_ui >= 0.12: job.touch(); last_ui = now
        if job.downloaded < min_sz:
            tmp.unlink(missing_ok=True)
            job.status = Status.ERROR; job.error = f"Too small ({fmt_bytes(job.downloaded)})"
            job.touch(); ui.mark_dirty(); return
        tmp.rename(dest)
        job.progress = 100.0; job.speed = 0.0; job.status = Status.DONE
        job.touch(); ui.mark_dirty()
    except Exception as exc:
        job.status = Status.ERROR; job.error = str(exc)[:80]
        job.touch(); ui.mark_dirty()
        if tmp.exists(): tmp.unlink(missing_ok=True)

async def load_entries(session: aiohttp.ClientSession, url: str) -> tuple[Optional[str], list[dict], str]:
    url = url.strip()
    if not url.startswith("http"): url = "https://" + url
    if ALBUM_RE.match(url):
        html = await fetch_text(session, url)
        if not html: raise RuntimeError("Failed to load album page")
        title, entries = parse_album_items(html)
        return title, entries, url
    if FILE_PAGE_RE.match(url):
        html = await fetch_text(session, url)
        if not html: raise RuntimeError("Failed to load file page")
        title, entries = parse_single_file_page(html, url)
        return title, entries, url
    raise RuntimeError("URL must be a Bunkr album (/a/) or file (/f/ /i/ /v/) page")

async def run(
    source_url: str, base_out: Path, media_filter: MediaFilter,
    workers: int, resolve_workers: int,
) -> int:
    t0 = time.monotonic()
    timeout = ClientTimeout(total=None, connect=30, sock_read=300)
    async with aiohttp.ClientSession(headers=HEADERS, timeout=timeout) as session:
        with console.status(f"[cyan]  Fetching[/] [dim]{source_url}[/]", spinner="dots"):
            try:
                album_title, entries, norm_url = await load_entries(session, source_url)
            except RuntimeError as exc:
                console.print(f"  [bold red]✗[/] {exc}"); return 1

        entries = [e for e in entries if matches_filter(e["kind"], media_filter)]
        if not entries:
            console.print(f"  [yellow]No items match filter '{media_filter.value}'[/]"); return 1

        folder_name = sanitize_folder(album_title or "bunkr_download")
        out_dir     = base_out / folder_name
        out_dir.mkdir(parents=True, exist_ok=True)

        jobs = [Job(index=i+1, filename=e["filename"], page_url=e["page_url"], kind=e["kind"])
                for i, e in enumerate(entries)]

        ui = DownloadUI(jobs, album_title or folder_name, norm_url, out_dir, media_filter)

        # ── pipeline: workers start now, downloads begin as resolves complete ──
        queue: asyncio.Queue[Optional[Job]] = asyncio.Queue()

        async def _dl_worker() -> None:
            while True:
                job = await queue.get()
                if job is None: queue.task_done(); break
                await download_file(session, job, out_dir, ui)
                queue.task_done()

        worker_tasks = [asyncio.create_task(_dl_worker()) for _ in range(workers)]
        resolve_sem  = asyncio.Semaphore(resolve_workers)

        async def _resolve_and_enqueue(job: Job) -> None:
            job.status = Status.RESOLVING; job.touch(); ui.mark_dirty()
            async with resolve_sem:
                job.direct_url = await resolve_download_url(session, job.page_url, job.filename)
            if job.direct_url:
                job.status = Status.QUEUED; await queue.put(job)
            else:
                job.status = Status.ERROR; job.error = "No download URL"
            job.touch(); ui.mark_dirty()

        async def _refresh(live: Live) -> None:
            while True:
                if any(j.dirty for j in jobs) or ui._stats_dirty:
                    live.update(ui.render()); ui._stats_dirty = False
                await asyncio.sleep(0.1)

        with Live(ui.render(), console=console, refresh_per_second=15,
                  transient=False, vertical_overflow="visible") as live:
            refresh_task = asyncio.create_task(_refresh(live))
            try:
                await asyncio.gather(
                    *[asyncio.create_task(_resolve_and_enqueue(j)) for j in jobs]
                )
                for _ in range(workers): await queue.put(None)
                await queue.join()
                await asyncio.gather(*worker_tasks)
            finally:
                refresh_task.cancel()
                try: await refresh_task
                except asyncio.CancelledError: pass
                live.update(ui.render())

    console.print()
    console.print(build_summary(jobs, time.monotonic() - t0))
    console.print(f"  [dim]Saved → {out_dir.resolve()}[/]\n")
    return 1 if any(j.status == Status.ERROR for j in jobs) else 0

# ── entry ──────────────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Bunkr downloader — search, select, download.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "  python bunkdl.py                        # search mode\n"
            "  python bunkdl.py https://bunkr.cr/a/X  # direct\n"
            "  python bunkdl.py <url> -v -j 4          # videos, 4 workers"
        ),
    )
    ap.add_argument("url",        nargs="?",       help="Bunkr album or file URL")
    ap.add_argument("-s","--search", action="store_true", help="Search balbums.st")
    ap.add_argument("-o","--output", default="./bunkr_downloads")
    ap.add_argument("-j","--jobs",   type=int, default=2,  help="Concurrent downloads")
    ap.add_argument("-r","--resolve",type=int, default=6,  help="Concurrent resolves")
    flt = ap.add_mutually_exclusive_group()
    flt.add_argument("-a","--all",    action="store_const", const=MediaFilter.ALL,   dest="media")
    flt.add_argument("-v","--videos", action="store_const", const=MediaFilter.VIDEO, dest="media")
    flt.add_argument("-i","--images", action="store_const", const=MediaFilter.IMAGE, dest="media")
    flt.add_argument("-f","--files",  action="store_const", const=MediaFilter.FILE,  dest="media")
    ap.set_defaults(media=MediaFilter.ALL)

    args         = ap.parse_args()
    media_filter = args.media

    if args.search or not args.url:
        result = interactive_search()
        if result is None:
            console.print("  [yellow]Aborted.[/]\n"); sys.exit(0)
        url, media_filter = result
    else:
        url = args.url

    try:
        code = asyncio.run(run(
            source_url=url.strip(), base_out=Path(args.output),
            media_filter=media_filter, workers=max(1, args.jobs),
            resolve_workers=max(1, args.resolve),
        ))
    except KeyboardInterrupt:
        console.print("\n  [yellow]Cancelled.[/]\n"); code = 130

    sys.exit(code)

if __name__ == "__main__":
    main()

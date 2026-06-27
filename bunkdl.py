#!/usr/bin/env python3
"""
Bunkr downloader — albums & single files with live table UI.

Usage:
    python bunkdl.py https://bunkr.cr/a/v1u9TeSn -a
    python bunkdl.py https://bunkr.cr/a/F6O66yio -i
    python bunkdl.py https://bunkr.cr/f/ZhwqKJGYjWJcm -f
    python bunkdl.py <url> -v -o ./downloads -j 2

Filters (pick one):
    -a  all media (default)
    -v  videos only
    -i  images only
    -f  other files only (.rar, .zip, etc.)
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse, urlunparse

import aiohttp
from aiohttp import ClientTimeout
from bs4 import BeautifulSoup
from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

BUNKR_BASE = "https://bunkr.cr"
ALBUM_RE = re.compile(r"https?://(?:www\.)?bunkr\.cr/a/[\w-]+", re.I)
FILE_PAGE_RE = re.compile(r"https?://(?:www\.)?bunkr\.cr/[fvi]/[^/\s?#]+", re.I)
FILE_PATH_RE = re.compile(r"^/[fvi]/[^/]+$")
DL_PAGE_RE = re.compile(r"https?://dl\.bunkr\.cr/file/(\d+)", re.I)

VIDEO_EXT = re.compile(r"\.(mp4|webm|mov|avi|mkv|m4v|wmv|flv)$", re.I)
IMAGE_EXT = re.compile(r"\.(jpe?g|png|gif|webp|bmp|tiff?|heic|avif)$", re.I)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://bunkr.cr/",
}

console = Console()


class MediaKind(str, Enum):
    VIDEO = "video"
    IMAGE = "image"
    FILE = "file"


class MediaFilter(str, Enum):
    ALL = "all"
    VIDEO = "video"
    IMAGE = "image"
    FILE = "file"


class Status(str, Enum):
    PENDING = "pending"
    RESOLVING = "resolving"
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    DONE = "done"
    ERROR = "error"
    SKIPPED = "skipped"


def min_valid_bytes(kind: MediaKind) -> int:
    if kind == MediaKind.VIDEO:
        return 512 * 1024
    if kind == MediaKind.IMAGE:
        return 512
    return 64


@dataclass
class Job:
    index: int
    filename: str
    page_url: str
    kind: MediaKind
    direct_url: Optional[str] = None
    status: Status = Status.PENDING
    progress: float = 0.0
    downloaded: int = 0
    total: int = 0
    speed: float = 0.0
    error: str = ""
    dest: Optional[Path] = None
    dirty: bool = True

    def touch(self) -> None:
        self.dirty = True


def classify_filename(name: str) -> MediaKind:
    if VIDEO_EXT.search(name):
        return MediaKind.VIDEO
    if IMAGE_EXT.search(name):
        return MediaKind.IMAGE
    return MediaKind.FILE


def classify_item_element(item) -> MediaKind:
    for span in item.find_all("span", class_=True):
        classes = " ".join(span.get("class", []))
        if "type-Video" in classes:
            return MediaKind.VIDEO
        if "type-Image" in classes:
            return MediaKind.IMAGE
        if "type-File" in classes:
            return MediaKind.FILE
    name = item.get("title") or ""
    return classify_filename(name)


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "_", name.strip())
    return name[:200] or "download"


def sanitize_folder(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "_", name.strip())
    name = name.strip(". ")
    return name[:120] or "bunkr_album"


def fmt_bytes(n: int) -> str:
    if n <= 0:
        return "—"
    val = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if val < 1024:
            return f"{val:.1f} {unit}" if unit != "B" else f"{int(val)} B"
        val /= 1024
    return f"{val:.1f} TB"


def fmt_speed(bps: float) -> str:
    if bps <= 0:
        return "—"
    return f"{fmt_bytes(int(bps))}/s"


def status_style(status: Status) -> str:
    return {
        Status.PENDING: "dim",
        Status.RESOLVING: "cyan",
        Status.QUEUED: "yellow",
        Status.DOWNLOADING: "bold blue",
        Status.DONE: "bold green",
        Status.ERROR: "bold red",
        Status.SKIPPED: "magenta",
    }.get(status, "white")


def matches_filter(kind: MediaKind, flt: MediaFilter) -> bool:
    if flt == MediaFilter.ALL:
        return True
    return kind.value == flt.value


async def fetch_text(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(url, allow_redirects=True) as resp:
        if resp.status == 200:
            return await resp.text()
    return ""


def parse_album_items(html: str) -> tuple[Optional[str], list[dict]]:
    soup = BeautifulSoup(html, "lxml")
    title = None
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(strip=True)

    items: list[dict] = []
    seen: set[str] = set()

    for item in soup.select("div.theItem"):
        name = (item.get("title") or "").strip()
        link = item.find("a", href=FILE_PATH_RE)
        if not link:
            continue
        page_url = urljoin(BUNKR_BASE, link["href"])
        if page_url in seen:
            continue
        seen.add(page_url)
        kind = classify_item_element(item)
        items.append({"filename": name or page_url.rsplit("/", 1)[-1], "page_url": page_url, "kind": kind})

    if items:
        return title, items

    for a in soup.find_all("a", href=FILE_PATH_RE):
        page_url = urljoin(BUNKR_BASE, a["href"])
        if page_url in seen:
            continue
        seen.add(page_url)
        name = a.get_text(strip=True) or page_url.rsplit("/", 1)[-1]
        items.append({"filename": name, "page_url": page_url, "kind": classify_filename(name)})

    return title, items


def parse_single_file_page(html: str, page_url: str) -> tuple[Optional[str], list[dict]]:
    soup = BeautifulSoup(html, "lxml")
    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else None
    filename = title or page_url.rsplit("/", 1)[-1]
    kind = classify_filename(filename)
    return title, [{"filename": filename, "page_url": page_url, "kind": kind}]


def parse_file_download_page(html: str) -> Optional[str]:
    soup = BeautifulSoup(html, "lxml")
    for a in soup.find_all("a", href=True):
        m = DL_PAGE_RE.match(a["href"])
        if m:
            return a["href"]
    m = DL_PAGE_RE.search(html)
    return m.group(0) if m else None


def _file_id_from_dl_url(dl_url: str) -> Optional[str]:
    m = DL_PAGE_RE.match(dl_url)
    return m.group(1) if m else None


async def resolve_signed_cdn_url(
    session: aiohttp.ClientSession,
    file_id: str,
    original_name: str = "",
) -> Optional[str]:
    api_headers = {
        **HEADERS,
        "Referer": "https://dl.bunkr.cr/",
        "Origin": "https://dl.bunkr.cr",
        "Content-Type": "application/json",
    }
    try:
        async with session.post(
            "https://dl.bunkr.cr/api/_001_v2",
            json={"id": file_id},
            headers=api_headers,
        ) as resp:
            if resp.status != 200:
                return None
            meta = await resp.json()

        base = meta.get("mediafiles", "") + meta.get("path", "")
        if not base:
            return None
        if base.startswith("//"):
            base = "https:" + base
        elif not base.startswith("http"):
            base = "https://" + base.lstrip("/")

        name = meta.get("original") or original_name
        parsed = urlparse(base)
        qs = parse_qs(parsed.query)
        if name:
            qs["n"] = [name]

        sign_url = "https://glb-apisign.cdn.cr/sign?path=" + quote(parsed.path, safe="")
        async with session.get(sign_url) as sign_resp:
            if sign_resp.status != 200:
                return None
            sig = await sign_resp.json()

        qs["token"] = [sig["token"]]
        qs["ex"] = [str(sig["ex"])]
        return urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))
    except Exception:
        return None


async def resolve_download_url(
    session: aiohttp.ClientSession,
    page_url: str,
    filename: str = "",
) -> Optional[str]:
    html = await fetch_text(session, page_url)
    if not html:
        return None
    dl_page = parse_file_download_page(html)
    if not dl_page:
        return None
    file_id = _file_id_from_dl_url(dl_page)
    if not file_id:
        return None
    return await resolve_signed_cdn_url(session, file_id, filename)


def job_row_cells(job: Job) -> tuple[str, str, str, str, str, str, str]:
    kind_tag = job.kind.value[:3].upper()
    name = job.filename
    if len(name) > 34:
        name = name[:31] + "…"

    if job.status == Status.DOWNLOADING and job.total > 0:
        prog = f"{job.progress:.0f}%"
        size = f"{fmt_bytes(job.downloaded)} / {fmt_bytes(job.total)}"
    elif job.status == Status.DONE:
        prog = "100%"
        size = fmt_bytes(job.total or job.downloaded)
    elif job.status == Status.SKIPPED:
        prog = "—"
        size = "exists"
    else:
        prog = "—"
        size = fmt_bytes(job.total) if job.total else "—"

    speed = fmt_speed(job.speed) if job.status == Status.DOWNLOADING else "—"
    st = job.status.value
    if job.error and job.status == Status.ERROR:
        st = "error"
    return (str(job.index), kind_tag, name, st, prog, size, speed)


class LiveJobTable:
    """Table display that only rebuilds rows marked dirty."""

    def __init__(self, jobs: list[Job], title: str, source_url: str, out_dir: Path):
        self.jobs = jobs
        self.title = title
        self.source_url = source_url
        self.out_dir = out_dir
        self._row_cache: list[tuple] = [() for _ in jobs]
        self._stats_dirty = True

    def _stats_line(self) -> str:
        done = sum(1 for j in self.jobs if j.status == Status.DONE)
        skipped = sum(1 for j in self.jobs if j.status == Status.SKIPPED)
        errors = sum(1 for j in self.jobs if j.status == Status.ERROR)
        active = sum(1 for j in self.jobs if j.status == Status.DOWNLOADING)
        queued = sum(
            1 for j in self.jobs if j.status in (Status.QUEUED, Status.PENDING, Status.RESOLVING)
        )
        return (
            f"Done {done} · Skipped {skipped} · Errors {errors} · "
            f"Active {active} · Queue {queued} · Total {len(self.jobs)}"
        )

    def _build_header_table(self) -> Table:
        t = Table(
            box=box.SIMPLE_HEAD,
            expand=True,
            show_header=True,
            header_style="bold cyan",
            border_style="bright_black",
            padding=(0, 1),
        )
        t.add_column("#", width=4, justify="right", style="dim")
        t.add_column("T", width=3)
        t.add_column("File", min_width=26, no_wrap=True)
        t.add_column("Status", width=11)
        t.add_column("Prog", width=6, justify="right")
        t.add_column("Size", width=16, justify="right")
        t.add_column("Speed", width=11, justify="right")
        return t

    def render(self, force: bool = False) -> Group:
        body = self._build_header_table()
        any_dirty = force or self._stats_dirty

        for job in self.jobs:
            cells = job_row_cells(job)
            if force or job.dirty or cells != self._row_cache[job.index - 1]:
                self._row_cache[job.index - 1] = cells
                job.dirty = False
                any_dirty = True

            idx, kind, name, st, prog, size, speed = cells
            body.add_row(
                idx,
                kind,
                name,
                Text(st, style=status_style(job.status)),
                prog,
                size,
                speed,
            )

        header = Panel(
            f"[bold]{self.title or 'Bunkr'}[/]\n"
            f"[dim]{self.source_url}[/]\n"
            f"[dim]→ {self.out_dir}[/]",
            border_style="bright_black",
            padding=(0, 1),
        )
        footer = Text(self._stats_line(), style="dim")
        self._stats_dirty = False
        return Group(header, body, footer)

    def mark_stats_dirty(self) -> None:
        self._stats_dirty = True


def build_footer(jobs: list[Job]) -> Panel:
    total_dl = sum(j.downloaded for j in jobs if j.status == Status.DONE)
    return Panel(
        " · ".join([
            f"[green]Completed[/]: {sum(1 for j in jobs if j.status == Status.DONE)}",
            f"[magenta]Skipped[/]: {sum(1 for j in jobs if j.status == Status.SKIPPED)}",
            f"[red]Failed[/]: {sum(1 for j in jobs if j.status == Status.ERROR)}",
            f"[blue]Downloaded[/]: {fmt_bytes(total_dl)}",
        ]),
        title="Summary",
        border_style="bright_black",
    )


async def download_file(
    session: aiohttp.ClientSession,
    job: Job,
    out_dir: Path,
    display: LiveJobTable,
) -> None:
    dest = out_dir / sanitize_filename(job.filename)
    job.dest = dest
    min_sz = min_valid_bytes(job.kind)

    if dest.exists() and dest.stat().st_size >= min_sz:
        job.status = Status.SKIPPED
        job.total = dest.stat().st_size
        job.downloaded = job.total
        job.progress = 100.0
        job.touch()
        display.mark_stats_dirty()
        return

    if dest.exists() and dest.stat().st_size < min_sz:
        dest.unlink(missing_ok=True)

    if not job.direct_url:
        job.status = Status.ERROR
        job.error = "No download URL"
        job.touch()
        display.mark_stats_dirty()
        return

    job.status = Status.DOWNLOADING
    job.touch()
    display.mark_stats_dirty()
    tmp = dest.with_suffix(dest.suffix + ".part")

    try:
        dl_headers = {**HEADERS, "Referer": "https://dl.bunkr.cr/"}
        async with session.get(job.direct_url, allow_redirects=True, headers=dl_headers) as resp:
            if resp.status != 200:
                job.status = Status.ERROR
                job.error = f"HTTP {resp.status}"
                job.touch()
                display.mark_stats_dirty()
                return

            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "text/html" in ctype:
                job.status = Status.ERROR
                job.error = "Got HTML, not file"
                job.touch()
                display.mark_stats_dirty()
                return

            job.total = int(resp.headers.get("Content-Length", 0))
            job.downloaded = 0
            last_tick = time.monotonic()
            last_bytes = 0
            last_ui = 0.0

            with open(tmp, "wb") as f:
                async for chunk in resp.content.iter_chunked(256 * 1024):
                    f.write(chunk)
                    job.downloaded += len(chunk)
                    now = time.monotonic()
                    if job.total > 0:
                        job.progress = min(99.9, job.downloaded / job.total * 100)
                    dt = now - last_tick
                    if dt >= 0.5:
                        job.speed = (job.downloaded - last_bytes) / dt if dt else 0
                        last_bytes = job.downloaded
                        last_tick = now
                    if now - last_ui >= 0.2:
                        job.touch()
                        last_ui = now

        if job.downloaded < min_sz:
            tmp.unlink(missing_ok=True)
            job.status = Status.ERROR
            job.error = f"Too small ({fmt_bytes(job.downloaded)})"
            job.touch()
            display.mark_stats_dirty()
            return

        tmp.rename(dest)
        job.progress = 100.0
        job.speed = 0.0
        job.status = Status.DONE
        job.touch()
        display.mark_stats_dirty()
    except Exception as e:
        job.status = Status.ERROR
        job.error = str(e)[:80]
        job.touch()
        display.mark_stats_dirty()
        if tmp.exists():
            tmp.unlink(missing_ok=True)


async def load_entries(
    session: aiohttp.ClientSession,
    url: str,
) -> tuple[Optional[str], list[dict], str]:
    """Return title, entries, normalized source url."""
    url = url.strip()
    if not url.startswith("http"):
        url = "https://" + url

    if ALBUM_RE.match(url):
        html = await fetch_text(session, url)
        if not html:
            raise RuntimeError("Failed to load album page")
        title, entries = parse_album_items(html)
        return title, entries, url

    if FILE_PAGE_RE.match(url):
        html = await fetch_text(session, url)
        if not html:
            raise RuntimeError("Failed to load file page")
        title, entries = parse_single_file_page(html, url)
        return title, entries, url

    raise RuntimeError("URL must be a Bunkr album (/a/) or file (/f/, /i/, /v/) page")


async def run(
    source_url: str,
    base_out: Path,
    media_filter: MediaFilter,
    workers: int,
    resolve_workers: int,
) -> int:
    timeout = ClientTimeout(total=None, connect=30, sock_read=300)

    async with aiohttp.ClientSession(headers=HEADERS, timeout=timeout) as session:
        console.print(f"[cyan]Fetching…[/] {source_url}")
        try:
            album_title, entries, norm_url = await load_entries(session, source_url)
        except RuntimeError as e:
            console.print(f"[red]{e}[/]")
            return 1

        entries = [e for e in entries if matches_filter(e["kind"], media_filter)]
        if not entries:
            console.print(f"[yellow]No items match filter [bold]{media_filter.value}[/].[/]")
            return 1

        folder_name = sanitize_folder(album_title or "bunkr_download")
        out_dir = base_out / folder_name
        out_dir.mkdir(parents=True, exist_ok=True)

        jobs = [
            Job(
                index=i + 1,
                filename=e["filename"],
                page_url=e["page_url"],
                kind=e["kind"],
            )
            for i, e in enumerate(entries)
        ]

        kinds = {j.kind.value for j in jobs}
        console.print(
            f"[green]Found {len(jobs)} item(s)[/]"
            f" ({', '.join(sorted(kinds))})"
            f" → [bold]{out_dir}[/]"
        )

        display = LiveJobTable(jobs, album_title or folder_name, norm_url, out_dir)
        resolve_sem = asyncio.Semaphore(resolve_workers)

        async def resolve_job(job: Job) -> None:
            job.status = Status.RESOLVING
            job.touch()
            display.mark_stats_dirty()
            async with resolve_sem:
                job.direct_url = await resolve_download_url(session, job.page_url, job.filename)
            if job.direct_url:
                job.status = Status.QUEUED
            else:
                job.status = Status.ERROR
                job.error = "Download link not found"
            job.touch()
            display.mark_stats_dirty()

        dl_sem = asyncio.Semaphore(workers)
        queue: asyncio.Queue[Optional[Job]] = asyncio.Queue()

        async def download_worker() -> None:
            while True:
                job = await queue.get()
                if job is None:
                    queue.task_done()
                    break
                async with dl_sem:
                    await download_file(session, job, out_dir, display)
                queue.task_done()

        worker_tasks = [asyncio.create_task(download_worker()) for _ in range(workers)]

        with Live(console=console, refresh_per_second=12, transient=False) as live:
            live.update(display.render(force=True))

            async def refresh_loop() -> None:
                while True:
                    if any(j.dirty for j in jobs) or display._stats_dirty:
                        live.update(display.render())
                    await asyncio.sleep(0.12)

            refresh_task = asyncio.create_task(refresh_loop())

            try:
                await asyncio.gather(*[resolve_job(j) for j in jobs])

                for job in jobs:
                    if job.status == Status.QUEUED:
                        await queue.put(job)

                for _ in range(workers):
                    await queue.put(None)

                await queue.join()
                await asyncio.gather(*worker_tasks)
            finally:
                refresh_task.cancel()
                try:
                    await refresh_task
                except asyncio.CancelledError:
                    pass
                live.update(display.render(force=True))

        console.print(build_footer(jobs))
        console.print(f"[dim]Saved to {out_dir.resolve()}[/]")
        return 1 if any(j.status == Status.ERROR for j in jobs) else 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download Bunkr albums and files with a live table UI.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python bunkdl.py https://bunkr.cr/a/v1u9TeSn -v
  python bunkdl.py https://bunkr.cr/a/F6O66yio -i
  python bunkdl.py https://bunkr.cr/f/ZhwqKJGYjWJcm -f
  python bunkdl.py https://bunkr.cr/a/v1u9TeSn -a -j 3
        """,
    )
    parser.add_argument("url", nargs="?", help="Bunkr album (/a/) or file (/f/) URL")
    parser.add_argument("-o", "--output", default="./bunkr_downloads", help="Base output directory")
    parser.add_argument("-j", "--jobs", type=int, default=2, help="Concurrent downloads")
    parser.add_argument("-r", "--resolve", type=int, default=6, help="Concurrent resolves")

    filt = parser.add_mutually_exclusive_group()
    filt.add_argument("-a", "--all", action="store_const", const=MediaFilter.ALL, dest="media", help="All media (default)")
    filt.add_argument("-v", "--videos", action="store_const", const=MediaFilter.VIDEO, dest="media", help="Videos only")
    filt.add_argument("-i", "--images", action="store_const", const=MediaFilter.IMAGE, dest="media", help="Images only")
    filt.add_argument("-f", "--files", action="store_const", const=MediaFilter.FILE, dest="media", help="Other files only")
    parser.set_defaults(media=MediaFilter.ALL)

    args = parser.parse_args()
    url = args.url or Prompt.ask("[cyan]Bunkr URL[/] (album /a/ or file /f/)")

    try:
        code = asyncio.run(
            run(
                source_url=url.strip(),
                base_out=Path(args.output),
                media_filter=args.media,
                workers=max(1, args.jobs),
                resolve_workers=max(1, args.resolve),
            )
        )
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled.[/]")
        code = 130

    sys.exit(code)


if __name__ == "__main__":
    main()

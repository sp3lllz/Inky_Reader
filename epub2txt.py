#!/usr/bin/env python3
"""
epub2txt — EPUB to plain-text converter for the Inky E-Reader
==============================================================

Converts DRM-free .epub files into clean .txt files ready to load
onto your Raspberry Pi e-reader.

Usage
-----
    Single file:    python3 epub2txt.py book.epub
    Multiple files: python3 epub2txt.py book1.epub book2.epub
    Whole folder:   python3 epub2txt.py ~/Downloads/epubs/
    Custom output:  python3 epub2txt.py book.epub -o ~/books/
    SCP to Pi:      python3 epub2txt.py book.epub --scp pi@raspberrypi.local:~/books/

Dependencies
------------
    pip install beautifulsoup4 lxml

How it works
------------
    EPUB files are ZIP archives containing XHTML documents.  This script:
    1. Reads META-INF/container.xml to locate the OPF package file
    2. Parses the OPF <spine> to determine reading order
    3. Extracts text from each XHTML chapter in order
    4. Cleans up whitespace, preserves paragraph breaks
    5. Writes a single UTF-8 .txt file
"""

from __future__ import annotations

import argparse
import html
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

# Optional but recommended — gives much better HTML-to-text conversion
try:
    from bs4 import BeautifulSoup, NavigableString, Tag

    HAS_BS4 = True
    # Suppress harmless warning when parsing XHTML with the HTML parser
    import warnings
    try:
        from bs4 import XMLParsedAsHTMLWarning
        warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
    except ImportError:
        pass
except ImportError:
    HAS_BS4 = False


# ---------------------------------------------------------------------------
# EPUB parsing
# ---------------------------------------------------------------------------

# Common XML namespaces found in EPUB packages
NS = {
    "container": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "xhtml": "http://www.w3.org/1999/xhtml",
}


def _find_opf_path(zf: zipfile.ZipFile) -> str:
    """Locate the OPF package file via META-INF/container.xml."""
    try:
        container_xml = zf.read("META-INF/container.xml")
    except KeyError:
        raise ValueError("Not a valid EPUB: missing META-INF/container.xml")

    root = ET.fromstring(container_xml)
    rootfile = root.find(".//container:rootfile", NS)
    if rootfile is None:
        # Try without namespace (some EPUBs are sloppy)
        rootfile = root.find(".//{*}rootfile")
    if rootfile is None:
        raise ValueError("Cannot find rootfile in container.xml")

    return rootfile.attrib["full-path"]


def _parse_opf(zf: zipfile.ZipFile, opf_path: str) -> tuple[str, list[str], dict]:
    """Parse the OPF and return (title, spine_href_list, metadata).

    Returns the book title, an ordered list of content-document paths
    (resolved relative to the OPF directory), and a dict of basic metadata.
    """
    opf_dir = str(Path(opf_path).parent)
    if opf_dir == ".":
        opf_dir = ""

    opf_xml = zf.read(opf_path)
    root = ET.fromstring(opf_xml)

    # --- Metadata ---
    metadata: dict = {}
    title_el = root.find(".//dc:title", NS)
    if title_el is None:
        title_el = root.find(".//{*}title")
    metadata["title"] = title_el.text.strip() if title_el is not None and title_el.text else ""

    creator_el = root.find(".//dc:creator", NS)
    if creator_el is None:
        creator_el = root.find(".//{*}creator")
    metadata["author"] = creator_el.text.strip() if creator_el is not None and creator_el.text else ""

    # --- Manifest (id → href mapping) ---
    manifest: dict[str, str] = {}
    for item in root.findall(".//{*}item"):
        item_id = item.attrib.get("id", "")
        item_href = item.attrib.get("href", "")
        media = item.attrib.get("media-type", "")
        if item_id and item_href:
            manifest[item_id] = item_href

    # --- Spine (ordered list of manifest ids) ---
    spine_ids: list[str] = []
    for itemref in root.findall(".//{*}itemref"):
        idref = itemref.attrib.get("idref", "")
        if idref:
            spine_ids.append(idref)

    # Resolve to file paths inside the ZIP
    spine_paths: list[str] = []
    for sid in spine_ids:
        href = manifest.get(sid, "")
        if not href:
            continue
        if opf_dir:
            full = opf_dir + "/" + href
        else:
            full = href
        spine_paths.append(full)

    title = metadata.get("title", "") or Path(opf_path).stem
    return title, spine_paths, metadata


# ---------------------------------------------------------------------------
# HTML → plain text
# ---------------------------------------------------------------------------

# Block-level elements that should produce paragraph breaks
_BLOCK_TAGS = frozenset({
    "p", "div", "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "li", "tr", "dt", "dd", "section",
    "article", "header", "footer", "figcaption", "pre",
})

# Elements to skip entirely
_SKIP_TAGS = frozenset({"script", "style", "head", "nav", "sup"})

# Heading tags — we'll add a blank line after them for visual separation
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})


def _html_to_text_bs4(markup: str) -> str:
    """Convert HTML/XHTML to plain text using BeautifulSoup."""
    # Strip XML processing instructions (<?xml ...?>)
    markup = re.sub(r"<\?xml[^?]*\?>", "", markup, flags=re.IGNORECASE)
    soup = BeautifulSoup(markup, "lxml")

    # Remove elements we don't want
    for tag_name in _SKIP_TAGS:
        for el in soup.find_all(tag_name):
            el.decompose()

    lines: list[str] = []
    _walk_bs4(soup, lines)
    return _clean_output(lines)


def _walk_bs4(node, lines: list[str]):
    """Recursively walk the DOM tree, appending text lines."""
    if isinstance(node, NavigableString):
        text = str(node)
        # Collapse whitespace within inline text
        text = re.sub(r"[ \t]+", " ", text)
        if text.strip():
            if lines and lines[-1] is not None:
                lines.append(text.strip())
            else:
                lines.append(text.strip())
        return

    if not isinstance(node, Tag):
        return

    tag = node.name.lower() if node.name else ""

    if tag in _SKIP_TAGS:
        return

    is_block = tag in _BLOCK_TAGS
    is_heading = tag in _HEADING_TAGS
    is_br = tag == "br"
    is_hr = tag == "hr"

    if is_hr:
        lines.append("")
        lines.append("* * *")
        lines.append("")
        return

    if is_br:
        lines.append(None)  # sentinel for line break
        return

    if is_block:
        lines.append(None)  # paragraph separator

    for child in node.children:
        _walk_bs4(child, lines)

    if is_block or is_heading:
        lines.append(None)
        if is_heading:
            lines.append(None)  # extra spacing after headings


def _html_to_text_stdlib(markup: str) -> str:
    """Fallback HTML-to-text using only the standard library."""
    # Strip XML processing instructions
    markup = re.sub(r"<\?xml[^?]*\?>", "", markup, flags=re.IGNORECASE)
    # Strip tags with a rough regex approach
    text = re.sub(r"<br\s*/?>", "\n", markup, flags=re.IGNORECASE)
    text = re.sub(r"<hr\s*/?>", "\n* * *\n", text, flags=re.IGNORECASE)
    # Add newlines around block elements
    for tag in _BLOCK_TAGS:
        text = re.sub(rf"</?{tag}[^>]*>", "\n", text, flags=re.IGNORECASE)
    # Strip remaining tags
    text = re.sub(r"<[^>]+>", "", text)
    # Decode entities
    text = html.unescape(text)
    lines = text.split("\n")
    return _clean_output(lines)


def html_to_text(markup: str) -> str:
    """Convert HTML/XHTML to clean plain text."""
    if HAS_BS4:
        return _html_to_text_bs4(markup)
    return _html_to_text_stdlib(markup)


def _clean_output(lines) -> str:
    """Normalise a list of text fragments / None sentinels into clean paragraphs."""
    result: list[str] = []
    for item in lines:
        if item is None:
            # Paragraph break — only add if we haven't just added one
            if result and result[-1] != "":
                result.append("")
        else:
            text = item.strip()
            if text:
                # If the last line is real text, join inline
                if result and result[-1] != "":
                    result[-1] += " " + text
                else:
                    result.append(text)

    # Clean up spacing artefacts from inline elements
    for i, line in enumerate(result):
        if line:
            # Remove spaces before sentence-ending punctuation (from inline tag boundaries)
            result[i] = re.sub(r"\s+([.,;:!?\)\]])", r"\1", line)
            # Remove spaces after opening punctuation
            result[i] = re.sub(r"([\(\[])\s+", r"\1", result[i])
            # Collapse double spaces
            result[i] = re.sub(r"  +", " ", result[i])

    # Strip leading/trailing blank lines
    while result and result[0] == "":
        result.pop(0)
    while result and result[-1] == "":
        result.pop()

    # Collapse runs of 3+ blank lines to 2
    cleaned: list[str] = []
    blank_count = 0
    for line in result:
        if line == "":
            blank_count += 1
            if blank_count <= 2:
                cleaned.append("")
        else:
            blank_count = 0
            cleaned.append(line)

    return "\n".join(cleaned) + "\n"


# ---------------------------------------------------------------------------
# Top-level conversion
# ---------------------------------------------------------------------------

def convert_epub(epub_path: str, output_dir: str | None = None) -> Path:
    """Convert a single .epub file to .txt. Returns the output path."""
    epub_path = str(Path(epub_path).resolve())

    if not zipfile.is_zipfile(epub_path):
        raise ValueError(f"Not a valid ZIP/EPUB file: {epub_path}")

    with zipfile.ZipFile(epub_path, "r") as zf:
        opf_path = _find_opf_path(zf)
        title, spine_paths, metadata = _parse_opf(zf, opf_path)

        # Extract text from each spine document in order
        all_parts: list[str] = []

        # Optionally prepend title and author
        if title:
            all_parts.append(title.upper())
        if metadata.get("author"):
            all_parts.append(f"by {metadata['author']}")
        if title or metadata.get("author"):
            all_parts.append("")
            all_parts.append("* * *")
            all_parts.append("")

        for doc_path in spine_paths:
            try:
                raw = zf.read(doc_path)
            except KeyError:
                # Some EPUBs have slightly wrong paths — try URL-decoded
                from urllib.parse import unquote
                try:
                    raw = zf.read(unquote(doc_path))
                except KeyError:
                    continue

            # Detect encoding (default UTF-8)
            markup = raw.decode("utf-8", errors="replace")
            text = html_to_text(markup)
            if text.strip():
                all_parts.append(text)

    full_text = "\n\n".join(all_parts)

    # Build output filename from the EPUB title or filename
    safe_name = _safe_filename(title or Path(epub_path).stem)
    if output_dir:
        out_dir = Path(output_dir)
    else:
        out_dir = Path(epub_path).parent

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{safe_name}.txt"

    # Avoid overwriting — add a number if needed
    counter = 1
    while out_path.exists():
        out_path = out_dir / f"{safe_name}_{counter}.txt"
        counter += 1

    out_path.write_text(full_text, encoding="utf-8")
    return out_path


def _safe_filename(name: str) -> str:
    """Sanitise a string for use as a filename."""
    # Replace problem characters
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    name = name.strip(". ")
    # Collapse spaces and limit length
    name = re.sub(r"\s+", "_", name)
    if len(name) > 120:
        name = name[:120]
    return name or "untitled"


# ---------------------------------------------------------------------------
# SCP upload
# ---------------------------------------------------------------------------

def scp_upload(files: list[Path], destination: str):
    """Upload converted files to the Pi via SCP."""
    if not shutil.which("scp"):
        print("[error] scp is not installed or not in PATH.", file=sys.stderr)
        return

    for f in files:
        print(f"[scp] Uploading {f.name} → {destination}")
        result = subprocess.run(["scp", str(f), destination])
        if result.returncode == 0:
            print(f"  ✓ Done")
        else:
            print(f"  ✗ Failed (exit code {result.returncode})", file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def gather_epubs(paths: list[str]) -> list[str]:
    """Expand a list of paths into individual .epub file paths."""
    epubs: list[str] = []
    for p in paths:
        path = Path(p).expanduser()
        if path.is_dir():
            for child in sorted(path.rglob("*.epub")):
                if child.is_file():
                    epubs.append(str(child))
        elif path.is_file() and path.suffix.lower() == ".epub":
            epubs.append(str(path))
        else:
            print(f"[warn] Skipping: {p} (not a .epub file or directory)",
                  file=sys.stderr)
    return epubs


def main():
    parser = argparse.ArgumentParser(
        description="Convert DRM-free EPUB files to plain text for the Inky E-Reader.",
        epilog="Examples:\n"
               "  epub2txt.py book.epub\n"
               "  epub2txt.py *.epub -o ~/books/\n"
               "  epub2txt.py ~/Downloads/epubs/ --scp pi@raspberrypi.local:~/books/\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input", nargs="+",
                        help="EPUB file(s) or folder(s) containing EPUBs")
    parser.add_argument("-o", "--output", default=None,
                        help="Output directory for .txt files (default: same folder as each EPUB)")
    parser.add_argument("--scp", default=None, metavar="DEST",
                        help="SCP destination to upload converted files "
                             "(e.g. pi@raspberrypi.local:~/books/)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be converted without writing files")
    args = parser.parse_args()

    epubs = gather_epubs(args.input)

    if not epubs:
        print("No .epub files found.")
        sys.exit(1)

    print(f"Found {len(epubs)} EPUB(s) to convert.\n")

    if args.dry_run:
        for ep in epubs:
            print(f"  Would convert: {ep}")
        sys.exit(0)

    converted: list[Path] = []
    for epub in epubs:
        name = Path(epub).name
        try:
            out = convert_epub(epub, args.output)
            size_kb = out.stat().st_size / 1024
            print(f"  ✓ {name}  →  {out.name}  ({size_kb:.0f} KB)")
            converted.append(out)
        except Exception as exc:
            print(f"  ✗ {name}  →  {exc}", file=sys.stderr)

    print(f"\nConverted {len(converted)}/{len(epubs)} file(s).")

    # SCP upload if requested
    if args.scp and converted:
        print()
        scp_upload(converted, args.scp)

    if not HAS_BS4:
        print(
            "\n[tip] Install beautifulsoup4 + lxml for better text extraction:\n"
            "      pip install beautifulsoup4 lxml",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Batch XML -> (XSLT) -> HTML -> PDF converter.

Features:
- Accepts XML files, directories, or ZIP files as input
- Detects xml-stylesheet processing instruction in each XML to locate XSL file in same directory (href attribute pointing to .xsl/.xslt)
- For ZIP files: extracts contents to temp directory, processes XMLs with their XSL files, outputs PDFs to caller directory (or --out-dir)
- Applies XSLT (lxml)
- Injects optional print CSS controlling page size/margins; auto chooses landscape if HTML body width hint implies wide table.
- Renders PDF via WeasyPrint by default. Optional --engine=chrome uses installed Chrome/Chromium headless (needs `google-chrome` or `chromium` in PATH).
- Produces one PDF per XML (same basename, .pdf extension) in output directory (default: current directory for ZIPs, alongside XML for files, or --out-dir).
- Ignores .csv files.
- Designed to be cross-platform (macOS/Windows/Linux) given Python deps.

Prereqs (default engine weasyprint):
    pip install -r requirements.txt

Chrome engine notes:
    - On macOS: typically /Applications/Google Chrome.app/Contents/MacOS/Google Chrome
      Provide path via --chrome-path if not on PATH.

Limitations:
    - XSLT 1.0 only (which matches your stylesheets).
    - Does not merge PDFs (intentionally per requirements).

"""
from __future__ import annotations
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import pathlib
import zipfile
from typing import Optional, Tuple

try:
    from lxml import etree  # type: ignore
except ImportError as e:
    print(
        "ERROR: Missing dependency 'lxml'.\n"
        "Install dependencies first, e.g.:\n"
        "  python -m pip install -r requirements.txt\n"
        "Or run setup script:\n"
        "  bash setup_env.sh\n"
        f"Original error: {e}"
    )
    sys.exit(1)

# Optional imports guarded (we don't want to fail before CLI parsed)
WEASYPRINT_AVAILABLE = True
try:
    from weasyprint import HTML, CSS
except Exception:
    WEASYPRINT_AVAILABLE = False

PRINT_CSS_TEMPLATE = """
/* Injected print styles */
@page {{ size: {page_size} {orientation}; margin: {margin}; }}
html, body {{
    -weasy-hyphens: auto;
    font-family: 'Yu Mincho', 'Hiragino Mincho ProN', 'Noto Serif CJK JP', serif;
    font-size: 12px;
}}
/* Avoid breaking inside table rows */
tr, td, th {{ page-break-inside: avoid; }}
/* Large tables: allow wrapping */
table {{ word-wrap: break-word; overflow-wrap: break-word; }}
""".strip()

STYLESHEET_PI_PATTERN = re.compile(
    r"<\?xml-stylesheet[^>]*href=['\"]([^'\"]+)['\"][^>]*>", re.IGNORECASE
)
COL_WIDTH_PATTERN = re.compile(
    r'<col\s+width=["\']?(\d+)(?:px)?["\']?(?:\s+span=["\']?(\d+)["\']?)?',
    re.IGNORECASE,
)
COLSPAN_PATTERN = re.compile(r'colspan=["\']?(\d+)["\']?', re.IGNORECASE)
HEAD_END_PATTERN = re.compile(r"</head>", re.IGNORECASE)
TABLE_PATTERN = re.compile(r"<table[^>]*>.*?</table>", re.IGNORECASE | re.DOTALL)


def discover_stylesheet(xml_text: str) -> Optional[str]:
    m = STYLESHEET_PI_PATTERN.search(xml_text)
    if m:
        return m.group(1)
    return None


def load_xml_tree(xml_path: pathlib.Path) -> Tuple[etree._ElementTree, str]:
    """Load XML file and return parsed tree + text for PI extraction."""
    xml_bytes = xml_path.read_bytes()
    parser = etree.XMLParser(remove_blank_text=False)
    tree = etree.fromstring(xml_bytes, parser)
    # Decode only once for stylesheet discovery
    text = xml_bytes.decode("utf-8")
    return etree.ElementTree(tree), text


_xslt_cache: dict[pathlib.Path, etree.XSLT] = {}


def compile_xslt(path: pathlib.Path) -> etree.XSLT:
    if path in _xslt_cache:
        return _xslt_cache[path]
    xslt_doc = etree.parse(str(path))
    transform = etree.XSLT(xslt_doc)
    _xslt_cache[path] = transform
    return transform


def transform(xml_tree: etree._ElementTree, xslt_path: pathlib.Path) -> str:
    transform_fn = compile_xslt(xslt_path)
    result = transform_fn(xml_tree)
    # Ensure we output a full HTML document
    html_str = str(result)
    if not html_str.lstrip().lower().startswith("<html"):
        # Some stylesheets may omit <html> because of output settings.
        html_str = f"<html><body>{html_str}</body></html>"
    return html_str


def guess_page_orientation(html: str) -> Tuple[str, str]:
    """Return (page_size, orientation).
    Heuristic: Calculate total table width from <col> width attributes.
    - If total width >= 1200px: A3 landscape
    - If total width >= 800px: A4 landscape
    - Otherwise: A4 portrait
    Falls back to colspan counting if no width attributes found.
    """
    max_table_width = 0

    # Find all tables and calculate their widths using pre-compiled patterns
    for table_match in TABLE_PATTERN.finditer(html):
        table_html = table_match.group(0)

        table_width = 0
        for m in COL_WIDTH_PATTERN.finditer(table_html):
            width = int(m.group(1))
            span = int(m.group(2)) if m.group(2) else 1
            table_width += width * span

        if table_width > max_table_width:
            max_table_width = table_width

    # Decision based on calculated width
    if max_table_width > 0:
        # Width-based detection (in pixels)
        if max_table_width >= 1200:
            return ("A3", "landscape")
        elif max_table_width >= 800:
            return ("A4", "landscape")
        else:
            return ("A4", "portrait")

    # Fallback: count columns and check colspan if no width found
    col_count = html.count("<col")
    if col_count >= 30:
        return ("A3", "landscape")

    for m in COLSPAN_PATTERN.finditer(html):
        if int(m.group(1)) >= 25:
            return ("A3", "landscape")

    return ("A4", "portrait")


def inject_print_css(html: str, page_size: str, orientation: str, margin: str) -> str:
    """Inject print CSS into HTML document."""
    css_block = PRINT_CSS_TEMPLATE.format(
        page_size=page_size, orientation=orientation, margin=margin
    )
    # If there's already a <head>, insert before </head> using pre-compiled pattern
    if "</head>" in html.lower():

        def repl(match):
            return f"<style>{css_block}</style>\n{match.group(0)}"

        html = HEAD_END_PATTERN.sub(repl, html, count=1)
    else:
        html = f"<html><head><style>{css_block}</style></head>{html}</html>"
    return html


def render_pdf_weasy(html: str, out_pdf: pathlib.Path):
    if not WEASYPRINT_AVAILABLE:
        raise RuntimeError(
            "WeasyPrint not installed. Install dependencies or use --engine=chrome"
        )
    HTML(string=html, base_url=str(out_pdf.parent)).write_pdf(str(out_pdf))


def find_chrome(explicit_path: Optional[str]) -> Optional[str]:
    """Find Chrome/Chromium executable on the system."""
    if explicit_path:
        return explicit_path if os.path.exists(explicit_path) else None
    candidates = [
        "google-chrome",
        "chrome",
        "chromium",
        "chromium-browser",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ]
    for c in candidates:
        which_result = shutil.which(c)
        if which_result:
            return which_result
        if os.path.exists(c):
            return c
    return None


def render_pdf_chrome(html: str, out_pdf: pathlib.Path, chrome_path: Optional[str]):
    """Render HTML to PDF using Chrome/Chromium headless."""
    chrome = find_chrome(chrome_path)
    if not chrome:
        raise RuntimeError(
            "Chrome/Chromium executable not found; specify --chrome-path"
        )
    with tempfile.TemporaryDirectory() as tmp:
        html_path = pathlib.Path(tmp) / "temp.html"
        html_path.write_text(html, encoding="utf-8")
        cmd = [
            chrome,
            "--headless",
            "--disable-gpu",
            f"--print-to-pdf={out_pdf}",
            str(html_path),
        ]
        subprocess.run(cmd, check=True)


def fix_zip_filename_encoding(filename: str, flag_bits: int) -> str:
    """Fix ZIP filename encoding issues for Japanese characters.

    Args:
        filename: Original filename from ZIP
        flag_bits: ZIP flag bits (0x800 = UTF-8 flag)

    Returns:
        Corrected filename with proper encoding
    """
    try:
        # If UTF-8 flag is set, use as-is
        if flag_bits & 0x800:
            return filename

        # Try to re-decode from CP437 to UTF-8 or Shift-JIS
        try:
            # Encode back to bytes using CP437, then decode as UTF-8
            return filename.encode("cp437").decode("utf-8")
        except (UnicodeDecodeError, UnicodeEncodeError):
            try:
                # Try Shift-JIS (common for Japanese Windows)
                return filename.encode("cp437").decode("shift-jis")
            except (UnicodeDecodeError, UnicodeEncodeError):
                # Fall back to original
                return filename
    except Exception:
        # If all else fails, keep original
        return filename


def process_xml_to_pdf(
    xml_path: pathlib.Path, out_dir: pathlib.Path, args
) -> Optional[pathlib.Path]:
    """Core logic to process a single XML file to PDF.

    Args:
        xml_path: Path to XML file
        out_dir: Output directory for PDF
        args: Command line arguments

    Returns:
        Path to generated PDF, or None if processing failed
    """
    tree, xml_text = load_xml_tree(xml_path)
    stylesheet_href = discover_stylesheet(xml_text)
    if not stylesheet_href:
        print(f"[WARN] No xml-stylesheet PI in {xml_path.name}; skipping")
        return None

    xslt_path = (xml_path.parent / stylesheet_href).resolve()
    if not xslt_path.exists():
        print(f"[ERROR] Stylesheet {stylesheet_href} not found for {xml_path.name}")
        return None

    html = transform(tree, xslt_path)
    page_size, orientation = guess_page_orientation(html)
    if args.force_page_size:
        page_size = args.force_page_size
    if args.force_orientation:
        orientation = args.force_orientation
    html = inject_print_css(html, page_size, orientation, args.margin)

    out_dir.mkdir(parents=True, exist_ok=True)
    out_pdf = out_dir / (xml_path.stem + ".pdf")

    if args.engine == "weasyprint":
        render_pdf_weasy(html, out_pdf)
    else:
        render_pdf_chrome(html, out_pdf, args.chrome_path)

    print(f"[OK] {xml_path.name} -> {out_pdf.name} ({page_size} {orientation})")
    return out_pdf


def process_zip_file(zip_path: pathlib.Path, args) -> int:
    """Extract ZIP file to temp directory, process XMLs, output PDFs to out_dir or current dir.

    Args:
        zip_path: Path to ZIP file
        args: Command line arguments

    Returns:
        Count of PDFs generated
    """
    if not zipfile.is_zipfile(zip_path):
        print(f"[ERROR] {zip_path.name} is not a valid ZIP file")
        return 0

    # Default output directory: caller's current directory
    out_dir = pathlib.Path(args.out_dir) if args.out_dir else pathlib.Path.cwd()

    pdf_count = 0
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = pathlib.Path(tmp_dir)

        # Extract ZIP contents with proper encoding handling
        print(f"[INFO] Extracting {zip_path.name}...")
        with zipfile.ZipFile(zip_path, "r") as zf:
            for zip_info in zf.infolist():
                # Fix filename encoding for Japanese characters
                zip_info.filename = fix_zip_filename_encoding(
                    zip_info.filename, zip_info.flag_bits
                )
                zf.extract(zip_info, tmp_path)

        # Find all XML files in extracted contents
        xml_files = list(tmp_path.rglob("*.xml"))
        if not xml_files:
            print(f"[WARN] No XML files found in {zip_path.name}")
            return 0

        print(f"[INFO] Found {len(xml_files)} XML file(s) in {zip_path.name}")

        # Process each XML file using common processing function
        for xml_file in sorted(xml_files):
            if process_xml_to_pdf(xml_file, out_dir, args):
                pdf_count += 1

    return pdf_count


def process_file(xml_path: pathlib.Path, args) -> Optional[pathlib.Path]:
    """Process a single XML file to PDF.

    Args:
        xml_path: Path to XML file
        args: Command line arguments

    Returns:
        Path to generated PDF, or None if not an XML file or processing failed
    """
    if xml_path.suffix.lower() != ".xml":
        return None

    # Default output directory: same as XML file
    out_dir = pathlib.Path(args.out_dir) if args.out_dir else xml_path.parent
    return process_xml_to_pdf(xml_path, out_dir, args)


def main():
    parser = argparse.ArgumentParser(
        description="Transform XML+XSLT (xml-stylesheet PI) into PDFs. Supports ZIP files."
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=["."],
        help="XML files, ZIP files, or directories (default: current dir)",
    )
    parser.add_argument(
        "--out-dir",
        help="Output directory for PDFs (default: current dir for ZIPs, alongside XML for files)",
    )
    parser.add_argument(
        "--engine",
        choices=["weasyprint", "chrome"],
        default="weasyprint",
        help="PDF rendering engine",
    )
    parser.add_argument(
        "--chrome-path",
        help="Explicit path to Chrome/Chromium binary when using --engine=chrome",
    )
    parser.add_argument(
        "--force-page-size", help="Override detected page size (e.g., A4, A3)"
    )
    parser.add_argument(
        "--force-orientation",
        choices=["portrait", "landscape"],
        help="Override detected orientation",
    )
    parser.add_argument(
        "--margin", default="10mm", help="Page margin (CSS size) default=10mm"
    )
    args = parser.parse_args()

    any_processed = False
    for p in args.paths:
        p_path = pathlib.Path(p)
        if p_path.is_dir():
            for sub in sorted(p_path.iterdir()):
                if process_file(sub, args):
                    any_processed = True
        elif p_path.suffix.lower() == ".zip":
            # Handle ZIP file
            count = process_zip_file(p_path, args)
            if count > 0:
                any_processed = True
        else:
            if process_file(p_path, args):
                any_processed = True
    if not any_processed:
        print("[INFO] No XML files with xml-stylesheet PI were processed.")


if __name__ == "__main__":
    main()

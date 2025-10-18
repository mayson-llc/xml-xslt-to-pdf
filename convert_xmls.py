#!/usr/bin/env python3
"""
Batch XML -> (XSLT) -> HTML -> PDF converter.

Features:
- Detects xml-stylesheet processing instruction in each XML to locate XSL file in same directory (href attribute pointing to .xsl/.xslt)
- Applies XSLT (lxml)
- Injects optional print CSS controlling page size/margins; auto chooses landscape if HTML body width hint implies wide table.
- Renders PDF via WeasyPrint by default. Optional --engine=chrome uses installed Chrome/Chromium headless (needs `google-chrome` or `chromium` in PATH).
- Produces one PDF per XML (same basename, .pdf extension) in output directory (default: current directory or --out-dir).
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
import sys
import tempfile
import pathlib
import subprocess
from typing import Optional, Tuple

try:
    from lxml import etree  # type: ignore
except ImportError as e:
    print("ERROR: Missing dependency 'lxml'.\n"
          "Install dependencies first, e.g.:\n"
          "  python -m pip install -r requirements.txt\n"
          "Or run setup script:\n"
          "  bash setup_env.sh\n"
          f"Original error: {e}")
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

STYLESHEET_PI_PATTERN = re.compile(r"<\?xml-stylesheet[^>]*href=['\"]([^'\"]+)['\"][^>]*>" , re.IGNORECASE)


def discover_stylesheet(xml_text: str) -> Optional[str]:
    m = STYLESHEET_PI_PATTERN.search(xml_text)
    if m:
        return m.group(1)
    return None


def load_xml_tree(xml_path: pathlib.Path) -> Tuple[etree._ElementTree, str]:
    text = xml_path.read_text(encoding='utf-8')
    parser = etree.XMLParser(remove_blank_text=False)
    tree = etree.fromstring(text.encode('utf-8'), parser)
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
    if not html_str.lstrip().lower().startswith('<html'):
        # Some stylesheets may omit <html> because of output settings.
        html_str = f"<html><body>{html_str}</body></html>"
    return html_str


def guess_page_orientation(html: str) -> Tuple[str, str]:
    """Return (page_size, orientation).
    Heuristic: Calculate total table width from <col> width attributes.
    - If total width >= 1200px (or any col > 1000px): A3 landscape
    - If total width >= 800px: A4 landscape  
    - Otherwise: A4 portrait
    Falls back to colspan counting if no width attributes found.
    """
    max_table_width = 0
    
    # Extract all col width attributes and calculate total per table
    # Match: <col width="123px" /> or <col width="123" span="4"/>
    col_pattern = re.compile(r'<col\s+width=["\']?(\d+)(?:px)?["\']?(?:\s+span=["\']?(\d+)["\']?)?', re.IGNORECASE)
    
    # Find all tables and calculate their widths
    table_sections = re.split(r'<table[^>]*>', html, flags=re.IGNORECASE)
    for section in table_sections:
        table_end = section.find('</table>')
        if table_end == -1:
            continue
        table_html = section[:table_end]
        
        table_width = 0
        for m in col_pattern.finditer(table_html):
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
    col_count = html.count('<col')
    if col_count >= 30:
        return ("A3", "landscape")
    
    for m in re.finditer(r'colspan=["\']?(\d+)["\']?', html, re.IGNORECASE):
        if int(m.group(1)) >= 25:
            return ("A3", "landscape")
    
    return ("A4", "portrait")


def inject_print_css(html: str, page_size: str, orientation: str, margin: str) -> str:
    css_block = PRINT_CSS_TEMPLATE.format(page_size=page_size, orientation=orientation, margin=margin)
    # If there's already a <head>, insert before </head>
    if '</head>' in html.lower():
        # naive case-insensitive replace: find index of last occurrence ignoring case
        pattern = re.compile(r'</head>', re.IGNORECASE)
        def repl(match):
            return f"<style>{css_block}</style>\n{match.group(0)}"
        html = pattern.sub(repl, html, count=1)
    else:
        html = f"<html><head><style>{css_block}</style></head>{html}</html>"
    return html


def render_pdf_weasy(html: str, out_pdf: pathlib.Path):
    if not WEASYPRINT_AVAILABLE:
        raise RuntimeError("WeasyPrint not installed. Install dependencies or use --engine=chrome")
    HTML(string=html, base_url=str(out_pdf.parent)).write_pdf(str(out_pdf))


def find_chrome(explicit_path: Optional[str]) -> Optional[str]:
    if explicit_path:
        return explicit_path if os.path.exists(explicit_path) else None
    candidates = [
        'google-chrome', 'chrome', 'chromium', 'chromium-browser',
        '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
        '/Applications/Chromium.app/Contents/MacOS/Chromium'
    ]
    for c in candidates:
        if shutil.which(c):  # type: ignore
            return shutil.which(c)  # type: ignore
        if os.path.exists(c):
            return c
    return None

import shutil

def render_pdf_chrome(html: str, out_pdf: pathlib.Path, chrome_path: Optional[str]):
    chrome = find_chrome(chrome_path)
    if not chrome:
        raise RuntimeError("Chrome/Chromium executable not found; specify --chrome-path")
    with tempfile.TemporaryDirectory() as tmp:
        html_path = pathlib.Path(tmp) / 'temp.html'
        html_path.write_text(html, encoding='utf-8')
        cmd = [chrome, '--headless', '--disable-gpu', f'--print-to-pdf={out_pdf}', str(html_path)]
        subprocess.run(cmd, check=True)


def process_file(xml_path: pathlib.Path, args) -> Optional[pathlib.Path]:
    if xml_path.suffix.lower() != '.xml':
        return None
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
    out_dir = pathlib.Path(args.out_dir) if args.out_dir else xml_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    out_pdf = out_dir / (xml_path.stem + '.pdf')
    if args.engine == 'weasyprint':
        render_pdf_weasy(html, out_pdf)
    else:
        render_pdf_chrome(html, out_pdf, args.chrome_path)
    print(f"[OK] {xml_path.name} -> {out_pdf.name} ({page_size} {orientation})")
    return out_pdf


def main():
    parser = argparse.ArgumentParser(description='Transform XML+XSLT (xml-stylesheet PI) into PDFs.')
    parser.add_argument('paths', nargs='*', default=['.'], help='XML files or directories (default: current dir)')
    parser.add_argument('--out-dir', help='Output directory for PDFs (default: alongside XML)')
    parser.add_argument('--engine', choices=['weasyprint', 'chrome'], default='weasyprint', help='PDF rendering engine')
    parser.add_argument('--chrome-path', help='Explicit path to Chrome/Chromium binary when using --engine=chrome')
    parser.add_argument('--force-page-size', help='Override detected page size (e.g., A4, A3)')
    parser.add_argument('--force-orientation', choices=['portrait', 'landscape'], help='Override detected orientation')
    parser.add_argument('--margin', default='10mm', help='Page margin (CSS size) default=10mm')
    args = parser.parse_args()

    any_processed = False
    for p in args.paths:
        p_path = pathlib.Path(p)
        if p_path.is_dir():
            for sub in sorted(p_path.iterdir()):
                if process_file(sub, args):
                    any_processed = True
        else:
            if process_file(p_path, args):
                any_processed = True
    if not any_processed:
        print("[INFO] No XML files with xml-stylesheet PI were processed.")

if __name__ == '__main__':
    main()

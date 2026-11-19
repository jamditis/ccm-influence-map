"""Render the viewer as a WordPress Custom HTML block for a password-protected page.

Usage: python3 wp_block.py <graph.json> <page-id> <out.html>

The graph is gzip-compressed and base64-encoded, and the app script is
base64-encoded, so WordPress content filters cannot alter either. Page CSS hides the theme title and container so the map
fills the width below the site header.
"""
import base64
import gzip
import json
import re
import sys
from pathlib import Path

PAGE_CSS = """
body.page-id-{pid} #main-content .page-header {{ display: none !important; }}
body.page-id-{pid} #main-content .main-content-background,
body.page-id-{pid} #main-content .grid-container,
body.page-id-{pid} #primary,
body.page-id-{pid} #post-{pid},
body.page-id-{pid} #post-{pid} > .grid-100,
body.page-id-{pid} #post-{pid} .entry-content {{
  width: 100% !important; max-width: none !important; margin: 0 !important;
  padding: 0 !important; float: none !important; min-height: 0 !important; }}
body.page-id-{pid} #ccmnet {{ display: block; width: 100%; margin: 0; }}
"""


# WordPress content filters rewrite "&&" and text between "<" and ">" inside inline scripts,
# so the app ships base64-encoded and this loader (free of those characters) runs it.
LOADER = ("(function(){var e=document.getElementById('ccmnet-app');var s=document.createElement('script');"
          "s.textContent=new TextDecoder().decode(Uint8Array.from(atob(e.textContent.trim()),function(c){return c.charCodeAt(0)}));"
          "document.body.appendChild(s)})();")


def render(graph_path, page_id):
    template = (Path(__file__).with_name("map.html")).read_text(encoding="utf-8")
    body = re.search(r"<!--ccmnet:start-->(.*)<!--ccmnet:end-->", template, re.S).group(1)
    graph = json.loads(Path(graph_path).read_text(encoding="utf-8"))
    raw = json.dumps(graph, ensure_ascii=True, separators=(",", ":")).encode()
    packed = base64.b64encode(gzip.compress(raw, 9, mtime=0)).decode()
    data = f'<script id="ccmnet-graph" type="application/octet-stream" data-encoding="gzip-base64">{packed}</script>'
    body, n = re.subn(r'<script id="ccmnet-graph"[^>]*>.*?</script>', lambda _: data, body, flags=re.S)
    if n != 1:
        raise SystemExit("graph script tag not found in map.html")
    app = re.search(r'<script id="ccmnet-app">(.*?)</script>', body, re.S)
    packed_app = base64.b64encode(app.group(1).encode()).decode()
    body = body[:app.start()] + f'<script id="ccmnet-app" type="text/plain" data-encoding="base64">{packed_app}</script>\n<script>{LOADER}</script>' + body[app.end():]
    css = PAGE_CSS.format(pid=int(page_id))
    return f"<!-- wp:html -->\n<style id=\"ccmnet-page\">{css}</style>\n{body.strip()}\n<!-- /wp:html -->\n"


if __name__ == "__main__":
    Path(sys.argv[3]).write_text(render(sys.argv[1], sys.argv[2]), encoding="utf-8")

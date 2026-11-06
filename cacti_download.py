#!/usr/bin/env python3
"""
Download graph data from a Cacti 1.2.x tree branch through the web UI.

Logs in, collects every graph on the branch and saves each graph's
graph_xport.php CSV into an output directory, plus an index.csv with the
graph id, title and period. The saved data is processed later by
cacti_analyze.py, so the download only has to run once per period.

Example:
  python cacti_download.py --url https://cacti.example.com/cacti --user myuser \
      --tree-url "https://cacti.example.com/cacti/graph_view.php?action=tree&node=tbranch-123" \
      --days 30 --out-dir data/2026-11
"""
import argparse
import csv
import getpass
import html as htmllib
import io
import os
import re
import sys
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

import requests

CSRF_RE = re.compile(r"""name=['"]__csrf_magic['"]\s+value=['"]([^'"]+)['"]""")
CSRF_JS_RE = re.compile(r"""csrfMagicToken\s*=\s*['"]([^'"]+)['"]""")
# Cacti 1.2 renders graphs as <div class='graphWrapper' id='wrapper_123' graph_id='123'>
# and loads the images via AJAX, so look for several patterns.
GRAPH_ID_RES = [
    re.compile(r"local_graph_id=(\d+)"),
    re.compile(r"""graph_id=['"](\d+)['"]"""),
    re.compile(r"wrapper_(\d+)"),
]
LI_ID_RE = re.compile(r"""<li[^>]*\bid=['"]((?:tbranch|tree_anchor)-[^'"]+)['"]""")
TREE_HREF_RE = re.compile(r"""href=['"]([^'"]*graph_view\.php\?[^'"]*action=tree[^'"]*)['"]""")


class Cacti:
    def __init__(self, base, verify=True, delay=0.2, debug_dir=None):
        self.debug_dir = debug_dir
        self.base = base.rstrip("/") + "/"
        self.s = requests.Session()
        self.s.verify = verify
        self.s.headers["User-Agent"] = "cactiexport/1.0"
        self.delay = delay

    @staticmethod
    def _csrf(html):
        m = CSRF_RE.search(html) or CSRF_JS_RE.search(html)
        return m.group(1) if m else None

    def login(self, user, password, realm=None):
        r = self.s.get(urljoin(self.base, "index.php"), timeout=30)
        r.raise_for_status()
        data = {"action": "login", "login_username": user, "login_password": password}
        tok = self._csrf(r.text)
        if tok:
            data["__csrf_magic"] = tok
        if realm:
            data["realm"] = realm
        r = self.s.post(urljoin(self.base, "index.php"), data=data, timeout=30)
        r.raise_for_status()
        if re.search(r"""name=['"]login_password['"]""", r.text):
            sys.exit("Login failed (login form returned again). Check user/password/realm.")

    def get(self, path_or_url, **params):
        url = path_or_url if path_or_url.startswith("http") else urljoin(self.base, path_or_url)
        r = self.s.get(url, params=params or None, timeout=120)
        r.raise_for_status()
        time.sleep(self.delay)
        return r.text

    def _graph_ids(self, page_html):
        found = []
        for rx in GRAPH_ID_RES:
            found.extend(int(x) for x in rx.findall(page_html))
        return list(dict.fromkeys(found))

    def _dump(self, name, text):
        if self.debug_dir:
            os.makedirs(self.debug_dir, exist_ok=True)
            with open(os.path.join(self.debug_dir, name), "w", encoding="utf-8") as f:
                f.write(text)

    def tree_nodes(self, tree_id, start_node):
        """Walk the tree with action=get_node (the jstree AJAX call) and return
        the query parameters of every node below start_node."""
        params = []
        seen = set()
        queue = ["#" if start_node.startswith("tree_anchor") else start_node]
        while queue:
            node = queue.pop(0)
            if node in seen:
                continue
            seen.add(node)
            page = self.get("graph_view.php", action="get_node", tree_id=tree_id, id=node)
            self._dump(f"get_node_{len(seen)}.html", page)
            for child in LI_ID_RE.findall(page):
                if child not in seen:
                    queue.append(child)
            for href in TREE_HREF_RE.findall(page):
                q = {k: v[0] for k, v in parse_qs(urlparse(htmllib.unescape(href)).query).items()}
                if q.get("node") and q not in params:
                    params.append(q)
            if len(seen) > 5000:
                break
        return params

    def graph_ids_from_tree(self, tree_url):
        """Collect graph ids from a tree URL copied from the browser.

        The URL may point at the tree root (node=tree_anchor-N) or a branch
        (node=tbranch-N). The tree is walked node by node and the right-hand
        pane of every node (action=tree_content) is read page by page.
        """
        tree_url = htmllib.unescape(tree_url)
        u = urlparse(tree_url)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        node = q.get("node", "")
        tree_id = q.get("tree_id")
        m = re.match(r"tree_anchor-(\d+)", node)
        if m:
            tree_id = m.group(1)
        if not tree_id and node.startswith("tbranch"):
            # find the tree id from the tree page itself
            page = self.get(tree_url)
            m = re.search(r"tree_anchor-(\d+)", page)
            tree_id = m.group(1) if m else None

        node_params = [q]
        if tree_id and node:
            node_params += self.tree_nodes(tree_id, node)
        print(f"{len(node_params)} tree nodes to read.")

        ids = []
        for n, base in enumerate(node_params, 1):
            for page_no in range(1, 51):
                params = dict(base, action="tree_content", graphs="1000", columns="1",
                              thumbnails="false", page=str(page_no))
                if tree_id:
                    params.setdefault("tree_id", tree_id)
                page = self.get(urljoin(self.base, "graph_view.php") + "?" + urlencode(params))
                self._dump(f"tree_content_{n}_{page_no}.html", page)
                new = [i for i in self._graph_ids(page) if i not in ids]
                if not new:
                    break
                ids.extend(new)
        return ids

    def xport(self, graph_id, start, end):
        return self.get("graph_xport.php", local_graph_id=graph_id, rra_id=0,
                        view_type="tree", graph_start=start, graph_end=end)


def xport_title(txt):
    for row in csv.reader(io.StringIO(txt.lstrip("\ufeff"))):
        if row and row[0].strip().lower().rstrip(":") == "title" and len(row) > 1:
            return row[1].strip()
        if row and row[0].strip().lower() == "date":
            break
    return ""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="Cacti base URL, e.g. https://host/cacti")
    ap.add_argument("--user", required=True)
    ap.add_argument("--password", help="omit to be prompted")
    ap.add_argument("--realm", help="login realm (e.g. for LDAP), if needed")
    ap.add_argument("--tree-url", help="tree branch URL copied from the browser")
    ap.add_argument("--graph-ids", help="text file with one local_graph_id per line (alternative to --tree-url)")
    ap.add_argument("--days", type=float, default=30, help="look-back period in days")
    ap.add_argument("--out-dir", default="data", help="directory for the downloaded CSV files")
    ap.add_argument("--skip-existing", action="store_true", help="do not download graphs already in --out-dir")
    ap.add_argument("--insecure", action="store_true", help="skip TLS verification (self-signed cert)")
    ap.add_argument("--debug-dir", help="save the fetched tree HTML pages here for troubleshooting")
    ap.add_argument("--ca-cert", help="PEM file of the server's self-signed certificate / CA to verify against")
    a = ap.parse_args()

    if not a.tree_url and not a.graph_ids:
        ap.error("give --tree-url or --graph-ids")
    if a.insecure:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    c = Cacti(a.url, verify=False if a.insecure else (a.ca_cert or True), debug_dir=a.debug_dir)
    c.login(a.user, a.password or getpass.getpass("Cacti password: "), a.realm)
    print("Logged in.")

    if a.graph_ids:
        with open(a.graph_ids) as f:
            ids = [int(x) for x in re.findall(r"\d+", f.read())]
    else:
        ids = c.graph_ids_from_tree(a.tree_url)
    print(f"{len(ids)} graphs found.")
    if not ids:
        sys.exit("No graphs found. Check the tree URL, run again with --debug-dir to save the pages, or use --graph-ids.")

    os.makedirs(a.out_dir, exist_ok=True)
    end = int(time.time())
    start = end - int(a.days * 86400)
    period = f"{datetime.fromtimestamp(start):%Y-%m-%d %H:%M} - {datetime.fromtimestamp(end):%Y-%m-%d %H:%M}"

    index_path = os.path.join(a.out_dir, "index.csv")
    index = {}
    if os.path.exists(index_path):
        with open(index_path, newline="", encoding="utf-8") as f:
            index = {int(r["graph_id"]): r for r in csv.DictReader(f)}

    errors = 0
    for n, gid in enumerate(ids, 1):
        path = os.path.join(a.out_dir, f"graph_{gid}.csv")
        if a.skip_existing and os.path.exists(path) and gid in index:
            print(f"  [{n}/{len(ids)}] {gid}: skipped (exists)")
            continue
        try:
            txt = c.xport(gid, start, end)
            if not any(r and r[0].strip().lower() == "date" for r in csv.reader(io.StringIO(txt.lstrip("\ufeff")))):
                raise ValueError("no 'Date' header in export (permission problem or wrong graph id?)")
        except Exception as e:  # keep going on single failures
            errors += 1
            print(f"  [{n}/{len(ids)}] {gid}: ERROR {e}")
            continue
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(txt)
        title = xport_title(txt)
        index[gid] = {"graph_id": gid, "title": title, "file": os.path.basename(path), "period": period}
        print(f"  [{n}/{len(ids)}] {gid}: {title}")

    with open(index_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["graph_id", "title", "file", "period"])
        w.writeheader()
        w.writerows(index[k] for k in sorted(index))

    print(f"\nSaved {len(index)} graphs to {a.out_dir} ({errors} errors). Period: {period}")


if __name__ == "__main__":
    main()

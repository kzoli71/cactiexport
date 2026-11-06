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
GRAPH_ID_RE = re.compile(r"local_graph_id=(\d+)")


class Cacti:
    def __init__(self, base, verify=True, delay=0.2):
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

    def graph_ids_from_tree(self, tree_url):
        """Collect graph ids from a tree branch URL copied from the browser.

        Cacti 1.x serves the right-hand pane via action=tree_content and
        paginates it; request a large page size and walk pages until no new
        ids appear.
        """
        u = urlparse(tree_url)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        candidates = [
            dict(q, action="tree_content", graphs="1000", columns="1", thumbnails="false"),
            dict(q, graphs="1000"),
        ]
        ids = []
        for params in candidates:
            for page in range(1, 51):
                params["page"] = str(page)
                html = self.get(urlunparse(u._replace(query=urlencode(params))))
                new = [i for i in dict.fromkeys(int(x) for x in GRAPH_ID_RE.findall(html)) if i not in ids]
                if not new:
                    break
                ids.extend(new)
            if ids:
                break
        return ids

    def xport(self, graph_id, start, end):
        return self.get("graph_xport.php", local_graph_id=graph_id, rra_id=0,
                        view_type="tree", graph_start=start, graph_end=end)


def xport_title(txt):
    for row in csv.reader(io.StringIO(txt)):
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
    a = ap.parse_args()

    if not a.tree_url and not a.graph_ids:
        ap.error("give --tree-url or --graph-ids")
    if a.insecure:
        requests.packages.urllib3.disable_warnings()

    c = Cacti(a.url, verify=not a.insecure)
    c.login(a.user, a.password or getpass.getpass("Cacti password: "), a.realm)
    print("Logged in.")

    if a.graph_ids:
        with open(a.graph_ids) as f:
            ids = [int(x) for x in re.findall(r"\d+", f.read())]
    else:
        ids = c.graph_ids_from_tree(a.tree_url)
    print(f"{len(ids)} graphs found.")
    if not ids:
        sys.exit("No graphs found. Check the tree URL, or use --graph-ids.")

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
            if not any(r and r[0].strip().lower() == "date" for r in csv.reader(io.StringIO(txt))):
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

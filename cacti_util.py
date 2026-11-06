#!/usr/bin/env python3
"""
Cacti interface utilization checker (web UI only, no shell access needed).

Steps:
  1. Log in to the Cacti web UI.
  2. Collect local_graph_id values from a tree branch URL (or from a file).
  3. Download each graph's data via graph_xport.php for the chosen period.
  4. Match graphs to the router/interface list in the input CSV.
  5. Compute peak / average / 95th percentile for in and out, compare to the
     interface speed and flag everything above the threshold (default 50%).

Requirements: Python 3.8+, `pip install requests`

Example:
  python cacti_util.py --url https://cacti.example.com/cacti \
      --user zoli --tree-url "https://cacti.example.com/cacti/graph_view.php?action=tree&node=tbranch-123" \
      --list links.csv --days 30 --metric avg --threshold 50 --out result.csv
"""
import argparse
import csv
import getpass
import io
import re
import sys
import time
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

import requests

CSRF_RE = re.compile(r"""name=['"]__csrf_magic['"]\s+value=['"]([^'"]+)['"]""")
CSRF_JS_RE = re.compile(r"""csrfMagicToken\s*=\s*['"]([^'"]+)['"]""")
GRAPH_ID_RE = re.compile(r"local_graph_id=(\d+)")
SPEED_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*([kKmMgGtT])(?:bit|bps|b/s|b)?\b")
UNIT = {"k": 1e3, "m": 1e6, "g": 1e9, "t": 1e12}


# --------------------------------------------------------------------------- web

class Cacti:
    def __init__(self, base, verify=True, delay=0.2):
        self.base = base.rstrip("/") + "/"
        self.s = requests.Session()
        self.s.verify = verify
        self.s.headers["User-Agent"] = "cacti-util-checker/1.0"
        self.delay = delay

    def _csrf(self, html):
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
        if 'name="login_password"' in r.text or "name='login_password'" in r.text:
            sys.exit("Login failed (login form returned again). Check user/password/realm.")

    def get(self, path_or_url, **params):
        url = path_or_url if path_or_url.startswith("http") else urljoin(self.base, path_or_url)
        r = self.s.get(url, params=params or None, timeout=120)
        r.raise_for_status()
        time.sleep(self.delay)
        return r.text

    def graph_ids_from_tree(self, tree_url):
        """Collect graph ids from a tree branch page copied from the browser.

        Cacti 1.x loads the right pane via action=tree_content, and paginates;
        we request a large page size and walk pages until no new ids appear.
        """
        u = urlparse(tree_url)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        candidates = []
        # 1.x: right-hand content is served by action=tree_content
        q1 = dict(q, action="tree_content", graphs="1000", columns="1", thumbnails="false")
        candidates.append(q1)
        # 0.8.x / fallback: the page as given
        candidates.append(dict(q, graphs="1000"))
        ids = []
        for params in candidates:
            page = 1
            while True:
                params["page"] = str(page)
                url = urlunparse(u._replace(query=urlencode(params)))
                html = self.get(url)
                new = [int(x) for x in GRAPH_ID_RE.findall(html) if int(x) not in ids]
                new = list(dict.fromkeys(new))
                if not new:
                    break
                ids.extend(new)
                page += 1
                if page > 50:
                    break
            if ids:
                break
        return ids

    def xport(self, graph_id, start, end):
        txt = self.get("graph_xport.php", local_graph_id=graph_id, rra_id=0,
                       view_type="tree", graph_start=start, graph_end=end)
        return parse_xport(txt)


# ------------------------------------------------------------------- parsing

def parse_xport(txt):
    """Return (title, columns, rows). rows = list of lists of floats/None."""
    rdr = list(csv.reader(io.StringIO(txt)))
    title = ""
    hdr_idx = None
    for i, row in enumerate(rdr):
        if not row:
            continue
        key = row[0].strip().strip('"').lower()
        if key in ("title", "title:") and len(row) > 1:
            title = row[1].strip()
        if key == "date":
            hdr_idx = i
            break
    if hdr_idx is None:
        raise ValueError("no 'Date' header row in export (permission problem or wrong graph id?)")
    cols = [c.strip() for c in rdr[hdr_idx][1:]]
    rows = []
    for row in rdr[hdr_idx + 1:]:
        if len(row) < 2:
            continue
        vals = []
        for c in row[1:]:
            c = c.strip()
            try:
                v = float(c)
                vals.append(None if v != v else v)  # NaN -> None
            except ValueError:
                vals.append(None)
        rows.append(vals)
    return title, cols, rows


def pick_in_out(cols):
    """Find inbound/outbound column indexes by name."""
    i_in = i_out = None
    for i, c in enumerate(cols):
        lc = c.lower()
        if i_in is None and ("inbound" in lc or lc.startswith("in") or "traffic_in" in lc):
            i_in = i
        elif i_out is None and ("outbound" in lc or lc.startswith("out") or "traffic_out" in lc):
            i_out = i
    if i_in is None and len(cols) >= 1:
        i_in = 0
    if i_out is None and len(cols) >= 2:
        i_out = 1
    return i_in, i_out


def stats(values):
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    k = max(0, int(round(0.95 * len(v))) - 1)
    return {"peak": v[-1], "avg": sum(v) / len(v), "p95": v[k]}


def parse_speed(text):
    """'1G', '100 Mbps', '10000000000' -> bits/s."""
    if text is None:
        return None
    t = str(text).strip()
    if not t:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", t):
        return float(t)
    m = SPEED_RE.search(t)
    if m:
        return float(m.group(1).replace(",", ".")) * UNIT[m.group(2).lower()]
    return None


def norm(s):
    s = s.lower()
    # common Cisco/Juniper abbreviations
    for long, short in (("tengigabitethernet", "te"), ("gigabitethernet", "gi"),
                        ("fastethernet", "fa"), ("ethernet", "eth"),
                        ("hundredgige", "hu"), ("fortygigabitethernet", "fo"),
                        ("port-channel", "po"), ("bundle-ether", "be")):
        s = s.replace(long, short)
    return re.sub(r"[^a-z0-9/.:]", "", s)


def load_list(path):
    """Input CSV: one link per line -> router1,interface1,router2,interface2[,speed]
    Header line is optional. Both ends of every link are checked."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        out = []
        for n, r in enumerate(csv.reader(f, dialect=dialect), 1):
            r = [x.strip() for x in r]
            if not any(r):
                continue
            if n == 1 and any("router" in x.lower() for x in r):
                continue  # header
            speed = parse_speed(r[4]) if len(r) > 4 else None
            link = f"{r[0]} {r[1]} <-> {r[2]} {r[3]}" if len(r) >= 4 else f"{r[0]} {r[1]}"
            for side, (ri, ii) in (("A", (0, 1)), ("B", (2, 3))):
                if len(r) > ii and r[ri] and r[ii]:
                    out.append({"line": n, "side": side, "link": link,
                                "router": r[ri], "iface": r[ii], "speed": speed})
        return out


def match(item, graphs):
    """Return the graph whose title contains both router and interface names."""
    nr, ni = norm(item["router"]), norm(item["iface"])
    best = None
    for g in graphs:
        nt = g["ntitle"]
        if nr in nt:
            # interface must match as a token, not as a prefix (gi0/1 vs gi0/10)
            if re.search(re.escape(ni) + r"(?![0-9/.:])", nt):
                if best is None or len(nt) < len(best["ntitle"]):
                    best = g
    return best


# ---------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="Cacti base URL, e.g. https://host/cacti")
    ap.add_argument("--user", required=True)
    ap.add_argument("--password", help="omit to be prompted")
    ap.add_argument("--realm", help="login realm (e.g. 2 for LDAP), if needed")
    ap.add_argument("--tree-url", help="tree branch URL copied from the browser")
    ap.add_argument("--graph-ids", help="text file with one local_graph_id per line (alternative to --tree-url)")
    ap.add_argument("--list", required=True, help="CSV: router1,interface1,router2,interface2[,speed]")
    ap.add_argument("--default-speed", help="speed to use when none is known, e.g. 1G")
    ap.add_argument("--days", type=float, default=30, help="look-back period in days")
    ap.add_argument("--metric", choices=["peak", "avg", "p95"], default="avg")
    ap.add_argument("--threshold", type=float, default=50.0, help="percent")
    ap.add_argument("--bytes", action="store_true",
                    help="graph values are bytes/s (multiply by 8); default assumes bits/s")
    ap.add_argument("--out", default="cacti_util_result.csv")
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

    end = int(time.time())
    start = end - int(a.days * 86400)
    mult = 8 if a.bytes else 1

    graphs = []
    for n, gid in enumerate(ids, 1):
        try:
            title, cols, rows = c.xport(gid, start, end)
        except Exception as e:  # keep going on single failures
            print(f"  [{n}/{len(ids)}] graph {gid}: ERROR {e}")
            continue
        i_in, i_out = pick_in_out(cols)
        s_in = stats([r[i_in] * mult if i_in is not None and i_in < len(r) and r[i_in] is not None else None for r in rows])
        s_out = stats([r[i_out] * mult if i_out is not None and i_out < len(r) and r[i_out] is not None else None for r in rows])
        graphs.append({"id": gid, "title": title, "ntitle": norm(title), "in": s_in, "out": s_out})
        print(f"  [{n}/{len(ids)}] {gid}: {title}")

    items = load_list(a.list)
    default_speed = parse_speed(a.default_speed)
    used = set()
    out_rows = []
    for it in items:
        g = match(it, graphs)
        row = {"line": it["line"], "side": it["side"], "link": it["link"], "router": it["router"], "interface": it["iface"], "graph_id": "", "graph_title": "",
               "speed_bps": "", "in_bps": "", "out_bps": "", "in_pct": "", "out_pct": "",
               "max_pct": "", "status": ""}
        if not g:
            row["status"] = "NO_GRAPH"
            out_rows.append(row)
            continue
        used.add(g["id"])
        speed = it["speed"] or parse_speed_from_title(g["title"]) or default_speed
        vin = g["in"][a.metric] if g["in"] else None
        vout = g["out"][a.metric] if g["out"] else None
        row.update(graph_id=g["id"], graph_title=g["title"],
                   in_bps=fmt(vin), out_bps=fmt(vout), speed_bps=fmt(speed))
        if vin is None and vout is None:
            row["status"] = "NO_DATA"
        elif not speed:
            row["status"] = "NO_SPEED"
        else:
            pin = 100 * (vin or 0) / speed
            pout = 100 * (vout or 0) / speed
            mx = max(pin, pout)
            row.update(in_pct=f"{pin:.1f}", out_pct=f"{pout:.1f}", max_pct=f"{mx:.1f}",
                       status="OVER" if mx > a.threshold else "OK")
        out_rows.append(row)

    for g in graphs:
        if g["id"] not in used:
            out_rows.append({"line": "", "side": "", "link": "", "router": "", "interface": "", "graph_id": g["id"], "graph_title": g["title"],
                             "speed_bps": "", "in_bps": "", "out_bps": "", "in_pct": "", "out_pct": "",
                             "max_pct": "", "status": "NOT_IN_LIST"})

    # a link is OVER if either end is over the threshold
    worst = {}
    for r in out_rows:
        if r["line"] != "" and r["max_pct"]:
            worst[r["line"]] = max(worst.get(r["line"], 0), float(r["max_pct"]))
    for r in out_rows:
        w = worst.get(r["line"]) if r["line"] != "" else None
        r["link_max_pct"] = f"{w:.1f}" if w is not None else ""
        r["link_status"] = ("OVER" if w > a.threshold else "OK") if w is not None else ""

    order = {"OVER": 0, "NO_SPEED": 1, "NO_DATA": 2, "NO_GRAPH": 3, "OK": 4, "NOT_IN_LIST": 5}
    out_rows.sort(key=lambda r: (order.get(r["status"], 9), -float(r["max_pct"] or 0)))
    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()), delimiter=";")
        w.writeheader()
        w.writerows(out_rows)

    cnt = {}
    for r in out_rows:
        cnt[r["status"]] = cnt.get(r["status"], 0) + 1
    print(f"\nMetric: {a.metric}, period: {a.days:g} days, threshold: {a.threshold:g}%")
    for k in sorted(cnt, key=lambda k: order.get(k, 9)):
        print(f"  {k:12s} {cnt[k]}")
    print(f"Result written to {a.out}")


def parse_speed_from_title(title):
    # e.g. "router1 - Traffic - Gi0/1 (1G)" or "... 1000 Mbps"
    m = re.search(r"[\(\[]\s*(\d+(?:[.,]\d+)?\s*[kKmMgGtT](?:bit|bps|b/s|b)?)\s*[\)\]]", title) \
        or re.search(r"(\d+(?:[.,]\d+)?\s*[kKmMgGtT](?:bit|bps|b/s))", title)
    return parse_speed(m.group(1)) if m else None


def fmt(v):
    return "" if v is None else f"{v:.0f}"


if __name__ == "__main__":
    main()

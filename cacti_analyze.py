#!/usr/bin/env python3
"""
Analyze graph data downloaded by cacti_download.py (no Cacti access needed).

Matches the saved graphs to the link list, computes average / peak / 95th
percentile for in and out, compares to the interface speed and flags every
interface above the threshold (default 50%).

Example:
  python cacti_analyze.py --data-dir data/2026-11 --list links.csv \
      --metric avg --threshold 50 --out result.csv
"""
import argparse
import csv
import io
import re
import sys
import os
from datetime import datetime, timedelta

SPEED_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*([kKmMgGtT])(?:bit|bps|b/s|b)?\b")
UNIT = {"k": 1e3, "m": 1e6, "g": 1e9, "t": 1e12}


def parse_xport(txt):
    """Return (title, columns, rows); each row is [timestamp, value1, value2, ...]."""
    rdr = list(csv.reader(io.StringIO(txt.lstrip("\ufeff"))))
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
        try:
            ts = datetime.strptime(row[0].strip()[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            ts = None
        vals = [ts]
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


# Default speed by interface name prefix in the link list (case-insensitive).
# Used when the CSV line has no explicit speed column.
SPEED_BY_NAME = [
    ("eth-trunk", 20e9),
    ("xe", 10e9),
]


def speed_from_name(iface):
    n = iface.strip().lower()
    for prefix, speed in SPEED_BY_NAME:
        if n.startswith(prefix):
            return speed
    return None


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
                                "router": r[ri], "iface": r[ii],
                                "speed": speed or speed_from_name(r[ii])})
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default="data", help="directory written by cacti_download.py")
    ap.add_argument("--list", required=True, help="CSV: router1,interface1,router2,interface2[,speed]")
    ap.add_argument("--default-speed", default="10G", help="speed used when none is known (default 10G)")
    ap.add_argument("--days", type=float, help="only use the last N days of the downloaded data")
    ap.add_argument("--metric", choices=["peak", "avg", "p95"], default="avg")
    ap.add_argument("--threshold", type=float, default=50.0, help="percent")
    ap.add_argument("--bytes", action="store_true",
                    help="graph values are bytes/s (multiply by 8); default assumes bits/s")
    ap.add_argument("--out", default="result.csv")
    a = ap.parse_args()

    files = sorted(f for f in os.listdir(a.data_dir) if re.fullmatch(r"graph_\d+\.csv", f))
    if not files:
        sys.exit(f"No graph_*.csv files in {a.data_dir}. Run cacti_download.py first.")
    mult = 8 if a.bytes else 1

    graphs = []
    for fn in files:
        gid = int(re.search(r"\d+", fn).group())
        with open(os.path.join(a.data_dir, fn), encoding="utf-8-sig") as f:
            try:
                title, cols, rows = parse_xport(f.read())
            except ValueError as e:
                print(f"  {fn}: ERROR {e}")
                continue
        if a.days:
            last = max((r[0] for r in rows if r[0]), default=None)
            if last:
                cut = last - timedelta(days=a.days)
                rows = [r for r in rows if r[0] and r[0] >= cut]
        i_in, i_out = pick_in_out(cols)
        graphs.append({"id": gid, "title": title, "ntitle": norm(title),
                       "in": stats(column(rows, i_in, mult)), "out": stats(column(rows, i_out, mult))})
    print(f"{len(graphs)} graphs loaded from {a.data_dir}.")

    items = load_list(a.list)
    default_speed = parse_speed(a.default_speed)
    used = set()
    by_graph = {}
    out_rows = []
    for it in items:
        g = match(it, graphs)
        row = {"line": it["line"], "side": it["side"], "link": it["link"], "router": it["router"], "interface": it["iface"], "graph_id": "", "graph_title": "",
               "speed_bps": "", "in_bps": "", "out_bps": "", "in_pct": "", "out_pct": "",
               "max_pct": "", "status": ""}
        if not g:
            row["status"] = "NO_GRAPH"
            # help troubleshooting: graphs that mention the router at all
            nr = norm(it["router"])
            cand = [x["title"] for x in graphs if nr in x["ntitle"]][:3]
            if cand:
                row["graph_title"] = "candidates: " + " | ".join(cand)
            out_rows.append(row)
            continue
        used.add(g["id"])
        # the graph title often names both ends of the link: report it once
        prev = by_graph.get((it["line"], g["id"]))
        if prev:
            prev["side"] += "+" + it["side"]
            prev["router"] += " / " + it["router"]
            prev["interface"] += " / " + it["iface"]
            continue
        by_graph[(it["line"], g["id"])] = row
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
    print(f"\nMetric: {a.metric}, threshold: {a.threshold:g}%, default speed: {a.default_speed}")
    for k in sorted(cnt, key=lambda k: order.get(k, 9)):
        print(f"  {k:12s} {cnt[k]}")
    print(f"Result written to {a.out}")


def parse_speed_from_title(title):
    # e.g. "router1 - Traffic - Gi0/1 (1G)" or "... 1000 Mbps"
    m = re.search(r"[\(\[]\s*(\d+(?:[.,]\d+)?\s*[kKmMgGtT](?:bit|bps|b/s|b)?)\s*[\)\]]", title) \
        or re.search(r"(\d+(?:[.,]\d+)?\s*[kKmMgGtT](?:bit|bps|b/s))", title)
    # Port names such as 25GE0/7/0 are deliberately not used: the optic may
    # run at a lower speed than the port type.
    return parse_speed(m.group(1)) if m else None


def column(rows, i, mult):
    if i is None:
        return []
    # abs(): traffic graphs often plot inbound below the axis (negative CDEF)
    return [abs(r[i + 1]) * mult for r in rows if i + 1 < len(r) and r[i + 1] is not None]


def fmt(v):
    return "" if v is None else f"{v:.0f}"


if __name__ == "__main__":
    main()

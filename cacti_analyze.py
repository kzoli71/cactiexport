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
    # separators become a single space so names keep their boundaries
    return re.sub(r"[^a-z0-9/.:]+", " ", s).strip()


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


def load_speeds(path):
    """Speed file: router,interface,speed (e.g. 10G). Header line optional.
    Returns {(norm(router), norm(interface)): bits/s}."""
    out = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        for n, r in enumerate(csv.reader(f, dialect=dialect), 1):
            r = [x.strip() for x in r]
            if len(r) < 3 or not r[0]:
                continue
            sp = parse_speed(r[2])
            if sp is None:
                if n > 1:
                    print(f"  {path}:{n}: cannot parse speed '{r[2]}', ignored")
                continue  # header or bad value
            out[(norm(r[0]), norm(r[1]))] = sp
    return out


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
                                "peer": r[2 if ri == 0 else 0] if len(r) > 2 else None,
                                "speed": speed, "name_speed": speed_from_name(r[ii])})
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
    ap.add_argument("--speeds", help="file with router,interface,speed lines (e.g. 10G) to look speeds up in")
    ap.add_argument("--default-speed", default="10G", help="speed used when none is known (default 10G)")
    ap.add_argument("--days", type=float, help="only use the last N days of the downloaded data")
    ap.add_argument("--metric", choices=["peak", "avg", "p95"], default="avg")
    ap.add_argument("--threshold", type=float, default=50.0, help="percent")
    ap.add_argument("--bytes", action="store_true",
                    help="graph values are bytes/s (multiply by 8); default assumes bits/s")
    ap.add_argument("--out", default="result.xlsx", help="result file; .xlsx (needs openpyxl) or .csv")
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
    speeds = load_speeds(a.speeds) if a.speeds else {}
    if a.speeds:
        print(f"{len(speeds)} speeds loaded from {a.speeds}.")
    # speed priority: CSV speed column, speed file, interface name rule
    for it in items:
        file_speed = speeds.get((norm(it["router"]), norm(it["iface"])))
        it["speed_rank"] = 3 if it["speed"] else 2 if file_speed else 1 if it["name_speed"] else 0
        it["speed"] = it["speed"] or file_speed or it["name_speed"]
    default_speed = parse_speed(a.default_speed)
    used = set()
    by_graph = {}
    out_rows = []
    exact = {id(it): match(it, graphs) for it in items}
    rank = {}

    def fill_metrics(row, g, speed):
        vin = g["in"][a.metric] if g["in"] else None
        vout = g["out"][a.metric] if g["out"] else None
        row.update(graph_id=g["id"], graph_title=g["title"], in_bps=vin, out_bps=vout,
                   speed_gbps=speed / 1e9 if speed else None,
                   in_pct=None, out_pct=None, max_pct=None)
        if vin is None and vout is None:
            row["status"] = "NO_DATA"
        elif not speed:
            row["status"] = "NO_SPEED"
        else:
            pin = round(100 * (vin or 0) / speed, 1)
            pout = round(100 * (vout or 0) / speed, 1)
            mx = max(pin, pout)
            row.update(in_pct=pin, out_pct=pout, max_pct=mx,
                       status="OVER" if mx > a.threshold else "OK")
    exact_ids = {g["id"] for g in exact.values() if g}
    for it in items:
        row = dict.fromkeys(COLUMNS, None)
        row.update(line=it["line"], side=it["side"], link=it["link"],
                   router=it["router"], interface=it["iface"])
        g = exact[id(it)]
        partner = next((x for x in items if x["line"] == it["line"] and x is not it), None)
        pg = exact.get(id(partner)) if partner else None
        if g:
            row["match"] = "exact"
        elif pg and norm(it["router"]) in pg["ntitle"]:
            # the other end matched exactly and its graph names this router too
            # (titles are often truncated, so this end's interface may be cut off)
            g = pg
            row["match"] = "other_end"
        else:
            # fallback: a graph whose title names both routers of the link,
            # preferring graphs no other link matched exactly
            pair = match_router_pair(it, graphs)
            free = [x for x in pair if x["id"] not in exact_ids]
            pair = free or pair
            if pair:
                g = pair[0]
                row["match"] = "router_pair" if len(pair) == 1 else f"router_pair ({len(pair)} graphs)"
                if len(pair) > 1:
                    row["candidates"] = " | ".join(x["title"] for x in pair[:5])
        if not g:
            row["status"] = "NO_GRAPH"
            # help troubleshooting: graphs that mention the router at all
            nr = norm(it["router"])
            cand = [x["title"] for x in graphs if nr in x["ntitle"]][:5]
            row["candidates"] = " | ".join(cand) or None
            out_rows.append(row)
            continue
        used.add(g["id"])
        # the graph title often names both ends of the link: report it once
        prev = by_graph.get((it["line"], g["id"]))
        if prev:
            prev["side"] += "+" + it["side"]
            prev["router"] += " / " + it["router"]
            prev["interface"] += " / " + it["iface"]
            if prev["match"] != row["match"]:
                prev["match"] += " / " + row["match"]
            # keep the speed from the more specific source of the two ends
            if it["speed_rank"] > rank[id(prev)]:
                rank[id(prev)] = it["speed_rank"]
                fill_metrics(prev, g, it["speed"] or default_speed)
            continue
        by_graph[(it["line"], g["id"])] = row
        rank[id(row)] = it["speed_rank"]
        fill_metrics(row, g, it["speed"] or parse_speed_from_title(g["title"]) or default_speed)
        out_rows.append(row)

    for g in graphs:
        if g["id"] not in used:
            row = dict.fromkeys(COLUMNS, None)
            row.update(graph_id=g["id"], graph_title=g["title"], status="NOT_IN_LIST")
            out_rows.append(row)

    # a link is OVER if either end is over the threshold
    worst = {}
    for r in out_rows:
        if r["line"] is not None and r["max_pct"] is not None:
            worst[r["line"]] = max(worst.get(r["line"], 0), r["max_pct"])
    for r in out_rows:
        w = worst.get(r["line"]) if r["line"] is not None else None
        r["link_max_pct"] = w
        r["link_status"] = ("OVER" if w > a.threshold else "OK") if w is not None else None

    order = {"OVER": 0, "NO_SPEED": 1, "NO_DATA": 2, "NO_GRAPH": 3, "OK": 4, "NOT_IN_LIST": 5}
    out_rows.sort(key=lambda r: (order.get(r["status"], 9), -(r["max_pct"] or 0)))

    if a.out.lower().endswith(".xlsx"):
        write_xlsx(a.out, out_rows, a)
    else:
        write_csv(a.out, out_rows)

    cnt = {}
    for r in out_rows:
        cnt[r["status"]] = cnt.get(r["status"], 0) + 1
    fallback = sum(1 for r in out_rows if r["match"] and r["match"].startswith("router_pair"))
    print(f"\nMetric: {a.metric}, threshold: {a.threshold:g}%, default speed: {a.default_speed}")
    for k in sorted(cnt, key=lambda k: order.get(k, 9)):
        print(f"  {k:12s} {cnt[k]}")
    if fallback:
        print(f"  ({fallback} rows matched by router pair only, check the 'match' column)")
    print(f"Result written to {a.out}")


COLUMNS = ["line", "side", "link", "router", "interface", "match", "graph_id", "graph_title",
           "speed_gbps", "in_bps", "out_bps", "in_pct", "out_pct", "max_pct", "status",
           "link_max_pct", "link_status", "candidates"]


def write_csv(path, rows):
    def cell(v):
        if v is None:
            return ""
        if isinstance(v, float) and abs(v) >= 1000:
            return f"{v:.0f}"  # bits/s
        return str(v)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(COLUMNS)
        for r in rows:
            w.writerow([cell(r[c]) for c in COLUMNS])


def write_xlsx(path, rows, a):
    try:
        from openpyxl import Workbook
        from openpyxl.formatting.rule import FormulaRule
        from openpyxl.styles import Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:
        sys.exit("openpyxl is required for .xlsx output: pip install openpyxl")

    wb = Workbook()
    ws = wb.active
    ws.title = "utilization"

    # settings sheet first, so the formulas can reference the threshold
    info = wb.create_sheet("settings")
    info.append(["threshold_pct", a.threshold])
    for k, v in (("metric", a.metric), ("default_speed", a.default_speed),
                 ("days", a.days or "all downloaded"), ("data_dir", a.data_dir), ("list", a.list), ("speeds", a.speeds or "")):
        info.append([k, v])
    info.append([])
    info.append(["Edit speed_gbps or threshold_pct and the percentages and statuses recalculate."])
    info.column_dimensions["A"].width = 16
    info.column_dimensions["B"].width = 40
    thr = "settings!$B$1"

    L = {name: get_column_letter(i) for i, name in enumerate(COLUMNS, 1)}
    ws.append(COLUMNS)
    for c in ws[1]:
        c.font = Font(bold=True)

    for r in rows:
        values = [r[c] for c in COLUMNS]
        ws.append(values)
        n = ws.max_row
        # rows with a graph and data get live formulas; the others stay as they are
        if r["status"] in ("OK", "OVER", "NO_SPEED"):
            sp, i_, o_ = f"{L['speed_gbps']}{n}", f"{L['in_bps']}{n}", f"{L['out_bps']}{n}"
            ip, op, mp = f"{L['in_pct']}{n}", f"{L['out_pct']}{n}", f"{L['max_pct']}{n}"

            def pct(v):
                return f'=IF(AND(ISNUMBER({sp}),{sp}>0,ISNUMBER({v})),ROUND({v}/({sp}*1000000000)*100,1),"")'
            ws[ip] = pct(i_)
            ws[op] = pct(o_)
            ws[mp] = f'=IF(COUNT({ip},{op})=0,"",MAX({ip},{op}))'
            ws[f"{L['status']}{n}"] = f'=IF(ISNUMBER({mp}),IF({mp}>{thr},"OVER","OK"),"NO_SPEED")'
        if r["line"] is not None:
            line_col, max_col = f"${L['line']}$2:${L['line']}${len(rows) + 1}", f"${L['max_pct']}$2:${L['max_pct']}${len(rows) + 1}"
            lm = f"{L['link_max_pct']}{n}"
            ln = f"{L['line']}{n}"
            ws[lm] = f'=IF(COUNTIFS({line_col},{ln},{max_col},">=0")=0,"",_xlfn.MAXIFS({max_col},{line_col},{ln}))'
            ws[f"{L['link_status']}{n}"] = f'=IF(ISNUMBER({lm}),IF({lm}>{thr},"OVER","OK"),"")'

    last = ws.max_row
    full = f"A2:{get_column_letter(len(COLUMNS))}{last}"
    st = f"${L['status']}2"
    ws.conditional_formatting.add(full, FormulaRule(formula=[f'{st}="OVER"'],
                                  fill=PatternFill("solid", fgColor="F8CBAD"), stopIfTrue=True))
    ws.conditional_formatting.add(full, FormulaRule(formula=[f'OR({st}="NO_GRAPH",{st}="NO_DATA",{st}="NO_SPEED")'],
                                  fill=PatternFill("solid", fgColor="FFE699")))
    fallback_fill = PatternFill("solid", fgColor="DDEBF7")
    for n, r in enumerate(rows, 2):
        if r["match"] and r["match"].startswith("router_pair"):
            ws[f"{L['match']}{n}"].fill = fallback_fill

    for name, fmt_ in (("speed_gbps", "0.##"), ("in_bps", "#,##0"), ("out_bps", "#,##0"),
                       ("in_pct", "0.0"), ("out_pct", "0.0"), ("max_pct", "0.0"), ("link_max_pct", "0.0")):
        for row in ws.iter_rows(min_row=2, min_col=COLUMNS.index(name) + 1, max_col=COLUMNS.index(name) + 1):
            row[0].number_format = fmt_

    widths = {"link": 40, "graph_title": 60, "candidates": 80, "router": 22, "interface": 22, "match": 22}
    for i, name in enumerate(COLUMNS, 1):
        ws.column_dimensions[get_column_letter(i)].width = widths.get(name, 14)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    wb.save(path)


def match_router_pair(item, graphs):
    """Graphs whose title contains both routers of the link."""
    if not item.get("peer"):
        return []
    nr, np_ = norm(item["router"]), norm(item["peer"])
    hits = [g for g in graphs if nr in g["ntitle"] and np_ in g["ntitle"]]
    return sorted(hits, key=lambda g: len(g["ntitle"]))


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


if __name__ == "__main__":
    main()

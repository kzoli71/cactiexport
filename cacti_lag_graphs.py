#!/usr/bin/env python3
"""
Create missing traffic graphs for LAG interfaces (Eth-Trunk) in Cacti 1.2.x
through the web UI.

For every device (or the ones selected with --host-filter / --host-ids):
  1. optionally reload the "SNMP - Interface Statistics" data query
     (--reindex), so new Eth-Trunk interfaces show up;
  2. open the device's "New Graphs" page and find the interfaces whose type
     is ieee8023adLag (161) and whose name matches --name-regex;
  3. report the ones that have no "In/Out Bits (64-bit Counters)" graph yet
     and, with --create, create those graphs.

Without --reindex / --create the script only reads and reports.
The Cacti user needs console access to Devices and Graph creation.

Example (report only, one device):
  python cacti_lag_graphs.py --url https://cacti.example.com/cacti --user admin \
      --insecure --host-filter 01010C21

Example (reindex and create on all devices):
  python cacti_lag_graphs.py --url https://cacti.example.com/cacti --user admin \
      --insecure --reindex --create --out lag_graphs.csv
"""
import argparse
import csv
import getpass
import html as htmllib
import os
import re
import sys
import time
from html.parser import HTMLParser
from urllib.parse import urljoin

import requests

CSRF_RE = re.compile(r"""name=['"]__csrf_magic['"]\s+value=['"]([^'"]+)['"]""")
CSRF_JS_RE = re.compile(r"""csrfMagicToken\s*=\s*['"]([^'"]+)['"]""")
HOST_LINK_RE = re.compile(r"""href=['"][^'"]*host\.php\?action=edit&(?:amp;)?id=(\d+)['"][^>]*>(.*?)</a>""", re.S)
DQ_TITLE_RE = re.compile(r"Data Query \[([^\]]+)\]")
DQ_RELOAD_RE = re.compile(r"""id=['"]reload(\d+)['"]""")
CREATED_RE = re.compile(r"created_graphs\[(\d+)\]\s*=\s*new Array\(([^)]*)\)")
TAG_RE = re.compile(r"<[^>]+>")


def text(fragment):
    return htmllib.unescape(TAG_RE.sub("", fragment)).strip()


class FormScraper(HTMLParser):
    """Collect the submittable fields of the first form on a page."""

    def __init__(self):
        super().__init__()
        self.fields = []
        self._select = None
        self._select_value = None
        self._option_value = None
        self._first_option = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "input":
            name = a.get("name")
            if not name or "disabled" in a:
                return
            t = (a.get("type") or "text").lower()
            if t in ("checkbox", "radio"):
                if "checked" in a:
                    self.fields.append((name, a.get("value", "on")))
            elif t not in ("button", "submit", "reset", "image"):
                self.fields.append((name, a.get("value", "")))
        elif tag == "select":
            self._select = a.get("name")
            self._select_value = None
            self._first_option = None
        elif tag == "option" and self._select:
            v = a.get("value", "")
            if self._first_option is None:
                self._first_option = v
            if "selected" in a:
                self._select_value = v
        elif tag == "textarea":
            self._select = None

    def handle_endtag(self, tag):
        if tag == "select" and self._select:
            v = self._select_value if self._select_value is not None else self._first_option
            self.fields.append((self._select, v or ""))
            self._select = None


class Cacti:
    def __init__(self, base, verify=True, delay=0.2, debug_dir=None):
        self.base = base.rstrip("/") + "/"
        self.s = requests.Session()
        self.s.verify = verify
        self.s.headers["User-Agent"] = "cactiexport/1.0"
        self.delay = delay
        self.debug_dir = debug_dir
        self.csrf = None

    def _remember_csrf(self, page):
        m = CSRF_RE.search(page) or CSRF_JS_RE.search(page)
        if m:
            self.csrf = m.group(1)

    def dump(self, name, page):
        if self.debug_dir:
            os.makedirs(self.debug_dir, exist_ok=True)
            with open(os.path.join(self.debug_dir, name), "w", encoding="utf-8") as f:
                f.write(page)

    def login(self, user, password, realm=None):
        r = self.s.get(urljoin(self.base, "index.php"), timeout=30)
        r.raise_for_status()
        self._remember_csrf(r.text)
        data = {"action": "login", "login_username": user, "login_password": password}
        if self.csrf:
            data["__csrf_magic"] = self.csrf
        if realm:
            data["realm"] = realm
        r = self.s.post(urljoin(self.base, "index.php"), data=data, timeout=30)
        r.raise_for_status()
        if re.search(r"""name=['"]login_password['"]""", r.text):
            sys.exit("Login failed (login form returned again). Check user/password/realm.")
        self._remember_csrf(r.text)

    def get(self, path, timeout=120, **params):
        r = self.s.get(urljoin(self.base, path), params=params or None, timeout=timeout)
        r.raise_for_status()
        self._remember_csrf(r.text)
        time.sleep(self.delay)
        return r.text

    def post(self, path, data, timeout=300):
        data = list(data)
        if self.csrf and not any(k == "__csrf_magic" for k, _ in data):
            data.append(("__csrf_magic", self.csrf))
        r = self.s.post(urljoin(self.base, path), data=data, timeout=timeout)
        r.raise_for_status()
        self._remember_csrf(r.text)
        time.sleep(self.delay)
        return r.text

    # ------------------------------------------------------------------ hosts

    def hosts(self):
        found = {}
        for page_no in range(1, 100):
            page = self.get("host.php", rows="5000", page=str(page_no), filter="",
                            host_template_id="-1", site_id="-1", host_status="-1")
            if page_no == 1:
                self.dump("host_list.html", page)
            new = 0
            for hid, label in HOST_LINK_RE.findall(page):
                hid = int(hid)
                if hid not in found:
                    found[hid] = text(label)
                    new += 1
            if not new:
                break
        return found

    # -------------------------------------------------------------- new graphs

    def new_graphs_page(self, host_id):
        return self.get("graphs_new.php", host_id=str(host_id), rows="5000", filter="", graph_type="-2", header="false")

    def reload_query(self, host_id, query_id):
        # reindexing walks the device, so allow plenty of time
        return self.get("graphs_new.php", timeout=600, action="query_reload",
                        id=str(query_id), host_id=str(host_id))


def parse_data_query(page, query_name):
    """Return (query_id, header, rows, graph_types, created) for the named
    data query on a graphs_new.php page, or None if the device lacks it."""
    titles = list(DQ_TITLE_RE.finditer(page))
    for n, t in enumerate(titles):
        if t.group(1).strip().lower() != query_name.lower():
            continue
        end = titles[n + 1].start() if n + 1 < len(titles) else len(page)
        section = page[t.start():end]
        m = DQ_RELOAD_RE.search(section)
        if not m:
            continue
        qid = int(m.group(1))
        header = [text(h) for h in re.findall(r"<th class='tableSubHeaderColumn'>(.*?)</th>", section, re.S)]
        rows = []
        for line in re.finditer(r"<tr id='dqline%d_([0-9a-f]{32})'[^>]*>(.*?)</tr>" % qid, section, re.S):
            cells = [text(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", line.group(2), re.S)]
            rows.append({"hash": line.group(1), "cells": dict(zip(header, cells))})
        graph_types = {}
        sel = re.search(r"<select[^>]*name='sgg_%d'[^>]*>(.*?)</select>" % qid, section, re.S)
        if sel:
            for v, label in re.findall(r"<option[^>]*value='(\d+)'[^>]*>(.*?)</option>", sel.group(1), re.S):
                graph_types[text(label)] = int(v)
        else:
            hid = re.search(r"name='sgg_%d' value='(\d+)'" % qid, section)
            if hid:
                graph_types["(only graph type)"] = int(hid.group(1))
        return qid, header, rows, graph_types
    return None


def created_hashes(page):
    out = {}
    for gt, arr in CREATED_RE.findall(page):
        out[int(gt)] = set(re.findall(r"'([0-9a-f]{32})'", arr))
    return out


def find_col(header, *names):
    for want in names:
        for h in header:
            if h.lower() == want.lower():
                return h
    for want in names:
        for h in header:
            if want.lower() in h.lower():
                return h
    return None


def is_lag(value):
    v = (value or "").strip().lower()
    return "ieee8023adlag" in v or v == "161" or v.endswith("(161)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="Cacti base URL, e.g. https://host/cacti")
    ap.add_argument("--user", required=True)
    ap.add_argument("--password", help="omit to be prompted")
    ap.add_argument("--realm", help="login realm (e.g. for LDAP), if needed")
    ap.add_argument("--host-filter", help="only devices whose description matches this regex")
    ap.add_argument("--host-ids", help="comma separated device ids")
    ap.add_argument("--data-query", default="SNMP - Interface Statistics")
    ap.add_argument("--graph-type", default="In/Out Bits (64-bit Counters)")
    ap.add_argument("--name-regex", default=r"(?i)^eth-trunk",
                    help="interface name (ifName or ifDescr) must match this regex")
    ap.add_argument("--reindex", action="store_true", help="reload the data query on each device first")
    ap.add_argument("--create", action="store_true", help="create the missing graphs (otherwise report only)")
    ap.add_argument("--out", default="lag_graphs.csv", help="report CSV")
    ap.add_argument("--debug-dir", help="save fetched pages here for troubleshooting")
    ap.add_argument("--insecure", action="store_true", help="skip TLS verification (self-signed cert)")
    ap.add_argument("--ca-cert", help="PEM file to verify the server certificate against")
    a = ap.parse_args()

    if a.insecure:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    c = Cacti(a.url, verify=False if a.insecure else (a.ca_cert or True), debug_dir=a.debug_dir)
    c.login(a.user, a.password or getpass.getpass("Cacti password: "), a.realm)
    print("Logged in.")

    hosts = c.hosts()
    if a.host_ids:
        wanted = {int(x) for x in re.findall(r"\d+", a.host_ids)}
        hosts = {k: v for k, v in hosts.items() if k in wanted} or {k: str(k) for k in wanted}
    if a.host_filter:
        rx = re.compile(a.host_filter, re.I)
        hosts = {k: v for k, v in hosts.items() if rx.search(v)}
    print(f"{len(hosts)} devices selected.")
    if not hosts:
        sys.exit("No devices. Check permissions (console access to Devices) or run with --debug-dir.")

    name_rx = re.compile(a.name_regex)
    report = []
    totals = {"missing": 0, "created": 0, "failed": 0}

    for n, (hid, hname) in enumerate(sorted(hosts.items(), key=lambda x: x[1]), 1):
        prefix = f"[{n}/{len(hosts)}] {hname} (id {hid})"
        try:
            page = c.new_graphs_page(hid)
            dq = parse_data_query(page, a.data_query)
            if not dq:
                print(f"{prefix}: no '{a.data_query}' data query, skipped")
                continue
            qid = dq[0]
            if a.reindex:
                print(f"{prefix}: reindexing ...", flush=True)
                c.reload_query(hid, qid)
                page = c.new_graphs_page(hid)
                dq = parse_data_query(page, a.data_query)
            c.dump(f"graphs_new_{hid}.html", page)
            qid, header, rows, graph_types = dq

            gt_id = graph_types.get(a.graph_type)
            if gt_id is None:
                print(f"{prefix}: graph type '{a.graph_type}' not offered (have: {', '.join(graph_types)})")
                continue
            have = created_hashes(page).get(gt_id, set())

            col_type = find_col(header, "Type", "ifType")
            col_names = [h for h in (find_col(header, "Name (IF-MIB)", "ifName"),
                                     find_col(header, "Description", "ifDescr")) if h]
            col_index = find_col(header, "Index", "ifIndex")
            if not col_type:
                print(f"{prefix}: no Type column on the New Graphs page (columns: {', '.join(header)})")
                continue

            missing = []
            for r in rows:
                cells = r["cells"]
                if not is_lag(cells.get(col_type)):
                    continue
                names = [cells.get(h, "") for h in col_names]
                if not any(name_rx.search(x) for x in names if x):
                    continue
                entry = {"host_id": hid, "host": hname, "if_index": cells.get(col_index, ""),
                         "interface": next((x for x in names if x), ""), "if_type": cells.get(col_type),
                         "hash": r["hash"], "status": "exists" if r["hash"] in have else "missing"}
                report.append(entry)
                if entry["status"] == "missing":
                    missing.append(entry)

            totals["missing"] += len(missing)
            lags = sum(1 for e in report if e["host_id"] == hid)
            print(f"{prefix}: {lags} LAG interfaces, {len(missing)} without graph"
                  + (": " + ", ".join(e["interface"] for e in missing) if missing else ""))

            if missing and a.create:
                data = [("action", "save"), ("save_component_graph", "1"),
                        ("host_id", str(hid)), ("host_template_id", host_template_id(page)),
                        (f"sgg_{qid}", str(gt_id))]
                data += [(f"sg_{qid}_{e['hash']}", "on") for e in missing]
                resp = c.post("graphs_new.php", data)
                if "save_component_new_graphs" in resp:
                    # the templates ask for values: accept the defaults Cacti offers
                    c.dump(f"graphs_new_confirm_{hid}.html", resp)
                    form = FormScraper()
                    form.feed(resp)
                    resp = c.post("graphs_new.php", form.fields)
                check = created_hashes(c.new_graphs_page(hid)).get(gt_id, set())
                for e in missing:
                    ok = e["hash"] in check
                    e["status"] = "created" if ok else "create_failed"
                    totals["created" if ok else "failed"] += 1
                print(f"{prefix}: created {sum(1 for e in missing if e['status'] == 'created')}/{len(missing)}")
        except requests.RequestException as e:
            print(f"{prefix}: ERROR {e}")

    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["host_id", "host", "if_index", "interface", "if_type", "status", "hash"],
                           delimiter=";")
        w.writeheader()
        w.writerows(report)

    print(f"\nLAG interfaces without '{a.graph_type}' graph: {totals['missing']}")
    if a.create:
        print(f"Created: {totals['created']}, failed: {totals['failed']}")
    else:
        print("Report only. Run again with --create to create them.")
    print(f"Report written to {a.out}")


def host_template_id(page):
    m = re.search(r"""name=['"]host_template_id['"][^>]*value=['"](\d+)['"]""", page) \
        or re.search(r"""id=['"]host_template_id['"][^>]*value=['"](\d+)['"]""", page)
    return m.group(1) if m else "0"


if __name__ == "__main__":
    main()

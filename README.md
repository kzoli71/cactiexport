# cactiexport

Checks interface utilization on a Cacti 1.2.x server using only the web UI
(no shell access to the Cacti host is required).

The work is split in two steps, so the data only has to be downloaded once:

1. `cacti_download.py` logs in, collects every graph on a tree branch and
   saves each graph's `graph_xport.php` CSV into a directory.
2. `cacti_analyze.py` works offline on that directory: it matches the graphs
   to a list of links and reports every interface whose average (or peak /
   95th percentile) traffic exceeds a threshold of its interface speed.

## Requirements

- Python 3.8+
- `pip install -r requirements.txt` (`requests` for the download, `openpyxl` for `.xlsx` output)
- A Cacti user that can view the graphs on the tree branch

## 1. Download

```
python cacti_download.py \
  --url https://cacti.example.com/cacti \
  --user myuser \
  --tree-url "https://cacti.example.com/cacti/graph_view.php?action=tree&node=tbranch-123" \
  --days 30 \
  --out-dir data/2026-11
```

Copy `--tree-url` from the browser after selecting the branch in the tree
view. Both the tree root (`node=tree_anchor-N`) and a branch
(`node=tbranch-N`) work: every node below it is walked and the graphs of
each are collected. Use `--debug-dir` to save the fetched pages when no
graphs are found. If the tree cannot be parsed, pass a text file of graph ids with
`--graph-ids` instead (one `local_graph_id` per line).

To skip the tree entirely, `--all` takes every graph the user can view from
the graph list view (`graph_view.php?action=list`); `--title-filter` narrows
it with a regex on the graph title, for example `--all --title-filter "_To_"`.

The directory gets one `graph_<id>.csv` per graph and an `index.csv` with the
graph id, title and downloaded period. `--skip-existing` resumes an
interrupted download.

| Option | Default | Description |
| --- | --- | --- |
| `--days` | 30 | Look-back period |
| `--out-dir` | `data` | Target directory |
| `--all` | off | Use all viewable graphs instead of a tree |
| `--title-filter` | none | With `--all`: regex on the graph title |
| `--skip-existing` | off | Skip graphs already downloaded |
| `--realm` | none | Login realm, e.g. for LDAP |
| `--insecure` | off | Skip TLS verification (self-signed certificate) |
| `--debug-dir` | none | Save fetched tree pages for troubleshooting |
| `--ca-cert` | none | Verify against this PEM certificate instead (e.g. the exported self-signed cert) |

The password is prompted for when `--password` is omitted.

## 2. Analyze

Input is a CSV file with one link per line; both ends are checked:

```
router1,interface1,router2,interface2[,speed]
```

The optional fifth column is the link speed (`100M`, `1G`, `10G` or bits/s).
A header line is optional. See `links.example.csv`.

Speed is resolved in this order:

1. the CSV speed column,
2. the speed file given with `--speeds` (lines of `router,interface,speed`,
   e.g. `01010C21,xe0/7/0,25G`; header optional, not every interface needs
   to be listed),
3. `--default-speed` (10G).

Speeds are never guessed from interface or port names (`25GE` may carry a
10G optic).

When both ends of a link share one graph, the speed from the more specific
source of the two ends is used.

Port type names such as `25GE` are not used, since the optic may run at a
lower speed.

```
python cacti_analyze.py --data-dir data/2026-11 --list links.csv --speeds speeds.txt --out result.xlsx
```

| Option | Default | Description |
| --- | --- | --- |
| `--metric` | `avg` | `avg`, `peak`, `p95` or `p85` (CSV output only; the workbook has a sheet for each) |
| `--threshold` | 50 | Percent of interface speed |
| `--default-speed` | 10G | Speed used when no other source is known |
| `--days` | all | Only use the last N days of the downloaded data |
| `--bytes` | off | Graph values are bytes/s instead of bits/s |
| `--keep-irregular` | off | Keep samples at irregular timestamps (DST change), dropped by default |

## Matching

Each end of a link is first matched exactly: the graph title must name the
router followed by the interface (at most one word in between, e.g.
`R1 - Traffic - Gi0/1` or `R1-xe0/4/0_To_R2-xe0/3/0`). An interface that
belongs to another router in the title does not count. If an end has no exact match but the
other end of the same link matched a graph whose title also names this
router, that graph is used (`other_end`; graph titles are often truncated, so
the far-end interface may be cut off). Otherwise a graph whose title contains
both router names of the link is used, preferring graphs no other link matched
exactly. The `match` column shows `exact`, `other_end` or `router_pair`; `router_pair (N graphs)` means
several graphs qualified, the shortest title was used and the others are
listed in `candidates`. For unmatched ends, `candidates` lists graphs that
mention the router.

## Output

An Excel workbook (`.xlsx`, default) with one sheet per metric (`avg`,
`peak`, `p95`, `p85`), or a semicolon-separated CSV for `--metric`, sorted by
severity. In the workbook OVER rows are red, rows needing attention are
yellow and router-pair matches are blue in the `match` column; a `settings`
sheet records the parameters used.

In the workbook `in_pct`, `out_pct`, `max_pct`, `status`, `link_max_pct` and
`link_status` are formulas: change `speed_gbps` (in Gbit/s) on a row, or
`threshold_pct` on the `settings` sheet, and they recalculate. Row colours
follow the recalculated status. `link_max_pct` uses `MAXIFS` (Excel 2019 /
Microsoft 365 or LibreOffice).

`graph_title` and `graph_url` link to the graph in Cacti. The base URL is taken
from `cacti_url.txt`, which `cacti_download.py` writes into the data
directory; for older downloads pass it with `--cacti-url`. Each row is one link end, or a whole link
when both ends match the same graph:

| Status | Meaning |
| --- | --- |
| `OVER` | Utilization above the threshold |
| `OK` | Below the threshold |
| `NO_SPEED` | Graph found, interface speed unknown |
| `NO_DATA` | Graph found, no data in the period |
| `NO_GRAPH` | No graph on the branch matched router + interface |
| `NOT_IN_LIST` | Graph on the branch that is not in the CSV |

`link_status` is `OVER` when either end of the link is over the threshold.
When both ends match the same graph (the title names the whole link), the
link is reported in one row with side `A+B`.

Graph titles are matched by router name and interface name; common long
interface names are normalised (`GigabitEthernet0/1` matches `Gi0/1`).

## Creating missing Eth-Trunk graphs

`cacti_lag_graphs.py` goes through the devices in the Cacti console and finds
LAG interfaces (`ifType` = `ieee8023adLag`, name matching `^Eth-Trunk`) that
have no `In/Out Bits (64-bit Counters)` graph yet. The Cacti user needs console
access to Devices and graph creation.

```
# report only
python cacti_lag_graphs.py --url https://cacti.example.com/cacti --user admin --insecure

# reload the "SNMP - Interface Statistics" data query, then create the graphs
python cacti_lag_graphs.py --url https://cacti.example.com/cacti --user admin --insecure \
  --reindex --create
```

Nothing is changed unless `--reindex` and/or `--create` is given. Start with
one device (`--host-filter <regex>` or `--host-ids 12,34`). After creating, the
script reloads the New Graphs page to confirm each graph exists and writes a
report (`--out`, default `lag_graphs.csv`) with status `exists`, `missing`,
`created` or `create_failed`.

| Option | Default | Description |
| --- | --- | --- |
| `--data-query` | `SNMP - Interface Statistics` | Data query to use |
| `--graph-type` | `In/Out Bits (64-bit Counters)` | Graph type to create |
| `--name-regex` | `(?i)^eth-trunk` | Interface name filter (ifName or ifDescr) |
| `--host-filter` | none | Regex on the device description |
| `--host-ids` | none | Comma separated device ids |
| `--debug-dir` | none | Save fetched pages for troubleshooting |

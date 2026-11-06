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
- `pip install -r requirements.txt` (only needed for the download step)
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

The directory gets one `graph_<id>.csv` per graph and an `index.csv` with the
graph id, title and downloaded period. `--skip-existing` resumes an
interrupted download.

| Option | Default | Description |
| --- | --- | --- |
| `--days` | 30 | Look-back period |
| `--out-dir` | `data` | Target directory |
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

Speed is resolved in this order: CSV column, a speed in the graph title
(for example `(1G)` or `1000 Mbps`), then `--default-speed` (10G).

```
python cacti_analyze.py --data-dir data/2026-11 --list links.csv --out result.csv
```

| Option | Default | Description |
| --- | --- | --- |
| `--metric` | `avg` | `avg`, `peak` or `p95` |
| `--threshold` | 50 | Percent of interface speed |
| `--default-speed` | 10G | Speed used when no other source is known |
| `--days` | all | Only use the last N days of the downloaded data |
| `--bytes` | off | Graph values are bytes/s instead of bits/s |

## Output

A semicolon-separated CSV sorted by severity. Each row is one link end:

| Status | Meaning |
| --- | --- |
| `OVER` | Utilization above the threshold |
| `OK` | Below the threshold |
| `NO_SPEED` | Graph found, interface speed unknown |
| `NO_DATA` | Graph found, no data in the period |
| `NO_GRAPH` | No graph on the branch matched router + interface |
| `NOT_IN_LIST` | Graph on the branch that is not in the CSV |

`link_status` is `OVER` when either end of the link is over the threshold.

Graph titles are matched by router name and interface name; common long
interface names are normalised (`GigabitEthernet0/1` matches `Gi0/1`).

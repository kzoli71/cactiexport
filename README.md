# cactiexport

Checks interface utilization on a Cacti 1.2.x server using only the web UI
(no shell access to the Cacti host is required).

The script logs in, collects every graph on a given tree branch, downloads
each graph's data through `graph_xport.php`, matches the graphs to a list of
links and reports every interface whose average (or peak / 95th percentile)
traffic exceeds a threshold of its interface speed.

## Requirements

- Python 3.8+
- `pip install -r requirements.txt`
- A Cacti user that can view the graphs on the tree branch

## Input

A CSV file with one link per line, both ends are checked:

```
router1,interface1,router2,interface2[,speed]
```

The optional fifth column is the link speed (`100M`, `1G`, `10G` or bits/s).
A header line is optional. See `links.example.csv`.

Speed is resolved in this order: CSV column, a speed in the graph title
(for example `(1G)` or `1000 Mbps`), then `--default-speed`.

## Usage

```
python cacti_util.py \
  --url https://cacti.example.com/cacti \
  --user myuser \
  --tree-url "https://cacti.example.com/cacti/graph_view.php?action=tree&node=tbranch-123" \
  --list links.csv \
  --days 30 --metric avg --threshold 50 \
  --out result.csv
```

Copy `--tree-url` from the browser after selecting the branch in the tree
view. If the tree cannot be parsed, pass a text file of graph ids with
`--graph-ids` instead (one `local_graph_id` per line).

| Option | Default | Description |
| --- | --- | --- |
| `--days` | 30 | Look-back period |
| `--metric` | `avg` | `avg`, `peak` or `p95` |
| `--threshold` | 50 | Percent of interface speed |
| `--default-speed` | none | Speed used when no other source is known |
| `--bytes` | off | Graph values are bytes/s instead of bits/s |
| `--realm` | none | Login realm, e.g. for LDAP |
| `--insecure` | off | Skip TLS verification (self-signed certificate) |

The password is prompted for when `--password` is omitted.

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

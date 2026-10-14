"""Build one dataset per mapping table (metrics, relationships, attributes, coverage, issues) from the SML repo.

Usage: python data_catalog/build_mapping_datasets.py <repo_root>

Writes, for each table:
  datasets/<table>.yml                  SML query dataset on the Postgres connection (inline VALUES)
  data_catalog/mapping/<table>.csv      the same rows as CSV
and data_catalog/mapping/load_mapping_tables.sql, which creates them as real tables in Postgres.
The issues table is hand-maintained in data_catalog/model_issues.csv.
"""
import csv, glob, os, sys, yaml

CATALOG_MODEL = "data_catalog_cube"
CONNECTION = os.environ.get("CATALOG_CONNECTION", "con_atscale_as_adventure")
SCHEMA = os.environ.get("CATALOG_SCHEMA", "as_adventure")

repo = sys.argv[1]


def load_all(folder):
    objs = {}
    for p in sorted(glob.glob(f"{repo}/{folder}/*.yml")):
        d = yaml.safe_load(open(p))
        if isinstance(d, dict) and "unique_name" in d:
            objs[d["unique_name"]] = d
    return objs


def warn(msg):
    print(f"::warning::{msg}")


metrics = load_all("metrics")
dims = load_all("dimensions")
datasets = load_all("datasets")
models = [m for n, m in sorted(load_all("models").items())
          if m.get("object_type") == "model" and n != CATALOG_MODEL]


def attributes(dim):
    """Yield (hierarchy, level_no, level, attribute dict, type) for every attribute of a dimension."""
    by_name = {a["unique_name"]: a for a in dim.get("level_attributes", [])}
    for h in dim.get("hierarchies", []):
        for i, lvl in enumerate(h.get("levels", []), 1):
            a = by_name.get(lvl["unique_name"])
            if a is None:
                warn(f"{dim['unique_name']}: level {lvl['unique_name']} has no level_attribute; skipped")
                continue
            yield h, i, a, a, "Level"
            for sa in lvl.get("secondary_attributes", []):
                yield h, i, a, sa, "Secondary"


metric_rows, rel_rows, attr_rows, cov_rows = [], [], [], []
used_dims = {}
for model in models:
    mname = model["unique_name"]
    join_levels = {}
    for rel in model.get("relationships", []):
        dim = dims.get(rel["to"]["dimension"])
        if dim is None:
            warn(f"{mname}: dimension {rel['to']['dimension']} not found; skipped")
            continue
        used_dims[dim["unique_name"]] = dim
        join_levels.setdefault(dim["unique_name"], set()).add(rel["to"]["level"])
        level = next((a for a in dim.get("level_attributes", []) if a["unique_name"] == rel["to"]["level"]), {})
        rel_rows.append({
            "model_name": mname,
            "relationship_name": rel["unique_name"].strip(),
            "fact_dataset": rel["from"]["dataset"],
            "fact_join_column": ",".join(rel["from"]["join_columns"]),
            "dimension_name": dim["unique_name"],
            "dimension_label": dim.get("label", dim["unique_name"]),
            "join_level": rel["to"]["level"],
            "dimension_dataset": level.get("dataset", ""),
            "dimension_key_column": ",".join(level.get("key_columns", [])),
        })

    for mref in model.get("metrics", []):
        m = metrics.get(mref["unique_name"])
        if m is None:
            warn(f"{mname}: metric {mref['unique_name']} not found in metrics/; skipped")
            continue
        metric_rows.append({
            "model_name": mname,
            "metric_name": m["unique_name"],
            "metric_label": m.get("label", m["unique_name"]),
            "aggregation": m["calculation_method"],
            "fact_dataset": m["dataset"],
            "fact_table": datasets.get(m["dataset"], {}).get("table", ""),
            "source_column": m["column"],
            "format": str(m.get("format", "")),
            "folder": mref.get("folder", ""),
        })
        for rel in model.get("relationships", []):
            if rel["from"]["dataset"] != m["dataset"] or rel["to"]["dimension"] not in dims:
                continue
            dim = dims[rel["to"]["dimension"]]
            cov_rows.append({
                "model_name": mname,
                "metric_name": m["unique_name"],
                "metric_label": m.get("label", m["unique_name"]),
                "dimension_name": dim["unique_name"],
                "dimension_label": dim.get("label", dim["unique_name"]),
                "attribute_count": sum(1 for _ in attributes(dim)),
            })

    for dname in sorted(join_levels):
        for h, i, lvl, a, kind in attributes(dims[dname]):
            if kind == "Level" and a["unique_name"] in join_levels[dname]:
                kind = "Join Level"
            attr_rows.append({
                "model_name": mname,
                "dimension_name": dname,
                "dimension_label": dims[dname].get("label", dname),
                "hierarchy_name": h["unique_name"],
                "level_number": i,
                "level_name": lvl["unique_name"],
                "attribute_name": a["unique_name"],
                "attribute_label": a.get("label", a["unique_name"]),
                "attribute_type": kind,
                "dataset": a["dataset"],
                "key_column": ",".join(a["key_columns"]),
                "name_column": a.get("name_column", ""),
            })

issues_path = f"{repo}/data_catalog/model_issues.csv"
issue_rows = list(csv.DictReader(open(issues_path))) if os.path.exists(issues_path) else []
for r in issue_rows:
    r["issue_number"] = int(r["issue_number"])

TABLES = [
    ("model_metrics", "Model Metrics", metric_rows),
    ("model_relationships", "Model Relationships", rel_rows),
    ("model_dimension_attributes", "Model Dimension Attributes", attr_rows),
    ("model_metric_coverage", "Model Metric Coverage", cov_rows),
    ("model_issues", "Model Issues", issue_rows),
]


class Literal(str):
    pass


yaml.SafeDumper.add_representer(Literal, lambda d, v: d.represent_scalar("tag:yaml.org,2002:str", v, style="|"))
sql_value = lambda v: str(v) if isinstance(v, int) else "'" + str(v).replace("'", "''") + "'"
out = f"{repo}/data_catalog/mapping"
os.makedirs(out, exist_ok=True)
load_sql = [f"-- Generated by data_catalog/build_mapping_datasets.py. Creates the mapping tables in {SCHEMA}.", ""]

for name, label, rows in TABLES:
    if not rows:
        warn(f"{name}: no rows; dataset not written")
        continue
    cols = list(rows[0])
    types = {c: "int" if isinstance(rows[0][c], int) else "string" for c in cols}
    with open(f"{out}/{name}.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    values = ",\n".join("  (" + ", ".join(sql_value(r[c]) for c in cols) + ")" for r in rows)
    dataset = {
        "unique_name": name,
        "object_type": "dataset",
        "label": label,
        "description": f"Mapping table generated by data_catalog/build_mapping_datasets.py ({len(rows)} rows).",
        "connection_id": CONNECTION,
        "sql": Literal(f"SELECT * FROM (VALUES\n{values}\n) AS t ({', '.join(cols)})"),
        "columns": [{"name": c, "data_type": types[c]} for c in cols],
    }
    yaml.safe_dump(dataset, open(f"{repo}/datasets/{name}.yml", "w"), sort_keys=False, width=10000)

    pg = {"int": "integer", "string": "text"}
    load_sql += [
        f"DROP TABLE IF EXISTS {SCHEMA}.{name};",
        f"CREATE TABLE {SCHEMA}.{name} (\n" + ",\n".join(f"  {c} {pg[types[c]]}" for c in cols) + "\n);",
        f"INSERT INTO {SCHEMA}.{name} ({', '.join(cols)}) VALUES\n{values};",
        "",
    ]
    print(f"{name}: {len(rows)} rows")

open(f"{out}/load_mapping_tables.sql", "w").write("\n".join(load_sql))

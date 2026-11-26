"""Build the offline CCM influence map from local, attributed inputs."""

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
import csv
from dataclasses import dataclass
from hashlib import sha256
from io import TextIOWrapper
import json
import os
from pathlib import Path, PurePosixPath
import re
import unicodedata
from urllib.parse import unquote, urlsplit
from zipfile import ZipFile, is_zipfile

import unresolved


TITLE = "CCM influence and interaction map"
NETWORKS = ("LinkedIn", "Bluesky", "Threads", "X", "Instagram", "CRM")
WEIGHTS = {"reaction": 1, "repost": 2, "comment": 3}
MUTUAL = {"connection", "mutual_follow"}
SOCIAL_TIES = {"follow", *MUTUAL}
RELATIONSHIPS = {*SOCIAL_TIES, *WEIGHTS}
EMAIL_ADDRESS = re.compile(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", re.I)
MEDIA_SECTORS = {"Local newsroom", "Public media", "Journalism support", "Media"}
SECTOR_TERMS = {
    "Public media": ("public media", "public radio", "public television", "public broadcasting", "pbs", "npr"),
    "Journalism support": ("journalism project", "journalism fund", "journalism lab", "journalism center",
                           "center for journalism", "for journalism", "news commons", "news product", "media institute",
                           "journalism resource", "journalism collaborative", "solutions journalism"),
    "Funder": ("foundation", "philanthropy", "grantmaking", "charitable trust", "endowment"),
    "University": ("university", "college", "school of journalism", "university press"),
    "Local newsroom": ("local news", "community news", "local newsroom", "inquirer", "patch", "daily news",
                       "news wire", "newswire", "tribune", "ledger", "star ledger", "nj spotlight news", "nj com"),
    "Media": ("journalism", "journalist", "newsroom", "reporter", "editor", "newspaper", "publish",
              "news", "media", "television", "broadcast", "radio", "magazine"),
    "Civic": ("city of", "county of", "public library", "township", "civic", "coalition", "municipal", "league of"),
}


def normalize(value):
    value = unicodedata.normalize("NFKD", value.casefold())
    return " ".join(re.findall(r"[^\W_]+", "".join(c for c in value if not unicodedata.combining(c))))


def remove_email(value):
    clean = EMAIL_ADDRESS.sub("", value)
    return clean.strip(" \t\r\n:–—-[]()<>") if clean != value else value


def linkedin_id(url):
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return ""
    if parsed.scheme != "https" or not re.fullmatch(r"(?:(?:www|[a-z]{2})\.)?linkedin\.com", parsed.netloc.lower()):
        return ""
    match = re.fullmatch(r"/in/([^/\s?#]+)/?", unquote(parsed.path))
    return f"linkedin:{match[1].casefold()}" if match else ""


@contextmanager
def connection_text(source):
    if is_zipfile(source):
        with ZipFile(source) as archive:
            matches = [entry for entry in archive.infolist() if not entry.is_dir()
                       and PurePosixPath(entry.filename).name.casefold() == "connections.csv"]
            if len(matches) != 1:
                raise ValueError("LinkedIn ZIP must contain exactly one Connections.csv")
            if matches[0].file_size > 16 * 1024 * 1024:
                raise ValueError("LinkedIn Connections.csv inside ZIP exceeds the import size limit")
            with archive.open(matches[0]) as entry, TextIOWrapper(entry, encoding="utf-8-sig", newline="") as handle:
                yield handle
    else:
        source.seek(0)
        with TextIOWrapper(source, encoding="utf-8-sig", newline="") as handle:
            yield handle


@dataclass(frozen=True)
class Observation:
    staff: str
    network: str
    name: str
    company: str
    account: str
    kind: str
    relation: str
    event: str


def connections(path, staff):
    """Read the LinkedIn header after any notes, with UTF-8 BOM and quoted cells."""
    with path.open("rb") as source, connection_text(source) as handle:
        reader = csv.reader(handle)
        for row in reader:
            columns = [normalize(value) for value in row]
            if {"first name", "last name"} <= set(columns):
                break
        else:
            raise ValueError("LinkedIn export is missing its connection header")
        rows = []
        for row in reader:
            fields = dict(zip(columns, row))
            name = " ".join(fields.get(part, "").strip() for part in ("first name", "last name")).strip()
            if normalize(name):
                rows.append(Observation(
                    staff, "LinkedIn", name, fields.get("company", "").strip(),
                    linkedin_id(fields.get("url", "")), "person", "connection", "",
                ))
        return rows


def key_for(*parts):
    return sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()[:24]


def crm_index(crm):
    index = defaultdict(list)
    for entry in crm["nodes"]:
        if entry["type"] not in ("contact", "organization"):
            continue
        kind = "person" if entry["type"] == "contact" else "organization"
        index[kind, normalize(entry["name"])].append(entry)
    return index


def crm_match(node, index, complete_orgs):
    if index is None:
        return "CRM unavailable", "Unavailable", None
    name = normalize(node["name"])
    if not name:
        return "Check CRM", "No comparable name", None
    names = index[node["kind"], name]
    companies = {normalize(value) for value in node["companies"]} - {""}
    if not names:
        if node["kind"] == "organization" and not complete_orgs:
            return "Check CRM", "No exact name in partial organization index", None
        if node["kind"] == "person" and not companies:
            return "Check CRM", "No exact name in snapshot · employer unverified", None
        return "Not in CRM", "No exact name in snapshot", None
    if node["kind"] == "organization" and len(names) == 1:
        return "In CRM", "High · exact organization name", names[0]
    def record_companies(entry):
        return {normalize(org) for org in (entry.get("organizations") or [entry.get("organization") or ""])} - {""}

    matches = [entry for entry in names if companies.intersection(record_companies(entry))]
    if len(matches) == 1:
        return "In CRM", "High · exact name and company", matches[0]
    if len(names) == 1 and (not companies or not record_companies(names[0])):
        return "Check CRM", "Low · name only", None
    return "Check CRM", "Ambiguous · duplicate name or company mismatch", None


def identities(rows):
    """Merge same-account rows and unambiguous cross-network name/company pairs."""
    accounts = defaultdict(lambda: defaultdict(set))
    for row in rows:
        pair = row.kind, normalize(row.name), normalize(row.company)
        if row.account and pair[1] and pair[2]:
            accounts[pair][row.network].add(row.account)

    aliases = defaultdict(set)
    for networks in accounts.values():
        if len(networks) > 1 and all(len(items) == 1 for items in networks.values()):
            group = set().union(*networks.values())
            for account in group:
                aliases[account].update(group)

    for row in rows:
        pair = row.kind, normalize(row.name), normalize(row.company)
        network_accounts = set().union(*accounts[pair].values())
        account = row.account
        if not account and len(network_accounts) == 1:
            account = next(iter(network_accounts))
        if account:
            group = aliases[account]
            reciprocal = all(aliases[other] == group for other in group)
            yield key_for(row.kind, min(group) if group and reciprocal else account)
        elif pair[2] and not network_accounts:
            yield key_for(*pair)
        else:
            yield key_for(*pair, row.staff, row.network, row.event)


def sector_for(name, companies, role, override):
    for value in (name, *companies):
        if normalize(value) in override:
            return override[normalize(value)]
    org_text = " " + normalize(" ".join([name, *companies])) + " "
    for sector, terms in SECTOR_TERMS.items():
        if any(f" {term} " in org_text for term in terms):
            return sector
    role_text = " " + normalize(role) + " "
    if any(f" {term} " in role_text for term in SECTOR_TERMS["Media"]):
        return "Media"
    return "Other"


def attach_crm(crm, nodes, match_nodes, staff, internal_names, edge):
    if crm is None:
        return {}, {}
    known = {person["crm_id"]: person["id"] for person in staff if person.get("crm_id")}
    membership = defaultdict(set)
    crm_ids = {}
    for entry in crm["nodes"]:
        rid = entry["id"]
        kind = {"contact": "person", "organization": "organization", "project": "project", "program": "program"}[entry["type"]]
        matches = match_nodes[rid]
        node_id = known.get(rid) or (matches[0] if len(matches) == 1 else key_for("crm", rid))
        if kind == "organization" and normalize(entry["name"]) in internal_names:
            crm_ids[rid] = None
            continue
        crm_ids[rid] = node_id
        companies = entry.get("organizations", [entry["organization"]] if entry.get("organization") else [])
        node = nodes.setdefault(node_id, {"id": node_id, "name": entry["name"], "kind": kind, "companies": companies})
        if node["kind"] in ("staff", "internal"):
            continue
        node["companies"] = sorted(set(node["companies"] + companies), key=str.casefold)
        node["crm"] = "In CRM"
        node.setdefault("confidence", "CRM record")
        node["sector_hint"] = entry.get("role") or entry.get("org_type") or ""
        if kind == "organization":
            node["hasContact"] = entry.get("has_contact")
        if kind == "project":
            node["projectDate"] = entry.get("updated", "")
        if kind == "program" and entry.get("catalog_source"):
            node["catalog"] = {"kind": entry["catalog_kind"], "source": entry["catalog_source"],
                               "status": entry["status"], "owners": entry["owners"], "funders": entry["funders"],
                               "fundingSources": entry.get("funding_sources", [])}

    project_links = defaultdict(set)
    program_links = defaultdict(set)
    for link in crm["links"]:
        source = crm_ids.get(link["source"], link["source"])
        target = crm_ids[link["target"]]
        if source is None or target is None:
            continue
        if source not in nodes:
            raise ValueError("CRM relationship refers to an unknown source ID")
        relation = link.get("relation", "CRM relationship")
        edge(source, target, "CRM", relation, link.get("count", 1))
        if relation == "CRM affiliation":
            membership[source].add(target)
        if nodes[target]["kind"] == "project":
            project_links[source].add(target)
        if nodes[target]["kind"] == "program":
            program_links[source].add(target)

    touch = defaultdict(set)
    crm_metrics = defaultdict(lambda: {"count": 0, "weight": 0.0, "by_type": Counter(),
                                      "first_contact": "", "last_contact": "", "recent90": 0})
    for rid, metrics in crm.get("interactions", {}).items():
        person = crm_ids[rid]
        if person is None or nodes[person]["kind"] == "staff":
            continue
        for staff_id, count in metrics["staff"].items():
            if staff_id not in nodes or nodes[staff_id]["kind"] != "staff":
                raise ValueError("CRM interaction refers to an unknown staff ID")
            edge(staff_id, person, "CRM", "Staff named in CRM interactions", count,
                 metrics.get("staff_last", {}).get(staff_id, ""), metrics.get("staff_weight", {}).get(staff_id))
        flagged = unresolved.reason(nodes[person])
        for node_id in {person} if flagged else {person, *membership[person]}:
            record = crm_metrics[node_id]
            record["count"] += metrics["count"]
            record["weight"] += metrics["weight"]
            record["recent90"] += metrics.get("recent90", 0)
            record["by_type"].update(metrics.get("by_type", {}))
            first = metrics.get("first_contact", "")
            if first:
                record["first_contact"] = min(record["first_contact"] or first, first)
            record["last_contact"] = max(record["last_contact"], metrics.get("last_contact", ""))
            touch[node_id].update(metrics["staff"])
    for contact, organizations in membership.items():
        if unresolved.reason(nodes[contact]):
            continue
        for org in organizations:
            program_links[org].update(program_links[contact])
            project_links[org].update(project_links[contact])
    for node_id, node in nodes.items():
        ordered_projects = sorted(project_links[node_id], key=lambda item: (nodes[item].get("projectDate", ""), item),
                                  reverse=True)
        node["projectNames"] = list(dict.fromkeys(nodes[item]["name"] for item in ordered_projects))[:8]
        node["projects"] = len(project_links[node_id])
        node["programs"] = len(program_links[node_id])
        node["programNames"] = sorted({nodes[item]["name"] for item in program_links[node_id]}, key=str.casefold)
    return crm_metrics, touch


def build_graph(staff, rows, crm, coverage, config=None):
    config = config or {}
    sector_overrides = {normalize(key): value for key, value in config.get("sectors", {}).items()}
    internal_names = {normalize(name) for name in config.get("internal_organizations", [])}
    index = crm_index(crm) if crm is not None else None
    complete_orgs = crm is not None and sum(
        record["type"] == "organization" for record in crm["nodes"]
    ) == crm.get("organization_total")
    central = defaultdict(set)
    reach = defaultdict(set)
    mutual = defaultdict(set)
    staff_links = defaultdict(set)
    activity = Counter()
    edges = Counter()
    edge_weights = Counter()
    edge_dates = {}
    seen = set()
    nodes = {}
    affiliation_evidence = set()
    match_nodes = defaultdict(list)
    staff_by_account = {}
    staff_by_name = {}
    staff_ids = set()
    for person in staff:
        node_id = person["id"]
        if node_id in staff_ids:
            raise ValueError("Duplicate staff ID")
        staff_ids.add(node_id)
        nodes[node_id] = {"id": node_id, "kind": "staff", "name": person["name"], "role": person.get("role", ""),
                          "companies": [], "crm": "", "confidence": ""}
        for label in [person["name"], *person.get("alternate_names", [])]:
            name = normalize(label)
            if name in staff_by_name and staff_by_name[name] != node_id:
                raise ValueError("Duplicate staff name or alias")
            staff_by_name[name] = node_id
        for account in person.get("accounts", []):
            if staff_by_account.get(account, node_id) != node_id:
                raise ValueError("Duplicate staff account")
            staff_by_account[account] = node_id

    def edge(source, target, network, relation, count=1, last="", weight=None):
        if source != target:
            key = source, target, network, relation
            edges[key] += count
            edge_weights[key] += weight if weight is not None else count * WEIGHTS.get(relation, 1)
            edge_dates[key] = max(edge_dates.get(key, ""), last)
            central[source].add(target)
            central[target].add(source)

    ids = list(identities(rows))
    org_ids_by_name = defaultdict(set)
    for node_id, row in zip(ids, rows):
        if row.kind == "organization" and row.account:
            org_ids_by_name[normalize(row.name)].add(node_id)

    def credit(member, staff_id, relation):
        staff_links[member].add(staff_id)
        if relation in SOCIAL_TIES:
            reach[member].add(staff_id)
        if relation in MUTUAL:
            mutual[member].add(staff_id)
        activity[member] += WEIGHTS.get(relation, 0)

    def organization(name, node_id=None):
        named = org_ids_by_name[normalize(name)]
        org = node_id or (next(iter(named)) if len(named) == 1 else key_for("organization", normalize(name)))
        nodes.setdefault(org, {"id": org, "kind": "organization", "name": name, "companies": set()})
        return org

    org_ties = []
    for node_id, row in zip(ids, rows):
        if row.staff not in staff_ids or row.network not in NETWORKS[:-1] or row.relation not in RELATIONSHIPS:
            raise ValueError("Observation has an unknown staff ID, network or relationship")
        if row.kind not in ("person", "organization") or not row.name.strip() or (
            not normalize(row.name) and (not row.account or row.kind == "organization")
        ):
            raise ValueError("Observation is missing a valid outside identity")
        if row.relation in WEIGHTS and not row.event:
            raise ValueError("Interactions require a stable event ID")
        target = staff_by_account.get(row.account, node_id)
        if row.kind == "person" and not row.account and normalize(row.company) in internal_names:
            target = staff_by_name.get(normalize(row.name), target)
        if row.kind == "organization":
            target = organization(row.name, node_id if row.account else None)
        if target == row.staff:
            continue
        node = nodes.setdefault(target, {"id": target, "kind": row.kind, "name": row.name, "companies": set()})
        org = None
        if row.company and node["kind"] == "person" and normalize(row.company):
            node["companies"].add(row.company)
            if normalize(row.company) not in internal_names:
                org = organization(row.company)
                affiliation = target, org, row.network, "affiliation"
                if affiliation not in edges:
                    edge(*affiliation)
                org_ties.append((target, org, row.staff, row.network, None))

        occurrence = row.staff, target, row.network, row.relation, row.event
        if occurrence in seen:
            continue
        seen.add(occurrence)
        edge(*occurrence[:4])
        credit(target, row.staff, row.relation)
        if org:
            org_ties.append((target, org, row.staff, row.network, row.relation))

    for node_id, node in nodes.items():
        node["companies"] = sorted(node["companies"], key=str.casefold)
        if node_id in staff_ids:
            continue
        node["crm"], node["confidence"], match = crm_match(node, index, complete_orgs)
        if match is not None:
            match_nodes[match["id"]].append(node_id)

    for matched in match_nodes.values():
        if len(matched) != 1:
            for node_id in matched:
                nodes[node_id].update(crm="Check CRM", confidence="Ambiguous · multiple observed identities")

    crm_metrics, crm_staff = attach_crm(crm, nodes, match_nodes, staff, internal_names, edge)
    for person, org, staff_id, network, relation in org_ties:
        if unresolved.reason(nodes[person]):
            continue
        affiliation_evidence.add((staff_id, org, network))
        if relation:
            credit(org, staff_id, relation)

    denominator = max(1, len(nodes) - 1)
    for node_id, node in nodes.items():
        metrics = crm_metrics.get(node_id, {})
        count, weight = metrics.get("count", 0), metrics.get("weight", 0)
        touched = crm_staff.get(node_id, set())
        sector = sector_for(node["name"], node["companies"], node.pop("sector_hint", ""), sector_overrides)
        projects = node.get("projects", 0)
        node.update(reach=len(reach[node_id]), weight=activity[node_id],
                    mutual=len(mutual[node_id]), crmCount=count, crmWeight=round(weight, 3), sector=sector,
                    crmStaff=sorted(touched), projects=projects, programs=node.get("programs", 0),
                    projectNames=node.get("projectNames", []), programNames=node.get("programNames", []),
                    lastContact=metrics.get("last_contact", ""), firstContact=metrics.get("first_contact", ""),
                    recent90=metrics.get("recent90", 0), byType=dict(metrics.get("by_type", {})),
                    centrality=round(len(central[node_id]) / denominator, 8),
                    staff=sorted(staff_links[node_id] | touched),
                    working=bool(count or projects or len(mutual[node_id]) >= 2 and sector in MEDIA_SECTORS))
        if touched and node["kind"] == "organization":
            affiliation_evidence.update((staff_id, node_id, "CRM") for staff_id in touched)
    ordered = sorted(nodes.values(), key=rank_key)
    return {"nodes": ordered, "edges": [
        {"source": source, "target": target, "network": network, "relation": relation, "count": count,
         "weight": round(edge_weights[source, target, network, relation], 4),
         "lastDate": edge_dates[source, target, network, relation]}
        for (source, target, network, relation), count in sorted(edges.items())
    ], "coverage": coverage, "mediaSectors": sorted(MEDIA_SECTORS),
        "affiliations": [list(tie) for tie in sorted(affiliation_evidence)]}


def rank_key(node):
    return (-node["crmWeight"], -node["crmCount"], -len(node["crmStaff"]), -node["projects"], -node["programs"],
            -node["mutual"], -node["weight"], -node["reach"], -node["centrality"], node["name"].casefold(), node["id"])


def spreadsheet_text(value):
    stripped = value.lstrip()
    if stripped.startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value


def write_catalog(graph, output):
    catalog = {node["id"]: node for node in graph["nodes"] if node.get("catalog")}
    if not catalog:
        return
    kinds = {node["id"]: node["kind"] for node in graph["nodes"]}
    relationships = defaultdict(set)
    for edge in graph["edges"]:
        if edge["target"] in catalog:
            evidence, _, _ = edge["relation"].partition(" · ")
            relationships[edge["target"], evidence, kinds[edge["source"]]].add(edge["source"])
    with (output / f"{TITLE} program catalog.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Name", "Catalog type", "Wiki source", "Status", "Recorded owners", "Funders", "Funding sources",
                         "Recorded people", "Inferred people", "Recorded organizations", "Inferred organizations",
                         "Recorded CCM staff", "Inferred CCM staff"])
        for pid, node in sorted(catalog.items(), key=lambda entry: entry[1]["name"].casefold()):
            meta = node["catalog"]
            counts = [len(relationships[pid, evidence, kind]) for kind in ("person", "organization", "staff")
                      for evidence in ("Recorded", "Inferred")]
            writer.writerow([spreadsheet_text(value) for value in
                             (node["name"], meta["kind"], meta["source"], meta["status"],
                              ", ".join(meta["owners"]), ", ".join(meta["funders"]),
                              ", ".join(meta["fundingSources"]))] + counts)


def write_outputs(graph, output, data_only=False):
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    unresolved.mark(graph)
    if data_only:
        (output / f"{TITLE} graph.json").write_text(json.dumps(graph, ensure_ascii=False, separators=(",", ":")),
                                                     encoding="utf-8")
    else:
        template = Path(__file__).with_name("map.html").read_text()
        payload = json.dumps(graph, ensure_ascii=True, separators=(",", ":")).replace("<", "\\u003c")
        (output / f"{TITLE}.html").write_text(template.replace("/*__GRAPH__*/", payload), encoding="utf-8")
    write_catalog(graph, output)
    external = [node for node in graph["nodes"] if node["kind"] in ("person", "organization")
                and not node.get("unresolved")]
    working = [node for node in external if node["working"] and node["sector"] in MEDIA_SECTORS]
    staff = {node["id"]: node["name"] for node in graph["nodes"] if node["kind"] == "staff"}
    with (output / f"{TITLE}.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Rank", "Name", "Type", "Company", "Sector", "CRM status", "Match confidence",
                         "CRM interactions", "CRM recency weight", "Staff named in CRM", "Board links", "Programs",
                         "Mutual staff ties", "Social reach", "Social interaction weight", "Degree centrality", "CCM staff"])
        for rank, node in enumerate(working, 1):
            writer.writerow([rank, spreadsheet_text(node["name"]), node["kind"],
                             spreadsheet_text("; ".join(node["companies"])), node["sector"], node["crm"], node["confidence"],
                             node["crmCount"], node["crmWeight"], len(node["crmStaff"]), node["projects"], node["programs"],
                             node["mutual"], node["reach"], node["weight"], node["centrality"],
                             spreadsheet_text("; ".join(staff[value] for value in node["staff"]))])

    summary = [f"# {TITLE}", "", "## Collection coverage", ""]
    summary.extend(f"- {line}" for line in graph["coverage"])
    summary.extend(["", "## Top 25 working relationships in journalism and media", ""])
    for rank, node in enumerate(working[:25], 1):
        evidence = [f"{node['crmCount']} CRM interactions (recency score {node['crmWeight']})"] if node["crmCount"] else []
        if node["crmStaff"]:
            evidence.append("Staff named in CRM: " + ", ".join(staff[value] for value in node["crmStaff"]))
        if node["projects"]:
            evidence.append(f"{node['projects']} linked Board items, including " + ", ".join(node["projectNames"][:2]))
        if node["programs"]:
            evidence.append(f"{node['programs']} linked programs")
        if node["mutual"]:
            evidence.append(f"{node['mutual']} mutual staff ties")
        summary.append(f"{rank}. {node['name']} ({node['sector']}): " + ". ".join(evidence) + ".")
    if not working:
        summary.append("No working relationships met the journalism and media criteria.")
    counts = Counter(node["crm"] for node in working)
    summary.extend(["", "## CRM coverage", ""])
    summary.extend(f"- {status}: {count}" for status, count in sorted(counts.items()))
    gaps = [node["name"] for node in working if node["crm"] == "Not in CRM"][:25]
    checks = [node["name"] for node in working if node["crm"] == "Check CRM"][:25]
    if gaps:
        summary.append(f"- Top-ranked people with no exact CRM name: {', '.join(gaps)}.")
    if checks:
        summary.append(f"- Working relationships to verify by employer or organization: {', '.join(checks)}.")
    summary.extend(["", "## Staff links to organizations", ""])
    for staff_id, name in sorted(staff.items(), key=lambda item: item[1]):
        orgs = [f"{node['name']} ({node['sector']})" for node in external if node["kind"] == "organization"
                and node["working"] and staff_id in node["staff"]]
        summary.append(f"- {name}: {', '.join(orgs[:5]) or 'No organization ties observed in this snapshot'}.")
    (output / f"{TITLE} summary.md").write_text("\n".join(summary) + "\n", encoding="utf-8")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data", type=Path)
    parser.add_argument("--data-only", action="store_true", help="Write graph JSON, rankings, catalog and summary")
    options = parser.parse_args()
    data = options.data
    staff = json.loads((data / "staff.json").read_text())
    rows = []
    exports = set()
    for person in staff:
        staff_id = person["id"]
        if not re.fullmatch(r"[a-z0-9-]+", staff_id):
            raise ValueError("Staff IDs must contain only lowercase ASCII letters, numbers and hyphens")
        path = data / "connections" / f"{staff_id}.csv"
        if not path.is_file():
            path = path.with_suffix(".zip")
        if person.get("export", True) and path.is_file():
            rows.extend(connections(path, staff_id))
            exports.add(staff_id)
        person.setdefault("accounts", []).extend({
            identifier for url in person.get("public_pages", {}).get("LinkedIn", [])
            if (identifier := linkedin_id(url))
        })
    coverage = []
    for source in sorted((data / "observations").glob("*.json")):
        batch = json.loads(source.read_text())
        rows.extend(Observation(**row) for row in batch["observations"])
        coverage.extend(batch["coverage"])
        exports.update(batch.get("connection_exports", []))
        for person in staff:
            account = batch.get("staff_accounts", {}).get(person["id"])
            if account:
                person["accounts"].append(account)
    coverage.insert(0, f"LinkedIn exports received: {len(exports)} of {sum(p.get('export', True) for p in staff)} expected.")
    identities_path = data / "identities.json"
    if identities_path.is_file():
        confirmed = json.loads(identities_path.read_text())
        rows = [Observation(**(vars(row) | confirmed.get(row.account, {}))) for row in rows]
    config_path = data / "config.json"
    config = json.loads(config_path.read_text()) if config_path.is_file() else {}
    exclude = set(config.get("exclude_accounts", []))
    excluded_names = {normalize(name) for name in config.get("excluded_entities", [])}
    internal_names = {normalize(name) for name in config.get("internal_organizations", [])}
    aliases = {normalize(key): value for key, value in config.get("organization_aliases", {}).items()}
    cleaned = []
    for row in rows:
        name = remove_email(row.name)
        if not name or normalize(name) in excluded_names or row.account in exclude:
            continue
        if row.kind == "organization":
            if normalize(name) in internal_names:
                continue
            name = aliases.get(normalize(name), name)
        company = remove_email(row.company)
        cleaned.append(Observation(**(vars(row) | {"name": name, "company": aliases.get(normalize(company), company)})))
    rows = cleaned
    for person in staff:
        person["crm_id"] = config.get("staff_crm_ids", {}).get(person["id"])
    crm_path = data / "crm.json"
    crm = json.loads(crm_path.read_text()) if crm_path.is_file() else None
    if crm is None:
        coverage.append("CRM snapshot unavailable.")
    else:
        counts = Counter(node["type"] for node in crm["nodes"])
        coverage.append(f"CRM snapshot: {counts['contact']} contacts and {counts['organization']} of "
                        f"{crm['organization_total']} organizations, read {crm['collected']}.")
        if crm.get("counts"):
            totals = crm["counts"]
            coverage.append(f"CRM activity: {totals['interactions']} interactions, "
                            f"{totals['named_interactions']} naming a staff member, "
                            f"{totals['board_items']} distinct Board items and {totals['programs']} programs.")
            if totals.get("organizations_without_contacts"):
                coverage.append(f"{totals['organizations_without_contacts']} CRM organizations have no contact on file.")
            if totals.get("interactions_without_contact"):
                coverage.append(f"{totals['interactions_without_contact']} CRM interactions lack a contact record "
                                "and are excluded from rankings.")
            if totals.get("unresolved_board_items"):
                coverage.append(f"{totals['unresolved_board_items']} linked Board IDs have no available item title.")
        if crm.get("catalog_counts"):
            catalog_counts = crm["catalog_counts"]
            coverage.append(f"CCM wiki catalog: {catalog_counts.get('Program', 0)} program concepts and "
                            f"{catalog_counts.get('Event', 0)} related event concepts.")
            coverage.append("Program links are marked Recorded or Inferred and identify their source.")
        if crm.get("cleaning"):
            coverage.append("Test records and internal entities were removed. Verified duplicate records were combined.")
            coverage.append("CRM staff ties show explicit staff names mentioned in the source. There is no owner field.")
        if counts["organization"] < crm["organization_total"]:
            coverage.append("The dashboard API identifies organizations with linked contacts.")
    graph = build_graph(staff, rows, crm, coverage, config)
    write_outputs(graph, data / "output", options.data_only)
    print(f"Built {len(graph['nodes'])} nodes and {len(graph['edges'])} edges.")


if __name__ == "__main__":
    main()

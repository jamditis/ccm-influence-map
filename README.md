# CCM influence and interaction map

Build an offline HTML relationship map, ranked CSV, and plain-language summary from public observations, voluntary LinkedIn connections exports, and a read-only CRM snapshot. The build uses Python 3.10 or later and the standard library.

```sh
python3 netmap.py data
```

Open the HTML in `data/output/` in a browser. It defaults to working relationships in journalism, media and local news. Search or filter by sector, staff member, network, CRM status and program. The graph displays the first 16 filtered results. Select a result for its activity and links to people, organizations, projects and programs.

## Private inputs

Keep all inputs, settings, and generated files in the ignored `data/` directory or another private directory outside the checkout. The Git repository contains the builder and synthetic tests.

| Local path | Format |
| --- | --- |
| `staff.json` | Array with a unique lowercase hyphenated `id` and `name` per staff member. Use `export: false` for public-data-only staff. Optional `bluesky` is a verified handle. Optional `public_pages` maps platform names to verified HTTPS profile or post URLs. |
| `connections/<staff-id>.csv` or `.zip` | Staff member's own LinkedIn Connections export. For ZIP input, the importer reads the one `Connections.csv` member and ignores other archive members. Use the stable staff ID as the filename. The importer reads name, company and public profile URL and ignores email, position and connection date. |
| `observations/*.json` | Object with a `coverage` array and an `observations` array. Each observation has `staff`, `network`, `name`, `company`, `account` (stable platform account ID or empty), `kind` (`person` or `organization`), `relation` (`connection`, `follow`, `mutual_follow`, `reaction`, `repost` or `comment`) and `event` (stable interaction ID, empty for connections and follows). Optional `staff_accounts` maps staff IDs to collected account IDs. Optional `connection_exports` lists staff IDs represented by normalized export observations. |
| `crm.json` | Object with an ISO `collected` timestamp, `organization_total`, `nodes` with `id`, `name`, `type` (`contact`, `organization`, `project` or `program`) and optional `organization`, `role` or `org_type`. Each `links` entry has `source`, `target` and `relation`. Use `CRM affiliation` for contact-to-organization links. `interactions` maps CRM contact IDs to a `count`, recency `weight`, and `staff` object of staff IDs and mention counts. Use only these fields and optional aggregate collection counts. |
| `identities.json` | Optional object keyed by stable account ID. Set `kind`, `name` or `company` from corroborated public evidence. |
| `config.json` | Optional `exclude_accounts` array for internal social accounts, `internal_organizations` array, `staff_crm_ids` map of confirmed staff IDs to CRM contact IDs, and `sectors` map from known names to sectors. |

The collector accepts `candidates` to inspect Bluesky account candidates and `bluesky` to collect a verified roster's outgoing follows and inbound reactions, reposts and replies on up to 12 authored posts from the last 90 days. Run `bluesky-mutual` after `bluesky` to confirm reciprocated follows. Collection caps and account failures appear in the coverage notes. The `firecrawl` action accepts an API credential on stdin and requests the explicitly listed public pages at three-second intervals. It writes responses to `data/scrapes/` for review. Record publicly attributable actors in an observations file with one record per event. Skip pages behind a login.

Canonical program records can carry `catalog_kind`, `catalog_source`, `status`, `owners`, `funders` and optional `funding_sources`. Program links can start from a CRM ID or a confirmed staff ID. Start the relationship label with `Recorded · ` or `Inferred · ` and identify the evidence after it. When catalog records are present the builder writes a separate catalog CSV with source and relationship counts.

## Matching and scores

Stable account IDs unite repeated observations. A name and employer can unite compatible records when the evidence is unique. An exact normalized name and company indicates a high-confidence CRM person match. An exact unique organization name indicates a high-confidence organization match. Ambiguous records, unverified employers and absent organization names in partial snapshots go to **Check CRM**. A named person with a known employer and no exact CRM name is listed as **Not in CRM** based on the snapshot.

The working network requires CRM activity, a Board link, or at least two mutual staff ties in a media sector. The CSV and top 25 use the default working-network and sector filters. The media lens covers local newsrooms, public media, journalism support, and media. Funder, university, civic, and other ties appear under All sectors. Sector labels use configured overrides and terms from the organization name or CRM role.

The rank gives priority to CRM interactions weighted by recency. Give an interaction four points on the day it occurs, halve the weight every 180 days, and use 0.25 points if the date is unavailable. Ties use interaction count, the number of distinct staff named in CRM, Board links, programs, and social observations. Use the presence of a staff name as evidence of a mention. Social interaction weights are 1 for a reaction, 2 for a repost and 3 for a reply. Degree centrality is the share of nodes linked to a record. Organization scores include its affiliated people. Scores use the complete edition and stay stable under filters.

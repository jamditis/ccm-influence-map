import json
import csv
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

from netmap import Observation, TITLE, build_graph, connections, write_outputs


class NetworkMapTests(unittest.TestCase):
    def test_export_preamble_and_quoted_fields(self):
        with TemporaryDirectory() as folder:
            file = Path(folder) / "Connections.csv"
            file.write_text(
                '\ufeffNotes:\nA voluntary export\n\nFirst Name,Last Name,URL,Email Address,Company,Position,Connected On\n'
                'Ari,Example,https://www.linkedin.com/in/ari-example/?tracking=1,,"Example, Inc.",Editor,02 Jan 2026\n',
                encoding="utf-8",
            )
            self.assertEqual(connections(file, "s1"), [Observation(
                "s1", "LinkedIn", "Ari Example", "Example, Inc.", "linkedin:ari-example", "person", "connection", "",
            )])

    def test_archive_reads_only_one_connections_member(self):
        with TemporaryDirectory() as folder:
            archive = Path(folder) / "export.zip"
            with ZipFile(archive, "w") as data:
                data.writestr("other.csv", "First Name,Last Name\nExcluded,Example")
                data.writestr("nested/Connections.csv", "First Name,Last Name,Company\nIncluded,Example,Demo News")
            self.assertEqual([row.name for row in connections(archive, "s1")], ["Included Example"])
            with ZipFile(archive, "a") as data:
                data.writestr("Connections.csv", "First Name,Last Name\nDuplicate,Example")
            with self.assertRaisesRegex(ValueError, "exactly one Connections.csv"):
                connections(archive, "s1")

    def test_duplicate_events_affiliations_and_conservative_crm_match(self):
        staff = [{"id": "s1", "name": "Staff One"}, {"id": "s2", "name": "Staff Two"}]
        connection = Observation("s1", "LinkedIn", "Ari Example", "Example Press", "linkedin:ari",
                                 "person", "connection", "")
        comment = Observation("s2", "Bluesky", "Ari Example", "Example Press", "fake-ari",
                              "person", "comment", "reply-1")
        crm = {"nodes": [
            {"id": "1", "type": "contact", "name": "Ari Example", "organization": "Example Press"},
            {"id": "2", "type": "contact", "name": "Ari Example", "organization": "Another Press"},
            {"id": "3", "type": "organization", "name": "Example Press"},
        ], "links": [{"source": "1", "target": "3"}]}
        graph = build_graph(staff, [connection, connection, comment, comment], crm, [])
        person = next(node for node in graph["nodes"] if node["kind"] == "person" and node["weight"])
        organization = next(node for node in graph["nodes"] if node["kind"] == "organization")
        self.assertEqual((person["reach"], person["weight"], person["crm"]), (1, 3, "In CRM"))
        self.assertEqual((organization["reach"], organization["weight"], organization["crm"]), (1, 3, "In CRM"))
        self.assertEqual(len(graph["affiliations"]), 2)
        self.assertEqual(sum(edge["network"] == "CRM" for edge in graph["edges"]), 1)
        self.assertTrue(all(edge["count"] == 1 for edge in graph["edges"]))
        graph = build_graph(staff, [Observation("s1", "Bluesky", "Ari Example", "", "fake-ari",
                                               "person", "follow", "")], crm, [])
        self.assertTrue(any(node["kind"] == "person" and node["crm"] == "Check CRM" for node in graph["nodes"]))

    def test_literals_in_html_and_csv(self):
        name = '=2+2</script><script>alert("test")</script>'
        graph = build_graph([{"id": "s1", "name": "Staff One"}], [
            Observation("s1", "Bluesky", name, "Example Media", "fake-ari", "person", "follow", ""),
        ], {"nodes": [{"id": "1", "type": "contact", "name": name, "organization": "Example Media"}],
            "links": [], "interactions": {"1": {"count": 1, "weight": 4, "staff": {"s1": 1}}}}, [])
        with TemporaryDirectory() as folder:
            output = Path(folder)
            write_outputs(graph, output)
            page = (output / f"{TITLE}.html").read_text()
            self.assertNotIn(name, page)
            payload = page.split('<script id="graph" type="application/json">', 1)[1].split("</script>", 1)[0]
            self.assertEqual(json.loads(payload), graph)
            self.assertIn("'=2+2", (output / f"{TITLE}.csv").read_text(encoding="utf-8-sig"))

    def test_crm_working_network_and_organization_rollup(self):
        staff = [{"id": "s1", "name": "Staff One"}, {"id": "s2", "name": "Staff Two"}]
        crm = {"nodes": [
            {"id": "1", "type": "contact", "name": "Ari Example", "organization": "Example Public Radio"},
            {"id": "2", "type": "organization", "name": "Example Public Radio"},
            {"id": "3", "type": "project", "name": "Example Board Project"},
            {"id": "4", "type": "program", "name": "Example Program"},
        ], "links": [
            {"source": "1", "target": "2", "relation": "CRM affiliation"},
            {"source": "1", "target": "3", "relation": "CRM Board link"},
            {"source": "1", "target": "4", "relation": "CRM program membership"},
        ], "interactions": {"1": {"count": 3, "weight": 7.5, "staff": {"s1": 2, "s2": 1}}}}
        one_way = [
            Observation(staff_id, "Bluesky", "Example Brand", "", "fake-brand", "organization", "follow", "")
            for staff_id in ("s1", "s2")
        ]
        graph = build_graph(staff, one_way, crm, [])
        by_name = {node["name"]: node for node in graph["nodes"]}
        organization = by_name["Example Public Radio"]
        self.assertEqual((organization["crmCount"], organization["crmWeight"]), (3, 7.5))
        self.assertEqual(organization["projectNames"], ["Example Board Project"])
        self.assertEqual(organization["programs"], 1)
        self.assertEqual(organization["crmStaff"], ["s1", "s2"])
        self.assertTrue(organization["working"])
        self.assertFalse(by_name["Example Brand"]["working"])

    def test_catalog_preserves_recorded_and_inferred_provenance(self):
        staff = [{"id": "s1", "name": "Staff One"}]
        crm = {"nodes": [
            {"id": "p1", "type": "contact", "name": "Example Participant"},
            {"id": "p2", "type": "organization", "name": "Example Media"},
            {"id": "program", "type": "program", "name": "Example Program", "catalog_kind": "Program",
             "catalog_source": "programs/example.md", "status": "Ongoing", "owners": ["Staff One"], "funders": []},
        ], "links": [
            {"source": "p1", "target": "program", "relation": "Recorded · CRM cohort membership"},
            {"source": "p2", "target": "program", "relation": "Inferred · CRM Board tag"},
            {"source": "s1", "target": "program", "relation": "Recorded · Wiki program responsibility"},
        ]}
        with TemporaryDirectory() as folder:
            output = Path(folder)
            write_outputs(build_graph(staff, [], crm, []), output)
            with (output / f"{TITLE} program catalog.csv").open(encoding="utf-8-sig", newline="") as handle:
                row = next(csv.DictReader(handle))
            for column in ("Recorded people", "Inferred organizations", "Recorded CCM staff"):
                self.assertEqual(row[column], "1")
            self.assertEqual(row["Inferred people"], "0")


if __name__ == "__main__":
    unittest.main()

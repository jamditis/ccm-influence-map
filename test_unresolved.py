import unittest

from unresolved import mark, reason


class UnresolvedTests(unittest.TestCase):
    def test_flags_agents_tests_and_first_name_only_crm_contacts(self):
        graph = {"nodes": [
            {"kind": "person", "name": "Glys", "crm": "In CRM"},
            {"kind": "organization", "name": "Test Org Alpha 01", "crm": "In CRM"},
            {"kind": "person", "name": "Ari", "crm": "In CRM", "companies": ["Example Press"]},
            {"kind": "person", "name": "Ari Example", "crm": "In CRM"},
            {"kind": "person", "name": "Claude Example", "crm": "In CRM"},
            {"kind": "person", "name": "examplehandle", "crm": "Check CRM"},
            {"kind": "organization", "name": "Testbed News", "crm": "In CRM"},
            {"kind": "staff", "name": "Pattern"},
        ]}
        self.assertEqual(mark(graph), 3)
        self.assertEqual([n.get("unresolved", "") for n in graph["nodes"]], [
            "Agent or bot account", "Test record", "First name only", "", "", "", "", "",
        ])

    def test_mark_clears_a_stale_flag(self):
        node = {"kind": "person", "name": "Ari Example", "crm": "In CRM", "unresolved": "First name only"}
        mark({"nodes": [node]})
        self.assertNotIn("unresolved", node)
        self.assertEqual(reason(node), "")


if __name__ == "__main__":
    unittest.main()

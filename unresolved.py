"""Flag graph records that cannot be read as a real relationship until the CRM record is fixed.

The viewer hides flagged records by default and lists them under "Needs cleanup in CRM".
"""
import re

AGENT_NAMES = {"claude", "codex", "glys", "pattern", "stoneward", "syl", "wyndle"}
TEST_NAME = re.compile(r"^test\b", re.I)


def reason(node):
    if node.get("kind") not in ("person", "organization"):
        return ""
    name = (node.get("name") or "").strip()
    if name.lower() in AGENT_NAMES:
        return "Agent or bot account"
    if TEST_NAME.match(name):
        return "Test record"
    if node["kind"] == "person" and node.get("crm") == "In CRM" and len(name.split()) == 1:
        return "First name only"
    return ""


def mark(graph):
    """Set `unresolved` on flagged nodes and return the number flagged."""
    count = 0
    for node in graph["nodes"]:
        why = reason(node)
        if why:
            node["unresolved"] = why
            count += 1
        else:
            node.pop("unresolved", None)
    return count

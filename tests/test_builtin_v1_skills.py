from __future__ import annotations

import sqlite3
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from omniagent.models import TaskNode, TaskNodeType
from omniagent.sandbox import build_sandbox
from omniagent.skill_registry import SkillRegistry
from omniagent.skills.loader import SkillLoader
from omniagent.storage import SQLiteStore


class BuiltinV1SkillTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.loader = SkillLoader(self.workspace)
        self.definitions = self.loader.load_all()

    def run_skill(
        self,
        skill_name: str,
        payload: dict,
        *,
        level: str = "workspace",
        allow_external_commands: bool | None = None,
    ):
        sandbox = build_sandbox(
            self.workspace,
            level=level,
            allow_external_commands=allow_external_commands,
        )
        self.addCleanup(sandbox.close)
        runner = self.loader.load_runner(self.definitions[skill_name])
        return runner(payload, {"sandbox": sandbox})

    def test_v1_skills_load_expected_contract_actions(self) -> None:
        expected = {
            "builtin.data.sqlite": {"list_tables", "schema", "query", "export_csv", "report"},
            "builtin.text.markdown": {
                "headings",
                "extract_section",
                "replace_section",
                "append_section",
                "table",
            },
            "builtin.vcs.git": {"status", "diff", "log", "show"},
            "builtin.web.html": {"extract", "links", "report"},
            "builtin.archive.basic": {"list", "extract", "create"},
            "builtin.data.config": {"query", "report"},
        }
        for skill_name, actions in expected.items():
            with self.subTest(skill=skill_name):
                self.assertIn(skill_name, self.definitions)
                got = {
                    contract.action
                    for contract in self.definitions[skill_name].metadata.contracts
                }
                self.assertEqual(got, actions)

    def test_registry_selects_sqlite_skill_by_tag_and_action(self) -> None:
        store = SQLiteStore(Path(self.tempdir.name) / "runtime.sqlite")
        registry = SkillRegistry(store)
        registry.sync(self.definitions)
        node = TaskNode(
            node_id="n1",
            type=TaskNodeType.ACTION,
            goal="query database",
            required_skills=["sqlite"],
            input_payload={
                "action": "query",
                "path": "app.sqlite3",
                "sql": "select count(*) from users",
            },
        )

        selection = registry.select(node)

        self.assertEqual(selection.selected_skill, "builtin.data.sqlite")

    def _create_sqlite_db(self) -> None:
        db_path = self.workspace / "app.sqlite3"
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("create table tickets(team text, points integer)")
            conn.executemany(
                "insert into tickets(team, points) values (?, ?)",
                [("alpha", 4), ("beta", 6), ("alpha", 5)],
            )
            conn.commit()
        finally:
            conn.close()

    def test_sqlite_query_export_and_report_are_readonly(self) -> None:
        self._create_sqlite_db()

        query = self.run_skill(
            "builtin.data.sqlite",
            {
                "action": "query",
                "path": "app.sqlite3",
                "sql": "select team, sum(points) as total from tickets group by team order by team",
            },
        )
        report = self.run_skill(
            "builtin.data.sqlite",
            {
                "action": "report",
                "path": "app.sqlite3",
                "queries": [
                    {"label": "ticket_count", "sql": "select count(*) as value from tickets"},
                    {"label": "total_points", "sql": "select sum(points) as value from tickets"},
                ],
                "output_path": "reports/db.md",
            },
        )
        exported = self.run_skill(
            "builtin.data.sqlite",
            {
                "action": "export_csv",
                "path": "app.sqlite3",
                "sql": "select team, points from tickets order by points",
                "output_path": "reports/tickets.csv",
            },
        )
        rejected = self.run_skill(
            "builtin.data.sqlite",
            {"action": "query", "path": "app.sqlite3", "sql": "delete from tickets"},
        )

        self.assertTrue(query["success"])
        self.assertEqual(query["output"]["columns"], ["team", "total"])
        self.assertEqual(query["output"]["rows"][0]["team"], "alpha")
        self.assertTrue(report["success"])
        self.assertIn(
            "total_points: 15",
            (self.workspace / "reports/db.md").read_text(encoding="utf-8"),
        )
        self.assertTrue(exported["success"])
        self.assertIn(
            "team,points",
            (self.workspace / "reports/tickets.csv").read_text(encoding="utf-8"),
        )
        self.assertFalse(rejected["success"])
        self.assertEqual(rejected["error_code"], "validation_error")

    def test_markdown_extract_replace_and_table(self) -> None:
        (self.workspace / "docs").mkdir()
        (self.workspace / "docs/report.md").write_text(
            "# Report\n\n## Summary\nold\n\n## Metrics\n| name | value |\n| --- | --- |\n| score | 42 |\n",
            encoding="utf-8",
        )

        headings = self.run_skill(
            "builtin.text.markdown", {"action": "headings", "path": "docs/report.md"}
        )
        replaced = self.run_skill(
            "builtin.text.markdown",
            {
                "action": "replace_section",
                "path": "docs/report.md",
                "heading": "Summary",
                "content": "new",
            },
        )
        section = self.run_skill(
            "builtin.text.markdown",
            {"action": "extract_section", "path": "docs/report.md", "heading": "Summary"},
        )
        table = self.run_skill(
            "builtin.text.markdown", {"action": "table", "path": "docs/report.md"}
        )

        self.assertTrue(headings["success"])
        self.assertEqual([item["title"] for item in headings["output"]["headings"]], ["Report", "Summary", "Metrics"])
        self.assertTrue(replaced["success"])
        self.assertEqual(section["output"]["section"], "new")
        self.assertEqual(table["output"]["rows"], [{"name": "score", "value": "42"}])

    def test_git_skill_requires_process_permission_and_runs_readonly_status(self) -> None:
        denied = self.run_skill(
            "builtin.vcs.git",
            {"action": "status"},
            level="process",
            allow_external_commands=False,
        )

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=["git", "status", "--short"],
                returncode=0,
                stdout=" M README.md\n",
                stderr="",
            )
            result = self.run_skill(
                "builtin.vcs.git",
                {"action": "status", "cwd": "."},
                level="process",
                allow_external_commands=True,
            )

        self.assertFalse(denied["success"])
        self.assertEqual(denied["error_code"], "permission_denied")
        self.assertTrue(result["success"])
        self.assertEqual(result["output"]["stdout"], " M README.md\n")
        self.assertEqual(run.call_args.args[0], ["git", "status", "--short"])
        self.assertFalse(run.call_args.kwargs["shell"])

    def test_html_extracts_title_links_tables_and_report(self) -> None:
        (self.workspace / "page.html").write_text(
            """
            <html><head><title>Status Page</title><meta name="description" content="ops"></head>
            <body><h1>Current</h1><a href="/docs">Docs</a>
            <table><tr><th>name</th><th>value</th></tr><tr><td>uptime</td><td>99</td></tr></table>
            </body></html>
            """,
            encoding="utf-8",
        )

        extracted = self.run_skill(
            "builtin.web.html", {"action": "extract", "path": "page.html"}
        )
        report = self.run_skill(
            "builtin.web.html",
            {"action": "report", "path": "page.html", "output_path": "reports/page.md"},
        )

        self.assertTrue(extracted["success"])
        self.assertEqual(extracted["output"]["title"], "Status Page")
        self.assertEqual(extracted["output"]["links"], [{"href": "/docs", "text": "Docs"}])
        self.assertEqual(
            extracted["output"]["tables"][0]["rows"],
            [{"name": "uptime", "value": "99"}],
        )
        self.assertTrue(report["success"])
        self.assertIn(
            "title: Status Page",
            (self.workspace / "reports/page.md").read_text(encoding="utf-8"),
        )

    def test_archive_create_list_extract_and_reject_traversal(self) -> None:
        (self.workspace / "inputs").mkdir()
        (self.workspace / "inputs/a.txt").write_text("alpha", encoding="utf-8")

        created = self.run_skill(
            "builtin.archive.basic",
            {
                "action": "create",
                "paths": ["inputs"],
                "output_path": "bundle.zip",
                "format": "zip",
            },
        )
        listed = self.run_skill(
            "builtin.archive.basic", {"action": "list", "path": "bundle.zip"}
        )
        extracted = self.run_skill(
            "builtin.archive.basic",
            {"action": "extract", "path": "bundle.zip", "output_dir": "unpacked"},
        )

        unsafe_path = self.workspace / "unsafe.zip"
        with zipfile.ZipFile(unsafe_path, "w") as archive:
            archive.writestr("../escape.txt", "bad")
        unsafe = self.run_skill(
            "builtin.archive.basic",
            {"action": "extract", "path": "unsafe.zip", "output_dir": "unsafe"},
        )

        self.assertTrue(created["success"])
        self.assertIn("inputs/a.txt", [item["name"] for item in listed["output"]["entries"]])
        self.assertTrue(extracted["success"])
        self.assertEqual(
            (self.workspace / "unpacked/inputs/a.txt").read_text(encoding="utf-8"),
            "alpha",
        )
        self.assertFalse(unsafe["success"])
        self.assertEqual(unsafe["error_code"], "validation_error")

    def test_config_queries_toml_yaml_xml_and_report(self) -> None:
        (self.workspace / "service.toml").write_text(
            "[service]\nname = \"api\"\nretries = 3\n",
            encoding="utf-8",
        )
        (self.workspace / "service.yaml").write_text(
            "service:\n  owner: ops\n  enabled: true\n",
            encoding="utf-8",
        )
        (self.workspace / "service.xml").write_text(
            "<service><name>api</name><owner team=\"ops\">Maya</owner></service>",
            encoding="utf-8",
        )

        toml = self.run_skill(
            "builtin.data.config",
            {"action": "query", "path": "service.toml", "query": "service.retries"},
        )
        yaml = self.run_skill(
            "builtin.data.config",
            {"action": "query", "path": "service.yaml", "query": "service.owner"},
        )
        xml = self.run_skill(
            "builtin.data.config",
            {
                "action": "query",
                "path": "service.xml",
                "format": "xml",
                "query": "service.owner.@team",
            },
        )
        report = self.run_skill(
            "builtin.data.config",
            {
                "action": "report",
                "path": "service.toml",
                "fields": [{"label": "name", "query": "service.name"}],
                "output_path": "reports/config.md",
            },
        )

        self.assertEqual(toml["output"]["value"], 3)
        self.assertEqual(yaml["output"]["value"], "ops")
        self.assertEqual(xml["output"]["value"], "ops")
        self.assertTrue(report["success"])
        self.assertIn(
            "name: api",
            (self.workspace / "reports/config.md").read_text(encoding="utf-8"),
        )

    def test_config_report_accepts_unambiguous_section_aliases(self) -> None:
        (self.workspace / "service.toml").write_text(
            "[service]\nname = \"billing\"\nowner = \"platform\"\nretries = 3\n",
            encoding="utf-8",
        )

        report = self.run_skill(
            "builtin.data.config",
            {
                "action": "report",
                "path": "service.toml",
                "fields": [
                    {"label": "service_name", "query": "service_name"},
                    {"label": "owner", "query": "owner"},
                    {"label": "retries", "query": "retries"},
                ],
                "output_path": "reports/config_summary.md",
            },
        )

        self.assertTrue(report["success"])
        self.assertEqual(
            (self.workspace / "reports/config_summary.md").read_text(encoding="utf-8"),
            "service_name: billing\nowner: platform\nretries: 3\n",
        )

    def test_config_report_accepts_unambiguous_nested_leaf_alias(self) -> None:
        (self.workspace / "service.toml").write_text(
            "[service]\nname = \"billing\"\nowner = \"platform\"\n[service.limits]\nretries = 3\n",
            encoding="utf-8",
        )

        report = self.run_skill(
            "builtin.data.config",
            {
                "action": "report",
                "path": "service.toml",
                "fields": [{"label": "retries", "query": "retries"}],
                "output_path": "reports/config_summary.md",
            },
        )

        self.assertTrue(report["success"])
        self.assertEqual(
            (self.workspace / "reports/config_summary.md").read_text(encoding="utf-8"),
            "retries: 3\n",
        )


if __name__ == "__main__":
    unittest.main()

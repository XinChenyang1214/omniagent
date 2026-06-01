from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from omniagent.models import TaskNode, TaskNodeType
from omniagent.sandbox import build_sandbox
from omniagent.skill_registry import SkillRegistry
from omniagent.skills.loader import SkillLoader, load_yaml_like
from omniagent.storage import SQLiteStore


REQUIRED_MANIFEST_KEYS = {
    "name",
    "version",
    "description",
    "input_schema",
    "output_schema",
    "permission_requirements",
    "dependency_requirements",
    "side_effects",
    "sandbox_level",
    "tags",
    "contracts",
}


class BuiltinSkillTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.sandbox = build_sandbox(self.workspace, level="workspace")
        self.addCleanup(self.sandbox.close)
        self.loader = SkillLoader(self.workspace)
        self.definitions = self.loader.load_all()

    def run_skill(self, skill_name: str, payload: dict):
        runner = self.loader.load_runner(self.definitions[skill_name])
        return runner(payload, {"sandbox": self.sandbox})


class BuiltinSkillStructureTest(BuiltinSkillTestCase):
    def test_all_builtin_skills_have_standard_files_and_manifest_keys(self) -> None:
        for manifest_path in sorted(self.loader.builtin_root.rglob("skill.yaml")):
            root = manifest_path.parent
            with self.subTest(skill=root.name):
                self.assertTrue((root / "main.py").exists())
                self.assertTrue((root / "README.md").exists())
                self.assertTrue((root / "tests").is_dir())
                manifest = load_yaml_like(manifest_path)
                self.assertTrue(REQUIRED_MANIFEST_KEYS.issubset(manifest))
                self.assertIsInstance(manifest["contracts"], list)
                self.assertGreater(len(manifest["contracts"]), 0)

    def test_new_basecap_skills_load_with_expected_contract_actions(self) -> None:
        expected = {
            "builtin.data.json": {"validate", "query", "format", "transform", "report"},
            "builtin.datetime.format": {"now", "parse", "format", "diff", "add"},
            "builtin.data.csv": {"read", "select", "filter", "aggregate", "report", "write"},
            "builtin.http.client": {"get", "post", "put", "delete"},
            "builtin.shell.safe": {"run"},
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
                got = {contract.action for contract in self.definitions[skill_name].metadata.contracts}
                self.assertEqual(got, actions)


class BuiltinFileSkillTest(BuiltinSkillTestCase):
    def test_append_inserts_separator_newline_for_line_oriented_existing_file(self) -> None:
        path = self.workspace / "notes" / "session.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("turn_one: alpha", encoding="utf-8")

        result = self.run_skill(
            "builtin.file.basic",
            {
                "action": "append",
                "path": "notes/session.md",
                "content": "turn_two: beta",
            },
        )

        self.assertTrue(result["success"])
        self.assertEqual(
            path.read_text(encoding="utf-8"),
            "turn_one: alpha\nturn_two: beta",
        )

    def test_append_preserves_explicit_leading_newline(self) -> None:
        path = self.workspace / "notes" / "session.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("turn_one: alpha", encoding="utf-8")

        result = self.run_skill(
            "builtin.file.basic",
            {
                "action": "append",
                "path": "notes/session.md",
                "content": "\nturn_two: beta",
            },
        )

        self.assertTrue(result["success"])
        self.assertEqual(
            path.read_text(encoding="utf-8"),
            "turn_one: alpha\nturn_two: beta",
        )

    def test_replace_line_updates_exactly_one_matching_line(self) -> None:
        path = self.workspace / "configs" / "service.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "service: payments\nstatus: draft\nowner: platform\n",
            encoding="utf-8",
        )

        result = self.run_skill(
            "builtin.file.basic",
            {
                "action": "replace_line",
                "path": "configs/service.md",
                "line_prefix": "status:",
                "content": "status: reviewed",
            },
        )

        self.assertTrue(result["success"])
        self.assertEqual(
            path.read_text(encoding="utf-8"),
            "service: payments\nstatus: reviewed\nowner: platform\n",
        )

    def test_replace_line_rejects_ambiguous_matches(self) -> None:
        path = self.workspace / "configs" / "service.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("status: draft\nstatus: stale\n", encoding="utf-8")

        result = self.run_skill(
            "builtin.file.basic",
            {
                "action": "replace_line",
                "path": "configs/service.md",
                "line_prefix": "status:",
                "content": "status: reviewed",
            },
        )

        self.assertFalse(result["success"])
        self.assertIn("匹配到 2 行", result["message"])

    def test_registry_selects_new_skill_by_exact_required_skill(self) -> None:
        store = SQLiteStore(Path(self.tempdir.name) / "runtime.sqlite")
        registry = SkillRegistry(store)
        registry.sync(self.definitions)
        node = TaskNode(
            node_id="n1",
            type=TaskNodeType.ACTION,
            goal="format JSON",
            required_skills=["builtin.data.json"],
            input_payload={"action": "format", "text": "{}"},
        )

        selection = registry.select(node)

        self.assertEqual(selection.selected_skill, "builtin.data.json")

    def test_registry_selects_new_skill_by_tag_and_action(self) -> None:
        store = SQLiteStore(Path(self.tempdir.name) / "runtime.sqlite")
        registry = SkillRegistry(store)
        registry.sync(self.definitions)
        node = TaskNode(
            node_id="n1",
            type=TaskNodeType.ACTION,
            goal="filter rows",
            required_skills=["csv"],
            input_payload={"action": "filter", "rows": [], "filters": []},
        )

        selection = registry.select(node)

        self.assertEqual(selection.selected_skill, "builtin.data.csv")


class JsonSkillTest(BuiltinSkillTestCase):
    def test_json_validate_accepts_valid_text(self) -> None:
        result = self.run_skill("builtin.data.json", {"action": "validate", "text": '{"a": 1}'})

        self.assertTrue(result["success"])
        self.assertTrue(result["output"]["valid"])
        self.assertEqual(result["output"]["errors"], [])

    def test_json_validate_reports_invalid_text(self) -> None:
        result = self.run_skill("builtin.data.json", {"action": "validate", "text": '{"a":'})

        self.assertTrue(result["success"])
        self.assertFalse(result["output"]["valid"])
        self.assertTrue(result["output"]["errors"])

    def test_json_query_supports_dot_path_and_list_index(self) -> None:
        result = self.run_skill(
            "builtin.data.json",
            {"action": "query", "text": '{"items": [{"name": "alpha"}]}', "query": "items.0.name"},
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["output"]["value"], "alpha")

    def test_json_format_compacts_and_sorts_keys(self) -> None:
        result = self.run_skill(
            "builtin.data.json",
            {
                "action": "format",
                "text": '{"b": 2, "a": 1}',
                "format_options": {"compact": True, "sort_keys": True},
            },
        )

        self.assertEqual(result["output"]["formatted"], '{"a":1,"b":2}')

    def test_json_transform_can_pick_rename_set_and_delete(self) -> None:
        result = self.run_skill(
            "builtin.data.json",
            {
                "action": "transform",
                "text": '{"name": "alpha", "old": 1, "drop": true}',
                "transform": {
                    "pick": ["name", "old", "drop"],
                    "rename": {"old": "new"},
                    "set": {"extra": 3},
                    "delete": ["drop"],
                },
            },
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["output"]["value"], {"name": "alpha", "new": 1, "extra": 3})

    def test_json_reads_from_workspace_path(self) -> None:
        (self.workspace / "payload.json").write_text('{"ok": true}', encoding="utf-8")

        result = self.run_skill(
            "builtin.data.json",
            {"action": "query", "path": "payload.json", "query": "ok"},
        )

        self.assertTrue(result["success"])
        self.assertTrue(result["output"]["value"])
        self.assertEqual(result["output"]["path"], "payload.json")

    def test_json_report_extracts_fields_and_writes_file(self) -> None:
        (self.workspace / "profile.json").write_text(
            '{"user": {"name": "Ada", "city": "London"}, "stats": {"tasks": 7}}',
            encoding="utf-8",
        )

        result = self.run_skill(
            "builtin.data.json",
            {
                "action": "report",
                "path": "profile.json",
                "fields": [
                    {"label": "name", "query": "user.name"},
                    {"label": "city", "query": "user.city"},
                    {"label": "tasks", "query": "stats.tasks"},
                ],
                "output_path": "reports/profile_summary.md",
            },
        )

        self.assertTrue(result["success"])
        content = (self.workspace / "reports/profile_summary.md").read_text(encoding="utf-8")
        self.assertIn("name: Ada", content)
        self.assertIn("city: London", content)
        self.assertIn("tasks: 7", content)

    def test_json_missing_text_and_path_fails(self) -> None:
        result = self.run_skill("builtin.data.json", {"action": "validate"})

        self.assertFalse(result["success"])
        self.assertIn("text or path", result["message"])

    def test_json_unauthorized_path_returns_permission_failure(self) -> None:
        outside = Path(self.tempdir.name) / "outside.json"
        outside.write_text("{}", encoding="utf-8")

        result = self.run_skill(
            "builtin.data.json",
            {"action": "validate", "path": str(outside)},
        )

        self.assertFalse(result["success"])
        self.assertEqual(result.get("error_code"), "permission_denied")


class DateTimeSkillTest(BuiltinSkillTestCase):
    def test_datetime_now_defaults_to_utc(self) -> None:
        result = self.run_skill("builtin.datetime.format", {"action": "now"})

        self.assertTrue(result["success"])
        self.assertEqual(result["output"]["timezone"], "UTC")
        self.assertIn("+00:00", result["output"]["iso"])

    def test_datetime_parse_handles_z_suffix(self) -> None:
        result = self.run_skill(
            "builtin.datetime.format",
            {"action": "parse", "value": "2026-01-02T03:04:05Z"},
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["output"]["iso"], "2026-01-02T03:04:05+00:00")

    def test_datetime_parse_custom_input_format(self) -> None:
        result = self.run_skill(
            "builtin.datetime.format",
            {
                "action": "parse",
                "value": "2026/01/02 03:04",
                "input_format": "%Y/%m/%d %H:%M",
                "timezone": "UTC",
            },
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["output"]["iso"], "2026-01-02T03:04:00+00:00")

    def test_datetime_format_outputs_requested_format(self) -> None:
        result = self.run_skill(
            "builtin.datetime.format",
            {
                "action": "format",
                "value": "2026-01-02T03:04:05+00:00",
                "output_format": "%Y-%m-%d",
            },
        )

        self.assertEqual(result["output"]["value"], "2026-01-02")

    def test_datetime_diff_returns_seconds(self) -> None:
        result = self.run_skill(
            "builtin.datetime.format",
            {
                "action": "diff",
                "start": "2026-01-01T00:00:00+00:00",
                "end": "2026-01-01T00:01:30+00:00",
            },
        )

        self.assertEqual(result["output"]["diff"]["seconds"], 90.0)

    def test_datetime_add_supports_days(self) -> None:
        result = self.run_skill(
            "builtin.datetime.format",
            {
                "action": "add",
                "value": "2026-01-01T00:00:00+00:00",
                "amount": 2,
                "unit": "days",
            },
        )

        self.assertEqual(result["output"]["iso"], "2026-01-03T00:00:00+00:00")

    def test_datetime_unknown_timezone_fails(self) -> None:
        result = self.run_skill(
            "builtin.datetime.format",
            {"action": "now", "timezone": "No/SuchZone"},
        )

        self.assertFalse(result["success"])
        self.assertIn("unknown timezone", result["message"])

    def test_datetime_unsupported_unit_fails(self) -> None:
        result = self.run_skill(
            "builtin.datetime.format",
            {
                "action": "add",
                "value": "2026-01-01T00:00:00+00:00",
                "amount": 1,
                "unit": "months",
            },
        )

        self.assertFalse(result["success"])
        self.assertIn("unsupported datetime unit", result["message"])


class CsvSkillTest(BuiltinSkillTestCase):
    def setUp(self) -> None:
        super().setUp()
        (self.workspace / "people.csv").write_text(
            "name,age,city,score\n"
            "Ann,31,Paris,10.5\n"
            "Bob,25,Berlin,7.5\n"
            "Ana,40,Paris,12\n",
            encoding="utf-8",
        )

    def test_csv_read_returns_rows_columns_and_count(self) -> None:
        result = self.run_skill("builtin.data.csv", {"action": "read", "path": "people.csv"})

        self.assertTrue(result["success"])
        self.assertEqual(result["output"]["columns"], ["name", "age", "city", "score"])
        self.assertEqual(result["output"]["row_count"], 3)

    def test_csv_select_projects_columns_from_rows(self) -> None:
        result = self.run_skill(
            "builtin.data.csv",
            {
                "action": "select",
                "rows": [{"name": "Ann", "age": 31}],
                "columns": ["name"],
            },
        )

        self.assertEqual(result["output"]["rows"], [{"name": "Ann"}])
        self.assertEqual(result["output"]["columns"], ["name"])

    def test_csv_filter_supports_equals(self) -> None:
        result = self.run_skill(
            "builtin.data.csv",
            {
                "action": "filter",
                "path": "people.csv",
                "filters": [{"column": "city", "op": "equals", "value": "Paris"}],
            },
        )

        self.assertEqual(result["output"]["row_count"], 2)

    def test_csv_filter_supports_contains_and_numeric_gt(self) -> None:
        result = self.run_skill(
            "builtin.data.csv",
            {
                "action": "filter",
                "path": "people.csv",
                "filters": [
                    {"column": "name", "op": "contains", "value": "A"},
                    {"column": "age", "op": "gt", "value": 35},
                ],
            },
        )

        self.assertEqual(result["output"]["rows"][0]["name"], "Ana")

    def test_csv_aggregate_sum(self) -> None:
        result = self.run_skill(
            "builtin.data.csv",
            {"action": "aggregate", "path": "people.csv", "aggregate": {"op": "sum", "column": "score"}},
        )

        self.assertEqual(result["output"]["result"]["value"], 30.0)

    def test_csv_aggregate_count(self) -> None:
        result = self.run_skill(
            "builtin.data.csv",
            {"action": "aggregate", "path": "people.csv", "aggregate": {"op": "count"}},
        )

        self.assertEqual(result["output"]["result"]["value"], 3)

    def test_csv_report_computes_metrics_and_writes_file(self) -> None:
        result = self.run_skill(
            "builtin.data.csv",
            {
                "action": "report",
                "path": "people.csv",
                "metrics": [
                    {"name": "total_score", "op": "sum", "column": "score"},
                    {
                        "name": "paris_score",
                        "op": "sum",
                        "column": "score",
                        "filters": [{"column": "city", "op": "equals", "value": "Paris"}],
                    },
                ],
                "output_path": "reports/people_summary.md",
            },
        )

        self.assertTrue(result["success"])
        content = (self.workspace / "reports/people_summary.md").read_text(encoding="utf-8")
        self.assertIn("total_score: 30.0", content)
        self.assertIn("paris_score: 22.5", content)

    def test_csv_write_creates_workspace_file(self) -> None:
        result = self.run_skill(
            "builtin.data.csv",
            {
                "action": "write",
                "rows": [{"name": "Ann", "age": 31}],
                "columns": ["name", "age"],
                "output_path": "out.csv",
            },
        )

        self.assertTrue(result["success"])
        self.assertTrue((self.workspace / "out.csv").exists())
        self.assertIn("Ann,31", (self.workspace / "out.csv").read_text(encoding="utf-8"))

    def test_csv_write_requires_output_path(self) -> None:
        result = self.run_skill(
            "builtin.data.csv",
            {"action": "write", "rows": [{"name": "Ann"}]},
        )

        self.assertFalse(result["success"])
        self.assertIn("output_path", result["message"])

    def test_csv_unauthorized_path_returns_permission_failure(self) -> None:
        outside = Path(self.tempdir.name) / "outside.csv"
        outside.write_text("a\n1\n", encoding="utf-8")

        result = self.run_skill("builtin.data.csv", {"action": "read", "path": str(outside)})

        self.assertFalse(result["success"])
        self.assertEqual(result.get("error_code"), "permission_denied")


if __name__ == "__main__":
    unittest.main()

"""META-001 contract system tests.

Covers the four Phase 3 surfaces the contract refactor changed:
1. Contract loading: skill.yaml `contracts` → SkillMetadata / SkillRegistryEntry
2. Hard filter in SkillRegistry.select() — wrong action routes to contract_gap
3. Proactive payload validation in SkillDispatcher (missing required field, type errors)
4. Startup consistency check between BaseExecutor.supported_actions and builtin skill contracts
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from omniagent.dispatcher import SkillDispatcher, _value_matches_type
from omniagent.errors import ErrorCode, OmniAgentError
from omniagent.executors.file import FileExecutor
from omniagent.models import (
    SkillContract,
    SkillContractField,
    SkillRegistryEntry,
    SkillSelection,
    TaskNode,
    TaskNodeType,
)
from omniagent.skill_registry import EXECUTOR_TO_BUILTIN, SkillRegistry
from omniagent.skills.loader import SkillLoader
from omniagent.storage import SQLiteStore


class ContractLoadingTest(unittest.TestCase):
    """Phase 3a: builtin skill manifests parse into SkillContract objects."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.loader = SkillLoader(self.workspace)

    def test_file_basic_declares_nine_actions(self) -> None:
        defs = self.loader.load_all()
        self.assertIn("builtin.file.basic", defs)
        contracts = defs["builtin.file.basic"].metadata.contracts
        actions = {c.action for c in contracts}
        self.assertEqual(
            actions,
            {
                "list",
                "read",
                "write",
                "append",
                "replace_line",
                "copy",
                "move",
                "rename",
                "delete",
            },
        )

    def test_python_basic_declares_run_with_required_code(self) -> None:
        defs = self.loader.load_all()
        contracts = defs["builtin.python.basic"].metadata.contracts
        self.assertEqual([c.action for c in contracts], ["run"])
        run = contracts[0]
        required = run.required_input_fields()
        self.assertIn("code", required)

    def test_browser_basic_declares_fetch_with_required_url(self) -> None:
        defs = self.loader.load_all()
        contracts = defs["builtin.browser.basic"].metadata.contracts
        self.assertEqual([c.action for c in contracts], ["fetch"])
        fetch = contracts[0]
        self.assertIn("url", fetch.required_input_fields())

    def test_file_read_contract_marks_path_required_action_required(self) -> None:
        defs = self.loader.load_all()
        contract = next(
            c for c in defs["builtin.file.basic"].metadata.contracts if c.action == "read"
        )
        required = contract.required_input_fields()
        self.assertIn("action", required)
        self.assertIn("path", required)

    def test_contracts_round_trip_through_sqlite(self) -> None:
        store = SQLiteStore(Path(self.tempdir.name) / "runtime.sqlite")
        registry = SkillRegistry(store)
        defs = self.loader.load_all()
        registry.sync(defs)

        got = store.get_skill_registry_entry("builtin.file.basic")
        self.assertIsNotNone(got)
        assert got is not None  # type: ignore[unreachable]
        actions = {c.action for c in got.contracts}
        self.assertEqual(len(got.contracts), 9)
        self.assertIn("read", actions)
        # Ensure per-field schema survived the round trip
        read = got.contract_for_action("read")
        self.assertIsNotNone(read)
        assert read is not None
        path_field = read.input_field("path")
        self.assertIsNotNone(path_field)
        assert path_field is not None
        self.assertEqual(path_field.type, "string")
        self.assertTrue(path_field.required)

    def test_legacy_database_gets_contracts_column_via_migration(self) -> None:
        import sqlite3

        db_path = Path(self.tempdir.name) / "legacy.sqlite"
        # Build a legacy schema without the contracts_json column
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE skill_registry (
                skill_name TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                version TEXT NOT NULL,
                description TEXT NOT NULL,
                permission_requirements_json TEXT NOT NULL,
                tags_json TEXT NOT NULL,
                location TEXT NOT NULL,
                scope TEXT NOT NULL,
                sandbox_level TEXT NOT NULL,
                success_count INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                success_rate REAL NOT NULL DEFAULT 0.0,
                lifecycle_status TEXT NOT NULL DEFAULT 'active',
                activation_source TEXT,
                activation_target TEXT,
                last_validated_at TEXT,
                last_approved_at TEXT,
                last_seen_at TEXT NOT NULL
            );
            """
        )
        conn.commit()
        conn.close()

        # SQLiteStore.__init__ runs _ensure_skill_registry_columns which adds the column
        SQLiteStore(db_path)
        conn = sqlite3.connect(db_path)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(skill_registry)").fetchall()}
        conn.close()
        self.assertIn("contracts_json", columns)


class HardFilterTest(unittest.TestCase):
    """Phase 3a: SkillRegistry.select() hard-filters by requested action."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.store = SQLiteStore(Path(self.tempdir.name) / "runtime.sqlite")
        self.registry = SkillRegistry(self.store)
        self.loader = SkillLoader(self.workspace)
        self.registry.sync(self.loader.load_all())

    def _node(self, executor_hint: str, payload: dict) -> TaskNode:
        return TaskNode(
            node_id="n1",
            type=TaskNodeType.ACTION,
            goal="test",
            executor_hint=executor_hint,
            input_payload=payload,
        )

    def test_valid_file_action_picks_builtin(self) -> None:
        sel = self.registry.select(self._node("file", {"action": "read", "path": "foo.txt"}))
        self.assertEqual(sel.selected_skill, "builtin.file.basic")

    def test_unsupported_file_action_returns_contract_gap(self) -> None:
        sel = self.registry.select(self._node("file", {"action": "fetch", "url": "http://x"}))
        self.assertIsNone(sel.selected_skill)
        self.assertEqual(sel.explanation.get("source"), "contract_gap")
        self.assertIn("fetch", sel.gap_reason or "")

    def test_python_no_action_passes_through_filter(self) -> None:
        sel = self.registry.select(self._node("python", {"code": "print(1)"}))
        self.assertEqual(sel.selected_skill, "builtin.python.basic")

    def test_browser_no_action_passes_through_filter(self) -> None:
        sel = self.registry.select(self._node("browser", {"url": "http://x"}))
        self.assertEqual(sel.selected_skill, "builtin.browser.basic")

    def test_explicit_skill_missing_action_returns_contract_mismatch(self) -> None:
        node = self._node("file", {"action": "fetch"})
        node.skill_name = "builtin.file.basic"
        sel = self.registry.select(node)
        self.assertIsNone(sel.selected_skill)
        self.assertEqual(sel.explanation.get("source"), "contract_mismatch")


class PayloadValidationTest(unittest.TestCase):
    """Phase 3a: SkillDispatcher._validate_payload_against_contract fires pre-execution."""

    def setUp(self) -> None:
        self.dispatcher = SkillDispatcher.__new__(SkillDispatcher)
        self.entry_file = SkillRegistryEntry(
            skill_name="builtin.file.basic",
            source="builtin",
            version="0.3.0",
            description="",
            permission_requirements=[],
            tags=[],
            location="",
            scope="builtin",
            sandbox_level="workspace",
            contracts=[
                SkillContract(
                    action="read",
                    input_fields=[
                        SkillContractField(name="action", type="string", required=True),
                        SkillContractField(name="path", type="string", required=True),
                    ],
                ),
                SkillContract(
                    action="write",
                    input_fields=[
                        SkillContractField(name="action", type="string", required=True),
                        SkillContractField(name="path", type="string", required=True),
                        SkillContractField(name="content", type="string", required=True),
                        SkillContractField(name="overwrite", type="boolean", required=False),
                    ],
                ),
            ],
        )
        self.entry_python = SkillRegistryEntry(
            skill_name="builtin.python.basic",
            source="builtin",
            version="0.2.0",
            description="",
            permission_requirements=[],
            tags=[],
            location="",
            scope="builtin",
            sandbox_level="workspace",
            contracts=[
                SkillContract(
                    action="run",
                    input_fields=[SkillContractField(name="code", type="string", required=True)],
                ),
            ],
        )
        self.entry_legacy = SkillRegistryEntry(
            skill_name="legacy",
            source="builtin",
            version="0.1.0",
            description="",
            permission_requirements=[],
            tags=[],
            location="",
            scope="builtin",
            sandbox_level="workspace",
        )

    def _selection(self, entry: SkillRegistryEntry, executor_hint: str | None) -> SkillSelection:
        return SkillSelection(
            node_id="n1",
            selected_skill=entry.skill_name,
            executor_hint=executor_hint,
            registry_match=entry,
        )

    def _node(self, payload: dict, executor_hint: str | None = "file") -> TaskNode:
        return TaskNode(
            node_id="n1",
            type=TaskNodeType.ACTION,
            goal="test",
            executor_hint=executor_hint,
            input_payload=payload,
        )

    def test_valid_read_payload_passes(self) -> None:
        self.dispatcher._validate_payload_against_contract(
            self._node({"action": "read", "path": "foo.txt"}),
            self._selection(self.entry_file, "file"),
        )  # no raise

    def test_missing_required_path_raises_validation_error(self) -> None:
        with self.assertRaises(OmniAgentError) as ctx:
            self.dispatcher._validate_payload_against_contract(
                self._node({"action": "read"}),
                self._selection(self.entry_file, "file"),
            )
        self.assertEqual(ctx.exception.code, ErrorCode.VALIDATION_ERROR)
        self.assertIn("path", ctx.exception.details.get("missing", []))

    def test_missing_write_content_raises(self) -> None:
        with self.assertRaises(OmniAgentError) as ctx:
            self.dispatcher._validate_payload_against_contract(
                self._node({"action": "write", "path": "foo"}),
                self._selection(self.entry_file, "file"),
            )
        self.assertIn("content", ctx.exception.details.get("missing", []))

    def test_optional_field_type_mismatch_is_ignored(self) -> None:
        # overwrite is not required; even a wrong type shouldn't fail since
        # we only type-check required fields in Phase 3a.
        self.dispatcher._validate_payload_against_contract(
            self._node({"action": "write", "path": "foo", "content": "hi", "overwrite": "yes"}),
            self._selection(self.entry_file, "file"),
        )  # no raise

    def test_single_contract_skill_validates_without_action_key(self) -> None:
        # builtin.python.basic has exactly one contract and no action key
        # in the payload — validation should still run.
        self.dispatcher._validate_payload_against_contract(
            self._node({"code": "print(1)"}, executor_hint="python"),
            self._selection(self.entry_python, "python"),
        )
        with self.assertRaises(OmniAgentError):
            self.dispatcher._validate_payload_against_contract(
                self._node({"not_code": "bad"}, executor_hint="python"),
                self._selection(self.entry_python, "python"),
            )

    def test_legacy_skill_without_contracts_passes_through(self) -> None:
        self.dispatcher._validate_payload_against_contract(
            self._node({"anything": "goes"}),
            self._selection(self.entry_legacy, "file"),
        )  # no raise

    def test_required_string_field_rejects_dict_value(self) -> None:
        with self.assertRaises(OmniAgentError) as ctx:
            self.dispatcher._validate_payload_against_contract(
                self._node({"action": "read", "path": {"nested": "thing"}}),
                self._selection(self.entry_file, "file"),
            )
        # The error mentions type mismatch on path
        self.assertTrue(
            any("path" in err for err in ctx.exception.details.get("type_errors", [])),
        )


class ValueTypeMatchTest(unittest.TestCase):
    """Unit coverage for the _value_matches_type helper."""

    def test_exact_type_matches(self) -> None:
        self.assertTrue(_value_matches_type("abc", "string"))
        self.assertTrue(_value_matches_type(1, "integer"))
        self.assertTrue(_value_matches_type(1.5, "number"))
        self.assertTrue(_value_matches_type(True, "boolean"))
        self.assertTrue(_value_matches_type({}, "object"))
        self.assertTrue(_value_matches_type([], "array"))

    def test_boolean_is_not_integer_or_number(self) -> None:
        self.assertFalse(_value_matches_type(True, "integer"))
        self.assertFalse(_value_matches_type(True, "number"))

    def test_wrong_type_rejected(self) -> None:
        self.assertFalse(_value_matches_type(1, "string"))
        self.assertFalse(_value_matches_type("x", "integer"))

    def test_any_and_unknown_permissive(self) -> None:
        self.assertTrue(_value_matches_type("x", "any"))
        self.assertTrue(_value_matches_type({}, ""))
        self.assertTrue(_value_matches_type(object(), "weird_custom_type"))


class ExecutorConsistencyTest(unittest.TestCase):
    """Phase 3a: gateway cross-check between executor code and skill manifest."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.workspace = Path(self.tempdir.name) / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)

    def _mk_gateway(self):
        # Build a minimal gateway stub that exposes the check method. We can't
        # call the real RuntimeGateway here without a real LLM provider, so we
        # fabricate just what _check_executor_skill_consistency needs: a store
        # with the builtin skills synced + a dispatcher.executors mapping.
        store = SQLiteStore(Path(self.tempdir.name) / "runtime.sqlite")
        registry = SkillRegistry(store)
        loader = SkillLoader(self.workspace)
        registry.sync(loader.load_all())

        from omniagent.gateway import RuntimeGateway

        gateway = RuntimeGateway.__new__(RuntimeGateway)
        gateway.skill_registry = registry
        gateway.dispatcher = MagicMock()
        gateway.dispatcher.executors = {"file": FileExecutor()}
        return gateway

    def test_matching_actions_pass_check(self) -> None:
        gateway = self._mk_gateway()
        gateway._check_executor_skill_consistency()  # no raise

    def test_executor_missing_action_raises(self) -> None:
        gateway = self._mk_gateway()
        original = FileExecutor.supported_actions
        # Remove "delete" from the executor to simulate drift
        FileExecutor.supported_actions = tuple(a for a in original if a != "delete")
        try:
            with self.assertRaises(OmniAgentError) as ctx:
                gateway._check_executor_skill_consistency()
            self.assertEqual(ctx.exception.code, ErrorCode.SKILL_ERROR)
            self.assertIn("不一致", ctx.exception.message)
            self.assertIn("delete", ctx.exception.details.get("missing_in_executor", []))
        finally:
            FileExecutor.supported_actions = original

    def test_executor_extra_action_raises(self) -> None:
        gateway = self._mk_gateway()
        original = FileExecutor.supported_actions
        FileExecutor.supported_actions = tuple(list(original) + ["teleport"])
        try:
            with self.assertRaises(OmniAgentError) as ctx:
                gateway._check_executor_skill_consistency()
            self.assertIn("teleport", ctx.exception.details.get("missing_in_skill", []))
        finally:
            FileExecutor.supported_actions = original

    def test_empty_supported_actions_skips_check(self) -> None:
        gateway = self._mk_gateway()
        # Swap FileExecutor for a fake executor with empty supported_actions.
        from omniagent.executors.base import BaseExecutor

        class _Noop(BaseExecutor):
            name = "llm_native"
            supported_actions: tuple[str, ...] = ()

            def execute(self, context):  # type: ignore[override]
                raise NotImplementedError

        gateway.dispatcher.executors = {"llm_native": _Noop()}
        gateway._check_executor_skill_consistency()  # no raise, no builtin match required

    def test_executor_to_builtin_covers_action_executors(self) -> None:
        # Sanity: every EXECUTOR_TO_BUILTIN key must have a matching builtin
        # skill so the consistency check can resolve it.
        store = SQLiteStore(Path(self.tempdir.name) / "runtime.sqlite")
        registry = SkillRegistry(store)
        loader = SkillLoader(self.workspace)
        registry.sync(loader.load_all())
        for executor_name, builtin_name in EXECUTOR_TO_BUILTIN.items():
            entry = store.get_skill_registry_entry(builtin_name)
            self.assertIsNotNone(entry, f"{executor_name} -> {builtin_name} missing")


if __name__ == "__main__":
    unittest.main()

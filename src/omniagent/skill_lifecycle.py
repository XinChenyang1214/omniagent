from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from omniagent.errors import ErrorCode, OmniAgentError
from omniagent.models import (
    ApprovalRecord,
    ExecutionResult,
    RegistrationRecord,
    SkillContract,
    SkillDraft,
    SkillLifecycleStatus,
    SkillRegistryEntry,
    ValidationReport,
    utc_now,
)
from omniagent.sandbox import build_sandbox
from omniagent.skills.loader import load_yaml_like
from omniagent.storage import SQLiteStore
from omniagent.workflow_compiler import WorkflowCompiler

_STATUS_RANK = {
    SkillLifecycleStatus.DRAFT: 0,
    SkillLifecycleStatus.VALIDATED: 1,
    SkillLifecycleStatus.APPROVED: 2,
    SkillLifecycleStatus.ACTIVE: 3,
    SkillLifecycleStatus.DEPRECATED: 4,
}


def _safe_component(value: str) -> str:
    return (
        "".join(char if char.isalnum() or char in "._-" else "_" for char in value).strip("_")
        or "skill"
    )


def _version_key(value: str) -> tuple[int, int, int, str]:
    parts = value.split(".")
    parsed: list[int] = []
    for part in parts[:3]:
        parsed.append(int(part) if part.isdigit() else 0)
    while len(parsed) < 3:
        parsed.append(0)
    return parsed[0], parsed[1], parsed[2], value


class SkillLifecycleManager:
    def __init__(
        self,
        store: SQLiteStore,
        workspace_root: Path,
        workflow_compiler: WorkflowCompiler | None = None,
    ) -> None:
        self.store = store
        self.workspace_root = workspace_root.resolve()
        self.project_root = self.workspace_root.parent
        self.workflow_compiler = workflow_compiler
        self.managed_root = self.workspace_root / "skills" / "managed"
        self.archive_root = self.project_root / "skills" / "archive"
        self.managed_root.mkdir(parents=True, exist_ok=True)
        self.archive_root.mkdir(parents=True, exist_ok=True)

    def sync_workflow_candidates(self) -> list[SkillDraft]:
        if self.workflow_compiler is None:
            return []
        drafts: list[SkillDraft] = []
        for candidate in self.workflow_compiler.list_candidates():
            drafts.append(self.register_candidate(candidate))
        return drafts

    def register_candidate(self, candidate: dict[str, Any]) -> SkillDraft:
        manifest_path = Path(candidate["manifest_path"]).resolve()
        main_path = Path(candidate["main_path"]).resolve()
        manifest = load_yaml_like(manifest_path)
        candidate_status = SkillLifecycleStatus(candidate.get("status", SkillLifecycleStatus.DRAFT))
        current = self.store.get_skill_draft(
            str(candidate["skill_name"]), version=str(candidate["version"])
        )
        lifecycle_status = candidate_status
        if (
            current is not None
            and _STATUS_RANK[current.lifecycle_status] > _STATUS_RANK[candidate_status]
        ):
            lifecycle_status = current.lifecycle_status
        draft = SkillDraft(
            skill_name=str(candidate["skill_name"]),
            version=str(candidate["version"]),
            source_kind="workflow_compiled",
            source_ref=str(candidate.get("compiled_from_task_id", "")),
            source_dir=str(Path(candidate["candidate_dir"]).resolve()),
            manifest_path=str(manifest_path),
            main_path=str(main_path),
            permission_requirements=list(
                candidate.get(
                    "permission_requirements", manifest.get("permission_requirements", [])
                )
            ),
            lifecycle_status=lifecycle_status,
            metadata={
                "candidate": candidate,
                "description": manifest.get("description", candidate.get("promotion_reason", "")),
                "tags": list(manifest.get("tags", [])),
                "sandbox_level": str(manifest.get("sandbox_level", "workspace")),
            },
            created_at=current.created_at
            if current is not None
            else str(candidate.get("generated_at", utc_now())),
            updated_at=utc_now(),
        )
        self.store.upsert_skill_draft(draft)
        self._refresh_registry_entry(draft, manifest=manifest, force=False)
        return draft

    def import_skill(self, skill_root: Path) -> SkillDraft:
        skill_root = skill_root.resolve()
        manifest_path = skill_root / "skill.yaml"
        main_path = skill_root / "main.py"
        if not manifest_path.exists() or not main_path.exists():
            raise OmniAgentError(
                ErrorCode.NOT_FOUND,
                f"Skill 路径不完整: {skill_root}",
                details={"manifest": str(manifest_path), "main": str(main_path)},
            )
        return self._register_skill_root(
            skill_root,
            source_kind="manual_import",
            source_ref=str(skill_root),
        )

    def register_generated_skill(
        self,
        skill_root: Path,
        *,
        source_ref: str,
        metadata: dict[str, Any] | None = None,
    ) -> SkillDraft:
        skill_root = skill_root.resolve()
        manifest_path = skill_root / "skill.yaml"
        main_path = skill_root / "main.py"
        if not manifest_path.exists() or not main_path.exists():
            raise OmniAgentError(
                ErrorCode.NOT_FOUND,
                f"生成 Skill 路径不完整: {skill_root}",
                details={"manifest": str(manifest_path), "main": str(main_path)},
            )
        return self._register_skill_root(
            skill_root,
            source_kind="generated",
            source_ref=source_ref,
            metadata=metadata,
        )

    def _register_skill_root(
        self,
        skill_root: Path,
        *,
        source_kind: str,
        source_ref: str,
        metadata: dict[str, Any] | None = None,
    ) -> SkillDraft:
        manifest_path = skill_root / "skill.yaml"
        main_path = skill_root / "main.py"
        manifest = load_yaml_like(manifest_path)
        draft = SkillDraft(
            skill_name=str(manifest["name"]),
            version=str(manifest.get("version", "0.1.0")),
            source_kind=source_kind,
            source_ref=source_ref,
            source_dir=str(skill_root),
            manifest_path=str(manifest_path),
            main_path=str(main_path),
            permission_requirements=list(manifest.get("permission_requirements", [])),
            lifecycle_status=SkillLifecycleStatus.DRAFT,
            metadata={
                "description": manifest.get("description", ""),
                "tags": list(manifest.get("tags", [])),
                "sandbox_level": str(manifest.get("sandbox_level", "workspace")),
                **(metadata or {}),
            },
            updated_at=utc_now(),
        )
        self.store.upsert_skill_draft(draft)
        self._refresh_registry_entry(draft, manifest=manifest, force=False)
        return draft

    def promote_workflow(
        self,
        task_id: str,
        *,
        skill_name: str | None = None,
        version: str | None = None,
        approver: str = "local_user",
        rationale: str | None = None,
    ) -> dict[str, Any]:
        if self.workflow_compiler is None:
            raise OmniAgentError(
                ErrorCode.NOT_FOUND, "当前未启用 workflow compiler，无法执行 workflow 提升确认"
            )

        candidates = self.workflow_compiler.list_candidates(task_id=task_id)
        if skill_name is not None:
            candidates = [item for item in candidates if item.get("skill_name") == skill_name]
        if version is not None:
            candidates = [item for item in candidates if item.get("version") == version]
        if not candidates:
            raise OmniAgentError(
                ErrorCode.NOT_FOUND,
                f"未找到可确认的 workflow candidate: task_id={task_id}",
                details={"task_id": task_id, "skill_name": skill_name, "version": version},
            )
        if len(candidates) > 1:
            raise OmniAgentError(
                ErrorCode.VALIDATION_ERROR,
                f"workflow candidate 不唯一，请显式指定 skill_name/version: task_id={task_id}",
                details={
                    "task_id": task_id,
                    "matches": [
                        {"skill_name": item.get("skill_name"), "version": item.get("version")}
                        for item in candidates
                    ],
                },
            )

        candidate = candidates[0]
        promotion_metadata = {
            "promotion_source": "user_confirmed",
            "promotion_confirmed": True,
            "promotion_confirmed_by": approver,
            "promotion_confirmed_at": utc_now(),
            "promotion_rationale": rationale or "用户确认将该 workflow 提升为可复用能力",
        }
        source_relation = dict(candidate.get("source_relation", {}))
        source_relation.update(
            {
                "promotion_source": "user_confirmed",
                "promotion_confirmed_by": approver,
                "promotion_confirmed_at": promotion_metadata["promotion_confirmed_at"],
            }
        )
        promotion_metadata["source_relation"] = source_relation
        updated_candidate = self.workflow_compiler.update_candidate_status(
            str(candidate["skill_name"]),
            version=str(candidate.get("version")) if candidate.get("version") is not None else None,
            status=str(candidate.get("status", SkillLifecycleStatus.DRAFT)),
            metadata=promotion_metadata,
        )
        if updated_candidate is None:
            raise OmniAgentError(
                ErrorCode.NOT_FOUND,
                f"workflow candidate 更新失败: {candidate['skill_name']}",
                details={
                    "task_id": task_id,
                    "skill_name": candidate["skill_name"],
                    "version": candidate.get("version"),
                },
            )
        draft = self.register_candidate(updated_candidate)
        registry_entry = self.store.get_skill_registry_entry(draft.skill_name)
        return {
            "candidate": updated_candidate,
            "draft": draft,
            "registry_entry": registry_entry,
        }

    def list_skills(self, *, status: str | None = None) -> list[SkillRegistryEntry]:
        self.sync_workflow_candidates()
        entries = self.store.list_skill_registry_entries()
        if status is None:
            return entries
        return [entry for entry in entries if str(entry.lifecycle_status) == status]

    def show_skill(self, skill_name: str) -> dict[str, Any]:
        self.sync_workflow_candidates()
        registry_entry = self.store.get_skill_registry_entry(skill_name)
        drafts = self.store.list_skill_drafts(skill_name)
        if registry_entry is None and not drafts:
            return {}
        return {
            "registry_entry": registry_entry,
            "drafts": drafts,
            "validation_reports": self.store.list_validation_reports(skill_name),
            "approval_records": self.store.list_approval_records(skill_name),
            "registration_records": self.store.list_registration_records(skill_name),
        }

    def validate_skill(self, skill_name: str, *, version: str | None = None) -> dict[str, Any]:
        draft = self._require_draft(skill_name, version=version)
        manifest = load_yaml_like(Path(draft.manifest_path))
        static_check = self._static_check(draft, manifest)
        dependency_check = self._dependency_check(manifest)
        permission_check = self._permission_check(draft, manifest)
        unit_test_check = self._unit_test_check(Path(draft.source_dir))
        sandbox_check = self._sandbox_smoke_check(draft)
        activation_check = self._activation_readiness_check(manifest, sandbox_check)
        checks = {
            "static": static_check,
            "dependencies": dependency_check,
            "permissions": permission_check,
            "unit_tests": unit_test_check,
            "sandbox_smoke": sandbox_check,
            "activation_readiness": activation_check,
        }
        findings = [
            {
                "check": name,
                "details": payload.get("errors")
                or payload.get("warnings")
                or payload.get("details"),
            }
            for name, payload in checks.items()
            if payload["status"] == "failed"
        ]
        passed = all(
            checks[name]["status"] in {"passed", "skipped"}
            for name in ("static", "dependencies", "permissions", "unit_tests", "sandbox_smoke")
        )
        summary = "Skill 校验通过" if passed else "Skill 校验失败"
        if passed and activation_check["status"] != "passed":
            summary = f"{summary}，但当前还不能激活"
        report = ValidationReport(
            skill_name=draft.skill_name,
            version=draft.version,
            passed=passed,
            summary=summary,
            checks=checks,
            findings=findings,
        )
        self.store.append_validation_report(report)
        draft.lifecycle_status = (
            SkillLifecycleStatus.VALIDATED if passed else SkillLifecycleStatus.DRAFT
        )
        draft.updated_at = utc_now()
        self.store.upsert_skill_draft(draft)

        approval_record: ApprovalRecord | None = None
        policy = self._approval_policy(draft, manifest)
        if passed and not policy["approval_required"]:
            approval_record = ApprovalRecord(
                skill_name=draft.skill_name,
                version=draft.version,
                status="not_required",
                approver="system",
                rationale="低风险 Skill，自动通过审批策略",
                policy=policy,
            )
            self.store.append_approval_record(approval_record)
            draft.lifecycle_status = SkillLifecycleStatus.APPROVED
            draft.updated_at = utc_now()
            self.store.upsert_skill_draft(draft)

        self._refresh_registry_entry(draft, manifest=manifest, force=False)
        self._sync_workflow_candidate_status(draft)
        return {
            "draft": draft,
            "validation_report": report,
            "approval_record": approval_record,
            "approval_policy": policy,
        }

    def approve_skill(
        self,
        skill_name: str,
        *,
        version: str | None = None,
        approver: str = "local_user",
        approved: bool = True,
        rationale: str | None = None,
    ) -> dict[str, Any]:
        draft = self._require_draft(skill_name, version=version)
        manifest = load_yaml_like(Path(draft.manifest_path))
        validation = self.store.latest_validation_report(skill_name, version=draft.version)
        if validation is None or not validation.passed:
            raise OmniAgentError(
                ErrorCode.VALIDATION_ERROR,
                f"Skill 尚未通过校验: {skill_name}@{draft.version}",
            )
        policy = self._approval_policy(draft, manifest)
        status = "approved" if approved else "rejected"
        record = ApprovalRecord(
            skill_name=draft.skill_name,
            version=draft.version,
            status=status,
            approver=approver,
            rationale=rationale or ("人工审批通过" if approved else "人工审批拒绝"),
            policy=policy,
        )
        self.store.append_approval_record(record)
        draft.lifecycle_status = (
            SkillLifecycleStatus.APPROVED if approved else SkillLifecycleStatus.VALIDATED
        )
        draft.updated_at = utc_now()
        self.store.upsert_skill_draft(draft)
        self._refresh_registry_entry(draft, manifest=manifest, force=False)
        self._sync_workflow_candidate_status(draft)
        return {"draft": draft, "approval_record": record, "approval_policy": policy}

    def activate_skill(self, skill_name: str, *, version: str | None = None) -> dict[str, Any]:
        draft = self._require_draft(skill_name, version=version)
        validation = self.store.latest_validation_report(skill_name, version=draft.version)
        if validation is None or not validation.passed:
            raise OmniAgentError(
                ErrorCode.VALIDATION_ERROR, f"Skill 未通过校验: {skill_name}@{draft.version}"
            )
        activation_check = validation.checks.get("activation_readiness", {})
        if activation_check.get("status") != "passed":
            raise OmniAgentError(
                ErrorCode.VALIDATION_ERROR,
                f"Skill 尚未达到可激活状态: {skill_name}@{draft.version}",
                details={"blockers": activation_check.get("errors", [])},
            )
        approval = self.store.latest_approval_record(skill_name, version=draft.version)
        if approval is None or approval.status not in {"approved", "not_required"}:
            raise OmniAgentError(
                ErrorCode.PERMISSION_DENIED, f"Skill 尚未通过审批: {skill_name}@{draft.version}"
            )

        source_dir = Path(draft.source_dir)
        active_dir = self.managed_root / _safe_component(skill_name)
        previous_version: str | None = None
        action = "activate"
        if active_dir.exists():
            current_manifest = load_yaml_like(active_dir / "skill.yaml")
            previous_version = str(current_manifest.get("version", "0.0.0"))
            archive_dir = self.archive_root / _safe_component(skill_name) / previous_version
            if archive_dir.exists():
                shutil.rmtree(archive_dir)
            archive_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(active_dir, archive_dir)
            shutil.rmtree(active_dir)
            action = "upgrade"

        shutil.copytree(source_dir, active_dir)
        activated_manifest = self._write_active_manifest(active_dir)
        record = RegistrationRecord(
            skill_name=skill_name,
            version=draft.version,
            action=action,
            status="active",
            source_path=str(source_dir),
            target_path=str(active_dir),
            previous_version=previous_version,
            metadata={"managed_root": str(self.managed_root)},
        )
        self.store.append_registration_record(record)
        draft.lifecycle_status = SkillLifecycleStatus.ACTIVE
        draft.updated_at = utc_now()
        self.store.upsert_skill_draft(draft)
        if previous_version:
            previous = self.store.get_skill_draft(skill_name, version=previous_version)
            if previous is not None:
                previous.lifecycle_status = SkillLifecycleStatus.APPROVED
                previous.updated_at = utc_now()
                self.store.upsert_skill_draft(previous)
        self._refresh_registry_entry(
            draft,
            manifest=activated_manifest,
            force=True,
            activation_target=str(active_dir),
        )
        self._sync_workflow_candidate_status(draft, extra={"activation_target": str(active_dir)})
        return {"draft": draft, "registration_record": record}

    def rollback_skill(self, skill_name: str, *, to_version: str | None = None) -> dict[str, Any]:
        entry = self.store.get_skill_registry_entry(skill_name)
        active_dir = self.managed_root / _safe_component(skill_name)
        if (
            entry is None
            or entry.lifecycle_status != SkillLifecycleStatus.ACTIVE
            or not active_dir.exists()
        ):
            raise OmniAgentError(ErrorCode.NOT_FOUND, f"当前没有 active Skill 可回滚: {skill_name}")

        archive_base = self.archive_root / _safe_component(skill_name)
        candidates = [path for path in archive_base.iterdir()] if archive_base.exists() else []
        if not candidates:
            raise OmniAgentError(ErrorCode.NOT_FOUND, f"没有可回滚版本: {skill_name}")
        if to_version is None:
            candidates.sort(key=lambda item: _version_key(item.name), reverse=True)
            target_dir = candidates[0]
        else:
            target_dir = archive_base / to_version
        if not target_dir.exists():
            raise OmniAgentError(
                ErrorCode.NOT_FOUND, f"回滚目标版本不存在: {skill_name}@{to_version}"
            )

        current_manifest = load_yaml_like(active_dir / "skill.yaml")
        current_version = str(current_manifest.get("version", entry.version))
        current_archive_dir = archive_base / current_version
        if current_archive_dir.exists():
            shutil.rmtree(current_archive_dir)
        current_archive_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(active_dir, current_archive_dir)
        shutil.rmtree(active_dir)
        shutil.copytree(target_dir, active_dir)
        rolled_manifest = self._write_active_manifest(active_dir)
        target_version = str(rolled_manifest.get("version", target_dir.name))

        record = RegistrationRecord(
            skill_name=skill_name,
            version=target_version,
            action="rollback",
            status="active",
            source_path=str(target_dir),
            target_path=str(active_dir),
            previous_version=current_version,
            metadata={"rolled_back_from": current_version},
        )
        self.store.append_registration_record(record)

        target_draft = self.store.get_skill_draft(skill_name, version=target_version)
        if target_draft is not None:
            target_draft.lifecycle_status = SkillLifecycleStatus.ACTIVE
            target_draft.updated_at = utc_now()
            self.store.upsert_skill_draft(target_draft)
            self._refresh_registry_entry(
                target_draft,
                manifest=rolled_manifest,
                force=True,
                activation_target=str(active_dir),
            )
            self._sync_workflow_candidate_status(
                target_draft, extra={"activation_target": str(active_dir)}
            )
        return {"registration_record": record, "target_version": target_version}

    def deprecate_skill(self, skill_name: str) -> dict[str, Any]:
        entry = self.store.get_skill_registry_entry(skill_name)
        active_dir = self.managed_root / _safe_component(skill_name)
        if entry is None:
            raise OmniAgentError(ErrorCode.NOT_FOUND, f"Skill 不存在: {skill_name}")
        source_path = entry.activation_target or entry.location
        if active_dir.exists():
            deprecated_dir = (
                self.archive_root / _safe_component(skill_name) / f"deprecated_{entry.version}"
            )
            if deprecated_dir.exists():
                shutil.rmtree(deprecated_dir)
            deprecated_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(active_dir, deprecated_dir)
            shutil.rmtree(active_dir)
        record = RegistrationRecord(
            skill_name=skill_name,
            version=entry.version,
            action="deprecate",
            status="deprecated",
            source_path=str(source_path),
            target_path=None,
            previous_version=entry.version,
            metadata={},
        )
        self.store.append_registration_record(record)
        draft = self.store.get_skill_draft(skill_name, version=entry.version)
        if draft is not None:
            draft.lifecycle_status = SkillLifecycleStatus.DEPRECATED
            draft.updated_at = utc_now()
            self.store.upsert_skill_draft(draft)
            manifest = load_yaml_like(Path(draft.manifest_path))
        else:
            manifest = {
                "description": entry.description,
                "permission_requirements": entry.permission_requirements,
                "tags": entry.tags,
                "sandbox_level": entry.sandbox_level,
            }
        self.store.upsert_skill_registry_entry(
            SkillRegistryEntry(
                skill_name=entry.skill_name,
                source=entry.source,
                version=entry.version,
                description=entry.description,
                permission_requirements=entry.permission_requirements,
                tags=[tag for tag in entry.tags if tag != "active"] + ["deprecated"],
                location=entry.location,
                scope=entry.scope,
                sandbox_level=entry.sandbox_level,
                success_count=entry.success_count,
                failure_count=entry.failure_count,
                success_rate=entry.success_rate,
                lifecycle_status=SkillLifecycleStatus.DEPRECATED,
                activation_source=entry.activation_source,
                activation_target=None,
                last_validated_at=entry.last_validated_at,
                last_approved_at=entry.last_approved_at,
                last_seen_at=utc_now(),
                # META-001: preserve the skill's declared contracts on deprecation
                # so downstream callers can still see what actions it *used to*
                # support. The dispatcher already filters out deprecated skills
                # via active_only, so this is purely informational.
                contracts=list(entry.contracts),
            )
        )
        if draft is not None:
            self._sync_workflow_candidate_status(draft)
        return {"registration_record": record, "manifest": manifest}

    def _require_draft(self, skill_name: str, *, version: str | None = None) -> SkillDraft:
        self.sync_workflow_candidates()
        draft = self.store.get_skill_draft(skill_name, version=version)
        if draft is None:
            raise OmniAgentError(
                ErrorCode.NOT_FOUND,
                f"Skill draft 不存在: {skill_name}{'@' + version if version else ''}",
            )
        return draft

    def _static_check(self, draft: SkillDraft, manifest: dict[str, Any]) -> dict[str, Any]:
        errors: list[str] = []
        if str(manifest.get("name", "")) != draft.skill_name:
            errors.append("manifest.name 与 draft skill_name 不一致")
        if str(manifest.get("version", "")) != draft.version:
            errors.append("manifest.version 与 draft version 不一致")
        if not Path(draft.main_path).exists():
            errors.append("缺少 main.py")
        for key in ("input_schema", "output_schema", "permission_requirements", "sandbox_level"):
            if key not in manifest:
                errors.append(f"manifest 缺少字段: {key}")
        return {"status": "passed" if not errors else "failed", "errors": errors}

    def _dependency_check(self, manifest: dict[str, Any]) -> dict[str, Any]:
        dependencies = [str(item) for item in manifest.get("dependency_requirements", [])]
        risky = [
            item
            for item in dependencies
            if any(token in item for token in ("git+", "http://", "https://", "-e "))
        ]
        return {
            "status": "passed" if not risky else "failed",
            "dependencies": dependencies,
            "errors": [f"依赖声明过于宽松或不可审计: {item}" for item in risky],
        }

    def _permission_check(self, draft: SkillDraft, manifest: dict[str, Any]) -> dict[str, Any]:
        declared = {str(item) for item in manifest.get("permission_requirements", [])}
        expected = {str(item) for item in draft.permission_requirements}
        missing = sorted(expected - declared)
        return {
            "status": "passed" if not missing else "failed",
            "declared": sorted(declared),
            "expected": sorted(expected),
            "errors": [f"缺少权限声明: {item}" for item in missing],
        }

    def _unit_test_check(self, skill_root: Path) -> dict[str, Any]:
        tests_dir = skill_root / "tests"
        if not tests_dir.exists() or not list(tests_dir.rglob("test_*.py")):
            return {"status": "skipped", "details": "未发现 tests 目录或 test_*.py"}
        completed = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
            cwd=skill_root,
            text=True,
            capture_output=True,
            check=False,
        )
        output = (completed.stdout + completed.stderr).strip()
        return {
            "status": "passed" if completed.returncode == 0 else "failed",
            "returncode": completed.returncode,
            "details": output[-4000:],
        }

    def _sandbox_smoke_check(self, draft: SkillDraft) -> dict[str, Any]:
        try:
            runner = self._load_runner(Path(draft.main_path), draft.skill_name)
            with tempfile.TemporaryDirectory() as tempdir:
                root = Path(tempdir)
                task_dir = root / "task"
                task_dir.mkdir(parents=True, exist_ok=True)
                sandbox = build_sandbox(
                    root,
                    level=str(draft.metadata.get("sandbox_level", "workspace")),
                )
                try:
                    payload = runner(
                        {},
                        {
                            "task": {
                                "task_id": f"validation_{uuid.uuid4().hex[:6]}",
                                "goal": draft.skill_name,
                                "status": "validation",
                            },
                            "workspace_root": str(sandbox.runtime_root),
                            "task_dir": str(sandbox.translate_path(task_dir)),
                            "sandbox": sandbox,
                            "bootstrap": {},
                            "memory_summaries": [],
                            "workflow_summaries": [],
                        },
                    )
                finally:
                    sandbox.close()
        except Exception as exc:  # pragma: no cover - exercised in failure path
            return {"status": "failed", "errors": [str(exc)]}

        if isinstance(payload, ExecutionResult):
            status = payload.status
            message = payload.message
        elif isinstance(payload, dict):
            status = str(payload.get("status", "completed"))
            message = str(payload.get("message", ""))
        else:
            return {"status": "failed", "errors": ["run() 返回值不是 dict / ExecutionResult"]}
        return {"status": "passed", "execution_status": status, "message": message}

    def _activation_readiness_check(
        self, manifest: dict[str, Any], sandbox_check: dict[str, Any]
    ) -> dict[str, Any]:
        errors: list[str] = []
        if bool(manifest.get("draft", False)):
            errors.append("manifest 仍标记为 draft=true")
        if "workflow_compiled_draft" in [str(item) for item in manifest.get("side_effects", [])]:
            errors.append("side_effects 表明当前仍是 workflow compiled draft")
        if (
            sandbox_check.get("status") == "passed"
            and sandbox_check.get("execution_status") == "draft"
        ):
            errors.append("smoke test 显示 run() 仍返回 draft 状态")
        return {"status": "passed" if not errors else "failed", "errors": errors}

    def _approval_policy(self, draft: SkillDraft, manifest: dict[str, Any]) -> dict[str, Any]:
        permissions = {
            str(item)
            for item in manifest.get("permission_requirements", draft.permission_requirements)
        }
        risk_level = "low"
        reasons: list[str] = []
        if (
            permissions & {"code_execution", "network"}
            or str(manifest.get("sandbox_level", "workspace")) != "workspace"
        ):
            risk_level = "high"
            reasons.append("涉及代码执行、网络或非 workspace sandbox")
        elif permissions & {"filesystem.delete"} or manifest.get("dependency_requirements"):
            risk_level = "medium"
            reasons.append("涉及删除权限或额外依赖")
        elif draft.source_kind not in {"workflow_compiled", "manual_import", "generated"}:
            risk_level = "medium"
            reasons.append("来源不在默认低风险集合")
        approval_required = risk_level in {"medium", "high"}
        return {
            "risk_level": risk_level,
            "approval_required": approval_required,
            "permissions": sorted(permissions),
            "source_kind": draft.source_kind,
            "reasons": reasons or ["低风险本地 Skill"],
        }

    def _refresh_registry_entry(
        self,
        draft: SkillDraft,
        *,
        manifest: dict[str, Any],
        force: bool,
        activation_target: str | None = None,
    ) -> SkillRegistryEntry:
        current = self.store.get_skill_registry_entry(draft.skill_name)
        if (
            current is not None
            and current.lifecycle_status == SkillLifecycleStatus.ACTIVE
            and current.version != draft.version
            and not force
        ):
            return current
        last_validation = self.store.latest_validation_report(
            draft.skill_name, version=draft.version
        )
        last_approval = self.store.latest_approval_record(draft.skill_name, version=draft.version)
        location = (
            activation_target
            or (current.activation_target if current and current.version == draft.version else None)
            or draft.source_dir
        )
        source_map = {
            "workflow_compiled": "workflow_compiler",
            "manual_import": "manual_import",
            "generated": "skill_generator",
        }
        scope_map = {
            "workflow_compiled": "generated",
            "manual_import": "managed",
            "generated": "generated",
        }
        # META-001: parse structured contracts off the manifest so the
        # registered entry exposes the same action schema that the loader
        # builds for builtin skills. Generated / workflow-compiled skills can
        # omit the section and fall back to empty contracts (legacy pass-through).
        manifest_contracts_raw = manifest.get("contracts", []) or []
        if not isinstance(manifest_contracts_raw, list):
            raise OmniAgentError(
                ErrorCode.SKILL_ERROR,
                f"Skill {draft.skill_name}@{draft.version} 的 manifest.contracts 必须是 list",
            )
        parsed_contracts: list[SkillContract] = []
        for item in manifest_contracts_raw:
            if isinstance(item, dict):
                parsed_contracts.append(SkillContract.from_dict(item))

        entry = SkillRegistryEntry(
            skill_name=draft.skill_name,
            source=source_map.get(draft.source_kind, draft.source_kind),
            version=draft.version,
            description=str(manifest.get("description", draft.metadata.get("description", ""))),
            permission_requirements=list(
                manifest.get("permission_requirements", draft.permission_requirements)
            ),
            tags=list(manifest.get("tags", draft.metadata.get("tags", []))),
            location=str(location),
            scope=scope_map.get(draft.source_kind, "managed"),
            sandbox_level=str(
                manifest.get("sandbox_level", draft.metadata.get("sandbox_level", "workspace"))
            ),
            success_count=current.success_count if current else 0,
            failure_count=current.failure_count if current else 0,
            success_rate=current.success_rate if current else 0.0,
            lifecycle_status=draft.lifecycle_status,
            activation_source=draft.source_dir,
            activation_target=activation_target
            if activation_target is not None
            else (current.activation_target if current else None),
            last_validated_at=last_validation.created_at
            if last_validation
            else (current.last_validated_at if current else None),
            last_approved_at=last_approval.created_at
            if last_approval
            else (current.last_approved_at if current else None),
            last_seen_at=utc_now(),
            contracts=parsed_contracts,
        )
        self.store.upsert_skill_registry_entry(entry)
        return entry

    def _sync_workflow_candidate_status(
        self, draft: SkillDraft, extra: dict[str, Any] | None = None
    ) -> None:
        if self.workflow_compiler is None or draft.source_kind != "workflow_compiled":
            return
        self.workflow_compiler.update_candidate_status(
            draft.skill_name,
            version=draft.version,
            status=str(draft.lifecycle_status),
            metadata=extra,
        )

    def _write_active_manifest(self, skill_root: Path) -> dict[str, Any]:
        manifest_path = skill_root / "skill.yaml"
        manifest = load_yaml_like(manifest_path)
        tags = [str(item) for item in manifest.get("tags", []) if str(item) != "draft"]
        if "active" not in tags:
            tags.append("active")
        manifest["tags"] = tags
        manifest["draft"] = False
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return manifest

    def _load_runner(self, main_path: Path, skill_name: str):
        module_name = f"omniagent_skill_validation_{_safe_component(skill_name)}_{uuid.uuid4().hex}"
        spec = importlib.util.spec_from_file_location(module_name, main_path)
        if spec is None or spec.loader is None:
            raise OmniAgentError(ErrorCode.SKILL_ERROR, f"无法加载 Skill: {main_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        runner = getattr(module, "run", None)
        if runner is None or not callable(runner):
            raise OmniAgentError(
                ErrorCode.SKILL_ERROR, f"Skill 缺少 run(payload, context): {main_path}"
            )
        return runner

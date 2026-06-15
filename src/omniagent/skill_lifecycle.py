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


def _bump_patch(value: str | None) -> str:
    major, minor, patch, _ = _version_key(value or "0.0.0")
    return f"{major}.{minor}.{patch + 1}"


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

    def promote_habit_to_skill(
        self,
        procedure: dict[str, Any],
        *,
        skill_name: str | None = None,
        version: str | None = None,
        approver: str = "local_user",
        rationale: str | None = None,
    ) -> dict[str, Any]:
        self._validate_habit_promotion_source(procedure)
        resolved_skill_name = skill_name or self._habit_skill_name(procedure)
        resolved_version = version or self._next_skill_version(resolved_skill_name)
        skill_root, source_relation = self._write_habit_skill_package(
            procedure,
            skill_name=resolved_skill_name,
            version=resolved_version,
            rationale=rationale,
        )
        draft = self._register_skill_root(
            skill_root,
            source_kind="habit_promoted",
            source_ref=str(procedure["procedure_id"]),
            metadata={
                "description": f"Habit promoted skill from {procedure.get('name')}",
                "source_relation": source_relation,
                "lineage_path": str(skill_root / "lineage.json"),
                "promotion_rationale": rationale
                or "稳定 habit 满足 promotion gate，生成可验证 Skill",
            },
        )
        validation_payload = self.validate_skill(draft.skill_name, version=draft.version)
        validation_report = validation_payload["validation_report"]
        approval_record = validation_payload[
            "approval_record"
        ] or self.store.latest_approval_record(draft.skill_name, version=draft.version)
        registration_record = None
        if validation_payload["draft"].lifecycle_status == SkillLifecycleStatus.APPROVED:
            activation_payload = self.activate_skill(draft.skill_name, version=draft.version)
            registration_record = activation_payload["registration_record"]
        registry_entry = self.store.get_skill_registry_entry(draft.skill_name)
        return {
            "procedure_id": procedure["procedure_id"],
            "habit_id": procedure["procedure_id"],
            "skill_name": draft.skill_name,
            "version": draft.version,
            "skill_root": str(skill_root),
            "lineage_path": str(skill_root / "lineage.json"),
            "lineage": source_relation,
            "draft": draft,
            "validation_report": validation_report,
            "approval_record": approval_record,
            "registration_record": registration_record,
            "registry_entry": registry_entry,
        }

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
            "lineage_records": [
                dict(draft.metadata["source_relation"])
                for draft in drafts
                if isinstance(draft.metadata.get("source_relation"), dict)
            ],
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

    def activate_skill(
        self,
        skill_name: str,
        *,
        version: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
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
        metadata = {"managed_root": str(self.managed_root)}
        if note:
            metadata["note"] = note
        record = RegistrationRecord(
            skill_name=skill_name,
            version=draft.version,
            action=action,
            status="active",
            source_path=str(source_dir),
            target_path=str(active_dir),
            previous_version=previous_version,
            metadata=metadata,
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

    def deprecate_skill(self, skill_name: str, *, note: str | None = None) -> dict[str, Any]:
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
            metadata={"note": note} if note else {},
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

    def _validate_habit_promotion_source(self, procedure: dict[str, Any]) -> None:
        if str(procedure.get("status")) != "habit":
            raise OmniAgentError(
                ErrorCode.VALIDATION_ERROR,
                "skill promotion requires a stable habit procedure",
                details={
                    "procedure_id": procedure.get("procedure_id"),
                    "status": procedure.get("status"),
                },
            )
        if int(procedure.get("success_count", 0)) < 3:
            raise OmniAgentError(
                ErrorCode.VALIDATION_ERROR,
                "skill promotion requires at least 3 habit successes",
                details={"procedure_id": procedure.get("procedure_id")},
            )
        if int(procedure.get("failure_count", 0)) > 0:
            raise OmniAgentError(
                ErrorCode.VALIDATION_ERROR,
                "skill promotion requires no recent habit failures",
                details={"procedure_id": procedure.get("procedure_id")},
            )
        if not procedure.get("source_experience_ids"):
            raise OmniAgentError(
                ErrorCode.VALIDATION_ERROR,
                "skill promotion requires source experience lineage",
                details={"procedure_id": procedure.get("procedure_id")},
            )
        validator = procedure.get("validator") or {}
        if not list(validator.get("required_evidence", [])):
            raise OmniAgentError(
                ErrorCode.VALIDATION_ERROR,
                "skill promotion requires validator evidence refs",
                details={"procedure_id": procedure.get("procedure_id")},
            )
        generation_type = self._habit_generation_type(procedure)
        if generation_type not in {
            "file",
            "json",
            "csv",
            "config",
            "text",
            "markdown",
            "sqlite",
            "html",
            "archive",
            "datetime",
            "git",
            "python",
            "http",
            "shell",
            "browser",
        }:
            raise OmniAgentError(
                ErrorCode.VALIDATION_ERROR,
                "habit skill promotion currently supports file, JSON/JSONL data, CSV/TSV data, config, text, markdown, SQLite, HTML, archive, datetime, read-only git, Python, HTTP, safe shell, and browser fetch habits only",
                details={
                    "procedure_id": procedure.get("procedure_id"),
                    "generation_type": generation_type,
                    "steps": procedure.get("steps", []),
                },
            )

    def _habit_skill_name(self, procedure: dict[str, Any]) -> str:
        name = str(procedure.get("name") or procedure.get("procedure_id") or "habit")
        normalized = _safe_component(name.lower().replace(" ", ".")).replace("_", ".")
        short_id = str(procedure.get("procedure_id", "habit"))[-6:]
        return f"generated.habit.{normalized}.{short_id}"

    def _next_skill_version(self, skill_name: str) -> str:
        versions = [draft.version for draft in self.store.list_skill_drafts(skill_name)]
        entry = self.store.get_skill_registry_entry(skill_name)
        if entry is not None:
            versions.append(entry.version)
        latest = max(versions, key=_version_key) if versions else None
        return _bump_patch(latest)

    def _habit_generation_type(self, procedure: dict[str, Any]) -> str | None:
        targets = {
            str(step.get("target", ""))
            for step in list(procedure.get("steps", []))
            if isinstance(step, dict)
        }
        if targets & {"builtin.data.csv", "data.csv", "csv", "tsv"}:
            return "csv"
        if targets & {"builtin.data.sqlite", "data.sqlite", "sqlite"}:
            return "sqlite"
        if targets & {
            "builtin.data.json",
            "data.json",
            "json",
            "jsonl",
            "ndjson",
            "json.lines",
            "data.jsonl",
        }:
            return "json"
        if targets & {
            "builtin.data.config",
            "data.config",
            "config",
            "toml",
            "yaml",
            "yml",
            "xml",
            "env",
            "dotenv",
            "ini",
            "properties",
            "conf",
            "cfg",
        }:
            return "config"
        if targets & {
            "builtin.text.basic",
            "text.basic",
            "text",
            "plain_text",
            "plaintext",
            "txt",
            "log",
        }:
            return "text"
        if targets & {"builtin.text.markdown", "text.markdown", "markdown", "md"}:
            return "markdown"
        if targets & {"builtin.web.html", "web.html", "html"}:
            return "html"
        if targets & {
            "builtin.archive.basic",
            "archive.basic",
            "archive",
            "zip",
            "tar",
            "gztar",
            "tgz",
            "tar.gz",
            "tar_gz",
            "tar-gz",
        }:
            return "archive"
        if targets & {"builtin.datetime.format", "datetime.format", "datetime", "time"}:
            return "datetime"
        if targets & {"builtin.vcs.git", "vcs.git", "git"}:
            return "git"
        if targets & {"builtin.file.basic", "file"}:
            return "file"
        if targets & {"builtin.python.basic", "python"}:
            return "python"
        if targets & {"builtin.shell.safe", "shell.safe", "shell"}:
            return "shell"
        if targets & {"builtin.browser.basic", "browser"}:
            return "browser"
        if targets & {"builtin.http.client", "http", "api"}:
            return "http"
        return None

    def _write_habit_skill_package(
        self,
        procedure: dict[str, Any],
        *,
        skill_name: str,
        version: str,
        rationale: str | None,
    ) -> tuple[Path, dict[str, Any]]:
        generation_type = self._habit_generation_type(procedure)
        if generation_type not in {
            "file",
            "json",
            "csv",
            "config",
            "text",
            "markdown",
            "sqlite",
            "html",
            "archive",
            "datetime",
            "git",
            "python",
            "http",
            "shell",
            "browser",
        }:
            raise OmniAgentError(
                ErrorCode.VALIDATION_ERROR,
                "habit skill promotion currently supports file, JSON/JSONL data, CSV/TSV data, config, text, markdown, SQLite, HTML, archive, datetime, read-only git, Python, HTTP, safe shell, and browser fetch habits only",
                details={"generation_type": generation_type},
            )
        skill_root = (
            self.project_root
            / "skills"
            / "generated"
            / "habits"
            / _safe_component(skill_name)
            / version
        )
        skill_root.mkdir(parents=True, exist_ok=True)
        validator = procedure.get("validator") or {}
        source_targets: list[str] = []
        for step in list(procedure.get("steps", [])):
            if not isinstance(step, dict):
                continue
            target = str(step.get("target") or "").strip()
            if target and target not in source_targets:
                source_targets.append(target)
        source_relation = {
            "kind": "habit_promoted",
            "procedure_id": procedure.get("procedure_id"),
            "habit_id": procedure.get("procedure_id"),
            "procedure_name": procedure.get("name"),
            "source_targets": source_targets,
            "source_experience_ids": list(procedure.get("source_experience_ids", [])),
            "source_task_ids": list(procedure.get("source_task_ids", [])),
            "validator_evidence_refs": list(validator.get("required_evidence", [])),
            "risk_level": procedure.get("risk_level", "low"),
            "success_count": int(procedure.get("success_count", 0)),
            "failure_count": int(procedure.get("failure_count", 0)),
            "skill_name": skill_name,
            "skill_version": version,
            "generation_type": generation_type,
            "promotion_rationale": rationale or "稳定 habit 满足 skill promotion gate",
            "promoted_at": utc_now(),
        }
        manifest = self._habit_manifest(
            generation_type=generation_type,
            skill_name=skill_name,
            version=version,
            procedure=procedure,
            source_relation=source_relation,
        )
        (skill_root / "skill.yaml").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if generation_type == "sqlite":
            main_code = self._habit_sqlite_main()
        elif generation_type == "csv":
            main_code = self._habit_csv_main()
        elif generation_type == "text":
            main_code = self._habit_text_main()
        elif generation_type == "markdown":
            main_code = self._habit_markdown_main()
        elif generation_type == "html":
            main_code = self._habit_html_main()
        elif generation_type == "archive":
            main_code = self._habit_archive_main()
        elif generation_type == "datetime":
            main_code = self._habit_datetime_main()
        elif generation_type == "git":
            main_code = self._habit_git_main()
        elif generation_type == "python":
            main_code = self._habit_python_main()
        elif generation_type == "http":
            main_code = self._habit_http_main()
        elif generation_type == "shell":
            main_code = self._habit_shell_main()
        elif generation_type == "browser":
            main_code = self._habit_browser_main()
        elif generation_type == "config":
            main_code = self._habit_config_main()
        elif generation_type == "json":
            main_code = self._habit_json_main()
        else:
            main_code = self._habit_file_main()
        (skill_root / "main.py").write_text(main_code, encoding="utf-8")
        tests_dir = skill_root / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        if generation_type == "sqlite":
            test_code = self._habit_sqlite_test(skill_name=skill_name)
        elif generation_type == "csv":
            test_code = self._habit_csv_test(skill_name=skill_name)
        elif generation_type == "text":
            test_code = self._habit_text_test(skill_name=skill_name)
        elif generation_type == "markdown":
            test_code = self._habit_markdown_test(skill_name=skill_name)
        elif generation_type == "html":
            test_code = self._habit_html_test(skill_name=skill_name)
        elif generation_type == "archive":
            test_code = self._habit_archive_test(skill_name=skill_name)
        elif generation_type == "datetime":
            test_code = self._habit_datetime_test(skill_name=skill_name)
        elif generation_type == "git":
            test_code = self._habit_git_test(skill_name=skill_name)
        elif generation_type == "python":
            test_code = self._habit_python_test(skill_name=skill_name)
        elif generation_type == "http":
            test_code = self._habit_http_test(skill_name=skill_name)
        elif generation_type == "shell":
            test_code = self._habit_shell_test(skill_name=skill_name)
        elif generation_type == "browser":
            test_code = self._habit_browser_test(skill_name=skill_name)
        elif generation_type == "config":
            test_code = self._habit_config_test(skill_name=skill_name)
        elif generation_type == "json":
            test_code = self._habit_json_test(skill_name=skill_name)
        else:
            test_code = self._habit_file_test(skill_name=skill_name)
        (tests_dir / "test_skill.py").write_text(test_code, encoding="utf-8")
        (skill_root / "lineage.json").write_text(
            json.dumps(source_relation, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (skill_root / "README.md").write_text(
            "\n".join(
                [
                    f"# {skill_name}",
                    "",
                    f"- version: {version}",
                    "- source: habit_promoted",
                    f"- generation_type: {generation_type}",
                    f"- habit_id: {procedure.get('procedure_id')}",
                    f"- source_experience_ids: {', '.join(source_relation['source_experience_ids'])}",
                    f"- validator_evidence_refs: {', '.join(source_relation['validator_evidence_refs'])}",
                    "",
                    "This skill was promoted from a stable habit with validation evidence.",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return skill_root, source_relation

    def _habit_manifest(
        self,
        *,
        generation_type: str,
        skill_name: str,
        version: str,
        procedure: dict[str, Any],
        source_relation: dict[str, Any],
    ) -> dict[str, Any]:
        if generation_type == "csv":
            return {
                "name": skill_name,
                "version": version,
                "description": f"Habit promoted CSV/TSV data skill from: {procedure.get('name')}",
                "input_schema": {
                    "action": "string",
                    "path": "string",
                    "rows": "array",
                    "columns": "array",
                    "filters": "array",
                    "aggregate": "object",
                    "metrics": "array",
                    "output_path": "string",
                    "delimiter": "string",
                    "overwrite": "boolean",
                },
                "output_schema": {
                    "rows": "array",
                    "columns": "array",
                    "row_count": "integer",
                    "result": "object",
                    "path": "string",
                },
                "permission_requirements": ["filesystem.read", "filesystem.write"],
                "dependency_requirements": [],
                "side_effects": [],
                "sandbox_level": "workspace",
                "tags": ["generated", "habit-promoted", "csv", "data"],
                "source_relation": source_relation,
                "contracts": self._habit_csv_contracts(),
            }
        if generation_type == "sqlite":
            return {
                "name": skill_name,
                "version": version,
                "description": f"Habit promoted SQLite data skill from: {procedure.get('name')}",
                "input_schema": {
                    "action": "string",
                    "path": "string",
                    "table": "string",
                    "sql": "string",
                    "params": "array",
                    "queries": "array",
                    "output_path": "string",
                    "max_rows": "integer",
                    "overwrite": "boolean",
                },
                "output_schema": {
                    "tables": "array",
                    "result": "object",
                    "columns": "array",
                    "rows": "array",
                    "row_count": "integer",
                    "truncated": "boolean",
                    "path": "string",
                    "output_path": "string",
                },
                "permission_requirements": ["filesystem.read", "filesystem.write"],
                "dependency_requirements": [],
                "side_effects": [],
                "sandbox_level": "workspace",
                "tags": ["generated", "habit-promoted", "sqlite", "database", "data"],
                "source_relation": source_relation,
                "contracts": self._habit_sqlite_contracts(),
            }
        if generation_type == "json":
            return {
                "name": skill_name,
                "version": version,
                "description": f"Habit promoted JSON/JSONL data skill from: {procedure.get('name')}",
                "input_schema": {
                    "action": "string",
                    "text": "string",
                    "path": "string",
                    "query": "string",
                    "format_options": "object",
                    "fields": "array",
                    "output_path": "string",
                    "overwrite": "boolean",
                },
                "output_schema": {
                    "valid": "boolean",
                    "value": "any",
                    "formatted": "string",
                    "errors": "array",
                    "path": "string",
                },
                "permission_requirements": ["filesystem.read", "filesystem.write"],
                "dependency_requirements": [],
                "side_effects": [],
                "sandbox_level": "workspace",
                "tags": ["generated", "habit-promoted", "json", "data"],
                "source_relation": source_relation,
                "contracts": self._habit_json_contracts(),
            }
        if generation_type == "config":
            return {
                "name": skill_name,
                "version": version,
                "description": f"Habit promoted config data skill from: {procedure.get('name')}",
                "input_schema": {
                    "action": "string",
                    "text": "string",
                    "path": "string",
                    "format": "string",
                    "query": "string",
                    "fields": "array",
                    "output_path": "string",
                    "overwrite": "boolean",
                },
                "output_schema": {
                    "format": "string",
                    "value": "any",
                    "result": "object",
                    "path": "string",
                    "output_path": "string",
                },
                "permission_requirements": ["filesystem.read", "filesystem.write"],
                "dependency_requirements": [],
                "side_effects": [],
                "sandbox_level": "workspace",
                "tags": ["generated", "habit-promoted", "config", "data"],
                "source_relation": source_relation,
                "contracts": self._habit_config_contracts(),
            }
        if generation_type == "text":
            return {
                "name": skill_name,
                "version": version,
                "description": f"Habit promoted plain text skill from: {procedure.get('name')}",
                "input_schema": {
                    "action": "string",
                    "text": "string",
                    "path": "string",
                    "contains": "string",
                    "line_prefix": "string",
                    "old": "string",
                    "new": "string",
                    "count": "integer",
                    "output_path": "string",
                    "ignore_case": "boolean",
                    "overwrite": "boolean",
                },
                "output_schema": {
                    "text": "string",
                    "lines": "array",
                    "matches": "array",
                    "stats": "object",
                    "path": "string",
                    "output_path": "string",
                    "replacement_count": "integer",
                },
                "permission_requirements": ["filesystem.read", "filesystem.write"],
                "dependency_requirements": [],
                "side_effects": [],
                "sandbox_level": "workspace",
                "tags": ["generated", "habit-promoted", "text", "plain-text"],
                "source_relation": source_relation,
                "contracts": self._habit_text_contracts(),
            }
        if generation_type == "markdown":
            return {
                "name": skill_name,
                "version": version,
                "description": f"Habit promoted Markdown text skill from: {procedure.get('name')}",
                "input_schema": {
                    "action": "string",
                    "text": "string",
                    "path": "string",
                    "heading": "string",
                    "content": "string",
                    "output_path": "string",
                    "table_index": "integer",
                    "overwrite": "boolean",
                },
                "output_schema": {
                    "headings": "array",
                    "section": "string",
                    "tables": "array",
                    "rows": "array",
                    "path": "string",
                    "output_path": "string",
                },
                "permission_requirements": ["filesystem.read", "filesystem.write"],
                "dependency_requirements": [],
                "side_effects": [],
                "sandbox_level": "workspace",
                "tags": ["generated", "habit-promoted", "markdown", "text", "document"],
                "source_relation": source_relation,
                "contracts": self._habit_markdown_contracts(),
            }
        if generation_type == "html":
            return {
                "name": skill_name,
                "version": version,
                "description": f"Habit promoted HTML extraction skill from: {procedure.get('name')}",
                "input_schema": {
                    "action": "string",
                    "path": "string",
                    "text": "string",
                    "output_path": "string",
                    "max_text_chars": "integer",
                    "overwrite": "boolean",
                },
                "output_schema": {
                    "title": "string",
                    "text": "string",
                    "links": "array",
                    "meta": "object",
                    "tables": "array",
                    "path": "string",
                    "output_path": "string",
                },
                "permission_requirements": ["filesystem.read", "filesystem.write"],
                "dependency_requirements": [],
                "side_effects": [],
                "sandbox_level": "workspace",
                "tags": ["generated", "habit-promoted", "html", "web", "extract"],
                "source_relation": source_relation,
                "contracts": self._habit_html_contracts(),
            }
        if generation_type == "archive":
            return {
                "name": skill_name,
                "version": version,
                "description": f"Habit promoted archive workspace skill from: {procedure.get('name')}",
                "input_schema": {
                    "action": "string",
                    "path": "string",
                    "paths": "array",
                    "output_path": "string",
                    "output_dir": "string",
                    "format": "string",
                    "overwrite": "boolean",
                },
                "output_schema": {
                    "entries": "array",
                    "path": "string",
                    "output_path": "string",
                    "output_dir": "string",
                    "format": "string",
                },
                "permission_requirements": ["filesystem.read", "filesystem.write"],
                "dependency_requirements": [],
                "side_effects": [],
                "sandbox_level": "workspace",
                "tags": ["generated", "habit-promoted", "archive", "zip", "tar"],
                "source_relation": source_relation,
                "contracts": self._habit_archive_contracts(),
            }
        if generation_type == "git":
            return {
                "name": skill_name,
                "version": version,
                "description": f"Habit promoted read-only git skill from: {procedure.get('name')}",
                "input_schema": {
                    "action": "string",
                    "cwd": "string",
                    "paths": "array",
                    "rev": "string",
                    "stat": "boolean",
                    "max_count": "integer",
                    "max_output_chars": "integer",
                    "timeout": "number",
                },
                "output_schema": {
                    "argv": "array",
                    "cwd": "string",
                    "returncode": "integer",
                    "stdout": "string",
                    "stderr": "string",
                    "stdout_truncated": "boolean",
                    "stderr_truncated": "boolean",
                },
                "permission_requirements": ["system.exec"],
                "dependency_requirements": ["git"],
                "side_effects": ["process_execution"],
                "sandbox_level": "process",
                "tags": ["generated", "habit-promoted", "git", "vcs", "readonly"],
                "source_relation": source_relation,
                "contracts": self._habit_git_contracts(),
            }
        if generation_type == "http":
            return {
                "name": skill_name,
                "version": version,
                "description": f"Habit promoted HTTP client skill from: {procedure.get('name')}",
                "input_schema": {
                    "action": "string",
                    "url": "string",
                    "headers": "object",
                    "params": "object",
                    "json": "any",
                    "json_body": "any",
                    "form": "object",
                    "raw_body": "string",
                    "body": "string",
                    "auth": "object",
                    "timeout": "number",
                    "max_response_chars": "integer",
                },
                "output_schema": {
                    "method": "string",
                    "url": "string",
                    "request_url": "string",
                    "status": "integer",
                    "reason": "string",
                    "headers": "object",
                    "body": "string",
                    "body_preview": "string",
                    "body_truncated": "boolean",
                    "body_size": "integer",
                    "json": "any",
                },
                "permission_requirements": ["network"],
                "dependency_requirements": [],
                "side_effects": ["network_read", "network_write"],
                "sandbox_level": "workspace",
                "tags": ["generated", "habit-promoted", "http", "api", "network"],
                "source_relation": source_relation,
                "contracts": self._habit_http_contracts(),
            }
        if generation_type == "browser":
            return {
                "name": skill_name,
                "version": version,
                "description": f"Habit promoted browser fetch skill from: {procedure.get('name')}",
                "input_schema": {
                    "action": "string",
                    "url": "string",
                    "max_body_chars": "integer",
                },
                "output_schema": {
                    "url": "string",
                    "request_url": "string",
                    "status": "integer",
                    "body": "string",
                    "body_preview": "string",
                    "body_truncated": "boolean",
                    "source": "string",
                    "blocked": "boolean",
                    "blocked_reason": "string",
                },
                "permission_requirements": ["network"],
                "dependency_requirements": [],
                "side_effects": ["network_read"],
                "sandbox_level": "workspace",
                "tags": ["generated", "habit-promoted", "browser", "web", "network"],
                "source_relation": source_relation,
                "contracts": self._habit_browser_contracts(),
            }
        if generation_type == "python":
            return {
                "name": skill_name,
                "version": version,
                "description": f"Habit promoted Python execution skill from: {procedure.get('name')}",
                "input_schema": {
                    "action": "string",
                    "code": "string",
                },
                "output_schema": {
                    "result": "any",
                    "stdout": "string",
                    "traceback": "string",
                },
                "permission_requirements": ["code_execution"],
                "dependency_requirements": [],
                "side_effects": ["python_execution"],
                "sandbox_level": "workspace",
                "tags": ["generated", "habit-promoted", "python", "code_execution"],
                "source_relation": source_relation,
                "contracts": self._habit_python_contracts(),
            }
        if generation_type == "shell":
            return {
                "name": skill_name,
                "version": version,
                "description": f"Habit promoted safe shell skill from: {procedure.get('name')}",
                "input_schema": {
                    "action": "string",
                    "argv": "array",
                    "command": "array",
                    "cwd": "string",
                    "timeout": "number",
                    "max_output_chars": "integer",
                },
                "output_schema": {
                    "argv": "array",
                    "cwd": "string",
                    "returncode": "integer",
                    "stdout": "string",
                    "stderr": "string",
                    "stdout_preview": "string",
                    "stderr_preview": "string",
                    "stdout_truncated": "boolean",
                    "stderr_truncated": "boolean",
                    "timed_out": "boolean",
                },
                "permission_requirements": ["system.exec"],
                "dependency_requirements": [],
                "side_effects": ["process_execution"],
                "sandbox_level": "process",
                "tags": ["generated", "habit-promoted", "shell", "safe_shell", "readonly"],
                "source_relation": source_relation,
                "contracts": self._habit_shell_contracts(),
            }
        if generation_type == "datetime":
            return {
                "name": skill_name,
                "version": version,
                "description": f"Habit promoted datetime utility skill from: {procedure.get('name')}",
                "input_schema": {
                    "action": "string",
                    "value": "string",
                    "timezone": "string",
                    "input_format": "string",
                    "output_format": "string",
                    "amount": "number",
                    "unit": "string",
                    "start": "string",
                    "end": "string",
                },
                "output_schema": {
                    "value": "string",
                    "iso": "string",
                    "timestamp": "number",
                    "timezone": "string",
                    "diff": "object",
                },
                "permission_requirements": [],
                "dependency_requirements": [],
                "side_effects": [],
                "sandbox_level": "workspace",
                "tags": ["generated", "habit-promoted", "datetime", "time"],
                "source_relation": source_relation,
                "contracts": self._habit_datetime_contracts(),
            }
        return {
            "name": skill_name,
            "version": version,
            "description": f"Habit promoted file skill from: {procedure.get('name')}",
            "input_schema": {
                "action": "string",
                "path": "string",
                "source": "string",
                "target": "string",
                "content": "string",
                "line_prefix": "string",
                "overwrite": "boolean",
            },
            "output_schema": {
                "path": "string",
                "source": "string",
                "target": "string",
                "action": "string",
                "line_index": "integer",
                "size": "integer",
                "entries": "array",
                "content": "string",
            },
            "permission_requirements": ["filesystem.read", "filesystem.write"],
            "dependency_requirements": [],
            "side_effects": [],
            "sandbox_level": "workspace",
            "tags": ["generated", "habit-promoted", "file"],
            "source_relation": source_relation,
            "contracts": self._habit_file_contracts(),
        }

    def _habit_file_contracts(self) -> list[dict[str, Any]]:
        return [
            {
                "action": "list",
                "description": "List a workspace directory.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    {"name": "path", "type": "string", "required": False},
                ],
                "output_fields": [
                    {"name": "entries", "type": "array"},
                    {"name": "path", "type": "string"},
                ],
                "side_effects": ["filesystem.read"],
            },
            {
                "action": "read",
                "description": "Read a workspace text file.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    {"name": "path", "type": "string", "required": True},
                ],
                "output_fields": [
                    {"name": "content", "type": "string"},
                    {"name": "path", "type": "string"},
                ],
                "side_effects": ["filesystem.read"],
            },
            {
                "action": "write",
                "description": "Write a workspace text file.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    {"name": "path", "type": "string", "required": True},
                    {"name": "content", "type": "string", "required": True},
                    {"name": "overwrite", "type": "boolean", "required": False},
                ],
                "output_fields": [
                    {"name": "path", "type": "string"},
                    {"name": "size", "type": "integer"},
                    {"name": "written_path", "type": "string"},
                ],
                "side_effects": ["filesystem.write"],
            },
            {
                "action": "append",
                "description": "Append to a workspace text file.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    {"name": "path", "type": "string", "required": True},
                    {"name": "content", "type": "string", "required": True},
                ],
                "output_fields": [
                    {"name": "path", "type": "string"},
                    {"name": "size", "type": "integer"},
                    {"name": "written_path", "type": "string"},
                ],
                "side_effects": ["filesystem.write"],
            },
            {
                "action": "replace_line",
                "description": "Replace exactly one line by prefix in a workspace text file.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    {"name": "path", "type": "string", "required": True},
                    {"name": "line_prefix", "type": "string", "required": True},
                    {"name": "content", "type": "string", "required": True},
                ],
                "output_fields": [
                    {"name": "path", "type": "string"},
                    {"name": "line_index", "type": "integer"},
                    {"name": "size", "type": "integer"},
                    {"name": "written_path", "type": "string"},
                ],
                "side_effects": ["filesystem.read", "filesystem.write"],
            },
            {
                "action": "copy",
                "description": "Copy a workspace file or directory.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    {"name": "source", "type": "string", "required": True},
                    {"name": "target", "type": "string", "required": True},
                    {"name": "overwrite", "type": "boolean", "required": False},
                ],
                "output_fields": [
                    {"name": "source", "type": "string"},
                    {"name": "target", "type": "string"},
                    {"name": "action", "type": "string"},
                ],
                "side_effects": ["filesystem.read", "filesystem.write"],
            },
        ]

    def _habit_json_contracts(self) -> list[dict[str, Any]]:
        return [
            {
                "action": "validate",
                "description": "Validate JSON text or a workspace JSON file.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    {"name": "text", "type": "string", "required": False},
                    {"name": "path", "type": "string", "required": False},
                ],
                "output_fields": [
                    {"name": "valid", "type": "boolean"},
                    {"name": "errors", "type": "array"},
                    {"name": "path", "type": "string"},
                ],
                "side_effects": [],
            },
            {
                "action": "query",
                "description": "Read a value by dot-path from JSON text or file.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    {"name": "text", "type": "string", "required": False},
                    {"name": "path", "type": "string", "required": False},
                    {"name": "query", "type": "string", "required": True},
                ],
                "output_fields": [
                    {"name": "valid", "type": "boolean"},
                    {"name": "value", "type": "any"},
                    {"name": "errors", "type": "array"},
                    {"name": "path", "type": "string"},
                ],
                "side_effects": [],
            },
            {
                "action": "query_lines",
                "description": "Read values by dot-path from newline-delimited JSON text or file.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    {"name": "text", "type": "string", "required": False},
                    {"name": "path", "type": "string", "required": False},
                    {"name": "query", "type": "string", "required": True},
                ],
                "output_fields": [
                    {"name": "valid", "type": "boolean"},
                    {"name": "value", "type": "array"},
                    {"name": "rows", "type": "array"},
                    {"name": "line_count", "type": "integer"},
                    {"name": "errors", "type": "array"},
                    {"name": "path", "type": "string"},
                ],
                "side_effects": [],
            },
            {
                "action": "report_lines",
                "description": "Extract JSONL fields and write a label-value report.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    {"name": "text", "type": "string", "required": False},
                    {"name": "path", "type": "string", "required": False},
                    {"name": "query", "type": "string", "required": False},
                    {"name": "label", "type": "string", "required": False},
                    {"name": "fields", "type": "array", "required": False},
                    {"name": "output_path", "type": "string", "required": True},
                    {"name": "overwrite", "type": "boolean", "required": False},
                ],
                "output_fields": [
                    {"name": "valid", "type": "boolean"},
                    {"name": "value", "type": "object"},
                    {"name": "formatted", "type": "string"},
                    {"name": "line_count", "type": "integer"},
                    {"name": "errors", "type": "array"},
                    {"name": "path", "type": "string"},
                    {"name": "source_path", "type": "string"},
                ],
                "side_effects": ["filesystem.write"],
            },
            {
                "action": "format",
                "description": "Pretty-print or compact JSON.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    {"name": "text", "type": "string", "required": False},
                    {"name": "path", "type": "string", "required": False},
                    {"name": "format_options", "type": "object", "required": False},
                ],
                "output_fields": [
                    {"name": "valid", "type": "boolean"},
                    {"name": "value", "type": "any"},
                    {"name": "formatted", "type": "string"},
                    {"name": "errors", "type": "array"},
                    {"name": "path", "type": "string"},
                ],
                "side_effects": [],
            },
            {
                "action": "report",
                "description": "Extract JSON fields and write a label-value report.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    {"name": "text", "type": "string", "required": False},
                    {"name": "path", "type": "string", "required": False},
                    {"name": "fields", "type": "array", "required": True},
                    {"name": "output_path", "type": "string", "required": True},
                    {"name": "overwrite", "type": "boolean", "required": False},
                ],
                "output_fields": [
                    {"name": "valid", "type": "boolean"},
                    {"name": "value", "type": "object"},
                    {"name": "formatted", "type": "string"},
                    {"name": "errors", "type": "array"},
                    {"name": "path", "type": "string"},
                ],
                "side_effects": ["filesystem.write"],
            },
        ]

    def _habit_csv_contracts(self) -> list[dict[str, Any]]:
        common_source_fields = [
            {"name": "path", "type": "string", "required": False},
            {"name": "rows", "type": "array", "required": False},
            {"name": "delimiter", "type": "string", "required": False},
        ]
        common_outputs = [
            {"name": "rows", "type": "array"},
            {"name": "columns", "type": "array"},
            {"name": "row_count", "type": "integer"},
            {"name": "path", "type": "string"},
        ]
        return [
            {
                "action": "read",
                "description": "Read CSV rows from a workspace file.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    {"name": "path", "type": "string", "required": True},
                    {"name": "delimiter", "type": "string", "required": False},
                ],
                "output_fields": common_outputs,
                "side_effects": [],
            },
            {
                "action": "select",
                "description": "Select a subset of columns from CSV rows or a workspace file.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    *common_source_fields,
                    {"name": "columns", "type": "array", "required": True},
                ],
                "output_fields": common_outputs,
                "side_effects": [],
            },
            {
                "action": "filter",
                "description": "Filter CSV rows with equals, contains, gt, gte, lt, or lte.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    *common_source_fields,
                    {"name": "filters", "type": "array", "required": True},
                ],
                "output_fields": common_outputs,
                "side_effects": [],
            },
            {
                "action": "aggregate",
                "description": "Aggregate CSV rows with count, sum, avg, min, or max.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    *common_source_fields,
                    {"name": "aggregate", "type": "object", "required": True},
                ],
                "output_fields": common_outputs + [{"name": "result", "type": "object"}],
                "side_effects": [],
            },
            {
                "action": "report",
                "description": "Compute aggregate metrics and write a label-value report.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    *common_source_fields,
                    {"name": "metrics", "type": "array", "required": True},
                    {"name": "output_path", "type": "string", "required": True},
                    {"name": "overwrite", "type": "boolean", "required": False},
                ],
                "output_fields": common_outputs
                + [
                    {"name": "result", "type": "object"},
                    {"name": "output_path", "type": "string"},
                ],
                "side_effects": ["filesystem.write"],
            },
            {
                "action": "write",
                "description": "Write rows to a workspace CSV file.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    {"name": "rows", "type": "array", "required": True},
                    {"name": "columns", "type": "array", "required": False},
                    {"name": "output_path", "type": "string", "required": True},
                    {"name": "delimiter", "type": "string", "required": False},
                    {"name": "overwrite", "type": "boolean", "required": False},
                ],
                "output_fields": common_outputs + [{"name": "result", "type": "object"}],
                "side_effects": ["filesystem.write"],
            },
        ]

    def _habit_sqlite_contracts(self) -> list[dict[str, Any]]:
        common_query_fields = [
            {"name": "path", "type": "string", "required": True},
            {"name": "sql", "type": "string", "required": True},
            {"name": "params", "type": "array", "required": False},
            {"name": "max_rows", "type": "integer", "required": False},
        ]
        query_outputs = [
            {"name": "columns", "type": "array"},
            {"name": "rows", "type": "array"},
            {"name": "row_count", "type": "integer"},
            {"name": "truncated", "type": "boolean"},
            {"name": "path", "type": "string"},
        ]
        return [
            {
                "action": "list_tables",
                "description": "List user tables and views in a workspace SQLite database.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    {"name": "path", "type": "string", "required": True},
                ],
                "output_fields": [
                    {"name": "tables", "type": "array"},
                    {"name": "path", "type": "string"},
                ],
                "side_effects": [],
            },
            {
                "action": "schema",
                "description": "Read SQLite table or view schema metadata.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    {"name": "path", "type": "string", "required": True},
                    {"name": "table", "type": "string", "required": False},
                ],
                "output_fields": [
                    {"name": "result", "type": "object"},
                    {"name": "path", "type": "string"},
                ],
                "side_effects": [],
            },
            {
                "action": "query",
                "description": "Run a bounded read-only SELECT, WITH, or PRAGMA query.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    *common_query_fields,
                ],
                "output_fields": query_outputs,
                "side_effects": [],
            },
            {
                "action": "export_csv",
                "description": "Run a read-only query and write the result as CSV.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    *common_query_fields,
                    {"name": "output_path", "type": "string", "required": True},
                    {"name": "overwrite", "type": "boolean", "required": False},
                ],
                "output_fields": query_outputs + [{"name": "output_path", "type": "string"}],
                "side_effects": ["filesystem.write"],
            },
            {
                "action": "report",
                "description": "Run read-only scalar queries and write a label-value report.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    {"name": "path", "type": "string", "required": True},
                    {"name": "queries", "type": "array", "required": True},
                    {"name": "output_path", "type": "string", "required": True},
                    {"name": "overwrite", "type": "boolean", "required": False},
                ],
                "output_fields": [
                    {"name": "result", "type": "object"},
                    {"name": "path", "type": "string"},
                    {"name": "output_path", "type": "string"},
                ],
                "side_effects": ["filesystem.write"],
            },
        ]

    def _habit_config_contracts(self) -> list[dict[str, Any]]:
        return [
            {
                "action": "query",
                "description": "Query a JSON, TOML, simple YAML, XML, or key-value config by dot path.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    {"name": "query", "type": "string", "required": True},
                    {"name": "path", "type": "string", "required": False},
                    {"name": "text", "type": "string", "required": False},
                    {"name": "format", "type": "string", "required": False},
                ],
                "output_fields": [
                    {"name": "format", "type": "string"},
                    {"name": "value", "type": "any"},
                    {"name": "path", "type": "string"},
                ],
                "side_effects": [],
            },
            {
                "action": "report",
                "description": "Extract config values and write a label-value report.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    {"name": "fields", "type": "array", "required": True},
                    {"name": "output_path", "type": "string", "required": True},
                    {"name": "path", "type": "string", "required": False},
                    {"name": "text", "type": "string", "required": False},
                    {"name": "format", "type": "string", "required": False},
                    {"name": "overwrite", "type": "boolean", "required": False},
                ],
                "output_fields": [
                    {"name": "format", "type": "string"},
                    {"name": "result", "type": "object"},
                    {"name": "path", "type": "string"},
                    {"name": "output_path", "type": "string"},
                ],
                "side_effects": ["filesystem.write"],
            },
        ]

    def _habit_text_contracts(self) -> list[dict[str, Any]]:
        common_source_fields = [
            {"name": "path", "type": "string", "required": False},
            {"name": "text", "type": "string", "required": False},
        ]
        return [
            {
                "action": "stats",
                "description": "Count lines, words, characters, and non-empty lines in plain text.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    *common_source_fields,
                ],
                "output_fields": [
                    {"name": "stats", "type": "object"},
                    {"name": "path", "type": "string"},
                ],
                "side_effects": [],
            },
            {
                "action": "extract_lines",
                "description": "Extract matching lines by substring or line prefix.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    *common_source_fields,
                    {"name": "contains", "type": "string", "required": False},
                    {"name": "line_prefix", "type": "string", "required": False},
                    {"name": "ignore_case", "type": "boolean", "required": False},
                ],
                "output_fields": [
                    {"name": "lines", "type": "array"},
                    {"name": "matches", "type": "array"},
                    {"name": "path", "type": "string"},
                ],
                "side_effects": [],
            },
            {
                "action": "replace",
                "description": "Replace literal text in a workspace text file.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    {"name": "path", "type": "string", "required": True},
                    {"name": "old", "type": "string", "required": True},
                    {"name": "new", "type": "string", "required": True},
                    {"name": "count", "type": "integer", "required": False},
                    {"name": "output_path", "type": "string", "required": False},
                    {"name": "overwrite", "type": "boolean", "required": False},
                ],
                "output_fields": [
                    {"name": "path", "type": "string"},
                    {"name": "output_path", "type": "string"},
                    {"name": "replacement_count", "type": "integer"},
                ],
                "side_effects": ["filesystem.write"],
            },
            {
                "action": "report",
                "description": "Write plain-text stats and optional matching lines to a report.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    *common_source_fields,
                    {"name": "contains", "type": "string", "required": False},
                    {"name": "line_prefix", "type": "string", "required": False},
                    {"name": "output_path", "type": "string", "required": True},
                    {"name": "ignore_case", "type": "boolean", "required": False},
                    {"name": "overwrite", "type": "boolean", "required": False},
                ],
                "output_fields": [
                    {"name": "stats", "type": "object"},
                    {"name": "lines", "type": "array"},
                    {"name": "path", "type": "string"},
                    {"name": "output_path", "type": "string"},
                ],
                "side_effects": ["filesystem.write"],
            },
        ]

    def _habit_markdown_contracts(self) -> list[dict[str, Any]]:
        return [
            {
                "action": "headings",
                "description": "Extract Markdown ATX headings.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    {"name": "path", "type": "string", "required": False},
                    {"name": "text", "type": "string", "required": False},
                ],
                "output_fields": [
                    {"name": "headings", "type": "array"},
                    {"name": "path", "type": "string"},
                ],
                "side_effects": [],
            },
            {
                "action": "extract_section",
                "description": "Extract the section under a Markdown heading.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    {"name": "heading", "type": "string", "required": True},
                    {"name": "path", "type": "string", "required": False},
                    {"name": "text", "type": "string", "required": False},
                ],
                "output_fields": [
                    {"name": "section", "type": "string"},
                    {"name": "heading", "type": "object"},
                    {"name": "path", "type": "string"},
                ],
                "side_effects": [],
            },
            {
                "action": "replace_section",
                "description": "Replace an existing heading section.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    {"name": "path", "type": "string", "required": True},
                    {"name": "heading", "type": "string", "required": True},
                    {"name": "content", "type": "string", "required": True},
                    {"name": "output_path", "type": "string", "required": False},
                    {"name": "overwrite", "type": "boolean", "required": False},
                ],
                "output_fields": [
                    {"name": "path", "type": "string"},
                    {"name": "output_path", "type": "string"},
                ],
                "side_effects": ["filesystem.write"],
            },
            {
                "action": "append_section",
                "description": "Append a new Markdown section to a file.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    {"name": "path", "type": "string", "required": True},
                    {"name": "heading", "type": "string", "required": True},
                    {"name": "content", "type": "string", "required": True},
                ],
                "output_fields": [{"name": "path", "type": "string"}],
                "side_effects": ["filesystem.write"],
            },
            {
                "action": "table",
                "description": "Extract a simple Markdown pipe table.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    {"name": "path", "type": "string", "required": False},
                    {"name": "text", "type": "string", "required": False},
                    {"name": "table_index", "type": "integer", "required": False},
                ],
                "output_fields": [
                    {"name": "tables", "type": "array"},
                    {"name": "rows", "type": "array"},
                    {"name": "path", "type": "string"},
                ],
                "side_effects": [],
            },
        ]

    def _habit_html_contracts(self) -> list[dict[str, Any]]:
        common_input_fields = [
            {"name": "path", "type": "string", "required": False},
            {"name": "text", "type": "string", "required": False},
            {"name": "max_text_chars", "type": "integer", "required": False},
        ]
        common_outputs = [
            {"name": "title", "type": "string"},
            {"name": "text", "type": "string"},
            {"name": "links", "type": "array"},
            {"name": "meta", "type": "object"},
            {"name": "tables", "type": "array"},
            {"name": "path", "type": "string"},
        ]
        return [
            {
                "action": "extract",
                "description": "Extract title, readable text, links, metadata, and simple tables from HTML.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    *common_input_fields,
                ],
                "output_fields": common_outputs,
                "side_effects": [],
            },
            {
                "action": "links",
                "description": "Extract links from HTML text or a workspace HTML file.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    *common_input_fields,
                ],
                "output_fields": [
                    {"name": "links", "type": "array"},
                    {"name": "path", "type": "string"},
                ],
                "side_effects": [],
            },
            {
                "action": "report",
                "description": "Extract HTML evidence and write title, link_count, and text summary lines.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    *common_input_fields,
                    {"name": "output_path", "type": "string", "required": True},
                    {"name": "overwrite", "type": "boolean", "required": False},
                ],
                "output_fields": common_outputs + [{"name": "output_path", "type": "string"}],
                "side_effects": ["filesystem.write"],
            },
        ]

    def _habit_archive_contracts(self) -> list[dict[str, Any]]:
        return [
            {
                "action": "list",
                "description": "List files inside a workspace zip, tar, or tar.gz archive.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    {"name": "path", "type": "string", "required": True},
                    {"name": "format", "type": "string", "required": False},
                ],
                "output_fields": [
                    {"name": "entries", "type": "array"},
                    {"name": "format", "type": "string"},
                    {"name": "path", "type": "string"},
                ],
                "side_effects": [],
            },
            {
                "action": "extract",
                "description": "Extract an archive into a workspace directory with traversal protection.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    {"name": "path", "type": "string", "required": True},
                    {"name": "output_dir", "type": "string", "required": True},
                    {"name": "format", "type": "string", "required": False},
                    {"name": "overwrite", "type": "boolean", "required": False},
                ],
                "output_fields": [
                    {"name": "entries", "type": "array"},
                    {"name": "path", "type": "string"},
                    {"name": "output_dir", "type": "string"},
                    {"name": "format", "type": "string"},
                ],
                "side_effects": ["filesystem.write"],
            },
            {
                "action": "create",
                "description": "Create a zip, tar, or tar.gz archive from workspace files or directories.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    {"name": "paths", "type": "array", "required": True},
                    {"name": "output_path", "type": "string", "required": True},
                    {"name": "format", "type": "string", "required": False},
                    {"name": "overwrite", "type": "boolean", "required": False},
                ],
                "output_fields": [
                    {"name": "entries", "type": "array"},
                    {"name": "output_path", "type": "string"},
                    {"name": "format", "type": "string"},
                ],
                "side_effects": ["filesystem.read", "filesystem.write"],
            },
        ]

    def _habit_datetime_contracts(self) -> list[dict[str, Any]]:
        timestamp_outputs = [
            {"name": "value", "type": "string"},
            {"name": "iso", "type": "string"},
            {"name": "timestamp", "type": "number"},
            {"name": "timezone", "type": "string"},
        ]
        return [
            {
                "action": "now",
                "description": "Return current time in the requested timezone.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    {"name": "timezone", "type": "string", "required": False},
                ],
                "output_fields": timestamp_outputs,
                "side_effects": [],
            },
            {
                "action": "parse",
                "description": "Parse an ISO datetime, timestamp, or custom input format.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    {"name": "value", "type": "string", "required": True},
                    {"name": "timezone", "type": "string", "required": False},
                    {"name": "input_format", "type": "string", "required": False},
                ],
                "output_fields": timestamp_outputs,
                "side_effects": [],
            },
            {
                "action": "format",
                "description": "Format a datetime with strftime syntax.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    {"name": "value", "type": "string", "required": True},
                    {"name": "timezone", "type": "string", "required": False},
                    {"name": "input_format", "type": "string", "required": False},
                    {"name": "output_format", "type": "string", "required": True},
                ],
                "output_fields": timestamp_outputs,
                "side_effects": [],
            },
            {
                "action": "diff",
                "description": "Return the duration between start and end.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    {"name": "start", "type": "string", "required": True},
                    {"name": "end", "type": "string", "required": True},
                    {"name": "timezone", "type": "string", "required": False},
                    {"name": "input_format", "type": "string", "required": False},
                ],
                "output_fields": [
                    {"name": "diff", "type": "object"},
                    {"name": "timezone", "type": "string"},
                ],
                "side_effects": [],
            },
            {
                "action": "add",
                "description": "Add seconds, minutes, hours, days, or weeks to a datetime.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    {"name": "value", "type": "string", "required": False},
                    {"name": "timezone", "type": "string", "required": False},
                    {"name": "input_format", "type": "string", "required": False},
                    {"name": "amount", "type": "number", "required": True},
                    {"name": "unit", "type": "string", "required": True},
                ],
                "output_fields": timestamp_outputs,
                "side_effects": [],
            },
        ]

    def _habit_git_contracts(self) -> list[dict[str, Any]]:
        common_inputs = [
            {"name": "cwd", "type": "string", "required": False},
            {"name": "max_output_chars", "type": "integer", "required": False},
            {"name": "timeout", "type": "number", "required": False},
        ]
        common_outputs = [
            {"name": "argv", "type": "array"},
            {"name": "cwd", "type": "string"},
            {"name": "returncode", "type": "integer"},
            {"name": "stdout", "type": "string"},
            {"name": "stderr", "type": "string"},
            {"name": "stdout_truncated", "type": "boolean"},
            {"name": "stderr_truncated", "type": "boolean"},
        ]
        return [
            {
                "action": "status",
                "description": "Run read-only git status --short in a workspace repository.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    *common_inputs,
                    {"name": "paths", "type": "array", "required": False},
                ],
                "output_fields": common_outputs,
                "side_effects": ["process_execution"],
            },
            {
                "action": "diff",
                "description": "Run read-only git diff --no-ext-diff in a workspace repository.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    *common_inputs,
                    {"name": "paths", "type": "array", "required": False},
                    {"name": "stat", "type": "boolean", "required": False},
                ],
                "output_fields": common_outputs,
                "side_effects": ["process_execution"],
            },
            {
                "action": "log",
                "description": "Run bounded read-only git log --oneline.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    *common_inputs,
                    {"name": "max_count", "type": "integer", "required": False},
                ],
                "output_fields": common_outputs,
                "side_effects": ["process_execution"],
            },
            {
                "action": "show",
                "description": "Run read-only git show --stat --oneline for a revision.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    *common_inputs,
                    {"name": "rev", "type": "string", "required": False},
                ],
                "output_fields": common_outputs,
                "side_effects": ["process_execution"],
            },
        ]

    def _habit_http_contracts(self) -> list[dict[str, Any]]:
        common_inputs = [
            {"name": "url", "type": "string", "required": True},
            {"name": "headers", "type": "object", "required": False},
            {"name": "params", "type": "object", "required": False},
            {"name": "auth", "type": "object", "required": False},
            {"name": "timeout", "type": "number", "required": False},
            {"name": "max_response_chars", "type": "integer", "required": False},
        ]
        body_inputs = [
            {"name": "json", "type": "any", "required": False},
            {"name": "json_body", "type": "any", "required": False},
            {"name": "form", "type": "object", "required": False},
            {"name": "raw_body", "type": "string", "required": False},
            {"name": "body", "type": "string", "required": False},
        ]
        common_outputs = [
            {"name": "status", "type": "integer"},
            {"name": "headers", "type": "object"},
            {"name": "body_preview", "type": "string"},
            {"name": "body_truncated", "type": "boolean"},
            {"name": "json", "type": "any"},
        ]
        return [
            {
                "action": "get",
                "description": "Send an HTTP GET request.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    *common_inputs,
                ],
                "output_fields": common_outputs,
                "side_effects": ["network_read"],
            },
            {
                "action": "post",
                "description": "Send an HTTP POST request with optional JSON, form, or raw body.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    *common_inputs,
                    *body_inputs,
                ],
                "output_fields": common_outputs,
                "side_effects": ["network_read", "network_write"],
            },
            {
                "action": "put",
                "description": "Send an HTTP PUT request with optional JSON, form, or raw body.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    *common_inputs,
                    *body_inputs,
                ],
                "output_fields": common_outputs,
                "side_effects": ["network_read", "network_write"],
            },
            {
                "action": "delete",
                "description": "Send an HTTP DELETE request.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    *common_inputs,
                ],
                "output_fields": common_outputs,
                "side_effects": ["network_read", "network_write"],
            },
        ]

    def _habit_shell_contracts(self) -> list[dict[str, Any]]:
        return [
            {
                "action": "run",
                "description": "Run a whitelisted read-oriented shell command using argv only.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": True},
                    {"name": "argv", "type": "array", "required": True},
                    {"name": "cwd", "type": "string", "required": False},
                    {"name": "timeout", "type": "number", "required": False},
                    {"name": "max_output_chars", "type": "integer", "required": False},
                ],
                "output_fields": [
                    {"name": "argv", "type": "array"},
                    {"name": "cwd", "type": "string"},
                    {"name": "returncode", "type": "integer"},
                    {"name": "stdout_preview", "type": "string"},
                    {"name": "stderr_preview", "type": "string"},
                    {"name": "stdout_truncated", "type": "boolean"},
                    {"name": "stderr_truncated", "type": "boolean"},
                    {"name": "timed_out", "type": "boolean"},
                ],
                "side_effects": ["process_execution"],
            }
        ]

    def _habit_browser_contracts(self) -> list[dict[str, Any]]:
        return [
            {
                "action": "fetch",
                "description": "Fetch a URL or workspace file through the sandbox browser fetch path.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": False},
                    {"name": "url", "type": "string", "required": True},
                    {"name": "max_body_chars", "type": "integer", "required": False},
                ],
                "output_fields": [
                    {"name": "url", "type": "string"},
                    {"name": "request_url", "type": "string"},
                    {"name": "status", "type": "integer"},
                    {"name": "body_preview", "type": "string"},
                    {"name": "body_truncated", "type": "boolean"},
                    {"name": "source", "type": "string"},
                    {"name": "blocked", "type": "boolean"},
                ],
                "side_effects": ["network_read"],
            }
        ]

    def _habit_python_contracts(self) -> list[dict[str, Any]]:
        return [
            {
                "action": "run",
                "description": "Run Python code inside the workspace sandbox.",
                "input_fields": [
                    {"name": "action", "type": "string", "required": False},
                    {"name": "code", "type": "string", "required": True},
                ],
                "output_fields": [
                    {"name": "result", "type": "any"},
                    {"name": "stdout", "type": "string"},
                    {"name": "traceback", "type": "string"},
                ],
                "side_effects": ["python_execution"],
            }
        ]

    def _habit_file_main(self) -> str:
        return "\n".join(
            [
                "from __future__ import annotations",
                "",
                "import shutil",
                "",
                "",
                "def run(payload, context):",
                "    sandbox = context['sandbox']",
                "    action = str(payload.get('action', '')).strip().lower()",
                "    if not action:",
                "        return {",
                "            'success': True,",
                "            'status': 'completed',",
                "            'message': 'habit promoted file skill ready',",
                "            'output': {'supported_actions': ['list', 'read', 'write', 'append', 'replace_line', 'copy']},",
                "        }",
                "    if action == 'list':",
                "        path = str(payload.get('path', '.'))",
                "        return {",
                "            'success': True,",
                "            'status': 'completed',",
                "            'message': f'列出目录 {path} 成功',",
                "            'output': {'entries': sandbox.list_dir(path), 'path': path},",
                "        }",
                "    if action == 'read':",
                "        path = str(payload.get('path', ''))",
                "        if not path:",
                "            raise ValueError('file skill 需要 path')",
                "        return {",
                "            'success': True,",
                "            'status': 'completed',",
                "            'message': f'读取文件 {path} 成功',",
                "            'output': {'content': sandbox.read_text(path), 'path': path},",
                "        }",
                "    if action in {'write', 'append'}:",
                "        path = str(payload.get('path', ''))",
                "        if not path:",
                "            raise ValueError('file skill 需要 path')",
                "        content = str(payload.get('content', ''))",
                "        overwrite = bool(payload.get('overwrite', False))",
                "        if action == 'append':",
                "            try:",
                "                existing = sandbox.read_text(path)",
                "            except Exception:",
                "                existing = ''",
                "            content = existing + content",
                "            overwrite = True",
                "        written = sandbox.write_text(path, content, overwrite=overwrite)",
                "        return {",
                "            'success': True,",
                "            'status': 'completed',",
                "            'message': f'写入文件 {path} 成功',",
                "            'output': {'path': path, 'size': len(content), 'written_path': str(written)},",
                "        }",
                "    if action == 'replace_line':",
                "        path = str(payload.get('path', ''))",
                "        line_prefix = str(payload.get('line_prefix', ''))",
                "        content = str(payload.get('content', ''))",
                "        if not path:",
                "            raise ValueError('file.replace_line 需要 path')",
                "        if not line_prefix:",
                "            raise ValueError('file.replace_line 需要 line_prefix')",
                "        target_path = sandbox.resolve_path(path)",
                "        if not target_path.exists():",
                "            raise ValueError(f'文件不存在: {path}')",
                "        lines = target_path.read_text(encoding='utf-8').splitlines(keepends=True)",
                "        matched = [index for index, line in enumerate(lines) if line.startswith(line_prefix)]",
                "        if len(matched) != 1:",
                "            raise ValueError(f'line_prefix 匹配到 {len(matched)} 行，期望 1 行: {line_prefix}')",
                "        line_index = matched[0]",
                "        newline = '\\n' if lines[line_index].endswith('\\n') else ''",
                "        lines[line_index] = content + newline",
                "        updated = ''.join(lines)",
                "        written = sandbox.write_text(path, updated, overwrite=True)",
                "        return {",
                "            'success': True,",
                "            'status': 'completed',",
                "            'message': f'替换文件 {path} 中的目标行成功',",
                "            'output': {'path': path, 'line_index': line_index, 'size': len(updated), 'written_path': str(written)},",
                "        }",
                "    if action == 'copy':",
                "        source = str(payload.get('source') or payload.get('path') or '')",
                "        target = str(payload.get('target') or payload.get('destination') or payload.get('dest') or '')",
                "        if not source or not target:",
                "            raise ValueError('file.copy 需要 source 和 target')",
                "        source_path = sandbox.resolve_path(source)",
                "        target_path = sandbox.resolve_path(target)",
                "        if not source_path.exists():",
                "            raise ValueError(f'源文件不存在: {source}')",
                "        overwrite = bool(payload.get('overwrite', False))",
                "        if target_path.exists() and not overwrite:",
                "            raise ValueError(f'目标已存在，如需覆盖请设置 overwrite=true: {target}')",
                "        target_path.parent.mkdir(parents=True, exist_ok=True)",
                "        if source_path.is_dir():",
                "            if target_path.exists():",
                "                if target_path.is_dir():",
                "                    shutil.rmtree(target_path)",
                "                else:",
                "                    target_path.unlink()",
                "            shutil.copytree(source_path, target_path)",
                "            sync_tree = getattr(sandbox, '_sync_tree_to_host', None)",
                "            if callable(sync_tree):",
                "                sync_tree()",
                "        else:",
                "            if target_path.exists() and target_path.is_dir():",
                "                raise ValueError('file.copy target must be a file path when source is a file')",
                "            shutil.copy2(source_path, target_path)",
                "            sync_file = getattr(sandbox, '_sync_file_to_host', None)",
                "            if callable(sync_file):",
                "                sync_file(target_path)",
                "        return {",
                "            'success': True,",
                "            'status': 'completed',",
                "            'message': f'复制 {source} 到 {target} 成功',",
                "            'output': {'source': source, 'target': target, 'action': 'copy'},",
                "            'artifacts': [{'kind': 'file', 'path': str(target_path), 'description': 'habit promoted copy target'}],",
                "        }",
                "    raise ValueError(f'不支持的 file action: {action}')",
                "",
            ]
        )

    def _habit_json_main(self) -> str:
        return "\n".join(
            [
                "from __future__ import annotations",
                "",
                "import json",
                "",
                "",
                "def _read_json_text(payload, sandbox):",
                "    if payload.get('text') is not None:",
                "        text = payload['text']",
                "        if not isinstance(text, str):",
                "            raise ValueError('json text must be a string')",
                "        return text, None",
                "    path = str(payload.get('path', '')).strip()",
                "    if not path:",
                "        raise ValueError('json skill requires text or path')",
                "    return sandbox.read_text(path), path",
                "",
                "",
                "def _split_path(path):",
                "    if path == '':",
                "        return []",
                "    return [part for part in str(path).split('.') if part != '']",
                "",
                "",
                "def _get_path(value, path):",
                "    current = value",
                "    for part in _split_path(path):",
                "        if isinstance(current, dict):",
                "            if part not in current:",
                "                raise KeyError(part)",
                "            current = current[part]",
                "            continue",
                "        if isinstance(current, list):",
                "            current = current[int(part)]",
                "            continue",
                "        raise KeyError(part)",
                "    return current",
                "",
                "",
                "def _parse(payload, sandbox):",
                "    text, path = _read_json_text(payload, sandbox)",
                "    return json.loads(text), path",
                "",
                "",
                "def _parse_lines(payload, sandbox):",
                "    text, path = _read_json_text(payload, sandbox)",
                "    rows = []",
                "    errors = []",
                "    for line_number, line in enumerate(text.splitlines(), start=1):",
                "        if not line.strip():",
                "            continue",
                "        try:",
                "            rows.append(json.loads(line))",
                "        except json.JSONDecodeError as exc:",
                "            errors.append(f'line {line_number}: {exc}')",
                "    return rows, errors, path",
                "",
                "",
                "def _format_report_value(value):",
                "    if isinstance(value, str):",
                "        return value",
                "    if isinstance(value, (int, float, bool)) or value is None:",
                "        return str(value)",
                "    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))",
                "",
                "",
                "def _normalize_report_fields(fields):",
                "    if isinstance(fields, dict):",
                "        return [{'label': str(label), 'query': str(query)} for label, query in fields.items()]",
                "    if not isinstance(fields, list) or not fields:",
                "        raise ValueError('json.report requires non-empty fields')",
                "    normalized = []",
                "    for item in fields:",
                "        if isinstance(item, str):",
                "            normalized.append({'label': item, 'query': item})",
                "            continue",
                "        if not isinstance(item, dict):",
                "            raise ValueError('json.report fields must be strings or objects')",
                "        query = str(item.get('query') or item.get('path') or '').strip()",
                "        if not query:",
                "            raise ValueError('json.report field requires query')",
                "        label = str(item.get('label') or item.get('name') or query).strip()",
                "        normalized.append({'label': label, 'query': query})",
                "    return normalized",
                "",
                "",
                "def run(payload, context):",
                "    sandbox = context['sandbox']",
                "    action = str(payload.get('action', '')).strip().lower()",
                "    if not action:",
                "        return {",
                "            'success': True,",
                "            'status': 'completed',",
                "            'message': 'habit promoted JSON/JSONL data skill ready',",
                "            'output': {'supported_actions': ['validate', 'query', 'query_lines', 'report_lines', 'format', 'report']},",
                "        }",
                "    if action == 'query_lines':",
                "        query = payload.get('query')",
                "        if query is None:",
                "            raise ValueError('json.query_lines requires query')",
                "        rows, errors, path = _parse_lines(payload, sandbox)",
                "        values = [] if errors else [_get_path(row, str(query)) for row in rows]",
                "        return {",
                "            'success': True,",
                "            'status': 'completed',",
                "            'message': 'JSON lines query completed' if not errors else 'JSON lines query completed: invalid',",
                "            'output': {'valid': not errors, 'value': values, 'rows': rows, 'line_count': len(rows), 'formatted': None, 'errors': errors, 'path': path},",
                "        }",
                "    if action == 'report_lines':",
                "        output_path = str(payload.get('output_path') or payload.get('path_out') or '').strip()",
                "        if not output_path:",
                "            raise ValueError('json.report_lines requires output_path')",
                "        rows, errors, source_path = _parse_lines(payload, sandbox)",
                "        if errors:",
                "            return {",
                "                'success': True,",
                "                'status': 'completed',",
                "                'message': 'JSON lines report completed: invalid',",
                "                'output': {'valid': False, 'value': {}, 'formatted': None, 'line_count': len(rows), 'errors': errors, 'path': output_path, 'source_path': source_path},",
                "            }",
                "        fields = payload.get('fields')",
                "        if not fields:",
                "            query = str(payload.get('query') or '').strip()",
                "            if not query:",
                "                raise ValueError('json.report_lines requires fields or query')",
                "            fields = [{'label': str(payload.get('label') or query), 'query': query}]",
                "        values = {}",
                "        lines = []",
                "        for field in _normalize_report_fields(fields):",
                "            field_values = [_get_path(row, field['query']) for row in rows]",
                "            values[field['label']] = field_values",
                "            rendered = ', '.join(_format_report_value(value) for value in field_values)",
                "            lines.append(f\"{field['label']}: {rendered}\")",
                "        content = '\\n'.join(lines) + '\\n'",
                "        sandbox.write_text(output_path, content, overwrite=bool(payload.get('overwrite', True)))",
                "        return {",
                "            'success': True,",
                "            'status': 'completed',",
                "            'message': f'JSON lines report written: {output_path}',",
                "            'output': {'valid': True, 'value': values, 'formatted': content, 'line_count': len(rows), 'errors': [], 'path': output_path, 'source_path': source_path},",
                "        }",
                "    try:",
                "        data, path = _parse(payload, sandbox)",
                "    except json.JSONDecodeError as exc:",
                "        return {",
                "            'success': True,",
                "            'status': 'completed',",
                "            'message': 'JSON validation completed: invalid',",
                "            'output': {'valid': False, 'value': None, 'formatted': None, 'errors': [str(exc)], 'path': payload.get('path')},",
                "        }",
                "    if action == 'validate':",
                "        return {",
                "            'success': True,",
                "            'status': 'completed',",
                "            'message': 'JSON validation completed: valid',",
                "            'output': {'valid': True, 'value': None, 'formatted': None, 'errors': [], 'path': path},",
                "        }",
                "    if action == 'query':",
                "        query = payload.get('query')",
                "        if query is None:",
                "            raise ValueError('json.query requires query')",
                "        value = _get_path(data, str(query))",
                "        return {",
                "            'success': True,",
                "            'status': 'completed',",
                "            'message': 'JSON query completed',",
                "            'output': {'valid': True, 'value': value, 'formatted': None, 'errors': [], 'path': path},",
                "        }",
                "    if action == 'format':",
                "        options = payload.get('format_options') or {}",
                "        if not isinstance(options, dict):",
                "            raise ValueError('json.format format_options must be an object')",
                "        compact = bool(options.get('compact', False))",
                "        indent = None if compact else int(options.get('indent', 2))",
                "        formatted = json.dumps(",
                "            data,",
                "            ensure_ascii=bool(options.get('ensure_ascii', False)),",
                "            indent=indent,",
                "            sort_keys=bool(options.get('sort_keys', False)),",
                "            separators=(',', ':') if compact else None,",
                "        )",
                "        return {",
                "            'success': True,",
                "            'status': 'completed',",
                "            'message': 'JSON format completed',",
                "            'output': {'valid': True, 'value': data, 'formatted': formatted, 'errors': [], 'path': path},",
                "        }",
                "    if action == 'report':",
                "        output_path = str(payload.get('output_path') or payload.get('path_out') or '').strip()",
                "        if not output_path:",
                "            raise ValueError('json.report requires output_path')",
                "        values = {}",
                "        lines = []",
                "        for field in _normalize_report_fields(payload.get('fields')):",
                "            value = _get_path(data, field['query'])",
                "            values[field['label']] = value",
                "            lines.append(f\"{field['label']}: {_format_report_value(value)}\")",
                "        content = '\\n'.join(lines) + '\\n'",
                "        sandbox.write_text(output_path, content, overwrite=bool(payload.get('overwrite', True)))",
                "        return {",
                "            'success': True,",
                "            'status': 'completed',",
                "            'message': f'JSON report written: {output_path}',",
                "            'output': {'valid': True, 'value': values, 'formatted': content, 'errors': [], 'path': output_path},",
                "        }",
                "    raise ValueError(f'unsupported json action: {action}')",
                "",
            ]
        )

    def _habit_config_main(self) -> str:
        return "\n".join(
            [
                "from __future__ import annotations",
                "",
                "import json",
                "import tomllib",
                "import xml.etree.ElementTree as ET",
                "",
                "",
                "def _read_config_text(payload, sandbox):",
                "    if payload.get('text') is not None:",
                "        text = payload['text']",
                "        if not isinstance(text, str):",
                "            raise ValueError('config text must be a string')",
                "        fmt = str(payload.get('format') or '').strip().lower()",
                "        if not fmt:",
                "            raise ValueError('config text input requires format')",
                "        return text, None, _normalize_format(fmt)",
                "    path = str(payload.get('path') or '').strip()",
                "    if not path:",
                "        raise ValueError('config skill requires text or path')",
                "    fmt = str(payload.get('format') or '').strip().lower()",
                "    if not fmt:",
                "        suffix = path.rsplit('.', 1)[-1].lower() if '.' in path else ''",
                "        fmt = suffix",
                "    return sandbox.read_text(path), path, _normalize_format(fmt)",
                "",
                "",
                "def _normalize_format(fmt):",
                "    aliases = {",
                "        'yml': 'yaml',",
                "        'conf': 'keyvalue',",
                "        'cfg': 'keyvalue',",
                "        'env': 'keyvalue',",
                "        'dotenv': 'keyvalue',",
                "        'ini': 'keyvalue',",
                "        'properties': 'keyvalue',",
                "        '': 'keyvalue',",
                "    }",
                "    return aliases.get(str(fmt).lower(), str(fmt).lower())",
                "",
                "",
                "def _parse_scalar(value):",
                "    value = value.strip()",
                "    if value == '':",
                "        return ''",
                "    lowered = value.lower()",
                "    if lowered in {'true', 'false'}:",
                "        return lowered == 'true'",
                "    if lowered in {'null', '~'}:",
                "        return None",
                "    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'\"', \"'\"}:",
                "        return value[1:-1]",
                "    try:",
                "        return int(value)",
                "    except ValueError:",
                "        pass",
                "    try:",
                "        return float(value)",
                "    except ValueError:",
                "        return value",
                "",
                "",
                "def _parse_keyvalue(text):",
                "    root = {}",
                "    current = root",
                "    for raw in text.splitlines():",
                "        line = raw.split('#', 1)[0].strip()",
                "        if not line:",
                "            continue",
                "        if line.startswith('[') and line.endswith(']'):",
                "            section = line[1:-1].strip()",
                "            current = root",
                "            for part in [item for item in section.split('.') if item]:",
                "                current = current.setdefault(part, {})",
                "            continue",
                "        separator = '=' if '=' in line else ':' if ':' in line else None",
                "        if separator is None:",
                "            raise ValueError('config key-value parser requires key=value or key: value lines')",
                "        key, value = line.split(separator, 1)",
                "        key = key.strip()",
                "        if not key:",
                "            raise ValueError('config key-value parser requires non-empty key')",
                "        current[key] = _parse_scalar(value)",
                "    return root",
                "",
                "",
                "def _parse_simple_yaml(text):",
                "    root = {}",
                "    stack = [(-1, root)]",
                "    for raw in text.splitlines():",
                "        stripped = raw.split('#', 1)[0].rstrip()",
                "        if not stripped.strip():",
                "            continue",
                "        indent = len(stripped) - len(stripped.lstrip(' '))",
                "        if ':' not in stripped:",
                "            raise ValueError('simple YAML parser only supports mapping lines')",
                "        key, value = stripped.strip().split(':', 1)",
                "        key = key.strip()",
                "        value = value.strip()",
                "        while stack and indent <= stack[-1][0]:",
                "            stack.pop()",
                "        parent = stack[-1][1]",
                "        if value == '':",
                "            child = {}",
                "            parent[key] = child",
                "            stack.append((indent, child))",
                "        else:",
                "            parent[key] = _parse_scalar(value)",
                "    return root",
                "",
                "",
                "def _parse_xml(text):",
                "    return ET.fromstring(text)",
                "",
                "",
                "def _parse(payload, sandbox):",
                "    text, path, fmt = _read_config_text(payload, sandbox)",
                "    if fmt == 'json':",
                "        return json.loads(text), path, fmt",
                "    if fmt == 'toml':",
                "        return tomllib.loads(text), path, fmt",
                "    if fmt == 'yaml':",
                "        return _parse_simple_yaml(text), path, fmt",
                "    if fmt == 'keyvalue':",
                "        return _parse_keyvalue(text), path, fmt",
                "    if fmt == 'xml':",
                "        return _parse_xml(text), path, fmt",
                "    raise ValueError(f'unsupported config format: {fmt}')",
                "",
                "",
                "def _split_path(path):",
                "    return [part for part in str(path).split('.') if part != '']",
                "",
                "",
                "def _get_path(value, path):",
                "    if isinstance(value, dict) and str(path) in value:",
                "        return value[str(path)]",
                "    current = value",
                "    for part in _split_path(path):",
                "        if isinstance(current, dict):",
                "            if part not in current:",
                "                raise KeyError(part)",
                "            current = current[part]",
                "            continue",
                "        if isinstance(current, list):",
                "            current = current[int(part)]",
                "            continue",
                "        raise KeyError(part)",
                "    return current",
                "",
                "",
                "def _get_xml_path(root, path):",
                "    parts = _split_path(path)",
                "    current = root",
                "    if parts and parts[0] == root.tag:",
                "        parts = parts[1:]",
                "    for part in parts:",
                "        if part == 'text':",
                "            return (current.text or '').strip()",
                "        if part.startswith('@'):",
                "            return current.attrib.get(part[1:])",
                "        matches = current.findall(part)",
                "        if not matches:",
                "            raise KeyError(part)",
                "        current = matches[0]",
                "    return {'tag': current.tag, 'text': (current.text or '').strip(), 'attributes': dict(current.attrib)}",
                "",
                "",
                "def _query_value(data, fmt, query):",
                "    if fmt == 'xml':",
                "        return _get_xml_path(data, query)",
                "    return _get_path(data, query)",
                "",
                "",
                "def _format_value(value):",
                "    if isinstance(value, str):",
                "        return value",
                "    if isinstance(value, (int, float, bool)) or value is None:",
                "        return str(value)",
                "    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))",
                "",
                "",
                "def _normalize_fields(fields):",
                "    if isinstance(fields, dict):",
                "        return [{'label': str(label), 'query': str(query)} for label, query in fields.items()]",
                "    if not isinstance(fields, list) or not fields:",
                "        raise ValueError('config.report requires non-empty fields')",
                "    normalized = []",
                "    for item in fields:",
                "        if isinstance(item, str):",
                "            normalized.append({'label': item, 'query': item})",
                "            continue",
                "        if not isinstance(item, dict):",
                "            raise ValueError('config.report fields must be strings or objects')",
                "        query = str(item.get('query') or item.get('path') or '').strip()",
                "        if not query:",
                "            raise ValueError('config.report field requires query')",
                "        label = str(item.get('label') or item.get('name') or query).strip()",
                "        normalized.append({'label': label, 'query': query})",
                "    return normalized",
                "",
                "",
                "def run(payload, context):",
                "    sandbox = context['sandbox']",
                "    action = str(payload.get('action', '')).strip().lower()",
                "    if not action:",
                "        return {",
                "            'success': True,",
                "            'status': 'completed',",
                "            'message': 'habit promoted config data skill ready',",
                "            'output': {'supported_actions': ['query', 'report']},",
                "        }",
                "    data, path, fmt = _parse(payload, sandbox)",
                "    if action == 'query':",
                "        query = str(payload.get('query') or '').strip()",
                "        if not query:",
                "            raise ValueError('config.query requires query')",
                "        value = _query_value(data, fmt, query)",
                "        return {",
                "            'success': True,",
                "            'status': 'completed',",
                "            'message': 'Config query completed',",
                "            'output': {'format': fmt, 'value': value, 'path': path},",
                "        }",
                "    if action == 'report':",
                "        output_path = str(payload.get('output_path') or '').strip()",
                "        if not output_path:",
                "            raise ValueError('config.report requires output_path')",
                "        values = {}",
                "        lines = []",
                "        for field in _normalize_fields(payload.get('fields')):",
                "            value = _query_value(data, fmt, field['query'])",
                "            values[field['label']] = value",
                "            lines.append(f\"{field['label']}: {_format_value(value)}\")",
                "        content = '\\n'.join(lines) + '\\n'",
                "        sandbox.write_text(output_path, content, overwrite=bool(payload.get('overwrite', True)))",
                "        return {",
                "            'success': True,",
                "            'status': 'completed',",
                "            'message': f'Config report completed: {output_path}',",
                "            'output': {'format': fmt, 'result': values, 'path': path, 'output_path': output_path},",
                "        }",
                "    raise ValueError(f'unsupported config action: {action}')",
                "",
            ]
        )

    def _habit_sqlite_main(self) -> str:
        return "\n".join(
            [
                "from __future__ import annotations",
                "",
                "import csv",
                "import io",
                "import sqlite3",
                "from urllib.parse import quote",
                "",
                "",
                "def _ok(message, output, artifacts=None):",
                "    result = {'success': True, 'status': 'completed', 'message': message, 'output': output}",
                "    if artifacts:",
                "        result['artifacts'] = artifacts",
                "    return result",
                "",
                "",
                "def _fail(message, output=None, error_code='validation_error'):",
                "    return {",
                "        'success': False,",
                "        'status': 'failed',",
                "        'message': message,",
                "        'output': output or {},",
                "        'error_code': error_code,",
                "    }",
                "",
                "",
                "def _db_path(payload, sandbox):",
                "    path = str(payload.get('path') or '').strip()",
                "    if not path:",
                "        raise ValueError('sqlite action requires path')",
                "    target = sandbox.resolve_path(path)",
                "    if not target.exists():",
                "        raise ValueError(f'sqlite database does not exist: {path}')",
                "    if target.is_dir():",
                "        raise ValueError(f'sqlite path is a directory: {path}')",
                "    return target, path",
                "",
                "",
                "def _connect(payload, sandbox):",
                "    target, raw_path = _db_path(payload, sandbox)",
                "    uri_path = quote(target.as_posix(), safe='/:')",
                "    conn = sqlite3.connect(f'file:{uri_path}?mode=ro', uri=True)",
                "    conn.row_factory = sqlite3.Row",
                "    conn.execute('PRAGMA query_only = ON')",
                "    return conn, raw_path",
                "",
                "",
                "def _validate_readonly_sql(sql):",
                "    if not isinstance(sql, str) or not sql.strip():",
                "        raise ValueError('sqlite query requires sql')",
                "    cleaned = sql.strip().rstrip(';').strip()",
                "    lowered = cleaned.lower()",
                "    first = lowered.split(None, 1)[0] if lowered.split(None, 1) else ''",
                "    if first not in {'select', 'with', 'pragma'}:",
                "        raise ValueError('sqlite skill only allows read-only SELECT, WITH, or PRAGMA queries')",
                "    blocked = (",
                "        ' insert ',",
                "        ' update ',",
                "        ' delete ',",
                "        ' create ',",
                "        ' drop ',",
                "        ' alter ',",
                "        ' attach ',",
                "        ' detach ',",
                "        ' replace ',",
                "        ' vacuum ',",
                "    )",
                "    padded = f' {lowered} '",
                "    if any(token in padded for token in blocked):",
                "        raise ValueError('sqlite query contains a write or schema-changing statement')",
                "    return cleaned",
                "",
                "",
                "def _params(payload):",
                "    params = payload.get('params', [])",
                "    if params is None:",
                "        return []",
                "    if not isinstance(params, list):",
                "        raise ValueError('sqlite params must be an array')",
                "    return params",
                "",
                "",
                "def _max_rows(payload):",
                "    value = payload.get('max_rows', 100)",
                "    if not isinstance(value, int) or isinstance(value, bool) or value < 1:",
                "        raise ValueError('sqlite max_rows must be a positive integer')",
                "    return min(value, 1000)",
                "",
                "",
                "def _run_query(conn, sql, params, max_rows):",
                "    cursor = conn.execute(sql, params)",
                "    rows = cursor.fetchmany(max_rows + 1)",
                "    truncated = len(rows) > max_rows",
                "    rows = rows[:max_rows]",
                "    columns = [item[0] for item in (cursor.description or [])]",
                "    return columns, [dict(row) for row in rows], truncated",
                "",
                "",
                "def _list_tables(payload, sandbox):",
                "    conn, raw_path = _connect(payload, sandbox)",
                "    try:",
                "        rows = conn.execute(",
                '            """',
                "            SELECT name, type",
                "            FROM sqlite_master",
                "            WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%'",
                "            ORDER BY type, name",
                '            """',
                "        ).fetchall()",
                "    finally:",
                "        conn.close()",
                "    return _ok('SQLite tables listed', {'tables': [dict(row) for row in rows], 'path': raw_path})",
                "",
                "",
                "def _schema(payload, sandbox):",
                "    conn, raw_path = _connect(payload, sandbox)",
                "    table = str(payload.get('table') or '').strip()",
                "    try:",
                "        if table:",
                "            rows = conn.execute(",
                '                """',
                "                SELECT name, type, sql",
                "                FROM sqlite_master",
                "                WHERE type IN ('table', 'view') AND name = ?",
                "                ORDER BY type, name",
                '                """,',
                "                [table],",
                "            ).fetchall()",
                "        else:",
                "            rows = conn.execute(",
                '                """',
                "                SELECT name, type, sql",
                "                FROM sqlite_master",
                "                WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%'",
                "                ORDER BY type, name",
                '                """',
                "            ).fetchall()",
                "    finally:",
                "        conn.close()",
                "    return _ok('SQLite schema loaded', {'result': {'objects': [dict(row) for row in rows]}, 'path': raw_path})",
                "",
                "",
                "def _query(payload, sandbox):",
                "    sql = _validate_readonly_sql(payload.get('sql'))",
                "    conn, raw_path = _connect(payload, sandbox)",
                "    try:",
                "        columns, rows, truncated = _run_query(conn, sql, _params(payload), _max_rows(payload))",
                "    finally:",
                "        conn.close()",
                "    return _ok(",
                "        'SQLite query completed',",
                "        {",
                "            'columns': columns,",
                "            'rows': rows,",
                "            'row_count': len(rows),",
                "            'truncated': truncated,",
                "            'path': raw_path,",
                "        },",
                "    )",
                "",
                "",
                "def _export_csv(payload, sandbox):",
                "    output_path = str(payload.get('output_path') or '').strip()",
                "    if not output_path:",
                "        raise ValueError('sqlite.export_csv requires output_path')",
                "    sql = _validate_readonly_sql(payload.get('sql'))",
                "    conn, raw_path = _connect(payload, sandbox)",
                "    try:",
                "        columns, rows, truncated = _run_query(conn, sql, _params(payload), _max_rows(payload))",
                "    finally:",
                "        conn.close()",
                "    buffer = io.StringIO()",
                "    writer = csv.DictWriter(buffer, fieldnames=columns)",
                "    writer.writeheader()",
                "    for row in rows:",
                "        writer.writerow({column: row.get(column, '') for column in columns})",
                "    written = sandbox.write_text(",
                "        output_path,",
                "        buffer.getvalue(),",
                "        overwrite=bool(payload.get('overwrite', True)),",
                "    )",
                "    return _ok(",
                "        'SQLite CSV export completed',",
                "        {",
                "            'columns': columns,",
                "            'row_count': len(rows),",
                "            'truncated': truncated,",
                "            'path': raw_path,",
                "            'output_path': output_path,",
                "        },",
                "        artifacts=[{'kind': 'file', 'path': str(written), 'description': 'SQLite CSV export'}],",
                "    )",
                "",
                "",
                "def _report(payload, sandbox):",
                "    output_path = str(payload.get('output_path') or '').strip()",
                "    queries = payload.get('queries')",
                "    if not output_path:",
                "        raise ValueError('sqlite.report requires output_path')",
                "    if not isinstance(queries, list) or not queries:",
                "        raise ValueError('sqlite.report requires non-empty queries array')",
                "    conn, raw_path = _connect(payload, sandbox)",
                "    result = {}",
                "    lines = []",
                "    try:",
                "        for item in queries:",
                "            if not isinstance(item, dict):",
                "                raise ValueError('sqlite.report queries must be objects')",
                "            label = str(item.get('label') or item.get('name') or '').strip()",
                "            if not label:",
                "                raise ValueError('sqlite.report query requires label')",
                "            sql = _validate_readonly_sql(item.get('sql'))",
                "            params = item.get('params') if isinstance(item.get('params'), list) else []",
                "            columns, rows, _ = _run_query(conn, sql, params, 1)",
                "            value = None",
                "            if rows:",
                "                column = str(item.get('column') or (columns[0] if columns else ''))",
                "                value = rows[0].get(column)",
                "            result[label] = value",
                "            lines.append(f'{label}: {value}')",
                "    finally:",
                "        conn.close()",
                "    written = sandbox.write_text(",
                "        output_path,",
                "        '\\n'.join(lines) + '\\n',",
                "        overwrite=bool(payload.get('overwrite', True)),",
                "    )",
                "    return _ok(",
                "        'SQLite report completed',",
                "        {'result': result, 'path': raw_path, 'output_path': output_path},",
                "        artifacts=[{'kind': 'file', 'path': str(written), 'description': 'SQLite report'}],",
                "    )",
                "",
                "",
                "def run(payload, context):",
                "    sandbox = context['sandbox']",
                "    try:",
                "        action = str(payload.get('action') or 'query').strip().lower()",
                "        if not action:",
                "            return _ok(",
                "                'habit promoted SQLite data skill ready',",
                "                {'supported_actions': ['list_tables', 'schema', 'query', 'export_csv', 'report']},",
                "            )",
                "        if action == 'list_tables':",
                "            return _list_tables(payload, sandbox)",
                "        if action == 'schema':",
                "            return _schema(payload, sandbox)",
                "        if action == 'query':",
                "            return _query(payload, sandbox)",
                "        if action == 'export_csv':",
                "            return _export_csv(payload, sandbox)",
                "        if action == 'report':",
                "            return _report(payload, sandbox)",
                "        return _fail(f'unsupported sqlite action: {action}')",
                "    except (sqlite3.Error, ValueError) as exc:",
                "        return _fail(str(exc))",
                "",
            ]
        )

    def _habit_text_main(self) -> str:
        return "\n".join(
            [
                "from __future__ import annotations",
                "",
                "import re",
                "",
                "",
                "def _read_text(payload, sandbox):",
                "    if payload.get('text') is not None:",
                "        text = payload['text']",
                "        if not isinstance(text, str):",
                "            raise ValueError('text skill requires string text')",
                "        return text, None",
                "    path = str(payload.get('path') or '').strip()",
                "    if not path:",
                "        raise ValueError('text skill requires text or path')",
                "    return sandbox.read_text(path), path",
                "",
                "",
                "def _stats(text):",
                "    lines = text.splitlines()",
                "    return {",
                "        'line_count': len(lines),",
                "        'non_empty_line_count': sum(1 for line in lines if line.strip()),",
                "        'word_count': len(re.findall(r'\\S+', text)),",
                "        'char_count': len(text),",
                "    }",
                "",
                "",
                "def _matching_lines(text, payload):",
                "    contains = str(payload.get('contains') or '')",
                "    line_prefix = str(payload.get('line_prefix') or '')",
                "    ignore_case = bool(payload.get('ignore_case', False))",
                "    contains_cmp = contains.lower() if ignore_case else contains",
                "    prefix_cmp = line_prefix.lower() if ignore_case else line_prefix",
                "    matches = []",
                "    for index, line in enumerate(text.splitlines(), start=1):",
                "        compare = line.lower() if ignore_case else line",
                "        if contains and contains_cmp not in compare:",
                "            continue",
                "        if line_prefix and not compare.startswith(prefix_cmp):",
                "            continue",
                "        if contains or line_prefix:",
                "            matches.append({'line_number': index, 'text': line})",
                "    return matches",
                "",
                "",
                "def _write_report(stats, matches):",
                "    lines = [",
                "        f\"lines: {stats['line_count']}\",",
                "        f\"non_empty_lines: {stats['non_empty_line_count']}\",",
                "        f\"words: {stats['word_count']}\",",
                "        f\"characters: {stats['char_count']}\",",
                "    ]",
                "    if matches:",
                "        lines.append('matches:')",
                "        for item in matches:",
                "            lines.append(f\"- {item['line_number']}: {item['text']}\")",
                "    return '\\n'.join(lines) + '\\n'",
                "",
                "",
                "def run(payload, context):",
                "    sandbox = context['sandbox']",
                "    action = str(payload.get('action', '')).strip().lower()",
                "    if not action:",
                "        return {",
                "            'success': True,",
                "            'status': 'completed',",
                "            'message': 'habit promoted text skill ready',",
                "            'output': {'supported_actions': ['stats', 'extract_lines', 'replace', 'report']},",
                "        }",
                "    if action == 'stats':",
                "        text, path = _read_text(payload, sandbox)",
                "        return {",
                "            'success': True,",
                "            'status': 'completed',",
                "            'message': 'Text stats computed',",
                "            'output': {'stats': _stats(text), 'path': path},",
                "        }",
                "    if action == 'extract_lines':",
                "        text, path = _read_text(payload, sandbox)",
                "        matches = _matching_lines(text, payload)",
                "        return {",
                "            'success': True,",
                "            'status': 'completed',",
                "            'message': 'Text lines extracted',",
                "            'output': {'lines': [item['text'] for item in matches], 'matches': matches, 'path': path},",
                "        }",
                "    if action == 'replace':",
                "        path = str(payload.get('path') or '').strip()",
                "        old = payload.get('old')",
                "        new = payload.get('new')",
                "        if not path or not isinstance(old, str) or old == '' or not isinstance(new, str):",
                "            raise ValueError('text.replace requires path, non-empty old, and string new')",
                "        text = sandbox.read_text(path)",
                "        count = payload.get('count', -1)",
                "        if not isinstance(count, int) or isinstance(count, bool):",
                "            count = -1",
                "        replacement_count = text.count(old) if count < 0 else min(text.count(old), count)",
                "        updated = text.replace(old, new, count)",
                "        output_path = str(payload.get('output_path') or path)",
                "        sandbox.write_text(output_path, updated, overwrite=True if output_path == path else bool(payload.get('overwrite', False)))",
                "        return {",
                "            'success': True,",
                "            'status': 'completed',",
                "            'message': f'Text replaced: {output_path}',",
                "            'output': {'path': path, 'output_path': output_path, 'replacement_count': replacement_count},",
                "        }",
                "    if action == 'report':",
                "        output_path = str(payload.get('output_path') or '').strip()",
                "        if not output_path:",
                "            raise ValueError('text.report requires output_path')",
                "        text, path = _read_text(payload, sandbox)",
                "        stats = _stats(text)",
                "        matches = _matching_lines(text, payload)",
                "        report = _write_report(stats, matches)",
                "        sandbox.write_text(output_path, report, overwrite=bool(payload.get('overwrite', False)))",
                "        return {",
                "            'success': True,",
                "            'status': 'completed',",
                "            'message': f'Text report written: {output_path}',",
                "            'output': {'stats': stats, 'lines': [item['text'] for item in matches], 'path': path, 'output_path': output_path},",
                "        }",
                "    raise ValueError(f'unsupported text action: {action}')",
                "",
            ]
        )

    def _habit_markdown_main(self) -> str:
        return "\n".join(
            [
                "from __future__ import annotations",
                "",
                "import re",
                "",
                "_HEADING_RE = re.compile(r'^(#{1,6})\\s+(.+?)\\s*#*\\s*$')",
                "",
                "",
                "def _read_text(payload, sandbox):",
                "    if payload.get('text') is not None:",
                "        text = payload['text']",
                "        if not isinstance(text, str):",
                "            raise ValueError('markdown text must be a string')",
                "        return text, None",
                "    path = str(payload.get('path') or '').strip()",
                "    if not path:",
                "        raise ValueError('markdown skill requires text or path')",
                "    return sandbox.read_text(path), path",
                "",
                "",
                "def _normalize_heading(value):",
                "    match = _HEADING_RE.match(str(value).strip())",
                "    if match:",
                "        return match.group(2).strip().lower()",
                "    return str(value).strip().strip('#').strip().lower()",
                "",
                "",
                "def _heading_line(heading):",
                "    heading = str(heading).strip()",
                "    if heading.startswith('#'):",
                "        return heading",
                "    return f'## {heading}'",
                "",
                "",
                "def _heading_positions(lines):",
                "    positions = []",
                "    for index, line in enumerate(lines):",
                "        match = _HEADING_RE.match(line.rstrip('\\n'))",
                "        if not match:",
                "            continue",
                "        positions.append({'level': len(match.group(1)), 'title': match.group(2).strip(), 'line': index})",
                "    return positions",
                "",
                "",
                "def _find_section(lines, heading):",
                "    target = _normalize_heading(heading)",
                "    positions = _heading_positions(lines)",
                "    for index, item in enumerate(positions):",
                "        if _normalize_heading(item['title']) != target:",
                "            continue",
                "        start = int(item['line'])",
                "        level = int(item['level'])",
                "        end = len(lines)",
                "        for later in positions[index + 1:]:",
                "            if int(later['level']) <= level:",
                "                end = int(later['line'])",
                "                break",
                "        return start, end, item",
                "    raise ValueError(f'markdown heading not found: {heading}')",
                "",
                "",
                "def _is_table_separator(line):",
                "    cells = [cell.strip() for cell in line.strip().strip('|').split('|')]",
                "    return bool(cells) and all(re.fullmatch(r':?-{3,}:?', cell or '') for cell in cells)",
                "",
                "",
                "def _parse_table_block(lines):",
                "    headers = [cell.strip() for cell in lines[0].strip().strip('|').split('|')]",
                "    rows = []",
                "    for line in lines[2:]:",
                "        values = [cell.strip() for cell in line.strip().strip('|').split('|')]",
                "        if len(values) != len(headers):",
                "            continue",
                "        rows.append(dict(zip(headers, values, strict=False)))",
                "    return {'columns': headers, 'rows': rows}",
                "",
                "",
                "def _tables_from_text(text):",
                "    lines = text.splitlines()",
                "    tables = []",
                "    index = 0",
                "    while index + 1 < len(lines):",
                "        if '|' not in lines[index] or not _is_table_separator(lines[index + 1]):",
                "            index += 1",
                "            continue",
                "        block = [lines[index], lines[index + 1]]",
                "        index += 2",
                "        while index < len(lines) and '|' in lines[index] and lines[index].strip():",
                "            block.append(lines[index])",
                "            index += 1",
                "        tables.append(_parse_table_block(block))",
                "    return tables",
                "",
                "",
                "def run(payload, context):",
                "    sandbox = context['sandbox']",
                "    action = str(payload.get('action', '')).strip().lower()",
                "    if not action:",
                "        return {",
                "            'success': True,",
                "            'status': 'completed',",
                "            'message': 'habit promoted Markdown skill ready',",
                "            'output': {'supported_actions': ['headings', 'extract_section', 'replace_section', 'append_section', 'table']},",
                "        }",
                "    if action == 'headings':",
                "        text, path = _read_text(payload, sandbox)",
                "        return {",
                "            'success': True,",
                "            'status': 'completed',",
                "            'message': 'Markdown headings extracted',",
                "            'output': {'headings': _heading_positions(text.splitlines()), 'path': path},",
                "        }",
                "    if action == 'extract_section':",
                "        text, path = _read_text(payload, sandbox)",
                "        heading = str(payload.get('heading') or '').strip()",
                "        if not heading:",
                "            raise ValueError('markdown.extract_section requires heading')",
                "        lines = text.splitlines(keepends=True)",
                "        start, end, item = _find_section(lines, heading)",
                "        section = ''.join(lines[start + 1:end]).strip('\\n')",
                "        return {",
                "            'success': True,",
                "            'status': 'completed',",
                "            'message': 'Markdown section extracted',",
                "            'output': {'section': section, 'heading': item, 'path': path},",
                "        }",
                "    if action == 'replace_section':",
                "        path = str(payload.get('path') or '').strip()",
                "        heading = str(payload.get('heading') or '').strip()",
                "        content = payload.get('content')",
                "        if not path or not heading or not isinstance(content, str):",
                "            raise ValueError('markdown.replace_section requires path, heading, and string content')",
                "        text = sandbox.read_text(path)",
                "        lines = text.splitlines(keepends=True)",
                "        start, end, _ = _find_section(lines, heading)",
                "        replacement = [lines[start]]",
                "        if content:",
                "            replacement.extend([line + '\\n' for line in content.rstrip('\\n').split('\\n')])",
                "        updated = ''.join(lines[:start + 1] + replacement[1:] + lines[end:])",
                "        output_path = str(payload.get('output_path') or path)",
                "        sandbox.write_text(output_path, updated, overwrite=True if output_path == path else bool(payload.get('overwrite', False)))",
                "        return {",
                "            'success': True,",
                "            'status': 'completed',",
                "            'message': f'Markdown section replaced: {output_path}',",
                "            'output': {'path': path, 'output_path': output_path},",
                "        }",
                "    if action == 'append_section':",
                "        path = str(payload.get('path') or '').strip()",
                "        heading = str(payload.get('heading') or '').strip()",
                "        content = payload.get('content')",
                "        if not path or not heading or not isinstance(content, str):",
                "            raise ValueError('markdown.append_section requires path, heading, and string content')",
                "        original = sandbox.read_text(path)",
                "        separator = '\\n\\n' if original and not original.endswith('\\n\\n') else ''",
                "        updated = original + separator + _heading_line(heading) + '\\n' + content.rstrip('\\n') + '\\n'",
                "        sandbox.write_text(path, updated, overwrite=True)",
                "        return {",
                "            'success': True,",
                "            'status': 'completed',",
                "            'message': f'Markdown section appended: {path}',",
                "            'output': {'path': path},",
                "        }",
                "    if action == 'table':",
                "        text, path = _read_text(payload, sandbox)",
                "        table_index = payload.get('table_index', 0)",
                "        if not isinstance(table_index, int) or isinstance(table_index, bool):",
                "            raise ValueError('markdown.table table_index must be an integer')",
                "        tables = _tables_from_text(text)",
                "        rows = tables[table_index]['rows'] if 0 <= table_index < len(tables) else []",
                "        return {",
                "            'success': True,",
                "            'status': 'completed',",
                "            'message': 'Markdown tables extracted',",
                "            'output': {'tables': tables, 'rows': rows, 'path': path},",
                "        }",
                "    raise ValueError(f'unsupported markdown action: {action}')",
                "",
            ]
        )

    def _habit_html_main(self) -> str:
        return "\n".join(
            [
                "from __future__ import annotations",
                "",
                "import re",
                "from html.parser import HTMLParser",
                "",
                "",
                "def _ok(message, output, artifacts=None):",
                "    result = {'success': True, 'status': 'completed', 'message': message, 'output': output}",
                "    if artifacts:",
                "        result['artifacts'] = artifacts",
                "    return result",
                "",
                "",
                "def _fail(message, error_code='validation_error', output=None):",
                "    return {",
                "        'success': False,",
                "        'status': 'failed',",
                "        'message': message,",
                "        'error_code': error_code,",
                "        'output': output or {},",
                "    }",
                "",
                "",
                "def _collapse_ws(value):",
                "    return re.sub(r'\\s+', ' ', value).strip()",
                "",
                "",
                "class _HTMLExtractor(HTMLParser):",
                "    def __init__(self):",
                "        super().__init__(convert_charrefs=True)",
                "        self.title_parts = []",
                "        self.text_parts = []",
                "        self.links = []",
                "        self.meta = {}",
                "        self.tables = []",
                "        self._skip_depth = 0",
                "        self._tag_stack = []",
                "        self._current_link = None",
                "        self._current_link_text = []",
                "        self._current_table = None",
                "        self._current_row = None",
                "        self._current_cell = None",
                "",
                "    def handle_starttag(self, tag, attrs):",
                "        tag = tag.lower()",
                "        attrs_dict = {key.lower(): value or '' for key, value in attrs}",
                "        self._tag_stack.append(tag)",
                "        if tag in {'script', 'style', 'noscript'}:",
                "            self._skip_depth += 1",
                "            return",
                "        if tag == 'a':",
                "            self._current_link = {'href': attrs_dict.get('href', ''), 'text': ''}",
                "            self._current_link_text = []",
                "        if tag == 'meta':",
                "            key = attrs_dict.get('name') or attrs_dict.get('property')",
                "            content = attrs_dict.get('content')",
                "            if key and content:",
                "                self.meta[key] = content",
                "        if tag == 'table':",
                "            self._current_table = []",
                "        if tag == 'tr' and self._current_table is not None:",
                "            self._current_row = []",
                "        if tag in {'td', 'th'} and self._current_row is not None:",
                "            self._current_cell = []",
                "",
                "    def handle_endtag(self, tag):",
                "        tag = tag.lower()",
                "        if tag in {'script', 'style', 'noscript'} and self._skip_depth:",
                "            self._skip_depth -= 1",
                "        if tag == 'a' and self._current_link is not None:",
                "            self._current_link['text'] = _collapse_ws(' '.join(self._current_link_text))",
                "            self.links.append(self._current_link)",
                "            self._current_link = None",
                "            self._current_link_text = []",
                "        if tag in {'td', 'th'} and self._current_cell is not None and self._current_row is not None:",
                "            self._current_row.append(_collapse_ws(' '.join(self._current_cell)))",
                "            self._current_cell = None",
                "        if tag == 'tr' and self._current_row is not None and self._current_table is not None:",
                "            if any(cell for cell in self._current_row):",
                "                self._current_table.append(self._current_row)",
                "            self._current_row = None",
                "        if tag == 'table' and self._current_table is not None:",
                "            if self._current_table:",
                "                headers = self._current_table[0]",
                "                rows = []",
                "                for row in self._current_table[1:]:",
                "                    if len(row) == len(headers):",
                "                        rows.append(dict(zip(headers, row, strict=False)))",
                "                self.tables.append({'columns': headers, 'rows': rows})",
                "            self._current_table = None",
                "        if self._tag_stack:",
                "            self._tag_stack.pop()",
                "",
                "    def handle_data(self, data):",
                "        if self._skip_depth:",
                "            return",
                "        text = data.strip()",
                "        if not text:",
                "            return",
                "        if self._tag_stack and self._tag_stack[-1] == 'title':",
                "            self.title_parts.append(text)",
                "        if self._current_link is not None:",
                "            self._current_link_text.append(text)",
                "        if self._current_cell is not None:",
                "            self._current_cell.append(text)",
                "        if not (self._tag_stack and self._tag_stack[-1] == 'title'):",
                "            self.text_parts.append(text)",
                "",
                "",
                "def _read_html(payload, sandbox):",
                "    if payload.get('text') is not None:",
                "        text = payload['text']",
                "        if not isinstance(text, str):",
                "            raise ValueError('html text must be a string')",
                "        return text, None",
                "    path = str(payload.get('path') or '').strip()",
                "    if not path:",
                "        raise ValueError('html skill requires text or path')",
                "    return sandbox.read_text(path), path",
                "",
                "",
                "def _parse(payload, sandbox):",
                "    html, path = _read_html(payload, sandbox)",
                "    max_chars = payload.get('max_text_chars', 4000)",
                "    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars < 1:",
                "        raise ValueError('html max_text_chars must be a positive integer')",
                "    parser = _HTMLExtractor()",
                "    parser.feed(html)",
                "    text = _collapse_ws(' '.join(parser.text_parts))",
                "    output = {",
                "        'title': _collapse_ws(' '.join(parser.title_parts)),",
                "        'text': text[:max_chars],",
                "        'text_truncated': len(text) > max_chars,",
                "        'links': parser.links,",
                "        'meta': parser.meta,",
                "        'tables': parser.tables,",
                "        'path': path,",
                "    }",
                "    return output",
                "",
                "",
                "def run(payload, context):",
                "    sandbox = context['sandbox']",
                "    action = str(payload.get('action') or '').strip().lower()",
                "    if not action:",
                "        return _ok(",
                "            'habit promoted HTML extraction skill ready',",
                "            {'supported_actions': ['extract', 'links', 'report']},",
                "        )",
                "    try:",
                "        if action == 'extract':",
                "            return _ok('HTML extracted', _parse(payload, sandbox))",
                "        if action == 'links':",
                "            output = _parse(payload, sandbox)",
                "            return _ok('HTML links extracted', {'links': output['links'], 'path': output['path']})",
                "        if action == 'report':",
                "            output_path = str(payload.get('output_path') or '').strip()",
                "            if not output_path:",
                "                raise ValueError('html.report requires output_path')",
                "            output = _parse(payload, sandbox)",
                "            lines = [",
                "                f\"title: {output['title']}\",",
                "                f\"link_count: {len(output['links'])}\",",
                "                f\"text: {output['text']}\",",
                "            ]",
                "            written = sandbox.write_text(",
                "                output_path,",
                "                '\\n'.join(lines) + '\\n',",
                "                overwrite=bool(payload.get('overwrite', True)),",
                "            )",
                "            output['output_path'] = output_path",
                "            return _ok(",
                "                'HTML report written',",
                "                output,",
                "                artifacts=[{'kind': 'file', 'path': str(written), 'description': 'HTML report'}],",
                "            )",
                "        return _fail(f'unsupported html action: {action}')",
                "    except ValueError as exc:",
                "        return _fail(str(exc))",
                "",
            ]
        )

    def _habit_archive_main(self) -> str:
        return "\n".join(
            [
                "from __future__ import annotations",
                "",
                "import tarfile",
                "import zipfile",
                "",
                "",
                "def _ok(message, output, artifacts=None):",
                "    result = {'success': True, 'status': 'completed', 'message': message, 'output': output}",
                "    if artifacts:",
                "        result['artifacts'] = artifacts",
                "    return result",
                "",
                "",
                "def _fail(message, error_code='validation_error', output=None):",
                "    return {",
                "        'success': False,",
                "        'status': 'failed',",
                "        'message': message,",
                "        'error_code': error_code,",
                "        'output': output or {},",
                "    }",
                "",
                "",
                "def _archive_path(payload, sandbox):",
                "    raw = str(payload.get('path') or '').strip()",
                "    if not raw:",
                "        raise ValueError('archive action requires path')",
                "    target = sandbox.resolve_path(raw)",
                "    if not target.exists():",
                "        raise ValueError(f'archive does not exist: {raw}')",
                "    if target.is_dir():",
                "        raise ValueError(f'archive path is a directory: {raw}')",
                "    return target, raw",
                "",
                "",
                "def _detect_format(path, requested=None):",
                "    fmt = str(requested or '').strip().lower()",
                "    if fmt in {'zip', 'tar', 'gztar', 'tgz'}:",
                "        return 'gztar' if fmt == 'tgz' else fmt",
                "    suffixes = ''.join(path.suffixes).lower()",
                "    if suffixes.endswith('.zip'):",
                "        return 'zip'",
                "    if suffixes.endswith('.tar.gz') or suffixes.endswith('.tgz'):",
                "        return 'gztar'",
                "    if suffixes.endswith('.tar'):",
                "        return 'tar'",
                "    raise ValueError('archive format must be zip, tar, or gztar')",
                "",
                "",
                "def _entry_dict(name, *, size=None, is_dir=False):",
                "    return {'name': name, 'size': size, 'is_dir': is_dir}",
                "",
                "",
                "def _list(payload, sandbox):",
                "    target, raw = _archive_path(payload, sandbox)",
                "    fmt = _detect_format(target, payload.get('format'))",
                "    entries = []",
                "    if fmt == 'zip':",
                "        with zipfile.ZipFile(target) as archive:",
                "            for item in archive.infolist():",
                "                entries.append(_entry_dict(item.filename, size=item.file_size, is_dir=item.is_dir()))",
                "    else:",
                "        mode = 'r:gz' if fmt == 'gztar' else 'r:'",
                "        with tarfile.open(target, mode) as archive:",
                "            for item in archive.getmembers():",
                "                entries.append(_entry_dict(item.name, size=item.size, is_dir=item.isdir()))",
                "    return _ok('Archive listed', {'entries': entries, 'path': raw, 'format': fmt})",
                "",
                "",
                "def _is_inside(path, root):",
                "    try:",
                "        path.relative_to(root)",
                "        return True",
                "    except ValueError:",
                "        return False",
                "",
                "",
                "def _safe_output(root, name):",
                "    if name.startswith('/') or '\\x00' in name:",
                "        raise ValueError(f'unsafe archive entry path: {name}')",
                "    target = (root / name).resolve()",
                "    if not _is_inside(target, root):",
                "        raise ValueError(f'unsafe archive entry path: {name}')",
                "    return target",
                "",
                "",
                "def _extract(payload, sandbox):",
                "    target, raw = _archive_path(payload, sandbox)",
                "    output_dir_raw = str(payload.get('output_dir') or '').strip()",
                "    if not output_dir_raw:",
                "        raise ValueError('archive.extract requires output_dir')",
                "    output_dir = sandbox.resolve_path(output_dir_raw)",
                "    output_dir.mkdir(parents=True, exist_ok=True)",
                "    overwrite = bool(payload.get('overwrite', False))",
                "    fmt = _detect_format(target, payload.get('format'))",
                "    entries = []",
                "    if fmt == 'zip':",
                "        with zipfile.ZipFile(target) as archive:",
                "            for item in archive.infolist():",
                "                destination = _safe_output(output_dir, item.filename)",
                "                entries.append(_entry_dict(item.filename, size=item.file_size, is_dir=item.is_dir()))",
                "                if item.is_dir():",
                "                    destination.mkdir(parents=True, exist_ok=True)",
                "                    continue",
                "                if destination.exists() and not overwrite:",
                "                    raise ValueError(f'target already exists: {item.filename}')",
                "                destination.parent.mkdir(parents=True, exist_ok=True)",
                "                destination.write_bytes(archive.read(item))",
                "    else:",
                "        mode = 'r:gz' if fmt == 'gztar' else 'r:'",
                "        with tarfile.open(target, mode) as archive:",
                "            for item in archive.getmembers():",
                "                destination = _safe_output(output_dir, item.name)",
                "                entries.append(_entry_dict(item.name, size=item.size, is_dir=item.isdir()))",
                "                if item.isdir():",
                "                    destination.mkdir(parents=True, exist_ok=True)",
                "                    continue",
                "                if not item.isfile():",
                "                    continue",
                "                if destination.exists() and not overwrite:",
                "                    raise ValueError(f'target already exists: {item.name}')",
                "                source = archive.extractfile(item)",
                "                if source is None:",
                "                    continue",
                "                destination.parent.mkdir(parents=True, exist_ok=True)",
                "                destination.write_bytes(source.read())",
                "    return _ok('Archive extracted', {'entries': entries, 'path': raw, 'output_dir': output_dir_raw, 'format': fmt})",
                "",
                "",
                "def _walk_files(path):",
                "    if path.is_file():",
                "        return [path]",
                "    if path.is_dir():",
                "        return sorted(item for item in path.rglob('*') if item.is_file())",
                "    raise ValueError(f'path does not exist: {path}')",
                "",
                "",
                "def _create(payload, sandbox):",
                "    raw_paths = payload.get('paths')",
                "    output_path = str(payload.get('output_path') or '').strip()",
                "    if not isinstance(raw_paths, list) or not raw_paths:",
                "        raise ValueError('archive.create requires non-empty paths array')",
                "    if not output_path:",
                "        raise ValueError('archive.create requires output_path')",
                "    output = sandbox.resolve_path(output_path)",
                "    if output.exists() and not bool(payload.get('overwrite', False)):",
                "        raise ValueError(f'archive output already exists: {output_path}')",
                "    output.parent.mkdir(parents=True, exist_ok=True)",
                "    fmt = _detect_format(output, payload.get('format'))",
                "    entries = []",
                "    resolved_roots = [(str(item), sandbox.resolve_path(str(item))) for item in raw_paths]",
                "    if fmt == 'zip':",
                "        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:",
                "            for raw, root in resolved_roots:",
                "                for file_path in _walk_files(root):",
                "                    arcname = file_path.relative_to(sandbox.resolve_path(raw).parent)",
                "                    archive.write(file_path, arcname.as_posix())",
                "                    entries.append(_entry_dict(arcname.as_posix(), size=file_path.stat().st_size))",
                "    else:",
                "        mode = 'w:gz' if fmt == 'gztar' else 'w:'",
                "        with tarfile.open(output, mode) as archive:",
                "            for raw, root in resolved_roots:",
                "                for file_path in _walk_files(root):",
                "                    arcname = file_path.relative_to(sandbox.resolve_path(raw).parent)",
                "                    archive.add(file_path, arcname=arcname.as_posix(), recursive=False)",
                "                    entries.append(_entry_dict(arcname.as_posix(), size=file_path.stat().st_size))",
                "    return _ok(",
                "        'Archive created',",
                "        {'entries': entries, 'output_path': output_path, 'format': fmt},",
                "        artifacts=[{'kind': 'file', 'path': str(output), 'description': 'Archive output'}],",
                "    )",
                "",
                "",
                "def run(payload, context):",
                "    sandbox = context['sandbox']",
                "    action = str(payload.get('action') or '').strip().lower()",
                "    if not action:",
                "        return _ok(",
                "            'habit promoted archive skill ready',",
                "            {'supported_actions': ['list', 'extract', 'create']},",
                "        )",
                "    try:",
                "        if action == 'list':",
                "            return _list(payload, sandbox)",
                "        if action == 'extract':",
                "            return _extract(payload, sandbox)",
                "        if action == 'create':",
                "            return _create(payload, sandbox)",
                "        return _fail(f'unsupported archive action: {action}')",
                "    except (OSError, tarfile.TarError, zipfile.BadZipFile, ValueError) as exc:",
                "        return _fail(str(exc))",
                "",
            ]
        )

    def _habit_datetime_main(self) -> str:
        return "\n".join(
            [
                "from __future__ import annotations",
                "",
                "from datetime import UTC, datetime, timedelta",
                "from zoneinfo import ZoneInfo, ZoneInfoNotFoundError",
                "",
                "",
                "def _ok(message, output):",
                "    return {'success': True, 'status': 'completed', 'message': message, 'output': output}",
                "",
                "",
                "def _fail(message, output=None):",
                "    return {'success': False, 'status': 'failed', 'message': message, 'output': output or {}}",
                "",
                "",
                "def _timezone(name):",
                "    if not name:",
                "        return UTC",
                "    normalized = str(name).strip()",
                "    if normalized.upper() == 'UTC':",
                "        return UTC",
                "    try:",
                "        return ZoneInfo(normalized)",
                "    except ZoneInfoNotFoundError as exc:",
                "        raise ValueError(f'unknown timezone: {normalized}') from exc",
                "",
                "",
                "def _parse_datetime(value, *, tz_name=None, input_format=None):",
                "    tz = _timezone(tz_name)",
                "    if isinstance(value, (int, float)):",
                "        return datetime.fromtimestamp(float(value), tz=tz)",
                "    if not isinstance(value, str) or not value.strip():",
                "        raise ValueError('datetime value must be a non-empty string or timestamp')",
                "    raw = value.strip()",
                "    if input_format:",
                "        parsed = datetime.strptime(raw, input_format)",
                "    else:",
                "        parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))",
                "    if parsed.tzinfo is None:",
                "        return parsed.replace(tzinfo=tz)",
                "    return parsed.astimezone(tz)",
                "",
                "",
                "def _serialize(dt, tz_name=None, *, value=None):",
                "    timezone_name = tz_name or (dt.tzinfo.tzname(dt) if dt.tzinfo else 'naive')",
                "    return {",
                "        'value': value if value is not None else dt.isoformat(),",
                "        'iso': dt.isoformat(),",
                "        'timestamp': dt.timestamp() if dt.tzinfo else None,",
                "        'timezone': timezone_name,",
                "        'diff': None,",
                "    }",
                "",
                "",
                "def _timedelta(amount, unit):",
                "    number = float(amount)",
                "    normalized = str(unit).strip().lower()",
                "    aliases = {",
                "        'second': 'seconds',",
                "        'seconds': 'seconds',",
                "        'minute': 'minutes',",
                "        'minutes': 'minutes',",
                "        'hour': 'hours',",
                "        'hours': 'hours',",
                "        'day': 'days',",
                "        'days': 'days',",
                "        'week': 'weeks',",
                "        'weeks': 'weeks',",
                "    }",
                "    key = aliases.get(normalized)",
                "    if key is None:",
                "        raise ValueError(f'unsupported datetime unit: {unit}')",
                "    return timedelta(**{key: number})",
                "",
                "",
                "def _now(payload):",
                "    tz_name = str(payload.get('timezone') or 'UTC')",
                "    dt = datetime.now(tz=_timezone(tz_name))",
                "    return _ok('datetime.now completed', _serialize(dt, tz_name))",
                "",
                "",
                "def _parse(payload):",
                "    tz_name = str(payload.get('timezone') or 'UTC')",
                "    dt = _parse_datetime(",
                "        payload.get('value'),",
                "        tz_name=tz_name,",
                "        input_format=payload.get('input_format'),",
                "    )",
                "    return _ok('datetime.parse completed', _serialize(dt, tz_name))",
                "",
                "",
                "def _format(payload):",
                "    tz_name = str(payload.get('timezone') or 'UTC')",
                "    dt = _parse_datetime(",
                "        payload.get('value'),",
                "        tz_name=tz_name,",
                "        input_format=payload.get('input_format'),",
                "    )",
                "    output_format = str(payload.get('output_format') or '%Y-%m-%dT%H:%M:%S%z')",
                "    formatted = dt.strftime(output_format)",
                "    return _ok('datetime.format completed', _serialize(dt, tz_name, value=formatted))",
                "",
                "",
                "def _diff(payload):",
                "    tz_name = str(payload.get('timezone') or 'UTC')",
                "    start = _parse_datetime(",
                "        payload.get('start'),",
                "        tz_name=tz_name,",
                "        input_format=payload.get('input_format'),",
                "    )",
                "    end = _parse_datetime(",
                "        payload.get('end'),",
                "        tz_name=tz_name,",
                "        input_format=payload.get('input_format'),",
                "    )",
                "    delta = end - start",
                "    output = _serialize(end, tz_name)",
                "    output['diff'] = {",
                "        'seconds': delta.total_seconds(),",
                "        'days': delta.days,",
                "        'microseconds': delta.microseconds,",
                "        'start': start.isoformat(),",
                "        'end': end.isoformat(),",
                "    }",
                "    return _ok('datetime.diff completed', output)",
                "",
                "",
                "def _add(payload):",
                "    tz_name = str(payload.get('timezone') or 'UTC')",
                "    base_value = payload.get('value') or datetime.now(tz=_timezone(tz_name)).isoformat()",
                "    dt = _parse_datetime(",
                "        base_value,",
                "        tz_name=tz_name,",
                "        input_format=payload.get('input_format'),",
                "    )",
                "    if payload.get('amount') is None or payload.get('unit') is None:",
                "        return _fail('datetime.add requires amount and unit')",
                "    result = dt + _timedelta(payload.get('amount'), str(payload.get('unit')))",
                "    return _ok('datetime.add completed', _serialize(result, tz_name))",
                "",
                "",
                "def run(payload, context):",
                "    del context",
                "    payload = dict(payload or {})",
                "    action = str(payload.get('action', 'now')).strip().lower()",
                "    try:",
                "        if action == 'now':",
                "            return _now(payload)",
                "        if action == 'parse':",
                "            return _parse(payload)",
                "        if action == 'format':",
                "            return _format(payload)",
                "        if action == 'diff':",
                "            return _diff(payload)",
                "        if action == 'add':",
                "            return _add(payload)",
                "        return _fail(f'Unsupported datetime action: {action}')",
                "    except (TypeError, ValueError) as exc:",
                "        return _fail(str(exc), {'errors': [str(exc)]})",
                "",
            ]
        )

    def _habit_git_main(self) -> str:
        return r"""
from __future__ import annotations

import subprocess


def _ok(message, output):
    return {'success': True, 'status': 'completed', 'message': message, 'output': output}


def _fail(message, output=None, error_code=None):
    result = {
        'success': False,
        'status': 'failed',
        'message': message,
        'output': output or {},
    }
    if error_code:
        result['error_code'] = error_code
    return result


def _bounded_text(value, limit):
    size = len(value)
    if size <= limit:
        return value, False, size
    return value[:limit], True, size


def _limit(payload):
    value = payload.get('max_output_chars', 12000)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError('git max_output_chars must be a positive integer')
    return min(value, 200000)


def _cwd(payload, sandbox):
    raw = str(payload.get('cwd') or '.')
    target = sandbox.resolve_path(raw)
    if not target.exists():
        raise ValueError(f'git cwd does not exist: {raw}')
    if not target.is_dir():
        raise ValueError(f'git cwd is not a directory: {raw}')
    return target, raw


def _paths(payload):
    raw = payload.get('paths', [])
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError('git paths must be an array')
    paths = []
    for item in raw:
        value = str(item)
        if not value or value.startswith('-') or '\x00' in value:
            raise ValueError('git paths must be non-option path strings')
        paths.append(value)
    return paths


def _max_count(payload):
    value = payload.get('max_count', 20)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError('git max_count must be a positive integer')
    return min(value, 200)


def _argv(payload):
    action = str(payload.get('action') or 'status').strip().lower()
    paths = _paths(payload)
    if action == 'status':
        argv = ['git', 'status', '--short']
        if paths:
            argv.extend(['--', *paths])
        return argv
    if action == 'diff':
        argv = ['git', 'diff', '--no-ext-diff']
        if bool(payload.get('stat', False)):
            argv.append('--stat')
        if paths:
            argv.extend(['--', *paths])
        return argv
    if action == 'log':
        return ['git', 'log', '--oneline', '-n', str(_max_count(payload))]
    if action == 'show':
        rev = str(payload.get('rev') or 'HEAD').strip()
        if not rev or rev.startswith('-') or '\x00' in rev:
            raise ValueError('git rev must be a non-option revision string')
        return ['git', 'show', '--stat', '--oneline', rev]
    raise ValueError(f'unsupported git action: {action}')


def _run_git(payload, sandbox):
    cwd, raw_cwd = _cwd(payload, sandbox)
    argv = _argv(payload)
    timeout = min(float(payload.get('timeout', 10)), 60.0)
    completed = subprocess.run(
        argv,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        shell=False,
        check=False,
    )
    limit = _limit(payload)
    stdout, stdout_truncated, stdout_size = _bounded_text(completed.stdout, limit)
    stderr, stderr_truncated, stderr_size = _bounded_text(completed.stderr, limit)
    output = {
        'argv': argv,
        'cwd': raw_cwd,
        'returncode': completed.returncode,
        'stdout': stdout,
        'stderr': stderr,
        'stdout_truncated': stdout_truncated,
        'stderr_truncated': stderr_truncated,
        'stdout_size': stdout_size,
        'stderr_size': stderr_size,
    }
    if completed.returncode == 0:
        return _ok('Git command completed', output)
    return _fail('Git command failed', output, error_code='execution_error')


def run(payload, context):
    payload = dict(payload or {})
    if not payload.get('action'):
        return _ok(
            'habit promoted read-only git skill ready',
            {'supported_actions': ['status', 'diff', 'log', 'show']},
        )
    sandbox = context['sandbox']
    try:
        if not getattr(sandbox, 'allow_external_commands', False):
            return _fail(
                'current sandbox does not allow external commands',
                error_code='permission_denied',
            )
        return _run_git(payload, sandbox)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.output if isinstance(exc.output, str) else (exc.output or b'').decode(
            'utf-8', 'replace'
        )
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b'').decode(
            'utf-8', 'replace'
        )
        limit = _limit(payload)
        stdout, stdout_truncated, stdout_size = _bounded_text(stdout, limit)
        stderr, stderr_truncated, stderr_size = _bounded_text(stderr, limit)
        return _fail(
            'Git command timed out',
            {
                'argv': _argv(payload),
                'returncode': None,
                'stdout': stdout,
                'stderr': stderr,
                'stdout_truncated': stdout_truncated,
                'stderr_truncated': stderr_truncated,
                'stdout_size': stdout_size,
                'stderr_size': stderr_size,
                'timed_out': True,
            },
            error_code='execution_error',
        )
    except (OSError, ValueError) as exc:
        return _fail(str(exc), error_code='validation_error')
""".lstrip()

    def _habit_http_main(self) -> str:
        return r"""
from __future__ import annotations

import base64
import contextlib
import json
import socket
import urllib.error as urllib_error
import urllib.parse as urllib_parse
import urllib.request as urllib_request

_ACTIONS = {'get': 'GET', 'post': 'POST', 'put': 'PUT', 'delete': 'DELETE'}
_DEFAULT_MAX_RESPONSE_CHARS = 4096
_URL_PATH_SAFE = "/%:@!$&'()*+,;="
_URL_QUERY_SAFE = "/%?=&:@!$'()*+,;[]"
_URL_FRAGMENT_SAFE = "/%?=&:@!$'()*+,;[]"


def _ok(message, output):
    return {'success': True, 'status': 'completed', 'message': message, 'output': output}


def _fail(message, output=None, error_code='execution_error'):
    return {
        'success': False,
        'status': 'failed',
        'message': message,
        'output': output or {},
        'error_code': error_code,
    }


def _encode_http_url(url):
    parsed = urllib_parse.urlparse(url)
    if parsed.scheme not in {'http', 'https'}:
        return url
    netloc = parsed.netloc
    if parsed.hostname:
        host = parsed.hostname
        try:
            host = host.encode('idna').decode('ascii')
        except UnicodeError:
            host = urllib_parse.quote(host, safe='')
        if ':' in host and not host.startswith('['):
            host = f'[{host}]'
        userinfo = ''
        if parsed.username is not None:
            userinfo = urllib_parse.quote(parsed.username, safe='')
            if parsed.password is not None:
                userinfo = f"{userinfo}:{urllib_parse.quote(parsed.password, safe='')}"
            userinfo = f'{userinfo}@'
        port = f':{parsed.port}' if parsed.port is not None else ''
        netloc = f'{userinfo}{host}{port}'
    return urllib_parse.urlunparse(
        (
            parsed.scheme,
            netloc,
            urllib_parse.quote(parsed.path, safe=_URL_PATH_SAFE),
            urllib_parse.quote(parsed.params, safe=_URL_PATH_SAFE),
            urllib_parse.quote(parsed.query, safe=_URL_QUERY_SAFE),
            urllib_parse.quote(parsed.fragment, safe=_URL_FRAGMENT_SAFE),
        )
    )


def _headers(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError('http headers must be an object')
    return {str(key): str(item) for key, item in value.items()}


def _append_params(url, params):
    if params in (None, {}):
        return url
    if not isinstance(params, dict):
        raise ValueError('http params must be an object')
    encoded = urllib_parse.urlencode(params, doseq=True)
    if not encoded:
        return url
    parts = urllib_parse.urlsplit(url)
    query = f'{parts.query}&{encoded}' if parts.query else encoded
    return urllib_parse.urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def _set_default_header(headers, name, value):
    lowered = {key.lower() for key in headers}
    if name.lower() not in lowered:
        headers[name] = value


def _apply_auth(headers, auth):
    if auth in (None, {}):
        return
    if not isinstance(auth, dict):
        raise ValueError('http auth must be an object')
    auth_type = str(auth.get('type') or '').strip().lower()
    if auth_type == 'bearer':
        token = str(auth.get('token') or '').strip()
        if not token:
            raise ValueError('bearer auth requires token')
        headers['Authorization'] = f'Bearer {token}'
        return
    if auth_type == 'basic':
        username = str(auth.get('username') or '')
        password = str(auth.get('password') or '')
        raw = f'{username}:{password}'.encode('utf-8')
        headers['Authorization'] = 'Basic ' + base64.b64encode(raw).decode('ascii')
        return
    raise ValueError(f'unsupported http auth type: {auth_type}')


def _request_body(payload, headers):
    if 'json' in payload or 'json_body' in payload:
        value = payload.get('json') if 'json' in payload else payload.get('json_body')
        _set_default_header(headers, 'Content-Type', 'application/json')
        return json.dumps(value, ensure_ascii=False).encode('utf-8')
    if 'form' in payload:
        form = payload.get('form')
        if not isinstance(form, dict):
            raise ValueError('http form body must be an object')
        _set_default_header(headers, 'Content-Type', 'application/x-www-form-urlencoded')
        return urllib_parse.urlencode(form, doseq=True).encode('utf-8')
    if 'raw_body' in payload or 'body' in payload:
        value = payload.get('raw_body') if 'raw_body' in payload else payload.get('body')
        if isinstance(value, bytes):
            return value
        if value is None:
            return None
        return str(value).encode('utf-8')
    return None


def _timeout(payload, sandbox):
    value = payload.get('timeout', getattr(sandbox, 'timeout_seconds', 30))
    timeout = float(value)
    if timeout <= 0:
        raise ValueError('http timeout must be greater than zero')
    return timeout


def _max_response_chars(payload):
    value = int(payload.get('max_response_chars', _DEFAULT_MAX_RESPONSE_CHARS))
    if value <= 0:
        raise ValueError('http max_response_chars must be greater than zero')
    return value


def _headers_map(headers_obj):
    if headers_obj is None:
        return {}
    if hasattr(headers_obj, 'items'):
        return {str(key): str(value) for key, value in headers_obj.items()}
    return {}


def _charset(headers_obj, headers_map):
    if hasattr(headers_obj, 'get_content_charset'):
        charset = headers_obj.get_content_charset()
        if charset:
            return str(charset)
    content_type = next(
        (value for key, value in headers_map.items() if key.lower() == 'content-type'),
        '',
    )
    for part in content_type.split(';'):
        name, sep, value = part.strip().partition('=')
        if sep and name.lower() == 'charset' and value.strip():
            return value.strip()
    return 'utf-8'


def _response_output(*, method, url, request_url, status, reason, headers_obj, body_bytes, max_chars):
    response_headers = _headers_map(headers_obj)
    body = body_bytes.decode(_charset(headers_obj, response_headers), errors='replace')
    preview = body[:max_chars]
    content_type = next(
        (value for key, value in response_headers.items() if key.lower() == 'content-type'),
        '',
    )
    parsed_json = None
    json_error = None
    if 'json' in content_type.lower() and body:
        try:
            parsed_json = json.loads(body)
        except json.JSONDecodeError as exc:
            json_error = str(exc)
    output = {
        'method': method,
        'url': url,
        'request_url': request_url,
        'status': status,
        'reason': reason,
        'headers': response_headers,
        'body': preview,
        'body_preview': preview,
        'body_truncated': len(body) > max_chars,
        'body_size': len(body),
        'json': parsed_json,
    }
    if json_error:
        output['json_error'] = json_error
    return output


def run(payload, context):
    payload = dict(payload or {})
    if not payload.get('action') and not payload.get('url'):
        return _ok(
            'habit promoted HTTP skill ready',
            {'supported_actions': ['get', 'post', 'put', 'delete']},
        )
    sandbox = context['sandbox']
    action = str(payload.get('action') or 'get').strip().lower()
    method = _ACTIONS.get(action)
    if method is None:
        return _fail(f'Unsupported HTTP action: {action}', error_code='validation_error')
    if not getattr(sandbox, 'allow_network', False):
        return _fail(
            'current sandbox does not allow network access',
            error_code='permission_denied',
        )
    try:
        url = str(payload.get('url') or '').strip()
        if not url:
            return _fail('http request requires url', error_code='validation_error')
        parsed = urllib_parse.urlparse(url)
        if parsed.scheme not in {'http', 'https'}:
            return _fail(
                f'unsupported HTTP URL scheme: {parsed.scheme}',
                error_code='validation_error',
            )
        request_url = _encode_http_url(_append_params(url, payload.get('params')))
        headers = _headers(payload.get('headers'))
        _apply_auth(headers, payload.get('auth'))
        body = _request_body(payload, headers)
        timeout = _timeout(payload, sandbox)
        max_chars = _max_response_chars(payload)
        request = urllib_request.Request(request_url, data=body, headers=headers, method=method)
        with contextlib.closing(urllib_request.urlopen(request, timeout=timeout)) as response:
            status = int(getattr(response, 'status', 0) or response.getcode() or 200)
            reason = str(getattr(response, 'reason', ''))
            output = _response_output(
                method=method,
                url=url,
                request_url=request_url,
                status=status,
                reason=reason,
                headers_obj=getattr(response, 'headers', None),
                body_bytes=response.read(),
                max_chars=max_chars,
            )
            return _ok(f'HTTP {method} completed: {status}', output)
    except urllib_error.HTTPError as exc:
        output = _response_output(
            method=method,
            url=str(payload.get('url') or ''),
            request_url=getattr(exc, 'url', str(payload.get('url') or '')),
            status=int(exc.code),
            reason=str(exc.reason),
            headers_obj=exc.headers,
            body_bytes=exc.read(),
            max_chars=_max_response_chars(payload),
        )
        return _fail(f'HTTP {method} failed: {exc.code}', output, 'execution_error')
    except (TimeoutError, socket.timeout) as exc:
        return _fail(f'HTTP {method} timeout: {exc}', error_code='execution_error')
    except urllib_error.URLError as exc:
        return _fail(f'HTTP {method} failed: {exc.reason}', error_code='execution_error')
    except (TypeError, ValueError) as exc:
        return _fail(str(exc), error_code='validation_error')
""".lstrip()

    def _habit_browser_main(self) -> str:
        return r"""
from __future__ import annotations

from urllib.parse import urlparse

_BLOCKED_CONTENT_TOKENS = (
    '百度安全验证',
    '安全验证',
    '人机验证',
    '验证码',
    '网络不给力，请稍后重试',
    'verify you are human',
    'captcha',
    'access denied',
    'forbidden',
    'just a moment',
    'enable javascript',
    'unusual traffic',
    'location.replace(location.href.replace',
    'http-equiv="refresh"',
    'url=http://www.baidu.com/',
)
_DEFAULT_MAX_BODY_CHARS = 8192


def _ok(message, output):
    return {'success': True, 'status': 'completed', 'message': message, 'output': output}


def _fail(message, output=None, error_code='execution_error'):
    return {
        'success': False,
        'status': 'failed',
        'message': message,
        'output': output or {},
        'error_code': error_code,
    }


def _looks_blocked(body):
    lowered = str(body).lower()
    return any(token.lower() in lowered for token in _BLOCKED_CONTENT_TOKENS)


def _max_body_chars(payload):
    value = int(payload.get('max_body_chars', _DEFAULT_MAX_BODY_CHARS))
    if value <= 0:
        raise ValueError('browser max_body_chars must be greater than zero')
    return value


def _compact_response(response, max_chars):
    body = str(response.get('body', ''))
    preview = body[:max_chars]
    output = {
        **dict(response),
        'body': preview,
        'body_preview': preview,
        'body_truncated': len(body) > max_chars,
        'body_size': len(body),
    }
    return output


def run(payload, context):
    payload = dict(payload or {})
    if not payload.get('action') and not payload.get('url'):
        return _ok('habit promoted browser fetch skill ready', {'supported_actions': ['fetch']})
    action = str(payload.get('action') or 'fetch').strip().lower()
    if action != 'fetch':
        return _fail(f'unsupported browser action: {action}', error_code='validation_error')
    url = str(payload.get('url') or '').strip()
    if not url:
        return _fail('browser fetch requires url', error_code='validation_error')
    sandbox = context['sandbox']
    parsed = urlparse(url)
    if parsed.scheme in {'http', 'https'} and not getattr(sandbox, 'allow_network', False):
        return _fail(
            'current sandbox does not allow network access',
            error_code='permission_denied',
        )
    try:
        response = sandbox.fetch_url(url)
        output = _compact_response(response, _max_body_chars(payload))
    except Exception as exc:
        return _fail(f'browser fetch failed: {exc}', error_code='execution_error')
    if _looks_blocked(output.get('body', '')):
        output['blocked'] = True
        output['blocked_reason'] = 'access_verification_or_denial'
        return _fail(
            'Browser fetch was blocked by verification, captcha, or access denial',
            output,
            'execution_error',
        )
    output['blocked'] = False
    return _ok(f"Browser fetch completed: {output.get('url')}", output)
""".lstrip()

    def _habit_python_main(self) -> str:
        return r"""
from __future__ import annotations


def _ok(message, output):
    return {'success': True, 'status': 'completed', 'message': message, 'output': output}


def _fail(message, output=None, error_code='execution_error'):
    return {
        'success': False,
        'status': 'failed',
        'message': message,
        'output': output or {},
        'error_code': error_code,
    }


def run(payload, context):
    payload = dict(payload or {})
    if not payload.get('action') and not payload.get('code'):
        return _ok('habit promoted Python skill ready', {'supported_actions': ['run']})
    action = str(payload.get('action') or 'run').strip().lower()
    if action != 'run':
        return _fail(f'unsupported Python action: {action}', error_code='validation_error')
    code = str(payload.get('code') or '').strip()
    if not code:
        return _fail('Python run requires code', error_code='validation_error')
    sandbox = context['sandbox']
    try:
        response = sandbox.run_python(code)
    except Exception as exc:
        return _fail(
            f'Python execution failed: {exc}',
            {'stdout': '', 'traceback': ''},
            error_code='execution_error',
        )
    if not bool(response.get('ok')):
        return _fail(
            str(response.get('error', 'Python execution failed')),
            {
                'stdout': response.get('stdout', ''),
                'traceback': response.get('traceback', ''),
                'result': response.get('result'),
            },
            error_code='execution_error',
        )
    return _ok(
        'Python execution completed',
        {
            'result': response.get('result'),
            'stdout': response.get('stdout', ''),
            'traceback': response.get('traceback', ''),
        },
    )
""".lstrip()

    def _habit_shell_main(self) -> str:
        return r"""
from __future__ import annotations

import subprocess
from pathlib import Path

_ALLOWED_COMMANDS = {
    'grep',
    'find',
    'wc',
    'head',
    'tail',
    'sort',
    'uniq',
    'cat',
    'ls',
    'du',
    'df',
}
_DEFAULT_MAX_OUTPUT_CHARS = 4096


def _ok(message, output):
    return {'success': True, 'status': 'completed', 'message': message, 'output': output}


def _fail(message, output=None, error_code='execution_error'):
    return {
        'success': False,
        'status': 'failed',
        'message': message,
        'output': output or {},
        'error_code': error_code,
    }


def _argv(payload):
    value = payload.get('argv', payload.get('command'))
    if not isinstance(value, list) or not value:
        raise ValueError('shell.safe requires non-empty argv array')
    argv = [str(item) for item in value]
    if any(not item for item in argv):
        raise ValueError('shell.safe argv items must be non-empty strings')
    command = argv[0]
    if '/' in command or '\\' in command:
        raise ValueError('shell.safe command must be a whitelisted command name, not a path')
    if Path(command).name not in _ALLOWED_COMMANDS:
        raise ValueError(f'shell.safe command is not allowed: {command}')
    return argv


def _timeout(payload, sandbox):
    value = payload.get('timeout', getattr(sandbox, 'timeout_seconds', 30))
    timeout = float(value)
    if timeout <= 0:
        raise ValueError('shell.safe timeout must be greater than zero')
    return timeout


def _max_output_chars(payload):
    value = int(payload.get('max_output_chars', _DEFAULT_MAX_OUTPUT_CHARS))
    if value <= 0:
        raise ValueError('shell.safe max_output_chars must be greater than zero')
    return value


def _cwd(payload, sandbox):
    raw_cwd = str(payload.get('cwd') or '.')
    cwd = sandbox.resolve_path(raw_cwd)
    if not cwd.exists():
        raise FileNotFoundError(f'cwd does not exist: {raw_cwd}')
    if not cwd.is_dir():
        raise ValueError(f'cwd is not a directory: {raw_cwd}')
    return cwd


def _preview(value, max_chars):
    if value is None:
        text = ''
    elif isinstance(value, bytes):
        text = value.decode('utf-8', errors='replace')
    else:
        text = str(value)
    return text[:max_chars], len(text) > max_chars, len(text)


def _output(*, argv, cwd, returncode, stdout, stderr, max_chars, timed_out=False):
    stdout_preview, stdout_truncated, stdout_size = _preview(stdout, max_chars)
    stderr_preview, stderr_truncated, stderr_size = _preview(stderr, max_chars)
    return {
        'argv': list(argv),
        'cwd': str(cwd),
        'returncode': returncode,
        'stdout': stdout_preview,
        'stderr': stderr_preview,
        'stdout_preview': stdout_preview,
        'stderr_preview': stderr_preview,
        'stdout_truncated': stdout_truncated,
        'stderr_truncated': stderr_truncated,
        'stdout_size': stdout_size,
        'stderr_size': stderr_size,
        'timed_out': timed_out,
    }


def run(payload, context):
    payload = dict(payload or {})
    if not payload.get('argv') and not payload.get('command'):
        return _ok(
            'habit promoted safe shell skill ready',
            {'supported_actions': ['run'], 'allowed_commands': sorted(_ALLOWED_COMMANDS)},
        )
    sandbox = context['sandbox']
    if not getattr(sandbox, 'allow_external_commands', False):
        return _fail(
            'current sandbox does not allow external commands',
            error_code='permission_denied',
        )
    try:
        argv = _argv(payload)
        cwd = _cwd(payload, sandbox)
        timeout = _timeout(payload, sandbox)
        max_chars = _max_output_chars(payload)
        completed = subprocess.run(
            argv,
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
        output = _output(
            argv=argv,
            cwd=cwd,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            max_chars=max_chars,
        )
        if completed.returncode == 0:
            return _ok(f"shell.safe completed: {' '.join(argv)}", output)
        return _fail(
            f'shell.safe command failed with returncode {completed.returncode}',
            output,
            'execution_error',
        )
    except subprocess.TimeoutExpired as exc:
        argv_value = [str(item) for item in exc.cmd] if isinstance(exc.cmd, list) else [str(exc.cmd)]
        try:
            cwd = _cwd(payload, sandbox)
            max_chars = _max_output_chars(payload)
        except (TypeError, ValueError, FileNotFoundError):
            cwd = getattr(sandbox, 'runtime_root', Path('.'))
            max_chars = _DEFAULT_MAX_OUTPUT_CHARS
        return _fail(
            f'shell.safe command timed out after {exc.timeout} seconds',
            _output(
                argv=argv_value,
                cwd=cwd,
                returncode=None,
                stdout=exc.stdout,
                stderr=exc.stderr,
                max_chars=max_chars,
                timed_out=True,
            ),
            'execution_error',
        )
    except (OSError, TypeError, ValueError, FileNotFoundError) as exc:
        return _fail(str(exc), error_code='validation_error')
""".lstrip()

    def _habit_csv_main(self) -> str:
        return "\n".join(
            [
                "from __future__ import annotations",
                "",
                "import csv",
                "import io",
                "",
                "",
                "def _delimiter(payload):",
                "    delimiter = str(payload.get('delimiter') or ',')",
                "    if len(delimiter) != 1:",
                "        raise ValueError('csv delimiter must be one character')",
                "    return delimiter",
                "",
                "",
                "def _columns_from_rows(rows):",
                "    columns = []",
                "    for row in rows:",
                "        for key in row:",
                "            if key not in columns:",
                "                columns.append(str(key))",
                "    return columns",
                "",
                "",
                "def _normalize_rows(value):",
                "    if not isinstance(value, list):",
                "        raise ValueError('csv rows must be an array')",
                "    rows = []",
                "    for item in value:",
                "        if not isinstance(item, dict):",
                "            raise ValueError('each csv row must be an object')",
                "        rows.append(dict(item))",
                "    return rows",
                "",
                "",
                "def _load_rows(payload, sandbox):",
                "    if payload.get('rows') is not None:",
                "        rows = _normalize_rows(payload['rows'])",
                "        columns = [str(item) for item in payload.get('columns', [])] or _columns_from_rows(rows)",
                "        return rows, columns, None",
                "    path = str(payload.get('path') or '').strip()",
                "    if not path:",
                "        raise ValueError('csv skill requires path or rows')",
                "    text = sandbox.read_text(path)",
                "    reader = csv.DictReader(io.StringIO(text), delimiter=_delimiter(payload))",
                "    rows = [dict(row) for row in reader]",
                "    return rows, list(reader.fieldnames or []), path",
                "",
                "",
                "def _numeric(value):",
                "    if value is None or str(value).strip() == '':",
                "        raise ValueError('empty numeric value')",
                "    return float(value)",
                "",
                "",
                "def _matches(row, condition):",
                "    column = str(condition.get('column') or '')",
                "    op = str(condition.get('op') or condition.get('operator') or 'equals').lower()",
                "    expected = condition.get('value')",
                "    actual = row.get(column)",
                "    if op == 'equals':",
                "        return str(actual) == str(expected)",
                "    if op == 'contains':",
                "        return str(expected) in str(actual)",
                "    if op == 'gt':",
                "        return _numeric(actual) > _numeric(expected)",
                "    if op == 'gte':",
                "        return _numeric(actual) >= _numeric(expected)",
                "    if op == 'lt':",
                "        return _numeric(actual) < _numeric(expected)",
                "    if op == 'lte':",
                "        return _numeric(actual) <= _numeric(expected)",
                "    raise ValueError(f'unsupported csv filter operator: {op}')",
                "",
                "",
                "def _filtered_rows(rows, filters):",
                "    if filters is None:",
                "        return rows",
                "    if isinstance(filters, dict):",
                "        filters = [filters]",
                "    if not isinstance(filters, list):",
                "        raise ValueError('csv filters must be an array')",
                "    conditions = [dict(item) for item in filters if isinstance(item, dict)]",
                "    return [row for row in rows if all(_matches(row, condition) for condition in conditions)]",
                "",
                "",
                "def _aggregate_value(rows, aggregate):",
                "    op = str(aggregate.get('op') or aggregate.get('function') or 'count').lower()",
                "    column = aggregate.get('column')",
                "    if op == 'count':",
                "        return len(rows)",
                "    if column is None:",
                "        raise ValueError('csv aggregate requires column for numeric aggregate')",
                "    numbers = [_numeric(row.get(str(column))) for row in rows]",
                "    if not numbers:",
                "        raise ValueError('csv aggregate has no numeric values')",
                "    if op == 'sum':",
                "        return sum(numbers)",
                "    if op == 'avg':",
                "        return sum(numbers) / len(numbers)",
                "    if op == 'min':",
                "        return min(numbers)",
                "    if op == 'max':",
                "        return max(numbers)",
                "    raise ValueError(f'unsupported csv aggregate op: {op}')",
                "",
                "",
                "def run(payload, context):",
                "    sandbox = context['sandbox']",
                "    action = str(payload.get('action', '')).strip().lower()",
                "    if not action:",
                "        return {",
                "            'success': True,",
                "            'status': 'completed',",
                "            'message': 'habit promoted CSV/TSV data skill ready',",
                "            'output': {'supported_actions': ['read', 'select', 'filter', 'aggregate', 'report', 'write']},",
                "        }",
                "    rows, columns, path = _load_rows(payload, sandbox)",
                "    if action == 'read':",
                "        return {",
                "            'success': True,",
                "            'status': 'completed',",
                "            'message': 'CSV read completed',",
                "            'output': {'rows': rows, 'columns': columns, 'row_count': len(rows), 'result': None, 'path': path},",
                "        }",
                "    if action == 'select':",
                "        selected_columns = [str(item) for item in payload.get('columns', [])]",
                "        if not selected_columns:",
                "            raise ValueError('csv.select requires non-empty columns array')",
                "        selected = [{column: row.get(column) for column in selected_columns} for row in rows]",
                "        return {",
                "            'success': True,",
                "            'status': 'completed',",
                "            'message': 'CSV select completed',",
                "            'output': {'rows': selected, 'columns': selected_columns, 'row_count': len(selected), 'result': None, 'path': path},",
                "        }",
                "    if action == 'filter':",
                "        filtered = _filtered_rows(rows, payload.get('filters', []))",
                "        return {",
                "            'success': True,",
                "            'status': 'completed',",
                "            'message': 'CSV filter completed',",
                "            'output': {'rows': filtered, 'columns': columns, 'row_count': len(filtered), 'result': None, 'path': path},",
                "        }",
                "    if action == 'aggregate':",
                "        aggregate = payload.get('aggregate')",
                "        if not isinstance(aggregate, dict):",
                "            raise ValueError('csv.aggregate requires aggregate object')",
                "        filtered = _filtered_rows(rows, aggregate.get('filters'))",
                "        value = _aggregate_value(filtered, aggregate)",
                "        op = str(aggregate.get('op') or aggregate.get('function') or 'count').lower()",
                "        return {",
                "            'success': True,",
                "            'status': 'completed',",
                "            'message': 'CSV aggregate completed',",
                "            'output': {'rows': rows, 'columns': columns, 'row_count': len(rows), 'result': {'op': op, 'column': aggregate.get('column'), 'value': value}, 'path': path},",
                "        }",
                "    if action == 'report':",
                "        output_path = str(payload.get('output_path') or '').strip()",
                "        if not output_path:",
                "            raise ValueError('csv.report requires output_path')",
                "        metrics = payload.get('metrics') or payload.get('aggregates')",
                "        if not isinstance(metrics, list) or not metrics:",
                "            raise ValueError('csv.report requires non-empty metrics array')",
                "        result = {}",
                "        lines = []",
                "        for item in metrics:",
                "            if not isinstance(item, dict):",
                "                raise ValueError('csv.report metrics must be objects')",
                "            name = str(item.get('name') or item.get('label') or item.get('column') or 'value')",
                "            value = _aggregate_value(_filtered_rows(rows, item.get('filters')), item)",
                "            result[name] = value",
                "            lines.append(f'{name}: {value}')",
                "        content = '\\n'.join(lines) + ('\\n' if payload.get('trailing_newline', True) else '')",
                "        sandbox.write_text(output_path, content, overwrite=bool(payload.get('overwrite', True)))",
                "        return {",
                "            'success': True,",
                "            'status': 'completed',",
                "            'message': f'CSV report completed: {output_path}',",
                "            'output': {'rows': rows, 'columns': columns, 'row_count': len(rows), 'result': result, 'path': path, 'output_path': output_path},",
                "        }",
                "    if action == 'write':",
                "        output_path = str(payload.get('output_path') or '').strip()",
                "        if not output_path:",
                "            raise ValueError('csv.write requires output_path')",
                "        columns = [str(item) for item in payload.get('columns', [])] or columns or _columns_from_rows(rows)",
                "        buffer = io.StringIO()",
                "        writer = csv.DictWriter(buffer, fieldnames=columns, delimiter=_delimiter(payload))",
                "        writer.writeheader()",
                "        for row in rows:",
                "            writer.writerow({column: row.get(column, '') for column in columns})",
                "        content = buffer.getvalue()",
                "        sandbox.write_text(output_path, content, overwrite=bool(payload.get('overwrite', False)))",
                "        return {",
                "            'success': True,",
                "            'status': 'completed',",
                "            'message': f'CSV write completed: {output_path}',",
                "            'output': {'rows': rows, 'columns': columns, 'row_count': len(rows), 'result': {'bytes': len(content.encode('utf-8'))}, 'path': output_path},",
                "        }",
                "    raise ValueError(f'unsupported csv action: {action}')",
                "",
            ]
        )

    def _habit_file_test(self, *, skill_name: str) -> str:
        return "\n".join(
            [
                "from __future__ import annotations",
                "",
                "import importlib.util",
                "import tempfile",
                "import unittest",
                "from pathlib import Path",
                "",
                "",
                "class FakeSandbox:",
                "    def __init__(self):",
                "        self._tmp = tempfile.TemporaryDirectory()",
                "        self.root = Path(self._tmp.name)",
                "        (self.root / 'source.txt').write_text('hello', encoding='utf-8')",
                "        (self.root / 'lines.txt').write_text('status: old\\nowner: bot\\n', encoding='utf-8')",
                "",
                "    def resolve_path(self, path):",
                "        return self.root / path",
                "",
                "    def list_dir(self, path):",
                "        return sorted(item.name for item in self.resolve_path(path).iterdir())",
                "",
                "    def read_text(self, path):",
                "        return self.resolve_path(path).read_text(encoding='utf-8')",
                "",
                "    def write_text(self, path, content, overwrite=False):",
                "        target = self.resolve_path(path)",
                "        if target.exists() and not overwrite:",
                "            raise ValueError('target exists')",
                "        target.parent.mkdir(parents=True, exist_ok=True)",
                "        target.write_text(content, encoding='utf-8')",
                "        return target",
                "",
                "",
                "def _load_module():",
                f"    module_name = 'habit_promoted_test_{_safe_component(skill_name)}'",
                "    main_path = Path(__file__).resolve().parents[1] / 'main.py'",
                "    spec = importlib.util.spec_from_file_location(module_name, main_path)",
                "    module = importlib.util.module_from_spec(spec)",
                "    assert spec is not None and spec.loader is not None",
                "    spec.loader.exec_module(module)",
                "    return module",
                "",
                "",
                "class HabitPromotedSkillTest(unittest.TestCase):",
                "    def test_write_action(self):",
                "        module = _load_module()",
                "        result = module.run(",
                "            {'action': 'write', 'path': 'tmp.txt', 'content': 'hello', 'overwrite': True},",
                "            {'sandbox': FakeSandbox()},",
                "        )",
                "        self.assertTrue(result['success'])",
                "        self.assertEqual(result['output']['path'], 'tmp.txt')",
                "",
                "    def test_replace_line_action(self):",
                "        module = _load_module()",
                "        sandbox = FakeSandbox()",
                "        result = module.run(",
                "            {'action': 'replace_line', 'path': 'lines.txt', 'line_prefix': 'status:', 'content': 'status: new'},",
                "            {'sandbox': sandbox},",
                "        )",
                "        self.assertTrue(result['success'])",
                "        self.assertEqual(result['output']['line_index'], 0)",
                "        self.assertEqual((sandbox.root / 'lines.txt').read_text(encoding='utf-8'), 'status: new\\nowner: bot\\n')",
                "",
                "    def test_copy_action(self):",
                "        module = _load_module()",
                "        sandbox = FakeSandbox()",
                "        result = module.run(",
                "            {'action': 'copy', 'source': 'source.txt', 'target': 'copies/source.txt'},",
                "            {'sandbox': sandbox},",
                "        )",
                "        self.assertTrue(result['success'])",
                "        self.assertEqual(result['output']['action'], 'copy')",
                "        self.assertEqual((sandbox.root / 'copies' / 'source.txt').read_text(encoding='utf-8'), 'hello')",
                "",
                "",
                "if __name__ == '__main__':",
                "    unittest.main()",
                "",
            ]
        )

    def _habit_json_test(self, *, skill_name: str) -> str:
        return "\n".join(
            [
                "from __future__ import annotations",
                "",
                "import importlib.util",
                "import unittest",
                "from pathlib import Path",
                "",
                "",
                "class FakeSandbox:",
                "    def __init__(self):",
                '        self.files = {\'profile.json\': \'{"user": {"name": "Ada"}}\'}',
                "",
                "    def read_text(self, path):",
                "        return self.files[path]",
                "",
                "    def write_text(self, path, content, overwrite=False):",
                "        self.files[path] = content",
                "        return Path(path)",
                "",
                "",
                "def _load_module():",
                f"    module_name = 'habit_promoted_json_test_{_safe_component(skill_name)}'",
                "    main_path = Path(__file__).resolve().parents[1] / 'main.py'",
                "    spec = importlib.util.spec_from_file_location(module_name, main_path)",
                "    module = importlib.util.module_from_spec(spec)",
                "    assert spec is not None and spec.loader is not None",
                "    spec.loader.exec_module(module)",
                "    return module",
                "",
                "",
                "class HabitPromotedJsonSkillTest(unittest.TestCase):",
                "    def test_query_action(self):",
                "        module = _load_module()",
                "        result = module.run(",
                "            {'action': 'query', 'path': 'profile.json', 'query': 'user.name'},",
                "            {'sandbox': FakeSandbox()},",
                "        )",
                "        self.assertTrue(result['success'])",
                "        self.assertEqual(result['output']['value'], 'Ada')",
                "",
                "    def test_report_action(self):",
                "        module = _load_module()",
                "        sandbox = FakeSandbox()",
                "        result = module.run(",
                "            {",
                "                'action': 'report',",
                "                'path': 'profile.json',",
                "                'fields': [{'label': 'name', 'query': 'user.name'}],",
                "                'output_path': 'reports/profile.md',",
                "            },",
                "            {'sandbox': sandbox},",
                "        )",
                "        self.assertTrue(result['success'])",
                "        self.assertIn('name: Ada', sandbox.files['reports/profile.md'])",
                "",
                "",
                "if __name__ == '__main__':",
                "    unittest.main()",
                "",
            ]
        )

    def _habit_csv_test(self, *, skill_name: str) -> str:
        return "\n".join(
            [
                "from __future__ import annotations",
                "",
                "import importlib.util",
                "import unittest",
                "from pathlib import Path",
                "",
                "",
                "class FakeSandbox:",
                "    def __init__(self):",
                "        self.files = {'sales.csv': 'region,amount\\nA,10\\nB,15\\n'}",
                "",
                "    def read_text(self, path):",
                "        return self.files[path]",
                "",
                "    def write_text(self, path, content, overwrite=False):",
                "        self.files[path] = content",
                "        return Path(path)",
                "",
                "",
                "def _load_module():",
                f"    module_name = 'habit_promoted_csv_test_{_safe_component(skill_name)}'",
                "    main_path = Path(__file__).resolve().parents[1] / 'main.py'",
                "    spec = importlib.util.spec_from_file_location(module_name, main_path)",
                "    module = importlib.util.module_from_spec(spec)",
                "    assert spec is not None and spec.loader is not None",
                "    spec.loader.exec_module(module)",
                "    return module",
                "",
                "",
                "class HabitPromotedCsvSkillTest(unittest.TestCase):",
                "    def test_aggregate_action(self):",
                "        module = _load_module()",
                "        result = module.run(",
                "            {'action': 'aggregate', 'path': 'sales.csv', 'aggregate': {'op': 'sum', 'column': 'amount'}},",
                "            {'sandbox': FakeSandbox()},",
                "        )",
                "        self.assertTrue(result['success'])",
                "        self.assertEqual(result['output']['result']['value'], 25.0)",
                "",
                "    def test_report_action(self):",
                "        module = _load_module()",
                "        sandbox = FakeSandbox()",
                "        result = module.run(",
                "            {",
                "                'action': 'report',",
                "                'path': 'sales.csv',",
                "                'metrics': [{'name': 'total_sales', 'op': 'sum', 'column': 'amount'}],",
                "                'output_path': 'reports/sales.md',",
                "            },",
                "            {'sandbox': sandbox},",
                "        )",
                "        self.assertTrue(result['success'])",
                "        self.assertIn('total_sales: 25.0', sandbox.files['reports/sales.md'])",
                "",
                "",
                "if __name__ == '__main__':",
                "    unittest.main()",
                "",
            ]
        )

    def _habit_sqlite_test(self, *, skill_name: str) -> str:
        return "\n".join(
            [
                "from __future__ import annotations",
                "",
                "import importlib.util",
                "import sqlite3",
                "import tempfile",
                "import unittest",
                "from pathlib import Path",
                "",
                "",
                "class FakeSandbox:",
                "    def __init__(self, root):",
                "        self.root = Path(root)",
                "",
                "    def resolve_path(self, path):",
                "        return self.root / path",
                "",
                "    def write_text(self, path, content, overwrite=False):",
                "        target = self.resolve_path(path)",
                "        target.parent.mkdir(parents=True, exist_ok=True)",
                "        if target.exists() and not overwrite:",
                "            raise ValueError(f'file already exists: {path}')",
                "        target.write_text(content, encoding='utf-8')",
                "        return target",
                "",
                "",
                "def _load_module():",
                f"    module_name = 'habit_promoted_sqlite_test_{_safe_component(skill_name)}'",
                "    main_path = Path(__file__).resolve().parents[1] / 'main.py'",
                "    spec = importlib.util.spec_from_file_location(module_name, main_path)",
                "    module = importlib.util.module_from_spec(spec)",
                "    assert spec is not None and spec.loader is not None",
                "    spec.loader.exec_module(module)",
                "    return module",
                "",
                "",
                "class HabitPromotedSqliteSkillTest(unittest.TestCase):",
                "    def _seed_db(self, root):",
                "        db_path = Path(root) / 'support.sqlite3'",
                "        conn = sqlite3.connect(db_path)",
                "        try:",
                "            conn.execute('create table tickets(team text, points integer)')",
                "            conn.executemany(",
                "                'insert into tickets(team, points) values (?, ?)',",
                "                [('alpha', 5), ('alpha', 7), ('beta', 3)],",
                "            )",
                "            conn.commit()",
                "        finally:",
                "            conn.close()",
                "",
                "    def test_query_action(self):",
                "        module = _load_module()",
                "        with tempfile.TemporaryDirectory() as root:",
                "            self._seed_db(root)",
                "            result = module.run(",
                "                {",
                "                    'action': 'query',",
                "                    'path': 'support.sqlite3',",
                "                    'sql': 'select sum(points) as total_points from tickets',",
                "                },",
                "                {'sandbox': FakeSandbox(root)},",
                "            )",
                "        self.assertTrue(result['success'])",
                "        self.assertEqual(result['output']['rows'][0]['total_points'], 15)",
                "",
                "    def test_report_action(self):",
                "        module = _load_module()",
                "        with tempfile.TemporaryDirectory() as root:",
                "            self._seed_db(root)",
                "            sandbox = FakeSandbox(root)",
                "            result = module.run(",
                "                {",
                "                    'action': 'report',",
                "                    'path': 'support.sqlite3',",
                "                    'queries': [",
                "                        {'label': 'ticket_count', 'sql': 'select count(*) as value from tickets'},",
                "                        {'label': 'total_points', 'sql': 'select sum(points) as value from tickets'},",
                "                    ],",
                "                    'output_path': 'reports/sqlite.md',",
                "                },",
                "                {'sandbox': sandbox},",
                "            )",
                "            report = (Path(root) / 'reports' / 'sqlite.md').read_text(encoding='utf-8')",
                "        self.assertTrue(result['success'])",
                "        self.assertIn('ticket_count: 3', report)",
                "        self.assertIn('total_points: 15', report)",
                "",
                "    def test_write_sql_is_rejected(self):",
                "        module = _load_module()",
                "        with tempfile.TemporaryDirectory() as root:",
                "            self._seed_db(root)",
                "            result = module.run(",
                "                {'action': 'query', 'path': 'support.sqlite3', 'sql': 'delete from tickets'},",
                "                {'sandbox': FakeSandbox(root)},",
                "            )",
                "        self.assertFalse(result['success'])",
                "        self.assertIn('read-only', result['message'])",
                "",
                "",
                "if __name__ == '__main__':",
                "    unittest.main()",
                "",
            ]
        )

    def _habit_config_test(self, *, skill_name: str) -> str:
        return "\n".join(
            [
                "from __future__ import annotations",
                "",
                "import importlib.util",
                "import unittest",
                "from pathlib import Path",
                "",
                "",
                "class FakeSandbox:",
                "    def __init__(self):",
                "        self.files = {",
                "            'service.toml': '[service]\\nname = \"billing\"\\nretries = 3\\n',",
                "            'service.yaml': 'service:\\n  name: billing\\n  retries: 3\\n',",
                "            'service.env': 'SERVICE_NAME=billing\\nRETRIES=3\\n',",
                "            'service.ini': '[service]\\nname=billing\\nretries=3\\n',",
                "            'service.properties': 'service.name=billing\\nservice.retries=3\\n',",
                "            'service.xml': '<service><name>billing</name><owner team=\"platform\">Maya</owner></service>',",
                "        }",
                "",
                "    def read_text(self, path):",
                "        return self.files[path]",
                "",
                "    def write_text(self, path, content, overwrite=False):",
                "        self.files[path] = content",
                "        return Path(path)",
                "",
                "",
                "def _load_module():",
                f"    module_name = 'habit_promoted_config_test_{_safe_component(skill_name)}'",
                "    main_path = Path(__file__).resolve().parents[1] / 'main.py'",
                "    spec = importlib.util.spec_from_file_location(module_name, main_path)",
                "    module = importlib.util.module_from_spec(spec)",
                "    assert spec is not None and spec.loader is not None",
                "    spec.loader.exec_module(module)",
                "    return module",
                "",
                "",
                "class HabitPromotedConfigSkillTest(unittest.TestCase):",
                "    def test_query_action(self):",
                "        module = _load_module()",
                "        result = module.run(",
                "            {'action': 'query', 'path': 'service.toml', 'query': 'service.name'},",
                "            {'sandbox': FakeSandbox()},",
                "        )",
                "        self.assertTrue(result['success'])",
                "        self.assertEqual(result['output']['value'], 'billing')",
                "",
                "    def test_yaml_query_action(self):",
                "        module = _load_module()",
                "        result = module.run(",
                "            {'action': 'query', 'path': 'service.yaml', 'query': 'service.name'},",
                "            {'sandbox': FakeSandbox()},",
                "        )",
                "        self.assertTrue(result['success'])",
                "        self.assertEqual(result['output']['format'], 'yaml')",
                "        self.assertEqual(result['output']['value'], 'billing')",
                "",
                "    def test_xml_query_action(self):",
                "        module = _load_module()",
                "        result = module.run(",
                "            {'action': 'query', 'path': 'service.xml', 'query': 'service.owner.@team'},",
                "            {'sandbox': FakeSandbox()},",
                "        )",
                "        self.assertTrue(result['success'])",
                "        self.assertEqual(result['output']['format'], 'xml')",
                "        self.assertEqual(result['output']['value'], 'platform')",
                "",
                "    def test_env_query_action(self):",
                "        module = _load_module()",
                "        result = module.run(",
                "            {'action': 'query', 'path': 'service.env', 'query': 'SERVICE_NAME'},",
                "            {'sandbox': FakeSandbox()},",
                "        )",
                "        self.assertTrue(result['success'])",
                "        self.assertEqual(result['output']['format'], 'keyvalue')",
                "        self.assertEqual(result['output']['value'], 'billing')",
                "",
                "    def test_ini_query_action(self):",
                "        module = _load_module()",
                "        result = module.run(",
                "            {'action': 'query', 'path': 'service.ini', 'query': 'service.name'},",
                "            {'sandbox': FakeSandbox()},",
                "        )",
                "        self.assertTrue(result['success'])",
                "        self.assertEqual(result['output']['format'], 'keyvalue')",
                "        self.assertEqual(result['output']['value'], 'billing')",
                "",
                "    def test_properties_query_action(self):",
                "        module = _load_module()",
                "        result = module.run(",
                "            {'action': 'query', 'path': 'service.properties', 'query': 'service.name'},",
                "            {'sandbox': FakeSandbox()},",
                "        )",
                "        self.assertTrue(result['success'])",
                "        self.assertEqual(result['output']['format'], 'keyvalue')",
                "        self.assertEqual(result['output']['value'], 'billing')",
                "",
                "    def test_report_action(self):",
                "        module = _load_module()",
                "        sandbox = FakeSandbox()",
                "        result = module.run(",
                "            {",
                "                'action': 'report',",
                "                'path': 'service.toml',",
                "                'fields': [{'label': 'service_name', 'query': 'service.name'}],",
                "                'output_path': 'reports/service.md',",
                "            },",
                "            {'sandbox': sandbox},",
                "        )",
                "        self.assertTrue(result['success'])",
                "        self.assertIn('service_name: billing', sandbox.files['reports/service.md'])",
                "",
                "",
                "if __name__ == '__main__':",
                "    unittest.main()",
                "",
            ]
        )

    def _habit_text_test(self, *, skill_name: str) -> str:
        return "\n".join(
            [
                "from __future__ import annotations",
                "",
                "import importlib.util",
                "import unittest",
                "from pathlib import Path",
                "",
                "",
                "class FakeSandbox:",
                "    def __init__(self):",
                "        self.files = {'notes.txt': 'alpha\\nERROR failed\\nerror retry\\n'}",
                "",
                "    def read_text(self, path):",
                "        return self.files[path]",
                "",
                "    def write_text(self, path, content, overwrite=False):",
                "        if path in self.files and not overwrite:",
                "            raise FileExistsError(path)",
                "        self.files[path] = content",
                "        return Path(path)",
                "",
                "",
                "def _load_module():",
                f"    module_name = 'habit_promoted_text_test_{_safe_component(skill_name)}'",
                "    main_path = Path(__file__).resolve().parents[1] / 'main.py'",
                "    spec = importlib.util.spec_from_file_location(module_name, main_path)",
                "    module = importlib.util.module_from_spec(spec)",
                "    assert spec is not None and spec.loader is not None",
                "    spec.loader.exec_module(module)",
                "    return module",
                "",
                "",
                "class HabitPromotedTextSkillTest(unittest.TestCase):",
                "    def test_extract_lines_action(self):",
                "        module = _load_module()",
                "        result = module.run(",
                "            {'action': 'extract_lines', 'path': 'notes.txt', 'contains': 'error', 'ignore_case': True},",
                "            {'sandbox': FakeSandbox()},",
                "        )",
                "        self.assertTrue(result['success'])",
                "        self.assertEqual(result['output']['lines'], ['ERROR failed', 'error retry'])",
                "",
                "    def test_replace_action(self):",
                "        module = _load_module()",
                "        sandbox = FakeSandbox()",
                "        result = module.run(",
                "            {'action': 'replace', 'path': 'notes.txt', 'old': 'ERROR', 'new': 'WARN'},",
                "            {'sandbox': sandbox},",
                "        )",
                "        self.assertTrue(result['success'])",
                "        self.assertEqual(result['output']['replacement_count'], 1)",
                "        self.assertIn('WARN failed', sandbox.files['notes.txt'])",
                "",
                "",
                "if __name__ == '__main__':",
                "    unittest.main()",
                "",
            ]
        )

    def _habit_markdown_test(self, *, skill_name: str) -> str:
        return "\n".join(
            [
                "from __future__ import annotations",
                "",
                "import importlib.util",
                "import unittest",
                "from pathlib import Path",
                "",
                "",
                "class FakeSandbox:",
                "    def __init__(self):",
                "        self.files = {",
                "            'docs/report.md': '# Report\\n\\n## Summary\\nOld summary\\n\\n| Name | Value |\\n| --- | --- |\\n| A | 1 |\\n'",
                "        }",
                "",
                "    def read_text(self, path):",
                "        return self.files[path]",
                "",
                "    def write_text(self, path, content, overwrite=False):",
                "        self.files[path] = content",
                "        return Path(path)",
                "",
                "",
                "def _load_module():",
                f"    module_name = 'habit_promoted_markdown_test_{_safe_component(skill_name)}'",
                "    main_path = Path(__file__).resolve().parents[1] / 'main.py'",
                "    spec = importlib.util.spec_from_file_location(module_name, main_path)",
                "    module = importlib.util.module_from_spec(spec)",
                "    assert spec is not None and spec.loader is not None",
                "    spec.loader.exec_module(module)",
                "    return module",
                "",
                "",
                "class HabitPromotedMarkdownSkillTest(unittest.TestCase):",
                "    def test_extract_section_action(self):",
                "        module = _load_module()",
                "        result = module.run(",
                "            {'action': 'extract_section', 'path': 'docs/report.md', 'heading': 'Summary'},",
                "            {'sandbox': FakeSandbox()},",
                "        )",
                "        self.assertTrue(result['success'])",
                "        self.assertIn('Old summary', result['output']['section'])",
                "",
                "    def test_replace_section_action(self):",
                "        module = _load_module()",
                "        sandbox = FakeSandbox()",
                "        result = module.run(",
                "            {",
                "                'action': 'replace_section',",
                "                'path': 'docs/report.md',",
                "                'heading': 'Summary',",
                "                'content': 'New summary',",
                "            },",
                "            {'sandbox': sandbox},",
                "        )",
                "        self.assertTrue(result['success'])",
                "        self.assertIn('New summary', sandbox.files['docs/report.md'])",
                "",
                "    def test_table_action(self):",
                "        module = _load_module()",
                "        result = module.run(",
                "            {'action': 'table', 'path': 'docs/report.md'},",
                "            {'sandbox': FakeSandbox()},",
                "        )",
                "        self.assertTrue(result['success'])",
                "        self.assertEqual(result['output']['rows'][0]['Name'], 'A')",
                "",
                "",
                "if __name__ == '__main__':",
                "    unittest.main()",
                "",
            ]
        )

    def _habit_html_test(self, *, skill_name: str) -> str:
        return "\n".join(
            [
                "from __future__ import annotations",
                "",
                "import importlib.util",
                "import unittest",
                "from pathlib import Path",
                "",
                "",
                "class FakeSandbox:",
                "    def __init__(self):",
                "        self.files = {",
                "            'inputs/page.html': '<html><head><title>Service Status</title></head><body><h1>OK</h1><a href=\"/docs\">Docs</a></body></html>'",
                "        }",
                "",
                "    def read_text(self, path):",
                "        return self.files[path]",
                "",
                "    def write_text(self, path, content, overwrite=False):",
                "        self.files[path] = content",
                "        return Path(path)",
                "",
                "",
                "def _load_module():",
                f"    module_name = 'habit_promoted_html_test_{_safe_component(skill_name)}'",
                "    main_path = Path(__file__).resolve().parents[1] / 'main.py'",
                "    spec = importlib.util.spec_from_file_location(module_name, main_path)",
                "    module = importlib.util.module_from_spec(spec)",
                "    assert spec is not None and spec.loader is not None",
                "    spec.loader.exec_module(module)",
                "    return module",
                "",
                "",
                "class HabitPromotedHtmlSkillTest(unittest.TestCase):",
                "    def test_extract_action(self):",
                "        module = _load_module()",
                "        result = module.run(",
                "            {'action': 'extract', 'path': 'inputs/page.html'},",
                "            {'sandbox': FakeSandbox()},",
                "        )",
                "        self.assertTrue(result['success'])",
                "        self.assertEqual(result['output']['title'], 'Service Status')",
                "        self.assertEqual(result['output']['links'][0]['href'], '/docs')",
                "",
                "    def test_links_action(self):",
                "        module = _load_module()",
                "        result = module.run(",
                "            {'action': 'links', 'path': 'inputs/page.html'},",
                "            {'sandbox': FakeSandbox()},",
                "        )",
                "        self.assertTrue(result['success'])",
                "        self.assertEqual(result['output']['links'][0]['text'], 'Docs')",
                "",
                "    def test_report_action(self):",
                "        module = _load_module()",
                "        sandbox = FakeSandbox()",
                "        result = module.run(",
                "            {",
                "                'action': 'report',",
                "                'path': 'inputs/page.html',",
                "                'output_path': 'reports/page_summary.md',",
                "            },",
                "            {'sandbox': sandbox},",
                "        )",
                "        self.assertTrue(result['success'])",
                "        self.assertIn('title: Service Status', sandbox.files['reports/page_summary.md'])",
                "        self.assertIn('link_count: 1', sandbox.files['reports/page_summary.md'])",
                "",
                "",
                "if __name__ == '__main__':",
                "    unittest.main()",
                "",
            ]
        )

    def _habit_archive_test(self, *, skill_name: str) -> str:
        return "\n".join(
            [
                "from __future__ import annotations",
                "",
                "import importlib.util",
                "import tarfile",
                "import tempfile",
                "import unittest",
                "import zipfile",
                "from pathlib import Path",
                "",
                "",
                "class FakeSandbox:",
                "    def __init__(self, root):",
                "        self.root = Path(root)",
                "",
                "    def resolve_path(self, path):",
                "        return (self.root / path).resolve()",
                "",
                "",
                "def _load_module():",
                f"    module_name = 'habit_promoted_archive_test_{_safe_component(skill_name)}'",
                "    main_path = Path(__file__).resolve().parents[1] / 'main.py'",
                "    spec = importlib.util.spec_from_file_location(module_name, main_path)",
                "    module = importlib.util.module_from_spec(spec)",
                "    assert spec is not None and spec.loader is not None",
                "    spec.loader.exec_module(module)",
                "    return module",
                "",
                "",
                "class HabitPromotedArchiveSkillTest(unittest.TestCase):",
                "    def test_create_and_list_archive_actions(self):",
                "        module = _load_module()",
                "        with tempfile.TemporaryDirectory() as root:",
                "            root_path = Path(root)",
                "            (root_path / 'inputs').mkdir(parents=True)",
                "            (root_path / 'inputs' / 'a.txt').write_text('alpha\\n', encoding='utf-8')",
                "            (root_path / 'inputs' / 'b.txt').write_text('beta\\n', encoding='utf-8')",
                "            sandbox = FakeSandbox(root)",
                "            create_result = module.run(",
                "                {",
                "                    'action': 'create',",
                "                    'paths': ['inputs'],",
                "                    'output_path': 'outputs/bundle.zip',",
                "                },",
                "                {'sandbox': sandbox},",
                "            )",
                "            list_result = module.run(",
                "                {'action': 'list', 'path': 'outputs/bundle.zip'},",
                "                {'sandbox': sandbox},",
                "            )",
                "            create_gztar_result = module.run(",
                "                {",
                "                    'action': 'create',",
                "                    'paths': ['inputs'],",
                "                    'output_path': 'outputs/bundle.tar.gz',",
                "                    'format': 'gztar',",
                "                },",
                "                {'sandbox': sandbox},",
                "            )",
                "            archive_exists = (root_path / 'outputs' / 'bundle.zip').exists()",
                "            with tarfile.open(root_path / 'outputs' / 'bundle.tar.gz', 'r:gz') as archive:",
                "                tar_names = set(archive.getnames())",
                "        self.assertTrue(create_result['success'])",
                "        self.assertTrue(create_gztar_result['success'])",
                "        self.assertTrue(archive_exists)",
                "        self.assertTrue(list_result['success'])",
                "        names = {item['name'] for item in list_result['output']['entries']}",
                "        self.assertEqual(names, {'inputs/a.txt', 'inputs/b.txt'})",
                "        self.assertEqual(tar_names, {'inputs/a.txt', 'inputs/b.txt'})",
                "",
                "    def test_extract_rejects_traversal_entry(self):",
                "        module = _load_module()",
                "        with tempfile.TemporaryDirectory() as root:",
                "            root_path = Path(root)",
                "            (root_path / 'inputs').mkdir(parents=True)",
                "            with zipfile.ZipFile(root_path / 'inputs' / 'unsafe.zip', 'w') as archive:",
                "                archive.writestr('../escape.txt', 'bad\\n')",
                "                archive.writestr('safe/readme.txt', 'ok\\n')",
                "            result = module.run(",
                "                {",
                "                    'action': 'extract',",
                "                    'path': 'inputs/unsafe.zip',",
                "                    'output_dir': 'extracted',",
                "                },",
                "                {'sandbox': FakeSandbox(root)},",
                "            )",
                "            escaped = (root_path / 'escape.txt').exists()",
                "            extracted_safe = (root_path / 'extracted' / 'safe' / 'readme.txt').exists()",
                "        self.assertFalse(result['success'])",
                "        self.assertIn('unsafe archive entry path', result['message'])",
                "        self.assertFalse(escaped)",
                "        self.assertFalse(extracted_safe)",
                "",
                "",
                "if __name__ == '__main__':",
                "    unittest.main()",
                "",
            ]
        )

    def _habit_datetime_test(self, *, skill_name: str) -> str:
        return "\n".join(
            [
                "from __future__ import annotations",
                "",
                "import importlib.util",
                "import unittest",
                "from pathlib import Path",
                "",
                "",
                "def _load_module():",
                f"    module_name = 'habit_promoted_datetime_test_{_safe_component(skill_name)}'",
                "    main_path = Path(__file__).resolve().parents[1] / 'main.py'",
                "    spec = importlib.util.spec_from_file_location(module_name, main_path)",
                "    module = importlib.util.module_from_spec(spec)",
                "    assert spec is not None and spec.loader is not None",
                "    spec.loader.exec_module(module)",
                "    return module",
                "",
                "",
                "class HabitPromotedDatetimeSkillTest(unittest.TestCase):",
                "    def test_format_action(self):",
                "        module = _load_module()",
                "        result = module.run(",
                "            {",
                "                'action': 'format',",
                "                'value': '2026-06-12T08:30:00+00:00',",
                "                'timezone': 'UTC',",
                "                'output_format': '%Y-%m-%d',",
                "            },",
                "            {},",
                "        )",
                "        self.assertTrue(result['success'])",
                "        self.assertEqual(result['output']['value'], '2026-06-12')",
                "",
                "    def test_diff_action(self):",
                "        module = _load_module()",
                "        result = module.run(",
                "            {",
                "                'action': 'diff',",
                "                'start': '2026-06-12T08:00:00+00:00',",
                "                'end': '2026-06-12T10:30:00+00:00',",
                "            },",
                "            {},",
                "        )",
                "        self.assertTrue(result['success'])",
                "        self.assertEqual(result['output']['diff']['seconds'], 9000.0)",
                "",
                "    def test_unknown_timezone_fails(self):",
                "        module = _load_module()",
                "        result = module.run(",
                "            {'action': 'now', 'timezone': 'No/SuchZone'},",
                "            {},",
                "        )",
                "        self.assertFalse(result['success'])",
                "        self.assertIn('unknown timezone', result['message'])",
                "",
                "",
                "if __name__ == '__main__':",
                "    unittest.main()",
                "",
            ]
        )

    def _habit_git_test(self, *, skill_name: str) -> str:
        module_name = f"habit_promoted_git_test_{_safe_component(skill_name)}"
        return r"""
from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path


class FakeSandbox:
    allow_external_commands = True

    def __init__(self, root):
        self.root = Path(root).resolve()

    def resolve_path(self, path):
        return (self.root / str(path)).resolve()


class NoExecSandbox(FakeSandbox):
    allow_external_commands = False


def _load_module():
    module_name = '__MODULE_NAME__'
    main_path = Path(__file__).resolve().parents[1] / 'main.py'
    spec = importlib.util.spec_from_file_location(module_name, main_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _git(root, *args):
    subprocess.run(
        ['git', *args],
        cwd=str(root),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _seed_repo(root):
    root = Path(root)
    _git(root, 'init')
    _git(root, 'config', 'user.email', 'test@example.com')
    _git(root, 'config', 'user.name', 'Test User')
    (root / 'tracked.txt').write_text('initial\n', encoding='utf-8')
    _git(root, 'add', 'tracked.txt')
    _git(root, 'commit', '-m', 'initial')
    (root / 'tracked.txt').write_text('changed\n', encoding='utf-8')


class HabitPromotedGitSkillTest(unittest.TestCase):
    def test_status_action(self):
        module = _load_module()
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            _seed_repo(root_path)
            result = module.run(
                {'action': 'status', 'cwd': '.'},
                {'sandbox': FakeSandbox(root_path)},
            )
        self.assertTrue(result['success'], result['message'])
        self.assertIn(' M tracked.txt', result['output']['stdout'])
        self.assertEqual(result['output']['argv'], ['git', 'status', '--short'])

    def test_log_action_is_bounded(self):
        module = _load_module()
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            _seed_repo(root_path)
            result = module.run(
                {'action': 'log', 'cwd': '.', 'max_count': 1},
                {'sandbox': FakeSandbox(root_path)},
            )
        self.assertTrue(result['success'], result['message'])
        self.assertIn('initial', result['output']['stdout'])

    def test_sandbox_without_process_exec_is_denied(self):
        module = _load_module()
        with tempfile.TemporaryDirectory() as root:
            result = module.run(
                {'action': 'status', 'cwd': '.'},
                {'sandbox': NoExecSandbox(root)},
            )
        self.assertFalse(result['success'])
        self.assertEqual(result['error_code'], 'permission_denied')


if __name__ == '__main__':
    unittest.main()
""".replace("__MODULE_NAME__", module_name).lstrip()

    def _habit_http_test(self, *, skill_name: str) -> str:
        module_name = f"habit_promoted_http_test_{_safe_component(skill_name)}"
        return r"""
from __future__ import annotations

import importlib.util
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class FakeSandbox:
    allow_network = True
    timeout_seconds = 5


class NoNetworkSandbox:
    allow_network = False
    timeout_seconds = 5


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        payload = {'message': 'ok', 'path': self.path}
        body = json.dumps(payload).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get('Content-Length', '0') or '0')
        raw_body = self.rfile.read(length).decode('utf-8')
        body = json.dumps({'received': raw_body}).encode('utf-8')
        self.send_response(201)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return None


def _load_module():
    module_name = '__MODULE_NAME__'
    main_path = Path(__file__).resolve().parents[1] / 'main.py'
    spec = importlib.util.spec_from_file_location(module_name, main_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ServerContext:
    def __enter__(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.url = f'http://{host}:{port}'
        return self.url

    def __exit__(self, exc_type, exc, tb):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class HabitPromotedHttpSkillTest(unittest.TestCase):
    def test_get_action(self):
        module = _load_module()
        with ServerContext() as url:
            result = module.run(
                {'action': 'get', 'url': f'{url}/data', 'params': {'q': 'search'}},
                {'sandbox': FakeSandbox()},
            )
        self.assertTrue(result['success'], result['message'])
        self.assertEqual(result['output']['status'], 200)
        self.assertEqual(result['output']['json']['message'], 'ok')
        self.assertIn('q=search', result['output']['request_url'])

    def test_post_action(self):
        module = _load_module()
        with ServerContext() as url:
            result = module.run(
                {'action': 'post', 'url': f'{url}/submit', 'json': {'name': 'Ada'}},
                {'sandbox': FakeSandbox()},
            )
        self.assertTrue(result['success'], result['message'])
        self.assertEqual(result['output']['status'], 201)
        self.assertIn('Ada', result['output']['json']['received'])

    def test_network_disabled_is_denied(self):
        module = _load_module()
        result = module.run(
            {'action': 'get', 'url': 'http://127.0.0.1:9/'},
            {'sandbox': NoNetworkSandbox()},
        )
        self.assertFalse(result['success'])
        self.assertEqual(result['error_code'], 'permission_denied')


if __name__ == '__main__':
    unittest.main()
""".replace("__MODULE_NAME__", module_name).lstrip()

    def _habit_browser_test(self, *, skill_name: str) -> str:
        module_name = f"habit_promoted_browser_test_{_safe_component(skill_name)}"
        return r"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


class FakeSandbox:
    allow_network = True

    def __init__(self, body='<html><body>weather: sunny</body></html>'):
        self.body = body

    def fetch_url(self, url):
        return {'url': url, 'request_url': url, 'status': 200, 'body': self.body, 'source': 'https'}


def _load_module():
    module_name = '__MODULE_NAME__'
    main_path = Path(__file__).resolve().parents[1] / 'main.py'
    spec = importlib.util.spec_from_file_location(module_name, main_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


class HabitPromotedBrowserSkillTest(unittest.TestCase):
    def test_fetch_action(self):
        module = _load_module()
        result = module.run(
            {'action': 'fetch', 'url': 'https://example.com/status', 'max_body_chars': 20},
            {'sandbox': FakeSandbox('<html><body>weather: sunny</body></html>')},
        )
        self.assertTrue(result['success'], result['message'])
        self.assertEqual(result['output']['status'], 200)
        self.assertEqual(result['output']['body'], '<html><body>weather:')
        self.assertTrue(result['output']['body_truncated'])
        self.assertFalse(result['output']['blocked'])

    def test_blocked_page_is_failed(self):
        module = _load_module()
        result = module.run(
            {'action': 'fetch', 'url': 'https://example.com/search'},
            {'sandbox': FakeSandbox('<html><title>百度安全验证</title></html>')},
        )
        self.assertFalse(result['success'])
        self.assertTrue(result['output']['blocked'])
        self.assertEqual(result['output']['blocked_reason'], 'access_verification_or_denial')


if __name__ == '__main__':
    unittest.main()
""".replace("__MODULE_NAME__", module_name).lstrip()

    def _habit_python_test(self, *, skill_name: str) -> str:
        module_name = f"habit_promoted_python_test_{_safe_component(skill_name)}"
        return r"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


class FakeSandbox:
    def run_python(self, code):
        self.code = code
        return {'ok': True, 'result': 42, 'stdout': 'hello\n', 'traceback': ''}


class FailingSandbox:
    def run_python(self, code):
        return {'ok': False, 'error': 'boom', 'traceback': 'trace', 'stdout': 'partial'}


def _load_module():
    module_name = '__MODULE_NAME__'
    main_path = Path(__file__).resolve().parents[1] / 'main.py'
    spec = importlib.util.spec_from_file_location(module_name, main_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


class HabitPromotedPythonSkillTest(unittest.TestCase):
    def test_ready_payload(self):
        module = _load_module()
        result = module.run({}, {'sandbox': FakeSandbox()})
        self.assertTrue(result['success'], result['message'])
        self.assertEqual(result['output']['supported_actions'], ['run'])

    def test_run_action(self):
        module = _load_module()
        sandbox = FakeSandbox()
        result = module.run(
            {'action': 'run', 'code': 'result = 42'},
            {'sandbox': sandbox},
        )
        self.assertTrue(result['success'], result['message'])
        self.assertEqual(sandbox.code, 'result = 42')
        self.assertEqual(result['output']['result'], 42)
        self.assertEqual(result['output']['stdout'], 'hello\n')

    def test_empty_code_is_rejected(self):
        module = _load_module()
        result = module.run(
            {'action': 'run', 'code': '  '},
            {'sandbox': FakeSandbox()},
        )
        self.assertFalse(result['success'])
        self.assertEqual(result['error_code'], 'validation_error')

    def test_sandbox_failure_is_reported(self):
        module = _load_module()
        result = module.run(
            {'action': 'run', 'code': 'raise RuntimeError("boom")'},
            {'sandbox': FailingSandbox()},
        )
        self.assertFalse(result['success'])
        self.assertEqual(result['error_code'], 'execution_error')
        self.assertEqual(result['output']['traceback'], 'trace')
        self.assertEqual(result['output']['stdout'], 'partial')


if __name__ == '__main__':
    unittest.main()
""".replace("__MODULE_NAME__", module_name).lstrip()

    def _habit_shell_test(self, *, skill_name: str) -> str:
        module_name = f"habit_promoted_shell_test_{_safe_component(skill_name)}"
        return r"""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


class FakeSandbox:
    allow_external_commands = True
    timeout_seconds = 5

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.runtime_root = self.root

    def resolve_path(self, path):
        return (self.root / str(path)).resolve()


class NoExecSandbox(FakeSandbox):
    allow_external_commands = False


def _load_module():
    module_name = '__MODULE_NAME__'
    main_path = Path(__file__).resolve().parents[1] / 'main.py'
    spec = importlib.util.spec_from_file_location(module_name, main_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


class HabitPromotedShellSkillTest(unittest.TestCase):
    def test_allowed_command_runs(self):
        module = _load_module()
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            (root_path / 'notes.txt').write_text('alpha\nbeta\n', encoding='utf-8')
            result = module.run(
                {'action': 'run', 'argv': ['wc', '-l', 'notes.txt'], 'cwd': '.'},
                {'sandbox': FakeSandbox(root_path)},
            )
        self.assertTrue(result['success'], result['message'])
        self.assertEqual(result['output']['argv'], ['wc', '-l', 'notes.txt'])
        self.assertIn('2', result['output']['stdout'])

    def test_non_whitelisted_command_is_rejected(self):
        module = _load_module()
        with tempfile.TemporaryDirectory() as root:
            result = module.run(
                {'action': 'run', 'argv': ['python', '-c', 'print(1)'], 'cwd': '.'},
                {'sandbox': FakeSandbox(root)},
            )
        self.assertFalse(result['success'])
        self.assertIn('not allowed', result['message'])

    def test_sandbox_without_process_exec_is_denied(self):
        module = _load_module()
        with tempfile.TemporaryDirectory() as root:
            result = module.run(
                {'action': 'run', 'argv': ['ls'], 'cwd': '.'},
                {'sandbox': NoExecSandbox(root)},
            )
        self.assertFalse(result['success'])
        self.assertEqual(result['error_code'], 'permission_denied')


if __name__ == '__main__':
    unittest.main()
""".replace("__MODULE_NAME__", module_name).lstrip()

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
        side_effects = {str(item) for item in manifest.get("side_effects", [])}
        source_relation = manifest.get("source_relation")
        if not isinstance(source_relation, dict):
            source_relation = {}
        source_risk = str(source_relation.get("risk_level", "low"))
        risk_level = "low"
        reasons: list[str] = []
        if source_risk in {"medium", "high"}:
            risk_level = source_risk
            reasons.append(f"source habit risk level is {source_risk}")
        elif (
            permissions & {"code_execution", "network", "system.exec", "dependency.install"}
            or side_effects & {"process_execution"}
            or str(manifest.get("sandbox_level", "workspace")) != "workspace"
        ):
            risk_level = "high"
            reasons.append("涉及代码执行、网络、系统执行或非 workspace sandbox")
        elif permissions & {"filesystem.delete"} or manifest.get("dependency_requirements"):
            risk_level = "medium"
            reasons.append("涉及删除权限或额外依赖")
        elif draft.source_kind not in {
            "workflow_compiled",
            "manual_import",
            "generated",
            "habit_promoted",
        }:
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
            "habit_promoted": "habit_promotion",
        }
        scope_map = {
            "workflow_compiled": "generated",
            "manual_import": "managed",
            "generated": "generated",
            "habit_promoted": "generated",
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

from __future__ import annotations

import unittest

from omniagent.models import PermissionLevel, TaskConstraints, TaskNode, TaskNodeType, TaskRequest
from omniagent.permissions import PermissionManager


class PermissionStaticAnalysisTest(unittest.TestCase):
    def test_python_identifier_containing_uv_does_not_imply_dependency_install(self) -> None:
        request = TaskRequest(
            goal="calculate revenue",
            constraints=TaskConstraints(max_permission=PermissionLevel.L3),
        )
        node = TaskNode(
            node_id="node_calculate",
            type=TaskNodeType.ACTION,
            goal="calculate revenue",
            executor_hint="python",
            input_payload={
                "code": "total_revenue = 70.0\nnorth_revenue = 20.0\nprint(total_revenue)"
            },
        )

        decision = PermissionManager().evaluate(request, node=node)

        self.assertEqual(decision.permission, "code_execution")
        self.assertEqual(decision.status, "granted")
        self.assertNotIn("dependency.install", decision.details["permissions"])

    def test_python_dependency_install_commands_still_require_l4(self) -> None:
        request = TaskRequest(
            goal="install dependency",
            constraints=TaskConstraints(max_permission=PermissionLevel.L4),
        )
        node = TaskNode(
            node_id="node_install",
            type=TaskNodeType.ACTION,
            goal="install dependency",
            executor_hint="python",
            input_payload={"code": "cmd = 'uv pip install demo-package'\nprint(cmd)"},
        )

        decision = PermissionManager().evaluate(request, node=node)

        self.assertEqual(decision.permission, "dependency.install")
        self.assertIn("dependency.install", decision.details["permissions"])


if __name__ == "__main__":
    unittest.main()

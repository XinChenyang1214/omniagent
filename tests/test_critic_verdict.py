from __future__ import annotations

import unittest

from omniagent.models import CriticAction, CriticVerdict


class CriticVerdictParsingTest(unittest.TestCase):
    def test_pass_is_coerced_to_accept(self) -> None:
        verdict = CriticVerdict.from_dict(
            {
                "target_type": "task",
                "target_id": "task_1",
                "action": "pass",
                "summary": "通过",
                "rationale": "任务成功完成",
            }
        )
        self.assertEqual(verdict.action, CriticAction.ACCEPT)

    def test_fallback_is_coerced_to_degrade(self) -> None:
        verdict = CriticVerdict.from_dict(
            {
                "target_type": "node",
                "target_id": "node_1",
                "action": "fallback",
                "summary": "降级",
                "rationale": "建议切换到更保守能力",
            }
        )
        self.assertEqual(verdict.action, CriticAction.DEGRADE)

    def test_unknown_action_falls_back_to_fail(self) -> None:
        verdict = CriticVerdict.from_dict(
            {
                "target_type": "node",
                "target_id": "node_2",
                "action": "ship_it",
                "summary": "无法识别",
                "rationale": "输出不符合约束",
            }
        )
        self.assertEqual(verdict.action, CriticAction.FAIL)


if __name__ == "__main__":
    unittest.main()

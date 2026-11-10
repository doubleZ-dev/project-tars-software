import unittest

from tars_companion.canon import CORE_IDENTITY, trusted_mission_context


class CanonMemoryTests(unittest.TestCase):
    def test_core_identity_is_mission_robot_not_chatbot(self):
        self.assertIn("前军用", CORE_IDENTITY)
        self.assertIn("不是对话机器人", CORE_IDENTITY)
        self.assertIn("诚实参数 95%", CORE_IDENTITY)
        self.assertIn("幽默参数 60%", CORE_IDENTITY)

    def test_mission_reason_retrieves_blight_and_nasa_plans(self):
        context = trusted_mission_context("为什么要执行这个任务，枯萎病发生了什么")
        for fact in ("枯萎病", "沙尘暴", "Plan A", "Plan B", "虫洞"):
            self.assertIn(fact, context)

    def test_detailed_history_contains_full_timeline_and_boundaries(self):
        context = trusted_mission_context("告诉我完整电影历史")
        for fact in ("Miller", "Mann", "Edmunds", "Gargantua", "Murph", "Cooper Station"):
            self.assertIn(fact, context)
        self.assertIn("没有展示他们已经与 Brand 重聚", context)

    def test_unrelated_chat_does_not_bloat_prompt(self):
        self.assertEqual(trusted_mission_context("给我来一个冷笑话"), "")


if __name__ == "__main__":
    unittest.main()

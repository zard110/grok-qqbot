import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.message_format import prepare, plain_text, may_fallback, split_content, FENCE, promote_mentions, markdown_mentions, has_mention


class FormatTests(unittest.TestCase):
    def test_text_mentions_are_sent_as_markdown_tags(self):
        a, b = "A" * 16 + "0" * 16, "b" * 16 + "1" * 16
        text = f"虚心求教 <@{a}> <qqbot-at-user id='{b}'/> 两位前辈\n1. 一"
        parts = promote_mentions(prepare(text, "plain")[0])
        self.assertEqual(parts, [{"msg_type": 2, "markdown": {"content":
            f'虚心求教 <qqbot-at-user id="{a}" /> <qqbot-at-user id="{b}" /> 两位前辈\n1. 一'}}])
        self.assertNotIn("<@", parts[0]["markdown"]["content"])
        self.assertEqual(promote_mentions([{"msg_type": 0, "content": "没有艾特 <@all>"}]),
                         [{"msg_type": 0, "content": "没有艾特 <@all>"}])
        self.assertEqual(markdown_mentions(f"<@!{a}>"), f'<qqbot-at-user id="{a}" />')
        self.assertTrue(has_mention(f'<qqbot-at-user id="{a}" />'))
        payload = {"msg_type": 2, "markdown": {"content": f'<qqbot-at-user id="{a}" /> hi'}}
        rejected = {"http_status": 400, "body": {"message": "markdown not supported"}}
        self.assertFalse(may_fallback(rejected, payload))

    def test_plain_default_and_auto_capability_gate(self):
        text = "## 结论\n\n- 一\n- 二"
        self.assertEqual(prepare(text, "plain")[0][0]["content"], "结论\n\n- 一\n- 二")
        self.assertNotIn("##", prepare(text, "plain", mode="auto")[0][0]["content"])
        self.assertEqual(prepare(text, "plain", {"message_format": {"markdown_verified": True}}, mode="auto")[0][0]["msg_type"], 2)

    def test_legacy_plain_cleans_tables_code_and_decorations(self):
        cases = [
            ("| **方案** | 评分 |\n|---|---|\n| A | **5** |", "方案：A；评分：5"),
            ("```python\nprint('**原样代码**')\n```", "代码（python）：\n    print('**原样代码**')"),
            ("## 结论\n**优秀**，__推荐__", "结论\n优秀，推荐"),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                payload = prepare(text, "plain")[0][0]
                self.assertEqual(payload["msg_type"], 0)
                self.assertEqual(payload["content"], expected)
        text = "  普通消息：a ** b，a | b，#话题，snake__name  "
        self.assertEqual(prepare(text, "plain")[0][0]["content"], text)
        self.assertEqual(prepare("**加粗**", "markdown")[0][0]["msg_type"], 2)

    def test_table_and_code_require_separate_verification(self):
        table = "| 方案 | 成本 |\n| --- | --- |\n| A | 20 |\n| B | 30 |"
        cfg = {"message_format": {"markdown_verified": True}}
        parts, _ = prepare(table, "markdown", cfg, mode="auto")
        self.assertEqual(parts[0]["msg_type"], 0)
        self.assertIn("方案：A；成本：20", parts[0]["content"])
        code = "```python\nprint('**literal**')\n```"
        self.assertIn("print('**literal**')", plain_text(code))
        self.assertEqual(prepare(code, "markdown", cfg, mode="auto")[0][0]["msg_type"], 0)
        # Explicit legacy Markdown remains available after an operator verifies it.
        self.assertEqual(prepare(table, "markdown")[0][0]["msg_type"], 2)

    def test_long_fences_are_closed_and_reopened_with_matching_marker(self):
        for fence in ("```", "~~~"):
            text = fence + "python\n" + "print('value')\n" * 20 + fence
            chunks = split_content(text, 160, markdown=True)
            self.assertGreater(len(chunks), 1)
            self.assertTrue(all(len(chunk) <= 160 for chunk in chunks))
            for chunk in chunks:
                markers = [FENCE.match(line)[1] for line in chunk.splitlines() if FENCE.match(line)]
                self.assertEqual(markers, [fence, fence])
            self.assertEqual(sum(chunk.count("print('value')") for chunk in chunks), 20)

    def test_split_never_truncates_and_rejects_too_many_parts(self):
        text = "abcd" * 100
        parts, _ = prepare(text, "plain", max_chars=180, max_parts=5)
        recovered = "".join(part["content"].split("\n", 1)[1] for part in parts)
        self.assertEqual(recovered, text)
        with self.assertRaises(ValueError):
            prepare(text, "plain", max_chars=120, max_parts=1)

    def test_fallback_only_for_definite_format_rejection(self):
        payload = {"msg_type": 2, "markdown": {"content": "## hello"}}
        rejection = {"http_status": 400, "body": {"message": "markdown not supported"}}
        self.assertTrue(may_fallback(rejection, payload))
        for status in (200, 401, 403, 408, 429, 500, 503):
            self.assertFalse(may_fallback({**rejection, "http_status": status}, payload))
        self.assertFalse(may_fallback(rejection, {**payload, "keyboard": {"id": "x"}}))
        self.assertFalse(may_fallback(rejection, {"msg_type": 2, "markdown": {"content": "![image](source)"}}))

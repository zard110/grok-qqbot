"""Offline tests for the ppt_poster plugin and core/sensenova.py (all HTTP is mocked)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requests

import groups as G
from core import action_runner
from core import sensenova as S
from core.plugins import PluginRegistry
from test_groups import A, GroupFixture


def response(status, body=None, text=""):
    r = MagicMock()
    r.status_code = status
    r.text = text or json.dumps(body or {})
    r.json.return_value = body or {}
    return r


class TitleTests(unittest.TestCase):
    def test_clean_title_rejects_generic_and_strips_unsafe_characters(self):
        for generic in ("deck", "PPT", "演示文稿", "  海报 ", "slides - pdf", "", None):
            self.assertEqual(S.clean_title(generic), "", generic)
        self.assertEqual(S.clean_title('乐扣/低代码:平台*介绍?'), "乐扣 低代码 平台 介绍")
        self.assertEqual(S.clean_title("<b>周六&amp;夜骑</b>"), "周六&夜骑")
        self.assertEqual(len(S.clean_title("长" * 80)), 30)

    def test_extract_title_and_cover_heading(self):
        self.assertEqual(S.extract_title("FILE_TITLE: 「乐扣低代码平台产品介绍」\nIMAGE_PROMPTS: []"), "乐扣低代码平台产品介绍")
        self.assertEqual(S.extract_title("FILE_TITLE: deck"), "")
        html = ('<section class="slide"><div class="cover-title">让开发 更快</div></section>'
                '<section class="slide"><h1>第二页</h1></section>')
        self.assertEqual(S.heading_title(html), "让开发更快")

    def test_pick_title_fallback_order(self):
        html = "<section class='slide'><h1>封面主标题</h1></section>"
        self.assertEqual(S.pick_title("指定名字", "模型标题", html, "主题", "ppt"), "指定名字")
        self.assertEqual(S.pick_title("deck", "模型标题", html, "主题", "ppt"), "模型标题")
        self.assertEqual(S.pick_title(None, "", html, "主题", "ppt"), "封面主标题")
        self.assertEqual(S.pick_title(None, "", "<div></div>", "骑行周报", "ppt"), "骑行周报")
        self.assertTrue(S.pick_title(None, "", "", "PPT", "poster").startswith("海报-"))


class FitTests(unittest.TestCase):
    def test_overlap_threshold_is_injected_and_parsed(self):
        page = S.prepare("<html><head></head><body><div class='slide'>x {{IMG1}} {{IMG9}}</div></body></html>",
                         1280, 720, ".slide", {"IMG1": "img1.jpg"})
        self.assertEqual(S.OVERLAP_RATIO, 0.25)
        self.assertIn("w*h/m>0.25", page)
        self.assertIn("img1.jpg", page)
        self.assertNotIn("{{IMG", page)
        self.assertFalse(S.needs_fix("FIT z=1.000 raw_overflow=1.00 left=1.00 overlap=0"))
        self.assertTrue(S.needs_fix("FIT z=1.000 raw_overflow=1.00 left=1.00 overlap=2 | a~b"))
        self.assertTrue(S.needs_fix("FIT z=0.600 raw_overflow=1.40 left=1.05 overlap=0"))
        self.assertFalse(S.needs_fix(""))


class HttpTests(unittest.TestCase):
    def setUp(self):
        for target in (patch.object(S, "_key", return_value="fake"), patch.object(S.time, "sleep")):
            target.start()
            self.addCleanup(target.stop)

    def test_quota_and_auth_map_to_exit_codes(self):
        cases = [(response(429, text="rate limit"), 3, "quota"), (response(400, text="积分不足"), 3, "quota"),
                 (response(401, text="bad"), 4, "auth"), (response(403, text="no"), 4, "auth"),
                 (response(400, text="bad request"), 2, "api")]
        for reply, code, kind in cases:
            with patch.object(S.requests, "post", return_value=reply):
                with self.assertRaises(S.MakeError) as caught:
                    S._post("/chat/completions", {}, 5)
            self.assertEqual((caught.exception.code, caught.exception.kind), (code, kind))

    def test_one_retry_on_timeout_then_success(self):
        ok = response(200, {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
                            "usage": {"total_tokens": 7}})
        with patch.object(S.requests, "post", side_effect=[requests.Timeout(), ok]) as post:
            self.assertEqual(S.chat("s", "u"), "hi")
        self.assertEqual(post.call_count, 2)
        headers = post.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer fake")
        with patch.object(S.requests, "post", side_effect=requests.ConnectionError()):
            with self.assertRaises(S.MakeError) as caught:
                S.chat("s", "u")
        self.assertEqual(caught.exception.kind, "network")

    def test_content_filter_is_reported(self):
        blocked = response(200, {"choices": [{"message": {"content": "x"}, "finish_reason": "content_filter"}]})
        with patch.object(S.requests, "post", return_value=blocked):
            with self.assertRaises(S.MakeError) as caught:
                S.chat("s", "u")
        self.assertEqual(caught.exception.kind, "content_filter")


class KeyAndCliTests(unittest.TestCase):
    def test_missing_key_is_auth_error(self):
        with tempfile.TemporaryDirectory() as d, patch.object(S, "BOT_DIR", Path(d)), \
                patch.dict(os.environ, {"SENSENOVA_API_KEY": ""}):
            with self.assertRaises(S.MakeError) as caught:
                S._key()
        self.assertEqual(caught.exception.code, 4)

    def test_cli_exit_codes_json_and_result_file(self):
        with tempfile.TemporaryDirectory() as d:
            for error, code in ((S.MakeError("q", "quota", 3), 3), (S.MakeError("k", "auth", 4), 4),
                                (RuntimeError("boom"), 2)):
                with patch.object(S, "chat", side_effect=error), \
                        patch("builtins.print") as printed:
                    self.assertEqual(S.main(["ppt", "--topic", "测试", "--out", d]), code)
                out = json.loads(printed.call_args.args[0])
                self.assertFalse(out["ok"])
                self.assertEqual(out["exit_code"], code)
                self.assertEqual(json.loads((Path(d) / "result.json").read_text(encoding="utf-8"))["exit_code"], code)

    def test_ppt_outputs_are_named_after_the_title(self):
        reply = ('FILE_TITLE: 乐扣低代码平台产品介绍\nIMAGE_PROMPTS: []\n```html\n<html><head><style></style></head><body>'
                 '<section class="slide"><h1>封面</h1></section><section class="slide"><h2>二</h2></section>'
                 '</body></html>\n```')

        def fake_render(jobs):
            from PIL import Image
            for _, png, w, h in jobs:
                Image.new("RGB", (w, h), (200, 30, 30)).save(png)
            return ["FIT z=1.000 raw_overflow=1.00 left=1.00 overlap=0"] * len(jobs)

        with tempfile.TemporaryDirectory() as d, patch.object(S, "chat", return_value=reply), \
                patch.object(S, "render_many", side_effect=fake_render), patch("builtins.print") as printed:
            self.assertEqual(S.main(["ppt", "--topic", "乐扣", "--slides", "2", "--images", "0", "--out", d]), 0)
            out = json.loads(printed.call_args.args[0])
            self.assertEqual(out["title"], "乐扣低代码平台产品介绍")
            for key, suffix in (("pptx", ".pptx"), ("pdf", ".pdf"), ("long_image", "-长图.jpg")):
                self.assertEqual(Path(out[key]).name, "乐扣低代码平台产品介绍" + suffix)
                self.assertTrue(Path(out[key]).exists())
            self.assertEqual(out["slide_count"], 2)
            # rerender --name renames deliverables without API calls and removes the old names
            with patch.object(S, "chat", side_effect=AssertionError("no API in rerender")):
                self.assertEqual(S.main(["rerender", d, "--name", "新名字"]), 0)
            names = sorted(p.name for p in Path(d).iterdir())
            self.assertIn("新名字.pptx", names)
            self.assertNotIn("乐扣低代码平台产品介绍.pptx", names)


class PluginTests(GroupFixture):
    def enable(self, **binding):
        G.update_config(A, features={**G.load_config(A)["features"], "ppt_poster": True})
        G.update_plugin_binding(A, "ppt_poster", {"enabled": True, **binding})

    def dispatch(self, text, at_me=True, msg_id="m1"):
        registry = PluginRegistry()
        self.addCleanup(registry.stop)
        return registry.dispatch({"group_openid": A, "post_type": "message", "at_me": at_me,
                                  "text": text, "msg_id": msg_id})

    def test_disabled_by_default_and_falls_through_to_reply(self):
        self.assertFalse(G.plugin_enabled(A, "ppt_poster"))
        result = self.dispatch("/海报 周六夜骑")
        self.assertEqual([(a["type"], a["plugin_id"]) for a in result["actions"]], [("routine.reply", "reply")])

    def test_commands_become_scoped_actions(self):
        self.enable(config={"max_slides": 8})
        poster = self.dispatch("/海报 周六夜骑团骑")["actions"][0]
        self.assertEqual((poster["type"], poster["prompt"], poster["plugin_id"]), ("media.poster", "周六夜骑团骑", "ppt_poster"))
        ppt = self.dispatch("/PPT 乐扣低代码平台介绍 10页", msg_id="m2")["actions"][0]
        self.assertEqual((ppt["type"], ppt["topic"], ppt["slides"]), ("media.ppt", "乐扣低代码平台介绍", 8))
        self.assertEqual(self.dispatch("/ppt", msg_id="m3")["actions"][0]["type"], "media.usage")
        # not @: no paid job, normal routing continues
        natural = self.dispatch("/海报 xx", at_me=False, msg_id="m4")
        self.assertNotIn("media.poster", [a["type"] for a in natural["actions"]])

    def test_quota_limits_paid_jobs(self):
        self.enable(quota={"max_actions_per_day": 1})
        self.assertEqual(self.dispatch("/海报 一")["actions"][0]["type"], "media.poster")
        self.assertEqual(self.dispatch("/海报 二", msg_id="m2")["errors"], ["quota_exceeded"])

    def test_action_runner_starts_one_detached_job_per_event(self):
        self.enable()
        event = {"group_openid": A, "post_type": "message", "at_me": True, "text": "/海报 周六", "msg_id": "job1"}
        actions = PluginRegistry().dispatch(event)["actions"]
        with tempfile.TemporaryDirectory() as d, patch.object(S, "DEFAULT_OUT", Path(d)), \
                patch.object(S.subprocess, "Popen") as popen:
            popen.return_value.pid = 4242
            first = action_runner.run_actions(event, actions)[0]["result"]
            second = action_runner.run_actions(event, actions)[0]["result"]
            self.assertEqual(first["status"], "started")
            self.assertEqual(second["status"], "running")
            self.assertTrue(second["duplicate"])
            self.assertEqual(popen.call_count, 1)
            argv = popen.call_args.args[0]
            self.assertEqual(argv[2:4], ["poster", "--out"])
            self.assertIn("周六", argv)
            self.assertTrue(first["result_file"].endswith("result.json"))


if __name__ == "__main__":
    unittest.main()

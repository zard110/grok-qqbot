import concurrent.futures
import unittest
from test_groups import A, B, GroupFixture
import groups as G
from core.plugins import HookResult, Plugin, PluginRegistry, reserve_quota


class PluginTests(GroupFixture):
    def test_command_has_one_owner_and_rejects_unauthorized_write(self):
        registry = PluginRegistry()
        self.addCleanup(registry.stop)
        event = {"group_openid": A, "post_type": "message", "at_me": True, "text": "/记忆", "msg_id": "one"}
        result = registry.dispatch(event)
        self.assertEqual([a["type"] for a in result["actions"]], ["memory.list"])
        rejected = registry.dispatch({**event, "text": "/记忆 private fact", "member_openid": "user"})
        self.assertTrue(rejected["handled"])
        self.assertEqual(rejected["actions"], [])
        G.update_config(A, admins=["user"])
        accepted = registry.dispatch({**event, "text": "/记忆 fact", "member_openid": "user"})
        self.assertEqual(accepted["actions"][0]["type"], "memory.add")

    def test_disabled_command_does_not_fall_through_to_llm(self):
        registry = PluginRegistry()
        G.set_plugin_enabled(A, "command", False)
        result = registry.dispatch({"group_openid": A, "post_type": "message", "at_me": True, "text": "/记忆 fact"})
        self.assertEqual(result["actions"], [])
        self.assertEqual(result["errors"], ["plugin_disabled"])

    def test_hot_configuration_and_code_reload_run_lifecycle(self):
        events = []
        class Recorder(Plugin):
            id, priority = "recorder", 1
            def start(self, context):
                super().start(context)
                events.append(("start", context.group_openid, context.revision))
            def stop(self):
                events.append(("stop", self.context.group_openid, self.context.revision))
            def on_event(self, event):
                return HookResult([{"type": "record"}], True)
        registry = PluginRegistry(builtins=False)
        registry.register(Recorder)
        G.set_plugin_enabled(A, "recorder", True)
        G.set_plugin_enabled(B, "recorder", True)
        registry.dispatch({"group_openid": A})
        registry.dispatch({"group_openid": B})
        G.update_config(A, style="new")
        registry.dispatch({"group_openid": A})
        registry.reload("recorder", Recorder)
        self.assertEqual(sum(e[0] == "start" for e in events), 3)
        self.assertEqual(sum(e[0] == "stop" for e in events), 3)
        registry.dispatch({"group_openid": A})
        G.update_config(A, enabled=False)
        registry.dispatch({"group_openid": A})
        self.assertEqual(sum(e[0] == "stop" for e in events), 4)

    def test_quota_is_atomic_and_group_scoped(self):
        G.update_config(A, quotas={"max_actions_per_day": 3})
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda i: reserve_quota(A, "command", str(i)), range(12)))
        self.assertEqual(sum(results), 3)
        self.assertTrue(reserve_quota(B, "command", "other"))
        accepted_id = next(str(i) for i, accepted in enumerate(results) if accepted)
        self.assertTrue(reserve_quota(A, "command", accepted_id))


if __name__ == "__main__":
    unittest.main()

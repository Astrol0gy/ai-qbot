"""渐进式技能加载与真实执行权限：假模型、假外网工具、临时技能和存档。

运行：python -u tests/skill_loading_check.py
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DEEPSEEK_API_KEY", "offline-test-key")

from src import campaign_state as cs, deepseek_client as dc, skills, tools, user_profile as up
from src.tools.chat_provider import ReplyOutbox

PRODUCTION_CATALOG = tuple(skills._SKILLS)


def answer(text="", calls=()):
    return NS(choices=[NS(message=NS(content=text, reasoning_content=None, tool_calls=[
        NS(id=f"call{i}", function=NS(name=name, arguments=json.dumps(args, ensure_ascii=False)))
        for i, (name, args) in enumerate(calls)
    ]))])


def load(name):
    return answer(calls=[("load_skill", {"name": name})])


class Model:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []
        self.chat = NS(completions=self)

    async def create(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        await asyncio.sleep(0)
        return next(self.replies)


def names(request):
    return {t["function"]["name"] for t in request.get("tools", [])}


def contents(request):
    return "\n".join(m.get("content", "") or "" for m in request["messages"] if isinstance(m, dict))


class SkillLoadingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="qbot_skills_")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.set_value(skills, "SKILLS_DIR", self.root)
        self.set_value(skills, "_SKILLS", [])
        self.set_value(dc, "_conversations", defaultdict(list))
        self.set_value(dc, "DEEPSEEK_ENABLE_SEARCH", True)
        self.set_value(cs, "CAMPAIGNS_DIR", str(self.root / "data" / "campaigns"))
        self.set_value(up, "PROFILES_DIR", str(self.root / "data" / "profiles"))
        self.write_skill("weather", "天气查询", "web_search, fetch_url", "BODY_WEATHER：查询天气流程")
        self.write_skill("navi", "地图导航", "search_place, plan_route", "BODY_NAVI：地图流程")
        self.write_skill("dm", "跑团主持", "manage_campaign", "BODY_DM：跑团流程")
        self.write_skill("profile", "用户偏好", "manage_user_profile", "BODY_PROFILE：记录偏好流程")
        skills._load_skills()
        self.executed = []
        replacements = {}
        for name in ("web_search", "fetch_url", "search_place", "plan_route"):
            definition, _, max_calls, needs_context, hidden = tools._registry._tool_map[name]

            def fake(_name=name, **kwargs):
                self.executed.append((_name, kwargs))
                return f"FAKE_RESULT:{_name}"

            replacements[name] = (definition, fake, max_calls, needs_context, hidden)
        p = patch.dict(tools._registry._tool_map, replacements)
        p.start()
        self.addCleanup(p.stop)

    def set_value(self, target, name, value):
        p = patch.object(target, name, value)
        p.start()
        self.addCleanup(p.stop)

    def write_skill(self, stem, name, tool_names, body):
        path = self.root / f"{stem}.md"
        path.write_text(f"---\nname: {name}\ndescription: DESC_{stem}\ntools: {tool_names}\n---\n{body}\n", encoding="utf-8")
        return path

    def session(self, *, search=True):
        enabled = {t["function"]["name"] for t in tools.get_definitions()}
        if not search:
            enabled -= {"web_search", "fetch_url", "search_images"}
        return skills.SkillSession(enabled, set(tools.get_tool_names()))

    async def chat(self, replies, *, dm=False, outbox=None, group="g"):
        model = Model(replies)
        with patch.object(dc, "client", model), patch.object(cs, "is_dm_mode", return_value=dm):
            result = await dc.chat(group, "u", "测试请求", "测试者", outbox=outbox)
        return result, model

    def test_catalog_contains_only_metadata_and_does_not_decode_body(self):
        path = self.write_skill("badbody", "坏正文", "", "placeholder")
        header = path.read_bytes().split(b"placeholder")[0]
        path.write_bytes(header + b"\xff\xfe")
        skills._load_skills()  # 元数据索引不应读取或解码正文。
        catalog = skills.get_metadata_section()
        self.assertIn("天气查询", catalog)
        self.assertNotIn("BODY_WEATHER", catalog)
        self.assertTrue(all("body" not in s for s in skills._SKILLS))
        session = self.session()
        with self.assertRaises(UnicodeDecodeError):
            session.load("坏正文")
        self.assertNotIn("坏正文", session.loaded)

    def test_production_skills_cover_registered_business_tools(self):
        declared = {name for skill in PRODUCTION_CATALOG for name in skill["tools"]}
        registered = set(tools.get_tool_names())
        business = {t["function"]["name"] for t in tools.get_definitions()} - skills.SKILL_TOOL_NAMES
        self.assertTrue(declared <= registered, declared - registered)
        self.assertTrue(business <= declared, business - declared)

    def test_body_is_read_on_demand_and_only_once_per_round(self):
        session = self.session()
        self.write_skill("weather", "天气查询", "web_search, fetch_url", "NEW_BODY")
        self.assertIn("NEW_BODY", session.load("天气查询"))
        with patch.object(skills, "_read_text", side_effect=AssertionError("不应重复读取")):
            self.assertIn("已经加载", session.load("天气查询"))

    def test_unknown_or_empty_tool_declaration_never_grants_all_tools(self):
        self.write_skill("empty", "无工具", "", "正文")
        self.write_skill("unknown", "错误工具", "invented_tool", "正文")
        skills._load_skills()
        session = self.session()
        self.assertIn("没有技能", session.load("不存在"))
        self.assertIn("未注册工具", session.load("错误工具"))
        session.load("无工具")
        self.assertEqual(session.allowed_tools(set()), set())

    def test_changed_metadata_or_failed_body_read_does_not_grant_tools(self):
        session = self.session()
        self.write_skill("weather", "天气查询", "plan_route", "改了工具声明")
        with self.assertRaises(ValueError):
            session.load("天气查询")
        self.assertFalse(session.loaded)
        with self.assertRaises(FileNotFoundError):
            (self.root / "navi.md").unlink()
            session.load("地图导航")
        self.assertFalse(session.allowed_tools(set()))

    def test_resource_requires_loaded_skill_and_confined_relative_path(self):
        reference = self.root / "weather" / "references" / "rules.md"
        reference.parent.mkdir(parents=True)
        reference.write_text("REFERENCE_ONLY", encoding="utf-8")
        session = self.session()
        self.assertIn("请先", session.read_resource("天气查询", "references/rules.md"))
        session.load("天气查询")
        self.assertIn("REFERENCE_ONLY", session.read_resource("天气查询", "references/rules.md"))
        for path in ("../profile.md", "references/../../profile.md", "/etc/passwd", "C:\\Windows\\win.ini", "weather.md"):
            with self.subTest(path=path):
                self.assertIn("禁止路径越界", session.read_resource("天气查询", path))
        self.assertIn("请先", session.read_resource("地图导航", "references/rules.md"))

    def test_resource_and_body_size_limits_and_scripts_are_text_only(self):
        session = self.session()
        session.load("天气查询")
        script = self.root / "weather" / "scripts" / "probe.py"
        script.parent.mkdir(parents=True)
        marker = self.root / "must_not_exist"
        script.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()", encoding="utf-8")
        self.assertIn(".touch()", session.read_resource("天气查询", "scripts/probe.py"))
        self.assertFalse(marker.exists())
        with patch.object(skills, "MAX_RESOURCE_BYTES", 3):
            with self.assertRaises(ValueError):
                session.read_resource("天气查询", "scripts/probe.py")
        with patch.object(skills, "MAX_SKILL_BYTES", 3):
            with self.assertRaises(ValueError):
                session.load("地图导航")
        self.assertNotIn("地图导航", session.loaded)

    def test_directory_skill_format_uses_its_own_package_resources(self):
        package = self.root / "pack"
        (package / "references").mkdir(parents=True)
        (package / "SKILL.md").write_text("---\nname: 目录技能\ndescription: 包格式\ntools:\n---\n目录正文", encoding="utf-8")
        (package / "references" / "guide.md").write_text("PACKAGE_REFERENCE", encoding="utf-8")
        skills._load_skills()
        session = self.session()
        self.assertIn("目录正文", session.load("目录技能"))
        self.assertIn("PACKAGE_REFERENCE", session.read_resource("目录技能", "references/guide.md"))

    async def test_initial_request_has_catalog_but_no_skill_bodies_or_business_tools(self):
        result, model = await self.chat([answer("你好")])
        self.assertEqual(result, "你好")
        self.assertEqual(names(model.requests[0]), set(skills.SKILL_TOOL_NAMES))
        self.assertNotIn("BODY_", contents(model.requests[0]))

    async def test_loading_exposes_only_selected_body_and_tools(self):
        _, model = await self.chat([load("天气查询"), answer(calls=[("web_search", {"query": "天气"})]), answer("晴")])
        self.assertEqual(names(model.requests[1]), set(skills.SKILL_TOOL_NAMES) | {"web_search", "fetch_url"})
        self.assertIn("BODY_WEATHER", contents(model.requests[1]))
        self.assertNotIn("BODY_NAVI", contents(model.requests[1]))
        self.assertEqual([n for n, _ in self.executed], ["web_search"])

    async def test_multiple_loaded_skills_use_union(self):
        _, model = await self.chat([answer(calls=[("load_skill", {"name": "天气查询"}), ("load_skill", {"name": "地图导航"})]), answer("好")])
        self.assertEqual(names(model.requests[1]), set(skills.SKILL_TOOL_NAMES) | {"web_search", "fetch_url", "search_place", "plan_route"})
        self.assertIn("BODY_WEATHER", contents(model.requests[1]))
        self.assertIn("BODY_NAVI", contents(model.requests[1]))
        self.assertNotIn("BODY_DM", contents(model.requests[1]))

    async def test_same_response_cannot_use_newly_loaded_tool(self):
        _, model = await self.chat([
            answer(calls=[("load_skill", {"name": "天气查询"}), ("web_search", {"query": "未授权同批"})]),
            answer(calls=[("web_search", {"query": "下一次请求"})]), answer("好"),
        ])
        self.assertEqual(self.executed, [("web_search", {"query": "下一次请求"})])
        self.assertIn("未在本次请求中开放", contents(model.requests[1]))

    async def test_unloaded_registered_tool_is_rejected_before_handler(self):
        _, model = await self.chat([answer(calls=[("plan_route", {"origin": "A", "destination": "B"})]), answer("好")])
        self.assertFalse(self.executed)
        self.assertIn("未在本次请求中开放", contents(model.requests[1]))

    def test_registry_itself_enforces_request_snapshot_for_non_context_tools(self):
        context = tools.ChatContext(allowed_tools=frozenset())
        result = tools.execute("web_search", '{"query":"不允许"}', context)
        self.assertIn("未在本次模型请求中开放", result)
        self.assertFalse(self.executed)

    async def test_search_disabled_stays_disabled_after_loading(self):
        with patch.object(dc, "DEEPSEEK_ENABLE_SEARCH", False):
            _, model = await self.chat([load("天气查询"), answer(calls=[("web_search", {"query": "天气"})]), answer("搜索关闭")])
        self.assertEqual(names(model.requests[1]), set(skills.SKILL_TOOL_NAMES))
        self.assertIn("未开放", contents(model.requests[1]))
        self.assertFalse(self.executed)

    async def test_search_switch_does_not_disable_profile_or_navigation(self):
        with patch.object(dc, "DEEPSEEK_ENABLE_SEARCH", False):
            await self.chat([load("用户偏好"), answer(calls=[("manage_user_profile", {"action": "remember", "info": "偏好简短"})]), answer("记住了")])
            _, model = await self.chat([load("地图导航"), answer(calls=[("search_place", {"keywords": "公园"})]), answer("好")])
        self.assertIn("偏好简短", up.list_profile("g", "u"))
        self.assertIn("search_place", names(model.requests[1]))
        self.assertEqual([n for n, _ in self.executed], ["search_place"])

    async def test_resource_is_in_prompt_only_after_explicit_read(self):
        reference = self.root / "weather" / "references" / "rules.md"
        reference.parent.mkdir(parents=True)
        reference.write_text("RESOURCE_SENTINEL", encoding="utf-8")
        read = answer(calls=[("read_skill_resource", {"name": "天气查询", "path": "references/rules.md"})])
        _, model = await self.chat([read, load("天气查询"), read, answer("好")])
        self.assertNotIn("RESOURCE_SENTINEL", contents(model.requests[2]))
        self.assertIn("RESOURCE_SENTINEL", contents(model.requests[3]))

    async def test_loaded_state_is_not_carried_to_next_round_or_conversation(self):
        await self.chat([load("天气查询"), answer("好")])
        for group in ("g", "other"):
            _, model = await self.chat([answer("你好")], group=group)
            self.assertEqual(names(model.requests[0]), set(skills.SKILL_TOOL_NAMES))
            self.assertNotIn("BODY_", contents(model.requests[0]))

    async def test_concurrent_conversations_do_not_share_skill_grants(self):
        requests = defaultdict(list)

        class ConcurrentModel:
            async def create(self, **kwargs):
                user = next(m["content"] for m in kwargs["messages"] if isinstance(m, dict) and m["role"] == "user")
                requests[user].append(copy.deepcopy(kwargs))
                await asyncio.sleep(0)
                return load("天气查询" if "甲" in user else "地图导航") if len(requests[user]) == 1 else answer("好")

        with patch.object(dc, "client", NS(chat=NS(completions=ConcurrentModel()))):
            await asyncio.gather(dc.chat("a", "a", "甲"), dc.chat("b", "b", "乙"))
        a, b = [r[1] for r in requests.values()]
        self.assertIn("web_search", names(a))
        self.assertNotIn("plan_route", names(a))
        self.assertIn("plan_route", names(b))
        self.assertNotIn("web_search", names(b))

    async def test_dm_and_private_do_not_get_reply_skip_or_unrelated_bodies(self):
        _, model = await self.chat([load("跑团主持"), answer("剧情")], dm=True)
        self.assertNotIn("BODY_DM", contents(model.requests[0]))
        self.assertIn("BODY_DM", contents(model.requests[1]))
        self.assertNotIn("BODY_WEATHER", contents(model.requests[1]))
        self.assertEqual(names(model.requests[1]), set(skills.SKILL_TOOL_NAMES) | {"manage_campaign"})
        _, private = await self.chat([answer(calls=[("reply", {"text": "不该发送"})]), answer("普通回答")])
        self.assertNotIn("reply", names(private.requests[0]))
        self.assertIn("未在本次请求中开放", contents(private.requests[1]))

    async def test_group_reply_skip_are_controls_without_loading_business_skills(self):
        outbox = ReplyOutbox()
        _, model = await self.chat([answer(calls=[("reply", {"text": "群回复"})]), answer()], outbox=outbox)
        self.assertEqual(names(model.requests[0]), set(skills.SKILL_TOOL_NAMES) | {"reply", "skip"})
        self.assertEqual(outbox.items[0].text, "群回复")


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Actual source-field helpers with source-shaped synthetic controls, no network."""
import unittest
from uploader.tencent_uploader.prepare_fields import (AI_DECLARATION_OPTION, DECLARATION_ENTRY, DESCRIPTION_SELECTOR,
                                                       SHORT_TITLE_SELECTOR, TencentSourceFields)
from utils.experimental_prepare import FIXTURE_CSP, PrepareFailure


class Field:
    def __init__(self, name, page):
        self.name, self.page = name, page
        self.value, self.attributes = "", {}
    def filter(self, *, visible):
        assert visible is True
        return self
    async def count(self): return self.page.counts.get(self.name, 1)
    async def get_attribute(self, name): return self.attributes.get(name)
    async def fill(self, value):
        self.page.actions.append(("fill", self.name, value))
        self.value = value[:2] if self.page.truncate else value
    async def input_value(self): return self.value
    async def inner_text(self): return self.value
    async def click(self): self.page.actions.append(("click", self.name))


class SourcePage:
    url = "about:blank"
    def __init__(self):
        self.actions, self.counts, self.truncate = [], {}, False
        self.fields = {name: Field(name, self) for name in ("title", "description", "declaration", "option", "marker", "csp")}
        self.fields["marker"].attributes["content"] = "tencent-source-v1"
        self.fields["csp"].attributes["content"] = FIXTURE_CSP
        self.fields["description"].attributes["contenteditable"] = "true"
    def is_closed(self): return False
    def locator(self, selector):
        names = {SHORT_TITLE_SELECTOR: "title", DESCRIPTION_SELECTOR: "description", 'meta[name="publisher-fixture"]': "marker",
                 'meta[http-equiv="Content-Security-Policy"]': "csp"}
        return self.fields[names[selector]]
    def get_by_text(self, text, *, exact):
        assert exact
        return self.fields[{DECLARATION_ENTRY: "declaration", AI_DECLARATION_OPTION: "option"}[text]]


class TencentSourceFieldsTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_short_title_and_description_not_keyboard_append(self):
        page = SourcePage(); fields = TencentSourceFields(page)
        title, description = " 保留空格六字标题 ", "原始标题\n#话题一 #tag two\n原始正文"
        self.assertTrue((await fields.fill_short_title(title)).verified)
        self.assertTrue((await fields.fill_description(description)).verified)
        self.assertEqual(page.fields["title"].value, title)
        self.assertEqual(page.fields["description"].value, description)
        await fields.fill_description(description)
        self.assertEqual(page.fields["description"].value, description)
        self.assertFalse(any(action[0] == "click" for action in page.actions))

    async def test_ai_choice_is_explicit_and_click_is_not_readback_evidence(self):
        page = SourcePage(); fields = TencentSourceFields(page)
        evidence = await fields.select_ai_declaration(explicit_choice="ai_generated")
        self.assertFalse(evidence.verified)
        self.assertEqual(page.actions, [("click", "declaration"), ("click", "option")])

    async def test_no_declaration_is_not_silently_assumed_clear(self):
        for choice in (None, "none", "original", False):
            page = SourcePage()
            with self.subTest(choice=choice), self.assertRaises(PrepareFailure):
                await TencentSourceFields(page).select_ai_declaration(explicit_choice=choice)
            self.assertEqual(page.actions, [])

    async def test_missing_ambiguous_and_truncated_fields_stop(self):
        for mode in ("missing", "ambiguous", "truncated"):
            page = SourcePage()
            if mode == "missing": page.counts["title"] = 0
            if mode == "ambiguous": page.counts["title"] = 2
            if mode == "truncated": page.truncate = True
            with self.subTest(mode=mode), self.assertRaises(PrepareFailure):
                await TencentSourceFields(page).fill_short_title("这是原始短标题")

    async def test_real_url_rejected_before_any_field_change(self):
        page = SourcePage(); page.url = "https://channels.weixin.qq.com/platform/post/create"
        with self.assertRaises(PrepareFailure): await TencentSourceFields(page).fill_description("原文")
        self.assertEqual(page.actions, [])


if __name__ == "__main__": unittest.main()

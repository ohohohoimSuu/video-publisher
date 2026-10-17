"""Exercise actual uploader functions against failing DOM-shaped fixtures."""
import importlib
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from uploader.tencent_uploader.main import TencentBaseUploader
from uploader.xiaohongshu_uploader.main import XiaoHongShuBaseUploader
from uploader.weibo_uploader.main import WeiBoVideo
from uploader.hupu_uploader.main import HuPuVideo
from uploader.douyin_uploader.main import _upload_cover_slot
from uploader.alipay_uploader.main import format_title_with_tags
import uploader.ks_uploader.main as ks
import uploader.youtube_uploader.main as youtube


class Field:
    def __init__(self, count=1, value=''):
        self.first = self
        self.count = AsyncMock(return_value=count)
        self.fill = AsyncMock()
        self.input_value = AsyncMock(return_value=value)
        self.wait_for = AsyncMock()
        self.click = AsyncMock()

    def locator(self, *args, **kwargs):
        return self


class MetadataRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_video_channel_title_field_stops(self):
        field = Field(count=0)
        page = SimpleNamespace(locator=lambda *a: field, get_by_text=lambda *a, **k: field)
        with self.assertRaisesRegex(RuntimeError, '未找到'):
            await TencentBaseUploader.set_short_title(None, page, '这是完整的短标题')

    async def test_video_channel_title_readback_mismatch_stops(self):
        field = Field(value='被截断')
        page = SimpleNamespace(locator=lambda *a: field)
        with self.assertRaisesRegex(RuntimeError, '未完整保留'):
            await TencentBaseUploader.set_short_title(None, page, '这是完整的短标题')

    async def test_video_channel_title_is_preserved(self):
        title = '这是完整的短标题'
        field = Field(value=title)
        page = SimpleNamespace(locator=lambda *a: field)
        await TencentBaseUploader.set_short_title(None, page, title)
        field.fill.assert_awaited_once_with(title)

    async def test_weibo_and_hupu_stop_when_requested_description_field_missing(self):
        for cls in (WeiBoVideo, HuPuVideo):
            field = Field(count=0)
            page = SimpleNamespace(get_by_placeholder=lambda *a: field)
            with self.subTest(platform=cls.__name__), self.assertRaises(RuntimeError):
                await cls._fill_description(SimpleNamespace(desc='正文不能丢', tags=['测试']), page)

    async def test_failed_cover_tab_does_not_overwrite_portrait(self):
        portrait = Field()
        landscape = Field()
        landscape.click.side_effect = TimeoutError('tab unavailable')
        portrait.filter = lambda **kwargs: portrait
        landscape.filter = lambda **kwargs: landscape
        portrait.evaluate = AsyncMock(return_value=True)
        preview_states = [{"signature": value, "ready": True, "busy": False, "error": False}
                          for value in ("old", "old", "old", "new", "new", "new")]
        dialog = SimpleNamespace(get_by_text=lambda label, **kw: portrait if label == '设置竖封面' else landscape,
                                 evaluate=AsyncMock(side_effect=preview_states))
        steps = SimpleNamespace(filter=lambda **kwargs: portrait if kwargs.get('has_text') == '设置竖封面' else landscape)
        dialog.locator = lambda selector: steps if 'steps-' in selector else dialog
        dialog.filter = lambda **kwargs: dialog
        dialog.count = AsyncMock(return_value=1)
        page = SimpleNamespace(wait_for_timeout=AsyncMock())
        file_input = SimpleNamespace(set_input_files=AsyncMock(), count=AsyncMock(return_value=1))
        await _upload_cover_slot(page, dialog, file_input, '设置竖封面', 'portrait.png')
        with self.assertRaises(RuntimeError):
            await _upload_cover_slot(page, dialog, file_input, '设置横封面', 'landscape.png')
        file_input.set_input_files.assert_awaited_once_with('portrait.png')

    async def test_kuaishou_preserves_both_title_and_description(self):
        keyboard = SimpleNamespace(type=AsyncMock(), press=AsyncMock())
        page = SimpleNamespace(keyboard=keyboard)
        with patch.object(ks, '_focus_desc_editor', new=AsyncMock()), patch.object(ks.asyncio, 'sleep', new=AsyncMock()):
            await ks._fill_video_caption(page, '标题保留', '正文\n换行保留', ['标签一', '标签二'])
        self.assertEqual([c.args[0] for c in keyboard.type.await_args_list], ['标题保留\n\n正文\n换行保留', '#标签一 ', '#标签二 '])
        keyboard.press.assert_any_await('ControlOrMeta+A')

    async def test_kuaishou_four_tags_stops_before_editing(self):
        keyboard = SimpleNamespace(type=AsyncMock(), press=AsyncMock())
        with self.assertRaises(ValueError):
            await ks._fill_video_caption(SimpleNamespace(keyboard=keyboard), '标题', '正文', ['一', '二', '三', '四'])
        keyboard.type.assert_not_awaited()

    async def test_youtube_empty_description_clears_channel_defaults(self):
        page = object()
        with patch.object(youtube, '_fill_editable', new=AsyncMock()) as fill:
            await youtube._fill_video_copy(page, '标题', '')
        fill.assert_any_await(page, '#description-textarea #textbox', '')

    async def test_xiaohongshu_does_not_choose_first_similar_suggestion(self):
        picked = []
        class Items:
            def __init__(self, labels):
                self.labels = labels
                self.first = self
            async def wait_for(self, **kw): pass
            async def count(self): return len(self.labels)
            async def click(self): picked.extend(self.labels)
            def filter(self, has):
                return Items([label for label in self.labels if has.fullmatch(label)])
        items = Items(['猫咪日常', '#猫'])
        page = SimpleNamespace(keyboard=SimpleNamespace(type=AsyncMock()),
                               locator=lambda selector: items, get_by_text=lambda pattern: pattern)
        await XiaoHongShuBaseUploader.fill_tags(SimpleNamespace(tags=['猫'], desc='正文'), page)
        self.assertEqual(picked, ['#猫'])

    async def test_xiaohongshu_no_exact_suggestion_stops(self):
        class Items:
            first = None
            def __init__(self): self.first = self
            async def wait_for(self, **kw): pass
            async def count(self): return 0
            def filter(self, **kw): return self
            async def click(self): raise AssertionError('must not click a substitute')
        page = SimpleNamespace(keyboard=SimpleNamespace(type=AsyncMock()), locator=lambda selector: Items(), get_by_text=lambda pattern: pattern)
        with self.assertRaises(RuntimeError):
            await XiaoHongShuBaseUploader.fill_tags(SimpleNamespace(tags=['猫'], desc='正文'), page)

    def test_alipay_preserves_trailing_hash_and_rejects_overflow(self):
        self.assertEqual(format_title_with_tags('测试', ['C#']), '测试 #C#')
        with self.assertRaises(ValueError):
            format_title_with_tags('字' * 29, ['标签'])


if __name__ == '__main__':
    unittest.main()

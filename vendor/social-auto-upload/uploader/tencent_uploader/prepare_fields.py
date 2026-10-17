"""Safe extraction of fields already present in the legacy Tencent source.

Selectors originate in main.py: set_short_title, fill_title_and_tags and
apply_original_statement. They are tested against controlled fixtures only.
This helper never invokes the legacy uploader, its retries, account navigation,
verification handling, broad overlay removal, final submission or browser close.
"""
import asyncio
from dataclasses import dataclass

from utils.experimental_prepare import FIXTURE_CSP, PrepareFailure

SHORT_TITLE_SELECTOR = 'input[placeholder="填写短标题有机会获得更多流量"]'
DESCRIPTION_SELECTOR = "div.input-editor"
DECLARATION_ENTRY = "选择视频标注"
AI_DECLARATION_OPTION = "含AI生成内容"


@dataclass(frozen=True)
class FieldEvidence:
    field: str
    verified: bool


class TencentSourceFields:
    """Exercise known field source without enabling restricted-site access.

    The page must be a CSP-isolated, about:blank source-shaped fixture. Actual
    editor/frame acquisition and live account/profile validation remain gated.
    """
    def __init__(self, page):
        self.page = page

    def _fail(self, reason, next_action):
        raise PrepareFailure("settings", reason, next_action, upload_started=True) from None

    async def _guard(self):
        if self.page.is_closed() or self.page.url != "about:blank":
            self._fail("production_disabled", "Only the controlled source-shaped fixture is allowed.")
        marker = self.page.locator('meta[name="publisher-fixture"]')
        csp = self.page.locator('meta[http-equiv="Content-Security-Policy"]')
        if (await marker.count() != 1 or await marker.get_attribute("content") != "tencent-source-v1"
                or await csp.count() != 1 or await csp.get_attribute("content") != FIXTURE_CSP):
            self._fail("fixture_contract_missing", "Load the checked-in source-shaped fixture.")

    async def _unique_visible(self, target):
        target = target.filter(visible=True)
        if await target.count() != 1:
            self._fail("control_missing_or_ambiguous", "Inspect the unique editor control; no hidden or first-match fallback was used.")
        return target

    async def _safe(self, operation):
        try:
            return await operation
        except (PrepareFailure, asyncio.CancelledError):
            raise
        except Exception:
            self._fail("source_field_failed", "Inspect the retained editor; raw field diagnostics were withheld.")

    async def fill_short_title(self, value):
        return await self._safe(self._fill_short_title(value))

    async def _fill_short_title(self, value):
        await self._guard()
        if not isinstance(value, str) or not 6 <= len(value) <= 16:
            self._fail("short_title_required", "Supply the original 6–16 character short title; no automatic shortening is allowed.")
        field = await self._unique_visible(self.page.locator(SHORT_TITLE_SELECTOR))
        await field.fill(value)
        if await field.input_value() != value:
            self._fail("short_title_not_preserved", "Inspect the exact short title in the retained editor.")
        return FieldEvidence("short_title", True)

    async def fill_description(self, value):
        return await self._safe(self._fill_description(value))

    async def _fill_description(self, value):
        await self._guard()
        if not isinstance(value, str):
            self._fail("description_invalid", "Supply the exact approved platform description.")
        field = await self._unique_visible(self.page.locator(DESCRIPTION_SELECTOR))
        if await field.get_attribute("contenteditable") != "true":
            self._fail("description_not_editable", "Inspect the retained editor without broadening selectors.")
        # Filling the scoped editor avoids stale keyboard focus or concatenating
        # another copy of title/tags after an interrupted first attempt.
        await field.fill(value)
        if await field.inner_text() != value:
            self._fail("description_not_preserved", "Inspect the exact original description; no partial value is accepted.")
        return FieldEvidence("description", True)

    async def select_ai_declaration(self, *, explicit_choice):
        return await self._safe(self._select_ai_declaration(explicit_choice=explicit_choice))

    async def _select_ai_declaration(self, *, explicit_choice):
        await self._guard()
        # "none" cannot be implemented by simply doing nothing: old drafts may
        # contain another declaration. No verified clear-state selector exists in
        # legacy source, so that request is blocked before any mutation.
        if explicit_choice != "ai_generated":
            self._fail("declaration_state_unimplemented", "Verify the declaration clear/readback control before applying this choice.")
        entry = await self._unique_visible(self.page.get_by_text(DECLARATION_ENTRY, exact=True))
        await entry.click()
        option = await self._unique_visible(self.page.get_by_text(AI_DECLARATION_OPTION, exact=True))
        await option.click()
        # The old code only logged after a click. Do not upgrade that to evidence:
        # readback of the selected field still needs an accepted platform binder.
        return FieldEvidence("declaration", False)

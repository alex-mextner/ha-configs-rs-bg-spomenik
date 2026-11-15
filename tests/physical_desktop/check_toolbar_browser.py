"""Real browser checks for the compact panel; no HA credentials or remote changes."""

import asyncio
import json
import re
from pathlib import Path
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[2]


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            executable_path="/usr/bin/google-chrome",
            headless=True,
            args=["--disable-dev-shm-usage"],
        )
        try:
            for width, height in [(1440, 900), (430, 932), (932, 430)]:
                sidebar = 256 if width == 1440 else 0
                context = await browser.new_context(
                    viewport={"width": width, "height": height},
                    is_mobile=width < 1000,
                    has_touch=width < 1000,
                )
                page = await context.new_page()
                await page.route(
                    re.compile(r"/api/chatgpt_native/vnc\.html(?:\?|$)"),
                    lambda route: route.fulfill(
                        content_type="text/html",
                        body="""<!doctype html><html class="noVNC_connected"><body style="margin:0;background:#eceff1"><canvas width="1920" height="1080" tabindex="0"></canvas><input id="noVNC_setting_view_only" type="checkbox"><script>window.events=[];const canvas=document.querySelector('canvas');for(const type of ['keydown','keyup'])canvas.addEventListener(type,e=>{window.events.push([e.type,e.key,e.code,e.ctrlKey]);e.preventDefault()});</script></body></html>""",
                    ),
                )
                await page.route(
                    "**/toolbar-test",
                    lambda route: route.fulfill(
                        content_type="text/html",
                        body='<html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head><body style="margin:0;height:100vh"><div style="height:auto;margin-left:SIDEBARpx"><ubuntu-desktop-panel></ubuntu-desktop-panel></div></body></html>'.replace(
                            "SIDEBAR", str(sidebar)
                        ),
                    ),
                )
                await page.goto("http://127.0.0.1/toolbar-test")
                await page.add_script_tag(
                    content=(
                        ROOT / "custom_components/chatgpt_native/panel.js"
                    ).read_text()
                )
                await page.evaluate("""() => {
                  window.apiCalls=[];
                  document.querySelector('ubuntu-desktop-panel').hass={callWS:async()=>({session:'test'}),callApi:async(...args)=>{apiCalls.push(args);return {text:'clipboard sample',ready:true}}};
                }""")
                frame = page.frame_locator("iframe")
                await frame.locator("canvas").wait_for()
                shell = page.locator("[data-desktop-shell]")
                iframe = page.locator("iframe")
                before = await iframe.bounding_box()
                assert round(before["y"]) == 28, before
                assert (
                    round(before["x"]) == sidebar
                    and round(before["width"]) == width - sidebar
                ), ("panel_covers_sidebar", before)
                await page.locator("[data-toolbar-toggle]").click()
                after = await iframe.bounding_box()
                assert round(after["y"]) == 0 and round(after["height"]) == height, (
                    "collapse_did_not_reclaim_header",
                    before,
                    after,
                )
                assert await page.locator("[data-clipboard-toggle]").is_hidden()
                assert (
                    await page.locator("[data-toolbar-toggle]").get_attribute(
                        "aria-expanded"
                    )
                    == "false"
                )
                await page.locator("[data-toolbar-toggle]").click()
                assert round((await iframe.bounding_box())["y"]) == 28
                await page.locator("[data-clipboard-toggle]").click()
                await page.locator("[data-clip-text]").fill("private preview")
                await page.locator('[data-clip-action="ctrl-c"]').click()
                await page.locator('[data-clip-action="ctrl-v"]').click()
                events = await frame.locator("canvas").evaluate("el => window.events")
                assert events == [
                    ["keydown", "Control", "ControlLeft", True],
                    ["keydown", "c", "KeyC", True],
                    ["keyup", "c", "KeyC", True],
                    ["keyup", "Control", "ControlLeft", False],
                    ["keydown", "Control", "ControlLeft", True],
                    ["keydown", "v", "KeyV", True],
                    ["keyup", "v", "KeyV", True],
                    ["keyup", "Control", "ControlLeft", False],
                ], events
                assert await page.evaluate("apiCalls.length") == 1, (
                    "Key buttons must not read/write the clipboard"
                )
                await page.locator("[data-toolbar-toggle]").click()
                assert await page.locator("[data-clipboard]").is_hidden()
                assert await page.locator("[data-clip-text]").input_value() == ""
                assert (
                    len(await frame.locator("canvas").evaluate("el => window.events"))
                    == 8
                ), "Collapse must retain the iframe"
                await page.locator("[data-toolbar-toggle]").click()
                await page.locator("[data-clipboard-toggle]").click()
                assert await page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth"
                ), "Horizontal overflow"
                assert await page.locator("[data-clipboard]").evaluate(
                    "(el)=>el.getBoundingClientRect().bottom <= innerHeight"
                ), "Drawer below mobile viewport"
                for button in await page.locator('[data-clip-action^="ctrl-"]').all():
                    assert (
                        await button.locator('svg[data-tabler="chevron-up"]').count()
                        == 1
                    )
                    assert "^" not in await button.inner_text()
                assert await page.locator("svg:not([data-tabler])").count() == 0
                assert "\U0001f4cb" not in await shell.inner_text()
                print(
                    json.dumps(
                        {
                            "viewport": [width, height],
                            "expanded_px": 28,
                            "collapsed_px": 0,
                            "shortcuts": True,
                            "tabler_only": True,
                            "iframe_retained": True,
                        }
                    )
                )
                await context.close()
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

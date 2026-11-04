"""Read-only phone app navigation checks, including the Model errors tab."""
import json
import re
from pathlib import Path
from playwright.sync_api import expect, sync_playwright


def main():
    output=Path(__file__).resolve().parents[3]/'reports/phone-sections-ui-20261023'
    output.mkdir(parents=True,exist_ok=True)
    with sync_playwright() as tool:
        browser=tool.chromium.launch(executable_path='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless=True)
        results=[]
        for width,height in [(390,844),(844,390)]:
            context=browser.new_context(viewport={'width':width,'height':height},is_mobile=True,has_touch=True)
            page=context.new_page();errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto('http://127.0.0.1:8765/?phone=1')
            tabs=page.get_by_role('navigation',name='Phone app sections')
            expect(tabs).to_be_visible()
            expect(tabs.get_by_role('link',name='Photos',exact=True)).to_have_attribute('aria-current','page')
            expect(tabs.get_by_role('link',name='Model errors',exact=True)).to_be_visible()
            page.screenshot(path=str(output/f'photos-{width}.png'),full_page=True)
            tabs.get_by_role('link',name='Model errors',exact=True).click()
            expect(page).to_have_url(re.compile(r'http://127\.0\.0\.1:8765/model-errors(?:\?.*)?$'))
            expect(page.locator('#case')).to_be_visible(timeout=30000)
            tabs=page.get_by_role('navigation',name='Phone app sections')
            expect(tabs.get_by_role('link',name='Model errors',exact=True)).to_have_attribute('aria-current','page')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(output/f'errors-{width}.png'),full_page=True)
            tabs.get_by_role('link',name='Experiments',exact=True).click()
            expect(page.locator('#detail')).to_be_visible(timeout=30000)
            expect(page.get_by_role('navigation',name='Phone app sections').get_by_role('link',name='Experiments')).to_have_attribute('aria-current','page')
            assert not errors,errors
            results.append({'width':width,'height':height,'model_errors_tab':True,'navigation':True,'javascript_errors':errors})
            context.close()
        context=browser.new_context(viewport={'width':1460,'height':950})
        page=context.new_page();page.goto('http://127.0.0.1:8765/model-errors')
        expect(page.get_by_role('navigation',name='Phone app sections')).to_be_hidden()
        expect(page.get_by_role('navigation',name='Studio navigation').get_by_role('link',name='Model mistakes')).to_be_visible()
        context.close();browser.close()
    (output/'result.json').write_text(json.dumps(results,indent=2))
    print(json.dumps(results))


if __name__=='__main__':main()

"""Exercise local-file playback, seeking and GPS synchronization in Edge."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT/'reports/ar-mock-test-20261023'

with sync_playwright() as tool:
    browser=tool.chromium.launch(executable_path='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
                                   headless=True,args=['--autoplay-policy=no-user-gesture-required','--mute-audio'])
    outcomes=[]
    for width in [1440,420]:
        page=browser.new_page(viewport={'width':width,'height':1000})
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto((OUT/'preview.html').as_uri())
        expect(page.locator('#status')).to_contain_text('64-second',timeout=20000)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert page.evaluate('document.querySelector("video").duration')==64
        for label,mode,t in [('Left bend','ALIGNED',8),('Intersection turn','DIRECTIONAL',29),
                             ('GPS gap','LOST',44),('Overload','LOST',50),('Recovery','ALIGNED',62)]:
            page.get_by_role('button',name=label,exact=True).click()
            expect(page.locator('#time')).to_have_text(f'{t:.1f} s')
            # Recovery intentionally requires several independent captures.
            if label=='Recovery':
                page.evaluate('document.querySelector("video").currentTime=63')
                expect(page.locator('#time')).to_have_text('63.0 s')
            expect(page.locator('#mode')).to_have_text(mode)
        if width==1440:
            page.get_by_role('button',name='Left bend',exact=True).click()
            page.evaluate('document.querySelector("video").play()')
            page.wait_for_function('document.querySelector("video").currentTime > 8.5')
            page.evaluate('document.querySelector("video").pause()')
            frozen=page.evaluate('document.querySelector("video").currentTime')
            page.wait_for_timeout(250)
            assert page.evaluate('document.querySelector("video").currentTime')==frozen
            page.screenshot(path=str(OUT/'viewer-desktop.png'),full_page=True)
        else:
            page.screenshot(path=str(OUT/'viewer-narrow.png'),full_page=True)
        assert not errors,errors
        outcomes.append({'viewport_width':width,'javascript_errors':errors,'playback_and_seek':'PASS','horizontal_overflow':False})
        page.close()
    browser.close()
(OUT/'viewer-verification.json').write_text(json.dumps({'passed':True,'local_file_tested':True,'checks':outcomes},indent=2)+'\n')
print(json.dumps(outcomes,indent=2))

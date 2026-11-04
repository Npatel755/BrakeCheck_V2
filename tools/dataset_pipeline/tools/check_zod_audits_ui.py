"""Check live ZOD progress and browser-only photo/edit fixtures; never save real votes."""
import json
from pathlib import Path
from playwright.sync_api import expect,sync_playwright
ROOT=Path(__file__).resolve().parents[3]
OUT=ROOT/'reports/zod-import-20261023'
row=json.loads((OUT/'browser-row.json').read_text(encoding='utf-8'))
geometry=json.loads(row['geometry_json']);warnings=json.loads(row['qc_json'])
complete={'missing_or_invalid_native_lane_annotations','no_converted_lane_requires_whole_photo_check','paint_polygon_needs_manual_trace','invalid_native_paint_shape','invalid_or_out_of_bounds_native_polygon','more_than_four_boundary_groups','boundary_exceeds_editor_point_limit'}
item={'image_id':row['image_id'],'source':'zod_mirror','split':'hold','session_id':'ZOD browser-only sample','group_key':'individual/'+row['frame_id'],'group_images':1,'geometry':geometry,'source_geometry':geometry,'source_polygons':geometry.get('source_polygons',[]),'source_qc_reasons':warnings,'qc_reasons':warnings,'resolved_qc_reasons':[],'repairable_qc_reasons':[w for w in warnings if w not in complete],'complete_annotation_qc_reasons':[w for w in warnings if w in complete],'annotation_policy_risks':['Provisional coordinate scaling and grouping'],'fingerprint':'a'*64,'revision':0,'decision':None,'image_url':'/api/studio/zod/samples/'+row['image_id']+'/photo'}
results=[]
with sync_playwright() as tool:
 browser=tool.chromium.launch(executable_path='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless=True)
 for phone in (False,True):
  context=browser.new_context(viewport={'width':390 if phone else 1460,'height':950},is_mobile=phone,has_touch=phone)
  page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
  page.goto('http://127.0.0.1:8765/source-audits?source=zod_mirror')
  expect(page.locator('#import-state')).to_contain_text('conversion groups',timeout=20000)
  expect(page.locator('#progress-note')).to_contain_text('up to 3 per group')
  page.screenshot(path=str(OUT/('phone-progress.png' if phone else 'desktop-progress.png')),full_page=True)
  state={'saved':False,'posts':[]}
  def fixture(route):
   url=route.request.url;method=route.request.method
   if url.endswith('/photo'):
    route.fulfill(body=(OUT/'browser-photo.jpg').read_bytes(),content_type='image/jpeg');return
   if method=='POST':
    body=route.request.post_data_json;state['posts'].append(body);state['saved']=True
    route.fulfill(json={**item,'revision':1,'decision':{'action':'approve','reviewer':'Browser fixture'},'qc_reasons':[]});return
   if '/queue?' in url:
    route.fulfill(json={'items':[] if state['saved'] else [item],'audits_ready':True});return
   if '/samples/' in url:route.fulfill(json=item);return
   route.fulfill(json={'source':'zod_mirror','audits_ready':True,'counts':{'indexed':1,'imported':1,'pending':0 if state['saved'] else 1,'audits_remaining':0 if state['saved'] else 1,'approved':1 if state['saved'] else 0,'groups':1},'progress':{'state':'audit_ready'},'policy':'Browser fixture: real downloads continue independently; no actual decisions are saved.'})
  page.route('**/api/studio/zod**',fixture)
  page.locator('#refresh').click()
  expect(page.locator('#sample')).to_be_visible()
  expect(page.locator('#zod-source-controls')).to_be_visible()
  assert page.locator('#lanes').get_attribute('width')=='1280'
  expect(page.locator('#zod-polygons')).not_to_be_checked()
  page.locator('#zod-polygons').check();page.locator('#zod-polygons').uncheck()
  expect(page.locator('#exclude')).to_have_text('Bad conversion — hold group')
  expect(page.locator('#repair-problems-option')).to_have_attribute('value','needs_repair')
  assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'), 'Horizontal overflow'
  page.screenshot(path=str(OUT/('phone-photo.png' if phone else 'desktop-photo.png')),full_page=True)
  page.locator('#fix').click();expect(page.locator('#fix-tools')).to_be_visible()
  if page.locator('#fix-resolve-warnings').is_visible():page.locator('#fix-resolve-warnings').check()
  if page.locator('#fix-complete-annotation').is_visible():page.locator('#fix-complete-annotation').check()
  page.locator('#save-fix').click()
  expect(page.locator('#empty')).to_be_visible()
  assert state['posts'] and state['posts'][0]['image_id']==item['image_id']
  assert not errors,errors
  results.append({'phone':phone,'live_progress':True,'source_polygons':True,'line_editor':True,'save_intercepted':True,'javascript_errors':errors})
  context.close()
 browser.close()
(OUT/'browser-checks.json').write_text(json.dumps(results,indent=2))
print(json.dumps(results))

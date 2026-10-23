"""Browser regression checks for the saved Prompt Parity explorer."""
import argparse
import copy
import functools
import http.server
import json
from pathlib import Path
import threading

from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).parent
FIXTURES = json.loads((ROOT / 'fixtures.json').read_text())
BY_ID = {item['id']: item for item in FIXTURES['cases']}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url')
    parser.add_argument('--artifacts', type=Path)
    parser.add_argument('--bundled-chromium', action='store_true', help='Use the Chromium version installed by Playwright, matching CI.')
    args = parser.parse_args()
    server = None
    if args.base_url:
        base_url = args.base_url.rstrip('/') + '/'
    else:
        class QuietHandler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *unused):
                pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(QuietHandler, directory=str(ROOT)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base_url = f'http://127.0.0.1:{server.server_port}/'
    checks, errors, requests = [], [], []
    try:
        with sync_playwright() as p:
            options = {'headless': True, 'args': ['--no-sandbox']}
            if not args.bundled_chromium and Path('/usr/bin/chromium').is_file():
                options['executable_path'] = '/usr/bin/chromium'
            browser = p.chromium.launch(**options)
            try:
                context = browser.new_context(viewport={'width': 1440, 'height': 1100}, accept_downloads=True, reduced_motion='reduce')
                page = context.new_page()
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('request', lambda request: requests.append(request.url))
                page.goto(base_url, wait_until='networkidle')
                expect(page.locator('#case-content')).to_be_visible()
                assert page.locator('.case-button').count() == 8
                assert page.locator('#case-count').inner_text() == '8'
                assert page.locator('#match-count').inner_text() == '6'
                assert page.locator('#difference-count').inner_text() == '2'
                checks.append('eight source-pinned cases and correct result counts')

                def select(case_id):
                    page.locator(f'[data-case-id="{case_id}"]').click()

                def view(name):
                    page.locator('#tab-' + name).click()

                for case_id, item in BY_ID.items():
                    select(case_id)
                    view('prompts')
                    assert page.locator('#case-title').inner_text() == item['title']
                    assert page.locator('#result-badge').inner_text() == ('EXACT MATCH' if item['observed_match'] else 'EXPECTED DIFFERENCE')
                    for side in ['reference', 'template']:
                        result = item[side]
                        if result['error']:
                            expect(page.locator('#' + side + '-error')).to_be_visible()
                            assert result['error']['message'] in page.locator('#' + side + '-error').inner_text()
                            assert page.locator('#copy-' + side).is_disabled()
                        else:
                            assert page.locator('#' + side + '-output').text_content() == result['output']
                            assert f"{len(result['output'].encode('utf-8'))} UTF-8 bytes" in page.locator('#' + side + '-meta').inner_text()
                    view('history')
                    assert json.loads(page.locator('#history-output').text_content()) == item['history']
                checks.append('all saved histories, exact prompt bytes, errors and match labels')

                select('raw_json_string_arguments')
                view('diff')
                assert page.locator('.diff-row.removed').count() == 2
                assert page.locator('.diff-row.added').count() == 1
                assert 'name="query"' in page.locator('.diff-row.removed').first.inner_text()
                assert 'name="arguments"' in page.locator('.diff-row.added').inner_text()
                page.locator('#related-case').click()
                assert page.locator('#case-title').inner_text() == BY_ID['normalized_json_string_arguments']['title']
                assert page.locator('#result-badge').inner_text() == 'EXACT MATCH'
                assert page.locator('.diff-row.removed').count() == page.locator('.diff-row.added').count() == 0
                select('medium_alias')
                assert 'two successful outputs' in page.locator('#diff-message').inner_text()
                assert 'AssertionError' in page.locator('.diff-error-summary').inner_text()
                checks.append('raw versus normalized line diff and error-output distinction')

                select('raw_json_string_arguments')
                view('prompts')
                page.evaluate('Object.defineProperty(navigator,"clipboard",{configurable:true,value:{writeText:async(value)=>{window.copiedText=value}}})')
                page.locator('#show-whitespace').check()
                assert '·' in page.locator('#reference-output').text_content()
                assert '↵' in page.locator('#reference-output').text_content()
                page.locator('#copy-reference').click()
                assert page.evaluate('window.copiedText') == BY_ID['raw_json_string_arguments']['reference']['output']
                page.locator('#copy-template').click()
                assert page.evaluate('window.copiedText') == BY_ID['raw_json_string_arguments']['template']['output']
                page.locator('#copy-case').click()
                exported = json.loads(page.evaluate('window.copiedText'))
                assert exported['case'] == BY_ID['raw_json_string_arguments']
                assert exported['sources'] == FIXTURES['sources']
                assert exported['rendering_environment'] == FIXTURES['rendering_environment']
                with page.expect_download() as event:
                    page.locator('#download-case').click()
                download = event.value
                assert download.suggested_filename == 'deepseek-raw_json_string_arguments.json'
                assert json.loads(Path(download.path()).read_text()) == exported
                checks.append('whitespace markers never alter copied or downloaded originals and provenance')

                page.evaluate('Object.defineProperty(navigator,"clipboard",{configurable:true,value:{writeText:()=>Promise.reject(new Error("denied"))}})')
                page.locator('#copy-reference').click()
                expect(page.locator('dialog')).to_be_visible()
                assert page.locator('dialog textarea').input_value() == BY_ID['raw_json_string_arguments']['reference']['output']
                assert page.locator('dialog textarea').evaluate('(el)=>el.selectionEnd-el.selectionStart') == len(BY_ID['raw_json_string_arguments']['reference']['output'])
                page.locator('dialog button').click()
                # The close event removes the node after dialog.close(). Wait
                # for that lifecycle event, then require complete DOM removal.
                expect(page.locator('dialog')).to_have_count(0)
                checks.append('exact selectable clipboard denial fallback')

                page.locator('#show-whitespace').uncheck()
                select('raw_json_string_arguments')
                button = page.locator('[data-case-id="raw_json_string_arguments"]')
                button.focus()
                button.press('ArrowDown')
                assert page.locator('[aria-current=true]').get_attribute('data-case-id') == 'normalized_json_string_arguments'
                page.locator('#tab-prompts').focus()
                page.locator('#tab-prompts').press('ArrowRight')
                assert page.locator('#tab-history').get_attribute('aria-selected') == 'true'
                page.locator('#tab-history').press('End')
                assert page.locator('#tab-diff').get_attribute('aria-selected') == 'true'
                page.goto(base_url + '#case=medium_alias&view=diff&ws=1', wait_until='networkidle')
                expect(page.locator('#case-content')).to_be_visible()
                assert page.locator('#case-title').inner_text() == BY_ID['medium_alias']['title']
                assert page.locator('#tab-diff').get_attribute('aria-selected') == 'true'
                assert page.locator('#show-whitespace').is_checked()
                checks.append('keyboard case and tab navigation, exact case deep links')

                for source in FIXTURES['sources']:
                    assert source['sha256'] in page.locator('#source-list').inner_text()
                    assert source['revision'] in page.locator('#source-list').inner_text()
                assert 'byte-identical' in page.locator('#encoder-note').inner_text()
                checks.append('all source revisions and full checksums visible')

                # Inject browser response fixtures only for defensive presentation tests.
                # These test fixtures never enter the public dataset or generator.
                malicious = copy.deepcopy(FIXTURES)
                raw = next(item for item in malicious['cases'] if item['id'] == 'raw_json_string_arguments')
                raw['history'][0]['content'] = '<img src=x onerror="window.xss=1">'
                raw['reference']['output'] = '<script>window.xss=1</script>\n'
                raw['template']['output'] = '<img src=x onerror="window.xss=1">'
                raw['observed_match'] = False
                page.route('**/fixtures.json', lambda route: route.fulfill(json=malicious))
                page.goto(base_url + '?fixture=literal#case=raw_json_string_arguments&view=prompts', wait_until='networkidle')
                expect(page.locator('#case-content')).to_be_visible()
                assert page.locator('#reference-output').text_content() == raw['reference']['output']
                assert page.locator('#reference-output script').count() == page.locator('#template-output img').count() == 0
                view('history')
                assert '<img' in page.locator('#history-output').text_content()
                assert page.evaluate('window.xss') is None
                checks.append('history, output and markup render as literal text without execution')

                controls = copy.deepcopy(FIXTURES)
                control = next(item for item in controls['cases'] if item['id'] == 'raw_json_string_arguments')
                control['reference']['output'] = 'a\n\n'
                control['template']['output'] = 'a\n'
                control['observed_match'] = False
                page.unroute('**/fixtures.json')
                page.route('**/fixtures.json', lambda route: route.fulfill(json=controls))
                page.goto(base_url + '?fixture=controls#case=raw_json_string_arguments&view=diff', wait_until='networkidle')
                expect(page.locator('#case-content')).to_be_visible()
                assert page.locator('.diff-row.removed').count() == 1
                assert '[empty' in page.locator('.diff-row.removed').inner_text()
                control['reference'] = {'output': None, 'error': {'type': 'TestError', 'message': 'same failure'}}
                control['template'] = {'output': None, 'error': {'type': 'TestError', 'message': 'same failure'}}
                control['observed_match'] = False
                control['expected_match'] = True
                page.reload(wait_until='networkidle')
                expect(page.locator('#case-content')).to_be_visible()
                assert page.locator('#result-badge').inner_text() == 'UNEXPECTED DIFFERENCE'
                assert 'Unexpected difference' in page.locator('#status').inner_text()
                assert 'two successful outputs' in page.locator('#diff-message').inner_text()
                checks.append('blank and terminal lines preserved, equal errors never count as parity')

                page.unroute('**/fixtures.json')
                page.route('**/fixtures.json', lambda route: route.fulfill(status=503, body='unavailable'))
                page.goto(base_url + '?fixture=unavailable', wait_until='networkidle')
                assert page.locator('#case-count').inner_text() == 'unavailable'
                assert page.locator('#match-count').inner_text() == 'unavailable'
                expect(page.locator('#case-content')).not_to_be_visible()
                assert 'could not be loaded' in page.locator('#loading').inner_text()
                checks.append('failed fixture loads show unavailable counts and no results')

                page.unroute('**/fixtures.json')
                page.goto(base_url + '#case=raw_json_string_arguments&view=diff', wait_until='networkidle')
                expect(page.locator('#case-content')).to_be_visible()
                if args.artifacts:
                    args.artifacts.mkdir(parents=True, exist_ok=True)
                    page.evaluate('document.activeElement?.blur(); window.scrollTo(0,0)')
                    page.screenshot(path=str(args.artifacts / 'parity-desktop.png'), full_page=True)

                phone_context = browser.new_context(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True, reduced_motion='reduce')
                phone = phone_context.new_page()
                phone.on('pageerror', lambda error: errors.append(str(error)))
                phone.on('request', lambda request: requests.append(request.url))
                phone.goto(base_url, wait_until='networkidle')
                expect(phone.locator('#case-content')).to_be_visible()
                assert phone.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
                phone.locator('[data-case-id="normalized_json_string_arguments"]').tap()
                assert phone.locator('#result-badge').inner_text() == 'EXACT MATCH'
                phone.locator('#tab-history').tap()
                assert phone.locator('#history-output').is_visible()
                phone.locator('#show-whitespace').tap()
                assert phone.locator('#show-whitespace').is_checked()
                phone.locator('#tab-prompts').tap()
                assert phone.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
                if args.artifacts:
                    phone.evaluate('document.activeElement?.blur(); window.scrollTo(0,0)')
                    phone.screenshot(path=str(args.artifacts / 'parity-phone.png'), full_page=True)
                phone.set_viewport_size({'width': 320, 'height': 740})
                assert phone.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
                phone_context.close()
                checks.append('390 and 320 px layouts and native touch case, tab and display controls')

                origin = '/'.join(base_url.split('/')[:3])
                foreign = [url for url in requests if not url.startswith(origin + '/') and not url.startswith(('blob:', 'data:'))]
                assert not foreign, foreign
                assert not errors, errors
                checks.append('no external network requests or browser runtime errors')
                context.close()
            finally:
                browser.close()
    finally:
        if server:
            server.shutdown()
            server.server_close()
    print(json.dumps({'passed': len(checks), 'checks': checks, 'runtime_errors': errors, 'base_url': args.base_url or 'local'}, indent=2))


if __name__ == '__main__':
    main()

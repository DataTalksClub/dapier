"""Mail gets its own CSP, keeps sender styles and grows with its content."""
import pytest

from src.dapier.api.router import _static


def test_mail_document_has_an_isolated_policy():
    response = _static('/email-message.html')
    policy = response['headers']['content-security-policy']
    assert "script-src 'none'" in policy
    assert "style-src 'unsafe-inline' https:" in policy
    assert "img-src https: data:" in policy
    assert "form-action 'none'" in policy
    assert 'sandbox allow-same-origin' in policy
    assert 'allow-scripts' not in policy
    assert "style-src 'self';" in _static('/emails')['headers']['content-security-policy']


@pytest.mark.parametrize('width', [1440, 390])
def test_styled_email_grows_and_stays_sandboxed(width, monkeypatch, tmp_path):
    pytest.importorskip('playwright.sync_api')
    from playwright.sync_api import sync_playwright
    import tests.test_console_boot as boot

    mail = '''<!doctype html><html><head><style>
      body{background:#eef4fb;color:#123456;font-family:Arial,sans-serif}
      .newsletter{width:600px;max-width:100%;margin:auto;background:white}
      h1{font-size:28px;color:#225588}
      p{padding:12px;line-height:1.5}
      </style></head><body><main class="newsletter"><table width="600" style="width:600px;max-width:100%"><tr><td><h1>AlphaSignal</h1>
      <p style="color:rgb(170, 20, 30)">Your daily AI newsletter</p>
      <img id="late-image" src="https://mail.example/banner.svg">
      <a href="https://example.com" target="_top">Read more</a>
      <script>parent.document.body.dataset.emailAttack='executed'</script>
      <img src="data:," onerror="parent.document.body.dataset.emailAttack='executed'">
      ''' + ''.join(f'<p>Story {n}: This is a full newsletter article with useful details.</p>' for n in range(45)) + '</td></tr></table></main></body></html>'
    event = {'inbox_id': 'email#styled', 'received_at': '2026-10-16T08:00:00Z',
             'outcome': 'handled', 'runs': [], 'email': {'from': 'AlphaSignal <news@example.com>',
             'to': ['newsletter@example.com'], 'subject': 'Styled newsletter', 'html': mail}}
    real = boot._api_payload

    def payload(path):
        if path == '/api/admin/triggers/inbox':
            return {'events': [event], 'paging': {}}
        if path.startswith('/api/admin/triggers/inbox/'):
            return {'event': event}
        return real(path)

    monkeypatch.setattr(boot, '_api_payload', payload)
    with boot.console_server() as origin, sync_playwright() as playwright:
        browser = boot._chromium(playwright)
        page = browser.new_page(viewport={'width': width, 'height': 900})
        # No bypass_csp: this catches srcdoc inheriting the console policy.
        page.route('https://mail.example/banner.svg', lambda route: route.fulfill(
            content_type='image/svg+xml', body='<svg xmlns="http://www.w3.org/2000/svg" width="600" height="120"><rect width="600" height="120" fill="#225588"/></svg>'))
        page.goto(origin + '/emails?mail=email%23styled')
        frame = page.frame_locator('.email-body-frame')
        frame.locator('h1').wait_for()
        page.wait_for_function('''() => {
          const frame = document.querySelector('.email-body-frame');
          return frame && frame.clientHeight > 2500 &&
            frame.contentDocument.querySelector('#late-image')?.naturalWidth > 0;
        }''')
        assert frame.locator('h1').evaluate('(el) => getComputedStyle(el).color') == 'rgb(34, 85, 136)'
        assert frame.locator('p').first.evaluate('(el) => getComputedStyle(el).color') == 'rgb(170, 20, 30)'
        assert page.evaluate('document.body.dataset.emailAttack') is None
        assert frame.locator('a').get_attribute('target') == '_blank'
        assert frame.locator('script').count() == 0
        assert page.locator('.email-body-frame').evaluate('''el => {
          const doc = el.contentDocument;
          return doc.documentElement.scrollHeight <= el.clientHeight &&
                 doc.documentElement.scrollWidth <= el.clientWidth + 2;
        }''')
        # Late content changes also resize the frame, without a fixed cap.
        before = page.locator('.email-body-frame').evaluate('(el) => el.clientHeight')
        frame.locator('main').evaluate('(el) => {const p=document.createElement("p");p.style.height="800px";p.textContent="Late content";el.append(p)}')
        page.wait_for_function('(before) => document.querySelector(".email-body-frame").clientHeight > before + 700', arg=before)
        page.screenshot(path=str(tmp_path / f'email-{width}.png'), full_page=True)
        browser.close()

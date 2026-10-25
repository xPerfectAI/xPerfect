"""Readable file views retain byte delivery, content isolation and scoped links."""
from html.parser import HTMLParser
from pathlib import Path
import tracemalloc

import pytest

from workers_projects_runtime.artifact_preview import render_artifact_page
from workers_projects_runtime.signed_links import create_signed_link_ref, sign_link_token
from test_artifact_streaming import artifacts  # noqa: F401


class Elements(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.nodes = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.nodes.append((tag, dict(attrs)))


def render(name, mime, text, *, size=None, limit=4096):
    content = text.encode() if isinstance(text, str) else text
    return render_artifact_page(display_name=name, suffix=Path(name).suffix,
        media_type=mime, content=content, size=len(content) if size is None else size,
        download_url='/download', workspace_url='/workspace', inline_url='/open?inline=1', text_limit=limit)


def test_markdown_is_a_document_with_tables_code_safe_links_and_optional_source():
    page = render('report.md', 'text/markdown', '# Report\n\n| Item | Amount |\n|---|---:|\n| Total | **42** |\n\n```js\nalert("example")\n```\n\n[Source](https://example.test)\n\n[native](artifact_sha256:abc)')
    assert '<article class="document-preview rendered-result"><h1>Report</h1>' in page
    assert '<table>' in page and '<strong>42</strong>' in page
    assert '<code class="language-js">' in page
    nodes = Elements(page).nodes
    links = [attrs for tag, attrs in nodes if tag == 'a']
    assert any(link.get('href') == 'https://example.test' and link['rel'] == 'noopener noreferrer' for link in links)
    assert not any(link.get('href', '').startswith('artifact_sha256:') for link in links)
    assert any(tag == 'details' and attrs.get('class') == 'source-preview' and 'open' not in attrs for tag, attrs in nodes)


def test_untrusted_markdown_never_adds_active_html_or_fetches_image_targets():
    page = render('unsafe.md', 'text/markdown', '<script>alert(1)</script>\n\n[bad](javascript:alert(1))\n\n![remote](https://tracker.test/pixel)\n![private](/v1/workers/other/private.png)')
    nodes = Elements(page).nodes
    assert not any(tag in {'script', 'img', 'iframe', 'object'} for tag, _ in nodes)
    assert not any(tag == 'a' and attrs.get('href', '').lower().startswith('javascript:') for tag, attrs in nodes)
    assert '&lt;script&gt;' in page
    assert 'Image not shown: remote' in page


def test_opaque_document_relative_targets_are_labels_and_empty_markdown_is_clear():
    page = render('report.md', 'text/markdown', '[relative](other.md) [root](/private) [fragment](#part) [protocol](//example.test) [email](mailto:reader@example.test)')
    links = [attrs.get('href') for tag, attrs in Elements(page).nodes if tag == 'a']
    assert 'other.md' not in links and '/private' not in links and '#part' not in links and '//example.test' not in links
    assert 'mailto:reader@example.test' in links
    assert 'This document is empty' in render('empty.md', 'text/markdown', ' \n')


@pytest.mark.parametrize('name,mime', [('notes.txt', 'text/plain'), ('data.json', 'application/json'), ('source.py', 'text/plain'), ('diagram.svg', 'image/svg+xml')])
def test_plain_text_and_code_stay_literal(name, mime):
    page = render(name, mime, '# Literal\n<b>not bold</b>\n| raw | source |')
    assert '<pre class="artifact-preview"># Literal\n&lt;b&gt;not bold&lt;/b&gt;' in page
    assert '<h1>Literal</h1>' not in page and '<b>not bold</b>' not in page


def test_html_has_its_own_strict_sandbox_and_network_policy():
    page = render('page.html', 'text/html', '<h1>Page</h1><script>bad()</script><img src="/private"><form action="/change"></form>')
    frame = next(attrs for tag, attrs in Elements(page).nodes if tag == 'iframe')
    assert 'sandbox' in frame and not frame['sandbox']
    assert 'credentialless' in frame and frame['referrerpolicy'] == 'no-referrer'
    inner = next(attrs for tag, attrs in Elements(frame['srcdoc']).nodes if tag == 'meta')
    assert "default-src 'none'" in inner['content']
    assert "img-src data:" in inner['content'] and "frame-src 'none'" in inner['content']
    assert 'allow-scripts' not in page and 'allow-same-origin' not in page
    assert not any(tag == 'script' for tag, _ in Elements(page).nodes)


def test_tables_escape_cells_keep_values_and_bound_rows_columns_and_bad_input():
    page = render('budget.csv', 'text/csv', 'Item,Amount\n"<img src=x onerror=bad()>","001.20"\n')
    assert '<table>' in page and '<td>001.20</td>' in page and '&lt;img' in page
    assert not any(tag == 'img' for tag, _ in Elements(page).nodes)
    assert '<td>\u20ac42</td>' in render('budget.tsv', 'text/tab-separated-values', 'Name\tTotal\nBudget\t\u20ac42')
    long_table = render('rows.csv', 'text/csv', 'A,B\n' + '1,2\n' * 250, limit=10000)
    assert long_table.count('<tr>') == 201 and 'first 200 data rows' in long_table
    assert 'could not be read' in render('bad.csv', 'text/csv', 'A,B\n"unfinished', limit=4096)
    assert 'could not be read' in render('field.csv', 'text/csv', 'A\n' + 'x' * 150000, limit=200000)
    assert 'This file is empty' in render('empty.csv', 'text/csv', '')


def test_byte_cut_csv_omits_incomplete_record_and_places_size_notice_first():
    for ending in ['partial,value', '"unfinished\nmultiline']:
        text = 'A,B\ncomplete,001.20\n' + ending
        page = render('cut.csv', 'text/csv', text, limit=len(text), size=len(text)+100)
        assert '<td>001.20</td>' in page and '<td>partial</td>' not in page
        assert 'incomplete final table record is omitted' in page
        assert page.index('Preview is shortened') < page.index('<table>')
    columns = render('wide.csv', 'text/csv', ','.join(str(i) for i in range(42)))
    assert 'first 40 columns' in columns and 'first 200 data rows' not in columns


def test_native_range_requests_do_not_add_document_open_events(artifacts, monkeypatch, tmp_path):
    app, client, root, base = artifacts
    monkeypatch.setenv('GLASSHIVE_SIGNED_LINK_SECRET','synthetic-event-fixture-key')
    monkeypatch.setenv('GLASSHIVE_LINK_REF_STATE_PATH',str(tmp_path/'refs.sqlite3'))
    wid = base.split('/')[3]
    worker = app.state.service.store.get_worker(wid)
    (root/'document.pdf').write_bytes(b'%PDF-1.4\nsynthetic')
    token = sign_link_token(kind='artifact_open',worker_id=wid,tenant_id=worker['tenant_id'],owner_id=worker['owner_id'],path='document.pdf')
    ref = create_signed_link_ref(token=token)
    targets = [(base+'/open',{'path':'document.pdf'}),('/v1/signed-links/'+token,{}),('/v1/link-refs/'+ref,{})]
    def count():
        return sum(event['event_type']=='worker.artifact_opened' for event in app.state.service.store.list_events(wid))
    before = count()
    for url, query in targets:
        assert client.get(url,params=query).status_code == 200
        assert client.get(url,params={**query,'inline':'1'},headers={'Range':'bytes=0-3'}).status_code == 206
        assert client.get(url,params=query,headers={'Range':'bytes=0-3'}).status_code == 200
    assert count() == before+3


def test_shortened_content_and_unsupported_office_have_truthful_download_paths():
    page = render('long.md', 'text/markdown', '# Heading\n' + 'x' * 5000)
    assert 'Preview is shortened' in page and 'Download file' in page
    html = render('large.html', 'text/html', '<h1>Large</h1>' + 'x' * 5000)
    assert 'too large for a safe preview' in html and not any(tag == 'iframe' for tag, _ in Elements(html).nodes)
    for name, mime in [('workbook.xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'), ('report.docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'), ('slides.pptx', 'application/vnd.openxmlformats-officedocument.presentationml.presentation')]:
        page = render(name, mime, b'PK\x03\x04')
        assert 'browser preview is not available' in page and 'Download file' in page
        assert not any(tag in {'iframe', 'script', 'object'} for tag, _ in Elements(page).nodes)


@pytest.mark.parametrize('name,mime,tag', [('document.pdf','application/pdf','iframe'), ('image.png','image/png','img'), ('sound.wav','audio/wav','audio'), ('movie.mp4','video/mp4','video')])
def test_native_previews_stream_existing_open_scope_without_autoplay(name, mime, tag):
    page = render(name, mime, b'', size=200000000)
    node = next(attrs for kind, attrs in Elements(page).nodes if kind == tag)
    assert node['src'] == '/open?inline=1' and 'autoplay' not in node
    if tag == 'iframe':
        assert 'sandbox' not in node and 'open the PDF in a new tab' in page
    if tag in {'audio','video'}:
        assert 'controls' in node and node['preload'] == 'metadata'


def test_native_range_view_preserves_download_and_rejects_active_types(artifacts):
    _, client, root, base = artifacts
    (root/'document.pdf').write_bytes(b'%PDF-1.4\nsynthetic')
    result = client.get(base+'/open', params={'path':'document.pdf','inline':'1'}, headers={'Range':'bytes=0-3'})
    assert result.status_code == 206 and result.content == b'%PDF'
    assert result.headers['content-disposition'].startswith('inline;')
    assert 'script-src' in result.headers['content-security-policy']
    assert 'no-store' in result.headers['cache-control'] and result.headers['x-content-type-options'] == 'nosniff'
    raw = client.get(base+'/download', params={'path':'document.pdf','inline':'1'})
    assert raw.headers['content-disposition'].startswith('attachment;') and raw.content == b'%PDF-1.4\nsynthetic'
    for name in ['page.html','code.svg','plain.txt']:
        (root/name).write_text('<script>unsafe()</script>')
        assert client.get(base+'/open', params={'path':name,'inline':'1'}).status_code == 415


def test_large_media_wrapper_and_text_prefix_have_bounded_memory(artifacts):
    _, client, root, base = artifacts
    for name, size in [('large.mp4',200*1024*1024),('large.txt',148*1024*1024)]:
        with (root/name).open('wb') as output:
            output.write(b'preview\n')
            output.seek(size-4)
            output.write(b'end!')
        tracemalloc.start()
        try:
            response = client.get(base+'/open',params={'path':name})
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert response.status_code == 200
        assert len(response.content) < 2*1024*1024 and peak < 12*1024*1024
        assert f'{size:,} bytes' in response.text
        assert 'end!' not in response.text


def test_opaque_native_view_cannot_select_another_path_or_change_download_kind(artifacts, monkeypatch, tmp_path):
    app, client, root, base = artifacts
    monkeypatch.setenv('GLASSHIVE_SIGNED_LINK_SECRET','synthetic-preview-only-key')
    monkeypatch.setenv('GLASSHIVE_LINK_REF_STATE_PATH',str(tmp_path/'link-refs.sqlite3'))
    worker_id = base.split('/')[3]
    worker = app.state.service.store.get_worker(worker_id)
    (root/'one.png').write_bytes(b'first-image')
    (root/'other.txt').write_bytes(b'private-neighbor')
    token = sign_link_token(kind='artifact_open',worker_id=worker_id,tenant_id=worker['tenant_id'],owner_id=worker['owner_id'],path='one.png')
    ref = create_signed_link_ref(token=token)
    response = client.get('/v1/link-refs/'+ref,params={'inline':'1','path':'other.txt'})
    assert response.status_code == 200 and response.content == b'first-image'
    assert client.get('/v1/signed-links/'+token+'tampered',params={'inline':'1'}).status_code == 401
    wrong = sign_link_token(kind='artifact_open',worker_id=worker_id,tenant_id=worker['tenant_id'],owner_id='other-owner',path='one.png')
    assert client.get('/v1/signed-links/'+wrong,params={'inline':'1'}).status_code == 401
    assert client.get(base+'/open',params={'path':'../other.txt','inline':'1'}).status_code == 400


def test_reader_appearance_is_validated_and_port_scoped(artifacts, monkeypatch):
    _, client, root, base = artifacts
    (root/'report.md').write_text('# Report')
    monkeypatch.setenv('GLASSHIVE_OPERATOR_BASE_URL','http://localhost:39980')
    client.cookies.set('xperfect_appearance_39880','dark')
    assert 'data-theme="system"' in client.get(base+'/open',params={'path':'report.md'}).text
    client.cookies.set('xperfect_appearance_39980','light')
    assert 'data-theme="light"' in client.get(base+'/open',params={'path':'report.md'}).text
    assert 'data-theme="dark"' in client.get(base+'/open',params={'path':'report.md'},headers={'x-xperfect-appearance':'dark'}).text
    response = client.get(base+'/open',params={'path':'report.md'},headers={'x-xperfect-appearance':'"><script>'})
    assert 'data-theme="light"' in response.text and '<script>' not in response.text
    assert "script-src 'none'" in response.headers['content-security-policy']


def test_files_reader_uses_exact_id_revision_and_existing_raw_stream(artifacts):
    _, client, root, artifact_base = artifacts
    base = artifact_base.removesuffix('/artifacts')+'/files'
    (root/'report.md').write_text('# File report\n\n**Readable**')
    (root/'page.html').write_text('<h1>Page</h1>')
    (root/'document.pdf').write_bytes(b'%PDF-1.4\nsynthetic')
    entries = {item['name']:item for item in client.get(base).json()['items']}
    item = entries['report.md']
    url = base+'/'+item['file_id']
    response = client.get(url+'/preview',params={'revision':item['revision']},
        headers={'x-xperfect-file-ui':'1','x-xperfect-appearance':'dark'})
    assert response.status_code == 200 and '<h1>File report</h1>' in response.text
    assert '<strong>Readable</strong>' in response.text and 'data-theme="dark"' in response.text
    assert '/api/workspace/' in response.text and '/content?revision='+item['revision'] in response.text
    assert 'no-store' in response.headers['cache-control']
    assert client.get(url+'/preview').status_code == 422
    assert client.get(url+'/preview',params={'revision':'stale'}).status_code == 409
    assert client.get(url+'/preview',params={'revision':item['revision'],'inline':'1'}).status_code == 415
    (root/'report.md').write_text('Changed')
    assert client.get(url+'/preview',params={'revision':item['revision']}).status_code == 409
    pdf = entries['document.pdf']
    pdf_url = base+'/'+pdf['file_id']
    inline = client.get(pdf_url+'/preview',params={'revision':pdf['revision'],'inline':'1'},headers={'Range':'bytes=0-3'})
    assert inline.status_code == 206 and inline.content == b'%PDF'
    assert inline.headers['content-type'] == 'application/pdf'
    assert inline.headers['content-disposition'].startswith('inline;')
    raw = client.get(pdf_url+'/content',params={'revision':pdf['revision'],'inline':'1'})
    assert raw.content == b'%PDF-1.4\nsynthetic' and raw.headers['content-disposition'].startswith('attachment;')
    html = entries['page.html']
    assert client.get(base+'/'+html['file_id']+'/preview',params={'revision':html['revision'],'inline':'1'}).status_code == 415


def test_preview_browser_errors_are_human_but_api_and_download_errors_stay_json(artifacts):
    _, client, root, base = artifacts
    (root/'report.md').write_text('# Report')
    files = base.removesuffix('/artifacts')+'/files'
    item = client.get(files).json()['items'][0]
    preview = files+'/'+item['file_id']+'/preview'
    for url, params, status in [
        ('/v1/link-refs/ghr_missing_synthetic', {}, 401),
        (base+'/open', {'path':'missing.txt'}, 404),
        (preview, {'revision':'stale'}, 409),
        (preview, {}, 422),
    ]:
        browser = client.get(url,params=params,headers={'Accept':'text/html'})
        assert browser.status_code == status and browser.headers['content-type'].startswith('text/html')
        assert 'View workspaces' in browser.text and '<h1 class="file-title">' in browser.text
        assert 'ghr_missing_synthetic' not in browser.text and str(root) not in browser.text
        assert "script-src 'none'" in browser.headers['content-security-policy']
        api = client.get(url,params=params,headers={'Accept':'application/json'})
        assert api.status_code == status and api.headers['content-type'].startswith('application/json')
    raw = client.get(base+'/download',params={'path':'missing.txt'},headers={'Accept':'text/html'})
    assert raw.status_code == 404 and raw.headers['content-type'].startswith('application/json')

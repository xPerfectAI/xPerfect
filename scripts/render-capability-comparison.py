#!/usr/bin/env python3
"""Generate the public landscape from docs/comparison.json. Standard library only."""
from pathlib import Path
import argparse
import html
import json
import re
import textwrap
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
E = html.escape


def load_data():
    data = json.loads((ROOT / "docs/comparison.json").read_text())
    columns, products = data["columns"], data["products"]
    if not 5 <= len(columns) <= 7 or not 12 <= len(products) <= 45:
        raise ValueError("Use a readable benefit matrix with the complete landscape")
    ids = [c["id"] for c in columns]
    if len(ids) != len(set(ids)) or products[0]["id"] != "xperfect":
        raise ValueError("Unique columns and xPerfect first are required")
    groups = {g["id"] for g in data["groups"]}
    if len({p["id"] for p in products}) != len(products):
        raise ValueError("Duplicate product")
    for p in products:
        if p["group"] not in groups or set(p["cells"]) != set(ids) or not p["sources"]:
            raise ValueError("Each product needs a group, complete mechanisms and primary sources")
        for url in [p["url"], *[s["url"] for s in p["sources"]]]:
            parsed = urlparse(url)
            if parsed.scheme != "https" or not parsed.netloc or parsed.username:
                raise ValueError("Use public HTTPS sources")
        for cell in p["cells"].values():
            if len(cell["label"]) > 65 or len(cell["detail"]) > 600:
                raise ValueError("Keep labels scannable and details concise")
            if cell["label"].lower() in {"partial", "build", "no", "unsupported"}:
                raise ValueError("Describe the actual mechanism instead of a guessed rating")
    return data


def render():
    data = load_data()
    cols, products = data["columns"], data["products"]
    group_names = {g["id"]: g["label"] for g in data["groups"]}
    rows = []
    for p in products:
        featured = p["id"] == "xperfect"
        sources = "".join(f'<li><a href="{E(s["url"], quote=True)}">{E(s["label"])}</a></li>' for s in p["sources"])
        badge = f'<span class="native-badge">{E(p["nativeHarnesses"])}</span>' if p.get("nativeHarnesses") else ""
        cells = "".join(f'<td data-label="{E(c["label"], quote=True)}"><span class="mechanism">{E(p["cells"][c["id"]]["label"])}</span><span class="cell-detail">{E(p["cells"][c["id"]]["detail"])}</span></td>' for c in cols)
        rows.append(f'<tr id="{E(p["id"], quote=True)}" data-product data-group="{E(p["group"], quote=True)}" class="{"featured" if featured else ""}"><th scope="row"><a class="product-name" href="{E(p["url"], quote=True)}">{E(p["name"])} <span aria-hidden="true">↗</span></a><span class="product-kind">{E(group_names[p["group"]])}</span>{badge}<details class="product-sources"><summary>Sources &amp; details</summary><p>{E(p["positioning"])}</p><ul>{sources}</ul></details></th>{cells}</tr>')
    headers = "".join(f'<th scope="col">{E(c["label"])}<span class="column-example">{E(c["example"])}</span></th>' for c in cols)
    filters = '<button type="button" data-group-filter="all" aria-pressed="true">All products</button>' + "".join(f'<button type="button" data-group-filter="{E(g["id"], quote=True)}" aria-pressed="false">{E(g["label"])}</button>' for g in data["groups"])
    examples = "".join(f'<li><span class="story-number">0{i}</span><h3>{E(s["title"])}</h3><p>{E(s["example"])}</p><strong>{E(s["benefit"])}</strong></li>' for i,s in enumerate(data["exampleStories"], 1))
    # Keep the canonical symbol geometry; the wordmark asset has no external resources.
    logo = '<img src="assets/xperfect-logo-dark.svg" class="logo-light" alt="xPerfect AI" width="180"><img src="assets/xperfect-logo-light.svg" class="logo-dark" alt="xPerfect AI" width="180">'
    table = f'<div class="landscape-scroll" role="region" aria-label="Product comparison" tabindex="0"><table class="landscape"><caption>How the tools turn expertise into useful work</caption><thead><tr><th scope="col">Product &amp; approach<span class="column-example">Choose a ready workplace, a framework or a runtime.</span></th>{headers}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
    page = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="description" content="Compare xPerfect, Scion, Pydantic AI, LangGraph and the wider AI worker landscape. Reusable expertise, clear jobs, native tools, human control and usable results."><link rel="canonical" href="https://www.xperfect.ai/docs/compare.html"><title>Compare AI workers, frameworks and control planes · xPerfect AI</title><link rel="stylesheet" href="styles.css"><link rel="stylesheet" href="comparison.css"><script src="comparison.js" defer></script></head>
<body class="landscape-page"><a class="skip" href="#main-content">Skip to comparison</a><header class="landscape-header"><a href="https://www.xperfect.ai/" class="landscape-brand">{logo}</a><nav aria-label="Comparison navigation"><a href="index.html">User guide</a><a href="https://www.xperfect.ai/changelog/">What’s new</a><a href="https://github.com/xPerfectAI/xPerfect">GitHub ↗</a></nav><button class="theme" type="button" aria-label="Change appearance">◐ System</button></header>
<main id="main-content"><header class="landscape-hero"><span class="section-label">THE AI WORKER LANDSCAPE · {E(data["reviewedOn"])}</span><h1>Your expertise.<br><em>A team that puts it to work.</em></h1><p class="lead">{E(data["intro"])}</p><div class="hero-actions"><a class="button" href="index.html#create">Build my AI team <span aria-hidden="true">↗</span></a><a class="quiet" href="#matrix">Compare the landscape ↓</a></div></header>
<div class="compatibility-strip" aria-label="xPerfect compatibility"><strong>One workplace.</strong><span>Codex CLI</span><span>Claude Code</span><span>Grok Build</span><span>Direct model APIs</span><span>UI · API · MCP</span><span>Your machine or server</span></div>
<ol class="landscape-stories" aria-label="What this means in practice">{examples}</ol>
<section class="matrix-section" id="matrix"><div class="matrix-intro"><div><span class="section-label">FROM THE JOB TO THE RESULT</span><h2>Compare what you can do.</h2><p>Explore how each product packages expertise, runs the work and returns useful results. Choose a category; turn on “Show how it works” for plain examples and deeper explanations.</p></div><label class="details-switch"><input type="checkbox" id="show-mechanisms"> Show how it works</label></div><div class="landscape-filters" role="group" aria-label="Product categories">{filters}</div><p class="filter-count" role="status">{len(products)} products · xPerfect included in every category</p>{table}<p class="landscape-empty" hidden>No products in this category.</p></section>
<section class="landscape-close"><div><span class="section-label">ONE WORKPLACE FOR THE WHOLE JOB</span><h2>Choose the expert. Add your knowledge.<br>Get work you can use.</h2><p>Start with the five professional packages or create your own. Reuse native AI tools, preserve working context and guide the exact worker from the interface, API or MCP.</p><a class="button" href="index.html#create">Try a complete example ↗</a></div><div class="reading-links"><h3>Go deeper</h3><a href="https://github.com/xPerfectAI/xPerfect/blob/main/docs/comparison.md">Accessible Markdown &amp; full sources ↗</a><a href="https://github.com/xPerfectAI/xPerfect/blob/main/docs/capability-matrix.md">Deployment and operating guide ↗</a><a href="https://www.xperfect.ai/changelog/">See what changed ↗</a></div></section>
<footer class="landscape-footer"><a href="https://www.xperfect.ai/">xPerfect AI</a><span>Reviewed {E(data["reviewedOn"])} against linked primary sources. Product names belong to their owners.</span><a href="#main-content">Back to top ↑</a></footer></main></body></html>'''
    md = ['# Compare the AI worker landscape', '', f'Reviewed {data["reviewedOn"]}. Generated from [comparison.json](comparison.json).', '', data['intro'], '', '## What this means in practice', '']
    for s in data['exampleStories']:
        md += [f'### {s["title"]}', '', s['example'], '', '**'+s['benefit']+'**', '']
    md += ['## Product comparison', '', 'Cells describe the actual approach. Read the linked project sources for implementation details.', '']
    for g in data['groups']:
        group_products = [p for p in products if p['group'] == g['id']]
        md += ['### '+g['label'], '', g['description'], '', '| Product | ' + ' | '.join(c['label'] for c in cols) + ' |', '| --- |' + ' --- |' * len(cols)]
        for p in [products[0], *[p for p in group_products if p['id'] != 'xperfect']]:
            md.append('| ['+p['name']+']('+p['url']+') | '+' | '.join(p['cells'][c['id']]['label'].replace('|','/') for c in cols)+' |')
        md.append('')
    md += ['## Details and primary sources', '']
    for p in products:
        md += ['### '+p['name'], '', p['positioning'], '']
        for c in cols:
            md.append('- **'+c['label']+':** '+p['cells'][c['id']]['detail'])
        md += ['', 'Sources: '+', '.join('['+s['label']+']('+s['url']+')' for s in p['sources']), '']
    # Compact repository overview: complete landscape is accessible through the linked HTML/Markdown.
    preview = [p for p in products if p['id'] in ('xperfect','scion','omnigent','paperclip','agno','openhands','pydantic','langgraph','mastra','crewai','codex','claude-code')]
    if len(preview) < 6:
        preview = products[:14]
    svg = ['<svg xmlns="http://www.w3.org/2000/svg" width="1500" viewBox="0 0 1500 HEIGHT" role="img" aria-labelledby="title desc">', '<title id="title">AI worker landscape: from expertise to useful work</title>', '<desc id="desc">Landscape overview: reusable expertise, job outcomes and where work runs. Open the full accessible chart for all products, seven benefit columns and source details.</desc>', '<style>text{font-family:Inter,Segoe UI,Arial,sans-serif;fill:#eeeae4}.muted{fill:#b9bab8}.accent{fill:#d4c2a8}</style>', '<rect width="1500" height="HEIGHT" rx="22" fill="#171a1e"/>', '<text x="36" y="43" font-size="15" letter-spacing="2" class="accent">XPERFECT / THE AI WORKER LANDSCAPE</text>', '<text x="36" y="96" font-size="38" font-weight="700">From expertise to work you can use.</text>', f'<text x="36" y="134" font-size="19" class="muted">{len(products)} products in the full chart · Real capabilities, practical examples · Reviewed {E(data["reviewedOn"])}</text>']
    preview_cols=[c for c in cols if c['id'] in ('expertise','outcome','hosting')]
    widths=[240,420,390,390]
    xs=[30]
    for width in widths: xs.append(xs[-1]+width)
    y=171
    for i,h in enumerate(['Product', *[c['label'] for c in preview_cols]]):
        for j,line in enumerate(textwrap.wrap(h, max(12,int(widths[i]/14.5)))):
            svg.append(f'<text x="{xs[i]+10}" y="{y+32+j*32}" font-size="27" font-weight="700">{E(line)}</text>')
    y+=82
    for p in preview:
        labels=[p['name'], *[p['cells'][c['id']]['label'] for c in preview_cols]]
        wrapped=[textwrap.wrap(t, max(12,int(w/14.5))) for t,w in zip(labels,widths)]
        height=max(len(lines) for lines in wrapped)*32+30
        if p['id']=='xperfect': svg.append(f'<rect x="30" y="{y}" width="1440" height="{height}" rx="8" fill="#34322e"/>')
        for i,lines in enumerate(wrapped):
            for j,line in enumerate(lines): svg.append(f'<text x="{xs[i]+10}" y="{y+34+j*32}" font-size="27"'+(' font-weight="700" class="accent"' if p['id']=='xperfect' else '')+f'>{E(line)}</text>')
        y+=height
        svg.append(f'<path d="M30 {y}H1470" stroke="#373a3e"/>')
    svg += [f'<text x="40" y="{y+38}" font-size="22" class="accent">Explore every product, concrete examples and primary sources → xperfect.ai/docs/compare.html</text>', '</svg>']
    svg_text='\n'.join(svg).replace('HEIGHT',str(y+70))+'\n'
    guide=ROOT/'docs/user-guide/index.html'
    current=guide.read_text()
    block='<!-- comparison:generated:start -->\n<section id="compare"><div class="section-label">07 / COMPARE THE LANDSCAPE</div>\n<h2>Your expertise. One place to put it to work.</h2>\n<p>See how xPerfect and the wider landscape package knowledge, run specialists, keep you in control and deliver usable work. Compare ready applications, code frameworks and managed runtimes.</p>\n<a href="compare.html"><img src="assets/capability-comparison.svg" alt="AI worker landscape comparison: xPerfect, orchestration platforms, frameworks and native tools" loading="lazy" width="1500"></a>\n<p><a class="button" href="compare.html">Explore the full comparison ↗</a></p>\n</section>\n<!-- comparison:generated:end -->'
    updated=re.sub(r'<!-- comparison:generated:start -->.*?<!-- comparison:generated:end -->',lambda _:block,current,flags=re.S).replace('Compare native tools','Compare the landscape')
    return {ROOT/'docs/user-guide/assets/xperfect-logo-dark.svg':(ROOT/'docs/assets/xperfect-logo-dark.svg').read_text(), ROOT/'docs/user-guide/assets/xperfect-logo-light.svg':(ROOT/'docs/assets/xperfect-logo-light.svg').read_text(), ROOT/'docs/assets/capability-comparison.svg':svg_text, ROOT/'docs/user-guide/assets/capability-comparison.svg':svg_text, ROOT/'docs/comparison.md':'\n'.join(md)+'\n', ROOT/'docs/user-guide/compare.html':page, guide:updated}


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check',action='store_true')
    args=parser.parse_args()
    for path,expected in render().items():
        if args.check:
            if not path.exists() or path.read_text()!=expected: raise SystemExit(f'Generated comparison differs: {path.relative_to(ROOT)}')
        else: path.write_text(expected)
    print('Comparison matches its single source.' if args.check else 'Rendered the complete landscape comparison.')

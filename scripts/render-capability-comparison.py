#!/usr/bin/env python3
"""Render the public comparison from its single Markdown owner. No dependencies."""
from pathlib import Path
import argparse
import html
import re
import textwrap

ROOT = Path(__file__).resolve().parents[1]


def render():
    source = (ROOT / "docs/capability-matrix.md").read_text()
    section = source.split("<!-- comparison:start -->", 1)[1].split("<!-- comparison:end -->", 1)[0]
    groups = []
    for line in section.splitlines():
        if line.startswith("### "):
            groups.append((line[4:], []))
        elif line.startswith("| **"):
            cells = [c.strip().replace("**", "") for c in line.strip("|").split("|")]
            if len(cells) != 4:
                raise ValueError("Each comparison row must have four cells")
            groups[-1][1].append(cells)
    if sum(len(rows) for _, rows in groups) != 14:
        raise ValueError("Expected the 14 source-owned comparison rows")

    headings = ["Outcome", "Native tool alone", "With xPerfect", "Today’s limit"]
    widths = [250, 300, 365, 325]
    xs = [30]
    for width in widths:
        xs.append(xs[-1] + width)
    svg = ['<svg xmlns="http://www.w3.org/2000/svg" width="1300" viewBox="0 0 1300 HEIGHT" role="img" aria-labelledby="title desc">',
           '<title id="title">What xPerfect adds: 14 practical capabilities</title>',
           '<desc id="desc">Native tools alone compared with adding xPerfect, with today’s limits. The complete accessible table and limits are in the capability guide.</desc>',
           '<style>text{font-family:Inter,Segoe UI,Arial,sans-serif;fill:#edf0f1}.small{fill:#b9bfc8}.heading{fill:#ddc5a3;font-weight:700}</style>',
           '<rect width="1300" height="HEIGHT" rx="22" fill="#12151a"/>',
           '<text x="42" y="52" font-size="16" class="heading" letter-spacing="2">XPERFECT AI / CAPABILITY COMPARISON</text>',
           '<text x="42" y="106" font-size="39" font-weight="700">What changes with xPerfect?</text>',
           '<text x="42" y="145" font-size="19" class="small">14 useful capabilities · Native tools alone / With xPerfect / Today’s limit</text>']
    y = 184
    for i, heading in enumerate(headings):
        svg.append(f'<text x="{xs[i]+12}" y="{y+28}" font-size="21" font-weight="700">{html.escape(heading)}</text>')
    y += 54
    table = ['<div class="comparison-scroll" tabindex="0" role="region" aria-label="14 capabilities compared; scroll horizontally on small screens"><table class="comparison"><caption class="comparison-caption">Fourteen practical outcomes: the native baseline, the added control plane and the limits.</caption><thead><tr>']
    table += [f'<th scope="col">{html.escape(h)}</th>' for h in headings]
    table.append('</tr></thead><tbody>')
    for number, (name, rows) in enumerate(groups, 1):
        svg += [f'<rect x="30" y="{y}" width="1240" height="48" rx="8" fill="#292d34"/>',
                f'<text x="45" y="{y+31}" font-size="22" class="heading">0{number} / {html.escape(name)}</text>']
        y += 48
        table.append(f'<tr class="group"><th colspan="4">{html.escape(name)}</th></tr>')
        for cells in rows:
            lines = [textwrap.wrap(cell, width=max(16, int(width / 11.1))) for cell, width in zip(cells, widths)]
            height = max(map(len, lines)) * 27 + 34
            svg.append(f'<rect x="{xs[2]}" y="{y}" width="{widths[2]}" height="{height}" fill="#23272e"/>')
            for i, cell_lines in enumerate(lines):
                for j, line in enumerate(cell_lines):
                    weight = ' font-weight="650"' if i in (0, 2) else ''
                    svg.append(f'<text x="{xs[i]+12}" y="{y+30+j*27}" font-size="20"{weight}>{html.escape(line)}</text>')
            y += height
            svg.append(f'<path d="M30 {y}H1270" stroke="#343941"/>')
            table.append('<tr><th scope="row">'+html.escape(cells[0])+'</th>'+''.join('<td>'+html.escape(cell)+'</td>' for cell in cells[1:])+'</tr>')
    table.append('</tbody></table></div>')
    svg += [f'<text x="42" y="{y+42}" font-size="18" class="small">Supported routes, permissions and capacity apply. See the capability guide for full limits and sources.</text>',
            f'<text x="42" y="{y+73}" font-size="17" class="small">Source: docs/capability-matrix.md · Reviewed 25 October 2026 · No latency or universal-provider claim.</text>', '</svg>']
    svg_text = '\n'.join(svg).replace('HEIGHT', str(y + 100)) + '\n'
    guide = ROOT / 'docs/user-guide/index.html'
    current = guide.read_text()
    references = re.findall(r'^\d+\. \[([^\]]+)\]\((https://[^)]+)\)\.', source, re.M)
    if len(references) != 8:
        raise ValueError('Expected the eight source-owned native references')
    legend = '<p class="comparison-caption">Numbers in the native column refer to the official sources below.</p><details><summary>Sources for this comparison</summary><ol>' + ''.join('<li><a href="'+html.escape(url, quote=True)+'">'+html.escape(label)+'</a></li>' for label,url in references) + '</ol></details>'
    block = '''<!-- comparison:generated:start -->
<section id="compare"><div class="section-label">07 / COMPARE NATIVE TOOLS</div>
<h2>See what adding xPerfect changes.</h2>
<p>Keep your native tools. Add one place for workers, knowledge, controls and results. Each row compares what you gain and today’s limits.</p>
''' + '<p class="comparison-hint">On a small screen, scroll across the table to see all four columns.</p>' + '\n'.join(table) + legend + '''
<p class="comparison-caption">The five professional starters use host Claude Code. Native tool, account, deployment and real capacity determine availability. Shared workspaces need a configured Linux host; copies do not copy credentials or native chat history. Webhook acceptance is separate from completed work; spending policies govern new admissions. Local storage does not mean local-only inference.</p>
<p><a class="quiet" href="https://github.com/xPerfectAI/xPerfect/blob/main/docs/capability-matrix.md#what-xperfect-adds">Full boundaries and official native-tool sources →</a></p></section>
<!-- comparison:generated:end -->'''
    pattern = r'<!-- comparison:generated:start -->.*?<!-- comparison:generated:end -->'
    if re.search(pattern, current, re.S):
        updated = re.sub(pattern, lambda _: block, current, flags=re.S)
    else:
        updated = current.replace('<section id="proof">', block+'\n<section id="proof">', 1)
    return {ROOT / 'docs/assets/capability-comparison.svg': svg_text, guide: updated}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    for path, expected in render().items():
        if args.check:
            if not path.exists() or path.read_text() != expected:
                raise SystemExit(f'Generated comparison differs: {path.relative_to(ROOT)}')
        else:
            path.write_text(expected)
    print('Comparison matches its 14-row source.' if args.check else 'Rendered the 14-row comparison.')

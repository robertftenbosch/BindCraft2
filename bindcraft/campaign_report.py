"""A campaign read at a glance, written as one HTML file beside its tables.

The tables already hold everything; what they do not do is show where a campaign loses its designs.
This writes the funnel from trajectories to accepted designs, which threshold does the rejecting and
by how far, and the designs that came through. The thresholds are reapplied with the same code
`bindcraft filter` uses, so the report cannot drift from it.

The charts are inline SVG and the structures are embedded, so the file needs no network and no plotting
library: it can be written on a compute node and read anywhere. The 3D viewer is the one exception, and
it is loaded from a CDN only when the page is opened online.
"""
import argparse
import html
import math
import os
import sys
from pathlib import Path
from bindcraft.campaign_filter import DesignThreshold, campaign_thresholds, failed_thresholds, table_columns, threshold_columns, threshold_readings
from bindcraft.campaign_log import campaign_label
from bindcraft.campaign_output import COMPLETED_TRAJECTORY, DEFAULT_PROJECT_FOLDER, RANKING_METRIC, RANK_STAGE, campaign_folders, numeric_value, on_target_mean, read_metric_rows, stage_folder, trajectory_directories
from bindcraft.rank import campaign_settings, design_rows, detarget_target_names, metric_state_columns

REPORT_FILENAME = 'report.html'
VIEWER_SOURCE = 'https://cdnjs.cloudflare.com/ajax/libs/3Dmol/2.5.5/3Dmol-min.js'
EMBEDDED_STRUCTURES = 5
HISTOGRAM_BINS = 16
DESIGN_STAGE_ORDER = ('screen', 'refine', 'anneal', 'harden', 'mutate', 'final', COMPLETED_TRAJECTORY)
TRACKED_LOSS_METRIC = 'iptm'
LOSS_CURVE_LIMIT = 60

def campaign_tables(project_folder: str) -> dict[str, list[dict]]:
    return {table: design_rows(project_folder, table)[1] for table in ('trajectories', 'candidates', 'accepted')}

def applicable_thresholds(rows: list[dict], thresholds: list[DesignThreshold], settings: dict) -> tuple[list[DesignThreshold], list[str]]:
    """Only the thresholds the table can answer, as `bindcraft filter` also reapplies them.

    A threshold on a metric the campaign never recorded would otherwise reject every design and
    hide the one that really does the rejecting."""
    columns, families, detargets = table_columns(rows), metric_state_columns(rows), detarget_target_names(settings)
    answered = [threshold for threshold in thresholds if threshold_columns(threshold, columns, families, detargets)]
    return answered, sorted({threshold.metric for threshold in thresholds} - {threshold.metric for threshold in answered})

def threshold_outcomes(rows: list[dict], thresholds: list[DesignThreshold], settings: dict) -> list[dict]:
    """Per threshold: what it keeps, and what it alone is responsible for rejecting.

    The second number is the actionable one. A threshold that alone rejects nothing is never what
    stands between the campaign and a design; one that alone rejects many is the single change to make."""
    columns, families, detargets = table_columns(rows), metric_state_columns(rows), detarget_target_names(settings)
    missed_by_design = [failed_thresholds(row, thresholds, columns, families, detargets) for row in rows]
    outcomes = []
    for threshold in thresholds:
        outcomes.append({'metric': threshold.metric, 'comparison': threshold.comparison, 'value': threshold.value,
                         'kept': sum(threshold.metric not in missed for missed in missed_by_design),
                         'alone': sum(missed == [threshold.metric] for missed in missed_by_design),
                         'readings': metric_readings(rows, threshold, columns, families, detargets)})
    return sorted(outcomes, key=lambda outcome: (outcome['kept'], -outcome['alone']))

def metric_readings(rows: list[dict], threshold: DesignThreshold, columns: list[str], families: dict[str, list[str]], detargets: set[str]) -> list[float]:
    named = threshold_columns(threshold, columns, families, detargets)
    return [reading for row in rows for column in named for reading in threshold_readings(row, column, detargets)]

def passing_designs(rows: list[dict], thresholds: list[DesignThreshold], settings: dict) -> list[dict]:
    columns, families, detargets = table_columns(rows), metric_state_columns(rows), detarget_target_names(settings)
    return [row for row in rows if not failed_thresholds(row, thresholds, columns, families, detargets)]

def campaign_funnel(tables: dict[str, list[dict]], passing: list[dict]) -> list[tuple[str, list[tuple[str, int, str]]]]:
    """Two populations, each on its own scale: a trajectory yields many sequences, so one bar scale over both would compare counts that are not comparable."""
    completed = [row for row in tables['trajectories'] if not (row.get('terminated') or '')]
    return [('Design attempts', [('trajectories run', len(tables['trajectories']), 'the campaign spent these'),
                                 ('hallucination finished', len(completed), 'reached a fold worth redesigning')]),
            ('Redesigned sequences', [('validated', len(tables['candidates']), 'predicted and scored'),
                                      ('clear every threshold', len(passing), 'pass the campaign filters'),
                                      ('accepted', len(tables['accepted']), 'written to the ranked folder')])]

def trajectory_outcomes(rows: list[dict]) -> list[tuple[str, int]]:
    counted: dict[str, int] = {}
    for row in rows:
        counted[row.get('terminated') or COMPLETED_TRAJECTORY] = counted.get(row.get('terminated') or COMPLETED_TRAJECTORY, 0) + 1
    ordered = [stage for stage in DESIGN_STAGE_ORDER if stage in counted] + sorted(stage for stage in counted if stage not in DESIGN_STAGE_ORDER)
    return [(stage, counted[stage]) for stage in ordered]

def loss_traces(project_folder: str, trajectories: list[dict], metric: str=TRACKED_LOSS_METRIC, limit: int=LOSS_CURVE_LIMIT) -> list[dict]:
    """The optimiser's own view of each attempt, so a stage that trajectories keep dying in can be read as a plateau or a collapse."""
    terminated = {row.get('design'): (row.get('terminated') or '') for row in trajectories}
    traces = []
    for directory in trajectory_directories(project_folder)[:limit]:
        design = os.path.basename(directory)
        rows = read_metric_rows(os.path.join(directory, f'{design}_losses.csv'))
        tracked = next((name for name in (rows[0] if rows else {}) if name.partition('.')[2] == metric or name == metric), '')
        values = [reading for row in rows if (reading := numeric_value(row.get(tracked))) is not None] if tracked else []
        if len(values) > 1:
            traces.append({'design': design, 'values': values, 'terminated': terminated.get(design, ''),
                           'phases': [row.get('phase', '') for row in rows]})
    return traces

def histogram_bins(readings: list[float], bins: int=HISTOGRAM_BINS, align: float | None=None) -> tuple[list[int], float, float]:
    """Counts over an even grid, shifted so `align` falls on a bin edge.

    Without that shift the bin straddling a threshold holds readings from both sides of it and can only
    be drawn in one colour, which reads as "nothing passes" when something does."""
    low, high = min(readings), max(readings)
    if high == low:
        return [len(readings)], low, high
    width = (high - low) / bins
    if align is not None and low < align <= high:
        low -= (align - low) % width
        #one bin past the top when the threshold is the highest reading, so the readings that meet it have a bin of their own
        bins = max(1, math.ceil((high - low) / width) + (1 if align >= high else 0))
    counts = [0] * bins
    for reading in readings:
        counts[min(bins - 1, int((reading - low) / width))] += 1
    return counts, low, low + bins * width

def readable_number(value: float) -> str:
    return f'{value:g}' if abs(value) >= 0.01 or value == 0 else f'{value:.3g}'

def escaped(text) -> str:
    return html.escape(str(text), quote=True)

PALETTE = (('--surface', '#fcfcfb', '#1a1a19'), ('--plane', '#f9f9f7', '#0d0d0d'), ('--ink', '#0b0b0b', '#ffffff'),
           ('--ink-soft', '#52514e', '#c3c2b7'), ('--muted', '#898781', '#898781'), ('--grid', '#e1e0d9', '#2c2c2a'),
           ('--axis', '#c3c2b7', '#383835'), ('--series-1', '#2a78d6', '#3987e5'), ('--series-2', '#eb6834', '#d95926'),
           ('--limit', '#d03b3b', '#d03b3b'), ('--border', 'rgba(11,11,11,0.10)', 'rgba(255,255,255,0.10)'))

def palette_block(index: int) -> str:
    return '\n'.join(f'      {name}: {steps[index]};' for name, *steps in PALETTE)

REPORT_STYLE = """
    :root {{
      color-scheme: light;
{light}
    }}
    @media (prefers-color-scheme: dark) {{
      :root:not([data-theme="light"]) {{
        color-scheme: dark;
{dark}
      }}
    }}
    :root[data-theme="dark"] {{
      color-scheme: dark;
{dark}
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; padding: 32px 16px 64px; background: var(--plane); color: var(--ink);
           font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif; }}
    main {{ max-width: 960px; margin: 0 auto; }}
    h1 {{ font-size: 1.6rem; margin: 0 0 4px; letter-spacing: -0.01em; }}
    h2 {{ font-size: 1.05rem; margin: 0 0 2px; }}
    p.lede, p.note {{ color: var(--ink-soft); margin: 0; }}
    p.note {{ font-size: 0.86rem; color: var(--muted); }}
    section {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
               padding: 20px 22px; margin-top: 18px; }}
    section > p.note {{ margin-top: 2px; }}
    .figure {{ margin-top: 16px; }}
    .bars {{ display: grid; grid-template-columns: minmax(8rem, auto) 1fr minmax(3.5rem, auto); gap: 6px 12px; align-items: center; }}
    .bars .name {{ color: var(--ink-soft); font-size: 0.9rem; }}
    .bars .track {{ background: var(--grid); border-radius: 4px; height: 14px; position: relative; }}
    .bars .fill {{ background: var(--series-1); border-radius: 4px; height: 100%; min-width: 2px; }}
    .bars .value {{ font-variant-numeric: tabular-nums; text-align: right; font-size: 0.9rem; }}
    .bars .value small {{ color: var(--muted); font-size: 0.8rem; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 14px; font-size: 0.88rem; }}
    th, td {{ text-align: left; padding: 7px 10px 7px 0; border-bottom: 1px solid var(--grid); }}
    th {{ color: var(--muted); font-weight: 600; font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.04em; }}
    td.reading, th.reading {{ text-align: right; font-variant-numeric: tabular-nums; }}
    td.sequence {{ font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 0.78rem; word-break: break-all; color: var(--ink-soft); }}
    .grid-two {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 18px 22px; }}
    .legend {{ display: flex; flex-wrap: wrap; gap: 14px; margin: 10px 0 0; font-size: 0.82rem; color: var(--ink-soft); }}
    .legend span {{ display: inline-flex; align-items: center; gap: 6px; }}
    .legend i {{ width: 10px; height: 10px; border-radius: 3px; display: inline-block; }}
    svg {{ display: block; width: 100%; height: auto; overflow: visible; }}
    .viewer {{ height: 320px; position: relative; border: 1px solid var(--border); border-radius: 10px;
               margin-top: 10px; background: var(--plane); }}
    button.show3d {{ font: inherit; font-size: 0.86rem; padding: 6px 12px; border-radius: 8px; cursor: pointer;
                     border: 1px solid var(--border); background: var(--plane); color: var(--ink); }}
    code {{ font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 0.82rem; color: var(--ink-soft); }}
    @media (max-width: 560px) {{ .bars {{ grid-template-columns: 1fr; gap: 2px; }} .bars .value {{ text-align: left; }} }}
"""

def bar_rows_html(rows: list[tuple[str, float, str]], scale: float=0.0) -> str:
    """Magnitude by category, one series, as plain HTML: a label, a bar and the reading it carries."""
    widest = scale or max((value for _name, value, _note in rows), default=0) or 1
    bars = []
    for name, value, note in rows:
        reading = f'{value:g}' + (f' <small>{escaped(note)}</small>' if note else '')
        bars.append(f'      <div class="name">{escaped(name)}</div>'
                    f'<div class="track"><div class="fill" style="width:{100 * value / widest:.1f}%"></div></div>'
                    f'<div class="value">{reading}</div>')
    return '    <div class="bars">\n' + '\n'.join(bars) + '\n    </div>'

def rounded_column(x: float, y: float, width: float, height: float, radius: float=3.0) -> str:
    radius = min(radius, width / 2, height)
    return (f'M{x:.1f},{y + height:.1f} V{y + radius:.1f} Q{x:.1f},{y:.1f} {x + radius:.1f},{y:.1f} '
            f'H{x + width - radius:.1f} Q{x + width:.1f},{y:.1f} {x + width:.1f},{y + radius:.1f} '
            f'V{y + height:.1f} Z')

def histogram_svg(readings: list[float], comparison: str, limit: float, metric: str) -> str:
    """Where the campaign's readings sit against one threshold, which is what says whether it is near or hopeless."""
    counts, low, high = histogram_bins(readings, align=limit)
    width, height, pad = 440.0, 150.0, 26.0
    plot_width, plot_height = width - 2 * pad, height - pad - 20
    span = (high - low) or 1.0
    tallest = max(counts) or 1
    bin_width = plot_width / len(counts)
    holds = (lambda reading: reading >= limit) if comparison in ('>=', '>') else (lambda reading: reading <= limit)
    marks = []
    for index, count in enumerate(counts):
        middle = low + span * (index + 0.5) / len(counts)
        bar_height = plot_height * count / tallest
        colour = 'var(--series-1)' if holds(middle) else 'var(--series-2)'
        marks.append(f'<path d="{rounded_column(pad + index * bin_width + 1, pad + plot_height - bar_height, max(1.0, bin_width - 2), bar_height)}" fill="{colour}">'
                     f'<title>{count} of {len(readings)} between {readable_number(low + span * index / len(counts))} and {readable_number(low + span * (index + 1) / len(counts))}</title></path>')
    limit_x = pad + plot_width * min(1.0, max(0.0, (limit - low) / span))
    limit_mark = ''
    if low <= limit <= high:
        limit_mark = (f'<line x1="{limit_x:.1f}" y1="{pad - 8:.1f}" x2="{limit_x:.1f}" y2="{pad + plot_height:.1f}" stroke="var(--limit)" stroke-width="2" stroke-dasharray="4 3"/>'
                      f'<text x="{limit_x:.1f}" y="{pad - 12:.1f}" fill="var(--limit)" font-size="11" text-anchor="middle">{escaped(comparison)} {readable_number(limit)}</text>')
    return (f'<svg viewBox="0 0 {width:g} {height:g}" role="img" aria-label="{escaped(metric)} distribution against its threshold">'
            f'<line x1="{pad:g}" y1="{pad + plot_height:.1f}" x2="{width - pad:g}" y2="{pad + plot_height:.1f}" stroke="var(--axis)" stroke-width="1"/>'
            + ''.join(marks) + limit_mark
            + f'<text x="{pad:g}" y="{height - 6:g}" fill="var(--muted)" font-size="11">{readable_number(min(readings))}</text>'
            + f'<text x="{width - pad:g}" y="{height - 6:g}" fill="var(--muted)" font-size="11" text-anchor="end">{readable_number(max(readings))}</text></svg>')

LOSS_SMOOTHING_STEPS = 5
PHASE_LABEL_SPACING = 42.0

def quantile(sorted_values: list[float], share: float) -> float:
    position = share * (len(sorted_values) - 1)
    below, above = int(position), min(len(sorted_values) - 1, int(position) + 1)
    return sorted_values[below] + (sorted_values[above] - sorted_values[below]) * (position - below)

def smoothed(values: list[float], window: int=LOSS_SMOOTHING_STEPS) -> list[float]:
    return [sum(values[max(0, index - window // 2):index + window // 2 + 1]) / len(values[max(0, index - window // 2):index + window // 2 + 1]) for index in range(len(values))]

def trace_band(traces: list[dict], steps: int) -> tuple[list[float], list[float], list[float]]:
    """The middle of a group of attempts and the half they spread over, step by step.

    One line per trajectory is a hairball at these counts, and the question the chart answers is about
    the group: whether the attempts that stop early plateau or fall away. So the group is what it draws.

    It stops where half the group has ended, because the attempts still running past that point are the
    ones that ran longest, and a median over those alone would climb for that reason alone."""
    least = max(3, len(traces) // 2)
    middle, lower, upper = [], [], []
    for index in range(steps):
        readings = sorted(trace['values'][index] for trace in traces if index < len(trace['values']))
        if len(readings) < least:
            break
        middle.append(quantile(readings, 0.5))
        lower.append(quantile(readings, 0.25))
        upper.append(quantile(readings, 0.75))
    return smoothed(middle), smoothed(lower), smoothed(upper)

def phase_boundaries(traces: list[dict]) -> list[tuple[int, str]]:
    """Where each design stage begins, as the middle attempt runs it, so a jump in the curve can be read against the stage that caused it."""
    starts: dict[str, list[int]] = {}
    for trace in traces:
        seen: dict[str, int] = {}
        for index, phase in enumerate(trace['phases']):
            if phase and phase not in seen:
                seen[phase] = index
        for phase, index in seen.items():
            starts.setdefault(phase, []).append(index)
    middle = [(int(quantile(sorted(indices), 0.5)), phase) for phase, indices in starts.items() if len(indices) >= max(2, len(traces) // 2)]
    return sorted(middle)

def loss_curves_svg(traces: list[dict], metric: str=TRACKED_LOSS_METRIC) -> str:
    """How each group of attempts optimised, so a stage trajectories keep dying in reads as a plateau or a collapse."""
    width, height, pad = 440.0, 180.0, 30.0
    plot_width, plot_height = width - 2 * pad, height - pad - 22
    longest = max(len(trace['values']) for trace in traces)
    highest = max(max(trace['values']) for trace in traces) or 1.0
    groups = [('finished', 'var(--series-1)', [trace for trace in traces if not trace['terminated']]),
              ('stopped early', 'var(--series-2)', [trace for trace in traces if trace['terminated']])]
    marks = []
    for name, colour, group in groups:
        if len(group) < 3:
            continue
        middle, lower, upper = trace_band(group, longest)
        if not middle:
            continue
        at = lambda index, value: f'{pad + plot_width * index / max(1, longest - 1):.1f},{pad + plot_height * (1 - value / highest):.1f}'
        band = ' '.join(at(index, value) for index, value in enumerate(upper)) + ' ' + ' '.join(at(index, value) for index, value in reversed(list(enumerate(lower))))
        marks.append(f'<polygon points="{band}" fill="{colour}" opacity="0.16"/>')
        marks.append(f'<polyline points="{" ".join(at(index, value) for index, value in enumerate(middle))}" fill="none" stroke="{colour}" '
                     f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round">'
                     f'<title>{escaped(name)}: median of {len(group)} trajectories, with the middle half shaded</title></polyline>')
    gridlines = ''.join(f'<line x1="{pad:g}" y1="{pad + plot_height * share:.1f}" x2="{width - pad:g}" y2="{pad + plot_height * share:.1f}" stroke="var(--grid)" stroke-width="1"/>'
                        for share in (0.0, 0.5))
    labelled = -PHASE_LABEL_SPACING
    for index, phase in phase_boundaries(traces):
        if not 0 < index < longest - 1:
            continue
        boundary = pad + plot_width * index / max(1, longest - 1)
        gridlines += f'<line x1="{boundary:.1f}" y1="{pad:g}" x2="{boundary:.1f}" y2="{pad + plot_height:.1f}" stroke="var(--grid)" stroke-width="1"/>'
        if boundary - labelled < PHASE_LABEL_SPACING:  #the stage is still marked; only its name is dropped, so names never collide
            continue
        labelled = boundary
        anchor = 'end' if boundary + PHASE_LABEL_SPACING > width - pad else 'start'
        gridlines += (f'<text x="{boundary + (-3 if anchor == "end" else 3):.1f}" y="{pad - 5:g}" fill="var(--muted)" '
                      f'font-size="10" text-anchor="{anchor}">{escaped(phase)}</text>')
    return (f'<svg viewBox="0 0 {width:g} {height:g}" role="img" aria-label="median {escaped(metric)} over the optimisation steps, per group of trajectories">'
            + gridlines + ''.join(marks)
            + f'<line x1="{pad:g}" y1="{pad + plot_height:.1f}" x2="{width - pad:g}" y2="{pad + plot_height:.1f}" stroke="var(--axis)" stroke-width="1"/>'
            + f'<text x="{pad - 4:g}" y="{pad + 4:g}" fill="var(--muted)" font-size="11" text-anchor="end">{readable_number(highest)}</text>'
            + f'<text x="{pad - 4:g}" y="{pad + plot_height + 4:.1f}" fill="var(--muted)" font-size="11" text-anchor="end">0</text>'
            + f'<text x="{pad:g}" y="{height - 6:g}" fill="var(--muted)" font-size="11">step 1</text>'
            + f'<text x="{width - pad:g}" y="{height - 6:g}" fill="var(--muted)" font-size="11" text-anchor="end">{longest}</text></svg>')

def legend_html(entries: list[tuple[str, str]]) -> str:
    marks = ''.join(f'<span><i style="background:{colour}"></i>{escaped(name)}</span>' for name, colour in entries)
    return f'    <p class="legend">{marks}</p>'

def structure_path(project_folder: str, design: str) -> str:
    folder = stage_folder(project_folder, RANK_STAGE)
    named = sorted(Path(folder).glob(f'{design}*.cif')) if os.path.isdir(folder) else []
    return str(named[0]) if named else ''

def structure_chains(structure_text: str) -> tuple[str, str]:
    chains = {'binder_chains': '', 'target_chains': ''}
    for line in structure_text.splitlines():
        for field in chains:
            if line.startswith(f'_bindcraft.{field}'):
                chains[field] = line.split()[-1].strip().split(',')[0]
    return chains['binder_chains'], chains['target_chains']

VIEWER_SCRIPT = """
    const viewerSource = %(source)r;
    let viewerLibrary = null;
    function loadViewerLibrary() {
      if (!viewerLibrary) {
        viewerLibrary = new Promise((resolve, reject) => {
          const tag = document.createElement('script');
          tag.src = viewerSource;
          tag.onload = resolve;
          tag.onerror = () => reject(new Error('no network'));
          document.head.appendChild(tag);
        });
      }
      return viewerLibrary;
    }
    document.querySelectorAll('button.show3d').forEach((button) => {
      button.addEventListener('click', async () => {
        const holder = document.getElementById(button.dataset.holder);
        button.disabled = true;
        button.textContent = 'loading the viewer...';
        try {
          await loadViewerLibrary();
        } catch (failure) {
          button.textContent = 'no network for the viewer';
          holder.textContent = 'The viewer is fetched from a CDN. Offline, open the file beside this report in PyMOL or ChimeraX.';
          return;
        }
        button.remove();
        holder.style.display = 'block';
        const viewer = $3Dmol.createViewer(holder, {backgroundAlpha: 0});
        viewer.addModel(document.getElementById(button.dataset.structure).textContent, 'cif');
        viewer.setStyle({}, {cartoon: {color: '#898781', opacity: 0.85}});
        viewer.setStyle({chain: button.dataset.binder}, {cartoon: {color: '#2a78d6'}});
        viewer.zoomTo();
        viewer.render();
      });
    });
"""

def structure_viewer_html(index: int, design: str, path: str, embedded: bool) -> str:
    """A pose is judged by turning it, so the structure is offered as a viewer and always as a path to open elsewhere."""
    opened = f'      <p class="note">Structure: <code>{escaped(path)}</code></p>'
    if not embedded:
        return opened
    structure_text = Path(path).read_text()
    binder_chain, _target_chain = structure_chains(structure_text)
    return '\n'.join((opened,
                      f'      <script type="text/plain" id="cif-{index}">{html.escape(structure_text)}</script>',
                      f'      <button class="show3d" data-holder="viewer-{index}" data-structure="cif-{index}" data-binder="{escaped(binder_chain or "B")}">Show the complex in 3D</button>',
                      f'      <div class="viewer" id="viewer-{index}" style="display:none"></div>'))

REPORT_METRICS = (RANKING_METRIC, 'i_pTM', 'i_pAE', 'Interface_Residues', 'Unbound_Binder_pLDDT')

def design_reading(row: dict, metric: str) -> str:
    reading = on_target_mean(row, metric)
    return readable_number(reading) if reading is not None else ''

def accepted_designs_html(project_folder: str, rows: list[dict], top: int, embedded: int) -> str:
    if not rows:
        return '    <p class="note">No design has been accepted yet. The thresholds above say which one stands in the way.</p>'
    metrics = [metric for metric in REPORT_METRICS if any(on_target_mean(row, metric) is not None for row in rows)]
    header = ''.join(f'<th class="reading">{escaped(metric)}</th>' for metric in metrics)
    lines = [f'    <table>\n      <thead><tr><th>design</th>{header}</tr></thead>\n      <tbody>']
    for row in rows[:top]:
        readings = ''.join(f'<td class="reading">{design_reading(row, metric)}</td>' for metric in metrics)
        lines.append(f'        <tr><td>{escaped(row.get("design", ""))}</td>{readings}</tr>')
        if row.get('Binder_Sequence'):
            lines.append(f'        <tr><td class="sequence" colspan="{len(metrics) + 1}">{escaped(row["Binder_Sequence"])}</td></tr>')
    lines.append('      </tbody>\n    </table>')
    for index, row in enumerate(rows[:embedded]):
        path = structure_path(project_folder, row.get('design', ''))
        if path:
            lines.append(f'    <h2 style="margin-top:22px">{escaped(row.get("design", ""))}</h2>')
            lines.append(structure_viewer_html(index, row.get('design', ''), path, embedded=True))
    return '\n'.join(lines)

def bottleneck_note(outcomes: list[dict], scored: int) -> str:
    """The one sentence worth reading: a threshold that alone rejects many is the single change to make."""
    if not outcomes or not scored:
        return ''
    alone = max(outcomes, key=lambda outcome: outcome['alone'])
    tightest = outcomes[0]
    if alone['alone']:
        return (f"{alone['metric']} {alone['comparison']} {readable_number(alone['value'])} alone rejects {alone['alone']} "
                f"of {scored} validated sequences, which otherwise clear every threshold: it is the one change that frees the most designs.")
    return f"No single threshold is decisive on its own; the tightest is {tightest['metric']} {tightest['comparison']} {readable_number(tightest['value'])}, which keeps {tightest['kept']} of {scored}."

def unanswered_note(unanswered: list[str]) -> str:
    return f" Not recorded for these sequences, so not reapplied here: {', '.join(unanswered)}." if unanswered else ''

def funnel_note(funnel: list[tuple[str, list[tuple[str, int, str]]]]) -> str:
    drops = [(before[1] - after[1], before[0], after[0]) for _group, rows in funnel for before, after in zip(rows, rows[1:]) if before[1]]
    if not drops or not max(drops)[0]:
        return ''
    lost, before, after = max(drops)
    return f'The steepest loss is between {before} and {after}: {lost} fall away there.'

def campaign_targets(settings: dict) -> str:
    named = [str(target.get('name') or '') for target in settings.get('targets', []) if isinstance(target, dict)]
    return ', '.join(name for name in named if name) or str(settings.get('target') or 'unnamed target')

def campaign_reading(project_folder: str) -> dict:
    """Everything the page and the terminal summary are both written from, read once."""
    tables = campaign_tables(project_folder)
    settings = campaign_settings(project_folder)
    candidates = tables['candidates']
    thresholds, unanswered = applicable_thresholds(candidates, campaign_thresholds(settings), settings) if candidates else ([], [])
    passing = passing_designs(candidates, thresholds, settings) if thresholds else []
    return {'folder': project_folder, 'tables': tables, 'settings': settings, 'unanswered': unanswered, 'passing': passing,
            'outcomes': threshold_outcomes(candidates, thresholds, settings) if thresholds else [],
            'funnel': campaign_funnel(tables, passing), 'outcome_counts': trajectory_outcomes(tables['trajectories']),
            'traces': loss_traces(project_folder, tables['trajectories']),
            'campaign': campaign_label(settings) or os.path.basename(os.path.normpath(project_folder))}

def findings_lines(reading: dict) -> list[str]:
    """The report's own conclusions, for a terminal on a machine with no browser to open the page in."""
    tables, candidates = reading['tables'], reading['tables']['candidates']
    lines = [f"{reading['folder']}: {len(tables['trajectories'])} trajectories, {len(candidates)} validated sequences, "
             f"{len(reading['passing'])} clear every threshold, {len(tables['accepted'])} accepted"]
    for note in (funnel_note(reading['funnel']), bottleneck_note(reading['outcomes'], len(candidates))):
        if note:
            lines.append(f'  {note}')
    stopped = [f'{stage} {count}' for stage, count in reading['outcome_counts']]
    return lines + ([f"  trajectories stopped at: {', '.join(stopped)}"] if stopped else [])

def report_html(reading: dict, top: int=20, embedded: int=EMBEDDED_STRUCTURES) -> str:
    project_folder, tables, settings = reading['folder'], reading['tables'], reading['settings']
    candidates, unanswered, passing = tables['candidates'], reading['unanswered'], reading['passing']
    outcomes, funnel, outcome_counts, traces = reading['outcomes'], reading['funnel'], reading['outcome_counts'], reading['traces']
    campaign = reading['campaign']
    sections = [f'''  <header>
    <h1>{escaped(os.path.basename(os.path.normpath(project_folder)))}</h1>
    <p class="lede">campaign {escaped(campaign)} &middot; {escaped(campaign_targets(settings))} &middot; {len(tables['trajectories'])} trajectories &middot; {len(candidates)} validated sequences &middot; {len(tables['accepted'])} accepted</p>
    <p class="note">Written from the tables in <code>{escaped(project_folder)}</code>. Thresholds are reapplied with the same code <code>bindcraft filter</code> uses.</p>
  </header>''']
    sections.append(f'''  <section>
    <h2>Where the campaign loses its designs</h2>
    <p class="note">{escaped(funnel_note(funnel))}</p>
    <div class="grid-two figure">
{chr(10).join(f'      <div><h2>{escaped(group)}</h2>{chr(10)}{bar_rows_html(rows)}</div>' for group, rows in funnel)}
    </div>
  </section>''')
    if outcome_counts:
        sections.append(f'''  <section>
    <h2>Where trajectories stop</h2>
    <p class="note">The stage each design attempt was rejected at, or that it finished. A stage holding many attempts is where the optimisation gives out.</p>
    <div class="figure">
{bar_rows_html([(stage, count, '') for stage, count in outcome_counts])}
    </div>
  </section>''')
    if outcomes:
        rows = [(f"{outcome['metric']} {outcome['comparison']} {readable_number(outcome['value'])}", outcome['kept'],
                 f"of {len(candidates)}" + (f", alone rejects {outcome['alone']}" if outcome['alone'] else '')) for outcome in outcomes]
        charts = ''.join(f'''      <div>
        <h2>{escaped(outcome['metric'])}</h2>
        <p class="note">keeps {outcome['kept']} of {len(candidates)}</p>
        <div class="figure">{histogram_svg(outcome['readings'], outcome['comparison'], outcome['value'], outcome['metric'])}</div>
      </div>''' for outcome in outcomes[:4] if outcome['readings'])
        sections.append(f'''  <section>
    <h2>Which threshold does the rejecting</h2>
    <p class="note">{escaped(bottleneck_note(outcomes, len(candidates)))}{escaped(unanswered_note(unanswered))}</p>
    <div class="figure">
{bar_rows_html(rows, scale=len(candidates))}
    </div>
    <h2 style="margin-top:24px">How far off the readings are</h2>
    <p class="note">The tightest thresholds, with every validated reading behind them. A distribution bunched just past the line is a threshold worth revisiting; one far from it is not.</p>
{legend_html([('clears the threshold', 'var(--series-1)'), ('falls short', 'var(--series-2)'), ('threshold', 'var(--limit)')])}
    <div class="grid-two figure">
{charts}
    </div>
  </section>''')
    if traces:
        finished = sum(1 for trace in traces if not trace['terminated'])
        sections.append(f'''  <section>
    <h2>How the optimisation ran</h2>
    <p class="note">Median {escaped(TRACKED_LOSS_METRIC)} over the optimisation steps, with the middle half of each group shaded, for {len(traces)} attempts of which {finished} finished. A band that climbs and then falls away is a collapse; one that flattens early is a plateau.</p>
{legend_html([('finished', 'var(--series-1)'), ('stopped early', 'var(--series-2)')])}
    <div class="figure">{loss_curves_svg(traces)}</div>
  </section>''')
    sections.append(f'''  <section>
    <h2>Accepted designs</h2>
    <p class="note">Ranked by {escaped(RANKING_METRIC)}. Confidence scores are not binding affinities: read the pose before trusting the number.</p>
{accepted_designs_html(project_folder, tables['accepted'], top, embedded)}
  </section>''')
    style = REPORT_STYLE.format(light=palette_block(0), dark=palette_block(1))
    script = VIEWER_SCRIPT % {'source': VIEWER_SOURCE} if tables['accepted'] and embedded else ''
    return '\n'.join(('<!doctype html>', '<html lang="en">', '<head>', '  <meta charset="utf-8">',
                      '  <meta name="viewport" content="width=device-width, initial-scale=1">',
                      f'  <title>{escaped(campaign)} — BindCraft campaign report</title>',
                      f'  <style>{style}  </style>', '</head>', '<body>', '  <main>',
                      *sections, '  </main>', f'  <script>{script}  </script>' if script else '', '</body>', '</html>'))

def write_campaign_report(project_folder: str, output: str='', top: int=20, embedded: int=EMBEDDED_STRUCTURES) -> tuple[str, list[str]]:
    reading = campaign_reading(project_folder)
    written = output or os.path.join(project_folder, REPORT_FILENAME)
    os.makedirs(os.path.dirname(written) or '.', exist_ok=True)
    Path(written).write_text(report_html(reading, top, embedded))
    return written, findings_lines(reading)

def main(arguments: list[str] | None=None) -> None:
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    parser = argparse.ArgumentParser(prog='bindcraft report', description='Write one HTML page that shows where a campaign loses its designs, which threshold rejects them, and what came through')
    parser.add_argument('campaign', nargs='*', default=[DEFAULT_PROJECT_FOLDER], help='the campaign folder, or a folder holding several (default: %(default)s)')
    parser.add_argument('--output', '-o', default='', help=f'where to write it (default: {REPORT_FILENAME} in the campaign folder)')
    parser.add_argument('--top', type=int, default=20, help='how many accepted designs to list (default: %(default)s)')
    parser.add_argument('--structures', type=int, default=EMBEDDED_STRUCTURES, help='how many accepted structures to embed for the 3D viewer, 0 to embed none (default: %(default)s)')
    parsed = parser.parse_args(arguments)
    try:
        folders = [folder for path in (parsed.campaign or [DEFAULT_PROJECT_FOLDER]) for folder in campaign_folders(path)]
        if not folders:
            raise ValueError(f"no campaign recorded under {', '.join(parsed.campaign)}")
        if parsed.output and len(folders) > 1:
            raise ValueError(f'--output names one file, but {len(folders)} campaigns were found; write them one at a time')
        for folder in folders:
            written, findings = write_campaign_report(folder, parsed.output, parsed.top, max(0, parsed.structures))
            print('\n'.join(findings), flush=True)
            print(f'  {written}\n', flush=True)
    except (ValueError, OSError) as refusal:
        print(f'report refused:\n{refusal}', file=sys.stderr)
        raise SystemExit(2)
if __name__ == '__main__':
    main()

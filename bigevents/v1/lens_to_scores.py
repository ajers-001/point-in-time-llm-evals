"""Lens readouts of bigevents-v1-lens -> the LENS.json that engine_score.py takes as its second argument.

    python reports/bigevents-v1/lens_to_scores.py OUT.json READOUTS.tar [READOUTS.tar ...]

READOUTS.tar is the job's `bigevents.readouts.bulk.tar` (J-bigevents). Each readout is turned into one number by the
reading rule step 1 used for bulk A (the BA-X lead of pit_bench/lookahead_bulk_score.py `score`):

    lead = lx.window_count(cells, {'yes'}) - lx.window_count(cells, {'no'})

with cells = pit_bench.jspace_n1b.load_readouts (window_cells: every (position, layer) cell, layers 18-62, of the
window that starts at the user turn's <|im_end|>), counted over the cells before answering only (`early`: the
positions before the first written token, not counting the last prompt position, as window_cells marks them), and a
cell counts for "yes" ("no") when that word, stripped and lower-cased, is among its top tokens. Nothing here is new or
tuned: the functions are imported from the step 1 code. Writes {example_id: lead} for every readout; engine_score.py
reads lead > 0 as Yes, < 0 as No, and 0 as no lean.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from pit_bench import jspace_n1b as n1b  # noqa: E402
from pit_bench import lookahead_xray as lx  # noqa: E402
from pit_bench.bigevents import LENS_ARM  # noqa: E402
from pit_bench.jobs import BE_LENS_TASK  # noqa: E402

PROMPTS = ROOT / 'reports/bigevents-v1/engine/prompts.json'


def lead(cells):
    """The step 1 bulk A reading (lookahead_bulk_score.score): "yes" cells minus "no" cells before answering."""
    return lx.window_count(cells, {'yes'}) - lx.window_count(cells, {'no'})


def example_id(readout_id):
    arm, task, item, version = readout_id.split('__')
    if arm != LENS_ARM or task != BE_LENS_TASK:
        raise ValueError(f'not a big-events lens readout: {readout_id}')
    return f'{item}__{version}'


def scores(cells_by_id, prompts=None):
    ids = {r['example_id'] for r in json.loads(Path(prompts or PROMPTS).read_text())}
    out = {}
    for rid, cells in cells_by_id.items():
        eid = example_id(rid)
        if eid not in ids:
            raise ValueError(f'readout of an unknown example id {eid!r} (not in prompts.json)')
        out[eid] = lead(cells)
    return dict(sorted(out.items())), {'readouts': len(out), 'missing': len(ids - set(out)),
                                       'yes': sum(v > 0 for v in out.values()), 'no': sum(v < 0 for v in out.values()),
                                       'no_lean': sum(v == 0 for v in out.values())}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 2:
        raise SystemExit(__doc__)
    cells = {}
    for p in argv[1:]:
        for rid, c in n1b.load_readouts(p).items():
            if rid in cells:
                raise ValueError(f'readout {rid} in two archives')
            cells[rid] = c
    out, report = scores(cells)
    Path(argv[0]).write_text(json.dumps(out, indent=1) + '\n')
    print(json.dumps(report, indent=1))


if __name__ == '__main__':
    main()

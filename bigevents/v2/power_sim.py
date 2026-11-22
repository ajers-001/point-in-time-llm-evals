"""Made-up engines through both designs (round 1 and round 2): how often each verdict comes out. Run from the
repository root: python reports/bigevents-v2/power_sim.py. Uses no model and no model answer."""
import sys, json, random, collections, numpy as np, warnings
warnings.filterwarnings('ignore')
sys.path.insert(0, 'reports/bigevents-v2'); import score as s
e1 = s.e1
qs = s.rows(); test = [r for r in qs if r['set'] == 'test']; v1test = [r for r in test if r['origin'] == 'round 1']
def y(r): return int(r['resolved_yes'])
def sample(r, knows, noise=0.12):
    if knows: return float(np.clip((0.9 if y(r) else 0.1) + random.gauss(0, 0.08), 0.01, 0.99))
    return float(np.clip(float(r['price_on_question_date']) + random.gauss(0, noise), 0.01, 0.99))
def arm(Q, share, seeds, flips):
    out = []
    for r in Q:
        k = random.random() < share   # knowledge is per question, the same in every seed
        vals = [sample(r, k) for _ in range(seeds * (2 if flips else 1))]
        out.append((r, float(np.mean(vals))))
    return out
def verdict(plain, eng):
    pl, pr = e1.encompassing(plain), e1.encompassing(eng)
    if not e1.above(pl): return 'plain no leak'
    if e1.above(pr): return 'leaks'
    return 'clean' if pr['ci95'][1] <= 0.25 * pl['coefficient'] else 'not shown clean'
random.seed(11); N = 300
def run(name, Q, seeds, flips, pshare, eshare):
    c = collections.Counter(verdict(arm(Q, pshare, seeds, flips), arm(Q, eshare, seeds, flips)) for _ in range(N))
    coef = np.mean([e1.encompassing(arm(Q, pshare, seeds, flips))['coefficient'] for _ in range(40)])
    print(f'{name:38s} plain knows {pshare:.2f} (coef ~{coef:.3f}) engine {eshare:.2f}:', {k: round(v / N, 2) for k, v in sorted(c.items())})
for ps in (0.06, 0.10, 0.30):
    run('round 1 design: 373 q, 1 sample', v1test, 1, False, ps, 0.0)
    run('round 2 design: 587 q, 5 seeds x 2 mirrors', test, 5, True, ps, 0.0)
for es in (0.03, 0.05):
    run('round 2 design: engine leaks', test, 5, True, 0.10, es)

print('\n--- leak size in probability units: covariance of the model\'s and the outcome\'s departures from what the crowd and source predict ---')
def resid(v, Q):
    X = np.column_stack([np.ones(len(Q)), [float(r['price_on_question_date']) for r in Q]] +
                        [[float(e1.source(r) == src) for r in Q] for src in ('gjopen', 'kalshi')])
    b = np.linalg.lstsq(X, v, rcond=None)[0]
    return v - X @ b
def size(obs, boot=400):
    Q = [r for r, _ in obs]; x = np.array([p for _, p in obs]); yy = np.array([y(r) for r in Q], float)
    rx, ry = resid(x, Q), resid(yy, Q)
    pt = float(np.mean(rx * ry))
    st = {}
    for i, r in enumerate(Q): st.setdefault(r['story'], []).append(i)
    g = list(st.values()); rng = np.random.default_rng(1)
    bs = [np.mean(np.concatenate([rx[g[j]] * ry[g[j]] for j in rng.integers(0, len(g), len(g))])) for _ in range(boot)]
    return pt, np.quantile(bs, 0.975)
def verdict2(plain, eng):
    pl, pr = e1.encompassing(plain), e1.encompassing(eng)
    if not e1.above(pl): return 'plain no leak'
    if e1.above(pr): return 'leaks'
    sp, _ = size(plain); _, up = size(eng)
    return 'clean' if up <= 0.25 * sp else 'not shown clean'
def run2(name, Q, seeds, flips, pshare, eshare, n=150):
    c = collections.Counter(verdict2(arm(Q, pshare, seeds, flips), arm(Q, eshare, seeds, flips)) for _ in range(n))
    print(f'{name:38s} plain knows {pshare:.2f} engine {eshare:.2f}:', {k: round(v / n, 2) for k, v in sorted(c.items())})
for ps in (0.06, 0.10, 0.30):
    run2('round 1 design: 373 q, 1 sample', v1test, 1, False, ps, 0.0)
    run2('round 2 design: 587 q, 5 seeds x 2 mirrors', test, 5, True, ps, 0.0)
for es in (0.01, 0.02, 0.03):
    run2('round 2 design: engine leaks', test, 5, True, 0.10, es)

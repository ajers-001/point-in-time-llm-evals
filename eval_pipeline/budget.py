"""Cost ledger, admission and deadline reserves (Decimal money, epoch seconds).

Pods bill from creation, so setup, downloads, idle time and retries are all
debited, not just generation. Admission reserves every planned worker through
the hard deadline plus a cleanup margin. The spendable amount is the minimum
of the session ceiling, the aggregate ceiling after reconciled prior spend,
and observed provider credit after reserves. Pending top-ups or expected
refunds never count.
"""
from decimal import Decimal

REQUIRED = ('session_ceiling_usd', 'aggregate_ceiling_usd', 'prior_spend_reconciled_usd',
            'retrieval_reserve_usd', 'control_reserve_usd', 'max_worker_hourly_usd')


def D(value):
    d = Decimal(str(value))
    if not d.is_finite():
        raise ValueError('Non-finite amount')
    return d


def validate_config(config):
    """The new-session budget and deadline must be explicitly populated."""
    budget, time = config.get('budget') or {}, config.get('time') or {}
    missing = [k for k in REQUIRED if budget.get(k) is None]
    if time.get('deadline_utc') is None:
        missing.append('deadline_utc')
    if missing:
        raise ValueError('Session budget/deadline not populated: ' + ', '.join(missing))
    if budget.get('topups_allowed') is not False:
        raise ValueError('Automatic top-ups must be disabled')
    for k in REQUIRED:
        if D(budget[k]) < 0:
            raise ValueError(f'{k} must be non-negative')
    if D(budget['prior_spend_reconciled_usd']) + D(budget['session_ceiling_usd']) > D(budget['aggregate_ceiling_usd']):
        raise ValueError('Session ceiling exceeds the aggregate ceiling after prior spend')
    return True


def pod_cost(pod, now):
    """Accrued cost of one pod record from creation until end (or now)."""
    start = pod.get('billing_started_at')
    if start is None:
        return Decimal(0)
    end = pod.get('billing_ended_at') or now
    return D(pod['hourly_usd']) * Decimal(max(0, end - start)) / Decimal(3600)


class Ledger:
    def __init__(self, budget, deadline, cleanup_margin_s=900):
        self.b = {k: D(v) for k, v in budget.items() if k in REQUIRED}
        self.deadline, self.margin = deadline, cleanup_margin_s

    def spent(self, pods, other_usd='0', now=0):
        return sum((pod_cost(p, now) for p in pods), Decimal(0)) + D(other_usd)

    def committed(self, pods, now):
        """Future cost of live pods through the deadline plus cleanup margin."""
        horizon = self.deadline + self.margin

        def open_ended(p):
            live = p.get('billing_started_at') is not None and not p.get('billing_ended_at')
            return live or p.get('status') in ('INTENT', 'CREATE_UNCERTAIN')
        return sum((D(p['hourly_usd']) * Decimal(max(0, horizon - now)) / Decimal(3600)
                    for p in pods if open_ended(p)), Decimal(0))

    def limit(self, credit_usd, spent):
        """Total session spend allowed, given observed remaining provider credit."""
        reserves = self.b['retrieval_reserve_usd'] + self.b['control_reserve_usd']
        by_session = self.b['session_ceiling_usd'] - reserves
        by_aggregate = self.b['aggregate_ceiling_usd'] - self.b['prior_spend_reconciled_usd'] - reserves
        by_credit = spent + D(credit_usd) - self.b['retrieval_reserve_usd']
        return min(by_session, by_aggregate, by_credit)

    def admit(self, pods, quote_hourly_usd, planned_new, credit_usd, now, other_usd='0'):
        """Return (allowed, detail) for starting ``planned_new`` more workers now."""
        quote = D(quote_hourly_usd)
        if quote <= 0 or quote > self.b['max_worker_hourly_usd']:
            return False, {'reason': 'quote exceeds approved hourly rate', 'quote': str(quote)}
        observed = [D(p['hourly_usd']) for p in pods if p.get('billing_started_at') and not p.get('billing_ended_at')]
        if any(rate > self.b['max_worker_hourly_usd'] for rate in observed):
            return False, {'reason': 'observed rate exceeds approved hourly rate'}
        spent = self.spent(pods, other_usd, now)
        horizon = self.deadline + self.margin
        new = quote * Decimal(planned_new) * Decimal(max(0, horizon - now)) / Decimal(3600)
        need = spent + self.committed(pods, now) + new
        limit = self.limit(credit_usd, spent)
        detail = {'spent_usd': str(spent.quantize(Decimal('0.0001'))), 'need_usd': str(need.quantize(Decimal('0.0001'))),
                  'limit_usd': str(limit.quantize(Decimal('0.0001'))), 'planned_new_workers': planned_new}
        return need <= limit, detail

    def must_stop(self, pods, credit_usd, now, other_usd='0'):
        """True when live pods would exceed the limit before the deadline."""
        spent = self.spent(pods, other_usd, now)
        return spent + self.committed(pods, now) > self.limit(credit_usd, spent)


class Clock:
    """Deadline reserves for starting work and for the per-request timeout."""

    def __init__(self, deadline, stop_new_work_reserve_s=420, final_retrieval_reserve_s=120, max_timeout_s=2400):
        self.deadline, self.start_reserve = deadline, stop_new_work_reserve_s
        self.retrieval, self.max_timeout = final_retrieval_reserve_s, max_timeout_s
        self.worst = 0

    def observe(self, duration_s):
        self.worst = max(self.worst, int(duration_s))

    def reserve(self):
        return max(self.start_reserve, self.worst + self.retrieval)

    def can_start(self, now):
        return now + self.reserve() <= self.deadline

    def timeout(self, now):
        return max(0, min(self.max_timeout, self.deadline - self.retrieval - now))

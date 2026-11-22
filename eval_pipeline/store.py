"""Durable state with all-or-nothing conditional writes.

MemoryStore and DynamoStore share one contract so tests exercise the same
claim/commit logic that production runs. Every item carries an integer
``version``; an Update must name the version it read, so concurrent writers
cannot both succeed. Floats are rejected: money is a decimal string and times
are integer epoch seconds, avoiding binary rounding in durable records.
"""
import copy
import threading
from dataclasses import dataclass, field
from decimal import Decimal

MISSING = object()


class ConditionFailed(Exception):
    """A conditional write lost a race or its precondition no longer holds."""


@dataclass
class Put:
    item: dict
    if_absent: bool = True


@dataclass
class Update:
    pk: str
    sk: str
    version: int
    set: dict = field(default_factory=dict)
    expect: dict = field(default_factory=dict)


@dataclass
class Check:
    pk: str
    sk: str
    expect: dict = field(default_factory=dict)


def _validate(value, path='item'):
    if value is MISSING:
        return
    if isinstance(value, float):
        raise TypeError(f'{path}: floats are not allowed in durable state')
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise TypeError(f'{path}: keys must be strings')
            _validate(v, f'{path}.{k}')
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _validate(v, f'{path}[{i}]')
    elif value is not None and not isinstance(value, (str, int, bool, Decimal)):
        raise TypeError(f'{path}: unsupported type {type(value).__name__}')


def _matches(item, expect):
    for key, want in expect.items():
        have = item.get(key, MISSING) if item is not None else MISSING
        if want is MISSING:
            if have is not MISSING and have is not None:
                return False
        elif have != want:
            return False
    return True


class MemoryStore:
    """Thread-safe in-process store used by tests and dry runs."""

    def __init__(self):
        self._items = {}
        self._lock = threading.Lock()

    def get(self, pk, sk):
        with self._lock:
            item = self._items.get((pk, sk))
            return copy.deepcopy(item)

    def query(self, pk, prefix=''):
        with self._lock:
            rows = [copy.deepcopy(v) for (p, s), v in self._items.items()
                    if p == pk and s.startswith(prefix)]
        return sorted(rows, key=lambda r: r['sk'])

    def transact(self, ops):
        if not ops or len(ops) > 100:
            raise ValueError('A transaction needs 1-100 operations')
        keys = [(o.item['pk'], o.item['sk']) if isinstance(o, Put) else (o.pk, o.sk) for o in ops]
        if len(keys) != len(set(keys)):
            raise ValueError('A transaction may touch each item once')
        for o in ops:
            if isinstance(o, Put):
                _validate(o.item)
            elif isinstance(o, Update):
                _validate(o.set)
        with self._lock:
            for o, key in zip(ops, keys):
                current = self._items.get(key)
                if isinstance(o, Put):
                    if o.if_absent and current is not None:
                        raise ConditionFailed(f'{key} already exists')
                elif isinstance(o, Update):
                    if current is None or current['version'] != o.version or not _matches(current, o.expect):
                        raise ConditionFailed(f'{key} changed or precondition failed')
                elif current is None or not _matches(current, o.expect):
                    raise ConditionFailed(f'{key} check failed')
            for o, key in zip(ops, keys):
                if isinstance(o, Put):
                    item = copy.deepcopy(o.item)
                    item.setdefault('version', 1)
                    self._items[key] = item
                elif isinstance(o, Update):
                    item = self._items[key]
                    for k, v in o.set.items():
                        if v is MISSING:
                            item.pop(k, None)
                        else:
                            item[k] = copy.deepcopy(v)
                    item['version'] = o.version + 1


class DynamoStore:
    """DynamoDB table with string keys pk/sk, using TransactWriteItems."""

    def __init__(self, table_name, client):
        from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
        self.table, self.client = table_name, client
        self._ser, self._de = TypeSerializer(), TypeDeserializer()

    def _to(self, item):
        return {k: self._ser.serialize(v) for k, v in item.items()}

    def _from(self, raw):
        def plain(v):
            if isinstance(v, Decimal):
                return int(v) if v == v.to_integral_value() else v
            if isinstance(v, dict):
                return {k: plain(x) for k, x in v.items()}
            if isinstance(v, (list, set)):
                return [plain(x) for x in v]
            return v
        return {k: plain(self._de.deserialize(v)) for k, v in raw.items()}

    def get(self, pk, sk):
        raw = self.client.get_item(TableName=self.table, ConsistentRead=True,
                                   Key=self._to({'pk': pk, 'sk': sk})).get('Item')
        return self._from(raw) if raw else None

    def query(self, pk, prefix=''):
        rows, start = [], None
        while True:
            kwargs = dict(TableName=self.table, ConsistentRead=True,
                          KeyConditionExpression='pk = :pk AND begins_with(sk, :p)',
                          ExpressionAttributeValues=self._to({':pk': pk, ':p': prefix}))
            if start:
                kwargs['ExclusiveStartKey'] = start
            page = self.client.query(**kwargs)
            rows += [self._from(r) for r in page['Items']]
            start = page.get('LastEvaluatedKey')
            if not start:
                return sorted(rows, key=lambda r: r['sk'])

    def _condition(self, expect, names, values, prefix):
        parts = []
        for i, (key, want) in enumerate(expect.items()):
            n, v = f'#{prefix}{i}', f':{prefix}{i}'
            names[n] = key
            if want is MISSING:
                parts.append(f'(attribute_not_exists({n}) OR {n} = :{prefix}null)')
                values[f':{prefix}null'] = {'NULL': True}
            else:
                parts.append(f'{n} = {v}')
                values[v] = self._ser.serialize(want)
        return parts

    def transact(self, ops):
        if not ops or len(ops) > 100:
            raise ValueError('A transaction needs 1-100 operations')
        items = []
        for o in ops:
            if isinstance(o, Put):
                _validate(o.item)
                item = dict(o.item)
                item.setdefault('version', 1)
                op = {'TableName': self.table, 'Item': self._to(item)}
                if o.if_absent:
                    op['ConditionExpression'] = 'attribute_not_exists(pk)'
                items.append({'Put': op})
                continue
            names, values = {}, {}
            key = self._to({'pk': o.pk, 'sk': o.sk})
            if isinstance(o, Update):
                _validate(o.set)
                names['#ver'] = 'version'
                values[':ver'] = {'N': str(o.version)}
                values[':next'] = {'N': str(o.version + 1)}
                conds = ['#ver = :ver'] + self._condition(o.expect, names, values, 'e')
                sets, removes = ['#ver = :next'], []
                for i, (k, v) in enumerate(o.set.items()):
                    names[f'#s{i}'] = k
                    if v is MISSING:
                        removes.append(f'#s{i}')
                    else:
                        values[f':s{i}'] = self._ser.serialize(v)
                        sets.append(f'#s{i} = :s{i}')
                expr = 'SET ' + ', '.join(sets) + (' REMOVE ' + ', '.join(removes) if removes else '')
                items.append({'Update': {'TableName': self.table, 'Key': key, 'UpdateExpression': expr,
                                         'ConditionExpression': ' AND '.join(conds),
                                         'ExpressionAttributeNames': names,
                                         'ExpressionAttributeValues': values}})
            else:
                conds = ['attribute_exists(pk)'] + self._condition(o.expect, names, values, 'c')
                op = {'TableName': self.table, 'Key': key, 'ConditionExpression': ' AND '.join(conds)}
                if names:
                    op['ExpressionAttributeNames'] = names
                    op['ExpressionAttributeValues'] = values
                items.append({'ConditionCheck': op})
        try:
            self.client.transact_write_items(TransactItems=items)
        except self.client.exceptions.TransactionCanceledException as error:
            raise ConditionFailed(str(error)) from error

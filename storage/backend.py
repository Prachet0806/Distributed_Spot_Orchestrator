"""Shared strict persistence boundary. Dynamo errors never select local state."""
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
import math
from threading import RLock


def encode(value):
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite number cannot be persisted")
        return Decimal(str(value))
    if isinstance(value, dict):
        return {str(k): encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    return deepcopy(value)


def decode(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [decode(v) for v in value]
    return deepcopy(value)


def conditional_failure(exc):
    return getattr(exc, "response", {}).get("Error", {}).get("Code") == "ConditionalCheckFailedException"


class Documents:
    def __init__(self, key, table_name, resource=None):
        self.key = key
        self.table = resource.Table(table_name) if resource is not None else None
        self.local = {}
        self.lock = RLock()

    def get(self, identity):
        with self.lock:
            if self.table is not None:
                item = self.table.get_item(Key={self.key: identity}, ConsistentRead=True).get("Item")
            else:
                item = self.local.get(identity)
            if item is None:
                raise KeyError(identity)
            return decode(item)

    def all(self):
        with self.lock:
            if self.table is None:
                return decode(list(self.local.values()))
            rows, kwargs = [], {"ConsistentRead": True}
            while True:
                page = self.table.scan(**kwargs)
                rows.extend(page.get("Items", []))
                cursor = page.get("LastEvaluatedKey")
                if not cursor:
                    return decode(rows)
                kwargs["ExclusiveStartKey"] = cursor

    def put(self, doc, *, create=False, expected_version=None):
        identity = doc[self.key]
        with self.lock:
            if self.table is not None:
                kwargs = {"Item": encode(doc)}
                if create:
                    kwargs.update(ConditionExpression="attribute_not_exists(#pk)",
                                  ExpressionAttributeNames={"#pk": self.key})
                elif expected_version is not None:
                    kwargs.update(ConditionExpression="#v = :v", ExpressionAttributeNames={"#v": "version"},
                                  ExpressionAttributeValues={":v": expected_version})
                self.table.put_item(**kwargs)
            else:
                cur = self.local.get(identity)
                if create and cur is not None:
                    raise KeyError(identity)
                if expected_version is not None and (cur is None or cur.get("version") != expected_version):
                    raise RuntimeError("concurrent record update")
            self.local[identity] = decode(encode(doc))
            return decode(doc)

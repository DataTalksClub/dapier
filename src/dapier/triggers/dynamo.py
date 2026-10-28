"""DynamoDB resource-API helpers shared by the trigger stores."""
from decimal import Decimal

SCAN_LIMIT = 200


def decode_numbers(value):
    """DynamoDB's resource API returns Decimals; engine params and API
    payloads must stay JSON-safe."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {key: decode_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [decode_numbers(item) for item in value]
    return value


def scan_all(table, *, consistent=False):
    """Read every scan page: a single Limit=200 scan silently dropped item
    #201 and beyond — a trigger would stop firing with no error anywhere."""
    items, start = [], None
    while True:
        kwargs = {"Limit": SCAN_LIMIT}
        if consistent:
            kwargs["ConsistentRead"] = True
        if start:
            kwargs["ExclusiveStartKey"] = start
        page = table.scan(**kwargs)
        items.extend(page.get("Items", []))
        start = page.get("LastEvaluatedKey")
        if not start:
            return items

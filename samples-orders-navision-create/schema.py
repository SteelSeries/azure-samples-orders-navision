from typing import TypedDict


class OrderEventPayload(TypedDict):
    id: int
    order_number: int

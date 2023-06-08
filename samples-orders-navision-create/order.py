from typing import TypeVar


from .schema import OrderEventPayload
from .navision import navision
from .country_vat import vat_dicts

T = TypeVar('T')


def generate_order_data(payload: OrderEventPayload) -> dict:
    """Generates order data for NAV from OrderEventPayload"""

    order = {}
    order["created_at"] = payload["created_at"]
    order["order_number_prefix"] = navision.order_number_prefix
    order["order_number"] = payload["order_number"]
    
    vat_dict = vat_dicts[payload["country_code"].upper()]
    order["navision_customer"] = vat_dict.navision_customer
    order["navision_department"] = vat_dict.navision_department
    order["navision_vat_business_group"] = 'TODO' # TODO
    order["charge_currency_id"] = 'TODO' # TODO
    order["payment_terms_code"] = 'TODO' # TODO
    order["location_code"] = 'TODO' # TODO
    order["phone_number"] = 'TODO' # TODO
    order["email"] = 'TODO' # TODO
    order["your_reference"] = 'TODO' # TODO

    # TODO: BUILD ADDRESS BILL TO
    # TODO: BUILD ADDRESS SHIP TO
    # TODO: BUILD ORDER LINES

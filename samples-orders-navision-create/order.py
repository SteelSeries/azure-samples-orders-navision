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
    order["navision_vat_business_group"] = vat_dict.navision_vat_business_group
    order["charge_currency_id"] = payload["currency".upper()]
    
    
    order["payment_terms_code"] = 'TODO' # TODO
    # See aggregates.py in dotcom to reference:  order.get_payment_method_instance().navision_code)
    
    order["location_code"] = 'TODO' # TODO
    # See aggregates.py in dotcom to reference:  order.warehouse.location_code, maybe reference payload["customer_locale"]?
    
    order["phone_number"] = payload["phone"]
    order["email"] = payload["email"]
    order["your_reference"] = payload["reference"] or payload["order_number"]

    # TODO: BUILD ADDRESS BILL TO
    # TODO: BUILD ADDRESS SHIP TO
    # TODO: BUILD ORDER LINES

    return order

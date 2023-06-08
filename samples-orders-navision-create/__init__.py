import logging

import azure.functions as func

from .country_vat import CountryVat
from .order import generate_order_data
from .schema import OrderEventPayload
from .navision import navision


def main(event: func.EventGridEvent):
    data: OrderEventPayload = event.get_json()
    logging.info('Creating order %s in navision - order=%s', data["order_number"], data["id"])

    if navision.order_exists(data["order_number"]):
        return True
    if navision.posted_shipment_exists(data["order_number"]):
        return True

    # TODO CREATE ORDER FROM DATA
    order = generate_order_data(data)

    return navision.create_order(order)
    

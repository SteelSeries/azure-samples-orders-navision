import logging

import azure.functions as func

from .format import generate_export_file_data
from .export import upload_export_file, generate_export_file_name
from .schema import OrderEventPayload


def main(event: func.EventGridEvent):
    data: OrderEventPayload = event.get_json()

    logging.info(
        'Processing order #%s (%s) to Navision',
        data["order_number"], data["id"],
    )
    
    # TODO
    

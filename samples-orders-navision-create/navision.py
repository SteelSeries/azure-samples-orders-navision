# coding: utf-8
import logging
import re
import os
import dateparser
import json
from collections import namedtuple
from datetime import date as datetime_date
from datetime import datetime
from decimal import Decimal
from typing import Union
from zoneinfo import ZoneInfo


import requests
import requests_ntlm
from lxml import etree
from lxml.builder import ElementMaker

logger = logging.getLogger(__name__)

VAT_CODES = json.load(open('vat_codes.json'))

# Types

Transaction = namedtuple('Transaction', 'document_number entry_number date sku type quantity external_document_number')
Item = namedtuple('Item', 'sku name')
Customer = namedtuple('Customer', 'no department')


# Client

class Navision(object):
    timeout = 120
    tz = ZoneInfo('Europe/Copenhagen')

    def __init__(self, url, username, password, order_number_prefix=''):
        self.base_url = url.rstrip('/')
        self.username = username
        self.password = password

        self.auth = requests_ntlm.HttpNtlmAuth(self.username, self.password)
        self.soap = ElementMaker(
            namespace="http://schemas.xmlsoap.org/soap/envelope/",
            nsmap={'SOAP-ENV': "http://schemas.xmlsoap.org/soap/envelope/"}
        )
        self.gateway = ElementMaker(
            namespace="urn:microsoft-dynamics-schemas/codeunit/Gateway",
            nsmap={'ns1': "urn:microsoft-dynamics-schemas/codeunit/Gateway"}
        )
        self.tax_area = ElementMaker(namespace="urn:microsoft-dynamics-schemas/page/taxarea")
        self.tax_area_line = ElementMaker(namespace="urn:microsoft-dynamics-schemas/page/taxarealine")
        self.tax_detail = ElementMaker(namespace="urn:microsoft-dynamics-schemas/page/taxdetail")
        self.tax_group = ElementMaker(namespace="urn:microsoft-dynamics-schemas/page/taxgroup")
        self.tax_jurisdiction = ElementMaker(namespace="urn:microsoft-dynamics-schemas/page/taxjurisdiction")

        self.order_number_prefix = order_number_prefix

    def _format_date(self, date_str: str, format_string="%m-%d-%Y"):
        dt = dateparser.parse(date_str)
        
        return dt.strftime(format_string).replace('T00:00:00', '')

    def _url(self, endpoint):
        return '{}/{}'.format(self.base_url, endpoint.lstrip('/'))

    def _request(self, method, soap, endpoint=None):
        endpoint = endpoint or 'Codeunit/Gateway'
        url = self._url(endpoint)
        headers = {
            'Content-Type': 'text/xml; charset=utf-8',
            'SOAPAction': '"urn:microsoft-dynamics-schemas/codeunit/Gateway:%s"' % method
        }
        data = etree.tostring(soap, encoding="UTF-8", xml_declaration=True)

        logger.info("Navision request - method=%s, headers=%s, data=%s", method, headers, data)
        r = requests.post(url, headers=headers, data=data, auth=self.auth, timeout=self.timeout)
        logger.info("Navision response - status_code=%s, text=%s", r.status_code, r.text)

        if r.status_code != 200:
            raise NavisionError(r.text)

        return etree.fromstring(r.text)

    def _create(self, page, object_type, endpoint, obj):
        attributes = []
        for key, value in obj:
            attributes.append(getattr(page, key)(value))

        soap = self.soap.Envelope(
            self.soap.Body(
                page.Create(
                    getattr(page, object_type)(
                        *attributes
                    )
                )
            )
        )
        doc = self._request('Create', soap, endpoint)
        result = doc.find('*//%sCreate_Result' % page._namespace)[0]
        return self._dict(result)

    def _bool(self, method, soap, endpoint=None):
        doc = self._request(method, soap, endpoint=endpoint)
        result = doc.find('*//{urn:microsoft-dynamics-schemas/codeunit/Gateway}%s_Result' % method)[0]
        return result.text == 'true'

    def _string(self, method, soap, endpoint=None):
        doc = self._request(method, soap, endpoint=endpoint)
        result = doc.find('*//{urn:microsoft-dynamics-schemas/codeunit/Gateway}%s_Result' % method)[0]
        return result.text

    def _list(self, namespace, attribute, doc):
        items = []
        uri = '*//%s%s' % (namespace, attribute)
        results = doc.find(uri)[0]
        for result in results:
            d = self._dict(result)
            items.append(d)
        return items

    def _list_of_tuples(self, params, defaults):
        obj = []
        for key, default_val in defaults.items():
            obj.append((key, params.get(key) or default_val))
        return obj

    def _dict(self, result):
        d = {}
        for child in result:
            key = child.tag.split('}')[1]
            value = child.text
            d[key] = value
        return d

    def _read_multiple(self, page, endpoint, filters):
        filter_soap = []
        for field, criteria in filters.items():
            filter_soap.append(
                page.filter(
                    page.Field(field),
                    page.Criteria(criteria),
                )
            )
        soap = self.soap.Envelope(
            self.soap.Body(
                page.ReadMultiple(*filter_soap),
            )
        )
        doc = self._request('ReadMultiple', soap, endpoint)
        return self._list(page._namespace, 'ReadMultiple_Result', doc)

    def _read_single(self, page, endpoint, filters):
        groups = self._read_multiple(page, endpoint, filters)
        if len(groups) != 1:
            raise NavisionError("There are more than one (%s) results for %s with filters %s" %
                                (len(groups), endpoint, filters))
        return groups[0]

    # Customers

    def get_customers(self):
        soap = self.soap.Envelope(
            self.soap.Body(
                self.gateway.GetCustomers(
                    self.gateway.customers()
                )
            )
        )
        doc = self._request('GetCustomers', soap)

        customers = []
        entries = doc.find('*//{urn:microsoft-dynamics-schemas/codeunit/Gateway}customers')
        for entry in entries:
            values = dict([(re.sub(r'{.+}', '', i.tag), i.text) for i in entry])
            customers.append(Customer(no=values.get('No'), department=values.get('Department')))

        return customers

    # Items

    def get_items(self):
        soap = self.soap.Envelope(
            self.soap.Body(
                self.gateway.GetItems(
                    self.gateway.items()
                )
            )
        )
        doc = self._request('GetItems', soap)

        items = []
        entries = doc.find('*//{urn:microsoft-dynamics-schemas/codeunit/Gateway}items')
        for entry in entries:
            values = dict([(re.sub(r'{.+}', '', i.tag), i.text) for i in entry])
            items.append(Item(sku=values.get('No'), name=values.get('Description')))

        return items

    # Inventory

    def get_inventory(self, location, sku):
        soap = self.soap.Envelope(
            self.soap.Body(
                self.gateway.GetInventory(
                    self.gateway.itemNo(sku),
                    self.gateway.locationCode(location)
                )
            )
        )
        doc = self._request('GetInventory', soap)
        result = doc.find('*//{urn:microsoft-dynamics-schemas/codeunit/Gateway}GetInventory_Result')[0]
        return int(result.text)

    def get_transactions(self, location, after_entry, num_entries=50):
        soap = self.soap.Envelope(
            self.soap.Body(
                self.gateway.GetTransactions(
                    self.gateway.transactions(),
                    self.gateway.forLocation(location),
                    self.gateway.afterEntryNo(str(after_entry)),
                    self.gateway.noOfEntries(str(num_entries))
                )
            )
        )
        doc = self._request('GetTransactions', soap)

        # parse it
        transactions = []
        entries = doc.find('*//{urn:microsoft-dynamics-schemas/codeunit/Gateway}transactions')
        for entry in entries:
            values = dict([(re.sub(r'{.+}', '', i.tag), i.text) for i in entry])
            if values.get('entryNo') == '0':
                continue

            try:
                trans = Transaction(
                    document_number=values.get('DocumentNo'),
                    entry_number=int(values.get('entryNo')),
                    date=datetime.strptime(values.get('PostingDate'), '%m/%d/%y').date(),
                    sku=values.get('ItemNo'),
                    type=values.get('EntryType').lower(),
                    quantity=int(values.get('Quantity', '0').replace(',', '')),
                    external_document_number=values.get('ExternalDocumentNo')
                )
                transactions.append(trans)
            except ValueError:
                logger.error(
                    "Navision transfer has invalid quantity: entryNo[%s] quantity[%s]" % (
                        values.get('entryNo'),
                        values.get('Quantity')
                    )
                )

        return transactions

    # Orders

    def order_exists(self, order_number):
        soap = self.soap.Envelope(
            self.soap.Body(
                self.gateway.OrderExists(
                    self.gateway.orderNo("%s%s" % (self.order_number_prefix, order_number))
                )
            )
        )
        return self._bool('OrderExists', soap)

    def posted_shipment_exists(self, order_number):
        soap = self.soap.Envelope(
            self.soap.Body(
                self.gateway.PostedShipmentExists(
                    self.gateway.orderNo("%s%s" % (self.order_number_prefix, order_number))
                )
            )
        )
        return self._bool('PostedShipmentExists', soap)

    def create_order(self, order_data):
        e = ElementMaker()

        order_date = self._format_date(order_data["created_at"])

        soap = self.soap.Envelope(
            self.soap.Body(
                self.gateway.CreateOrder(
                    self.gateway.order(
                        e.header(
                            e.orderNo("%s%s" % (self.order_number_prefix, order_data["order_number"])),
                            e.externalDocNo(order_data["order_number"]),
                            e.sellToCustomerNo(order_data["customer"]["id"]),
                            e.department(order_data["navision_department"]),
                            e.genBusPostingGroup(""),
                            e.vatBusPostingGroup(VAT_CODES[order_data["shipping_address"]["country_code"]] or ""),
                            e.internalComment(""),
                            e.orderDate(order_date),
                            e.currency(order_data["charge_currency_id"]),
                            e.paymentTermsCode(order.get_payment_method_instance().navision_code),
                            e.locationCode(order_data["location_code"]),
                            e.phoneNo(order_data[""]),
                            e.email(order_data[""]),
                            e.yourReference(order.payment.reference or order.order_number)
                        ),
                        self._build_address('billToAddress', order.billing_address),
                        self._build_address('shipToAddress', order.shipping_address),
                        self._build_order_lines(order),
                    )
                )
            )
        )
        return self._request('CreateOrder', soap)

    def _build_address(self, elem, address):
        e = ElementMaker()
        if address.subdivision_id and address.city:
            subshort = address.subdivision_short_id
            city = "%s, %s" % (address.city[:28 - len(subshort)], subshort)
        elif address.city:
            city = address.city[:30]
        else:
            city = ''

        parts = [
            e.name(address.company[:30] if address.company else address.name[:30]),
            e.address1(address.line1[:30]),
            e.address2(address.line2[:30] if address.line2 else ''),
            e.postalNo(address.postcode[:25] if address.postcode else ''),
            e.city(city),
            e.county(address.subdivision_short_id if address.subdivision_id else ''),
            e.country(address.country_id),
            e.contactName(address.name[:30])
        ]
        return getattr(e, elem)(*parts)

    def _build_order_lines(self, order):
        shipping = order.shipping_method

        e = ElementMaker()
        lines = []

        # Alacart and Customizer children get posted like normal, Customizer parents do not get posted.
        items_to_handle = \
            order.customizer_children + order.alacart_items + order.bundle_children

        # Handle items
        for item in items_to_handle:
            lines.append(e.orderLine(
                e.lineType('Item'),
                e.itemNo(item.sku),
                e.itemName(item.name[:30]),
                e.quantity(str(item.quantity)),
                e.price(str(item.msrp_charge)),
                e.total(str(item.total_with_discount_charge)),
                e.salesTaxCode(''),
            ))

        # Handle shipping
        lines.append(e.orderLine(
            e.lineType('G/L'),
            e.itemNo(os.environ["NAVISION_SHIPPING_ACCOUNT"]),
            e.itemName(shipping.name),
            e.quantity('1'),
            e.price(str(shipping.msrp_charge)),
            e.total(str(shipping.price_charge)),
            e.salesTaxCode(''),
        ))

        # Handle taxes (optional)
        if order.report_sales_tax:
            for region, tax in order.sales_tax_by_region().items():
                # Only add lines with a tax that isn't 0
                if tax != 0:
                    lines.append(e.orderLine(
                        e.lineType('G/L'),
                        e.itemNo(order.country.navision_vat_account_number),
                        e.itemName('Sales Tax'),
                        e.quantity('1'),
                        e.price(str(tax)),
                        e.total(str(tax)),
                        e.salesTaxCode(region),
                    ))

        return e.orderLineList(*lines)

    def cancel_order(self, order_number):
        soap = self.soap.Envelope(
            self.soap.Body(
                self.gateway.CancelOrder(
                    self.gateway.orderNo("%s%s" % (self.order_number_prefix, order_number))
                )
            )
        )
        return self._bool('CancelOrder', soap)

    def post_order(self, order_number, date):
        formatted_date = self._formatted_date(date, format_string='%Y-%m-%d')
        soap = self.soap.Envelope(
            self.soap.Body(
                self.gateway.PostOrder(
                    self.gateway.orderNo("%s%s" % (self.order_number_prefix, order_number)),
                    self.gateway.postingDate(formatted_date),
                    self.gateway.documentDate(formatted_date),
                    self.gateway.shipmentDate(formatted_date)
                )
            )
        )
        return self._bool('PostOrder', soap)

    # Credit Memos

    def credit_memo_exists(self, credit_memo_number):
        soap = self.soap.Envelope(
            self.soap.Body(
                self.gateway.CreditMemoExists(
                    self.gateway.cmNo(credit_memo_number)
                )
            )
        )
        return self._bool('CreditMemoExists', soap)

    def posted_credit_memo_exists(self, credit_memo_number):
        soap = self.soap.Envelope(
            self.soap.Body(
                self.gateway.PostedCreditMemoExists(
                    self.gateway.cmNo(credit_memo_number)
                )
            )
        )
        return self._bool('PostedCreditMemoExists', soap)

    def find_credit_memo(self, your_reference):
        soap = self.soap.Envelope(
            self.soap.Body(
                self.gateway.FindCreditMemo(
                    self.gateway.yourReference(your_reference)
                )
            )
        )
        result = self._string('FindCreditMemo', soap)
        if result:
            return result
        return None

    def find_posted_credit_memo(self, your_reference):
        soap = self.soap.Envelope(
            self.soap.Body(
                self.gateway.FindPostedCreditMemo(
                    self.gateway.yourReference(your_reference)
                )
            )
        )
        result = self._string('FindPostedCreditMemo', soap)
        if result:
            return result
        return None

    def create_credit_memo(self, order, refund):
        order_date = self._formatted_date(refund.refunded_at)

        e = ElementMaker()
        soap = self.soap.Envelope(
            self.soap.Body(
                self.gateway.CreateCreditMemo(
                    self.gateway.creditMemo(
                        e.cmHeader(
                            e.cmNo(''),
                            e.externalDocNo(order.order_number),
                            e.yourReference(refund.reference[:30]),
                            e.sellToCustomerNo(order.navision_customer),
                            e.orderDate(order_date),
                            e.currency(order.charge_currency_id),
                            e.department(order.navision_department)
                        ),
                        self._build_address('billToAddress', order.billing_address),
                        self._build_address('shipToAddress', order.shipping_address),
                        self._build_credit_memo_lines(order, refund)
                    )
                )
            )
        )
        doc = self._request('CreateCreditMemo', soap)

        # find credit memo number
        return doc.find('*//{urn:microsoft-dynamics-nav/xmlports/x50012}cmNo').text

    def _build_credit_memo_lines(self, order, refund):
        e = ElementMaker()
        lines = []
        # Handle items

        items_to_handle = \
            order.customizer_children + order.alacart_items + order.bundle_children

        for item in refund.items:
            cart_group_relation = None
            if hasattr(item, 'cart_group_relation'):
                cart_group_relation = item.cart_group_relation

                if cart_group_relation in ['parent', 'child']:
                    continue
            else:
                if item.sku not in [i.sku for i in items_to_handle]:
                    continue

            # We are going to omit actually posting the $0.00 custom skus from posting to Navision
            if cart_group_relation != 'parent':
                lines.append(e.cmLine(
                    e.lineType('Item'),
                    e.itemNo(item.sku),
                    e.itemName(item.name[:30]),
                    e.quantity(str(item.quantity)),
                    e.price(str(item.price)),
                    e.total(str(item.refund)),
                    e.locationCode(refund.location),
                    e.returnReasonCode(refund.reason),
                    e.salesTaxCode(''),
                ))

        # Handle shipping
        if refund.shipping > 0:
            lines.append(e.cmLine(
                e.lineType('G/L'),
                e.itemNo(os.environ["NAVISION_SHIPPING_ACCOUNT"]),
                e.itemName('Shipping'),
                e.quantity('1'),
                e.price(str(refund.shipping_excluding_sales_tax)),
                e.total(str(refund.shipping_excluding_sales_tax)),
                e.locationCode(refund.location),
                e.returnReasonCode(refund.reason),
                e.salesTaxCode(''),
            ))

        # Handle taxes
        if order.report_sales_tax and refund.sales_tax > 0:
            for region, tax in refund.sales_tax_by_region().items():
                # Only add tax lines that aren't 0
                if tax != 0:
                    lines.append(e.cmLine(
                        e.lineType('G/L'),
                        e.itemNo(order.country.navision_vat_account_number),
                        e.itemName('Sales Tax'),
                        e.quantity('1'),
                        e.price(str(tax)),
                        e.total(str(tax)),
                        e.locationCode(refund.location),
                        e.returnReasonCode(refund.reason),
                        e.salesTaxCode(region),
                    ))

        return e.cmLineList(*lines)

    def cancel_credit_memo(self, credit_memo_number):
        soap = self.soap.Envelope(
            self.soap.Body(
                self.gateway.CancelCreditMemo(
                    self.gateway.cmNo(credit_memo_number)
                )
            )
        )
        return self._bool('CancelCreditMemo', soap)

    def post_credit_memo(self, refund, posting_date):
        formatted_date = self._formatted_date(posting_date, format_string='%Y-%m-%d')

        soap = self.soap.Envelope(
            self.soap.Body(
                self.gateway.PostCreditMemo(
                    self.gateway.cmNo(refund.credit_memo_number),
                    self.gateway.postingDate(formatted_date),
                    self.gateway.documentDate(formatted_date)
                )
            )
        )
        return self._bool('PostCreditMemo', soap)

    def upload_order_settlement_batch(self, order, balance_transaction, posting_date=None):
        e = ElementMaker()

        posting_date = self._formatted_date(posting_date or balance_transaction.timestamp)

        settlements = []

        # Add debit on customer account
        settlements.append(
            e.Settlement(
                e.PostingDate(posting_date),
                e.Currency(balance_transaction.payment_currency.id),
                e.AccountType('CUSTOMER'),
                e.AccountNo(order.navision_customer),
                e.Amount(str(-1 * balance_transaction.amount_payment)),
                e.Description(balance_transaction.description),
                e.ExternalDocNo(order.order_number),
                e.Department(order.navision_department),
                e.PaymentReference(balance_transaction.reference),
            )
        )

        # Add credit on payment gateway
        if balance_transaction.amount_net:
            settlements.append(
                e.Settlement(
                    e.PostingDate(posting_date),
                    e.Currency(balance_transaction.balance.balance_currency.id),
                    e.AccountType('BANK'),
                    e.AccountNo(balance_transaction.balance.account_method),
                    e.Amount(str(balance_transaction.amount_net)),
                    e.Description(balance_transaction.description),
                    e.ExternalDocNo(order.order_number),
                    e.Department(order.navision_department),
                    e.PaymentReference(balance_transaction.reference),
                )
            )

        # Add any fees
        # TODO: currency fees?
        if balance_transaction.amount_provider_fee:
            # TODO: remove this
            department = order.navision_department
            # Special fee posting until end of 2017
            localized_date = self._localize_date(balance_transaction.timestamp)
            if localized_date < datetime(2018, 1, 1, tzinfo=self.tz):
                if order.type == 'default':
                    department = 'HQ-WEB.IT'

            settlements.append(
                e.Settlement(
                    e.PostingDate(posting_date),
                    e.Currency(balance_transaction.balance.balance_currency.id),
                    e.AccountType('GL'),
                    e.AccountNo(balance_transaction.balance.account_provider_fee),
                    e.Amount(str(balance_transaction.amount_provider_fee)),
                    e.Description(balance_transaction.description),
                    e.ExternalDocNo(order.order_number),
                    e.Department(department),
                    e.PaymentReference(balance_transaction.reference),
                )
            )

        soap = self.soap.Envelope(
            self.soap.Body(
                self.gateway.UploadSettlement(
                    self.gateway.settlement(
                        *settlements
                    )
                )
            )
        )

        return self._request('UploadSettlement', soap)

    def upload_fee_settlement_batch(self, balance_transaction, posting_date=None):
        e = ElementMaker()

        posting_date = self._formatted_date(posting_date or balance_transaction.timestamp)

        settlements = []
        external_doc_no = '%s: %s' % (balance_transaction.balance.payment_method, balance_transaction.type)

        # Add credit on payment gateway
        settlements.append(
            e.Settlement(
                e.PostingDate(posting_date),
                e.Currency(balance_transaction.balance.balance_currency.id),
                e.AccountType('BANK'),
                e.AccountNo(balance_transaction.balance.account_method),
                e.Amount(str(balance_transaction.amount_net)),
                e.Description(balance_transaction.description[:50]),
                e.ExternalDocNo(external_doc_no),
                e.Department(balance_transaction.balance.department_fee),
                e.PaymentReference(balance_transaction.reference),
            )
        )

        # Add fee on fee account
        settlements.append(
            e.Settlement(
                e.PostingDate(posting_date),
                e.Currency(balance_transaction.balance.balance_currency.id),
                e.AccountType('GL'),
                e.AccountNo(balance_transaction.balance.account_provider_fee),
                e.Amount(str(-1 * balance_transaction.amount_net)),
                e.Description(balance_transaction.description[:50]),
                e.ExternalDocNo(external_doc_no),
                e.Department(balance_transaction.balance.department_fee),
                e.PaymentReference(balance_transaction.reference),
            )
        )

        soap = self.soap.Envelope(
            self.soap.Body(
                self.gateway.UploadSettlement(
                    self.gateway.settlement(
                        *settlements
                    )
                )
            )
        )

        return self._request('UploadSettlement', soap)

    def clear_settlements(self):
        soap = self.soap.Envelope(
            self.soap.Body(
                self.gateway.ClearSettlements()
            )
        )
        return self._request('ClearSettlements', soap)

    def post_settlement(self):
        soap = self.soap.Envelope(
            self.soap.Body(
                self.gateway.PostSettlement()
            )
        )
        return self._bool('PostSettlement', soap)

    def get_unapplied_amount(self, order):
        soap = self.soap.Envelope(
            self.soap.Body(
                self.gateway.GetUnappliedAmount(
                    self.gateway.custNo(order.navision_customer),
                    self.gateway.externalDocumentNo(order.order_number)
                )
            )
        )
        doc = self._request('GetUnappliedAmount', soap)

        return_value = doc.find('*//{urn:microsoft-dynamics-schemas/codeunit/Gateway}GetUnappliedAmount_Result')[0]
        return Decimal(return_value.text)

    def get_applied_amount(self, order, balance_transaction):
        soap = self.soap.Envelope(
            self.soap.Body(
                self.gateway.GetAppliedAmount(
                    self.gateway.custNo(order.navision_customer),
                    self.gateway.externalDocumentNo(order.order_number),
                    self.gateway.paymentReference(balance_transaction.reference),
                )
            )
        )
        doc = self._request('GetAppliedAmount', soap)

        return_value = doc.find('*//{urn:microsoft-dynamics-schemas/codeunit/Gateway}GetAppliedAmount_Result')[0]
        return Decimal(return_value.text)

    # Tax Groups

    def create_tax_group(self, code, description):
        obj = (
            ('Code', code),
            ('Description', description),
        )
        return self._create(self.tax_group, 'TaxGroup', '/Page/TaxGroup', obj)

    def get_tax_group(self, code):
        filters = {'Code': code}
        return self._read_single(self.tax_group, '/Page/TaxGroup', filters)

    def list_tax_groups(self, **filters):
        return self._read_multiple(self.tax_group, '/Page/TaxGroup', filters)

    # Tax Areas

    def create_tax_area(self, code, description):
        obj = (
            ('Code', code),
            ('Description', description),
        )
        return self._create(self.tax_area, 'TaxArea', '/Page/TaxArea', obj)

    def get_tax_area(self, code):
        filters = {'Code': code}
        return self._read_single(self.tax_area, '/Page/TaxArea', filters)

    def list_tax_areas(self, **filters):
        return self._read_multiple(self.tax_area, '/Page/TaxArea', filters)

    # Tax Area Lines

    def create_tax_area_line(self, tax_area, tax_jurisdiction_code, calculation_order):
        obj = (
            ('Tax_Area', tax_area),
            ('Tax_Jurisdiction_Code', tax_jurisdiction_code),
            ('Calculation_Order', calculation_order),
        )
        return self._create(self.tax_area_line, 'TaxAreaLine', '/Page/TaxAreaLine', obj)

    def get_tax_area_line(self, tax_area='', tax_jurisdiction_code='', calculation_order=''):
        filters = {'Tax_Area': tax_area,
                   'Tax_Jurisdiction_Code': tax_jurisdiction_code,
                   'Calculation_Order': calculation_order}
        return self._read_single(self.tax_area_line, '/Page/TaxAreaLine', filters)

    def list_tax_area_lines(self, **filters):
        return self._read_multiple(self.tax_area_line, '/Page/TaxAreaLine', filters)

    # Tax Details

    def create_tax_detail(self, **kwargs):
        default_params = {
            'Tax_Jurisdiction_Code': 'Code',
            'Tax_Group_Code': 'Code',
            'Tax_Type': 'Sales_Tax',
            'Maximum_Amount_Qty': '0.0',
            'Tax_Below_Maximum': '0.0',
            'Tax_Above_Maximum': '0.0',
            'Effective_Date': str(datetime_date.today()),
            'Calculate_Tax_on_Tax': 'false',
        }
        obj = self._list_of_tuples(kwargs, default_params)
        return self._create(self.tax_detail, 'TaxDetail', '/Page/TaxDetail', obj)

    def get_tax_detail(self, group_code='', jurisdiction_code=''):
        filters = {'Tax_Group_Code': group_code,
                   'Tax_Jurisdiction_Code': jurisdiction_code,
                   }
        return self._read_single(self.tax_detail, '/Page/TaxDetail', filters)

    def list_tax_details(self, **filters):
        return self._read_multiple(self.tax_detail, '/Page/TaxDetail', filters)

    # Tax Jurisdictions

    def create_tax_jurisdiction(self, **kwargs):
        default_params = {
            'Code': 'Code',
            'Description': 'Description',
            'Tax_Account_Sales': '',
            'Tax_Account_Purchases': '',
            'Report_to_Jurisdiction': '',
        }
        obj = self._list_of_tuples(kwargs, default_params)
        return self._create(self.tax_jurisdiction, 'TaxJurisdiction', '/Page/TaxJurisdiction', obj)

    def get_tax_jurisdiction(self, code):
        filters = {'Code': code}
        return self._read_single(self.tax_jurisdiction, '/Page/TaxJurisdiction', filters)

    def list_tax_jurisdictions(self, **filters):
        return self._read_multiple(self.tax_jurisdiction, '/Page/TaxJurisdiction', filters)
    

class NavisionError(Exception):
    pass


navision = Navision(
    url=os.environ["NAVISION_URL"],
    username=os.environ["NAVISION_USERNAME"],
    password=os.environ["NAVISION_PASSWORD"],
    order_number_prefix=os.environ["NAVISION_ORDER_NUMBER_PREFIX"],
)

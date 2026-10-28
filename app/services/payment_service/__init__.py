from .core import PaymentServiceCore
from .webhook import process_webhook

class PaymentService(PaymentServiceCore):
    """
    Facade combining core functionalities and the webhook processing logic.
    """
    process_webhook = staticmethod(process_webhook)

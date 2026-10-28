import stripe
import json
from datetime import datetime, timezone
from app.config import settings
from app.core.database import get_database
from app.services.payment_service.handlers import (
    handle_checkout_completed,
    handle_invoice_paid,
    handle_subscription_deleted,
    handle_checkout_failed,
    handle_invoice_failed,
    handle_subscription_updated,
    handle_invoice_upcoming
)

async def process_webhook(payload: bytes, sig_header: str) -> None:
    """
    Verify Stripe webhook signature and process event using match-case.
    """
    if not settings.STRIPE_WEBHOOK_SECRET:
        event = json.loads(payload)
    else:
        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
            )
        except ValueError:
            raise Exception("Invalid webhook payload")
        except stripe.error.SignatureVerificationError:
            raise Exception("Invalid webhook signature")

    event_type = event['type']
    print(event_type)
    
    db = await get_database()
    now = datetime.now(timezone.utc)

    match event_type:
        case 'checkout.session.completed':
            await handle_checkout_completed(event, db, now)
        case 'invoice.paid':
            await handle_invoice_paid(event, db, now)
        case 'invoice.upcoming':
            await handle_invoice_upcoming(event, db, now)
        case 'invoice.payment_failed':
            await handle_invoice_failed(event, db, now)
        case 'customer.subscription.updated':
            await handle_subscription_updated(event, db, now)
        case 'customer.subscription.deleted':
            await handle_subscription_deleted(event, db, now)
        case 'checkout.session.expired' | 'checkout.session.async_payment_failed':
            await handle_checkout_failed(event, db, now)
        case _:
            pass # Unhandled event type, do nothing


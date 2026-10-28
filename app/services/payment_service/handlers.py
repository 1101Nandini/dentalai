import stripe
from datetime import datetime, timezone
from app.config import settings
from app.services.subscription_service import SubscriptionService
from app.services.payment_service.core import PaymentServiceCore
from app.background.report_processor import log_audit_event

stripe.api_key = settings.STRIPE_SECRET_KEY

async def handle_checkout_completed(event: dict, db, now) -> None:
    session = event['data']['object']
    session_id = session.get('id')
    metadata = session.get('metadata', {})
    
    user_id = session.get('client_reference_id') or metadata.get('user_id')
    plan = metadata.get('plan', 'pro')
    stripe_subscription_id = session.get('subscription')
    stripe_customer_id = session.get('customer')

    await db["transaction_logs"].update_one(
        {"session_id": session_id},
        {"$set": {
            "status": "completed", 
            "updated_at": now,
            "metadata": metadata,
            "type": "checkout.session.completed"
        }}
    )

    if user_id and stripe_subscription_id:
        await SubscriptionService.link_stripe_subscription(
            user_id=user_id, 
            stripe_subscription_id=stripe_subscription_id, 
            stripe_customer_id=stripe_customer_id, 
            plan=plan
        )

        # If the user redeemed a promo code, immediately end any trial in Stripe
        # so the 1-month coupon applies immediately rather than waiting 14 days.
        try:
            stripe_sub_obj = stripe.Subscription.retrieve(stripe_subscription_id)
            if stripe_sub_obj.discount and stripe_sub_obj.status == "trialing":
                stripe.Subscription.modify(stripe_subscription_id, trial_end="now")
        except Exception as e:
            print(f"Could not end trial for promo subscription {stripe_subscription_id}: {e}")
    
    # Mark user and org as having had their trial period so Month 2 has no delayed trial
    if user_id:
        from bson import ObjectId
        await db["users"].update_one(
            {"_id": ObjectId(user_id)},
            {"$set": {"has_had_trial": True, "updated_at": now}}
        )
        org_id = metadata.get("organization_id")
        if org_id:
            await db["organizations"].update_one(
                {"_id": ObjectId(org_id)},
                {"$set": {"has_had_trial": True, "updated_at": now}}
            )
    
    try:
        await log_audit_event(
            actor_id=user_id or "system",
            action="SUBSCRIPTION_CHECKOUT_COMPLETED",
            resource="subscriptions",
            resource_id=session_id or "",
            metadata={
                "plan": plan,
                "stripe_subscription_id": stripe_subscription_id,
                "stripe_customer_id": stripe_customer_id,
            },
        )
    except Exception:
        pass
        
    PaymentServiceCore._trigger_event(session_id)


async def handle_invoice_paid(event: dict, db, now) -> None:
    try:
        invoice = event['data']['object']
        
        stripe_subscription_id = invoice.get('subscription')
        user_id = None
        plan = None
        metadata = {}
        
        # Try to extract details from the nested 'parent.subscription_details' object (newer Stripe API versions)
        if 'parent' in invoice and invoice['parent'] and invoice['parent'].get('type') == 'subscription_details':
            sub_details = invoice['parent'].get('subscription_details', {})
            stripe_subscription_id = stripe_subscription_id or sub_details.get('subscription')
            
            metadata = sub_details.get('metadata', {})
            user_id = metadata.get('user_id')
            plan = metadata.get('plan')
            
        line_end_date = None
        # Also check line item metadata just in case, and parse period end_date for exact 30/31 days sync
        if invoice.get('lines', {}).get('data'):
            for line in invoice['lines']['data']:
                if not user_id and line.get('metadata') and line['metadata'].get('user_id'):
                    user_id = line['metadata']['user_id']
                    plan = line['metadata'].get('plan', 'pro')
                
                period = line.get('period', {})
                if period.get('end'):
                    line_end_date = datetime.fromtimestamp(period['end'], timezone.utc)
                    
                if user_id and line_end_date:
                    break

        if not stripe_subscription_id:
            print("No stripe_subscription_id found on invoice anywhere! Skipping.")
            return

        # Idempotency lock to prevent double-granting from Stripe retries/race conditions
        existing_log = await db["transaction_logs"].find_one({"session_id": invoice.get('id'), "type": "invoice.paid"})
        if existing_log:
            print(f"Invoice {invoice.get('id')} already processed. Skipping to avoid race condition.")
            return

        # If we still don't have user_id and plan, try DB then Stripe API
        if not user_id or not plan:
            stripe_sub = await db["stripe_subscriptions"].find_one({"stripe_subscription_id": stripe_subscription_id})
            if stripe_sub:
                user_id = stripe_sub.get("user_id")
                plan = stripe_sub.get("plan")
            else:
                try:
                    stripe_sub_obj = stripe.Subscription.retrieve(stripe_subscription_id)
                    user_id = stripe_sub_obj.metadata.get('user_id')
                    plan = stripe_sub_obj.metadata.get('plan', 'pro')
                except Exception as e:
                    print("Stripe retrieving error:", e)
                    raise e
        # Safely resolve promo_code if available
        promo_code = metadata.get('promo_code')
        if not promo_code and stripe_subscription_id:
            checkout_log = await db["transaction_logs"].find_one({
                "$or": [
                    {"metadata.stripe_subscription_id": stripe_subscription_id},
                    {"session_id": stripe_subscription_id}
                ]
            })
            if checkout_log and checkout_log.get("metadata", {}).get("promo_code"):
                promo_code = checkout_log["metadata"]["promo_code"]

        if user_id and plan:
            await db["transaction_logs"].insert_one({
                "session_id": invoice.get('id'), # use invoice ID for context
                "user_id": user_id,
                "plan": plan,
                "status": "completed",
                "amount": invoice.get('amount_paid'),
                "currency": invoice.get('currency'),
                "created_at": now,
                "updated_at": now,
                "type": "invoice.paid"
            })
            
            # Grant access matching Stripe's exact billing period cycle
            await SubscriptionService.create_subscription(
                user_id=user_id,
                plan=plan,
                end_date=line_end_date,
                transaction_id=invoice.get('id'),
                stripe_subscription_id=stripe_subscription_id,
                promo_code=promo_code
            )
            
            try:
                await log_audit_event(
                    actor_id=user_id,
                    action="SUBSCRIPTION_PAYMENT_SUCCESS",
                    resource="subscriptions",
                    resource_id=invoice.get('id', ''),
                    metadata={
                        "plan": plan,
                        "amount": invoice.get('amount_paid'),
                        "currency": invoice.get('currency'),
                        "stripe_subscription_id": stripe_subscription_id,
                    },
                )
            except Exception:
                pass
    except Exception as e:
        import traceback
        print(f"Error inside handle_invoice_paid: {e}")
        traceback.print_exc()
        raise e


async def handle_invoice_failed(event: dict, db, now) -> None:
    invoice = event['data']['object']
    stripe_subscription_id = invoice.get('subscription')
    
    # Try to extract details from the nested 'parent.subscription_details' object (newer Stripe API versions)
    if not stripe_subscription_id and 'parent' in invoice and invoice['parent'] and invoice['parent'].get('type') == 'subscription_details':
        sub_details = invoice['parent'].get('subscription_details', {})
        stripe_subscription_id = sub_details.get('subscription')

    if not stripe_subscription_id:
        print("No stripe_subscription_id found on failed invoice anywhere! Skipping.")
        return

    # Update stripe_subscriptions to past_due
    await SubscriptionService.update_stripe_subscription_status(stripe_subscription_id, "past_due")
    
    # Intentionally freeze PRO access
    await SubscriptionService.suspend_active_subscriptions(stripe_subscription_id)

    # Cancel the unpaid subscription in Stripe so retries/dunning don't harass cardless promo users
    try:
        stripe.Subscription.cancel(stripe_subscription_id)
    except Exception as e:
        print(f"Could not cancel failed stripe subscription {stripe_subscription_id}: {e}")
    
    # Log the failure
    await db["transaction_logs"].insert_one({
        "session_id": invoice.get('id'),
        "user_id": None, # Unassociated user explicitly ok, subscription service uses sub ID
        "status": "failed",
        "amount": invoice.get('amount_due'),
        "currency": invoice.get('currency'),
        "created_at": now,
        "updated_at": now,
        "type": "invoice.payment_failed"
    })
    
    try:
        await log_audit_event(
            actor_id="system",
            action="SUBSCRIPTION_PAYMENT_FAILED",
            resource="subscriptions",
            resource_id=invoice.get('id', ''),
            metadata={
                "stripe_subscription_id": stripe_subscription_id,
                "amount_due": invoice.get('amount_due'),
                "currency": invoice.get('currency'),
            },
        )
    except Exception:
        pass

async def handle_invoice_upcoming(event: dict, db, now) -> None:
    """
    Handle Stripe invoice.upcoming event (sent 3-7 days before next invoice is billed).
    Checks if user has a card on file; if not, sends reminder email to add payment method.
    """
    invoice = event['data']['object']
    stripe_subscription_id = invoice.get('subscription')
    stripe_customer_id = invoice.get('customer')

    if not stripe_subscription_id or not stripe_customer_id:
        return

    try:
        customer = stripe.Customer.retrieve(stripe_customer_id)
        default_pm = getattr(customer.invoice_settings, 'default_payment_method', None) if hasattr(customer, 'invoice_settings') else None
        
        has_card = bool(default_pm)
        if not has_card:
            pms = stripe.PaymentMethod.list(customer=stripe_customer_id, type="card", limit=1)
            has_card = len(pms.data) > 0

        # If user has no card on file, remind them 3 days before expiry
        if not has_card:
            stripe_sub = await db["stripe_subscriptions"].find_one({"stripe_subscription_id": stripe_subscription_id})
            user_id = stripe_sub.get("user_id") if stripe_sub else None
            
            if user_id:
                from bson import ObjectId
                from app.background.email_tasks import send_subscription_expiring_email
                from app.config import settings
                
                user = await db["users"].find_one({"_id": ObjectId(user_id)})
                if user and user.get("email"):
                    sub = await db["subscriptions"].find_one({
                        "stripe_subscription_id": stripe_subscription_id,
                        "is_active": True
                    })
                    if sub and not sub.get("expiring_email_sent"):
                        period_end = invoice.get("period_end")
                        days_left = 3
                        if period_end:
                            end_dt = datetime.fromtimestamp(period_end, timezone.utc)
                            delta = end_dt - now
                            days_left = max(1, delta.days)

                        action_url = f"{settings.FRONTEND_URL}/plan"
                        try:
                            portal = stripe.billing_portal.Session.create(
                                customer=stripe_customer_id,
                                return_url=f"{settings.FRONTEND_URL}/plan"
                            )
                            action_url = portal.url
                        except Exception:
                            pass

                        user_name = user.get("full_name") or user.get("name", "Doctor")
                        await send_subscription_expiring_email(
                            to_email=user["email"],
                            user_name=user_name,
                            days_left=days_left,
                            action_url=action_url
                        )
                        await db["subscriptions"].update_one(
                            {"_id": sub["_id"]},
                            {"$set": {"expiring_email_sent": True, "updated_at": now}}
                        )
    except Exception as e:
        print(f"Error handling invoice.upcoming webhook: {e}")

    
async def handle_subscription_updated(event: dict, db, now) -> None:
    subscription = event['data']['object']
    stripe_subscription_id = subscription.get('id')
    status = subscription.get('status')
    plan = subscription.get('metadata', {}).get('plan')
    
    if stripe_subscription_id and status:
        # Determine the correct internal status
        is_canceled = (status == "canceled")
        is_pending_cancel = subscription.get('cancel_at_period_end') is True
        
        local_status = status
        if is_pending_cancel:
            local_status = "pending_cancellation"

        update_doc = {
            "status": local_status,
            "updated_at": now
        }
        if plan:
            update_doc["plan"] = plan
            
        await db["stripe_subscriptions"].update_one(
            {"stripe_subscription_id": stripe_subscription_id},
            {"$set": update_doc}
        )

        # Log cancellation if status moved to canceled or scheduled to cancel
        if is_canceled or is_pending_cancel:
            user_id = subscription.get('metadata', {}).get('user_id')
            if not user_id:
                stripe_sub = await db["stripe_subscriptions"].find_one({"stripe_subscription_id": stripe_subscription_id})
                user_id = stripe_sub.get("user_id") if stripe_sub else None

            # Check if we already logged this cancellation to avoid duplicates
            log_type = "customer.subscription.updated"
            log_status = "canceled" if is_canceled else "pending_cancellation"
            
            existing_log = await db["transaction_logs"].find_one({
                "session_id": stripe_subscription_id,
                "type": log_type,
                "status": log_status
            })
            
            if not existing_log:
                await db["transaction_logs"].insert_one({
                    "session_id": stripe_subscription_id,
                    "user_id": user_id,
                    "status": log_status,
                    "created_at": now,
                    "updated_at": now,
                    "type": log_type,
                    "metadata": subscription.get('metadata')
                })

async def handle_subscription_deleted(event: dict, db, now) -> None:
    subscription = event['data']['object']
    stripe_subscription_id = subscription.get('id')
    
    # 1. Update status
    await SubscriptionService.update_stripe_subscription_status(stripe_subscription_id, "canceled")
    
    # 2. Ensure we have the user_id for the log
    user_id = subscription.get('metadata', {}).get('user_id')
    if not user_id:
        stripe_sub = await db["stripe_subscriptions"].find_one({"stripe_subscription_id": stripe_subscription_id})
        user_id = stripe_sub.get("user_id") if stripe_sub else None

    # 3. Insert specific cancellation log
    await db["transaction_logs"].insert_one({
        "session_id": stripe_subscription_id,
        "user_id": user_id,
        "status": "canceled",
        "created_at": now,
        "updated_at": now,
        "type": "subscription.deleted",
        "metadata": subscription.get('metadata')
    })
    
    try:
        await log_audit_event(
            actor_id=user_id or "system",
            action="SUBSCRIPTION_DELETED",
            resource="subscriptions",
            resource_id=stripe_subscription_id or "",
            metadata={
                "stripe_subscription_id": stripe_subscription_id,
            },
        )
    except Exception:
        pass


async def handle_checkout_failed(event: dict, db, now) -> None:
    session = event['data']['object']
    session_id = session.get('id')
    
    await db["transaction_logs"].update_one(
        {"session_id": session_id},
        {"$set": {"status": "failed", "updated_at": now}}
    )
    
    PaymentServiceCore._trigger_event(session_id)

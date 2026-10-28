import stripe
import asyncio
import json
from datetime import datetime, timezone
from app.config import settings
from app.core.database import get_database

stripe.api_key = settings.STRIPE_SECRET_KEY

class PaymentServiceCore:
    _session_events: dict[str, asyncio.Event] = {}

    @classmethod
    def _get_event(cls, session_id: str) -> asyncio.Event:
        if session_id not in cls._session_events:
            cls._session_events[session_id] = asyncio.Event()
        return cls._session_events[session_id]

    @classmethod
    def _trigger_event(cls, session_id: str):
        if session_id in cls._session_events:
            cls._session_events[session_id].set()

    @staticmethod
    async def validate_promotion_code(code: str, user_id: str = None) -> dict:
        """
        Validate an active Stripe promotion code.
        If user_id is provided, also ensures the user/organization is eligible
        (e.g., has not already redeemed a trial or promo code).
        """
        try:
            from bson import ObjectId
            db = await get_database()
            promos = stripe.PromotionCode.list(code=code.strip(), active=True, limit=1)
            if not promos or not promos.data:
                return {
                    "valid": False,
                    "code": code,
                    "message": "Invalid or inactive promotion code."
                }
            promo = promos.data[0]
            coupon = promo.coupon
            restrictions = getattr(promo, "restrictions", {}) or {}
            is_first_time_only = restrictions.get("first_time_transaction", False)

            # If user_id is provided, check user and org eligibility
            if user_id:
                user = await db["users"].find_one({"_id": ObjectId(user_id)})
                if user:
                    account_type = user.get("account_type")
                    org_id = user.get("organization_id")

                    has_had_trial = False
                    org = None
                    if account_type == "individual":
                        has_had_trial = user.get("has_had_trial", False)
                    elif org_id:
                        org = await db["organizations"].find_one({"_id": ObjectId(org_id)})
                        if org:
                            has_had_trial = org.get("has_had_trial", False)

                    if account_type == "individual" or not org_id:
                        sub_query = {"user_id": user_id}
                        stripe_sub_query = {"user_id": user_id}
                    else:
                        sub_query = {"$or": [{"user_id": user_id}, {"organization_id": org_id}]}
                        stripe_sub_query = {"$or": [{"user_id": user_id}, {"organization_id": org_id}]}

                    prior_promo_sub = await db["subscriptions"].find_one({**sub_query, "has_promo_discount": True})
                    prior_any_sub = await db["subscriptions"].find_one(sub_query)

                    existing_stripe_sub = await db["stripe_subscriptions"].find_one(
                        {**stripe_sub_query, "stripe_customer_id": {"$exists": True, "$ne": None}},
                        sort=[("updated_at", -1)]
                    )
                    has_stripe_customer = bool(existing_stripe_sub or user.get("stripe_customer_id") or (org and org.get("stripe_customer_id")))
                    is_returning_customer = bool(has_had_trial or prior_promo_sub or prior_any_sub or has_stripe_customer)

                    if is_returning_customer and (is_first_time_only or coupon.percent_off == 100):
                        return {
                            "valid": False,
                            "code": code,
                            "message": "This promotion code is only valid for first-time subscribers."
                        }

            return {
                "valid": True,
                "code": promo.code,
                "percent_off": coupon.percent_off,
                "amount_off": (coupon.amount_off / 100.0) if coupon.amount_off else None,
                "currency": coupon.currency,
                "duration": coupon.duration,
                "duration_in_months": coupon.duration_in_months,
                "message": "Promotion code is valid."
            }
        except Exception as e:
            return {
                "valid": False,
                "code": code,
                "message": f"Error validating promo code: {str(e)}"
            }

    @staticmethod
    async def create_customer_portal_session(user_id: str, return_url: str) -> dict:
        """
        Create a self-service Stripe Customer Portal session so the user can add or update payment methods.
        """
        from fastapi import HTTPException
        from bson import ObjectId

        db = await get_database()
        stripe_sub = await db["stripe_subscriptions"].find_one(
            {"user_id": user_id, "stripe_customer_id": {"$exists": True, "$ne": None}},
            sort=[("updated_at", -1)]
        )
        customer_id = stripe_sub.get("stripe_customer_id") if stripe_sub else None

        if not customer_id:
            user = await db["users"].find_one({"_id": ObjectId(user_id)})
            if user:
                customer_id = user.get("stripe_customer_id")

        if not customer_id:
            raise HTTPException(
                status_code=400,
                detail="No Stripe billing customer record found for this user. Please subscribe first."
            )

        portal_session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=return_url
        )
        return {"url": portal_session.url}

    @staticmethod
    async def create_checkout_session(
        user_id: str, 
        plan: str, 
        success_url: str, 
        cancel_url: str, 
        promo_code: str = None
    ) -> dict:
        """
        Create a Stripe Checkout Session and log the transaction as initiated.
        Supports promo codes, cardless $0 trials, and Month 2 trial suppression.
        """
        from fastapi import HTTPException
        from bson import ObjectId

        db = await get_database()
        
        # 1. Fetch purchasing user
        user = await db["users"].find_one({"_id": ObjectId(user_id)})
        if not user:
            raise HTTPException(status_code=404, detail="Purchasing user not found")

        # 2. Gating checks
        account_type = user.get("account_type")
        role = user.get("role")
        if account_type != "individual" and role != "org_admin":
            raise HTTPException(
                status_code=403,
                detail="Only organization admins are allowed to purchase subscription plans for the organization."
            )

        # 3. Lookup plan from database
        plan_doc = await db["plans"].find_one({
            "$or": [
                {"plan_id": plan},
                {"name": plan},
                {"user_type": plan},
                {"allowed_roles": plan},
                {"stripe_price_id": plan}
            ]
        })

        if plan_doc:
            allowed_roles = plan_doc.get("allowed_roles")
            user_role = user.get("role")
            if allowed_roles and user_role not in allowed_roles:
                raise HTTPException(
                    status_code=403,
                    detail=f"This plan is not available for users with the role '{user_role}'."
                )

        # Determine if we should use fallback or real price ID
        use_fallback = True
        stripe_price_id = ""
        
        if plan_doc:
            stripe_price_id = plan_doc.get("stripe_price_id", "")
            if stripe_price_id and stripe_price_id.startswith("price_") and "placeholder" not in stripe_price_id:
                use_fallback = False

        plan_name = plan_doc["name"] if plan_doc else plan
        plan_id = plan_doc["plan_id"] if plan_doc else None
        if plan_doc:
            amount = int(plan_doc["price"] * 100)
        elif plan.lower() == "student":
            amount = 2000  # $20.00
        else:
            amount = 9900  # $99.00

        currency = plan_doc.get("currency", "usd") if plan_doc else "usd"
        display_name = (plan_doc.get("display_name") if plan_doc else None) or f"{plan.replace('_', ' ').title()} Plan"
        cycle = plan_doc.get("cycle", "monthly") if plan_doc else "monthly"

        org_id = user.get("organization_id")
        initial_quantity = 1
        if account_type != "individual" and org_id:
            from app.core.constants import UserRole
            active_dentists = await db["users"].count_documents({
                "organization_id": org_id,
                "role": UserRole.DENTIST,
                "is_active": True
            })
            initial_quantity = max(1, active_dentists)

        # Build line items
        if use_fallback:
            interval = "year" if cycle == "yearly" else "month"
            line_items = [{
                'price_data': {
                    'currency': currency,
                    'product_data': {
                        'name': display_name,
                    },
                    'unit_amount': amount,
                    'recurring': {'interval': interval}
                },
                'quantity': initial_quantity,
            }]
        else:
            line_items = [{
                'price': stripe_price_id,
                'quantity': initial_quantity,
            }]

        # Prepare metadata
        session_metadata = {
            "plan": plan_name,
            "user_id": user_id,
            "amount": amount,
            "quantity": initial_quantity,
            "date": datetime.now(timezone.utc).isoformat()
        }
        if plan_id:
            session_metadata["plan_id"] = plan_id

        if account_type != "individual" and org_id:
            session_metadata["organization_id"] = org_id

        # Check if the user/org has already had a trial or prior subscription
        has_had_trial = False
        org = None
        if account_type == "individual":
            has_had_trial = user.get("has_had_trial", False)
        else:
            if org_id:
                org = await db["organizations"].find_one({"_id": ObjectId(org_id)})
                if org:
                    has_had_trial = org.get("has_had_trial", False)

        if account_type == "individual" or not org_id:
            sub_query = {"user_id": user_id}
            stripe_sub_query = {"user_id": user_id}
        else:
            sub_query = {"$or": [{"user_id": user_id}, {"organization_id": org_id}]}
            stripe_sub_query = {"$or": [{"user_id": user_id}, {"organization_id": org_id}]}

        prior_promo_sub = await db["subscriptions"].find_one({**sub_query, "has_promo_discount": True})
        prior_any_sub = await db["subscriptions"].find_one(sub_query)

        # Check if user or organization already has an existing Stripe customer ID
        existing_stripe_sub = await db["stripe_subscriptions"].find_one(
            {**stripe_sub_query, "stripe_customer_id": {"$exists": True, "$ne": None}},
            sort=[("updated_at", -1)]
        )
        has_stripe_customer = bool(existing_stripe_sub or user.get("stripe_customer_id") or (org and org.get("stripe_customer_id")))
        is_returning_customer = bool(has_had_trial or prior_promo_sub or prior_any_sub or has_stripe_customer)

        # Validate promo code if provided
        promo_obj = None
        is_promo_applied = False
        if promo_code:
            promos = stripe.PromotionCode.list(code=promo_code.strip(), active=True, limit=1)
            if not promos or not promos.data:
                raise HTTPException(
                    status_code=400,
                    detail="The provided promotion code is invalid or inactive."
                )
            promo_obj = promos.data[0]
            coupon = promo_obj.coupon
            restrictions = getattr(promo_obj, "restrictions", {}) or {}
            is_first_time_only = restrictions.get("first_time_transaction", False)

            if is_returning_customer and (is_first_time_only or coupon.percent_off == 100):
                raise HTTPException(
                    status_code=400,
                    detail="This promotion code is only valid for first-time subscribers."
                )

            is_promo_applied = True
            session_metadata["promo_code"] = promo_obj.code

        try:
            subscription_data = {"metadata": session_metadata}
            
            # If promo code is applied, the promo IS the free trial period (suppress default plan trial).
            # When Month 2 arrives, has_had_trial will be True so trial will never apply twice.
            if not is_promo_applied and plan_doc and plan_doc.get("trial_period") and not has_had_trial:
                subscription_data["trial_period_days"] = plan_doc["trial_period"]

            # Only skip card collection if a 100% off promo code is applied (amount due is $0).
            # General trials and all other plans strictly require credit card details upfront.
            coupon_obj = getattr(promo_obj, "coupon", None) if promo_obj else None
            percent_off = getattr(coupon_obj, "percent_off", None) if coupon_obj else None
            amount_off = getattr(coupon_obj, "amount_off", None) if coupon_obj else None

            is_100_percent_promo = bool(
                promo_obj and (
                    percent_off == 100.0 or
                    (amount_off is not None and amount_off >= amount)
                )
            )

            payment_method_collection = "if_required" if is_100_percent_promo else "always"

            session_kwargs = {
                "line_items": line_items,
                "mode": 'subscription', 
                "success_url": success_url,
                "cancel_url": cancel_url,
                "client_reference_id": user_id,
                "metadata": session_metadata,
                "subscription_data": subscription_data,
                "payment_method_collection": payment_method_collection,
            }

            # If user already has an existing Stripe customer ID, attach it
            if existing_stripe_sub and existing_stripe_sub.get("stripe_customer_id"):
                session_kwargs["customer"] = existing_stripe_sub["stripe_customer_id"]
                session_kwargs["customer_update"] = {"address": "auto"}

            # Handle discounts / promotion codes (Stripe does not allow both simultaneously)
            if promo_obj:
                session_kwargs["discounts"] = [{"promotion_code": promo_obj.id}]
            elif not is_returning_customer:
                session_kwargs["allow_promotion_codes"] = True

            session = stripe.checkout.Session.create(**session_kwargs)

            now = datetime.now(timezone.utc)
            
            transaction = {
                "session_id": session.id,
                "user_id": user_id,
                "plan": plan_name,
                "plan_id": plan_id,
                "status": "initiated",
                "amount": amount / 100.0,
                "currency": currency,
                "created_at": now,
                "updated_at": now
            }
            if promo_obj:
                transaction["promo_code"] = promo_obj.code
            if account_type != "individual" and org_id:
                transaction["organization_id"] = org_id

            await db["transaction_logs"].insert_one(transaction)

            return {
                "session_id": session.id,
                "url": session.url
            }

        except HTTPException as he:
            raise he
        except stripe.error.StripeError as se:
            err_msg = getattr(se, "user_message", None) or (se.error.message if hasattr(se, "error") and se.error else str(se))
            raise HTTPException(status_code=400, detail=err_msg)
        except Exception as e:
            raise Exception(f"Stripe setup error: {str(e)}")

    @staticmethod
    async def verify_payment_generator(session_id: str):
        db = await get_database()
        collection = db["transaction_logs"]
        
        max_retries = 24
        retry_interval_seconds = 5
        
        event = PaymentServiceCore._get_event(session_id)
        
        for _ in range(max_retries):
            transaction = await collection.find_one({"session_id": session_id})
            
            if transaction:
                current_status = transaction.get("status")
                if current_status in ["completed", "failed"]:
                    event_data = {
                        "status": current_status,
                        "session_id": session_id,
                        "plan": transaction.get("plan")
                    }
                    yield f"data: {json.dumps(event_data)}\n\n"
                    PaymentServiceCore._session_events.pop(session_id, None)
                    return
            
            yield ": keepalive\n\n"
            
            try:
                await asyncio.wait_for(event.wait(), timeout=retry_interval_seconds)
                event.clear()
            except asyncio.TimeoutError:
                pass
                
        PaymentServiceCore._session_events.pop(session_id, None)
        yield f"data: {json.dumps({'status': 'timeout', 'session_id': session_id})}\n\n"

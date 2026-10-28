from datetime import datetime, timezone, timedelta
from typing import Optional
from fastapi import HTTPException, status
from bson import ObjectId
from app.core.database import get_database

class SubscriptionService:

    @staticmethod
    async def get_active_subscription(user_id: str) -> Optional[dict]:
        """
        Fetch the current active subscription for a user.
        Checks both user-level and organization-level subscriptions.
        """
        db = await get_database()
        user = await db["users"].find_one({"_id": ObjectId(user_id)})
        if not user:
            return None
        
        now = datetime.now(timezone.utc)
        org_id = user.get("organization_id")

        if org_id:
            org = await db["organizations"].find_one({"_id": ObjectId(org_id)})
            if org and org.get("is_exempt"):
                from app.core.constants import UserRole
                active_dentists = await db["users"].count_documents({
                    "organization_id": org_id,
                    "role": UserRole.DENTIST,
                    "is_active": True
                })
                return {
                    "id": f"exempt_{org_id}",
                    "user_id": user_id,
                    "organization_id": org_id,
                    "plan": "owner_unlimited",
                    "plan_id": "owner_unlimited",
                    "is_active": "ACTIVE",
                    "is_trial": False,
                    "is_exempt": True,
                    "unit_price": 0.0,
                    "quantity": max(1, active_dentists),
                    "monthly_total": 0.0,
                    "amount_paid": 0.0,
                    "start_date": user.get("created_at") or now,
                    "end_date": None,
                    "trial_days_left": 0
                }

        conditions = [{"user_id": user_id}]
        if org_id:
            conditions.append({"organization_id": org_id})

        query = {
            "is_active": True,
            "$or": conditions,
            "$and": [
                {
                    "$or": [
                        {"end_date": {"$exists": False}},
                        {"end_date": None},
                        {"end_date": {"$gt": now}}
                    ]
                }
            ]
        }

        subscription = await db["subscriptions"].find_one(query, sort=[("created_at", -1)])
        if subscription:
            subscription["id"] = str(subscription.pop("_id"))
            
            # Calculate trial days left
            subscription["is_trial"] = subscription.get("is_trial", False)
            subscription["trial_days_left"] = 0
            if subscription["is_trial"] and subscription.get("trial_end_date"):
                trial_end = subscription["trial_end_date"]
                if trial_end.tzinfo is None:
                    trial_end = trial_end.replace(tzinfo=timezone.utc)
                if trial_end > now:
                    delta = trial_end - now
                    subscription["trial_days_left"] = max(0, int(delta.total_seconds() / 86400) + 1)
                else:
                    subscription["is_trial"] = False
                    
            return subscription
        return None

    @staticmethod
    async def list_user_subscriptions(user_id: str, skip: int = 0, limit: int = 50) -> dict:
        """
        List all subscription records for a user or their organization, newest first, with pagination.
        """
        db = await get_database()
        user = await db["users"].find_one({"_id": ObjectId(user_id)})
        if not user:
            return {"total": 0, "skip": skip, "limit": limit, "subscriptions": []}

        is_individual = (user.get("account_type") == "individual")

        if is_individual:
            query = {"user_id": user_id}
        else:
            org_id = user.get("organization_id")
            if not org_id:
                return {"total": 0, "skip": skip, "limit": limit, "subscriptions": []}
            org = await db["organizations"].find_one({"_id": ObjectId(org_id)})
            if org and org.get("is_exempt"):
                from app.core.constants import UserRole
                active_dentists = await db["users"].count_documents({
                    "organization_id": org_id,
                    "role": UserRole.DENTIST,
                    "is_active": True
                })
                exempt_sub = {
                    "id": f"exempt_{org_id}",
                    "user_id": user_id,
                    "organization_id": org_id,
                    "plan": "owner_unlimited",
                    "plan_id": "owner_unlimited",
                    "is_active": "ACTIVE",
                    "is_trial": False,
                    "is_exempt": True,
                    "unit_price": 0.0,
                    "quantity": max(1, active_dentists),
                    "monthly_total": 0.0,
                    "amount_paid": 0.0,
                    "start_date": user.get("created_at") or datetime.now(timezone.utc),
                    "end_date": None,
                    "trial_days_left": 0,
                    "created_at": user.get("created_at") or datetime.now(timezone.utc)
                }
                return {
                    "total": 1,
                    "skip": skip,
                    "limit": limit,
                    "subscriptions": [exempt_sub]
                }
            query = {"organization_id": org_id}

        total = await db["subscriptions"].count_documents(query)
        cursor = db["subscriptions"].find(query, sort=[("created_at", -1)]).skip(skip).limit(limit)

        subscriptions = []
        now = datetime.now(timezone.utc)
        async for sub in cursor:
            sub["id"] = str(sub.pop("_id"))

            is_active_bool = sub.get("is_active", False)
            end_date = sub.get("end_date")

            if not is_active_bool:
                sub["is_active"] = "CANCELLED"
            elif end_date:
                # Normalize naive datetimes from MongoDB to aware UTC
                if end_date.tzinfo is None:
                    end_date = end_date.replace(tzinfo=timezone.utc)

                if end_date < now:
                    sub["is_active"] = "EXPIRED"
                else:
                    sub["is_active"] = "ACTIVE"
            else:
                sub["is_active"] = "ACTIVE"

            # Reconcile / self-heal subscriptions that have a promo code applied
            # (e.g. if an older webhook handler mistakenly set is_trial=True)
            if sub.get("is_trial") or not sub.get("coupon_duration"):
                tx_id = sub.get("transaction_id")
                sub_stripe_id = sub.get("stripe_subscription_id")
                sub_user_id = sub.get("user_id")

                tlog = await db["transaction_logs"].find_one({
                    "$or": [
                        {"metadata.stripe_subscription_id": sub_stripe_id},
                        {"session_id": tx_id},
                        {"metadata.user_id": sub_user_id, "metadata.promo_code": {"$exists": True, "$ne": None}},
                        {"user_id": sub_user_id, "promo_code": {"$exists": True, "$ne": None}}
                    ]
                }, sort=[("created_at", -1)])

                promo_found = None
                if tlog:
                    promo_found = tlog.get("promo_code") or (tlog.get("metadata", {}).get("promo_code") if isinstance(tlog.get("metadata"), dict) else None)

                if not promo_found and tx_id and str(tx_id).startswith("in_"):
                    try:
                        import stripe
                        from app.config import settings
                        stripe.api_key = settings.STRIPE_SECRET_KEY
                        inv = stripe.Invoice.retrieve(tx_id, expand=["discount.coupon", "discount.promotion_code"])
                        if inv.discount and inv.discount.coupon:
                            promo_found = getattr(inv.discount.promotion_code, "code", None) or inv.discount.coupon.id
                    except Exception:
                        pass

                if promo_found:
                    sub["is_trial"] = False
                    sub["trial_end_date"] = None
                    sub["trial_days_left"] = 0
                    sub["has_promo_discount"] = True
                    sub["coupon_code"] = promo_found
                    sub["coupon_duration"] = "once"
                    sub["coupon_duration_in_months"] = 1
                    sub["coupon_name"] = "One Month Free"
                    sub["coupon_percent_off"] = 100.0

                    # Auto-heal MongoDB document asynchronously
                    await db["subscriptions"].update_one(
                        {"_id": ObjectId(sub["id"])},
                        {"$set": {
                            "is_trial": False,
                            "trial_end_date": None,
                            "has_promo_discount": True,
                            "coupon_code": sub["coupon_code"],
                            "coupon_duration": sub["coupon_duration"],
                            "coupon_duration_in_months": sub["coupon_duration_in_months"],
                            "coupon_name": sub["coupon_name"],
                            "coupon_percent_off": sub["coupon_percent_off"]
                        }}
                    )

            # Calculate trial days left
            sub["is_trial"] = sub.get("is_trial", False)
            sub["trial_days_left"] = 0
            if sub["is_trial"] and sub.get("trial_end_date"):
                trial_end = sub["trial_end_date"]
                if trial_end.tzinfo is None:
                    trial_end = trial_end.replace(tzinfo=timezone.utc)
                if trial_end > now:
                    delta = trial_end - now
                    sub["trial_days_left"] = max(0, int(delta.total_seconds() / 86400) + 1)
                else:
                    # Trial is over
                    sub["is_trial"] = False

            # Calculate billing price & amount paid
            unit_price = 99.0
            if sub.get("plan_id"):
                plan_doc = await db["plans"].find_one({"plan_id": sub["plan_id"]})
                if plan_doc and plan_doc.get("price"):
                    unit_price = float(plan_doc["price"])

            quantity = max(1, sub.get("quantity", 1))
            monthly_total = round(unit_price * quantity, 2)

            amount_paid = 0.0
            tx_id = sub.get("transaction_id")
            if tx_id:
                tx_log = await db["transaction_logs"].find_one({"session_id": tx_id})
                if tx_log and tx_log.get("amount") is not None:
                    raw_amt = float(tx_log["amount"])
                    amount_paid = round(raw_amt / 100.0, 2) if raw_amt > 1000 else round(raw_amt, 2)
                elif not sub["is_trial"]:
                    amount_paid = monthly_total
            elif not sub["is_trial"]:
                amount_paid = monthly_total

            sub["unit_price"] = unit_price
            sub["quantity"] = quantity
            sub["monthly_total"] = monthly_total
            sub["amount_paid"] = amount_paid

            # Query Stripe pending proration items for active organization subscriptions
            pending_proration = 0.0
            if is_active_bool and sub.get("organization_id") and not sub.get("is_trial"):
                org_id = sub["organization_id"]
                stripe_sub_doc = await db["stripe_subscriptions"].find_one({"organization_id": org_id, "status": "active"})
                if stripe_sub_doc and stripe_sub_doc.get("stripe_subscription_id"):
                    try:
                        import stripe
                        sub_id = stripe_sub_doc["stripe_subscription_id"]
                        preview_invoice = stripe.Invoice.upcoming(
                            customer=stripe_sub_doc.get("stripe_customer_id"),
                            subscription=sub_id
                        )
                        if preview_invoice and preview_invoice.get("lines", {}).get("data"):
                            prorated_items = [
                                item for item in preview_invoice["lines"]["data"]
                                if item.get("proration") or item.get("type") == "proration"
                            ]
                            if prorated_items:
                                pending_proration = round(sum(item.get("amount", 0) for item in prorated_items) / 100.0, 2)
                    except Exception:
                        pass
            sub["pending_proration"] = pending_proration

            subscriptions.append(sub)

        return {
            "total": total,
            "skip": skip,
            "limit": limit,
            "subscriptions": subscriptions
        }

    @staticmethod
    async def list_transaction_logs(skip: int = 0, limit: int = 50, search: Optional[str] = None, status: Optional[str] = None, transaction_type: Optional[str] = None) -> dict:
        db = await get_database()

        query = {}
        if search:
            # Find users matching by name or email to get their IDs
            matching_users = await db["users"].find({
                "$or": [
                    {"full_name": {"$regex": search, "$options": "i"}},
                    {"email": {"$regex": search, "$options": "i"}},
                    # Also allow searching by the ID itself
                    {"_id": {"$regex": search, "$options": "i"} if len(search) == 24 else None}
                ]
            }, {"_id": 1}).to_list(100)

            user_ids = [str(u["_id"]) for u in matching_users]

            # Always include the search string itself in the list of IDs (direct match)
            if search not in user_ids:
                user_ids.append(search)

            query["user_id"] = {"$in": user_ids}

        if status:
            query["status"] = status
        if transaction_type:
            query["type"] = transaction_type

        total = await db["transaction_logs"].count_documents(query)

        # Aggregation pipeline for joined results
        pipeline = [
            {"$match": query},
            {"$sort": {"created_at": -1}},
            {"$skip": skip},
            {"$limit": limit},
            {"$addFields": {
                "user_oid": {
                    "$cond": {
                        "if": {"$and": [{"$ne": ["$user_id", None]}, {"$ne": ["$user_id", ""]}]},
                        "then": {"$toObjectId": "$user_id"},
                        "else": None
                    }
                }
            }},
            {"$lookup": {
                "from": "users",
                "localField": "user_oid",
                "foreignField": "_id",
                "as": "user_info"
            }},
            {"$unwind": {"path": "$user_info", "preserveNullAndEmptyArrays": True}},
            {"$addFields": {
                "id": {"$toString": "$_id"},
                "user_name": {
                    "$cond": {
                        "if": {"$ne": ["$user_id", None]},
                        "then": {"$ifNull": ["$user_info.full_name", {"$ifNull": ["$user_info.email", "Unknown"]}]},
                        "else": "System / Unlinked"
                    }
                }
            }},
            {"$project": {"user_info": 0, "user_oid": 0, "_id": 0}}
        ]

        logs = await db["transaction_logs"].aggregate(pipeline).to_list(limit)

        return {
            "total": total,
            "skip": skip,
            "limit": limit,
            "transactions": logs
        }

    @staticmethod
    async def create_subscription(user_id: str, plan: str, end_date: Optional[datetime] = None, transaction_id: Optional[str] = None, stripe_subscription_id: Optional[str] = None, promo_code: Optional[str] = None) -> dict:
        """
        Create a new subscription record. Deactivates previous active subscriptions.
        """
        print('create subscription called with', user_id, plan, end_date, transaction_id, stripe_subscription_id, promo_code)
        db = await get_database()
        now = datetime.now(timezone.utc)

        # 1. Fetch purchasing user
        user = await db["users"].find_one({"_id": ObjectId(user_id)})
        if not user:
            raise HTTPException(status_code=404, detail="Purchasing user not found")

        is_individual = (user.get("account_type") == "individual")
        org_id = user.get("organization_id") if not is_individual else None

        # 2. Lookup plan from database
        plan_doc = await db["plans"].find_one({
            "$or": [
                {"plan_id": plan},
                {"name": plan},
                {"stripe_price_id": plan}
            ]
        })

        if plan_doc:
            allowed_roles = plan_doc.get("allowed_roles")
            user_role = user.get("role")
            if allowed_roles and user_role not in allowed_roles:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"This plan is not available for users with the role '{user_role}'."
                )

        # Check if user/org has already had a trial
        has_had_trial = False
        if is_individual:
            has_had_trial = user.get("has_had_trial", False)
        else:
            if org_id:
                org = await db["organizations"].find_one({"_id": ObjectId(org_id)})
                if org:
                    has_had_trial = org.get("has_had_trial", False)

        plan_name = plan
        plan_id = None
        cycle = "monthly"

        if plan_doc:
            plan_name = plan_doc["name"]
            plan_id = plan_doc["plan_id"]
            cycle = plan_doc.get("cycle", "monthly")

        is_trial = False
        trial_end_date = None

        has_promo_discount = False
        coupon_id = None
        coupon_name = None
        coupon_code = promo_code
        coupon_duration = None  # "once", "repeating", "forever"
        coupon_duration_in_months = None  # e.g. 1, 3, 6, 12, or None if forever
        coupon_percent_off = None
        coupon_amount_off = None
        discount_end = None

        import stripe
        from app.config import settings
        stripe.api_key = settings.STRIPE_SECRET_KEY

        discount_data = None
        # 1. Inspect Stripe Subscription discount
        if stripe_subscription_id:
            try:
                stripe_sub_obj = stripe.Subscription.retrieve(
                    stripe_subscription_id,
                    expand=["discount.coupon", "discount.promotion_code"]
                )
                discount_data = getattr(stripe_sub_obj, "discount", None) or (stripe_sub_obj.get("discount") if isinstance(stripe_sub_obj, dict) else None)
                if not coupon_code and hasattr(stripe_sub_obj, "metadata") and stripe_sub_obj.metadata:
                    coupon_code = stripe_sub_obj.metadata.get("promo_code")
            except Exception as e:
                print(f"Error fetching Stripe subscription discount: {e}")

        # 2. If no discount on subscription (e.g. 'once' coupon consumed upon invoice creation), check Stripe Invoice
        if not discount_data and transaction_id and str(transaction_id).startswith("in_"):
            try:
                inv_obj = stripe.Invoice.retrieve(
                    transaction_id,
                    expand=["discount.coupon", "discount.promotion_code", "discounts.coupon", "discounts.promotion_code"]
                )
                discount_data = getattr(inv_obj, "discount", None) or (inv_obj.get("discount") if isinstance(inv_obj, dict) else None)
                if not discount_data:
                    discounts = getattr(inv_obj, "discounts", None) or (inv_obj.get("discounts") if isinstance(inv_obj, dict) else None)
                    if discounts and len(discounts) > 0:
                        discount_data = discounts[0]
            except Exception as e:
                print(f"Error fetching Stripe invoice discount: {e}")

        # 3. Extract coupon details and duration from discount
        if discount_data:
            has_promo_discount = True
            coupon = getattr(discount_data, "coupon", None) or (discount_data.get("coupon") if isinstance(discount_data, dict) else None)
            if coupon:
                coupon_id = getattr(coupon, "id", None) or (coupon.get("id") if isinstance(coupon, dict) else None)
                coupon_name = getattr(coupon, "name", None) or (coupon.get("name") if isinstance(coupon, dict) else None)
                coupon_duration = getattr(coupon, "duration", None) or (coupon.get("duration") if isinstance(coupon, dict) else None)
                coupon_duration_in_months = getattr(coupon, "duration_in_months", None) or (coupon.get("duration_in_months") if isinstance(coupon, dict) else None)

                # Duration normalization:
                # "once" -> 1 month
                # "repeating" -> coupon.duration_in_months (e.g. 2, 3, 6, 12)
                # "forever" -> None (indefinite)
                if coupon_duration == "once":
                    coupon_duration_in_months = 1

                coupon_percent_off = getattr(coupon, "percent_off", None) or (coupon.get("percent_off") if isinstance(coupon, dict) else None)
                raw_amt = getattr(coupon, "amount_off", None) or (coupon.get("amount_off") if isinstance(coupon, dict) else None)
                if raw_amt:
                    coupon_amount_off = round(float(raw_amt) / 100.0, 2)

            promo_ref = getattr(discount_data, "promotion_code", None) or (discount_data.get("promotion_code") if isinstance(discount_data, dict) else None)
            if promo_ref:
                if isinstance(promo_ref, str) and promo_ref.startswith("promo_"):
                    try:
                        p_obj = stripe.PromotionCode.retrieve(promo_ref)
                        coupon_code = p_obj.code
                    except Exception:
                        if not coupon_code:
                            coupon_code = promo_ref
                elif hasattr(promo_ref, "code"):
                    coupon_code = promo_ref.code

            raw_end = getattr(discount_data, "end", None) or (discount_data.get("end") if isinstance(discount_data, dict) else None)
            if raw_end:
                discount_end = datetime.fromtimestamp(raw_end, timezone.utc)

        # 4. Fallback: if coupon_code was provided but not found via discount_data, lookup via Stripe API
        if not has_promo_discount and coupon_code:
            try:
                promos = stripe.PromotionCode.list(code=coupon_code.strip(), active=True, limit=1)
                if promos and promos.data:
                    p_obj = promos.data[0]
                    coupon = p_obj.coupon
                    has_promo_discount = True
                    coupon_id = coupon.id
                    coupon_name = coupon.name
                    coupon_code = p_obj.code
                    coupon_duration = coupon.duration
                    coupon_duration_in_months = coupon.duration_in_months
                    if coupon_duration == "once":
                        coupon_duration_in_months = 1
                    coupon_percent_off = coupon.percent_off
                    if coupon.amount_off:
                        coupon_amount_off = round(float(coupon.amount_off) / 100.0, 2)
            except Exception as e:
                print(f"Error validating fallback promo code: {e}")

        # Free Trial logic:
        # A promo code coupon gives free access via discount, which DISABLES the free trial.
        # Trial ONLY applies if NO coupon was applied and plan has a trial period and user hasn't had trial.
        if has_promo_discount:
            is_trial = False
            trial_end_date = None
        elif plan_doc and plan_doc.get("trial_period") and not has_had_trial:
            is_trial = True
            trial_days = plan_doc.get("trial_period", 14)
            trial_end_date = now + timedelta(days=trial_days)
            end_date = trial_end_date

            # Mark user/org as having had a trial
            if is_individual:
                await db["users"].update_one(
                    {"_id": ObjectId(user_id)},
                    {"$set": {"has_had_trial": True, "updated_at": now}}
                )
            elif org_id:
                await db["organizations"].update_one(
                    {"_id": ObjectId(org_id)},
                    {"$set": {"has_had_trial": True, "updated_at": now}}
                )
        else:
            is_trial = False
            trial_end_date = None

        if end_date is None:
            if cycle == "yearly":
                end_date = now + timedelta(days=365)
            else:
                end_date = now + timedelta(days=30)

        # 3. Deactivate previous active subscriptions for the target user/org
        if is_individual:
            await db["subscriptions"].update_many(
                {"user_id": user_id, "is_active": True},
                {"$set": {"is_active": False, "updated_at": now}}
            )
        else:
            if org_id:
                await db["subscriptions"].update_many(
                    {"organization_id": org_id, "is_active": True},
                    {"$set": {"is_active": False, "updated_at": now}}
                )

        # 4. Insert new subscription
        new_sub = {
            "user_id": user_id,
            "plan": plan_name,
            "plan_id": plan_id,
            "is_active": True,
            "start_date": now,
            "end_date": end_date,
            "is_trial": is_trial,
            "trial_end_date": trial_end_date,
            "has_promo_discount": has_promo_discount,
            "coupon_id": coupon_id,
            "coupon_name": coupon_name,
            "coupon_code": coupon_code,
            "coupon_duration": coupon_duration,
            "coupon_duration_in_months": coupon_duration_in_months,
            "coupon_percent_off": coupon_percent_off,
            "coupon_amount_off": coupon_amount_off,
            "discount_end": discount_end,
            "created_at": now,
            "updated_at": now
        }

        if not is_individual and org_id:
            new_sub["organization_id"] = org_id
            # Preserve or fetch organization seat quantity
            stripe_sub_doc = None
            if stripe_subscription_id:
                stripe_sub_doc = await db["stripe_subscriptions"].find_one({"stripe_subscription_id": stripe_subscription_id})
            if not stripe_sub_doc:
                stripe_sub_doc = await db["stripe_subscriptions"].find_one({"organization_id": org_id, "status": {"$in": ["active", "trialing"]}})

            if stripe_sub_doc and stripe_sub_doc.get("quantity"):
                new_sub["quantity"] = stripe_sub_doc["quantity"]
            else:
                from app.core.constants import UserRole
                active_dentists = await db["users"].count_documents({
                    "organization_id": org_id,
                    "role": UserRole.DENTIST,
                    "is_active": True
                })
                new_sub["quantity"] = max(1, active_dentists)

        if transaction_id:
            new_sub["transaction_id"] = transaction_id
        if stripe_subscription_id:
            new_sub["stripe_subscription_id"] = stripe_subscription_id

        result = await db["subscriptions"].insert_one(new_sub)
        new_sub["id"] = str(result.inserted_id)
        return new_sub

    @staticmethod
    async def link_stripe_subscription(user_id: str, stripe_subscription_id: str, stripe_customer_id: str, plan: str) -> None:
        db = await get_database()
        now = datetime.now(timezone.utc)

        user = await db["users"].find_one({"_id": ObjectId(user_id)})
        is_individual = user.get("account_type") == "individual" if user else True
        org_id = user.get("organization_id") if (user and not is_individual) else None

        update_fields = {
            "status": "active",
            "plan": plan,
            "stripe_customer_id": stripe_customer_id,
            "updated_at": now
        }
        if org_id:
            update_fields["organization_id"] = org_id

        await db["stripe_subscriptions"].update_one(
            {"user_id": user_id, "stripe_subscription_id": stripe_subscription_id},
            {"$set": update_fields},
            upsert=True
        )

    @staticmethod
    async def update_stripe_subscription_status(stripe_subscription_id: str, status: str) -> None:
        db = await get_database()
        await db["stripe_subscriptions"].update_one(
            {"stripe_subscription_id": stripe_subscription_id},
            {"$set": {
                "status": status,
                "updated_at": datetime.now(timezone.utc)
            }}
        )

    @staticmethod
    async def suspend_active_subscriptions(stripe_subscription_id: str) -> None:
        """
        Immediately suspend / revoke active access chunks linked to this subscription ID.
        This is used for failed invoices or dunning periods (past_due).
        """
        db = await get_database()
        now = datetime.now(timezone.utc)
        await db["subscriptions"].update_many(
            {"stripe_subscription_id": stripe_subscription_id, "is_active": True},
            {"$set": {"is_active": False, "updated_at": now}}
        )

    @staticmethod
    async def cancel_subscription(user_id: str) -> dict:
        import stripe
        from app.config import settings
        stripe.api_key = settings.STRIPE_SECRET_KEY

        db = await get_database()
        user = await db["users"].find_one({"_id": ObjectId(user_id)})
        if not user:
            raise HTTPException(status_code=404, detail="User not found.")

        is_individual = (user.get("account_type") == "individual")

        active_statuses = {"$in": ["active", "trialing"]}

        if is_individual:
            stripe_sub = await db["stripe_subscriptions"].find_one({"user_id": user_id, "status": active_statuses})
        else:
            if user.get("role") != "org_admin":
                raise HTTPException(status_code=403, detail="Only organization admins are allowed to cancel the subscription.")
            org_id = user.get("organization_id")
            if not org_id:
                raise HTTPException(status_code=400, detail="User does not belong to any organization.")
            stripe_sub = await db["stripe_subscriptions"].find_one({"organization_id": org_id, "status": active_statuses})
            if not stripe_sub:
                # Fall back to finding by user_id
                stripe_sub = await db["stripe_subscriptions"].find_one({"user_id": user_id, "status": active_statuses})

        if not stripe_sub:
            raise HTTPException(status_code=400, detail="No active or trialing Stripe subscription found.")

        try:
            stripe.Subscription.modify(
                stripe_sub["stripe_subscription_id"],
                cancel_at_period_end=True
            )
            # Update local state so we know it's pending cancellation
            await SubscriptionService.update_stripe_subscription_status(stripe_sub["stripe_subscription_id"], "pending_cancellation")
            msg = "Trial will end without renewal at the end of the trial period." if stripe_sub.get("status") == "trialing" else "Subscription will cancel at period end."
            return {"success": True, "message": msg}
        except stripe.error.InvalidRequestError as e:
            if "No such subscription" in str(e) or e.code == "resource_missing":
                await SubscriptionService.update_stripe_subscription_status(stripe_sub["stripe_subscription_id"], "canceled")
                await SubscriptionService.suspend_active_subscriptions(stripe_sub["stripe_subscription_id"])
                return {"success": True, "message": "Subscription was already canceled or not found on Stripe. Updated local status."}
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

    @staticmethod
    async def resume_subscription(user_id: str) -> dict:
        """
        Resume/reactivate a subscription that is currently set to cancel at period end.
        """
        import stripe
        from app.config import settings
        stripe.api_key = settings.STRIPE_SECRET_KEY

        db = await get_database()
        user = await db["users"].find_one({"_id": ObjectId(user_id)})
        if not user:
            raise HTTPException(status_code=404, detail="User not found.")

        is_individual = (user.get("account_type") == "individual")

        if is_individual:
            stripe_sub = await db["stripe_subscriptions"].find_one({
                "user_id": user_id, 
                "status": "pending_cancellation"
            })
        else:
            if user.get("role") != "org_admin":
                raise HTTPException(status_code=403, detail="Only organization admins are allowed to resume the subscription.")
            org_id = user.get("organization_id")
            if not org_id:
                raise HTTPException(status_code=400, detail="User does not belong to any organization.")
            stripe_sub = await db["stripe_subscriptions"].find_one({
                "organization_id": org_id, 
                "status": "pending_cancellation"
            })
            if not stripe_sub:
                stripe_sub = await db["stripe_subscriptions"].find_one({
                    "user_id": user_id, 
                    "status": "pending_cancellation"
                })

        if not stripe_sub:
            raise HTTPException(status_code=400, detail="No subscription pending cancellation found.")

        try:
            stripe.Subscription.modify(
                stripe_sub["stripe_subscription_id"],
                cancel_at_period_end=False
            )
            await SubscriptionService.update_stripe_subscription_status(stripe_sub["stripe_subscription_id"], "active")
            return {"success": True, "message": "Subscription reactivated successfully. Automated renewal resumed."}
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Failed to resume subscription: {str(e)}")

    @staticmethod
    async def sync_org_dentist_subscription_quantity(org_id: str) -> None:
        """
        Sync Stripe subscription seat count for an organization based on its active dentist count.
        Uses deferred proration (proration_behavior='create_prorations') so prorated charges or
        credits are deferred to the next monthly invoice cycle instead of charging immediately.
        """
        import stripe
        import logging
        from app.config import settings
        from app.core.constants import UserRole
        stripe.api_key = settings.STRIPE_SECRET_KEY
        logger = logging.getLogger(__name__)

        db = await get_database()
        org = await db["organizations"].find_one({"_id": ObjectId(org_id)})
        if org and org.get("is_exempt"):
            return

        # 1. Count active dentists in the organization (only dentist role)
        active_dentists = await db["users"].count_documents({
            "organization_id": org_id,
            "role": UserRole.DENTIST,
            "is_active": True
        })

        new_quantity = max(1, active_dentists)

        # 2. Get active Stripe subscription record for the org
        stripe_sub_doc = await db["stripe_subscriptions"].find_one({
            "organization_id": org_id,
            "status": {"$in": ["active", "pending_cancellation", "trialing"]}
        })
        if not stripe_sub_doc:
            return

        stripe_sub_id = stripe_sub_doc.get("stripe_subscription_id")
        if not stripe_sub_id:
            return

        try:
            stripe_sub_obj = stripe.Subscription.retrieve(stripe_sub_id)
            if not stripe_sub_obj or not stripe_sub_obj.get('items', {}).get('data'):
                return

            item_id = stripe_sub_obj['items']['data'][0]['id']
            current_quantity = stripe_sub_obj['items']['data'][0].get('quantity', 1)

            if current_quantity != new_quantity:
                now = datetime.now(timezone.utc)
                # Approach A: If organization is in a 100% promo period or trial, suppress prorations
                active_sub = await db["subscriptions"].find_one({"organization_id": org_id, "is_active": True})
                is_promo_free = False
                if active_sub and active_sub.get("has_promo_discount"):
                    if active_sub.get("coupon_percent_off") == 100.0 or active_sub.get("coupon_duration") in ["once", "forever"]:
                        end_d = active_sub.get("end_date")
                        if end_d:
                            if end_d.tzinfo is None:
                                end_d = end_d.replace(tzinfo=timezone.utc)
                            if now < end_d:
                                is_promo_free = True
                        else:
                            is_promo_free = True

                # Also verify directly on Stripe subscription discount
                if not is_promo_free and hasattr(stripe_sub_obj, "discount") and stripe_sub_obj.discount:
                    coupon = getattr(stripe_sub_obj.discount, "coupon", None)
                    if coupon and getattr(coupon, "percent_off", None) == 100.0:
                        is_promo_free = True

                is_trial_mode = bool(stripe_sub_doc.get("status") == "trialing" or (active_sub and active_sub.get("is_trial")))
                proration_behavior = "none" if (is_promo_free or is_trial_mode) else "create_prorations"

                stripe.Subscription.modify(
                    stripe_sub_id,
                    items=[{"id": item_id, "quantity": new_quantity}],
                    proration_behavior=proration_behavior
                )
                await db["stripe_subscriptions"].update_one(
                    {"stripe_subscription_id": stripe_sub_id},
                    {"$set": {"quantity": new_quantity, "updated_at": now}}
                )
                await db["subscriptions"].update_many(
                    {"organization_id": org_id, "is_active": True},
                    {"$set": {"quantity": new_quantity, "updated_at": now}}
                )

                # Log audit event for subscription price change
                try:
                    from app.background.report_processor import log_audit_event
                    unit_price = 99.0
                    old_monthly_rate = round(unit_price * current_quantity, 2)
                    new_monthly_rate = round(unit_price * new_quantity, 2)
                    price_change = round(new_monthly_rate - old_monthly_rate, 2)
                    action_type = "DENTIST_SEAT_ADDED" if new_quantity > current_quantity else "DENTIST_SEAT_REMOVED"

                    await log_audit_event(
                        actor_id="system",
                        action="SUBSCRIPTION_PRICE_CHANGED",
                        resource="subscriptions",
                        resource_id=stripe_sub_id,
                        metadata={
                            "organization_id": org_id,
                            "event_type": action_type,
                            "old_quantity": current_quantity,
                            "new_quantity": new_quantity,
                            "unit_price": unit_price,
                            "old_monthly_rate": old_monthly_rate,
                            "new_monthly_rate": new_monthly_rate,
                            "monthly_price_change": price_change,
                            "stripe_subscription_id": stripe_sub_id,
                            "proration_behavior": proration_behavior
                        },
                        organization_id=org_id
                    )
                except Exception as audit_err:
                    logger.warning(f"[STRIPE-SEATS] Failed to write audit log for price change: {audit_err}")

                logger.info(f"[STRIPE-SEATS] Updated org {org_id} subscription quantity to {new_quantity} (proration_behavior={proration_behavior})")
        except Exception as e:
            logger.error(f"[STRIPE-SEATS] Failed to sync subscription quantity for org {org_id}: {e}")

    @staticmethod
    async def preview_org_member_change_impact(
        org_id: str,
        target_user_id: Optional[str] = None,
        action: Optional[str] = None,
        new_role: Optional[str] = None
    ) -> dict:
        """
        Calculate billing preview and price impact for an organization member change.
        Handles actions: 'activate', 'deactivate', 'change_role', 'invite'.
        Accurately computes current seat count vs proposed seat count, monthly pricing changes,
        and estimated prorated charges/credits deferred to the upcoming billing cycle.
        """
        import stripe
        import logging
        from app.config import settings
        from app.core.constants import UserRole
        stripe.api_key = settings.STRIPE_SECRET_KEY
        logger = logging.getLogger(__name__)

        db = await get_database()
        now = datetime.now(timezone.utc)

        # 1. Fetch current active dentist roles count (only dentist role)
        dentist_roles = [UserRole.DENTIST, "dentist"]
        current_dentist_count = await db["users"].count_documents({
            "organization_id": org_id,
            "role": {"$in": dentist_roles},
            "is_active": True
        })

        proposed_dentist_count = current_dentist_count

        target_user = None
        if target_user_id and ObjectId.is_valid(target_user_id):
            target_user = await db["users"].find_one({"_id": ObjectId(target_user_id)})

        # 2. Determine proposed count based on action
        if action == "activate":
            target_role = target_user.get("role") if target_user else new_role
            if target_role in dentist_roles and (not target_user or not target_user.get("is_active")):
                proposed_dentist_count += 1
        elif action == "deactivate":
            if target_user and target_user.get("role") in dentist_roles and target_user.get("is_active"):
                proposed_dentist_count = max(0, current_dentist_count - 1)
        elif action == "change_role":
            if target_user and target_user.get("is_active"):
                old_role = str(target_user.get("role", "")).lower()
                new_role_str = str(new_role or "").lower()
                if old_role not in dentist_roles and new_role_str in dentist_roles:
                    proposed_dentist_count += 1
                elif old_role in dentist_roles and new_role_str not in dentist_roles:
                    proposed_dentist_count = max(0, current_dentist_count - 1)
        elif action == "invite":
            if new_role in dentist_roles:
                proposed_dentist_count += 1

        org = await db["organizations"].find_one({"_id": ObjectId(org_id)})
        if org and org.get("is_exempt"):
            return {
                "current_dentist_count": current_dentist_count,
                "proposed_dentist_count": proposed_dentist_count,
                "current_billed_quantity": max(1, current_dentist_count),
                "proposed_billed_quantity": max(1, proposed_dentist_count),
                "unit_price": 0.0,
                "current_monthly_total": 0.0,
                "upcoming_monthly_total": 0.0,
                "price_difference_per_month": 0.0,
                "estimated_proration": 0.0,
                "next_invoice_total_estimate": 0.0,
                "next_billing_date": None,
                "is_exempt": True,
                "impact_summary": "This organization is exempt from subscription billing (Owner Plan)."
            }

        unit_price = 99.0
        active_sub = await db["subscriptions"].find_one({"organization_id": org_id, "is_active": True})
        if active_sub and active_sub.get("plan_id"):
            plan_doc = await db["plans"].find_one({"plan_id": active_sub["plan_id"]})
            if plan_doc and plan_doc.get("price"):
                unit_price = float(plan_doc["price"])

        current_billed_quantity = max(1, current_dentist_count)
        proposed_billed_quantity = max(1, proposed_dentist_count)
        quantity_delta = proposed_billed_quantity - current_billed_quantity

        current_monthly_total = unit_price * current_billed_quantity
        upcoming_monthly_total = unit_price * proposed_billed_quantity
        price_difference_per_month = upcoming_monthly_total - current_monthly_total

        # 3. Calculate period proration estimate
        end_date = active_sub.get("end_date") if active_sub else None
        start_date = active_sub.get("start_date") if active_sub else None

        remaining_days = 20
        total_days = 30

        if end_date and isinstance(end_date, datetime):
            if end_date.tzinfo is None:
                end_date = end_date.replace(tzinfo=timezone.utc)
            if end_date > now:
                remaining_days = max(1, (end_date - now).days)

        if start_date and end_date and isinstance(start_date, datetime) and isinstance(end_date, datetime):
            if start_date.tzinfo is None:
                start_date = start_date.replace(tzinfo=timezone.utc)
            if end_date > start_date:
                total_days = max(1, (end_date - start_date).days)

        days_ratio = min(1.0, max(0.0, remaining_days / total_days))

        # Check if the active subscription is in a trial period or 100% promo period
        is_trial = False
        if active_sub:
            is_trial = bool(active_sub.get("is_trial") or active_sub.get("status") == "trialing")

        stripe_sub_doc = await db["stripe_subscriptions"].find_one({"organization_id": org_id, "status": {"$in": ["active", "trialing"]}})
        if stripe_sub_doc and stripe_sub_doc.get("status") == "trialing":
            is_trial = True

        is_promo = False
        is_forever_promo = False
        if active_sub and active_sub.get("has_promo_discount"):
            if active_sub.get("coupon_percent_off") == 100.0 or active_sub.get("coupon_duration") in ["once", "forever"]:
                end_d = active_sub.get("end_date")
                if end_d:
                    if end_d.tzinfo is None:
                        end_d = end_d.replace(tzinfo=timezone.utc)
                    if now < end_d:
                        is_promo = True
                else:
                    is_promo = True
                if active_sub.get("coupon_duration") == "forever":
                    is_forever_promo = True

        if is_forever_promo:
            estimated_proration = 0.0
            next_invoice_total_estimate = 0.0
            current_monthly_total = 0.0
            upcoming_monthly_total = 0.0
            price_difference_per_month = 0.0
        elif is_trial or is_promo:
            estimated_proration = 0.0
            next_invoice_total_estimate = upcoming_monthly_total
        else:
            estimated_proration = round(unit_price * quantity_delta * days_ratio, 2)
            next_invoice_total_estimate = max(0.0, round(upcoming_monthly_total + estimated_proration, 2))

        # 4. Stripe Invoice preview attempt if live active (non-trial, non-promo) Stripe subscription exists
        if stripe_sub_doc and stripe_sub_doc.get("stripe_subscription_id") and not is_trial and not is_promo:
            try:
                sub_id = stripe_sub_doc["stripe_subscription_id"]
                sub_obj = stripe.Subscription.retrieve(sub_id)
                if sub_obj and sub_obj.get('items', {}).get('data'):
                    item_id = sub_obj['items']['data'][0]['id']
                    preview_invoice = stripe.Invoice.upcoming(
                        customer=stripe_sub_doc.get("stripe_customer_id"),
                        subscription=sub_id,
                        subscription_items=[{"id": item_id, "quantity": proposed_billed_quantity}]
                    )
                    if preview_invoice:
                        next_invoice_total_estimate = round(preview_invoice.get("amount_due", 0) / 100.0, 2)
            except Exception:
                pass

        # 5. Build human-readable summary
        if is_trial:
            trial_date_str = end_date.strftime("%B %d, %Y") if isinstance(end_date, datetime) else "the end of your trial"
            if quantity_delta > 0:
                summary = (
                    f"Your organization is currently in a free trial period (ends in {remaining_days} days). "
                    f"Adding this dentist will increase your active dentist seats from {current_dentist_count} to {proposed_dentist_count}. "
                    f"No prorated charge is billed during free trial. "
                    f"When your trial ends on {trial_date_str}, your monthly subscription will be billed at ${upcoming_monthly_total:.2f}/mo."
                )
            elif quantity_delta < 0:
                summary = (
                    f"Your organization is currently in a free trial period (ends in {remaining_days} days). "
                    f"Deactivating this dentist will adjust your upcoming seat count to {proposed_dentist_count}. "
                    f"When your trial ends on {trial_date_str}, your monthly subscription will be billed at ${upcoming_monthly_total:.2f}/mo."
                )
            else:
                summary = (
                    f"Your organization is currently in a free trial period. "
                    f"This action does not change your upcoming plan rate (${upcoming_monthly_total:.2f}/mo starting after trial ends)."
                )
        elif is_promo:
            promo_date_str = end_date.strftime("%B %d, %Y") if isinstance(end_date, datetime) else "the end of your promotional month"
            if is_forever_promo:
                summary = (
                    f"Your organization has an active lifetime promotional plan (100% off). "
                    f"Adding this dentist will increase your active dentist seats from {current_dentist_count} to {proposed_dentist_count} at $0.00/mo."
                )
            elif quantity_delta > 0:
                summary = (
                    f"Your organization is currently in your 1-month free promotional period (ends on {promo_date_str}). "
                    f"Adding this dentist will increase your active dentist seats from {current_dentist_count} to {proposed_dentist_count}. "
                    f"No prorated charge is billed during your free month. "
                    f"When your next billing cycle begins on {promo_date_str}, your monthly subscription will be billed at ${upcoming_monthly_total:.2f}/mo."
                )
            elif quantity_delta < 0:
                summary = (
                    f"Your organization is currently in your promotional period. "
                    f"Deactivating this dentist will adjust your upcoming seat count to {proposed_dentist_count} (${upcoming_monthly_total:.2f}/mo starting on {promo_date_str})."
                )
            else:
                summary = (
                    f"Your organization is currently in your promotional period. "
                    f"This action does not change your upcoming plan rate (${upcoming_monthly_total:.2f}/mo starting on {promo_date_str})."
                )
        else:
            if quantity_delta > 0:
                summary = (
                    f"Performing this action will increase your active dentist seats from {current_dentist_count} to {proposed_dentist_count}. "
                    f"Your recurring subscription rate will change from ${current_monthly_total:.2f}/mo to ${upcoming_monthly_total:.2f}/mo. "
                    f"An estimated prorated charge of ~${abs(estimated_proration):.2f} for the remaining {remaining_days} days of the current cycle "
                    f"will be added to your next invoice (estimated total next invoice: ${next_invoice_total_estimate:.2f})."
                )
            elif quantity_delta < 0:
                summary = (
                    f"Performing this action will decrease your active dentist seats from {current_dentist_count} to {proposed_dentist_count}. "
                    f"Your recurring subscription rate will decrease from ${current_monthly_total:.2f}/mo to ${upcoming_monthly_total:.2f}/mo. "
                    f"An estimated prorated credit of ~${abs(estimated_proration):.2f} for the unused {remaining_days} days of the current cycle "
                    f"will be credited toward your next invoice (estimated total next invoice: ${next_invoice_total_estimate:.2f})."
                )
            else:
                if proposed_dentist_count == 0:
                    summary = (
                        f"Deactivating this dentist leaves 0 active dentists in your organization. "
                        f"Note that the minimum base organization plan rate is ${upcoming_monthly_total:.2f}/mo (1 seat minimum)."
                    )
                else:
                    summary = (
                        f"This action does not change your billed subscription quantity ({current_dentist_count} dentist seats). "
                        f"Your subscription rate remains ${current_monthly_total:.2f}/mo."
                    )

        return {
            "unit_price": unit_price,
            "currency": "usd",
            "current_dentist_count": current_dentist_count,
            "proposed_dentist_count": proposed_dentist_count,
            "seat_delta": quantity_delta,
            "current_monthly_total": current_monthly_total,
            "upcoming_monthly_total": upcoming_monthly_total,
            "price_difference_per_month": price_difference_per_month,
            "remaining_days_in_cycle": remaining_days,
            "total_days_in_cycle": total_days,
            "estimated_proration": estimated_proration,
            "next_invoice_total_estimate": next_invoice_total_estimate,
            "next_billing_date": end_date.isoformat() if isinstance(end_date, datetime) else end_date,
            "impact_summary": summary
        }


    @staticmethod
    async def skip_trial(user_id: str) -> dict:
        """
        Skip the trial phase of the current active subscription, immediately
        activating the full plan benefits, deducting Stripe payment, and extending
        the end date based on the plan cycle.
        """
        import stripe
        from app.config import settings
        stripe.api_key = settings.STRIPE_SECRET_KEY

        db = await get_database()
        
        # 1. Fetch active subscription
        active_sub = await SubscriptionService.get_active_subscription(user_id)
        if not active_sub:
            raise HTTPException(status_code=400, detail="No active subscription found.")
            
        if not active_sub.get("is_trial"):
            raise HTTPException(status_code=400, detail="Active subscription is not on a trial plan.")
            
        # 2. Get plan cycle (default monthly)
        cycle = "monthly"
        if active_sub.get("plan_id"):
            plan_doc = await db["plans"].find_one({"plan_id": active_sub["plan_id"]})
            if plan_doc:
                cycle = plan_doc.get("cycle", "monthly")

        # 3. End trial on Stripe immediately to deduct payment
        stripe_sub_id = active_sub.get("stripe_subscription_id")
        if stripe_sub_id:
            try:
                stripe.Subscription.modify(
                    stripe_sub_id,
                    trial_end="now"
                )
                await db["stripe_subscriptions"].update_one(
                    {"stripe_subscription_id": stripe_sub_id},
                    {"$set": {"status": "active", "updated_at": datetime.now(timezone.utc)}}
                )
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Failed to deduct payment on Stripe: {str(e)}"
                )
                
        now = datetime.now(timezone.utc)
        if cycle == "yearly":
            end_date = now + timedelta(days=365)
        else:
            end_date = now + timedelta(days=30)
            
        # 4. Update the active subscription locally
        await db["subscriptions"].update_one(
            {"_id": ObjectId(active_sub["id"])},
            {
                "$set": {
                    "is_trial": False,
                    "trial_end_date": None,
                    "end_date": end_date,
                    "updated_at": now
                }
            }
        )

        # 5. Mark user/org as having had a trial to prevent future trials
        user = await db["users"].find_one({"_id": ObjectId(user_id)})
        if user:
            is_individual = (user.get("account_type") == "individual")
            org_id = user.get("organization_id")
            if is_individual:
                await db["users"].update_one(
                    {"_id": ObjectId(user_id)},
                    {"$set": {"has_had_trial": True, "updated_at": now}}
                )
            elif org_id:
                await db["organizations"].update_one(
                    {"_id": ObjectId(org_id)},
                    {"$set": {"has_had_trial": True, "updated_at": now}}
                )

        
        return {
            "success": True, 
            "message": "Trial skipped successfully. Stripe payment deducted and full plan benefits activated."
        }


    @staticmethod
    async def check_message_limit(user_id: str, plan_type: Optional[str] = None) -> None:
        """
        Check if the user is on a free plan and has reached their limit.
        If they have >= 5 messages across all their chats, deny the request.
        """
        db = await get_database()

        # Check active subscription using our new helper logic
        active_sub = await SubscriptionService.get_active_subscription(user_id)

        if active_sub:
            # Active subscription bypasses message limit checks
            return

        # Get all chats owned by this user
        cursor = db["chats"].find({"user_id": user_id}, {"_id": 1})
        chat_ids = []
        async for chat in cursor:
            chat_ids.append(str(chat["_id"]))

        # If user has no chats, they definitely haven't reached the limit
        if not chat_ids:
            return

        # Count total messages created by this user across those chats
        message_count = await db["messages"].count_documents({
            "chat_id": {"$in": chat_ids},
            "role": "user"
        })

        if message_count >= float('inf'): # change this limit in production if desired
            msg = "You have reached the maximum of 5 free messages. Please purchase a pro plan to continue."
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "FREE_LIMIT_REACHED", "message": msg}
            )

    @staticmethod
    async def check_expiring_promo_subscriptions() -> dict:
        """
        Background/cron helper: Scan for subscriptions expiring in ~3 days that have not yet received
        the card-addition reminder email.
        """
        import stripe
        from app.config import settings
        from app.background.email_tasks import send_subscription_expiring_email

        db = await get_database()
        now = datetime.now(timezone.utc)
        target_start = now + timedelta(days=2)
        target_end = now + timedelta(days=3, hours=2)

        cursor = db["subscriptions"].find({
            "is_active": True,
            "end_date": {"$gte": target_start, "$lte": target_end},
            "expiring_email_sent": {"$ne": True}
        })

        notified_count = 0
        async for sub in cursor:
            # If coupon duration is forever, discount never expires - do not send card reminder
            if sub.get("coupon_duration") == "forever":
                continue

            # If coupon duration is repeating and discount_end is in the future, discount has not expired
            discount_end = sub.get("discount_end")
            if sub.get("coupon_duration") == "repeating" and discount_end:
                if discount_end.tzinfo is None:
                    discount_end = discount_end.replace(tzinfo=timezone.utc)
                if discount_end > target_end:
                    continue

            user_id = sub.get("user_id")
            if not user_id:
                continue

            user = await db["users"].find_one({"_id": ObjectId(user_id)})
            if not user or not user.get("email"):
                continue

            # Check if user has a card on Stripe
            stripe_sub_id = sub.get("stripe_subscription_id")
            customer_id = None
            if stripe_sub_id:
                stripe_sub_doc = await db["stripe_subscriptions"].find_one({"stripe_subscription_id": stripe_sub_id})
                customer_id = stripe_sub_doc.get("stripe_customer_id") if stripe_sub_doc else None

            if not customer_id:
                customer_id = user.get("stripe_customer_id")

            has_card = False
            action_url = f"{settings.FRONTEND_URL}/plan"

            if customer_id:
                try:
                    customer = stripe.Customer.retrieve(customer_id)
                    default_pm = getattr(customer.invoice_settings, 'default_payment_method', None) if hasattr(customer, 'invoice_settings') else None
                    if default_pm:
                        has_card = True
                    else:
                        pms = stripe.PaymentMethod.list(customer=customer_id, type="card", limit=1)
                        has_card = len(pms.data) > 0

                    if not has_card:
                        try:
                            portal = stripe.billing_portal.Session.create(
                                customer=customer_id,
                                return_url=f"{settings.FRONTEND_URL}/plan"
                            )
                            action_url = portal.url
                        except Exception:
                            pass
                except Exception as e:
                    print(f"Error checking customer {customer_id} on Stripe: {e}")

            # If user has no card on file, send reminder email
            if not has_card:
                end_dt = sub.get("end_date")
                days_left = 3
                if isinstance(end_dt, datetime):
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=timezone.utc)
                    days_left = max(1, (end_dt - now).days)

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
                notified_count += 1

        return {"status": "success", "notified_count": notified_count}


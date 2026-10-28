# Technical Proposal & Scope of Work (SOW)

**Document Title:** Promotion & Coupon Code Implementation for Subscription Checkout  
**Project:** Petal AI / Smile Guide AI  
**Prepared By:** Engineering Team  
**Date:** September 15, 2026  
**Status:** Under Review / For Approval  

---

## 1. Executive Summary & Business Objective

To accelerate user acquisition and facilitate promotional pilot campaigns (e.g., offering a 1-month free trial or percentage discounts), we propose implementing a **Stripe Promotion & Coupon Code Integration** within the subscription checkout flow.

---

## 2. Key Business & Operational Rules

The implementation strictly follows these operational rules:

1. **Covered Promo Code Varieties:** Only two promotional varieties are covered under this integration:
   * **1-Month Promo Code (1 Month Duration):** Applies for exactly one month. If configured with a 100% discount, credit card collection is completely bypassed during checkout, granting immediate free access for 30 days without entering card details. (On Day 31, normal billing resumes or a card is required).
   * **Forever Promo Code (Lifetime / Indefinite Duration):** Applies indefinitely across all recurring billing cycles (`duration: forever`), keeping the discount or promotional access active for the lifetime of the subscription without expiring after 1 month.
2. **Direct Management in Stripe:** All promo codes and coupons will be created and managed directly inside the **Stripe Dashboard**. Any code configured in Stripe will be instantly recognized by the application without requiring code or backend changes.
3. **Trial Bypass on Promo Code:** If a user applies a valid promo code, the standard 14-day free trial is **skipped**, allowing the promotional duration/discount to take effect immediately.
4. **Standard Flow Without Promo Code:** When subscribing without a promo code, the user goes through the default onboarding: credit card details are captured upfront, the user receives a **14-day free trial**, and the subscription fee is **automatically deducted from the credit card** once the 14 days elapse.
5. **Automated Expiration & Renewal Alerts:** For users on the card-less 100% free promo, an automated notification is sent 3 days prior to expiration with a direct link to the secure Stripe Customer Portal to add payment details. On Day 31, if no card is added, access is cleanly suspended and the user is redirected back to the subscription purchase screen (no failed charges or lingering debt).

---

## 3. Scope of Technical Changes

### Module 1: Backend API & Stripe Checkout Integration
* **Promo Code Validation Endpoint (`POST /stripe/validate-promo-code`):** Real-time API to verify promo code validity, active status, expiration, and calculate discount amounts directly against Stripe before checkout.
* **Checkout Session Enhancement (`POST /stripe/create-checkout-session`):** Update Stripe session generation to support promotion codes and dynamically enable `payment_method_collection: 'if_required'` when the total payable amount is $0.00.
* **Customer Portal Session Endpoint (`POST /stripe/customer-portal`):** Secure endpoint to generate authenticated Stripe Customer Portal sessions allowing users to add or update credit cards.

### Module 2: Webhook Architecture & Subscription Lifecycle
* **Webhook Handler Updates (`checkout.session.completed`):** Handle $0 checkouts, log promotional metadata, and bypass standard 14-day trial delays for promo users so discount periods activate immediately.
* **Upcoming Invoice Webhook (`invoice.upcoming`):** Listen for Stripe's pre-billing event (~3 days before renewal) to verify if a valid payment method exists.
* **Payment Failure & Clean Suspension (`invoice.payment_failed`):** Intercept failed renewal invoices for cardless users, cancel the unpaid Stripe subscription, and suspend database access cleanly without generating persistent dunning debt.
* **Database Schema Updates:** Extend subscription and transaction records in MongoDB to persist `coupon_id`, `coupon_code`, `percent_off`, `amount_off`, and `discount_end` tracking.

### Module 3: Automated Notifications & Scheduled Background Tasks
* **Branded Email Template:** Responsive HTML email notification informing users that their free promotional access is expiring in 3 days, with a direct call-to-action to add a payment method.
* **Scheduled Expiration Checker:** Daily background audit job to scan for expiring promotional subscriptions and ensure timely delivery of reminder emails.

### Module 4: Frontend UI / UX Enhancements
* **Promo Code Input Component:** Interactive coupon code input field with "Apply" and "Remove" triggers on the subscription checkout modal/page.
* **Live Validation & Feedback:** Display real-time UI states (validating, success badge with discount summary, or error messaging like "Invalid or expired code").
* **Dynamic Price Recalculation:** Dynamically display:
  * Original Plan Price
  * Promotional Discount Applied
  * Final Total Due Today ($0.00 for 100% promo codes)
* **Dynamic Checkout Button:** Adaptive CTA button labeling ("Start Free Month" vs. "Start 14-Day Free Trial" / "Proceed to Payment") based on whether a promo code is applied.

### Module 5: Stripe Configuration & Anti-Abuse Rules
* Direct configuration of Stripe Coupons and customer-facing promotion codes with restrictions (1 redemption per customer, first-time subscribers only).
* Configuration of Stripe Customer Portal branding and payment method management.

---

## 4. Total Estimated Effort

| Scope / Deliverables | Description |
| :--- | :--- |
| **Complete End-to-End Implementation** | • Backend API endpoints for promo validation & checkout sessions<br>• Webhook lifecycle handlers & trial-bypass logic<br>• Automated 3-day reminder email & background scheduler<br>• Frontend coupon input UI, live price calculation & dynamic checkout<br>• Stripe Dashboard configuration & anti-abuse rules<br>• Comprehensive end-to-end quality assurance (QA) & edge case testing |
| **Total Estimated Effort** | **20 Working Hours** |

---

## 5. Next Steps & Approval

Please confirm if you would like us to proceed with this scope so we can schedule the rollout and deliver the functionality according to the specifications above.

# Stripe Coupon & Promo Code Setup Guide

This guide explains how to create, configure, and manage promotion codes (e.g., `FREEMONTH100`) directly inside the **Stripe Dashboard**. Any codes created following these steps will automatically work with Petal AI without requiring any code changes.

---

## 1. Creating a 100% Free Month Coupon in Stripe

1. Log into your [Stripe Dashboard](https://dashboard.stripe.com).
2. In the left navigation, go to **Product catalog** > **Coupons** (or search for *Coupons* in the top search bar).
3. Click **+ New**:
   - **Name**: `1 Month Free Promo` (or internal reference name)
   - **ID** *(optional)*: You can leave this blank (Stripe will auto-generate) or specify `freemonth100`.
   - **Type**: Select **Percentage discount**.
   - **Percentage off**: Enter `100%`.
   - **Duration**: Select **Once** (for exactly 1 billing cycle/month) OR **Repeating** with **Number of months: 1**.
   - Click **Create coupon**.

---

## 2. Creating Customer-Facing Promotion Codes

A Coupon can have one or more customer-facing promotion codes that users actually type into the purchase screen.

1. Click on the coupon you just created (`1 Month Free Promo`).
2. Scroll down to the **Promotion codes** section and click **+ Add promotion code**:
   - **Code**: Enter the exact code you want users to type, e.g.:
     `FREEMONTH100`
   - **Eligible customers**: 
     - Check **"Limit to first-time orders only"** (recommended).
     - Check **"Limit to one redemption per customer"** (*crucial* — prevents the same user from using it repeatedly).
   - **Max redemptions** *(optional)*: e.g. `100` if you want to cap the total uses across all customers.
   - **Expiry date** *(optional)*: Set a date if this promo expires on a specific day.
3. Click **Save promotion code**.

> [!TIP]
> You can create multiple codes under the same 100% coupon (e.g. `FREEMONTH100`, `PILOT2026`, `VIPDENTIST`) or create different coupons for different campaigns. They will all work immediately.

---

## 3. Configuring the Stripe Customer Portal (For Card Updates)

The system automatically sends a reminder email 3 days before the promo expires with a secure link to add a payment method. To ensure this link works smoothly:

1. In Stripe Dashboard, go to **Settings** > **Billing** > **Customer portal** (or search *Customer portal*).
2. Under **Invoice history & Payment methods**:
   - Enable **"Allow customers to update their payment methods"**.
   - Enable **"Allow customers to manage their subscriptions"** if desired.
3. Under **Branding & Appearance**:
   - Add your company logo, brand color, and business name (*Smile Guide AI* / *Petal AI*).
4. Click **Save changes**.

---

## 4. Configuring Failed Payment Behavior in Stripe

When Month 2 begins for a user who did not add a card:
1. Go to **Settings** > **Billing** > **Subscriptions and emails** (or *Manage failed payments*).
2. Under **Customer emails**:
   - Choose whether you want Stripe to send automatic emails for failed payments or rely entirely on Petal AI's custom emails.
3. Under **Retry schedule / Failed invoice handling**:
   - Set status when all retries fail to **Cancel the subscription** or **Mark as past_due**.
   - *Note*: Our backend immediately freezes access upon receiving `invoice.payment_failed` and cleanly prompts the user to enter their card on the purchase screen.

---

## 5. Summary of the User Experience

- **Month 1**: User enters `FREEMONTH100` $\rightarrow$ Total drops to $0.00 $\rightarrow$ Card collection is skipped entirely $\rightarrow$ Full access is granted.
- **Day 27 (3 days before expiry)**: User receives an automated email reminding them to add a card to avoid interruption, with a one-click button to Stripe's secure portal.
- **Day 31 (Month 2)**: 
  - If a card was added $\rightarrow$ Subscription renews automatically.
  - If no card was added $\rightarrow$ Access is cleanly suspended, and the subscription purchase screen appears again requiring credit card entry. Plan trial periods do not repeat.

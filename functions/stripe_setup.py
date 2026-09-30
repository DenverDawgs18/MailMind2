"""
`flask stripe-setup`: make a Stripe account (test or live) ready for MailMind.

Idempotent. Creates what's missing:
  * the monthly price, found by STRIPE_PRICE_LOOKUP_KEY
  * a webhook endpoint at {DOMAIN}/webhook with the events the app handles
    (the signing secret is printed once, on creation — store it as WEBHOOK_SECRET)
  * a customer-portal configuration, so "Manage billing" works

Uses STRIPE_API_KEY from the environment, so run it with the key for the
mode you're setting up.
"""
import click
import stripe

WEBHOOK_EVENTS = [
    "checkout.session.completed",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
]


def register(app, domain, lookup_key):
    @app.cli.command("stripe-setup")
    @click.option("--amount", default=2500, show_default=True, help="Monthly price in cents.")
    def stripe_setup(amount):
        mode = "LIVE" if (stripe.api_key or "").startswith(("sk_live_", "rk_live_")) else "test"
        click.echo(f"Stripe {mode} mode")

        prices = stripe.Price.list(lookup_keys=[lookup_key], active=True).data
        if prices:
            click.echo(f"price: {prices[0].id} ({lookup_key}) exists")
        else:
            product = stripe.Product.create(name="MailMind", description="Your inbox, as one short list.")
            price = stripe.Price.create(product=product.id, unit_amount=amount, currency="usd",
                                        recurring={"interval": "month"}, lookup_key=lookup_key)
            click.echo(f"price: created {price.id} ({lookup_key}, {amount / 100:.2f} USD/month)")

        url = f"{domain}/webhook"
        hooks = [h for h in stripe.WebhookEndpoint.list(limit=100).data if h.url == url]
        if hooks:
            hook = hooks[0]
            missing = set(WEBHOOK_EVENTS) - set(hook.enabled_events)
            if missing and "*" not in hook.enabled_events:
                stripe.WebhookEndpoint.modify(hook.id, enabled_events=sorted(set(hook.enabled_events) | missing))
            click.echo(f"webhook: {hook.id} -> {url} exists (WEBHOOK_SECRET unchanged)")
        else:
            hook = stripe.WebhookEndpoint.create(url=url, enabled_events=WEBHOOK_EVENTS,
                                                 description="MailMind subscriptions")
            click.echo(f"webhook: created {hook.id} -> {url}")
            click.echo(f"WEBHOOK_SECRET={hook.secret}")

        if stripe.billing_portal.Configuration.list(limit=1).data:
            click.echo("portal: configured")
        else:
            stripe.billing_portal.Configuration.create(
                business_profile={"headline": "Manage your MailMind subscription",
                                  "privacy_policy_url": f"{domain}/termsandprivacy",
                                  "terms_of_service_url": f"{domain}/termsandprivacy"},
                features={"subscription_cancel": {"enabled": True, "mode": "at_period_end"},
                          "payment_method_update": {"enabled": True},
                          "invoice_history": {"enabled": True}},
            )
            click.echo("portal: created")

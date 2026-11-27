# Provider profiles

HookStorm can generate synthetic webhook events for four delivery formats. Profiles are selected with a top-level `provider` key or the `--provider` flag. They do not call provider APIs and do not claim to create an event accepted by a provider's test platform; they generate representative local test requests.

## Select a profile

YAML:

~~~yaml
version: 1
name: Shopify orders contract
provider: shopify
events:
  - expect_status: [200, 202, 204]
    expect_json:
      accepted: true
~~~

CLI override:

~~~bash
hookstorm run scenarios/webhook.yaml --target http://127.0.0.1:8000/webhook --provider github
~~~

Generate a starter scenario:

~~~bash
hookstorm init --provider stripe --no-workflow
~~~

All profiles default to `signature: valid` and the matching `signature_provider`. Set `HOOKSTORM_SECRET` in the environment before running. For Standard Webhooks, the secret must use the `whsec_` prefix and contain valid Base64-encoded key material. No secret is written to the scenario.

Explicit event fields (`id`, `event_id`, `payload`) override the generated defaults. Provider-managed headers cannot be overridden through the scenario's custom `headers` mapping. The generated delivery IDs are synthetic and stable for a given event ID; they are not IDs assigned by the upstream provider.

## Included examples

| Profile | Default event | Provider-specific headers |
|---|---|---|
| Stripe | `payment_intent.succeeded` | `Stripe-Signature` |
| GitHub | `issues.opened` | `X-GitHub-Event`, `X-GitHub-Delivery`, `X-Hub-Signature-256` |
| Shopify | `orders/create` | `X-Shopify-Topic`, `X-Shopify-Shop-Domain`, `X-Shopify-Webhook-Id`, `X-Shopify-Hmac-Sha256` |
| Standard Webhooks | `order.created` | `webhook-id`, `webhook-timestamp`, `webhook-signature` |

Run the examples against the local demo receiver or an isolated test endpoint:

- [Stripe profile](../examples/provider-stripe.yaml)
- [GitHub profile](../examples/provider-github.yaml)
- [Shopify profile](../examples/provider-shopify.yaml)
- [Standard Webhooks profile](../examples/provider-standard-webhooks.yaml)

The body shapes are intentionally small synthetic fixtures. Replace them with payloads representative of the event variants and API versions your receiver actually supports.

## Retry policy presets: deliberately not inferred

A provider's retry policy is not the same thing as HookStorm's retry engine. The current engine uses a bounded attempt count and exponential `backoff_ms`; it does not reproduce provider-side delivery schedulers.

The official documentation available on **November 27, 2026** does not expose a common, fully specified schedule across these providers:

- **Stripe:** live-mode deliveries retry for up to three days with exponential backoff; sandbox events are retried three times over a few hours. Stripe's docs do not publish exact individual intervals.
- **GitHub:** failed deliveries are not automatically redelivered. A user or script can manually redeliver eligible deliveries from the previous three days.
- **Shopify:** failed deliveries are retried up to eight times over four hours using exponential backoff. The cited documentation does not publish each exact interval.
- **Standard Webhooks:** the specification recommends a multi-day exponential-backoff schedule with jitter and provides an example schedule. That example's final retry is more than 24 hours after the initial attempt, beyond HookStorm's current maximum `run_timeout_ms` of 24 hours.

For these reasons HookStorm does **not** silently translate provider policies into guessed `max_attempts` or `backoff_ms` values. Provider retry presets remain future work until the engine can represent explicit per-attempt schedules and a safe run-time budget for multi-day tests.

## Sources checked November 27, 2026

- [Stripe — Webhook event delivery behaviors and automatic retries](https://docs.stripe.com/webhooks#automatic-retries)
- [GitHub — Redelivering webhooks](https://docs.github.com/en/webhooks/testing-and-troubleshooting-webhooks/redelivering-webhooks)
- [Shopify — Troubleshoot webhooks](https://shopify.dev/docs/apps/build/webhooks/troubleshoot)
- [Standard Webhooks — Specification and example retry schedule](https://github.com/standard-webhooks/standard-webhooks/blob/main/spec/standard-webhooks.md)

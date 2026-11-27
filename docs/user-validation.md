# User validation plan

**Status:** market demand is unverified. This is an experiment plan, not a claim that HookStorm has product-market fit.

## Product hypothesis

Backend engineers who own webhook consumers need a repeatable way to test the failure modes that ordinary happy-path integration tests miss: retries, duplicate events, and an acknowledgement that disappears after the business operation has already committed.

HookStorm's narrow promise:

> Test whether your webhook handler stays correct when delivery is unreliable — before you trust it in production.

HookStorm is a local-first test runner. It is not a hosted webhook inbox, production gateway, delivery provider, or monitoring service.

## First audience to test

Start with Python backend engineers and small SaaS teams that:

- receive webhooks for payments, orders, subscriptions, or account events;
- own the receiver implementation and its idempotency behavior;
- already use pytest or CI, but do not have a repeatable webhook failure test;
- can run an isolated local or staging endpoint with synthetic data.

Do not target every developer or every webhook use case at once. The first question is whether this specific group has the problem often enough to run a dedicated tool.

## The first demo: committed side effect, lost acknowledgement

This demo shows the most important invariant: a retry must not apply the same business effect twice.

### Run it locally

Python 3.11+ is required. In a terminal:

~~~bash
git clone https://github.com/blvxx/HookStorm.git
cd HookStorm
python -m pip install .
python examples/receiver.py
~~~

The example receiver binds to `127.0.0.1:8000` and uses an in-memory SQLite database. In a second terminal, from the repository directory:

~~~bash
hookstorm validate examples/commit-then-drop.yaml
hookstorm run examples/commit-then-drop.yaml \
  --target http://127.0.0.1:8000/webhook/commit-then-drop
~~~

Open `hookstorm-report.html` in the repository directory.

### What should happen

1. The receiver writes the event to SQLite and commits the transaction.
2. It closes the connection without returning an HTTP response.
3. HookStorm observes a transport failure and retries the same event ID.
4. The receiver recognizes the duplicate and does not apply the business effect again.
5. HookStorm calls the read-only state endpoint and checks that exactly one event was stored.

A successful run should show two delivery attempts and a verified final state with `processed_events: 1`. This is a demonstration fixture, not a substitute for testing your own transaction boundaries.

## Test it against a real application

Only use an endpoint you own or are explicitly authorized to test. Use a local environment or isolated staging tenant with synthetic records. Never point the demo at production, and do not use real customer payloads or production credentials.

A useful first application test is deliberately small:

1. Pick one webhook event with a stable provider event ID.
2. Configure your test receiver so duplicate delivery is safe and observable.
3. Create a scenario with one expected event and a read-only verification probe for the resulting test state.
4. Run it against your isolated endpoint.
5. Check whether the report catches a behavior your existing tests do not cover.

If your endpoint is remote, review the security guidance and explicitly opt in to the target with the appropriate CLI flags. Do not weaken target safeguards to make a test pass.

## What to learn from each tester

Ask the tester to run the demo or, preferably, adapt one scenario to their own non-production endpoint. Record observable behavior rather than compliments.

- Role and context: solo developer, platform team, product team; what webhook types do they own?
- Current workflow: how do they test retries, duplicates, and idempotency today?
- First-run result: did installation and the demo complete; how long did it take?
- Application result: did HookStorm expose a real gap or confirm an existing guarantee?
- Friction: what stopped them, confused them, or required manual work?
- Repeat intent: would they put the scenario in CI or run it again before a release? What exact change would make that happen?

Do not lead with “Would you use this?” or “Would you pay for this?” A completed run against a real test endpoint is a much stronger signal than a positive opinion.

## Two-week validation experiment

This is a lightweight decision rule for the first round, not an industry benchmark.

| Step | Target | Evidence |
|---|---:|---|
| Relevant, personalized invitations | 15 | Messages sent to Python/backend engineers who work with inbound webhooks |
| Substantive conversations | 5 | A specific webhook-testing problem and current workaround are identified |
| Independent hands-on runs | 3 | People outside the project install and run HookStorm |
| Application-level validation | 1 or more | At least one tester adapts a scenario to their own isolated endpoint |
| Strong continuation signal | 1 or more | A tester adds it to CI, repeats a run, or requests a concrete change needed for adoption |

### Decision after the experiment

- **Continue product development** if several independent teams complete application-level tests and at least one demonstrates repeat use or CI adoption.
- **Narrow or reposition** if users value only one workflow (for example, idempotency verification) and ignore the broader fault matrix.
- **Stop feature expansion and keep it as an open-source portfolio project** if targeted conversations do not lead to hands-on runs. That is useful evidence; it is not a reason to keep adding features indefinitely.

Do not build a hosted service, dashboard, paid infrastructure, or provider-specific retry presets before this test produces a repeat-use signal.

## Outreach messages

### Russian

> Привет! Я разрабатываю HookStorm — локальный CLI для проверки webhook-обработчиков на повторы, сбои доставки и потерянные подтверждения. Ищу несколько Python/backend-разработчиков, которые принимают webhooks и готовы прогнать небольшой тест на локальном или staging-окружении. Самый показательный сценарий проверяет ситуацию, когда обработчик уже записал заказ, но HTTP-ответ потерялся: повторная доставка не должна создать второй заказ. Мне важен не отзыв “прикольно”, а понять, получилось ли запустить тест на реальном обработчике и полезен ли результат. Репозиторий: https://github.com/blvxx/HookStorm. Будет интересно попробовать на одном тестовом событии?

### English

> Hi! I'm building HookStorm, a local CLI for testing webhook consumers under retries, duplicate deliveries, and lost acknowledgements. I'm looking for a few Python/backend engineers who own webhook endpoints and can try a small test in a local or isolated staging environment. The key demo covers a handler that commits an order but loses the HTTP acknowledgement: the retry must not create a second order. I'm looking for hands-on feedback, not just a reaction to the idea. Repo: https://github.com/blvxx/HookStorm. Would you be open to trying it with one synthetic event?

## Feedback record

For each run, keep a short record:

~~~text
Tester / team:
Language and framework:
Webhook provider or event type:
Current testing approach:
Installed successfully: yes / no
Ran the local demo: yes / no
Ran against an isolated application endpoint: yes / no
Found a gap in existing tests: yes / no — details:
Would add to CI or repeat: yes / no — why:
Main blocker or missing capability:
Next action agreed:
~~~

The experiment is complete only when the evidence is recorded. Stars, downloads, and “looks cool” comments are not substitutes for independent use.

# JSON Schema for scenario version 1

HookStorm ships a JSON Schema 2020-12 document for editor completion and structural validation of version 1 YAML scenarios.

## Export the schema

Print the schema to stdout:

~~~bash
hookstorm schema
~~~

Save a local copy without overwriting an existing file:

~~~bash
hookstorm schema --output .hookstorm/scenario-v1.schema.json
~~~

Use --force only when intentionally replacing a local copy. The packaged schema is also available at src/hookstorm/schemas/scenario-v1.schema.json in the source repository.

## Configure editor support

In VS Code, add a schema association to your workspace settings:

~~~json
{
  "yaml.schemas": {
    "./.hookstorm/scenario-v1.schema.json": [
      "scenarios/*.yaml",
      "examples/**/*.yaml"
    ]
  }
}
~~~

For a scenario file, the editor can now offer field completion, enum suggestions, and structural warnings for delivery settings, retry rules, provider profiles, verification probes, and capture metadata.

## What the schema checks

The document describes the supported root keys, event structure, header mappings, retry and delivery limits, supported provider names, signature modes, fault-injection fields, verification authentication, and capture metadata.

Provider profiles allow event type and event ID defaults to be omitted from source YAML. Without a provider profile, each event must specify both fields.

## What still requires HookStorm

JSON Schema cannot conveniently enforce every runtime rule. Always run:

~~~bash
hookstorm validate scenarios/webhook.yaml
~~~

HookStorm performs semantic checks beyond the schema, including:

- unique event IDs and provider-managed event constraints;
- retry-attempt bounds for each injected fault;
- conflicting fault types on the same attempt;
- header reserved-name checks and aggregate header-size limits;
- serialized payload byte limits;
- capture metadata correspondence with scenario events;
- estimated request count and worst-case runtime budget.

The schema is an editor aid, not a replacement for HookStorm validation or endpoint safety controls.

# Making Assist obey a role

**Status: proposal. None of this is implemented.** Today the intent surface is
refused outright for every restricted role, which is what `BASELINE_DENY` in
`policy.py` is for. This describes what it would take to replace that refusal
with enforcement, and what it would cost.

Read [DESIGN.md](DESIGN.md) first; this assumes the gates it describes.

## The problem

Assist takes free text. "Turn off the lights" names no entity, so the resource
gate has nothing to check, and the boundedness gate correctly calls it
unbounded. Filtering the response is no use either: the conversation result does
say which entities it hit, but by then it has hit them. That is the rule from
DESIGN.md -- reads are filtered on the way out, mutations are gated on the way
in -- and a sentence is a mutation whose target is unknown until Home Assistant
resolves it.

So the layer refuses the whole surface. A role that may control its own bedroom
lights still cannot ask Assist to turn them on. That is safe and blunt.

The reason this looked unfixable from a proxy is that resolution and execution
appeared to be one step. They are not.

## The seam

Home Assistant's default agent separates recognising a sentence from acting on
it:

| What | Where |
| --- | --- |
| `DefaultAgent.async_recognize_intent(user_input)` | `conversation/default_agent.py` |
| `intent.async_match_targets(hass, constraints)` | `helpers/intent.py` |
| `intent.async_handle(hass, DOMAIN, name, slots, ...)` | `helpers/intent.py` |

`async_recognize_intent` returns a `RecognizeResult` carrying the intent name
and the slots it parsed, and does not touch a single entity. The handler behind
`async_handle` then turns those slots into a `MatchTargetsConstraints` -- `name`,
`area_name`, `floor_name`, `domains`, `device_classes` -- and asks
`async_match_targets` which entities they select.

Both halves are callable on their own. So the sentence can be resolved to a set
of entities, checked against the role, and only then executed.

The other half of what makes this possible: `ConversationInput.context` is a
Home Assistant `Context`, and it carries `user_id`. The identity survives into
the agent, which is usually the hard part.

## The flow

```
text arrives at our agent
  |
  +- user_id = user_input.context.user_id
  |    no user  -> refuse. An intent with nobody behind it is not judgeable
  |
  +- result = default_agent.async_recognize_intent(user_input)
  |    None    -> delegate unchanged. Nothing was recognised, so nothing will act
  |
  +- constraints = constraints_from(result.entities_list)
  +- match = intent.async_match_targets(hass, constraints)
  |    no match -> delegate unchanged. It will fail the same way for everyone
  |
  +- every matched entity permitted for POLICY_CONTROL?
       no  -> refuse, naming the entity only if the role may see it
       yes -> execute
```

The check reuses `permissions.check_entity`, so a role's entity rules, its
attribute rules and its dashboard grants all apply without being restated.

Refusals should be spoken, not thrown: an `IntentResponse` with
`IntentResponseType.ERROR` and the same wording the proxy uses, so a person hears
"You do not have permission to control the front door" rather than a traceback.

Refusals must also reach the deny log. A denial that only happens out loud is
invisible to whoever maintains the roles, and the Denials tab is the one place
they look.

## Executing, and the double-recognition trap

The obvious final step is to delegate to the default agent once the check
passes. Do not. Delegating re-runs recognition, and the second pass can differ
from the first: the state machine moves, an entity is renamed, a timer expires.
Approving one sentence and executing another is exactly the bug this is meant to
prevent, and it would be unreproducible.

Execute from the result already recognised:

```python
await intent.async_handle(
    hass,
    conversation.DOMAIN,
    result.intent.name,
    slots,
    user_input.text,
    user_input.context,
    language,
    assistant=conversation.DOMAIN,
    device_id=...,
    satellite_id=...,
    conversation_agent_id=user_input.agent_id,
)
```

This is a real cost. `_async_process_intent_result` does more than call
`async_handle`: trigger sentences, the "that entity is not exposed" messages,
error phrasing per `MatchFailedReason`, conversation tracing. Reimplementing it
means tracking it. The alternative is to accept the double-recognition window
and document it, which for a security layer is the worse trade.

## Fail closed by default

Registering an agent does not intercept anything. `async_set_agent` makes an
agent *available*, and a pipeline has to be pointed at it. So enforcement would
depend on the operator selecting the right agent, and forgetting means no
enforcement with nothing to notice. That inverts this project's rule that you
cannot accidentally go round it.

The interlock: keep the refusal in `BASELINE_DENY` and relax it only for
requests naming our agent. The proxy already sees `agent_id` in the
`conversation/process` payload, so it can allow the ones routed through the
enforcing agent and refuse everything else, exactly as it does today. A pipeline
pointed anywhere else keeps the current behaviour, which is safe.

That also gives the feature an off switch that is a configuration rather than a
code path.

## What this does not cover

**LLM agents.** If Assist is backed by OpenAI, Gemini or a local model, none of
the above happens. There is no sentence recognition; the model is handed tools
built by `llm.API` and calls them. Enforcing there means registering a filtered
API through `llm.async_register_api` and building each user's tool set from the
entities their role allows -- a comparable amount of work, a different mechanism,
and it has to be right about a model that can be argued with. Until then an
LLM-backed Assist stays all-or-nothing, and should be refused rather than
half-covered.

**Custom sentences and trigger sentences.** A user-defined trigger runs a
script. Scripts already execute with no user context, which DESIGN.md lists as a
standing limitation, so a role that may reach Assist may reach whatever its
triggers do.

**`assist_satellite` and voice.** A satellite speaking on behalf of a person is
identified by device, not by user. Whether a device implies a person is a policy
question this layer has no answer to, so satellites should stay refused.

## Non-public API, and what happens when it moves

`async_recognize_intent` is a method on a component's internal class.
`async_get_agent` lives in `agent_manager.py` and is not in the conversation
component's `__all__`. Neither is public API and both can move.

Follow `http_config.py`: probe for what is needed at setup, and if it is not
there, degrade to the current behaviour -- refuse the intent surface -- rather
than raising or, far worse, allowing. The failure direction matters more than
the failure.

That means the feature can silently stop working after a Home Assistant upgrade.
It should say so in the log and on the panel when it does, because "Assist
stopped answering" is a support question and "Assist quietly stopped checking"
would be a vulnerability.

## Tests worth writing first

- A role denied one entity, asked in words, is refused, and the entity does not
  move. This is the test the current bypass would have failed.
- A role allowed an entity, asked in words, succeeds.
- A sentence matching several entities where one is denied refuses the whole
  thing rather than acting on the rest.
- A sentence that matches nothing behaves the same for a restricted role as for
  the owner, so the refusal is not an entity-existence oracle.
- Recognition returning `None` delegates unchanged.
- A request naming an agent that is not ours is still refused by the proxy.
- The probe for the non-public API failing leaves the surface refused.

## Effort

The default-agent path is a self-contained module: agent registration, slot to
constraint mapping, the permission check, response building, and the
`agent_id` interlock in `decide.py`. The reimplementation of
`_async_process_intent_result` is the bulk of it and the part that will need
revisiting on Home Assistant upgrades.

The LLM path is a separate piece of work of similar size and should not be
started until the first is in a real house.

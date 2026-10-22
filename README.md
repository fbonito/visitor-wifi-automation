# Automated visitor Wi-Fi: sign-in → guest network account

Give office visitors Wi-Fi access the moment they sign in, with no front-desk
or IT involvement and no shared guest password.

This repo describes a simple, general pattern and includes one working
reference implementation:

> **Envoy** (visitor management) → **Tines** (automation) → **Arista AGNI** (guest network access)

It runs in production across multiple offices on different continents from a
single workflow. The pattern isn't tied to those three products. If you use
different tools, the design and the decision logic should carry over. See
[Adapting to other platforms](#adapting-to-other-platforms).

## The pattern

Any setup like this has three parts:

```
┌──────────────────┐   sign-in    ┌──────────────────────┐   create    ┌──────────────────┐
│ Visitor system   │   event      │ Automation + rules   │   guest     │ Guest network    │
│ (check-in kiosk) │ ───────────► │ (decide, build)      │ ──────────► │ access system    │
└──────────────────┘   (webhook)  └──────────────────────┘   account   └────────┬─────────┘
                                                                                 │ emails
                                                                                 ▼ credentials
                                                                             the visitor
```

1. **Event source:** a visitor-management system sends an event when someone
   checks in, ideally a webhook, so there is no polling and no race.
2. **Decision and build step:** a small piece of logic decides *whether* this
   visitor gets Wi-Fi and builds the account details (credentials, expiry).
3. **Account creation:** an API call to the network access system creates a
   time-limited guest account and notifies the visitor.

Most of the value, and most of the code in this repo, is step 2: the rules that
are the same whichever products sit on either side.

## The rules (the reusable part)

For each sign-in event, [`envoy_agni_guest.py`](envoy_agni_guest.py):

1. **Validates** that the visitor has a name and an email.
2. **Skips** visitors whose visitor type is on a block list, for example
   `Employee`, who already have network access.
3. **Applies the Wi-Fi opt-in rule:**
   - Offices on an auto-provision list give every non-blocked visitor an
     account and never ask the question.
   - All other offices require the visitor to answer **Yes** to a form
     question such as *"Do you want Wi-Fi access?"*. The match is lenient: any
     answer *starting with* "Yes" counts, so you can put reminder text in the
     answer label (useful when the visitor system has no standalone notes
     between questions).
4. **Computes expiry:**
   - Normal visitors: 23:59 **office-local time** on the day of sign-in.
   - Multi-day visitors: 23:59 on the date they enter in a "last day of your
     visit" field.
   - The office timezone is taken from the event, so one workflow serves every
     office. If it is missing or invalid, a default is used.
5. **Generates** a random 6-digit password (using Python's `secrets`).
6. **Returns** a ready-to-send account payload, or a skip reason you can log.

Every rule is a few lines you can change or delete. Different opt-in wording,
a different expiry policy or a longer password is a small edit.

## Reference implementation: Envoy + Tines + AGNI

### Flow

```
 Envoy entry_sign_in webhook
          │
          ▼
 Tines Story
   1. Webhook trigger
   2. Run Script  ◄── envoy_agni_guest.py (this repo)
   3. Trigger: stop if skip = true
   4. HTTP Request: AGNI keyLogin
   5. HTTP Request: AGNI guest user add
          │
          ▼
 AGNI creates the guest account and emails the credentials
```

The script is deliberately **logic only**. Tines' Python sandbox can't make
outbound HTTP requests, so the two AGNI API calls are native Tines HTTP
Request actions and the script only decides what to send. The same split works
anywhere your automation platform separates code from API calls.

### Script input and output

The script expects a flat object (see
[`examples/tines_payload.json`](examples/tines_payload.json)):

`full_name`, `email`, `flow_name`, `signed_in_at`, `user_data`, `entry_id`,
`office_timezone`, `location_id`

It returns:

```json
{
  "skip": false,
  "full_name": "Jane Visitor",
  "email": "jane.visitor@example.com",
  "valid_to": "2026-04-30T06:59:00Z",
  "password": "523275",
  "agni_payload": {
    "orgID": "YOUR-AGNI-ORG-ID",
    "portalID": 0,
    "loginName": "jane.visitor@example.com",
    "name": "Jane Visitor",
    "email": "jane.visitor@example.com",
    "password": "523275",
    "deviceLimit": 1,
    "userType": "guest",
    "validTo": "2026-04-30T06:59:00Z",
    "sendEmail": true
  }
}
```

When skipping, it returns `{"skip": true, "reason": "...", ...}`.

### Setup

**Envoy**
- Create a webhook for the `entry_sign_in` event pointing at your Tines
  Story's webhook URL.
- Add a form question **Do you want Wi-Fi access?** with answers starting
  "Yes" / "No". Skip this for offices you want to auto-provision.
- For multi-day visitors, add a date question **Last day of your visit**
  (`YYYY-MM-DD`) and a flow named **Multiday visitor**.
- Question labels and flow names must match the script's constants character
  for character. Change either side to suit you.

**Tines Story**
1. **Webhook** action receiving the Envoy payload.
2. **Run Script** (Python) action containing `envoy_agni_guest.py`, with its
   Payload set to `examples/tines_payload.json`. The payload is flattened on
   purpose: Tines stringifies nested objects substituted inside quoted JSON,
   and bracket notation (`['full-name']`) stops Tines reading hyphens as
   subtraction.
3. **Trigger** action that stops the Story when `skip` is `true`.
4. **HTTP Request** to AGNI's `keyLogin` API, using an API key held in a Tines
   Credential or Resource.
5. **HTTP Request** to AGNI's guest-user-add API (`identity.guest.user.add`)
   with `<<decide_and_build.output.agni_payload>>` as the body.

Check your AGNI tenant's API documentation for exact endpoint URLs,
authentication headers and API-key setup. They depend on your tenant and
region.

**Script configuration** (constants at the top of `envoy_agni_guest.py`)

| Constant | Meaning |
|----------|---------|
| `AGNI_ORG_ID`, `AGNI_PORTAL_ID` | IDs from your AGNI tenant (not secrets) |
| `DEFAULT_OFFICE_TIMEZONE` | Fallback IANA timezone |
| `NO_WIFI_VISITOR_TYPES` | Visitor types that never get Wi-Fi |
| `AUTO_WIFI_LOCATION_IDS` | Location IDs that skip the opt-in question |
| `MULTIDAY_VISITOR_TYPE` | Visitor type name for multi-day visitors |
| `ENVOY_FIELD_WIFI_OPT_IN`, `ENVOY_FIELD_END_DATE` | Form question labels |

Find an Envoy location ID in the webhook at `meta.location.id`.

## Adapting to other platforms

> The mappings below are a guide to where each piece goes, not tested
> integrations. Only the Envoy + Tines + AGNI combination has been run in
> production.

| Piece | In this repo | What to look for in an alternative |
|-------|--------------|------------------------------------|
| **Event source** | Envoy `entry_sign_in` webhook | A visitor-management system that can push a check-in event (webhook) with the visitor's name, email, visitor type, custom form answers, timestamp and site. Examples: SwipedOn, Sign In App, Proxyclick/Cvent, Traction Guest, or a custom kiosk. |
| **Automation host** | Tines Story | Anything that can receive a webhook, run a bit of code and make HTTP calls. Examples: n8n, Zapier, Power Automate, Workato, or a small serverless function (AWS Lambda, Cloud Functions, Azure Functions). |
| **Guest network system** | Arista AGNI | A system with an API for creating time-limited guest accounts or vouchers. Examples: Aruba ClearPass or Central, Cisco ISE or Meraki, Ruckus, Fortinet, UniFi hotspot, Cloudi-Fi, Purple. |

To port it:

1. **Map the input.** Your visitor system's event will use different field
   names. Rewrite `_attributes_from_flat_input` (and the constants) so the
   rules see the same normalized values: name, email, visitor type, answers,
   sign-in time, timezone and location.
2. **Keep the rules.** `should_skip`, `calculate_expiry` and
   `generate_password` don't depend on any product. Reuse them as they are, or
   change the policy.
3. **Map the output.** Replace the `agni_payload` dict with whatever your
   guest network system's API expects. The fields you need are almost always
   the same: name, email, credential, expiry, device limit.
4. **Run it where you can.** If your platform allows outbound HTTP from code,
   you can call the guest network API directly in the same script instead of
   splitting logic and HTTP like the Tines version does.

## Testing locally

The script is plain Python 3.9+ with no third-party dependencies:

```python
import json
import envoy_agni_guest as m

print(json.dumps(m.main(json.load(open("examples/sample_input.json"))), indent=2))
```

## Security notes

- **No secrets live in the script.** Network-system credentials belong in your
  automation platform's secret store and are injected into the API call.
- The guest password is emailed to the visitor (`sendEmail: true`) and is also
  part of the workflow's output, so restrict who can view run history and
  consider a short retention period.
- Passwords are 6 digits, short-lived (end of day or end of visit) and limited
  to one device (`deviceLimit: 1`). Adjust to your risk appetite.

## Repository contents

| File | Purpose |
|------|---------|
| `envoy_agni_guest.py` | The decision/build logic (Tines Run Script). Entry point is `main(input)`. |
| `examples/tines_payload.json` | The Payload to configure on the Tines Run Script action. |
| `examples/sample_input.json` | A sample input for local testing. |

## Disclaimer

This is an independent project and is not affiliated with or endorsed by any
product mentioned here, including Envoy, Tines and Arista. All product names
are trademarks of their respective owners. Provided as-is under the MIT
license.

## License

[MIT](LICENSE)

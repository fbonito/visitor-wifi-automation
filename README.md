# Envoy → Arista AGNI guest Wi-Fi automation

Automatically give office visitors Wi-Fi access the moment they sign in.

When a visitor checks in with [Envoy](https://envoy.com) (Visitors), this
automation creates a time-limited guest account in
[Arista AGNI](https://www.arista.com/en/products/agni) and AGNI emails the
credentials to the visitor. No front-desk or IT involvement, no shared guest
password.

It runs in production across multiple offices on different continents from a
single workflow.

## How it works

```
 Visitor signs in          Envoy sends            Tines Story
 on the Envoy tablet  ──►  entry_sign_in   ──►  ┌──────────────────────────────┐
                           webhook              │ 1. Webhook trigger           │
                                                │ 2. Run Script (this repo)    │
                                                │      decide + build payload  │
                                                │ 3. Trigger: stop if skip     │
                                                │ 4. HTTP: AGNI keyLogin       │
                                                │ 5. HTTP: AGNI guest user add │
                                                └──────────────┬───────────────┘
                                                               ▼
                                              AGNI creates the guest account and
                                              emails the credentials to the visitor
```

The Python in this repo is step 2. It is deliberately **logic only**: Tines'
Python sandbox can't make outbound HTTP requests, so the two AGNI API calls
are native Tines HTTP Request actions, and the script just decides what to
send.

For each Envoy sign-in the script:

1. **Validates** that the visitor has a name and an email.
2. **Skips** visitors whose Envoy flow (visitor type) is on a block list, for
   example `Employee`, who already have network access.
3. **Applies the Wi-Fi opt-in rule:**
   - Offices listed in `AUTO_WIFI_LOCATION_IDS` give every non-blocked visitor
     an account and never ask the question.
   - All other offices require the visitor to answer **Yes** to the Envoy form
     question *"Do you want Wi-Fi access?"*. The match is lenient: any answer
     *starting with* "Yes" counts, so you can put reminder text in the label
     (Envoy has no standalone notes between questions).
4. **Computes expiry:**
   - Normal visitors: 23:59 **office-local time** on the day of sign-in.
   - Multi-day visitors: 23:59 on the date they enter in the *"Last day of
     your visit"* field.
   - The office timezone comes from the webhook (`location-timezone`), so one
     Story serves every office. If it is missing or invalid, it falls back to
     `DEFAULT_OFFICE_TIMEZONE`.
5. **Generates** a random 6-digit password (using `secrets`).
6. **Returns** a ready-to-send AGNI payload.

Returned object:

```json
{
  "skip": false,
  "full_name": "Jane Visitor",
  "email": "jane.visitor@example.com",
  "valid_to": "2026-04-29T21:59:00Z",
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
    "validTo": "2026-04-29T21:59:00Z",
    "sendEmail": true
  }
}
```

When skipping, it returns `{"skip": true, "reason": "...", ...}` so you can log
why a visitor didn't get an account.

## Repository contents

| File | Purpose |
|------|---------|
| `envoy_agni_guest.py` | The Tines Run Script (Python). Entry point is `main(input)`. |
| `examples/tines_payload.json` | The exact Payload to configure on the Run Script action. |
| `examples/sample_input.json` | A sample input for testing the script locally. |

## Setup

### 1. Envoy

- Create a webhook for the `entry_sign_in` event that points at your Tines
  Story's webhook URL.
- Add a custom form question on your visitor flows: **Do you want Wi-Fi
  access?** (answer options starting with "Yes" / "No"). Skip this for offices
  you want to auto-provision.
- For multi-day visitors, add a date question **Last day of your visit**
  (format `YYYY-MM-DD`) and a flow named **Multiday visitor**.
- Question labels and flow names must match the constants in the script
  character for character. Change either side to suit you.

### 2. Tines Story

1. **Webhook** action receiving the Envoy payload.
2. **Run Script** (Python) action with the contents of `envoy_agni_guest.py`.
   Set its Payload to `examples/tines_payload.json`. The payload is flattened
   on purpose: Tines stringifies nested objects substituted inside quoted JSON,
   and bracket notation (`['full-name']`) stops Tines reading hyphens as
   subtraction.
3. **Trigger** action that stops the Story when the script's `skip` output is
   `true`.
4. **HTTP Request** to AGNI's `keyLogin` API to get a session/token, using
   an AGNI API key stored in a Tines **Credential** or **Resource**.
5. **HTTP Request** to AGNI's guest-user-add API (`identity.guest.user.add`)
   with `<<decide_and_build.output.agni_payload>>` as the body.

Refer to your AGNI tenant's API documentation for the exact endpoint URLs,
authentication headers and API-key setup. They depend on your tenant and
region.

### 3. Configure the script

Edit the constants at the top of `envoy_agni_guest.py`:

| Constant | Meaning |
|----------|---------|
| `AGNI_ORG_ID`, `AGNI_PORTAL_ID` | IDs from your AGNI tenant (not secrets) |
| `DEFAULT_OFFICE_TIMEZONE` | Fallback IANA timezone |
| `NO_WIFI_VISITOR_TYPES` | Envoy flow names that never get Wi-Fi |
| `AUTO_WIFI_LOCATION_IDS` | Envoy location IDs that skip the opt-in question |
| `MULTIDAY_VISITOR_TYPE` | Envoy flow name for multi-day visitors |
| `ENVOY_FIELD_WIFI_OPT_IN`, `ENVOY_FIELD_END_DATE` | Form question labels |

Find an Envoy location ID in the webhook at `meta.location.id`.

## Testing locally

The script is plain Python 3.9+ with no third-party dependencies:

```python
import json
import envoy_agni_guest as m

print(json.dumps(m.main(json.load(open("examples/sample_input.json"))), indent=2))
```

## Security notes

- **No secrets live in the script.** AGNI credentials belong in Tines
  Credentials/Resources and are injected into the HTTP actions.
- The guest password is emailed to the visitor by AGNI (`sendEmail: true`) and
  is also part of the Story's action output, so restrict who can view Story
  events and consider a short event-retention period.
- Passwords are 6 digits, short-lived (end of day or end of visit) and limited
  to one device (`deviceLimit: 1`). Adjust to your risk appetite.

## Disclaimer

This is an independent project and is not affiliated with or endorsed by
Envoy, Tines or Arista. All product names are trademarks of their respective
owners. Provided as-is under the MIT license.

## License

[MIT](LICENSE)

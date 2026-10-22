"""
Envoy -> Arista AGNI Guest Account Creator v3 (Tines Edition)
==============================================================
PURPOSE:
    Runs inside a Tines "Event Transform" action (Python mode).
    Reads the Envoy webhook payload, decides whether the visitor should
    receive a guest Wi-Fi account, and (if yes) builds the AGNI payload
    + expiry + random password for the downstream HTTP Request actions.

DESIGN NOTES:
    - Tines's Python sandbox cannot make outbound HTTP, so the two AGNI
      calls (keyLogin, identity.guest.user.add) are native Tines
      HTTP Request actions in the surrounding Story.
    - This module is logic-only. Tines calls main(input) and uses the
      returned dict as the action's output, consumed downstream via
      <<decide_and_build.agni_payload>> etc.
    - The Envoy webhook payload is consumed directly — no polling, no race.

TINES STORY SHAPE:
    1. Webhook action  (entry_sign_in from Envoy)
    2. THIS Run Script (Python)  → returns {skip, agni_payload, ...}
    3. Trigger action  → stop if <<decide_and_build.skip>> == true
    4. HTTP Request    → AGNI keyLogin
    5. HTTP Request    → AGNI identity.guest.user.add (body = <<decide_and_build.agni_payload>>)

INPUT:
    Tines's Run Script Payload is configured as a flat per-field object,
    because Tines stringifies nested objects when substituted inside a
    quoted JSON value. Expected keys on `input`:
      full_name, email, flow_name, signed_in_at, user_data, entry_id,
      office_timezone (IANA, e.g. "Europe/Berlin" — falls back to default
      if missing/invalid; sourced from
      webhook_trigger.body.payload.attributes['location-timezone']),
      location_id (Envoy site ID, used to decide whether the visitor needs
      to answer the Wi-Fi opt-in question — see AUTO_WIFI_LOCATION_IDS).

RETURNS (from main):
    {
      "skip":         bool,
      "reason":       str,    # populated when skip=True
      "full_name":    str,
      "email":        str,
      "valid_to":     str,    # ISO 8601 UTC, e.g. "2026-04-27T21:59:00Z"
      "password":     str,    # 6 digits
      "agni_payload": {       # ready to drop into the AGNI HTTP body
        "orgID", "portalID", "loginName", "name", "email",
        "password", "deviceLimit", "userType", "validTo", "sendEmail"
      }
    }
"""

import json
import secrets
import string
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


# ---------------------------------------------------------------------------
# Configuration
# Secrets (AGNI key ID + value) are NOT here — Tines holds them in
# CREDENTIAL/RESOURCE and injects them into the HTTP Request actions.
# These are non-secret IDs needed to build the AGNI payload. Replace the
# placeholders with the values from your own AGNI tenant.
# ---------------------------------------------------------------------------
AGNI_ORG_ID    = "YOUR-AGNI-ORG-ID"
AGNI_PORTAL_ID = 0  # your numeric AGNI guest portal ID

# Per-event office timezone is resolved from the Envoy webhook payload
# (see _resolve_timezone). This default is only used when the webhook
# omits the field or supplies an invalid IANA name.
DEFAULT_OFFICE_TIMEZONE = "UTC"

ENVOY_FIELD_VISITOR_TYPE = "flow-name"

NO_WIFI_VISITOR_TYPES = [
    "Employee",  # employees already have their own network access
]

# Envoy location IDs for offices that auto-provision Wi-Fi for every visitor
# (no opt-in question on the form). For these sites the script skips the
# Wi-Fi opt-in check entirely; visitor-type filtering still applies.
# Sites NOT listed here use the standard opt-in flow (Wi-Fi question must
# be answered Yes — see ENVOY_FIELD_WIFI_OPT_IN below).
#
# Find a location ID in the webhook at meta.location.id, or in Envoy under
# Settings → Locations → click the site → look at the URL.
AUTO_WIFI_LOCATION_IDS = set([
    # "123456",  # Example Office A
    # "234567",  # Example Office B
    # add auto-provision sites here as you onboard them
])

# Matches the Envoy flow name exactly (case-sensitive). Set this to the
# name of your multi-day visitor flow in Envoy.
MULTIDAY_VISITOR_TYPE = "Multiday visitor"

# Live Envoy form question label (must match character-for-character,
# including punctuation and capitalization).
ENVOY_FIELD_WIFI_OPT_IN = "Do you want Wi-Fi access?"

# We match the Wi-Fi answer leniently — the Envoy option label may include
# extra reminder text (e.g. "Yes — your password will be emailed to you")
# because Envoy doesn't support standalone notes between questions, so the
# warning has to live inside the answer label itself. Anything that starts
# with this prefix (case-insensitive, after trimming) counts as opt-in.
ENVOY_WIFI_OPT_IN_YES_PREFIX = "Yes"

ENVOY_FIELD_END_DATE    = "Last day of your visit"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_user_data_value(attributes, field_label):
    """Read a custom-field answer from the Envoy `user-data` array."""
    for item in attributes.get("user-data", []) or []:
        if item.get("field") == field_label:
            return item.get("value") or ""
    return ""


def end_of_day(dt, tz):
    """23:59 in `tz` on the same calendar day as `dt`, returned as UTC."""
    local_dt = dt.astimezone(tz)
    eod_local = local_dt.replace(hour=23, minute=59, second=0, microsecond=0)
    return eod_local.astimezone(timezone.utc)


def parse_envoy_timestamp(value):
    """
    Envoy sends `signed-in-at` in one of two shapes — handle both:
      "2026-04-27T10:00:00Z"
      "2026-04-27 10:00:00 UTC"
    """
    if value.endswith(" UTC"):
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def calculate_expiry(attributes, tz):
    """End-of-day for normal visitors, end of selected end-date for Multiday."""
    checkin_raw = attributes.get("signed-in-at")
    base_time = parse_envoy_timestamp(checkin_raw) if checkin_raw else datetime.now(timezone.utc)

    if attributes.get(ENVOY_FIELD_VISITOR_TYPE) == MULTIDAY_VISITOR_TYPE:
        end_date_str = get_user_data_value(attributes, ENVOY_FIELD_END_DATE)
        if end_date_str:
            try:
                end_dt = datetime.strptime(end_date_str, "%Y-%m-%d").replace(tzinfo=tz)
                return end_of_day(end_dt, tz).strftime("%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                pass

    return end_of_day(base_time, tz).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_wifi_opt_in_yes(answer):
    """Lenient match — see comment on ENVOY_WIFI_OPT_IN_YES_PREFIX."""
    return answer.strip().lower().startswith(ENVOY_WIFI_OPT_IN_YES_PREFIX.lower())


def should_skip(attributes, location_id):
    """
    Reasons to skip:
      1. Visitor type is in the blocked list (e.g. Employee).
      2. Visitor declined the Wi-Fi opt-in question.
    Sites in AUTO_WIFI_LOCATION_IDS bypass step 2 — every non-blocked
    visitor gets an account because their form doesn't ask the question.
    """
    visitor_type = attributes.get(ENVOY_FIELD_VISITOR_TYPE, "")
    if visitor_type in NO_WIFI_VISITOR_TYPES:
        return True, "visitor type '{}' does not receive Wi-Fi".format(visitor_type)

    if location_id in AUTO_WIFI_LOCATION_IDS:
        return False, ""

    wifi_answer = get_user_data_value(attributes, ENVOY_FIELD_WIFI_OPT_IN)
    if not _is_wifi_opt_in_yes(wifi_answer):
        return True, "visitor opted out of Wi-Fi (answer: '{}')".format(wifi_answer)

    return False, ""


def generate_password(length=6):
    return "".join(secrets.choice(string.digits) for _ in range(length))


# ---------------------------------------------------------------------------
# Entry point — Tines Run Script (Python) calls main(input) and uses the
# returned dict as this action's output. Reference downstream as
# <<decide_and_build.output.skip>>, <<decide_and_build.output.agni_payload>>, ...
#
# The Tines Payload field on this action MUST be the flat per-field shape:
#   {
#     "full_name":       "<<webhook_trigger.body.payload.attributes['full-name']>>",
#     "email":           "<<webhook_trigger.body.payload.attributes.email>>",
#     "flow_name":       "<<webhook_trigger.body.payload.attributes['flow-name']>>",
#     "signed_in_at":    "<<webhook_trigger.body.payload.attributes['signed-in-at']>>",
#     "user_data":       "<<webhook_trigger.body.payload.attributes['user-data']>>",
#     "entry_id":        "<<webhook_trigger.body.payload.id>>",
#     "office_timezone": "<<webhook_trigger.body.payload.attributes['location-timezone']>>",
#     "location_id":     "<<webhook_trigger.body.meta.location.id>>"
#   }
# We chose this over `<<webhook_action.body>>` because Tines stringifies
# nested objects when substituted inside a quoted JSON value. Bracket
# notation on hyphenated keys avoids Tines parsing the hyphens as the
# subtraction operator.
# ---------------------------------------------------------------------------

def _attributes_from_flat_input(input):
    """
    Reassemble an Envoy-shaped `attributes` dict from the flat per-field
    Tines Payload, so the rest of the script can stay unchanged.
    Defensive against Tines passing list values as JSON-encoded strings.
    """
    user_data = input.get("user_data") or []
    if isinstance(user_data, str):
        try:
            user_data = json.loads(user_data)
        except (ValueError, TypeError):
            user_data = []

    return {
        "full-name":    (input.get("full_name") or "").strip(),
        "email":        (input.get("email") or "").strip(),
        "flow-name":    input.get("flow_name") or "",
        "signed-in-at": input.get("signed_in_at") or "",
        "user-data":    user_data,
    }


def _resolve_timezone(input):
    """
    Pick the office timezone for this event. The Envoy webhook supplies
    location-timezone per sign-in, so the same Story can serve any office
    without code changes. Falls back to DEFAULT_OFFICE_TIMEZONE if the
    webhook omitted it or supplied a name ZoneInfo doesn't recognize.
    """
    tz_name = input.get("office_timezone") or DEFAULT_OFFICE_TIMEZONE
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo(DEFAULT_OFFICE_TIMEZONE)


def main(input):
    attributes  = _attributes_from_flat_input(input)
    office_tz   = _resolve_timezone(input)
    location_id = str(input.get("location_id") or "")

    full_name = attributes["full-name"]
    email     = attributes["email"]

    if not full_name or not email:
        return {
            "skip":   True,
            "reason": "Incomplete visitor data — name: '{}', email: '{}'".format(full_name, email),
        }

    skip, reason = should_skip(attributes, location_id)
    if skip:
        return {
            "skip":      True,
            "reason":    reason,
            "full_name": full_name,
            "email":     email,
        }

    valid_to = calculate_expiry(attributes, office_tz)
    password = generate_password()

    agni_payload = {
        "orgID":       AGNI_ORG_ID,
        "portalID":    AGNI_PORTAL_ID,
        "loginName":   email,
        "name":        full_name,
        "email":       email,
        "password":    password,
        "deviceLimit": 1,
        "userType":    "guest",
        "validTo":     valid_to,
        "sendEmail":   True,
    }

    return {
        "skip":         False,
        "full_name":    full_name,
        "email":        email,
        "valid_to":     valid_to,
        "password":     password,
        "agni_payload": agni_payload,
    }

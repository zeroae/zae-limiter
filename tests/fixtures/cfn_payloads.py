"""CloudFormation custom-resource payloads recorded from real AWS (#554).

Every other test in this repository builds a `Custom::ZaeLimiterLimits` event
out of native Python types — `{"Disabled": False, "Capacity": 1000}` — which is
exactly why none of them caught #554. CloudFormation stringifies **every**
scalar in a custom resource's `ResourceProperties` before delivering them, so
the provisioner Lambda never sees a `bool` or an `int`, and a test that hands it
one is testing a shape that cannot occur in production.

:data:`RECORDED_CFN_RESOURCE_PROPERTIES` is the verbatim payload captured on
issue #554 from a real ``aws cloudformation deploy`` (account 733153035800,
us-east-1, disposable stack ``zae554-cfn-type-probe``, since deleted). Use it,
rather than a synthetic dict, for anything that asserts what the CFN boundary
produces.

What the recording established, and what any test built on it inherits:

* Every scalar arrives as ``str``. Structure survives — lists stay lists, maps
  stay maps — only the leaves are stringified.
* Quoting in the template is **not** preserved: ``Disabled: false`` and
  ``Disabled: "false"`` are byte-identical on arrival (`gpt-4` and
  `quoted-model` below were written each way and are indistinguishable here).
* ``!Ref`` of a ``Type: Number`` parameter is a string too.
* Create and Update are identical in typing, and ``OldResourceProperties`` is
  stringified as well.

Two quirks of the probe template itself are preserved deliberately, because
trimming them would make the fixture synthetic again:

1. ``System.Limits.rpm`` carries ``RefillPeriodSeconds``, which is the
   *schedule-entry* spelling; the limit level takes ``RefillPeriod``. The
   handler drops unrecognised properties by design, so it is silently ignored.
2. ``Resources."gpt-4".Limits.rpm.Schedule[0]`` sets both ``Scale`` and
   ``Capacity``, which ``ScheduleEntry`` forbids (exactly-one-of). So the
   recorded payload does **not** parse all the way through even once the types
   are right — it fails in `manifest._parse_entries` with a ``ValueError``
   naming the entry, which is the correct, reportable failure.
   :data:`RECORDED_CFN_RESOURCE_PROPERTIES_VALID` is the same payload with that
   one entry corrected, for tests that need an end-to-end parse.
"""

from __future__ import annotations

import copy
from typing import Any

RECORDED_CFN_RESOURCE_PROPERTIES: dict[str, Any] = {
    "ServiceToken": "arn:aws:lambda:us-east-1:733153035800:function:zae554-cfn-type-probe-echo",
    "Namespace": "default",
    "System": {
        "OnUnavailable": "block",
        "Limits": {
            "rpm": {"Capacity": "1000", "RefillAmount": "1000", "RefillPeriodSeconds": "60"}
        },
    },
    "Resources": {
        "gpt-4": {
            "Disabled": "false",
            "Limits": {
                "rpm": {
                    "Capacity": "500",
                    "RefillPeriodSeconds": "60",
                    "Schedule": [
                        {
                            "Cron": "0 9 * * 1-5",
                            "Tz": "America/New_York",
                            "Scale": "0.5",
                            "Capacity": "2000",
                        },
                        {"Cron": "0 18 * * 1-5", "Scale": "1"},
                    ],
                }
            },
        },
        # Template said `Disabled: "false"` (quoted); indistinguishable from the
        # unquoted `gpt-4` above by the time it reaches the Lambda.
        "quoted-model": {"Disabled": "false"},
        "enabled-model": {"Disabled": "true"},
    },
    "Entities": {
        "user-premium": {
            "Resources": {
                "gpt-4": {"Disabled": "false", "Limits": {"rpm": {"Capacity": "100"}}},
            }
        }
    },
}


def _valid_variant() -> dict[str, Any]:
    """The recorded payload with quirk (2) above corrected, so it parses.

    Only the offending schedule entry's ``Capacity`` is removed — every scalar
    stays a string, which is the property the fixture exists to carry.
    """
    props = copy.deepcopy(RECORDED_CFN_RESOURCE_PROPERTIES)
    del props["Resources"]["gpt-4"]["Limits"]["rpm"]["Schedule"][0]["Capacity"]
    return props


RECORDED_CFN_RESOURCE_PROPERTIES_VALID: dict[str, Any] = _valid_variant()

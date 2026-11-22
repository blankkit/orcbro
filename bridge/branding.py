"""Stable identifiers used by the app and discovery."""

# Orcbro is the bridge, Bambro is the app; Brotocol is their interface.
PRODUCT = "Orcbro"
PAIRING_SCHEME = "bambro"
MDNS_SERVICE_TYPE = "_orcbro._tcp.local."
# Fixed-access-key app-token context; keep unchanged so existing pairings remain valid.
LEGACY_TOKEN_CONTEXT = b"orcaios-app-v1"
# Live Activity push relay (relay/) for bridges without their own app APNs key.
PUSH_RELAY_URL = "https://push.blankk.it"

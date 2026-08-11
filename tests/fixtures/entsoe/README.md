# ENTSO-E fixtures

Real responses from `https://web-api.tp.entsoe.eu/api`, recorded on
2026-08-11. Nothing here is generated. The security token has been replaced
with the literal `REDACTED-TOKEN` where it appeared in a response body.

Source: ENTSO-E Transparency Platform. Check their reuse terms before this
repository goes public, since these files are republished platform data.

| File | Request | Result |
|---|---|---|
| `a75_fr_20260804.xml` | `documentType=A75&processType=A16&in_Domain=10YFR-RTE------C&periodStart=202608040000&periodEnd=202608050000` | http 200, `GL_MarketDocument`, 15 TimeSeries, PT15M |
| `ack_no_data.xml` | same but `periodStart=203001010000&periodEnd=203001020000` | **http 200**, acknowledgement, reason 999 |
| `ack_bad_domain.xml` | same as the first but `in_Domain=10YNOTAREALZONE1` | http 200, acknowledgement, reason 999, byte-identical shape to no-data |
| `ack_bad_token.xml` | first request with an all-zero token | http 401, acknowledgement, reason 999 "Authentication failed." |

Three things these recordings pinned down that the docs do not say plainly.

A rejection can arrive with http 200. Checking the status code alone is not
enough to know whether a request succeeded.

Reason code 999 is used for everything, including authentication failure and
an empty window. The code on its own carries no information, so the client
keys off the status and the reason text.

A zone code that does not exist returns the same "no matching data found" as a
window that is genuinely empty. There is no way for the client to tell a typo
from a quiet period, which means zone codes have to be validated before they
reach the API rather than after.

## Refreshing these

The generation window is fixed at 2026-08-04, so `a75_fr_20260804.xml` should
stay stable. If it is ever re-recorded and the contents change, that is a
revision to the published figures, which is the phenomenon this whole project
is about. Keep the old copy.

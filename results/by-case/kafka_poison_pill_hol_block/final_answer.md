> **Finding:** During **2026-09-28T02:58:14.893574Z–02:59:23.894717Z**, the `payment` service rejected checkout requests because the payment token was invalid (`app.loyalty.level=gold`). `checkout` propagated the rejection as gRPC `13 INTERNAL`, causing frontend HTTP 500s and the order-validation backlog.

**Status:** mechanism found but trigger missing

**Confidence:** Low

**Why this conclusion:** `checkout` showed 105 requests, 18 errors, 8 root-cause errors, a 17.14% error rate, and 33.75 seconds P90 latency; `payment` also recorded errors. A complete 32-span checkout trace identified `grpc.oteldemo.PaymentService/Charge` as the backend-marked root-cause span with the message **“Payment request failed. Invalid token. app.loyalty.level=gold”**. The same trace showed the failure propagating through `checkout` as “could not charge the card” and then through `frontend` as HTTP 500, which weakens checkout or frontend as the origin. The trace does not establish why the token became invalid.

**Impact:** Checkout: 18 errors / 105 requests, 17.14% error rate, 33.75 s P90 latency. Payment: 6 errors / 61 requests.

**Calibration:** Not calibrated — the supplied fixed window only. Problem onset, recovery, and episode completeness were not established.

**Caveat:** The token rejection mechanism is directly observed, but the in-window telemetry does not identify the token-rotation, credential, or configuration change that made the token invalid.

**Next check:** Inspect the payment token source and token-rotation/configuration history for the `app.loyalty.level=gold` path.

A separate fault was also present in the same window: `flagd` and its evaluation endpoint reported 100% errors, while `oteldemo.RecommendationService` and `oteldemo.ProductCatalogService` also showed 100% errors. However, the complete checkout trace attributes the observed order-validation failure to `payment`, not those services.

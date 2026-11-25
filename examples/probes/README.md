# Business validation RPC contract

`demo.proto` is copied from OpenTelemetry Demo tag `3.0.0`,
https://github.com/open-telemetry/opentelemetry-demo/blob/3.0.0/pb/demo.proto .
It retains its upstream copyright and Apache 2.0 license header.

`scripts/business_validate.py` uses this pinned contract with grpcurl so validation
does not depend on payment/cart/checkout enabling gRPC reflection. Requests use a
fresh test user, a dummy card, and `synthetic_request=true` baggage.

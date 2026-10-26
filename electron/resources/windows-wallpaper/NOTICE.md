# Windows wallpaper helper

Amadeus's helper is first-party AGPL-3.0 code and uses only Python's standard
library. Python is supplied by the same runtime selection as the desktop backend;
this resource does not bundle a Python or .NET runtime.

The bounded RPC wire adapter was implemented with reference to the external
GrpcDotNetNamedPipes protocol and framing at revision
`da307b551a9d56b62f5780e91a2d95a699f32430` (3.1.0):
https://github.com/cyanfish/grpc-dotnet-namedpipes/tree/da307b551a9d56b62f5780e91a2d95a699f32430

Protocol/framing reference: Copyright 2020 Google LLC; Apache License 2.0,
included in `Apache-2.0.txt`. No GrpcDotNetNamedPipes, Google.Protobuf,
Grpc.Core.Api, System.Memory, or .NET runtime binaries are distributed here.
The original source and pinned revision remain available at the URL above.

The service field numbers describe Lively 2.2.1.0's external IPC interface.
Lively is a separate optional GPL-3.0 application obtained from its publisher;
its own prerequisites and notices are the responsibility of that distribution.
Known reporting/close limitations are tracked in Amadeus #233.

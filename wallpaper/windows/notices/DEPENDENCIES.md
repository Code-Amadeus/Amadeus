# Windows wallpaper helper dependencies

The helper is Amadeus first-party code (AGPL-3.0). Its small protocol definitions
describe the Lively v2.2.1.0 external wire interface; Lively binaries are not
included. Windows Package Manager obtains unmodified Lively directly from its
publisher; Lively retains its own GPL-3.0 license and installer notices.

Runtime packages (exact dependency graph and integrity hashes in
`Amadeus.Wallpaper/packages.lock.json`):

- Google.Protobuf 3.32.0: Copyright 2008 Google Inc.; BSD-3-Clause.
  https://github.com/protocolbuffers/protobuf/tree/4fbd1111a292d04746c732573025e3251de0bb9c
- Grpc.Core.Api 2.67.0: Copyright 2019 The gRPC Authors; Apache-2.0.
  https://github.com/grpc/grpc-dotnet/tree/7c43ddb5d68008782dc0dba2d0feaa3ed91a9fb2
- GrpcDotNetNamedPipes 3.1.0: Ben Olden-Cooligan; package copyright
  Copyright 2020 Google LLC; Apache-2.0.
  https://github.com/cyanfish/grpc-dotnet-namedpipes/tree/da307b551a9d56b62f5780e91a2d95a699f32430
- System.Memory 4.6.0: Copyright Microsoft Corporation; MIT.
  https://github.com/dotnet/maintenance-packages/tree/d0c2a5a83211e271826172a6b0510c25a52dbd53
- Microsoft.NETCore.App.Runtime.win-x64 8.0.31: .NET Foundation and
  Contributors; MIT and the accompanying .NET third-party notices.

Grpc.Tools 2.72.0 is a build-time dependency and is not shipped.

These notice files are copied into the published helper. Keep the .NET notices
in sync with RuntimeFrameworkVersion when updating its bundled runtime.

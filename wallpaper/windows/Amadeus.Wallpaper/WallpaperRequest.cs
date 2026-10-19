using System.ComponentModel;
using System.Diagnostics;
using System.IO.Pipes;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Runtime.ExceptionServices;
using System.Runtime.InteropServices;
using Google.Protobuf;
using Google.Protobuf.WellKnownTypes;
using Grpc.Core;
using GrpcDotNetNamedPipes;
using Microsoft.Win32.SafeHandles;
using Amadeus.Wallpaper.Protocol.Desktop;

[assembly: InternalsVisibleTo("Amadeus.Wallpaper.Tests")]

namespace Amadeus.Wallpaper;

internal static class WallpaperRequest
{
    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    static extern bool GetNamedPipeServerProcessId(SafePipeHandle pipe, out uint processId);

    static ProcessIdentity Identify(NamedPipeClientStream pipe)
    {
        if (!GetNamedPipeServerProcessId(pipe.SafePipeHandle, out var id))
            throw new Win32Exception(Marshal.GetLastWin32Error(), "Could not identify the wallpaper request receiver.");
        using var process = Process.GetProcessById(checked((int)id));
        return new(process.Id, process.StartTime.ToUniversalTime().Ticks);
    }

    static void BindDefault(NamedPipeChannel channel, Action<NamedPipeClientStream> capture) =>
        Bind(channel, capture, typeof(NamedPipeChannel).GetProperty("PipeCallback", BindingFlags.Instance | BindingFlags.NonPublic));

    internal static void Bind(NamedPipeChannel channel, Action<NamedPipeClientStream> capture, PropertyInfo? callback)
    {
        // 锁定 GrpcDotNetNamedPipes 3.1.0 的唯一内部适配点；缺失时必须阻止派发。
        if (callback?.PropertyType != typeof(Action<NamedPipeClientStream>) || callback.SetMethod is null)
            throw new NotSupportedException("GrpcDotNetNamedPipes no longer exposes the required request pipe binding; wallpaper dispatch was blocked.");
        callback.SetValue(channel, capture);
    }

    internal static async Task Send(string pipeName, SetWallpaperRequest request, Action<ProcessIdentity> dispatching,
        Func<NamedPipeClientStream, ProcessIdentity>? identify = null,
        Action<NamedPipeChannel, Action<NamedPipeClientStream>>? bind = null, DateTime? deadline = null)
    {
        var channel = new NamedPipeChannel(".", pipeName);
        NamedPipeClientStream? pipe = null;
        (bind ?? BindDefault)(channel, connected => pipe = connected);
        var dispatchGate = new object();
        var ended = false;
        Exception? boundaryError = null;
        var marshaller = Marshallers.Create<SetWallpaperRequest>(value => {
            lock (dispatchGate)
            {
                if (ended) throw new InvalidOperationException("Wallpaper Set invocation ended before dispatch; no request was sent.");
                try
                {
                    // 该版本在同一连接 Connect 完成后才序列化，随后才写入请求；不得另开探测连接。
                    var payload = value.ToByteArray();
                    var identity = (identify ?? Identify)(pipe ?? throw new IOException("Wallpaper request pipe was not captured."));
                    dispatching(identity);
                    return payload;
                }
                catch (Exception error) { boundaryError = error; throw; }
            }
        }, SetWallpaperRequest.Parser.ParseFrom);
        var method = new Method<SetWallpaperRequest, Empty>(MethodType.Unary,
            DesktopService.Descriptor.FullName, "SetWallpaper", marshaller,
            Marshallers.Create<Empty>(value => value.ToByteArray(), Empty.Parser.ParseFrom));
        try
        {
            try
            {
                using var call = channel.AsyncUnaryCall(method, null,
                    new CallOptions(deadline: deadline ?? DateTime.UtcNow.AddSeconds(15)), request);
                await call.ResponseAsync;
            }
            finally
            {
                // ResponseAsync 超时不等待后台连接；关闭派发门，同时等已进入的保存边界结束。
                lock (dispatchGate) ended = true;
            }
        }
        catch (Exception) when (boundaryError is not null)
        {
            // 传输库会包装部分 IO 异常；保留发送前身份或持久化失败的原始原因。
            ExceptionDispatchInfo.Capture(boundaryError).Throw();
        }
    }
}

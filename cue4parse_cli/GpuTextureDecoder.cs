using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
using CUE4Parse.UE4.Assets.Exports.Texture;
using CUE4Parse_Conversion.Textures;
using Silk.NET.Vulkan;
using VkBuffer = Silk.NET.Vulkan.Buffer;

namespace CUE4ParseCLI;

/// <summary>
/// GPU texture decode for block-compressed UE formats (BC1/BC3/BC4/BC5/BC7 and
/// ASTC LDR when the driver supports it), implemented on top of Vulkan's
/// fixed-function sampler instead of handwritten compute kernels.
///
/// The compressed mip is uploaded as a native-format sampled image, mapped 1:1
/// into an offscreen RGBA8 target by trivial fullscreen shaders, and copied
/// back to host memory. The hardware sampler performs the decode itself — one
/// code path covers Windows and Linux with no per-format kernels to maintain.
/// Fragment shaders replicate the exact channel layout CUE4Parse's CPU
/// decoders produce per format/OS (see CUE4Parse-Conversion's
/// TextureDecoder.DecodeBytes) so exports stay comparable to plain CLI runs.
///
/// All failure modes degrade to the CPU decoder: init failure marks the
/// decoder unavailable for the rest of the session; a per-texture failure
/// falls back for that texture only. Scratch resources are reused across
/// same-sized textures and rebuilt (destroy + create) on size changes.
/// </summary>
internal static class GpuTextureDecoder
{
    private enum FragMode { Pass, SwizzleBgra, Bc4, Bc5, Normal }

    private sealed record FormatSpec(
        Format VkFormat, FragMode Mode, int BlockW, int BlockH, int BlockBytes, EPixelFormat OutFormat);

    private static readonly Dictionary<EPixelFormat, FormatSpec> AllFormats = new()
    {
        [EPixelFormat.PF_DXT1] = new(Format.BC1RgbUnormBlock, FragMode.Pass, 4, 4, 8, EPixelFormat.PF_R8G8B8A8),
        [EPixelFormat.PF_DXT5] = new(Format.BC3UnormBlock, FragMode.Pass, 4, 4, 16, EPixelFormat.PF_R8G8B8A8),
        [EPixelFormat.PF_BC4] = new(Format.BC4UnormBlock, FragMode.Bc4, 4, 4, 16, EPixelFormat.PF_B8G8R8A8),
        [EPixelFormat.PF_BC5] = new(Format.BC5UnormBlock, FragMode.Bc5, 4, 4, 16, EPixelFormat.PF_B8G8R8A8),
        // Windows CPU baseline (Detex) yields BGRA byte order; on Linux it is
        // the AssetRipper Bc7 path with RGBA. The fragment mode mirrors the
        // current OS so outputs match whatever the plain CLI run produces.
        [EPixelFormat.PF_BC7] = new(Format.BC7UnormBlock,
            OperatingSystem.IsWindows() ? FragMode.SwizzleBgra : FragMode.Pass, 4, 4, 16,
            OperatingSystem.IsWindows() ? EPixelFormat.PF_B8G8R8A8 : EPixelFormat.PF_R8G8B8A8),
        [EPixelFormat.PF_ASTC_4x4] = new(Format.Astc4x4UnormBlock, FragMode.Pass, 4, 4, 16, EPixelFormat.PF_R8G8B8A8),
        [EPixelFormat.PF_ASTC_6x6] = new(Format.Astc6x6UnormBlock, FragMode.Pass, 6, 6, 16, EPixelFormat.PF_R8G8B8A8),
        [EPixelFormat.PF_ASTC_8x8] = new(Format.Astc8x8UnormBlock, FragMode.Pass, 8, 8, 16, EPixelFormat.PF_R8G8B8A8),
        [EPixelFormat.PF_ASTC_10x10] = new(Format.Astc10x10UnormBlock, FragMode.Pass, 10, 10, 16, EPixelFormat.PF_R8G8B8A8),
        [EPixelFormat.PF_ASTC_12x12] = new(Format.Astc12x12UnormBlock, FragMode.Pass, 12, 12, 16, EPixelFormat.PF_R8G8B8A8),
    };

    private const string AstcLdrExtension = "VK_KHR_texture_compression_astc_ldr";

    // SPIR-V blobs (compiled with glslc, below sources are documented in the
    // repo docs). Vertex emits a fullscreen triangle with vUv = (p + 1) * 0.5;
    // the fragment variants implement the per-format channel layouts and turn
    // vUv into exact texel sampling coordinates via push-constant image sizes.
    private const string VertexBlob = "AwIjBwAAAQALAA0AMAAAAAAAAAARAAIAAQAAAAsABgABAAAAR0xTTC5zdGQuNDUwAAAAAA4AAwAAAAAAAQAAAA8ACAAAAAAABAAAAG1haW4AAAAADAAAABoAAAAnAAAAAwADAAIAAADCAQAABAAKAEdMX0dPT0dMRV9jcHBfc3R5bGVfbGluZV9kaXJlY3RpdmUAAAQACABHTF9HT09HTEVfaW5jbHVkZV9kaXJlY3RpdmUABQAEAAQAAABtYWluAAAAAAUAAwAJAAAAcAAAAAUABgAMAAAAZ2xfVmVydGV4SW5kZXgAAAUAAwAaAAAAdlV2AAUABgAlAAAAZ2xfUGVyVmVydGV4AAAAAAYABgAlAAAAAAAAAGdsX1Bvc2l0aW9uAAYABwAlAAAAAQAAAGdsX1BvaW50U2l6ZQAAAAAGAAcAJQAAAAIAAABnbF9DbGlwRGlzdGFuY2UABgAHACUAAAADAAAAZ2xfQ3VsbERpc3RhbmNlAAUAAwAnAAAAAAAAAEcABAAMAAAACwAAACoAAABHAAQAGgAAAB4AAAAAAAAASAAFACUAAAAAAAAACwAAAAAAAABIAAUAJQAAAAEAAAALAAAAAQAAAEgABQAlAAAAAgAAAAsAAAADAAAASAAFACUAAAADAAAACwAAAAQAAABHAAMAJQAAAAIAAAATAAIAAgAAACEAAwADAAAAAgAAABYAAwAGAAAAIAAAABcABAAHAAAABgAAAAIAAAAgAAQACAAAAAcAAAAHAAAAFQAEAAoAAAAgAAAAAQAAACAABAALAAAAAQAAAAoAAAA7AAQACwAAAAwAAAABAAAAKwAEAAoAAAAOAAAAAQAAABQAAgAPAAAAKwAEAAYAAAARAAAAAABAQCsABAAGAAAAEgAAAAAAgL8rAAQACgAAABUAAAACAAAAIAAEABkAAAADAAAABwAAADsABAAZAAAAGgAAAAMAAAArAAQABgAAABwAAAAAAIA/KwAEAAYAAAAfAAAAAAAAPxcABAAhAAAABgAAAAQAAAAVAAQAIgAAACAAAAAAAAAAKwAEACIAAAAjAAAAAQAAABwABAAkAAAABgAAACMAAAAeAAYAJQAAACEAAAAGAAAAJAAAACQAAAAgAAQAJgAAAAMAAAAlAAAAOwAEACYAAAAnAAAAAwAAACsABAAKAAAAKAAAAAAAAAArAAQABgAAACoAAAAAAAAAIAAEAC4AAAADAAAAIQAAADYABQACAAAABAAAAAAAAAADAAAA+AACAAUAAAA7AAQACAAAAAkAAAAHAAAAPQAEAAoAAAANAAAADAAAAKoABQAPAAAAEAAAAA0AAAAOAAAAqQAGAAYAAAATAAAAEAAAABEAAAASAAAAPQAEAAoAAAAUAAAADAAAAKoABQAPAAAAFgAAABQAAAAVAAAAqQAGAAYAAAAXAAAAFgAAABEAAAASAAAAUAAFAAcAAAAYAAAAEwAAABcAAAA+AAMACQAAABgAAAA9AAQABwAAABsAAAAJAAAAUAAFAAcAAAAdAAAAHAAAABwAAACBAAUABwAAAB4AAAAbAAAAHQAAAI4ABQAHAAAAIAAAAB4AAAAfAAAAPgADABoAAAAgAAAAPQAEAAcAAAApAAAACQAAAFEABQAGAAAAKwAAACkAAAAAAAAAUQAFAAYAAAAsAAAAKQAAAAEAAABQAAcAIQAAAC0AAAArAAAALAAAACoAAAAcAAAAQQAFAC4AAAAvAAAAJwAAACgAAAA+AAMALwAAAC0AAAD9AAEAOAABAA==";
    private static readonly Dictionary<FragMode, string> FragmentBlobs = new()
    {
        [FragMode.Pass] = "AwIjBwAAAQALAA0AFAAAAAAAAAARAAIAAQAAAAsABgABAAAAR0xTTC5zdGQuNDUwAAAAAA4AAwAAAAAAAQAAAA8ABwAEAAAABAAAAG1haW4AAAAACQAAABEAAAAQAAMABAAAAAcAAAADAAMAAgAAAMIBAAAEAAoAR0xfR09PR0xFX2NwcF9zdHlsZV9saW5lX2RpcmVjdGl2ZQAABAAIAEdMX0dPT0dMRV9pbmNsdWRlX2RpcmVjdGl2ZQAFAAQABAAAAG1haW4AAAAABQAFAAkAAABvdXRDb2xvcgAAAAAFAAMADQAAAHNyYwAFAAMAEQAAAHZVdgBHAAQACQAAAB4AAAAAAAAARwAEAA0AAAAiAAAAAAAAAEcABAANAAAAIQAAAAAAAABHAAQAEQAAAB4AAAAAAAAAEwACAAIAAAAhAAMAAwAAAAIAAAAWAAMABgAAACAAAAAXAAQABwAAAAYAAAAEAAAAIAAEAAgAAAADAAAABwAAADsABAAIAAAACQAAAAMAAAAZAAkACgAAAAYAAAABAAAAAAAAAAAAAAAAAAAAAQAAAAAAAAAbAAMACwAAAAoAAAAgAAQADAAAAAAAAAALAAAAOwAEAAwAAAANAAAAAAAAABcABAAPAAAABgAAAAIAAAAgAAQAEAAAAAEAAAAPAAAAOwAEABAAAAARAAAAAQAAADYABQACAAAABAAAAAAAAAADAAAA+AACAAUAAAA9AAQACwAAAA4AAAANAAAAPQAEAA8AAAASAAAAEQAAAFcABQAHAAAAEwAAAA4AAAASAAAAPgADAAkAAAATAAAA/QABADgAAQA=",
        [FragMode.SwizzleBgra] = "AwIjBwAAAQALAA0AJQAAAAAAAAARAAIAAQAAAAsABgABAAAAR0xTTC5zdGQuNDUwAAAAAA4AAwAAAAAAAQAAAA8ABwAEAAAABAAAAG1haW4AAAAAEQAAABUAAAAQAAMABAAAAAcAAAADAAMAAgAAAMIBAAAEAAoAR0xfR09PR0xFX2NwcF9zdHlsZV9saW5lX2RpcmVjdGl2ZQAABAAIAEdMX0dPT0dMRV9pbmNsdWRlX2RpcmVjdGl2ZQAFAAQABAAAAG1haW4AAAAABQADAAkAAAB3AAAABQADAA0AAABzcmMABQADABEAAAB2VXYABQAFABUAAABvdXRDb2xvcgAAAABHAAQADQAAACIAAAAAAAAARwAEAA0AAAAhAAAAAAAAAEcABAARAAAAHgAAAAAAAABHAAQAFQAAAB4AAAAAAAAAEwACAAIAAAAhAAMAAwAAAAIAAAAWAAMABgAAACAAAAAXAAQABwAAAAYAAAAEAAAAIAAEAAgAAAAHAAAABwAAABkACQAKAAAABgAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAABsAAwALAAAACgAAACAABAAMAAAAAAAAAAsAAAA7AAQADAAAAA0AAAAAAAAAFwAEAA8AAAAGAAAAAgAAACAABAAQAAAAAQAAAA8AAAA7AAQAEAAAABEAAAABAAAAIAAEABQAAAADAAAABwAAADsABAAUAAAAFQAAAAMAAAAVAAQAFgAAACAAAAAAAAAAKwAEABYAAAAXAAAAAgAAACAABAAYAAAABwAAAAYAAAArAAQAFgAAABsAAAABAAAAKwAEABYAAAAeAAAAAAAAACsABAAWAAAAIQAAAAMAAAA2AAUAAgAAAAQAAAAAAAAAAwAAAPgAAgAFAAAAOwAEAAgAAAAJAAAABwAAAD0ABAALAAAADgAAAA0AAAA9AAQADwAAABIAAAARAAAAVwAFAAcAAAATAAAADgAAABIAAAA+AAMACQAAABMAAABBAAUAGAAAABkAAAAJAAAAFwAAAD0ABAAGAAAAGgAAABkAAABBAAUAGAAAABwAAAAJAAAAGwAAAD0ABAAGAAAAHQAAABwAAABBAAUAGAAAAB8AAAAJAAAAHgAAAD0ABAAGAAAAIAAAAB8AAABBAAUAGAAAACIAAAAJAAAAIQAAAD0ABAAGAAAAIwAAACIAAABQAAcABwAAACQAAAAaAAAAHQAAACAAAAAjAAAAPgADABUAAAAkAAAA/QABADgAAQA=",
        [FragMode.Bc4] = "AwIjBwAAAQALAA0AHgAAAAAAAAARAAIAAQAAAAsABgABAAAAR0xTTC5zdGQuNDUwAAAAAA4AAwAAAAAAAQAAAA8ABwAEAAAABAAAAG1haW4AAAAAEAAAABgAAAAQAAMABAAAAAcAAAADAAMAAgAAAMIBAAAEAAoAR0xfR09PR0xFX2NwcF9zdHlsZV9saW5lX2RpcmVjdGl2ZQAABAAIAEdMX0dPT0dMRV9pbmNsdWRlX2RpcmVjdGl2ZQAFAAQABAAAAG1haW4AAAAABQADAAgAAABnAAAABQADAAwAAABzcmMABQADABAAAAB2VXYABQAFABgAAABvdXRDb2xvcgAAAABHAAQADAAAACIAAAAAAAAARwAEAAwAAAAhAAAAAAAAAEcABAAQAAAAHgAAAAAAAABHAAQAGAAAAB4AAAAAAAAAEwACAAIAAAAhAAMAAwAAAAIAAAAWAAMABgAAACAAAAAgAAQABwAAAAcAAAAGAAAAGQAJAAkAAAAGAAAAAQAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAGwADAAoAAAAJAAAAIAAEAAsAAAAAAAAACgAAADsABAALAAAADAAAAAAAAAAXAAQADgAAAAYAAAACAAAAIAAEAA8AAAABAAAADgAAADsABAAPAAAAEAAAAAEAAAAXAAQAEgAAAAYAAAAEAAAAFQAEABQAAAAgAAAAAAAAACsABAAUAAAAFQAAAAAAAAAgAAQAFwAAAAMAAAASAAAAOwAEABcAAAAYAAAAAwAAACsABAAGAAAAHAAAAAAAgD82AAUAAgAAAAQAAAAAAAAAAwAAAPgAAgAFAAAAOwAEAAcAAAAIAAAABwAAAD0ABAAKAAAADQAAAAwAAAA9AAQADgAAABEAAAAQAAAAVwAFABIAAAATAAAADQAAABEAAABRAAUABgAAABYAAAATAAAAAAAAAD4AAwAIAAAAFgAAAD0ABAAGAAAAGQAAAAgAAAA9AAQABgAAABoAAAAIAAAAPQAEAAYAAAAbAAAACAAAAFAABwASAAAAHQAAABkAAAAaAAAAGwAAABwAAAA+AAMAGAAAAB0AAAD9AAEAOAABAA==",
        [FragMode.Bc5] = "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",
        [FragMode.Normal] = "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",
    };

    private static readonly object _sync = new();

    private static bool _probed;
    private static bool _available;
    private static string _deviceName = "";
    private static string _reason = "not probed yet";

    private static Vk? _vk;
    private static Instance _instance;
    private static PhysicalDevice _physical;
    private static Device _device;
    private static Queue _queue;
    private static uint _queueFamily;
    private static CommandPool _commandPool;
    private static CommandBuffer _commandBuffer;
    private static Fence _fence;
    private static RenderPass _renderPass;
    private static Sampler _sampler;
    private static DescriptorSetLayout _setLayout;
    private static DescriptorPool _descriptorPool;
    private static DescriptorSet _descriptorSet;
    private static PipelineLayout _pipelineLayout;
    private static ShaderModule _vertModule;
    private static readonly Dictionary<FragMode, ShaderModule> _fragModules = new();
    private static readonly Dictionary<FragMode, Pipeline> _pipelines = new();
    private static DeviceMemory _stagingMemory;
    private static VkBuffer _stagingBuffer;
    private static IntPtr _stagingMapped;
    private static ulong _stagingCapacity;
    private static DeviceMemory _readbackMemory;
    private static VkBuffer _readbackBuffer;
    private static IntPtr _readbackMapped;
    private static ulong _readbackCapacity;
    private static Image _srcImage;
    private static ImageView _srcView;
    private static DeviceMemory _srcMemory;
    private static Format _srcFormat;
    private static uint _srcWidth;
    private static uint _srcHeight;
    private static Image _dstImage;
    private static ImageView _dstView;
    private static DeviceMemory _dstMemory;
    private static Framebuffer _framebuffer;
    private static uint _dstWidth;
    private static uint _dstHeight;
    private static bool _descriptorBound;

    private static Dictionary<EPixelFormat, FormatSpec> _formats = new();

    /// <summary>
    /// GPU-decode a texture. Returns null for every case that should use the
    /// CPU decoder: no GPU, virtual textures, volumetric data, crunched
    /// mipmaps, or unsupported formats.
    /// </summary>
    public static CTexture? TryDecode(UTexture2D texture, out string? fallbackReason)
    {
        fallbackReason = null;
        try
        {
            lock (_sync)
            {
                if (_probed && !_available)
                {
                    fallbackReason = _reason;
                    return null;
                }
        if (!ProbeInternal())
        {
            fallbackReason = _reason;
            return null;
        }
                return TryDecodeLocked(texture, out fallbackReason);
            }
        }
        catch (Exception ex)
        {
            fallbackReason = ex.Message;
            Log($"GPU decode failed for {texture.Name}: {ex.Message}");
            return null;
        }
    }

    private static CTexture? TryDecodeLocked(UTexture2D texture, out string? fallbackReason)
    {
        fallbackReason = null;

        var pd = texture.PlatformData;
        if (pd is null)
            return null;
        if (pd.VTData != null && pd.VTData.IsInitialized())
        {
            fallbackReason = "virtual texture (CPU path required)";
            return null;
        }

        var mip = texture.GetMip(texture.GetFirstMipIndex());
        if (mip?.BulkData?.Data is not { Length: > 0 } blockData)
            return null;
        if (mip.SizeX <= 0 || mip.SizeY <= 0)
            return null;
        if (mip.SizeZ > 1 || pd.GetNumSlices() > 1)
        {
            fallbackReason = "volumetric/cubemap texture (CPU path required)";
            return null;
        }
        if (pd.PixelFormat.EndsWith("_crunched", StringComparison.OrdinalIgnoreCase))
        {
            fallbackReason = "crunched texture (CPU path required)";
            return null;
        }

        if (!_formats.TryGetValue(texture.Format, out var spec))
            return null;

        var sizeX = mip.SizeX;
        var sizeY = mip.SizeY;
        if (texture.Format == EPixelFormat.PF_BC7)
        {
            sizeX = (sizeX + 3) / 4 * 4;
            sizeY = (sizeY + 3) / 4 * 4;
        }

        // ASTC normal-map textures restore the Z channel post-decode (BC5
        // restores it unconditionally, baked into its fragment mode).
        if (spec.Mode == FragMode.Pass && texture.IsNormalMap && IsAstcFormat(texture.Format))
            spec = spec with { Mode = FragMode.Normal };

        var blocksX = (uint)((sizeX + spec.BlockW - 1) / spec.BlockW);
        var blocksY = (uint)((sizeY + spec.BlockH - 1) / spec.BlockH);
        var padX = blocksX * (uint)spec.BlockW;
        var padY = blocksY * (uint)spec.BlockH;
        var required = (ulong)blocksX * blocksY * (ulong)spec.BlockBytes;
        if ((ulong)blockData.Length < required)
            return null;

        if (!GpuDecodeLocked(spec, blockData, padX, padY, required, sizeX, sizeY, out var rgba))
        {
            fallbackReason = "GPU decode failed for this texture";
            return null;
        }

        return new CTexture(sizeX, sizeY, spec.OutFormat, rgba);
    }

    private static bool IsAstcFormat(EPixelFormat format) =>
        format is EPixelFormat.PF_ASTC_4x4 or EPixelFormat.PF_ASTC_6x6 or EPixelFormat.PF_ASTC_8x8
            or EPixelFormat.PF_ASTC_10x10 or EPixelFormat.PF_ASTC_12x12;

    /// <summary>Core GPU op: upload → draw → readback, cropped to sizeX×sizeY.</summary>
    private static unsafe bool GpuDecodeLocked(FormatSpec spec, byte[] blockData,
        uint padX, uint padY, ulong required, int sizeX, int sizeY, out byte[] rgba)
    {
        rgba = Array.Empty<byte>();
        var vk = _vk!;

        if (!EnsureScratchBuffers(required, (ulong)padX * padY * 4))
            return false;

        if (_srcWidth != padX || _srcHeight != padY || _srcFormat != spec.VkFormat)
        {
            DestroySourceResources();
            if (!CreateImage(spec.VkFormat, padX, padY,
                    ImageUsageFlags.TransferDstBit | ImageUsageFlags.SampledBit,
                    out _srcImage, out _srcMemory))
                return false;
            if (!CreateImageView(_srcImage, spec.VkFormat, out _srcView))
                return false;
            _srcWidth = padX;
            _srcHeight = padY;
            _srcFormat = spec.VkFormat;
            _descriptorBound = false;
        }

        if (_dstWidth != padX || _dstHeight != padY)
        {
            DestroyTargetResources();
            if (!CreateImage(Format.R8G8B8A8Unorm, padX, padY,
                    ImageUsageFlags.ColorAttachmentBit | ImageUsageFlags.TransferSrcBit,
                    out _dstImage, out _dstMemory))
                return false;
            if (!CreateImageView(_dstImage, Format.R8G8B8A8Unorm, out _dstView))
                return false;
            if (!CreateFramebuffer())
                return false;
            _dstWidth = padX;
            _dstHeight = padY;
        }

        if (!_descriptorBound && !BindDescriptor())
            return false;

        var stagingSpan = new Span<byte>((void*)_stagingMapped, (int)Math.Min(required, int.MaxValue));
        blockData.AsSpan(0, (int)required).CopyTo(stagingSpan);

        var cmd = _commandBuffer;
        vk.ResetFences(_device, new Fence[] { _fence });
        ThrowOn(vk.ResetCommandBuffer(cmd, 0), "reset command buffer");
        var beginInfo = new CommandBufferBeginInfo
        {
            SType = StructureType.CommandBufferBeginInfo,
            Flags = CommandBufferUsageFlags.OneTimeSubmitBit,
        };
        ThrowOn(vk.BeginCommandBuffer(cmd, in beginInfo), "begin command buffer");

        var copyRegion = new BufferImageCopy
        {
            BufferOffset = 0,
            BufferRowLength = 0,
            BufferImageHeight = 0,
            ImageSubresource = new ImageSubresourceLayers
            {
                AspectMask = ImageAspectFlags.ColorBit,
                MipLevel = 0,
                BaseArrayLayer = 0,
                LayerCount = 1,
            },
            ImageOffset = new Offset3D(),
            ImageExtent = new Extent3D(padX, padY, 1),
        };
        vk.CmdCopyBufferToImage(cmd, _stagingBuffer, _srcImage, ImageLayout.TransferDstOptimal,
            new BufferImageCopy[] { copyRegion });

        var toShaderRead = new ImageMemoryBarrier
        {
            SType = StructureType.ImageMemoryBarrier,
            SrcAccessMask = AccessFlags.TransferWriteBit,
            DstAccessMask = AccessFlags.ShaderReadBit,
            OldLayout = ImageLayout.TransferDstOptimal,
            NewLayout = ImageLayout.ShaderReadOnlyOptimal,
            SrcQueueFamilyIndex = Vk.QueueFamilyIgnored,
            DstQueueFamilyIndex = Vk.QueueFamilyIgnored,
            Image = _srcImage,
            SubresourceRange = new ImageSubresourceRange
            {
                AspectMask = ImageAspectFlags.ColorBit,
                BaseMipLevel = 0,
                LevelCount = 1,
                BaseArrayLayer = 0,
                LayerCount = 1,
            },
        };
        vk.CmdPipelineBarrier(cmd,
            PipelineStageFlags.TransferBit, PipelineStageFlags.FragmentShaderBit,
            DependencyFlags.None,
            0, null, 0, null, 1, &toShaderRead);

        var passBegin = new RenderPassBeginInfo
        {
            SType = StructureType.RenderPassBeginInfo,
            RenderPass = _renderPass,
            Framebuffer = _framebuffer,
            RenderArea = new Rect2D(default(Offset2D), new Extent2D(padX, padY)),
            ClearValueCount = 0,
        };
        vk.CmdBeginRenderPass(cmd, in passBegin, SubpassContents.Inline);

        vk.CmdBindPipeline(cmd, PipelineBindPoint.Graphics, _pipelines[spec.Mode]);

        var viewport = new Viewport { X = 0, Y = 0, Width = padX, Height = padY, MinDepth = 0f, MaxDepth = 1f };
        var scissor = new Rect2D(default(Offset2D), new Extent2D(padX, padY));
        vk.CmdSetViewport(cmd, 0, new Viewport[] { viewport });
        vk.CmdSetScissor(cmd, 0, new Rect2D[] { scissor });

        // vUv = (x + 1) / 2 → uv = (x + 0.5) / size via pc = (2/W, 2/H, .5/W, .5/H).
        var pc = stackalloc float[] { 2f / padX, 2f / padY, .5f / padX, .5f / padY };
        vk.CmdPushConstants(cmd, _pipelineLayout, ShaderStageFlags.FragmentBit, 0, 16, pc);

        var setLocal = _descriptorSet;
        vk.CmdBindDescriptorSets(cmd, PipelineBindPoint.Graphics, _pipelineLayout, 0, 1, &setLocal, null);
        vk.CmdDraw(cmd, 3, 1, 0, 0);
        vk.CmdEndRenderPass(cmd);

        var toTransferSrc = new ImageMemoryBarrier
        {
            SType = StructureType.ImageMemoryBarrier,
            SrcAccessMask = AccessFlags.ColorAttachmentWriteBit,
            DstAccessMask = AccessFlags.TransferReadBit,
            OldLayout = ImageLayout.ColorAttachmentOptimal,
            NewLayout = ImageLayout.TransferSrcOptimal,
            SrcQueueFamilyIndex = Vk.QueueFamilyIgnored,
            DstQueueFamilyIndex = Vk.QueueFamilyIgnored,
            Image = _dstImage,
            SubresourceRange = new ImageSubresourceRange
            {
                AspectMask = ImageAspectFlags.ColorBit,
                BaseMipLevel = 0,
                LevelCount = 1,
                BaseArrayLayer = 0,
                LayerCount = 1,
            },
        };
        vk.CmdPipelineBarrier(cmd,
            PipelineStageFlags.ColorAttachmentOutputBit, PipelineStageFlags.TransferBit,
            DependencyFlags.None,
            0, null, 0, null, 1, &toTransferSrc);

        vk.CmdCopyImageToBuffer(cmd, _dstImage, ImageLayout.TransferSrcOptimal, _readbackBuffer,
            new BufferImageCopy[] { copyRegion });

        ThrowOn(vk.EndCommandBuffer(cmd), "end command buffer");

        var submitInfo = new SubmitInfo
        {
            SType = StructureType.SubmitInfo,
            CommandBufferCount = 1,
        };
        fixed (CommandBuffer* pCb = new CommandBuffer[] { cmd })
        {
            submitInfo.PCommandBuffers = pCb;
            ThrowOn(vk.QueueSubmit(_queue, new SubmitInfo[] { submitInfo }, _fence), "submit");
        }
        ThrowOn(vk.WaitForFences(_device, new Fence[] { _fence }, true, ulong.MaxValue), "wait fence");

        // Crop padX×padY → sizeX×sizeY row by row.
        rgba = new byte[sizeX * sizeY * 4];
        var strideSrc = (int)padX * 4;
        var strideDst = sizeX * 4;
        var sourceSpan = new ReadOnlySpan<byte>((void*)_readbackMapped,
            strideSrc * (sizeY - 1) + strideDst);
        for (var y = 0; y < sizeY; y++)
        {
            sourceSpan.Slice(y * strideSrc, strideDst)
                .CopyTo(rgba.AsSpan(y * strideDst, strideDst));
        }
        return true;
    }

    private static void ThrowOn(Result result, string what)
    {
        if (result != Result.Success)
            throw new InvalidOperationException($"Vulkan {what} failed: {result}");
    }

    // ─── Probe / init ─────────────────────────────────────────────────

    public static (bool Available, string Device, string Reason) Probe()
    {
        lock (_sync)
        {
            var ok = ProbeInternal();
            return (ok, ok ? _deviceName : "", _reason);
        }
    }

    private static bool ProbeInternal()
    {
        if (_probed)
            return _available;
        _probed = true;
        _reason = "";
        try
        {
            TryInit();
        }
        catch (Exception ex)
        {
            _reason = ex.Message;
        }
        if (!_available && _reason.Length == 0)
            _reason = "Vulkan init failed";
        return _available;
    }

    private static unsafe void TryInit()
    {
        var vk = Vk.GetApi();
        _vk = vk;

        var appInfo = new ApplicationInfo
        {
            SType = StructureType.ApplicationInfo,
            PApplicationName = (byte*)Marshal.StringToHGlobalAnsi("CUE4ParseCLI"),
            PEngineName = (byte*)Marshal.StringToHGlobalAnsi("CUE4ParseCLI"),
            ApplicationVersion = Vk.Version10,
            EngineVersion = Vk.Version10,
            ApiVersion = Vk.Version10,
        };
        try
        {
            var instanceInfo = new InstanceCreateInfo
            {
                SType = StructureType.InstanceCreateInfo,
                PApplicationInfo = &appInfo,
            };
            ThrowOn(vk.CreateInstance(&instanceInfo, null, out _instance), "create instance");
        }
        finally
        {
            Marshal.FreeHGlobal((IntPtr)appInfo.PApplicationName);
            Marshal.FreeHGlobal((IntPtr)appInfo.PEngineName);
        }

        Log($"probe: instance ok, devices enumerated");
        if (!SelectDevice(vk))
            return;
        if (!FormatSupportsSampling(Format.R8G8B8A8Unorm,
                FormatFeatureFlags.ColorAttachmentBit | FormatFeatureFlags.TransferSrcBit) ||
            !FormatSupportsSampling(Format.R8G8B8A8Unorm, FormatFeatureFlags.SampledImageBit))
        {
            _reason = "sampler/attachment path unsupported";
            return;
        }

        if (!FindGraphicsQueue(vk, ref _queueFamily))
        {
            _reason = "no graphics queue";
            return;
        }

        var hasAstc = FindDeviceExtension(vk, AstcLdrExtension);
        foreach (var (format, candidate) in AllFormats)
        {
            if (IsAstcFormat(format) && !hasAstc)
                continue;
            if (!FormatSupportsSampling(candidate.VkFormat,
                    FormatFeatureFlags.SampledImageBit | FormatFeatureFlags.TransferDstBit))
                continue;
            _formats[format] = candidate;
        }
        if (_formats.Count == 0)
        {
            _reason = hasAstc ? "no supported compressed formats" : "no ASTC LDR support and no BCn formats";
            return;
        }

        CreateDeviceSafe(vk, hasAstc);
        if (!_available)
        {
            if (_reason.Length == 0)
                _reason = "device init failed";
            Log($"probe: device init failed: {_reason}");
            return;
        }

        Log($"GPU texture decode active on {_deviceName} ({DescribeFormats()})");
    }

    private static string DescribeFormats()
    {
        var bc = 0;
        var astc = 0;
        foreach (var format in _formats.Keys)
        {
            if (IsAstcFormat(format)) astc++;
            else bc++;
        }
        return $"{bc} BCn formats{(astc > 0 ? $", {astc} ASTC" : "")}";
    }

    private static unsafe bool SelectDevice(Vk vk)
    {
        uint count = 0;
        ThrowOn(vk.EnumeratePhysicalDevices(_instance, &count, null), "count physical devices");
        if (count == 0)
        {
            _reason = "no Vulkan physical devices";
            return false;
        }
        var devices = new PhysicalDevice[count];
        fixed (PhysicalDevice* p = devices)
            ThrowOn(vk.EnumeratePhysicalDevices(_instance, &count, p), "enumerate physical devices");

        PhysicalDevice best = default;
        var bestScore = 0;
        var bestName = "";
        for (var i = 0; i < (int)count; i++)
        {
            vk.GetPhysicalDeviceProperties(devices[i], out var props);
            var name = Marshal.PtrToStringAnsi((IntPtr)props.DeviceName) ?? "";
            // CPU-type adapters (llvmpipe and friends) are slower than the
            // handwritten CPU decoder; require a real GPU device.
            var score = props.DeviceType switch
            {
                PhysicalDeviceType.DiscreteGpu => 2,
                PhysicalDeviceType.IntegratedGpu => 1,
                _ => 0,
            };
            if (score > bestScore)
            {
                best = devices[i];
                bestScore = score;
                bestName = name;
            }
        }
        if (bestScore <= 0)
        {
            _reason = "no Vulkan GPU devices (CPU adapters excluded: slower than CPU decode)";
            return false;
        }
        _physical = best;
        _deviceName = bestName;
        return true;
    }

    private static unsafe bool FormatSupportsSampling(Format format, FormatFeatureFlags required)
    {
        var vk = _vk!;
        vk.GetPhysicalDeviceFormatProperties(_physical, format, out var props);
        return (props.OptimalTilingFeatures & required) == required;
    }

    private static unsafe bool FindGraphicsQueue(Vk vk, ref uint queueFamily)
    {
        uint count = 0;
        vk.GetPhysicalDeviceQueueFamilyProperties(_physical, &count, null);
        if (count == 0)
            return false;
        var families = new QueueFamilyProperties[count];
        fixed (QueueFamilyProperties* p = families)
            vk.GetPhysicalDeviceQueueFamilyProperties(_physical, &count, p);
        for (var i = 0u; i < count; i++)
        {
            if ((families[i].QueueFlags & QueueFlags.GraphicsBit) != 0 && families[i].QueueCount > 0)
            {
                queueFamily = i;
                return true;
            }
        }
        return false;
    }

    private static unsafe bool FindDeviceExtension(Vk vk, string extension)
    {
        uint count = 0;
        if (vk.EnumerateDeviceExtensionProperties(_physical, (byte*)null, &count, null) != Result.Success)
            return false;
        if (count == 0)
            return false;
        var props = new ExtensionProperties[count];
        fixed (ExtensionProperties* p = props)
        {
            if (vk.EnumerateDeviceExtensionProperties(_physical, (byte*)null, &count, p) != Result.Success)
                return false;
        }
        for (var i = 0; i < (int)count; i++)
        {
            fixed (byte* namePtr = props[i].ExtensionName)
            {
                if (Marshal.PtrToStringAnsi((IntPtr)namePtr) == extension)
                    return true;
            }
        }
        return false;
    }

    private static unsafe void CreateDeviceSafe(Vk vk, bool hasAstc)
    {
        var queuePriority = 1f;
        var queueInfo = new DeviceQueueCreateInfo
        {
            SType = StructureType.DeviceQueueCreateInfo,
            QueueFamilyIndex = _queueFamily,
            QueueCount = 1,
            PQueuePriorities = &queuePriority,
        };

        var extensionNames = hasAstc ? new[] { AstcLdrExtension } : Array.Empty<string>();
        var extHandles = new IntPtr[extensionNames.Length];
        for (var i = 0; i < extensionNames.Length; i++)
            extHandles[i] = Marshal.StringToHGlobalAnsi(extensionNames[i]);
        var extPtrs = (byte**)Marshal.AllocHGlobal(sizeof(byte*) * extHandles.Length);
        for (var i = 0; i < extHandles.Length; i++)
            extPtrs[i] = (byte*)extHandles[i];
        try
        {
            var deviceInfo = new DeviceCreateInfo
            {
                SType = StructureType.DeviceCreateInfo,
                QueueCreateInfoCount = 1,
                PQueueCreateInfos = &queueInfo,
                EnabledExtensionCount = (uint)extensionNames.Length,
                PpEnabledExtensionNames = extPtrs,
            };
            ThrowOn(vk.CreateDevice(_physical, &deviceInfo, null, out _device), "create device");
        }
        finally
        {
            foreach (var h in extHandles)
                Marshal.FreeHGlobal(h);
            Marshal.FreeHGlobal((IntPtr)extPtrs);
        }

        vk.GetDeviceQueue(_device, _queueFamily, 0, out _queue);

        var poolInfo = new CommandPoolCreateInfo
        {
            SType = StructureType.CommandPoolCreateInfo,
            Flags = CommandPoolCreateFlags.ResetCommandBufferBit,
            QueueFamilyIndex = _queueFamily,
        };
        ThrowOn(vk.CreateCommandPool(_device, &poolInfo, null, out _commandPool), "create command pool");

        var cbAllocInfo = new CommandBufferAllocateInfo
        {
            SType = StructureType.CommandBufferAllocateInfo,
            CommandPool = _commandPool,
            Level = CommandBufferLevel.Primary,
            CommandBufferCount = 1,
        };
        var buffers = new CommandBuffer[1];
        fixed (CommandBuffer* pBuffers = buffers)
            ThrowOn(vk.AllocateCommandBuffers(_device, &cbAllocInfo, pBuffers), "allocate command buffer");
        _commandBuffer = buffers[0];

        var fenceInfo = new FenceCreateInfo { SType = StructureType.FenceCreateInfo };
        ThrowOn(vk.CreateFence(_device, &fenceInfo, null, out _fence), "create fence");

        var samplerInfo = new SamplerCreateInfo
        {
            SType = StructureType.SamplerCreateInfo,
            MagFilter = Filter.Nearest,
            MinFilter = Filter.Nearest,
            MipmapMode = SamplerMipmapMode.Nearest,
            AddressModeU = SamplerAddressMode.ClampToEdge,
            AddressModeV = SamplerAddressMode.ClampToEdge,
            AddressModeW = SamplerAddressMode.ClampToEdge,
            MinLod = 0,
            MaxLod = 1,
        };
        ThrowOn(vk.CreateSampler(_device, &samplerInfo, null, out _sampler), "create sampler");

        CreatePipelineSetup();

        _available = true;
    }

    private static unsafe void CreatePipelineSetup()
    {
        var vk = _vk!;

        var binding = new DescriptorSetLayoutBinding
        {
            Binding = 0,
            DescriptorType = DescriptorType.CombinedImageSampler,
            DescriptorCount = 1,
            StageFlags = ShaderStageFlags.FragmentBit,
        };
        var layoutInfo = new DescriptorSetLayoutCreateInfo
        {
            SType = StructureType.DescriptorSetLayoutCreateInfo,
            BindingCount = 1,
            PBindings = &binding,
        };
        ThrowOn(vk.CreateDescriptorSetLayout(_device, &layoutInfo, null, out _setLayout),
            "create descriptor set layout");

        var pcRange = new PushConstantRange
        {
            StageFlags = ShaderStageFlags.FragmentBit,
            Offset = 0,
            Size = 16,
        };
        var setLayoutLocal = _setLayout;
        var plInfo = new PipelineLayoutCreateInfo
        {
            SType = StructureType.PipelineLayoutCreateInfo,
            SetLayoutCount = 1,
            PSetLayouts = &setLayoutLocal,
            PushConstantRangeCount = 1,
            PPushConstantRanges = &pcRange,
        };
        ThrowOn(vk.CreatePipelineLayout(_device, &plInfo, null, out _pipelineLayout),
            "create pipeline layout");

        var attachment = new AttachmentDescription
        {
            Format = Format.R8G8B8A8Unorm,
            Samples = SampleCountFlags.SampleCount1Bit,
            LoadOp = AttachmentLoadOp.DontCare,
            StoreOp = AttachmentStoreOp.Store,
            StencilLoadOp = AttachmentLoadOp.DontCare,
            StencilStoreOp = AttachmentStoreOp.DontCare,
            InitialLayout = ImageLayout.Undefined,
            FinalLayout = ImageLayout.TransferSrcOptimal,
        };
        var colorRef = new AttachmentReference
        {
            Attachment = 0,
            Layout = ImageLayout.ColorAttachmentOptimal,
        };
        var subpass = new SubpassDescription
        {
            PipelineBindPoint = PipelineBindPoint.Graphics,
            ColorAttachmentCount = 1,
            PColorAttachments = &colorRef,
        };
        var renderPassInfo = new RenderPassCreateInfo
        {
            SType = StructureType.RenderPassCreateInfo,
            AttachmentCount = 1,
            PAttachments = &attachment,
            SubpassCount = 1,
            PSubpasses = &subpass,
        };
        ThrowOn(vk.CreateRenderPass(_device, &renderPassInfo, null, out _renderPass),
            "create render pass");

        if (!CreateShaderModule(DecodeB64(VertexBlob), out _vertModule))
            return;
        foreach (var (mode, blob) in FragmentBlobs)
        {
            if (!CreateShaderModule(DecodeB64(blob), out var module))
            {
                Log($"shader module creation failed for {mode}");
                return;
            }
            _fragModules[mode] = module;
        }

        foreach (var mode in new[] { FragMode.Pass, FragMode.SwizzleBgra, FragMode.Bc4, FragMode.Bc5, FragMode.Normal })
        {
            if (!CreateGraphicsPipeline(mode, out var pipeline))
            {
                Log($"pipeline creation failed for {mode}");
                return;
            }
            _pipelines[mode] = pipeline;
        }
    }

    private static unsafe bool CreateShaderModule(byte[] spirv, out ShaderModule module)
    {
        var vk = _vk!;
        module = default;
        var pCode = (uint*)Marshal.AllocHGlobal(spirv.Length);
        try
        {
            Marshal.Copy(spirv, 0, (IntPtr)pCode, spirv.Length);
            var info = new ShaderModuleCreateInfo
            {
                SType = StructureType.ShaderModuleCreateInfo,
                CodeSize = (nuint)spirv.Length,
                PCode = pCode,
            };
            ThrowOn(vk.CreateShaderModule(_device, in info, null, out module), "create shader module");
            return true;
        }
        finally
        {
            Marshal.FreeHGlobal((IntPtr)pCode);
        }
    }

    private static byte[] DecodeB64(string blob) => Convert.FromBase64String(blob);

    private static unsafe bool CreateGraphicsPipeline(FragMode mode, out Pipeline pipeline)
    {
        var vk = _vk!;
        pipeline = default;
        if (!_fragModules.TryGetValue(mode, out var fragModule))
            return false;

        var vertStage = new PipelineShaderStageCreateInfo
        {
            SType = StructureType.PipelineShaderStageCreateInfo,
            Stage = ShaderStageFlags.VertexBit,
            Module = _vertModule,
            PName = MainNamePtr(),
        };
        var fragStage = new PipelineShaderStageCreateInfo
        {
            SType = StructureType.PipelineShaderStageCreateInfo,
            Stage = ShaderStageFlags.FragmentBit,
            Module = fragModule,
            PName = MainNamePtr(),
        };
        var stages = new PipelineShaderStageCreateInfo[] { vertStage, fragStage };
        fixed (PipelineShaderStageCreateInfo* pStages = stages)
        {
            var vertexInput = new PipelineVertexInputStateCreateInfo
            {
                SType = StructureType.PipelineVertexInputStateCreateInfo,
                VertexBindingDescriptionCount = 0,
                VertexAttributeDescriptionCount = 0,
            };
            var inputAssembly = new PipelineInputAssemblyStateCreateInfo
            {
                SType = StructureType.PipelineInputAssemblyStateCreateInfo,
                Topology = PrimitiveTopology.TriangleList,
                PrimitiveRestartEnable = false,
            };
            var rasterization = new PipelineRasterizationStateCreateInfo
            {
                SType = StructureType.PipelineRasterizationStateCreateInfo,
                PolygonMode = PolygonMode.Fill,
                CullMode = CullModeFlags.None,
                FrontFace = FrontFace.CounterClockwise,
                LineWidth = 1f,
            };
            var multisample = new PipelineMultisampleStateCreateInfo
            {
                SType = StructureType.PipelineMultisampleStateCreateInfo,
                RasterizationSamples = SampleCountFlags.SampleCount1Bit,
                SampleShadingEnable = false,
            };
            var blendAttachment = new PipelineColorBlendAttachmentState
            {
                BlendEnable = false,
                ColorWriteMask = ColorComponentFlags.RBit | ColorComponentFlags.GBit
                    | ColorComponentFlags.BBit | ColorComponentFlags.ABit,
            };
            var colorBlend = new PipelineColorBlendStateCreateInfo
            {
                SType = StructureType.PipelineColorBlendStateCreateInfo,
                LogicOpEnable = false,
                AttachmentCount = 1,
                PAttachments = &blendAttachment,
            };
            var dynamicState = stackalloc DynamicState[]
            {
                DynamicState.Viewport, DynamicState.Scissor,
            };
            var dynamicInfo = new PipelineDynamicStateCreateInfo
            {
                SType = StructureType.PipelineDynamicStateCreateInfo,
                DynamicStateCount = 2,
                PDynamicStates = dynamicState,
            };
            var viewportState = new PipelineViewportStateCreateInfo
            {
                SType = StructureType.PipelineViewportStateCreateInfo,
                ViewportCount = 1,
                ScissorCount = 1,
            };
            var info = new GraphicsPipelineCreateInfo
            {
                SType = StructureType.GraphicsPipelineCreateInfo,
                StageCount = 2,
                PStages = pStages,
                PVertexInputState = &vertexInput,
                PInputAssemblyState = &inputAssembly,
                PViewportState = &viewportState,
                PRasterizationState = &rasterization,
                PMultisampleState = &multisample,
                PDepthStencilState = null,
                PColorBlendState = &colorBlend,
                PDynamicState = &dynamicInfo,
                Layout = _pipelineLayout,
                RenderPass = _renderPass,
                Subpass = 0,
            };
            var pInfos = stackalloc GraphicsPipelineCreateInfo[] { info };
            ThrowOn(vk.CreateGraphicsPipelines(_device, default, 1, pInfos, null, out pipeline),
                $"create pipeline {mode}");
        }
        return true;
    }

    private static readonly GCHandle MainNameHandle =
        GCHandle.Alloc(Encoding.ASCII.GetBytes("main\0"), GCHandleType.Pinned);

    private static unsafe byte* MainNamePtr() => (byte*)MainNameHandle.AddrOfPinnedObject();

    // ─── Scratch resources ────────────────────────────────────────────

    private static unsafe bool EnsureScratchBuffers(ulong stagingSize, ulong readbackSize)
    {
        var vk = _vk!;
        if (_stagingCapacity < stagingSize)
        {
            if (_stagingMapped != IntPtr.Zero)
                vk.UnmapMemory(_device, _stagingMemory);
            if (_stagingBuffer.Handle != 0)
                vk.DestroyBuffer(_device, _stagingBuffer, null);
            if (_stagingMemory.Handle != 0)
                vk.FreeMemory(_device, _stagingMemory, null);
            var needed = stagingSize + 4096;
            if (!CreateMappedBuffer(needed, MemoryPropertyFlags.HostVisibleBit | MemoryPropertyFlags.HostCoherentBit,
                    out _stagingBuffer, out _stagingMemory, out _stagingMapped))
                return false;
            _stagingCapacity = needed;
        }

        if (_readbackCapacity < readbackSize)
        {
            if (_readbackMapped != IntPtr.Zero)
                vk.UnmapMemory(_device, _readbackMemory);
            if (_readbackBuffer.Handle != 0)
                vk.DestroyBuffer(_device, _readbackBuffer, null);
            if (_readbackMemory.Handle != 0)
                vk.FreeMemory(_device, _readbackMemory, null);
            var needed = readbackSize + 4096;
            if (!CreateMappedBuffer(needed, MemoryPropertyFlags.HostVisibleBit | MemoryPropertyFlags.HostCoherentBit,
                    out _readbackBuffer, out _readbackMemory, out _readbackMapped))
                return false;
            _readbackCapacity = needed;
        }
        return true;
    }

    private static unsafe bool CreateMappedBuffer(ulong size, MemoryPropertyFlags properties,
        out VkBuffer buffer, out DeviceMemory memory, out IntPtr mapped)
    {
        var vk = _vk!;
        buffer = default;
        memory = default;
        mapped = IntPtr.Zero;
        var info = new BufferCreateInfo
        {
            SType = StructureType.BufferCreateInfo,
            Size = size,
            Usage = BufferUsageFlags.TransferSrcBit | BufferUsageFlags.TransferDstBit,
            SharingMode = SharingMode.Exclusive,
        };
        ThrowOn(vk.CreateBuffer(_device, &info, null, out buffer), "create scratch buffer");

        vk.GetBufferMemoryRequirements(_device, buffer, out var reqs);
        var typeIndex = FindMemoryType(reqs.MemoryTypeBits, properties);
        if (typeIndex < 0)
            throw new InvalidOperationException("no suitable host memory type");
        var allocInfo = new MemoryAllocateInfo
        {
            SType = StructureType.MemoryAllocateInfo,
            AllocationSize = reqs.Size,
            MemoryTypeIndex = (uint)typeIndex,
        };
        ThrowOn(vk.AllocateMemory(_device, &allocInfo, null, out memory), "allocate scratch memory");
        ThrowOn(vk.BindBufferMemory(_device, buffer, memory, 0), "bind scratch buffer");
        void* mappedPtr;
        ThrowOn(vk.MapMemory(_device, memory, 0, Vk.WholeSize, 0, &mappedPtr), "map scratch memory");
        mapped = (nint)mappedPtr;
        return true;
    }

    private static int FindMemoryType(uint typeBits, MemoryPropertyFlags properties)
    {
        var vk = _vk!;
        vk.GetPhysicalDeviceMemoryProperties(_physical, out var memProps);
        for (var i = 0; i < memProps.MemoryTypeCount; i++)
        {
            if ((typeBits & (1u << i)) == 0)
                continue;
            if ((memProps.MemoryTypes[i].PropertyFlags & properties) == properties)
                return i;
        }
        return -1;
    }

    private static unsafe bool CreateImage(Format format, uint width, uint height,
        ImageUsageFlags usage, out Image image, out DeviceMemory memory)
    {
        var vk = _vk!;
        image = default;
        memory = default;
        var info = new ImageCreateInfo
        {
            SType = StructureType.ImageCreateInfo,
            ImageType = ImageType.ImageType2D,
            Format = format,
            Extent = new Extent3D(width, height, 1),
            MipLevels = 1,
            ArrayLayers = 1,
            Samples = SampleCountFlags.SampleCount1Bit,
            Tiling = ImageTiling.Optimal,
            Usage = usage,
            SharingMode = SharingMode.Exclusive,
            InitialLayout = ImageLayout.Undefined,
        };
        ThrowOn(vk.CreateImage(_device, &info, null, out image), $"create image {format}");

        vk.GetImageMemoryRequirements(_device, image, out var reqs);
        var typeIndex = FindMemoryType(reqs.MemoryTypeBits, MemoryPropertyFlags.DeviceLocalBit);
        if (typeIndex < 0)
            throw new InvalidOperationException("no device-local memory type");
        var allocInfo = new MemoryAllocateInfo
        {
            SType = StructureType.MemoryAllocateInfo,
            AllocationSize = reqs.Size,
            MemoryTypeIndex = (uint)typeIndex,
        };
        ThrowOn(vk.AllocateMemory(_device, &allocInfo, null, out memory), "allocate image memory");
        ThrowOn(vk.BindImageMemory(_device, image, memory, 0), "bind image memory");
        return true;
    }

    private static unsafe bool CreateImageView(Image image, Format format, out ImageView view)
    {
        var vk = _vk!;
        var info = new ImageViewCreateInfo
        {
            SType = StructureType.ImageViewCreateInfo,
            Image = image,
            ViewType = ImageViewType.ImageViewType2D,
            Format = format,
            Components = new ComponentMapping(
                ComponentSwizzle.Identity, ComponentSwizzle.Identity,
                ComponentSwizzle.Identity, ComponentSwizzle.Identity),
            SubresourceRange = new ImageSubresourceRange
            {
                AspectMask = ImageAspectFlags.ColorBit,
                BaseMipLevel = 0,
                LevelCount = 1,
                BaseArrayLayer = 0,
                LayerCount = 1,
            },
        };
        ThrowOn(vk.CreateImageView(_device, &info, null, out view), "create image view");
        return true;
    }

    private static unsafe bool CreateFramebuffer()
    {
        var vk = _vk!;
        var dstViewLocal = _dstView;
        var info = new FramebufferCreateInfo
        {
            SType = StructureType.FramebufferCreateInfo,
            RenderPass = _renderPass,
            AttachmentCount = 1,
            PAttachments = &dstViewLocal,
            Width = _dstWidth,
            Height = _dstHeight,
            Layers = 1,
        };
        ThrowOn(vk.CreateFramebuffer(_device, &info, null, out _framebuffer), "create framebuffer");
        return true;
    }

    private static unsafe bool BindDescriptor()
    {
        var vk = _vk!;
        if (_descriptorPool.Handle == 0)
        {
            var poolSize = new DescriptorPoolSize
            {
                Type = DescriptorType.CombinedImageSampler,
                DescriptorCount = 4,
            };
            var poolInfo = new DescriptorPoolCreateInfo
            {
                SType = StructureType.DescriptorPoolCreateInfo,
                MaxSets = 4,
                PoolSizeCount = 1,
                PPoolSizes = &poolSize,
            };
            ThrowOn(vk.CreateDescriptorPool(_device, &poolInfo, null, out _descriptorPool), "create descriptor pool");
        }
        else
        {
            // The bound image may have changed (BindDescriptor re-runs whenever
            // the source-scratch image is rebuilt); recycle the single slot
            // instead of growing Leaking the pool one set per texture.
            ThrowOn(vk.ResetDescriptorPool(_device, _descriptorPool, 0), "reset descriptor pool");
        }

        var setLayoutLocal = _setLayout;
        var setImage = new DescriptorSetAllocateInfo
        {
            SType = StructureType.DescriptorSetAllocateInfo,
            DescriptorPool = _descriptorPool,
            DescriptorSetCount = 1,
            PSetLayouts = &setLayoutLocal,
        };
        ThrowOn(vk.AllocateDescriptorSets(_device, &setImage, out _descriptorSet), "allocate descriptor set");

        var imageInfo = new DescriptorImageInfo
        {
            Sampler = _sampler,
            ImageView = _srcView,
            ImageLayout = ImageLayout.ShaderReadOnlyOptimal,
        };
        var write = new WriteDescriptorSet
        {
            SType = StructureType.WriteDescriptorSet,
            DstSet = _descriptorSet,
            DstBinding = 0,
            DescriptorCount = 1,
            DescriptorType = DescriptorType.CombinedImageSampler,
            PImageInfo = &imageInfo,
        };
        vk.UpdateDescriptorSets(_device, 1, &write, 0, null);
        _descriptorBound = true;
        return true;
    }

    private static unsafe void DestroySourceResources()
    {
        var vk = _vk!;
        _descriptorBound = false;
        if (_srcView.Handle != 0)
            vk.DestroyImageView(_device, _srcView, null);
        if (_srcImage.Handle != 0)
            vk.DestroyImage(_device, _srcImage, null);
        if (_srcMemory.Handle != 0)
            vk.FreeMemory(_device, _srcMemory, null);
        _srcView = default;
        _srcImage = default;
        _srcMemory = default;
        _srcWidth = 0;
        _srcHeight = 0;
        _srcFormat = default;
    }

    private static unsafe void DestroyTargetResources()
    {
        var vk = _vk!;
        if (_framebuffer.Handle != 0)
            vk.DestroyFramebuffer(_device, _framebuffer, null);
        if (_dstView.Handle != 0)
            vk.DestroyImageView(_device, _dstView, null);
        if (_dstImage.Handle != 0)
            vk.DestroyImage(_device, _dstImage, null);
        if (_dstMemory.Handle != 0)
            vk.FreeMemory(_device, _dstMemory, null);
        _framebuffer = default;
        _dstView = default;
        _dstImage = default;
        _dstMemory = default;
        _dstWidth = 0;
        _dstHeight = 0;
    }

    private static void Log(string message)
    {
        // stderr so the NDJSON stdout channel stays clean; the GUI logs stderr.
        Console.Error.WriteLine($"[gpu] {message}");
    }
}

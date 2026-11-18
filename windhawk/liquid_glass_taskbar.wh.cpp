// ==WindhawkMod==
// @id              liquid-glass-taskbar-icons
// @name            Liquid Glass Taskbar Icons
// @description     Universal real-time optical Liquid Glass transformation for Windows 11 taskbar icons
// @version         1.0.0
// @author          LiquidGlassIconCenter
// @include         explorer.exe
// @architecture    x86-64
// @compilerOptions -lole32 -loleaut32 -lruntimeobject -lshcore -lwindowscodecs
// ==/WindhawkMod==

// ==WindhawkModReadme==
/*
# Liquid Glass Taskbar Icons

Applies real-time optical Liquid Glass rendering (zero black/gray visible RGB,
translucent colored glass, frosted highlights, directional inner bevel rim and
seam glints) to Windows 11 Taskbar buttons without touching `.lnk` files or
leaving temporary icon files on disk.
*/
// ==/WindhawkModReadme==

// ==WindhawkModSettings==
/*
- Enabled: 1
  $name: Enable Liquid Glass
- Opacity: 100
  $name: Colored Glass Body Opacity (%)
- Frost: 100
  $name: Frost Highlight Intensity (%)
- Glow: 100
  $name: Rim & Seam Glow (%)
- DarkProtect: 100
  $name: Dark Logo Floor Protection (%)
*/
// ==/WindhawkModSettings==

#include <windhawk_utils.h>

#undef GetCurrentTime

#include <windows.h>
#include <objbase.h>
#include <robuffer.h>
#include <shcore.h>
#include <wincodec.h>

#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <list>
#include <memory>
#include <mutex>
#include <string>
#include <unordered_map>
#include <vector>

#include <winrt/Windows.Foundation.Collections.h>
#include <winrt/Windows.Foundation.h>
#include <winrt/Windows.Storage.Streams.h>
#include <winrt/Windows.UI.Core.h>
#include <winrt/Windows.UI.Xaml.Controls.h>
#include <winrt/Windows.UI.Xaml.Media.Imaging.h>
#include <winrt/Windows.UI.Xaml.Media.h>
#include <winrt/Windows.UI.Xaml.h>
#include <winrt/base.h>

namespace wf = winrt::Windows::Foundation;
namespace wss = winrt::Windows::Storage::Streams;
namespace wuc = winrt::Windows::UI::Core;
namespace wux = winrt::Windows::UI::Xaml;
namespace wuxc = winrt::Windows::UI::Xaml::Controls;
namespace wuxm = winrt::Windows::UI::Xaml::Media;
namespace wuxmi = winrt::Windows::UI::Xaml::Media::Imaging;

namespace {

constexpr size_t kMaxCacheEntries = 64;
constexpr size_t kMaxCacheBytes = 12ull * 1024ull * 1024ull;
constexpr int kWorkSize = 256;
constexpr double kPi = 3.14159265358979323846;

struct OpticalSettings {
    bool enabled = true;
    int opacityPct = 100;
    int frostPct = 100;
    int glowPct = 100;
    int darkProtectPct = 100;
    uint32_t generation = 1;
};

struct RawImage {
    uint32_t width = 0;
    uint32_t height = 0;
    std::vector<uint8_t> bgra;  // straight BGRA, width * height * 4
};

struct CacheKey {
    uint64_t srcHash = 0;
    uint32_t width = 0;
    uint32_t height = 0;
    uint32_t generation = 0;

    bool operator==(const CacheKey& o) const noexcept {
        return srcHash == o.srcHash && width == o.width &&
               height == o.height && generation == o.generation;
    }
};

struct CacheKeyHash {
    size_t operator()(const CacheKey& k) const noexcept {
        uint64_t h = k.srcHash;
        h ^= (static_cast<uint64_t>(k.width) << 32) | k.height;
        h *= 0x9E3779B97F4A7C15ULL;
        h ^= k.generation;
        return static_cast<size_t>(h);
    }
};

struct CacheEntry {
    CacheKey key;
    std::vector<uint8_t> pngBytes;
    uint64_t pngHash = 0;
};

struct ButtonRecord {
    winrt::weak_ref<wux::FrameworkElement> weakFe;
    std::vector<uint8_t> origStreamBytes;
    uint64_t origStreamHash = 0;
    uint64_t lastGlassPngHash = 0;
    uint32_t appliedGeneration = 0;
    uint32_t lastWidth = 0;
    uint32_t lastHeight = 0;
    bool captureInFlight = false;
    uint32_t captureAttemptedGen = 0;
};

std::mutex g_stateMutex;
OpticalSettings g_settings;
std::atomic<bool> g_unloading{false};
std::atomic<uint32_t> g_settingsGeneration{1};
std::atomic<int> g_pendingAsyncOps{0};

// Cached COM identity & metadata (verified dynamically at runtime).
std::atomic<bool> g_hasTaskListButtonIid{false};
GUID g_iidTaskListButton{};

// Tracked taskbar UI frames and live button records (accessed cleanly from UI thread or locked).
std::vector<winrt::weak_ref<wux::FrameworkElement>> g_taskbarFrames;
std::unordered_map<void*, ButtonRecord> g_buttons;  // key = canonical IUnknown* pointer (weak-checked before use)

// Pure-RAM LRU Cache (capped at 64 entries AND 32 MiB total).
std::list<CacheEntry> g_lruList;
std::unordered_map<CacheKey, std::list<CacheEntry>::iterator, CacheKeyHash> g_lruMap;
size_t g_lruTotalBytes = 0;

// Telemetry counters (written atomically to %TEMP%\lgic_windhawk_status.json for APP UI).
std::atomic<uint64_t> g_renderCount{0};
std::atomic<uint64_t> g_cacheHits{0};
std::atomic<uint64_t> g_lowResCount{0};
std::atomic<uint64_t> g_errorCount{0};
std::atomic<uint64_t> g_lastStatusFlushTick{0};
std::atomic<uint32_t> g_lastSweepGeneration{0};
std::atomic<size_t> g_sweptFrameCount{0};
std::atomic<size_t> g_targetTaskbarCount{1};
std::atomic<uint64_t> g_diagUpdFhHits{0};
std::atomic<uint64_t> g_diagNudgeCalls{0};
std::atomic<uint64_t> g_diagButtonsSeen{0};
std::atomic<uint64_t> g_diagQiFail{0};
std::atomic<uint64_t> g_diagGetIconOk{0};
std::atomic<uint64_t> g_diagRtbQueued{0};
std::atomic<uint64_t> g_diagRtbStarted{0};
std::atomic<uint64_t> g_diagRtbPixQueued{0};
std::atomic<uint64_t> g_diagRtbPixDone{0};
std::atomic<uint64_t> g_diagRtbOk{0};
std::atomic<int32_t> g_diagRtbHr{0};
std::atomic<uint32_t> g_diagRtbFail{0};
std::atomic<int32_t> g_diagRtbLastTarget{0};
std::atomic<int32_t> g_diagRtbLastW{0};
std::atomic<int32_t> g_diagRtbLastH{0};
std::atomic<uint64_t> g_diagRtbAlphaPx{0};
std::atomic<uint32_t> g_diagNudgeSent{0};
struct PixKeepEntry {
    uint64_t token;
    wf::IAsyncOperation<wss::IBuffer> op;
};
std::mutex g_pixKeepMutex;
std::vector<PixKeepEntry> g_pixKeepAlive;
std::wstring g_statusPath;
std::mutex g_statusMutex;

struct FrameEntry {
    winrt::weak_ref<wux::FrameworkElement> frame;
    uint32_t sweptGen = 0;
};
std::vector<FrameEntry> g_frameEntries;

thread_local bool t_inPutIconOverride = false;
thread_local winrt::com_ptr<IWICImagingFactory> t_wicFactory;

// Hooked / resolved function pointers.
using TaskListButton_put_Icon_t = int(WINAPI*)(void* pThis, void* value);
TaskListButton_put_Icon_t TaskListButton_put_Icon_Original = nullptr;

using TaskListButton_get_Icon_t = int(WINAPI*)(void* pThis, void** outValue);
TaskListButton_get_Icon_t TaskListButton_get_Icon_Raw = nullptr;

using TaskListButton_OnApplyTemplate_t = int(WINAPI*)(void* pThis);
TaskListButton_OnApplyTemplate_t TaskListButton_OnApplyTemplate_Original = nullptr;

using TaskListButton_put_IsActive_t = int(WINAPI*)(void* pThis, bool value);
TaskListButton_put_IsActive_t TaskListButton_put_IsActive_Original = nullptr;

using FrameElement_MeasureOverride_t = int(WINAPI*)(void* pThis, uint64_t availSizeBits, void* pReturnSize);
FrameElement_MeasureOverride_t TaskbarFrame_MeasureOverride_Original = nullptr;

using FrameElement_ArrangeOverride_t = int(WINAPI*)(void* pThis, uint64_t finalSizeBits, void* pReturnSize);
FrameElement_ArrangeOverride_t TaskListButton_ArrangeOverride_Original = nullptr;

void* TaskbarController_OnGroupingModeChanged_Raw = nullptr;
using TaskbarController_UpdateFrameHeight_t = void(WINAPI*)(void* pThis);
TaskbarController_UpdateFrameHeight_t TaskbarController_UpdateFrameHeight_Original = nullptr;

// ---------------------------------------------------------------------------
// Utility & Hashing
// ---------------------------------------------------------------------------

inline uint64_t Fnv1a64(const uint8_t* data, size_t len) noexcept {
    uint64_t h = 14695981039346656037ULL;
    for (size_t i = 0; i < len; ++i) {
        h ^= data[i];
        h *= 1099511628211ULL;
    }
    return h;
}

inline float Clamp01(float v) noexcept {
    return v < 0.0f ? 0.0f : (v > 1.0f ? 1.0f : v);
}

inline float Smoothstep(float e0, float e1, float x) noexcept {
    float denom = e1 - e0;
    if (denom < 1e-6f) denom = 1e-6f;
    float t = Clamp01((x - e0) / denom);
    return t * t * (3.0f - 2.0f * t);
}

void InitStatusPath() {
    wchar_t tempDir[MAX_PATH]{};
    DWORD n = GetTempPathW(ARRAYSIZE(tempDir), tempDir);
    if (n > 0 && n < ARRAYSIZE(tempDir)) {
        g_statusPath = std::wstring(tempDir) + L"lgic_windhawk_status.json";
    } else {
        g_statusPath = L"C:\\Windows\\Temp\\lgic_windhawk_status.json";
    }
}

void FlushStatusFile(bool force) {
    uint64_t now = GetTickCount64();
    uint64_t prev = g_lastStatusFlushTick.load(std::memory_order_relaxed);
    if (!force && (now - prev) < 1000) {
        return;
    }
    g_lastStatusFlushTick.store(now, std::memory_order_relaxed);

    size_t btnCount = 0;
    size_t cacheEntries = 0;
    size_t cacheBytes = 0;
    OpticalSettings snap{};
    {
        std::lock_guard<std::mutex> lock(g_stateMutex);
        btnCount = g_buttons.size();
        cacheEntries = g_lruList.size();
        cacheBytes = g_lruTotalBytes;
        snap = g_settings;
    }

    char json[768];
    int len = _snprintf_s(
        json, sizeof(json), _TRUNCATE,
        "{\"active\":%s,\"unloading\":%s,\"pid\":%lu,\"generation\":%u,"
        "\"tracked_buttons\":%zu,\"cache_entries\":%zu,\"cache_bytes\":%zu,"
        "\"max_cache_entries\":%zu,\"max_cache_bytes\":%zu,"
        "\"render_count\":%llu,\"cache_hits\":%llu,\"low_res_count\":%llu,\"error_count\":%llu,"
        "\"swept_frames\":%zu,\"upd_fh_hits\":%llu,\"nudge_calls\":%llu,"
        "\"btns_seen\":%llu,\"qi_fail\":%llu,\"get_icon_ok\":%llu,"
        "\"rtb_queued\":%llu,\"rtb_started\":%llu,\"pq\":%llu,\"pd\":%llu,\"rtb_ok\":%llu,\"rtb_hr\":%d,"
        "\"rtb_fail\":%u,\"rtb_tgt\":%d,\"rtb_w\":%d,\"rtb_h\":%d,\"rtb_alpha\":%llu,"
        "\"pending_async\":%d,\"diag_nudge_sent\":%u,\"has_iid\":%s,"
        "\"opacity\":%d,\"frost\":%d,\"glow\":%d,\"dark_protect\":%d}",
        (snap.enabled && !g_unloading.load()) ? "true" : "false",
        g_unloading.load() ? "true" : "false",
        GetCurrentProcessId(),
        snap.generation,
        btnCount, cacheEntries, cacheBytes,
        kMaxCacheEntries, kMaxCacheBytes,
        static_cast<unsigned long long>(g_renderCount.load()),
        static_cast<unsigned long long>(g_cacheHits.load()),
        static_cast<unsigned long long>(g_lowResCount.load()),
        static_cast<unsigned long long>(g_errorCount.load()),
        g_sweptFrameCount.load(std::memory_order_acquire),
        static_cast<unsigned long long>(g_diagUpdFhHits.load()),
        static_cast<unsigned long long>(g_diagNudgeCalls.load()),
        static_cast<unsigned long long>(g_diagButtonsSeen.load()),
        static_cast<unsigned long long>(g_diagQiFail.load()),
        static_cast<unsigned long long>(g_diagGetIconOk.load()),
        static_cast<unsigned long long>(g_diagRtbQueued.load()),
        static_cast<unsigned long long>(g_diagRtbStarted.load()),
        static_cast<unsigned long long>(g_diagRtbPixQueued.load()),
        static_cast<unsigned long long>(g_diagRtbPixDone.load()),
        static_cast<unsigned long long>(g_diagRtbOk.load()),
        g_diagRtbHr.load(std::memory_order_relaxed),
        g_diagRtbFail.load(std::memory_order_relaxed),
        g_diagRtbLastTarget.load(std::memory_order_relaxed),
        g_diagRtbLastW.load(std::memory_order_relaxed),
        g_diagRtbLastH.load(std::memory_order_relaxed),
        static_cast<unsigned long long>(g_diagRtbAlphaPx.load(std::memory_order_relaxed)),
        g_pendingAsyncOps.load(std::memory_order_acquire),
        g_diagNudgeSent.load(std::memory_order_relaxed),
        g_hasTaskListButtonIid.load() ? "true" : "false",
        snap.opacityPct, snap.frostPct, snap.glowPct, snap.darkProtectPct);
    if (len <= 0) {
        return;
    }

    std::lock_guard<std::mutex> slock(g_statusMutex);
    if (g_statusPath.empty()) {
        InitStatusPath();
    }
    std::wstring tmpPath = g_statusPath + L".tmp";
    HANDLE h = CreateFileW(
        tmpPath.c_str(), GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr,
        CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h != INVALID_HANDLE_VALUE) {
        DWORD written = 0;
        WriteFile(h, json, static_cast<DWORD>(len), &written, nullptr);
        CloseHandle(h);
        MoveFileExW(tmpPath.c_str(), g_statusPath.c_str(), MOVEFILE_REPLACE_EXISTING);
    }
}

int ReadIntSettingDefault(PCWSTR name, int defVal, int lo, int hi) {
    WindhawkUtils::StringSetting s = WindhawkUtils::StringSetting::make(name);
    if (!s.get() || s.get()[0] == L'\0') {
        int iv = Wh_GetIntSetting(name);
        if (iv == 0) {
            return defVal;
        }
        return std::clamp(iv, lo, hi);
    }
    int iv = _wtoi(s.get());
    return std::clamp(iv, lo, hi);
}

bool ReadBoolSettingDefaultTrue(PCWSTR name) {
    WindhawkUtils::StringSetting s = WindhawkUtils::StringSetting::make(name);
    if (!s.get() || s.get()[0] == L'\0') {
        return true;
    }
    return _wtoi(s.get()) != 0;
}

void PurgeStaleLruLocked(uint32_t keepGen) {
    for (auto it = g_lruList.begin(); it != g_lruList.end();) {
        if (it->key.generation != keepGen) {
            g_lruTotalBytes -= it->pngBytes.size();
            g_lruMap.erase(it->key);
            it = g_lruList.erase(it);
        } else {
            ++it;
        }
    }
}

void LoadSettings() {
    std::lock_guard<std::mutex> lock(g_stateMutex);
    g_settings.enabled = ReadBoolSettingDefaultTrue(L"Enabled");
    g_settings.opacityPct = ReadIntSettingDefault(L"Opacity", 100, 20, 180);
    g_settings.frostPct = ReadIntSettingDefault(L"Frost", 100, 20, 130);
    g_settings.glowPct = ReadIntSettingDefault(L"Glow", 100, 30, 180);
    g_settings.darkProtectPct = ReadIntSettingDefault(L"DarkProtect", 100, 50, 200);
    g_settings.generation = g_settingsGeneration.fetch_add(1) + 1;
    PurgeStaleLruLocked(g_settings.generation);
    g_sweptFrameCount.store(0, std::memory_order_release);
}

// ---------------------------------------------------------------------------
// WIC Stream Helpers (100% Synchronous COM, Zero Async .get())
// ---------------------------------------------------------------------------

IWICImagingFactory* GetThreadWicFactory() {
    if (!t_wicFactory) {
        CoCreateInstance(
            CLSID_WICImagingFactory, nullptr, CLSCTX_INPROC_SERVER,
            IID_PPV_ARGS(t_wicFactory.put()));
    }
    return t_wicFactory.get();
}

bool ReadStreamToBytes(IUnknown* rasUnknown, std::vector<uint8_t>& outBytes) {
    outBytes.clear();
    if (!rasUnknown) {
        return false;
    }
    try {
        wss::IRandomAccessStream ras{nullptr};
        if (FAILED(rasUnknown->QueryInterface(
                winrt::guid_of<wss::IRandomAccessStream>(), winrt::put_abi(ras))) ||
            !ras) {
            return false;
        }
        wss::IRandomAccessStream cloned = ras.CloneStream();
        if (!cloned) {
            cloned = ras;
        }
        uint64_t sz = cloned.Size();
        if (sz == 0 || sz > 16ull * 1024ull * 1024ull) {
            return false;
        }
        winrt::com_ptr<IStream> comStream;
        if (FAILED(CreateStreamOverRandomAccessStream(
                reinterpret_cast<IUnknown*>(winrt::get_abi(cloned)),
                IID_PPV_ARGS(comStream.put()))) ||
            !comStream) {
            return false;
        }
        LARGE_INTEGER zero{};
        comStream->Seek(zero, STREAM_SEEK_SET, nullptr);
        outBytes.resize(static_cast<size_t>(sz));
        size_t offset = 0;
        while (offset < outBytes.size()) {
            ULONG chunk = static_cast<ULONG>(std::min<size_t>(outBytes.size() - offset, 65536));
            ULONG readNow = 0;
            HRESULT hr = comStream->Read(outBytes.data() + offset, chunk, &readNow);
            if (FAILED(hr) || readNow == 0) {
                break;
            }
            offset += readNow;
        }
        outBytes.resize(offset);
        return !outBytes.empty();
    } catch (...) {
        return false;
    }
}

bool BytesToRandomAccessStream(const std::vector<uint8_t>& bytes,
                               wss::IRandomAccessStream& outRas) {
    outRas = nullptr;
    if (bytes.empty()) {
        return false;
    }
    HGLOBAL hMem = GlobalAlloc(GMEM_MOVEABLE, bytes.size());
    if (!hMem) {
        return false;
    }
    void* locked = GlobalLock(hMem);
    if (!locked) {
        GlobalFree(hMem);
        return false;
    }
    std::memcpy(locked, bytes.data(), bytes.size());
    GlobalUnlock(hMem);

    winrt::com_ptr<IStream> memStream;
    if (FAILED(CreateStreamOnHGlobal(hMem, TRUE, memStream.put())) || !memStream) {
        GlobalFree(hMem);
        return false;
    }
    HRESULT hr = CreateRandomAccessStreamOverStream(
        memStream.get(), BSOS_DEFAULT,
        winrt::guid_of<wss::IRandomAccessStream>(),
        winrt::put_abi(outRas));
    return SUCCEEDED(hr) && outRas != nullptr;
}

bool DecodeBytesToRawBgra(const std::vector<uint8_t>& bytes, RawImage& outImg) {
    outImg = {};
    IWICImagingFactory* wic = GetThreadWicFactory();
    if (!wic || bytes.empty()) {
        return false;
    }
    winrt::com_ptr<IWICStream> wicStream;
    if (FAILED(wic->CreateStream(wicStream.put())) || !wicStream) {
        return false;
    }
    if (FAILED(wicStream->InitializeFromMemory(
            const_cast<BYTE*>(bytes.data()), static_cast<DWORD>(bytes.size())))) {
        return false;
    }
    winrt::com_ptr<IWICBitmapDecoder> decoder;
    if (FAILED(wic->CreateDecoderFromStream(
            wicStream.get(), nullptr, WICDecodeMetadataCacheOnLoad, decoder.put())) ||
        !decoder) {
        return false;
    }
    UINT frameCount = 0;
    if (FAILED(decoder->GetFrameCount(&frameCount)) || frameCount == 0) {
        return false;
    }
    // If multi-frame (e.g. ICO), pick the largest frame cleanly without upscaling small frames.
    winrt::com_ptr<IWICBitmapFrameDecode> bestFrame;
    UINT bestW = 0, bestH = 0;
    for (UINT i = 0; i < frameCount; ++i) {
        winrt::com_ptr<IWICBitmapFrameDecode> fr;
        if (SUCCEEDED(decoder->GetFrame(i, fr.put())) && fr) {
            UINT w = 0, h = 0;
            if (SUCCEEDED(fr->GetSize(&w, &h)) && (w * h >= bestW * bestH)) {
                bestW = w;
                bestH = h;
                bestFrame = fr;
            }
        }
    }
    if (!bestFrame || bestW == 0 || bestH == 0 || bestW > 2048 || bestH > 2048) {
        return false;
    }

    winrt::com_ptr<IWICFormatConverter> converter;
    if (FAILED(wic->CreateFormatConverter(converter.put())) || !converter) {
        return false;
    }
    if (FAILED(converter->Initialize(
            bestFrame.get(), GUID_WICPixelFormat32bppBGRA,
            WICBitmapDitherTypeNone, nullptr, 0.0, WICBitmapPaletteTypeCustom))) {
        return false;
    }

    outImg.width = bestW;
    outImg.height = bestH;
    outImg.bgra.resize(static_cast<size_t>(bestW) * bestH * 4);
    HRESULT copyHr = converter->CopyPixels(
        nullptr, bestW * 4, static_cast<UINT>(outImg.bgra.size()), outImg.bgra.data());
    return SUCCEEDED(copyHr);
}

bool EncodeRawBgraToPng(const RawImage& img, std::vector<uint8_t>& outPng) {
    outPng.clear();
    IWICImagingFactory* wic = GetThreadWicFactory();
    if (!wic || img.width == 0 || img.height == 0 || img.bgra.empty()) {
        return false;
    }
    winrt::com_ptr<IStream> comStream;
    if (FAILED(CreateStreamOnHGlobal(nullptr, TRUE, comStream.put())) || !comStream) {
        return false;
    }
    winrt::com_ptr<IWICBitmapEncoder> encoder;
    if (FAILED(wic->CreateEncoder(GUID_ContainerFormatPng, nullptr, encoder.put())) || !encoder) {
        return false;
    }
    if (FAILED(encoder->Initialize(comStream.get(), WICBitmapEncoderNoCache))) {
        return false;
    }
    winrt::com_ptr<IWICBitmapFrameEncode> frame;
    if (FAILED(encoder->CreateNewFrame(frame.put(), nullptr)) || !frame) {
        return false;
    }
    if (FAILED(frame->Initialize(nullptr))) {
        return false;
    }
    if (FAILED(frame->SetSize(img.width, img.height))) {
        return false;
    }
    WICPixelFormatGUID fmt = GUID_WICPixelFormat32bppBGRA;
    if (FAILED(frame->SetPixelFormat(&fmt))) {
        return false;
    }
    if (FAILED(frame->WritePixels(
            img.height, img.width * 4,
            static_cast<UINT>(img.bgra.size()),
            const_cast<BYTE*>(img.bgra.data())))) {
        return false;
    }
    if (FAILED(frame->Commit()) || FAILED(encoder->Commit())) {
        return false;
    }

    STATSTG stat{};
    if (FAILED(comStream->Stat(&stat, STATFLAG_NONAME))) {
        return false;
    }
    uint64_t sz = stat.cbSize.QuadPart;
    if (sz == 0 || sz > 16ull * 1024ull * 1024ull) {
        return false;
    }
    LARGE_INTEGER zero{};
    comStream->Seek(zero, STREAM_SEEK_SET, nullptr);
    outPng.resize(static_cast<size_t>(sz));
    ULONG readBytes = 0;
    if (FAILED(comStream->Read(outPng.data(), static_cast<ULONG>(sz), &readBytes)) ||
        readBytes != sz) {
        outPng.clear();
        return false;
    }
    return true;
}

// ---------------------------------------------------------------------------
// Optical Liquid Glass Renderer (Matches render_optical_liquid_glass Reference)
// ---------------------------------------------------------------------------

void SeparableGaussianBlur(const std::vector<float>& src, int w, int h, float radius,
                           std::vector<float>& tmp, std::vector<float>& dst,
                           bool zeroPad = false) {
    dst.resize(static_cast<size_t>(w) * h);
    if (radius < 0.05f) {
        dst = src;
        return;
    }
    int r = std::max(1, static_cast<int>(std::ceil(2.5f * radius)));
    std::vector<float> kernel(static_cast<size_t>(2 * r + 1));
    double sum = 0.0;
    double invTwoSigma2 = 1.0 / (2.0 * static_cast<double>(radius) * static_cast<double>(radius));
    for (int i = -r; i <= r; ++i) {
        double v = std::exp(-(i * i) * invTwoSigma2);
        kernel[static_cast<size_t>(i + r)] = static_cast<float>(v);
        sum += v;
    }
    float invSum = static_cast<float>(1.0 / sum);
    for (float& kv : kernel) {
        kv *= invSum;
    }

    tmp.resize(static_cast<size_t>(w) * h);
    // Horizontal pass
    for (int y = 0; y < h; ++y) {
        const float* row = &src[static_cast<size_t>(y) * w];
        float* outRow = &tmp[static_cast<size_t>(y) * w];
        for (int x = 0; x < w; ++x) {
            float acc = 0.0f;
            for (int k = -r; k <= r; ++k) {
                int sx = x + k;
                float val = 0.0f;
                if (sx >= 0 && sx < w) {
                    val = row[sx];
                } else if (!zeroPad) {
                    val = row[std::clamp(sx, 0, w - 1)];
                }
                acc += val * kernel[static_cast<size_t>(k + r)];
            }
            outRow[x] = acc;
        }
    }
    // Vertical pass
    for (int y = 0; y < h; ++y) {
        float* outRow = &dst[static_cast<size_t>(y) * w];
        for (int x = 0; x < w; ++x) {
            float acc = 0.0f;
            for (int k = -r; k <= r; ++k) {
                int sy = y + k;
                float val = 0.0f;
                if (sy >= 0 && sy < h) {
                    val = tmp[static_cast<size_t>(sy) * w + x];
                } else if (!zeroPad) {
                    val = tmp[static_cast<size_t>(std::clamp(sy, 0, h - 1)) * w + x];
                }
                acc += val * kernel[static_cast<size_t>(k + r)];
            }
            outRow[x] = acc;
        }
    }
}

void SobelGradients(const std::vector<float>& src, int w, int h,
                    std::vector<float>& gx, std::vector<float>& gy, std::vector<float>& mag,
                    bool zeroPad = false) {
    size_t n = static_cast<size_t>(w) * h;
    gx.resize(n);
    gy.resize(n);
    mag.resize(n);
    auto sample = [&](int x, int y) noexcept -> float {
        if (x >= 0 && x < w && y >= 0 && y < h) {
            return src[static_cast<size_t>(y) * w + x];
        }
        if (zeroPad) return 0.0f;
        return src[static_cast<size_t>(std::clamp(y, 0, h - 1)) * w + std::clamp(x, 0, w - 1)];
    };
    for (int y = 0; y < h; ++y) {
        for (int x = 0; x < w; ++x) {
            float dx = ((sample(x + 1, y - 1) + 2.0f * sample(x + 1, y) + sample(x + 1, y + 1)) -
                        (sample(x - 1, y - 1) + 2.0f * sample(x - 1, y) + sample(x - 1, y + 1))) * 0.125f;
            float dy = ((sample(x - 1, y + 1) + 2.0f * sample(x, y + 1) + sample(x + 1, y + 1)) -
                        (sample(x - 1, y - 1) + 2.0f * sample(x, y - 1) + sample(x + 1, y - 1))) * 0.125f;
            size_t idx = static_cast<size_t>(y) * w + x;
            gx[idx] = dx;
            gy[idx] = dy;
            mag[idx] = std::hypot(dx, dy);
        }
    }
}

inline double Sinc(double x) noexcept {
    if (std::abs(x) < 1e-8) return 1.0;
    double px = kPi * x;
    return std::sin(px) / px;
}

inline double Lanczos3Weight(double x) noexcept {
    double ax = std::abs(x);
    if (ax >= 3.0) return 0.0;
    return Sinc(x) * Sinc(x / 3.0);
}

inline double CatmullRomWeight(double x) noexcept {
    double ax = std::abs(x);
    if (ax <= 1.0) {
        return (1.5 * ax - 2.5) * ax * ax + 1.0;
    }
    if (ax < 2.0) {
        return ((-0.5 * ax + 2.5) * ax - 4.0) * ax + 2.0;
    }
    return 0.0;
}

void PremultipliedResizeFloat(
    const float* srcR, const float* srcG, const float* srcB, const float* srcA,
    int sw, int sh,
    std::vector<float>& dstR, std::vector<float>& dstG, std::vector<float>& dstB, std::vector<float>& dstA,
    int dw, int dh, bool useLanczos) {

    size_t srcN = static_cast<size_t>(sw) * sh;
    std::vector<float> pR(srcN), pG(srcN), pB(srcN), pA(srcN);
    for (size_t i = 0; i < srcN; ++i) {
        float a = Clamp01(srcA[i]);
        pR[i] = srcR[i] * a;
        pG[i] = srcG[i] * a;
        pB[i] = srcB[i] * a;
        pA[i] = a;
    }

    auto filterFn = useLanczos ? Lanczos3Weight : CatmullRomWeight;
    double supportBase = useLanczos ? 3.0 : 2.0;

    // Horizontal pass: sw x sh -> dw x sh
    double scaleX = static_cast<double>(sw) / dw;
    double radiusX = scaleX > 1.0 ? supportBase * scaleX : supportBase;
    double invScaleX = scaleX > 1.0 ? (1.0 / scaleX) : 1.0;

    size_t midN = static_cast<size_t>(dw) * sh;
    std::vector<float> mR(midN), mG(midN), mB(midN), mA(midN);
    std::vector<std::vector<std::pair<int, float>>> xTaps(static_cast<size_t>(dw));
    for (int dx = 0; dx < dw; ++dx) {
        double center = (dx + 0.5) * scaleX - 0.5;
        int left = static_cast<int>(std::floor(center - radiusX));
        int right = static_cast<int>(std::ceil(center + radiusX));
        double wsum = 0.0;
        for (int sx = left; sx <= right; ++sx) {
            double w = filterFn((sx - center) * invScaleX);
            if (std::abs(w) > 1e-6) {
                xTaps[static_cast<size_t>(dx)].emplace_back(std::clamp(sx, 0, sw - 1), static_cast<float>(w));
                wsum += w;
            }
        }
        if (std::abs(wsum) > 1e-8) {
            for (auto& p : xTaps[static_cast<size_t>(dx)]) {
                p.second = static_cast<float>(p.second / wsum);
            }
        }
    }

    for (int y = 0; y < sh; ++y) {
        size_t sOff = static_cast<size_t>(y) * sw;
        size_t mOff = static_cast<size_t>(y) * dw;
        for (int dx = 0; dx < dw; ++dx) {
            float ar = 0, ag = 0, ab = 0, aa = 0;
            for (const auto& [sx, w] : xTaps[static_cast<size_t>(dx)]) {
                size_t si = sOff + sx;
                ar += pR[si] * w;
                ag += pG[si] * w;
                ab += pB[si] * w;
                aa += pA[si] * w;
            }
            size_t mi = mOff + dx;
            mR[mi] = ar;
            mG[mi] = ag;
            mB[mi] = ab;
            mA[mi] = aa;
        }
    }

    // Vertical pass: dw x sh -> dw x dh
    double scaleY = static_cast<double>(sh) / dh;
    double radiusY = scaleY > 1.0 ? supportBase * scaleY : supportBase;
    double invScaleY = scaleY > 1.0 ? (1.0 / scaleY) : 1.0;

    std::vector<std::vector<std::pair<int, float>>> yTaps(static_cast<size_t>(dh));
    for (int dy = 0; dy < dh; ++dy) {
        double center = (dy + 0.5) * scaleY - 0.5;
        int top = static_cast<int>(std::floor(center - radiusY));
        int bot = static_cast<int>(std::ceil(center + radiusY));
        double wsum = 0.0;
        for (int sy = top; sy <= bot; ++sy) {
            double w = filterFn((sy - center) * invScaleY);
            if (std::abs(w) > 1e-6) {
                yTaps[static_cast<size_t>(dy)].emplace_back(std::clamp(sy, 0, sh - 1), static_cast<float>(w));
                wsum += w;
            }
        }
        if (std::abs(wsum) > 1e-8) {
            for (auto& p : yTaps[static_cast<size_t>(dy)]) {
                p.second = static_cast<float>(p.second / wsum);
            }
        }
    }

    size_t dstN = static_cast<size_t>(dw) * dh;
    dstR.resize(dstN);
    dstG.resize(dstN);
    dstB.resize(dstN);
    dstA.resize(dstN);
    for (int dy = 0; dy < dh; ++dy) {
        size_t dOff = static_cast<size_t>(dy) * dw;
        for (int x = 0; x < dw; ++x) {
            float ar = 0, ag = 0, ab = 0, aa = 0;
            for (const auto& [sy, w] : yTaps[static_cast<size_t>(dy)]) {
                size_t mi = static_cast<size_t>(sy) * dw + x;
                ar += mR[mi] * w;
                ag += mG[mi] * w;
                ab += mB[mi] * w;
                aa += mA[mi] * w;
            }
            float a = Clamp01(aa);
            size_t di = dOff + x;
            dstA[di] = a;
            if (a > 1e-4f) {
                dstR[di] = Clamp01(ar / a);
                dstG[di] = Clamp01(ag / a);
                dstB[di] = Clamp01(ab / a);
            } else {
                dstR[di] = dstG[di] = dstB[di] = 0.0f;
            }
        }
    }
}

void RenderOpticalLiquidGlass(const RawImage& src, const OpticalSettings& cfg, RawImage& out) {
    const int sw = static_cast<int>(src.width);
    const int sh = static_cast<int>(src.height);
    size_t srcN = static_cast<size_t>(sw) * sh;

    std::vector<float> sR(srcN), sG(srcN), sB(srcN), sA(srcN);
    for (size_t i = 0; i < srcN; ++i) {
        sB[i] = src.bgra[i * 4 + 0] / 255.0f;
        sG[i] = src.bgra[i * 4 + 1] / 255.0f;
        sR[i] = src.bgra[i * 4 + 2] / 255.0f;
        sA[i] = src.bgra[i * 4 + 3] / 255.0f;
    }

    // Always run optical gradients at 256x256 work canvas so thin Rim bevels and Seam glints
    // stay identically crisp before premultiplied-alpha Lanczos downscaling to screen size.
    constexpr int W = kWorkSize;
    constexpr int H = kWorkSize;
    constexpr size_t N = static_cast<size_t>(W) * H;
    std::vector<float> r, g, b, a;
    if (sw == W && sh == H) {
        r = std::move(sR);
        g = std::move(sG);
        b = std::move(sB);
        a = std::move(sA);
    } else {
        bool downscale = (sw > W && sh > H);
        PremultipliedResizeFloat(sR.data(), sG.data(), sB.data(), sA.data(), sw, sh,
                               r, g, b, a, W, H, downscale);
    }

    std::vector<float> lum(N), interiorLums;
    interiorLums.reserve(N / 2);
    double sumLum = 0.0;
    for (size_t i = 0; i < N; ++i) {
        float L = 0.2126f * r[i] + 0.7152f * g[i] + 0.0722f * b[i];
        lum[i] = L;
        if (a[i] > 0.25f) {
            interiorLums.push_back(L);
            sumLum += L;
        }
    }

    float p90 = 0.5f, p15 = 0.1f, meanL = 0.5f;
    if (!interiorLums.empty()) {
        meanL = static_cast<float>(sumLum / interiorLums.size());
        std::sort(interiorLums.begin(), interiorLums.end());
        auto pct = [&](double p) -> float {
            double pos = (p / 100.0) * (interiorLums.size() - 1);
            size_t lo = static_cast<size_t>(std::floor(pos));
            size_t hi = std::min(interiorLums.size() - 1, lo + 1);
            double frac = pos - lo;
            return static_cast<float>(interiorLums[lo] * (1.0 - frac) + interiorLums[hi] * frac);
        };
        p90 = pct(90.0);
        p15 = pct(15.0);
    }

    const bool hasDarkCutout = (p90 > 0.62f) && (p15 < 0.12f);
    const bool isDarkLogo = (meanL < 0.34f);

    const float opacityScale = cfg.opacityPct / 100.0f;
    const float frostScale = cfg.frostPct / 100.0f;
    const float glowScale = cfg.glowPct / 100.0f;
    const float darkProtectScale = cfg.darkProtectPct / 100.0f;

    // Resolution compensation + Plateau Bevel Rim + Detail-Guarded Seam
    const float srcScale = static_cast<float>(W) / std::max(1, std::min(sw, sh));
    const float tLow = Clamp01((srcScale - 1.0f) / 6.0f);
    const float sigma1 = 1.9f + 1.0f * tLow;
    const float sigma2 = 5.4f + 2.8f * tLow;
    const float edgeGain = 1.0f + 1.45f * tLow;
    const float gradGain = 1.0f + 2.35f * tLow;

    std::vector<float> tmp, aB1, aB2, rB, gB, bB, lB;
    SeparableGaussianBlur(a, W, H, sigma1, tmp, aB1, true);
    SeparableGaussianBlur(a, W, H, sigma2, tmp, aB2, true);
    SeparableGaussianBlur(r, W, H, 0.6f, tmp, rB, false);
    SeparableGaussianBlur(g, W, H, 0.6f, tmp, gB, false);
    SeparableGaussianBlur(b, W, H, 0.6f, tmp, bB, false);
    SeparableGaussianBlur(lum, W, H, 0.6f, tmp, lB, false);

    std::vector<float> gxs, gys, ms, dummyGx, dummyGy, gr, gg, gb, gl;
    SobelGradients(aB1, W, H, gxs, gys, ms, true);
    SobelGradients(rB, W, H, dummyGx, dummyGy, gr, false);
    SobelGradients(gB, W, H, dummyGx, dummyGy, gg, false);
    SobelGradients(bB, W, H, dummyGx, dummyGy, gb, false);
    SobelGradients(lB, W, H, dummyGx, dummyGy, gl, false);

    std::vector<float> rawGrad(N), rawGradClamped(N), gradDensity;
    for (size_t i = 0; i < N; ++i) {
        float cEdge = std::sqrt(gr[i] * gr[i] + gg[i] * gg[i] + gb[i] * gb[i]) / 1.7320508f;
        float rg = std::max(gl[i], 1.25f * cEdge) * gradGain;
        rawGrad[i] = rg;
        rawGradClamped[i] = Clamp01(rg * 4.0f);
    }
    SeparableGaussianBlur(rawGradClamped, W, H, 3.6f, tmp, gradDensity, false);

    std::vector<float> outR(N), outG(N), outB(N), outA(N);
    for (int y = 0; y < H; ++y) {
        float ny = (y / static_cast<float>(H - 1)) * 2.0f - 1.0f;
        for (int x = 0; x < W; ++x) {
            float nx = (x / static_cast<float>(W - 1)) * 2.0f - 1.0f;
            size_t i = static_cast<size_t>(y) * W + x;

            float ai = a[i];
            if (ai <= 1e-5f) {
                outR[i] = outG[i] = outB[i] = outA[i] = 0.0f;
                continue;
            }

            float L = lum[i];
            float cMin = std::min(r[i], std::min(g[i], b[i]));
            float cMax = std::max(r[i], std::max(g[i], b[i]));
            float chroma = cMax - cMin;
            float neutralWhite = 1.0f - Smoothstep(0.18f, 0.50f, chroma);
            float frostSignal = Smoothstep(0.45f, 0.88f, cMin) * neutralWhite;

            float bodyAlpha = 0.0f;
            if (hasDarkCutout) {
                float cutout = 1.0f - Smoothstep(0.03f, 0.22f, L);
                bodyAlpha = (0.048f + 0.080f * std::pow(Clamp01(L), 0.90f)) * (1.0f - 0.86f * cutout);
            } else if (isDarkLogo) {
                float relLum = Clamp01((L - 0.03f) / std::max(p90, 0.22f));
                bodyAlpha = 0.065f + 0.070f * std::pow(Clamp01(L), 0.85f) + 0.065f * darkProtectScale * relLum;
            } else {
                bodyAlpha = 0.050f + 0.075f * std::pow(Clamp01(L), 0.90f);
            }
            bodyAlpha = Clamp01(bodyAlpha * opacityScale);
            float frostAlpha = Clamp01(0.58f * frostScale) * std::clamp(opacityScale * 0.35f + 0.65f, 0.35f, 1.25f);
            float baseAlpha = frostAlpha * frostSignal + bodyAlpha * (1.0f - frostSignal);

            float diag = Clamp01(0.58f - 0.42f * (0.68f * nx + 0.73f * ny));
            float dxArc = nx + 0.38f;
            float dyArc = ny + 0.42f;
            float arc = std::exp(-(dxArc * dxArc + dyArc * dyArc) * 2.4f);
            float sup = hasDarkCutout ? (1.0f - Smoothstep(0.03f, 0.16f, L)) : 0.0f;
            float sheen = (0.060f * std::pow(diag, 1.7f) + 0.095f * arc) *
                          (1.0f - 0.55f * frostSignal) * (1.0f - 0.85f * sup) * glowScale;

            float edgeRaw = Clamp01((ai - aB1[i]) * 3.0f) + Clamp01((aB1[i] - aB2[i]) * 2.1f);
            float edgePlateau = Smoothstep(0.14f, 0.46f, std::min(edgeRaw * edgeGain, 1.5f));
            float edgeBand = edgePlateau * Smoothstep(0.28f, 0.80f, ai);
            float invM = 1.0f / std::max(ms[i], 1e-4f);
            float onx = -gxs[i] * invM;
            float ony = -gys[i] * invM;
            float dot = -0.70710678f * (onx + ony);
            float rim = edgeBand * (0.32f +
                                    0.72f * std::pow(Clamp01(dot), 1.10f) +
                                    0.40f * std::pow(Clamp01(-dot), 1.35f)) * glowScale;

            float seamThreshHi = 0.16f + 0.10f * Clamp01(gradDensity[i]);
            float seam = Smoothstep(0.020f, seamThreshHi, rawGrad[i]) *
                         Smoothstep(0.48f, 0.92f, ai);
            float seamLight = 0.58f * glowScale * seam * (1.0f - 0.30f * edgeBand);

            float finalA = std::min(std::max(baseAlpha + sheen + rim + seamLight, 0.0f), 0.90f) * ai;
            float spec = Clamp01(frostSignal + 0.95f * rim + 0.80f * seamLight + 0.65f * sheen);
            outR[i] = 0.94f + 0.06f * spec;
            outG[i] = 0.97f + 0.03f * spec;
            outB[i] = 1.00f;
            outA[i] = finalA;
        }
    }

    std::vector<float> finR, finG, finB, finA;
    if (sw == W && sh == H) {
        finR = std::move(outR);
        finG = std::move(outG);
        finB = std::move(outB);
        finA = std::move(outA);
    } else {
        PremultipliedResizeFloat(outR.data(), outG.data(), outB.data(), outA.data(), W, H,
                               finR, finG, finB, finA, sw, sh, true);
    }

    out.width = src.width;
    out.height = src.height;
    out.bgra.resize(srcN * 4);
    for (size_t i = 0; i < srcN; ++i) {
        float oa = std::min(Clamp01(finA[i]), 0.90f);
        if (oa <= 1e-4f) {
            out.bgra[i * 4 + 0] = 0;
            out.bgra[i * 4 + 1] = 0;
            out.bgra[i * 4 + 2] = 0;
            out.bgra[i * 4 + 3] = 0;
        } else {
            // Keep unpremultiplied visible RGB cleanly inside the ice-white gamut >= 0.94.
            float rVal = std::max(0.94f, Clamp01(finR[i]));
            float gVal = std::max(0.97f, Clamp01(finG[i]));
            float bVal = 1.00f;
            out.bgra[i * 4 + 0] = static_cast<uint8_t>(std::min(255.0f, bVal * 255.0f + 0.5f));
            out.bgra[i * 4 + 1] = static_cast<uint8_t>(std::min(255.0f, gVal * 255.0f + 0.5f));
            out.bgra[i * 4 + 2] = static_cast<uint8_t>(std::min(255.0f, rVal * 255.0f + 0.5f));
            out.bgra[i * 4 + 3] = static_cast<uint8_t>(std::min(255.0f, oa * 255.0f + 0.5f));
        }
    }
}

// ---------------------------------------------------------------------------
// RAM LRU Cache & Stream Conversion
// ---------------------------------------------------------------------------

bool TransformBytesWithLru(const std::vector<uint8_t>& origBytes,
                           const OpticalSettings& cfg,
                           std::vector<uint8_t>& outPngBytes,
                           uint64_t& outPngHash,
                           uint32_t& outW,
                           uint32_t& outH) {
    outPngBytes.clear();
    outPngHash = 0;
    outW = outH = 0;

    RawImage decoded;
    if (!DecodeBytesToRawBgra(origBytes, decoded)) {
        g_errorCount.fetch_add(1);
        return false;
    }
    outW = decoded.width;
    outH = decoded.height;
    if (decoded.width < 24 || decoded.height < 24) {
        g_lowResCount.fetch_add(1);
    }

    uint64_t pixHash = Fnv1a64(decoded.bgra.data(), decoded.bgra.size());
    CacheKey key{pixHash, decoded.width, decoded.height, cfg.generation};

    {
        std::lock_guard<std::mutex> lock(g_stateMutex);
        auto it = g_lruMap.find(key);
        if (it != g_lruMap.end()) {
            g_lruList.splice(g_lruList.begin(), g_lruList, it->second);
            outPngBytes = it->second->pngBytes;
            outPngHash = it->second->pngHash;
            g_cacheHits.fetch_add(1);
            return true;
        }
    }

    RawImage rendered;
    RenderOpticalLiquidGlass(decoded, cfg, rendered);
    if (!EncodeRawBgraToPng(rendered, outPngBytes)) {
        g_errorCount.fetch_add(1);
        return false;
    }
    outPngHash = Fnv1a64(outPngBytes.data(), outPngBytes.size());
    g_renderCount.fetch_add(1);

    {
        std::lock_guard<std::mutex> lock(g_stateMutex);
        if (g_lruMap.find(key) == g_lruMap.end() && outPngBytes.size() <= kMaxCacheBytes) {
            while (!g_lruList.empty() &&
                   (g_lruList.size() >= kMaxCacheEntries ||
                    g_lruTotalBytes + outPngBytes.size() > kMaxCacheBytes)) {
                auto& back = g_lruList.back();
                g_lruTotalBytes -= back.pngBytes.size();
                g_lruMap.erase(back.key);
                g_lruList.pop_back();
            }
            g_lruList.push_front(CacheEntry{key, outPngBytes, outPngHash});
            g_lruTotalBytes += outPngBytes.size();
            g_lruMap[key] = g_lruList.begin();
        }
    }
    return true;
}

// ---------------------------------------------------------------------------
// TaskListButton Discovery, Tracking & Instant Reapply/Restore
// ---------------------------------------------------------------------------

bool IsReadableMemory(const void* ptr, size_t size) noexcept {
    if (!ptr || reinterpret_cast<uintptr_t>(ptr) < 0x10000) {
        return false;
    }
    MEMORY_BASIC_INFORMATION mbi{};
    if (VirtualQuery(ptr, &mbi, sizeof(mbi)) != sizeof(mbi)) {
        return false;
    }
    if (mbi.State != MEM_COMMIT || (mbi.Protect & (PAGE_NOACCESS | PAGE_GUARD))) {
        return false;
    }
    constexpr DWORD kReadable = PAGE_READONLY | PAGE_READWRITE | PAGE_WRITECOPY |
                                PAGE_EXECUTE_READ | PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY;
    if ((mbi.Protect & kReadable) == 0) {
        return false;
    }
    uintptr_t endPtr = reinterpret_cast<uintptr_t>(ptr) + size;
    uintptr_t regionEnd = reinterpret_cast<uintptr_t>(mbi.BaseAddress) + mbi.RegionSize;
    return endPtr >= reinterpret_cast<uintptr_t>(ptr) && endPtr <= regionEnd;
}

void* GetCanonicalUnknown(IUnknown* unk) {
    if (!unk) return nullptr;
    winrt::com_ptr<IUnknown> canon;
    if (SUCCEEDED(unk->QueryInterface(IID_PPV_ARGS(canon.put())))) {
        return canon.get();
    }
    return nullptr;
}

bool ResolveComInterfaceSlot(IUnknown* obj,
                             IUnknown* knownPtr,
                             const void* const* rawTargets,
                             size_t targetCount,
                             GUID& outIid,
                             int& outSlot) {
    if (!obj) {
        return false;
    }
    wf::IInspectable insp{nullptr};
    if (FAILED(obj->QueryInterface(
            winrt::guid_of<wf::IInspectable>(), winrt::put_abi(insp))) ||
        !insp) {
        return false;
    }
    auto* abiInsp = reinterpret_cast<winrt::impl::inspectable_abi*>(winrt::get_abi(insp));
    uint32_t iidCount = 0;
    winrt::guid* iids = nullptr;
    if (!abiInsp || FAILED(abiInsp->GetIids(&iidCount, &iids)) || !iids) {
        return false;
    }

    bool found = false;
    GUID resolved{};
    int matchedSlot = -1;
    for (uint32_t i = 0; i < iidCount; ++i) {
        winrt::com_ptr<IUnknown> probe;
        REFIID riid = *reinterpret_cast<const GUID*>(&iids[i]);
        if (SUCCEEDED(obj->QueryInterface(riid, reinterpret_cast<void**>(probe.put()))) && probe) {
            if (knownPtr && probe.get() == knownPtr) {
                resolved = riid;
                found = true;
                break;
            }
            if (!knownPtr && rawTargets && targetCount > 0) {
                void** vtbl = *reinterpret_cast<void***>(probe.get());
                if (IsReadableMemory(vtbl, sizeof(void*) * 8)) {
                    for (int slot = 6; slot < 96; ++slot) {
                        if (!IsReadableMemory(&vtbl[slot], sizeof(void*))) {
                            break;
                        }
                        for (size_t t = 0; t < targetCount; ++t) {
                            if (rawTargets[t] && vtbl[slot] == rawTargets[t]) {
                                resolved = riid;
                                matchedSlot = slot;
                                found = true;
                                break;
                            }
                        }
                        if (found) break;
                    }
                    if (found) break;
                }
            }
        }
    }
    CoTaskMemFree(iids);
    if (found) {
        outIid = resolved;
        outSlot = matchedSlot;
    }
    return found;
}

bool EnsureTaskListButtonIid(IUnknown* anyButtonComPtr, IUnknown* knownItbPtr) {
    if (g_hasTaskListButtonIid.load(std::memory_order_acquire)) {
        return true;
    }
    GUID resolved{};
    int unusedSlot = -1;
    const void* rawTarget = reinterpret_cast<const void*>(TaskListButton_get_Icon_Raw);
    if (!ResolveComInterfaceSlot(anyButtonComPtr, knownItbPtr, &rawTarget, 1, resolved, unusedSlot)) {
        return false;
    }
    std::lock_guard<std::mutex> lock(g_stateMutex);
    if (!g_hasTaskListButtonIid.load()) {
        g_iidTaskListButton = resolved;
        g_hasTaskListButtonIid.store(true, std::memory_order_release);
    }
    return true;
}

void ApplyOrCaptureButton(const wux::FrameworkElement& btnFe, const OpticalSettings& cfg);

std::shared_ptr<void> MakeAsyncOpGuard() {
    g_pendingAsyncOps.fetch_add(1, std::memory_order_acq_rel);
    return std::shared_ptr<void>(nullptr, [](void*) {
        g_pendingAsyncOps.fetch_sub(1, std::memory_order_acq_rel);
    });
}

wuxc::Image FindIconImageInButton(const wux::FrameworkElement& btnFe) {
    if (!btnFe) return nullptr;
    try {
        if (auto named = btnFe.FindName(L"Icon")) {
            if (auto img = named.try_as<wuxc::Image>()) {
                return img;
            }
        }
        std::vector<wux::DependencyObject> queue;
        queue.push_back(btnFe);
        for (size_t qi = 0; qi < queue.size() && queue.size() < 40; ++qi) {
            int cc = wuxm::VisualTreeHelper::GetChildrenCount(queue[qi]);
            for (int ci = 0; ci < cc && queue.size() < 40; ++ci) {
                auto ch = wuxm::VisualTreeHelper::GetChild(queue[qi], ci);
                if (!ch) continue;
                if (auto img = ch.try_as<wuxc::Image>()) {
                    if (img.Name() == L"Icon") {
                        return img;
                    }
                }
                queue.push_back(ch);
            }
        }
    } catch (...) {
    }
    return nullptr;
}

bool PremultBgraBufferToPng(const wss::IBuffer& buf,
                            uint32_t width,
                            uint32_t height,
                            std::vector<uint8_t>& outPng) {
    outPng.clear();
    if (!buf || width == 0 || height == 0 || width > 512 || height > 512) {
        return false;
    }
    static constexpr GUID kIBufferByteAccessIid = {
        0x905a0fef, 0xbc53, 0x11df, {0x8c, 0x49, 0x00, 0x1e, 0x4f, 0xc6, 0x86, 0xda}};
    winrt::com_ptr<::Windows::Storage::Streams::IBufferByteAccess> access;
    IUnknown* rawBuf = reinterpret_cast<IUnknown*>(winrt::get_abi(buf));
    if (!rawBuf ||
        FAILED(rawBuf->QueryInterface(kIBufferByteAccessIid, reinterpret_cast<void**>(access.put()))) ||
        !access) {
        g_diagRtbFail.store(4, std::memory_order_relaxed);
        return false;
    }
    BYTE* pixels = nullptr;
    if (FAILED(access->Buffer(&pixels)) || !pixels) {
        g_diagRtbFail.store(5, std::memory_order_relaxed);
        return false;
    }
    uint64_t expected = static_cast<uint64_t>(width) * height * 4ull;
    if (static_cast<uint64_t>(buf.Length()) < expected) {
        g_diagRtbFail.store(6, std::memory_order_relaxed);
        return false;
    }

    RawImage cap;
    cap.width = width;
    cap.height = height;
    cap.bgra.resize(static_cast<size_t>(expected));
    size_t nonZeroAlpha = 0;
    const size_t pxCount = static_cast<size_t>(width) * height;
    for (size_t i = 0; i < pxCount; ++i) {
        uint8_t b = pixels[i * 4 + 0];
        uint8_t g = pixels[i * 4 + 1];
        uint8_t r = pixels[i * 4 + 2];
        uint8_t a = pixels[i * 4 + 3];
        if (a == 0) {
            cap.bgra[i * 4 + 0] = 0;
            cap.bgra[i * 4 + 1] = 0;
            cap.bgra[i * 4 + 2] = 0;
            cap.bgra[i * 4 + 3] = 0;
        } else {
            ++nonZeroAlpha;
            if (a == 255) {
                cap.bgra[i * 4 + 0] = b;
                cap.bgra[i * 4 + 1] = g;
                cap.bgra[i * 4 + 2] = r;
                cap.bgra[i * 4 + 3] = 255;
            } else {
                uint32_t ua = a;
                cap.bgra[i * 4 + 0] = static_cast<uint8_t>(std::min<uint32_t>(255u, (static_cast<uint32_t>(b) * 255u + ua / 2u) / ua));
                cap.bgra[i * 4 + 1] = static_cast<uint8_t>(std::min<uint32_t>(255u, (static_cast<uint32_t>(g) * 255u + ua / 2u) / ua));
                cap.bgra[i * 4 + 2] = static_cast<uint8_t>(std::min<uint32_t>(255u, (static_cast<uint32_t>(r) * 255u + ua / 2u) / ua));
                cap.bgra[i * 4 + 3] = a;
            }
        }
    }
    g_diagRtbAlphaPx.store(nonZeroAlpha, std::memory_order_relaxed);
    if (nonZeroAlpha < 4) {
        g_diagRtbFail.store(7, std::memory_order_relaxed);
        return false;
    }
    bool encOk = EncodeRawBgraToPng(cap, outPng);
    if (!encOk) {
        g_diagRtbFail.store(8, std::memory_order_relaxed);
    }
    return encOk;
}

void ClearCaptureInFlight(void* key) {
    std::lock_guard<std::mutex> lock(g_stateMutex);
    auto it = g_buttons.find(key);
    if (it != g_buttons.end()) {
        it->second.captureInFlight = false;
    }
}

void ScheduleColdButtonCapture(const wux::FrameworkElement& btnFe, void* key, const OpticalSettings& cfg) {
    if (!btnFe || !key || !cfg.enabled || g_unloading.load(std::memory_order_acquire)) {
        return;
    }
    wuxc::Image iconImg = FindIconImageInButton(btnFe);
    if (!iconImg) {
        return;
    }
    try {
        if (iconImg.Visibility() != wux::Visibility::Visible ||
            iconImg.ActualWidth() < 4.0 || iconImg.ActualHeight() < 4.0 ||
            !iconImg.Source()) {
            return;
        }
        int32_t targetPx = 0;
        if (auto bmpSrc = iconImg.Source().try_as<wuxmi::BitmapSource>()) {
            int32_t pw = bmpSrc.PixelWidth();
            int32_t ph = bmpSrc.PixelHeight();
            int32_t maxDim = std::max(pw, ph);
            if (maxDim >= 64) {
                targetPx = std::clamp(maxDim, 64, 128);
            }
        }

        {
            std::lock_guard<std::mutex> lock(g_stateMutex);
            auto& rec = g_buttons[key];
            rec.weakFe = winrt::make_weak(btnFe);
            if (!rec.origStreamBytes.empty() || rec.captureInFlight || rec.captureAttemptedGen == cfg.generation) {
                return;
            }
            rec.captureInFlight = true;
            rec.captureAttemptedGen = cfg.generation;
        }

        auto disp = btnFe.Dispatcher();
        if (!disp) {
            ClearCaptureInFlight(key);
            return;
        }
        auto weakBtn = winrt::make_weak(btnFe);
        auto weakImg = winrt::make_weak(iconImg);
        auto opGuard = MakeAsyncOpGuard();
        g_diagRtbQueued.fetch_add(1, std::memory_order_relaxed);

        disp.RunAsync(wuc::CoreDispatcherPriority::Normal, [weakBtn, weakImg, key, targetPx, opGuard]() {
            if (g_unloading.load(std::memory_order_acquire)) {
                ClearCaptureInFlight(key);
                return;
            }
            try {
                auto fe = weakBtn.get();
                auto img = weakImg.get();
                if (!fe || !img) {
                    ClearCaptureInFlight(key);
                    return;
                }
                OpticalSettings curCfg;
                bool alreadyHave = false;
                {
                    std::lock_guard<std::mutex> lock(g_stateMutex);
                    curCfg = g_settings;
                    auto it = g_buttons.find(key);
                    if (it != g_buttons.end() && !it->second.origStreamBytes.empty()) {
                        alreadyHave = true;
                        it->second.captureInFlight = false;
                    }
                }
                if (alreadyHave) {
                    ApplyOrCaptureButton(fe, curCfg);
                    FlushStatusFile(false);
                    return;
                }
                if (!curCfg.enabled) {
                    ClearCaptureInFlight(key);
                    return;
                }
                if (img.Visibility() != wux::Visibility::Visible ||
                    img.ActualWidth() < 4.0 || img.ActualHeight() < 4.0 ||
                    !img.Source()) {
                    ClearCaptureInFlight(key);
                    return;
                }

                wuxmi::RenderTargetBitmap rtb;
                g_diagRtbStarted.fetch_add(1, std::memory_order_relaxed);
                g_diagRtbLastTarget.store(targetPx, std::memory_order_relaxed);
                auto renderOp = (targetPx > 0) ? rtb.RenderAsync(img, targetPx, 0) : rtb.RenderAsync(img);
                renderOp.Completed([weakBtn, key, rtb, opGuard](wf::IAsyncAction const& action, wf::AsyncStatus status) {
                    if (g_unloading.load(std::memory_order_acquire) || status != wf::AsyncStatus::Completed) {
                        if (status == wf::AsyncStatus::Error) {
                            g_diagRtbHr.store(action.ErrorCode(), std::memory_order_relaxed);
                        }
                        g_diagRtbFail.store(10 + static_cast<uint32_t>(status), std::memory_order_relaxed);
                        ClearCaptureInFlight(key);
                        FlushStatusFile(false);
                        return;
                    }
                    try {
                        int32_t pw = rtb.PixelWidth();
                        int32_t ph = rtb.PixelHeight();
                        g_diagRtbLastW.store(pw, std::memory_order_relaxed);
                        g_diagRtbLastH.store(ph, std::memory_order_relaxed);
                        if (pw <= 0 || ph <= 0) {
                            g_diagRtbFail.store(2, std::memory_order_relaxed);
                            ClearCaptureInFlight(key);
                            FlushStatusFile(true);
                            return;
                        }
                        uint64_t pixToken = g_diagRtbPixQueued.fetch_add(1, std::memory_order_relaxed) + 1;
                        auto pixOp = rtb.GetPixelsAsync();
                        {
                            std::lock_guard<std::mutex> lk(g_pixKeepMutex);
                            g_pixKeepAlive.push_back(PixKeepEntry{pixToken, pixOp});
                        }
                        pixOp.Completed([weakBtn, key, pw, ph, rtb, opGuard, pixToken](wf::IAsyncOperation<wss::IBuffer> const& op, wf::AsyncStatus pstatus) {
                            g_diagRtbPixDone.fetch_add(1, std::memory_order_relaxed);
                            {
                                std::lock_guard<std::mutex> lk(g_pixKeepMutex);
                                for (size_t i = 0; i < g_pixKeepAlive.size(); ++i) {
                                    if (g_pixKeepAlive[i].token == pixToken) {
                                        g_pixKeepAlive[i] = std::move(g_pixKeepAlive.back());
                                        g_pixKeepAlive.pop_back();
                                        break;
                                    }
                                }
                            }
                            if (g_unloading.load(std::memory_order_acquire) || pstatus != wf::AsyncStatus::Completed) {
                                if (pstatus == wf::AsyncStatus::Error) {
                                    g_diagRtbHr.store(op.ErrorCode(), std::memory_order_relaxed);
                                }
                                uint32_t code = g_unloading.load(std::memory_order_acquire)
                                    ? 29u
                                    : (20u + static_cast<uint32_t>(pstatus));
                                g_diagRtbFail.store(code, std::memory_order_relaxed);
                                ClearCaptureInFlight(key);
                                FlushStatusFile(true);
                                return;
                            }
                            try {
                                wss::IBuffer buf = op.GetResults();
                                std::vector<uint8_t> pngBytes;
                                bool encoded = PremultBgraBufferToPng(buf, static_cast<uint32_t>(pw), static_cast<uint32_t>(ph), pngBytes);
                                if (!encoded || pngBytes.empty()) {
                                    g_diagRtbFail.store(!encoded ? 31u : 32u, std::memory_order_relaxed);
                                    ClearCaptureInFlight(key);
                                    FlushStatusFile(true);
                                    return;
                                }
                                uint64_t h = Fnv1a64(pngBytes.data(), pngBytes.size());
                                OpticalSettings applyCfg;
                                {
                                    std::lock_guard<std::mutex> lock(g_stateMutex);
                                    auto& rec = g_buttons[key];
                                    rec.captureInFlight = false;
                                    if (rec.origStreamBytes.empty() && h != rec.lastGlassPngHash) {
                                        rec.origStreamBytes = std::move(pngBytes);
                                        rec.origStreamHash = h;
                                    }
                                    applyCfg = g_settings;
                                }
                                g_diagRtbOk.fetch_add(1, std::memory_order_relaxed);

                                auto fe = weakBtn.get();
                                if (!fe || g_unloading.load(std::memory_order_acquire)) {
                                    return;
                                }
                                auto btnDisp = fe.Dispatcher();
                                if (btnDisp && !btnDisp.HasThreadAccess()) {
                                    btnDisp.RunAsync(wuc::CoreDispatcherPriority::Normal, [weakBtn, opGuard]() {
                                        if (g_unloading.load(std::memory_order_acquire)) return;
                                        if (auto feUi = weakBtn.get()) {
                                            OpticalSettings latest;
                                            {
                                                std::lock_guard<std::mutex> lock(g_stateMutex);
                                                latest = g_settings;
                                            }
                                            ApplyOrCaptureButton(feUi, latest);
                                            FlushStatusFile(false);
                                        }
                                    });
                                } else {
                                    ApplyOrCaptureButton(fe, applyCfg);
                                    FlushStatusFile(false);
                                }
                            } catch (const winrt::hresult_error& e) {
                                g_diagRtbHr.store(e.code(), std::memory_order_relaxed);
                                g_errorCount.fetch_add(1, std::memory_order_relaxed);
                                ClearCaptureInFlight(key);
                            } catch (...) {
                                g_errorCount.fetch_add(1, std::memory_order_relaxed);
                                ClearCaptureInFlight(key);
                            }
                        });
                    } catch (const winrt::hresult_error& e) {
                        g_diagRtbHr.store(e.code(), std::memory_order_relaxed);
                        g_errorCount.fetch_add(1, std::memory_order_relaxed);
                        ClearCaptureInFlight(key);
                    } catch (...) {
                        g_errorCount.fetch_add(1, std::memory_order_relaxed);
                        ClearCaptureInFlight(key);
                    }
                });
            } catch (const winrt::hresult_error& e) {
                g_diagRtbHr.store(e.code(), std::memory_order_relaxed);
                g_errorCount.fetch_add(1, std::memory_order_relaxed);
                ClearCaptureInFlight(key);
            } catch (...) {
                g_errorCount.fetch_add(1, std::memory_order_relaxed);
                ClearCaptureInFlight(key);
            }
        });
    } catch (const winrt::hresult_error& e) {
        g_diagRtbHr.store(e.code(), std::memory_order_relaxed);
        g_errorCount.fetch_add(1, std::memory_order_relaxed);
        ClearCaptureInFlight(key);
    } catch (...) {
        g_errorCount.fetch_add(1, std::memory_order_relaxed);
        ClearCaptureInFlight(key);
    }
}

bool QueryTaskListButtonInterface(const wux::FrameworkElement& fe, winrt::com_ptr<IUnknown>& outItb) {
    outItb = nullptr;
    if (!fe) return false;
    IUnknown* unk = reinterpret_cast<IUnknown*>(winrt::get_abi(fe));
    if (!unk) return false;
    if (!EnsureTaskListButtonIid(unk, nullptr)) {
        return false;
    }
    return SUCCEEDED(unk->QueryInterface(g_iidTaskListButton, reinterpret_cast<void**>(outItb.put()))) && outItb;
}

bool RecordTaskbarFrame(const wux::FrameworkElement& anyEl) {
    if (!anyEl) return false;
    bool newlyAdded = false;
    try {
        wux::DependencyObject cur = anyEl;
        while (cur) {
            auto fe = cur.try_as<wux::FrameworkElement>();
            if (fe && winrt::get_class_name(fe) == L"Taskbar.TaskbarFrame") {
                bool exists = false;
                std::erase_if(g_taskbarFrames, [](const auto& w) { return !w.get(); });
                for (const auto& w : g_taskbarFrames) {
                    if (w.get() == fe) {
                        exists = true;
                        break;
                    }
                }
                if (!exists) {
                    g_taskbarFrames.push_back(winrt::make_weak(fe));
                    g_frameEntries.push_back(FrameEntry{winrt::make_weak(fe), 0});
                    newlyAdded = true;
                }
                std::erase_if(g_frameEntries, [](const auto& e) { return !e.frame.get(); });
                break;
            }
            cur = wuxm::VisualTreeHelper::GetParent(cur);
        }
    } catch (...) {
    }
    return newlyAdded;
}

void CallPutIconOriginal(void* itbPtr, const std::vector<uint8_t>& streamBytes) {
    if (!itbPtr || !TaskListButton_put_Icon_Original || streamBytes.empty()) {
        return;
    }
    wss::IRandomAccessStream ras{nullptr};
    if (!BytesToRandomAccessStream(streamBytes, ras) || !ras) {
        return;
    }
    t_inPutIconOverride = true;
    TaskListButton_put_Icon_Original(itbPtr, winrt::get_abi(ras));
    t_inPutIconOverride = false;
}

void ApplyOrCaptureButton(const wux::FrameworkElement& btnFe, const OpticalSettings& cfg) {
    if (!btnFe || !TaskListButton_put_Icon_Original) return;

    winrt::com_ptr<IUnknown> itb;
    if (!QueryTaskListButtonInterface(btnFe, itb)) {
        g_diagQiFail.fetch_add(1, std::memory_order_relaxed);
        return;
    }
    void* key = GetCanonicalUnknown(itb.get());
    if (!key) return;

    // Ensure we have original bytes captured for this cold-start or tracked button.
    std::vector<uint8_t> origBytes;
    bool needApply = false;
    {
        std::lock_guard<std::mutex> lock(g_stateMutex);
        auto it = g_buttons.find(key);
        if (it != g_buttons.end() && !it->second.origStreamBytes.empty()) {
            origBytes = it->second.origStreamBytes;
            if (cfg.enabled && !g_unloading.load()) {
                needApply = (it->second.appliedGeneration != cfg.generation);
            } else {
                needApply = (it->second.appliedGeneration != 0);
            }
        }
    }

    if (origBytes.empty()) {
        std::vector<uint8_t> curBytes;
        if (TaskListButton_get_Icon_Raw) {
            void* rawStreamAbi = nullptr;
            if (SUCCEEDED(TaskListButton_get_Icon_Raw(itb.get(), &rawStreamAbi)) && rawStreamAbi) {
                winrt::com_ptr<IUnknown> streamRef;
                streamRef.attach(reinterpret_cast<IUnknown*>(rawStreamAbi));
                if (ReadStreamToBytes(streamRef.get(), curBytes)) {
                    g_diagGetIconOk.fetch_add(1, std::memory_order_relaxed);
                }
            }
        }
        if (!curBytes.empty()) {
            uint64_t h = Fnv1a64(curBytes.data(), curBytes.size());
            std::lock_guard<std::mutex> lock(g_stateMutex);
            auto& rec = g_buttons[key];
            rec.weakFe = winrt::make_weak(btnFe);
            if (rec.origStreamBytes.empty() && h != rec.lastGlassPngHash) {
                rec.origStreamBytes = std::move(curBytes);
                rec.origStreamHash = h;
            }
            origBytes = rec.origStreamBytes;
            needApply = cfg.enabled && !g_unloading.load() && (rec.appliedGeneration != cfg.generation);
        } else if (cfg.enabled && !g_unloading.load()) {
            ScheduleColdButtonCapture(btnFe, key, cfg);
        }
    }

    if (origBytes.empty() || !needApply) {
        return;
    }

    if (!cfg.enabled || g_unloading.load()) {
        CallPutIconOriginal(itb.get(), origBytes);
        std::lock_guard<std::mutex> lock(g_stateMutex);
        auto it = g_buttons.find(key);
        if (it != g_buttons.end()) {
            it->second.appliedGeneration = 0;
        }
        return;
    }

    std::vector<uint8_t> pngBytes;
    uint64_t pngHash = 0;
    uint32_t w = 0, h = 0;
    if (TransformBytesWithLru(origBytes, cfg, pngBytes, pngHash, w, h)) {
        CallPutIconOriginal(itb.get(), pngBytes);
        std::lock_guard<std::mutex> lock(g_stateMutex);
        auto it = g_buttons.find(key);
        if (it != g_buttons.end()) {
            it->second.weakFe = winrt::make_weak(btnFe);
            it->second.lastGlassPngHash = pngHash;
            it->second.appliedGeneration = cfg.generation;
            it->second.lastWidth = w;
            it->second.lastHeight = h;
        }
    }
}

void WalkButtonsInSubtree(const wux::DependencyObject& node, const OpticalSettings& cfg) {
    if (!node) return;
    try {
        if (winrt::get_class_name(node) == L"Taskbar.TaskListButton") {
            g_diagButtonsSeen.fetch_add(1, std::memory_order_relaxed);
            if (auto btnFe = node.try_as<wux::FrameworkElement>()) {
                ApplyOrCaptureButton(btnFe, cfg);
            }
            return;
        }
        int c = wuxm::VisualTreeHelper::GetChildrenCount(node);
        for (int i = 0; i < c; ++i) {
            auto ch = wuxm::VisualTreeHelper::GetChild(node, i);
            if (ch) {
                WalkButtonsInSubtree(ch, cfg);
            }
        }
    } catch (...) {
    }
}

void SweepRecordedFramesForGen(const OpticalSettings& cfg) {
    if (!cfg.enabled || g_unloading.load()) {
        return;
    }
    std::erase_if(g_frameEntries, [](const auto& e) { return !e.frame.get(); });
    std::vector<wux::FrameworkElement> toSweep;
    size_t alreadySwept = 0;
    for (auto& e : g_frameEntries) {
        auto fr = e.frame.get();
        if (!fr) continue;
        if (e.sweptGen != cfg.generation) {
            e.sweptGen = cfg.generation;
            toSweep.push_back(std::move(fr));
        } else {
            ++alreadySwept;
        }
    }
    for (const auto& fr : toSweep) {
        WalkButtonsInSubtree(fr, cfg);
    }
    g_sweptFrameCount.store(alreadySwept + toSweep.size(), std::memory_order_release);
    g_lastSweepGeneration.store(cfg.generation, std::memory_order_release);
}

void ReapplyOrRestoreAllTrackedOnUiThread(bool restoreOnly) {
    OpticalSettings cfg;
    std::vector<std::pair<void*, ButtonRecord>> snapshot;
    std::vector<wux::FrameworkElement> frames;
    {
        std::lock_guard<std::mutex> lock(g_stateMutex);
        cfg = g_settings;
        if (restoreOnly) {
            cfg.enabled = false;
        }
        std::erase_if(g_buttons, [](const auto& kv) { return !kv.second.weakFe.get(); });
        for (const auto& kv : g_buttons) {
            snapshot.push_back(kv);
        }
    }

    for (auto& [key, rec] : snapshot) {
        auto fe = rec.weakFe.get();
        if (!fe) continue;
        ApplyOrCaptureButton(fe, cfg);
    }

    if (!restoreOnly && cfg.enabled && !g_unloading.load()) {
        SweepRecordedFramesForGen(cfg);
    }
    FlushStatusFile(true);
}

void HandleFrameElementLifecycle(IUnknown* unk, bool requireTaskbarFrameClass, bool applySelfButton, bool respectSweepFastPath) {
    if (!unk || g_unloading.load()) {
        return;
    }
    uint32_t curGen = g_settingsGeneration.load(std::memory_order_acquire);
    if (respectSweepFastPath &&
        g_lastSweepGeneration.load(std::memory_order_acquire) == curGen &&
        g_sweptFrameCount.load(std::memory_order_acquire) >= g_targetTaskbarCount.load(std::memory_order_acquire)) {
        return;
    }
    try {
        wux::FrameworkElement fe{nullptr};
        if (FAILED(unk->QueryInterface(winrt::guid_of<wux::FrameworkElement>(), winrt::put_abi(fe))) || !fe) {
            return;
        }
        if (requireTaskbarFrameClass && winrt::get_class_name(fe) != L"Taskbar.TaskbarFrame") {
            return;
        }
        RecordTaskbarFrame(fe);
        OpticalSettings cfg;
        {
            std::lock_guard<std::mutex> lock(g_stateMutex);
            cfg = g_settings;
        }
        if (cfg.enabled) {
            if (applySelfButton) {
                ApplyOrCaptureButton(fe, cfg);
            }
            SweepRecordedFramesForGen(cfg);
            FlushStatusFile(false);
        }
    } catch (...) {
    }
}

// ---------------------------------------------------------------------------
// Symbol Hooks
// ---------------------------------------------------------------------------

int WINAPI TaskListButton_put_Icon_Hook(void* pThis, void* value) {
    if (t_inPutIconOverride || g_unloading.load()) {
        return TaskListButton_put_Icon_Original(pThis, value);
    }

    try {
        IUnknown* itbUnk = reinterpret_cast<IUnknown*>(pThis);
        wux::FrameworkElement fe{nullptr};
        if (itbUnk) {
            itbUnk->QueryInterface(winrt::guid_of<wux::FrameworkElement>(), winrt::put_abi(fe));
            EnsureTaskListButtonIid(itbUnk, itbUnk);
            if (fe) {
                RecordTaskbarFrame(fe);
            }
        }

        if (!value) {
            void* key = GetCanonicalUnknown(itbUnk);
            if (key) {
                std::lock_guard<std::mutex> lock(g_stateMutex);
                g_buttons.erase(key);
            }
            return TaskListButton_put_Icon_Original(pThis, value);
        }

        std::vector<uint8_t> inBytes;
        if (!ReadStreamToBytes(reinterpret_cast<IUnknown*>(value), inBytes)) {
            return TaskListButton_put_Icon_Original(pThis, value);
        }

        uint64_t inHash = Fnv1a64(inBytes.data(), inBytes.size());
        void* key = GetCanonicalUnknown(itbUnk);
        OpticalSettings cfg;
        bool isFeedbackEcho = false;
        {
            std::lock_guard<std::mutex> lock(g_stateMutex);
            cfg = g_settings;
            if (key) {
                auto& rec = g_buttons[key];
                if (fe) rec.weakFe = winrt::make_weak(fe);
                if (inHash == rec.lastGlassPngHash && rec.lastGlassPngHash != 0) {
                    isFeedbackEcho = true;
                } else {
                    rec.origStreamBytes = inBytes;
                    rec.origStreamHash = inHash;
                }
            }
        }

        if (isFeedbackEcho || !cfg.enabled) {
            if (key && !cfg.enabled) {
                std::lock_guard<std::mutex> lock(g_stateMutex);
                g_buttons[key].appliedGeneration = 0;
            }
            int hr = TaskListButton_put_Icon_Original(pThis, value);
            FlushStatusFile(false);
            return hr;
        }

        std::vector<uint8_t> pngBytes;
        uint64_t pngHash = 0;
        uint32_t w = 0, h = 0;
        if (TransformBytesWithLru(inBytes, cfg, pngBytes, pngHash, w, h)) {
            wss::IRandomAccessStream outRas{nullptr};
            if (BytesToRandomAccessStream(pngBytes, outRas) && outRas) {
                if (key) {
                    std::lock_guard<std::mutex> lock(g_stateMutex);
                    auto& rec = g_buttons[key];
                    rec.lastGlassPngHash = pngHash;
                    rec.appliedGeneration = cfg.generation;
                    rec.lastWidth = w;
                    rec.lastHeight = h;
                }
                t_inPutIconOverride = true;
                int hr = TaskListButton_put_Icon_Original(pThis, winrt::get_abi(outRas));
                t_inPutIconOverride = false;
                // Sweep any cold siblings on the parent TaskbarFrame once per generation.
                SweepRecordedFramesForGen(cfg);
                FlushStatusFile(false);
                return hr;
            }
        }
    } catch (...) {
        g_errorCount.fetch_add(1);
    }

    return TaskListButton_put_Icon_Original(pThis, value);
}

int WINAPI TaskListButton_OnApplyTemplate_Hook(void* pThis) {
    int hr = TaskListButton_OnApplyTemplate_Original(pThis);
    HandleFrameElementLifecycle(reinterpret_cast<IUnknown*>(pThis), false, true, false);
    return hr;
}

int WINAPI TaskListButton_put_IsActive_Hook(void* pThis, bool value) {
    int hr = TaskListButton_put_IsActive_Original(pThis, value);
    if (!g_unloading.load() && pThis) {
        IUnknown* itbUnk = reinterpret_cast<IUnknown*>(pThis);
        EnsureTaskListButtonIid(itbUnk, itbUnk);
        HandleFrameElementLifecycle(itbUnk, false, true, true);
    }
    return hr;
}

int WINAPI TaskbarFrame_MeasureOverride_Hook(void* pThis, uint64_t availSizeBits, void* pReturnSize) {
    int hr = TaskbarFrame_MeasureOverride_Original(pThis, availSizeBits, pReturnSize);
    HandleFrameElementLifecycle(reinterpret_cast<IUnknown*>(pThis), true, false, true);
    return hr;
}

int WINAPI TaskListButton_ArrangeOverride_Hook(void* pThis, uint64_t finalSizeBits, void* pReturnSize) {
    int hr = TaskListButton_ArrangeOverride_Original(pThis, finalSizeBits, pReturnSize);
    HandleFrameElementLifecycle(reinterpret_cast<IUnknown*>(pThis), false, false, true);
    return hr;
}

LONG DecodeTaskbarFrameOffset() noexcept {
    const uint8_t* p = reinterpret_cast<const uint8_t*>(TaskbarController_OnGroupingModeChanged_Raw);
    if (!IsReadableMemory(p, 16)) {
        return 0;
    }
    if (p[0] == 0x48 && p[1] == 0x83 && p[2] == 0xEC &&
        (p[4] == 0x48 || p[4] == 0x4C) && p[5] == 0x8B && (p[6] & 0xC0) == 0x80) {
        LONG off = *reinterpret_cast<const LONG*>(p + 7);
        if (off >= 0x100 && off <= 0x800) {
            return off;
        }
    }
    return 0;
}

bool TryExtractTaskbarFrameFromImpl(void* implPtr, wux::FrameworkElement& outFe) {
    outFe = nullptr;
    if (!IsReadableMemory(implPtr, sizeof(void*) * 4)) {
        return false;
    }
    void* const* slots = reinterpret_cast<void* const*>(implPtr);
    for (int idx : {1, 0, 2}) {
        void* cand = slots[idx];
        if (!IsReadableMemory(cand, sizeof(void*))) {
            continue;
        }
        void* const* vtbl = *reinterpret_cast<void* const* const*>(cand);
        if (!IsReadableMemory(vtbl, sizeof(void*))) {
            continue;
        }
        try {
            wux::FrameworkElement fe{nullptr};
            if (SUCCEEDED(reinterpret_cast<IUnknown*>(cand)->QueryInterface(
                    winrt::guid_of<wux::FrameworkElement>(), winrt::put_abi(fe))) &&
                fe && winrt::get_class_name(fe) == L"Taskbar.TaskbarFrame") {
                outFe = std::move(fe);
                return true;
            }
        } catch (...) {
        }
    }
    return false;
}

void WINAPI TaskbarController_UpdateFrameHeight_Hook(void* pThis) {
    TaskbarController_UpdateFrameHeight_Original(pThis);
    if (g_unloading.load() || !pThis) {
        return;
    }
    g_diagUpdFhHits.fetch_add(1, std::memory_order_relaxed);
    uint32_t curGen = g_settingsGeneration.load(std::memory_order_acquire);
    if (g_lastSweepGeneration.load(std::memory_order_acquire) == curGen &&
        g_sweptFrameCount.load(std::memory_order_acquire) >= g_targetTaskbarCount.load(std::memory_order_acquire)) {
        return;
    }
    static LONG taskbarFrameOffset = DecodeTaskbarFrameOffset();
    if (taskbarFrameOffset <= 0 || !IsReadableMemory(reinterpret_cast<const uint8_t*>(pThis) + taskbarFrameOffset, sizeof(void*))) {
        return;
    }
    void* tfImpl = *reinterpret_cast<void* const*>(reinterpret_cast<const uint8_t*>(pThis) + taskbarFrameOffset);
    wux::FrameworkElement fe{nullptr};
    if (!TryExtractTaskbarFrameFromImpl(tfImpl, fe)) {
        return;
    }
    RecordTaskbarFrame(fe);
    OpticalSettings cfg;
    {
        std::lock_guard<std::mutex> lock(g_stateMutex);
        cfg = g_settings;
    }
    if (cfg.enabled) {
        SweepRecordedFramesForGen(cfg);
        FlushStatusFile(false);
    }
}

std::vector<HWND> EnumCurrentProcessTaskbarWindows() {
    std::vector<HWND> out;
    struct Ctx {
        DWORD pid;
        std::vector<HWND>* list;
    } ctx{GetCurrentProcessId(), &out};
    EnumWindows(
        [](HWND hWnd, LPARAM lp) -> BOOL {
            auto* c = reinterpret_cast<Ctx*>(lp);
            DWORD pid = 0;
            if (!GetWindowThreadProcessId(hWnd, &pid) || pid != c->pid) {
                return TRUE;
            }
            WCHAR cls[48]{};
            if (GetClassNameW(hWnd, cls, ARRAYSIZE(cls)) > 0) {
                if (_wcsicmp(cls, L"Shell_TrayWnd") == 0) {
                    c->list->insert(c->list->begin(), hWnd);
                } else if (_wcsicmp(cls, L"Shell_SecondaryTrayWnd") == 0) {
                    c->list->push_back(hWnd);
                }
            }
            return TRUE;
        },
        reinterpret_cast<LPARAM>(&ctx));
    return out;
}

using RunFromWindowThreadProc_t = void(WINAPI*)(void*);
bool RunFromWindowThread(HWND window, RunFromWindowThreadProc_t proc, void* param) {
    static const UINT msgId = RegisterWindowMessageW(L"Windhawk_RunFromWindowThread_" WH_MOD_ID);
    struct Payload {
        RunFromWindowThreadProc_t fn;
        void* p;
    };
    DWORD tid = GetWindowThreadProcessId(window, nullptr);
    if (!tid) return false;
    if (tid == GetCurrentThreadId()) {
        proc(param);
        return true;
    }
    HHOOK hook = SetWindowsHookExW(
        WH_CALLWNDPROC,
        [](int code, WPARAM wp, LPARAM lp) -> LRESULT {
            if (code == HC_ACTION) {
                const auto* cwp = reinterpret_cast<const CWPSTRUCT*>(lp);
                if (cwp->message == msgId) {
                    auto* pl = reinterpret_cast<Payload*>(cwp->lParam);
                    pl->fn(pl->p);
                }
            }
            return CallNextHookEx(nullptr, code, wp, lp);
        },
        nullptr, tid);
    if (!hook) return false;
    Payload pl{proc, param};
    DWORD_PTR smRes = 0;
    LRESULT sent = SendMessageTimeoutW(
        window, msgId, 0, reinterpret_cast<LPARAM>(&pl),
        SMTO_ABORTIFHUNG | SMTO_BLOCK, 1500, &smRes);
    UnhookWindowsHookEx(hook);
    return sent != 0;
}

HMODULE GetTaskbarViewModule() {
    if (HMODULE m = GetModuleHandleW(L"Taskbar.View.dll")) {
        return m;
    }
    return GetModuleHandleW(L"ExplorerExtensions.dll");
}

}  // namespace

BOOL Wh_ModInit() {
    InitStatusPath();
    LoadSettings();

    HMODULE tbView = GetTaskbarViewModule();
    if (!tbView) {
        Wh_Log(L"Taskbar.View.dll not loaded");
        FlushStatusFile(true);
        return FALSE;
    }

    WindhawkUtils::SYMBOL_HOOK viewHooks[] = {
        {
            {LR"(public: virtual int __cdecl winrt::impl::produce<struct winrt::Taskbar::implementation::TaskListButton,struct winrt::Taskbar::ITaskListButton>::put_Icon(void *))"},
            &TaskListButton_put_Icon_Original,
            TaskListButton_put_Icon_Hook,
        },
        {
            {LR"(public: virtual int __cdecl winrt::impl::produce<struct winrt::Taskbar::implementation::TaskListButton,struct winrt::Taskbar::ITaskListButton>::get_Icon(void * *))"},
            &TaskListButton_get_Icon_Raw,
            nullptr,
            true,
        },
        {
            {LR"(public: virtual int __cdecl winrt::impl::produce<struct winrt::Taskbar::implementation::TaskListButton,struct winrt::Windows::UI::Xaml::IFrameworkElementOverrides>::OnApplyTemplate(void))"},
            &TaskListButton_OnApplyTemplate_Original,
            TaskListButton_OnApplyTemplate_Hook,
            true,
        },
        {
            {LR"(public: virtual int __cdecl winrt::impl::produce<struct winrt::Taskbar::implementation::TaskListButton,struct winrt::Taskbar::ITaskListButton>::put_IsActive(bool))"},
            &TaskListButton_put_IsActive_Original,
            TaskListButton_put_IsActive_Hook,
            true,
        },
        {
            {LR"(public: virtual int __cdecl winrt::impl::produce<struct winrt::Taskbar::implementation::TaskbarFrame,struct winrt::Windows::UI::Xaml::IFrameworkElementOverrides>::MeasureOverride(struct winrt::Windows::Foundation::Size,struct winrt::Windows::Foundation::Size *))"},
            &TaskbarFrame_MeasureOverride_Original,
            TaskbarFrame_MeasureOverride_Hook,
            true,
        },
        {
            {LR"(public: virtual int __cdecl winrt::impl::produce<struct winrt::Taskbar::implementation::TaskListButton,struct winrt::Windows::UI::Xaml::IFrameworkElementOverrides>::ArrangeOverride(struct winrt::Windows::Foundation::Size,struct winrt::Windows::Foundation::Size *))"},
            &TaskListButton_ArrangeOverride_Original,
            TaskListButton_ArrangeOverride_Hook,
            true,
        },
        {
            {LR"(private: void __cdecl winrt::Taskbar::implementation::TaskbarController::OnGroupingModeChanged(void))"},
            &TaskbarController_OnGroupingModeChanged_Raw,
            nullptr,
            true,
        },
        {
            {LR"(private: void __cdecl winrt::Taskbar::implementation::TaskbarController::UpdateFrameHeight(void))"},
            &TaskbarController_UpdateFrameHeight_Original,
            TaskbarController_UpdateFrameHeight_Hook,
            true,
        },
    };

    if (!WindhawkUtils::HookSymbols(tbView, viewHooks, ARRAYSIZE(viewHooks))) {
        Wh_Log(L"HookSymbols(Taskbar.View) failed");
        g_errorCount.fetch_add(1);
        FlushStatusFile(true);
        return FALSE;
    }

    Wh_Log(L"LiquidGlassTaskbar initialized: put_Icon=%p get_Icon=%p ApplyTpl=%p IsActive=%p Measure=%p Arrange=%p UpdFH=%p",
           TaskListButton_put_Icon_Original, TaskListButton_get_Icon_Raw,
           TaskListButton_OnApplyTemplate_Original, TaskListButton_put_IsActive_Original,
           TaskbarFrame_MeasureOverride_Original,
           TaskListButton_ArrangeOverride_Original, TaskbarController_UpdateFrameHeight_Original);
    FlushStatusFile(true);
    return TRUE;
}

void ApplyCurrentSettingsAndSweepIfCold(bool triggerColdNudge) {
    OpticalSettings cfg;
    {
        std::lock_guard<std::mutex> lock(g_stateMutex);
        cfg = g_settings;
    }
    auto hwnds = EnumCurrentProcessTaskbarWindows();
    g_targetTaskbarCount.store(std::max<size_t>(1, hwnds.size()), std::memory_order_release);
    if (!hwnds.empty()) {
        RunFromWindowThread(
            hwnds[0],
            [](PVOID) { ReapplyOrRestoreAllTrackedOnUiThread(false); },
            nullptr);
        if (triggerColdNudge && cfg.enabled && !g_unloading.load() &&
            g_sweptFrameCount.load(std::memory_order_acquire) < g_targetTaskbarCount.load(std::memory_order_acquire)) {
            g_diagNudgeCalls.fetch_add(1, std::memory_order_relaxed);
            // Trigger TrayUI::_HandleSettingChange -> TaskbarController::UpdateFrameHeight immediately on cold start.
            uint32_t sentMask = 0;
            if (SendMessageTimeoutW(hwnds[0], WM_SETTINGCHANGE, SPI_SETLOGICALDPIOVERRIDE, 0,
                                    SMTO_ABORTIFHUNG, 500, nullptr)) {
                sentMask |= 1;
            }
            if (g_sweptFrameCount.load(std::memory_order_acquire) < g_targetTaskbarCount.load(std::memory_order_acquire)) {
                if (SendMessageTimeoutW(hwnds[0], WM_SETTINGCHANGE, 0,
                                        reinterpret_cast<LPARAM>(L"TraySettings"),
                                        SMTO_ABORTIFHUNG, 500, nullptr)) {
                    sentMask |= 2;
                }
            }
            if (g_sweptFrameCount.load(std::memory_order_acquire) < g_targetTaskbarCount.load(std::memory_order_acquire)) {
                if (SendMessageTimeoutW(hwnds[0], WM_SETTINGCHANGE, 0,
                                        reinterpret_cast<LPARAM>(L"VisualEffects"),
                                        SMTO_ABORTIFHUNG, 500, nullptr)) {
                    sentMask |= 4;
                }
            }
            if (g_sweptFrameCount.load(std::memory_order_acquire) < g_targetTaskbarCount.load(std::memory_order_acquire)) {
                if (SendMessageTimeoutW(hwnds[0], WM_SETTINGCHANGE, 0,
                                        reinterpret_cast<LPARAM>(L"WindowMetrics"),
                                        SMTO_ABORTIFHUNG, 500, nullptr)) {
                    sentMask |= 8;
                }
            }
            g_diagNudgeSent.store(sentMask, std::memory_order_relaxed);
            RunFromWindowThread(
                hwnds[0],
                [](PVOID) { ReapplyOrRestoreAllTrackedOnUiThread(false); },
                nullptr);
        }
    }
    FlushStatusFile(true);
}

void Wh_ModAfterInit() {
    OpticalSettings cfg;
    {
        std::lock_guard<std::mutex> lock(g_stateMutex);
        cfg = g_settings;
    }
    if (cfg.enabled && !g_unloading.load()) {
        ApplyCurrentSettingsAndSweepIfCold(true);
    }
}

void Wh_ModSettingsChanged() {
    LoadSettings();
    ApplyCurrentSettingsAndSweepIfCold(true);
}

void Wh_ModBeforeUninit() {
    g_unloading.store(true, std::memory_order_release);
    for (int i = 0; i < 200 && g_pendingAsyncOps.load(std::memory_order_acquire) > 0; ++i) {
        Sleep(10);
    }
    auto hwnds = EnumCurrentProcessTaskbarWindows();
    if (!hwnds.empty()) {
        RunFromWindowThread(
            hwnds[0],
            [](PVOID) { ReapplyOrRestoreAllTrackedOnUiThread(true); },
            nullptr);
    }
    {
        std::lock_guard<std::mutex> lock(g_stateMutex);
        g_buttons.clear();
        g_taskbarFrames.clear();
        g_frameEntries.clear();
        g_lruList.clear();
        g_lruMap.clear();
        g_lruTotalBytes = 0;
    }
    g_sweptFrameCount.store(0, std::memory_order_release);
    FlushStatusFile(true);
}

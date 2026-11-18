// ==WindhawkMod==
// @id              local@liquid-glass-tray-icons
// @name            Liquid Glass System Tray Icons
// @description     Real-time optical Liquid Glass transformation for Windows 11 system tray & overflow (^) icons
// @version         1.0.0
// @author          LiquidGlassIconCenter
// @include         explorer.exe
// @architecture    x86-64
// @compilerOptions -lole32 -loleaut32 -lruntimeobject -lshcore
// ==/WindhawkMod==

// ==WindhawkModReadme==
/*
# Liquid Glass System Tray Icons (Experimental 4th Scope)

Applies real-time optical Liquid Glass rendering (translucent ice-white glass,
frosted highlights, directional inner bevel rim and seam glints) to Windows 11
Notification Area (System Tray) and Overflow (`^`) icons inside `SystemTray.dll`
without mutating on-disk shortcuts or colliding with layout/spacing mods.
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

#include <winrt/Windows.Foundation.h>
#include <winrt/Windows.Graphics.Imaging.h>
#include <winrt/Windows.Storage.Streams.h>
#include <winrt/Windows.UI.Core.h>
#include <winrt/Windows.UI.Xaml.Controls.h>
#include <winrt/Windows.UI.Xaml.Media.Imaging.h>
#include <winrt/Windows.UI.Xaml.Media.h>
#include <winrt/Windows.UI.Xaml.h>
#include <winrt/base.h>

namespace wf = winrt::Windows::Foundation;
namespace wgi = winrt::Windows::Graphics::Imaging;
namespace wss = winrt::Windows::Storage::Streams;
namespace wuc = winrt::Windows::UI::Core;
namespace wux = winrt::Windows::UI::Xaml;
namespace wuxc = winrt::Windows::UI::Xaml::Controls;
namespace wuxm = winrt::Windows::UI::Xaml::Media;
namespace wuxmi = winrt::Windows::UI::Xaml::Media::Imaging;

namespace {

constexpr size_t kMaxCacheEntries = 128;
constexpr size_t kMaxCacheBytes = 12ull * 1024ull * 1024ull;
constexpr size_t kMaxImgRecords = 512;
constexpr int kMaxConcurrentRtb = 4;
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
    RawImage premulGlass;
    uint64_t premulHash = 0;
};

struct TrayImgRecord {
    winrt::weak_ref<wuxc::Image> weakImg;
    wuc::CoreDispatcher dispatcher{nullptr};
    wuxm::ImageSource origSource{nullptr};
    wuxmi::SoftwareBitmapSource glassSbs{nullptr};
    void* origSourceKey = nullptr;
    void* glassSbsKey = nullptr;
    RawImage origBgra;
    uint64_t origHash = 0;
    uint32_t appliedGeneration = 0;
    uint32_t captureAttemptedGen = 0;
    bool captureInFlight = false;
    bool callbacksAttached = false;
    int64_t propToken = 0;
    winrt::event_token unloadedToken{};
};

struct PendingRtbOp {
    wf::IAsyncAction renderOp{nullptr};
    wf::IAsyncOperation<wss::IBuffer> pixOp{nullptr};
};

std::mutex g_stateMutex;
OpticalSettings g_settings;
std::atomic<bool> g_unloading{false};
std::atomic<uint32_t> g_settingsGeneration{1};
std::atomic<int> g_pendingAsyncOps{0};
std::atomic<int> g_activeRtbCount{0};
std::atomic<bool> g_hookInstalled{false};

std::unordered_map<void*, TrayImgRecord> g_imgMap;
std::list<CacheEntry> g_lruList;
std::unordered_map<CacheKey, std::list<CacheEntry>::iterator, CacheKeyHash> g_lruMap;
size_t g_lruTotalBytes = 0;
winrt::weak_ref<wux::FrameworkElement> g_trayFrameWeak;
winrt::weak_ref<wux::FrameworkElement> g_overflowWeak;
std::atomic<uint64_t> g_lastTraySweepTick{0};
std::atomic<uint32_t> g_lastTraySweepGen{0};
std::atomic<uint64_t> g_lastOverflowSweepTick{0};
std::atomic<uint32_t> g_lastOverflowSweepGen{0};

std::mutex g_keepMutex;
std::vector<std::shared_ptr<PendingRtbOp>> g_keepRtbOps;
std::vector<wf::IAsyncAction> g_keepSbsOps;

// Telemetry counters -> %TEMP%\lgic_windhawk_tray_status.json
std::atomic<uint64_t> g_renderCount{0};
std::atomic<uint64_t> g_cacheHits{0};
std::atomic<uint64_t> g_lowResCount{0};
std::atomic<uint64_t> g_errorCount{0};
std::atomic<uint64_t> g_measureCalls{0};
std::atomic<uint64_t> g_arrangeCalls{0};
std::atomic<uint64_t> g_overflowCalls{0};
std::atomic<uint64_t> g_nudgeCalls{0};
std::atomic<uint64_t> g_vmPutCalls{0};
std::atomic<uint64_t> g_rtbQueued{0};
std::atomic<uint64_t> g_rtbOk{0};
std::atomic<uint64_t> g_rtbFail{0};
std::atomic<uint64_t> g_xamlRootCalls{0};
std::atomic<uint64_t> g_ovInitCalls{0};
std::atomic<uint64_t> g_ovShowCalls{0};
std::atomic<uint64_t> g_ovUpdateCalls{0};
std::atomic<bool> g_systemTrayFound{false};
std::atomic<bool> g_overflowFound{false};
std::atomic<uint64_t> g_lastStatusFlushTick{0};
std::wstring g_statusPath;
std::mutex g_statusMutex;

thread_local bool t_inOurSourceOverride = false;

using MeasureOverride_t = int32_t(WINAPI*)(void* pThis, uint64_t availSizeBits, void* pReturnSize);
using OnApplyTemplate_t = int32_t(WINAPI*)(void* pThis);
using PutIconImageSource_t = int32_t(WINAPI*)(void* pThis, void* value);
using SimpleThisProc_t = void(WINAPI*)(void* pThis);
using OverflowManagerShow_t = void(WINAPI*)(void* pThis, POINT pt, int32_t inputDeviceKind);
using CTaskBand_GetTaskbarHost_t = void*(WINAPI*)(void* pThis, void** result);

MeasureOverride_t SystemTrayFrame_ArrangeOverride_Original = nullptr;
MeasureOverride_t Overflow_ArrangeOverride_Original = nullptr;
OnApplyTemplate_t Overflow_OnApplyTemplate_Original = nullptr;
MeasureOverride_t ImageIconContent_MeasureOverride_Original = nullptr;
MeasureOverride_t TaskbarFrame_MeasureOverride_Original = nullptr;
PutIconImageSource_t ImageIconVM_put_IconImageSource_Original = nullptr;
SimpleThisProc_t OverflowManager_InitializeIfNeeded_Original = nullptr;
OverflowManagerShow_t OverflowManager_Show_Original = nullptr;
SimpleThisProc_t SystemTrayController_UpdateOverflowIcons_Original = nullptr;

void* CTaskBand_ITaskListWndSite_vftable = nullptr;
CTaskBand_GetTaskbarHost_t CTaskBand_GetTaskbarHost_Original = nullptr;
void* TaskbarHost_FrameHeight_Original = nullptr;
SimpleThisProc_t std__Ref_count_base__Decref_Original = nullptr;

// ---------------------------------------------------------------------------
// Utility, Hashing & Telemetry
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

void* GetCanonicalUnknown(IUnknown* unk) noexcept {
    if (!unk) return nullptr;
    winrt::com_ptr<IUnknown> canon;
    if (SUCCEEDED(unk->QueryInterface(IID_PPV_ARGS(canon.put())))) {
        return canon.get();
    }
    return nullptr;
}

template <typename T>
void* GetCanonicalInspectable(const T& obj) noexcept {
    if (!obj) return nullptr;
    return GetCanonicalUnknown(reinterpret_cast<IUnknown*>(winrt::get_abi(obj)));
}

void InitStatusPath() {
    wchar_t tempDir[MAX_PATH]{};
    DWORD n = GetTempPathW(ARRAYSIZE(tempDir), tempDir);
    if (n > 0 && n < ARRAYSIZE(tempDir)) {
        g_statusPath = std::wstring(tempDir) + L"lgic_windhawk_tray_status.json";
    } else {
        g_statusPath = L"C:\\Windows\\Temp\\lgic_windhawk_tray_status.json";
    }
}

void FlushStatusFile(bool force) {
    uint64_t now = GetTickCount64();
    uint64_t prev = g_lastStatusFlushTick.load(std::memory_order_relaxed);
    if (!force && (now - prev) < 1000) {
        return;
    }
    g_lastStatusFlushTick.store(now, std::memory_order_relaxed);

    size_t imgCount = 0;
    size_t cacheEntries = 0;
    size_t cacheBytes = 0;
    OpticalSettings snap{};
    {
        std::lock_guard<std::mutex> lock(g_stateMutex);
        imgCount = g_imgMap.size();
        cacheEntries = g_lruList.size();
        cacheBytes = g_lruTotalBytes;
        snap = g_settings;
    }

    char json[1024];
    int len = _snprintf_s(
        json, sizeof(json), _TRUNCATE,
        "{\"active\":%s,\"unloading\":%s,\"pid\":%lu,\"generation\":%u,"
        "\"tracked_buttons\":%zu,\"cache_entries\":%zu,\"cache_bytes\":%zu,"
        "\"max_cache_entries\":%zu,\"max_cache_bytes\":%zu,"
        "\"render_count\":%llu,\"cache_hits\":%llu,\"low_res_count\":%llu,\"error_count\":%llu,"
        "\"measure_calls\":%llu,\"arrange_calls\":%llu,\"overflow_calls\":%llu,\"nudge_calls\":%llu,"
        "\"vm_put_calls\":%llu,\"rtb_queued\":%llu,\"rtb_ok\":%llu,\"rtb_fail\":%llu,"
        "\"xaml_root_calls\":%llu,\"overflow_init_calls\":%llu,\"overflow_show_calls\":%llu,\"overflow_update_calls\":%llu,"
        "\"system_tray_found\":%s,\"overflow_found\":%s,"
        "\"pending_async\":%d,\"hook_ok\":%s,"
        "\"opacity\":%d,\"frost\":%d,\"glow\":%d,\"dark_protect\":%d}",
        (snap.enabled && !g_unloading.load()) ? "true" : "false",
        g_unloading.load() ? "true" : "false",
        GetCurrentProcessId(),
        snap.generation,
        imgCount, cacheEntries, cacheBytes,
        kMaxCacheEntries, kMaxCacheBytes,
        static_cast<unsigned long long>(g_renderCount.load()),
        static_cast<unsigned long long>(g_cacheHits.load()),
        static_cast<unsigned long long>(g_lowResCount.load()),
        static_cast<unsigned long long>(g_errorCount.load()),
        static_cast<unsigned long long>(g_measureCalls.load()),
        static_cast<unsigned long long>(g_arrangeCalls.load()),
        static_cast<unsigned long long>(g_overflowCalls.load()),
        static_cast<unsigned long long>(g_nudgeCalls.load()),
        static_cast<unsigned long long>(g_vmPutCalls.load()),
        static_cast<unsigned long long>(g_rtbQueued.load()),
        static_cast<unsigned long long>(g_rtbOk.load()),
        static_cast<unsigned long long>(g_rtbFail.load()),
        static_cast<unsigned long long>(g_xamlRootCalls.load()),
        static_cast<unsigned long long>(g_ovInitCalls.load()),
        static_cast<unsigned long long>(g_ovShowCalls.load()),
        static_cast<unsigned long long>(g_ovUpdateCalls.load()),
        g_systemTrayFound.load() ? "true" : "false",
        g_overflowFound.load() ? "true" : "false",
        g_pendingAsyncOps.load(),
        g_hookInstalled.load() ? "true" : "false",
        snap.opacityPct, snap.frostPct, snap.glowPct, snap.darkProtectPct);
    if (len <= 0) return;

    std::lock_guard<std::mutex> lock(g_statusMutex);
    if (g_statusPath.empty()) InitStatusPath();
    HANDLE h = CreateFileW(
        g_statusPath.c_str(), GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr,
        CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h != INVALID_HANDLE_VALUE) {
        DWORD written = 0;
        WriteFile(h, json, static_cast<DWORD>(len), &written, nullptr);
        CloseHandle(h);
    }
}

void LoadSettings() {
    OpticalSettings next;
    next.enabled = Wh_GetIntSetting(L"Enabled") != 0;
    next.opacityPct = std::clamp(Wh_GetIntSetting(L"Opacity"), 20, 180);
    next.frostPct = std::clamp(Wh_GetIntSetting(L"Frost"), 20, 130);
    next.glowPct = std::clamp(Wh_GetIntSetting(L"Glow"), 30, 180);
    next.darkProtectPct = std::clamp(Wh_GetIntSetting(L"DarkProtect"), 50, 200);

    std::lock_guard<std::mutex> lock(g_stateMutex);
    bool changed = (next.enabled != g_settings.enabled ||
                    next.opacityPct != g_settings.opacityPct ||
                    next.frostPct != g_settings.frostPct ||
                    next.glowPct != g_settings.glowPct ||
                    next.darkProtectPct != g_settings.darkProtectPct);
    next.generation = g_settings.generation + (changed ? 1u : 0u);
    g_settings = next;
    g_settingsGeneration.store(next.generation, std::memory_order_release);
    if (changed) {
        for (auto it = g_lruList.begin(); it != g_lruList.end();) {
            if (it->key.generation != next.generation) {
                g_lruTotalBytes -= it->premulGlass.bgra.size();
                g_lruMap.erase(it->key);
                it = g_lruList.erase(it);
            } else {
                ++it;
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Optical Liquid Glass Renderer (1:1 Parity With Python renderer.py)
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
    for (float& kv : kernel) kv *= invSum;

    tmp.resize(static_cast<size_t>(w) * h);
    for (int y = 0; y < h; ++y) {
        const float* row = &src[static_cast<size_t>(y) * w];
        float* outRow = &tmp[static_cast<size_t>(y) * w];
        for (int x = 0; x < w; ++x) {
            float acc = 0.0f;
            for (int k = -r; k <= r; ++k) {
                int sx = x + k;
                float val = 0.0f;
                if (sx >= 0 && sx < w) val = row[sx];
                else if (!zeroPad) val = row[std::clamp(sx, 0, w - 1)];
                acc += val * kernel[static_cast<size_t>(k + r)];
            }
            outRow[x] = acc;
        }
    }
    for (int y = 0; y < h; ++y) {
        float* outRow = &dst[static_cast<size_t>(y) * w];
        for (int x = 0; x < w; ++x) {
            float acc = 0.0f;
            for (int k = -r; k <= r; ++k) {
                int sy = y + k;
                float val = 0.0f;
                if (sy >= 0 && sy < h) val = tmp[static_cast<size_t>(sy) * w + x];
                else if (!zeroPad) val = tmp[static_cast<size_t>(std::clamp(sy, 0, h - 1)) * w + x];
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
        if (x >= 0 && x < w && y >= 0 && y < h) return src[static_cast<size_t>(y) * w + x];
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
    return (ax >= 3.0) ? 0.0 : (Sinc(x) * Sinc(x / 3.0));
}

inline double CatmullRomWeight(double x) noexcept {
    double ax = std::abs(x);
    if (ax <= 1.0) return (1.5 * ax - 2.5) * ax * ax + 1.0;
    if (ax < 2.0) return ((-0.5 * ax + 2.5) * ax - 4.0) * ax + 2.0;
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

    double scaleX = static_cast<double>(sw) / dw;
    double radiusX = scaleX > 1.0 ? supportBase * scaleX : supportBase;
    double invScaleX = scaleX > 1.0 ? (1.0 / scaleX) : 1.0;
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
            for (auto& p : xTaps[static_cast<size_t>(dx)]) p.second = static_cast<float>(p.second / wsum);
        }
    }

    size_t midN = static_cast<size_t>(dw) * sh;
    std::vector<float> mR(midN), mG(midN), mB(midN), mA(midN);
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
            for (auto& p : yTaps[static_cast<size_t>(dy)]) p.second = static_cast<float>(p.second / wsum);
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

void RenderOpticalLiquidGlassPremul(const RawImage& src, const OpticalSettings& cfg, RawImage& outPremul) {
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

    outPremul.width = src.width;
    outPremul.height = src.height;
    outPremul.bgra.resize(srcN * 4);
    for (size_t i = 0; i < srcN; ++i) {
        float oa = std::min(Clamp01(finA[i]), 0.90f);
        if (oa <= 1e-4f) {
            outPremul.bgra[i * 4 + 0] = 0;
            outPremul.bgra[i * 4 + 1] = 0;
            outPremul.bgra[i * 4 + 2] = 0;
            outPremul.bgra[i * 4 + 3] = 0;
        } else {
            uint8_t a8 = static_cast<uint8_t>(std::min(255.0f, oa * 255.0f + 0.5f));
            float rVal = std::max(0.94f, Clamp01(finR[i]));
            float gVal = std::max(0.97f, Clamp01(finG[i]));
            uint32_t ua = a8;
            uint8_t r8 = static_cast<uint8_t>(std::min(255.0f, rVal * 255.0f + 0.5f));
            uint8_t g8 = static_cast<uint8_t>(std::min(255.0f, gVal * 255.0f + 0.5f));
            uint8_t b8 = 255u;
            outPremul.bgra[i * 4 + 0] = static_cast<uint8_t>((static_cast<uint32_t>(b8) * ua + 127u) / 255u);
            outPremul.bgra[i * 4 + 1] = static_cast<uint8_t>((static_cast<uint32_t>(g8) * ua + 127u) / 255u);
            outPremul.bgra[i * 4 + 2] = static_cast<uint8_t>((static_cast<uint32_t>(r8) * ua + 127u) / 255u);
            outPremul.bgra[i * 4 + 3] = a8;
        }
    }
}

void PremulBytesToStraight(const BYTE* src, uint32_t w, uint32_t h, RawImage& outStraight, size_t& outNonZeroAlpha) {
    outNonZeroAlpha = 0;
    outStraight.width = w;
    outStraight.height = h;
    size_t pxCount = static_cast<size_t>(w) * h;
    outStraight.bgra.resize(pxCount * 4);
    for (size_t i = 0; i < pxCount; ++i) {
        uint8_t b = src[i * 4 + 0];
        uint8_t g = src[i * 4 + 1];
        uint8_t r = src[i * 4 + 2];
        uint8_t a = src[i * 4 + 3];
        if (a == 0) {
            outStraight.bgra[i * 4 + 0] = 0;
            outStraight.bgra[i * 4 + 1] = 0;
            outStraight.bgra[i * 4 + 2] = 0;
            outStraight.bgra[i * 4 + 3] = 0;
        } else {
            ++outNonZeroAlpha;
            if (a == 255) {
                outStraight.bgra[i * 4 + 0] = b;
                outStraight.bgra[i * 4 + 1] = g;
                outStraight.bgra[i * 4 + 2] = r;
                outStraight.bgra[i * 4 + 3] = 255;
            } else {
                uint32_t ua = a;
                outStraight.bgra[i * 4 + 0] = static_cast<uint8_t>(std::min<uint32_t>(255u, (static_cast<uint32_t>(b) * 255u + ua / 2u) / ua));
                outStraight.bgra[i * 4 + 1] = static_cast<uint8_t>(std::min<uint32_t>(255u, (static_cast<uint32_t>(g) * 255u + ua / 2u) / ua));
                outStraight.bgra[i * 4 + 2] = static_cast<uint8_t>(std::min<uint32_t>(255u, (static_cast<uint32_t>(r) * 255u + ua / 2u) / ua));
                outStraight.bgra[i * 4 + 3] = a;
            }
        }
    }
}

bool ExtractStraightFromBuffer(const wss::IBuffer& buf, uint32_t w, uint32_t h, bool isPremul, RawImage& outStraight) {
    if (!buf || w == 0 || h == 0 || w > 512 || h > 512) return false;
    static constexpr GUID kIBufferByteAccessIid = {
        0x905a0fef, 0xbc53, 0x11df, {0x8c, 0x49, 0x00, 0x1e, 0x4f, 0xc6, 0x86, 0xda}};
    winrt::com_ptr<::Windows::Storage::Streams::IBufferByteAccess> access;
    IUnknown* rawBuf = reinterpret_cast<IUnknown*>(winrt::get_abi(buf));
    if (!rawBuf || FAILED(rawBuf->QueryInterface(kIBufferByteAccessIid, reinterpret_cast<void**>(access.put()))) || !access) {
        return false;
    }
    BYTE* pixels = nullptr;
    if (FAILED(access->Buffer(&pixels)) || !pixels) return false;
    uint64_t expected = static_cast<uint64_t>(w) * h * 4ull;
    if (static_cast<uint64_t>(buf.Length()) < expected) return false;
    size_t nonZero = 0;
    if (isPremul) {
        PremulBytesToStraight(pixels, w, h, outStraight, nonZero);
    } else {
        outStraight.width = w;
        outStraight.height = h;
        outStraight.bgra.assign(pixels, pixels + expected);
        for (size_t i = 3; i < expected; i += 4) {
            if (pixels[i] > 0) ++nonZero;
        }
    }
    return nonZero >= 4;
}

wgi::SoftwareBitmap MakePremulSoftwareBitmap(const RawImage& premul) {
    if (premul.width == 0 || premul.height == 0 || premul.bgra.empty()) return nullptr;
    try {
        uint32_t bytes = static_cast<uint32_t>(premul.bgra.size());
        wss::Buffer buf(bytes);
        buf.Length(bytes);
        static constexpr GUID kIBufferByteAccessIid = {
            0x905a0fef, 0xbc53, 0x11df, {0x8c, 0x49, 0x00, 0x1e, 0x4f, 0xc6, 0x86, 0xda}};
        winrt::com_ptr<::Windows::Storage::Streams::IBufferByteAccess> access;
        IUnknown* rawBuf = reinterpret_cast<IUnknown*>(winrt::get_abi(buf));
        if (!rawBuf || FAILED(rawBuf->QueryInterface(kIBufferByteAccessIid, reinterpret_cast<void**>(access.put()))) || !access) {
            return nullptr;
        }
        BYTE* dst = nullptr;
        if (FAILED(access->Buffer(&dst)) || !dst) return nullptr;
        std::memcpy(dst, premul.bgra.data(), bytes);
        return wgi::SoftwareBitmap::CreateCopyFromBuffer(
            buf, wgi::BitmapPixelFormat::Bgra8,
            static_cast<int32_t>(premul.width), static_cast<int32_t>(premul.height),
            wgi::BitmapAlphaMode::Premultiplied);
    } catch (...) {
        return nullptr;
    }
}

bool TransformRawWithLru(const RawImage& origStraight,
                         uint64_t origHash,
                         const OpticalSettings& cfg,
                         RawImage& outPremul,
                         uint64_t& outPremulHash) {
    if (origStraight.width == 0 || origStraight.height == 0 || origStraight.bgra.empty()) return false;
    if (origStraight.width < 24 || origStraight.height < 24) {
        g_lowResCount.fetch_add(1, std::memory_order_relaxed);
    }

    CacheKey key{origHash, origStraight.width, origStraight.height, cfg.generation};
    {
        std::lock_guard<std::mutex> lock(g_stateMutex);
        auto it = g_lruMap.find(key);
        if (it != g_lruMap.end()) {
            g_lruList.splice(g_lruList.begin(), g_lruList, it->second);
            outPremul = it->second->premulGlass;
            outPremulHash = it->second->premulHash;
            g_cacheHits.fetch_add(1, std::memory_order_relaxed);
            return true;
        }
    }

    RenderOpticalLiquidGlassPremul(origStraight, cfg, outPremul);
    outPremulHash = Fnv1a64(outPremul.bgra.data(), outPremul.bgra.size());
    g_renderCount.fetch_add(1, std::memory_order_relaxed);

    {
        std::lock_guard<std::mutex> lock(g_stateMutex);
        if (g_lruMap.find(key) == g_lruMap.end() && outPremul.bgra.size() <= kMaxCacheBytes) {
            while (!g_lruList.empty() &&
                   (g_lruList.size() >= kMaxCacheEntries ||
                    g_lruTotalBytes + outPremul.bgra.size() > kMaxCacheBytes)) {
                auto& back = g_lruList.back();
                g_lruTotalBytes -= back.premulGlass.bgra.size();
                g_lruMap.erase(back.key);
                g_lruList.pop_back();
            }
            size_t sz = outPremul.bgra.size();
            g_lruList.push_front(CacheEntry{key, outPremul, outPremulHash});
            g_lruTotalBytes += sz;
            g_lruMap[key] = g_lruList.begin();
        }
    }
    return true;
}

void KeepSbsAction(wf::IAsyncAction const& act) {
    if (!act) return;
    {
        std::lock_guard<std::mutex> lk(g_keepMutex);
        g_keepSbsOps.push_back(act);
    }
    act.Completed([act](wf::IAsyncAction const&, wf::AsyncStatus) {
        std::lock_guard<std::mutex> lk(g_keepMutex);
        for (size_t i = 0; i < g_keepSbsOps.size(); ++i) {
            if (winrt::get_abi(g_keepSbsOps[i]) == winrt::get_abi(act)) {
                g_keepSbsOps[i] = std::move(g_keepSbsOps.back());
                g_keepSbsOps.pop_back();
                break;
            }
        }
    });
}

// ---------------------------------------------------------------------------
// Visual Tree Inspection & Crash-Free Image Overlay Management
// ---------------------------------------------------------------------------

void PruneImgMapLocked() {
    if (g_imgMap.size() > kMaxImgRecords) {
        std::erase_if(g_imgMap, [](const auto& kv) { return !kv.second.weakImg.get(); });
    }
}

bool HasNotifyIconViewAncestor(const wux::FrameworkElement& fe) {
    try {
        wux::DependencyObject cur = fe;
        for (int depth = 0; depth < 6 && cur; ++depth) {
            if (winrt::get_class_name(cur) == L"SystemTray.NotifyIconView") {
                return true;
            }
            cur = wuxm::VisualTreeHelper::GetParent(cur);
        }
    } catch (...) {
    }
    return false;
}

wuxc::Image FindChildImage(const wux::DependencyObject& root, int maxDepth = 4) {
    if (!root || maxDepth <= 0) return nullptr;
    try {
        int count = wuxm::VisualTreeHelper::GetChildrenCount(root);
        for (int i = 0; i < count; ++i) {
            auto child = wuxm::VisualTreeHelper::GetChild(root, i);
            if (!child) continue;
            if (auto img = child.try_as<wuxc::Image>()) {
                return img;
            }
            if (auto found = FindChildImage(child, maxDepth - 1)) {
                return found;
            }
        }
    } catch (...) {
    }
    return nullptr;
}

bool ApplyRenderedGlassOverlay(const wuxc::Image& img,
                               void* imgKey,
                               const RawImage& origStraight,
                               uint64_t origHash,
                               const OpticalSettings& cfg) {
    if (!img || !imgKey || !cfg.enabled || g_unloading.load()) return false;
    RawImage premul;
    uint64_t premulHash = 0;
    if (!TransformRawWithLru(origStraight, origHash, cfg, premul, premulHash)) {
        return false;
    }
    wgi::SoftwareBitmap bmp = MakePremulSoftwareBitmap(premul);
    if (!bmp) return false;

    wuxmi::SoftwareBitmapSource sbs{nullptr};
    wuxm::ImageSource curSrc = img.Source();
    void* curKey = GetCanonicalInspectable(curSrc);
    {
        std::lock_guard<std::mutex> lock(g_stateMutex);
        auto it = g_imgMap.find(imgKey);
        if (it != g_imgMap.end() && it->second.glassSbs) {
            sbs = it->second.glassSbs;
        }
    }
    if (!sbs) {
        sbs = wuxmi::SoftwareBitmapSource();
    }
    void* sbsKey = GetCanonicalInspectable(sbs);

    try {
        auto act = sbs.SetBitmapAsync(bmp);
        KeepSbsAction(act);
    } catch (...) {
        g_errorCount.fetch_add(1, std::memory_order_relaxed);
        return false;
    }

    wuxm::ImageSource sbsAsSrc = sbs.as<wuxm::ImageSource>();
    bool needAssign = (curKey != sbsKey);
    {
        std::lock_guard<std::mutex> lock(g_stateMutex);
        auto it = g_imgMap.find(imgKey);
        if (it == g_imgMap.end()) return false;
        if (curKey && curKey != sbsKey) {
            it->second.origSource = curSrc;
            it->second.origSourceKey = curKey;
        }
        it->second.glassSbs = sbs;
        it->second.glassSbsKey = sbsKey;
        it->second.origBgra = origStraight;
        it->second.origHash = origHash;
        it->second.appliedGeneration = cfg.generation;
    }

    if (needAssign) {
        t_inOurSourceOverride = true;
        img.Source(sbsAsSrc);
        t_inOurSourceOverride = false;
    }
    return true;
}

void RestoreImageToOriginalOnUiThread(const wuxc::Image& img, void* imgKey) {
    if (!img || !imgKey) return;
    wuxm::ImageSource restoreSrc{nullptr};
    void* glassKey = nullptr;
    {
        std::lock_guard<std::mutex> lock(g_stateMutex);
        auto it = g_imgMap.find(imgKey);
        if (it == g_imgMap.end()) return;
        restoreSrc = it->second.origSource;
        glassKey = it->second.glassSbsKey;
        it->second.appliedGeneration = 0;
    }
    try {
        wuxm::ImageSource curSrc = img.Source();
        void* curKey = GetCanonicalInspectable(curSrc);
        if (restoreSrc && curKey && glassKey && curKey == glassKey) {
            t_inOurSourceOverride = true;
            img.Source(restoreSrc);
            t_inOurSourceOverride = false;
        }
    } catch (...) {
    }
}

void DetachCallbacksLocked(TrayImgRecord& rec, const wuxc::Image& img) {
    if (!rec.callbacksAttached || !img) return;
    try {
        if (rec.propToken != 0) {
            img.UnregisterPropertyChangedCallback(wuxc::Image::SourceProperty(), rec.propToken);
            rec.propToken = 0;
        }
        if (rec.unloadedToken) {
            img.Unloaded(rec.unloadedToken);
            rec.unloadedToken = {};
        }
    } catch (...) {
    }
    rec.callbacksAttached = false;
}

void ProcessImageOnUiThread(const wuxc::Image& img, bool allowAsyncCapture);

void BeginSafeRtbCaptureOnUiThread(const wuxc::Image& img, void* imgKey, uint32_t targetGen) {
    if (!img || !imgKey || g_unloading.load()) return;
    if (!img.IsLoaded() || img.Visibility() != wux::Visibility::Visible) return;
    if (img.ActualWidth() < 8.0 || img.ActualHeight() < 8.0) return;
    if (!wuxm::VisualTreeHelper::GetParent(img)) return;

    wuxm::ImageSource curSrc = img.Source();
    if (!curSrc) return;
    void* curKey = GetCanonicalInspectable(curSrc);

    {
        std::lock_guard<std::mutex> lock(g_stateMutex);
        auto it = g_imgMap.find(imgKey);
        if (it == g_imgMap.end()) return;
        if (curKey && curKey == it->second.glassSbsKey) return;
        if (it->second.captureInFlight) return;
        if (g_activeRtbCount.load(std::memory_order_acquire) >= kMaxConcurrentRtb) return;
        it->second.captureInFlight = true;
        it->second.captureAttemptedGen = targetGen;
        if (curKey) {
            it->second.origSource = curSrc;
            it->second.origSourceKey = curKey;
        }
    }

    auto op = std::make_shared<PendingRtbOp>();
    try {
        g_rtbQueued.fetch_add(1, std::memory_order_relaxed);
        g_pendingAsyncOps.fetch_add(1, std::memory_order_acq_rel);
        g_activeRtbCount.fetch_add(1, std::memory_order_acq_rel);

        wuxmi::RenderTargetBitmap rtb;
        {
            std::lock_guard<std::mutex> lk(g_keepMutex);
            g_keepRtbOps.push_back(op);
        }
        op->renderOp = rtb.RenderAsync(img);
        op->renderOp.Completed([weakImg = winrt::make_weak(img), imgKey, curKey, targetGen, rtb, op](
                                   wf::IAsyncAction const& act, wf::AsyncStatus status) {
            auto releaseRtb = [&]() {
                g_activeRtbCount.fetch_sub(1, std::memory_order_acq_rel);
                g_pendingAsyncOps.fetch_sub(1, std::memory_order_acq_rel);
                std::lock_guard<std::mutex> lk(g_keepMutex);
                g_keepRtbOps.erase(std::remove(g_keepRtbOps.begin(), g_keepRtbOps.end(), op), g_keepRtbOps.end());
            };
            auto clearInFlight = [&]() {
                std::lock_guard<std::mutex> lock(g_stateMutex);
                auto it = g_imgMap.find(imgKey);
                if (it != g_imgMap.end()) it->second.captureInFlight = false;
            };

            if (g_unloading.load() || status != wf::AsyncStatus::Completed) {
                g_rtbFail.fetch_add(1, std::memory_order_relaxed);
                clearInFlight();
                releaseRtb();
                return;
            }
            try {
                act.GetResults();
                uint32_t pw = static_cast<uint32_t>(std::max(0, rtb.PixelWidth()));
                uint32_t ph = static_cast<uint32_t>(std::max(0, rtb.PixelHeight()));
                if (pw == 0 || ph == 0 || pw > 256 || ph > 256) {
                    g_rtbFail.fetch_add(1, std::memory_order_relaxed);
                    clearInFlight();
                    releaseRtb();
                    return;
                }
                op->pixOp = rtb.GetPixelsAsync();
                op->pixOp.Completed([weakImg, imgKey, curKey, pw, ph, op](
                                        wf::IAsyncOperation<wss::IBuffer> const& pixAct, wf::AsyncStatus pixStatus) {
                    auto releaseAll = [&]() {
                        std::lock_guard<std::mutex> lock(g_stateMutex);
                        auto it = g_imgMap.find(imgKey);
                        if (it != g_imgMap.end()) it->second.captureInFlight = false;
                        g_activeRtbCount.fetch_sub(1, std::memory_order_acq_rel);
                        g_pendingAsyncOps.fetch_sub(1, std::memory_order_acq_rel);
                        std::lock_guard<std::mutex> lk(g_keepMutex);
                        g_keepRtbOps.erase(std::remove(g_keepRtbOps.begin(), g_keepRtbOps.end(), op), g_keepRtbOps.end());
                    };
                    if (g_unloading.load() || pixStatus != wf::AsyncStatus::Completed) {
                        g_rtbFail.fetch_add(1, std::memory_order_relaxed);
                        releaseAll();
                        return;
                    }
                    try {
                        wss::IBuffer buf = pixAct.GetResults();
                        RawImage straight;
                        if (!ExtractStraightFromBuffer(buf, pw, ph, true, straight)) {
                            g_rtbFail.fetch_add(1, std::memory_order_relaxed);
                            releaseAll();
                            return;
                        }
                        uint64_t origHash = Fnv1a64(straight.bgra.data(), straight.bgra.size());
                        OpticalSettings cfg;
                        {
                            std::lock_guard<std::mutex> lock(g_stateMutex);
                            cfg = g_settings;
                        }
                        auto liveImg = weakImg.get();
                        if (liveImg && cfg.enabled && !g_unloading.load()) {
                            wuxm::ImageSource nowSrc = liveImg.Source();
                            if (GetCanonicalInspectable(nowSrc) == curKey) {
                                ApplyRenderedGlassOverlay(liveImg, imgKey, straight, origHash, cfg);
                            }
                        }
                        g_rtbOk.fetch_add(1, std::memory_order_relaxed);
                        FlushStatusFile(false);
                    } catch (...) {
                        g_rtbFail.fetch_add(1, std::memory_order_relaxed);
                    }
                    releaseAll();
                });
            } catch (...) {
                g_rtbFail.fetch_add(1, std::memory_order_relaxed);
                clearInFlight();
                releaseRtb();
            }
        });
    } catch (...) {
        g_rtbFail.fetch_add(1, std::memory_order_relaxed);
        g_activeRtbCount.fetch_sub(1, std::memory_order_acq_rel);
        g_pendingAsyncOps.fetch_sub(1, std::memory_order_acq_rel);
        std::lock_guard<std::mutex> lock(g_stateMutex);
        auto it = g_imgMap.find(imgKey);
        if (it != g_imgMap.end()) it->second.captureInFlight = false;
        std::lock_guard<std::mutex> lk(g_keepMutex);
        g_keepRtbOps.erase(std::remove(g_keepRtbOps.begin(), g_keepRtbOps.end(), op), g_keepRtbOps.end());
    }
}

void EnsureImageTrackedOnUiThread(const wuxc::Image& img, void* imgKey) {
    if (!img || !imgKey || g_unloading.load()) return;
    bool needAttach = false;
    {
        std::lock_guard<std::mutex> lock(g_stateMutex);
        PruneImgMapLocked();
        auto& rec = g_imgMap[imgKey];
        rec.weakImg = winrt::make_weak(img);
        if (!rec.dispatcher) {
            rec.dispatcher = img.Dispatcher();
        }
        needAttach = !rec.callbacksAttached;
        if (needAttach) rec.callbacksAttached = true;
    }
    if (!needAttach) return;

    int64_t propTok = 0;
    winrt::event_token unlTok{};
    try {
        propTok = img.RegisterPropertyChangedCallback(
            wuxc::Image::SourceProperty(),
            [imgKey](wux::DependencyObject const& sender, wux::DependencyProperty const&) {
                if (t_inOurSourceOverride || g_unloading.load()) return;
                if (auto live = sender.try_as<wuxc::Image>()) {
                    ProcessImageOnUiThread(live, true);
                }
            });
        unlTok = img.Unloaded([imgKey](wf::IInspectable const& sender, wux::RoutedEventArgs const&) {
            auto live = sender.try_as<wuxc::Image>();
            std::lock_guard<std::mutex> lock(g_stateMutex);
            auto it = g_imgMap.find(imgKey);
            if (it != g_imgMap.end()) {
                DetachCallbacksLocked(it->second, live);
                g_imgMap.erase(it);
            }
        });
    } catch (...) {
    }

    std::lock_guard<std::mutex> lock(g_stateMutex);
    auto it = g_imgMap.find(imgKey);
    if (it != g_imgMap.end()) {
        it->second.propToken = propTok;
        it->second.unloadedToken = unlTok;
    }
}

void ProcessImageOnUiThread(const wuxc::Image& img, bool allowAsyncCapture) {
    if (!img || g_unloading.load()) return;
    void* imgKey = GetCanonicalInspectable(img);
    if (!imgKey) return;

    EnsureImageTrackedOnUiThread(img, imgKey);

    OpticalSettings cfg;
    {
        std::lock_guard<std::mutex> lock(g_stateMutex);
        cfg = g_settings;
    }
    if (!cfg.enabled) {
        RestoreImageToOriginalOnUiThread(img, imgKey);
        return;
    }

    wuxm::ImageSource curSrc = img.Source();
    if (!curSrc) return;
    void* curKey = GetCanonicalInspectable(curSrc);
    if (!curKey) return;

    bool isOurGlass = false;
    bool alreadyCurrentGen = false;
    bool sameOrigSource = false;
    bool alreadyAttemptedGen = false;
    RawImage savedOrig;
    uint64_t savedHash = 0;
    {
        std::lock_guard<std::mutex> lock(g_stateMutex);
        auto it = g_imgMap.find(imgKey);
        if (it == g_imgMap.end()) return;
        isOurGlass = (it->second.glassSbsKey != nullptr && curKey == it->second.glassSbsKey);
        if (isOurGlass) {
            alreadyCurrentGen = (it->second.appliedGeneration == cfg.generation);
            if (!alreadyCurrentGen && !it->second.origBgra.bgra.empty()) {
                savedOrig = it->second.origBgra;
                savedHash = it->second.origHash;
            }
        } else {
            sameOrigSource = (it->second.origSourceKey == curKey && !it->second.origBgra.bgra.empty());
            alreadyAttemptedGen = (it->second.origSourceKey == curKey &&
                                   it->second.captureAttemptedGen == cfg.generation);
            if (sameOrigSource) {
                savedOrig = it->second.origBgra;
                savedHash = it->second.origHash;
            } else {
                it->second.origSource = curSrc;
                it->second.origSourceKey = curKey;
                it->second.appliedGeneration = 0;
            }
        }
    }

    if (isOurGlass) {
        if (!alreadyCurrentGen && !savedOrig.bgra.empty()) {
            ApplyRenderedGlassOverlay(img, imgKey, savedOrig, savedHash, cfg);
        }
        return;
    }

    if (sameOrigSource && !savedOrig.bgra.empty()) {
        ApplyRenderedGlassOverlay(img, imgKey, savedOrig, savedHash, cfg);
        return;
    }

    // Fast Path 1: Synchronous pixel extraction from WriteableBitmap (zero RTB needed).
    if (auto wb = curSrc.try_as<wuxmi::WriteableBitmap>()) {
        try {
            int32_t w = wb.PixelWidth();
            int32_t h = wb.PixelHeight();
            if (w >= 8 && h >= 8 && w <= 256 && h <= 256) {
                RawImage straight;
                if (ExtractStraightFromBuffer(wb.PixelBuffer(), static_cast<uint32_t>(w), static_cast<uint32_t>(h), true, straight)) {
                    uint64_t hash = Fnv1a64(straight.bgra.data(), straight.bgra.size());
                    ApplyRenderedGlassOverlay(img, imgKey, straight, hash, cfg);
                    FlushStatusFile(false);
                    return;
                }
            }
        } catch (...) {
        }
    }

    // Path 2: Safe deferred RenderTargetBitmap capture AFTER MeasureOverride unwinds on Low priority.
    if (!allowAsyncCapture || alreadyAttemptedGen) return;
    try {
        if (auto disp = img.Dispatcher()) {
            winrt::weak_ref<wuxc::Image> weakImg = winrt::make_weak(img);
            uint32_t gen = cfg.generation;
            disp.RunAsync(wuc::CoreDispatcherPriority::Low, [weakImg, imgKey, gen]() {
                if (g_unloading.load()) return;
                if (auto live = weakImg.get()) {
                    BeginSafeRtbCaptureOnUiThread(live, imgKey, gen);
                }
            });
        }
    } catch (...) {
    }
}

void ReapplyOrRestoreAllDispatchers(bool restoreOnly, bool waitForCompletion) {
    struct WorkItem {
        void* imgKey;
        winrt::weak_ref<wuxc::Image> weakImg;
        wuc::CoreDispatcher dispatcher{nullptr};
    };
    std::unordered_map<void*, std::vector<WorkItem>> byDispatcher;
    {
        std::lock_guard<std::mutex> lock(g_stateMutex);
        for (auto& [key, rec] : g_imgMap) {
            if (!rec.dispatcher) continue;
            void* dKey = winrt::get_abi(rec.dispatcher);
            byDispatcher[dKey].push_back(WorkItem{key, rec.weakImg, rec.dispatcher});
        }
    }

    for (auto& [dKey, items] : byDispatcher) {
        if (items.empty()) continue;
        wuc::CoreDispatcher disp = items[0].dispatcher;
        if (!disp) continue;

        auto runBatch = [items = std::move(items), restoreOnly]() {
            for (const auto& item : items) {
                auto live = item.weakImg.get();
                if (!live) continue;
                if (restoreOnly || g_unloading.load()) {
                    RestoreImageToOriginalOnUiThread(live, item.imgKey);
                    if (g_unloading.load()) {
                        std::lock_guard<std::mutex> lock(g_stateMutex);
                        auto it = g_imgMap.find(item.imgKey);
                        if (it != g_imgMap.end()) {
                            DetachCallbacksLocked(it->second, live);
                        }
                    }
                } else {
                    ProcessImageOnUiThread(live, true);
                }
            }
        };

        try {
            if (disp.HasThreadAccess()) {
                runBatch();
            } else {
                HANDLE doneEvt = waitForCompletion ? CreateEventW(nullptr, TRUE, FALSE, nullptr) : nullptr;
                auto act = disp.RunAsync(
                    restoreOnly ? wuc::CoreDispatcherPriority::High : wuc::CoreDispatcherPriority::Normal,
                    [runBatch = std::move(runBatch), doneEvt]() mutable {
                        runBatch();
                        if (doneEvt) SetEvent(doneEvt);
                    });
                if (doneEvt) {
                    WaitForSingleObject(doneEvt, 1200);
                    CloseHandle(doneEvt);
                }
                (void)act;
            }
        } catch (...) {
        }
    }
}

// ---------------------------------------------------------------------------
// Visual Tree Walk & SystemTray.dll Symbol Hooks
// ---------------------------------------------------------------------------

void WalkTraySubtree(const wux::DependencyObject& node, int depth = 0) {
    if (!node || depth > 24 || g_unloading.load()) return;
    try {
        auto cls = winrt::get_class_name(node);
        if (cls == L"SystemTray.NotifyIconView") {
            if (auto img = FindChildImage(node, 10)) {
                ProcessImageOnUiThread(img, true);
            }
            return;
        }
        int count = wuxm::VisualTreeHelper::GetChildrenCount(node);
        for (int i = 0; i < count; ++i) {
            if (auto child = wuxm::VisualTreeHelper::GetChild(node, i)) {
                WalkTraySubtree(child, depth + 1);
            }
        }
    } catch (...) {
    }
}

wux::FrameworkElement FindVisualChildByClassName(const wux::DependencyObject& node,
                                                 const wchar_t* targetCls,
                                                 int maxDepth) {
    if (!node || maxDepth <= 0 || g_unloading.load()) return nullptr;
    try {
        if (winrt::get_class_name(node) == targetCls) {
            return node.try_as<wux::FrameworkElement>();
        }
        int count = wuxm::VisualTreeHelper::GetChildrenCount(node);
        for (int i = 0; i < count; ++i) {
            if (auto child = wuxm::VisualTreeHelper::GetChild(node, i)) {
                if (auto found = FindVisualChildByClassName(child, targetCls, maxDepth - 1)) {
                    return found;
                }
            }
        }
    } catch (...) {
    }
    return nullptr;
}

wux::FrameworkElement FindSystemTrayFrameInTaskbar(const wux::FrameworkElement& rootFe) {
    if (!rootFe) return nullptr;
    try {
        if (winrt::get_class_name(rootFe) == L"SystemTray.SystemTrayFrame") {
            return rootFe;
        }
        if (auto rep = rootFe.FindName(L"TaskbarFrameRepeater").try_as<wux::DependencyObject>()) {
            int count = wuxm::VisualTreeHelper::GetChildrenCount(rep);
            for (int i = 0; i < count; ++i) {
                auto item = wuxm::VisualTreeHelper::GetChild(rep, i);
                if (!item || winrt::get_class_name(item) != L"Taskbar.TaskbarExtensionElement") continue;
                if (auto cc = item.try_as<wuxc::ContentControl>()) {
                    if (auto content = cc.Content().try_as<wux::FrameworkElement>()) {
                        if (winrt::get_class_name(content) == L"SystemTray.SystemTrayFrame") {
                            return content;
                        }
                    }
                }
            }
        }
        if (auto fallback = FindVisualChildByClassName(rootFe, L"SystemTray.SystemTrayFrame", 10)) {
            return fallback;
        }
    } catch (...) {
    }
    return nullptr;
}

bool IsReadableMemory(const void* ptr, size_t bytes) noexcept {
    if (!ptr || bytes == 0) return false;
    uintptr_t start = reinterpret_cast<uintptr_t>(ptr);
    if (start < 0x10000ULL) return false;
    MEMORY_BASIC_INFORMATION mbi{};
    if (VirtualQuery(ptr, &mbi, sizeof(mbi)) != sizeof(mbi)) return false;
    if (mbi.State != MEM_COMMIT) return false;
    if (mbi.Protect & (PAGE_NOACCESS | PAGE_GUARD)) return false;
    constexpr DWORD kReadMask = PAGE_READONLY | PAGE_READWRITE | PAGE_WRITECOPY |
                                PAGE_EXECUTE_READ | PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY;
    if ((mbi.Protect & kReadMask) == 0) return false;
    uintptr_t regionEnd = reinterpret_cast<uintptr_t>(mbi.BaseAddress) + mbi.RegionSize;
    return start <= regionEnd && (regionEnd - start) >= bytes;
}

wux::XamlRoot GetTaskbarXamlRoot(HWND hTaskbarWnd) {
    if (!hTaskbarWnd || !CTaskBand_ITaskListWndSite_vftable ||
        !CTaskBand_GetTaskbarHost_Original || !TaskbarHost_FrameHeight_Original ||
        !std__Ref_count_base__Decref_Original) {
        return nullptr;
    }
    HWND hTaskSwWnd = reinterpret_cast<HWND>(GetPropW(hTaskbarWnd, L"TaskbandHWND"));
    if (!hTaskSwWnd) return nullptr;

    void* taskBand = reinterpret_cast<void*>(GetWindowLongPtrW(hTaskSwWnd, 0));
    if (!taskBand) return nullptr;

    void* taskBandForSite = taskBand;
    bool foundSite = false;
    for (int i = 0; i < 20; ++i) {
        if (!IsReadableMemory(taskBandForSite, sizeof(void*))) return nullptr;
        if (*reinterpret_cast<void**>(taskBandForSite) == CTaskBand_ITaskListWndSite_vftable) {
            foundSite = true;
            break;
        }
        taskBandForSite = reinterpret_cast<void**>(taskBandForSite) + 1;
    }
    if (!foundSite) return nullptr;

    void* taskbarHostSharedPtr[2]{};
    CTaskBand_GetTaskbarHost_Original(taskBandForSite, taskbarHostSharedPtr);
    if (!taskbarHostSharedPtr[0]) {
        if (taskbarHostSharedPtr[1]) {
            std__Ref_count_base__Decref_Original(taskbarHostSharedPtr[1]);
        }
        return nullptr;
    }

    size_t elemOffset = 0x10;
#if defined(_M_X64) || defined(__x86_64__)
    if (IsReadableMemory(TaskbarHost_FrameHeight_Original, 8)) {
        const BYTE* b = reinterpret_cast<const BYTE*>(TaskbarHost_FrameHeight_Original);
        if (b[0] == 0x48 && b[1] == 0x83 && b[2] == 0xEC && b[4] == 0x48 &&
            b[5] == 0x83 && b[6] == 0xC1 && b[7] <= 0x7F) {
            elemOffset = b[7];
        }
    }
#endif

    wux::XamlRoot result{nullptr};
    const uint8_t* elemSlot = reinterpret_cast<const uint8_t*>(taskbarHostSharedPtr[0]) + elemOffset;
    if (IsReadableMemory(elemSlot, sizeof(void*))) {
        IUnknown* elemUnk = *reinterpret_cast<IUnknown* const*>(elemSlot);
        if (IsReadableMemory(elemUnk, sizeof(void*)) &&
            IsReadableMemory(*reinterpret_cast<void* const*>(elemUnk), 4 * sizeof(void*))) {
            try {
                wux::FrameworkElement taskbarElement{nullptr};
                if (SUCCEEDED(elemUnk->QueryInterface(winrt::guid_of<wux::FrameworkElement>(),
                                                      winrt::put_abi(taskbarElement))) &&
                    taskbarElement) {
                    result = taskbarElement.XamlRoot();
                    g_xamlRootCalls.fetch_add(1, std::memory_order_relaxed);
                }
            } catch (...) {
            }
        }
    }

    if (taskbarHostSharedPtr[1]) {
        std__Ref_count_base__Decref_Original(taskbarHostSharedPtr[1]);
    }
    return result;
}

using RunFromWindowThreadProc_t = void(WINAPI*)(void* parameter);

bool RunFromWindowThread(HWND hWnd, RunFromWindowThreadProc_t proc, void* procParam) {
    static const UINT s_msg = RegisterWindowMessageW(L"LiquidGlassTray_RunFromWindowThread");
    struct Param {
        RunFromWindowThreadProc_t proc;
        void* procParam;
    };
    DWORD dwThreadId = GetWindowThreadProcessId(hWnd, nullptr);
    if (dwThreadId == 0) return false;
    if (dwThreadId == GetCurrentThreadId()) {
        proc(procParam);
        return true;
    }
    HHOOK hook = SetWindowsHookExW(
        WH_CALLWNDPROC,
        [](int nCode, WPARAM wParam, LPARAM lParam) -> LRESULT {
            if (nCode == HC_ACTION) {
                const CWPSTRUCT* cwp = reinterpret_cast<const CWPSTRUCT*>(lParam);
                if (cwp && cwp->message == s_msg && cwp->lParam) {
                    auto* p = reinterpret_cast<Param*>(cwp->lParam);
                    p->proc(p->procParam);
                }
            }
            return CallNextHookEx(nullptr, nCode, wParam, lParam);
        },
        nullptr, dwThreadId);
    if (!hook) return false;
    Param param{proc, procParam};
    SendMessageTimeoutW(hWnd, s_msg, 0, reinterpret_cast<LPARAM>(&param), SMTO_ABORTIFHUNG, 800, nullptr);
    UnhookWindowsHookEx(hook);
    return true;
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
            if (!GetWindowThreadProcessId(hWnd, &pid) || pid != c->pid) return TRUE;
            WCHAR cls[48]{};
            if (GetClassNameW(hWnd, cls, ARRAYSIZE(cls)) > 0) {
                if (_wcsicmp(cls, L"Shell_TrayWnd") == 0) c->list->insert(c->list->begin(), hWnd);
                else if (_wcsicmp(cls, L"Shell_SecondaryTrayWnd") == 0) c->list->push_back(hWnd);
            }
            return TRUE;
        },
        reinterpret_cast<LPARAM>(&ctx));
    return out;
}

void NudgeTaskbarLayoutIfCold() {
    auto hwnds = EnumCurrentProcessTaskbarWindows();
    if (hwnds.empty()) return;
    g_nudgeCalls.fetch_add(1, std::memory_order_relaxed);
#ifndef SPI_SETLOGICALDPIOVERRIDE
#define SPI_SETLOGICALDPIOVERRIDE 0x009F
#endif
    for (HWND hwnd : hwnds) {
        SendMessageTimeoutW(hwnd, WM_SETTINGCHANGE, SPI_SETLOGICALDPIOVERRIDE, 0, SMTO_ABORTIFHUNG, 500, nullptr);
        SendMessageTimeoutW(hwnd, WM_SETTINGCHANGE, 0, reinterpret_cast<LPARAM>(L"TraySettings"), SMTO_ABORTIFHUNG, 500, nullptr);
    }
}

void DispatchWalkContainer(winrt::weak_ref<wux::FrameworkElement>& weakRoot, bool lowPriority = false) {
    auto fe = weakRoot.get();
    if (!fe || g_unloading.load()) return;
    try {
        if (auto disp = fe.Dispatcher()) {
            auto prio = lowPriority ? wuc::CoreDispatcherPriority::Low : wuc::CoreDispatcherPriority::Normal;
            disp.RunAsync(prio, [weakRoot]() {
                if (g_unloading.load()) return;
                if (auto live = weakRoot.get()) {
                    WalkTraySubtree(live);
                    FlushStatusFile(true);
                }
            });
        }
    } catch (...) {
    }
}

void SweepTaskbarColdStart() {
    if (g_unloading.load()) return;
    auto hwnds = EnumCurrentProcessTaskbarWindows();
    bool anyFound = false;
    for (HWND hTaskbarWnd : hwnds) {
        struct SweepParam {
            HWND hWnd;
            bool found;
        } param{hTaskbarWnd, false};
        RunFromWindowThread(
            hTaskbarWnd,
            [](void* lp) {
                if (g_unloading.load()) return;
                auto* sp = reinterpret_cast<SweepParam*>(lp);
                auto xamlRoot = GetTaskbarXamlRoot(sp->hWnd);
                if (!xamlRoot) return;
                try {
                    if (auto rootFe = xamlRoot.Content().try_as<wux::FrameworkElement>()) {
                        if (auto stFrame = FindSystemTrayFrameInTaskbar(rootFe)) {
                            g_trayFrameWeak = winrt::make_weak(stFrame);
                            g_systemTrayFound.store(true, std::memory_order_release);
                            uint32_t curGen = g_settingsGeneration.load(std::memory_order_acquire);
                            g_lastTraySweepGen.store(curGen, std::memory_order_release);
                            g_lastTraySweepTick.store(GetTickCount64(), std::memory_order_relaxed);
                            WalkTraySubtree(stFrame);
                            sp->found = true;
                        }
                    }
                } catch (...) {
                }
            },
            &param);
        if (param.found) anyFound = true;
    }
    if (!anyFound && !g_systemTrayFound.load()) {
        NudgeTaskbarLayoutIfCold();
    }
}

void TryExtractOverflowRootGridFromManager(void* pManager) {
    if (g_unloading.load() || !pManager) return;
    if (!IsReadableMemory(pManager, 6 * sizeof(void*))) return;
    void* raw = reinterpret_cast<void* const*>(pManager)[5];
    if (!IsReadableMemory(raw, sizeof(void*))) return;
    void* vtbl = *reinterpret_cast<void* const*>(raw);
    if (!IsReadableMemory(vtbl, 4 * sizeof(void*))) return;
    try {
        wux::FrameworkElement fe{nullptr};
        if (SUCCEEDED(reinterpret_cast<IUnknown*>(raw)->QueryInterface(
                winrt::guid_of<wuxc::Grid>(), winrt::put_abi(fe))) &&
            fe && fe.IsLoaded()) {
            g_overflowWeak = winrt::make_weak(fe);
            g_overflowFound.store(true, std::memory_order_release);
        }
    } catch (...) {
    }
}

void SweepContainerOnArrange(void* pThis,
                             winrt::weak_ref<wux::FrameworkElement>& storeWeak,
                             std::atomic<uint64_t>& lastTick,
                             std::atomic<uint32_t>& lastGen,
                             uint64_t minIntervalMs,
                             std::atomic<bool>* foundFlag) {
    if (g_unloading.load() || !pThis) return;
    try {
        wux::FrameworkElement fe{nullptr};
        if (FAILED(reinterpret_cast<IUnknown*>(pThis)->QueryInterface(
                winrt::guid_of<wux::FrameworkElement>(), winrt::put_abi(fe))) || !fe) {
            return;
        }
        storeWeak = winrt::make_weak(fe);
        if (foundFlag) foundFlag->store(true, std::memory_order_release);
        uint32_t curGen = g_settingsGeneration.load(std::memory_order_acquire);
        uint64_t now = GetTickCount64();
        if (lastGen.load(std::memory_order_acquire) == curGen &&
            (now - lastTick.load(std::memory_order_relaxed)) < minIntervalMs) {
            return;
        }
        lastGen.store(curGen, std::memory_order_release);
        lastTick.store(now, std::memory_order_relaxed);
        WalkTraySubtree(fe);
        FlushStatusFile(false);
    } catch (...) {
    }
}

int32_t WINAPI SystemTrayFrame_ArrangeOverride_Hook(void* pThis, uint64_t finalSizeBits, void* pReturnSize) {
    int32_t hr = SystemTrayFrame_ArrangeOverride_Original(pThis, finalSizeBits, pReturnSize);
    g_arrangeCalls.fetch_add(1, std::memory_order_relaxed);
    SweepContainerOnArrange(pThis, g_trayFrameWeak, g_lastTraySweepTick, g_lastTraySweepGen, 250, &g_systemTrayFound);
    return hr;
}

int32_t WINAPI Overflow_ArrangeOverride_Hook(void* pThis, uint64_t finalSizeBits, void* pReturnSize) {
    int32_t hr = Overflow_ArrangeOverride_Original(pThis, finalSizeBits, pReturnSize);
    g_overflowCalls.fetch_add(1, std::memory_order_relaxed);
    SweepContainerOnArrange(pThis, g_overflowWeak, g_lastOverflowSweepTick, g_lastOverflowSweepGen, 80, &g_overflowFound);
    return hr;
}

int32_t WINAPI Overflow_OnApplyTemplate_Hook(void* pThis) {
    int32_t hr = Overflow_OnApplyTemplate_Original(pThis);
    if (g_unloading.load() || !pThis) return hr;
    try {
        wux::FrameworkElement fe{nullptr};
        if (SUCCEEDED(reinterpret_cast<IUnknown*>(pThis)->QueryInterface(
                winrt::guid_of<wux::FrameworkElement>(), winrt::put_abi(fe))) && fe) {
            g_overflowWeak = winrt::make_weak(fe);
            g_overflowFound.store(true, std::memory_order_release);
            DispatchWalkContainer(g_overflowWeak, true);
        }
    } catch (...) {
    }
    return hr;
}

void WINAPI OverflowManager_InitializeIfNeeded_Hook(void* pThis) {
    OverflowManager_InitializeIfNeeded_Original(pThis);
    g_ovInitCalls.fetch_add(1, std::memory_order_relaxed);
    if (g_unloading.load()) return;
    if (!g_overflowWeak.get()) {
        TryExtractOverflowRootGridFromManager(pThis);
    }
    if (g_overflowWeak.get()) {
        DispatchWalkContainer(g_overflowWeak, true);
    }
    FlushStatusFile(false);
}

void WINAPI OverflowManager_Show_Hook(void* pThis, POINT pt, int32_t inputDeviceKind) {
    OverflowManager_Show_Original(pThis, pt, inputDeviceKind);
    g_ovShowCalls.fetch_add(1, std::memory_order_relaxed);
    if (g_unloading.load()) return;
    if (!g_overflowWeak.get()) {
        TryExtractOverflowRootGridFromManager(pThis);
    }
    if (g_overflowWeak.get()) {
        DispatchWalkContainer(g_overflowWeak, true);
    }
    FlushStatusFile(false);
}

void WINAPI SystemTrayController_UpdateOverflowIcons_Hook(void* pThis) {
    SystemTrayController_UpdateOverflowIcons_Original(pThis);
    g_ovUpdateCalls.fetch_add(1, std::memory_order_relaxed);
    if (g_unloading.load()) return;
    if (g_overflowWeak.get()) {
        DispatchWalkContainer(g_overflowWeak, true);
    }
}

int32_t WINAPI TaskbarFrame_MeasureOverride_Hook(void* pThis, uint64_t availSizeBits, void* pReturnSize) {
    int32_t hr = TaskbarFrame_MeasureOverride_Original(pThis, availSizeBits, pReturnSize);
    if (g_unloading.load() || !pThis) return hr;
    try {
        uint32_t curGen = g_settingsGeneration.load(std::memory_order_acquire);
        if (g_trayFrameWeak.get() && g_lastTraySweepGen.load(std::memory_order_acquire) == curGen) {
            return hr;
        }
        wux::FrameworkElement fe{nullptr};
        if (SUCCEEDED(reinterpret_cast<IUnknown*>(pThis)->QueryInterface(
                winrt::guid_of<wux::FrameworkElement>(), winrt::put_abi(fe))) && fe) {
            if (auto stFrame = FindSystemTrayFrameInTaskbar(fe)) {
                g_trayFrameWeak = winrt::make_weak(stFrame);
                g_systemTrayFound.store(true, std::memory_order_release);
                g_lastTraySweepGen.store(curGen, std::memory_order_release);
                g_lastTraySweepTick.store(GetTickCount64(), std::memory_order_relaxed);
                WalkTraySubtree(stFrame);
                FlushStatusFile(false);
            }
        }
    } catch (...) {
    }
    return hr;
}

int32_t WINAPI ImageIconContent_MeasureOverride_Hook(void* pThis, uint64_t availSizeBits, void* pReturnSize) {
    int32_t hr = ImageIconContent_MeasureOverride_Original(pThis, availSizeBits, pReturnSize);
    if (g_unloading.load() || !pThis) return hr;

    g_measureCalls.fetch_add(1, std::memory_order_relaxed);
    try {
        wux::FrameworkElement fe{nullptr};
        if (SUCCEEDED(reinterpret_cast<IUnknown*>(pThis)->QueryInterface(
                winrt::guid_of<wux::FrameworkElement>(), winrt::put_abi(fe))) &&
            fe && fe.IsLoaded() && fe.Visibility() == wux::Visibility::Visible &&
            HasNotifyIconViewAncestor(fe)) {
            if (auto img = FindChildImage(fe)) {
                ProcessImageOnUiThread(img, true);
            }
        }
    } catch (...) {
    }
    return hr;
}

int32_t WINAPI ImageIconVM_put_IconImageSource_Hook(void* pThis, void* value) {
    g_vmPutCalls.fetch_add(1, std::memory_order_relaxed);
    return ImageIconVM_put_IconImageSource_Original(pThis, value);
}

}  // namespace

BOOL Wh_ModInit() {
    InitStatusPath();
    LoadSettings();

    HMODULE sysTray = GetModuleHandleW(L"SystemTray.dll");
    if (!sysTray) {
        Wh_Log(L"SystemTray.dll not loaded yet");
        FlushStatusFile(true);
        return FALSE;
    }

    WindhawkUtils::SYMBOL_HOOK trayHooks[] = {
        {
            {LR"(public: virtual int __cdecl winrt::impl::produce<struct winrt::SystemTray::implementation::SystemTrayFrame,struct winrt::Windows::UI::Xaml::IFrameworkElementOverrides>::ArrangeOverride(struct winrt::Windows::Foundation::Size,struct winrt::Windows::Foundation::Size *))"},
            &SystemTrayFrame_ArrangeOverride_Original,
            SystemTrayFrame_ArrangeOverride_Hook,
        },
        {
            {LR"(public: virtual int __cdecl winrt::impl::produce<struct winrt::SystemTray::implementation::NotificationAreaOverflow,struct winrt::Windows::UI::Xaml::IFrameworkElementOverrides>::ArrangeOverride(struct winrt::Windows::Foundation::Size,struct winrt::Windows::Foundation::Size *))"},
            &Overflow_ArrangeOverride_Original,
            Overflow_ArrangeOverride_Hook,
            true,
        },
        {
            {LR"(public: virtual int __cdecl winrt::impl::produce<struct winrt::SystemTray::implementation::NotificationAreaOverflow,struct winrt::Windows::UI::Xaml::IFrameworkElementOverrides>::OnApplyTemplate(void))"},
            &Overflow_OnApplyTemplate_Original,
            Overflow_OnApplyTemplate_Hook,
            true,
        },
        {
            {LR"(public: virtual int __cdecl winrt::impl::produce<struct winrt::SystemTray::implementation::ImageIconContent,struct winrt::Windows::UI::Xaml::IFrameworkElementOverrides>::MeasureOverride(struct winrt::Windows::Foundation::Size,struct winrt::Windows::Foundation::Size *))"},
            &ImageIconContent_MeasureOverride_Original,
            ImageIconContent_MeasureOverride_Hook,
            true,
        },
        {
            {LR"(public: virtual int __cdecl winrt::impl::produce<struct winrt::SystemTray::implementation::ImageIconContentViewModel,struct winrt::SystemTray::IImageIconContentViewModel>::put_IconImageSource(void *))"},
            &ImageIconVM_put_IconImageSource_Original,
            ImageIconVM_put_IconImageSource_Hook,
            true,
        },
        {
            {LR"(private: void __cdecl winrt::SystemTray::OverflowXamlIslandManager::InitializeIfNeeded(void))"},
            &OverflowManager_InitializeIfNeeded_Original,
            OverflowManager_InitializeIfNeeded_Hook,
            true,
        },
        {
            {LR"(public: void __cdecl winrt::SystemTray::OverflowXamlIslandManager::Show(struct tagPOINT,enum winrt::WindowsUdk::UI::Shell::InputDeviceKind))"},
            &OverflowManager_Show_Original,
            OverflowManager_Show_Hook,
            true,
        },
        {
            {LR"(private: void __cdecl winrt::SystemTray::implementation::SystemTrayController::UpdateNotificationAreaOverflowIcons(void))"},
            &SystemTrayController_UpdateOverflowIcons_Original,
            SystemTrayController_UpdateOverflowIcons_Hook,
            true,
        },
    };

    if (!WindhawkUtils::HookSymbols(sysTray, trayHooks, ARRAYSIZE(trayHooks))) {
        Wh_Log(L"HookSymbols(SystemTray.dll) failed");
        g_hookInstalled.store(false, std::memory_order_release);
        g_errorCount.fetch_add(1, std::memory_order_relaxed);
        FlushStatusFile(true);
        return FALSE;
    }

    if (HMODULE tbView = GetModuleHandleW(L"Taskbar.View.dll")) {
        WindhawkUtils::SYMBOL_HOOK tbHooks[] = {
            {
                {LR"(public: virtual int __cdecl winrt::impl::produce<struct winrt::Taskbar::implementation::TaskbarFrame,struct winrt::Windows::UI::Xaml::IFrameworkElementOverrides>::MeasureOverride(struct winrt::Windows::Foundation::Size,struct winrt::Windows::Foundation::Size *))"},
                &TaskbarFrame_MeasureOverride_Original,
                TaskbarFrame_MeasureOverride_Hook,
                true,
            },
        };
        WindhawkUtils::HookSymbols(tbView, tbHooks, ARRAYSIZE(tbHooks));
    }

    if (HMODULE taskbarDll = GetModuleHandleW(L"taskbar.dll")) {
        WindhawkUtils::SYMBOL_HOOK taskbarDllHooks[] = {
            {
                {LR"(const CTaskBand::`vftable'{for `ITaskListWndSite'})"},
                &CTaskBand_ITaskListWndSite_vftable,
                nullptr,
                true,
            },
            {
                {LR"(public: virtual class std::shared_ptr<class TaskbarHost> __cdecl CTaskBand::GetTaskbarHost(void)const )"},
                &CTaskBand_GetTaskbarHost_Original,
                nullptr,
                true,
            },
            {
                {LR"(public: int __cdecl TaskbarHost::FrameHeight(void)const )"},
                &TaskbarHost_FrameHeight_Original,
                nullptr,
                true,
            },
            {
                {LR"(public: void __cdecl std::_Ref_count_base::_Decref(void))"},
                &std__Ref_count_base__Decref_Original,
                nullptr,
                true,
            },
        };
        WindhawkUtils::HookSymbols(taskbarDll, taskbarDllHooks, ARRAYSIZE(taskbarDllHooks));
    }

    g_hookInstalled.store(true, std::memory_order_release);
    Wh_Log(L"LiquidGlassSystemTray initialized: TrayArr=%p OvArr=%p OvTpl=%p OvInit=%p OvShow=%p OvUpd=%p ImgMeas=%p TbHost=%p",
           SystemTrayFrame_ArrangeOverride_Original, Overflow_ArrangeOverride_Original,
           Overflow_OnApplyTemplate_Original, OverflowManager_InitializeIfNeeded_Original,
           OverflowManager_Show_Original, SystemTrayController_UpdateOverflowIcons_Original,
           ImageIconContent_MeasureOverride_Original, CTaskBand_GetTaskbarHost_Original);
    FlushStatusFile(true);
    return TRUE;
}

void Wh_ModAfterInit() {
    g_unloading.store(false, std::memory_order_release);
    SweepTaskbarColdStart();
    FlushStatusFile(true);
}

void Wh_ModSettingsChanged() {
    LoadSettings();
    OpticalSettings cfg;
    {
        std::lock_guard<std::mutex> lock(g_stateMutex);
        cfg = g_settings;
    }
    ReapplyOrRestoreAllDispatchers(!cfg.enabled, false);
    if (cfg.enabled) {
        if (g_trayFrameWeak.get()) {
            DispatchWalkContainer(g_trayFrameWeak, false);
        } else {
            SweepTaskbarColdStart();
        }
        DispatchWalkContainer(g_overflowWeak, true);
    }
    FlushStatusFile(true);
}

void Wh_ModBeforeUninit() {
    g_unloading.store(true, std::memory_order_release);

    for (int i = 0; i < 100 && g_pendingAsyncOps.load(std::memory_order_acquire) > 0; ++i) {
        Sleep(15);
    }

    ReapplyOrRestoreAllDispatchers(true, true);

    {
        std::lock_guard<std::mutex> lock(g_stateMutex);
        g_imgMap.clear();
        g_lruMap.clear();
        g_lruList.clear();
        g_lruTotalBytes = 0;
    }
    {
        std::lock_guard<std::mutex> lk(g_keepMutex);
        g_keepRtbOps.clear();
        g_keepSbsOps.clear();
    }
    FlushStatusFile(true);
}

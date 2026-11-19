// ==WindhawkMod==
// @id              liquid-glass-startmenu-icons
// @name            Liquid Glass Start Menu Icons
// @description     Universal real-time optical Liquid Glass transformation for Windows 11 Start Menu icons
// @version         1.0.0
// @author          LiquidGlassIconCenter
// @include         StartMenuExperienceHost.exe
// @architecture    x86-64
// @compilerOptions -lole32 -loleaut32 -lruntimeobject -lshcore -lwindowscodecs
// ==/WindhawkMod==

// ==WindhawkModReadme==
/*
# Liquid Glass Start Menu Icons

Applies real-time optical Liquid Glass rendering (translucent ice-white glass,
frosted highlights, directional inner bevel rim and seam glints) to Windows 11
Start Menu Pinned, All Apps, Category, and Folder icons inside
StartMenuExperienceHost.exe without mutating `.lnk`/`.url` files on disk.
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
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <deque>
#include <list>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

#include <winrt/Windows.Foundation.Collections.h>
#include <winrt/Windows.Foundation.h>
#include <winrt/Windows.Graphics.Imaging.h>
#include <winrt/Windows.Storage.Streams.h>
#include <winrt/Windows.UI.Core.h>
#include <winrt/Windows.UI.Xaml.Controls.h>
#include <winrt/Windows.UI.Xaml.Controls.Primitives.h>
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
namespace wuxcp = winrt::Windows::UI::Xaml::Controls::Primitives;
namespace wuxm = winrt::Windows::UI::Xaml::Media;
namespace wuxmi = winrt::Windows::UI::Xaml::Media::Imaging;

namespace {

constexpr size_t kMaxCacheEntries = 256;
constexpr size_t kMaxCacheBytes = 12ull * 1024ull * 1024ull;
constexpr size_t kMaxSbsRecords = 1024;
constexpr int kMaxConcurrentRtb = 6;
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
    RawImage premulGlass;  // premultiplied BGRA ready for SoftwareBitmap
    uint64_t premulHash = 0;
};

struct SbsRecord {
    winrt::weak_ref<wuxmi::SoftwareBitmapSource> weakSbs;
    RawImage origBgra;
    uint64_t origHash = 0;
    uint64_t lastGlassPremulHash = 0;
    uint32_t appliedGeneration = 0;
    bool approvedAppIcon = false;
    bool disallowedUiIcon = false;
    bool captureInFlight = false;
    uint32_t captureAttemptedGen = 0;
};

struct ImgRecord {
    winrt::weak_ref<wuxc::Image> weakImg;
    wuxm::ImageSource origNonSbsSource{nullptr};
    winrt::weak_ref<wuxmi::SoftwareBitmapSource> glassSbsOverlay;
    RawImage origBgra;
    uint64_t origHash = 0;
    uint32_t appliedGeneration = 0;
    bool captureInFlight = false;
    uint32_t captureAttemptedGen = 0;
    bool callbacksAttached = false;
    int64_t propToken = 0;
    winrt::event_token openedToken{};
    winrt::event_token unloadedToken{};
};

struct PendingRtbOp {
    wf::IAsyncAction renderOp{nullptr};
    wf::IAsyncOperation<wss::IBuffer> pixOp{nullptr};
};

struct CaptureTask {
    winrt::weak_ref<wuxc::Image> weakImg;
    void* imgKey = nullptr;
    void* sbsKey = nullptr;
    bool isSbs = false;
    std::wstring uriKey;
};

std::mutex g_stateMutex;
OpticalSettings g_settings;
std::atomic<bool> g_unloading{false};
std::atomic<uint32_t> g_settingsGeneration{1};
std::atomic<int> g_pendingAsyncOps{0};
std::atomic<int> g_activeRtbCount{0};
std::atomic<bool> g_hookInstalled{false};
std::atomic<uint64_t> g_lastWalkTick{0};
std::atomic<uint64_t> g_pendingTrailingScanTick{0};

std::unordered_map<void*, SbsRecord> g_sbsMap;
std::unordered_map<void*, ImgRecord> g_imgMap;
std::unordered_map<std::wstring, RawImage> g_uriOrigCache;
std::deque<CaptureTask> g_rtbQueue;

std::list<CacheEntry> g_lruList;
std::unordered_map<CacheKey, std::list<CacheEntry>::iterator, CacheKeyHash> g_lruMap;
size_t g_lruTotalBytes = 0;

std::mutex g_keepMutex;
std::vector<std::shared_ptr<PendingRtbOp>> g_keepRtbOps;
std::vector<wf::IAsyncAction> g_keepSbsOps;

// CoreWindow / UI thread subscription state.
HWND g_coreWnd = nullptr;
wux::FrameworkElement g_rootFe{nullptr};
wuc::CoreWindow g_coreWinObj{nullptr};
wux::Window g_xamlWinObj{nullptr};
winrt::event_token g_layoutUpdatedToken{};
winrt::event_token g_coreVisToken{};
winrt::event_token g_winActToken{};
bool g_uiEventsHooked = false;

std::mutex g_cvMutex;
std::condition_variable g_cv;
std::thread g_worker;

// Telemetry counters -> %TEMP%\lgic_windhawk_startmenu_status.json
std::atomic<uint64_t> g_renderCount{0};
std::atomic<uint64_t> g_cacheHits{0};
std::atomic<uint64_t> g_lowResCount{0};
std::atomic<uint64_t> g_errorCount{0};
std::atomic<uint64_t> g_sbsHookCalls{0};
std::atomic<uint64_t> g_rtbQueued{0};
std::atomic<uint64_t> g_rtbOk{0};
std::atomic<uint64_t> g_rtbFail{0};
std::atomic<uint64_t> g_lastStatusFlushTick{0};
std::wstring g_statusPath;
std::mutex g_statusMutex;

thread_local bool t_inOurSetBitmap = false;
thread_local bool t_inImgSourceOverride = false;

using SetBitmapAsync_t = int32_t(WINAPI*)(void* pThis, void* pBitmap, void** ppAsyncAction);
SetBitmapAsync_t SetBitmapAsync_Original = nullptr;
void* g_hookedSetBitmapTarget = nullptr;

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

bool IsWindowCloaked(HWND hwnd) {
    if (!hwnd) return true;
    using PFN_DwmGetWindowAttribute = HRESULT(WINAPI*)(HWND, DWORD, PVOID, DWORD);
    static PFN_DwmGetWindowAttribute pDwm = []() -> PFN_DwmGetWindowAttribute {
        HMODULE m = LoadLibraryExW(L"dwmapi.dll", nullptr, LOAD_LIBRARY_SEARCH_SYSTEM32);
        return m ? reinterpret_cast<PFN_DwmGetWindowAttribute>(GetProcAddress(m, "DwmGetWindowAttribute")) : nullptr;
    }();
    if (!pDwm) return false;
    BOOL cloaked = FALSE;
    constexpr DWORD DWMWA_CLOAKED_VAL = 14;
    if (SUCCEEDED(pDwm(hwnd, DWMWA_CLOAKED_VAL, &cloaked, sizeof(cloaked)))) {
        return cloaked != FALSE;
    }
    return false;
}

bool IsStartMenuVisibleOnScreen(HWND hwnd) {
    return hwnd && IsWindow(hwnd) && IsWindowVisible(hwnd) && !IsWindowCloaked(hwnd);
}

void InitStatusPath() {
    wchar_t tempDir[MAX_PATH]{};
    DWORD n = GetTempPathW(ARRAYSIZE(tempDir), tempDir);
    if (n > 0 && n < ARRAYSIZE(tempDir)) {
        g_statusPath = std::wstring(tempDir) + L"lgic_windhawk_startmenu_status.json";
    } else {
        g_statusPath = L"C:\\Windows\\Temp\\lgic_windhawk_startmenu_status.json";
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
    size_t sbsCount = 0;
    size_t cacheEntries = 0;
    size_t cacheBytes = 0;
    OpticalSettings snap{};
    {
        std::lock_guard<std::mutex> lock(g_stateMutex);
        imgCount = g_imgMap.size();
        sbsCount = g_sbsMap.size();
        cacheEntries = g_lruList.size();
        cacheBytes = g_lruTotalBytes;
        snap = g_settings;
    }

    char json[640];
    int len = _snprintf_s(
        json, sizeof(json), _TRUNCATE,
        "{\"active\":%s,\"unloading\":%s,\"pid\":%lu,\"generation\":%u,"
        "\"tracked_buttons\":%zu,\"sbs_count\":%zu,\"cache_entries\":%zu,\"cache_bytes\":%zu,"
        "\"max_cache_entries\":%zu,\"max_cache_bytes\":%zu,"
        "\"render_count\":%llu,\"cache_hits\":%llu,\"low_res_count\":%llu,\"error_count\":%llu,"
        "\"sbs_hooks\":%llu,\"rtb_queued\":%llu,\"rtb_ok\":%llu,\"rtb_fail\":%llu,"
        "\"pending_async\":%d,\"hook_ok\":%s,"
        "\"opacity\":%d,\"frost\":%d,\"glow\":%d,\"dark_protect\":%d}",
        (snap.enabled && !g_unloading.load()) ? "true" : "false",
        g_unloading.load() ? "true" : "false",
        GetCurrentProcessId(),
        snap.generation,
        imgCount, sbsCount, cacheEntries, cacheBytes,
        kMaxCacheEntries, kMaxCacheBytes,
        static_cast<unsigned long long>(g_renderCount.load()),
        static_cast<unsigned long long>(g_cacheHits.load()),
        static_cast<unsigned long long>(g_lowResCount.load()),
        static_cast<unsigned long long>(g_errorCount.load()),
        static_cast<unsigned long long>(g_sbsHookCalls.load()),
        static_cast<unsigned long long>(g_rtbQueued.load()),
        static_cast<unsigned long long>(g_rtbOk.load()),
        static_cast<unsigned long long>(g_rtbFail.load()),
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
// Optical Liquid Glass Renderer (1:1 Parity With Taskbar/Desktop Pipeline)
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
            float bVal = 1.00f;
            // Premultiply for SoftwareBitmap (Bgra8 + Premultiplied).
            uint32_t ua = a8;
            uint8_t r8 = static_cast<uint8_t>(std::min(255.0f, rVal * 255.0f + 0.5f));
            uint8_t g8 = static_cast<uint8_t>(std::min(255.0f, gVal * 255.0f + 0.5f));
            uint8_t b8 = static_cast<uint8_t>(std::min(255.0f, bVal * 255.0f + 0.5f));
            outPremul.bgra[i * 4 + 0] = static_cast<uint8_t>((static_cast<uint32_t>(b8) * ua + 127u) / 255u);
            outPremul.bgra[i * 4 + 1] = static_cast<uint8_t>((static_cast<uint32_t>(g8) * ua + 127u) / 255u);
            outPremul.bgra[i * 4 + 2] = static_cast<uint8_t>((static_cast<uint32_t>(r8) * ua + 127u) / 255u);
            outPremul.bgra[i * 4 + 3] = a8;
        }
    }
}

void StraightToPremul(const RawImage& straight, RawImage& outPremul) {
    outPremul.width = straight.width;
    outPremul.height = straight.height;
    size_t pxCount = static_cast<size_t>(straight.width) * straight.height;
    outPremul.bgra.resize(pxCount * 4);
    for (size_t i = 0; i < pxCount; ++i) {
        uint8_t b = straight.bgra[i * 4 + 0];
        uint8_t g = straight.bgra[i * 4 + 1];
        uint8_t r = straight.bgra[i * 4 + 2];
        uint8_t a = straight.bgra[i * 4 + 3];
        if (a == 0) {
            outPremul.bgra[i * 4 + 0] = 0;
            outPremul.bgra[i * 4 + 1] = 0;
            outPremul.bgra[i * 4 + 2] = 0;
            outPremul.bgra[i * 4 + 3] = 0;
        } else if (a == 255) {
            outPremul.bgra[i * 4 + 0] = b;
            outPremul.bgra[i * 4 + 1] = g;
            outPremul.bgra[i * 4 + 2] = r;
            outPremul.bgra[i * 4 + 3] = 255;
        } else {
            uint32_t ua = a;
            outPremul.bgra[i * 4 + 0] = static_cast<uint8_t>((static_cast<uint32_t>(b) * ua + 127u) / 255u);
            outPremul.bgra[i * 4 + 1] = static_cast<uint8_t>((static_cast<uint32_t>(g) * ua + 127u) / 255u);
            outPremul.bgra[i * 4 + 2] = static_cast<uint8_t>((static_cast<uint32_t>(r) * ua + 127u) / 255u);
            outPremul.bgra[i * 4 + 3] = a;
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
    if (!buf || w == 0 || h == 0 || w > 512 || h > 512) {
        return false;
    }
    static constexpr GUID kIBufferByteAccessIid = {
        0x905a0fef, 0xbc53, 0x11df, {0x8c, 0x49, 0x00, 0x1e, 0x4f, 0xc6, 0x86, 0xda}};
    winrt::com_ptr<::Windows::Storage::Streams::IBufferByteAccess> access;
    IUnknown* rawBuf = reinterpret_cast<IUnknown*>(winrt::get_abi(buf));
    if (!rawBuf || FAILED(rawBuf->QueryInterface(kIBufferByteAccessIid, reinterpret_cast<void**>(access.put()))) || !access) {
        return false;
    }
    BYTE* pixels = nullptr;
    if (FAILED(access->Buffer(&pixels)) || !pixels) {
        return false;
    }
    uint64_t expected = static_cast<uint64_t>(w) * h * 4ull;
    if (static_cast<uint64_t>(buf.Length()) < expected) {
        return false;
    }
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

bool ExtractStraightFromSoftwareBitmap(const wgi::SoftwareBitmap& bmp, RawImage& outStraight, uint64_t& outPremulHash) {
    if (!bmp) return false;
    try {
        int32_t w = bmp.PixelWidth();
        int32_t h = bmp.PixelHeight();
        if (w <= 0 || h <= 0 || w > 512 || h > 512) {
            return false;
        }
        wgi::SoftwareBitmap readBmp = bmp;
        bool isPremul = (bmp.BitmapAlphaMode() == wgi::BitmapAlphaMode::Premultiplied);
        if (bmp.BitmapPixelFormat() != wgi::BitmapPixelFormat::Bgra8 ||
            bmp.BitmapAlphaMode() == wgi::BitmapAlphaMode::Ignore) {
            readBmp = wgi::SoftwareBitmap::Convert(bmp, wgi::BitmapPixelFormat::Bgra8, wgi::BitmapAlphaMode::Premultiplied);
            isPremul = true;
        }
        wss::Buffer buf(static_cast<uint32_t>(w) * static_cast<uint32_t>(h) * 4u);
        buf.Length(static_cast<uint32_t>(w) * static_cast<uint32_t>(h) * 4u);
        readBmp.CopyToBuffer(buf);

        static constexpr GUID kIBufferByteAccessIid = {
            0x905a0fef, 0xbc53, 0x11df, {0x8c, 0x49, 0x00, 0x1e, 0x4f, 0xc6, 0x86, 0xda}};
        winrt::com_ptr<::Windows::Storage::Streams::IBufferByteAccess> access;
        IUnknown* rawBuf = reinterpret_cast<IUnknown*>(winrt::get_abi(buf));
        BYTE* pixels = nullptr;
        if (rawBuf && SUCCEEDED(rawBuf->QueryInterface(kIBufferByteAccessIid, reinterpret_cast<void**>(access.put()))) &&
            access && SUCCEEDED(access->Buffer(&pixels)) && pixels) {
            uint32_t byteLen = static_cast<uint32_t>(w) * static_cast<uint32_t>(h) * 4u;
            if (isPremul) {
                outPremulHash = Fnv1a64(pixels, byteLen);
            } else {
                outPremulHash = 0;
            }
            return ExtractStraightFromBuffer(buf, static_cast<uint32_t>(w), static_cast<uint32_t>(h), isPremul, outStraight);
        }
        return false;
    } catch (...) {
        return false;
    }
}

wgi::SoftwareBitmap MakePremulSoftwareBitmap(const RawImage& premul) {
    if (premul.width == 0 || premul.height == 0 || premul.bgra.empty()) {
        return nullptr;
    }
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
        if (FAILED(access->Buffer(&dst)) || !dst) {
            return nullptr;
        }
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
    if (origStraight.width == 0 || origStraight.height == 0 || origStraight.bgra.empty()) {
        return false;
    }
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

// ---------------------------------------------------------------------------
// COM Helpers & SoftwareBitmapSource Hook
// ---------------------------------------------------------------------------

void* GetCanonicalUnknown(IUnknown* unk) {
    if (!unk) return nullptr;
    winrt::com_ptr<IUnknown> canon;
    if (SUCCEEDED(unk->QueryInterface(IID_PPV_ARGS(canon.put())))) {
        return canon.get();
    }
    return nullptr;
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

bool ApplyPixelsToSbs(const wuxmi::SoftwareBitmapSource& sbs, const RawImage& premul) {
    if (!sbs) return false;
    wgi::SoftwareBitmap bmp = MakePremulSoftwareBitmap(premul);
    if (!bmp) return false;
    try {
        t_inOurSetBitmap = true;
        auto act = sbs.SetBitmapAsync(bmp);
        t_inOurSetBitmap = false;
        KeepSbsAction(act);
        return true;
    } catch (...) {
        t_inOurSetBitmap = false;
        g_errorCount.fetch_add(1, std::memory_order_relaxed);
        return false;
    }
}

void PruneMapsLocked() {
    if (g_sbsMap.size() > kMaxSbsRecords) {
        std::erase_if(g_sbsMap, [](const auto& kv) { return !kv.second.weakSbs.get(); });
    }
    if (g_imgMap.size() > kMaxSbsRecords) {
        std::erase_if(g_imgMap, [](const auto& kv) { return !kv.second.weakImg.get(); });
    }
}

int32_t WINAPI SetBitmapAsync_Hook(void* pThis, void* pBitmap, void** ppAsyncAction) {
    if (t_inOurSetBitmap || g_unloading.load(std::memory_order_acquire) || !pThis || !pBitmap) {
        return SetBitmapAsync_Original(pThis, pBitmap, ppAsyncAction);
    }

    g_sbsHookCalls.fetch_add(1, std::memory_order_relaxed);

    try {
        wuxmi::SoftwareBitmapSource sbs{nullptr};
        winrt::copy_from_abi(sbs, pThis);
        wgi::SoftwareBitmap inBmp{nullptr};
        winrt::copy_from_abi(inBmp, pBitmap);

        void* key = GetCanonicalUnknown(reinterpret_cast<IUnknown*>(pThis));
        RawImage straight;
        uint64_t premulHash = 0;
        if (key && sbs && inBmp && ExtractStraightFromSoftwareBitmap(inBmp, straight, premulHash)) {
            uint64_t straightHash = Fnv1a64(straight.bgra.data(), straight.bgra.size());
            OpticalSettings cfg;
            bool shouldTransformInline = false;
            bool isEcho = false;

            {
                std::lock_guard<std::mutex> lock(g_stateMutex);
                cfg = g_settings;
                auto& rec = g_sbsMap[key];
                rec.weakSbs = winrt::make_weak(sbs);
                if (premulHash != 0 && premulHash == rec.lastGlassPremulHash) {
                    isEcho = true;
                } else {
                    rec.origBgra = std::move(straight);
                    rec.origHash = straightHash;
                    rec.appliedGeneration = 0;
                    rec.lastGlassPremulHash = 0;
                    rec.captureInFlight = false;
                    const bool iconSized = (rec.origBgra.width >= 16 && rec.origBgra.height >= 16 &&
                                            rec.origBgra.width <= 256 && rec.origBgra.height <= 256);
                    shouldTransformInline = (!rec.disallowedUiIcon && (rec.approvedAppIcon || iconSized) && cfg.enabled);
                }
                PruneMapsLocked();
            }

            if (!isEcho && shouldTransformInline) {
                RawImage origSnap;
                uint64_t origH = 0;
                {
                    std::lock_guard<std::mutex> lock(g_stateMutex);
                    auto it = g_sbsMap.find(key);
                    if (it != g_sbsMap.end()) {
                        origSnap = it->second.origBgra;
                        origH = it->second.origHash;
                    }
                }
                RawImage glassPremul;
                uint64_t glassPremulHash = 0;
                if (!origSnap.bgra.empty() &&
                    TransformRawWithLru(origSnap, origH, cfg, glassPremul, glassPremulHash)) {
                    wgi::SoftwareBitmap glassBmp = MakePremulSoftwareBitmap(glassPremul);
                    if (glassBmp) {
                        t_inOurSetBitmap = true;
                        int32_t hr = SetBitmapAsync_Original(pThis, winrt::get_abi(glassBmp), ppAsyncAction);
                        t_inOurSetBitmap = false;
                        if (SUCCEEDED(hr)) {
                            std::lock_guard<std::mutex> lock(g_stateMutex);
                            auto it = g_sbsMap.find(key);
                            if (it != g_sbsMap.end()) {
                                it->second.appliedGeneration = cfg.generation;
                                it->second.lastGlassPremulHash = glassPremulHash;
                            }
                            return hr;
                        }
                    }
                }
            } else if (!isEcho && cfg.enabled) {
                // Newly created SoftwareBitmapSource (e.g. inside an opening folder modal/popup)
                // hasn't been visited by WalkSubtree yet; schedule a trailing UI tree scan.
                g_pendingTrailingScanTick.store(GetTickCount64(), std::memory_order_release);
                g_cv.notify_all();
            }
        }
    } catch (...) {
        t_inOurSetBitmap = false;
        g_errorCount.fetch_add(1, std::memory_order_relaxed);
    }

    return SetBitmapAsync_Original(pThis, pBitmap, ppAsyncAction);
}

void EnsureSetBitmapHookInstalledOnUiThread() {
    if (g_hookInstalled.load(std::memory_order_acquire)) {
        return;
    }
    try {
        wuxmi::SoftwareBitmapSource probe;
        auto iface = probe.try_as<wuxmi::ISoftwareBitmapSource>();
        if (!iface) return;
        void** vtbl = *reinterpret_cast<void***>(winrt::get_abi(iface));
        if (!vtbl || !vtbl[6]) return;

        void* target = vtbl[6];
        if (!g_hookInstalled.exchange(true, std::memory_order_acq_rel)) {
            g_hookedSetBitmapTarget = target;
            if (Wh_SetFunctionHook(target,
                                   reinterpret_cast<void*>(SetBitmapAsync_Hook),
                                   reinterpret_cast<void**>(&SetBitmapAsync_Original))) {
                Wh_ApplyHookOperations();
            } else {
                g_hookInstalled.store(false, std::memory_order_release);
                g_errorCount.fetch_add(1, std::memory_order_relaxed);
            }
        }
    } catch (...) {
        g_hookInstalled.store(false, std::memory_order_release);
        g_errorCount.fetch_add(1, std::memory_order_relaxed);
    }
}

// ---------------------------------------------------------------------------
// Visual Tree Target Filter & Cold-Start RTB Queue
// ---------------------------------------------------------------------------

bool ContainsNoCase(const std::wstring& hay, const wchar_t* needle) {
    if (hay.empty() || !needle) return false;
    size_t nlen = wcslen(needle);
    if (hay.size() < nlen) return false;
    for (size_t i = 0; i <= hay.size() - nlen; ++i) {
        if (_wcsnicmp(hay.c_str() + i, needle, nlen) == 0) {
            return true;
        }
    }
    return false;
}

bool IsDisallowedStartMenuUiImage(const wuxc::Image& img) {
    if (!img) return true;
    try {
        std::wstring nm(img.Name());
        if (nm == L"SearchIconOff" || nm == L"SearchIconOn" ||
            nm == L"PlaceholderIcon" || nm == L"SearchBoxOnTaskbarGleamImage") {
            return true;
        }
        wux::DependencyObject cur = img;
        for (int depth = 0; depth < 16 && cur; ++depth) {
            cur = wuxm::VisualTreeHelper::GetParent(cur);
            if (!cur) break;
            std::wstring pCls(winrt::get_class_name(cur));
            std::wstring pName;
            if (auto fe = cur.try_as<wux::FrameworkElement>()) {
                pName = fe.Name();
            }
            if (ContainsNoCase(pName, L"UserTile") || ContainsNoCase(pCls, L"UserTile") ||
                ContainsNoCase(pCls, L"AccountControl") || ContainsNoCase(pCls, L"PersonPicture") ||
                ContainsNoCase(pName, L"PersonPicture") || ContainsNoCase(pCls, L"SearchBox") ||
                ContainsNoCase(pName, L"SearchBox") || ContainsNoCase(pCls, L"Cortana") ||
                ContainsNoCase(pName, L"RightCompanion")) {
                return true;
            }
        }
        return false;
    } catch (...) {
        return false;
    }
}

bool IsApprovedStartMenuIconImage(const wuxc::Image& img) {
    if (!img || IsDisallowedStartMenuUiImage(img)) return false;
    try {
        std::wstring nm(img.Name());

        double aw = img.ActualWidth();
        double ah = img.ActualHeight();
        if (aw > 0.0 && ah > 0.0 && (aw < 10.0 || ah < 10.0 || aw > 128.0 || ah > 128.0)) {
            return false;
        }

        bool nameMatched = (nm == L"Logo" || nm == L"AllAppsItemLogo" || nm == L"AllAppsTileLogo" ||
                            nm == L"AppListItemLogo" || nm == L"FolderItemLogo" || nm == L"TileLogo" ||
                            nm == L"AppIcon");
        bool parentMatched = false;

        wux::DependencyObject cur = img;
        for (int depth = 0; depth < 16 && cur; ++depth) {
            cur = wuxm::VisualTreeHelper::GetParent(cur);
            if (!cur) break;
            std::wstring pCls(winrt::get_class_name(cur));
            std::wstring pName;
            if (auto fe = cur.try_as<wux::FrameworkElement>()) {
                pName = fe.Name();
            }
            if (depth <= 2 && (pName == L"LogoContainer" || pName == L"LogoBackgroundPlate" ||
                               pName == L"LogoBorder" || pName == L"LogosContainer")) {
                parentMatched = true;
            }
            if (ContainsNoCase(pCls, L"FolderModal") || ContainsNoCase(pName, L"FolderModal") ||
                ContainsNoCase(pCls, L"ExpandedFolder") || ContainsNoCase(pName, L"ExpandedFolder") ||
                ContainsNoCase(pName, L"FolderList") || ContainsNoCase(pName, L"FolderRegion") ||
                ContainsNoCase(pCls, L"PinnedListTile") || ContainsNoCase(pCls, L"UniversalTileContainer") ||
                ContainsNoCase(pName, L"LogosItemsControl") || ContainsNoCase(pCls, L"GridViewItem") ||
                ContainsNoCase(pCls, L"ListViewItem")) {
                parentMatched = true;
            }
        }
        return nameMatched || parentMatched;
    } catch (...) {
        return false;
    }
}

void ScanStartMenuOnUiThread(bool allowCapture);
void PumpColdCaptureQueue();

void ApplyToImgNonSbs(const wuxc::Image& img, void* imgKey, const OpticalSettings& cfg) {
    RawImage origSnap;
    uint64_t origH = 0;
    wuxm::ImageSource origSrc{nullptr};
    uint32_t curApplied = 0;
    {
        std::lock_guard<std::mutex> lock(g_stateMutex);
        auto it = g_imgMap.find(imgKey);
        if (it == g_imgMap.end()) return;
        origSnap = it->second.origBgra;
        origH = it->second.origHash;
        origSrc = it->second.origNonSbsSource;
        curApplied = it->second.appliedGeneration;
    }

    if (!cfg.enabled || g_unloading.load(std::memory_order_acquire)) {
        if (curApplied != 0 && origSrc) {
            try {
                t_inImgSourceOverride = true;
                img.Source(origSrc);
                t_inImgSourceOverride = false;
                std::lock_guard<std::mutex> lock(g_stateMutex);
                auto it = g_imgMap.find(imgKey);
                if (it != g_imgMap.end()) {
                    it->second.appliedGeneration = 0;
                    it->second.glassSbsOverlay = nullptr;
                }
            } catch (...) {
                t_inImgSourceOverride = false;
            }
        }
        return;
    }

    if (origSnap.bgra.empty() || curApplied == cfg.generation) {
        return;
    }

    RawImage premul;
    uint64_t premulHash = 0;
    if (!TransformRawWithLru(origSnap, origH, cfg, premul, premulHash)) {
        return;
    }
    wuxmi::SoftwareBitmapSource glassSbs;
    if (!ApplyPixelsToSbs(glassSbs, premul)) {
        return;
    }
    try {
        t_inImgSourceOverride = true;
        img.Source(glassSbs);
        t_inImgSourceOverride = false;
        std::lock_guard<std::mutex> lock(g_stateMutex);
        auto it = g_imgMap.find(imgKey);
        if (it != g_imgMap.end()) {
            it->second.glassSbsOverlay = winrt::make_weak(glassSbs);
            it->second.appliedGeneration = cfg.generation;
        }
    } catch (...) {
        t_inImgSourceOverride = false;
        g_errorCount.fetch_add(1, std::memory_order_relaxed);
    }
}

void ApplyToSbsKey(void* sbsKey, const OpticalSettings& cfg) {
    wuxmi::SoftwareBitmapSource sbs{nullptr};
    RawImage origSnap;
    uint64_t origH = 0;
    uint32_t curApplied = 0;
    {
        std::lock_guard<std::mutex> lock(g_stateMutex);
        auto it = g_sbsMap.find(sbsKey);
        if (it == g_sbsMap.end()) return;
        sbs = it->second.weakSbs.get();
        origSnap = it->second.origBgra;
        origH = it->second.origHash;
        curApplied = it->second.appliedGeneration;
    }
    if (!sbs || origSnap.bgra.empty()) return;

    if (!cfg.enabled || g_unloading.load(std::memory_order_acquire)) {
        if (curApplied == 0) return;
        RawImage origPremul;
        StraightToPremul(origSnap, origPremul);
        if (ApplyPixelsToSbs(sbs, origPremul)) {
            std::lock_guard<std::mutex> lock(g_stateMutex);
            auto it = g_sbsMap.find(sbsKey);
            if (it != g_sbsMap.end()) {
                it->second.appliedGeneration = 0;
                it->second.lastGlassPremulHash = 0;
            }
        }
        return;
    }

    if (curApplied == cfg.generation) return;

    RawImage premul;
    uint64_t premulHash = 0;
    if (TransformRawWithLru(origSnap, origH, cfg, premul, premulHash) &&
        ApplyPixelsToSbs(sbs, premul)) {
        std::lock_guard<std::mutex> lock(g_stateMutex);
        auto it = g_sbsMap.find(sbsKey);
        if (it != g_sbsMap.end()) {
            it->second.appliedGeneration = cfg.generation;
            it->second.lastGlassPremulHash = premulHash;
        }
    }
}

void QueueColdCapture(const wuxc::Image& img, void* imgKey, void* sbsKey, bool isSbs, const std::wstring& uriKey, uint32_t gen) {
    if (!img || !imgKey || g_unloading.load(std::memory_order_acquire)) return;
    if (img.Visibility() != wux::Visibility::Visible || img.ActualWidth() < 8.0 || img.ActualHeight() < 8.0) {
        return;
    }

    {
        std::lock_guard<std::mutex> lock(g_stateMutex);
        if (isSbs) {
            auto& rec = g_sbsMap[sbsKey];
            if (!rec.origBgra.bgra.empty() || rec.captureInFlight || rec.captureAttemptedGen == gen) {
                return;
            }
            rec.captureInFlight = true;
            rec.captureAttemptedGen = gen;
        } else {
            auto& rec = g_imgMap[imgKey];
            if (!rec.origBgra.bgra.empty() || rec.captureInFlight || rec.captureAttemptedGen == gen) {
                return;
            }
            rec.captureInFlight = true;
            rec.captureAttemptedGen = gen;
        }
        if (g_rtbQueue.size() < 256) {
            g_rtbQueue.push_back(CaptureTask{winrt::make_weak(img), imgKey, sbsKey, isSbs, uriKey});
            g_rtbQueued.fetch_add(1, std::memory_order_relaxed);
        } else {
            if (isSbs) {
                g_sbsMap[sbsKey].captureInFlight = false;
            } else {
                g_imgMap[imgKey].captureInFlight = false;
            }
        }
    }
}

void ClearCaptureInFlightFlag(const CaptureTask& task, bool resetGen = false) {
    std::lock_guard<std::mutex> lock(g_stateMutex);
    if (task.isSbs) {
        auto it = g_sbsMap.find(task.sbsKey);
        if (it != g_sbsMap.end()) {
            it->second.captureInFlight = false;
            if (resetGen) it->second.captureAttemptedGen = 0;
        }
    } else {
        auto it = g_imgMap.find(task.imgKey);
        if (it != g_imgMap.end()) {
            it->second.captureInFlight = false;
            if (resetGen) it->second.captureAttemptedGen = 0;
        }
    }
}

void StartSingleRtbCapture(const CaptureTask& task) {
    if (g_unloading.load(std::memory_order_acquire) || !IsStartMenuVisibleOnScreen(g_coreWnd)) {
        ClearCaptureInFlightFlag(task, true);
        return;
    }
    auto img = task.weakImg.get();
    if (!img) {
        ClearCaptureInFlightFlag(task);
        return;
    }
    try {
        if (img.Visibility() != wux::Visibility::Visible ||
            img.ActualWidth() < 8.0 || img.ActualHeight() < 8.0 || !img.Source()) {
            ClearCaptureInFlightFlag(task, true);
            return;
        }

        // If another sibling already captured this shared URI or SoftwareBitmapSource, reuse immediately.
        bool reuseSbs = false;
        bool reuseUri = false;
        OpticalSettings reuseCfg{};
        {
            std::lock_guard<std::mutex> lock(g_stateMutex);
            if (task.isSbs) {
                auto it = g_sbsMap.find(task.sbsKey);
                if (it != g_sbsMap.end() && !it->second.origBgra.bgra.empty()) {
                    it->second.captureInFlight = false;
                    reuseCfg = g_settings;
                    reuseSbs = true;
                }
            } else if (!task.uriKey.empty()) {
                auto uit = g_uriOrigCache.find(task.uriKey);
                if (uit != g_uriOrigCache.end()) {
                    auto it = g_imgMap.find(task.imgKey);
                    if (it != g_imgMap.end()) {
                        it->second.origBgra = uit->second;
                        it->second.origHash = Fnv1a64(uit->second.bgra.data(), uit->second.bgra.size());
                        it->second.captureInFlight = false;
                    }
                    reuseCfg = g_settings;
                    reuseUri = true;
                }
            }
        }
        if (reuseSbs) {
            ApplyToSbsKey(task.sbsKey, reuseCfg);
            return;
        }
        if (reuseUri) {
            ApplyToImgNonSbs(img, task.imgKey, reuseCfg);
            return;
        }

        g_activeRtbCount.fetch_add(1, std::memory_order_acq_rel);
        g_pendingAsyncOps.fetch_add(1, std::memory_order_acq_rel);

        wuxmi::RenderTargetBitmap rtb;
        auto renderOp = rtb.RenderAsync(img);
        auto keeper = std::make_shared<PendingRtbOp>();
        keeper->renderOp = renderOp;
        {
            std::lock_guard<std::mutex> lk(g_keepMutex);
            g_keepRtbOps.push_back(keeper);
        }

        auto finishKeep = [keeper, task]() {
            {
                std::lock_guard<std::mutex> lk(g_keepMutex);
                std::erase(g_keepRtbOps, keeper);
            }
            g_activeRtbCount.fetch_sub(1, std::memory_order_acq_rel);
            g_pendingAsyncOps.fetch_sub(1, std::memory_order_acq_rel);
            if (!g_unloading.load(std::memory_order_acquire)) {
                PumpColdCaptureQueue();
            }
        };

        renderOp.Completed([rtb, keeper, task, finishKeep](wf::IAsyncAction const&, wf::AsyncStatus st) {
            if (g_unloading.load(std::memory_order_acquire) || st != wf::AsyncStatus::Completed) {
                g_rtbFail.fetch_add(1, std::memory_order_relaxed);
                ClearCaptureInFlightFlag(task, st != wf::AsyncStatus::Completed);
                finishKeep();
                return;
            }
            try {
                int32_t pw = rtb.PixelWidth();
                int32_t ph = rtb.PixelHeight();
                if (pw <= 0 || ph <= 0 || pw > 512 || ph > 512) {
                    g_rtbFail.fetch_add(1, std::memory_order_relaxed);
                    ClearCaptureInFlightFlag(task, true);
                    finishKeep();
                    return;
                }
                auto pixOp = rtb.GetPixelsAsync();
                keeper->pixOp = pixOp;
                pixOp.Completed([rtb, keeper, pw, ph, task, finishKeep](wf::IAsyncOperation<wss::IBuffer> const& op, wf::AsyncStatus pst) {
                    if (g_unloading.load(std::memory_order_acquire) || pst != wf::AsyncStatus::Completed) {
                        g_rtbFail.fetch_add(1, std::memory_order_relaxed);
                        ClearCaptureInFlightFlag(task, pst != wf::AsyncStatus::Completed);
                        finishKeep();
                        return;
                    }
                    try {
                        wss::IBuffer buf = op.GetResults();
                        RawImage straight;
                        if (!ExtractStraightFromBuffer(buf, static_cast<uint32_t>(pw), static_cast<uint32_t>(ph), true, straight)) {
                            g_rtbFail.fetch_add(1, std::memory_order_relaxed);
                            ClearCaptureInFlightFlag(task, true);
                            finishKeep();
                            return;
                        }
                        uint64_t sh = Fnv1a64(straight.bgra.data(), straight.bgra.size());
                        OpticalSettings cfg;
                        {
                            std::lock_guard<std::mutex> lock(g_stateMutex);
                            cfg = g_settings;
                            if (task.isSbs) {
                                auto& rec = g_sbsMap[task.sbsKey];
                                rec.captureInFlight = false;
                                if (rec.origBgra.bgra.empty()) {
                                    rec.origBgra = std::move(straight);
                                    rec.origHash = sh;
                                }
                            } else {
                                auto& rec = g_imgMap[task.imgKey];
                                rec.captureInFlight = false;
                                if (rec.origBgra.bgra.empty()) {
                                    rec.origBgra = straight;
                                    rec.origHash = sh;
                                }
                                if (!task.uriKey.empty() && g_uriOrigCache.size() < 128) {
                                    g_uriOrigCache.try_emplace(task.uriKey, std::move(straight));
                                }
                            }
                        }
                        g_rtbOk.fetch_add(1, std::memory_order_relaxed);

                        auto dispatchApply = [task]() {
                            if (g_unloading.load(std::memory_order_acquire)) return;
                            OpticalSettings latest;
                            {
                                std::lock_guard<std::mutex> lock(g_stateMutex);
                                latest = g_settings;
                            }
                            if (task.isSbs) {
                                ApplyToSbsKey(task.sbsKey, latest);
                            } else if (auto im = task.weakImg.get()) {
                                ApplyToImgNonSbs(im, task.imgKey, latest);
                            }
                            FlushStatusFile(false);
                        };

                        if (auto im = task.weakImg.get()) {
                            auto disp = im.Dispatcher();
                            if (disp && !disp.HasThreadAccess()) {
                                disp.RunAsync(wuc::CoreDispatcherPriority::Normal, [dispatchApply]() { dispatchApply(); });
                            } else {
                                dispatchApply();
                            }
                        }
                    } catch (...) {
                        g_rtbFail.fetch_add(1, std::memory_order_relaxed);
                        ClearCaptureInFlightFlag(task);
                    }
                    finishKeep();
                });
            } catch (...) {
                g_rtbFail.fetch_add(1, std::memory_order_relaxed);
                ClearCaptureInFlightFlag(task);
                finishKeep();
            }
        });
    } catch (...) {
        ClearCaptureInFlightFlag(task);
        g_errorCount.fetch_add(1, std::memory_order_relaxed);
    }
}

void PumpColdCaptureQueue() {
    while (!g_unloading.load(std::memory_order_acquire) &&
           g_activeRtbCount.load(std::memory_order_acquire) < kMaxConcurrentRtb) {
        CaptureTask next;
        bool hasNext = false;
        {
            std::lock_guard<std::mutex> lock(g_stateMutex);
            if (!g_rtbQueue.empty()) {
                next = std::move(g_rtbQueue.front());
                g_rtbQueue.pop_front();
                hasNext = true;
            }
        }
        if (!hasNext) break;
        StartSingleRtbCapture(next);
    }
}

void ProcessImageElement(const wuxc::Image& img, const OpticalSettings& cfg, bool allowCapture) {
    if (!img || g_unloading.load(std::memory_order_acquire)) return;
    void* imgKey = GetCanonicalUnknown(reinterpret_cast<IUnknown*>(winrt::get_abi(img)));
    if (!imgKey) return;

    // Ensure PropertyChanged + ImageOpened callbacks are wired once per UI Image element.
    bool needAttach = false;
    {
        std::lock_guard<std::mutex> lock(g_stateMutex);
        auto& irec = g_imgMap[imgKey];
        irec.weakImg = winrt::make_weak(img);
        needAttach = !irec.callbacksAttached;
        if (needAttach) irec.callbacksAttached = true;
    }

    if (needAttach) {
        try {
            auto weakImg = winrt::make_weak(img);
            int64_t pTok = img.RegisterPropertyChangedCallback(
                wuxc::Image::SourceProperty(),
                [weakImg, imgKey](wux::DependencyObject const&, wux::DependencyProperty const&) {
                    if (t_inImgSourceOverride || g_unloading.load(std::memory_order_acquire)) return;
                    if (auto im = weakImg.get()) {
                        auto s = im.Source();
                        {
                            std::lock_guard<std::mutex> lock(g_stateMutex);
                            auto it = g_imgMap.find(imgKey);
                            if (it != g_imgMap.end()) {
                                auto ov = it->second.glassSbsOverlay.get();
                                if (ov && s && winrt::get_abi(ov) == winrt::get_abi(s)) {
                                    return;
                                }
                                if (s) {
                                    if (!s.try_as<wuxmi::SoftwareBitmapSource>()) {
                                        it->second.origNonSbsSource = s;
                                    } else {
                                        it->second.origNonSbsSource = nullptr;
                                    }
                                }
                                it->second.origBgra.bgra.clear();
                                it->second.origHash = 0;
                                it->second.appliedGeneration = 0;
                                it->second.captureAttemptedGen = 0;
                            }
                        }
                        OpticalSettings cur;
                        {
                            std::lock_guard<std::mutex> lock(g_stateMutex);
                            cur = g_settings;
                        }
                        if (cur.enabled) {
                            ProcessImageElement(im, cur, IsStartMenuVisibleOnScreen(g_coreWnd));
                        }
                    }
                });
            auto oTok = img.ImageOpened([weakImg](wf::IInspectable const&, wux::RoutedEventArgs const&) {
                if (g_unloading.load(std::memory_order_acquire)) return;
                if (auto im = weakImg.get()) {
                    OpticalSettings cur;
                    {
                        std::lock_guard<std::mutex> lock(g_stateMutex);
                        cur = g_settings;
                    }
                    if (cur.enabled) {
                        ProcessImageElement(im, cur, IsStartMenuVisibleOnScreen(g_coreWnd));
                    }
                }
            });
            std::lock_guard<std::mutex> lock(g_stateMutex);
            auto it = g_imgMap.find(imgKey);
            if (it != g_imgMap.end()) {
                it->second.propToken = pTok;
                it->second.openedToken = oTok;
            }
        } catch (...) {
        }
    }

    auto src = img.Source();
    if (!src) return;

    if (auto sbs = src.try_as<wuxmi::SoftwareBitmapSource>()) {
        void* sbsKey = GetCanonicalUnknown(reinterpret_cast<IUnknown*>(winrt::get_abi(sbs)));
        if (!sbsKey) return;

        bool hasOrig = false;
        {
            std::lock_guard<std::mutex> lock(g_stateMutex);
            auto& srec = g_sbsMap[sbsKey];
            srec.weakSbs = winrt::make_weak(sbs);
            srec.approvedAppIcon = true;
            hasOrig = !srec.origBgra.bgra.empty();
            auto it = g_imgMap.find(imgKey);
            if (it != g_imgMap.end()) {
                it->second.origNonSbsSource = nullptr;
            }
        }
        if (hasOrig) {
            ApplyToSbsKey(sbsKey, cfg);
        } else if (cfg.enabled && allowCapture) {
            QueueColdCapture(img, imgKey, sbsKey, true, L"", cfg.generation);
        }
        return;
    }

    // Non-SoftwareBitmapSource (e.g., UnplatedFolder.ico BitmapImage).
    {
        std::lock_guard<std::mutex> lock(g_stateMutex);
        auto it = g_imgMap.find(imgKey);
        if (it != g_imgMap.end()) {
            auto ov = it->second.glassSbsOverlay.get();
            if (ov && winrt::get_abi(ov) == winrt::get_abi(src)) {
                if (!cfg.enabled) {
                    // Restore handled below.
                } else if (it->second.appliedGeneration == cfg.generation) {
                    return;
                }
            } else {
                it->second.origNonSbsSource = src;
            }
        }
    }

    if (!cfg.enabled) {
        ApplyToImgNonSbs(img, imgKey, cfg);
        return;
    }

    std::wstring uriKey;
    if (auto bi = src.try_as<wuxmi::BitmapImage>()) {
        if (auto u = bi.UriSource()) {
            uriKey = std::wstring(u.RawUri());
        }
    }

    bool ready = false;
    {
        std::lock_guard<std::mutex> lock(g_stateMutex);
        auto it = g_imgMap.find(imgKey);
        if (it != g_imgMap.end()) {
            if (it->second.origBgra.bgra.empty() && !uriKey.empty()) {
                auto uit = g_uriOrigCache.find(uriKey);
                if (uit != g_uriOrigCache.end()) {
                    it->second.origBgra = uit->second;
                    it->second.origHash = Fnv1a64(uit->second.bgra.data(), uit->second.bgra.size());
                }
            }
            ready = !it->second.origBgra.bgra.empty();
        }
    }

    if (ready) {
        ApplyToImgNonSbs(img, imgKey, cfg);
    } else if (allowCapture) {
        QueueColdCapture(img, imgKey, nullptr, false, uriKey, cfg.generation);
    }
}

void WalkSubtree(const wux::DependencyObject& node, const OpticalSettings& cfg, bool allowCapture, int depth, int& count) {
    if (!node || depth > 64 || count >= 20000 || g_unloading.load(std::memory_order_acquire)) return;
    ++count;
    try {
        if (auto img = node.try_as<wuxc::Image>()) {
            if (IsDisallowedStartMenuUiImage(img)) {
                if (auto src = img.Source()) {
                    if (auto sbs = src.try_as<wuxmi::SoftwareBitmapSource>()) {
                        if (void* sbsKey = GetCanonicalUnknown(reinterpret_cast<IUnknown*>(winrt::get_abi(sbs)))) {
                            RawImage restoreOrig;
                            {
                                std::lock_guard<std::mutex> lock(g_stateMutex);
                                auto& srec = g_sbsMap[sbsKey];
                                srec.disallowedUiIcon = true;
                                srec.approvedAppIcon = false;
                                if (srec.appliedGeneration != 0 && !srec.origBgra.bgra.empty()) {
                                    restoreOrig = srec.origBgra;
                                    srec.appliedGeneration = 0;
                                    srec.lastGlassPremulHash = 0;
                                }
                            }
                            if (!restoreOrig.bgra.empty()) {
                                RawImage origPremul;
                                StraightToPremul(restoreOrig, origPremul);
                                ApplyPixelsToSbs(sbs, origPremul);
                            }
                        }
                    }
                }
            } else if (IsApprovedStartMenuIconImage(img)) {
                ProcessImageElement(img, cfg, allowCapture);
            }
        }
        if (auto pu = node.try_as<wuxcp::Popup>()) {
            if (auto ch = pu.Child()) {
                WalkSubtree(ch, cfg, allowCapture, depth + 1, count);
            }
        }
        int c = wuxm::VisualTreeHelper::GetChildrenCount(node);
        for (int i = 0; i < c; ++i) {
            WalkSubtree(wuxm::VisualTreeHelper::GetChild(node, i), cfg, allowCapture, depth + 1, count);
        }
    } catch (...) {
    }
}

void ScanStartMenuOnUiThread(bool allowCapture) {
    if (g_unloading.load(std::memory_order_acquire)) return;
    g_pendingTrailingScanTick.store(0, std::memory_order_release);
    OpticalSettings cfg;
    {
        std::lock_guard<std::mutex> lock(g_stateMutex);
        cfg = g_settings;
    }

    if (!cfg.enabled) {
        // Fast restore from cached originals without walking full tree if already tracked.
        std::vector<void*> sbsKeys;
        std::vector<std::pair<void*, wuxc::Image>> imgs;
        {
            std::lock_guard<std::mutex> lock(g_stateMutex);
            for (const auto& [k, rec] : g_sbsMap) {
                if (rec.appliedGeneration != 0) sbsKeys.push_back(k);
            }
            for (const auto& [k, rec] : g_imgMap) {
                if (rec.appliedGeneration != 0) {
                    if (auto im = rec.weakImg.get()) imgs.emplace_back(k, std::move(im));
                }
            }
        }
        for (void* k : sbsKeys) ApplyToSbsKey(k, cfg);
        for (const auto& [k, im] : imgs) ApplyToImgNonSbs(im, k, cfg);
        FlushStatusFile(true);
        return;
    }

    try {
        auto win = wux::Window::Current();
        if (!win) return;
        int visited = 0;
        if (auto content = win.Content()) {
            wux::DependencyObject topRoot = content;
            for (int up = 0; up < 16; ++up) {
                auto parent = wuxm::VisualTreeHelper::GetParent(topRoot);
                if (!parent) break;
                topRoot = parent;
            }
            WalkSubtree(topRoot, cfg, allowCapture, 0, visited);
            try {
                if (auto xr = content.XamlRoot()) {
                    if (auto xrPopups = wuxm::VisualTreeHelper::GetOpenPopupsForXamlRoot(xr)) {
                        for (auto const& pu : xrPopups) {
                            int puVisited = 0;
                            WalkSubtree(pu, cfg, allowCapture, 0, puVisited);
                        }
                    }
                }
            } catch (...) {
            }
        }
        if (auto popups = wuxm::VisualTreeHelper::GetOpenPopups(win)) {
            for (auto const& pu : popups) {
                int puVisited = 0;
                WalkSubtree(pu, cfg, allowCapture, 0, puVisited);
            }
        }
        // Also apply to any icon-sized SoftwareBitmapSource already captured by SetBitmapAsync_Hook
        // (such as items inside virtualized or newly expanded inner folders).
        std::vector<void*> pendingSbsKeys;
        {
            std::lock_guard<std::mutex> lock(g_stateMutex);
            for (const auto& [k, rec] : g_sbsMap) {
                if (!rec.disallowedUiIcon && !rec.origBgra.bgra.empty() &&
                    rec.appliedGeneration != cfg.generation &&
                    rec.origBgra.width >= 16 && rec.origBgra.height >= 16 &&
                    rec.origBgra.width <= 256 && rec.origBgra.height <= 256) {
                    pendingSbsKeys.push_back(k);
                }
            }
        }
        for (void* k : pendingSbsKeys) {
            ApplyToSbsKey(k, cfg);
        }
        if (allowCapture) {
            PumpColdCaptureQueue();
        }
        FlushStatusFile(false);
    } catch (...) {
        g_errorCount.fetch_add(1, std::memory_order_relaxed);
    }
}

void AttachCoreUiEventsOnUiThread() {
    EnsureSetBitmapHookInstalledOnUiThread();

    if (g_uiEventsHooked) return;
    try {
        auto win = wux::Window::Current();
        if (!win) return;
        g_xamlWinObj = win;
        if (auto fe = win.Content().try_as<wux::FrameworkElement>()) {
            g_rootFe = fe;
            g_layoutUpdatedToken = fe.LayoutUpdated([](wf::IInspectable const&, wf::IInspectable const&) {
                if (g_unloading.load(std::memory_order_acquire)) return;
                if (!IsStartMenuVisibleOnScreen(g_coreWnd)) return;
                uint64_t now = GetTickCount64();
                uint64_t prev = g_lastWalkTick.load(std::memory_order_relaxed);
                if (now - prev < 120) {
                    g_pendingTrailingScanTick.store(now, std::memory_order_release);
                    g_cv.notify_all();
                    return;
                }
                g_lastWalkTick.store(now, std::memory_order_relaxed);
                ScanStartMenuOnUiThread(true);
            });
        }
        if (auto cw = wuc::CoreWindow::GetForCurrentThread()) {
            g_coreWinObj = cw;
            g_winActToken = cw.Activated([](wuc::CoreWindow const&, wuc::WindowActivatedEventArgs const&) {
                if (g_unloading.load(std::memory_order_acquire)) return;
                if (IsStartMenuVisibleOnScreen(g_coreWnd)) {
                    ScanStartMenuOnUiThread(true);
                }
            });
            g_coreVisToken = cw.VisibilityChanged([](wuc::CoreWindow const&, wuc::VisibilityChangedEventArgs const& args) {
                if (g_unloading.load(std::memory_order_acquire)) return;
                if (args.Visible() && IsStartMenuVisibleOnScreen(g_coreWnd)) {
                    ScanStartMenuOnUiThread(true);
                }
            });
        }
        g_uiEventsHooked = true;
    } catch (...) {
        g_errorCount.fetch_add(1, std::memory_order_relaxed);
    }
}

void DetachAndRestoreAllOnUiThread() {
    if (g_uiEventsHooked) {
        try {
            if (g_coreWinObj && g_winActToken.value) {
                g_coreWinObj.Activated(g_winActToken);
            }
        } catch (...) {
        }
        try {
            if (g_rootFe && g_layoutUpdatedToken.value) {
                g_rootFe.LayoutUpdated(g_layoutUpdatedToken);
            }
        } catch (...) {
        }
        try {
            if (g_coreWinObj && g_coreVisToken.value) {
                g_coreWinObj.VisibilityChanged(g_coreVisToken);
            }
        } catch (...) {
        }
        g_winActToken = {};
        g_layoutUpdatedToken = {};
        g_coreVisToken = {};
        g_xamlWinObj = nullptr;
        g_rootFe = nullptr;
        g_coreWinObj = nullptr;
        g_uiEventsHooked = false;
    }

    {
        std::lock_guard<std::mutex> lock(g_stateMutex);
        g_settings.enabled = false;
    }
    ScanStartMenuOnUiThread(false);

    std::vector<ImgRecord> imgCleanup;
    {
        std::lock_guard<std::mutex> lock(g_stateMutex);
        for (auto& [k, rec] : g_imgMap) {
            imgCleanup.push_back(std::move(rec));
        }
        g_imgMap.clear();
        g_sbsMap.clear();
        g_uriOrigCache.clear();
        g_rtbQueue.clear();
        g_lruList.clear();
        g_lruMap.clear();
        g_lruTotalBytes = 0;
    }

    for (auto& rec : imgCleanup) {
        if (auto im = rec.weakImg.get()) {
            try {
                if (rec.propToken) {
                    im.UnregisterPropertyChangedCallback(wuxc::Image::SourceProperty(), rec.propToken);
                }
                if (rec.openedToken.value) {
                    im.ImageOpened(rec.openedToken);
                }
            } catch (...) {
            }
        }
    }
}

// ---------------------------------------------------------------------------
// CoreWindow Discovery & Thread Dispatch
// ---------------------------------------------------------------------------

using RunFromWindowThreadProc_t = void(WINAPI*)(PVOID);

bool RunFromWindowThread(HWND window, RunFromWindowThreadProc_t proc, PVOID param) {
    if (!window || !IsWindow(window)) return false;
    static const UINT msgId = RegisterWindowMessageW(L"Windhawk_RunFromWindowThread_" WH_MOD_ID);
    struct Payload {
        RunFromWindowThreadProc_t fn;
        PVOID p;
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
    DWORD_PTR res = 0;
    LRESULT sent = SendMessageTimeoutW(window, msgId, 0, reinterpret_cast<LPARAM>(&pl),
                                       SMTO_ABORTIFHUNG | SMTO_BLOCK, 1500, &res);
    UnhookWindowsHookEx(hook);
    return sent != 0;
}

HWND FindCoreWnd() {
    HWND found = nullptr;
    EnumWindows(
        [](HWND hwnd, LPARAM lp) -> BOOL {
            DWORD wp = 0;
            GetWindowThreadProcessId(hwnd, &wp);
            if (wp != GetCurrentProcessId()) return TRUE;
            wchar_t cls[64]{};
            if (GetClassNameW(hwnd, cls, ARRAYSIZE(cls)) &&
                _wcsicmp(cls, L"Windows.UI.Core.CoreWindow") == 0) {
                *reinterpret_cast<HWND*>(lp) = hwnd;
                return FALSE;
            }
            return TRUE;
        },
        reinterpret_cast<LPARAM>(&found));
    return found;
}

void ApplyOrRestoreOnCoreThread(bool allowCapture) {
    HWND core = g_coreWnd ? g_coreWnd : FindCoreWnd();
    if (!core) return;
    g_coreWnd = core;
    RunFromWindowThread(
        core,
        [](PVOID p) {
            bool cap = (p != nullptr);
            AttachCoreUiEventsOnUiThread();
            ScanStartMenuOnUiThread(cap);
        },
        reinterpret_cast<PVOID>(static_cast<uintptr_t>(allowCapture ? 1 : 0)));
    FlushStatusFile(true);
}

void WorkerLoop() {
    bool wasVisible = false;
    while (!g_unloading.load(std::memory_order_acquire)) {
        if (!g_coreWnd || !IsWindow(g_coreWnd)) {
            HWND core = FindCoreWnd();
            if (core) {
                g_coreWnd = core;
                RunFromWindowThread(core, [](PVOID) {
                    AttachCoreUiEventsOnUiThread();
                    OpticalSettings cfg;
                    {
                        std::lock_guard<std::mutex> lock(g_stateMutex);
                        cfg = g_settings;
                    }
                    if (cfg.enabled) {
                        ScanStartMenuOnUiThread(IsStartMenuVisibleOnScreen(g_coreWnd));
                    }
                }, nullptr);
                FlushStatusFile(true);
            }
        } else {
            bool nowVisible = IsStartMenuVisibleOnScreen(g_coreWnd);
            uint64_t nowTick = GetTickCount64();
            uint64_t lastWalk = g_lastWalkTick.load(std::memory_order_relaxed);
            uint64_t pendingTick = g_pendingTrailingScanTick.load(std::memory_order_acquire);
            bool needTrailing = nowVisible && pendingTick != 0 && (nowTick - lastWalk >= 120);
            bool needPeriodicVisible = nowVisible && (nowTick - lastWalk >= 350);
            if ((nowVisible && !wasVisible) || needTrailing || needPeriodicVisible) {
                OpticalSettings cfg;
                {
                    std::lock_guard<std::mutex> lock(g_stateMutex);
                    cfg = g_settings;
                }
                if (cfg.enabled) {
                    g_lastWalkTick.store(nowTick, std::memory_order_relaxed);
                    RunFromWindowThread(g_coreWnd, [](PVOID) {
                        AttachCoreUiEventsOnUiThread();
                        ScanStartMenuOnUiThread(true);
                    }, nullptr);
                    FlushStatusFile(true);
                } else {
                    g_pendingTrailingScanTick.store(0, std::memory_order_release);
                }
            }
            wasVisible = nowVisible;
        }

        std::unique_lock<std::mutex> lk(g_cvMutex);
        g_cv.wait_for(lk, std::chrono::milliseconds(150), []() {
            return g_unloading.load(std::memory_order_acquire);
        });
    }
}

}  // namespace

BOOL Wh_ModInit() {
    InitStatusPath();
    LoadSettings();
    FlushStatusFile(true);
    return TRUE;
}

void Wh_ModAfterInit() {
    g_unloading.store(false, std::memory_order_release);
    if (HWND core = FindCoreWnd()) {
        g_coreWnd = core;
        RunFromWindowThread(core, [](PVOID) {
            AttachCoreUiEventsOnUiThread();
            OpticalSettings cfg;
            {
                std::lock_guard<std::mutex> lock(g_stateMutex);
                cfg = g_settings;
            }
            if (cfg.enabled) {
                ScanStartMenuOnUiThread(IsStartMenuVisibleOnScreen(g_coreWnd));
            }
        }, nullptr);
    }
    g_worker = std::thread(WorkerLoop);
    FlushStatusFile(true);
}

void Wh_ModSettingsChanged() {
    LoadSettings();
    OpticalSettings cfg;
    {
        std::lock_guard<std::mutex> lock(g_stateMutex);
        cfg = g_settings;
    }
    ApplyOrRestoreOnCoreThread(cfg.enabled && IsStartMenuVisibleOnScreen(g_coreWnd));
}

void Wh_ModBeforeUninit() {
    g_unloading.store(true, std::memory_order_release);
    g_cv.notify_all();
    if (g_worker.joinable()) {
        g_worker.join();
    }

    for (int i = 0; i < 100 && g_pendingAsyncOps.load(std::memory_order_acquire) > 0; ++i) {
        Sleep(15);
    }

    HWND core = g_coreWnd ? g_coreWnd : FindCoreWnd();
    if (core && IsWindow(core)) {
        RunFromWindowThread(core, [](PVOID) { DetachAndRestoreAllOnUiThread(); }, nullptr);
    }

    {
        std::lock_guard<std::mutex> lk(g_keepMutex);
        g_keepRtbOps.clear();
        g_keepSbsOps.clear();
    }
    FlushStatusFile(true);
}

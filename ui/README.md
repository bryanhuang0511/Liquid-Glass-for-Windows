# `ui/` — Control Center Frontend

PyWebView (EdgeChromium) interface rendered from [`index.html`](index.html).

- **Static Dark-Glass Aesthetics**: Uses pure CSS gradients without expensive continuous `backdrop-filter` passes.
- **Visibility-Aware Telemetry Polling**: Polls Windhawk C++ telemetry every `4000ms` only while the active tab is a live Windhawk scope and the document is visible.
- **Optional Windhawk Mode UX**: Displays non-blocking guidance when Windhawk is absent while keeping the standalone Desktop tab and `套用／還原` controls fully functional.
- **Lifecycle Controls**: Dedicated switches for **關閉視窗直接結束程式** (`exit_when_window_closes`) and **退出自動還原即時外觀** (`restore_live_on_exit`).

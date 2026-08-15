# tt2gcal — TimeTree → Google Calendar 單向同步

把 TimeTree 帳號底下的共享日曆，單向鏡像到 Google Calendar 的專用次要日曆。
新增、修改、刪除都會反映；TimeTree 的標籤分類以事件顏色保留。

## ⚠️ 先讀這段

TimeTree **沒有**官方的匯出／同步 API：

- 官方 Public API 已於 [2023-12-22 終止](https://timetreeapp.com/intl/ja/newsroom/2023-12-14/connect-app-api-202312)，
  `developers.timetreeapp.com` 現在直接導向該公告。
- 官方 Help 明確表示[不支援把事件匯出到其他日曆](https://support.timetreeapp.com/hc/en-us/articles/205954655-Can-you-export-events-and-sync-TimeTree-to-OS-calendars-Google-Calendar-etc-)。

因此本工具透過 TimeTree **網頁版的私有 API** 讀取資料，底層沿用社群專案
[eoleedi/TimeTree-Exporter](https://github.com/eoleedi/TimeTree-Exporter)（MIT）逆向出的 client。

這代表：

- **本專案非 TimeTree 官方授權**，僅供你用自己的帳號讀取自己的資料。
- **私有 API 隨時可能變更或被封鎖**，屆時同步會失敗。失敗一定會以非零 exit code
  結束並寫進 log，不會靜默跳過。
- 登入端點有 rate limit，所以 session cookie 會被持久化，只在失效時才重新登入。

## 運作方式

```
TimeTree 私有 API                        Google Calendar API
─────────────────                        ───────────────────
GET /api/v1/calendars                    calendars.insert   （每本 TimeTree 日曆一本）
GET /api/v1/calendar/{id}/labels         events.list        （建 ttUid → eventId 索引）
GET /api/v1/calendar/{id}/events/sync    events.insert / update / delete
```

每輪都做**全量**拉取後比對差異，因此刪除靠「Google 有、TimeTree 沒有」判定，
不依賴增量游標語意。每個寫入 Google 的事件都帶 `extendedProperties.private`：

| key | 內容 |
| --- | --- |
| `ttSource` | 固定 `timetree`，查詢過濾用 |
| `ttUid` | TimeTree event `uuid`（穩定主鍵） |
| `ttCal` | TimeTree calendar id |
| `ttHash` | 映射後 payload 的雜湊，用來判斷是否需要 update |

## 安裝

```bash
uv sync
```

## 設定

複製 `.env.example` 成 `.env` 並填入：

| 變數 | 說明 |
| --- | --- |
| `TIMETREE_EMAIL` | TimeTree 帳號（需可用 email + 密碼登入） |
| `TIMETREE_PASSWORD` | TimeTree 密碼 |
| `GOOGLE_CLIENT_ID` | Google Cloud OAuth client（Desktop app 類型） |
| `GOOGLE_CLIENT_SECRET` | 同上 |
| `TT2GCAL_STATE_DIR` | 狀態目錄，預設 `./state` |
| `TT2GCAL_CALENDAR_PREFIX` | Google 日曆名稱前綴，預設 `TimeTree · ` |

`.env` 與 `state/` 都在 `.gitignore` 內，部署時請 `chmod 600` / `700`。

## 使用

```bash
uv run tt2gcal auth-google      # 首次授權，產生 state/google-token.json
uv run tt2gcal doctor           # 檢查憑證、scope、API 連通性
uv run tt2gcal sync --dry-run   # 只印差異，不寫入
uv run tt2gcal sync             # 實際同步
uv run tt2gcal recon            # 傾印 TimeTree 原始 JSON 供除錯（含個資，勿提交）
```

## 部署

```bash
./deploy/deploy.sh root@your-server --with-secrets   # 首次：連 .env 與 state/ 一起送
./deploy/deploy.sh root@your-server                  # 之後：只更新程式碼
```

`--with-secrets` 會一併送出 `state/calendars.json`。**這一步不能省** —— 少了對應表，
伺服器會再建一整套重複的 Google 日曆，而不是接手既有的那批。
`state/session.json` 刻意不送：TimeTree session 綁在建立它的機器上，伺服器自己登入一次即可。

伺服器端由 systemd 排程，每 15 分鐘一次：

```bash
systemctl enable --now tt2gcal.timer
systemctl list-timers tt2gcal.timer
journalctl -u tt2gcal.service -n 50
```

同步失敗會以非零 exit code 結束，觸發 `OnFailure` 的 `tt2gcal-alert.service`，
把最近 20 行 log 寫進 journal。在 `.env` 設 `TT2GCAL_ALERT_URL`（例如某個
[ntfy.sh](https://ntfy.sh) topic 網址）就會另外推播一則通知。

伺服器時區通常是 UTC，所以 `.env` 裡的 `TT2GCAL_CALENDAR_TIMEZONE` 要明確設定，
否則新建的 Google 日曆會是 UTC。

### 只能有一個寫入者

排程一旦交給伺服器，**本機就只做唯讀操作**（`sync --dry-run`、`recon`、測試）。
兩台機器同時寫同一批日曆會競爭：撞在一起可能對同一個事件插入兩次。
（`index_synced_events` 偵測到重複的 `ttUid` 會刪掉多的那筆並記 warning，
但那是補救，不是許可。）

要把本機退成唯讀，刪掉本機的 token 即可：

```bash
rm state/google-token.json
```

## 驗證

```bash
uv run pytest                              # 單元測試
uv run tt2gcal sync --dry-run              # 只印差異，不寫入
uv run python scripts/live_roundtrip.py    # 對真實 Google Calendar 驗 update / delete / 冪等
```

## 已知限制

- **重複事件的單次修改／刪除尚未支援**。TimeTree 如何表示這種例外還沒觀察到（帳號裡
  沒有非生日的重複事件）。遇到時程式會**拒絕寫入並記 warning**，而不是猜著做 ——
  同時寫 master 和被改過的那一次會讓那天出現兩個事件。要補完的話：在 TimeTree 建一個
  每週重複事件、改掉其中一次、刪掉另一次，然後重跑 `tt2gcal recon`，答案會出現在
  `docs/timetree-api-notes.md` 的 B 問題。
- 零長度的定時事件會被延長成 1 分鐘（Google 不接受零長度）。
- 生日與備忘錄預設略過，可用 `TT2GCAL_INCLUDE_BIRTHDAYS` / `TT2GCAL_INCLUDE_MEMOS` 開啟。
- TimeTree 日曆若被移除或退出，**Google 那本日曆會保留**，只記 log。事件層是完全鏡像，
  但整本日曆連同歷史一起消失的影響太大，不由事件層的設定推論。

## 授權

MIT。與 TimeTree, Inc. 無任何關聯。

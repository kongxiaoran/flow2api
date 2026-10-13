# Link 分支说明（kongxiaoran/flow2api `link/main`）

本分支基于上游 `TheSmallHanCat/flow2api` `main`，承载 Link 在 `spin1:/opt/flow2api`（`admin.flow-gemini.klinkw.com`）
生产使用 personal 打码模式时的补丁。所有改动都由环境变量控制，**不设置时行为与上游一致**，便于跟进上游。

## 环境变量开关

| 变量 | 作用 |
| --- | --- |
| `PERSONAL_DISABLE_COOKIE_MIRROR=1` | 不再把 Google 会话 Cookie 镜像到 `www.google.com` / `www.recaptcha.net`，并剔除历史回填的镜像副本。镜像副本会让 reCAPTCHA 给出 ~600 的低分 token，Flow 返回 `PUBLIC_ERROR_UNUSUAL_ACTIVITY` |
| `PERSONAL_IMAGE_VIA_OGIZ0B=1` | personal 生图不走 StreamChat（当前返回空结果），改为 trusted token(`IMAGE_GENERATION`) + `ogiZ0b`；`NARWHAL` 映射为 `PERSONAL_NARWHAL_MODEL_KEY`（默认 `BELUGA`） |
| `PERSONAL_TRUSTED_BATCHEXECUTE=1` | 视频（`YhhmEf`/`MZZa6b` 等）、放大（`SPrCad`）、上传（`maseQ`）在未配置第三方打码时改用浏览器 trusted token，不再报“请先配置 YesCaptcha…” |
| `FLOW_VIDEO_UPSAMPLE_WEB_LAYOUT=1` | 视频放大 `p0UkFb` 按网页真实请求构造（先用 `jwpduf` 查源视频 workflowId，`[4]=[null,workflowId,null,null,uuid]`、`[2]`=画幅、`[6]`=分辨率枚举 1080p=2、外层第 3 项 `[uuid]`）。上游结构会被 Flow 以 `code=[3]` 拒绝。提交后以 `<源id>_upsampled` 建任务，轮询允许该后缀 id，完成后用它调 `as29s` 取 `/video/` 直链（`jwpduf` 状态 `[1]` 处理中、`[3]` 完成，本身不带直链） |
| `FLOW_COOKIE_KEEPALIVE_MINUTES=30` | Cookie 保活：后台每 N 分钟（2～240，0=关闭；线上用 30。上次尝试时间落盘到 `data/cookie_keepalive_state.json`，重启后不会立刻再轮换；Google 返回 429 时多退让一个周期）对每个启用且有 Cookie 的 Token 模拟浏览器的 `POST accounts.google.com/RotateCookies`，把新的 `__Secure-*PSIDTS` / `*PSIDCC` / `SIDCC` 合并写回 `tokens.google_cookies`（走与请求相同的代理）。上游只在“新建浏览器标签页”时回填一次、空闲 10 分钟回收标签页，协议 ST 刷新又丢弃 `Set-Cookie`，没人用时副本会过期，AT 刷新 401 后 Token 被自动禁用。`docker logs` 里每个间隔一行 `[COOKIE_KEEPALIVE]`，不含 Cookie 值 |
| `PERSONAL_FORCE_UA` / `_MACOS` / `_EN_US` / `_SCREEN` / `_CORES` / `_MEMORY` / `_APPLE_GPU` | 固定伪装画像，避免随机组合自相矛盾 |
| `PERSONAL_DISABLE_SURFACE_SPOOF` / `PERSONAL_BROWSER_CDP_HOST` / `PERSONAL_BROWSER_CDP_PORT` | 连接外部浏览器（诊断用） |

trusted token：页面加载前挂钩 `Function.prototype.bind`，捕获 Flow 前端劫持前的 `enterprise.execute`，
action 保持真实值；非 `CHAT_GENERATION` action 在开关打开时取码前也会重新打开项目页，保证钩子已捕获。

## 同步上游

```bash
git fetch origin
git rebase origin/main   # 冲突只会出现在 browser_captcha_personal.py / flow_current.py 的 Link 补丁块
```

所有补丁块都带 `Link 补丁` / `Link patch` 注释，便于检索。

## 接口行为变更（无环境变量开关，直接生效）

### 模型列表只列基础模型
- `GET /v1/models`、`GET /v1beta/models` 默认只返回基础模型：图片 `gemini-3.1-flash-image`，视频 `veo-3.1-lite` / `veo-3.1-fast` / `veo-3.1-quality` / `omni-1.1-flash`。每项带 `type` 和 `parameters`（可传的参数及取值）。
- `?full=1`（或 `?detail=all`）返回全部具体模型名（如 `gemini-3.1-flash-image-portrait-4k`、`veo-3.1-fast-6s-portrait`）。具体名仍然可以直接调用，向后兼容；`GET /v1/models/{id}` 对基础名和具体名都能查。
- 图片参数：`generationConfig.imageConfig.aspectRatio`（16:9 / 9:16 / 1:1 / 4:3 / 3:4）、`imageSize`（2k / 4k，默认 1k）；OpenAI 风格 `size`（如 `1024x1792`）、`quality`（medium→2k，high/hd→4k）也支持。
- 视频参数：`generationConfig.videoConfig.{aspectRatio, durationSeconds, imageMode}`（也认顶层 `seconds` / `duration` / `aspect_ratio` / `size`）。不带参数且不带图片时等价于原来的 8 秒横屏别名。带 1-2 张图 → 首帧/首尾帧（i2v，4|6 秒）；3 张图或 `imageMode=reference` → 多图参考（r2v，固定 8 秒，只有 lite/fast，quality 自动用 fast）；Omni 带 0-3 张图都用同一个名字。不支持的时长取最接近的档位。视频续写（extend）需要源视频，只在具体名里。

### OpenAI Images 协议
- `POST /v1/images/generations`（JSON）、`POST /v1/images/edits`（multipart，也接受 JSON 的 `image` / `images`），并挂载不带 `/v1` 的 `/images/generations`、`/images/edits`。
- 请求：`model`（默认 `gemini-3.1-flash-image`）、`prompt`、`n`（1-4，顺序生成）、`size`、`quality`、`response_format`；未指定尺寸的文生图默认 `1024x1024`（方图），图生图按输入图比例推断。`mask` 会被忽略（Flow 不支持蒙版），`stream` 返回 400。
- 响应：`{"created": ..., "data": [{"url", "b64_json"}]}`，不带 `response_format` 时同时给 `url`（Flow 签名链接，会过期）和 `b64_json`；指定时只给对应字段。响应头 `X-Flow2API-Model` 是实际解析出的具体模型名。n>1 中途失败时保留已成功的图片，全部失败才返回错误。错误体为 OpenAI 格式 `{"error": {"message", "type", "param", "code"}}`。
- Sub2API 对 `gemini-*-image` 模型名走“Gemini 兼容生图”路径，转发到 `/v1/images/generations`；上游只返回 `url` 时它会自己补 `b64_json`。


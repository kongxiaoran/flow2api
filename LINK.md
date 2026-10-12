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

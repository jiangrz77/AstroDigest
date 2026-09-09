# 使用指南

[首页](../README_zh.md) · [English](usage.md)

下载安装版无需 Python，安装步骤见首页。

## 邮件通知

**Sender Email** 填写用于 SMTP 登录的单个发件邮箱；**Recipients** 可填写多个收件邮箱，用逗号、分号或空格分隔，留空则发送给自己。

在 **Settings → Email Notification** 中启用邮件通知并填写 SMTP 配置。邮件会在每日摘要成功更新后发送：

- 即使当天没有 5 星论文，也会发送一封日报邮件；
- 邮件包含所有 5 星论文的标题、评分、推荐理由、作者、分类、arXiv 链接和完整摘要；
- 4 星及以下论文的详细内容请在 AstroPaperDigest App 中查看；
- 同一日期默认只发送一次，Settings 中可手动重发最近日报，命令行也可使用 `send_digests.py --resend YYYY-MM-DD`；
- 邮件同时包含 HTML 和纯文本版本，并通过邮件线程头信息尽量让每日更新集中显示在同一组会话中；
- 邮件中的 App 链接在 App 未运行时会通过 `astropaperdigest://` 启动 App，并打开对应日期。

建议使用邮箱服务商提供的 **App Password**，不要填写主账号密码。密码只保存在本机 App 支持目录中，不会在设置页回显。

## 使用 Zotero 文献库

在 **Settings → Research Profile** 中选择 **Use Zotero Library**。应用默认尝试读取：

```text
~/Zotero/zotero.sqlite
```

保存 Research Profile 设置后，应用会立即验证并读取文献库。应用会先复制数据库，再以只读方式读取，因此 Zotero 正在运行时通常也可以使用。

如果默认位置读取失败，设置页会显示错误原因，并提示输入真实的 `zotero.sqlite` 路径。自定义数据目录可以在 Zotero 的 **Settings → Advanced → Files and Folders** 中查看。成功读取自定义路径后，应用会保存该路径供后续运行使用。

配置文件也可以直接指定：

```yaml
profile_source: zotero
zotero_db: ~/Zotero/zotero.sqlite
```

如果数据库不存在、没有权限、不是有效的 Zotero 数据库或结构不兼容，应用会报告错误，不会静默切换到关键词或 BibTeX profile。

## 从源码安装（需要 Python 3.9+）

请先将项目移动到固定目录，再进行安装。

### 安装步骤

1. 双击 **`Install.command`** —— 自动配置 Python 环境并构建应用
2. 双击 **`AstroPaperDigest.app`** —— 原生桌面窗口自动打开

> **macOS 安全提示？** 前往 **系统设置 → 隐私与安全性**，点击 **"仍要打开"**。仅需操作一次。

## 更新机制

- **检查时机**：应用启动时自动在后台检查一次（网络失败静默），也可在 **设置页（⛭）→ General → Update** 手动点击「Check for Updates」。
- **推送提示**：发现新版本时，Digest 页顶部显示蓝色横幅；设置页的 Update 分组显示当前/最新版本与更新日志。
- **安装流程**（半自动）：在设置页点击「Download Update」→ 下载完成后自动做 SHA-256 校验 → 点击「Install & Restart」→ 自动备份旧代码、替换源码、重建 .app 并重新打开。
- **更新源**：GitHub Releases（公开仓库）。检查接口：`https://api.github.com/repos/jiangrz77/AstroPaperDigest/releases/latest`。
- **版本号**：单一版本源 `version.txt`（构建 .app 时由 `build_app.sh` 读取）。
- **保留文件**：更新不会覆盖 `.env`、`config.yaml`、`preferences.json`、`feedback.json`、`data/`、`output/`、`.venv`；旧代码自动备份到 `backups/`。

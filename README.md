[Uploading README.md…]()
# Auto Typer Pro

Auto Typer Pro 是基于 C#、.NET 10、WPF、MVVM 和 Win32 `SendInput` 的 Windows 自动键盘录入助手。它逐字符提交真实键盘事件，不使用剪贴板粘贴，并提供倒计时、任务控制、目标窗口焦点保护、全局紧急停止、进度和日志。

## 发布信息

- 版本：`0.1.0`
- 发布日期：`2026-11-28`
- 发布平台：`Windows x64`
- 最低系统：Windows 10 1809（build 17763）或更高版本
- 发布形式：自包含、单文件 `AutoTyperPro.App.exe`
- 运行发布版无需安装 .NET 10 运行时或 SDK

## 快速开始

1. 将 `AutoTyperPro-0.1.0-win-x64.zip` 解压到一个可写目录。
2. 双击 `AutoTyperPro.App.exe` 启动。
3. 在“录入文本”中输入或粘贴需要录入的原始文本。
4. 确认输入间隔和开始倒计时。默认间隔为 `30 ms`，默认倒计时为 `3 秒`。
5. 将插入点放到目标程序的正确位置。
6. 点击“选择目标窗口”，在应用最小化后的选择倒计时内点击目标程序。
7. 回到应用核对目标窗口标题和进程，然后点击“开始输入”。
8. 在开始倒计时内切换到目标窗口并保持其前台焦点。
9. 使用“暂停 / 继续 / 停止”控制任务，并查看进度、结果状态和任务日志。
10. 输入结束后人工核对目标内容。

> 如果目标程序在 `30 ms` 下偶发丢字，可适当提高输入间隔。事件提交完成不代表目标程序已正确接收全部内容。

## 主要功能

- **逐字符 SendInput 输入**：保留中文、英文、符号、空格、Tab、换行、空行和末尾换行，不执行 Trim 或空白折叠。
- **Unicode 处理**：按 Unicode 码点规划，保留对应 UTF-16 子串；未配对代理项和不支持的控制字符会被明确拒绝。
- **目标窗口保护**：识别目标窗口、进程和进程 ID；每次提交前检查前台状态，目标关闭、失焦或句柄被复用后停止继续输入。
- **任务控制**：倒计时、开始、暂停、继续、停止、完成、取消和失败状态。
- **暂停语义**：在字符边界暂停，并中断当前字符间隔等待。
- **进度与诊断**：显示已处理字符、总字符数、百分比、耗时、预计剩余时间和结果状态。
- **实时日志**：独立显示时间、级别和消息，支持滚动、自动滚动及清空面板。
- **JSON 设置**：保存默认输入间隔、默认倒计时、主题和紧急停止快捷键。

## 紧急停止快捷键

默认快捷键为 **F8**。倒计时、输入和暂停期间均可全局触发紧急停止，Auto Typer Pro 位于后台时仍然有效。

### 自定义快捷键

1. 在“安全保护”区域点击“录制快捷键”。
2. 直接按下组合键，例如：
   - `Ctrl + F8`
   - `Ctrl + Shift + F8`
   - `Alt + F8`
   - `Ctrl + Alt + F8`
   - `Ctrl + Shift + F9`
3. 点击“保存并应用”。系统会先调用 `RegisterHotKey` 实际注册候选快捷键，注册成功后才释放旧快捷键并保存配置。
4. 如需取消录制，按 `Esc` 或点击“取消”。

支持 `F1–F24`，可配合 `Ctrl`、`Alt`、`Shift` 或 `Win`。任务运行期间不能修改快捷键。旧版配置没有快捷键字段时自动使用 F8。

### 注册失败处理

注册或切换失败时，原工作快捷键和原 JSON 配置保持不变。界面会根据 `GetLastWin32Error` 显示具体原因：

| Win32 错误码 | 含义 | 建议 |
|---|---|---|
| `1409` | 快捷键已被其他程序注册 | 更换组合键 |
| `87` | 参数无效或属于系统保留组合 | 更换组合键 |
| `5` | 系统拒绝访问 | 检查系统或安全软件设置 |
| `1400` | 主窗口句柄无效 | 重启应用 |
| 其他 | 其他 Win32 注册错误 | 根据显示的错误码排查，或更换组合键 |

点击“恢复默认 F8”会实际注册 F8 并保存。若 F8 已被占用，会显示失败原因，当前快捷键保持不变。

## 输入与验证边界

- 应用只负责提交键盘事件，**不会读取目标正文，也不会自动证明最终内容完全一致**。
- 界面中的验证状态为“未尝试目标正文验证”；`SendInput` 完成仅表示事件已提交。
- 目标程序必须能够接收 `SendInput` 事件。受保护输入框、管理员权限差异或目标程序自身限制可能导致无法输入。
- 目标窗口失去前台焦点后，后续字符不会继续提交。
- 请勿将本工具用于未经授权的系统或程序。

## 设置与日志

用户数据按 Windows 用户隔离，默认位置为：

```text
%LOCALAPPDATA%\AutoTyperPro\settings.json
%LOCALAPPDATA%\AutoTyperPro\logs\
```

配置使用 UTF-8 无 BOM 保存。配置损坏时会备份为 `.corrupted-*.bak`，记录警告并使用默认设置。

## 常见问题

### 双击 EXE 没有反应

- 确认操作系统为 Windows x64，并且版本不低于 Windows 10 build 17763。
- 查看 `%LOCALAPPDATA%\AutoTyperPro\logs\` 中是否有启动错误。
- 确认压缩包已完整解压，且 EXE 所在目录可写。

### 出现 Windows SmartScreen 提示

当前发布包未进行 Authenticode 代码签名。请仅从可信来源获取本包；确认来源后，可按 Windows 提示选择继续运行。

### 提示快捷键已被占用

更换为其他组合键，例如 `Ctrl + F8` 或 `Ctrl + Shift + F8`，然后点击“保存并应用”。

### 输入丢字或目标内容不完整

提高输入间隔后重试，并在输入结束后人工核对目标内容。不要把“事件提交完成”当作目标内容验证通过。

### 开始输入后立即停止

检查目标窗口是否仍为前台、是否被关闭，以及应用与目标程序的权限级别是否一致。

## 发布包内容

```text
AutoTyperPro.App.exe    自包含单文件 Windows x64 程序
README.md              本说明
SHA256SUMS.txt         SHA-256 校验值
```

`AutoTyperPro.App.exe` 的 SHA-256：

```text
889c399e87bdd77db08f83ed0521f68c3581ab4bb555fc9b05d376690505dff5
```

在 PowerShell 中校验：

```powershell
Get-FileHash .\AutoTyperPro.App.exe -Algorithm SHA256
```

## 从源码构建和发布

需要 .NET 10 SDK（`global.json` 指定 `10.0.100`，并允许同一功能带内的后续版本）。

```powershell
dotnet restore AutoTyperPro.sln
dotnet build AutoTyperPro.sln --configuration Release
```

重新生成自包含单文件发布版：

```powershell
dotnet publish src\AutoTyperPro.App\AutoTyperPro.App.csproj `
  -c Release `
  -r win-x64 `
  --self-contained true `
  -p:PublishSingleFile=true `
  -p:IncludeNativeLibrariesForSelfExtract=true `
  -p:EnableCompressionInSingleFile=true `
  -p:DebugType=None `
  -p:DebugSymbols=false `
  -o artifacts\release\AutoTyperPro-0.1.0-win-x64
```

## 本次验证说明

用户已对当前功能进行手动测试，未发现其他问题。本次打包执行了 Release 发布编译和产物结构核对，未重新运行自动化测试，也未将编译成功等同于目标窗口内容验证通过。

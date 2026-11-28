using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Reflection;
using System.Windows.Input;
using AutoTyperPro.App.Services;
using AutoTyperPro.Core.Input;
using AutoTyperPro.Core.Interfaces;
using AutoTyperPro.Core.Models;
using AutoTyperPro.Core.Services;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using Serilog;

namespace AutoTyperPro.App.ViewModels;

public sealed partial class MainViewModel : ObservableObject, IDisposable
{
    private readonly InputTaskManager _taskManager;
    private readonly IApplicationSettingsService _settingsService;
    private readonly ITargetWindowService _targetWindowService;
    private readonly IApplicationWindowService _windowService;
    private readonly IEmergencyStopService _emergencyStopService;
    private readonly TimeProvider _timeProvider;
    private readonly ILogger _logger;
    private CancellationTokenSource? _selectionCancellation;

    [ObservableProperty]
    [NotifyCanExecuteChangedFor(nameof(StartCommand))]
    private string _text = string.Empty;

    [ObservableProperty]
    private int _intervalMilliseconds = 30;

    [ObservableProperty]
    private int _countdownSeconds = 3;

    [ObservableProperty]
    private bool _isSelectingTarget;

    [ObservableProperty]
    private int _targetSelectionCountdown;

    [ObservableProperty]
    private string _stateText = "就绪";

    [ObservableProperty]
    private string _statusMessage = "请选择目标窗口并输入文本。";

    [ObservableProperty]
    private string _targetTitle = "尚未选择";

    [ObservableProperty]
    private string _targetProcessName = "点击“选择目标窗口”后，在倒计时内切换到目标程序";

    [ObservableProperty]
    private string _progressText = "0 / 0 字符";

    [ObservableProperty]
    private double _progressPercent;

    [ObservableProperty]
    private string _elapsedText = "0:00";

    [ObservableProperty]
    private string _estimatedRemainingText = "0:00";

    [ObservableProperty]
    private string _countdownDisplay = "—";

    [ObservableProperty]
    private string _verificationText = "未验证目标内容";

    [ObservableProperty]
    private string _statisticsText = "0 个码点 · UTF-16 0 · 0 行";

    [ObservableProperty]
    private string _selectionMessage = "选择时应用会最小化，请在倒计时结束前点击目标窗口。";

    [ObservableProperty]
    private string _actionError = string.Empty;

    [ObservableProperty]
    private bool _f8Ready;

    [ObservableProperty]
    private EmergencyStopShortcut _emergencyStopShortcut = EmergencyStopShortcut.Default;

    [ObservableProperty]
    private EmergencyStopShortcut? _pendingEmergencyStopShortcut;

    [ObservableProperty]
    private bool _isRecordingEmergencyStopShortcut;

    [ObservableProperty]
    private bool _isApplyingEmergencyStopShortcut;

    [ObservableProperty]
    private string _emergencyStopShortcutMessage = "默认快捷键为 F8，可点击“录制快捷键”自定义。";

    public MainViewModel(
        InputTaskManager taskManager,
        IApplicationSettingsService settingsService,
        ITargetWindowService targetWindowService,
        IApplicationWindowService windowService,
        IEmergencyStopService emergencyStopService,
        TimeProvider timeProvider,
        ILogger logger)
    {
        _taskManager = taskManager ?? throw new ArgumentNullException(nameof(taskManager));
        _settingsService = settingsService ?? throw new ArgumentNullException(nameof(settingsService));
        _targetWindowService = targetWindowService ?? throw new ArgumentNullException(nameof(targetWindowService));
        _windowService = windowService ?? throw new ArgumentNullException(nameof(windowService));
        _emergencyStopService = emergencyStopService ?? throw new ArgumentNullException(nameof(emergencyStopService));
        _timeProvider = timeProvider ?? throw new ArgumentNullException(nameof(timeProvider));
        _logger = logger ?? throw new ArgumentNullException(nameof(logger));

        IntervalMilliseconds = _settingsService.Current.DefaultInputIntervalMilliseconds;
        CountdownSeconds = _settingsService.Current.DefaultCountdownSeconds;
        EmergencyStopShortcut = _settingsService.Current.EmergencyStopShortcut;
        EmergencyStopShortcutMessage = $"当前快捷键为 {EmergencyStopShortcut}，可点击“录制快捷键”自定义。";
        UpdateStatistics();
        _taskManager.PropertyChanged += OnTaskManagerPropertyChanged;
        _taskManager.CommandStateChanged += OnTaskManagerCommandStateChanged;
        _taskManager.LogRecorded += OnTaskLogRecorded;
        SyncFromTaskManager();
    }

    public string Title => "Auto Typer Pro";

    public string Version => $"v{Assembly.GetExecutingAssembly().GetName().Version?.ToString(3) ?? "0.1.0"}";

    public string EngineStatus => "SendInput 逐字符输入";

    public string EmergencyStopShortcutDisplay => EmergencyStopShortcut.ToString();

    public string EmergencyStopButtonText => $"{EmergencyStopShortcut} 紧急停止";

    public string EmergencyStopHelpText =>
        $"倒计时、输入和暂停期间均可按 {EmergencyStopShortcut} 紧急停止。目标窗口失去前台焦点时，后续字符不会继续提交。";

    public string EmergencyStopStatus => F8Ready
        ? $"{EmergencyStopShortcut} 紧急停止已启用"
        : $"{EmergencyStopShortcut} 紧急停止未启用";

    public string EmergencyStopRegistrationStatus => F8Ready
        ? $"已注册：{EmergencyStopShortcut}"
        : $"未注册：{EmergencyStopShortcut}";

    public string PendingEmergencyStopShortcutDisplay => PendingEmergencyStopShortcut?.ToString() ?? "尚未录入";

    public bool IsTaskActive => _taskManager.IsActive || IsSelectingTarget;

    public bool CanEditText => !_taskManager.IsActive && !IsSelectingTarget;

    public bool HasActionError => !string.IsNullOrWhiteSpace(ActionError);

    public bool CanStartInput => _taskManager.CanStart &&
                                 Text.Length > 0 &&
                                 !IsSelectingTarget &&
                                 !IsRecordingEmergencyStopShortcut &&
                                 !IsApplyingEmergencyStopShortcut;

    public bool CanPauseInput => _taskManager.CanPause;

    public bool CanResumeInput => _taskManager.CanResume;

    public bool CanStopInput => _taskManager.CanStop || IsSelectingTarget;

    public bool CanSelectTarget => _taskManager.CanStart &&
                                   !IsSelectingTarget &&
                                   !IsRecordingEmergencyStopShortcut &&
                                   !IsApplyingEmergencyStopShortcut;

    public bool CanConfigureEmergencyStopShortcut => !IsTaskActive &&
                                                     !IsRecordingEmergencyStopShortcut &&
                                                     !IsApplyingEmergencyStopShortcut;

    public bool CanApplyEmergencyStopShortcut => CanConfigureEmergencyStopShortcut &&
                                                 PendingEmergencyStopShortcut is not null;

    public bool CanCancelEmergencyStopShortcut => !IsApplyingEmergencyStopShortcut &&
                                                  (IsRecordingEmergencyStopShortcut ||
                                                   PendingEmergencyStopShortcut is not null);

    public ObservableCollection<InputTaskLogEntry> LogEntries { get; } = [];

    public void SetEmergencyStopReady(bool ready, string? errorMessage = null)
    {
        F8Ready = ready;
        if (!ready && !string.IsNullOrWhiteSpace(errorMessage))
        {
            ActionError = errorMessage;
            EmergencyStopShortcutMessage = errorMessage;
        }

        OnPropertyChanged(nameof(EmergencyStopStatus));
        OnPropertyChanged(nameof(EmergencyStopRegistrationStatus));
    }

    [RelayCommand(CanExecute = nameof(CanStartInput))]
    private async Task StartAsync()
    {
        ActionError = string.Empty;
        try
        {
            await SaveSettingsAsync();
            await _taskManager.StartAsync(
                Text,
                new InputOptions { IntervalMilliseconds = IntervalMilliseconds },
                CountdownSeconds);
        }
        catch (Exception exception)
        {
            _logger.Warning(exception, "界面启动输入任务失败。");
            ActionError = exception.Message;
        }
    }

    [RelayCommand(CanExecute = nameof(CanPauseInput))]
    private void Pause() => _taskManager.Pause();

    [RelayCommand(CanExecute = nameof(CanResumeInput))]
    private void Resume() => _taskManager.Resume();

    [RelayCommand(CanExecute = nameof(CanStopInput))]
    private async Task StopAsync()
    {
        ActionError = string.Empty;
        try
        {
            if (IsSelectingTarget)
            {
                _selectionCancellation?.Cancel();
                SelectionMessage = "正在取消目标选择…";
                return;
            }

            await _taskManager.StopAsync();
        }
        catch (Exception exception)
        {
            _logger.Warning(exception, "界面停止输入任务失败。");
            ActionError = exception.Message;
        }
    }

    [RelayCommand(CanExecute = nameof(CanSelectTarget))]
    private async Task SelectTargetAsync()
    {
        ActionError = string.Empty;
        IsSelectingTarget = true;
        _selectionCancellation = new CancellationTokenSource();
        CancellationToken selectionToken = _selectionCancellation.Token;
        SelectionMessage = "应用正在最小化，请在倒计时内点击目标窗口。";
        _windowService.Minimize();

        try
        {
            for (int remaining = 3; remaining > 0; remaining--)
            {
                TargetSelectionCountdown = remaining;
                SelectionMessage = $"{remaining} 秒后识别前台窗口，请立即切换到目标程序。";
                await Task.Delay(TimeSpan.FromSeconds(1), _timeProvider, selectionToken);
                selectionToken.ThrowIfCancellationRequested();
            }

            TargetWindowInfo target = _targetWindowService.CaptureForeground();
            if (target.Handle == _windowService.Handle)
            {
                throw new InvalidOperationException("识别到的是 Auto Typer Pro 自身，请切换到其他目标窗口后重试。");
            }

            _taskManager.SetTarget(target);
            SelectionMessage = $"已选择：{target.Title}（{target.ProcessName}）。开始输入后仍需在倒计时内切换到该窗口。";
        }
        catch (OperationCanceledException) when (_selectionCancellation?.IsCancellationRequested == true)
        {
            SelectionMessage = "目标选择已取消。";
        }
        catch (Exception exception)
        {
            _logger.Warning(exception, "目标窗口选择失败。");
            ActionError = exception.Message;
            SelectionMessage = "目标窗口选择失败，请重试。";
        }
        finally
        {
            TargetSelectionCountdown = 0;
            IsSelectingTarget = false;
            _selectionCancellation?.Dispose();
            _selectionCancellation = null;
            _windowService.RestoreAndActivate();
        }
    }

    [RelayCommand]
    private void EmergencyStop() => _taskManager.EmergencyStop(EmergencyStopShortcut.ToString());

    [RelayCommand(CanExecute = nameof(CanConfigureEmergencyStopShortcut))]
    private void RecordEmergencyStopShortcut()
    {
        ActionError = string.Empty;
        PendingEmergencyStopShortcut = null;
        IsRecordingEmergencyStopShortcut = true;
        EmergencyStopShortcutMessage = "正在等待快捷键输入，请按下例如 Ctrl + Shift + F8 的组合键；按 Esc 取消。";
    }

    [RelayCommand(CanExecute = nameof(CanApplyEmergencyStopShortcut))]
    private Task ApplyEmergencyStopShortcutAsync() =>
        ApplyEmergencyStopShortcutCoreAsync(PendingEmergencyStopShortcut);

    [RelayCommand(CanExecute = nameof(CanConfigureEmergencyStopShortcut))]
    private Task RestoreDefaultEmergencyStopShortcutAsync() =>
        ApplyEmergencyStopShortcutCoreAsync(EmergencyStopShortcut.Default);

    [RelayCommand(CanExecute = nameof(CanCancelEmergencyStopShortcut))]
    private void CancelEmergencyStopShortcut()
    {
        PendingEmergencyStopShortcut = null;
        IsRecordingEmergencyStopShortcut = false;
        EmergencyStopShortcutMessage = "已取消快捷键设置，当前快捷键未改变。";
    }

    public void CaptureEmergencyStopShortcut(Key key, ModifierKeys modifiers)
    {
        if (!IsRecordingEmergencyStopShortcut)
        {
            return;
        }

        if (key == Key.Escape)
        {
            CancelEmergencyStopShortcut();
            return;
        }

        int virtualKey = KeyInterop.VirtualKeyFromKey(key);
        CaptureEmergencyStopShortcut(new EmergencyStopShortcut(ToEmergencyStopModifiers(modifiers), virtualKey));
    }

    public void CaptureEmergencyStopShortcut(EmergencyStopShortcut shortcut)
    {
        if (!IsRecordingEmergencyStopShortcut)
        {
            return;
        }

        try
        {
            shortcut.Validate();
        }
        catch (ArgumentOutOfRangeException exception)
        {
            EmergencyStopShortcutMessage = $"无法录入该快捷键：{exception.Message} 仅支持 F1–F24，可配合 Ctrl、Alt、Shift 或 Win。";
            return;
        }

        PendingEmergencyStopShortcut = shortcut;
        IsRecordingEmergencyStopShortcut = false;
        ActionError = string.Empty;
        EmergencyStopShortcutMessage = $"已录入 {shortcut}，点击“保存并应用”完成设置。";
    }

    [RelayCommand]
    private void ClearLog() => LogEntries.Clear();

    private async Task ApplyEmergencyStopShortcutCoreAsync(EmergencyStopShortcut? shortcut)
    {
        if (shortcut is null || IsApplyingEmergencyStopShortcut || IsTaskActive)
        {
            return;
        }

        IsApplyingEmergencyStopShortcut = true;
        IsRecordingEmergencyStopShortcut = false;
        ActionError = string.Empty;
        bool shortcutRegistered = false;

        try
        {
            shortcut.Validate();
            _emergencyStopService.ChangeShortcut(shortcut);
            shortcutRegistered = true;
            EmergencyStopShortcut = shortcut;
            _settingsService.Current.EmergencyStopShortcut = shortcut;
            await _settingsService.SaveAsync();
            PendingEmergencyStopShortcut = null;
            SetEmergencyStopReady(true);
            EmergencyStopShortcutMessage = $"已应用并保存 {shortcut}。";
            StatusMessage = $"紧急停止快捷键已更新为 {shortcut}。";
        }
        catch (Win32Exception exception)
        {
            SetEmergencyStopReady(_emergencyStopService.IsRegistered);
            string message = $"{exception.Message}\n请更换组合键，建议尝试 Ctrl + F8 或 Ctrl + Shift + F8。";
            EmergencyStopShortcutMessage = message;
            ActionError = message;
            _logger.Warning(exception, "紧急停止热键注册或切换失败。");
        }
        catch (Exception exception)
        {
            SetEmergencyStopReady(_emergencyStopService.IsRegistered);
            string message = shortcutRegistered
                ? $"快捷键 {shortcut} 已注册，但配置保存失败：{exception.Message}"
                : $"快捷键设置未完成：{exception.Message}";
            EmergencyStopShortcutMessage = message;
            ActionError = message;
            _logger.Warning(exception, "紧急停止快捷键设置失败。");
        }
        finally
        {
            IsApplyingEmergencyStopShortcut = false;
        }
    }

    private static EmergencyStopModifiers ToEmergencyStopModifiers(ModifierKeys modifiers)
    {
        EmergencyStopModifiers result = EmergencyStopModifiers.None;

        if (modifiers.HasFlag(ModifierKeys.Control))
        {
            result |= EmergencyStopModifiers.Control;
        }

        if (modifiers.HasFlag(ModifierKeys.Alt))
        {
            result |= EmergencyStopModifiers.Alt;
        }

        if (modifiers.HasFlag(ModifierKeys.Shift))
        {
            result |= EmergencyStopModifiers.Shift;
        }

        if (modifiers.HasFlag(ModifierKeys.Windows))
        {
            result |= EmergencyStopModifiers.Win;
        }

        return result;
    }

    public Task SaveSettingsAsync()
    {
        _settingsService.Current.DefaultInputIntervalMilliseconds = IntervalMilliseconds;
        _settingsService.Current.DefaultCountdownSeconds = CountdownSeconds;
        _settingsService.Current.EmergencyStopShortcut = EmergencyStopShortcut;
        return _settingsService.SaveAsync();
    }

    public async Task ShutdownAsync()
    {
        _selectionCancellation?.Cancel();
        if (_taskManager.IsActive)
        {
            try
            {
                await _taskManager.StopAsync();
            }
            catch (Exception exception)
            {
                _logger.Warning(exception, "关闭应用时停止输入任务失败。");
            }
        }

        try
        {
            await SaveSettingsAsync();
        }
        catch (Exception exception)
        {
            _logger.Warning(exception, "关闭应用时保存设置失败。");
        }

        Dispose();
    }

    public void Dispose()
    {
        _taskManager.PropertyChanged -= OnTaskManagerPropertyChanged;
        _taskManager.CommandStateChanged -= OnTaskManagerCommandStateChanged;
        _taskManager.LogRecorded -= OnTaskLogRecorded;
    }

    partial void OnTextChanged(string value)
    {
        UpdateStatistics();
        StartCommand.NotifyCanExecuteChanged();
    }

    partial void OnIsSelectingTargetChanged(bool value)
    {
        OnPropertyChanged(nameof(CanEditText));
        OnPropertyChanged(nameof(CanStartInput));
        OnPropertyChanged(nameof(CanSelectTarget));
        OnPropertyChanged(nameof(CanStopInput));
        StartCommand.NotifyCanExecuteChanged();
        SelectTargetCommand.NotifyCanExecuteChanged();
        StopCommand.NotifyCanExecuteChanged();
        NotifyEmergencyStopShortcutCommands();
    }

    partial void OnEmergencyStopShortcutChanged(EmergencyStopShortcut value)
    {
        OnPropertyChanged(nameof(EmergencyStopShortcutDisplay));
        OnPropertyChanged(nameof(EmergencyStopButtonText));
        OnPropertyChanged(nameof(EmergencyStopHelpText));
        OnPropertyChanged(nameof(EmergencyStopStatus));
        OnPropertyChanged(nameof(EmergencyStopRegistrationStatus));
    }

    partial void OnPendingEmergencyStopShortcutChanged(EmergencyStopShortcut? value)
    {
        OnPropertyChanged(nameof(PendingEmergencyStopShortcutDisplay));
        NotifyEmergencyStopShortcutCommands();
    }

    partial void OnIsRecordingEmergencyStopShortcutChanged(bool value) => NotifyEmergencyStopShortcutCommands();

    partial void OnIsApplyingEmergencyStopShortcutChanged(bool value) => NotifyEmergencyStopShortcutCommands();

    partial void OnActionErrorChanged(string value) => OnPropertyChanged(nameof(HasActionError));

    private void NotifyEmergencyStopShortcutCommands()
    {
        OnPropertyChanged(nameof(CanStartInput));
        OnPropertyChanged(nameof(CanSelectTarget));
        OnPropertyChanged(nameof(CanConfigureEmergencyStopShortcut));
        OnPropertyChanged(nameof(CanApplyEmergencyStopShortcut));
        OnPropertyChanged(nameof(CanCancelEmergencyStopShortcut));
        StartCommand.NotifyCanExecuteChanged();
        SelectTargetCommand.NotifyCanExecuteChanged();
        RecordEmergencyStopShortcutCommand.NotifyCanExecuteChanged();
        ApplyEmergencyStopShortcutCommand.NotifyCanExecuteChanged();
        RestoreDefaultEmergencyStopShortcutCommand.NotifyCanExecuteChanged();
        CancelEmergencyStopShortcutCommand.NotifyCanExecuteChanged();
    }

    private void UpdateStatistics()
    {
        TextStatistics statistics = TextStatistics.Analyze(Text);
        StatisticsText = $"{statistics.RuneCount} 个码点 · UTF-16 {statistics.Utf16Length} · {statistics.LineCount} 行 · Tab {statistics.TabCount}"
            + (statistics.EndsWithLineBreak ? " · 保留末尾换行" : string.Empty);
    }

    private void OnTaskManagerPropertyChanged(object? sender, PropertyChangedEventArgs eventArgs) => SyncFromTaskManager();

    private void OnTaskManagerCommandStateChanged(object? sender, EventArgs eventArgs) => NotifyCommandStates();

    private void OnTaskLogRecorded(object? sender, InputTaskLogEntry entry)
    {
        if (LogEntries.Count >= 200)
        {
            LogEntries.RemoveAt(0);
        }

        LogEntries.Add(entry);
    }

    private void SyncFromTaskManager()
    {
        StateText = _taskManager.StateText;
        StatusMessage = _taskManager.StatusMessage;
        TargetTitle = _taskManager.Target?.Title ?? "尚未选择";
        TargetProcessName = _taskManager.Target is { } target
            ? $"{target.ProcessName} · 已绑定窗口句柄"
            : "点击“选择目标窗口”后，在倒计时内切换到目标程序";
        ProgressText = _taskManager.ProgressText;
        ProgressPercent = _taskManager.ProgressPercent;
        ElapsedText = _taskManager.ElapsedText;
        EstimatedRemainingText = _taskManager.EstimatedRemainingText;
        CountdownDisplay = _taskManager.CountdownSecondsRemaining > 0
            ? $"{_taskManager.CountdownSecondsRemaining} 秒"
            : "—";
        VerificationText = _taskManager.VerificationText;
        NotifyCommandStates();
        OnPropertyChanged(nameof(IsTaskActive));
        OnPropertyChanged(nameof(CanEditText));
    }

    private void NotifyCommandStates()
    {
        OnPropertyChanged(nameof(CanStartInput));
        OnPropertyChanged(nameof(CanPauseInput));
        OnPropertyChanged(nameof(CanResumeInput));
        OnPropertyChanged(nameof(CanStopInput));
        OnPropertyChanged(nameof(CanSelectTarget));
        StartCommand.NotifyCanExecuteChanged();
        PauseCommand.NotifyCanExecuteChanged();
        ResumeCommand.NotifyCanExecuteChanged();
        StopCommand.NotifyCanExecuteChanged();
        SelectTargetCommand.NotifyCanExecuteChanged();
        EmergencyStopCommand.NotifyCanExecuteChanged();
        NotifyEmergencyStopShortcutCommands();
    }
}

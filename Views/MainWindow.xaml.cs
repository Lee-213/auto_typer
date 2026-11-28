using System.Collections.Specialized;
using System.ComponentModel;
using System.Windows;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Threading;
using AutoTyperPro.App.ViewModels;
using AutoTyperPro.Core.Interfaces;
using Serilog;

namespace AutoTyperPro.App.Views;

public partial class MainWindow : Window
{
    private readonly MainViewModel _viewModel;
    private readonly IEmergencyStopService _emergencyStopService;
    private HwndSource? _hwndSource;
    private bool _isClosing;

    public MainWindow(MainViewModel viewModel, IEmergencyStopService emergencyStopService)
    {
        InitializeComponent();
        _viewModel = viewModel;
        _emergencyStopService = emergencyStopService;
        DataContext = viewModel;
        _viewModel.LogEntries.CollectionChanged += OnLogEntriesCollectionChanged;
    }

    protected override void OnSourceInitialized(EventArgs e)
    {
        base.OnSourceInitialized(e);
        _hwndSource = HwndSource.FromHwnd(new WindowInteropHelper(this).EnsureHandle());
        _hwndSource?.AddHook(HandleWindowMessage);
    }

    protected override void OnPreviewKeyDown(KeyEventArgs e)
    {
        if (!_viewModel.IsRecordingEmergencyStopShortcut)
        {
            base.OnPreviewKeyDown(e);
            return;
        }

        e.Handled = true;
        Key key = e.Key == Key.System ? e.SystemKey : e.Key;
        _viewModel.CaptureEmergencyStopShortcut(key, Keyboard.Modifiers);
    }

    protected override void OnPreviewKeyUp(KeyEventArgs e)
    {
        if (_viewModel.IsRecordingEmergencyStopShortcut)
        {
            e.Handled = true;
        }

        base.OnPreviewKeyUp(e);
    }

    protected override async void OnClosing(CancelEventArgs eventArgs)
    {
        if (_isClosing)
        {
            base.OnClosing(eventArgs);
            return;
        }

        eventArgs.Cancel = true;
        _isClosing = true;
        await _viewModel.ShutdownAsync();

        try
        {
            _emergencyStopService.Stop();
        }
        catch (Exception exception)
        {
            Log.Warning(exception, "关闭窗口时注销紧急停止热键失败。");
        }

        Close();
    }

    protected override void OnClosed(EventArgs e)
    {
        _viewModel.LogEntries.CollectionChanged -= OnLogEntriesCollectionChanged;
        _hwndSource?.RemoveHook(HandleWindowMessage);
        _hwndSource = null;
        base.OnClosed(e);
    }

    private IntPtr HandleWindowMessage(IntPtr hwnd, int msg, IntPtr wParam, IntPtr lParam, ref bool handled)
    {
        if (_viewModel.IsRecordingEmergencyStopShortcut &&
            _emergencyStopService.IsCurrentHotKeyMessage(msg, wParam))
        {
            handled = true;
            _viewModel.CaptureEmergencyStopShortcut(_emergencyStopService.CurrentShortcut);
            return IntPtr.Zero;
        }

        if (_emergencyStopService.HandleHotKeyMessage(msg, wParam))
        {
            handled = true;
        }

        return IntPtr.Zero;
    }

    private void OnLogEntriesCollectionChanged(object? sender, NotifyCollectionChangedEventArgs eventArgs)
    {
        Dispatcher.BeginInvoke(DispatcherPriority.ContextIdle, new Action(ScrollLogToEnd));
    }

    private void ScrollLogToEnd()
    {
        LogScrollViewer?.ScrollToEnd();
    }
}

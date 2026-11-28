using System.Windows;
using System.Windows.Interop;

namespace AutoTyperPro.App.Services;

public sealed class ApplicationWindowService : IApplicationWindowService
{
    private Window? MainWindow => Application.Current?.MainWindow;

    public IntPtr Handle
    {
        get
        {
            Window? window = MainWindow;
            return window is null ? IntPtr.Zero : new WindowInteropHelper(window).EnsureHandle();
        }
    }

    public void Minimize()
    {
        if (MainWindow is { } window)
        {
            window.WindowState = WindowState.Minimized;
        }
    }

    public void RestoreAndActivate()
    {
        if (MainWindow is not { } window)
        {
            return;
        }

        window.WindowState = WindowState.Normal;
        window.Activate();
    }
}

using System.IO;
using System.Windows;
using AutoTyperPro.App.Services;
using AutoTyperPro.App.ViewModels;
using AutoTyperPro.App.Views;
using AutoTyperPro.Core.Configuration;
using AutoTyperPro.Core.Interfaces;
using AutoTyperPro.Core.Input;
using AutoTyperPro.Core.Services;
using AutoTyperPro.Windows;
using AutoTyperPro.Windows.Keyboard;
using Microsoft.Extensions.DependencyInjection;
using Serilog;

namespace AutoTyperPro.App;

public partial class App : Application
{
    private ServiceProvider? _serviceProvider;

    protected override async void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        ConfigureLogging();

        try
        {
            ServiceCollection services = new();
            services.AddSingleton(Log.Logger);
            services.AddSingleton<IApplicationSettingsService>(provider =>
                new ApplicationSettingsService(AppDataPaths.SettingsFilePath, provider.GetRequiredService<ILogger>()));
            services.AddSingleton(TimeProvider.System);
            services.AddSingleton<ITargetWindowService, TargetWindowService>();
            services.AddSingleton<IApplicationWindowService, ApplicationWindowService>();
            services.AddSingleton<SendInputSink>();
            services.AddSingleton<TargetWindowInputGuard>(provider =>
                new TargetWindowInputGuard(
                    provider.GetRequiredService<SendInputSink>(),
                    provider.GetRequiredService<ITargetWindowService>()));
            services.AddSingleton<IKeyboardEventSink>(provider => provider.GetRequiredService<TargetWindowInputGuard>());
            services.AddSingleton<ITargetWindowGuard>(provider => provider.GetRequiredService<TargetWindowInputGuard>());
            services.AddSingleton<IEmergencyStopService, EmergencyStopService>();
            services.AddSingleton<IKeyboardInputEngine, KeyboardInputEngine>();
            services.AddSingleton<InputTaskManager>();
            services.AddSingleton<MainViewModel>();
            services.AddSingleton<MainWindow>();

            _serviceProvider = services.BuildServiceProvider();
            await _serviceProvider.GetRequiredService<IApplicationSettingsService>().LoadAsync();

            MainWindow window = _serviceProvider.GetRequiredService<MainWindow>();
            MainWindow = window;
            window.Show();

            MainViewModel viewModel = _serviceProvider.GetRequiredService<MainViewModel>();
            IEmergencyStopService emergencyStopService = _serviceProvider.GetRequiredService<IEmergencyStopService>();
            IApplicationWindowService windowService = _serviceProvider.GetRequiredService<IApplicationWindowService>();
            try
            {
                emergencyStopService.Start(windowService.Handle, viewModel.EmergencyStopShortcut);
                viewModel.SetEmergencyStopReady(true);
            }
            catch (Exception exception)
            {
                Log.Warning(exception, "紧急停止热键注册失败。");
                viewModel.SetEmergencyStopReady(
                    false,
                    $"紧急停止热键注册失败：{exception.Message}\n请在“安全保护”区域重新设置，建议尝试 Ctrl + F8 或 Ctrl + Shift + F8。");
            }

            Log.Information("Auto Typer Pro {Version} 启动完成。", GetVersion());
        }
        catch (Exception exception)
        {
            Log.Fatal(exception, "应用程序启动失败。");
            MessageBox.Show(
                $"应用程序启动失败：{exception.Message}\n\n请检查日志目录：{AppDataPaths.LogDirectory}",
                "Auto Typer Pro",
                MessageBoxButton.OK,
                MessageBoxImage.Error);
            Shutdown(1);
        }
    }

    protected override void OnExit(ExitEventArgs e)
    {
        try
        {
            _serviceProvider?.GetRequiredService<IEmergencyStopService>().Stop();
        }
        catch (Exception exception)
        {
            Log.Warning(exception, "注销紧急停止热键失败。");
        }

        _serviceProvider?.Dispose();
        Log.CloseAndFlush();
        base.OnExit(e);
    }

    private static void ConfigureLogging()
    {
        Log.Logger = new LoggerConfiguration().MinimumLevel.Information().CreateLogger();

        try
        {
            Directory.CreateDirectory(AppDataPaths.LogDirectory);
            Log.Logger = new LoggerConfiguration()
                .MinimumLevel.Information()
                .WriteTo.File(
                    Path.Combine(AppDataPaths.LogDirectory, "autotyperpro-.log"),
                    rollingInterval: RollingInterval.Day,
                    retainedFileCountLimit: 14,
                    flushToDiskInterval: TimeSpan.FromSeconds(1),
                    outputTemplate: "{Timestamp:yyyy-MM-dd HH:mm:ss.fff zzz} [{Level:u3}] {Message:lj}{NewLine}{Exception}")
                .CreateLogger();
        }
        catch (Exception exception)
        {
            Log.Warning(exception, "文件日志初始化失败，本次运行不会写入日志文件。");
            MessageBox.Show(
                $"无法创建日志目录，本次运行不会记录文件日志：{exception.Message}",
                "Auto Typer Pro",
                MessageBoxButton.OK,
                MessageBoxImage.Warning);
        }
    }

    private static string GetVersion() =>
        typeof(App).Assembly.GetName().Version?.ToString(3) ?? "0.1.0";
}

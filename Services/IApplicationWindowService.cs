namespace AutoTyperPro.App.Services;

public interface IApplicationWindowService
{
    IntPtr Handle { get; }

    void Minimize();

    void RestoreAndActivate();
}

using System;
using System.IO;
using Backer.Desktop.Services;
using Backer.Desktop.ViewModels;
using Xunit;

namespace Backer.Desktop.Tests;

public sealed class NavigationTests : IDisposable
{
    private readonly string _temp = Directory.CreateTempSubdirectory("backer-nav").FullName;

    public void Dispose() => Directory.Delete(_temp, recursive: true);

    private MainWindowViewModel Shell() => new(new AppServices
    {
        Config = new ConfigStore(Path.Combine("Fixtures", "config.yaml")),
        Data = new DataDirStore(_temp),
    });

    [Fact]
    public void ViewInstancesAreRetainedAcrossNavigation()
    {
        var shell = Shell();
        var home = shell.CurrentView;

        shell.Navigate("restore");
        Assert.IsType<RestoreViewModel>(shell.CurrentView);
        Assert.Equal("Restore", shell.Status.Subtitle);
        var restore = shell.CurrentView;

        shell.Navigate("home");
        Assert.Same(home, shell.CurrentView);
        shell.Navigate("restore");
        Assert.Same(restore, shell.CurrentView);
    }

    [Fact]
    public void EscapeReturnsHomeAndUnknownKeysAreIgnored()
    {
        var shell = Shell();
        shell.Navigate("settings");
        shell.Navigate("nope");
        Assert.IsType<SettingsViewModel>(shell.CurrentView);

        shell.GoHome();
        Assert.IsType<HomeViewModel>(shell.CurrentView);
    }

    [Fact]
    public void EnterIsANoOpWhenTheViewHasNoPrimaryCommand()
    {
        var shell = Shell();
        shell.Navigate("settings");
        shell.InvokePrimary();
        Assert.IsType<SettingsViewModel>(shell.CurrentView);
    }

    [Fact]
    public void StartOpensWelcomeOnlyWhenThereIsNothingConfigured()
    {
        var configured = Shell();
        configured.Start();
        Assert.IsType<HomeViewModel>(configured.CurrentView);

        var empty = new MainWindowViewModel(new AppServices
        {
            Config = new ConfigStore(Path.Combine(_temp, "config.yaml")),
            Data = new DataDirStore(_temp),
        });
        empty.Start();
        Assert.IsType<WelcomeViewModel>(empty.CurrentView);
    }
}
